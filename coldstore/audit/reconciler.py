"""Cross reconciliation of the compressor, supply and quota records.

The reconciliation groups the audit journal by operation kind and then cross
checks three views that must agree:

* unit (compressor bank live state) versus supply (valve live state) — a
  running compressor must have an open supply valve and vice versa;
* the journal legs of one operation (matched by correlation id) — a journaled
  compressor leg must carry a supply sequenced partner;
* the quota journal summed with the accumulator wrap versus the live
  accumulator value.

Runs are incremental: a cursor remembers how many journal rows were already
examined so pairing and quota summation only touch new rows. The live state
checks are O(number of compressors). Every run is appended to an append only
ledger and newly observed discrepancies are themselves written into the audit
journal so the trail shows what differed and why.
"""

from __future__ import annotations

import json
import uuid

from coldstore.comp import Bank
from coldstore.quota import Accumulator
from coldstore.quota.accumulator import EPOCH_CAP
from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store
from coldstore.valve import SupplyValve

from .auditor import (
    AUDIT_KEY,
    COMP_START,
    COMP_STOP,
    EventRecord,
    PAIRED_KINDS,
    QUOTA_ADD,
    QUOTA_RESET,
    SUPPLY_KINDS,
    now_stamp,
)
from .flow import QUOTA_TOLERANCE
from .reconcile_report import Finding, ReconcileRun, ReconcileState

RECONCILE_KEY = "audit:reconcile"

# Finding kinds written into the audit journal itself.
FINDING_KIND = "reconcile_finding"
CYCLE_TRIGGER = "cycle"
MANUAL_TRIGGER = "manual"


