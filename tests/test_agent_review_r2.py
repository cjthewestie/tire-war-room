"""Review round 2 on warroom/agent: a stale schedule confirm halts its round and re-plans, physics
numbers (cure formula inputs, solver cure overrides, set_cure_time press) come only from the plant
and the formula, a supervisor reject keeps the hypothesis, VERIFY cannot be passed trivially or by a
safety stop, R1 notices are fixed templates, page-11 replans survive an LLM-style H2 expectation,
moves are grouped on the card, and the audit log catches a removed head anchor."""
from __future__ import annotations

import asyncio
import os

import pytest

from tests.test_agent import auto_approve, make_sm, mill_anomaly
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import JsonlAuditLog, MockCommander, RuleGate, StateMachine
from warroom.agent.audit import AuditKeyError
from warroom.agent.narrate import moves_text
from warroom.contracts import (
    ApprovalDecision,
    AuditKind,
    Channel,
    CompoundStage,
    DecisionKind,
    EventState,
    Expectation,
    GateVerdict,
    Hypothesis,
    HypothesisStatus,
    MachineStatus,
    PlanStep,
    RiskLevel,
    SafetyAction,
    SafetyActionKind,
    ToolName,
    ToolResult,
)

always_low = lambda m, now: 119.5 if m == "CURE-07" else 128.0     # noqa: E731
stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0         # noqa: E731


def _writes(tools, tool):
    return [c.args for c in tools.calls if c.tool is tool]


