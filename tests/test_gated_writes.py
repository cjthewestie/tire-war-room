"""Writes only through the Action Gate (research report 2026-11, 建議 1: G2 + G3 + G20).

(a) a write tool called without the gate's permit (call or acall) returns ok=False and leaves an
    INTERCEPTED audit entry; a permit is single-use and bound to its step, tool and args;
(b) the staffing withdrawal notice is a code-only template, with no free text from the earlier round;
(c) a rollback's staffing notice is audited GATE -> TOOL_CALL -> TOOL_RESULT;
(d) a withdrawal the gate holds for approval gets its own card and an alert, never a silent drop;
plus: the gate records an unknown tool's name as asked (not as read_sensors)."""
from __future__ import annotations

import asyncio

import pytest

from tests.agent_fakes import T0, sign_write
from tests.test_agent_page11 import build, page11_anomaly, supervisor
from warroom.agent import JsonlAuditLog, RuleGate
from warroom.agent.gate import CODE_NOTICE_TEMPLATES, R1_TEMPLATES, gated_call, revoke_notice, rollback_notice
from warroom.clock import SimClock
from warroom.contracts import (
    AuditKind,
    DecisionKind,
    EventState,
    GateVerdict,
    NotifyKind,
    PlanStep,
    RiskLevel,
    StaffingChange,
    TeamId,
    ToolCall,
    ToolName,
)
from warroom.tools import ToolContext, build_registry

NOTICE = {"teams": ["maintenance"], "kind": "maintenance", "machine_id": "CURE-07",
          "message": "7 號機冷卻水流偏低，請檢修冷卻水路"}


def _audit(tmp_path):
    return JsonlAuditLog(tmp_path / "audit.jsonl")


def _ctx(audit) -> ToolContext:
    return ToolContext(audit=audit, clock=SimClock(start=T0, manual=True))


def _intercepted(audit):
    return [e for e in audit.tail(0, 10_000) if e.kind is AuditKind.INTERCEPTED]


# ---------------------------------------------------------------- (a) the registry refuses ungated writes
@pytest.mark.parametrize("gated", [False, True])
def test_write_without_permit_is_refused_and_intercepted(tmp_path, gated):
    audit = _audit(tmp_path)
    reg = build_registry(_ctx(audit), gate=RuleGate() if gated else None)
    res = reg.call(ToolCall(id="c1", step_id="S1", tool=ToolName.NOTIFY_TEAMS, args=NOTICE))
    assert not res.ok and res.error == "ungated_write"
    hits = _intercepted(audit)
    assert len(hits) == 1 and hits[0].payload["tool"] == "notify_teams" and "ungated_write" in hits[0].payload["reason"]


async def test_acall_write_without_permit_is_refused_and_intercepted(tmp_path):
    audit = _audit(tmp_path)
    reg = build_registry(_ctx(audit), gate=RuleGate())
    res = await reg.acall(ToolCall(id="c1", step_id="S1", tool=ToolName.NOTIFY_TEAMS, args=NOTICE))
    assert not res.ok and res.error == "ungated_write"
    assert len(_intercepted(audit)) == 1
    res = await reg.acall(ToolCall(id="c2", step_id="S1", tool=ToolName.NOTIFY_TEAMS, args=NOTICE), permit="forged")
    assert not res.ok and res.error == "ungated_write" and len(_intercepted(audit)) == 2


def test_permit_is_single_use_and_bound_to_step_tool_and_args(tmp_path):
    audit = _audit(tmp_path)
    gate = RuleGate()
    reg = build_registry(_ctx(audit), gate=gate)
    step = PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, args=NOTICE, purpose="notice")
    assert gate.check(step, EventState.EXECUTE, True).verdict is GateVerdict.ALLOW
    call = ToolCall(id="c1", step_id="S1", tool=ToolName.NOTIFY_TEAMS, args=NOTICE)

    other_args = gate.issue_permit(step)
    changed = call.model_copy(update={"args": {**NOTICE, "message": "改成別的內容"}})
    assert reg.call(changed, permit=other_args).error == "ungated_write"        # args differ from what was gated
    other_step = gate.issue_permit(step)
    assert reg.call(call.model_copy(update={"step_id": "S9"}), permit=other_step).error == "ungated_write"

    permit = gate.issue_permit(step)
    assert reg.call(call, permit=permit).ok                                       # the gated write runs once
    assert reg.call(call, permit=permit).error == "ungated_write"                 # ... and only once
    assert len(_intercepted(audit)) == 3