class Reconciler:
    """Runs batch cross checks and keeps the audit ledger."""

    def __init__(
        self,
        store: Store,
        auditor,
        bank: Bank,
        supply: SupplyValve,
        accumulator: Accumulator,
    ) -> None:
        self._store = store
        self._auditor = auditor
        self._bank = bank
        self._supply = supply
        self._accumulator = accumulator

    def run(self, trigger: str = MANUAL_TRIGGER) -> ReconcileRun:
        # Parse the journal once and reuse it for grouping, quota roll and
        # integrity checks instead of rescanning the whole document per check.
        entries = self._auditor.entries()
        cursor, quota_running = self._load_cursor()
        cursor_before = cursor
        new_entries = entries[cursor:]

        findings: list[Finding] = []
        findings.extend(self._cross_check_live())
        findings.extend(self._cross_check_pairing(new_entries))
        quota_running, quota_findings = self._roll_quota(new_entries, quota_running)
        findings.extend(quota_findings)
        if self._auditor.raw_count() != len(entries):
            findings.append(
                Finding(
                    code="journal_corrupt",
                    severity="warn",
                    scope=AUDIT_KEY,
                    reason=(
                        f"{self._auditor.raw_count() - len(entries)} audit rows failed to parse"
                    ),
                )
            )

        quota_actual = self._accumulator.value()
        if abs(quota_running - quota_actual) > QUOTA_TOLERANCE:
            findings.append(
                Finding(
                    code="quota_mismatch",
                    severity="error",
                    scope="quota",
                    reason=(
                        f"journal quota {quota_running:.4f} does not match accumulator "
                        f"{quota_actual:.4f}"
                    ),
                )
            )

        # Persist the new discrepancies into the journal first: they are
        # themselves auditable rows, so the cursor advances past them.
        self._record_new_findings(findings)
        cursor_after = self._auditor.count()

        run = ReconcileRun(
            id=uuid.uuid4().hex,
            time=now_stamp(),
            trigger=trigger,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            journal_rows=cursor_after,
            quota_journal=round(quota_running, 4),
            quota_actual=round(quota_actual, 4),
            findings=findings,
        )

        # The run carries the cursor and rolled quota, so it doubles as the
        # checkpoint and needs no extra (full document) cursor write.
        append_command(self._store, RECONCILE_KEY, json.dumps(run.as_dict(), ensure_ascii=False))
        return run

    def state(self) -> ReconcileState:
        runs = self.runs()
        last = runs[-1] if runs else None
        open_findings = last.findings if last else []
        return ReconcileState(
            runs=runs,
            last=last,
            cursor=self._load_cursor()[0],
            open_findings=open_findings,
            error_count=sum(1 for finding in open_findings if finding.severity == "error"),
            warn_count=sum(1 for finding in open_findings if finding.severity == "warn"),
        )

    def runs(self) -> list[ReconcileRun]:
        loaded: list[ReconcileRun] = []
        for item in load_commands(self._store, RECONCILE_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            findings = [
                Finding(
                    code=str(row.get("code", "")),
                    severity=str(row.get("severity", "")),
                    scope=str(row.get("scope", "")),
                    reason=str(row.get("reason", "")),
                    records=[dict(record) for record in row.get("records", []) if isinstance(record, dict)],
                )
                for row in payload.get("findings", [])
                if isinstance(row, dict)
            ]
            loaded.append(
                ReconcileRun(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    trigger=str(payload.get("trigger", "")),
                    cursor_before=int(payload.get("cursor_before", 0)),
                    cursor_after=int(payload.get("cursor_after", 0)),
                    journal_rows=int(payload.get("journal_rows", 0)),
                    quota_journal=float(payload.get("quota_journal", 0.0)),
                    quota_actual=float(payload.get("quota_actual", 0.0)),
                    findings=findings,
                )
            )
        return loaded

    def has_open_errors(self) -> bool:
        last = self.runs()[-1] if self.runs() else None
        return bool(last and any(f.severity == "error" for f in last.findings))

    # -- cross checks -------------------------------------------------------

    def _cross_check_live(self) -> list[Finding]:
        """Unit versus supply: running state and open valve must agree."""

        findings: list[Finding] = []
        compressor_ids = set(self._bank.compressor_ids())
        compressor_ids.update(self._supply.opened_ids())
        for comp_id in sorted(compressor_ids):
            stage, present = self._bank.stage(comp_id)
            running = present and stage > 0
            open_valve = self._supply.is_open(comp_id)
            if running and not open_valve:
                findings.append(
                    Finding(
                        code="comp_running_supply_closed",
                        severity="error",
                        scope=comp_id,
                        reason=f"compressor {comp_id} running at stage {stage} but supply valve closed",
                    )
                )
            elif open_valve and not running:
                findings.append(
                    Finding(
                        code="supply_open_comp_stopped",
                        severity="warn",
                        scope=comp_id,
                        reason=f"supply valve open for {comp_id} but the compressor is stopped",
                    )
                )
        return findings

    def _cross_check_pairing(self, new_entries) -> list[Finding]:
        """Every journaled compressor leg needs a supply leg in its correlation.

        Only compressor legs are directional: a lone valve open (line primed
        ahead of a start) is legitimate, but a journaled compressor movement
        is required to be supply sequenced.
        """

        by_corr: dict[str, dict[str, list]] = {}
        for entry in new_entries:
            if entry.kind not in PAIRED_KINDS or not entry.corr:
                continue
            sides = by_corr.setdefault(entry.corr, {"comp": [], "supply": []})
            if entry.kind in (COMP_START, COMP_STOP):
                sides["comp"].append(entry)
            elif entry.kind in SUPPLY_KINDS:
                sides["supply"].append(entry)

        findings: list[Finding] = []
        for corr, sides in by_corr.items():
            if sides["comp"] and not sides["supply"]:
                legs = sorted({entry.kind for entry in sides["comp"]})
                findings.append(
                    Finding(
                        code="comp_without_supply",
                        severity="warn",
                        scope=corr,
                        reason=(
                            "journaled compressor leg "
                            + ", ".join(legs)
                            + " has no supply valve leg under the same correlation"
                        ),
                        records=[entry.as_dict() for entry in sides["comp"]],
                    )
                )
        return findings

    def _roll_quota(self, new_entries, running: float) -> tuple[float, list[Finding]]:
        """Replay quota legs with the accumulator wrap from the cursor onward."""

        findings: list[Finding] = []
        for entry in new_entries:
            if entry.kind == QUOTA_RESET:
                running = 0.0
            elif entry.kind == QUOTA_ADD:
                if entry.amount is None:
                    findings.append(
                        Finding(
                            code="quota_bad_record",
                            severity="warn",
                            scope=entry.id,
                            reason=f"quota_add entry {entry.id} has no parseable amount",
                            records=[entry.as_dict()],
                        )
                    )
                    continue
                running += entry.amount
                if EPOCH_CAP > 0 and running >= EPOCH_CAP:
                    running -= EPOCH_CAP
        return running, findings

    # -- persistence --------------------------------------------------------

    def _record_new_findings(self, findings: list[Finding]) -> None:
        previous = {self._finding_key(f) for run in self.runs()[-1:] for f in run.findings}
        fresh = [finding for finding in findings if self._finding_key(finding) not in previous]
        if not fresh:
            return
        events = [
            EventRecord(
                kind=FINDING_KIND,
                detail=finding.reason,
                corr="",
                target=finding.scope,
            )
            for finding in fresh
        ]
        self._auditor.record_many(events)

    @staticmethod
    def _finding_key(finding: Finding) -> tuple[str, str, str]:
        return finding.code, finding.scope, finding.reason

    def _load_cursor(self) -> tuple[int, float]:
        """Checkpoint is the last appended run, so no extra key is needed."""

        runs = load_commands(self._store, RECONCILE_KEY)
        if not runs:
            return 0, 0.0
        try:
            payload = json.loads(runs[-1])
        except ValueError:
            return 0, 0.0
        if not isinstance(payload, dict):
            return 0, 0.0
        return int(payload.get("cursor_after", 0)), float(payload.get("quota_journal", 0.0))
