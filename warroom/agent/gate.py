from __future__ import annotations

from warroom.contracts import EventState, GateDecision, PlanStep


class RuleGate:
    """Implements contracts.ActionGate. Checks: tool in allow-list (ToolName), tool allowed in
    state (TOOLS_BY_STATE), args validate against the *Args model, and for writes
    simulation_feasible. Risk: read/compute R0; notify MAINTENANCE/INFO R1; set_cure_time,
    confirm_schedule, notify OVERTIME R2; notify SLOW_FEED/PAUSE_FEED/STAFF_MOVE/MACHINE_OUT and
    any schedule with a cross-team StaffingChange R3. Unknown tool -> REJECT (logged INTERCEPTED)."""

    def check(self, step: PlanStep, state: EventState, simulation_feasible: bool) -> GateDecision:
        raise NotImplementedError
