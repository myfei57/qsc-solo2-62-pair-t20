"""One full refrigeration control cycle."""

from __future__ import annotations

from coldstore.ammonia import validate_ppm
from coldstore.audit import CYCLE, CYCLE_TRIGGER, EventRecord, flow, new_correlation
from coldstore.quota import validate_amount
from coldstore.temp import validate_temp

from .http import Request, Response, json_response
from .runtime import Runtime


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the whole control line once, in production order."""

    room_id = request.str_field("room_id")
    comp_id = request.str_field("comp_id")
    temp = request.float_field("temp")
    ppm = request.float_field("ppm")
    amount = request.float_field("amount")
    defrost = request.int_field("defrost")

    validate_temp(temp)
    validate_ppm(ppm)
    validate_amount(amount)

    corr = new_correlation()
    verdict = rt.temp.record(temp)
    ammonia = rt.ammonia.detect(ppm)
    tripped = False
    records: list[EventRecord] = []
    if ammonia.trip:
        rt.interlock.trip(f"ammonia {ammonia.calibrated:.2f} ppm")
        if comp_id:
            records.extend(flow.sequenced_stop(rt.compressors, rt.supply, comp_id))
        tripped = True
    else:
        if comp_id:
            if rt.load.is_high(temp):
                stage = rt.compressors.stage_up(comp_id)
            else:
                stage = rt.compressors.stage_down(comp_id)
            stage_records, _ = flow.comp_stage_records(comp_id, stage, corr=corr)
            records.extend(stage_records)
            records.extend(flow.sequenced_start(rt.compressors, rt.supply, comp_id))
        if defrost and room_id:
            rt.defrost.start(room_id)
        elif room_id:
            rt.defrost.finish(room_id)

    quota_value = rt.accumulator.add(amount)
    quota_records, _ = flow.quota_add_records(amount, corr=corr)
    records.extend(quota_records)
    records.append(EventRecord(CYCLE, detail=f"{temp:.4f}", corr=corr))
    rt.auditor.record_many(records)

    # Batch reconciliation at the end of every production cycle, incremental
    # so it stays inside the performance budget as the journal grows.
    reconcile = rt.reconciler.run(trigger=CYCLE_TRIGGER)

    return json_response(
        {
            "temp": verdict.value,
            "rate": verdict.rate,
            "within_window": verdict.within,
            "ammonia_level": ammonia.level,
            "tripped": tripped,
            "stage": rt.compressors.stage(comp_id)[0] if comp_id else 0,
            "supply_open": rt.supply.is_open(comp_id) if comp_id else False,
            "defrosting": rt.defrost.defrosting(),
            "quota": quota_value,
            "cooling_load": rt.load.load_for(temp),
            "audit_count": rt.auditor.count(),
            "reconcile_ok": reconcile.ok,
            "reconcile_findings": len(reconcile.findings),
        }
    )
