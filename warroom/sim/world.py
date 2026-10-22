"""The rest of the plant around the 28 machines: orders over time, raw materials and purchasing, finished
goods and shipping, people, energy, quality and the maintenance log. Issue #11 (CJ, 2026-10-20:
「製作全部工廠實時數據模擬」).

The world READS the Factory (machine status, true temperature, cure jobs, the seed order book) and never
writes to it, and it draws from its own seeded random stream. So machine physics, cure cycles, the
schedule and the Factory's RNG stream are exactly what they were before this module existed, and every
existing scenario (page 11 included) plays out the same.

Time: the world advances on a fixed 10-s factory-time grid (WORLD_DT_S) from the scenario start, whatever
step sizes the Factory is driven with, so it is deterministic for a given seed and end time.

Extension point for the agent (not wired yet, by design): `World.add_listener(fn)` calls fn(WorldEvent)
for every world event (new / rush order, delivery late or short, stock-out, defect, repair opened ...).
A later change can turn chosen kinds into detector-style wake-ups for the state machine there.

Sources (every item carries `source`):
- team_dataset (data/team_dataset, the team's official synthetic dataset; ids mapped in warroom/data/team_ids.py,
  dates shifted per scenario in warroom/data/team_dataset.py): the 20 production staff and their roster (shifts,
  overtime, rest days, S11 sick on the EV-2 night = page 11), mold changes, the incident history and the daily
  output history.
- generated (this module): orders arriving over time, materials and purchasing, shipping, energy, inspections,
  planned maintenance windows, and the support staff the dataset does not list (maintenance, QC, supervisors).
Every generated number is an assumption for a small solid-tyre plant (no real company data); each constant says
what it stands for.
"""
from __future__ import annotations

import logging
import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict

from warroom.contracts import Line, MachineKind, MachineStatus, ScenarioName, TeamId, TireSize
from warroom.data import team_dataset
from warroom.data.team_ids import WORK_GROUP_MACHINES, WORK_GROUP_TEAM
from warroom.sim.work import GoodsTracker, GoodsView, TaskBoard, TasksView, goods_view
from warroom.factory import (
    CURE_HOURS,
    CURE_TEMP_MIN_C,
    MACHINE_BY_ID,
    MACHINES,
    OPTIMISED_SHIFTS,
    TEAM_LABELS,
    TZ,
    ShiftDef,
)

if TYPE_CHECKING:  # pragma: no cover
    from warroom.sim.factory_sim import Factory

log = logging.getLogger("warroom.sim.world")

# ------------------------------------------------------------------ assumptions (constants)
WORLD_DT_S = 10.0                    # world update grid, factory seconds
WORLD_SEED_SALT = 1_000_003          # world RNG = Random(seed * salt + 11): never the Factory's stream
EVENT_KEEP = 40                      # recent world events kept for the dashboard
SIZE_LABEL = {TireSize.S: "小胎", TireSize.M: "中胎", TireSize.L: "大胎"}

# Orders
ORDER_ARRIVAL_PER_H = 0.35           # new orders per office hour (Mon–Sat 08–18): ~3.5 a day
RUSH_SHARE = 0.15                    # share of new orders that are rush (急單, due in 3–5 days)
ORDER_CHANGE_PER_H = 0.05            # quantity / due-date change requests per office hour (~1 every 2 days)
OFFICE_HOURS = (8, 18)
NEW_QTY = {TireSize.S: (8, 40), TireSize.M: (4, 20), TireSize.L: (2, 10)}
SCHED_DELAY_H = (2.0, 6.0)           # 待排程 → 生產中 for a normal world order (planning lead)
RUSH_SCHED_DELAY_H = (0.5, 1.0)
WORLD_PRESS_EQ = 1.0                 # a world-only order advances like one press curing it (no real press used)
PACK_H = 1.0                         # 完成 → 待出貨: final inspection + packing
CUSTOMERS = ("客戶甲", "客戶乙", "客戶丙", "客戶丁", "客戶戊", "客戶己", "客戶庚", "客戶辛", "客戶壬", "客戶癸")

# Shipping (outbound trucks): a truck comes 45 min before each dispatch time when something waits to ship
DISPATCH_TIMES = (time(10, 0), time(16, 0))
DISPATCH_DAYS = (0, 1, 2, 3, 4, 5)   # Mon–Sat
LOADING_MIN = 45
FG_CAPACITY = 150                    # finished-tyre warehouse slots

# Receiving (inbound deliveries)
RECEIVING_HOURS = (7, 19)            # the dock takes deliveries 07:00–19:00; a later ETA waits to 07:00
UNLOADING_MIN = 30
LATE_SHARE = 0.12                    # deliveries that come late (+4–12 h)
SHORT_SHARE = 0.08                   # deliveries that come short (75–90 % of the ordered quantity)


@dataclass(frozen=True)
class MaterialDef:
    key: str
    name: str
    unit: str
    stock0: float                    # stock at scenario start
    capacity: float                  # storage space
    reorder_point: float
    order_qty: float
    lead_h: float                    # supplier lead time
    supplier: str
    use_per_h: dict[str, float]      # machine id -> consumption per running hour


# Rubber compound ~30 kg/h across both mixers (~700 kg a day for ~9 tyres of 20–90 kg plus scrap and trim).
MATERIALS: tuple[MaterialDef, ...] = (
    MaterialDef("nr", "天然橡膠", "kg", 1240, 2000, 500, 1000, 48, "供應商甲", {"A-MIX": 9.0, "B-MIX": 4.0}),
    MaterialDef("sbr", "合成橡膠", "kg", 640, 1000, 250, 500, 48, "供應商乙", {"A-MIX": 4.0, "B-MIX": 2.0}),
    MaterialDef("cb", "碳黑", "kg", 520, 1500, 400, 800, 24, "供應商丙", {"A-MIX": 7.0, "B-MIX": 3.0}),
    MaterialDef("cure", "硫化劑", "kg", 64, 120, 30, 60, 72, "供應商丁", {"A-MIX": 0.5, "B-MIX": 0.3, "C-EXTR": 0.1}),
    MaterialDef("steel", "鋼絲（胎圈）", "kg", 260, 600, 150, 300, 72, "供應商戊", {"C-PAYOFF": 2.5}),
    MaterialDef("nylon", "尼龍布", "m", 180, 600, 160, 400, 96, "供應商己", {"D-CUT": 3.0}),
)
MATERIAL_BY_KEY = {m.key: m for m in MATERIALS}

# Energy: rated electric kW per machine kind (small plant). Presses are steam heated (boiler fuel, not
# counted here): their kW is hydraulics + controls.
KW_BY_KIND: dict[MachineKind, float] = {
    MachineKind.MIXER: 110.0, MachineKind.OPEN_MILL: 45.0, MachineKind.CALENDER: 55.0, MachineKind.COOLER: 11.0,
    MachineKind.WINDER: 5.5, MachineKind.HYDRAULIC_PRESS: 15.0, MachineKind.PAYOFF: 2.2, MachineKind.EXTRUDER: 37.0,
    MachineKind.BLADDER: 3.0, MachineKind.CUTTER: 5.5, MachineKind.CURING_PRESS: 7.5,
}
KW_OVERRIDE = {"A-BIGOPEN": 55.0}
LOAD_BY_STATUS: dict[MachineStatus, float] = {
    MachineStatus.RUNNING: 1.0, MachineStatus.WARNING: 1.0, MachineStatus.ALARM: 1.0, MachineStatus.IDLE: 0.25,
    MachineStatus.STOPPED: 0.03, MachineStatus.OUT_OF_SERVICE: 0.03, MachineStatus.OFFLINE: 0.0,
}
BASE_KW = 45.0                       # lighting, compressed air, cooling tower, offices
CONTRACT_KW = 800.0                  # contracted demand
PEAK_DAYS = (0, 1, 2, 3, 4)          # weekdays
PEAK_HOURS = (16, 22)                # assumed time-of-use peak 16:00–22:00 on weekdays
ENERGY_SAMPLE_MIN = 5                # history point every 5 factory minutes
ENERGY_KEEP = 72                     # 6 h of history

# Quality
INLINE_EVERY_MIN = 30                # in-process inspection per line every 30 min
BASE_DEFECT = 0.015                  # normal defect rate per inspected batch / tyre
ABNORMAL_DEFECT = 0.35               # when the machine that made it was abnormal
QUALITY_KEEP = 30
MACHINE_DEFECT: dict[str, str] = {
    "A-MIX": "硬度偏差", "A-OPEN": "厚度不均", "A-BIGOPEN": "焦燒（膠料過熱）", "A-SMALLOPEN": "膠料溫度不足",
    "A-CAL": "押出厚度偏差", "A-COOL": "冷卻不足變形", "A-WIND": "捲取皺褶", "B-MIX": "雜質", "B-OPEN": "厚度不均",
    "B-FORM": "尺寸偏差", "B-ASSY": "組裝偏位", "C-PAYOFF": "鋼絲張力不均", "C-EXTR": "包膠不均", "C-WIND": "鋼絲圈偏位",
    "C-WRAP": "包布鬆脫", "D-CUT": "裁切寬度偏差", "BOND-01": "貼合氣泡",
}
CURE_DEFECTS = ("缺膠", "氣泡", "表面雜質")
UNDERCURE = "欠硫（硫化溫度不足）"

# Maintenance: planned windows on Sundays (no upstream shift); presses every 4 weeks, others every 2
PM_HOUR = {MachineKind.MIXER: 6.0, MachineKind.CURING_PRESS: 4.0}
PM_DEFAULT_H = 3.0
PM_SPAN_DAYS = (-14, 21)

