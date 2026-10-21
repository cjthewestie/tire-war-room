"""Cross-module contracts for the tire war room.

Every boundary between owners is defined here: data models (pydantic v2), enums, the
commander's constrained JSON output, the WebSocket/REST shapes the frontend consumes, and
the Protocols each module implements. Builders import from here and never redefine these.
A change here is a change to everyone's interface: keep web/src/contracts.ts in sync.

Conventions
- Machine ids come from warroom.factory (A-MIX ... CURE-01..CURE-11, BOND-01).
- All datetimes are timezone-aware, factory time Asia/Taipei. JSON = ISO 8601 with offset.
- Temperatures °C, pressures kgf/cm², water flow L/min, durations noted in field names.
- API JSON is produced with model.model_dump(mode="json").
"""
from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, AsyncIterator, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


# =====================================================================================
# Enums
# =====================================================================================
class Line(StrEnum):
    A = "A"          # 胎體／胎面膠
    B = "B"          # 胎側膠
    C = "C"          # 鋼絲圈
    D = "D"          # 尼龍布
    BOND = "BOND"    # 貼合
    CURE = "CURE"    # 硫化


class MachineKind(StrEnum):
    MIXER = "mixer"                    # 密煉機
    OPEN_MILL = "open_mill"            # 開煉機 / 大開煉機 / 小開煉機
    CALENDER = "calender"              # 壓延機
    COOLER = "cooler"                  # 冷卻機
    WINDER = "winder"                  # 捲取設備 / 纏繞機
    HYDRAULIC_PRESS = "hydraulic_press"  # 油壓機
    PAYOFF = "payoff"                  # 放線架
    EXTRUDER = "extruder"              # 押出機
    BLADDER = "bladder"                # 氣囊
    CUTTER = "cutter"                  # 裁切機
    CURING_PRESS = "curing_press"      # 硫化機


class TireSize(StrEnum):
    S = "S"   # 小胎 24 h
    M = "M"   # 中胎 28 h
    L = "L"   # 大胎 34 h


class TeamId(StrEnum):
    CURING = "curing"            # 硫化機組
    BODY = "body"                # 胎體／胎面膠組
    SIDE = "side"                # 胎側膠組
    BEAD_NYLON = "bead_nylon"    # 鋼絲／尼龍組
    BIGOPEN = "bigopen"          # 大開煉機組
    BOND = "bond"                # 貼合組
    MAINTENANCE = "maintenance"  # 維修
    QC = "qc"
    TRIM = "trim"                # 修邊組（資料集的第 8 個工作組：人工整修）
    SUPERVISOR = "supervisor"


class MachineStatus(StrEnum):
    RUNNING = "running"
    IDLE = "idle"
    WARNING = "warning"              # detector flagged, still inside safety limits
    ALARM = "alarm"                  # active event on this machine (dashboard red)
    STOPPED = "stopped"              # safety core stopped it (red path)
    OUT_OF_SERVICE = "out_of_service"  # taken out by decision (e.g. press 7 停用)
    OFFLINE = "offline"              # no data / bad data


class CompoundStage(StrEnum):
    NONE = "none"                    # machine does not hold rubber compound
    UNVULCANIZED = "unvulcanized"    # 煉膠, stop at ≥120°C
    CURATIVE_ADDED = "curative_added"  # 已加硫, stop at ≥110°C (scorch)


class Channel(StrEnum):
    TEMP = "temp_c"
    PRESSURE = "pressure_kgf_cm2"
    WATER_FLOW = "water_flow_lpm"
    STEAM = "steam_kgf_cm2"


class DataQuality(StrEnum):
    OK = "ok"
    OUT_OF_RANGE = "out_of_range"    # physically impossible value
    JUMP = "jump"                    # step larger than physics allows in 1 s
    STALE = "stale"                  # same value / no update too long
    NEIGHBOR_MISMATCH = "neighbor_mismatch"  # disagrees with redundant/neighbour sensor
    MISSING = "missing"


class AnomalyKind(StrEnum):
    OVERHEAT = "overheat"
    TEMP_DROP = "temp_drop"
    PRESSURE_LOW = "pressure_low"
    WATER_FLOW_LOW = "water_flow_low"
    STEAM_LOW = "steam_low"
    SENSOR_FAULT = "sensor_fault"
    MULTI_MACHINE = "multi_machine"  # several machines on one header/line at once
    SCHEDULE_SLIP = "schedule_slip"  # page 6 box 4: plan vs actual (late de-mold / mold-in / WIP behind T)
    SUPERVISOR_COMMAND = "supervisor_command"  # issue #10: a task the supervisor typed (POST /api/command)
    # issue #17: plant events (no sensor): the shift starts, a rush order arrives, someone calls in sick
    SHIFT_START = "shift_start"
    ORDER_RUSH = "order_rush"
    STAFFING_SHORT = "staffing_short"


class Severity(StrEnum):
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class TileStatus(StrEnum):
    NORMAL = "normal"
    WARN = "warn"
    ALARM = "alarm"


class EventState(StrEnum):
    """Event state machine (page 5): classify→plan→simulate→gate→execute→verify→close,
    ↺ REPLAN (swap hypothesis), ■ SAFE_STOP. AWAIT_APPROVAL sits between GATE and EXECUTE."""
    DETECTED = "detected"
    CLASSIFY = "classify"
    PLAN = "plan"
    SIMULATE = "simulate"
    GATE = "gate"
    AWAIT_APPROVAL = "await_approval"
    EXECUTE = "execute"
    VERIFY = "verify"
    REPLAN = "replan"
    CLOSED = "closed"
    SAFE_STOP = "safe_stop"


TERMINAL_STATES = frozenset({EventState.CLOSED, EventState.SAFE_STOP})

ALLOWED_TRANSITIONS: dict[EventState, frozenset[EventState]] = {
    EventState.DETECTED: frozenset({EventState.CLASSIFY, EventState.SAFE_STOP}),
    EventState.CLASSIFY: frozenset({EventState.PLAN, EventState.CLOSED, EventState.SAFE_STOP}),
    EventState.PLAN: frozenset({EventState.SIMULATE, EventState.SAFE_STOP}),
    EventState.SIMULATE: frozenset({EventState.GATE, EventState.REPLAN, EventState.SAFE_STOP}),
    EventState.GATE: frozenset({EventState.AWAIT_APPROVAL, EventState.EXECUTE, EventState.REPLAN,
                                EventState.SAFE_STOP}),
    EventState.AWAIT_APPROVAL: frozenset({EventState.EXECUTE, EventState.REPLAN, EventState.SAFE_STOP}),
    # EXECUTE -> AWAIT_APPROVAL: auto (R1) writes run first, then the event waits for the R2/R3
    # ones (page 2: "✓ 已自動 通知維修" while "☐ 待核准 放慢生料投入").
    EventState.EXECUTE: frozenset({EventState.VERIFY, EventState.AWAIT_APPROVAL, EventState.SAFE_STOP}),
    EventState.VERIFY: frozenset({EventState.CLOSED, EventState.REPLAN, EventState.SAFE_STOP}),
    EventState.REPLAN: frozenset({EventState.SIMULATE, EventState.SAFE_STOP}),
    EventState.CLOSED: frozenset(),
    EventState.SAFE_STOP: frozenset(),
}

