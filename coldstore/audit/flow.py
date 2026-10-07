"""Operation builders that journal the unit, supply and quota legs together.

Each mutating control operation is recorded with an ``op_kind`` so the journal
can be grouped by operation family, and with a correlation id so the
reconciliation can cross check the compressor, supply valve and quota legs of
the same operation against each other.

A full supply sequenced cycle records a ``comp_*`` leg and a ``supply_*`` leg
under one correlation. Quota movements are recorded with their amount. The
builders only *construct* :class:`EventRecord` rows; the caller decides when
to flush them so a cycle performs a single journal write.
"""

from __future__ import annotations

from coldstore.comp import Bank
from coldstore.quota import Accumulator
from coldstore.valve import OPEN, SupplyValve

from .auditor import (
    COMP_STAGE,
    COMP_START,
    COMP_STOP,
    EventRecord,
    QUOTA_ADD,
    QUOTA_RESET,
    SUPPLY_CLOSE,
    SUPPLY_OPEN,
    new_correlation,
)

# Amounts persisted by the quota accumulator are rounded to four decimals, so
# the cross check against the journal uses the same tolerance.
QUOTA_TOLERANCE = 1e-4


def comp_start_records(comp_id: str, corr: str = "") -> tuple[list[EventRecord], str]:
    corr = corr or new_correlation()
    return [EventRecord(COMP_START, target=comp_id, corr=corr)], corr


def comp_stop_records(comp_id: str, corr: str = "") -> tuple[list[EventRecord], str]:
    corr = corr or new_correlation()
    return [EventRecord(COMP_STOP, target=comp_id, corr=corr)], corr


def comp_stage_records(comp_id: str, stage: int, corr: str = "") -> tuple[list[EventRecord], str]:
    corr = corr or new_correlation()
    return [
        EventRecord(
            COMP_STAGE,
            detail=f"stage={stage}",
            target=comp_id,
            corr=corr,
            amount=float(stage),
        )
    ], corr


def supply_open_records(comp_id: str, corr: str = "") -> tuple[list[EventRecord], str]:
    corr = corr or new_correlation()
    return [EventRecord(SUPPLY_OPEN, detail=OPEN, target=comp_id, corr=corr)], corr


def supply_close_records(comp_id: str, corr: str = "") -> tuple[list[EventRecord], str]:
    corr = corr or new_correlation()
    return [EventRecord(SUPPLY_CLOSE, detail="closed", target=comp_id, corr=corr)], corr


def quota_add_records(amount: float, corr: str = "") -> tuple[list[EventRecord], str]:
    corr = corr or new_correlation()
    return [
        EventRecord(
            QUOTA_ADD,
            detail=f"amount={amount:.4f}",
            corr=corr,
            amount=amount,
        )
    ], corr


def quota_reset_records(corr: str = "") -> tuple[list[EventRecord], str]:
    corr = corr or new_correlation()
    return [EventRecord(QUOTA_RESET, detail="reset", corr=corr)], corr


def sequenced_start(bank: Bank, supply: SupplyValve, comp_id: str) -> list[EventRecord]:
    """Supply sequenced start: open the valve first, then start the unit.

    Only legs that actually change state are recorded, which keeps a repeated
    start from fabricating a supply leg that never happened.
    """

    corr = new_correlation()
    was_open = supply.is_open(comp_id)
    was_running = bank.compressor(comp_id) is not None and bank.stage(comp_id)[0] > 0

    supply.open_valve(comp_id)
    bank.start(comp_id)

    records: list[EventRecord] = []
    if not was_open:
        records.append(EventRecord(SUPPLY_OPEN, detail=OPEN, target=comp_id, corr=corr))
    if not was_running:
        records.append(EventRecord(COMP_START, target=comp_id, corr=corr))
    return records


def sequenced_stop(bank: Bank, supply: SupplyValve, comp_id: str) -> list[EventRecord]:
    """Supply sequenced stop: stop the unit first, then close the valve."""

    corr = new_correlation()
    was_running = bank.compressor(comp_id) is not None and bank.stage(comp_id)[0] > 0
    was_open = supply.is_open(comp_id)

    bank.stop(comp_id)
    supply.close_valve(comp_id)

    records: list[EventRecord] = []
    if was_running:
        records.append(EventRecord(COMP_STOP, target=comp_id, corr=corr))
    if was_open:
        records.append(EventRecord(SUPPLY_CLOSE, detail="closed", target=comp_id, corr=corr))
    return records


def quota_add(accumulator: Accumulator, amount: float) -> tuple[float, list[EventRecord]]:
    """Advance the accumulator and journal the movement under one correlation."""

    value = accumulator.add(amount)
    records, _ = quota_add_records(amount)
    return value, records


def quota_reset(accumulator: Accumulator) -> list[EventRecord]:
    accumulator.reset()
    records, _ = quota_reset_records()
    return records
