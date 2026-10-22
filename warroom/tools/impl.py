"""Implementations of the 9 allow-listed tools, in the Registry signature fn(ctx, args) -> dict.

Read/compute tools never change anything. Write tools only record intent: a schedule version is
activated in the store, a cure-time suggestion is recorded on the running job, a notification goes
out on the bus. No tool writes to a PLC or an actuator, and none may be added.

Output keys follow the contracts' *Data models, plus the extra keys the agent reads
(solve_schedule: `schedule_version`; read_sensors summary: `<channel>_now`)."""
from __future__ import annotations

import contextlib
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from warroom import factory as F
from warroom.contracts import (
    Channel,
    ConfirmScheduleArgs,
    CureFormulaArgs,
    MachineStatus,
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


# A cure press in one of these states takes no new mold-in, whatever the commander asked for. The
# same set guards the rollback endpoint (api.runtime.ROLLBACK_BLOCKING); the safety core's CURE_HOLD
# shows as STOPPED. Holds are also read from the store's holds.json (written by the Runtime) so a
# restart that re-enabled the press in the sim cannot reopen it.
BLOCKING_PRESS_STATUSES = (MachineStatus.OUT_OF_SERVICE, MachineStatus.STOPPED, MachineStatus.OFFLINE)
HOLDS_FILE = "holds.json"


def held_presses(ctx: ToolContext) -> dict[str, str]:
    """{cure press id: status value} for presses that must not get new mold-ins right now."""
    out: dict[str, str] = {}
    root = getattr(ctx.store, "root", None) if ctx.store is not None else None
    if root is not None:
        path = Path(root) / HOLDS_FILE
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                blocking = {st.value for st in BLOCKING_PRESS_STATUSES}
                for mid, h in (raw.items() if isinstance(raw, dict) else []):
                    if mid in F.CURE_PRESS_IDS and isinstance(h, dict) and h.get("status") in blocking:
                        out[mid] = h["status"]
        except Exception:  # noqa: BLE001, S110 - unreadable holds file: the sim status below still applies
            pass
    if ctx.sim is not None:
        for pid in F.CURE_PRESS_IDS:
            try:
                st = ctx.sim.machine(pid).status
            except Exception:  # noqa: BLE001, S112
                continue
            if st in BLOCKING_PRESS_STATUSES:
                out[pid] = MachineStatus(st).value
    return out


def future_jobs_on_presses(schedule: Any, presses: dict[str, str] | set[str], now: datetime) -> dict[str, int]:
    """{press: count} of not-frozen jobs with mold-in at/after now on the given presses."""
    hits: dict[str, int] = {}
    for j in schedule.jobs:
        if j.press_id in presses and not j.frozen and j.mold_in >= now:
            hits[j.press_id] = hits.get(j.press_id, 0) + 1
    return hits


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


def _norm_order(oid: Any) -> str:
    """An order id compared the way people write it: '#a-311', 'A311', ' SO-004 ' -> 'A-311' / 'SO-004'."""
    s = re.sub(r"\s", "", str(oid)).lstrip("#").upper()
    return re.sub(r"^([A-Z]+)(\d)", r"\1-\2", s)


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
    # issue #182: 「SO-004」「#so-004」「A-311」 name the same orders as the plant's ids (case, '#' and spaces ignored)
    want = {_norm_order(o) for o in a.order_ids or []}
    orders = [o for o in sim.orders() if not want or _norm_order(o.id) in want]
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
            next_jobs = [j for j in next_jobs if _norm_order(j.order_id) in want]
        next_jobs.sort(key=lambda j: j.mold_in)
    out = {"orders": [o.model_dump(mode="json") for o in orders],
           "shifts": [s.model_dump(mode="json") for s in shifts],
           "gaps": gaps,
           "next_jobs": [j.model_dump(mode="json") for j in next_jobs],
           "gap_details": gap_details}
    # issue #17: the plant's people and materials, read from the sim's world when it has one (read only):
    # who is on leave now, material stock against reorder points, and today's mold-ins
    world = getattr(sim, "world", None)
    if world is not None:
        try:
            out["leave"] = world.leave_now()
            out["materials"] = [{k: m[k] for k in ("key", "name", "unit", "stock", "reorder_point", "on_order", "status")}
                                for m in world.materials_status()]
            today_jobs = 0
            if ctx.store is not None:
                try:
                    today_jobs = sum(1 for j in ctx.store.current().schedule.jobs
                                     if j.mold_in.date() == now.date() and j.mold_in >= now)
                except Exception:  # noqa: BLE001
                    today_jobs = 0
            out["today"] = {"date": now.date().isoformat(), "on_duty": world.on_duty_count(), "mold_ins": today_jobs,
                            "due_within_3d": (now.date() + timedelta(days=3)).isoformat()}
        except Exception:  # noqa: BLE001 - the world is a view; the tool answers without it
            pass
    # issue #95: today's orders from ONE rule set (warroom/tools/orders_today.py), over the dashboard's order list
    # (seed + world), the ACTIVE schedule and the tyres in a press now; the sim-only `orders` above is kept as is
    try:
        from warroom.tools import orders_today
        out["orders_today"] = orders_today.collect(sim, ctx.store, now)
    except Exception as e:  # noqa: BLE001 - say so instead of leaving the agent to guess
        out["orders_today_error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def solve_schedule(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Solve against the ACTIVE schedule (CP-SAT, heuristic backup) and store the result as a
    PROPOSED version. Activation happens only through confirm_schedule after approval."""
    a = SolveScheduleArgs.model_validate(args)
    for p in [*a.unavailable_presses, *a.cure_overrides_h, *a.press_blackouts]:
        if p not in F.CURE_PRESS_IDS:
            raise ToolError(f"{p!r} is not a curing press")
    sim = _need(ctx.sim, "factory sim")
    scheduler = ctx.scheduler
    if scheduler is None:
        from warroom.schedule import FallbackScheduler
        scheduler = FallbackScheduler()
    now = _now(ctx)
    orders, shifts = list(sim.orders()), list(sim.shifts())
    # Presses the plant has stopped / taken out are unavailable whatever the commander listed.
    held = held_presses(ctx)
    auto_added = sorted(p for p in held if p not in a.unavailable_presses)
    unavailable = [*a.unavailable_presses, *auto_added]
    if ctx.store is not None:
        from warroom.schedule import reschedule
        res, ver = reschedule(ctx.store, scheduler, now, orders, shifts,
                              unavailable_presses=unavailable, cure_overrides_h=a.cure_overrides_h,
                              time_limit_s=a.time_limit_s, allow_overtime=a.allow_overtime,
                              allow_cross_team=a.allow_cross_team, horizon_days=a.horizon_days,
                              press_blackouts=a.press_blackouts)
        version = ver.version if ver is not None else None   # proposal only: no bus publish (worker thread)
    else:
        res = scheduler.solve(SolveRequest(now=now, orders=orders, shifts=shifts,
                                           unavailable_presses=unavailable,
                                           cure_overrides_h=a.cure_overrides_h, horizon_days=a.horizon_days,
                                           time_limit_s=a.time_limit_s, allow_overtime=a.allow_overtime,
                                           allow_cross_team=a.allow_cross_team,
                                           press_blackouts=a.press_blackouts))
        version = None
    out = res.model_dump(mode="json")
    out["schedule_version"] = version
    return out


# ------------------------------------------------------------------ write (record intent only)
def confirm_schedule(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    a = ConfirmScheduleArgs.model_validate(args)
    store = _need(ctx.store, "schedule store")
    # Check-and-activate under the store's lock when it has one, so two events cannot both pass the
    # stale check against the same parent (the file store's lock is re-entrant).
    lock = getattr(store, "_lock", None) or contextlib.nullcontext()
    with lock:
        prev = store.current().version
        target = store.get(a.schedule_version)
        if target.status is not ScheduleStatus.PROPOSED:
            raise ToolError(f"schedule v{a.schedule_version} is {target.status.value}, not proposed")
        if target.parent_version is not None and target.parent_version != prev:
            # Lost-update guard: the proposal was solved against an older active schedule. Activating it
            # would silently drop whatever another event confirmed since (e.g. an approved cure extension).
            raise ToolError(f"schedule v{a.schedule_version} is stale: solved against v{target.parent_version}, "
                            f"but v{prev} is active now; re-run solve_schedule and re-approve")
        # Held-press guard (same rule as the rollback gate): the plant may have stopped or taken out a
        # press between solve and approval. Activating would load it again, so refuse like a stale
        # proposal; the state machine halts the round and re-solves (solve_schedule excludes it).
        held = held_presses(ctx)
        hits: dict[str, int] = {}
        if held:
            try:
                now = _now(ctx)
            except ToolError:
                now = datetime.min.replace(tzinfo=F.TZ)   # no clock: every not-frozen job counts
            hits = future_jobs_on_presses(target.schedule, held, now)
        if hits:
            what = ", ".join(f"{p} ({held[p]}): {n} new mold-in(s)" for p, n in sorted(hits.items()))
            raise ToolError(f"schedule v{a.schedule_version} is stale: it loads presses that are held now "
                            f"[{what}]; re-run solve_schedule and re-approve")
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
