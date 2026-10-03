"""Cure-press-centric schedulers (page 6, page 7). Owner: schedule builder.

Model, shared by both solvers (built in `_prepare`):
- Time in minutes from `req.now`. Every job is one tire on one press.
- Frozen jobs (in progress, mold_in <= now, or listed in frozen_job_ids) keep press and mold-in.
  `cure_overrides_h[press]` replaces the cure time of the in-progress job on that press
  (page 11 S1: CURE-07 28 h -> 36.5 h). Their de-mold waits for the next staffed slot.
- Free jobs come from `previous` (the active schedule); when it has none, they are generated
  from the orders, earliest due first, up to what the presses can cure in the horizon.
- Hard (page 6 principles 1, 2): M/L only on factory.ML_PRESSES, S only on SMALL_ONLY_PRESSES;
  unavailable presses get no new job; one tire per press; mold-in AND de-mold (mold-in + cure)
  both start inside a staffed window with room for the 30-min op (curing team, or BODY night);
  one crew does one half-op at a time (labor as a constraint); feed must have started:
  a new job can't mold-in before now + A-line feed lead (T − 1h40/2h30/3h30).
- Mold change (principle 4): when consecutive tires on a press use different molds, the next
  mold-in waits MOLD_GAP_MIN (8 h) after the de-mold — hard in both solvers and in
  check_schedule; PEN_MOLD_CHANGE still prefers fewer changes.
- Weekend rule (principle 3) follows from the windows: nothing de-molds Sunday after 05:00, and
  staffing.MONDAY_EARLY adds the Monday 00–05 de-mold, so the Saturday night shift loads the
  last round and it de-molds Monday 清晨 (no fire-out, no re-preheat).
- Generated jobs share the M/L presses by press-time between sizes (page 6 sim: M, L 2 presses
  each), EDD within a size, so large tires run every week instead of queueing behind M.
- Soft: lateness vs due date (heavy), early de-mold (level-loading / throughput), moving a job
  to another press, shifting its time, mold changes (principle 4, ~8 h stop each), ops in a
  short-staffed shift (needs 夜班加班 or 從 2 線調 1 人), an M tire on a press curing L now or
  vice versa (中大胎固定機台; rush orders exempt), each extra mold a press carries, and (repair
  only) re-timing a tire on a press the event did not touch. Rush orders (priority > 0) weigh
  their de-mold time (1 + priority)×.
- Repair, not re-plan: when the request names affected presses (unavailable / cure override),
  jobs of the untouched press pool keep their previous slot if still valid; moving work onto a
  press that shares the affected press's steam header (page 11: 6 and 8 share 7's pipe) costs
  extra, so #A-311 goes to CURE-09 rather than CURE-08.
- Identical tires of one order are interchangeable: ids are re-matched to the previous plan
  after solving so `moves` lists real moves, not id swaps.
- Gaps (SOP-HR-01): an op in a night shift with <2 people (or <3 for simultaneous de-molds)
  -> "10/03 夜班缺 1 人" with options 夜班加班 (R2) / 從 2 線調 1 人 (R3); the solver's pick
  is the transfer when allowed (page 11 02:14:25), else overtime.
- deliveries_met means no order ends later than in the previous plan (交期不受影響); every
  late order is still listed in late_orders, pre-existing ones also as a warning. It covers
  only the tires in this plan, not tires of open orders beyond the horizon.
CP-SAT is primary (15-min grid; stops at the time limit or after STALL_S without improvement);
HeuristicScheduler is the page-7 backup solver; FallbackScheduler switches on
timeout/error/infeasible and sets fell_back=True.
"""
from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

from warroom import factory as F
from warroom.contracts import (
    JobMove,
    Order,
    Schedule,
    ScheduleJob,
    Scheduler,
    Shift,
    SolveRequest,
    SolveResult,
    SolverName,
    SolveStatus,
    StaffingChange,
    TireSize,
)
from warroom.schedule.backprop import back_propagate, labor_conflicts, prep_needs
from warroom.schedule.staffing import (
    HALF_MIN,
    OP_MIN,
    Window,
    complete_shifts,
    gap_label,
    is_short,
    op_windows,
    staffing_gaps,
    staffing_options,
)

# objective weights (minutes-equivalent)
W_DEMOLD = 2            # per minute of de-mold time from now (earlier is better)
W_SHIFT = 3             # per minute a previous job's mold-in moves (stability beats earliness)
W_LATE = 200            # per minute late vs due date end-of-day
PEN_MOVE = 4 * 60 * W_DEMOLD          # moving a job to another press ≈ 4 h
PEN_MOLD_CHANGE = int(F.MOLD_CHANGE_STOP_H * 60 * W_DEMOLD)   # principle 4: ~8 h stop
MOLD_GAP_MIN = int(F.MOLD_CHANGE_STOP_H * 60)   # hard: de-mold -> next mold-in with another mold
PEN_SHORT_OP = 60 * W_DEMOLD          # an op inside a short-staffed shift
PEN_SIZE_HOME = 48 * 60 * W_DEMOLD  # principle 4 中大胎固定機台: M on an L press or L on an M press (rush orders exempt)
PEN_DISTURB = 4 * 60 * W_DEMOLD      # repair: changing a tire on a press the event did not touch at all
PEN_SAME_HEADER = 12 * 60 * W_DEMOLD  # moving work onto the affected press's steam pipe (page 11: 6, 8 share 7's pipe)
EXTRA_DAYS = 4          # jobs may finish this many days past the horizon
REL_GAP = 0.002         # stop when within 0.2 % of the proven bound
STALL_S = 1.5           # or when the best plan has not improved for this long (weak bounds)
GRID_MIN = 15           # mold-in times on a 15-min wall-clock grid (both solvers)
MIN_HORIZON_DAYS = 1    # a degenerate horizon is clamped (and CP-SAT is skipped for it)
MAX_HORIZON_DAYS = 28


@dataclass
class _Job:
    id: str
    order_id: str
    size: TireSize
    mold_id: str | None
    cure_min: int
    frozen: bool
    eligible: list[str]
    lb: int                      # earliest mold-in (minutes)
    due: int | None              # minutes, end of due date
    priority: int = 0
    press: str | None = None     # frozen: fixed press
    mold_in: int | None = None   # frozen: fixed mold-in
    orig_press: str | None = None
    orig_mold_in: int | None = None
    release: int | None = None   # frozen: staffed de-mold op start (press free from here)


