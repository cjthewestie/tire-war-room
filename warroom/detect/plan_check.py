"""Plan-vs-actual check (page 6, box 4: 「事件偵測：每分鐘比對計畫與實際，異常喚醒指揮官」).

Once per sim minute the active schedule is compared with the plant state. Three slips are
checked, each from data the plant really reports:

* de-mold late   -- a press still holds a tire SLIP_GRACE_MIN after its planned de-mold time
                    (the cure job's planned_demold_at, which already includes any approved
                    set_cure_time extension);
* mold-in late   -- a scheduled job's mold-in time T passed SLIP_GRACE_MIN ago and its press has
                    stood empty the whole time since T;
* off plan       -- a press holds a cure job the active schedule does not have on that press
                    (neither its job id nor the same order with a mold-in within SLIP_GRACE_MIN),
                    e.g. a mold loaded on the old press after an approved move;
* WIP behind T   -- the tire being built for the next press (page 2 panel ②) has a part that is
                    not started at its back-propagated start (A feed T-1h40/2h30/3h30), or a
                    part not ready (waiting / alert) when bonding must start (T-40min/1h/1h30).

Each slip is one Anomaly of kind SCHEDULE_SLIP, raised once per (job, task), and wakes the
commander like a sensor anomaly. A machine that already has an open event is skipped: that
event owns the slip. Pure and deterministic: time always comes from the caller.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from warroom.contracts import (
    Anomaly,
    AnomalyKind,
    Channel,
    MachineKind,
    MachineState,
    MachineStatus,
    Schedule,
    SemiFinishedProgress,
    Severity,
)
from warroom.factory import BACKPROP, CURE_PRESS_IDS, MACHINE_BY_ID

CHECK_PERIOD_S = 60.0           # page 6: every minute
SLIP_GRACE_MIN = 30.0           # de-mold / mold-in take 30 min (page 6 sim conditions)
WIP_GRACE_MIN = 15.0
LOOKBACK_H = 6.0                # older planned mold-ins are history, not a fresh slip
CRITICAL_LATE_MIN = 60.0
TASK_TEXT = {"demold": "出模", "mold_in": "入模", "a_feed": "A 線投料", "bond": "貼合", "off_plan": "計畫外入模"}


def _label(mid: str) -> str:
    d = MACHINE_BY_ID[mid]
    if d.kind is MachineKind.CURING_PRESS:
        return f"{int(d.short_name)} 號"
    return f"{d.line.value} 線{d.name}" if d.line.value in ("A", "B", "C", "D") else d.name


class PlanWatcher:
    """Compares plan with actual at most once per sim minute; debounced per (job, task)."""

    def __init__(self, period_s: float = CHECK_PERIOD_S, grace_min: float = SLIP_GRACE_MIN,
                 wip_grace_min: float = WIP_GRACE_MIN):
        self.period_s = period_s
        self.grace = timedelta(minutes=grace_min)
        self.wip_grace = timedelta(minutes=wip_grace_min)
        self._last: datetime | None = None
        self._seen: set[tuple] = set()
        self._empty_since: dict[str, datetime] = {}

    def due(self, now: datetime) -> bool:
        return self._last is None or (now - self._last).total_seconds() >= self.period_s

    def reset(self) -> None:
        self._last = None
        self._seen.clear()
        self._empty_since.clear()

    def check(self, now: datetime, schedule: Schedule | None, machines: Iterable[MachineState],
              semi: SemiFinishedProgress | None = None) -> list[Anomaly]:
        self._last = now
        by_id = {m.machine_id: m for m in machines}
        for pid in CURE_PRESS_IDS:
            m = by_id.get(pid)
            if m is not None and m.cure_job is None:
                self._empty_since.setdefault(pid, now)
            else:
                self._empty_since.pop(pid, None)
        out: list[Anomaly] = []
        out += self._demold(now, by_id)
        if schedule is not None:
            out += self._mold_in(now, schedule, by_id)
            out += self._off_plan(now, schedule, by_id)
        if semi is not None:
            out += self._wip(now, semi, by_id)
        return out

    # ------------------------------------------------------------------ checks
    def _demold(self, now: datetime, by_id: dict[str, MachineState]) -> list[Anomaly]:
        out = []
        for pid in CURE_PRESS_IDS:
            m = by_id.get(pid)
            if m is None or m.cure_job is None or m.active_event_id:
                continue
            cj = m.cure_job
            if now < cj.planned_demold_at + self.grace:
                continue
            key = (cj.job_id, "demold")
            if key in self._seen:
                continue
            self._seen.add(key)
            out.append(self._make(now, pid, "demold", cj.planned_demold_at, cj.order_id,
                                  f"{_label(pid)} {cj.order_id} 計畫 {cj.planned_demold_at:%m/%d %H:%M} 出模，"
                                  f"仍在模內（{m.status.value}）"))
        return out

    def _mold_in(self, now: datetime, schedule: Schedule, by_id: dict[str, MachineState]) -> list[Anomaly]:
        out = []
        for j in schedule.jobs:
            if j.frozen or not (now - timedelta(hours=LOOKBACK_H) <= j.mold_in <= now - self.grace):
                continue
            m = by_id.get(j.press_id)
            if m is None or m.active_event_id or m.status is MachineStatus.OUT_OF_SERVICE:
                continue
            if m.cure_job is not None:       # press busy: either done, or a de-mold slip reports it
                continue
            # empty the whole time since T (a normal 30-min de-mold/mold-in change is not a slip)
            since = self._empty_since.get(j.press_id)
            if since is None or since > j.mold_in + timedelta(seconds=self.period_s):
                continue
            key = (j.id, "mold_in")
            if key in self._seen:
                continue
            self._seen.add(key)
            out.append(self._make(now, j.press_id, "mold_in", j.mold_in, j.order_id,
                                  f"{_label(j.press_id)} {j.order_id} 計畫 {j.mold_in:%m/%d %H:%M} 入模，"
                                  f"硫化機仍空（{m.status.value}）"))
        return out

    def _off_plan(self, now: datetime, schedule: Schedule, by_id: dict[str, MachineState]) -> list[Anomaly]:
        out = []
        for pid in CURE_PRESS_IDS:
            m = by_id.get(pid)
            if m is None or m.cure_job is None or m.active_event_id:
                continue
            cj = m.cure_job
            on_press = [j for j in schedule.jobs if j.press_id == pid]
            if any(j.id == cj.job_id for j in on_press):
                continue
            if any(j.order_id == cj.order_id and abs(j.mold_in - cj.mold_in_at) <= self.grace for j in on_press):
                continue
            key = (cj.job_id, "off_plan")
            if key in self._seen:
                continue
            self._seen.add(key)
            elsewhere = next((j.press_id for j in schedule.jobs if j.id == cj.job_id), None)
            where = f"，計畫在 {_label(elsewhere)}" if elsewhere and elsewhere != pid else ""
            out.append(self._make(now, pid, "off_plan", cj.mold_in_at, cj.order_id,
                                  f"{_label(pid)} 正在硫化 {cj.order_id}（{cj.mold_in_at:%m/%d %H:%M} 入模），"
                                  f"生效排程此機台沒有這一模{where}"))
        return out

    def _wip(self, now: datetime, semi: SemiFinishedProgress, by_id: dict[str, MachineState]) -> list[Anomaly]:
        T = semi.planned_mold_in_at
        if T is None or now >= T:
            return []
        bp = BACKPROP[semi.size]
        a_feed = T - timedelta(minutes=bp["a_feed_min"])
        bond = T - timedelta(minutes=bp["bond_start_min"])
        out = []
        for p in semi.parts:
            if p.machine_id is None:
                continue
            m = by_id.get(p.machine_id)
            if m is not None and m.active_event_id:
                continue
            due = []
            if p.part == "A" and p.state == "waiting" and now >= a_feed + self.wip_grace:
                due.append(("a_feed", a_feed))
            if p.state in ("waiting", "alert") and now >= bond + self.wip_grace:
                due.append(("bond", bond))
            for task, planned in due:
                key = (semi.order_id, T, p.part, task)
                if key in self._seen:
                    continue
                self._seen.add(key)
                state = "未開始" if p.state == "waiting" else "異常中"
                out.append(self._make(now, p.machine_id, task, planned, semi.order_id,
                                      f"{semi.tire_label} {p.label} {state}：{TASK_TEXT[task]}應於 {planned:%H:%M} "
                                      f"開始（{T:%H:%M} 入硫化機往回推）"))
        return out

    # ------------------------------------------------------------------ anomaly
    def _make(self, now: datetime, mid: str, task: str, planned: datetime, order_id: str, text: str) -> Anomaly:
        late = max(0.0, (now - planned).total_seconds() / 60.0)
        sev = Severity.CRITICAL if late >= CRITICAL_LATE_MIN else Severity.WARN
        msg = f"排程延誤：{text}，已落後 {late:.0f} 分（計畫與實際比對）"
        if task == "off_plan":          # not late: the plant runs a mold the plan does not have
            sev, msg = Severity.WARN, f"偏離排程：{text}（計畫與實際比對）"
        return Anomaly(
            id=f"AN-{now:%Y%m%d-%H%M%S}-{mid}-SLIP-{task}", ts=now, machine_id=mid,
            kind=AnomalyKind.SCHEDULE_SLIP,
            channel=Channel.TEMP,       # required by the contract; a slip has no sensor channel
            value=round(late, 1), baseline=0.0, prediction=None, severity=sev,
            message=msg,
            related_machines=[], detection_latency_s=None)
