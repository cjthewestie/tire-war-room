"""Panels ① 訂單進度 and ② 半成品進度, derived from live plant state instead of the page-2 seed.

① remaining_h: the page-2 estimate (factory.DEMO_ORDER_PROGRESS) is the time left with every press
of that size class available. When a press is taken out of service (approved 停用), the stretch comes
from the solver's plans, not a fixed ratio: the cure press-hours the first plan (v1, every press up)
de-molds in its first 7 days, against what the plan in force de-molds in its first 7 days without the
lost press. A press the safety core only holds (扣模, not yet approved out of service) changes no
number: the note says it waits for the supervisor and the new plan. Scheduled deliveries are claimed
only for what that plan holds (本週已排訂單), never for the whole order book.

② WIP: when the focus event is on a curing press, the panel follows the tire in that press (its parts
A–D are already bonded and in the mould) with its cure progress; otherwise it is the sim's page-2
tire 大胎 #07, whose part states follow the A/B/C machines live.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from warroom import factory as F
from warroom.contracts import (
    TERMINAL_STATES,
    CureJobState,
    Event,
    MachineStatus,
    OrderProgress,
    SemiFinishedPart,
    SemiFinishedProgress,
    TireSize,
    ToolName,
)

SIZE_LABEL = {TireSize.S: "小胎", TireSize.M: "中胎", TireSize.L: "大胎"}
LOST_STATUSES = {MachineStatus.OUT_OF_SERVICE, MachineStatus.STOPPED}
PRESSES_FOR: dict[TireSize, tuple[str, ...]] = {
    TireSize.S: F.SMALL_ONLY_PRESSES, TireSize.M: F.ML_PRESSES, TireSize.L: F.ML_PRESSES}


def _press_no(pid: str) -> int:
    return int(pid.split("-")[1])


def _lost_presses(sim: Any) -> set[str]:
    out = set()
    for pid in F.CURE_PRESS_IDS:
        try:
            if sim.machine(pid).status in LOST_STATUSES:
                out.add(pid)
        except Exception:  # noqa: BLE001
            continue
    return out


def _latest_solve(events: list[Event]) -> dict | None:
    for ev in sorted(events, key=lambda e: e.opened_at, reverse=True):
        for sr in reversed(ev.steps):
            if sr.step.tool is ToolName.SOLVE_SCHEDULE and sr.result and sr.result.ok:
                return sr.result.data
    return None


def _status(sim: Any, pid: str) -> MachineStatus | None:
    try:
        return sim.machine(pid).status
    except Exception:  # noqa: BLE001
        return None


def _week_press_hours(version: Any, pool: tuple[str, ...], exclude: set[str]) -> float | None:
    """Cure press-hours the plan de-molds on `pool` in the first 7 days of its horizon."""
    sched = getattr(version, "schedule", None)
    if sched is None:
        return None
    a = sched.horizon_start
    b = a + timedelta(days=7)
    return sum(j.cure_h for j in sched.jobs if j.press_id in pool and j.press_id not in exclude and a <= j.demold < b)


def _capacity_stretch(store: Any, pool: tuple[str, ...], down: set[str]) -> tuple[float, float, float, int] | None:
    """(factor, press-hours/week with every press, press-hours/week in force, active version) from the
    solver's plans, or None when there is no store or no comparable plan."""
    if store is None:
        return None
    try:
        versions = store.versions()
        active = store.current()
    except Exception:  # noqa: BLE001
        return None
    if not versions or active.version == versions[0].version:
        return None
    full = _week_press_hours(versions[0], pool, set())
    now = _week_press_hours(active, pool, down)
    if not full or not now:
        return None
    return full / now, full, now, active.version


