"""Action Gate (page 5 動作把關, page 7 threshold 4). Deterministic, no LLM.

Checks, in order: the tool is on the allow-list, the tool is open in the state it will run in
(TOOLS_BY_STATE), the args validate against the tool's *Args model with known machine ids and no
unresolved "$S.." references, and for writes the simulation was feasible. Then a risk level:

  R0 read/compute (auto)                R1 notify maintenance/info (auto, 已自動)
  R2 set_cure_time, confirm_schedule,   R3 notify slow_feed/pause_feed/staff_move/machine_out, or
     notify overtime (approval)            a schedule containing a cross-team transfer (approval)

Anything not on the allow-list or not open in that state is REJECT with risk None; the state
machine writes it to the audit log as INTERCEPTED. There are no actuator / PLC tools to allow.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from warroom.contracts import (
    APPROVAL_REQUIRED,
    READ_COMPUTE_TOOLS,
    TOOLS_BY_STATE,
    WRITE_TOOLS,
    ConfirmScheduleArgs,
    CureFormulaArgs,
    EventState,
    GateDecision,
    GateVerdict,
    NotifyKind,
    NotifyTeamsArgs,
    PlanStep,
    PredictArgs,
    QueryOrdersShiftsArgs,
    ReadSensorsArgs,
    RiskLevel,
    SetCureTimeArgs,
    SolveScheduleArgs,
    SopSearchArgs,
    ToolName,
)
from warroom.factory import CURE_PRESS_IDS, MACHINE_IDS

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

NOTIFY_RISK: dict[NotifyKind, RiskLevel] = {
    NotifyKind.MAINTENANCE: RiskLevel.R1_NOTIFY,
    NotifyKind.INFO: RiskLevel.R1_NOTIFY,
    NotifyKind.OVERTIME: RiskLevel.R2_CHANGE,
    NotifyKind.SLOW_FEED: RiskLevel.R3_CRITICAL,
    NotifyKind.PAUSE_FEED: RiskLevel.R3_CRITICAL,
    NotifyKind.STAFF_MOVE: RiskLevel.R3_CRITICAL,
    NotifyKind.MACHINE_OUT: RiskLevel.R3_CRITICAL,
}


def has_unresolved_ref(v: Any) -> bool:
    if isinstance(v, str):
        return v.startswith("$S") or "${S" in v
    if isinstance(v, dict):
        return any(has_unresolved_ref(x) for x in v.values()) or any(has_unresolved_ref(k) for k in v)
    if isinstance(v, (list, tuple)):
        return any(has_unresolved_ref(x) for x in v)
    return False


def _bad_ids(tool: ToolName, a: BaseModel) -> list[str]:
    bad: list[str] = []
    if isinstance(a, ReadSensorsArgs):
        bad += [m for m in a.machine_ids if m not in MACHINE_IDS]
    elif isinstance(a, PredictArgs):
        bad += [a.machine_id] if a.machine_id not in MACHINE_IDS else []
    elif isinstance(a, SetCureTimeArgs):
        bad += [a.press_id] if a.press_id not in CURE_PRESS_IDS else []
    elif isinstance(a, NotifyTeamsArgs):
        bad += [a.machine_id] if a.machine_id and a.machine_id not in MACHINE_IDS else []
    elif isinstance(a, SolveScheduleArgs):
        bad += [p for p in [*a.unavailable_presses, *a.cure_overrides_h] if p not in CURE_PRESS_IDS]
    elif isinstance(a, QueryOrdersShiftsArgs):
        bad += [p for p in (a.press_ids or []) if p not in CURE_PRESS_IDS]
    return bad


class RuleGate:
    """Implements contracts.ActionGate.

    store: optional ScheduleStore; confirm_schedule is R3 when that version's schedule has a
    cross-team transfer. The state machine can also pass cross_team= directly (it knows the solver
    result the step refers to)."""

    def __init__(self, store=None):
        self._store = store

    def _schedule_has_transfer(self, version: int) -> bool:
        if self._store is None:
            return False
        try:
            sv = self._store.get(version)
        except Exception:  # noqa: BLE001
            return False
        return any(c.kind == "transfer" for c in sv.schedule.staffing_changes)

    def risk_of(self, tool: ToolName, args: BaseModel | None, cross_team: bool | None = None) -> RiskLevel:
        if tool in READ_COMPUTE_TOOLS:
            return RiskLevel.R0_READ
        if tool is ToolName.NOTIFY_TEAMS:
            kind = args.kind if isinstance(args, NotifyTeamsArgs) else NotifyKind.INFO
            return NOTIFY_RISK[NotifyKind(kind)]
        if tool is ToolName.CONFIRM_SCHEDULE:
            if cross_team is None and isinstance(args, ConfirmScheduleArgs):
                cross_team = self._schedule_has_transfer(args.schedule_version)
            return RiskLevel.R3_CRITICAL if cross_team else RiskLevel.R2_CHANGE
        return RiskLevel.R2_CHANGE   # set_cure_time

    def check(self, step: PlanStep, state: EventState, simulation_feasible: bool,
              *, cross_team: bool | None = None) -> GateDecision:
        checks = {"allow_list": False, "state_allowed": False, "format_ok": False, "simulation_feasible": False}
        reasons: list[str] = []
        try:
            tool = ToolName(step.tool)
        except ValueError:
            reasons.append(f"tool {step.tool!r} is not on the allow-list (intercepted)")
            return GateDecision(step_id=step.id, tool=ToolName.READ_SENSORS, verdict=GateVerdict.REJECT,
                                risk=None, checks=checks, reasons=reasons)
        checks["allow_list"] = tool in READ_COMPUTE_TOOLS or tool in WRITE_TOOLS
        if not checks["allow_list"]:
            reasons.append(f"{tool.value} not on the allow-list")
        checks["state_allowed"] = tool in TOOLS_BY_STATE.get(state, frozenset())
        if not checks["state_allowed"]:
            reasons.append(f"{tool.value} is not open in state {state.value}")

        parsed: BaseModel | None = None
        if has_unresolved_ref(step.args):
            reasons.append("args contain an unresolved $S reference")
        else:
            try:
                parsed = ARGS_MODELS[tool].model_validate(step.args)
            except ValidationError as e:
                reasons.append(f"args invalid: {e.errors()[0].get('msg', 'validation error')}"
                               f" at {'.'.join(map(str, e.errors()[0].get('loc', ())))}")
            else:
                bad = _bad_ids(tool, parsed)
                if bad:
                    reasons.append(f"unknown machine id(s): {', '.join(bad)}")
                else:
                    checks["format_ok"] = True

        is_write = tool in WRITE_TOOLS
        checks["simulation_feasible"] = simulation_feasible or not is_write
        if not checks["simulation_feasible"]:
            reasons.append("simulation not feasible: writes are blocked")

        out_of_scope = not (checks["allow_list"] and checks["state_allowed"])
        risk = None if out_of_scope else self.risk_of(tool, parsed, cross_team)
        if not all(checks.values()):
            verdict = GateVerdict.REJECT
        elif risk in APPROVAL_REQUIRED:
            verdict = GateVerdict.NEEDS_APPROVAL
            reasons.append(f"{risk.value}: supervisor approval required")
        else:
            verdict = GateVerdict.ALLOW
        return GateDecision(step_id=step.id, tool=tool, verdict=verdict, risk=risk, checks=checks,
                            reasons=reasons)
