"""Worker tasks and goods status (issue #17). CJ, 2026-10-20: 「我希望這些輪胎也是有補貨跟完成的狀態圈圈 然後人力會去
執行 這樣模擬插單才有意義才會知道貨品狀態 然後審核一個plan之後人員要去執行」.

Worker tasks
  An approved (or gate-allowed R1) write step becomes concrete tasks for named people: who, where, what, and when
  it should be done. CODE builds them from the step's validated args and the plant (`plan_tasks`); the model never
  writes a task. The world then runs them: 待派 (waiting for a free person of the right team) → 前往中 (walking to
  the place) → 執行中 (doing it) → 完成. Every change goes to the task listener (the runtime audits it). A task
  never touches a machine: warm-up, material, mold and cure-time tasks are things people do by hand.

Goods status
  Every tyre in the plan is one item with a status and a progress ring: 待補料 (queued; materials not issued yet)
  → 備料中 (A feed .. bond start, page 6 back-propagation) → 生產中 (bond start .. mold-in) → 硫化中 (in the press,
  progress = cure elapsed / required) → 完成 (de-molded, final inspection and packing) → 待出貨 → 已出貨 (the order
  level, from the dock). A tyre whose order has an open 補料 task stays 待補料 until that task is done: this is the
  link between the people and the goods. The view reads the active schedule and the presses; it never changes
  them.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from warroom.contracts import Line, MachineStatus, TeamId, TireSize
from warroom.factory import CURE_HOURS, MACHINE_BY_ID, MACHINES, TEAM_LABELS

if TYPE_CHECKING:  # pragma: no cover
    from warroom.sim.world import World

TaskStatus = Literal["待派", "前往中", "執行中", "完成"]
TASK_STATUSES: tuple[str, ...] = ("待派", "前往中", "執行中", "完成")
GoodsStatus = Literal["待補料", "備料中", "生產中", "硫化中", "完成", "待出貨", "已出貨"]
GOODS_STATUSES: tuple[str, ...] = ("待補料", "備料中", "生產中", "硫化中", "完成", "待出貨", "已出貨")

# ------------------------------------------------------------------ assumptions (constants)
WALK_SAME_ZONE_MIN = 1.0             # walking inside one zone
WALK_MIN = 3.0                       # walking to another zone of the hall
WALK_DOCK_MIN = 4.0                  # to / from the dock
WORK_MIN: dict[str, float] = {       # how long each kind of task takes once there (factory minutes)
    "暖機": 30.0,                    # SOP-02 (team dataset): 空轉暖機 30 分鐘; 各班開煉機於工作窗開始先暖機 30 分鐘
    "補料": 15.0,                    # carry the compound / parts for the next lots to the line-side bin
    "盤點": 10.0,                    # check a low material and the delivery in transit
    "換模準備": 20.0,                # fetch the mold, pre-heat check for the next mold-in
    "設定硫化時間": 5.0,             # enter the approved cure time on the press panel (by hand; no PLC write)
    "加班補位": 10.0,                # confirm overtime and take over the missing post
    "調派支援": 10.0,                # walk to the other team and report in
    "停用檢修": 30.0,
    "檢修": 30.0,
    "品管": 15.0,
    "放慢投料": 5.0,
    "暫停投料": 5.0,
    "宣達": 5.0,
}
TASK_KEEP = 40                       # tasks kept in the view (oldest done ones drop first)
UNASSIGNED_ANY_TEAM_MIN = 5.0        # a task nobody of its team can take goes to any free operator after 5 min
RUSH_FEED_LOTS = 8                   # SOP-02: 中胎 8 批 (A 線) -> what one 補料 carries for a medium rush tyre
GOODS_AHEAD_H = 24.0                 # tyres whose mold-in is within the next 24 h are listed
DONE_KEEP = 6                        # de-molded tyres kept in the view
PACK_H = 1.0                         # 完成 → 待出貨 (same as world.PACK_H)
# SOP-02 (team dataset, 密煉機開機暖機與換批): 每班開始先暖機 30 分鐘；開煉機於工作窗開始先暖機. Order: the mixers first,
# then the mills they feed.
WARMUP_ORDER: tuple[tuple[str, str | None], ...] = (
    ("A-MIX", None), ("B-MIX", None), ("A-OPEN", "A-MIX"), ("A-BIGOPEN", "A-MIX"), ("B-OPEN", "B-MIX"),
)
TEAM_HOME: dict[TeamId, str] = {
    TeamId.CURING: "CURE-01", TeamId.BODY: "A-MIX", TeamId.SIDE: "B-MIX", TeamId.BEAD_NYLON: "C-PAYOFF",
    TeamId.BIGOPEN: "A-BIGOPEN", TeamId.BOND: "BOND-01",
}
MACHINE_TEAM: dict[str, TeamId] = {}
for _m in MACHINES:
    MACHINE_TEAM[_m.id] = (TeamId.CURING if _m.line is Line.CURE else TeamId.BIGOPEN if _m.id == "A-BIGOPEN" else
                           {Line.A: TeamId.BODY, Line.B: TeamId.SIDE, Line.C: TeamId.BEAD_NYLON,
                            Line.D: TeamId.BEAD_NYLON, Line.BOND: TeamId.BOND}.get(_m.line, TeamId.BODY))
SIZE_LABEL = {TireSize.S: "小胎", TireSize.M: "中胎", TireSize.L: "大胎"}


def zone_of(machine_id: str | None) -> str:
    if machine_id is None or machine_id not in MACHINE_BY_ID:
        return "staff"
    return f"line_{MACHINE_BY_ID[machine_id].line.value}"


def where_label(machine_id: str | None, zone: str) -> str:
    if machine_id and machine_id in MACHINE_BY_ID:
        return f"{MACHINE_BY_ID[machine_id].name}（{machine_id}）"
    from warroom.sim.world import ZONE_LABELS
    return ZONE_LABELS.get(zone, zone)


# ------------------------------------------------------------------ snapshot models
class _W(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkTask(_W):
    id: str                          # TK-0001
    event_id: str | None
    approval_id: str | None
    step_id: str | None
    kind: str                        # 暖機 / 補料 / 換模準備 / ... (WORK_MIN keys)
    title: str                       # what, in the operator's words
    team: TeamId
    team_label: str
    worker_id: str | None = None
    worker_name: str | None = None
    machine_id: str | None = None
    zone: str
    where: str
    status: TaskStatus
    created_at: datetime
    assigned_at: datetime | None = None
    started_at: datetime | None = None
    done_at: datetime | None = None
    eta: datetime | None = None      # expected done (walk + work from now, or from assignment)
    walk_min: float
    work_min: float
    progress: float = 0.0            # 0..1 over walk + work
    after: str | None = None         # waits for this task (SOP order)
    order_ids: list[str] = []        # 補料 / 換模準備: the orders whose tyres wait for it
    source: Literal["code"] = "code"


class TasksView(_W):
    items: list[WorkTask]
    counts: dict[str, int]           # by status
    open: int


class GoodsItem(_W):
    id: str                          # schedule job id / press job id
    order_id: str
    size: TireSize
    status: GoodsStatus
    progress: float                  # 0..1 within the current status
    machine_id: str | None = None    # where it is now (3D ring above it)
    press_id: str | None = None
    zone: str
    mold_in: datetime | None = None
    stage_end: datetime | None = None
    note: str | None = None
    rush: bool = False
    blocked: bool = False            # waiting for a 補料 task
    late: bool = False               # its feed time passed while still waiting for material


class OrderGoods(_W):
    order_id: str
    size: TireSize
    qty: int
    done: int
    in_press: int
    queued: int                      # tyres planned in the next GOODS_AHEAD_H, not yet in a press
    status: str                      # world order status (待排程 / 生產中 / 完成 / 待出貨 / 已出貨)
    progress: float                  # done / qty
    rush: bool
    due: date
    next_mold_in: datetime | None = None
    moved_h: float | None = None     # how far its next mold-in moved with the last schedule change (+ = later)
    late: bool = False               # the active schedule finishes it after its due date


class GoodsView(_W):
    items: list[GoodsItem]
    orders: list[OrderGoods]
    counts: dict[str, int]
    schedule_id: str | None = None


# ------------------------------------------------------------------ internal task state
@dataclass
class _Task:
    id: str
    event_id: str | None
    approval_id: str | None
    step_id: str | None
    kind: str
    title: str
    team: TeamId
    machine_id: str | None
    zone: str
    created_at: datetime
    after: str | None = None
    order_ids: list[str] = field(default_factory=list)
    any_team_ok: bool = True
    status: str = "待派"
    worker_id: str | None = None
    assigned_at: datetime | None = None
    started_at: datetime | None = None
    done_at: datetime | None = None
    walk_min: float = WALK_MIN
    work_min: float = 10.0


@dataclass
class TaskRequest:
    """What the state machine hands over for one executed write: the step as it ran (validated args), its result,
    the event kind, and code-computed context (the solve's moves for a confirmed version)."""
    event_id: str | None
    approval_id: str | None
    step_id: str | None
    event_kind: str                  # anomaly kind value (shift_start / order_rush / temp_drop / ...)
    tool: str                        # ToolName value
    args: dict[str, Any]
    result: dict[str, Any] = field(default_factory=dict)
    moves: list[dict[str, Any]] = field(default_factory=list)
    staffing: dict[str, Any] | None = None     # the StaffingChange the chosen option carries
    order_ids: list[str] = field(default_factory=list)   # orders this event is about (rush order)


class TaskBoard:
    """The world's task list. Deterministic (no RNG)."""

    def __init__(self, world: World):
        self.w = world
        self._tasks: dict[str, _Task] = {}
        self._seq = 0
        self._listeners: list[Any] = []

    # ---- public
    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def busy_worker_ids(self) -> set[str]:
        return {t.worker_id for t in self._tasks.values() if t.worker_id and t.status in ("前往中", "執行中")}

    def task_of(self, worker_id: str) -> _Task | None:
        return next((t for t in self._tasks.values() if t.worker_id == worker_id and t.status in ("前往中", "執行中")),
                    None)

    def for_event(self, event_id: str) -> list[WorkTask]:
        return [self._view(t) for t in self._tasks.values() if t.event_id == event_id]

    def open_order_ids(self, kind: str = "補料") -> set[str]:
        return {o for t in self._tasks.values() if t.kind == kind and t.status != "完成" for o in t.order_ids}

    def dispatch(self, req: TaskRequest, now: datetime) -> list[WorkTask]:
        specs = plan_tasks(self.w, req, now)
        out = []
        for spec in specs:
            # the same job for the same order at the same place is one task (a confirmed schedule and its 派工 can
            # both ask for the mold of an inserted order)
            if spec["order_ids"] and any(t.event_id == req.event_id and t.kind == spec["kind"]
                                         and t.machine_id == spec["machine_id"] and t.order_ids == spec["order_ids"]
                                         for t in self._tasks.values()):
                continue
            self._seq += 1
            t = _Task(id=f"TK-{self._seq:04d}", event_id=req.event_id, approval_id=req.approval_id, step_id=req.step_id,
                      created_at=now, **spec)
            # an "after" given as a kind+machine key is resolved to the task created just before for that machine
            if t.after and not t.after.startswith("TK-"):
                prev = next((x for x in reversed(list(self._tasks.values()))
                             if x.event_id == req.event_id and x.machine_id == t.after and x.kind == t.kind), None)
                t.after = prev.id if prev else None
            self._tasks[t.id] = t
            out.append(t)
            self._emit(t, now, "created")
        self._trim()
        self.update(now)                 # assign at once when someone is free
        return [self._view(t) for t in out]

    def cancel(self, event_id: str, step_id: str, now: datetime) -> list[str]:
        """Drop the not-yet-done tasks of a step that was revoked (the supervisor picked another option later)."""
        gone = [t for t in self._tasks.values() if t.event_id == event_id and t.step_id == step_id and t.status != "完成"]
        for t in gone:
            del self._tasks[t.id]
            self._emit(t, now, "cancelled")
        return [t.id for t in gone]

    def update(self, now: datetime) -> None:
        for t in list(self._tasks.values()):
            if t.status == "待派":
                if t.after and self._tasks.get(t.after) is not None and self._tasks[t.after].status != "完成":
                    continue
                wid = self.w.pick_worker(t.team, t.machine_id, self.busy_worker_ids(),
                                         any_team=t.any_team_ok and (now - t.created_at).total_seconds()
                                         >= UNASSIGNED_ANY_TEAM_MIN * 60)
                if wid is None:
                    continue
                t.worker_id, t.assigned_at, t.status = wid, now, "前往中"
                t.walk_min = self.w.walk_minutes(wid, t.zone)
                self._emit(t, now, "assigned")
            if t.status == "前往中" and t.assigned_at is not None and now >= t.assigned_at + timedelta(minutes=t.walk_min):
                t.status, t.started_at = "執行中", t.assigned_at + timedelta(minutes=t.walk_min)
                self._emit(t, now, "started")
            if t.status == "執行中" and t.started_at is not None and now >= t.started_at + timedelta(minutes=t.work_min):
                t.status, t.done_at = "完成", t.started_at + timedelta(minutes=t.work_min)
                self._emit(t, now, "done")

    def view(self) -> TasksView:
        items = [self._view(t) for t in self._tasks.values()]
        rank = {s: i for i, s in enumerate(("執行中", "前往中", "待派", "完成"))}
        items.sort(key=lambda x: (rank[x.status], -(x.done_at or x.created_at).timestamp(), x.id))
        counts = {s: 0 for s in TASK_STATUSES}
        for x in items:
            counts[x.status] += 1
        return TasksView(items=items, counts=counts, open=sum(v for k, v in counts.items() if k != "完成"))

    # ---- internals
    def _trim(self) -> None:
        if len(self._tasks) <= TASK_KEEP:
            return
        done = sorted((t for t in self._tasks.values() if t.status == "完成"), key=lambda t: t.done_at or t.created_at)
        for t in done[: len(self._tasks) - TASK_KEEP]:
            del self._tasks[t.id]

    def _emit(self, t: _Task, now: datetime, what: str) -> None:
        v = self._view(t)
        for fn in list(self._listeners):
            try:
                fn(what, v, now)
            except Exception:  # noqa: BLE001 - a listener must never break the plant loop
                import logging
                logging.getLogger("warroom.sim.work").exception("task listener failed")

    def _view(self, t: _Task) -> WorkTask:
        now = self.w.now()
        total = max(1e-6, t.walk_min + t.work_min)
        if t.status == "完成":
            prog = 1.0
        elif t.status == "待派" or t.assigned_at is None:
            prog = 0.0
        else:
            prog = min(1.0, max(0.0, (now - t.assigned_at).total_seconds() / 60.0 / total))
        if t.done_at is not None:
            eta = t.done_at
        elif t.assigned_at is not None:
            eta = t.assigned_at + timedelta(minutes=total)
        else:
            eta = now + timedelta(minutes=WALK_MIN + t.work_min)
        name = self.w.worker_name(t.worker_id) if t.worker_id else None
        return WorkTask(id=t.id, event_id=t.event_id, approval_id=t.approval_id, step_id=t.step_id, kind=t.kind,
                        title=t.title, team=t.team, team_label=TEAM_LABELS.get(t.team, t.team.value),
                        worker_id=t.worker_id, worker_name=name, machine_id=t.machine_id, zone=t.zone,
                        where=where_label(t.machine_id, t.zone), status=t.status,  # type: ignore[arg-type]
                        created_at=t.created_at, assigned_at=t.assigned_at, started_at=t.started_at, done_at=t.done_at,
                        eta=eta, walk_min=round(t.walk_min, 1), work_min=t.work_min, progress=round(prog, 3),
                        after=t.after, order_ids=list(t.order_ids))


# ------------------------------------------------------------------ step -> tasks (code, never the model)
def _spec(kind: str, title: str, team: TeamId, machine_id: str | None = None, zone: str | None = None,
          after: str | None = None, order_ids: list[str] | None = None, any_team_ok: bool = True) -> dict[str, Any]:
    return {"kind": kind, "title": title[:80], "team": team, "machine_id": machine_id,
            "zone": zone or zone_of(machine_id), "after": after, "order_ids": list(order_ids or []),
            "work_min": WORK_MIN.get(kind, 10.0), "any_team_ok": any_team_ok}


def _team(v: Any) -> TeamId | None:
    try:
        return TeamId(v)
    except ValueError:
        return None


def _feed_orders(world: World, now: datetime, hours: float) -> list[str]:
    """Orders whose next tyres start their A feed within `hours` (the 補料 they need now)."""
    plan = world.plan()
    if plan is None:
        return []
    started = world.started_job_ids()
    out: list[str] = []
    for j in sorted(plan.jobs, key=lambda j: j.a_feed_start):
        if j.frozen or j.id in started:
            continue
        if j.a_feed_start <= now + timedelta(hours=hours) and j.mold_in >= now and j.order_id not in out:
            out.append(j.order_id)
    return out


def plan_tasks(world: World, req: TaskRequest, now: datetime) -> list[dict[str, Any]]:
    """Concrete tasks for one executed write. Only from the step's validated args, its result and the plant."""
    from warroom.contracts import NotifyKind, ToolName
    tool, a = ToolName(req.tool), req.args
    specs: list[dict[str, Any]] = []
    if tool is ToolName.SET_CURE_TIME:
        press = str(a.get("press_id"))
        specs.append(_spec("設定硫化時間", f"在{MACHINE_BY_ID[press].name}面板輸入硫化總時數 {float(a.get('total_cure_h', 0)):.1f} h"
                                         "（核准值，人工設定）", TeamId.CURING, press))
    elif tool is ToolName.CONFIRM_SCHEDULE:
        soon = now + timedelta(hours=12)
        seen: set[str] = set()
        for mv in sorted(req.moves, key=lambda m: str(m.get("to_mold_in"))):
            to = mv.get("to_press")
            try:
                at = datetime.fromisoformat(str(mv.get("to_mold_in")))
            except ValueError:
                continue
            if not to or to not in MACHINE_BY_ID or at > soon or (mv.get("from_press") == to and req.event_kind != "order_rush"):
                continue
            key = f"{mv.get('order_id')}|{to}"
            if key in seen or len(seen) >= 3:
                continue
            seen.add(key)
            specs.append(_spec("換模準備", f"{mv.get('order_id')} 備模到{MACHINE_BY_ID[to].name}，{at:%H:%M} 入模",
                               TeamId.CURING, to, order_ids=[str(mv.get("order_id"))]))
    elif tool is ToolName.NOTIFY_TEAMS:
        kind = str(a.get("kind"))
        teams = [t for t in (_team(x) for x in a.get("teams") or []) if t is not None]
        mid = a.get("machine_id") if a.get("machine_id") in MACHINE_BY_ID else None
        if kind == NotifyKind.DISPATCH.value:
            specs += _dispatch_tasks(world, req, now, teams)
        elif kind == NotifyKind.MACHINE_OUT.value and mid:
            specs.append(_spec("停用檢修", f"{MACHINE_BY_ID[mid].name}停用：檢查蒸汽管路與溫度計", TeamId.MAINTENANCE, mid))
            if mid.startswith("CURE-"):
                specs.append(_spec("品管", f"{MACHINE_BY_ID[mid].name}在模胎標示待判，交品管", TeamId.QC, mid))
        elif kind == NotifyKind.MAINTENANCE.value and mid:
            specs.append(_spec("檢修", f"{MACHINE_BY_ID[mid].name}：{str(a.get('message', ''))[:40]}", TeamId.MAINTENANCE, mid))
        elif kind in (NotifyKind.SLOW_FEED.value, NotifyKind.PAUSE_FEED.value) and mid:
            k = "放慢投料" if kind == NotifyKind.SLOW_FEED.value else "暫停投料"
            specs.append(_spec(k, f"{MACHINE_BY_ID[mid].name}{k}（現場人工調整）", MACHINE_TEAM.get(mid, TeamId.BODY), mid))
        elif kind == NotifyKind.OVERTIME.value:
            to = _team((req.staffing or {}).get("to_team")) or (teams[0] if teams else TeamId.CURING)
            shift = (req.staffing or {}).get("shift_name") or ""
            home = TEAM_HOME.get(to)
            specs.append(_spec("加班補位", f"{TEAM_LABELS.get(to, to.value)}{shift}加班：延長工時、接手缺人的出入模崗位",
                               to, home, any_team_ok=False))
        elif kind == NotifyKind.STAFF_MOVE.value:
            st = req.staffing or {}
            frm = _team(st.get("from_team")) or (teams[0] if teams else TeamId.SIDE)
            to = _team(st.get("to_team")) or (teams[-1] if teams else TeamId.CURING)
            specs.append(_spec("調派支援", f"從{TEAM_LABELS.get(frm, frm.value)}調 1 人到{TEAM_LABELS.get(to, to.value)}"
                                         f"{st.get('shift_name') or ''}支援出入模", frm, TEAM_HOME.get(to), any_team_ok=False))
        elif kind == NotifyKind.INFO.value:
            msg = str(a.get("message", ""))
            for team in teams[:3]:
                if team in (TeamId.MAINTENANCE, TeamId.QC, TeamId.SUPERVISOR):
                    continue
                specs.append(_spec("宣達", f"向{TEAM_LABELS.get(team, team.value)}宣達：{msg[:30]}", team,
                                   TEAM_HOME.get(team), any_team_ok=False))
    return specs


def _dispatch_tasks(world: World, req: TaskRequest, now: datetime, teams: list[TeamId]) -> list[dict[str, Any]]:
    """派工 (dispatch) of an approved plan: what people must do to start it."""
    specs: list[dict[str, Any]] = []
    if req.event_kind == "shift_start":
        for mid, after in WARMUP_ORDER:          # SOP-02 order: mixers, then the mills they feed
            specs.append(_spec("暖機", f"{MACHINE_BY_ID[mid].name}開班暖機 30 分鐘（SOP-02）", MACHINE_TEAM[mid], mid,
                               after=after))
        feed = _feed_orders(world, now, 6.0)
        if feed:
            specs.append(_spec("補料", f"今日首批膠料送 A 線密煉機（{'、'.join(feed[:4])}）", TeamId.BODY, "A-MIX",
                               order_ids=feed))
        low = [m for m in world.materials_status() if m["status"] != "ok"]
        if low:
            names = "、".join(m["name"] for m in low[:3])
            specs.append(_spec("盤點", f"到碼頭盤點{names}，確認在途採購到貨時間", TeamId.BODY, None, zone="dock"))
    elif req.event_kind == "order_rush":
        orders = req.order_ids or []
        for oid in orders[:2]:
            specs.append(_spec("補料", f"{oid} 急單膠料 {RUSH_FEED_LOTS} 批送 A 線密煉機（SOP-02 中胎 8 批）",
                               TeamId.BODY, "A-MIX", order_ids=[oid]))
            press = world.next_press_for(oid)
            if press:
                specs.append(_spec("換模準備", f"{oid} 模具 MD-{oid.lstrip('#').replace('-', '')} 送{MACHINE_BY_ID[press].name}備模",
                                   TeamId.CURING, press, order_ids=[oid]))
    else:
        for team in teams[:3]:
            if team in TEAM_HOME:
                specs.append(_spec("宣達", f"向{TEAM_LABELS.get(team, team.value)}宣達新排程", team, TEAM_HOME.get(team),
                                   any_team_ok=False))
    return specs


# ------------------------------------------------------------------ goods view
class GoodsTracker:
    """Remembers what the goods view needs between snapshots: de-molded tyres and the last schedule's next
    mold-in per order (to say how far a schedule change moved each order)."""

    def __init__(self) -> None:
        self.done: deque[tuple[str, str, TireSize, str, datetime]] = deque(maxlen=DONE_KEEP)
        self._sched_id: str | None = None
        self._next_by_order: dict[str, datetime] = {}
        self.moved_h: dict[str, float] = {}

    def demolded(self, job_id: str, order_id: str, size: TireSize, press: str, t: datetime) -> None:
        self.done.append((job_id, order_id, size, press, t))

    def schedule_changed(self, sched_id: str | None, next_by_order: dict[str, datetime]) -> None:
        if sched_id == self._sched_id:
            return
        if self._sched_id is not None:
            self.moved_h = {}
            for oid, at in next_by_order.items():
                prev = self._next_by_order.get(oid)
                if prev is None:
                    continue
                h = round((at - prev).total_seconds() / 3600.0, 1)
                if abs(h) >= 0.1:
                    self.moved_h[oid] = h
        self._sched_id, self._next_by_order = sched_id, dict(next_by_order)


def goods_view(world: World, t: datetime) -> GoodsView:
    f = world.f
    plan = world.plan()
    tracker: GoodsTracker = world.goods
    blocked_orders = world.tasks.open_order_ids("補料")
    started = world.started_job_ids()
    orders = {o.id: o for o in f.orders()}
    items: list[GoodsItem] = []
    in_press: dict[str, int] = {}
    # in the presses
    for pid, ps in sorted(f._press.items()):
        job = ps.job
        if job is None:
            continue
        in_press[job.order_id] = in_press.get(job.order_id, 0) + 1
        el = (t - job.mold_in_at).total_seconds() / 3600.0
        status = f._m[pid].status
        held = status in (MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE)
        o = orders.get(job.order_id)
        items.append(GoodsItem(id=job.job_id, order_id=job.order_id, size=job.size, status="硫化中",
                               progress=round(min(1.0, max(0.0, el / max(job.cure_required_h, 0.1))), 3),
                               machine_id=pid, press_id=pid, zone="line_CURE", mold_in=job.mold_in_at,
                               stage_end=job.planned_demold_at,
                               note=("扣模／停用中" if held else f"{el:.1f}/{job.cure_required_h:g} h"),
                               rush=bool(o and o.priority >= 2), blocked=held))
    # planned, not loaded yet
    queued: dict[str, int] = {}
    next_by_order: dict[str, datetime] = {}
    late_orders: set[str] = set()
    if plan is not None:
        for j in plan.jobs:
            if j.frozen or j.id in started:
                continue
            if j.order_id not in next_by_order or j.mold_in < next_by_order[j.order_id]:
                next_by_order[j.order_id] = j.mold_in
            o = orders.get(j.order_id)
            if o is not None and j.demold.date() > o.due:
                late_orders.add(j.order_id)
        tracker.schedule_changed(plan.id, next_by_order)
        ahead = sorted((j for j in plan.jobs if not j.frozen and j.id not in started
                        and j.mold_in <= t + timedelta(hours=GOODS_AHEAD_H)), key=lambda j: (j.mold_in, j.id))
        for j in ahead:
            o = orders.get(j.order_id)
            queued[j.order_id] = queued.get(j.order_id, 0) + 1
            rush = bool(o and o.priority >= 2)
            blocked = j.order_id in blocked_orders
            if blocked or t < j.a_feed_start:
                status, prog, mid, zone = "待補料", 0.0, None, "dock"
                note = "等補料任務" if blocked else f"{j.a_feed_start:%H:%M} 開始備料"
                end = j.a_feed_start
            elif t < j.bond_start:
                status, mid, zone = "備料中", "A-MIX", "line_A"
                prog = (t - j.a_feed_start) / max(j.bond_start - j.a_feed_start, timedelta(seconds=1))
                note, end = f"{j.bond_start:%H:%M} 貼合", j.bond_start
            elif t < j.mold_in:
                status, mid, zone = "生產中", "BOND-01", "line_BOND"
                prog = (t - j.bond_start) / max(j.mold_in - j.bond_start, timedelta(seconds=1))
                note, end = f"{j.mold_in:%H:%M} 入{j.press_id[-2:].lstrip('0')} 號", j.mold_in
            else:
                status, prog, mid, zone = "生產中", 1.0, "BOND-01", "line_BOND"
                note, end = f"待入{j.press_id[-2:].lstrip('0')} 號（硫化機未空）", j.mold_in
            items.append(GoodsItem(id=j.id, order_id=j.order_id, size=j.size, status=status,  # type: ignore[arg-type]
                                   progress=round(min(1.0, max(0.0, float(prog))), 3), machine_id=mid, press_id=j.press_id,
                                   zone=zone, mold_in=j.mold_in, stage_end=end, note=note, rush=rush, blocked=blocked,
                                   late=blocked and t >= j.a_feed_start))
    # de-molded
    for job_id, oid, size, press, at in reversed(tracker.done):
        o = orders.get(oid)
        ost = world.order_status(oid)
        status = ost if ost in ("待出貨", "已出貨") else "完成"
        prog = 1.0 if status != "完成" else min(1.0, (t - at).total_seconds() / 3600.0 / PACK_H)
        items.append(GoodsItem(id=job_id, order_id=oid, size=size, status=status, progress=round(prog, 3),  # type: ignore[arg-type]
                               machine_id=None, press_id=press, zone="dock", stage_end=at + timedelta(hours=PACK_H),
                               note=f"{at:%H:%M} 出模（{press[-2:].lstrip('0')} 號）", rush=bool(o and o.priority >= 2)))
    counts = {s: 0 for s in GOODS_STATUSES}
    for it in items:
        counts[it.status] += 1
    og: list[OrderGoods] = []
    for o in orders.values():
        og.append(OrderGoods(order_id=o.id, size=o.size, qty=o.qty, done=o.done, in_press=in_press.get(o.id, 0),
                             queued=queued.get(o.id, 0), status=world.order_status(o.id),
                             progress=round(o.done / max(1, o.qty), 3), rush=o.priority >= 2, due=o.due,
                             next_mold_in=next_by_order.get(o.id), moved_h=tracker.moved_h.get(o.id),
                             late=o.id in late_orders))
    og.sort(key=lambda x: (not x.rush, x.next_mold_in or datetime.max.replace(tzinfo=t.tzinfo), x.order_id))
    return GoodsView(items=items, orders=og, counts=counts, schedule_id=plan.id if plan is not None else None)


__all__ = ["GoodsItem", "GoodsTracker", "GoodsView", "OrderGoods", "TaskBoard", "TaskRequest", "TasksView", "WorkTask",
           "goods_view", "plan_tasks", "CURE_HOURS"]