# Page 7 threshold 3: retry a failed tool once, then SOP path; solver timeout -> backup
# solver; 2 consecutive failures -> SAFE_STOP keeping the previous schedule.
MAX_TOOL_RETRIES = 1
MAX_CONSECUTIVE_FAILURES = 2
# issue #10: a waiting plan may be re-planned from the supervisor's discussion at most this many times
MAX_DISCUSS_REPLANS = 3


class ToolKind(StrEnum):
    READ = "read"        # auto-run
    COMPUTE = "compute"  # auto-run
    WRITE = "write"      # must pass the action gate; R2/R3 need approval


class ToolName(StrEnum):
    """The allow-list. Page 5: six read/compute tools, three write tools."""
    READ_SENSORS = "read_sensors"                # 讀感測器
    PREDICT_ANOMALY = "predict_anomaly"          # 異常預測
    CURE_FORMULA = "cure_formula"                # 硫化公式 (Arrhenius)
    SOLVE_SCHEDULE = "solve_schedule"            # 排程求解 (OR-Tools)
    SOP_SEARCH = "sop_search"                    # SOP 知識庫 (Qdrant)
    QUERY_ORDERS_SHIFTS = "query_orders_shifts"  # 訂單／班表資料庫
    CONFIRM_SCHEDULE = "confirm_schedule"        # 確認新排程 (write)
    SET_CURE_TIME = "set_cure_time"              # 設定硫化時間（建議值）(write)
    NOTIFY_TEAMS = "notify_teams"                # 通知各組 (write)


READ_COMPUTE_TOOLS = frozenset({ToolName.READ_SENSORS, ToolName.PREDICT_ANOMALY, ToolName.CURE_FORMULA,
                                ToolName.SOLVE_SCHEDULE, ToolName.SOP_SEARCH,
                                ToolName.QUERY_ORDERS_SHIFTS})
WRITE_TOOLS = frozenset({ToolName.CONFIRM_SCHEDULE, ToolName.SET_CURE_TIME, ToolName.NOTIFY_TEAMS})

# Page 7 threshold 2: tools are opened by state.
TOOLS_BY_STATE: dict[EventState, frozenset[ToolName]] = {
    EventState.CLASSIFY: frozenset({ToolName.READ_SENSORS, ToolName.PREDICT_ANOMALY, ToolName.SOP_SEARCH}),
    EventState.PLAN: frozenset({ToolName.READ_SENSORS, ToolName.PREDICT_ANOMALY, ToolName.SOP_SEARCH,
                                ToolName.CURE_FORMULA, ToolName.QUERY_ORDERS_SHIFTS}),
    EventState.SIMULATE: frozenset({ToolName.CURE_FORMULA, ToolName.QUERY_ORDERS_SHIFTS,
                                    ToolName.SOLVE_SCHEDULE, ToolName.READ_SENSORS}),
    EventState.EXECUTE: WRITE_TOOLS,
    EventState.VERIFY: frozenset({ToolName.READ_SENSORS, ToolName.PREDICT_ANOMALY}),
    EventState.REPLAN: frozenset({ToolName.READ_SENSORS, ToolName.PREDICT_ANOMALY, ToolName.SOP_SEARCH,
                                  ToolName.CURE_FORMULA, ToolName.QUERY_ORDERS_SHIFTS}),
}

# 問戰情室 (ask the war room): the supervisor's read-only question path. Same allow-list, same
# registry, same audit log as an event, but only these tools are ever open: no write tool, and no
# solve_schedule (it stores a PROPOSED schedule version, which is a side effect).
ASK_TOOLS = frozenset({ToolName.READ_SENSORS, ToolName.PREDICT_ANOMALY, ToolName.CURE_FORMULA,
                       ToolName.SOP_SEARCH, ToolName.QUERY_ORDERS_SHIFTS})
ASK_MAX_STEPS = 4


class RiskLevel(StrEnum):
    """4 risk levels (page 7 threshold 4). Anything not on the allow-list is not a level:
    the gate REJECTs and logs it as INTERCEPTED."""
    R0_READ = "R0"       # read/compute, auto
    R1_NOTIFY = "R1"     # informational notify (e.g. 通知維修冷卻水路), auto + logged ("已自動")
    R2_CHANGE = "R2"     # schedule / cure-time suggestion within one team, supervisor approval
    R3_CRITICAL = "R3"   # pause/slow feed, cross-team staff move, take machine out of service:
                         # supervisor approval with options and reason recorded


APPROVAL_REQUIRED = frozenset({RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL})


