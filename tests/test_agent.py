"""Agent module: audit chain, action gate, approvals, commander (mock + OpenAI-compatible over a
fake HTTP transport), state-machine failure policy and the page-2 scenario."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.agent_fakes import T0, FakeRegistry, FakeSim, FakeStore, at
from tests.test_agent_page11 import page11_anomaly, press7_temp
from warroom.agent import (
    ApprovalBrokerImpl,
    JsonlAuditLog,
    MockCommander,
    OpenAICommander,
    RuleGate,
    StateMachine,
)
from warroom.agent.approvals import ApprovalError
from warroom.agent.commander import (
    SYSTEM_PROMPT,
    approx_tokens,
    build_messages,
    extract_alert_card,
    make_commander,
    parse_plan,
    plan_schema,
)
from warroom.agent.state_machine import resolve_refs
from warroom.bus import InProcessBus
from warroom.clock import SimClock
from warroom.config import Settings
from warroom.contracts import (
    TOOLS_BY_STATE,
    WRITE_TOOLS,
    Anomaly,
    AnomalyKind,
    ApprovalDecision,
    ApprovalOption,
    ApprovalRequest,
    ApprovalStatus,
    AuditKind,
    Channel,
    CommanderRequest,
    CompoundStage,
    DecisionKind,
    EventState,
    GateVerdict,
    HypothesisStatus,
    LLMMode,
    PlanStep,
    Prediction,
    RiskLevel,
    Severity,
    ToolName,
    ToolResult,
    WsType,
)


# =====================================================================================
# audit
# =====================================================================================
def test_audit_chain_and_tamper(tmp_path):
    clock = SimClock(start=T0, manual=True)
    bus = InProcessBus(clock=clock)
    log = JsonlAuditLog(tmp_path / "a.jsonl", bus=bus, clock=clock)
    for i in range(5):
        log.append(AuditKind.TOOL_CALL, "tool:x", {"i": i, "中文": "溫度 122°C"}, event_id="EV")
    assert log.verify_chain()
    assert [e.seq for e in log.tail(0, 3)] == [3, 4, 5]
    assert [e.seq for e in log.tail(2, 2)] == [3, 4]
    assert sum(1 for m in bus.history if m.type is WsType.AUDIT) == 5

    # reopen continues the chain
    log2 = JsonlAuditLog(tmp_path / "a.jsonl", clock=clock)
    e6 = log2.append(AuditKind.SYSTEM, "x", {})
    assert e6.seq == 6 and e6.prev_hash == log.tail(0, 1)[0].hash and log2.verify_chain()

    # edit one payload value -> chain broken
    p = tmp_path / "a.jsonl"
    lines = p.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"i":2', '"i":9')
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not log2.verify_chain()


def test_audit_detects_truncation(tmp_path):
    log = JsonlAuditLog(tmp_path / "b.jsonl")
    for i in range(3):
        log.append(AuditKind.SYSTEM, "x", {"i": i})
    p = tmp_path / "b.jsonl"
    p.write_text("\n".join(p.read_text().splitlines()[:2]) + "\n")
    assert not log.verify_chain()


# =====================================================================================
# gate
# =====================================================================================
def step(tool, args, sid="S1"):
    return PlanStep(id=sid, tool=tool, args=args, purpose="t")


@pytest.mark.parametrize("tool,args,risk,verdict", [
    (ToolName.NOTIFY_TEAMS, {"teams": ["maintenance"], "kind": "maintenance", "machine_id": "A-BIGOPEN",
                             "message": "A 線大開煉機冷卻水流偏低，請檢修冷卻水路"}, RiskLevel.R1_NOTIFY, GateVerdict.ALLOW),
    (ToolName.NOTIFY_TEAMS, {"teams": ["curing"], "kind": "info", "machine_id": "CURE-07",
                             "message": "7 號機異常處置中，請留意後續通知"}, RiskLevel.R1_NOTIFY, GateVerdict.ALLOW),
    (ToolName.NOTIFY_TEAMS, {"teams": ["curing"], "kind": "overtime", "message": "m"}, RiskLevel.R2_CHANGE,
     GateVerdict.NEEDS_APPROVAL),
    (ToolName.SET_CURE_TIME, {"press_id": "CURE-07", "total_cure_h": 36.5, "reason": "r"}, RiskLevel.R2_CHANGE,
     GateVerdict.NEEDS_APPROVAL),
    (ToolName.CONFIRM_SCHEDULE, {"schedule_version": 2, "reason": "r"}, RiskLevel.R2_CHANGE,
     GateVerdict.NEEDS_APPROVAL),
    (ToolName.NOTIFY_TEAMS, {"teams": ["bigopen"], "kind": "slow_feed", "message": "m"}, RiskLevel.R3_CRITICAL,
     GateVerdict.NEEDS_APPROVAL),
    (ToolName.NOTIFY_TEAMS, {"teams": ["body"], "kind": "pause_feed", "message": "m"}, RiskLevel.R3_CRITICAL,
     GateVerdict.NEEDS_APPROVAL),
    (ToolName.NOTIFY_TEAMS, {"teams": ["side", "curing"], "kind": "staff_move", "message": "m"},
     RiskLevel.R3_CRITICAL, GateVerdict.NEEDS_APPROVAL),
    (ToolName.NOTIFY_TEAMS, {"teams": ["curing"], "kind": "machine_out", "message": "m", "machine_id": "CURE-07"},
     RiskLevel.R3_CRITICAL, GateVerdict.NEEDS_APPROVAL),
])
def test_gate_risk_levels(tool, args, risk, verdict):
    d = RuleGate().check(step(tool, args), EventState.EXECUTE, True)
    assert (d.risk, d.verdict) == (risk, verdict), d.reasons
    assert all(d.checks.values())


def test_gate_reads_are_r0_and_state_scoped():
    g = RuleGate()
    d = g.check(step(ToolName.READ_SENSORS, {"machine_ids": ["CURE-06", "CURE-08"]}), EventState.SIMULATE, False)
    assert d.verdict is GateVerdict.ALLOW and d.risk is RiskLevel.R0_READ
    # a write outside EXECUTE is out of scope -> REJECT with no risk level (logged INTERCEPTED)
    d = g.check(step(ToolName.CONFIRM_SCHEDULE, {"schedule_version": 2, "reason": "r"}), EventState.SIMULATE, True)
    assert d.verdict is GateVerdict.REJECT and d.risk is None and not d.checks["state_allowed"]
    # sop_search is not open in SIMULATE
    d = g.check(step(ToolName.SOP_SEARCH, {"query": "x"}), EventState.SIMULATE, True)
    assert d.verdict is GateVerdict.REJECT and d.risk is None


def test_gate_format_and_feasibility():
    g = RuleGate()
    ok_args = {"press_id": "CURE-07", "total_cure_h": 36.5, "reason": "r"}
    assert g.check(step(ToolName.SET_CURE_TIME, ok_args), EventState.EXECUTE, False).verdict is GateVerdict.REJECT
    for bad in ({"press_id": "CURE-99", "total_cure_h": 36.5, "reason": "r"},
                {"press_id": "A-MIX", "total_cure_h": 36.5, "reason": "r"},
                {"press_id": "CURE-07", "total_cure_h": "$S1.total_cure_h", "reason": "r"},
                {"press_id": "CURE-07", "total_cure_h": 36.5, "reason": "r", "plc_write": True},
                {"press_id": "CURE-07"}):
        d = g.check(step(ToolName.SET_CURE_TIME, bad), EventState.EXECUTE, True)
        assert d.verdict is GateVerdict.REJECT and not d.checks["format_ok"], bad
        assert d.risk is RiskLevel.R2_CHANGE      # in scope, just malformed


def test_gate_cross_team_schedule_is_r3():
    clock = SimClock(start=T0, manual=True)
    store = FakeStore(clock)
    sim = FakeSim(clock, press7_temp)
    reg = FakeRegistry(sim, store)
    data = reg._solve_schedule({})            # proposes v2 with a transfer
    s = step(ToolName.CONFIRM_SCHEDULE, {"schedule_version": data["schedule_version"], "reason": "r"})
    assert RuleGate(store=store).check(s, EventState.EXECUTE, True).risk is RiskLevel.R3_CRITICAL
    assert RuleGate().check(s, EventState.EXECUTE, True, cross_team=True).risk is RiskLevel.R3_CRITICAL
    assert RuleGate().check(s, EventState.EXECUTE, True, cross_team=False).risk is RiskLevel.R2_CHANGE


def test_no_actuator_tools_exist():
    names = {t.value for t in ToolName}
    assert not any(w in n for n in names for w in ("plc", "actuat", "write_register", "set_temp", "valve"))
    assert TOOLS_BY_STATE[EventState.EXECUTE] == WRITE_TOOLS


# =====================================================================================
# approvals
# =====================================================================================
def _req(i="AP-1", options=None):
    return ApprovalRequest(id=i, event_id="EV", created_at=T0, risk=RiskLevel.R3_CRITICAL, title="t", reason="r",
                           options=options or [
                               ApprovalOption(id="A", label="夜班加班", tool=ToolName.NOTIFY_TEAMS, args={}),
                               ApprovalOption(id="B", label="從 2 線調 1 人", tool=ToolName.NOTIFY_TEAMS, args={},
                                              recommended=True)])


async def test_approval_flow(tmp_path):
    clock = SimClock(start=T0, manual=True)
    bus = InProcessBus(clock=clock)
    audit = JsonlAuditLog(tmp_path / "a.jsonl", clock=clock)
    b = ApprovalBrokerImpl(bus=bus, audit=audit, clock=clock)
    b.request(_req())
    assert [r.id for r in b.pending()] == ["AP-1"]
    waiter = asyncio.create_task(b.wait("AP-1", timeout_s=5))
    await asyncio.sleep(0)
    with pytest.raises(ApprovalError):
        b.decide(ApprovalDecision(request_id="AP-1", decision=DecisionKind.APPROVE, option_id="Z"))
    r = b.decide(ApprovalDecision(request_id="AP-1", decision=DecisionKind.APPROVE, reason="ok"))
    assert r.decision.option_id == "B"                       # defaults to recommended
    got = await waiter
    assert got.status is ApprovalStatus.APPROVED and got.decision.decided_at == T0
    with pytest.raises(ApprovalError):
        b.decide(ApprovalDecision(request_id="AP-1", decision=DecisionKind.REJECT))
    assert [m.type for m in bus.history] == [WsType.APPROVAL_REQUEST, WsType.APPROVAL_RESOLVED]
    kinds = [e.kind for e in audit.tail(0, 0)]
    assert kinds == [AuditKind.APPROVAL_REQUEST, AuditKind.APPROVAL_DECISION]


async def test_approval_expires():
    b = ApprovalBrokerImpl()
    b.request(_req("AP-2"))
    got = await b.wait("AP-2", timeout_s=0.01)
    assert got.status is ApprovalStatus.EXPIRED and not b.pending()


# =====================================================================================
# commander
# =====================================================================================
def _req_plan(anomaly, phase="plan", rejected=(), context=None):
    return CommanderRequest(phase=phase, event_id="EV", anomaly=anomaly, allowed_tools=list(ToolName),
                            context=context or {}, rejected=list(rejected))


async def test_mock_commander_page11():
    m = MockCommander()
    plan, info = await m.plan(_req_plan(page11_anomaly(), context={"cure_job": {"size": "M", "setpoint_c": 128,
                                                                                  "cure_elapsed_h": 10}}))
    assert info.mode is LLMMode.MOCK
    h = plan.hypothesis
    assert (h.id, h.cause_code, h.expectation.machine_id, h.expectation.op, h.expectation.value,
            h.expectation.within_min) == ("H1", "steam_pressure_low", "CURE-07", ">=", 121, 30)
    assert [(s.id, s.tool) for s in plan.steps] == [
        ("S1", ToolName.CURE_FORMULA), ("S2", ToolName.QUERY_ORDERS_SHIFTS), ("S3", ToolName.SOLVE_SCHEDULE),
        ("S4", ToolName.CONFIRM_SCHEDULE), ("S4b", ToolName.SET_CURE_TIME), ("S5", ToolName.NOTIFY_TEAMS)]
    assert plan.steps[2].depends_on == ["S1", "S2"]
    assert plan.steps[4].args == {"press_id": "CURE-07", "total_cure_h": "$S1.total_cure_h",
                                  "reason": plan.steps[4].args["reason"]}       # the formula's value, by reference
    assert plan.steps[0].args["actual_temp_c"] == 122 and plan.steps[0].args["elapsed_h"] == 10
    assert plan.goal == "7 號降溫，大胎不能延誤"
    assert approx_tokens(plan.alert_card.title + plan.alert_card.summary) <= 100

    rej = h.model_copy(update={"status": HypothesisStatus.REJECTED})
    plan2, _ = await m.plan(_req_plan(page11_anomaly(), "replan", [rej],
                                      {"failed_expectation": {"observed": 119.5},
                                       "neighbours": {"CURE-06": {"temp_c_now": 128}, "CURE-07": {"temp_c_now": 119.5},
                                                      "CURE-08": {"temp_c_now": 128}},
                                       "sop": [{"title": "硫化中斷處置"}]}))
    assert plan2.hypothesis.id == "H2" and plan2.hypothesis.cause_code == "steam_leak_or_sensor_drift"
    assert plan2.rejected_hypothesis_ids == ["H1"]
    assert plan2.steps[0].args["unavailable_presses"] == ["CURE-07"]
    assert plan2.alert_card.title == "7 號機停用，重排中"
    assert any("6、8 號" in e for e in plan2.hypothesis.evidence)

    # third miss -> safe stop
    rej2 = plan2.hypothesis.model_copy(update={"status": HypothesisStatus.REJECTED})
    plan3, _ = await m.plan(_req_plan(page11_anomaly(), "replan", [rej, rej2]))
    assert plan3.safe_stop and not plan3.steps


def mill_anomaly():
    return Anomaly(id="AN-M", ts=T0, machine_id="A-BIGOPEN", kind=AnomalyKind.OVERHEAT, channel=Channel.TEMP,
                   value=112.0, baseline=100.0, severity=Severity.WARN, message="A 線大開煉機 膠溫 112°C 持續上升",
                   prediction=Prediction(machine_id="A-BIGOPEN", channel=Channel.TEMP, now_value=112,
                                         predicted_value=118, band_lo=116, band_hi=120, horizon_s=1800,
                                         trend_per_min=0.4))


async def test_mock_commander_page2():
    low_flow = {"machine": {"compound_stage": "unvulcanized"},
                "sensors": {"A-BIGOPEN": {"water_flow_lpm_now": 22.0}},
                "sensor_baseline": {"A-BIGOPEN": {"water_flow_lpm": 40.0}}}
    plan, _ = await MockCommander().plan(_req_plan(mill_anomaly(), context=low_flow))
    assert plan.hypothesis.cause_code == "cooling_insufficient"
    kinds = [s.args.get("kind") for s in plan.steps if s.tool is ToolName.NOTIFY_TEAMS]
    assert kinds == ["maintenance", "slow_feed"]
    assert "冷卻能力不足" in plan.alert_card.summary
    # flow normal (review r2 #4): no 水流偏低 claim, a generic overheating hypothesis instead
    normal = {**low_flow, "sensors": {"A-BIGOPEN": {"water_flow_lpm_now": 40.2}}}
    plan2, _ = await MockCommander().plan(_req_plan(mill_anomaly(), context=normal))
    assert plan2.hypothesis.cause_code == "overheat_unknown"
    assert not any("水流偏低" in str(s.args) for s in plan2.steps)
    assert "水流偏低" not in plan2.hypothesis.statement and "水流偏低" not in plan2.alert_card.summary


def test_prompt_prefix_is_stable():
    a = build_messages(_req_plan(page11_anomaly()))
    b = build_messages(_req_plan(mill_anomaly(), "replan"))
    assert a[0] == b[0] and a[0]["content"] == SYSTEM_PROMPT
    assert a[1] != b[1]
    assert list(plan_schema()["properties"])[0] == "alert_card"


def test_parse_plan_intercepts_unknown_tools_and_clamps_card():
    raw = {"alert_card": {"title": "7 號機溫度下降", "summary": "很長" * 200}, "goal": "g",
           "hypothesis": {"id": "H1", "statement": "s", "cause_code": "c"},
           "steps": [{"id": "S1", "tool": "plc_write", "args": {"addr": 1}, "purpose": "x"},
                     {"id": "S2", "tool": "read_sensors", "args": {"machine_ids": ["CURE-07"]}, "purpose": "y"}]}
    plan, intercepted = parse_plan("```json\n" + json.dumps(raw, ensure_ascii=False) + "\n```")
    assert [s.id for s in plan.steps] == ["S2"] and intercepted[0]["tool"] == "plc_write"
    assert approx_tokens(plan.alert_card.title + plan.alert_card.summary) <= 100
    assert extract_alert_card('{"alert_card": {"title": "a", "summary": "b"}, "goal": "') .title == "a"
    assert extract_alert_card('{"alert_card": {"title": "a", "summ') is None


def _sse(text_chunks, usage=True):
    out = []
    for i, c in enumerate(text_chunks):
        out.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                    "choices": [{"index": 0, "delta": {"content": c}, "finish_reason": None}]})
    if usage:
        out.append({"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": [],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 300, "total_tokens": 1200}})
    body = "".join(f"data: {json.dumps(o, ensure_ascii=False)}\n\n" for o in out) + "data: [DONE]\n\n"
    return body.encode()


def _llm_plan_json():
    d = MockCommander().build(_req_plan(page11_anomaly())).model_dump(mode="json")
    ordered = {"alert_card": d.pop("alert_card"), **d}
    return json.dumps(ordered, ensure_ascii=False)


@pytest.mark.parametrize("base_url,expect_guided", [("http://spark:8000/v1", True),
                                                     ("http://localhost:11434/v1", False)])
async def test_openai_commander_streams_and_constrains(base_url, expect_guided):
    seen = {}
    text = _llm_plan_json()
    chunks = [text[i:i + 40] for i in range(0, len(text), 40)]

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, content=_sse(chunks), headers={"content-type": "text/event-stream"})

    s = Settings(llm_mode=LLMMode.LLM, base_url=base_url, model="nemotron-test", llm_fallback_to_mock=False)
    cmd = OpenAICommander(s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    alerts = []
    plan, info = await cmd.plan(_req_plan(page11_anomaly()), on_alert=lambda c: alerts.append((c, len(text))))

    body = seen["body"]
    assert seen["path"].endswith("/chat/completions") and body["stream"] is True and body["temperature"] == 0
    assert body["model"] == "nemotron-test" and body["messages"][0]["content"] == SYSTEM_PROMPT
    assert body["response_format"]["type"] == "json_schema" and "guided_json" not in body
    if expect_guided:      # vLLM: reasoning off through the chat template
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
    else:                  # Ollama: reasoning_effort none
        assert body["reasoning_effort"] == "none" and body["think"] is False
    assert plan.hypothesis.id == "H1" and len(plan.steps) == 6
    assert info.valid_json and not info.fell_back_to_mock and info.ttft_ms is not None
    assert info.prompt_tokens == 900 and info.completion_tokens == 300
    assert len(alerts) == 1 and alerts[0][0].title.startswith("7 號機")


async def test_openai_commander_falls_back_to_mock_on_bad_json_or_down_server():
    def bad(request):
        return httpx.Response(200, content=_sse(["not json at all"]), headers={"content-type": "text/event-stream"})

    def down(request):
        raise httpx.ConnectError("refused")

    for handler in (bad, down):
        s = Settings(llm_mode=LLMMode.LLM, base_url="http://spark:8000/v1", llm_fallback_to_mock=True)
        cmd = OpenAICommander(s, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        plan, info = await cmd.plan(_req_plan(page11_anomaly()))
        assert info.fell_back_to_mock and not info.valid_json and plan.hypothesis.cause_code == "steam_pressure_low"


def test_make_commander():
    assert make_commander(Settings(llm_mode=LLMMode.MOCK)).mode is LLMMode.MOCK
    assert isinstance(make_commander(Settings(llm_mode=LLMMode.LLM)), OpenAICommander)


# =====================================================================================
# state machine policy
# =====================================================================================
def test_resolve_refs():
    res = {"S1": ToolResult(call_id="c", tool=ToolName.CURE_FORMULA, ok=True,
                            data={"total_cure_h": 36.5, "moves": [{"to_press": "CURE-09"}]})}
    out = resolve_refs({"a": "$S1.total_cure_h", "b": {"CURE-07": "$S1.total_cure_h"}, "c": "延長至 ${S1.total_cure_h} h",
                        "d": "$S1.moves.0.to_press", "e": "$S9.x", "f": ["$S1.total_cure_h"]}, res)
    assert out == {"a": 36.5, "b": {"CURE-07": 36.5}, "c": "延長至 36.5 h", "d": "CURE-09", "e": "$S9.x", "f": [36.5]}


def make_sm(tmp_path, temp_fn=press7_temp, fail=None, feasible=True, fell_back=False, commander=None,
            approval_timeout_s=5.0, compound=None):
    clock = SimClock(start=T0, manual=True)
    bus = InProcessBus(clock=clock)
    audit = JsonlAuditLog(tmp_path / "audit.jsonl", bus=bus, clock=clock)
    sim = FakeSim(clock, temp_fn, compound)
    store = FakeStore(clock)
    tools = FakeRegistry(sim, store, fail=fail, feasible=feasible, fell_back=fell_back)
    approvals = ApprovalBrokerImpl(bus=bus, audit=audit, clock=clock)

    async def sleep(s):
        clock.advance(s)
        await asyncio.sleep(0)

    sm = StateMachine(commander or MockCommander(), tools, RuleGate(store=store), approvals, audit, sim=sim, bus=bus,
                      clock=clock, store=store, sleep=sleep, approval_timeout_s=approval_timeout_s)
    return clock, audit, sim, store, tools, approvals, sm


async def auto_approve(approvals, n, kind=DecisionKind.APPROVE, option=None):
    for _ in range(n):
        while not approvals.pending():
            await asyncio.sleep(0.001)
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=kind, option_id=option))


def _transitions(audit):
    return [e.payload["to"] for e in audit.tail(0, 0) if e.kind is AuditKind.STATE_TRANSITION]


async def test_tool_retry_once_then_succeeds(tmp_path):
    stable = lambda m, now: 128.0 if m != "CURE-07" else 122.0
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, fail={ToolName.CURE_FORMULA: 1})
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED and ev.hypotheses[0].status is HypothesisStatus.CONFIRMED
    attempts = [c.attempt for c in tools.calls_of(ToolName.CURE_FORMULA)]
    assert attempts == [1, 2]
    assert any("重試一次" in t.text for t in ev.timeline)
    assert ev.consecutive_failures == 0


async def test_tool_fails_twice_goes_sop_path(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, fail={ToolName.CURE_FORMULA: 2})
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    assert ev.state is EventState.CLOSED
    assert ev.hypotheses[-1].cause_code == "sop_path"
    assert any("走 SOP" in t.text for t in ev.timeline)
    sop_notes = [c for c in tools.calls_of(ToolName.NOTIFY_TEAMS) if "SOP" in c.args["message"]]
    assert sop_notes and store.active == 1 and ev.consecutive_failures == 1
    assert "simulate" in _transitions(audit) and "replan" in _transitions(audit)


async def test_two_consecutive_failures_safe_stop_and_rollback(tmp_path):
    # cure formula fails 4 times: round 1 (2 attempts) -> SOP path; SOP path has no sim steps so we
    # force the second failure with an infeasible schedule on a commander that keeps re-planning.
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, feasible=False)
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    # infeasible -> replan (fail 1) -> H2 solve infeasible again (fail 2) -> SAFE_STOP
    assert ev.state is EventState.SAFE_STOP and ev.consecutive_failures == 2
    assert "維持原排程" in ev.outcome and store.active == 1
    assert not tools.calls_of(ToolName.CONFIRM_SCHEDULE)


async def test_safe_stop_keeps_the_approved_schedule(tmp_path):
    """Round 1 confirms v2 (approved), verify fails (H1), round 2 supervisor rejects -> 2 failures ->
    SAFE_STOP. The supervisor-approved v2 stays in force: rolling back to v1 would put #A-311 back on
    the press the safety core is holding (review finding 5)."""
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path)

    async def sup():
        await auto_approve(approvals, 1)
        while not approvals.pending():
            await asyncio.sleep(0.001)
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                          reason="不同意停機"))
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert ev.state is EventState.SAFE_STOP
    assert store.rollbacks == [] and store.active == 2
    assert not any(e.kind is AuditKind.ROLLBACK for e in audit.tail(0, 0))
    assert "維持主管已核准的排程 v2" in ev.outcome
    kept = next(e for e in audit.tail(0, 0) if e.payload.get("safe_stop_schedule") == "kept")
    assert kept.payload["approved_by_this_event"] == [2]


async def test_approval_timeout_safe_stop(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, approval_timeout_s=0.05)
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    assert ev.state is EventState.SAFE_STOP and "核准逾時" in ev.outcome
    assert approvals.all()[0].status is ApprovalStatus.EXPIRED


async def test_solver_fallback_shown(tmp_path):
    stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, fell_back=True)
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED
    assert any("切換備援求解器" in t.text for t in ev.timeline)


class _RogueCommander(MockCommander):
    """Plans a write in SIMULATE's place and a read that is not open in SIMULATE."""

    def build(self, req):
        plan = super().build(req)
        steps = [PlanStep(id="S1", tool=ToolName.SOP_SEARCH, args={"query": "x"}, purpose="不該在模擬階段"),
                 PlanStep(id="S2", tool=ToolName.NOTIFY_TEAMS, purpose="越權：不存在的機台",
                          args={"teams": ["curing"], "kind": "machine_out", "message": "m", "machine_id": "CURE-99"})]
        return plan.model_copy(update={"steps": steps,
                                       "hypothesis": plan.hypothesis.model_copy(update={"expectation": None})})


