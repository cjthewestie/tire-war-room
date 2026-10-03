"""Approval broker (page 5 主管手機, page 11 第二輪核准).

The state machine calls request() and awaits wait(); the supervisor's phone / touchscreen /
dashboard posts an ApprovalDecision to the API, which calls decide(). The decision (approve /
modify / reject, option, reason) is audited and published, then flows back to the waiting event.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from warroom.contracts import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    AuditKind,
    DecisionKind,
    WsType,
)
from warroom.factory import TZ

_STATUS_FOR = {DecisionKind.APPROVE: ApprovalStatus.APPROVED, DecisionKind.MODIFY: ApprovalStatus.MODIFIED,
               DecisionKind.REJECT: ApprovalStatus.REJECTED}


class ApprovalError(ValueError):
    """Bad decision: unknown request, already resolved, unknown option, or an edit of a staffing option."""


STAFFING_NOTIFY_KINDS = ("overtime", "staff_move")


def staffing_modification(opt, modified_args) -> list[str]:
    """Keys a MODIFY would change on a staffing option (overtime / staff move). Empty when the option
    is not a staffing one or modified_args repeats its values."""
    if opt is None or not modified_args or opt.args.get("kind") not in STAFFING_NOTIFY_KINDS:
        return []
    return sorted(k for k, v in modified_args.items() if opt.args.get(k) != v)


class ApprovalBrokerImpl:
    """Implements contracts.ApprovalBroker. request() publishes WsType.APPROVAL_REQUEST and audits;
    decide() resolves, audits (with reason) and publishes APPROVAL_RESOLVED; wait() is awaited by
    the state machine. A wait() that times out marks the request EXPIRED (no answer = no change)."""

    def __init__(self, bus=None, audit=None, clock=None):
        self._bus, self._audit, self._clock = bus, audit, clock
        self._reqs: dict[str, ApprovalRequest] = {}
        self._signals: dict[str, asyncio.Event] = {}

    def _now(self) -> datetime:
        return self._clock.now() if self._clock else datetime.now(TZ)

    def _signal(self, request_id: str) -> asyncio.Event:
        if request_id not in self._signals:
            self._signals[request_id] = asyncio.Event()
        return self._signals[request_id]

    def _publish(self, kind: WsType, req: ApprovalRequest) -> None:
        if self._bus is not None:
            try:
                self._bus.publish(kind, req)
            except Exception:  # noqa: BLE001
                pass

    def request(self, req: ApprovalRequest) -> ApprovalRequest:
        if req.id in self._reqs:
            raise ApprovalError(f"duplicate approval request id {req.id}")
        req = req.model_copy(update={"status": ApprovalStatus.PENDING, "decision": None})
        self._reqs[req.id] = req
        self._signal(req.id)
        if self._audit is not None:
            self._audit.append(AuditKind.APPROVAL_REQUEST, "state_machine",
                               req.model_dump(mode="json"), event_id=req.event_id)
        self._publish(WsType.APPROVAL_REQUEST, req)
        return req

    def decide(self, decision: ApprovalDecision) -> ApprovalRequest:
        req = self._reqs.get(decision.request_id)
        if req is None:
            raise ApprovalError(f"unknown approval request {decision.request_id}")
        if req.status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"approval {req.id} already {req.status.value}")
        option_id = decision.option_id
        if decision.decision is not DecisionKind.REJECT and req.options:
            ids = [o.id for o in req.options]
            if option_id is None:
                rec = [o.id for o in req.options if o.recommended]
                option_id = rec[0] if rec else ids[0]
            elif option_id not in ids:
                raise ApprovalError(f"unknown option {option_id!r}; expected one of {ids}")
            opt = next((o for o in req.options if o.id == option_id), None)
            changed = staffing_modification(opt, decision.modified_args)
            if changed:
                # a staffing option is a solver StaffingChange (team, shift, headcount) that the schedule
                # records once it is approved: editing who is told or how many would notify one thing
                # and record another, so it is refused (pick the other option, or reject with a reason)
                raise ApprovalError(f"人力方案 {option_id} 不可修改（{', '.join(changed)}）：請改選其他方案或否決並說明理由")
        decision = decision.model_copy(update={"option_id": option_id,
                                               "decided_at": decision.decided_at or self._now()})
        req = req.model_copy(update={"status": _STATUS_FOR[decision.decision], "decision": decision})
        self._reqs[req.id] = req
        if self._audit is not None:
            self._audit.append(AuditKind.APPROVAL_DECISION, decision.decided_by,
                               {"request_id": req.id, "decision": decision.decision.value,
                                "option_id": option_id, "reason": decision.reason, "via": decision.via,
                                "modified_args": decision.modified_args, "round": req.round,
                                "risk": req.risk.value}, event_id=req.event_id)
        self._publish(WsType.APPROVAL_RESOLVED, req)
        self._signal(req.id).set()
        return req

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self._reqs.values() if r.status is ApprovalStatus.PENDING]

    def all(self) -> list[ApprovalRequest]:
        return list(self._reqs.values())

    def get(self, request_id: str) -> ApprovalRequest:
        try:
            return self._reqs[request_id]
        except KeyError:
            raise ApprovalError(f"unknown approval request {request_id}") from None

    async def wait(self, request_id: str, timeout_s: float | None = None) -> ApprovalRequest:
        req = self.get(request_id)
        if req.status is not ApprovalStatus.PENDING:
            return req
        try:
            await asyncio.wait_for(self._signal(request_id).wait(), timeout_s)
        except TimeoutError:
            req = self._reqs[request_id]
            if req.status is ApprovalStatus.PENDING:
                req = req.model_copy(update={"status": ApprovalStatus.EXPIRED})
                self._reqs[request_id] = req
                if self._audit is not None:
                    self._audit.append(AuditKind.APPROVAL_DECISION, "system",
                                       {"request_id": request_id, "decision": "expired",
                                        "timeout_s": timeout_s}, event_id=req.event_id)
                self._publish(WsType.APPROVAL_RESOLVED, req)
        return self._reqs[request_id]