# People
NAMES = (
    "陳志明", "林怡君", "黃建宏", "張雅婷", "李俊傑", "王淑芬", "吳宗翰", "劉佩珊", "蔡明哲", "楊雅芳",
    "許家豪", "鄭欣怡", "謝文彬", "郭美玲", "洪志偉", "曾惠如", "邱冠宇", "廖淑華", "賴柏翰", "周佳穎",
    "徐振宇", "蘇怡萱", "葉俊宏", "江秀蘭", "何承恩", "羅詩涵", "高志豪", "簡雅雯",
)
TOILET_EVERY_H = 3.0
TOILET_MIN = (4, 8)
CARRY_EVERY_H = 2.0
CARRY_MIN = (8, 15)
CARRY_BOOST_AT_DOCK = 6.0            # carrying is 6x likelier while a truck is at the dock
COFFEE_EVERY_H = 5.0
COFFEE_MIN = (8, 12)
MEAL_MIN = 60

Zone = Literal["war_room", "staff", "line_A", "line_B", "line_C", "line_D", "line_BOND", "line_CURE",
               "break", "toilet", "dock", "off_site"]
ZONE_LABELS: dict[str, str] = {
    "war_room": "戰情室", "staff": "員工待命區", "line_A": "A 胎體／胎面膠", "line_B": "B 胎側膠", "line_C": "C 鋼絲圈",
    "line_D": "D 尼龍布", "line_BOND": "貼合", "line_CURE": "硫化區", "break": "休息區", "toilet": "廁所",
    "dock": "碼頭", "off_site": "廠外",
}
WorkerState = Literal["待命", "在機台", "搬運", "休息", "如廁", "下班", "請假", "前往中", "執行中"]   # 前往中/執行中: a task (issue #17)
OrderStatus = Literal["待排程", "生產中", "完成", "待出貨", "已出貨"]
ORDER_STAGES: tuple[str, ...] = ("待排程", "生產中", "完成", "待出貨", "已出貨")

# Non-production staff shifts (production teams use factory.OPTIMISED_SHIFTS, the page-6 premise)
EVERY_DAY = (0, 1, 2, 3, 4, 5, 6)
SUPPORT_SHIFTS: tuple[ShiftDef, ...] = (
    ShiftDef(TeamId.MAINTENANCE, "日班", time(8), time(17), headcount=2),
    ShiftDef(TeamId.MAINTENANCE, "夜間待命", time(20), time(8), weekdays=EVERY_DAY, headcount=1),
    ShiftDef(TeamId.QC, "日班", time(8), time(17), headcount=2),
    ShiftDef(TeamId.SUPERVISOR, "日班", time(8), time(20), weekdays=EVERY_DAY, headcount=1),
    ShiftDef(TeamId.SUPERVISOR, "夜班", time(20), time(8), weekdays=EVERY_DAY, headcount=1),
)
TEAM_MACHINES: dict[TeamId, tuple[str, ...]] = {
    TeamId.CURING: tuple(m.id for m in MACHINES if m.line is Line.CURE),
    TeamId.BODY: ("A-MIX", "A-OPEN", "A-SMALLOPEN", "A-CAL", "A-COOL", "A-WIND"),
    TeamId.BIGOPEN: ("A-BIGOPEN",),
    TeamId.SIDE: ("B-MIX", "B-OPEN", "B-FORM", "B-ASSY"),
    TeamId.BEAD_NYLON: ("C-PAYOFF", "C-EXTR", "C-WIND", "C-WRAP", "D-CUT"),
    TeamId.BOND: ("BOND-01",),
}
WORKING = (MachineStatus.RUNNING, MachineStatus.WARNING, MachineStatus.ALARM)
NEEDS_REPAIR = (MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE, MachineStatus.ALARM)


def zone_of(machine_id: str) -> str:
    return f"line_{MACHINE_BY_ID[machine_id].line.value}"


