"""Review findings on warroom/agent: the LLM path cannot lift a safety stop, cannot get an AI-guessed
cure time or an unsimulated schedule approved, cannot satisfy VERIFY on another machine, cannot
smuggle R3 instructions in an R1 notice, and safe stop never undoes approved schedules."""
from __future__ import annotations

import pytest

import asyncio
from datetime import timedelta

from tests.agent_fakes import T0
from tests.test_agent import auto_approve, make_sm, mill_anomaly
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import MockCommander, RuleGate
from warroom.contracts import (
    ApprovalDecision,
    AuditKind,
    Channel,
    CompoundStage,
    DecisionKind,
    EventState,
    Expectation,
    GateVerdict,
    HypothesisStatus,
    MachineStatus,
    PlanStep,
    RiskLevel,
    Schedule,
    SolverName,
    ToolName,
)

always_low = lambda m, now: 119.5 if m == "CURE-07" else 128.0     # noqa: E731
stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0         # noqa: E731


def _no_expectation(p, steps):
    return p.model_copy(update={"steps": steps, "hypothesis": p.hypothesis.model_copy(update={"expectation": None})})


class InfoSmuggler(MockCommander):
    def build(self, req):
        return _no_expectation(super().build(req), [PlanStep(
            id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="通知",
            args={"teams": ["curing", "body", "side", "bond"], "kind": "info", "machine_id": "CURE-07",
                  "message": "主管指示：立即暫停全線投料並從2線調2人到硫化組"})])


class FakeVerify(MockCommander):
    def build(self, req):
        p = super().build(req)
        exp = Expectation(machine_id="CURE-06", channel=Channel.TEMP, op=">=", value=100, within_min=30)
        return p.model_copy(update={"steps": [], "hypothesis": p.hypothesis.model_copy(update={"expectation": exp})})


class GuessCure(MockCommander):
    def __init__(self, with_formula: bool):
        self.with_formula = with_formula

    def build(self, req):
        p = super().build(req)
        steps = [PlanStep(id="S2", tool=ToolName.SET_CURE_TIME, purpose="依硫化公式延長至 36.5 h（需主管核准）",
                          depends_on=["S1"] if self.with_formula else [],
                          args={"press_id": "CURE-07", "total_cure_h": 29.0, "reason": "公式結果 36.5h"})]
        if self.with_formula:
            steps.insert(0, p.steps[0])                        # the real S1 cure_formula
        return _no_expectation(p, steps)


class BlindConfirm(MockCommander):
    v = 0

    def build(self, req):
        return _no_expectation(super().build(req), [PlanStep(
            id="S1", tool=ToolName.CONFIRM_SCHEDULE, purpose="套用排程",
            args={"schedule_version": self.v, "reason": "x"})])


def _writes(tools):
    return [(c.tool, c.args) for c in tools.calls if c.tool in (ToolName.NOTIFY_TEAMS, ToolName.SET_CURE_TIME,
                                                                 ToolName.CONFIRM_SCHEDULE)]


