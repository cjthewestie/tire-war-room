"""Static factory model: the single source of machine ids, lines, processes and the
numbers the proposal fixes. Every module imports ids and constants from here; never
hard-code a machine id string elsewhere.

Page refs are to docs/proposal-final.pdf.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from warroom.contracts import Line, MachineKind, TeamId, TireSize

TZ = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class MachineDef:
    id: str
    line: Line
    name: str            # full Chinese machine name (page 3)
    short_name: str      # dashboard chip label (page 2, panel 4)
    process: str         # the process this machine performs (page 3)
    kind: MachineKind
    order: int           # display order inside its line
    handles_compound: bool = False          # rubber compound passes through -> compound safety rules apply
    small_only: bool = False                # curing presses only
    steam_header: str | None = None         # curing presses only: presses on one header share a steam pipe
    channels: tuple[str, ...] = ("temp_c", "pressure_kgf_cm2", "water_flow_lpm")


def _m(*a, **k) -> MachineDef:
    return MachineDef(*a, **k)


# ---- 28 machines (page 2 panel 4 lists exactly these: 7+4+4+1+1+11) ----
MACHINES: tuple[MachineDef, ...] = (
    # Line A 胎體/胎面膠
    _m("A-MIX", Line.A, "密煉機", "密煉", "混煉", MachineKind.MIXER, 1, handles_compound=True),
    _m("A-OPEN", Line.A, "開煉機", "開煉", "壓片", MachineKind.OPEN_MILL, 2, handles_compound=True),
    _m("A-BIGOPEN", Line.A, "大開煉機", "大開煉", "預熱", MachineKind.OPEN_MILL, 3, handles_compound=True),
    _m("A-SMALLOPEN", Line.A, "小開煉機", "小開煉", "保溫儲料", MachineKind.OPEN_MILL, 4, handles_compound=True),
    _m("A-CAL", Line.A, "壓延機", "壓延", "押出", MachineKind.CALENDER, 5, handles_compound=True),
    _m("A-COOL", Line.A, "冷卻機", "冷卻", "冷卻", MachineKind.COOLER, 6),
    _m("A-WIND", Line.A, "捲取設備", "捲取", "捲取", MachineKind.WINDER, 7),
    # Line B 胎側膠
    _m("B-MIX", Line.B, "密煉機", "密煉", "混煉", MachineKind.MIXER, 1, handles_compound=True),
    _m("B-OPEN", Line.B, "開煉機", "開煉", "壓片", MachineKind.OPEN_MILL, 2, handles_compound=True),
    _m("B-FORM", Line.B, "油壓機", "成形", "成形", MachineKind.HYDRAULIC_PRESS, 3, handles_compound=True),
    _m("B-ASSY", Line.B, "油壓機", "組裝", "組裝", MachineKind.HYDRAULIC_PRESS, 4),
    # Line C 鋼絲圈
    _m("C-PAYOFF", Line.C, "放線架", "放線", "拉線", MachineKind.PAYOFF, 1),
    _m("C-EXTR", Line.C, "押出機", "押出", "押出包膠", MachineKind.EXTRUDER, 2, handles_compound=True),
    _m("C-WIND", Line.C, "纏繞機", "纏繞", "拉圈", MachineKind.WINDER, 3),
    _m("C-WRAP", Line.C, "氣囊", "包布", "包布", MachineKind.BLADDER, 4),
    # Line D 尼龍布
    _m("D-CUT", Line.D, "裁切機", "裁切", "裁切", MachineKind.CUTTER, 1),
    # Bonding 貼合
    _m("BOND-01", Line.BOND, "油壓機", "油壓", "貼合", MachineKind.HYDRAULIC_PRESS, 1),
) + tuple(
    # Curing 硫化, 11 presses. Decision (CJ, 2026-10-20, team dataset V1-V11): presses 08-11 are the
    # 4 shared medium+large presses, presses 01-07 are small-only (page 3 draws 7 small then 4 M/L).
    # Press 7 therefore cures a SMALL tire (EV-2: S-027, 24 h). Steam header H2 = presses 06,07,08
    # (page 11: "對照 6、8 號同管線").
    _m(
        f"CURE-{i:02d}", Line.CURE, "硫化機", str(i), "硫化", MachineKind.CURING_PRESS, i,
        small_only=i not in (8, 9, 10, 11),
        steam_header="H1" if i <= 5 else ("H2" if i <= 8 else "H3"),
        channels=("temp_c", "pressure_kgf_cm2", "steam_kgf_cm2"),
    )
    for i in range(1, 12)
)

MACHINE_IDS: tuple[str, ...] = tuple(m.id for m in MACHINES)
MACHINE_BY_ID: dict[str, MachineDef] = {m.id: m for m in MACHINES}
CURE_PRESS_IDS: tuple[str, ...] = tuple(m.id for m in MACHINES if m.line is Line.CURE)
SMALL_ONLY_PRESSES: tuple[str, ...] = tuple(m.id for m in MACHINES if m.line is Line.CURE and m.small_only)
ML_PRESSES: tuple[str, ...] = tuple(m.id for m in MACHINES if m.line is Line.CURE and not m.small_only)
COMPOUND_MACHINES: tuple[str, ...] = tuple(m.id for m in MACHINES if m.handles_compound)
LINE_LABELS: dict[Line, str] = {
    Line.A: "A 胎體／胎面膠", Line.B: "B 胎側膠", Line.C: "C 鋼絲圈", Line.D: "D 尼龍布",
    Line.BOND: "貼合", Line.CURE: "硫化",
}


def press_id(n: int) -> str:
    """press_id(7) -> 'CURE-07'."""
    return f"CURE-{n:02d}"


def presses_on_header(header: str) -> list[str]:
    return [m.id for m in MACHINES if m.steam_header == header]


# ---- 26 processes (page 3). 成品 is the output, not a process. ----
PROCESSES: tuple[tuple[Line, str, str | None], ...] = (
    (Line.A, "原料", "傳輸帶"), (Line.A, "混煉", "A-MIX"), (Line.A, "QC 取樣試驗", None),
    (Line.A, "壓片", "A-OPEN"), (Line.A, "熟成靜置", None), (Line.A, "預熱", "A-BIGOPEN"),
    (Line.A, "保溫儲料", "A-SMALLOPEN"), (Line.A, "押出", "A-CAL"), (Line.A, "冷卻", "A-COOL"),
    (Line.A, "捲取", "A-WIND"), (Line.A, "入模", "模具"),
    (Line.B, "原料", "傳輸帶"), (Line.B, "混煉", "B-MIX"), (Line.B, "壓片", "B-OPEN"),
    (Line.B, "成形", "B-FORM"), (Line.B, "冷卻靜置", None), (Line.B, "組裝", "B-ASSY"),
    (Line.C, "拉線", "C-PAYOFF"), (Line.C, "押出包膠", "C-EXTR"), (Line.C, "拉圈", "C-WIND"),
    (Line.C, "包布", "C-WRAP"),
    (Line.D, "裁切", "D-CUT"),
    (Line.BOND, "貼合", "BOND-01"), (Line.CURE, "硫化", "CURE-*"),
    (Line.CURE, "冷卻靜置", None), (Line.CURE, "整修（人工）", None),
)
ASSEMBLY_ORDER = "D 包住 C → 配上 B → 與 A 在油壓機貼合 → 硫化 (page 3)"

# ---- Curing physics (pages 3, 11) ----
CURE_HOURS: dict[TireSize, float] = {TireSize.S: 24.0, TireSize.M: 28.0, TireSize.L: 34.0}
CURE_TEMP_MIN_C = 120.0
CURE_TEMP_MAX_C = 130.0
CURE_TEMP_NOMINAL_C = 128.0          # page 11: 中胎 128°C 硫化 28 小時
CURE_PRESSURE_KGF_CM2 = 20.0         # hydraulic gauge, page 3
ACTIVATION_ENERGY_KJ_MOL = 85.0      # page 11, assumed value (needs factory calibration)
GAS_CONSTANT_J_MOL_K = 8.314
DEMOLD_MOLDIN_MIN = 30               # page 6 sim condition
MOLD_CHANGE_STOP_H = 8.0             # page 6 principle 4

# ---- Safety core thresholds (page 5, page 2) ----
COMPOUND_STOP_C = 120.0              # 煉膠 (unvulcanised) ≥120°C stop
CURATIVE_STOP_C = 110.0              # 已加硫 (curative added) ≥110°C stop, scorch guard
CURE_LOW_STOP_C = CURE_TEMP_MIN_C    # cure press below 120°C -> safety core acts (page 11 02:14:01)

# ---- Back-propagation from mold-in / de-mold time T (page 6 table) ----
# offsets are minutes BEFORE T
BACKPROP: dict[TireSize, dict[str, int]] = {
    TireSize.S: {"bond_start_min": 40, "a_feed_min": 100, "side_pieces": 6, "bead_pieces": 6},
    TireSize.M: {"bond_start_min": 60, "a_feed_min": 150, "side_pieces": 8, "bead_pieces": 8},
    TireSize.L: {"bond_start_min": 90, "a_feed_min": 210, "side_pieces": 10, "bead_pieces": 10},
}


@dataclass(frozen=True)
class ShiftDef:
    team: TeamId
    name: str
    start: time
    end: time            # end < start means it crosses midnight
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5)   # Mon..Sat (page 6 sim: Mon–Sat)
    headcount: int = 2


# Current state (page 4 Gantt): most teams day shift only; 胎體/胎面膠組 also has night.
CURRENT_SHIFTS: tuple[ShiftDef, ...] = (
    ShiftDef(TeamId.CURING, "早班", time(8), time(17)),
    ShiftDef(TeamId.BODY, "早班", time(8), time(17)),
    ShiftDef(TeamId.BODY, "晚班", time(20), time(5)),
    ShiftDef(TeamId.SIDE, "早班", time(8), time(17)),
    ShiftDef(TeamId.BEAD_NYLON, "早班", time(8), time(17)),
    ShiftDef(TeamId.BIGOPEN, "早班", time(8), time(17)),
    ShiftDef(TeamId.BOND, "早班", time(8), time(17)),
)
# Optimised premise (page 6, assumed feasible): curing team staggered 6–15 and 14–23;
# 胎體/胎面膠組 night 20:00–05:00 also does de-mold / mold-in.
OPTIMISED_SHIFTS: tuple[ShiftDef, ...] = (
    ShiftDef(TeamId.CURING, "早班", time(6), time(15)),
    ShiftDef(TeamId.CURING, "中班", time(14), time(23)),
    ShiftDef(TeamId.BODY, "早班", time(8), time(17)),
    ShiftDef(TeamId.BODY, "夜班", time(20), time(5)),
    ShiftDef(TeamId.SIDE, "早班", time(8), time(17)),
    ShiftDef(TeamId.BEAD_NYLON, "早班", time(8), time(17)),
    ShiftDef(TeamId.BIGOPEN, "早班", time(8), time(17)),
    ShiftDef(TeamId.BOND, "早班", time(8), time(17)),
)
TEAM_LABELS: dict[TeamId, str] = {
    TeamId.CURING: "硫化機組", TeamId.BODY: "胎體／胎面膠組", TeamId.SIDE: "胎側膠組",
    TeamId.BEAD_NYLON: "鋼絲／尼龍組", TeamId.BIGOPEN: "大開煉機組", TeamId.BOND: "貼合組",
    TeamId.MAINTENANCE: "維修組", TeamId.QC: "品管", TeamId.SUPERVISOR: "主管",
}


@dataclass(frozen=True)
class CapacityRow:
    label: str
    small: float
    medium: float
    large: float


# Page 6 "每台硫化機每週可做幾顆（模擬）" — schedule module must reproduce these.
CAPACITY_TARGETS: tuple[CapacityRow, ...] = (
    CapacityRow("現況", 6, 5, 3),
    CapacityRow("只延長班表", 6, 5.9, 4.5),
    CapacityRow("AI 排出模時刻", 7, 6, 4.9),
)
LEAD_TIME_TARGET_WEEKS = {"ML_100_each": (13.3, 9.3), "small": (2.4, 2.0)}   # page 6
CAPACITY_SIM_CONDITIONS = "Mon–Sat; Sat night shift runs to Sun 05:00; M,L 2 presses each; no mold change; de-mold/mold-in 30 min (page 6)"

# ---- Dashboard demo numbers (page 2) ----
DEMO_ORDER_PROGRESS = {TireSize.S: (38, 100, 208), TireSize.M: (12, 100, 1241), TireSize.L: (8, 100, 2162)}

# ---- Edge / latency budgets (pages 9, 10) ----
LATENCY_SAFETY_S = 0.2
LATENCY_FIRST_ALERT_S = 2.0
ALERT_CARD_MAX_TOKENS = 100
LATENCY_FULL_EVENT_S = 22.0
TTFT_TARGET_S = 0.5
CONCURRENT_ANOMALIES = 3
CONCURRENT_EACH_S = 4.0
MEMORY_BUDGET_GB = {"model_nvfp4": 19, "draft": 2, "kv_state": 4, "vllm_cuda": 5, "dgx_os": 10, "qdrant": 3, "sched_app": 6}
DETECTION_TARGET_S = 60               # page 1: detect in 1 minute (was 2 h patrol)