async def test_out_of_scope_steps_intercepted_and_logged(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, commander=_RogueCommander())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    entries = audit.tail(0, 0)
    inter = [e for e in entries if e.kind is AuditKind.INTERCEPTED]
    assert any(e.payload["tool"] == "sop_search" for e in inter)
    rejected_gate = [e for e in entries if e.kind is AuditKind.GATE and e.payload.get("tool") == "notify_teams"]
    assert rejected_gate                                       # malformed (bad machine id) write rejected
    assert not tools.calls_of(ToolName.NOTIFY_TEAMS)
    # a plan whose only write the gate refused did nothing: a miss, not a closed success
    assert ev.state is EventState.SAFE_STOP and audit.verify_chain()


async def test_tool_not_open_in_state_is_blocked(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path)
    from warroom.agent.state_machine import _Run
    from warroom.contracts import Event
    ev = Event(id="EV-x", opened_at=T0, machine_id="CURE-07", anomaly=page11_anomaly(), state=EventState.SIMULATE)
    res = await sm._call(_Run(ev=ev), EventState.SIMULATE, "S9", ToolName.NOTIFY_TEAMS,
                         {"teams": ["curing"], "kind": "info", "message": "m"})
    assert not res.ok and not tools.calls
    assert audit.tail(0, 1)[0].kind is AuditKind.INTERCEPTED