# ---------------------------------------------------------------- 1: stale confirm halts the round
async def test_stale_confirm_skips_round_and_resolves(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    orig = tools._confirm_schedule
    state = {"n": 0}

    def flaky(a):
        state["n"] += 1
        if state["n"] <= 2:                      # first confirm (and its retry): stale
            raise RuntimeError("schedule v2 is stale: solved against v1, but v9 is active now")
        return orig(a)
    tools._confirm_schedule = flaky
    real_call = tools.call

    def call(c):
        try:
            return real_call(c)
        except RuntimeError as e:
            return ToolResult(call_id=c.id, tool=c.tool, ok=False, error=f"ToolError: {e}", attempt=c.attempt)
    tools.call = call
    # round 4: the re-solve carries exactly what was approved, so the supervisor's approval is carried
    # over (recorded as its own approval via "auto") instead of asking a second time
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    reqs = approvals.all()
    assert len(reqs) == 2 and reqs[1].id == f"{reqs[0].id}-rebased" and reqs[1].decision.via == "auto"
    assert reqs[0].id in reqs[1].decision.reason
    assert any("✗ 已核准但未生效" in t.text for t in ev.timeline)
    assert any(f"沿用核准 {reqs[0].id}" in t.text for t in ev.timeline)
    assert ev.consecutive_failures == 0                      # a stale schedule is not a plan failure
    sct = _writes(tools, ToolName.SET_CURE_TIME)
    assert len(sct) == 1                                     # only after the re-solved schedule took effect
    infos = [a for a in _writes(tools, ToolName.NOTIFY_TEAMS) if a["kind"] == "info"]
    assert len(infos) == 1
    assert any("未生效 → 本輪其餘動作不執行" in t.text for t in ev.timeline)
    # another event's schedule took effect first: said as that, not as a failed round (「動作未完成…」)
    assert any("依現行排程重算（不算失敗）" in t.text for t in ev.timeline)
    assert not any(t.text.startswith("動作未完成") for t in ev.timeline)
    assert len(tools.calls_of(ToolName.SOLVE_SCHEDULE)) == 2  # re-solved against the active schedule
    assert ev.hypotheses[0].id == "H1" and len(ev.hypotheses) == 1     # hypothesis kept, not swapped
    assert ev.state is EventState.CLOSED and store.active == 3
    assert ev.outcome.count("排程 v") == 1 and "排程 v3 生效" in ev.outcome


# ---------------------------------------------------------------- 2 + 6 + 10: physics from plant + formula
def _gate_solve(over, evidence):
    st = PlanStep(id="S3", tool=ToolName.SOLVE_SCHEDULE, purpose="p", args={"cure_overrides_h": over})
    return RuleGate().check(st, EventState.SIMULATE, True, evidence=evidence)


def test_gate_bounds_and_ties_cure_overrides_to_the_formula():
    ev = {"cure_formula_h": [36.5], "cure_formula_by_press": {"CURE-07": [36.5]}}
    assert _gate_solve({"CURE-07": 36.5}, ev).verdict is GateVerdict.ALLOW
    for bad in ({"CURE-07": -5.0}, {"CURE-07": 10000.0}, {"CURE-07": 24.0}, {"CURE-03": 36.5}):
        d = _gate_solve(bad, ev)
        assert d.verdict is GateVerdict.REJECT, (bad, d.reasons)
    assert _gate_solve({"CURE-07": 30.0}, {"cure_formula_h": [], "cure_formula_by_press": {}}).verdict \
        is GateVerdict.REJECT


class GuessOverride(MockCommander):
    def build(self, req):
        p = super().build(req)
        if req.phase != "plan":
            return p
        steps = [s.model_copy(update={"args": {**s.args, "cure_overrides_h": {"CURE-07": 24.0}}})
                 if s.tool is ToolName.SOLVE_SCHEDULE else s for s in p.steps]
        return p.model_copy(update={"steps": steps})


async def test_ai_cure_override_is_replaced_by_formula(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=GuessOverride())
    sup = asyncio.create_task(auto_approve(approvals, 1))
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert tools.calls_of(ToolName.SOLVE_SCHEDULE)[0].args["cure_overrides_h"] == {"CURE-07": 36.5}
    assert any("cure_overrides_from_formula" in e.payload for e in audit.tail(0, 0))
    card = approvals.all()[0]
    assert any("7 號硫化 36.5 h" in a for a in card.actions), card.actions     # solver inputs shown


class WrongPressCure(MockCommander):
    def build(self, req):
        p = super().build(req)
        steps = [PlanStep(id="S1", tool=ToolName.CURE_FORMULA, purpose="公式",
                          args={"size": "M", "actual_temp_c": 128, "elapsed_h": 0, "ref_cure_h": 90}),
                 PlanStep(id="S2", tool=ToolName.SET_CURE_TIME, depends_on=["S1"], purpose="設定",
                          args={"press_id": "CURE-03", "total_cure_h": "$S1.total_cure_h", "reason": "r"})]
        return p.model_copy(update={"steps": steps})


async def test_cure_formula_inputs_from_plant_and_press_bound(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, always_low, commander=WrongPressCure())
    sup = asyncio.create_task(auto_approve(approvals, 3))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    sup.cancel()
    cf = tools.calls_of(ToolName.CURE_FORMULA)[0].args
    assert (cf["size"], cf["elapsed_h"], cf["ref_cure_h"], cf["actual_temp_c"], cf["activation_energy_kj_mol"]) == \
        ("M", 10.0, 28.0, 122.0, 85.0)                       # live cure job + predicted temperature
    assert not _writes(tools, ToolName.SET_CURE_TIME)        # CURE-03 has no formula result in this event
    sr = next(sr for sr in ev.steps if sr.step.tool is ToolName.SET_CURE_TIME)
    assert sr.state == "rejected" and any("CURE-03" in r for r in sr.gate.reasons)


# ---------------------------------------------------------------- 3: supervisor reject keeps H1
async def test_reject_keeps_hypothesis_and_drops_cross_team(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                          reason="test reject r1"))
        await auto_approve(approvals, 1)
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert [(h.id, h.cause_code) for h in ev.hypotheses] == [("H1", "steam_pressure_low")]
    assert ev.hypotheses[0].status is HypothesisStatus.CONFIRMED
    assert tools.calls_of(ToolName.SOLVE_SCHEDULE)[1].args.get("allow_cross_team") is False
    assert not _writes(tools, ToolName.NOTIFY_TEAMS) or all(a["kind"] != "machine_out"
                                                           for a in _writes(tools, ToolName.NOTIFY_TEAMS))
    assert any("主管否決 → 換方案（H1" in t.text for t in ev.timeline)
    assert not any("推翻 ?" in t.text for t in ev.timeline)