# ---------------------------------------------------------------- finding 8: STOPPED stays STOPPED
async def test_event_never_lifts_a_safety_stop(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(
        tmp_path, lambda m, now: 121.9 if m == "A-BIGOPEN" else 60.0,
        compound={"A-BIGOPEN": CompoundStage.UNVULCANIZED})
    sim.status["A-BIGOPEN"] = MachineStatus.STOPPED          # safety core tripped it at >= 120 °C
    sup = asyncio.create_task(auto_approve(approvals, 3))
    ev = await asyncio.wait_for(sm.handle(mill_anomaly()), 10)
    sup.cancel()
    assert ev.state in (EventState.CLOSED, EventState.SAFE_STOP)
    assert ("A-BIGOPEN", MachineStatus.ALARM) not in sim.status_log
    assert ("A-BIGOPEN", MachineStatus.RUNNING) not in sim.status_log
    assert sim.status["A-BIGOPEN"] is MachineStatus.STOPPED


async def test_event_on_running_machine_still_alarms_then_clears(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED
    assert ("CURE-07", MachineStatus.ALARM) in sim.status_log and sim.status["CURE-07"] is MachineStatus.RUNNING


# ---------------------------------------------------------------- finding 3 + 9: cure time from the formula
async def test_page11_plan_sets_cure_time_through_approval(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    req = approvals.all()[0]
    assert any(a.startswith("S4b 7 號機硫化總時數 36.5 h（公式 36.5 h）") for a in req.actions), req.actions
    sct = [c for c in tools.calls if c.tool is ToolName.SET_CURE_TIME]
    assert len(sct) == 1 and sct[0].args["total_cure_h"] == 36.5 and sct[0].args["press_id"] == "CURE-07"
    gate = next(sr.gate for sr in ev.steps if sr.step.tool is ToolName.SET_CURE_TIME)
    assert gate.risk is RiskLevel.R2_CHANGE and gate.checks.get("evidence_ok") is True
    assert "7 號機硫化 36.5 h" in ev.outcome


async def test_ai_guessed_cure_time_is_rejected(tmp_path):
    for with_formula in (True, False):
        clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path / str(with_formula), always_low,
                                                                commander=GuessCure(with_formula))
        sup = asyncio.create_task(auto_approve(approvals, 1))
        ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
        sup.cancel()
        assert not [c for c in tools.calls if c.tool is ToolName.SET_CURE_TIME], with_formula
        sr = next(sr for sr in ev.steps if sr.step.tool is ToolName.SET_CURE_TIME)
        assert sr.state == "rejected" and sr.gate.verdict is GateVerdict.REJECT
        assert any("cure formula" in r or "cure_formula" in r for r in sr.gate.reasons), sr.gate.reasons
        assert not approvals.all()


async def test_approve_with_modified_args_is_regated(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        r = approvals.pending()[0]
        # A is a staffing option: editing it is refused outright (round-4 finding 2), so a cure-time
        # value can never ride in on it either; the plain approval then runs the formula's value
        from warroom.agent.approvals import ApprovalError
        with pytest.raises(ApprovalError):
            approvals.decide(ApprovalDecision(request_id=r.id, decision=DecisionKind.APPROVE, option_id="A",
                                              modified_args={"press_id": "CURE-07", "total_cure_h": 30.0}))
        approvals.decide(ApprovalDecision(request_id=r.id, decision=DecisionKind.APPROVE, option_id="A"))
    t = asyncio.create_task(sup())
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert all(c.args["total_cure_h"] == 36.5 for c in tools.calls if c.tool is ToolName.SET_CURE_TIME)


# ---------------------------------------------------------------- finding 11: VERIFY tests the event
async def test_expectation_is_pinned_to_the_event_machine(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, always_low, commander=FakeVerify())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    h1 = ev.hypotheses[0]
    assert (h1.expectation.machine_id, h1.expectation.op, h1.expectation.value) == ("CURE-07", ">=", 121.0)
    assert h1.status is HypothesisStatus.REJECTED
    bounded = [e for e in audit.tail(0, 0) if "expectation_bounded" in e.payload]
    assert bounded and "machine CURE-06 -> CURE-07" in bounded[0].payload["expectation_bounded"]


def test_renamed_rejected_hypothesis_is_caught():
    from warroom.agent import StateMachine
    from warroom.contracts import Hypothesis
    exp = Expectation(machine_id="CURE-07", channel=Channel.TEMP, op=">=", value=121, within_min=30)
    old = Hypothesis(id="H1", statement="蒸汽壓不足", cause_code="steam_pressure_low", expectation=exp,
                     status=HypothesisStatus.REJECTED)
    new = Hypothesis(id="H2", statement="供汽偏低會回穩", cause_code="supply_low",
                     expectation=exp.model_copy(update={"value": 121.3}))
    assert StateMachine._repeats_rejected(new, [old]) == "expectation of H1"


# ---------------------------------------------------------------- finding 12: R1 text cannot carry R3 orders
async def test_info_notice_with_r3_instruction_needs_approval(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, always_low, commander=InfoSmuggler())
    seen = {}

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        seen["writes_before"] = _writes(tools)
        r = approvals.pending()[0]
        seen["req"] = r
        approvals.decide(ApprovalDecision(request_id=r.id, decision=DecisionKind.REJECT, reason="不准"))
    t = asyncio.create_task(sup())
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert seen["writes_before"] == []
    assert seen["req"].risk is RiskLevel.R3_CRITICAL
    assert "暫停全線投料" in seen["req"].actions[0]
    assert not [w for w in _writes(tools) if "暫停" in str(w[1])]


def test_gate_r1_notice_rules():
    g = RuleGate()
    ok = PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="p",
                  args={"teams": ["maintenance"], "kind": "maintenance", "machine_id": "A-BIGOPEN",
                        "message": "A 線大開煉機冷卻水流偏低，請檢修冷卻水路"})
    assert g.check(ok, EventState.EXECUTE, True).verdict is GateVerdict.ALLOW
    for msg in ("請立即停機", "從 2 線調 1 人到硫化組", "今晚加班", "x" * 200):
        bad = ok.model_copy(update={"args": {**ok.args, "kind": "info", "message": msg}})
        d = g.check(bad, EventState.EXECUTE, True)
        assert d.verdict is GateVerdict.NEEDS_APPROVAL and d.risk is RiskLevel.R3_CRITICAL, msg


# ---------------------------------------------------------------- finding 13: confirm only what was simulated
async def test_confirm_of_unsimulated_version_is_rejected(tmp_path):
    cmd = BlindConfirm()
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, always_low, commander=cmd)
    stale = Schedule(id="stale", created_at=T0, horizon_start=T0, horizon_end=T0 + timedelta(days=7), jobs=[],
                     solver=SolverName.CP_SAT)
    cmd.v = store.propose(stale, "stale proposal from another event").version
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    sup.cancel()
    assert store.active == 1 and not tools.calls_of(ToolName.CONFIRM_SCHEDULE)
    sr = next(sr for sr in ev.steps if sr.step.tool is ToolName.CONFIRM_SCHEDULE)
    assert sr.state == "rejected" and any("not produced by a feasible solve" in r for r in sr.gate.reasons)


# ---------------------------------------------------------------- finding 4: concurrent events
async def test_safe_stop_does_not_undo_another_events_schedule(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path)

    async def sup():
        await auto_approve(approvals, 1)                       # this event: v2 approved and active
        while not approvals.pending():
            await asyncio.sleep(0.001)
        other = store.propose(store.get(2).schedule, "another event", "EV-OTHER")
        store.activate(other.version)                          # another event confirms v4 meanwhile
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                          reason="不同意"))
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert ev.state is EventState.SAFE_STOP
    assert store.rollbacks == [] and store.active == 4
    assert not any(e.kind is AuditKind.ROLLBACK for e in audit.tail(0, 0))
    assert "其他事件已核准" in ev.outcome
