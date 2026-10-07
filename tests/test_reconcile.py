"""Tests for grouping and the compressor/supply/quota reconciliation."""

from __future__ import annotations

import json
import tempfile
import unittest

from coldstore.audit import (
    COMP_START,
    CYCLE,
    EventRecord,
    QUOTA_ADD,
    QUOTA_RESET,
    SUPPLY_OPEN,
    Auditor,
    Reconciler,
    flow,
    new_correlation,
)
from coldstore.comp import Bank
from coldstore.quota import Accumulator
from coldstore.quota.accumulator import EPOCH_CAP
from coldstore.reporting import reconcile_export
from coldstore.store import Store
from coldstore.valve import SupplyValve


class ReconcileCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(self.store)
        self.bank = Bank()
        self.bank.add_compressor("c1", 4)
        self.supply = SupplyValve(self.store)
        self.accumulator = Accumulator(self.store)
        self.reconciler = Reconciler(
            self.store, self.auditor, self.bank, self.supply, self.accumulator
        )

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_grouped_by_operation_kind(self) -> None:
        corr = new_correlation()
        start, _ = flow.comp_start_records("c1", corr=corr)
        opened, _ = flow.supply_open_records("c1", corr=corr)
        added, _ = flow.quota_add_records(3.0, corr=corr)
        self.auditor.record_many(start + opened + added)

        groups = self.auditor.grouped()
        self.assertEqual(set(groups), {COMP_START, SUPPLY_OPEN, QUOTA_ADD})
        for entries in groups.values():
            self.assertTrue(all(entry.corr == corr for entry in entries))
        self.assertEqual(self.auditor.totals()[QUOTA_ADD], 3.0)

    def test_clean_sequenced_cycle_reconciles(self) -> None:
        records = flow.sequenced_start(self.bank, self.supply, "c1")
        value, quota_records = flow.quota_add(self.accumulator, 10.0)
        self.assertEqual(value, 10.0)
        records.extend(quota_records)
        records.append(EventRecord(CYCLE, detail="-5.0000"))
        self.auditor.record_many(records)

        run = self.reconciler.run()
        self.assertTrue(run.ok)
        self.assertEqual(run.findings, [])
        self.assertEqual(run.quota_journal, 10.0)
        self.assertEqual(run.quota_actual, 10.0)
        self.assertFalse(self.reconciler.has_open_errors())

    def test_running_compressor_with_closed_supply_is_error(self) -> None:
        # Journal a clean supply sequenced start, then close the valve out of
        # band so the live unit/supply states diverge.
        self.auditor.record_many(flow.sequenced_start(self.bank, self.supply, "c1"))
        self.supply.close_valve("c1")

        run = self.reconciler.run()
        codes = {finding.code for finding in run.findings}
        self.assertIn("comp_running_supply_closed", codes)
        self.assertTrue(any(f.severity == "error" for f in run.findings))
        self.assertTrue(self.reconciler.has_open_errors())

    def test_open_supply_with_stopped_compressor_is_warning(self) -> None:
        # Prime the line without starting the compressor.
        self.supply.open_valve("c1")
        records, _ = flow.supply_open_records("c1")
        self.auditor.record_many(records)

        run = self.reconciler.run()
        codes = {finding.code for finding in run.findings}
        self.assertIn("supply_open_comp_stopped", codes)
        self.assertTrue(run.ok)  # warnings do not fail the run

    def test_compressor_leg_without_supply_leg_is_flagged(self) -> None:
        # A compressor started directly, journaled with no supply leg under
        # the same correlation.
        self.bank.start("c1")
        self.supply.open_valve("c1")
        records, _ = flow.comp_start_records("c1")
        self.auditor.record_many(records)

        run = self.reconciler.run()
        codes = {finding.code for finding in run.findings}
        self.assertIn("comp_without_supply", codes)
        finding = next(f for f in run.findings if f.code == "comp_without_supply")
        # The raw source record rides along in the finding.
        self.assertEqual(finding.records[0]["kind"], COMP_START)

    def test_quota_wraps_and_stays_consistent(self) -> None:
        first, _ = flow.quota_add_records(60.0)
        second, _ = flow.quota_add_records(70.0)  # 60 + 70 wraps at 100 -> 30
        self.accumulator.add(60.0)
        wrapped = self.accumulator.add(70.0)
        self.assertEqual(wrapped, 30.0)
        self.auditor.record_many(first + second)

        run = self.reconciler.run()
        quota_findings = [f for f in run.findings if f.scope == "quota"]
        self.assertEqual(quota_findings, [])
        self.assertEqual(run.quota_journal, 30.0)
        self.assertEqual(run.quota_actual, 30.0)

    def test_quota_divergence_is_detected(self) -> None:
        # Journal says 10 but the accumulator was advanced out of band to 12.
        records, _ = flow.quota_add_records(10.0)
        self.auditor.record_many(records)
        self.accumulator.add(12.0)

        run = self.reconciler.run()
        codes = {finding.code for finding in run.findings}
        self.assertIn("quota_mismatch", codes)
        self.assertTrue(self.reconciler.has_open_errors())

    def test_quota_reset_rebaselines_the_roll(self) -> None:
        add_a, _ = flow.quota_add_records(90.0)
        self.accumulator.add(90.0)
        reset = flow.quota_reset(self.accumulator)
        add_b, _ = flow.quota_add_records(5.0)
        self.accumulator.add(5.0)
        self.auditor.record_many(add_a + reset + add_b)

        run = self.reconciler.run()
        self.assertTrue(run.ok)
        self.assertEqual(run.quota_journal, 5.0)
        self.assertEqual(run.quota_actual, 5.0)

    def test_incremental_cursor_only_scans_new_rows(self) -> None:
        records_a, _ = flow.quota_add_records(10.0)
        self.accumulator.add(10.0)
        self.auditor.record_many(records_a)
        first = self.reconciler.run()
        self.assertEqual(first.cursor_before, 0)
        self.assertGreater(first.cursor_after, 0)

        records_b, _ = flow.quota_add_records(5.0)
        self.accumulator.add(5.0)
        self.auditor.record_many(records_b)
        second = self.reconciler.run()
        self.assertEqual(second.cursor_before, first.cursor_after)
        self.assertEqual(second.cursor_after, first.cursor_after + 1)
        self.assertEqual(second.quota_journal, 15.0)

    def test_run_is_appended_to_ledger_and_exported(self) -> None:
        self.reconciler.run()
        self.reconciler.run()
        state = self.reconciler.state()
        self.assertEqual(len(state.runs), 2)
        self.assertEqual(state.last.cursor_after, state.cursor)

        columns, rows = reconcile_export(self.reconciler)
        self.assertEqual(columns[0], "time")
        self.assertIn("findings", columns)
        self.assertEqual(len(rows), 2)

    def test_new_findings_are_journaled_and_deduplicated(self) -> None:
        self.supply.open_valve("c1")
        records, _ = flow.supply_open_records("c1")
        self.auditor.record_many(records)

        first = self.reconciler.run()
        self.assertTrue(first.findings)
        kinds_after_first = set(self.auditor.count_by_kind())
        self.assertIn("reconcile_finding", kinds_after_first)

        before = self.auditor.count()
        second = self.reconciler.run()
        self.assertTrue(second.findings)
        # The same open finding must not be re-journaled on every run.
        self.assertEqual(self.auditor.count(), before)

    def test_corrupt_journal_row_is_flagged(self) -> None:
        self.store.put("audit:entries", json.dumps(["not-json", "2"]))
        run = self.reconciler.run()
        codes = {finding.code for finding in run.findings}
        self.assertIn("journal_corrupt", codes)


class EpochConstantTest(unittest.TestCase):
    def test_wrap_cap_is_the_accumulator_cap(self) -> None:
        self.assertEqual(EPOCH_CAP, 100.0)


if __name__ == "__main__":
    unittest.main()
