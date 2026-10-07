"""Append only audit journal for every control decision.

Entries are no longer a bare time ordered list. Each entry carries an
``op_kind`` (the operation family), a ``corr`` correlation id that ties the
compressor, supply valve and quota legs of one control cycle together, and a
numeric ``amount`` where the operation moved a quantity. That lets a
reconciliation group entries by operation kind and cross check the unit,
supply and quota sides of the same operation.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass

from coldstore.store.commands import append_command, load_commands
from coldstore.store.store import Store

from .report import AuditState

AUDIT_KEY = "audit:entries"

# Operation families the reconciliation knows how to group and cross check.
COMP_START = "comp_start"
COMP_STOP = "comp_stop"
COMP_STAGE = "comp_stage"
SUPPLY_OPEN = "supply_open"
SUPPLY_CLOSE = "supply_close"
QUOTA_ADD = "quota_add"
QUOTA_RESET = "quota_reset"
CYCLE = "cycle"

COMP_KINDS = (COMP_START, COMP_STOP, COMP_STAGE)
SUPPLY_KINDS = (SUPPLY_OPEN, SUPPLY_CLOSE)
QUOTA_KINDS = (QUOTA_ADD, QUOTA_RESET)
PAIRED_KINDS = (COMP_START, COMP_STOP, SUPPLY_OPEN, SUPPLY_CLOSE)


def now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class Entry:
    """One recorded control decision.

    ``op_kind`` groups the operation family, ``corr`` ties together every leg
    of one operation and ``amount`` carries the numeric quantity (stage change
    or quota amount). Legacy entries that only stored a detail string still
    load with empty ``corr`` and no ``amount``.
    """

    id: str
    time: str
    kind: str
    detail: str
    corr: str = ""
    amount: float | None = None
    target: str = ""

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": self.id,
            "time": self.time,
            "kind": self.kind,
            "detail": self.detail,
            "corr": self.corr,
            "target": self.target,
        }
        if self.amount is not None:
            payload["amount"] = self.amount
        return payload


@dataclass(frozen=True)
class EventRecord:
    """One journal entry that has not been persisted yet.

    Operation builders return these so a whole control cycle can append its
    several legs in a single journal write instead of rewriting the whole
    store document once per leg.
    """

    kind: str
    detail: str = ""
    corr: str = ""
    amount: float | None = None
    target: str = ""

    def to_entry(self, entry_id: str, stamp: str) -> Entry:
        return Entry(
            id=entry_id,
            time=stamp,
            kind=self.kind,
            detail=self.detail,
            corr=self.corr,
            amount=self.amount,
            target=self.target,
        )


def new_correlation() -> str:
    return uuid.uuid4().hex


class Auditor:
    """Records and queries runtime journal entries."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def record(self, kind: str, detail: str) -> Entry:
        """Backwards compatible single entry append."""

        return self.append(EventRecord(kind=kind, detail=detail))

    def append(self, event: EventRecord) -> Entry:
        entry = event.to_entry(uuid.uuid4().hex, now_stamp())
        append_command(self._store, AUDIT_KEY, json.dumps(entry.as_dict(), ensure_ascii=False))
        return entry

    def record_many(self, events: list[EventRecord]) -> list[Entry]:
        """Append several legs of one operation in a single store write."""

        if not events:
            return []
        stamp = now_stamp()
        entries = [event.to_entry(uuid.uuid4().hex, stamp) for event in events]
        raw = load_commands(self._store, AUDIT_KEY)
        raw.extend(json.dumps(entry.as_dict(), ensure_ascii=False) for entry in entries)
        self._store.put(AUDIT_KEY, json.dumps(raw, ensure_ascii=False))
        return entries

    def entries(self) -> list[Entry]:
        return self._load_entries()

    def _load_entries(self) -> list[Entry]:
        entries: list[Entry] = []
        for item in load_commands(self._store, AUDIT_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            amount = payload.get("amount")
            try:
                parsed_amount = float(amount) if amount is not None else None
            except (TypeError, ValueError):
                parsed_amount = None
            entries.append(
                Entry(
                    id=str(payload.get("id", "")),
                    time=str(payload.get("time", "")),
                    kind=str(payload.get("kind", "")),
                    detail=str(payload.get("detail", "")),
                    corr=str(payload.get("corr", "")),
                    amount=parsed_amount,
                    target=str(payload.get("target", "")),
                )
            )
        return entries

    def raw_count(self) -> int:
        """Count the raw stored rows including rows that fail to parse."""

        return len(load_commands(self._store, AUDIT_KEY))

    def last(self) -> Entry | None:
        entries = self.entries()
        if not entries:
            return None
        return entries[-1]

    def count(self) -> int:
        return len(self.entries())

    def count_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self.entries():
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return counts

    def filter(self, kind: str) -> list[Entry]:
        return [entry for entry in self.entries() if entry.kind == kind]

    def grouped(self) -> dict[str, list[Entry]]:
        """Every entry grouped by operation family in first seen order."""

        groups: dict[str, list[Entry]] = {}
        for entry in self.entries():
            groups.setdefault(entry.kind, []).append(entry)
        return groups

    def kinds(self) -> list[str]:
        seen: list[str] = []
        for entry in self.entries():
            if entry.kind not in seen:
                seen.append(entry.kind)
        return seen

    def totals(self) -> dict[str, float]:
        """Sum the numeric amount recorded for each entry kind.

        Prefers the structured ``amount`` field and falls back to a numeric
        ``detail`` for legacy entries.
        """

        totals: dict[str, float] = {}
        for entry in self.entries():
            value = entry.amount
            if value is None:
                try:
                    value = float(entry.detail)
                except ValueError:
                    continue
            totals[entry.kind] = totals.get(entry.kind, 0.0) + value
        return totals

    def state(self) -> AuditState:
        entries = self.entries()
        return AuditState(entries=[entry.as_dict() for entry in entries], count=len(entries))

    def describe(self) -> str:
        return f"audit entries={len(self.entries())}"
