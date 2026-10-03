"""Seed schedules and the reschedule entry point used by the page-11 demo. Owner: schedule builder.

    orders, shifts, seed = demo_inputs(now)          # page-11 factory state at 10/03 02:14
    store = FileScheduleStore(root, initial=seed)
    res, ver = reschedule(store, FallbackScheduler(), now, orders, shifts,
                          cure_overrides_h={"CURE-07": 36.5})   # S3 after S1 (H1: temp holds at 122 °C)
    # -> #A-311 moves CURE-07 -> CURE-09, deliveries met, gap "10/03 夜班缺 1 人",
    #    options 夜班加班 / 從 2 線調 1 人, solver's pick = transfer
    res, ver = reschedule(store, ..., unavailable_presses=["CURE-07"])   # 02:31 H2: press 7 out

Page-11 layout (demo day D = now's date; the proposal's demo day 10/03/2026 is a Saturday):
- CURE-07 cures #A-307 (M) since D-1 16:14 — 10 h done at 02:14 — next mold #A-311 (due D+3).
- CURE-08 / CURE-09 cure #L-07 (L), ending D 22:00 / 14:00; CURE-10 cures #A-307 ending D+1 00:00.
  CURE-09 is free early enough for its 8 h mold change (principle 4) to land #A-311 on the
  Saturday night, de-molding Monday 清晨 — the page-11 move 7 -> 9.
- Small presses cure #S-38. The Saturday night BODY crew has 1 of 2 people (one on leave); night
  ops need ≥2 people (SOP-HR-01 §4) -> "10/03 夜班缺 1 人".
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

from warroom import factory as F
from warroom.contracts import (
    MachineState,
    Order,
    Schedule,
    ScheduleJob,
    Scheduler,
    ScheduleVersion,
    Shift,
    SolveRequest,
    SolveResult,
    SolverName,
    TeamId,
    TireSize,
)
from warroom.schedule.backprop import back_propagate
from warroom.schedule.solver import FallbackScheduler
from warroom.schedule.staffing import complete_shifts

DEMO_ORDER = "#A-311"


def demo_orders(day: date) -> list[Order]:
    return [
        Order(id="#A-311", size=TireSize.M, qty=20, done=0, due=day + timedelta(days=3), mold_id="A-311", priority=1),
        Order(id="#A-307", size=TireSize.M, qty=40, done=12, due=day + timedelta(days=20), mold_id="A-307"),
        Order(id="#L-07", size=TireSize.L, qty=100, done=8, due=day + timedelta(days=60), mold_id="L-07"),
        Order(id="#S-38", size=TireSize.S, qty=100, done=38, due=day + timedelta(days=10), mold_id="S-38"),
    ]


def demo_shifts(day: date, days: int = 10) -> list[Shift]:
    """Optimised premise shifts (2 people each); the D night BODY shift (夜班) has 1 of 2 people
    (one on leave), as in warroom.sim. Night ops need ≥2 people (SOP-HR-01), so any de-mold in
    that shift needs one more person."""
    start = datetime.combine(day - timedelta(days=1), time(0), tzinfo=F.TZ)
    out = []
    for s in complete_shifts([], start, start + timedelta(days=days)):
        if s.team is TeamId.BODY and s.name == "夜班" and s.start.date() == day:
            s = s.model_copy(update={"headcount": s.headcount - 1, "required": s.headcount})
        out.append(s)
    return out


def _at(day: date, d_off: int, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day + timedelta(days=d_off), time(hh, mm), tzinfo=F.TZ)


def demo_inputs(now: datetime) -> tuple[list[Order], list[Shift], Schedule]:
    """(orders, shifts, seed schedule) for the page-11 scenario, anchored to now's date."""
    day = now.astimezone(F.TZ).date()
    orders, shifts = demo_orders(day), demo_shifts(day)
    frozen = [
        ("CURE-07", "#A-307", TireSize.M, _at(day, -1, 16, 14), "J-0307"),
        ("CURE-08", "#L-07", TireSize.L, _at(day, -1, 12, 0), "J-L07-A"),
        ("CURE-09", "#L-07", TireSize.L, _at(day, -1, 4, 0), "J-L07-B"),
        ("CURE-10", "#A-307", TireSize.M, _at(day, -1, 20, 0), "J-0307-B"),
    ]
    for i, p in enumerate(F.SMALL_ONLY_PRESSES):
        frozen.append((p, "#S-38", TireSize.S, _at(day, -1, 8 + i, 0), f"J-S38-{i + 1:02d}"))
    jobs: list[ScheduleJob] = []
    mold = {o.id: o.mold_id for o in orders}
    for p, oid, size, mi, jid in frozen:
        jobs.append(back_propagate(jid, oid, size, p, mi, mold_id=mold[oid], shifts=shifts, frozen=True))
    # next jobs, provisional times (the solver places them on staffed slots)
    nxt = [("CURE-07", "#A-311", TireSize.M, "J-A311-01"), ("CURE-08", "#L-07", TireSize.L, "J-L07-C"),
           ("CURE-10", "#A-307", TireSize.M, "J-0307-C")]
    nxt += [(p, "#S-38", TireSize.S, f"J-S38-{i + 8:02d}") for i, p in enumerate(F.SMALL_ONLY_PRESSES)]
    ends = {j.press_id: j.demold for j in jobs}
    t0 = now.astimezone(F.TZ).replace(second=0, microsecond=0)

    def draft(js: list[ScheduleJob]) -> Schedule:
        return Schedule(id="SCH-DRAFT", created_at=now, horizon_start=t0, horizon_end=t0 + timedelta(days=7),
                        jobs=js, solver=SolverName.HEURISTIC)

    base = SolveRequest(now=now, orders=orders, shifts=shifts, previous=draft(list(jobs)))
    for p, oid, size, jid in nxt:
        j = back_propagate(jid, oid, size, p, ends[p], mold_id=mold[oid], shifts=shifts)
        if oid == DEMO_ORDER:   # 下一模 #A-311 on press 7, at its first valid slot (8 h mold change)
            j = back_propagate(jid, oid, size, p, _first_valid_mold_in(base, p, j), mold_id=mold[oid], shifts=shifts)
        jobs.append(j)
    pin = [j.id for j in jobs if j.order_id == DEMO_ORDER]
    res = FallbackScheduler().solve(base.model_copy(update={"previous": draft(jobs), "frozen_job_ids": pin}))
    if res.schedule is None:
        raise RuntimeError(f"demo seed failed: {res.gaps}")
    unpin = [j.model_copy(update={"frozen": False}) if j.id in pin else j for j in res.schedule.jobs]
    seed = res.schedule.model_copy(update={"id": f"SCH-SEED-{day:%Y%m%d}", "staffing_changes": [], "jobs": unpin})
    return orders, shifts, seed