class GateVerdict(StrEnum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    REJECT = "reject"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    MODIFIED = "modified"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"   # issue #10: replaced by a newer card after a plan discussion; cannot be decided


class DecisionKind(StrEnum):
    APPROVE = "approve"   # approve option as proposed
    MODIFY = "modify"     # approve with changed args / a different option
    REJECT = "reject"


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    REJECTED = "rejected"
    CONFIRMED = "confirmed"


class SafetyActionKind(StrEnum):
    STOP_MACHINE = "stop_machine"   # compound overheat: stop + hold batch
    HOLD_BATCH = "hold_batch"
    CURE_HOLD = "cure_hold"         # press below 120°C: hold press, block new mold-in, flag tire for QC
    ALARM_ONLY = "alarm_only"       # warning light / buzzer without stop


class FaultKind(StrEnum):
    TEMP_RAMP = "temp_ramp"            # linear drift toward target at rate °C/min
    TEMP_STEP = "temp_step"
    WATER_FLOW_DROP = "water_flow_drop"
    STEAM_PRESSURE_DROP = "steam_pressure_drop"
    STEAM_LEAK = "steam_leak"          # persistent leak: steam & temp keep falling
    SENSOR_DRIFT = "sensor_drift"
    SENSOR_DROPOUT = "sensor_dropout"
    SPIKE = "spike"                    # single bad sample


class ScenarioName(StrEnum):
    NORMAL = "normal"
    PRESS7_TEMP_DROP = "press7_temp_drop"      # page 11
    OPEN_MILL_OVERHEAT = "open_mill_overheat"  # page 2
    BAD_DATA = "bad_data"                      # bad sensor must NOT wake the AI (page 10)
    CONCURRENT_3 = "concurrent_3"              # 3 anomalies at once (page 10 bench)
    # issue #17: plant events, no machine fault
    SHIFT_START = "shift_start"                # 早班開工 08:00: today's plan card
    RUSH_ORDER = "rush_order"                  # 插單: a rush order arrives, re-solve
    STAFFING = "staffing"                      # 人力調度: an operator calls in sick
    DAY_LOOP = "day_loop"                      # issue #36 一日循環: one dataset day 00:00 -> 24:00, repeated


class AuditKind(StrEnum):
    STATE_TRANSITION = "state_transition"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    GATE = "gate"
    INTERCEPTED = "intercepted"      # out-of-scope write blocked by gate/sandbox
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_DECISION = "approval_decision"
    SAFETY_ACTION = "safety_action"
    ANOMALY = "anomaly"
    SCHEDULE_VERSION = "schedule_version"
    ROLLBACK = "rollback"
    NOTIFY = "notify"
    SYSTEM = "system"
    TASK = "task"                    # issue #17: a worker task created from an approved step, or its status change


class ScheduleStatus(StrEnum):
    DRAFT = "draft"            # solver output, not proposed
    PROPOSED = "proposed"      # waiting approval
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class SolveStatus(StrEnum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    TIMEOUT = "timeout"
    ERROR = "error"


class SolverName(StrEnum):
    CP_SAT = "cp-sat"
    HEURISTIC = "heuristic"   # backup solver (page 7)


class LLMMode(StrEnum):
    LLM = "llm"     # real model over OpenAI-compatible API
    MOCK = "mock"   # deterministic rule-based commander, no model


# =====================================================================================
# Sensors, machines, factory state
# =====================================================================================
class SensorReading(_M):
    """One per machine per second (page 5 每秒讀值). Missing channel = None."""
    ts: datetime
    machine_id: str
    temp_c: float | None = None
    pressure_kgf_cm2: float | None = None
    water_flow_lpm: float | None = None
    steam_kgf_cm2: float | None = None
    seq: int = 0                       # per-machine monotonically increasing sample number
    quality: DataQuality = DataQuality.OK   # set by detect.validate(); sim always emits OK


class CureJobState(_M):
    """What a curing press is doing right now."""
    job_id: str
    order_id: str                      # e.g. "#A-311"
    size: TireSize
    mold_id: str
    mold_in_at: datetime
    planned_demold_at: datetime
    setpoint_c: float = 128.0
    cure_required_h: float             # at setpoint (24/28/34) or extended by cure_formula
    cure_elapsed_h: float              # wall-clock hours since mold-in


class MachineState(_M):
    machine_id: str
    line: Line
    name: str
    short_name: str
    status: MachineStatus
    last_reading: SensorReading | None = None
    step_elapsed_min: float = 0.0      # page 2 tile 時間 (minutes in current step/batch)
    compound_stage: CompoundStage = CompoundStage.NONE
    batch_id: str | None = None
    cure_job: CureJobState | None = None   # curing presses only
    active_event_id: str | None = None
    note: str | None = None


class SemiFinishedPart(_M):
    part: Literal["A", "B", "C", "D"]
    label: str            # "A 胎體／胎面膠"
    status_text: str      # "預熱中" / "成形中" / "包布中" / "已完成"
    state: Literal["waiting", "in_progress", "done", "alert"]
    machine_id: str | None = None


class SemiFinishedProgress(_M):
    tire_label: str       # "大胎 #07"
    order_id: str
    size: TireSize
    parts: list[SemiFinishedPart]
    planned_mold_in_at: datetime | None = None   # "大胎 #07 仍 17:00 入硫化機"
    note: str | None = None                      # e.g. "7 號機硫化中 14.4／28.5 h" when following a press event's tire
    note_state: Literal["waiting", "in_progress", "done", "alert"] | None = None


class Order(_M):
    id: str                 # "#A-311"
    size: TireSize
    qty: int
    done: int = 0
    due: date
    customer: str | None = None
    mold_id: str | None = None
    priority: int = 0       # higher = more urgent


class Shift(_M):
    """A concrete staffed window (a ShiftDef from factory.py instantiated on a date)."""
    id: str
    team: TeamId
    name: str               # "早班" / "中班" / "夜班"
    start: datetime
    end: datetime
    headcount: int
    required: int | None = None   # needed headcount; headcount < required = gap ("夜班缺 1 人")


class FaultSpec(_M):
    id: str
    machine_id: str
    kind: FaultKind
    start_offset_s: float = 0.0       # relative to injection time
    duration_s: float | None = None   # None = persists
    params: dict[str, float] = Field(default_factory=dict)  # e.g. {"target": 122, "rate_c_per_min": 1.0}


class PlantInput(_M):
    """Issue #17: something that happens to the plant that is not a machine fault, scripted by a scenario.
    It changes the plant's inputs (the order book, the roster), never a machine, and wakes the state
    machine as a plant event."""
    kind: Literal["shift_start", "rush_order", "absence"]
    text: str                         # "急單 #R-009 到：客戶G 中胎 2 條，交期 10/09"
    order: Order | None = None      # rush_order: the order that arrives
    staff_id: str | None = None       # absence: team dataset staff id (S13)
    team: TeamId | None = None      # absence: our team whose shift loses a person
    shift_name: str | None = None     # absence: "早班" (factory.OPTIMISED_SHIFTS name)
    people: int = 1
    note: str | None = None


class ScenarioStep(_M):
    at_offset_s: float
    fault: FaultSpec | None = None
    note: str | None = None
    plant: PlantInput | None = None   # issue #17


class Scenario(_M):
    name: ScenarioName
    title: str
    start_at: datetime                # sim clock start, e.g. 02:13:30 for page-11 demo
    steps: list[ScenarioStep]
    expected: list[str] = Field(default_factory=list)   # human-readable expected beats


# =====================================================================================
# Detection
# =====================================================================================
class Prediction(_M):
    machine_id: str
    channel: Channel
    now_value: float
    predicted_value: float
    band_lo: float
    band_hi: float
    horizon_s: float
    trend_per_min: float
    model: str = "ewma-linear"         # time-series model name (INT8 model on Spark)


class Anomaly(_M):
    id: str
    ts: datetime
    machine_id: str
    kind: AnomalyKind
    channel: Channel
    value: float
    baseline: float
    prediction: Prediction | None = None
    severity: Severity
    message: str                       # "7 號 128 → 122°C，預測穩定在 122±1°C"
    related_machines: list[str] = Field(default_factory=list)
    detection_latency_s: float | None = None   # fault start -> anomaly (sim knows fault start)
    # issue #54: the detector's pre-classification before any LLM: which rule fired, the signals and thresholds,
    # the validation the readings passed, the forecast model (warroom/detect/detector.py _make)
    preclass: dict[str, Any] | None = None


# =====================================================================================
# Safety core (red path)
# =====================================================================================
class SafetyRule(_M):
    id: str
    description: str
    machine_ids: list[str]
    channel: Channel
    op: Literal[">=", "<"]
    threshold: float
    compound_stage: CompoundStage | None = None   # rule only applies in this stage
    only_while_curing: bool = False
    action: SafetyActionKind


class SafetyAction(_M):
    id: str
    ts: datetime
    machine_id: str
    rule_id: str
    action: SafetyActionKind
    value: float
    threshold: float
    message: str                      # "7 號機 119.5°C < 120°C：安全核心停用並扣模"
    latency_ms: float                 # reading ts -> action decided


# =====================================================================================
# Tools
# =====================================================================================
class ToolSpec(_M):
    name: ToolName
    kind: ToolKind
    description: str                  # shown to the LLM
    params_schema: dict[str, Any]     # JSON Schema of args
    base_risk: RiskLevel              # gate may raise it from args (e.g. notify kind=slow_feed -> R3)
    allowed_states: list[EventState]
    timeout_s: float = 5.0


class ToolCall(_M):
    id: str
    event_id: str | None = None
    step_id: str | None = None
    tool: ToolName
    args: dict[str, Any]
    attempt: int = 1


class ToolResult(_M):
    call_id: str
    tool: ToolName
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    attempt: int = 1


# --- canonical args/results of the read/compute tools (data payload shapes) ---
class ReadSensorsArgs(_M):
    machine_ids: list[str]
    minutes: float = 30.0             # window of history; 0 = latest only


class ReadSensorsData(_M):
    readings: dict[str, list[SensorReading]]   # machine_id -> samples (downsampled ≤ 1/10 s ok)
    summary: dict[str, dict[str, float]]       # machine_id -> {"temp_c_now":.., "temp_c_min":.., "trend_c_per_min":..}


class PredictArgs(_M):
    machine_id: str
    channel: Channel = Channel.TEMP
    horizon_min: float = 30.0


class CureFormulaArgs(_M):
    """Arrhenius equivalent cure (page 11): rate(T)/rate(Tref) = exp(-Ea/R·(1/T − 1/Tref))."""
    size: TireSize
    ref_temp_c: float = 128.0
    actual_temp_c: float
    elapsed_h: float                  # hours already cured at ref temp
    ref_cure_h: float | None = None   # default factory.CURE_HOURS[size]
    activation_energy_kj_mol: float = 85.0


class CureFormulaData(_M):
    rate_ratio: float                 # 0.679 for 122 vs 128°C
    remaining_ref_h: float            # 18.0
    remaining_actual_h: float         # 14.1
    total_cure_h: float               # 28.5 (page 11, dataset EV-2)
    extra_h: float                    # +4.5
    formula: str


class QueryOrdersShiftsArgs(_M):
    order_ids: list[str] | None = None
    press_ids: list[str] | None = None
    from_ts: datetime | None = None
    to_ts: datetime | None = None


class QueryOrdersShiftsData(_M):
    orders: list[Order]
    shifts: list[Shift]
    gaps: list[str]                   # ["10/03 夜班缺 1 人"]
    next_jobs: list["ScheduleJob"]    # upcoming jobs on the queried presses
    # issue #17: read from the sim's world when it has one (people on leave today, material stock, today's mold-ins)
    leave: list[dict[str, Any]] | None = None
    materials: list[dict[str, Any]] | None = None
    today: dict[str, Any] | None = None


class SopSearchArgs(_M):
    query: str
    k: int = 3


class SopHit(_M):
    sop_id: str                       # "SOP-CP-02"
    title: str
    source: str                       # file name
    score: float
    text: str


class SolveScheduleArgs(_M):
    unavailable_presses: list[str] = Field(default_factory=list)   # e.g. ["CURE-07"] out of service
    # press_id -> new total cure h; bounded so a malformed plan fails the gate's format check
    cure_overrides_h: dict[str, Annotated[float, Field(gt=0, le=96)]] = Field(default_factory=dict)
    horizon_days: int = Field(default=7, ge=1, le=14)
    time_limit_s: float = Field(default=10.0, gt=0, le=30)
    allow_overtime: bool = True
    allow_cross_team: bool = True
    # issue #10: press -> [[start, end], ...] when it may hold no tire (maintenance). Filled by CODE from
    # the supervisor's constraints, never by the model; the solver keeps every new tire out of it.
    press_blackouts: dict[str, list[tuple[datetime, datetime]]] = Field(default_factory=dict)


class NotifyKind(StrEnum):
    MAINTENANCE = "maintenance"     # R1 通知維修
    INFO = "info"                   # R1
    SLOW_FEED = "slow_feed"         # R3 放慢生料投入 (human does it; no actuator permission)
    PAUSE_FEED = "pause_feed"       # R3 暫停投料
    STAFF_MOVE = "staff_move"       # R3 跨組調人
    OVERTIME = "overtime"           # R2 加班
    MACHINE_OUT = "machine_out"     # R3 機台停用
    DISPATCH = "dispatch"           # R2 派工：今日計畫／新排程派給現場（issue #17; code turns it into worker tasks）


class NotifyTeamsArgs(_M):
    teams: list[TeamId]
    kind: NotifyKind
    message: str
    machine_id: str | None = None


class SetCureTimeArgs(_M):
    press_id: str
    total_cure_h: float               # suggested value from cure_formula, never an AI guess
    reason: str


class ConfirmScheduleArgs(_M):
    schedule_version: int             # a PROPOSED version from solve_schedule
    reason: str


# =====================================================================================
# Scheduling
# =====================================================================================
class LaborNeed(_M):
    team: TeamId
    task: Literal["demold", "mold_in", "bonding", "a_feed", "side_bead", "trim"]
    start: datetime
    end: datetime
    people: int = 1


class ScheduleJob(_M):
    id: str
    order_id: str
    size: TireSize
    press_id: str
    mold_id: str | None = None
    mold_in: datetime                 # T (入硫化機)
    demold: datetime                  # mold_in + cure_h (出模時刻)
    cure_h: float
    bond_start: datetime              # T − 40 min / 1 h / 1 h 30 (page 6)
    a_feed_start: datetime            # T − 1 h 40 / 2 h 30 / 3 h 30
    side_pieces: int                  # 6 / 8 / 10
    bead_pieces: int
    labor: list[LaborNeed] = Field(default_factory=list)
    frozen: bool = False              # already in progress, solver may not move it


class StaffingChange(_M):
    kind: Literal["overtime", "transfer"]
    shift_date: date
    shift_name: str
    to_team: TeamId
    from_team: TeamId | None = None   # transfer only ("從 2 線調 1 人")
    from_label: str | None = None
    people: int = 1
    label: str                        # "夜班加班" / "從 2 線調 1 人"


class JobMove(_M):
    job_id: str
    order_id: str
    from_press: str | None
    to_press: str
    from_mold_in: datetime | None
    to_mold_in: datetime
    late_h: float = 0.0


class Schedule(_M):
    id: str
    created_at: datetime
    horizon_start: datetime
    horizon_end: datetime
    jobs: list[ScheduleJob]
    staffing_changes: list[StaffingChange] = Field(default_factory=list)
    solver: SolverName
    objective: float | None = None
    warnings: list[str] = Field(default_factory=list)


class SolveRequest(_M):
    now: datetime
    orders: list[Order]
    shifts: list[Shift]
    previous: Schedule | None = None
    unavailable_presses: list[str] = Field(default_factory=list)
    cure_overrides_h: dict[str, float] = Field(default_factory=dict)
    frozen_job_ids: list[str] = Field(default_factory=list)
    horizon_days: int = 7
    time_limit_s: float = 10.0
    allow_overtime: bool = True
    allow_cross_team: bool = True
    press_blackouts: dict[str, list[tuple[datetime, datetime]]] = Field(default_factory=dict)


class SolveResult(_M):
    status: SolveStatus
    solver: SolverName
    solve_ms: float
    schedule: Schedule | None
    feasible: bool                    # gate only passes writes when True (page 5)
    deliveries_met: bool
    late_orders: list[str] = Field(default_factory=list)
    moves: list[JobMove] = Field(default_factory=list)       # diff vs previous
    staffing_options: list[StaffingChange] = Field(default_factory=list)  # alternatives for the supervisor
    gaps: list[str] = Field(default_factory=list)
    fell_back: bool = False           # True if CP-SAT timed out and heuristic answered


class ScheduleVersion(_M):
    version: int                      # monotonically increasing
    parent_version: int | None
    status: ScheduleStatus
    created_at: datetime
    reason: str
    event_id: str | None = None
    schedule: Schedule


class CapacityRowOut(_M):
    label: str                        # 現況 / 只延長班表 / AI 排出模時刻
    small: float
    medium: float
    large: float


class CapacityReport(_M):
    rows: list[CapacityRowOut]        # must reproduce page 6 within ±0.1
    ml_100_weeks: tuple[float, float]     # (13.3, 9.3)
    small_100_weeks: tuple[float, float]  # (2.4, 2.0)
    conditions: str


# =====================================================================================
# Commander (the only LLM) — constrained JSON output
# =====================================================================================
class Expectation(_M):
    """What VERIFY (code, not LLM) compares against: e.g. CURE-07 temp_c >= 121 within 30 min."""
    machine_id: str
    channel: Channel
    op: Literal[">=", "<=", "between"]
    value: float
    value_hi: float | None = None     # for "between"
    within_min: float


class Hypothesis(_M):
    id: str                           # "H1", "H2"
    statement: str                    # "蒸汽壓不足，溫度會穩定"
    cause_code: str                   # "steam_pressure_low" | "steam_leak" | "sensor_drift" | "cooling_insufficient" | ...
    expectation: Expectation | None = None
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    evidence: list[str] = Field(default_factory=list)


class PlanStep(_M):
    """One step. args values may reference earlier results with "$S1.total_cure_h"
    (step id + dotted path into ToolResult.data); the state machine resolves them."""
    id: str                           # "S1".."S9"
    tool: ToolName
    args: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list)
    purpose: str                      # one line, Chinese, shown in timeline


class AlertCard(_M):
    """First alert card, ≤ 100 tokens, must reach the screen ≤ 2 s (page 9)."""
    title: str                        # "7 號機溫度下降 128→122°C"
    summary: str                      # one or two short lines


class CommanderPlan(_M):
    """The commander's whole output for one call; its JSON schema is passed to
    vLLM/xgrammar (guided_json) or response_format=json_schema."""
    goal: str                         # "7 號降溫，大胎不能延誤"
    hypothesis: Hypothesis
    rejected_hypothesis_ids: list[str] = Field(default_factory=list)
    steps: list[PlanStep]
    alert_card: AlertCard
    safe_stop: bool = False           # commander may request safe stop (keep original schedule)
    rationale: str = ""               # short, for the audit log


class CommanderRequest(_M):
    phase: Literal["plan", "replan"]
    event_id: str
    anomaly: Anomaly
    allowed_tools: list[ToolName]
    context: dict[str, Any] = Field(default_factory=dict)  # sensor window, SOP hits, prior results, failed expectation
    rejected: list[Hypothesis] = Field(default_factory=list)


class LLMCallInfo(_M):
    mode: LLMMode
    model: str
    base_url: str | None
    ttft_ms: float | None = None
    total_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    valid_json: bool = True
    fell_back_to_mock: bool = False


# =====================================================================================
# Gate, approvals, audit
# =====================================================================================
class GateDecision(_M):
    step_id: str
    tool: ToolName
    verdict: GateVerdict
    risk: RiskLevel | None            # None when rejected as out-of-scope
    checks: dict[str, bool]           # {"allow_list", "state_allowed", "format_ok", "simulation_feasible"}
    reasons: list[str] = Field(default_factory=list)


class ApprovalOption(_M):
    id: str                           # "A" / "B"
    label: str                        # "夜班加班"
    tool: ToolName
    args: dict[str, Any]
    recommended: bool = False


class ApprovalRequest(_M):
    id: str
    event_id: str
    step_id: str | None = None
    round: int = 1                    # "第二輪核准"
    created_at: datetime
    risk: RiskLevel
    title: str                        # "7 號機停用，重排中"
    reason: str                       # "H1 不成立：溫度續降至 119.5°C"
    done: list[str] = Field(default_factory=list)    # 已做
    gaps: list[str] = Field(default_factory=list)    # 缺口
    delivery_impact: str = ""                         # 交期 "不受影響"
    actions: list[str] = Field(default_factory=list)  # 核准內容: every gated write with its key args (code-built)
    options: list[ApprovalOption]
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: "ApprovalDecision | None" = None
    expires_at: datetime | None = None
    # issue #10: a plan discussion replaces a waiting card with a new version of the same round
    version: int = 1                  # v1, v2, ... within this round
    supersedes: str | None = None     # id of the card this one replaced
    changes: list[str] = Field(default_factory=list)      # "#A-311：9 號 → 8 號" (code-built, vs the replaced card)
    constraints: list[str] = Field(default_factory=list)  # chips in force: "9 號機 今晚不可用"


class ApprovalDecision(_M):
    request_id: str
    decision: DecisionKind
    option_id: str | None = None
    modified_args: dict[str, Any] | None = None
    reason: str = ""
    decided_by: str = "supervisor"
    via: Literal["phone", "touchscreen", "dashboard", "auto"] = "phone"
    decided_at: datetime | None = None   # server fills


class AuditEntry(_M):
    """Append-only (JSONL, hash-chained). hash = sha256(prev_hash + canonical json of the rest)."""
    seq: int
    ts: datetime
    kind: AuditKind
    actor: str                        # "detector" | "safety" | "state_machine" | "commander" | "gate" | "supervisor" | "tool:<name>"
    event_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str
    hash: str


class TimelineItem(_M):
    """One row of the page-11 timeline."""
    ts: datetime
    actor: Literal["事件偵測", "安全核心", "指揮官", "工具", "求解器", "主管", "驗證（程式）", "系統"]
    text: str
    tag: Literal["tool", "human", "safety", "decision", "dynamic", "info"] = "info"


class StepRun(_M):
    step: PlanStep
    state: Literal["pending", "running", "ok", "failed", "skipped", "awaiting_approval", "rejected"]
    result: ToolResult | None = None
    gate: GateDecision | None = None


class StepRound(_M):
    """An earlier round's step plan, kept when a replan replaces ev.steps (page 7: the S1..S5
    decomposition stays demoable after ↺ 換假設)."""
    round: int                        # 1 = first plan
    hypothesis_id: str | None = None
    goal: str | None = None
    steps: list[StepRun] = Field(default_factory=list)


class Event(_M):
    id: str                           # "EV-20261003-021400-CURE-07"
    opened_at: datetime
    machine_id: str
    anomaly: Anomaly
    state: EventState
    goal: str | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    plan: CommanderPlan | None = None
    steps: list[StepRun] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)       # ApprovalRequest ids
    safety_actions: list[SafetyAction] = Field(default_factory=list)
    timeline: list[TimelineItem] = Field(default_factory=list)
    alert_card: AlertCard | None = None
    consecutive_failures: int = 0
    replans: int = 0
    prior_rounds: list[StepRound] = Field(default_factory=list)   # earlier rounds' steps, oldest first
    closed_at: datetime | None = None
    outcome: str | None = None
    # issue #10: who opened it (the detector, or a task the supervisor typed) and what the supervisor
    # asked the agent to honour while discussing its plan
    source: Literal["detector", "supervisor", "plant"] = "detector"   # plant = issue #17 plant event
    command: str | None = None
    constraints: list["PlanConstraint"] = Field(default_factory=list)
    discuss_replans: int = 0


