"""Reconciliation projections."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Finding:
    """One cross check discrepancy.

    ``severity`` is ``error`` for a hard mismatch the operators must act on and
    ``warn`` for degraded journal evidence. ``reason`` explains the gap and
    ``records`` carries the raw journal rows involved so the finding can be
    audited back to source.
    """

    code: str
    severity: str
    scope: str
    reason: str
    records: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "severity": self.severity,
            "scope": self.scope,
            "reason": self.reason,
            "records": self.records,
        }


@dataclass(frozen=True)
class ReconcileRun:
    """One batch reconciliation run appended to the audit ledger."""

    id: str
    time: str
    trigger: str
    cursor_before: int
    cursor_after: int
    journal_rows: int
    quota_journal: float
    quota_actual: float
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "time": self.time,
            "trigger": self.trigger,
            "cursor_before": self.cursor_before,
            "cursor_after": self.cursor_after,
            "journal_rows": self.journal_rows,
            "quota_journal": self.quota_journal,
            "quota_actual": self.quota_actual,
            "ok": self.ok,
            "findings": [finding.as_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class ReconcileState:
    """Ledger projection returned by the reconciliation endpoints."""

    runs: list[ReconcileRun]
    last: ReconcileRun | None
    cursor: int
    open_findings: list[Finding]
    error_count: int
    warn_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "runs": [run.as_dict() for run in self.runs],
            "last": self.last.as_dict() if self.last else None,
            "cursor": self.cursor,
            "open_findings": [finding.as_dict() for finding in self.open_findings],
            "error_count": self.error_count,
            "warn_count": self.warn_count,
        }
