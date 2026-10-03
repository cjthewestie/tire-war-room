from __future__ import annotations

from warroom.contracts import ApprovalDecision, ApprovalRequest


class ApprovalBrokerImpl:
    """Implements contracts.ApprovalBroker. request() publishes WsType.APPROVAL_REQUEST and audits;
    decide() resolves, audits (with reason) and publishes APPROVAL_RESOLVED; wait() is awaited by
    the state machine."""

    def __init__(self, bus=None, audit=None, clock=None):
        raise NotImplementedError

    def request(self, req: ApprovalRequest) -> ApprovalRequest: raise NotImplementedError
    def decide(self, decision: ApprovalDecision) -> ApprovalRequest: raise NotImplementedError
    def pending(self) -> list[ApprovalRequest]: raise NotImplementedError
    def get(self, request_id: str) -> ApprovalRequest: raise NotImplementedError
    async def wait(self, request_id: str, timeout_s: float | None = None) -> ApprovalRequest:
        raise NotImplementedError