# =====================================================================================
# API / WebSocket shapes the frontend consumes
# =====================================================================================
class MetricTile(_M):
    label: Literal["溫度", "壓力", "時間", "水流", "蒸汽"]
    value: str                        # "112°C" / "正常" / "14 分" / "偏低"
    status: TileStatus


class PendingAction(_M):
    approval_id: str
    option_id: str | None = None
    label: str                        # "放慢生料投入"


class ApprovedAction(_M):
    """An R2/R3 write the supervisor approved: ③ shows it as ✓ 已核准 (✓ 已自動 is R1 only)."""
    approval_id: str
    option_id: str | None = None
    label: str                        # "放慢生料投入" / "夜班加班"
    decided_by: str = "supervisor"
    status: ApprovalStatus = ApprovalStatus.APPROVED


class FocusPanel(_M):
    """③ 中央說明面板."""
    event_id: str
    machine_id: str
    title: str                        # "異常：A 線大開煉機　溫度過高"
    detail: str                       # "膠溫 112°C 且持續上升（未加硫上限 120°C；已加硫另設 110°C 防焦燒）；原因：冷卻能力不足"
    tiles: list[MetricTile]
    auto_done: list[str]              # ✓ 已自動
    pending: list[PendingAction]      # ☐ 待核准 [核准]
    schedule_impact: str              # "無，大胎 #07 仍 17:00 入硫化機"
    state: EventState
    timeline: list[TimelineItem] = Field(default_factory=list)
    approved: list[ApprovedAction] = Field(default_factory=list)  # ✓ 已核准
    revoked: list[str] = Field(default_factory=list)  # ↺ 已撤回: "從 2 線調 1 人（因主管改選夜班加班）" (a consequence, never an approval)