def test_gate_signs_only_what_it_let_through():
    gate = RuleGate()
    # never checked -> no permit
    assert gate.issue_permit(PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, args=NOTICE, purpose="x")) is None
    # R2 needs approval: a permit only with the approval that cleared it
    cure = PlanStep(id="S4", tool=ToolName.SET_CURE_TIME,
                    args={"press_id": "CURE-07", "total_cure_h": 36.5, "reason": "formula"}, purpose="x")
    assert gate.check(cure, EventState.EXECUTE, True).verdict is GateVerdict.NEEDS_APPROVAL
    assert gate.issue_permit(cure) is None
    assert gate.issue_permit(cure, approval_id="AP-1")
    # rejected -> never a permit, even with an approval id
    bad = PlanStep(id="S5", tool=ToolName.SET_CURE_TIME,
                   args={"press_id": "A-MIX", "total_cure_h": 10, "reason": "x"}, purpose="x")
    assert gate.check(bad, EventState.EXECUTE, True).verdict is GateVerdict.REJECT
    assert gate.issue_permit(bad, approval_id="AP-1") is None
    # a write checked outside EXECUTE is not signed either
    other = PlanStep(id="S6", tool=ToolName.NOTIFY_TEAMS, args=NOTICE, purpose="x")
    gate.check(other, EventState.SIMULATE, True)
    assert gate.issue_permit(other) is None


def test_read_tools_need_no_permit(tmp_path):
    audit = _audit(tmp_path)
    reg = build_registry(_ctx(audit))
    res = reg.call(ToolCall(id="r1", tool=ToolName.CURE_FORMULA,
                            args={"press_id": "CURE-07", "temp_c": 122.0, "base_cure_h": 28.0}))
    assert res.error != "ungated_write"
    assert not _intercepted(audit)


def test_sign_write_helper_goes_through_a_real_gate(tmp_path):
    reg = build_registry(_ctx(_audit(tmp_path)))
    ok = ToolCall(id="c", step_id="S1", tool=ToolName.NOTIFY_TEAMS, args=NOTICE)
    assert reg.call(ok, permit=sign_write(reg, ok)).ok
    bad = ToolCall(id="d", step_id="S2", tool=ToolName.NOTIFY_TEAMS, args={**NOTICE, "kind": "plc_write"})
    assert sign_write(reg, bad) is None


def test_unknown_tool_name_is_recorded_as_asked():
    step = PlanStep.model_construct(id="S9", tool="plc_write", args={}, depends_on=[], purpose="x")
    gd = RuleGate().check(step, EventState.EXECUTE, True)
    assert gd.verdict is GateVerdict.REJECT and gd.risk is None
    assert gd.model_dump(mode="json")["tool"] == "plc_write"


# ---------------------------------------------------------------- code-only notice templates
def test_code_notices_are_not_shown_to_the_model():
    from warroom.agent import commander
    shown = commander._r1_templates_text() if hasattr(commander, "_r1_templates_text") else str(R1_TEMPLATES)
    for tpl in CODE_NOTICE_TEMPLATES.values():
        head = tpl.split("{", 1)[0]
        assert head not in shown
        assert all(head not in t for ts in R1_TEMPLATES.values() for t in ts)


