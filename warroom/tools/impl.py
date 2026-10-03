"""Implementations of the 9 allow-listed tools, in the Registry signature fn(ctx, args) -> dict.

Read/compute tools never change anything. Write tools only record intent: a schedule version is
activated in the store, a cure-time suggestion is recorded on the running job, a notification goes
out on the bus. No tool writes to a PLC or an actuator, and none may be added.

Output keys follow the contracts' *Data models, plus the extra keys the agent reads
(solve_schedule: `schedule_version`; read_sensors summary: `<channel>_now`)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from warroom import factory as F
from warroom.contracts import (
    Channel,
    ConfirmScheduleArgs,
    CureFormulaArgs,
    NotifyTeamsArgs,
    PredictArgs,
    QueryOrdersShiftsArgs,
    ReadSensorsArgs,
    ScheduleStatus,
    SetCureTimeArgs,
    SolveRequest,
    SolveScheduleArgs,
    SopSearchArgs,
    WsType,
)
from warroom.tools.cure import arrhenius_cure

if TYPE_CHECKING:
    from warroom.tools.registry import ToolContext

MAX_SAMPLES = 180          # downsample history to ≤ 180 points per machine (≤ 1 per 10 s for 30 min)
CHANNELS = [c for c in Channel]


class ToolError(RuntimeError):
    """A tool could not do its job (missing dependency, unknown id). Registry turns it into ok=False."""


def _need(obj: Any, what: str) -> Any:
    if obj is None:
        raise ToolError(f"{what} unavailable in this tool context")
    return obj


def _check_machine(mid: str) -> None:
    if mid not in F.MACHINE_BY_ID:
        raise ToolError(f"unknown machine id {mid!r}")


def _now(ctx: ToolContext) -> datetime:
    if ctx.clock is not None:
        return ctx.clock.now()
    if ctx.sim is not None and getattr(ctx.sim, "clock", None) is not None:
        return ctx.sim.clock.now()
    raise ToolError("no clock in tool context")


# ------------------------------------------------------------------ read / compute
def read_sensors(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    a = ReadSensorsArgs.model_validate(args)
    sim = _need(ctx.sim, "factory sim")
    readings: dict[str, list[dict[str, Any]]] = {}
    summary: dict[str, dict[str, float]] = {}
    for mid in a.machine_ids:
        _check_machine(mid)
        latest = sim.machine(mid).last_reading
        hist = list(sim.history(mid, a.minutes)) if a.minutes > 0 else []
        if latest is not None and (not hist or hist[-1].ts < latest.ts):
            hist.append(latest)
        step = max(1, math.ceil(len(hist) / MAX_SAMPLES))
        sampled = hist[::step]
        if hist and sampled[-1] is not hist[-1]:
            sampled.append(hist[-1])
        readings[mid] = [r.model_dump(mode="json") for r in sampled]
        s: dict[str, float] = {}
        for ch in CHANNELS:
            vals = [(r.ts, getattr(r, ch.value)) for r in hist if getattr(r, ch.value) is not None]
            if not vals:
                continue
            key = "temp_c" if ch is Channel.TEMP else ch.value
            s[f"{key}_now"] = round(vals[-1][1], 2)
            if ch is Channel.TEMP:
                s["temp_c_min"] = round(min(v for _, v in vals), 2)
                s["temp_c_max"] = round(max(v for _, v in vals), 2)
                span_min = (vals[-1][0] - vals[0][0]).total_seconds() / 60.0
                s["trend_c_per_min"] = round((vals[-1][1] - vals[0][1]) / span_min, 3) if span_min > 0 else 0.0
        summary[mid] = s
    return {"readings": readings, "summary": summary}


def predict_anomaly(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    a = PredictArgs.model_validate(args)
    _check_machine(a.machine_id)
    det = _need(ctx.detector, "detector")
    p = det.predict(a.machine_id, a.channel, horizon_s=a.horizon_min * 60.0)
    return p.model_dump(mode="json")


def cure_formula(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return arrhenius_cure(CureFormulaArgs.model_validate(args)).model_dump(mode="json")


def sop_search(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    SopSearchArgs.model_validate(args)
    from warroom.kb.tools import sop_search_tool
    return sop_search_tool(ctx, args)


def query_orders_shifts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Orders/shifts DB. With a live sim (the plant's truth) the answer comes from the sim's orders
    and shifts plus the ACTIVE schedule's upcoming jobs, so it agrees with what the solver sees.
    With ctx.orders_db (or no sim) it reads the kb's Excel-backed OrdersDB."""
    a = QueryOrdersShiftsArgs.model_validate(args)
    if getattr(ctx, "orders_db", None) is not None or ctx.sim is None:
        from warroom.kb.tools import query_orders_shifts_tool
        return query_orders_shifts_tool(ctx, args)
    from warroom.schedule.staffing import gap_label
    for p in a.press_ids or []:
        _check_machine(p)
    now = _now(ctx)
    t_from = a.from_ts or now - timedelta(hours=12)
    t_to = a.to_ts or now + timedelta(days=3)
    sim = ctx.sim
    orders = [o for o in sim.orders() if not a.order_ids or o.id in a.order_ids]
    shifts = [s for s in sim.shifts() if s.end > t_from and s.start < t_to]
    gaps, gap_details = [], []
    for s in sorted(shifts, key=lambda s: s.start):
        if s.required is not None and s.headcount < s.required and s.end > now:
            short = s.required - s.headcount
            gaps.append(gap_label(s, short))
            gap_details.append({"shift_id": s.id, "team": s.team.value, "team_label": F.TEAM_LABELS.get(s.team, s.team.value),
                                "name": s.name, "start": s.start.isoformat(), "headcount": s.headcount,
                                "required": s.required, "short": short})
    next_jobs = []
    if ctx.store is not None:
        try:
            jobs = ctx.store.current().schedule.jobs
        except Exception:  # noqa: BLE001 - empty store
            jobs = []
        wanted = set(a.press_ids or [])
        for press in sorted({j.press_id for j in jobs}):
            if wanted and press not in wanted:
                continue
            upcoming = sorted((j for j in jobs if j.press_id == press and j.mold_in > now and not j.frozen),
                              key=lambda j: j.mold_in)
            next_jobs += upcoming[:2]
        if a.order_ids:
            next_jobs = [j for j in next_jobs if j.order_id in a.order_ids]
        next_jobs.sort(key=lambda j: j.mold_in)
    return {"orders": [o.model_dump(mode="json") for o in orders],
            "shifts": [s.model_dump(mode="json") for s in shifts],
            "gaps": gaps,
            "next_jobs": [j.model_dump(mode="json") for j in next_jobs],
            "gap_details": gap_details}


