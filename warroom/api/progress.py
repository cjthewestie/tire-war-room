"""Panels ① 訂單進度 and ② 半成品進度, derived from live plant state instead of the page-2 seed.

① remaining_h: the page-2 estimate (factory.DEMO_ORDER_PROGRESS) is the time left with every press
of that size class available. It is scaled by tires left (sim) and by press capacity: a press that is
out of service or held by the safety core stops producing, so the classes it serves take longer
(CURE-07 停用 -> 中／大胎 4 -> 3 presses -> x4/3). The note says which press is lost and whether the
solver kept the scheduled deliveries.

② WIP: when the focus event is on a curing press, the panel follows the tire in that press (its parts
A–D are already bonded and in the mould) with its cure progress; otherwise it is the sim's page-2
tire 大胎 #07, whose part states follow the A/B/C machines live.
"""
from __future__ import annotations

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


def order_progress(sim: Any, events: list[Event]) -> list[OrderProgress]:
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
        avail = len(pool) - len(down)
        names = "、".join(f"{_press_no(p)} 號機" for p in down)
        if avail <= 0:
            out.append(o.model_copy(update={"note": f"{names}停用：無可用硫化機，剩餘時間無法估算"}))
            continue
        note = f"{names}停用：可用硫化機 {avail}/{len(pool)} 台，剩餘時間 ×{len(pool) / avail:.2f}"
        if solve is not None:
            late = solve.get("late_orders") or []
            note += "；已排訂單交期不受影響" if solve.get("deliveries_met", not late) else f"；延誤：{'、'.join(late)}"
        out.append(o.model_copy(update={"remaining_h": round(o.remaining_h * len(pool) / avail, 1), "note": note}))
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