def seed_schedule(now: datetime, orders: list[Order], shifts: list[Shift] | None = None,
                  machines: list[MachineState] | None = None, horizon_days: int = 7,
                  scheduler: Scheduler | None = None, next_on: dict[str, str] | None = None) -> Schedule:
    """Generic initial schedule: in-progress cures from machines' cure_job (frozen) + jobs
    generated from orders (earliest due first). `next_on` pins the next mold of a press, e.g.
    {"CURE-07": "#A-311"} for page 11 (下一模 #A-311). Use demo_inputs() for a fully scripted plant."""
    scheduler = scheduler or FallbackScheduler()
    frozen: list[ScheduleJob] = []
    for m in machines or []:
        cj = m.cure_job
        if cj is None or m.machine_id not in F.CURE_PRESS_IDS:
            continue
        frozen.append(back_propagate(cj.job_id, cj.order_id, cj.size, m.machine_id, cj.mold_in_at,
                                     cure_h=cj.cure_required_h, mold_id=cj.mold_id, shifts=shifts, frozen=True))
    t0 = now.astimezone(F.TZ).replace(second=0, microsecond=0)

    def sched(jobs: list[ScheduleJob], sid: str) -> Schedule:
        return Schedule(id=sid, created_at=now, horizon_start=t0, horizon_end=t0 + timedelta(days=horizon_days),
                        jobs=jobs, solver=SolverName.HEURISTIC)

    req = SolveRequest(now=now, orders=orders, shifts=shifts or [], previous=sched(frozen, "SCH-FROZEN") if frozen
                       else None, horizon_days=horizon_days)
    res = scheduler.solve(req)
    if res.schedule is None:
        raise RuntimeError(f"seed schedule failed: {res.status.value} {res.gaps}")
    if next_on:
        jobs = list(res.schedule.jobs)
        by_id = {o.id: o for o in orders}
        pinned: list[str] = []
        for press, oid in next_on.items():
            cand = sorted((j for j in jobs if j.order_id == oid and not j.frozen), key=lambda j: j.mold_in)
            if cand:
                pin = cand[0]
                jobs.remove(pin)
            else:
                o = by_id[oid]
                pin = back_propagate(f"J-{oid.lstrip('#')}-01", oid, o.size, press, t0, mold_id=o.mold_id)
            # the planner's pin is the press's next tire, at the first valid slot (staffed, 8 h
            # mold change if needed); the press's other planned tires are re-planned around it
            mi = _first_valid_mold_in(req.model_copy(update={"previous": sched(jobs, "SCH-PIN")}), press, pin)
            jobs.append(back_propagate(pin.id, pin.order_id, pin.size, press, mi, mold_id=pin.mold_id, shifts=shifts))
            pinned.append(pin.id)
        # the pin holds while the rest is planned afresh around it (in-progress tires + pins
        # only, so no stale slot anchors the plan), then becomes an ordinary planned tire
        # again (a later reschedule may move it: page 11 #A-311 -> CURE-09)
        base = [j for j in jobs if j.frozen or j.id in pinned]
        res = scheduler.solve(req.model_copy(update={"previous": sched(base, "SCH-PINNED"), "frozen_job_ids": pinned}))
        if res.schedule is None:
            raise RuntimeError(f"seed schedule (pinned) failed: {res.status.value} {res.gaps}")
        unpin = [j.model_copy(update={"frozen": False}) if j.id in pinned else j for j in res.schedule.jobs]
        res = res.model_copy(update={"schedule": res.schedule.model_copy(update={"jobs": unpin})})
    return res.schedule.model_copy(update={"staffing_changes": []})


