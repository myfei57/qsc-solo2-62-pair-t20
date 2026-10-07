"""CSV rendering for operator exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor, Reconciler
    from coldstore.console.telemetry import Telemetry

QUOTE_TRIGGERS = (",", '"', "\n")


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    if any(trigger in text for trigger in QUOTE_TRIGGERS):
        return '"' + text.replace('"', '""') + '"'
    return text


def to_csv(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a header row and body rows as comma separated text."""

    lines = [",".join(_escape(column) for column in columns)]
    for row in rows:
        lines.append(",".join(_escape(value) for value in row))
    return "\n".join(lines) + "\n"


def audit_export(auditor: Auditor) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the runtime audit trail.

    The correlation and target columns let an operator tie the compressor,
    supply and quota legs of one operation together; amount carries the
    numeric quantity for the kinds that move one.
    """

    columns = ["time", "kind", "target", "amount", "corr", "detail", "id"]
    rows: list[list[object]] = []
    for entry in auditor.entries():
        rows.append(
            [
                entry.time,
                entry.kind,
                entry.target,
                "" if entry.amount is None else entry.amount,
                entry.corr,
                entry.detail,
                entry.id,
            ]
        )
    return columns, rows


def reconcile_export(reconciler: Reconciler) -> tuple[list[str], list[list[object]]]:
    """One row per reconciliation run, flattened for operator reconciliation."""

    columns = [
        "time",
        "id",
        "trigger",
        "cursor_before",
        "cursor_after",
        "journal_rows",
        "quota_journal",
        "quota_actual",
        "ok",
        "findings",
    ]
    rows: list[list[object]] = []
    for run in reconciler.runs():
        rows.append(
            [
                run.time,
                run.id,
                run.trigger,
                run.cursor_before,
                run.cursor_after,
                run.journal_rows,
                run.quota_journal,
                run.quota_actual,
                run.ok,
                "; ".join(f"[{finding.severity}] {finding.code}: {finding.reason}" for finding in run.findings),
            ]
        )
    return columns, rows


def telemetry_export(telemetry: Telemetry) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the telemetry counters."""

    return ["metric", "value"], [[key, value] for key, value in telemetry.as_dict().items()]