# ---------------------------------------------------------------- 4: safety stop makes VERIFY inconclusive
async def test_verify_inconclusive_when_safety_stopped(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(
        tmp_path, lambda m, now: 60.0, compound={"A-BIGOPEN": CompoundStage.UNVULCANIZED})
    sim.status["A-BIGOPEN"] = MachineStatus.STOPPED
    sm.on_safety_action(SafetyAction(id="SA", ts=clock.now(), machine_id="A-BIGOPEN", rule_id="compound",
                                     action=SafetyActionKind.STOP_MACHINE, value=121.0, threshold=120.0,
                                     message="停機", latency_ms=1.0))
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(mill_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED
    assert ev.hypotheses[0].status is not HypothesisStatus.CONFIRMED
    assert "無法驗證" in ev.outcome and sim.status["A-BIGOPEN"] is MachineStatus.STOPPED
    assert not any("H1 成立" in t.text for t in ev.timeline)


# ---------------------------------------------------------------- 5: R1 = fixed templates only
@pytest.mark.parametrize("msg", ["7號機立即下線，模具拆下", "請2線派一人到硫化組", "大開煉機降速，生料減半投入",
                                 "Ｐａｕｓｅ ｆｅｅｄ", "暫 停 投 料", "停​機", "請關掉密煉機電源",
                                 "S.T.O.P all lines", "全線熄火至週一", "夜班延長4小時"])
@pytest.mark.parametrize("kind", ["info", "maintenance"])
def test_r1_free_text_needs_approval(kind, msg):
    st = PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="p",
                  args={"teams": ["maintenance"], "kind": kind, "message": msg, "machine_id": "CURE-07"})
    d = RuleGate().check(st, EventState.EXECUTE, True)
    assert d.verdict is GateVerdict.NEEDS_APPROVAL and d.risk is RiskLevel.R3_CRITICAL


def test_r1_template_rules():
    g = RuleGate()
    base = {"teams": ["maintenance"], "kind": "maintenance", "machine_id": "A-BIGOPEN",
            "message": "A 線大開煉機冷卻水流偏低，請檢修冷卻水路"}
    ok = PlanStep(id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="p", args=base)
    assert g.check(ok, EventState.EXECUTE, True).verdict is GateVerdict.ALLOW
    for upd in ({"machine_id": "B-OPEN"},                                  # label does not match machine
                {"teams": ["maintenance", "curing", "side", "bond"]},     # teams beyond the machine's own
                {"machine_id": None}):
        st = ok.model_copy(update={"args": {**base, **upd}})
        assert g.check(st, EventState.EXECUTE, True).risk is RiskLevel.R3_CRITICAL, upd


class SmuggleMaintenance(MockCommander):
    def build(self, req):
        p = super().build(req)
        return p.model_copy(update={"steps": [PlanStep(
            id="S1", tool=ToolName.NOTIFY_TEAMS, purpose="通知維修",
            args={"teams": ["maintenance"], "kind": "maintenance", "machine_id": "CURE-07",
                  "message": "7號機立即下線；請2線派一人到硫化組；大開煉機生料減半投入"})]})


async def test_r1_smuggled_orders_wait_for_supervisor_end_to_end(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=SmuggleMaintenance())
    seen = {}

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        seen["before"] = _writes(tools, ToolName.NOTIFY_TEAMS)
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                          reason="不准"))
        while not approvals.pending():
            await asyncio.sleep(0.001)
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                          reason="不准"))
    t = asyncio.create_task(sup())
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert seen["before"] == [] and approvals.all()[0].risk is RiskLevel.R3_CRITICAL
    assert not _writes(tools, ToolName.NOTIFY_TEAMS)


# ---------------------------------------------------------------- 7: VERIFY cannot be passed trivially
class NullExpectation(MockCommander):
    def build(self, req):
        p = super().build(req)
        return p.model_copy(update={"steps": [], "hypothesis": p.hypothesis.model_copy(update={"expectation": None})})


class OtherChannel(MockCommander):
    def build(self, req):
        p = super().build(req)
        exp = Expectation(machine_id=req.anomaly.machine_id, channel=Channel.WATER_FLOW, op=">=", value=0,
                          within_min=5)
        return p.model_copy(update={"steps": [], "hypothesis": p.hypothesis.model_copy(update={"expectation": exp})})


@pytest.mark.parametrize("commander", [NullExpectation, OtherChannel])
async def test_verify_cannot_be_passed_trivially_on_press(tmp_path, commander):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, always_low, commander=commander())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    h1 = ev.hypotheses[0]
    assert (h1.expectation.channel, h1.expectation.op, h1.expectation.value) == (Channel.TEMP, ">=", 121.0)
    assert h1.status is HypothesisStatus.REJECTED
    assert ("CURE-07", MachineStatus.RUNNING) not in sim.status_log


