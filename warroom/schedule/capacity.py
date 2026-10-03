"""Page-6 capacity simulation: 每台硫化機每週可做幾顆. Owner: schedule builder.

Discrete-event simulation of one press per size, steady state (weeks 3..N averaged), plus a
fleet simulation for the 100-tire lead times. Stated page-6 conditions: Mon–Sat, Saturday night
shift runs to Sun 05:00, M and L 2 presses each, no mold change counted, de-mold/mold-in 30 min.

Cycle convention (documented assumption): the 30-min de-mold/mold-in must fit inside a staffed
window, and the next cure starts when that op starts — i.e. the op is counted inside the
nominal 24/28/34 h. With this convention 現況 reproduces page 6 exactly (6/5/3). Counting the
30 min on top of the cure instead gives 6/4/3 for 現況, further from the page.

Policies (who can de-mold, page 4 Gantt, page 6 premise and principles):
- 現況: only the curing team, day shift 08:00–17:00 Mon–Sat. Reload as soon as staffed.
- 只延長班表: optimised premise — curing 06–15 + 14–23, BODY night 20:00–05:00 also de-molds,
  Mon–Sat nights (Sat night runs to Sun 05:00). Reload as soon as staffed (盡快重入).
- AI 排出模時刻: same staffing + principle 3 週末不斷火 (Saturday night loads the last round,
  it de-molds Monday 清晨 00:00–05:00 instead of waiting for Monday 06:00, no re-preheat).
  For one press, reloading at the earliest staffed slot already maximises the count (any later
  start can only push later de-molds later), so the policy difference is which slots exist.

Lead time (page 6 box): M&L 100 each on the 4 shared presses (2 M + 2 L, a press switches size
when its size is done, no mold change counted); small 100 on the 7 small presses.
"""
from __future__ import annotations

from dataclasses import dataclass

from warroom import factory as F
from warroom.contracts import CapacityReport, CapacityRowOut, TireSize

WEEK_H = 168.0
MON_SAT = tuple(range(6))
OP_H = F.DEMOLD_MOLDIN_MIN / 60.0


@dataclass(frozen=True)
class Policy:
    label: str
    windows: tuple[tuple[float, float, tuple[int, ...]], ...]   # (start_h, end_h, weekdays); end may pass 24
    note: str


POLICIES: tuple[Policy, ...] = (
    Policy("現況", ((8, 17, MON_SAT),), "硫化機組早班 08–17 出入模"),
    Policy("只延長班表", ((6, 23, MON_SAT), (20, 29, MON_SAT)),
           "硫化機組 06–15、14–23；胎體／胎面膠組晚班 20–05 兼出入模；週六晚班到週日 05:00"),
    Policy("AI 排出模時刻", ((6, 23, MON_SAT), (20, 29, MON_SAT), (0, 5, (0,))),
           "同上 + 週末不斷火：週六晚班送最後一輪，週一清晨 00–05 出模"),
)


def _windows(policy: Policy, weeks: int) -> list[tuple[float, float]]:
    iv = []
    for w in range(weeks):
        for d in range(7):
            for s, e, days in policy.windows:
                if d in days:
                    iv.append((w * WEEK_H + d * 24 + s, w * WEEK_H + d * 24 + e))
    iv.sort()
    merged: list[list[float]] = []
    for a, b in iv:
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _next_slot(t: float, windows: list[tuple[float, float]]) -> float | None:
    for a, b in windows:
        s = max(t, a)
        if s + OP_H <= b + 1e-9:
            return s
    return None


def press_completions(policy: Policy, cure_h: float, weeks: int = 40, start_h: float = 0.0) -> list[float]:
    """Cure-end times of one press reloading at the earliest staffed slot."""
    win = _windows(policy, weeks + 2)
    ends: list[float] = []
    t = _next_slot(start_h, win)
    while t is not None:
        e = t + cure_h
        if e > weeks * WEEK_H:
            break
        ends.append(e)
        t = _next_slot(e, win)
    return ends


def weekly_per_press(policy: Policy, cure_h: float, weeks: int = 40, warmup: int = 2) -> float:
    ends = press_completions(policy, cure_h, weeks)
    n = sum(1 for e in ends if warmup * WEEK_H <= e < weeks * WEEK_H)
    return n / (weeks - warmup)


def fleet_weeks(policy: Policy, demand: dict[TireSize, int], presses: dict[TireSize, int]) -> float:
    """Weeks until `demand` is cured. Each press starts on its size; when a size's demand is fully
    loaded, its presses switch to the other size (no mold change counted, page 6)."""
    win = _windows(policy, 60)
    left = dict(demand)            # tires not yet loaded
    t_free = []                    # (time, size)
    for s, n in presses.items():
        t_free += [(0.0, s)] * n
    last_end = 0.0
    while any(v > 0 for v in left.values()):
        t_free.sort(key=lambda x: x[0])
        t, size = t_free.pop(0)
        if left.get(size, 0) <= 0:
            size = next(s for s, v in left.items() if v > 0)
        slot = _next_slot(t, win)
        if slot is None:
            raise RuntimeError("simulation horizon too short")
        left[size] -= 1
        end = slot + F.CURE_HOURS[size]
        last_end = max(last_end, end)
        t_free.append((end, size))
    return last_end / WEEK_H


def capacity_report() -> CapacityReport:
    """Simulate weekly per-press output for 現況 / 只延長班表 / AI 排出模時刻 under page-6
    conditions. Rows are simulated values, not the proposal's (compare factory.CAPACITY_TARGETS).
    Lead times: M&L 100 each on 2+2 presses, small 100 on 7 presses, 現況 vs AI 排出模時刻."""
    rows = [CapacityRowOut(label=p.label,
                           small=round(weekly_per_press(p, F.CURE_HOURS[TireSize.S]), 2),
                           medium=round(weekly_per_press(p, F.CURE_HOURS[TireSize.M]), 2),
                           large=round(weekly_per_press(p, F.CURE_HOURS[TireSize.L]), 2))
            for p in POLICIES]
    cur, ai = POLICIES[0], POLICIES[2]
    ml_demand = {TireSize.M: 100, TireSize.L: 100}
    ml_presses = {TireSize.M: 2, TireSize.L: 2}
    s_demand, s_presses = {TireSize.S: 100}, {TireSize.S: len(F.SMALL_ONLY_PRESSES)}
    ml = (round(fleet_weeks(cur, ml_demand, ml_presses), 1), round(fleet_weeks(ai, ml_demand, ml_presses), 1))
    sm = (round(fleet_weeks(cur, s_demand, s_presses), 1), round(fleet_weeks(ai, s_demand, s_presses), 1))
    conditions = (F.CAPACITY_SIM_CONDITIONS + ". Steady state of a 40-week discrete-event run; 30-min op inside "
                  "a staffed window, counted inside the nominal cure. " +
                  "; ".join(f"{p.label}: {p.note}" for p in POLICIES))
    return CapacityReport(rows=rows, ml_100_weeks=ml, small_100_weeks=sm, conditions=conditions)


def lead_time_from_rates(medium: float, large: float, small: float, ml_presses: int = 4,
                         small_presses: int = len(F.SMALL_ONLY_PRESSES), qty: int = 100) -> tuple[float, float]:
    """Closed form behind the page-6 box: press-weeks pooled over the shared presses.
    (100/5 + 100/3)/4 = 13.3; (100/6 + 100/4.9)/4 = 9.3; 100/(7·6) = 2.4; 100/(7·7) = 2.0."""
    return (qty / medium + qty / large) / ml_presses, qty / (small_presses * small)