def solve_schedule(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Solve against the ACTIVE schedule (CP-SAT, heuristic backup) and store the result as a
    PROPOSED version. Activation happens only through confirm_schedule after approval."""
    a = SolveScheduleArgs.model_validate(args)
    for p in [*a.unavailable_presses, *a.cure_overrides_h]:
        if p not in F.CURE_PRESS_IDS:
            raise ToolError(f"{p!r} is not a curing press")
    sim = _need(ctx.sim, "factory sim")
    scheduler = ctx.scheduler
    if scheduler is None:
        from warroom.schedule import FallbackScheduler
        scheduler = FallbackScheduler()
    now = _now(ctx)
    orders, shifts = list(sim.orders()), list(sim.shifts())
    if ctx.store is not None:
        from warroom.schedule import reschedule
        res, ver = reschedule(ctx.store, scheduler, now, orders, shifts,
                              unavailable_presses=a.unavailable_presses, cure_overrides_h=a.cure_overrides_h,
                              time_limit_s=a.time_limit_s, allow_overtime=a.allow_overtime,
                              allow_cross_team=a.allow_cross_team, horizon_days=a.horizon_days)
        version = ver.version if ver is not None else None   # proposal only: no bus publish (worker thread)
    else:
        res = scheduler.solve(SolveRequest(now=now, orders=orders, shifts=shifts,
                                           unavailable_presses=a.unavailable_presses,
                                           cure_overrides_h=a.cure_overrides_h, horizon_days=a.horizon_days,
                                           time_limit_s=a.time_limit_s, allow_overtime=a.allow_overtime,
                                           allow_cross_team=a.allow_cross_team))
        version = None
    out = res.model_dump(mode="json")
    out["schedule_version"] = version
    return out


# ------------------------------------------------------------------ write (record intent only)
def confirm_schedule(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    a = ConfirmScheduleArgs.model_validate(args)
    store = _need(ctx.store, "schedule store")
    prev = store.current().version
    target = store.get(a.schedule_version)
    if target.status is not ScheduleStatus.PROPOSED:
        raise ToolError(f"schedule v{a.schedule_version} is {target.status.value}, not proposed")
    ver = store.activate(a.schedule_version)
    if ctx.bus is not None:
        ctx.bus.publish(WsType.SCHEDULE, ver)
    return {"version": ver.version, "status": ver.status.value, "previous_version": prev, "reason": a.reason}


def set_cure_time(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Record the approved cure-time suggestion on the running job (recipe value, not a PLC write)."""
    a = SetCureTimeArgs.model_validate(args)
    if a.press_id not in F.CURE_PRESS_IDS:
        raise ToolError(f"{a.press_id!r} is not a curing press")
    if not 0 < a.total_cure_h <= 96:
        raise ToolError(f"total_cure_h {a.total_cure_h} out of range")
    applied = False
    hook = getattr(ctx.sim, "set_cure_required", None) if ctx.sim is not None else None
    planned = None
    if callable(hook):
        job = hook(a.press_id, a.total_cure_h)
        applied = True
        planned = job.planned_demold_at.isoformat() if getattr(job, "planned_demold_at", None) else None
    return {"press_id": a.press_id, "total_cure_h": a.total_cure_h, "reason": a.reason, "recorded": applied,
            "planned_demold_at": planned}


def notify_teams(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    a = NotifyTeamsArgs.model_validate(args)
    if a.machine_id is not None:
        _check_machine(a.machine_id)
    now = _now(ctx)
    payload = {"teams": [t.value for t in a.teams], "team_labels": [F.TEAM_LABELS.get(t, t.value) for t in a.teams],
               "kind": a.kind.value, "message": a.message, "machine_id": a.machine_id, "sent_at": now.isoformat(),
               "channel": "factory-lan"}
    return {"delivered": payload["teams"], **payload}