class OrderProgress(_M):
    size: TireSize
    label: str                        # 小胎 / 中胎 / 大胎
    done: int
    total: int
    remaining_h: float
    note: str | None = None           # why remaining_h moved, e.g. "7 號機停用：可用硫化機 3/4 台…"


class MachineTile(_M):
    machine_id: str
    line: Line
    short_name: str
    status: MachineStatus
    temp_c: float | None = None
    # issue #36: how far the current batch / step is (0-1): a press's cure (elapsed / required), any other
    # machine's batch (MachineState.step_elapsed_min / its cycle). None = no batch (an empty press).
    step_progress: float | None = None


class BadDataMark(_M):
    """Readings the validation layer rejected for one machine in the recent window (page 10:
    bad data is counted and shown, and never wakes the AI)."""
    machine_id: str
    count: int                        # rejected readings in the window
    last_quality: DataQuality
    last_at: datetime


class HeaderStatus(_M):
    normal_count: int                 # 正常 27 台
    abnormal_count: int               # 異常 1 台
    factory_time: datetime
    llm_mode: LLMMode
    network: Literal["online", "weak", "wan_down", "lan_down"] = "online"   # page 9: weak = 弱網 (pushes delayed, agent unaffected)
    warning_light: bool = False       # page 9: LAN down -> on-site touch screen + warning light (on while something needs a person)
    bad_data_total: int = 0           # rejected readings in the last BAD_DATA_WINDOW_MIN sim-minutes, all machines
    bad_data: list[BadDataMark] = Field(default_factory=list)   # per machine, same window