def test_code_notice_is_r1_only_with_matching_evidence():
    gate = RuleGate()
    c = StaffingChange(kind="overtime", shift_date="2026-10-03", shift_name="夜班", to_team=TeamId.BODY,
                       label="夜班加班")
    msg = rollback_notice(2, 1, "撤回", c)
    args = {"teams": ["body"], "kind": NotifyKind.INFO.value, "message": msg}
    step = PlanStep(id="RB-1", tool=ToolName.NOTIFY_TEAMS, args=args, purpose="x")
    ev = {"code_notice": {"message": msg, "teams": ["body"]}}
    assert gate.check(step, EventState.EXECUTE, True, evidence=ev).risk is RiskLevel.R1_NOTIFY
    # the same text without the code's evidence (e.g. written by the model) is free text: R3
    assert gate.check(step, EventState.EXECUTE, True).risk is RiskLevel.R3_CRITICAL
    # evidence for other teams or another message does not carry over
    wide = {**args, "teams": ["body", "side"]}
    assert gate.check(step.model_copy(update={"args": wide}), EventState.EXECUTE, True,
                      evidence=ev).risk is RiskLevel.R3_CRITICAL
    text = {**args, "message": msg + "，另外全線停機"}
    assert gate.check(step.model_copy(update={"args": text}), EventState.EXECUTE, True,
                      evidence={"code_notice": {"message": text["message"], "teams": ["body"]}}
                      ).risk is RiskLevel.R3_CRITICAL


# ---------------------------------------------------------------- (b) + (d) withdrawal in the page-11 flow
PAGE11 = [("02:16:40", DecisionKind.APPROVE, None, ""),
          ("02:31:30", DecisionKind.APPROVE, "A", "跨組調人影響 2 線，改夜班加班")]


async def test_page11_writes_all_carry_gate_permits_and_withdrawal_is_code_text(tmp_path):
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    assert tools.gate_permits is sm.gate.permits          # the state machine attached its gate
    sup = asyncio.create_task(supervisor(clock, approvals, PAGE11))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED
    writes = [c for c in tools.calls if c.tool in (ToolName.NOTIFY_TEAMS, ToolName.CONFIRM_SCHEDULE,
                                                   ToolName.SET_CURE_TIME)]
    assert writes and not tools.ungated, [c.id for c in tools.ungated]
    assert all(tools.permits[c.id] for c in writes)

    notices = tools.calls_of(ToolName.NOTIFY_TEAMS)
    move, revoke = notices[1], notices[-1]
    assert move.args["kind"] == NotifyKind.STAFF_MOVE.value
    # (b) the withdrawal carries only code-built fields, none of the earlier round's message
    assert move.args["message"] not in revoke.args["message"]
    want = revoke_notice({"shift_date": "2026-10-03", "shift_name": "夜班", "label": "從 2 線調 1 人"},
                         {"label": "夜班加班"})
    assert revoke.args["message"] == want, revoke.args["message"]
    assert revoke.args["teams"] == move.args["teams"]


class _HoldRevocations(RuleGate):
    """Judges every withdrawal notice NEEDS_APPROVAL (R3), as a stricter gate might."""

    def check(self, step, state, simulation_feasible, **kw):
        gd = super().check(step, state, simulation_feasible, **kw)
        if "-revoke-" in step.id and gd.verdict is GateVerdict.ALLOW:
            gd = gd.model_copy(update={"verdict": GateVerdict.NEEDS_APPROVAL, "risk": RiskLevel.R3_CRITICAL})
            self.permits.note(step, gd)
        return gd


def _with_gate(tmp_path, gate_cls):
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    sm.gate = gate_cls(store=store)
    tools.attach_gate(sm.gate)
    return clock, bus, audit, sim, store, tools, approvals, sm