async def test_page2_open_mill_auto_then_approval(tmp_path):
    """Page 2: ✓ auto notify maintenance (R1) goes out before the supervisor approves slow feed (R3)."""
    def temp(m, now):
        if m != "A-BIGOPEN":
            return 80.0
        return 112.0 if now < at("02:16:00") else 110.0
    clock, audit, sim, store, tools, approvals, sm = make_sm(
        tmp_path, temp, compound={"A-BIGOPEN": CompoundStage.UNVULCANIZED})
    seen = {}

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        seen["notifies_before"] = [c.args["kind"] for c in tools.calls_of(ToolName.NOTIFY_TEAMS)]
        seen["req"] = approvals.pending()[0]
        clock.advance(60)
        approvals.decide(ApprovalDecision(request_id=seen["req"].id, decision=DecisionKind.APPROVE))
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(mill_anomaly()), 10)
    await t
    assert seen["notifies_before"] == ["maintenance"]
    req = seen["req"]
    assert req.risk is RiskLevel.R3_CRITICAL and [o.label for o in req.options] == ["放慢生料投入"]
    assert req.actions == ["S4 放慢生料投入 → 大開煉機組［A 線大開煉機］：A 線大開煉機膠溫偏高，請放慢生料投入直到冷卻恢復"]
    assert "已通知維修" in req.done
    assert any("膠溫 112°C 未達 120°C 停機線" in t.text for t in ev.timeline)
    assert any(t.text.startswith("✓ 已自動：通知維修") for t in ev.timeline)
    assert ev.state is EventState.CLOSED and ev.hypotheses[0].status is HypothesisStatus.CONFIRMED
    assert _transitions(audit)[:9] == ["detected", "classify", "plan", "simulate", "gate", "execute",
                                       "await_approval", "execute", "verify"]