class DashboardSnapshot(_M):
    header: HeaderStatus
    orders: list[OrderProgress]
    semi_finished: SemiFinishedProgress
    focus: FocusPanel | None
    machines: list[MachineTile]       # all 28, factory.MACHINES order
    events: list[Event]               # open + last few closed
    pending_approvals: list[ApprovalRequest]
    safety_actions: list[SafetyAction]   # recent


class MachineDetail(_M):
    state: MachineState
    tiles: list[MetricTile]
    history: list[SensorReading]      # last N minutes, downsampled
    prediction: Prediction | None = None


class WsType(StrEnum):
    SNAPSHOT = "snapshot"               # data: DashboardSnapshot (on connect and every ~5 s)
    MACHINES = "machines"               # data: {"machines": MachineTile[]} every 1 s
    ANOMALY = "anomaly"                 # data: Anomaly
    SAFETY_ACTION = "safety_action"     # data: SafetyAction
    EVENT = "event"                     # data: Event (on every state change)
    ALERT_CARD = "alert_card"           # data: {"event_id": str, "card": AlertCard, "source": "detector"|"commander"}
    APPROVAL_REQUEST = "approval_request"   # data: ApprovalRequest
    APPROVAL_RESOLVED = "approval_resolved" # data: ApprovalRequest (with decision)
    SCHEDULE = "schedule"               # data: ScheduleVersion
    AUDIT = "audit"                     # data: AuditEntry
    CLOCK = "clock"                     # data: {"factory_time": iso, "speed": float, "paused": bool}
    WORLD = "world"                     # data: {"full": bool, "rev": int, "factory_time": iso, <changed WorldSnapshot sections>}
    SUBAGENTS = "subagents"             # issue #54: {"items": subagent rows, "queue": model queue} (warroom/agent/orchestrator.py)