async def test_verify_cannot_be_passed_trivially_on_mill(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(
        tmp_path, lambda m, now: 119.0 if m == "A-BIGOPEN" else 80.0,
        compound={"A-BIGOPEN": CompoundStage.UNVULCANIZED}, commander=OtherChannel())
    ev = await asyncio.wait_for(sm.handle(mill_anomaly()), 10)
    h1 = ev.hypotheses[0]
    assert (h1.expectation.channel, h1.expectation.op, h1.expectation.value) == (Channel.TEMP, "<=", 118.0)
    assert h1.status is HypothesisStatus.REJECTED
    assert ("A-BIGOPEN", MachineStatus.RUNNING) not in sim.status_log


# ---------------------------------------------------------------- 12: LLM-style H2 '<= 119' on replan
class LLMStyleH2(MockCommander):
    """What Nemotron returned on the page-11 replan: H2 with expectation CURE-07 temp <= 119."""

    def build(self, req):
        p = super().build(req)
        if req.phase != "replan":
            return p
        exp = Expectation(machine_id="CURE-07", channel=Channel.TEMP, op="<=", value=119, within_min=30)
        return p.model_copy(update={"hypothesis": p.hypothesis.model_copy(update={"expectation": exp})})


def test_repeat_check_uses_the_raw_expectation():
    h1 = Hypothesis(id="H1", statement="蒸汽壓不足", cause_code="steam_pressure_low", status=HypothesisStatus.REJECTED,
                    expectation=Expectation(machine_id="CURE-07", channel=Channel.TEMP, op=">=", value=121,
                                            within_min=30))
    h2 = Hypothesis(id="H2", statement="持續洩壓或感測器漂移", cause_code="steam_leak_or_sensor_drift",
                    expectation=Expectation(machine_id="CURE-07", channel=Channel.TEMP, op="<=", value=119,
                                            within_min=30))
    assert StateMachine._repeats_rejected(h2, [h1]) is None


async def test_page11_replan_with_llm_style_h2_finishes(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, commander=LLMStyleH2())

    async def sup():
        await auto_approve(approvals, 1)
        await auto_approve(approvals, 1, option="A")
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert ev.state is EventState.CLOSED, ev.outcome
    assert not any(e.payload.get("error") == "commander re-proposed a rejected hypothesis" for e in audit.tail(0, 0))
    h2 = ev.hypotheses[1]
    assert h2.cause_code == "steam_leak_or_sensor_drift" and h2.expectation is None   # verified by status
    assert h2.status is HypothesisStatus.ACTIVE                                      # never CONFIRMED
    assert sim.status["CURE-07"] is MachineStatus.OUT_OF_SERVICE


# ---------------------------------------------------------------- 11: grouped moves
def test_moves_are_grouped_per_order():
    mv = [{"order_id": "#M-201", "from_press": "CURE-09", "to_press": "CURE-08"},
          {"order_id": "#M-201", "from_press": "CURE-07", "to_press": "CURE-08"},
          {"order_id": "#M-201", "from_press": "CURE-07", "to_press": "CURE-09"},
          {"order_id": "#A-311", "from_press": "CURE-07", "to_press": "CURE-09"},
          {"order_id": "#L-301", "from_press": "CURE-10", "to_press": "CURE-10"}]
    assert moves_text(mv) == ["#M-201 ×2 → 8 號、×1 → 9 號", "#A-311 改 9 號"]


# ---------------------------------------------------------------- 8 + 9: audit anchor and key
def test_audit_missing_head_with_truncated_log_is_caught(tmp_path):
    p = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(p)
    log.append(AuditKind.SYSTEM, "x", {})
    log.append(AuditKind.APPROVAL_DECISION, "supervisor", {"decision": "reject"})
    log.append(AuditKind.TOOL_CALL, "tool:notify_teams", {"kind": "staff_move"})
    p.write_text(p.read_text().splitlines()[0] + "\n")
    os.remove(str(p) + ".head")
    log2 = JsonlAuditLog(p)
    assert log2.recovery and "anchor missing" in log2.recovery[0]["reason"]
    assert (tmp_path / "audit.jsonl.broken-1").exists()


def test_audit_external_anchor_catches_a_deleted_log(tmp_path, monkeypatch):
    monkeypatch.setenv("WARROOM_AUDIT_ANCHOR", str(tmp_path / "elsewhere" / "anchor.json"))
    (tmp_path / "elsewhere").mkdir()
    p = tmp_path / "data" / "audit.jsonl"
    log = JsonlAuditLog(p)
    log.append(AuditKind.SYSTEM, "x", {})
    log.append(AuditKind.SYSTEM, "y", {})
    os.remove(p)
    os.remove(str(p) + ".head")
    log2 = JsonlAuditLog(p)
    assert log2.recovery and log2.recovery[0]["anchor_seq"] == 2


def test_audit_key_policy(tmp_path, monkeypatch):
    monkeypatch.delenv("WARROOM_AUDIT_KEY", raising=False)
    monkeypatch.delenv("WARROOM_AUDIT_KEY_FILE", raising=False)
    assert JsonlAuditLog(tmp_path / "a.jsonl").key_source == "beside_log"
    monkeypatch.setenv("WARROOM_AUDIT_REQUIRE_KEY", "1")
    with pytest.raises(AuditKeyError):
        JsonlAuditLog(tmp_path / "b.jsonl")
    kf = tmp_path / "secret.key"
    kf.write_bytes(b"0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("WARROOM_AUDIT_KEY_FILE", str(kf))
    log = JsonlAuditLog(tmp_path / "b.jsonl")
    assert log.key_source == "file" and not (tmp_path / "b.jsonl.key").exists()

