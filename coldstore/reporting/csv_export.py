"""CSV rendering for operator exports."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from coldstore.audit import parse_detail
from coldstore.reconcile.report import RECONCILE_KIND

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from coldstore.audit import Auditor
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


def audit_export(auditor: Auditor) -> tuple[list[str], list[list[str]]]:
    """Columns and rows for the runtime audit trail."""

    columns = ["time", "kind", "detail", "id"]
    rows = [[entry.time, entry.kind, entry.detail, entry.id] for entry in auditor.entries()]
    return columns, rows


def reconcile_export(auditor: Auditor) -> tuple[list[str], list[list[object]]]:
    """Columns and rows flattening every reconciliation run and its diffs."""

    columns = ["run_id", "at", "check", "comp", "reason", "evidence"]
    rows: list[list[object]] = []
    for entry in auditor.entries():
        if entry.kind != RECONCILE_KIND:
            continue
        diffs = parse_detail(entry.detail).get("diffs", [])
        if not diffs:
            rows.append([entry.id, entry.time, "ok", "", "no diffs", ""])
            continue
        for diff in diffs:
            if not isinstance(diff, dict):
                continue
            evidence = diff.get("evidence", [])
            joined = ";".join(str(item) for item in evidence) if isinstance(evidence, list) else ""
            rows.append(
                [
                    entry.id,
                    entry.time,
                    str(diff.get("check", "")),
                    str(diff.get("comp", "")),
                    str(diff.get("reason", "")),
                    joined,
                ]
            )
    return columns, rows


def telemetry_export(telemetry: Telemetry) -> tuple[list[str], list[list[object]]]:
    """Columns and rows for the telemetry counters."""

    return ["metric", "value"], [[key, value] for key, value in telemetry.as_dict().items()]