@dataclass
class _Ctx:
    req: SolveRequest
    t0: datetime
    hmax: int
    shifts: list[Shift]
    windows: list[Window]          # usable op windows
    short_windows: list[tuple[int, int, Shift]]   # op windows inside short shifts (start,end,shift)
    jobs: list[_Job]
    frozen: list[_Job]
    free: list[_Job]
    locked: list[_Job] = field(default_factory=list)   # kept as in `previous` (unaffected pool)
    press_mold: dict[str, str | None] = field(default_factory=dict)   # last mold per press
    press_size: dict[str, TireSize] = field(default_factory=dict)     # M/L press -> home size (its in-progress tire)
    suspect: set[str] = field(default_factory=set)   # presses sharing a steam header with an affected one
    affected: set[str] = field(default_factory=set)  # presses the request re-cured or took out
    warnings: list[str] = field(default_factory=list)
    orders: dict[str, Order] = field(default_factory=dict)
    now_m: int = 0                  # req.now in minutes from the grid origin t0
    horizon_days: int = 7           # req.horizon_days clamped to [MIN_HORIZON_DAYS, MAX_HORIZON_DAYS]
    prep_ok: dict[TireSize, list[tuple[int, int]]] = field(default_factory=dict)  # mold-in minutes whose prep is staffed

    def m(self, dt: datetime) -> int:
        return math.floor((dt - self.t0).total_seconds() / 60.0)

    def dt(self, minutes: int) -> datetime:
        return self.t0 + timedelta(minutes=minutes)

    def mold_in_ranges(self, size: TireSize) -> list[tuple[int, int]]:
        """Staffed op ranges (incl. short shifts) where the tire's prep can also be staffed."""
        ops = _merge(self.slot_ranges() + [(a, b) for a, b, _ in self.short_windows])
        prep = self.prep_ok.get(size)
        return ops if prep is None else _intersect(ops, prep)

    def slot_ranges(self) -> list[tuple[int, int]]:
        """Allowed op start minutes [a, b] (inclusive), op fits before window end."""
        out = []
        for w in self.windows:
            a, b = self.m(w.start), self.m(w.end) - OP_MIN
            if b >= a:
                out.append((a, b))
        return out


def _mold_key(job: _Job | ScheduleJob) -> str:
    return job.mold_id or f"size-{job.size.value}"


def _eligible(size: TireSize, unavailable: set[str]) -> list[str]:
    pool = F.ML_PRESSES if size in (TireSize.M, TireSize.L) else F.SMALL_ONLY_PRESSES
    return [p for p in pool if p not in unavailable]


def _prepare(req: SolveRequest) -> _Ctx:
    now = req.now.astimezone(F.TZ)
    t0 = now.replace(minute=now.minute - now.minute % GRID_MIN, second=0, microsecond=0)   # grid origin
    hdays = min(max(int(req.horizon_days), MIN_HORIZON_DAYS), MAX_HORIZON_DAYS)
    horizon_end = t0 + timedelta(days=hdays)
    hmax = int((hdays + EXTRA_DAYS) * 1440)
    shifts = complete_shifts(req.shifts, t0 - timedelta(days=1), t0 + timedelta(minutes=hmax + 1440))
    staffing_ok = req.allow_overtime or req.allow_cross_team
    # shifts that are already short with a single op can only be used if staffing may be added
    short_ids = {s.id for s in shifts if is_short(s, 1) > 0}
    windows = op_windows(shifts, exclude=set() if staffing_ok else short_ids)
    ctx = _Ctx(req=req, t0=t0, hmax=hmax, shifts=shifts, windows=windows, short_windows=[], jobs=[], frozen=[],
               free=[], orders={o.id: o for o in req.orders})
    ctx.now_m = now_m = ctx.m(now)
    ctx.horizon_days = hdays
    if hdays != req.horizon_days:
        ctx.warnings.append(f"排程視窗 {req.horizon_days} 天無效，改用 {hdays} 天")
    for s in shifts:
        if s.id in short_ids and staffing_ok:
            a, b = ctx.m(s.start), ctx.m(s.end) - OP_MIN
            if b >= a:
                ctx.short_windows.append((a, b, s))
    ctx.prep_ok = {size: _prep_ranges(ctx, size) for size in TireSize}
    unavailable = set(req.unavailable_presses)
    frozen_ids = set(req.frozen_job_ids)

    def due_min(order_id: str) -> int | None:
        o = ctx.orders.get(order_id)
        if o is None:
            return None
        return ctx.m(datetime.combine(o.due, datetime.max.time(), tzinfo=F.TZ).replace(microsecond=0))

    def prio(order_id: str) -> int:
        o = ctx.orders.get(order_id)
        return o.priority if o else 0

    prev_jobs = list(req.previous.jobs) if req.previous else []
    for pj in prev_jobs:
        mi = ctx.m(pj.mold_in)
        is_frozen = pj.frozen or pj.id in frozen_ids or pj.mold_in <= now
        cure_h = pj.cure_h
        if is_frozen and pj.press_id in req.cure_overrides_h:
            cure_h = float(req.cure_overrides_h[pj.press_id])
        cure_min = round(cure_h * 60)
        if is_frozen:
            j = _Job(pj.id, pj.order_id, pj.size, pj.mold_id, cure_min, True, [pj.press_id], lb=mi,
                     due=due_min(pj.order_id), priority=prio(pj.order_id), press=pj.press_id, mold_in=mi,
                     orig_press=pj.press_id, orig_mold_in=mi)
            if mi + cure_min <= now_m and pj.press_id not in req.cure_overrides_h:
                continue    # already de-molded: history, not schedule
            ctx.frozen.append(j)
            if pj.press_id in unavailable:
                ctx.warnings.append(f"{pj.press_id} 停用：在製 {pj.order_id} 依 SOP 處置，不列入排程交期")
        else:
            feed_lead = F.BACKPROP[pj.size]["a_feed_min"]
            lb = max(now_m, min(mi, now_m + feed_lead))      # feed already started for near jobs
            ctx.free.append(_Job(pj.id, pj.order_id, pj.size, pj.mold_id, cure_min, False,
                                 _eligible(pj.size, unavailable), lb=lb, due=due_min(pj.order_id),
                                 priority=prio(pj.order_id), orig_press=pj.press_id, orig_mold_in=mi))
    if not ctx.free:
        ctx.free = _generate_jobs(ctx, unavailable, due_min, prio, horizon_end)
    affected = set(req.cure_overrides_h) | unavailable
    ctx.affected = affected
    for p in affected:
        hdr = F.MACHINE_BY_ID[p].steam_header if p in F.MACHINE_BY_ID else None
        if hdr:
            ctx.suspect |= set(F.presses_on_header(hdr)) - affected
    for p in F.CURE_PRESS_IDS:
        fz = sorted((j for j in ctx.frozen if j.press == p), key=lambda j: j.mold_in)
        ctx.press_mold[p] = _mold_key(fz[-1]) if fz else None
        if fz and p in F.ML_PRESSES:
            ctx.press_size[p] = fz[0].size      # the tire curing now, not a pinned next one
    for j in ctx.free:
        if not j.eligible:
            ctx.warnings.append(f"{j.id} 無可用硫化機（{j.size.value}）")
    # frozen jobs: press release = staffed de-mold slot, one crew (de-molds serialized). Presses
    # the event did not touch keep their de-mold first, then still-valid jobs of untouched pools
    # are locked, then the touched presses' de-molds fit around them.
    busy: list[tuple[int, int]] = []
    _assign_releases(ctx, [j for j in ctx.frozen if j.press not in affected], busy)
    ctx.jobs = ctx.frozen + ctx.free
    _lock_unaffected(ctx, affected)
    busy += _locked_busy(ctx)
    # the touched press's de-mold fits around the ops the plan already has on other presses
    # (repair, not re-plan: e.g. the Monday 00–05 de-mold crew is not pulled off their slots)
    for j in ctx.free:
        if j.orig_press is not None and j.orig_press not in affected and j.orig_mold_in is not None \
                and j.orig_mold_in >= ctx.now_m:
            busy += [(j.orig_mold_in + HALF_MIN, j.orig_mold_in + OP_MIN),
                     (j.orig_mold_in + j.cure_min, j.orig_mold_in + j.cure_min + HALF_MIN)]
    _assign_releases(ctx, [j for j in ctx.frozen if j.press in affected], busy)
    return ctx


