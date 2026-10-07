"""Cross check the audit trail against compressors, supply and quota.

The runtime audit is append only, so a compressor started without a supply
record, or a quota add that never reached the ledger, stays invisible until
someone compares the three sources. The reconciler walks the trail once,
groups entries by operation kind, and cross checks the projection against
the compressor bank, the supply valves and the quota accumulator. Every run
is itself recorded as a ``reconcile`` entry, so the ledger holds the raw
records, the verdict over them and the reason for every difference.

Runs are periodic, not per operation: mid request states are transiently
inconsistent by design (the valve opens before the compressor starts), so
checking after every mutation would flag noise. A run is due every
``stride`` control cycles and can also be triggered directly.
"""

from __future__ import annotations

import time
import uuid

from coldstore.audit import Auditor, Entry, parse_detail
from coldstore.comp import Bank
from coldstore.quota import EPOCH_CAP, Accumulator
from coldstore.valve import SupplyValve

from .report import RECONCILE_KIND, Diff, Report

CYCLE_KIND = "cycle"
DEFAULT_STRIDE = 10
DEFAULT_BUDGET_MS = 50.0
SCAN_CHUNK = 256
EVIDENCE_LIMIT = 3
QUOTA_TOLERANCE = 1e-6


def _advance_epoch(value: float, amount: float) -> float:
    """Replay one accumulator add, including its single wrap and rounding."""

    next_value = round(value + amount, 4)
    if EPOCH_CAP > 0 and next_value >= EPOCH_CAP:
        next_value = round(next_value - EPOCH_CAP, 4)
    return next_value


def _evidence(*id_lists: list[str]) -> tuple[str, ...]:
    """Keep the tail of each id list so a diff points at its raw records."""

    evidence: list[str] = []
    for ids in id_lists:
        evidence.extend(ids[-EVIDENCE_LIMIT:])
    return tuple(evidence)