def order_progress(sim: Any, events: list[Event], store: Any = None) -> list[OrderProgress]:
    base = sim.order_progress()
    lost = _lost_presses(sim)
    solve = _latest_solve(events) if lost else None
    out = []
    for o in base:
        pool = PRESSES_FOR[o.size]
        down = sorted(p for p in pool if p in lost)
        if not down:
            out.append(o)
            continue
        out_of_service = [p for p in down if _status(sim, p) is MachineStatus.OUT_OF_SERVICE]
        held = [p for p in down if p not in out_of_service]
        names = lambda ps: "、".join(f"{_press_no(p)} 號機" for p in ps)   # noqa: E731
        if not out_of_service:
            # safety core hold only: no approved change yet, so no new number
            out.append(o.model_copy(update={"note": f"{names(held)}安全核心扣模：待主管核准停用與新排程後重估剩餘時間"}))
            continue
        avail = len(pool) - len(down)
        if avail <= 0:
            out.append(o.model_copy(update={"note": f"{names(down)}停用：無可用硫化機，剩餘時間無法估算"}))
            continue
        st = _capacity_stretch(store, pool, set(down))
        if st is not None and st[0] > 1.0:
            factor, full, now, ver = st
            note = (f"{names(out_of_service)}停用：依生效排程 v{ver}，週硫化 {full:,.0f}→{now:,.0f} 機時"
                    f"（{(now - full) / full:+.0%}），剩餘時間依此延長")
        elif st is not None:
            factor, note = 1.0, f"{names(out_of_service)}停用：生效排程 v{st[3]} 週硫化機時未減少，剩餘時間不變"
        else:
            factor = len(pool) / avail
            note = f"{names(out_of_service)}停用：尚無新排程，依可用硫化機 {avail}/{len(pool)} 台估算"
        if held:
            note += f"；{names(held)}安全核心扣模中"
        if solve is not None:
            late = solve.get("late_orders") or []
            note += "；本週已排訂單交期不受影響" if solve.get("deliveries_met", not late) else f"；延誤：{'、'.join(late)}"
        out.append(o.model_copy(update={"remaining_h": round(o.remaining_h * factor, 1), "note": note}))
    return out


class WipTracker:
    """Remembers the tire in a press when its event opens, so the panel keeps showing it after the
    press is taken out of service and the sim no longer reports a cure job there."""

    def __init__(self) -> None:
        self._jobs: dict[tuple[str, str], CureJobState] = {}

    def semi_finished(self, sim: Any, focus: Event | None) -> SemiFinishedProgress:
        if focus is None or focus.machine_id not in F.CURE_PRESS_IDS:
            return sim.semi_finished()
        m = sim.machine(focus.machine_id)
        key = (focus.id, focus.opened_at.isoformat())     # ids restart after a scenario reset
        if m.cure_job is not None:
            self._jobs[key] = m.cure_job
        job = self._jobs.get(key)
        if job is None:
            return sim.semi_finished()
        n = _press_no(focus.machine_id)
        if m.status in LOST_STATUSES:
            cure_text, cure_state = ("已停用，胎依 SOP 扣留" if m.status is MachineStatus.OUT_OF_SERVICE
                                     else "安全核心扣模待 QC"), "alert"
        else:
            cure_text = f"硫化中 {job.cure_elapsed_h:.1f}／{job.cure_required_h:.1f} h"
            cure_state = "alert" if focus.state not in TERMINAL_STATES else "in_progress"
        parts = [SemiFinishedPart(part=p, label=F.LINE_LABELS[line], status_text="已貼合入模", state="done",
                                  machine_id=None)  # type: ignore[arg-type]
                 for p, line in (("A", F.Line.A), ("B", F.Line.B), ("C", F.Line.C), ("D", F.Line.D))]
        return SemiFinishedProgress(tire_label=f"{SIZE_LABEL[job.size]} {job.order_id}（{n} 號機）",
                                    order_id=job.order_id, size=job.size, parts=parts, planned_mold_in_at=None,
                                    note=f"{n} 號機{cure_text}", note_state=cure_state)