# ------------------------------------------------------------------ snapshot models (GET /api/world)
class _W(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorldEvent(_W):
    ts: datetime
    kind: str            # order_new | order_rush | order_change | order_completed | shipment | shipment_departed
    #                      | po_created | delivery
    #                      | delivery_late | delivery_short | stockout | defect | maint_start | maint_end
    #                      | repair_open | repair_close | truck_arrive
    text: str
    ref: str | None = None
    # issue #36 order_completed / shipment_departed: for the 訂單 tab, the ticker and the 3D success moment
    order_ids: list[str] = []
    qty: int | None = None
    on_time: bool | None = None


class WorldOrder(_W):
    id: str
    size: TireSize
    qty: int
    done: int
    due: date
    customer: str
    status: OrderStatus
    rush: bool = False
    source: Literal["seed", "world"]   # seed = the factory's order book (scheduler / agent); world = arrived in the sim
    created_at: datetime | None = None
    shipped_at: datetime | None = None


class OrdersView(_W):
    pipeline: dict[str, int]
    items: list[WorldOrder]
    new_today: int
    rush_open: int


class MaterialView(_W):
    key: str
    name: str
    unit: str
    stock: float
    capacity: float
    reorder_point: float
    on_order: float
    use_per_day: float
    days_left: float | None
    status: Literal["ok", "reorder", "low", "out"]


class PurchaseOrder(_W):
    id: str
    material: str
    name: str
    qty: float
    unit: str
    supplier: str
    ordered_at: datetime
    eta: datetime
    status: Literal["在途", "延遲", "卸貨中", "已收貨"]
    received_qty: float | None = None
    note: str | None = None


class FinishedGoods(_W):
    by_size: dict[str, int]
    total: int
    capacity: int
    waiting_ship: int


class Truck(_W):
    id: str
    kind: Literal["inbound", "outbound"]
    plate: str
    cargo: str
    arrived_at: datetime
    leaves_at: datetime


class DockEntry(_W):
    ts: datetime
    kind: Literal["arrive", "depart"]
    direction: Literal["inbound", "outbound"]
    text: str


class DockView(_W):
    trucks: list[Truck]
    next_departure: datetime | None
    next_arrival: datetime | None
    log: list[DockEntry]


class Worker(_W):
    id: str
    name: str
    team: TeamId
    team_label: str
    role: Literal["作業員", "組長", "維修", "品管", "主管"]
    shift: str
    state: WorkerState
    zone: str
    machine_id: str | None = None
    since: datetime
    source: Literal["team_dataset", "generated"] = "generated"
    note: str | None = None          # roster note (sick leave, overtime)


class PeopleView(_W):
    workers: list[Worker]
    by_zone: dict[str, int]
    by_state: dict[str, int]
    on_duty: int
    total: int


class EnergyView(_W):
    total_kw: float
    by_line: dict[str, float]
    base_kw: float
    contract_kw: float
    peak: bool
    kwh_today: float
    history: list[tuple[datetime, float]]


class Inspection(_W):
    id: str
    ts: datetime
    stage: Literal["inline", "final"]
    line: str
    machine_id: str
    batch: str
    order_id: str | None = None
    result: Literal["合格", "不良"]
    cause: str | None = None
    disposition: str | None = None
    inspector: str


class IncidentRecord(_W):
    id: str
    date: date
    machine_id: str | None
    equipment: str
    symptom: str
    action: str
    downtime_min: int
    outcome: str
    source: Literal["team_dataset"] = "team_dataset"


class DailyRecord(_W):
    date: date
    tires_done: int
    flex_util_pct: float
    ot_hours: float
    downtime_min: int
    source: Literal["team_dataset"] = "team_dataset"


class QualityView(_W):
    inspected_today: int
    defects_today: int
    yield_pct: float | None
    recent: list[Inspection]
    incidents: list[IncidentRecord] = []      # team dataset history, dates shifted to the sim clock
    daily: list[DailyRecord] = []             # team dataset daily output before today


class MaintenanceItem(_W):
    id: str
    machine_id: str
    kind: Literal["planned", "repair", "mold_change"]
    title: str
    start: datetime
    end: datetime | None
    status: Literal["planned", "in_progress", "done"]
    assignee: str | None = None
    source: Literal["team_dataset", "generated"] = "generated"


class MaintenanceView(_W):
    in_progress: list[MaintenanceItem]
    upcoming: list[MaintenanceItem]
    recent: list[MaintenanceItem]


class WorldSnapshot(_W):
    factory_time: datetime
    rev: int
    orders: OrdersView
    materials: list[MaterialView]
    purchase_orders: list[PurchaseOrder]
    finished_goods: FinishedGoods
    dock: DockView
    people: PeopleView
    energy: EnergyView
    quality: QualityView
    maintenance: MaintenanceView
    events: list[WorldEvent]
    tasks: TasksView                 # issue #17: worker tasks from approved steps
    goods: GoodsView                 # issue #17: every planned tyre's status + progress ring


WORLD_SECTIONS: tuple[str, ...] = ("orders", "materials", "purchase_orders", "finished_goods", "dock", "people",
                                   "energy", "quality", "maintenance", "events", "tasks", "goods")


# ------------------------------------------------------------------ internal state
@dataclass
class _WOrder:
    id: str
    size: TireSize
    qty: int
    due: date
    customer: str
    created_at: datetime
    rush: bool = False
    done: int = 0
    status: str = "待排程"
    start_at: datetime | None = None
    progress: float = 0.0
    completed_at: datetime | None = None
    shipped_at: datetime | None = None


@dataclass
class _Worker:
    id: str
    name: str
    team: TeamId
    role: str
    sd: ShiftDef
    slot: int                        # index inside its shift (meal break stagger, home machine)
    home: str | None                 # home machine id
    machines: tuple[str, ...] = ()   # machines this worker's group runs
    group: str | None = None         # team dataset work group (貼合組 ...)
    staff_id: str | None = None      # team dataset staff id
    note: str | None = None
    state: str = "下班"
    zone: str = "off_site"
    machine_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None    # end of a temporary state (如廁 / 搬運 / 休息)


@dataclass
class _PO:
    id: str
    key: str
    qty: float
    ordered_at: datetime
    eta: datetime                    # promised
    arrive_at: datetime              # actual (late deliveries come after eta)
    short: bool
    status: str = "在途"
    received_qty: float | None = None
    truck: str | None = None


@dataclass
class _Truck:
    id: str
    kind: str
    plate: str
    cargo: str
    arrived_at: datetime
    leaves_at: datetime
    po_id: str | None = None
    order_ids: list[str] = field(default_factory=list)


@dataclass
class _Maint:
    id: str
    machine_id: str
    kind: str
    title: str
    start: datetime
    end: datetime | None
    assignee: str | None = None
    announced: int = 0               # 0 none, 1 start logged, 2 end logged
    source: str = "generated"


def _shift_instance(sd: ShiftDef, t: datetime) -> datetime | None:
    """Start of the instance of `sd` that covers t, or None when off shift."""
    for back in (0, 1):
        d = t.date() - timedelta(days=back)
        if d.weekday() not in sd.weekdays:
            continue
        start = datetime.combine(d, sd.start, tzinfo=TZ)
        end = datetime.combine(d, sd.end, tzinfo=TZ)
        if end <= start:
            end += timedelta(days=1)
        if start <= t < end:
            return start
    return None


def _p(rate_per_h: float, dt_s: float) -> float:
    """Probability of at least one Poisson event in dt."""
    return 1.0 - math.exp(-rate_per_h * dt_s / 3600.0)


class World:
    """Everything around the machines. Implements nothing in contracts on purpose: it is read by the API
    (GET /api/world, WsType.WORLD) and the dashboard; the agent's tools do not see it yet."""

    def __init__(self, factory: Factory, t0: datetime, seed: int = 7):
        self.f = factory
        self.rng = random.Random(seed * WORLD_SEED_SALT + 11)
        self.t0 = t0
        self._t = t0
        self.rev = 0
        self.events: deque[WorldEvent] = deque(maxlen=EVENT_KEEP)
        self._listeners: list[Callable[[WorldEvent], None]] = []
        self._seq = {"order": 400, "po": 1001, "truck": 0, "qc": 0, "rep": 0}   # PO-1001 is the delivery in transit at the start
        self._init_orders(t0)
        self._stock = {m.key: float(m.stock0) for m in MATERIALS}
        self._stockout: set[str] = set()
        self._pos: list[_PO] = []
        self._init_pos(t0)
        self._trucks: list[_Truck] = []
        self._dock_log: deque[DockEntry] = deque(maxlen=12)
        self._outbound_done: set[str] = set()       # dispatch slots already handled
        self.ds = team_dataset.load()
        try:
            scen = ScenarioName(factory.scenario.name)
        except (AttributeError, ValueError):
            scen = ScenarioName.NORMAL
        self._scenario = scen
        self._ds_cache: dict[date, date] = {}
        self._init_workers()
        self._kw: dict[str, float] = {}
        self._kwh_today = 0.0
        self._kwh_date = t0.date()
        self._energy_hist: deque[tuple[datetime, float]] = deque(maxlen=ENERGY_KEEP)
        self._inspections: deque[Inspection] = deque(maxlen=QUALITY_KEEP)
        self._q_today = (t0.date(), 0, 0)
        self._next_inline = t0 + timedelta(minutes=INLINE_EVERY_MIN)
        self._press_jobs: dict[str, tuple[str, str, TireSize] | None] = {}
        self._job_abnormal: set[str] = set()
        self._seed_done: dict[str, int] = {}
        self._seed_completed: dict[str, datetime] = {}
        self._seed_shipped: dict[str, datetime] = {}
        self._maint: list[_Maint] = self._plan_maintenance(t0) + self._dataset_mold_changes(t0)
        self._repairs: dict[str, _Maint] = {}
        self._closed_repairs: deque[_Maint] = deque(maxlen=8)
        self._extra_absent: dict[tuple[str, date], str] = {}   # issue #17: (staff id, date) -> note (a sick call)
        self.tasks = TaskBoard(self)
        self.goods = GoodsTracker()
        self._observe_factory(t0, first=True)
        self._update_people(t0, 0.0)
        self._update_energy(t0, 0.0)

    # ------------------------------------------------------------------ public
    def add_listener(self, fn: Callable[[WorldEvent], None]) -> None:
        """Extension point: called with every WorldEvent (e.g. wake the state machine on a stock-out)."""
        self._listeners.append(fn)

    def now(self) -> datetime:
        return self._t

    def advance(self, now: datetime) -> None:
        """Advance to `now` on the fixed WORLD_DT_S grid. Never raises into the plant loop."""
        step = timedelta(seconds=WORLD_DT_S)
        while self._t + step <= now:
            t = self._t + step
            self._step(t, WORLD_DT_S)
            self._t = t
            self.rev += 1

    def snapshot(self) -> WorldSnapshot:
        t = self._t
        return WorldSnapshot(factory_time=t, rev=self.rev, orders=self._orders_view(t), materials=self._materials_view(),
                             purchase_orders=self._po_view(), finished_goods=self._fg_view(), dock=self._dock_view(t),
                             people=self._people_view(), energy=self._energy_view(t), quality=self._quality_view(),
                             maintenance=self._maint_view(t), events=list(reversed(self.events)),
                             tasks=self.tasks.view(), goods=goods_view(self, t))

    # ------------------------------------------------------------------ step
    def _step(self, t: datetime, dt: float) -> None:
        self._observe_factory(t)
        self._update_orders(t, dt)
        self._update_materials(t, dt)
        self._update_dock(t)
        self._update_quality(t)
        self._update_maintenance(t)
        self.tasks.update(t)
        self._update_people(t, dt)
        self._update_energy(t, dt)

    def _emit(self, t: datetime, kind: str, text: str, ref: str | None = None, **extra: Any) -> None:
        ev = WorldEvent(ts=t, kind=kind, text=text, ref=ref, **extra)
        self.events.append(ev)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # a listener must never break the plant loop
                log.exception("world listener failed")

    def _next_id(self, kind: str) -> int:
        self._seq[kind] += 1
        return self._seq[kind]

    def _status(self, mid: str) -> MachineStatus:
        return self.f._m[mid].status

    # ------------------------------------------------------------------ factory observation (read only)
    def _observe_factory(self, t: datetime, first: bool = False) -> None:
        """Cure completions (final inspection), abnormal cures, seed-order progress."""
        for pid, ps in self.f._press.items():
            job = ps.job
            cur = (job.job_id, job.order_id, job.size) if job is not None else None
            prev = self._press_jobs.get(pid)
            if not first and prev is not None and (cur is None or cur[0] != prev[0]):
                self._final_inspection(t, pid, *prev)
                self.goods.demolded(prev[0], prev[1], prev[2], pid, t)
            self._press_jobs[pid] = cur
            if cur is not None:
                m = self.f._m[pid]
                if m.status not in (MachineStatus.RUNNING,) or m.temp < CURE_TEMP_MIN_C:
                    self._job_abnormal.add(cur[0])
        for o in self.f._orders.values():
            prev_done = self._seed_done.get(o.id)
            self._seed_done[o.id] = o.done
            if o.done >= o.qty and o.id not in self._seed_completed:
                self._seed_completed[o.id] = t
                if prev_done is not None and prev_done < o.qty:
                    self._emit(t, "order_completed", f"訂單 {o.id} 完成：{SIZE_LABEL[o.size]} {o.qty} 條", o.id,
                               order_ids=[o.id], qty=o.qty, on_time=t.date() <= o.due)

    # ------------------------------------------------------------------ orders
    def _init_orders(self, t0: datetime) -> None:
        d0 = t0.date()
        self._orders: dict[str, _WOrder] = {}
        hist = [  # world-only history so every pipeline stage has something at the start
            ("#W-0391", TireSize.S, 20, -1, "客戶壬", "已出貨"),
            ("#W-0392", TireSize.M, 6, -2, "客戶癸", "已出貨"),
            ("#W-0395", TireSize.S, 12, 4, "客戶甲", "待出貨"),
            ("#W-0397", TireSize.L, 4, 9, "客戶乙", "生產中"),
            ("#W-0398", TireSize.M, 8, 14, "客戶丙", "待排程"),
        ]
        for oid, size, qty, due_d, cust, status in hist:
            o = _WOrder(id=oid, size=size, qty=qty, due=d0 + timedelta(days=due_d), customer=cust,
                        created_at=t0 - timedelta(days=6), status=status)
            if status == "已出貨":
                o.done, o.completed_at, o.shipped_at = qty, t0 - timedelta(days=-due_d + 1), t0 + timedelta(days=due_d)
            elif status == "待出貨":
                o.done, o.completed_at = qty, t0 - timedelta(hours=5)
            elif status == "生產中":
                o.done, o.start_at, o.progress = 1, t0 - timedelta(hours=30), 0.4
            else:
                o.created_at, o.start_at = t0 - timedelta(hours=3), t0 + timedelta(hours=3)
            self._orders[oid] = o
        self._seq["order"] = 398
        self._new_today = (d0, 0)
        self._scripted: list[tuple[datetime, TireSize, int, bool, str]] = []
        if getattr(getattr(self.f, "scenario", None), "name", None) is ScenarioName.DAY_LOOP:
            self._day_loop_orders(t0)

    def _day_loop_orders(self, t0: datetime) -> None:
        """Issue #36 一日循環: the day carries the whole order -> shipment chain. Work in progress is shaped so two
        orders finish and ship inside the loop (#W-0397 finishes 06:30, packed 07:30, leaves on the 10:00 truck;
        #W-0396 finishes 13:30, leaves 16:00), and two new orders come in during the morning (one rush), which
        the plan then schedules. CJ, 2026-10-20: 「設計今天這個從訂單進來一直到東西出貨的一個流程」."""
        d0 = t0.date()

        def near_done(o: _WOrder, finish: datetime) -> None:
            left_h = (finish - t0).total_seconds() / 3600.0
            o.status, o.done, o.start_at = "生產中", o.qty - 1, t0 - timedelta(hours=30)
            o.progress = max(0.0, 1.0 - left_h * WORLD_PRESS_EQ / CURE_HOURS[o.size])
        near_done(self._orders["#W-0397"], t0 + timedelta(hours=6, minutes=30))
        o = _WOrder(id="#W-0396", size=TireSize.S, qty=10, due=d0 + timedelta(days=2), customer="客戶丁",
                    created_at=t0 - timedelta(days=4))
        near_done(o, t0 + timedelta(hours=13, minutes=30))
        self._orders[o.id] = o
        self._scripted = [(t0 + timedelta(hours=9, minutes=10), TireSize.M, 8, False, "客戶戊"),
                          (t0 + timedelta(hours=10, minutes=40), TireSize.S, 4, True, "客戶己")]

    def _update_orders(self, t: datetime, dt: float) -> None:
        rng = self.rng
        office = t.weekday() <= 5 and OFFICE_HOURS[0] <= t.hour < OFFICE_HOURS[1]
        if self._new_today[0] != t.date():
            self._new_today = (t.date(), 0)
        while self._scripted and self._scripted[0][0] <= t:      # issue #36 一日循環
            _, *fixed = self._scripted.pop(0)
            self._new_order(t, tuple(fixed))                    # type: ignore[arg-type]
        if office and rng.random() < _p(ORDER_ARRIVAL_PER_H, dt):
            self._new_order(t)
        if office and rng.random() < _p(ORDER_CHANGE_PER_H, dt):
            self._change_order(t)
        for o in self._orders.values():
            if o.status == "待排程" and o.start_at is not None and t >= o.start_at:
                o.status = "生產中"
                self._emit(t, "order_change", f"{o.id} 排入生產（{SIZE_LABEL[o.size]} {o.qty} 條）", o.id)
            elif o.status == "生產中":
                o.progress += dt * WORLD_PRESS_EQ / (CURE_HOURS[o.size] * 3600.0)
                while o.progress >= 1.0 and o.done < o.qty:
                    o.progress -= 1.0
                    o.done += 1
                if o.done >= o.qty:
                    o.status, o.completed_at = "完成", t
                    self._emit(t, "order_completed", f"訂單 {o.id} 完成：{SIZE_LABEL[o.size]} {o.qty} 條", o.id,
                               order_ids=[o.id], qty=o.qty, on_time=t.date() <= o.due)
            elif o.status == "完成" and o.completed_at is not None and t >= o.completed_at + timedelta(hours=PACK_H):
                o.status = "待出貨"
        # prune: keep shipped world orders 3 days
        old = [k for k, o in self._orders.items() if o.shipped_at is not None and t - o.shipped_at > timedelta(days=3)]
        for k in old:
            del self._orders[k]

    def _new_order(self, t: datetime, fixed: tuple[TireSize, int, bool, str] | None = None) -> None:
        rng = self.rng
        if fixed is None:
            size = rng.choices((TireSize.S, TireSize.M, TireSize.L), weights=(5, 3, 2))[0]
            lo, hi = NEW_QTY[size]
            qty = rng.randint(lo, hi)
            rush = rng.random() < RUSH_SHARE
            due = t.date() + timedelta(days=rng.randint(3, 5) if rush else rng.randint(10, 45))
            delay = rng.uniform(*(RUSH_SCHED_DELAY_H if rush else SCHED_DELAY_H))
            cust = rng.choice(CUSTOMERS)
        else:                                   # issue #36 一日循環: a scripted arrival, no RNG drawn
            size, qty, rush, cust = fixed
            due = t.date() + timedelta(days=4 if rush else 21)
            delay = RUSH_SCHED_DELAY_H[0] if rush else SCHED_DELAY_H[0]
        oid = f"#W-{self._next_id('order'):04d}"
        o = _WOrder(id=oid, size=size, qty=qty, due=due, customer=cust, created_at=t, rush=rush,
                    start_at=t + timedelta(hours=delay))
        self._orders[oid] = o
        self._new_today = (t.date(), self._new_today[1] + 1)
        if rush:
            self._emit(t, "order_rush", f"急單 {oid}：{o.customer} {SIZE_LABEL[size]} {qty} 條，交期 {due:%m/%d}", oid)
        else:
            self._emit(t, "order_new", f"新訂單 {oid}：{o.customer} {SIZE_LABEL[size]} {qty} 條，交期 {due:%m/%d}", oid)

    def _change_order(self, t: datetime) -> None:
        """Customer changes quantity or due date of an open world order (seed orders feed the scheduler and
        the page-11 story, so they are never changed here)."""
        rng = self.rng
        open_ = sorted(k for k, o in self._orders.items() if o.status in ("待排程", "生產中"))
        if not open_:
            return
        o = self._orders[rng.choice(open_)]
        if rng.random() < 0.5:
            new = max(o.done + 1, o.qty + rng.choice((-3, -2, 2, 3, 5)))
            if new != o.qty:
                self._emit(t, "order_change", f"{o.id} 數量 {o.qty} → {new} 條（{o.customer} 變更）", o.id)
                o.qty = new
        else:
            shift = rng.choice((-3, -2, 2, 4))
            new_due = max(t.date() + timedelta(days=2), o.due + timedelta(days=shift))
            if new_due != o.due:
                self._emit(t, "order_change", f"{o.id} 交期 {o.due:%m/%d} → {new_due:%m/%d}（{o.customer} 變更）", o.id)
                o.due = new_due

    def _seed_status(self, o: Any, t: datetime) -> str:
        if o.id in self._seed_shipped:
            return "已出貨"
        if o.done >= o.qty:
            c = self._seed_completed.get(o.id)
            return "待出貨" if c is not None and t >= c + timedelta(hours=PACK_H) else "完成"
        if o.done > 0 or self.f._in_press.get(o.id, 0) > 0:
            return "生產中"
        return "待排程"

    def _all_orders(self, t: datetime) -> list[WorldOrder]:
        out = [WorldOrder(id=o.id, size=o.size, qty=o.qty, done=o.done, due=o.due, customer=o.customer or "—",
                          status=self._seed_status(o, t), rush=o.priority >= 2, source="seed",
                          shipped_at=self._seed_shipped.get(o.id))  # type: ignore[arg-type]
               for o in self.f._orders.values()]
        out += [WorldOrder(id=o.id, size=o.size, qty=o.qty, done=o.done, due=o.due, customer=o.customer,
                           status=o.status, rush=o.rush, source="world", created_at=o.created_at,  # type: ignore[arg-type]
                           shipped_at=o.shipped_at)
                for o in self._orders.values()]
        return out

    def order_inputs(self, t: datetime | None = None) -> list[dict[str, Any]]:
        """Every order the dashboard shows (the factory's order book AND the orders that arrived in the world), with
        the fields the order brief needs and the dashboard's view does not carry: when a world order was scheduled in
        or completed, and the exact due time a seed order has. Read only; ids are unique (the seed book wins)."""
        t = t or self._t
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for o in self.f._orders.values():
            seen.add(o.id)
            out.append({"id": o.id, "size": o.size, "qty": o.qty, "done": o.done, "due": o.due,
                        "due_at": self.f.due_at(o.id), "customer": o.customer, "status": self._seed_status(o, t),
                        "rush": o.priority >= 2, "source": "seed", "start_at": None,
                        "completed_at": self._seed_completed.get(o.id), "shipped_at": self._seed_shipped.get(o.id)})
        for o in self._orders.values():
            if o.id in seen:
                continue
            out.append({"id": o.id, "size": o.size, "qty": o.qty, "done": o.done, "due": o.due, "due_at": None,
                        "customer": o.customer, "status": o.status, "rush": o.rush, "source": "world",
                        "start_at": o.start_at, "completed_at": o.completed_at, "shipped_at": o.shipped_at})
        return out

    def _orders_view(self, t: datetime) -> OrdersView:
        items = self._all_orders(t)
        pipe = {s: 0 for s in ORDER_STAGES}
        for o in items:
            pipe[o.status] += 1
        rank = {s: i for i, s in enumerate(ORDER_STAGES)}
        items.sort(key=lambda o: (rank[o.status] == 4, not o.rush, rank[o.status], o.due, o.id))
        rush_open = sum(1 for o in items if o.rush and o.status in ("待排程", "生產中"))
        return OrdersView(pipeline=pipe, items=items, new_today=self._new_today[1], rush_open=rush_open)

    # ------------------------------------------------------------------ materials and purchasing
    def _init_pos(self, t0: datetime) -> None:
        # one delivery already on its way at the start: nylon, promised for the next receiving morning
        eta = self._receiving_slot(t0 + timedelta(hours=2))
        self._pos.append(_PO(id="PO-1001", key="nylon", qty=400, ordered_at=t0 - timedelta(days=3), eta=eta,
                             arrive_at=eta, short=False))

    @staticmethod
    def _receiving_slot(t: datetime) -> datetime:
        lo, hi = RECEIVING_HOURS
        if t.hour < lo:
            return t.replace(hour=lo, minute=0, second=0, microsecond=0)
        if t.hour >= hi:
            return (t + timedelta(days=1)).replace(hour=lo, minute=0, second=0, microsecond=0)
        return t

    def _on_order(self, key: str) -> float:
        return sum(p.qty for p in self._pos if p.key == key and p.status in ("在途", "延遲", "卸貨中"))

    def _use_per_h(self, m: MaterialDef) -> float:
        return sum(r for mid, r in m.use_per_h.items() if self._status(mid) in WORKING)

    def _update_materials(self, t: datetime, dt: float) -> None:
        for m in MATERIALS:
            use = self._use_per_h(m) * dt / 3600.0
            if use <= 0:
                continue
            self._stock[m.key] = max(0.0, self._stock[m.key] - use)
            if self._stock[m.key] <= 0 and m.key not in self._stockout:
                self._stockout.add(m.key)
                self._emit(t, "stockout", f"{m.name} 庫存用完（產線仍運轉，需緊急調料）", m.key)
            if self._stock[m.key] + self._on_order(m.key) <= m.reorder_point:
                self._purchase(t, m)
        for p in self._pos:
            if p.status == "在途" and t >= p.eta and t < p.arrive_at:
                p.status = "延遲"
                m = MATERIAL_BY_KEY[p.key]
                self._emit(t, "delivery_late", f"{p.id} {m.name} 延遲到貨（原定 {p.eta:%m/%d %H:%M}）", p.id)

    def _purchase(self, t: datetime, m: MaterialDef) -> None:
        rng = self.rng
        pid = f"PO-{self._next_id('po')}"
        eta = self._receiving_slot(t + timedelta(hours=m.lead_h))
        late = rng.random() < LATE_SHARE
        short = rng.random() < SHORT_SHARE
        arrive = self._receiving_slot(eta + timedelta(hours=rng.uniform(4, 12))) if late else eta
        self._pos.append(_PO(id=pid, key=m.key, qty=m.order_qty, ordered_at=t, eta=eta, arrive_at=arrive, short=short))
        self._emit(t, "po_created", f"低於再訂購點：{pid} 向{m.supplier}訂 {m.name} {m.order_qty:g} {m.unit}，"
                                    f"預計 {eta:%m/%d %H:%M} 到貨", pid)

    def _materials_view(self) -> list[MaterialView]:
        out = []
        for m in MATERIALS:
            s = self._stock[m.key]
            per_day = sum(m.use_per_h.values()) * 24.0
            status = "out" if s <= 0 else "low" if s <= m.reorder_point * 0.5 else "reorder" if s <= m.reorder_point else "ok"
            out.append(MaterialView(key=m.key, name=m.name, unit=m.unit, stock=round(s, 1), capacity=m.capacity,
                                    reorder_point=m.reorder_point, on_order=self._on_order(m.key),
                                    use_per_day=round(per_day, 1),
                                    days_left=round(s / per_day, 1) if per_day > 0 else None, status=status))  # type: ignore[arg-type]
        return out

    def _po_view(self) -> list[PurchaseOrder]:
        keep = [p for p in self._pos if p.status != "已收貨"] + [p for p in self._pos if p.status == "已收貨"][-4:]
        out = []
        for p in keep:
            m = MATERIAL_BY_KEY[p.key]
            note = None
            if p.status == "已收貨" and p.received_qty is not None and p.received_qty < p.qty:
                note = f"短少 {p.qty - p.received_qty:g} {m.unit}"
            out.append(PurchaseOrder(id=p.id, material=p.key, name=m.name, qty=p.qty, unit=m.unit, supplier=m.supplier,
                                     ordered_at=p.ordered_at, eta=p.eta, status=p.status,  # type: ignore[arg-type]
                                     received_qty=p.received_qty, note=note))
        return out

    # ------------------------------------------------------------------ dock: deliveries and shipments
    def _plate(self) -> str:
        return f"{self.rng.choice('KLMNRT')}{self.rng.choice('ABCDE')}{self.rng.choice('ABCDE')}-{self.rng.randint(1000, 9999)}"

    def _waiting_ship(self, t: datetime) -> list[tuple[str, int, str]]:
        """(order id, qty, source) of every order waiting to ship."""
        out = [(o.id, o.qty, "seed") for o in self.f._orders.values() if self._seed_status(o, t) == "待出貨"]
        out += [(o.id, o.qty, "world") for o in self._orders.values() if o.status == "待出貨"]
        return out

    def _update_dock(self, t: datetime) -> None:
        # trucks leaving
        for tr in list(self._trucks):
            if t < tr.leaves_at:
                continue
            self._trucks.remove(tr)
            if tr.kind == "inbound":
                p = next(p for p in self._pos if p.id == tr.po_id)
                m = MATERIAL_BY_KEY[p.key]
                got = round(p.qty * self.rng.uniform(0.75, 0.9)) if p.short else p.qty
                p.status, p.received_qty = "已收貨", float(got)
                self._stock[m.key] = min(m.capacity, self._stock[m.key] + got)
                self._stockout.discard(m.key)
                self._dock_log.append(DockEntry(ts=t, kind="depart", direction="inbound",
                                                text=f"{tr.plate} 卸貨完成離場（{m.name} {got:g} {m.unit}）"))
                if p.short:
                    self._emit(t, "delivery_short", f"{p.id} {m.name} 短少：訂 {p.qty:g}，到 {got:g} {m.unit}", p.id)
                else:
                    self._emit(t, "delivery", f"{p.id} {m.name} {got:g} {m.unit} 收貨入庫", p.id)
            else:
                n = 0
                shipped: list[tuple[str, int, bool]] = []
                for oid in tr.order_ids:
                    if oid in self._orders:
                        o = self._orders[oid]
                        o.status, o.shipped_at = "已出貨", t
                        n += o.qty
                        shipped.append((oid, o.qty, t.date() <= o.due))
                    elif oid in self.f._orders:
                        self._seed_shipped[oid] = t
                        n += self.f._orders[oid].qty
                        shipped.append((oid, self.f._orders[oid].qty, t.date() <= self.f._orders[oid].due))
                self._dock_log.append(DockEntry(ts=t, kind="depart", direction="outbound",
                                                text=f"{tr.plate} 出車：{len(tr.order_ids)} 筆訂單 {n} 條"))
                self._emit(t, "shipment", f"出車 {tr.plate}：{'、'.join(tr.order_ids)}（{n} 條）", tr.id)
                for oid, q, on_time in shipped:          # issue #36: one success moment per order
                    self._emit(t, "shipment_departed", f"出貨 {oid} 已出廠（{q} 條）{'' if on_time else '・逾期'}", tr.id,
                               order_ids=[oid], qty=q, on_time=on_time)
        # inbound arrivals
        for p in self._pos:
            if p.status in ("在途", "延遲") and t >= p.arrive_at:
                m = MATERIAL_BY_KEY[p.key]
                tr = _Truck(id=f"T-{self._next_id('truck'):03d}", kind="inbound", plate=self._plate(),
                            cargo=f"{m.name} {p.qty:g} {m.unit}（{m.supplier}）", arrived_at=t,
                            leaves_at=t + timedelta(minutes=UNLOADING_MIN), po_id=p.id)
                p.status, p.truck = "卸貨中", tr.id
                self._trucks.append(tr)
                self._dock_log.append(DockEntry(ts=t, kind="arrive", direction="inbound", text=f"{tr.plate} 到站收貨：{tr.cargo}"))
                self._emit(t, "truck_arrive", f"進貨車 {tr.plate} 到站：{tr.cargo}", p.id)
        # outbound: a truck comes LOADING_MIN before each dispatch slot when something waits to ship
        if t.weekday() in DISPATCH_DAYS:
            for dt_ in DISPATCH_TIMES:
                slot = datetime.combine(t.date(), dt_, tzinfo=TZ)
                key = slot.isoformat()
                if key in self._outbound_done or not (slot - timedelta(minutes=LOADING_MIN) <= t < slot):
                    continue
                self._outbound_done.add(key)
                waiting = self._waiting_ship(t)
                if not waiting:
                    continue
                n = sum(q for _, q, _ in waiting)
                tr = _Truck(id=f"T-{self._next_id('truck'):03d}", kind="outbound", plate=self._plate(),
                            cargo=f"成品 {n} 條（{len(waiting)} 筆訂單）", arrived_at=t, leaves_at=slot,
                            order_ids=[oid for oid, _, _ in waiting])
                self._trucks.append(tr)
                self._dock_log.append(DockEntry(ts=t, kind="arrive", direction="outbound", text=f"{tr.plate} 到站裝貨：{tr.cargo}"))
                self._emit(t, "truck_arrive", f"出貨車 {tr.plate} 到站裝貨：{tr.cargo}", tr.id)

    def _next_departure(self, t: datetime) -> datetime | None:
        for k in range(8):
            d = t.date() + timedelta(days=k)
            if d.weekday() not in DISPATCH_DAYS:
                continue
            for dt_ in DISPATCH_TIMES:
                slot = datetime.combine(d, dt_, tzinfo=TZ)
                if slot > t:
                    return slot
        return None

    def _dock_view(self, t: datetime) -> DockView:
        pending = [p.arrive_at for p in self._pos if p.status in ("在途", "延遲")]
        return DockView(trucks=[Truck(id=tr.id, kind=tr.kind, plate=tr.plate, cargo=tr.cargo,  # type: ignore[arg-type]
                                      arrived_at=tr.arrived_at, leaves_at=tr.leaves_at) for tr in self._trucks],
                        next_departure=self._next_departure(t),
                        next_arrival=min((p.eta for p in self._pos if p.status in ("在途", "延遲")), default=None)
                        if pending else None,
                        log=list(reversed(self._dock_log)))

    def _fg_view(self) -> FinishedGoods:
        by = {s.value: 0 for s in TireSize}
        waiting = 0
        t = self._t
        for o in self.f._orders.values():
            if o.id not in self._seed_shipped:
                by[o.size.value] += o.done
                if self._seed_status(o, t) == "待出貨":
                    waiting += o.qty
        for o in self._orders.values():
            if o.status != "已出貨":
                by[o.size.value] += o.done
                if o.status == "待出貨":
                    waiting += o.qty
        return FinishedGoods(by_size=by, total=sum(by.values()), capacity=FG_CAPACITY, waiting_ship=waiting)

    # ------------------------------------------------------------------ quality
    def _count_inspection(self, t: datetime, defect: bool) -> None:
        d, n, bad = self._q_today
        if d != t.date():
            d, n, bad = t.date(), 0, 0
        self._q_today = (d, n + 1, bad + (1 if defect else 0))

    def _inspector(self) -> str:
        qc = [w for w in self._workers if w.team is TeamId.QC and w.state not in ("下班", "請假")]
        return qc[0].name if qc else "作業員自主檢查"

    def _final_inspection(self, t: datetime, pid: str, job_id: str, order_id: str, size: TireSize) -> None:
        abnormal = job_id in self._job_abnormal
        self._job_abnormal.discard(job_id)
        defect = self.rng.random() < (ABNORMAL_DEFECT if abnormal else BASE_DEFECT)
        cause = (UNDERCURE if abnormal else self.rng.choice(CURE_DEFECTS)) if defect else None
        disp = ("報廢" if abnormal else "整修後複檢") if defect else None
        qid = f"QC-{self._next_id('qc'):05d}"
        self._inspections.append(Inspection(id=qid, ts=t, stage="final", line="CURE", machine_id=pid, batch=job_id,
                                            order_id=order_id, result="不良" if defect else "合格", cause=cause,
                                            disposition=disp, inspector=self._inspector()))
        self._count_inspection(t, defect)
        if defect:
            self._emit(t, "defect", f"成品檢驗不良：{pid} {SIZE_LABEL[size]}（{order_id}）{cause}，{disp}", qid)

    def _update_quality(self, t: datetime) -> None:
        if t < self._next_inline:
            return
        self._next_inline += timedelta(minutes=INLINE_EVERY_MIN)
        for line in (Line.A, Line.B, Line.C, Line.D, Line.BOND):
            mids = [m.id for m in MACHINES if m.line is line]
            running = [mid for mid in mids if self._status(mid) in WORKING]
            if not running:
                continue
            bad = [mid for mid in mids if self._status(mid) in (MachineStatus.WARNING, MachineStatus.ALARM, MachineStatus.STOPPED)
                   or self.f._active(mid, t, self.f._PROCESS_KINDS)]
            mid = bad[0] if bad else self.rng.choice(running)
            defect = self.rng.random() < (ABNORMAL_DEFECT if bad else BASE_DEFECT)
            cause = MACHINE_DEFECT.get(mid) if defect else None
            qid = f"QC-{self._next_id('qc'):05d}"
            batch = self.f.machine(mid).batch_id or mid
            self._inspections.append(Inspection(id=qid, ts=t, stage="inline", line=line.value, machine_id=mid,
                                                batch=batch, result="不良" if defect else "合格", cause=cause,
                                                disposition="批次隔離待判" if defect else None, inspector=self._inspector()))
            self._count_inspection(t, defect)
            if defect:
                self._emit(t, "defect", f"製程抽檢不良：{MACHINE_BY_ID[mid].name}（{mid}）批 {batch} {cause}，批次隔離", qid)

    def _quality_view(self) -> QualityView:
        d, n, bad = self._q_today
        if d != self._t.date():
            n, bad = 0, 0
        incidents: list[IncidentRecord] = []
        daily: list[DailyRecord] = []
        if self.ds is not None:
            shift = self._ds_date(self.t0.date()) - self.t0.date()
            today = self._t.date()
            incidents = [IncidentRecord(id=i.incident_id, date=i.date - shift, machine_id=i.machine_id, equipment=i.equipment,
                                        symptom=i.symptom, action=i.action, downtime_min=i.downtime_min, outcome=i.outcome)
                         for i in self.ds.incidents if i.date - shift < today]
            incidents.sort(key=lambda r: r.date, reverse=True)
            daily = [DailyRecord(date=x.date - shift, tires_done=x.tires_done, flex_util_pct=x.flex_util_pct,
                                 ot_hours=x.ot_hours, downtime_min=x.downtime_min)
                     for x in self.ds.daily if x.date - shift < today]
        return QualityView(inspected_today=n, defects_today=bad, yield_pct=round(100.0 * (n - bad) / n, 1) if n else None,
                           recent=list(reversed(self._inspections)), incidents=incidents, daily=daily)

    # ------------------------------------------------------------------ maintenance
    def _plan_maintenance(self, t0: datetime) -> list[_Maint]:
        """Planned windows on Sundays: presses every 4 weeks, other machines every 2 (staggered by index)."""
        out: list[_Maint] = []
        d0 = t0.date()
        first_sun = d0 + timedelta(days=PM_SPAN_DAYS[0] + (6 - (d0 + timedelta(days=PM_SPAN_DAYS[0])).weekday()) % 7)
        week = 0
        d = first_sun
        while d <= d0 + timedelta(days=PM_SPAN_DAYS[1]):
            iso_week = d.isocalendar()[1]
            slot = 0
            for i, m in enumerate(MACHINES):
                period = 4 if m.kind is MachineKind.CURING_PRESS else 2
                if (i + iso_week) % period:
                    continue
                hours = PM_HOUR.get(m.kind, PM_DEFAULT_H)
                start = datetime.combine(d, time(8), tzinfo=TZ) + timedelta(hours=(slot % 3) * 3)
                slot += 1
                title = "硫化機定期保養（模具清潔、蒸汽閥檢查）" if m.kind is MachineKind.CURING_PRESS else \
                    "密煉機定期保養（轉子間隙、密封）" if m.kind is MachineKind.MIXER else "定期保養（潤滑、皮帶、感測器校正）"
                out.append(_Maint(id=f"PM-{d:%m%d}-{m.id}", machine_id=m.id, kind="planned", title=title, start=start,
                                  end=start + timedelta(hours=hours)))
            d += timedelta(days=7)
            week += 1
        for x in out:                      # windows already over at the start are history, not news
            if x.end is not None and x.end <= t0:
                x.announced = 2
            elif x.start <= t0:
                x.announced = 1
        return out

    def _dataset_mold_changes(self, t0: datetime) -> list[_Maint]:
        """Team dataset mold changes (data/team_dataset/mold_change.csv), moved onto the sim clock."""
        if self.ds is None:
            return []
        shift = self._ds_date(t0.date()) - t0.date()
        out = []
        for i, mc in enumerate(self.ds.mold_changes):
            start, end = mc.start - shift, mc.end - shift
            x = _Maint(id=f"MC-{start:%m%d}-{mc.machine_id}-{i}", machine_id=mc.machine_id, kind="mold_change",
                       title=f"換模 {mc.from_mold}→{mc.to_mold}（停機 {(mc.end - mc.start).total_seconds() / 3600:g} h）",
                       start=start, end=end, source="team_dataset")
            x.announced = 2 if end <= t0 else 1 if start <= t0 else 0
            out.append(x)
        return out

    def _maint_staff(self) -> str | None:
        on = [w for w in self._workers if w.team is TeamId.MAINTENANCE and w.state not in ("下班", "請假")]
        return on[0].name if on else None

    def _update_maintenance(self, t: datetime) -> None:
        for x in self._maint:
            if x.announced == 0 and x.start <= t:
                x.announced, x.assignee = 1, self._maint_staff()
                self._emit(t, "maint_start", f"計畫保養開始：{MACHINE_BY_ID[x.machine_id].name}（{x.machine_id}）", x.id)
            if x.announced == 1 and x.end is not None and x.end <= t:
                x.announced = 2
                self._emit(t, "maint_end", f"計畫保養完成：{MACHINE_BY_ID[x.machine_id].name}（{x.machine_id}）", x.id)
        # corrective: a machine the safety core stopped, or taken out of service, gets a repair ticket
        for m in MACHINES:
            st = self._status(m.id)
            rep = self._repairs.get(m.id)
            if st in (MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE) and rep is None:
                rid = f"RP-{self._next_id('rep'):04d}"
                note = self.f._m[m.id].note or "停機"
                rep = _Maint(id=rid, machine_id=m.id, kind="repair", title=f"異常處理：{note}"[:60], start=t, end=None,
                             assignee=self._maint_staff(), announced=1)
                self._repairs[m.id] = rep
                self._emit(t, "repair_open", f"維修單 {rid}：{m.name}（{m.id}）{note}"[:80], rid)
            elif rep is not None and st in (MachineStatus.RUNNING, MachineStatus.IDLE):
                rep.end, rep.announced = t, 2
                del self._repairs[m.id]
                self._closed_repairs.append(rep)
                self._emit(t, "repair_close", f"維修單 {rep.id} 結案：{m.name}（{m.id}）恢復運轉", rep.id)

    def _maint_view(self, t: datetime) -> MaintenanceView:
        def item(x: _Maint) -> MaintenanceItem:
            st = "done" if x.end is not None and x.end <= t else "in_progress" if x.start <= t else "planned"
            return MaintenanceItem(id=x.id, machine_id=x.machine_id, kind=x.kind, title=x.title, start=x.start,  # type: ignore[arg-type]
                                   end=x.end, status=st, assignee=x.assignee, source=x.source)  # type: ignore[arg-type]
        running = [item(x) for x in self._maint if x.start <= t and (x.end is None or t < x.end)]
        running += [item(x) for x in self._repairs.values()]
        ahead = sorted((x for x in self._maint if x.start > t), key=lambda x: x.start)
        upcoming = ahead[:7]
        nxt_mold = next((x for x in ahead if x.kind == "mold_change"), None)   # always show the next dataset mold change
        if nxt_mold is not None and nxt_mold not in upcoming:
            upcoming.append(nxt_mold)
        done = sorted((x for x in self._maint if x.end is not None and x.end <= t), key=lambda x: x.end or t)[-5:]
        recent = [item(x) for x in list(self._closed_repairs)[-3:] + done]
        recent.sort(key=lambda i: i.end or i.start, reverse=True)
        return MaintenanceView(in_progress=running, upcoming=[item(x) for x in upcoming], recent=recent[:8])

    # ------------------------------------------------------------------ people
    def _ds_date(self, d: date) -> date:
        if d not in self._ds_cache:
            assert self.ds is not None
            self._ds_cache[d] = self.ds.dataset_date(d, self._scenario, self.t0.date())
        return self._ds_cache[d]

    def _init_workers(self) -> None:
        self._workers: list[_Worker] = []
        names = iter(NAMES)
        n = 0
        production = OPTIMISED_SHIFTS
        if self.ds is not None:      # the team dataset's 20 staff replace the generated production teams
            production = ()
            slots: dict[tuple[str, str], int] = {}
            for st in self.ds.staff:
                k = slots.get((st.work_group, st.shift), 0)
                slots[(st.work_group, st.shift)] = k + 1
                machines = WORK_GROUP_MACHINES.get(st.work_group, ())
                sd = ShiftDef(WORK_GROUP_TEAM.get(st.work_group, TeamId.BODY), "早班" if st.shift == "早" else "夜班",
                              time(8) if st.shift == "早" else time(20), time(17) if st.shift == "早" else time(5))
                self._workers.append(_Worker(
                    id=st.staff_id, name=st.staff_id, team=sd.team, role="作業員", sd=sd, slot=k,
                    home=machines[k % len(machines)] if machines else None, machines=machines,
                    group=st.work_group, staff_id=st.staff_id, since=self.t0))
        for sd in production + SUPPORT_SHIFTS:
            machines = TEAM_MACHINES.get(sd.team, ())
            for k in range(sd.headcount):
                n += 1
                role = {TeamId.MAINTENANCE: "維修", TeamId.QC: "品管", TeamId.SUPERVISOR: "主管"}.get(
                    sd.team, "組長" if k == 0 else "作業員")
                home = machines[k % len(machines)] if machines else None
                self._workers.append(_Worker(id=f"P{n:02d}", name=next(names), team=sd.team, role=role, sd=sd, slot=k,
                                             home=home, machines=machines, since=self.t0))

    def _duty(self, w: _Worker, t: datetime) -> tuple[datetime | None, bool]:
        """(start of the shift covering t, absent). Team-dataset staff follow their roster (dates shifted to the
        sim clock); generated staff follow their ShiftDef."""
        if w.staff_id is None or self.ds is None:
            start = _shift_instance(w.sd, t)
            return start, start is not None and self._absent(w, start)
        w.note = None
        for back in (0, 1):
            d = t.date() - timedelta(days=back)
            row = self.ds.roster.get((w.staff_id, self._ds_date(d)))
            if row is None:
                continue
            if row.code == "病":        # sick: the shift they would have worked
                start = datetime.combine(d, w.sd.start, tzinfo=TZ)
                end = datetime.combine(d, w.sd.end, tzinfo=TZ)
            elif row.start is not None and row.end is not None:
                start = datetime.combine(d, row.start, tzinfo=TZ)
                end = start + timedelta(hours=row.hours) if row.code == "加" else datetime.combine(d, row.end, tzinfo=TZ)
            else:
                continue
            if end <= start:
                end += timedelta(days=1)
            if start <= t < end:
                if row.code == "病":
                    w.note = f"病假：{row.note}" if row.note else "病假"
                    return start, True
                if row.code == "加":
                    w.note = f"加班 {row.ot_hours:g} h" + (f"：{row.note}" if row.note else "")
                return start, False
        return None, False

    def _sick_call(self, w: _Worker, start: datetime | None) -> bool:
        """Issue #17: a scripted sick call (PlantInput absence) for this staff member on this shift's date."""
        if start is None or w.staff_id is None:
            return False
        note = self._extra_absent.get((w.staff_id, start.date()))
        if note is None:
            return False
        w.note = note
        return True

    def _absent(self, w: _Worker, start: datetime) -> bool:
        """Page 11: the 胎體／胎面膠組 night shift of the scenario date is one operator short (10/03 夜班缺 1 人)."""
        return (w.team is TeamId.BODY and w.sd.name == "夜班" and start.date() == self.f._gap_date
                and w.slot == w.sd.headcount - 1)

    def _set(self, w: _Worker, t: datetime, state: str, zone: str, machine: str | None = None,
             until: datetime | None = None) -> None:
        if (w.state, w.zone, w.machine_id) != (state, zone, machine):
            w.since = t
        w.state, w.zone, w.machine_id, w.until = state, zone, machine, until

    def _work(self, w: _Worker, t: datetime) -> None:
        """Default place for an on-duty worker when nothing temporary is going on."""
        if w.team is TeamId.SUPERVISOR:
            self._set(w, t, "待命", "war_room")
            return
        if w.team is TeamId.MAINTENANCE:
            jobs = [r.machine_id for r in self._repairs.values()] + \
                [x.machine_id for x in self._maint if x.start <= t and x.end is not None and t < x.end]
            if jobs:
                mid = jobs[w.slot % len(jobs)]
                self._set(w, t, "在機台", zone_of(mid), mid)
            else:
                self._set(w, t, "待命", "staff")
            return
        if w.team is TeamId.TRIM:         # 修邊組 整修 (manual finishing) by the presses
            self._set(w, t, "在機台", "line_CURE")
            return
        if w.team is TeamId.QC:
            lines = ("A", "B", "C", "D", "BOND", "CURE")
            ln = lines[(t.hour + w.slot * 3) % len(lines)]
            mid = next(m.id for m in MACHINES if m.line.value == ln)
            self._set(w, t, "在機台", f"line_{ln}", mid)
            return
        # night 胎體／胎面膠組 also de-molds / molds in (page 6): a press changing molds pulls one of them over
        movers = w.team in (TeamId.CURING, TeamId.BOND) if w.staff_id else \
            (w.team is TeamId.CURING or (w.team is TeamId.BODY and w.sd.name == "夜班"))
        if movers and w.slot == 0:
            changing = [pid for pid, ps in self.f._press.items() if ps.job is None and self._status(pid) is MachineStatus.IDLE]
            if changing:
                self._set(w, t, "在機台", "line_CURE", changing[0])
                return
        home = w.home
        if home is not None and self._status(home) in WORKING:
            self._set(w, t, "在機台", zone_of(home), home)
            return
        team = w.machines or TEAM_MACHINES.get(w.team, ())
        live = [mid for mid in team if self._status(mid) in WORKING]
        if live:
            mid = live[w.slot % len(live)]
            self._set(w, t, "在機台", zone_of(mid), mid)
        else:
            self._set(w, t, "待命", "staff")

    def _update_people(self, t: datetime, dt: float) -> None:
        rng = self.rng
        dock_busy = bool(self._trucks)
        busy = self.tasks.busy_worker_ids()
        for w in self._workers:
            start, absent = self._duty(w, t)
            if start is not None and not absent and self._sick_call(w, start):
                absent = True
            if w.id in busy:
                # on a task (issue #17): walk to its place, then do it there. The RNG draw of a normal step is
                # still taken so the world's random stream does not depend on who is busy
                if dt > 0 and start is not None and not absent:
                    rng.random()
                tk = self.tasks.task_of(w.id)
                if tk is not None:
                    self._set(w, t, "前往中" if tk.status == "前往中" else "執行中", tk.zone, tk.machine_id)
                    continue
            if start is None:
                self._set(w, t, "下班", "off_site")
                continue
            if absent:
                self._set(w, t, "請假", "off_site")
                continue
            if w.state in ("下班", "請假"):
                self._work(w, t)
                continue
            if w.until is not None and t < w.until:
                continue
            # meal break: 4 h into the shift for even slots, 5 h for odd ones
            meal = start + timedelta(hours=4 + (w.slot % 2))
            if meal <= t < meal + timedelta(minutes=MEAL_MIN) and w.team is not TeamId.SUPERVISOR:
                self._set(w, t, "休息", "break", until=meal + timedelta(minutes=MEAL_MIN))
                continue
            if dt <= 0:
                self._work(w, t)
                continue
            r = rng.random()
            p_toilet = _p(1.0 / TOILET_EVERY_H, dt)
            carry_rate = (1.0 / CARRY_EVERY_H) * (CARRY_BOOST_AT_DOCK if dock_busy else 1.0)
            p_carry = _p(carry_rate, dt) if w.role in ("作業員", "組長") else 0.0
            p_coffee = _p(1.0 / COFFEE_EVERY_H, dt)
            if r < p_toilet:
                self._set(w, t, "如廁", "toilet", until=t + timedelta(minutes=rng.randint(*TOILET_MIN)))
            elif r < p_toilet + p_carry:
                zone = "dock" if dock_busy or rng.random() < 0.5 else (zone_of(w.home) if w.home else "dock")
                self._set(w, t, "搬運", zone, until=t + timedelta(minutes=rng.randint(*CARRY_MIN)))
            elif r < p_toilet + p_carry + p_coffee:
                self._set(w, t, "休息", "break", until=t + timedelta(minutes=rng.randint(*COFFEE_MIN)))
            else:
                self._work(w, t)

    # ------------------------------------------------------------------ issue #17: plant inputs, tasks, goods
    def on_plant_input(self, pin: Any, at: datetime) -> None:
        """A scripted plant input (the Factory already changed the order book / shifts): show it in the feed and
        mark a sick call on the roster."""
        if pin.kind == "absence" and pin.staff_id:
            self._extra_absent[(pin.staff_id, at.date())] = f"病假：{pin.note}" if pin.note else "病假"
            self._emit(at, "absence", pin.text, pin.staff_id)
        elif pin.kind == "rush_order" and pin.order is not None:
            self._emit(at, "order_rush", pin.text, pin.order.id)
        elif pin.kind == "shift_start":
            self._emit(at, "shift_start", pin.text, None)

    def on_order_arrival(self, order: Any, at: datetime) -> None:
        """Issue #104: a scripted normal order joined the factory's order book: show it in the feed."""
        self._emit(at, "order_new", f"新訂單 {order.id}：{order.customer} {SIZE_LABEL[order.size]} {order.qty} 條，"
                                    f"交期 {order.due:%m/%d}{'（優先）' if order.priority >= 2 else ''}", order.id)

    # ------------------------------------------------------------------ issue #36 模擬事件 (world only)
    # Material events stay in the world: the feed shows them and the stock moves. Raising a card for a shortfall is
    # the patrol's job (#38, material_short owned there), so nothing here opens a plant event.
    def in_transit(self) -> list[tuple[str, str]]:
        """(PO id, material key) of the deliveries still on the way, for the random-event generator."""
        return [(p.id, p.key) for p in self._pos if p.status in ("在途", "延遲")]

    def delay_delivery(self, at: datetime, po_id: str, late_h: float, text: str) -> None:
        """The supplier calls: an in-transit delivery comes `late_h` hours later."""
        p = next((p for p in self._pos if p.id == po_id), None)
        if p is not None:
            p.arrive_at = self._receiving_slot(max(p.arrive_at, p.eta) + timedelta(hours=late_h))
        self._emit(at, "delivery_late", text, po_id)

    def empty_stock(self, at: datetime, key: str) -> None:
        """A lot fails inspection: the material's stock is gone. The next world step emits `stockout` and reorders,
        as for any stock that runs out."""
        if key in self._stock:
            self._stock[key] = 0.0

    def leave_now(self) -> list[dict[str, Any]]:
        """People on leave today, any shift (the team dataset roster's sick days, scripted sick calls, and anyone
        on leave right now), for query_orders_shifts."""
        out: dict[str, dict[str, Any]] = {}
        today = self._t.date()
        for w in self._workers:
            note = None
            if w.state == "請假":
                note = w.note or "請假"
            elif w.staff_id and (w.staff_id, today) in self._extra_absent:
                note = self._extra_absent[(w.staff_id, today)]
            elif w.staff_id and self.ds is not None:
                row = self.ds.roster.get((w.staff_id, self._ds_date(today)))
                if row is not None and row.code == "病":
                    note = f"病假：{row.note}" if row.note else "病假"
            if note is not None:
                out[w.id] = {"id": w.id, "name": w.name, "group": w.group, "team_label": TEAM_LABELS[w.team],
                             "shift": w.sd.name, "note": note}
        return list(out.values())

    def on_duty_count(self) -> int:
        return sum(1 for w in self._workers if w.state not in ("下班", "請假"))

    def plan(self) -> Any:
        """The ACTIVE schedule the plant runs (None without one)."""
        try:
            return self.f._plan()
        except Exception:  # noqa: BLE001
            return None

    def started_job_ids(self) -> set[str]:
        return set(getattr(self.f, "_plan_started", set()))

    def order_status(self, order_id: str) -> str:
        o = self.f._orders.get(order_id)
        return self._seed_status(o, self._t) if o is not None else "待排程"

    def next_press_for(self, order_id: str) -> str | None:
        plan = self.plan()
        if plan is None:
            return None
        started = self.started_job_ids()
        js = sorted((j for j in plan.jobs if j.order_id == order_id and not j.frozen and j.id not in started),
                    key=lambda j: j.mold_in)
        return js[0].press_id if js else None

    def materials_status(self) -> list[dict[str, Any]]:
        return [m.model_dump() for m in self._materials_view()]

    def worker_name(self, worker_id: str | None) -> str | None:
        """A dataset worker has only a staff id: 「S14（硫化機組）」, the team as every screen names it (TEAM_LABELS), not
        the dataset's 「加硫組」."""
        w = next((w for w in self._workers if w.id == worker_id), None)
        if w is None:
            return None
        return f"{w.name}（{TEAM_LABELS[w.team]}）" if w.group and w.name == w.staff_id else w.name

    def walk_minutes(self, worker_id: str, zone: str) -> float:
        from warroom.sim.work import WALK_DOCK_MIN, WALK_MIN, WALK_SAME_ZONE_MIN
        w = next((w for w in self._workers if w.id == worker_id), None)
        here = w.zone if w is not None else "staff"
        if here == zone:
            return WALK_SAME_ZONE_MIN
        return WALK_DOCK_MIN if "dock" in (here, zone) else WALK_MIN

    def pick_worker(self, team: TeamId, machine_id: str | None, busy: set[str], any_team: bool = False) -> str | None:
        """A free on-duty person for a task: same team first (one who runs that machine, then one at work, then
        anyone of the team on duty), else, when allowed, any free production operator. Deterministic."""
        def free(w: _Worker) -> bool:
            return w.id not in busy and w.state not in ("下班", "請假", "如廁") and w.role != "主管"
        pool = [w for w in self._workers if free(w) and w.team is team]
        if not pool and any_team:
            pool = [w for w in self._workers if free(w) and w.role in ("作業員", "組長")]
        if not pool:
            return None
        pool.sort(key=lambda w: (machine_id not in (w.machines or ()) and w.home != machine_id,
                                 w.state not in ("在機台", "待命"), w.slot, w.id))
        return pool[0].id

    def _people_view(self) -> PeopleView:
        workers = [Worker(id=w.id, name=w.name, team=w.team, team_label=TEAM_LABELS[w.team],
                          role=w.role, shift=w.sd.name, state=w.state, zone=w.zone,  # type: ignore[arg-type]
                          machine_id=w.machine_id, since=w.since or self.t0, note=w.note,
                          source="team_dataset" if w.staff_id else "generated") for w in self._workers]
        by_zone: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for w in workers:
            by_state[w.state] = by_state.get(w.state, 0) + 1
            if w.zone != "off_site":
                by_zone[w.zone] = by_zone.get(w.zone, 0) + 1
        on = sum(1 for w in workers if w.state not in ("下班", "請假"))
        return PeopleView(workers=workers, by_zone=by_zone, by_state=by_state, on_duty=on, total=len(workers))

    # ------------------------------------------------------------------ energy
    def _update_energy(self, t: datetime, dt: float) -> None:
        by: dict[str, float] = {}
        for m in MACHINES:
            rated = KW_OVERRIDE.get(m.id, KW_BY_KIND[m.kind])
            load = LOAD_BY_STATUS.get(self._status(m.id), 1.0)
            if load >= 1.0:
                load *= 1.0 + self.rng.uniform(-0.04, 0.04)
            by[m.line.value] = by.get(m.line.value, 0.0) + rated * load
        self._kw = {k: round(v, 1) for k, v in by.items()}
        total = sum(by.values()) + BASE_KW
        if self._kwh_date != t.date():
            self._kwh_date, self._kwh_today = t.date(), 0.0
        self._kwh_today += total * dt / 3600.0
        if not self._energy_hist or (t - self._energy_hist[-1][0]) >= timedelta(minutes=ENERGY_SAMPLE_MIN):
            self._energy_hist.append((t, round(total, 1)))

    def _energy_view(self, t: datetime) -> EnergyView:
        total = sum(self._kw.values()) + BASE_KW
        peak = t.weekday() in PEAK_DAYS and PEAK_HOURS[0] <= t.hour < PEAK_HOURS[1]
        return EnergyView(total_kw=round(total, 1), by_line=dict(self._kw), base_kw=BASE_KW, contract_kw=CONTRACT_KW,
                          peak=peak, kwh_today=round(self._kwh_today, 1), history=list(self._energy_hist))