class Reconciler:
    """Groups audit entries by kind and cross checks the three sources."""

    def __init__(
        self,
        auditor: Auditor,
        bank: Bank,
        supply: SupplyValve,
        accumulator: Accumulator,
        stride: int = DEFAULT_STRIDE,
        budget_ms: float = DEFAULT_BUDGET_MS,
    ) -> None:
        self._auditor = auditor
        self._bank = bank
        self._supply = supply
        self._accumulator = accumulator
        self._stride = max(1, stride)
        self._budget_ms = budget_ms

    def due(self) -> bool:
        """Whether enough control cycles passed since the last run."""

        return self._cycles_since_last_run(self._auditor.entries()) >= self._stride

    def maybe_run(self) -> Report | None:
        """Run only when the stride says a run is due."""

        entries = self._auditor.entries()
        if self._cycles_since_last_run(entries) < self._stride:
            return None
        return self._run(entries)

    def run(self) -> Report:
        """Run a full reconciliation pass now, regardless of the stride."""

        return self._run(self._auditor.entries())

    def latest(self) -> dict[str, object] | None:
        """The most recent reconcile report, or none when never run."""

        for entry in reversed(self._auditor.entries()):
            if entry.kind == RECONCILE_KIND:
                detail = parse_detail(entry.detail)
                if detail:
                    return detail
        return None

    def describe(self) -> str:
        latest = self.latest()
        if latest is None:
            return "reconcile never_run"
        diffs = latest.get("diffs", [])
        count = len(diffs) if isinstance(diffs, list) else 0
        return f"reconcile ok={latest.get('ok')} diffs={count}"

    def _run(self, entries: list[Entry]) -> Report:
        started = time.monotonic()
        by_kind: dict[str, int] = {}
        opens: dict[str, list[str]] = {}
        closes: dict[str, list[str]] = {}
        starts: dict[str, list[str]] = {}
        stops: dict[str, list[str]] = {}
        quota_value = 0.0
        quota_adds = 0
        complete = True
        scanned = 0
        for entry in entries:
            by_kind[entry.kind] = by_kind.get(entry.kind, 0) + 1
            detail = parse_detail(entry.detail)
            comp = str(detail.get("comp", ""))
            if entry.kind == "valve.open" and comp:
                opens.setdefault(comp, []).append(entry.id)
            elif entry.kind == "valve.close" and comp:
                closes.setdefault(comp, []).append(entry.id)
            elif entry.kind == "comp.start" and comp:
                starts.setdefault(comp, []).append(entry.id)
            elif entry.kind == "comp.stop" and comp:
                stops.setdefault(comp, []).append(entry.id)
            elif entry.kind == "quota.reset":
                quota_value = 0.0
                quota_adds = 0
            elif entry.kind == "quota.add":
                amount = detail.get("amount")
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    quota_value = _advance_epoch(quota_value, float(amount))
                    quota_adds += 1
            scanned += 1
            if scanned % SCAN_CHUNK == 0 and self._over_budget(started):
                complete = False
                break

        diffs: list[Diff] = []
        if complete:
            diffs.extend(self._pairing_diffs(starts, opens, "comp.start", "valve.open"))
            diffs.extend(self._pairing_diffs(stops, closes, "comp.stop", "valve.close"))
            actual = self._accumulator.value()
            if abs(quota_value - actual) > QUOTA_TOLERANCE:
                diffs.append(
                    Diff(
                        check="quota_replay",
                        reason=(
                            f"audited quota replays to {quota_value:.4f} over "
                            f"{quota_adds} adds but accumulator holds {actual:.4f}"
                        ),
                    )
                )
        diffs.extend(self._state_diffs())

        duration_ms = (time.monotonic() - started) * 1000.0
        within_budget = duration_ms <= self._budget_ms
        if not complete:
            diffs.append(
                Diff(
                    check="budget",
                    reason=(
                        f"scan truncated at {scanned} entries after "
                        f"{duration_ms:.3f}ms over budget {self._budget_ms:.3f}ms"
                    ),
                )
            )
        elif not within_budget:
            diffs.append(
                Diff(
                    check="budget",
                    reason=(
                        f"scan of {scanned} entries took {duration_ms:.3f}ms "
                        f"over budget {self._budget_ms:.3f}ms"
                    ),
                )
            )

        report = Report(
            id=uuid.uuid4().hex,
            at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ok=not diffs,
            complete=complete,
            within_budget=within_budget,
            duration_ms=round(duration_ms, 3),
            entries_scanned=scanned,
            by_kind=by_kind,
            diffs=diffs,
        )
        self._auditor.record_op(RECONCILE_KIND, **report.as_dict())
        return report

    def _pairing_diffs(
        self,
        left: dict[str, list[str]],
        right: dict[str, list[str]],
        left_kind: str,
        right_kind: str,
    ) -> list[Diff]:
        """Flag compressors whose paired operation records do not line up."""

        diffs: list[Diff] = []
        for comp in sorted(set(left) | set(right)):
            left_ids = left.get(comp, [])
            right_ids = right.get(comp, [])
            if len(left_ids) == len(right_ids):
                continue
            diffs.append(
                Diff(
                    check="record_pairing",
                    comp=comp,
                    reason=(
                        f"{comp} has {len(left_ids)} {left_kind} records but "
                        f"{len(right_ids)} {right_kind} records"
                    ),
                    evidence=_evidence(left_ids, right_ids),
                )
            )
        return diffs

    def _state_diffs(self) -> list[Diff]:
        """Cross check live compressor state against live supply state."""

        diffs: list[Diff] = []
        for compressor in self._bank.compressors():
            if compressor.running and not self._supply.is_open(compressor.id):
                diffs.append(
                    Diff(
                        check="running_without_supply",
                        comp=compressor.id,
                        reason=(
                            f"{compressor.id} running at stage {compressor.stage} "
                            "with supply closed"
                        ),
                    )
                )
        for comp_id in self._supply.opened_ids():
            if self._bank.compressor(comp_id) is None:
                diffs.append(
                    Diff(
                        check="supply_open_unknown_comp",
                        comp=comp_id,
                        reason=f"supply open for unregistered compressor {comp_id}",
                    )
                )
        return diffs

    def _over_budget(self, started: float) -> bool:
        return (time.monotonic() - started) * 1000.0 > self._budget_ms

    @staticmethod
    def _cycles_since_last_run(entries: list[Entry]) -> int:
        cycles = 0
        for entry in reversed(entries):
            if entry.kind == RECONCILE_KIND:
                break
            if entry.kind == CYCLE_KIND:
                cycles += 1
        return cycles
