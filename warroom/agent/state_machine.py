from __future__ import annotations

from warroom.contracts import (ActionGate, Anomaly, ApprovalBroker, AuditLog, Bus, Clock, CommanderClient,
                               Event, FactorySim, ToolRegistry)


class StateMachine:
    """Implements contracts.EventStateMachine (page 5/7/11). Transitions only along
    contracts.ALLOWED_TRANSITIONS; every transition audited and published (WsType.EVENT).
    VERIFY compares Hypothesis.expectation against sim readings in code (no LLM). Tool failure
    -> retry MAX_TOOL_RETRIES then SOP path; solver timeout -> backup (inside FallbackScheduler);
    MAX_CONSECUTIVE_FAILURES -> SAFE_STOP keeping the previous schedule."""

    def __init__(self, commander: CommanderClient, tools: ToolRegistry, gate: ActionGate,
                 approvals: ApprovalBroker, audit: AuditLog, sim: FactorySim | None = None,
                 bus: Bus | None = None, clock: Clock | None = None):
        raise NotImplementedError

    async def handle(self, anomaly: Anomaly) -> Event: raise NotImplementedError
    def events(self) -> list[Event]: raise NotImplementedError
    def get(self, event_id: str) -> Event: raise NotImplementedError