class WsMessage(_M):
    type: WsType
    ts: datetime
    data: dict[str, Any]


class SimControl(_M):
    action: Literal["play", "pause", "speed", "reset", "scenario", "jump"]
    speed: float | None = None          # sim seconds per wall second
    scenario: ScenarioName | None = None
    preroll: bool = False               # issue #36 scenario: run headless to ~15 s before the first scripted step
    at: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")   # issue #36 jump (一日循環): "HH:MM"


class HealthStatus(_M):
    ok: bool
    llm_mode: LLMMode
    model: str
    llm_reachable: bool
    qdrant_ok: bool
    strict: bool = False              # --strict: model failures are surfaced, no rule-based fallback
    sim_running: bool
    factory_time: datetime
    uptime_s: float
    agent_runtime: str | None = None   # WARROOM_AGENT_RUNTIME, e.g. 「NemoClaw 沙箱」 when the app runs inside OpenShell


# REST surface (warroom/api implements; web/src/contracts.ts mirrors). method, path, request, response
REST_ENDPOINTS: tuple[tuple[str, str, str | None, str], ...] = (
    ("GET", "/api/health", None, "HealthStatus"),
    ("GET", "/api/snapshot", None, "DashboardSnapshot"),
    ("GET", "/api/machines", None, "MachineState[]"),
    ("GET", "/api/machines/{machine_id}", None, "MachineDetail"),
    ("GET", "/api/events", None, "Event[]"),
    ("GET", "/api/events/{event_id}", None, "Event"),
    ("GET", "/api/approvals", None, "ApprovalRequest[]"),         # ?status=pending
    ("POST", "/api/approvals/{request_id}/decision", "ApprovalDecision", "ApprovalRequest"),
    ("GET", "/api/schedule", None, "ScheduleVersion"),            # current ACTIVE
    ("GET", "/api/schedule/versions", None, "ScheduleVersion[]"),
    ("POST", "/api/schedule/rollback", "{version?: int}", "ScheduleVersion"),  # one-click rollback
    ("GET", "/api/capacity", None, "CapacityReport"),
    ("GET", "/api/orders", None, "Order[]"),
    ("GET", "/api/shifts", None, "Shift[]"),
    ("GET", "/api/audit", None, "AuditEntry[]"),                  # ?since_seq=N&limit=200
    ("GET", "/api/sop/search", None, "SopHit[]"),                 # ?q=...&k=3
    ("POST", "/api/sim/control", "SimControl", "{ok: bool}"),
    ("GET", "/api/sim/events", None, "SimEventsView"),                  # issue #36 模擬事件
    ("POST", "/api/sim/events", "SimEventsBody", "SimEventsView"),
    ("POST", "/api/sim/events/fire", "{kind}", "{ok: bool, fired}"),
    ("POST", "/api/ask", "AskRequest", "AskAnswer"),               # 問戰情室: read-only Q&A
    ("GET", "/api/world", None, "WorldSnapshot"),                 # the whole plant: orders, materials, dock, people, energy, quality, maintenance
    ("POST", "/api/command", "CommandRequest", "CommandReply"),     # issue #10: 下新任務 (opens an event)
    ("POST", "/api/events/{event_id}/discuss", "DiscussRequest", "DiscussReply"),  # 討論這個計劃
    ("WS", "/ws", None, "WsMessage stream"),                      # dashboard + phone share one stream
)


class AskRequest(_M):
    question: str = Field(min_length=1, max_length=300)


class AskStep(_M):
    """One step the model asks for. `tool` is a plain string on purpose: a name off the allow-list
    must reach the code so it can be refused and logged INTERCEPTED, not fail JSON parsing."""
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class AskPlan(_M):
    """What the model returns for a question: up to ASK_MAX_STEPS read steps. It never answers here;
    the answer is written only from the tool results."""
    steps: list[AskStep] = Field(default_factory=list)
    rationale: str | None = None


class AskStepResult(_M):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    error: str | None = None
    summary: str = ""                 # one line the answer is built from


class AskAnswer(_M):
    id: str                           # "ASK-0001"; the audit entries carry it as event_id
    question: str
    answer: str
    steps: list[AskStepResult] = Field(default_factory=list)
    intercepted: list[dict[str, Any]] = Field(default_factory=list)   # refused tool requests
    mode: LLMMode
    fell_back_to_mock: bool = False
    factory_time: datetime
    total_ms: float = 0.0
    # chitchat: a greeting / thanks, answered with no tool call; unclear: not sure what to look up (no
    # machine is guessed); model_cold: the model did not answer in time and the rules could not tell
    intent: Literal["query", "chitchat", "unclear", "model_cold"] = "query"


# =====================================================================================
# Issue #10: supervisor commands and plan discussion
# =====================================================================================
class PlanConstraint(_M):
    """A change the supervisor asked for while a plan waits, in a form CODE enforces (the model may
    propose one; code validates it, applies it to every solve and checks the result honours it)."""
    kind: Literal["machine_unavailable", "no_overtime", "no_transfer", "prefer_option"]
    machine_id: str | None = None         # machine_unavailable
    window_label: str | None = None       # "今晚"
    start: datetime | None = None         # machine_unavailable window (factory time)
    end: datetime | None = None
    option: str | None = None             # prefer_option: "A" / "B" / "overtime" / "transfer"
    label: str                            # chip text: "9 號機 今晚不可用"
    text: str = ""                        # the supervisor's words it came from
    at: datetime | None = None


class CommandRequest(_M):
    text: str = Field(min_length=1, max_length=300)


class CommandReply(_M):
    event_id: str
    accepted: bool                        # False: nothing the tools can do; the event closed with `reply`
    reply: str