def _prep_ranges(ctx: _Ctx, size: TireSize) -> list[tuple[int, int]]:
    """Mold-in minutes T whose prep (A feed, side/bead, bonding) has a staffed slot (page 6:
    back-propagate feed and labor from T). Each prep need is fine if its back-propagated start
    T - offset falls in a shift of its team, or if a shift of that team has room for it inside
    [T - L, T]: the press holds one blank (principle 5) and its previous tire went in at least
    L = the pool's shortest cure ago, so the blank can be made ahead in that window. A team
    with no shift in the data is not constrained."""
    lookback = int(min(F.CURE_HOURS[s] for s in ((TireSize.S,) if size is TireSize.S else (TireSize.M, TireSize.L))) * 60)
    out: list[tuple[int, int]] | None = None
    for team, off, dur in prep_needs(size):
        ivs = []
        for sh in ctx.shifts:
            if sh.team is not team:
                continue
            a, b = ctx.m(sh.start), ctx.m(sh.end)
            ivs.append((a + off, b + off - 1))            # own start staffed: a <= T - off < b
            if b - a >= dur and lookback >= dur:
                ivs.append((a + dur, b + lookback - dur))   # done ahead in [T - L, T]
        if not ivs:
            continue
        team_ok = _merge(ivs)
        out = team_ok if out is None else _intersect(out, team_ok)
    return out if out is not None else [(-10**9, 10**9)]


