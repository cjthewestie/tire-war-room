"""ToolSpec for all 9 allow-listed tools (descriptions in Chinese+English for the LLM,
params_schema = the *Args model's JSON schema from contracts).

The allow-list is exactly contracts.ToolName (page 5: six read/compute tools, three write tools).
There are no actuator / PLC write tools and none may be added: the write tools only record intent
(a schedule version, a cure-time suggestion, a notification)."""
from __future__ import annotations

from pydantic import BaseModel

from warroom.contracts import (
    TOOLS_BY_STATE,
    ConfirmScheduleArgs,
    CureFormulaArgs,
    EventState,
    NotifyTeamsArgs,
    PredictArgs,
    QueryOrdersShiftsArgs,
    ReadSensorsArgs,
    RiskLevel,
    SetCureTimeArgs,
    SolveScheduleArgs,
    SopSearchArgs,
    ToolKind,
    ToolName,
    ToolSpec,
)

ARGS_MODELS: dict[ToolName, type[BaseModel]] = {
    ToolName.READ_SENSORS: ReadSensorsArgs,
    ToolName.PREDICT_ANOMALY: PredictArgs,
    ToolName.CURE_FORMULA: CureFormulaArgs,
    ToolName.SOLVE_SCHEDULE: SolveScheduleArgs,
    ToolName.SOP_SEARCH: SopSearchArgs,
    ToolName.QUERY_ORDERS_SHIFTS: QueryOrdersShiftsArgs,
    ToolName.CONFIRM_SCHEDULE: ConfirmScheduleArgs,
    ToolName.SET_CURE_TIME: SetCureTimeArgs,
    ToolName.NOTIFY_TEAMS: NotifyTeamsArgs,
}

# tool -> (kind, base risk, timeout s, description)
_DESC: dict[ToolName, tuple[ToolKind, RiskLevel, float, str]] = {
    ToolName.READ_SENSORS: (
        ToolKind.READ, RiskLevel.R0_READ, 5.0,
        ("讀感測器：讀取機台近 N 分鐘的溫度／壓力／水流／蒸汽讀值與摘要（現值、最低、趨勢）。"
        " Read recent sensor history and a per-machine summary.")),
    ToolName.PREDICT_ANOMALY: (
        ToolKind.COMPUTE, RiskLevel.R0_READ, 5.0,
        ("異常預測：時序模型預測某機台某通道在未來 horizon 分鐘的穩定值與區間。"
        " Time-series forecast of one channel (value and band).")),
    ToolName.CURE_FORMULA: (
        ToolKind.COMPUTE, RiskLevel.R0_READ, 5.0,
        ("硫化公式（Arrhenius 等效硫化）：依實際溫度與已硫化時間算出所需總硫化時間。硫化時間只能來自此公式。"
        " Arrhenius equivalent cure: total cure hours at the actual temperature.")),
    ToolName.SOLVE_SCHEDULE: (
        ToolKind.COMPUTE, RiskLevel.R0_READ, 30.0,
        ("排程求解（OR-Tools CP-SAT，逾時自動切備援）：以硫化機為中心重排，回傳移動、人力選項、缺口與提案版本號 schedule_version。"
        " Re-solve the cure-press schedule; returns moves, staffing options and the proposed schedule_version.")),
    ToolName.SOP_SEARCH: (
        ToolKind.READ, RiskLevel.R0_READ, 10.0,
        ("SOP 知識庫（Qdrant）：以中文查詢相關標準作業程序，回傳 SOP 編號、標題與段落。"
        " Search the SOP knowledge base.")),
    ToolName.QUERY_ORDERS_SHIFTS: (
        ToolKind.READ, RiskLevel.R0_READ, 5.0,
        ("訂單／班表資料庫：查訂單交期、班表、人力缺口與指定硫化機的下一模。"
        " Orders, shifts, staffing gaps and the next jobs on the given presses.")),
    ToolName.CONFIRM_SCHEDULE: (
        ToolKind.WRITE, RiskLevel.R2_CHANGE, 5.0,
        ("確認新排程（寫入，需主管核准）：啟用 solve_schedule 提出的版本；上一版保留可一鍵回復。"
        " Activate a PROPOSED schedule version (approval required).")),
    ToolName.SET_CURE_TIME: (
        ToolKind.WRITE, RiskLevel.R2_CHANGE, 5.0,
        ("設定硫化時間建議值（寫入，需主管核准）：數值必須來自 cure_formula，不可自行估計。"
        " Record a suggested total cure time for a press (approval required).")),
    ToolName.NOTIFY_TEAMS: (
        ToolKind.WRITE, RiskLevel.R1_NOTIFY, 5.0,
        ("通知各組（寫入）：maintenance/info 為自動通知；slow_feed、pause_feed、staff_move、overtime、machine_out 需主管核准。"
        " Notify teams; only maintenance/info are automatic.")),
}


def _states(tool: ToolName) -> list[EventState]:
    return [s for s, tools in TOOLS_BY_STATE.items() if tool in tools]


TOOL_SPECS: dict[ToolName, ToolSpec] = {
    name: ToolSpec(name=name, kind=kind, description=desc, params_schema=ARGS_MODELS[name].model_json_schema(),
                   base_risk=risk, allowed_states=_states(name), timeout_s=timeout)
    for name, (kind, risk, timeout, desc) in _DESC.items()
}

assert set(TOOL_SPECS) == set(ToolName), "every allow-listed tool needs a spec"