class DiscussRequest(_M):
    text: str = Field(min_length=1, max_length=300)


class DiscussReply(_M):
    id: str                               # "DIS-0001"
    event_id: str
    text: str
    intent: Literal["question", "change", "unsupported", "capped", "not_waiting", "chitchat", "unclear"]
    reply: str                            # plain Chinese, numbers only from tool results
    constraints: list[PlanConstraint] = Field(default_factory=list)   # added by this message
    replanned: bool = False
    superseded: str | None = None         # old card id
    approval: ApprovalRequest | None = None   # the new card (v2...)
    changes: list[str] = Field(default_factory=list)
    classified_by: Literal["model", "rules"] = "rules"
    fell_back: bool = False               # the model failed or its answer was not usable
    ask: AskAnswer | None = None          # a question: the read-only answer
    factory_time: datetime | None = None


# =====================================================================================
# Protocols — one per module owner
# =====================================================================================
@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class FactorySim(Protocol):
    """warroom/sim. Owns factory truth: machine physics, faults, orders, shifts, WIP."""
    clock: Clock

    def load_scenario(self, name: ScenarioName) -> Scenario: ...
    def inject_fault(self, fault: FaultSpec) -> None: ...
    def clear_faults(self, machine_id: str | None = None) -> None: ...
    def step(self, dt_s: float = 1.0) -> list[SensorReading]: ...      # advance clock, 1 reading per machine
    def machines(self) -> list[MachineState]: ...
    def machine(self, machine_id: str) -> MachineState: ...
    def history(self, machine_id: str, minutes: float) -> list[SensorReading]: ...
    def orders(self) -> list[Order]: ...
    def shifts(self) -> list[Shift]: ...
    def order_progress(self) -> list[OrderProgress]: ...
    def semi_finished(self) -> SemiFinishedProgress: ...
    def apply_safety_action(self, action: SafetyAction) -> None: ...   # red path: stop/hold in the plant
    def set_machine_status(self, machine_id: str, status: MachineStatus, note: str | None = None) -> None: ...
    def fault_started_at(self, machine_id: str) -> datetime | None: ...  # for detection latency


@runtime_checkable
class Detector(Protocol):
    """warroom/detect. Non-LLM, always on. Bad data never wakes the AI."""
    def validate(self, reading: SensorReading) -> DataQuality: ...
    def ingest(self, readings: list[SensorReading]) -> list[Anomaly]: ...   # returns NEW anomalies only (debounced)
    def predict(self, machine_id: str, channel: Channel = Channel.TEMP, horizon_s: float = 1800) -> Prediction: ...
    def reset(self, machine_id: str | None = None, *, repaired: bool = False) -> None: ...


@runtime_checkable
class SafetyCore(Protocol):
    """warroom/safety. Deterministic; must not import warroom.agent / warroom.tools / any LLM client."""
    rules: list[SafetyRule]

    def check(self, readings: list[SensorReading], states: dict[str, MachineState]) -> list[SafetyAction]: ...


@runtime_checkable
class SopKB(Protocol):
    """warroom/kb."""
    def ingest(self) -> int: ...                           # returns chunk count
    def search(self, query: str, k: int = 3) -> list[SopHit]: ...


@runtime_checkable
class Scheduler(Protocol):
    """warroom/schedule. CP-SAT first; on timeout/error fall back to heuristic (fell_back=True)."""
    def solve(self, req: SolveRequest) -> SolveResult: ...


@runtime_checkable
class ScheduleStore(Protocol):
    """warroom/schedule. Keeps every version; one-click rollback (page 7)."""
    def current(self) -> ScheduleVersion: ...
    def get(self, version: int) -> ScheduleVersion: ...
    def versions(self) -> list[ScheduleVersion]: ...
    def propose(self, schedule: Schedule, reason: str, event_id: str | None = None) -> ScheduleVersion: ...
    def activate(self, version: int) -> ScheduleVersion: ...
    def rollback(self, to_version: int | None = None) -> ScheduleVersion: ...   # None = previous ACTIVE


@runtime_checkable
class ToolRegistry(Protocol):
    """warroom/tools."""
    def specs(self, names: list[ToolName] | None = None) -> list[ToolSpec]: ...
    def openai_tools(self, names: list[ToolName]) -> list[dict[str, Any]]: ...  # OpenAI "tools" array
    def call(self, call: ToolCall) -> ToolResult: ...   # never raises; errors -> ok=False


@runtime_checkable
class CommanderClient(Protocol):
    """warroom/agent. The ONLY LLM. mode=MOCK returns deterministic plans (no model)."""
    mode: LLMMode

    async def plan(self, req: CommanderRequest) -> tuple[CommanderPlan, LLMCallInfo]: ...


@runtime_checkable
class ActionGate(Protocol):
    """warroom/agent. Deterministic: allow-list, 4-level risk, format check, simulation feasible."""
    def check(self, step: PlanStep, state: EventState, simulation_feasible: bool) -> GateDecision: ...


@runtime_checkable
class AuditLog(Protocol):
    """warroom/agent. Append-only."""
    def append(self, kind: AuditKind, actor: str, payload: dict[str, Any],
               event_id: str | None = None) -> AuditEntry: ...
    def tail(self, since_seq: int = 0, limit: int = 200) -> list[AuditEntry]: ...
    def verify_chain(self) -> bool: ...


@runtime_checkable
class ApprovalBroker(Protocol):
    """warroom/agent. Supervisor phone / touchscreen decisions flow back to the state machine."""
    def request(self, req: ApprovalRequest) -> ApprovalRequest: ...
    def decide(self, decision: ApprovalDecision) -> ApprovalRequest: ...
    def pending(self) -> list[ApprovalRequest]: ...
    def get(self, request_id: str) -> ApprovalRequest: ...
    async def wait(self, request_id: str, timeout_s: float | None = None) -> ApprovalRequest: ...


@runtime_checkable
class EventStateMachine(Protocol):
    """warroom/agent. One Event per wake-up; drives classify→…→close."""
    async def handle(self, anomaly: Anomaly) -> Event: ...
    def events(self) -> list[Event]: ...
    def get(self, event_id: str) -> Event: ...


@runtime_checkable
class Bus(Protocol):
    """warroom/bus.py (implemented). In-process pub/sub that the API relays to WebSockets."""
    def publish(self, type: WsType, data: BaseModel | dict[str, Any]) -> None: ...
    def subscribe(self) -> AsyncIterator[WsMessage]: ...


QueryOrdersShiftsData.model_rebuild()
ApprovalRequest.model_rebuild()
Event.model_rebuild()

__all__ = [n for n in dir() if not n.startswith("_")]