def _intersect(xs: list[tuple[int, int]], ys: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Intersection of two sorted lists of inclusive integer ranges."""
    out, i, k = [], 0, 0
    while i < len(xs) and k < len(ys):
        a, b = max(xs[i][0], ys[k][0]), min(xs[i][1], ys[k][1])
        if a <= b:
            out.append((a, b))
        if xs[i][1] < ys[k][1]:
            i += 1
        else:
            k += 1
    return out


def _assign_releases(ctx: _Ctx, jobs: list[_Job], busy: list[tuple[int, int]]) -> None:
    """Earliest staffed de-mold slot ≥ cure end that the crew is free for (appends to busy)."""
    for j in sorted(jobs, key=lambda j: (j.mold_in + j.cure_min, j.press)):
        t = j.mold_in + j.cure_min
        r = None
        for _ in range(500):
            r = _next_slot(ctx, t)
            if r is None:
                break
            clash = [b for a, b in busy if a < r + HALF_MIN and r < b]
            if not clash:
                break
            t = max(clash)
        if r is None:
            r = j.mold_in + j.cure_min
            ctx.warnings.append(f"{j.id} 出模時刻無人班表")
        j.release = r
        busy.append((r, r + HALF_MIN))


def _lock_unaffected(ctx: _Ctx, affected: set[str]) -> None:
    """Repair, not re-plan: when an event touches some presses, jobs of the other press pool
    (S pool = small-only presses, ML pool = CURE-07..10) keep their previous slot if it is still
    valid. Keeps the plan stable and the CP-SAT model small enough to solve in seconds."""
    if not affected:
        return
    pools = []
    if affected & set(F.SMALL_ONLY_PRESSES):
        pools.append(set(F.SMALL_ONLY_PRESSES))
    if affected & set(F.ML_PRESSES):
        pools.append(set(F.ML_PRESSES))
    touched = set().union(*pools) if pools else set()
    ranges = ctx.slot_ranges() + [(a, b) for a, b, _ in ctx.short_windows]
    busy = [(j.release, j.release + HALF_MIN) for j in ctx.frozen if j.press not in affected]
    on_press: dict[str, list[tuple[int, int, str]]] = {}
    for j in ctx.frozen:
        on_press.setdefault(j.press, []).append((j.mold_in, j.release, _mold_key(j)))
    keep, lock = [], []
    for j in sorted(ctx.free, key=lambda j: (j.orig_mold_in if j.orig_mold_in is not None else 10**9, j.id)):
        ok = (j.orig_press is not None and j.orig_press not in touched and j.orig_press in j.eligible
              and j.orig_mold_in is not None and j.orig_mold_in >= j.lb
              and _in_ranges(j.orig_mold_in, ctx.mold_in_ranges(j.size))
              and _in_ranges(j.orig_mold_in + j.cure_min, ranges))
        if ok:
            mine = ((j.orig_mold_in + HALF_MIN, j.orig_mold_in + OP_MIN),
                    (j.orig_mold_in + j.cure_min, j.orig_mold_in + j.cure_min + HALF_MIN))
            ok = not any(a < y and x < b for a, b in busy for x, y in mine)
        if ok:
            ok = not _press_clash(on_press.get(j.orig_press, []), j.orig_mold_in, j.orig_mold_in + j.cure_min,
                                  _mold_key(j))
        if ok:
            j.press, j.mold_in = j.orig_press, j.orig_mold_in
            lock.append(j)
            on_press.setdefault(j.press, []).append((j.mold_in, j.mold_in + j.cure_min, _mold_key(j)))
        else:
            keep.append(j)
    ctx.free, ctx.locked = keep, lock
    ctx.jobs = ctx.frozen + ctx.locked + ctx.free


def _press_clash(entries: list[tuple[int, int, str]], start: int, end: int, mold: str) -> list[int]:
    """Ends (incl. any mold-change stop) of the press occupations [a, b) that a cure [start, end)
    with `mold` collides with: overlap, or a mold change with less than MOLD_GAP_MIN between."""
    out = []
    for a, b, k in entries:
        g = 0 if k == mold else MOLD_GAP_MIN
        if a < end + g and start < b + g:
            out.append(b + g)
    return out


def _locked_busy(ctx: _Ctx) -> list[tuple[int, int]]:
    out = []
    for j in ctx.locked:
        out += [(j.mold_in + HALF_MIN, j.mold_in + OP_MIN), (j.mold_in + j.cure_min, j.mold_in + j.cure_min + HALF_MIN)]
    return out


def _generate_jobs(ctx: _Ctx, unavailable: set[str], due_min, prio, horizon_end: datetime) -> list[_Job]:
    out: list[_Job] = []
    horizon_h = (horizon_end - ctx.t0).total_seconds() / 3600
    for sizes in ([TireSize.S], [TireSize.M, TireSize.L]):
        presses = _eligible(sizes[0], unavailable)
        if not presses:
            continue
        # tires already curing (frozen) count toward the order: generate only what is left
        curing: dict[str, int] = {}
        for fj in ctx.frozen:
            curing[fj.order_id] = curing.get(fj.order_id, 0) + 1
        left = {o.id: o.qty - o.done - curing.get(o.id, 0) for o in ctx.req.orders if o.size in sizes}
        demand = {sz: sum(n for o in ctx.req.orders if o.size is sz and (n := left[o.id]) > 0) for sz in sizes}
        quota = _size_quota(len(presses), horizon_h, sizes, demand)
        orders = sorted((o for o in ctx.req.orders if o.size in sizes and left[o.id] > 0),
                        key=lambda o: (o.due, -o.priority, o.id))
        # earliest due first (EDD) within a size; tires beyond the size's quota are left for later
        for o in orders:
            for k in range(1, left[o.id] + 1):
                if quota[o.size] <= 0:
                    break
                quota[o.size] -= 1
                out.append(_Job(f"J-{o.id.lstrip('#')}-{k:02d}", o.id, o.size, o.mold_id,
                                int(F.CURE_HOURS[o.size] * 60), False, presses,
                                lb=ctx.now_m + F.BACKPROP[o.size]["a_feed_min"], due=due_min(o.id),
                                priority=o.priority))
    return out


def _size_quota(n_presses: int, horizon_h: float, sizes: list[TireSize], demand: dict[TireSize, int]) -> dict[TireSize, int]:
    """Tires to generate per size. Mon–Sat staffing and waits for staffed de-molds: ~85 % of
    back-to-back cures. One size: as many as fit. M and L share the 4 presses by press-time
    (page 6 sim: M, L 2 presses each); press-time a size cannot use goes to the other."""
    if len(sizes) == 1:
        sz = sizes[0]
        return {sz: n_presses * max(1, int(horizon_h / F.CURE_HOURS[sz] * 0.85))}
    budget = n_presses * horizon_h * 0.85
    active = [sz for sz in sizes if demand.get(sz, 0) > 0]
    quota = {sz: 0 for sz in sizes}
    if not active:
        return quota
    share = budget / len(active)
    spare = 0.0
    for sz in sorted(active, key=lambda z: demand[z] * F.CURE_HOURS[z]):   # smaller need first
        cure = F.CURE_HOURS[sz]
        n = min(demand[sz], max(1, int((share + spare) / cure)))
        spare = max(0.0, share + spare - n * cure)
        quota[sz] = n
    return quota


def _next_slot(ctx: _Ctx, t: int, ranges: list[tuple[int, int]] | None = None) -> int | None:
    for a, b in (ranges if ranges is not None else _all_ranges(ctx)):
        if b < t:
            continue
        return max(a, t)
    return None


def _all_ranges(ctx: _Ctx) -> list[tuple[int, int]]:
    rs = ctx.slot_ranges()
    return rs


def _in_ranges(t: int, ranges: list[tuple[int, int]]) -> bool:
    return any(a <= t <= b for a, b in ranges)


# =====================================================================================
# Result assembly + independent feasibility check (both solvers)
# =====================================================================================
def _finalize(ctx: _Ctx, assign: dict[str, tuple[str, int]], solver: SolverName, status: SolveStatus,
              t_start: float, objective: float | None = None) -> SolveResult:
    req = ctx.req
    assign = _relabel(ctx, assign)
    jobs: list[ScheduleJob] = []
    for j in ctx.frozen:
        jobs.append(back_propagate(j.id, j.order_id, j.size, j.press, ctx.dt(j.mold_in), j.cure_min / 60.0,
                                   j.mold_id, ctx.shifts, frozen=True))
    assign = {**{j.id: (j.press, j.mold_in) for j in ctx.locked}, **assign}
    for j in ctx.locked + ctx.free:
        if j.id not in assign:
            continue
        p, mi = assign[j.id]
        jobs.append(back_propagate(j.id, j.order_id, j.size, p, ctx.dt(mi), j.cure_min / 60.0, j.mold_id, ctx.shifts))
    jobs.sort(key=lambda x: (x.press_id, x.mold_in))
    unscheduled = [j.id for j in ctx.free if j.id not in assign and j.orig_press is not None]
    later = [j.id for j in ctx.free if j.id not in assign and j.orig_press is None]
    if later:
        ctx.warnings.append(f"{len(later)} 顆超出排程視窗，下次排程再排")
    warnings = list(ctx.warnings)
    if unscheduled:
        warnings.append(f"未排入：{', '.join(unscheduled)}")
    # late orders (frozen jobs on out-of-service presses are handled by SOP, not counted)
    late: set[str] = set()
    unavailable = set(req.unavailable_presses)
    for sj in jobs:
        o = ctx.orders.get(sj.order_id)
        if o is None or (sj.frozen and sj.press_id in unavailable):
            continue
        if sj.demold.date() > o.due:
            late.add(sj.order_id)
    # 交期不受影響 = this plan makes no order later than the previous plan did
    prev_late: dict[str, datetime] = {}
    if req.previous is not None:
        for pj in req.previous.jobs:
            o = ctx.orders.get(pj.order_id)
            if o is not None and pj.demold.date() > o.due:
                prev_late[pj.order_id] = max(prev_late.get(pj.order_id, pj.demold), pj.demold)
    new_late = set()
    for oid in late:
        last = max(sj.demold for sj in jobs if sj.order_id == oid)
        if oid not in prev_late or last > prev_late[oid]:
            new_late.add(oid)
    if late - new_late:
        warnings.append(f"既有延遲（非本次異動造成）：{', '.join(sorted(late - new_late))}")
    warnings += _open_order_outlook(ctx, jobs)
    # diff vs previous
    moves: list[JobMove] = []
    prev = {pj.id: pj for pj in req.previous.jobs} if req.previous else {}
    for sj in jobs:
        pj = prev.get(sj.id)
        if pj is None or sj.frozen:
            continue
        if pj.press_id != sj.press_id or pj.mold_in != sj.mold_in:
            o = ctx.orders.get(sj.order_id)
            late_h = 0.0
            if o is not None:
                due_end = datetime.combine(o.due, datetime.max.time(), tzinfo=F.TZ)
                late_h = max(0.0, (sj.demold - due_end).total_seconds() / 3600)
            moves.append(JobMove(job_id=sj.id, order_id=sj.order_id, from_press=pj.press_id, to_press=sj.press_id,
                                 from_mold_in=pj.mold_in, to_mold_in=sj.mold_in, late_h=round(late_h, 2)))
    # staffing: de-mold op starts at the staffed release; mold-in op at mold-in
    op_times = [("mold_in", sj.press_id, sj.mold_in) for sj in jobs if not sj.frozen]
    op_times += [("demold", j.press, ctx.dt(j.release)) for j in ctx.frozen]
    op_times += [("demold", sj.press_id, sj.demold) for sj in jobs if not sj.frozen]
    gaps_t = [(s, n, short) for s, n, short in staffing_gaps(op_times, ctx.shifts)
              if s.end > ctx.t0 and s.start < ctx.dt(ctx.horizon_days * 1440)]
    gaps = [gap_label(s, short) for s, _, short in gaps_t]
    options: list[StaffingChange] = []
    chosen: list[StaffingChange] = []
    for s, _, short in gaps_t:
        opts = staffing_options(s, short, req.allow_overtime, req.allow_cross_team)
        options.extend(opts)
        if opts:
            # solver's pick: transfer if allowed (no overtime cost), else overtime (page 11 02:14:25)
            chosen.append(next((o for o in opts if o.kind == "transfer"), opts[0]))
    # prep outside its team's shift is normal (done ahead, one blank per press); warn only for
    # prep that no shift can cover before the mold-in, within the visible horizon
    prep = labor_conflicts(jobs, ctx.shifts, horizon_end=ctx.dt(ctx.horizon_days * 1440))
    if prep:
        warnings.append(f"{len(prep)} 項備料／貼合無班可提前備妥：{prep[0]}" + (" 等" if len(prep) > 1 else ""))
    sched = Schedule(id=f"SCH-{ctx.t0:%Y%m%d-%H%M}-{solver.value}", created_at=req.now, horizon_start=ctx.t0,
                     horizon_end=ctx.t0 + timedelta(days=ctx.horizon_days), jobs=jobs, staffing_changes=chosen,
                     solver=solver, objective=objective, warnings=warnings)
    errors = check_schedule(sched, req, ctx)
    if errors:
        sched = sched.model_copy(update={"warnings": warnings + [f"檢核失敗：{e}" for e in errors]})
    unstaffed_gap = any(not staffing_options(s, sh, req.allow_overtime, req.allow_cross_team) for s, _, sh in gaps_t)
    feasible = not errors and not unscheduled and not unstaffed_gap
    return SolveResult(status=status, solver=solver, solve_ms=round((_time.perf_counter() - t_start) * 1000, 1),
                       schedule=sched, feasible=feasible, deliveries_met=not new_late, late_orders=sorted(late),
                       moves=moves, staffing_options=options, gaps=gaps)


def _open_order_outlook(ctx: _Ctx, jobs: list[ScheduleJob]) -> list[str]:
    """deliveries_met covers only the tires in this plan. For tires of open orders that are not
    in it yet, project their finish at the page-6 AI rate (7/6/4.9 per press per week) on the
    presses of their size after the plan's last de-mold of that size, and warn when that is past
    the due date — so 交期 claims are not read as covering the whole order book."""
    rate = F.CAPACITY_TARGETS[-1]
    per_press = {TireSize.S: rate.small, TireSize.M: rate.medium, TireSize.L: rate.large}
    unavailable = set(ctx.req.unavailable_presses)
    in_plan: dict[str, int] = {}
    for sj in jobs:
        in_plan[sj.order_id] = in_plan.get(sj.order_id, 0) + 1
    out = []
    queue_end: dict[TireSize, datetime] = {}
    for o in sorted(ctx.req.orders, key=lambda o: (o.due, -o.priority, o.id)):
        rest = o.qty - o.done - in_plan.get(o.id, 0)
        if rest <= 0:
            continue
        if o.size is TireSize.S:
            n = len([p for p in F.SMALL_ONLY_PRESSES if p not in unavailable])
        else:
            avail = [p for p in F.ML_PRESSES if p not in unavailable]
            home = [p for p in avail if ctx.press_size.get(p) is o.size]
            n = len(home) or max(1, len(avail) // 2)
        if n == 0:
            continue
        start = queue_end.get(o.size) or max((sj.demold for sj in jobs if sj.size is o.size),
                                              default=ctx.dt(ctx.horizon_days * 1440))
        end = start + timedelta(weeks=rest / (n * per_press[o.size]))
        queue_end[o.size] = end
        if end.date() > o.due:
            out.append(f"{o.id} 剩 {rest} 顆預估 {end:%m/%d}（交期 {o.due:%m/%d}）")
    return [f"本週排程外，依產能預估晚於交期：{'、'.join(out)}"] if out else []


def _relabel(ctx: _Ctx, assign: dict[str, tuple[str, int]]) -> dict[str, tuple[str, int]]:
    """Tires of one order with the same mold and cure are interchangeable. Give each planned slot
    the id of the previous job it most resembles (same press first, then nearest time), so the
    diff vs the previous plan shows real moves, not id swaps."""
    groups: dict[tuple, list[_Job]] = {}
    for j in ctx.free:
        if j.id in assign:
            groups.setdefault((j.order_id, j.size, j.mold_id, j.cure_min), []).append(j)
    out = dict(assign)
    for js in groups.values():
        if len(js) < 2 or any(j.orig_press is None for j in js):
            continue
        slots = sorted((assign[j.id] for j in js), key=lambda x: (x[1], x[0]))
        unused = list(js)
        for p, t in slots:
            best = min(unused, key=lambda j: (j.orig_press != p, abs(j.orig_mold_in - t), j.id))
            unused.remove(best)
            out[best.id] = (p, t)
    return out


def check_schedule(sched: Schedule, req: SolveRequest, ctx: _Ctx | None = None) -> list[str]:
    """Deterministic feasibility check, independent of the solver that produced the plan:
    press eligibility, unavailable presses, one tire per press, staffed mold-in/de-mold."""
    ctx = ctx or _prepare(req)
    errs: list[str] = []
    unavailable = set(req.unavailable_presses)
    ranges = ctx.slot_ranges() + [(a, b) for a, b, _ in ctx.short_windows]
    by_press: dict[str, list[ScheduleJob]] = {}
    for j in sched.jobs:
        by_press.setdefault(j.press_id, []).append(j)
        if j.frozen:
            continue
        if j.press_id in unavailable:
            errs.append(f"{j.id} 排在停用機台 {j.press_id}")
        if j.size in (TireSize.M, TireSize.L) and j.press_id not in F.ML_PRESSES:
            errs.append(f"{j.id} 中大胎排在小胎專用機 {j.press_id}")
        if j.size is TireSize.S and j.press_id not in F.SMALL_ONLY_PRESSES:
            errs.append(f"{j.id} 小胎佔用中大胎機 {j.press_id}")
        if not _in_ranges(ctx.m(j.mold_in), ranges):
            errs.append(f"{j.id} 入模 {j.mold_in:%m/%d %H:%M} 不在有人時段")
        elif not _in_ranges(ctx.m(j.mold_in), ctx.mold_in_ranges(j.size)):
            errs.append(f"{j.id} 入模 {j.mold_in:%m/%d %H:%M} 前無班可備料／貼合")
        if not _in_ranges(ctx.m(j.demold), ranges):
            errs.append(f"{j.id} 出模 {j.demold:%m/%d %H:%M} 不在有人時段")
    for p, js in by_press.items():
        js.sort(key=lambda x: x.mold_in)
        for a, b in pairwise(js):
            if b.mold_in < a.demold:
                errs.append(f"{p} {a.id} 與 {b.id} 重疊")
            elif not b.frozen and _mold_key(a) != _mold_key(b) and b.mold_in < a.demold + timedelta(minutes=MOLD_GAP_MIN):
                errs.append(f"{p} {a.id}→{b.id} 換模停機不足 {F.MOLD_CHANGE_STOP_H:g} 小時")
    return errs


# =====================================================================================
# Backup: greedy heuristic
# =====================================================================================
class HeuristicScheduler:
    """Backup solver (page 7): greedy earliest-staffed-slot dispatch. Always returns quickly.
    Jobs by (due, priority, original time); each takes the press with the lowest
    de-mold time + move/mold-change penalty, at the first slot where both mold-in and
    de-mold are staffed and the op crew is free."""

    def solve(self, req: SolveRequest) -> SolveResult:
        t_start = _time.perf_counter()
        try:
            ctx = _prepare(req)
            assign = greedy_assign(ctx)
            return _finalize(ctx, assign, SolverName.HEURISTIC, SolveStatus.FEASIBLE, t_start)
        except Exception as e:  # noqa: BLE001 - backup must never raise
            return SolveResult(status=SolveStatus.ERROR, solver=SolverName.HEURISTIC,
                               solve_ms=round((_time.perf_counter() - t_start) * 1000, 1), schedule=None,
                               feasible=False, deliveries_met=False, gaps=[f"heuristic error: {e}"])


def greedy_assign(ctx: _Ctx) -> dict[str, tuple[str, int]]:
    ranges = sorted(ctx.slot_ranges() + [(a, b) for a, b, _ in ctx.short_windows])
    short = [(a, b) for a, b, _ in ctx.short_windows]
    free_at = {p: 0 for p in F.CURE_PRESS_IDS}
    mold = dict(ctx.press_mold)
    busy: list[tuple[int, int]] = []        # crew-busy half-op intervals (one crew)
    for j in ctx.frozen:
        free_at[j.press] = max(free_at[j.press], j.release)
        busy.append((j.release, j.release + HALF_MIN))
    busy += _locked_busy(ctx)
    press_busy: dict[str, list[tuple[int, int, str]]] = {p: [] for p in F.CURE_PRESS_IDS}
    for j in ctx.frozen:
        press_busy[j.press].append((j.mold_in, j.release, _mold_key(j)))
    for j in ctx.locked:
        press_busy[j.press].append((j.mold_in, j.mold_in + j.cure_min, _mold_key(j)))
    assign: dict[str, tuple[str, int]] = {}
    order = sorted(ctx.free, key=lambda j: (j.due if j.due is not None else 10**9, -j.priority,
                                            j.orig_mold_in if j.orig_mold_in is not None else 10**9, j.id))
    for j in order:
        best = None
        mi_ranges = ctx.mold_in_ranges(j.size)
        for p in j.eligible:
            t = _first_feasible(ranges, max(free_at[p], j.lb), j.cure_min, busy, ctx.hmax, press_busy[p], mi_ranges,
                                _mold_key(j))
            if t is None:
                continue
            cost = W_DEMOLD * (1 + max(0, j.priority)) * (t + j.cure_min)
            if j.orig_press is not None and p != j.orig_press:
                cost += PEN_MOVE + (PEN_SAME_HEADER if p in ctx.suspect else 0)
            if mold.get(p) is not None and mold[p] != _mold_key(j):
                cost += PEN_MOLD_CHANGE
            cost += _size_home_pen(ctx, j, p)
            if _in_ranges(t, short) or _in_ranges(t + j.cure_min, short):
                cost += PEN_SHORT_OP
            if j.due is not None:
                cost += W_LATE * max(0, t + j.cure_min - j.due)
            key = (cost, p != j.orig_press, p)
            if best is None or key < best[0]:
                best = (key, p, t)
        if best is None:
            continue
        _, p, t = best
        assign[j.id] = (p, t)
        free_at[p] = t + j.cure_min
        press_busy[p].append((t, t + j.cure_min, _mold_key(j)))
        mold[p] = _mold_key(j)
        busy.extend([(t + HALF_MIN, t + OP_MIN), (t + j.cure_min, t + j.cure_min + HALF_MIN)])
    return assign


def _size_home_pen(ctx: _Ctx, j: _Job, p: str) -> int:
    """M/L presses keep the size they are curing now (page 6: M, L 2 presses each). A rush order
    (priority > 0, e.g. page 11 #A-311) may take any shared press."""
    home = ctx.press_size.get(p)
    return PEN_SIZE_HOME if home is not None and home is not j.size and j.priority <= 0 else 0


def _first_feasible(ranges, t, cure_min, busy, hmax, press_busy=(), mi_ranges=None, mold: str = "") -> int | None:
    """First grid t' >= t with mold-in (in mi_ranges: staffed op + staffed prep) and de-mold
    (t'+cure, in staffed op ranges), no op clash, and no press clash (press_busy entries
    (start, end, mold); another mold needs MOLD_GAP_MIN between de-mold and mold-in)."""
    mi_ranges = ranges if mi_ranges is None else mi_ranges
    while t <= hmax:
        nt = _next_slot_r(mi_ranges, -(-t // GRID_MIN) * GRID_MIN)
        if nt is None:
            return None
        if nt % GRID_MIN:
            t = nt
            continue
        nd = nt + cure_min
        if not _in_ranges(nd, ranges):
            nd2 = _next_slot_r(ranges, nd)
            if nd2 is None:
                return None
            t = max(nt + 1, nd2 - cure_min)
            continue
        blocking = _press_clash(list(press_busy), nt, nd, mold)
        if blocking:
            t = max(blocking)
            continue
        mine = ((nt + HALF_MIN, nt + OP_MIN), (nd, nd + HALF_MIN))
        if any(a < y and x < b for a, b in busy for x, y in mine):
            t = nt + 5
            continue
        return nt
    return None


def _next_slot_r(ranges, t) -> int | None:
    for a, b in ranges:
        if b >= t:
            return max(a, t)
    return None


# =====================================================================================
# Primary: OR-Tools CP-SAT
# =====================================================================================
class CpSatScheduler:
    """OR-Tools CP-SAT. Decision: press + mold-in per job. Hard: M/L only on factory.ML_PRESSES,
    small on SMALL_ONLY_PRESSES, one tire per press, unavailable presses excluded,
    de-mold/mold-in inside staffed windows, one op per crew at a time. Soft: lateness, mold
    changes, press moves, short-staffed ops, early de-mold. Page 6 principles 1–5."""

    def __init__(self, num_workers: int = 8, stall_s: float | None = STALL_S):
        self.num_workers = num_workers
        self.stall_s = stall_s

    def solve(self, req: SolveRequest) -> SolveResult:
        t_start = _time.perf_counter()
        try:
            from ortools.sat.python import cp_model
            from ortools.util.python.sorted_interval_list import Domain
        except Exception as e:  # noqa: BLE001
            return SolveResult(status=SolveStatus.ERROR, solver=SolverName.CP_SAT, solve_ms=0.0, schedule=None,
                               feasible=False, deliveries_met=False, gaps=[f"ortools unavailable: {e}"])
        bad = _bad_request(req)
        if bad:   # never hand OR-Tools a degenerate model: the backup solver takes it (page 7)
            return SolveResult(status=SolveStatus.ERROR, solver=SolverName.CP_SAT,
                               solve_ms=round((_time.perf_counter() - t_start) * 1000, 1), schedule=None,
                               feasible=False, deliveries_met=False, gaps=[f"cp-sat skipped: {bad}"])
        try:
            ctx = _prepare(req)
            if not ctx.free:
                return _finalize(ctx, {}, SolverName.CP_SAT, SolveStatus.OPTIMAL, t_start, 0.0)
            ranges = sorted(ctx.slot_ranges() + [(a, b) for a, b, _ in ctx.short_windows])
            ranges = _merge(ranges)
            if not ranges:
                return SolveResult(status=SolveStatus.INFEASIBLE, solver=SolverName.CP_SAT,
                                   solve_ms=round((_time.perf_counter() - t_start) * 1000, 1), schedule=None,
                                   feasible=False, deliveries_met=False, gaps=["no staffed window in horizon"])
            m = cp_model.CpModel()
            dom = Domain.FromIntervals([[a, b] for a, b in ranges])
            # hint: the previous plan where it still fits (repair, not re-plan), else greedy
            hint = greedy_assign(ctx)
            # OR-Tools 9.15 aborts the whole process (SIGABRT in QuickSolveWithHint) when a model
            # with hints turns out infeasible, e.g. a short horizon that cannot hold every job of
            # the previous plan. Hint only when greedy placed every job: the hint is then a
            # complete feasible solution, so the model is feasible. Otherwise solve unhinted.
            use_hint = all(j.id in hint for j in ctx.free)
            for j in ctx.free:
                if (j.orig_press in j.eligible and j.orig_mold_in is not None and j.orig_mold_in >= j.lb
                        and _in_ranges(j.orig_mold_in, ctx.mold_in_ranges(j.size))
                        and _in_ranges(j.orig_mold_in + j.cure_min, ranges)):
                    hint[j.id] = (j.orig_press, j.orig_mold_in)
            obj = []
            press_iv: dict[str, list] = {p: [] for p in F.CURE_PRESS_IDS}
            op_iv = []
            # frozen
            for j in ctx.frozen:
                start = j.mold_in
                end = max(j.release, start + 1)
                press_iv[j.press].append(m.NewIntervalVar(start, end - start, end, f"fz_{j.id}"))
                op_iv.append(m.NewIntervalVar(j.release, HALF_MIN, j.release + HALF_MIN, f"fzop_{j.id}"))
            for k, (a, b) in enumerate(_locked_busy(ctx)):
                op_iv.append(m.NewIntervalVar(a, b - a, b, f"lk_{k}"))
            for j in ctx.locked:
                press_iv[j.press].append(m.NewIntervalVar(j.mold_in, j.cure_min, j.mold_in + j.cure_min, f"lkp_{j.id}"))
            xs: dict[tuple[str, str], object] = {}
            mi_vars = {}
            for j in ctx.free:
                gr = [[-(-max(a, j.lb) // GRID_MIN), min(b, ctx.hmax) // GRID_MIN] for a, b in ctx.mold_in_ranges(j.size)]
                gr = [x for x in gr if x[0] <= x[1]]
                if not gr or not j.eligible:
                    continue
                gi = m.NewIntVarFromDomain(Domain.FromIntervals(gr), f"g_{j.id}")   # mold-in on a 15-min grid
                mi = GRID_MIN * gi
                de = mi + j.cure_min
                m.AddLinearExpressionInDomain(de, dom)          # de-mold inside a staffed window
                mi_vars[j.id] = (gi, de)
                lits = []
                for p in j.eligible:
                    x = m.NewBoolVar(f"x_{j.id}_{p}")
                    xs[(j.id, p)] = x
                    lits.append(x)
                    press_iv[p].append(m.NewOptionalFixedSizeIntervalVar(mi, j.cure_min, x, f"iv_{j.id}_{p}"))
                    if j.orig_press is not None and p != j.orig_press:
                        obj.append((PEN_MOVE + (PEN_SAME_HEADER if p in ctx.suspect else 0)) * x)
                    pm = ctx.press_mold.get(p)
                    if pm is not None and pm != _mold_key(j):
                        obj.append(PEN_MOLD_CHANGE * x)
                    if _size_home_pen(ctx, j, p):
                        obj.append(PEN_SIZE_HOME * x)
                m.AddExactlyOne(lits)
                op_iv.append(m.NewFixedSizeIntervalVar(mi + HALF_MIN, HALF_MIN, f"opi_{j.id}"))
                op_iv.append(m.NewFixedSizeIntervalVar(de, HALF_MIN, f"opd_{j.id}"))
                obj.append(W_DEMOLD * (1 + max(0, j.priority)) * de)      # rush orders out first
                if j.due is not None:
                    late = m.NewIntVar(0, ctx.hmax + j.cure_min, f"late_{j.id}")
                    m.Add(late >= de - j.due)
                    obj.append(W_LATE * late)
                if (ctx.affected and j.orig_press is not None and j.orig_press not in ctx.affected
                        and (j.id, j.orig_press) in xs and j.orig_mold_in is not None and j.orig_mold_in % GRID_MIN == 0
                        and any(lo <= j.orig_mold_in // GRID_MIN <= hi for lo, hi in gr)):
                    # repair, not re-plan: knock-on re-timings on untouched presses cost extra
                    keep = m.NewBoolVar(f"keep_{j.id}")
                    m.Add(gi == j.orig_mold_in // GRID_MIN).OnlyEnforceIf(keep)
                    m.AddImplication(keep, xs[(j.id, j.orig_press)])
                    obj.append(PEN_DISTURB * (1 - keep))
                if j.orig_mold_in is not None:
                    dev = m.NewIntVar(0, 2 * (ctx.hmax + abs(j.orig_mold_in)), f"dev_{j.id}")
                    m.AddAbsEquality(dev, mi - j.orig_mold_in)
                    obj.append(W_SHIFT * dev)
                for (a, b, _s) in ctx.short_windows:
                    for var, tag in ((mi, "i"), (de, "d")):
                        if a > ctx.hmax + j.cure_min:
                            continue
                        ge, le, inw = m.NewBoolVar(""), m.NewBoolVar(""), m.NewBoolVar(f"short{tag}_{j.id}_{a}")
                        m.Add(var >= a).OnlyEnforceIf(ge)
                        m.Add(var < a).OnlyEnforceIf(ge.Not())
                        m.Add(var <= b).OnlyEnforceIf(le)
                        m.Add(var > b).OnlyEnforceIf(le.Not())
                        m.AddBoolOr([ge.Not(), le.Not(), inw])
                        m.AddImplication(inw, ge)
                        m.AddImplication(inw, le)
                        obj.append(PEN_SHORT_OP * inw)
                if use_hint and j.id in hint:
                    hp, ht = hint[j.id]
                    hg = -(-ht // GRID_MIN)
                    # a hint outside the variable's domain (e.g. a previous slot beyond a short
                    # horizon) aborts OR-Tools inside QuickSolveWithHint: hint only valid values
                    if hp in j.eligible and any(lo <= hg <= hi for lo, hi in gr):
                        m.AddHint(gi, hg)
                        for p in j.eligible:
                            m.AddHint(xs[(j.id, p)], 1 if p == hp else 0)
            for p, ivs in press_iv.items():
                if len(ivs) > 1:
                    m.AddNoOverlap(ivs)
            _add_mold_gaps(m, ctx, xs, mi_vars)
            obj += _distinct_mold_terms(m, ctx, xs)
            # labor: one de-mold/mold-in crew, one half-op at a time (de-mold [t, t+15),
            # mold-in [t+15, t+30)), so a de-mold and the next mold-in on one press chain cleanly
            if op_iv:
                m.AddNoOverlap(op_iv)
            m.Minimize(sum(obj))
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = max(0.001, float(req.time_limit_s))
            solver.parameters.num_workers = self.num_workers
            solver.parameters.random_seed = 7
            solver.parameters.interleave_search = True   # reproducible plans for the same input
            solver.parameters.relative_gap_limit = REL_GAP
            st = _solve_with_stall_stop(cp_model, solver, m, self.stall_s)
            if st in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                assign = {}
                for j in ctx.free:
                    if j.id not in mi_vars:
                        continue
                    p = next(p for p in j.eligible if solver.Value(xs[(j.id, p)]))
                    assign[j.id] = (p, GRID_MIN * solver.Value(mi_vars[j.id][0]))
                status = SolveStatus.OPTIMAL if st == cp_model.OPTIMAL else SolveStatus.FEASIBLE
                return _finalize(ctx, assign, SolverName.CP_SAT, status, t_start, solver.ObjectiveValue())
            status = {cp_model.INFEASIBLE: SolveStatus.INFEASIBLE, cp_model.MODEL_INVALID: SolveStatus.ERROR}.get(
                st, SolveStatus.TIMEOUT)
            return SolveResult(status=status, solver=SolverName.CP_SAT,
                               solve_ms=round((_time.perf_counter() - t_start) * 1000, 1), schedule=None,
                               feasible=False, deliveries_met=False, gaps=[f"cp-sat {solver.StatusName(st)}"])
        except Exception as e:  # noqa: BLE001
            return SolveResult(status=SolveStatus.ERROR, solver=SolverName.CP_SAT,
                               solve_ms=round((_time.perf_counter() - t_start) * 1000, 1), schedule=None,
                               feasible=False, deliveries_met=False, gaps=[f"cp-sat error: {e}"])


def _add_mold_gaps(m, ctx: _Ctx, xs: dict, mi_vars: dict) -> None:
    """Principle 4 as a hard constraint: two tires on one press with different molds are at
    least MOLD_GAP_MIN apart (de-mold -> next mold-in). Pairwise is exact: between two tires of
    different molds there is always a change, so some consecutive pair carries the gap."""
    for p in F.CURE_PRESS_IDS:
        fixed = [(j.mold_in, j.release, _mold_key(j), True) for j in ctx.frozen if j.press == p]
        fixed += [(j.mold_in, j.mold_in + j.cure_min, _mold_key(j), False) for j in ctx.locked if j.press == p]
        free = [(GRID_MIN * mi_vars[j.id][0], mi_vars[j.id][1], _mold_key(j), xs[(j.id, p)])
                for j in ctx.free if j.id in mi_vars and (j.id, p) in xs]
        for s0, e0, k0, is_frozen in fixed:
            for s1, e1, k1, x1 in free:
                if k0 == k1:
                    continue
                if is_frozen:          # in progress: always before every free tire
                    m.Add(s1 >= e0 + MOLD_GAP_MIN).OnlyEnforceIf(x1)
                    continue
                before = m.NewBoolVar("")
                m.Add(s1 >= e0 + MOLD_GAP_MIN).OnlyEnforceIf([x1, before])
                m.Add(e1 + MOLD_GAP_MIN <= s0).OnlyEnforceIf([x1, before.Not()])
        for i, (s0, e0, k0, x0) in enumerate(free):
            for s1, e1, k1, x1 in free[i + 1:]:
                if k0 == k1:
                    continue
                before = m.NewBoolVar("")
                m.Add(s1 >= e0 + MOLD_GAP_MIN).OnlyEnforceIf([x0, x1, before])
                m.Add(s0 >= e1 + MOLD_GAP_MIN).OnlyEnforceIf([x0, x1, before.Not()])


def _distinct_mold_terms(m, ctx: _Ctx, xs: dict) -> list:
    """Fewer mold changes (principle 4) between planned tires too: each extra mold a press
    carries costs PEN_MOLD_CHANGE (a lower bound on its changes). The in-progress and locked
    molds count as already present."""
    terms = []
    for p in F.CURE_PRESS_IDS:
        present = {_mold_key(j) for j in ctx.frozen + ctx.locked if j.press == p}
        by_mold: dict[str, list] = {}
        for j in ctx.free:
            if (j.id, p) in xs:
                by_mold.setdefault(_mold_key(j), []).append(xs[(j.id, p)])
        for k, lits in by_mold.items():
            if k in present:
                continue
            used = m.NewBoolVar(f"mold_{p}_{k}")
            for x in lits:
                m.AddImplication(x, used)
            terms.append(PEN_MOLD_CHANGE * used)
    return terms


def _bad_request(req: SolveRequest) -> str | None:
    if not MIN_HORIZON_DAYS <= req.horizon_days <= MAX_HORIZON_DAYS:
        return f"horizon_days={req.horizon_days} outside [{MIN_HORIZON_DAYS}, {MAX_HORIZON_DAYS}]"
    if not math.isfinite(req.time_limit_s) or req.time_limit_s <= 0:
        return f"time_limit_s={req.time_limit_s} must be > 0"
    for p, h in req.cure_overrides_h.items():
        if not math.isfinite(h) or not 0 < h <= 96:
            return f"cure override {p}={h} h outside (0, 96]"
    return None


def _solve_with_stall_stop(cp_model, solver, model, stall_s: float | None):
    """Solve; stop early once the incumbent has not improved for `stall_s` seconds. CP-SAT finds
    its final plan in ~1 s on the demo but cannot prove optimality quickly (staffed-window
    domains give a weak bound), so waiting for the full time limit buys nothing."""
    if not stall_s:
        return solver.Solve(model)
    import threading

    class _CB(cp_model.CpSolverSolutionCallback):
        def __init__(self):
            super().__init__()
            self.best = None
            self.last = _time.perf_counter()

        def on_solution_callback(self):
            v = self.ObjectiveValue()
            if self.best is None or v < self.best - 1e-6:
                self.best, self.last = v, _time.perf_counter()

    cb = _CB()
    done = threading.Event()

    def watch():
        while not done.wait(0.05):
            if cb.best is not None and _time.perf_counter() - cb.last > stall_s:
                solver.StopSearch()
                return

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    try:
        return solver.Solve(model, cb)
    finally:
        done.set()
        th.join(timeout=1)


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


class FallbackScheduler:
    """CP-SAT first; on TIMEOUT/ERROR/INFEASIBLE use backup and set fell_back=True."""

    def __init__(self, primary: Scheduler | None = None, backup: Scheduler | None = None):
        self.primary = primary if primary is not None else CpSatScheduler()
        self.backup = backup if backup is not None else HeuristicScheduler()

    def solve(self, req: SolveRequest) -> SolveResult:
        try:
            res = self.primary.solve(req)
        except Exception as e:  # noqa: BLE001
            res = SolveResult(status=SolveStatus.ERROR, solver=SolverName.CP_SAT, solve_ms=0.0, schedule=None,
                              feasible=False, deliveries_met=False, gaps=[f"primary raised: {e}"])
        if res.schedule is not None and res.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE):
            return res
        back = self.backup.solve(req)
        note = f"主求解器 {res.solver.value} {res.status.value}，改用備援 {back.solver.value}"
        sched = back.schedule
        if sched is not None:
            sched = sched.model_copy(update={"warnings": [note, *sched.warnings]})
        return back.model_copy(update={"fell_back": True, "schedule": sched,
                                       "solve_ms": round(res.solve_ms + back.solve_ms, 1)})