def _first_valid_mold_in(req: SolveRequest, press: str, job: ScheduleJob) -> datetime:
    """First mold-in on `press` after its in-progress tire where the change, the de-mold and the
    tire's prep are staffed, with the 8 h mold-change stop when the mold differs."""
    from warroom.schedule import solver as S
    ctx = S._prepare(req)
    ranges = sorted(ctx.slot_ranges() + [(a, b) for a, b, _ in ctx.short_windows])
    entries = [(j.mold_in, j.release, S._mold_key(j)) for j in ctx.frozen if j.press == press]
    busy = [(j.release, j.release + S.HALF_MIN) for j in ctx.frozen]
    start = max([ctx.now_m + F.BACKPROP[job.size]["a_feed_min"]] + [e for _, e, _ in entries])
    t = S._first_feasible(ranges, start, round(job.cure_h * 60), busy, ctx.hmax, entries,
                          ctx.mold_in_ranges(job.size), S._mold_key(job))
    return ctx.dt(t if t is not None else start)


def reschedule(store, scheduler: Scheduler, now: datetime, orders: list[Order], shifts: list[Shift], *,
               unavailable_presses: list[str] | None = None, cure_overrides_h: dict[str, float] | None = None,
               event_id: str | None = None, reason: str | None = None, time_limit_s: float = 10.0,
               allow_overtime: bool = True, allow_cross_team: bool = True, horizon_days: int = 7,
               propose: bool = True) -> tuple[SolveResult, ScheduleVersion | None]:
    """Solve against the store's ACTIVE schedule and, if feasible, store it as PROPOSED (activation
    happens only after approval: store.activate). Returns (result, proposed version or None)."""
    req = SolveRequest(now=now, orders=orders, shifts=shifts, previous=store.current().schedule,
                       unavailable_presses=list(unavailable_presses or []),
                       cure_overrides_h=dict(cure_overrides_h or {}), horizon_days=horizon_days,
                       time_limit_s=time_limit_s, allow_overtime=allow_overtime, allow_cross_team=allow_cross_team)
    res = scheduler.solve(req)
    ver = None
    if propose and res.feasible and res.schedule is not None:
        why = reason or describe(res)
        ver = store.propose(res.schedule, why, event_id)
    return res, ver


def _job_label(order_id: str, job_id: str, n_in_plan: int) -> str:
    """'#M-201 第 3 模' for one tire of a multi-tire order, so a planned batch is never confused
    with the tire of the same order already curing on that press; '#A-311' when it is the only one."""
    m = re.search(r"-(\d+)$", job_id)
    if n_in_plan > 1 and m and not job_id.startswith("J-CURE-"):
        return f"{order_id} 第 {int(m.group(1))} 模"
    return order_id


def describe(res: SolveResult) -> str:
    """One-line Chinese summary for audit / approval cards."""
    parts = []
    per_order: dict[str, int] = {}
    for sj in (res.schedule.jobs if res.schedule else []):
        per_order[sj.order_id] = per_order.get(sj.order_id, 0) + 1
    for mv in res.moves:
        if mv.from_press != mv.to_press:
            parts.append(f"{_job_label(mv.order_id, mv.job_id, per_order.get(mv.order_id, 1))} "
                         f"由 {mv.from_press} 改到 {mv.to_press}")
    if res.staffing_options and res.schedule and res.schedule.staffing_changes:
        parts.append("；".join(f"{g}→{c.label}" for g, c in zip(res.gaps, res.schedule.staffing_changes)))
    # deliveries_met covers this plan's tires only (open-order outlook is in schedule.warnings)
    parts.append("本週排程訂單交期滿足" if res.deliveries_met else f"延遲：{', '.join(res.late_orders)}")
    if res.fell_back:
        parts.append("備援求解器")
    return "；".join(parts)