@pytest.mark.parametrize("approve", [True, False])
async def test_withdrawal_needing_approval_opens_a_card_and_an_alert(tmp_path, approve):
    clock, bus, audit, sim, store, tools, approvals, sm = _with_gate(tmp_path, _HoldRevocations)
    third = ("02:32:00", DecisionKind.APPROVE if approve else DecisionKind.REJECT, "A" if approve else None,
             "" if approve else "先不撤回")
    sup = asyncio.create_task(supervisor(clock, approvals, [*PAGE11, third]))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    reqs = approvals.all()
    assert len(reqs) == 3 and "revoke" in reqs[2].id and reqs[2].title == "撤回人力通知"
    assert reqs[2].options[0].args["message"].startswith("撤回：")
    held = [e for e in audit.tail(0, 10_000) if e.kind is AuditKind.SYSTEM and "revocation_needs_approval" in e.payload]
    assert held, "no SYSTEM audit for the held withdrawal"
    assert any("撤回通知待核准" in t.text or "需主管核准才會送出" in t.text for t in ev.timeline)
    sent = [c for c in tools.calls_of(ToolName.NOTIFY_TEAMS) if c.args["message"].startswith("撤回：")]
    assert not tools.ungated
    if approve:
        assert len(sent) == 1 and tools.permits[sent[0].id]
        assert "撤回從 2 線調 1 人" in ev.outcome
    else:
        assert sent == []
        assert any(e.kind is AuditKind.SYSTEM and "revocation_not_sent" in e.payload for e in audit.tail(0, 10_000))
        assert any("舊的人力通知仍有效" in t.text for t in ev.timeline)


# ---------------------------------------------------------------- (c) rollback notice: GATE -> TOOL_CALL -> TOOL_RESULT
def test_gated_call_audits_gate_then_call_then_result(tmp_path):
    audit = _audit(tmp_path)
    gate = RuleGate()
    reg = build_registry(_ctx(audit), gate=gate)
    c = StaffingChange(kind="transfer", shift_date="2026-10-03", shift_name="夜班", to_team=TeamId.BODY,
                       from_team=TeamId.SIDE, label="從 2 線調 1 人")
    msg = rollback_notice(3, 2, "撤回", c)
    step = PlanStep(id="AP-RB-1-撤回-1003-transfer", tool=ToolName.NOTIFY_TEAMS, purpose="x",
                    args={"teams": ["body", "side"], "kind": "info", "message": msg})
    gd, res = gated_call(gate, reg, audit, step, actor="supervisor",
                         evidence={"code_notice": {"message": msg, "teams": ["body", "side"]}})
    assert gd.verdict is GateVerdict.ALLOW and res is not None and res.ok, (gd.reasons, res)
    kinds = [e.kind for e in audit.tail(0, 10_000)]
    assert kinds[-3:] == [AuditKind.GATE, AuditKind.TOOL_CALL, AuditKind.TOOL_RESULT], kinds
    # the same notice without evidence is R3: no call, nothing intercepted at the registry
    gd2, res2 = gated_call(gate, reg, audit, step.model_copy(update={"id": "x2"}), actor="supervisor")
    assert gd2.verdict is GateVerdict.NEEDS_APPROVAL and res2 is None


def test_runtime_rollback_notice_goes_through_the_gate(tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_api_review_r4 import _real
    from warroom.api import create_app
    rt = _real(tmp_path)
    store, now = rt.c.store, rt.now()
    v1 = store.current()
    ot = StaffingChange(kind="overtime", shift_date=now.date(), shift_name="夜班", to_team=TeamId.BODY,
                        label="夜班加班")
    v2 = store.propose(v1.schedule.model_copy(update={"id": "S2", "staffing_changes": [ot]}), "v2 overtime")
    store.activate(v2.version)
    with TestClient(create_app(rt, local_hosts=frozenset({"testclient"}))) as c:
        r = c.post("/api/schedule/rollback", json={"confirm": True})
        assert r.status_code == 200, r.text
    entries = rt.audit.tail(0, 10_000)
    notice = [e for e in entries if e.payload.get("msg") == "rollback staffing notice"][-1]
    assert notice.payload["delivered"] is True and notice.payload["gate"] == "allow"
    assert notice.payload["text"] == rollback_notice(v2.version, v1.version, "撤回", ot)
    sid = next(e.payload["step_id"] for e in entries if e.kind is AuditKind.GATE
               and str(e.payload.get("step_id", "")).startswith("AP-RB-"))
    seq = [e.kind for e in entries if e.payload.get("step_id") == sid]
    assert seq == [AuditKind.GATE, AuditKind.TOOL_CALL, AuditKind.TOOL_RESULT], seq
    assert not [e for e in entries if e.kind is AuditKind.INTERCEPTED and "ungated_write" in str(e.payload)]
