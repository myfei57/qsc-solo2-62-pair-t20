"""Reconciliation projections."""

from __future__ import annotations

from dataclasses import dataclass

RECONCILE_KIND = "reconcile"


@dataclass(frozen=True)
class Diff:
    """One inconsistency found between the audit trail and live state."""

    check: str
    reason: str
    comp: str = ""
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "reason": self.reason,
            "comp": self.comp,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class Report:
    """The outcome of one reconciliation run."""

    id: str
    at: str
    ok: bool
    complete: bool
    within_budget: bool
    duration_ms: float
    entries_scanned: int
    by_kind: dict[str, int]
    diffs: list[Diff]

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "at": self.at,
            "ok": self.ok,
            "complete": self.complete,
            "within_budget": self.within_budget,
            "duration_ms": self.duration_ms,
            "entries_scanned": self.entries_scanned,
            "by_kind": dict(self.by_kind),
            "diffs": [diff.as_dict() for diff in self.diffs],
        }
