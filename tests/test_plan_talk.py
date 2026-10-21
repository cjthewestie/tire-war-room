"""Issue #10: the supervisor gives the agent tasks (下新任務) and discusses a waiting plan (討論這個計劃).

Safety model under test: the model only proposes; code turns words into constraints it can enforce,
applies them to every solve, checks the solver's result honours them, re-gates every replan, retires
the old card (it can no longer be approved) and logs everything in the hash-chained audit log."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from warroom import factory as F
from warroom.agent import ApprovalBrokerImpl
from warroom.agent.approvals import ApprovalError
from warroom.agent.discuss import PlanTalk
from warroom.agent.talk import (
    physical_request,
    rule_constraints,
    rule_intent,
    validate_constraint,
    window_for,
)
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import (
    MAX_DISCUSS_REPLANS,
    REST_ENDPOINTS,
    ApprovalDecision,
    ApprovalOption,
    ApprovalRequest,
    ApprovalStatus,
    AuditKind,
    DecisionKind,
    EventState,
    RiskLevel,
    ScenarioName,
    SolveRequest,
    ToolName,
)

NOW = datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ)      # page 11: 02:14, the night before "tonight"


# ================================================================ words -> constraints (code)
@pytest.mark.parametrize("text,intent", [
    ("9 號機今晚要保養", "change"),
    ("不要加班", "change"),
    ("不要跨組調人，用加班", "change"),
    ("選 A 方案", "change"),
    ("為什麼用 9 號機？", "question"),
    ("交期會不會延誤", "question"),
    ("把 7 號機溫度調高一點", "unsupported"),
    ("要保養", "unsupported"),                       # which machine? nothing code can enforce
])
def test_rule_intent(text, intent):
    assert rule_intent(text) == intent


def test_tonight_window_at_0214_is_the_coming_night():
    label, start, end, explicit = window_for("9 號機今晚要保養", NOW)
    assert (label, explicit) == ("今晚", True)
    assert start == datetime(2026, 10, 3, 20, 0, tzinfo=F.TZ) and end == datetime(2026, 10, 4, 5, 0, tzinfo=F.TZ)
    assert window_for("明早", NOW)[1:3] == (datetime(2026, 10, 4, 6, tzinfo=F.TZ), datetime(2026, 10, 4, 12, tzinfo=F.TZ))
    assert window_for("隨便", NOW)[3] is False


def test_rule_constraints_each_kind():
    cons, why = rule_constraints("9 號機今晚要保養", NOW)
    assert why is None and len(cons) == 1
    c = cons[0]
    assert (c.kind, c.machine_id, c.label) == ("machine_unavailable", "CURE-09", "9 號機 今晚不可用")
    assert [c.kind for c in rule_constraints("不要加班也不要跨組調人", NOW)[0]] == ["no_overtime", "no_transfer"]
    pref = rule_constraints("改用加班", NOW)[0]
    assert [(c.kind, c.option) for c in pref] == [("prefer_option", "overtime")]
    assert [(c.kind, c.option) for c in rule_constraints("選 B", NOW)[0]] == [("prefer_option", "B")]
    # a machine the press-centric solver does not schedule: honest refusal, no constraint
    cons, why = rule_constraints("A 線大開煉機今晚要保養", NOW)
    assert cons == [] and "排程只排 11 台硫化機" in why


def test_model_constraint_is_validated_by_code():
    ok, why = validate_constraint({"kind": "machine_unavailable", "machine": "CURE-09", "when": "tonight"},
                                  "9 號機今晚要保養", NOW)
    assert ok is not None and ok.start == datetime(2026, 10, 3, 20, tzinfo=F.TZ)
    # the model names a press the supervisor never mentioned -> dropped
    bad, why = validate_constraint({"kind": "machine_unavailable", "machine": "CURE-08"}, "9 號機今晚要保養", NOW)
    assert bad is None and "not named" in why
    bad, why = validate_constraint({"kind": "no_overtime"}, "9 號機今晚要保養", NOW)
    assert bad is None
    bad, why = validate_constraint({"kind": "move_machine", "machine": "CURE-09"}, "9 號機搬走", NOW)
    assert bad is None and "unknown constraint kind" in why


def test_physical_commands_are_refused_by_code():
    assert physical_request("把 7 號機搬到 B 區")
    assert physical_request("7 號機溫度設成 130 度")
    assert physical_request("通知維修組明早檢查 3 號機") is None


# ================================================================ classification: model first, rules as fallback
class FakeLLM:
    def __init__(self, *contents):
        self.contents = list(contents)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        c = self.contents.pop(0)
        if isinstance(c, Exception):
            raise c
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=c))])


def _llm_talk(*contents) -> PlanTalk:
    return PlanTalk(Settings(llm_mode="llm", base_url="http://x/v1", model="nemotron"), client=FakeLLM(*contents))


async def test_classify_uses_the_model_with_a_json_schema():
    t = _llm_talk(json.dumps({"intent": "change", "constraints": [
        {"kind": "machine_unavailable", "machine": "CURE-09", "when": "tonight"}]}))
    got = await t.classify("9 號機今晚要保養", None, NOW)
    assert got["by"] == "model" and not got["fell_back"]
    assert [c.machine_id for c in got["constraints"]] == ["CURE-09"]
    kw = t.client.calls[0]
    assert kw["response_format"]["type"] == "json_schema"


async def test_classify_falls_back_to_rules_when_the_model_fails():
    t = _llm_talk("not json at all")
    got = await t.classify("9 號機今晚要保養", None, NOW)
    assert got["by"] == "rules" and got["fell_back"] and got["error"]
    assert got["intent"] == "change" and got["constraints"][0].machine_id == "CURE-09"


async def test_classify_drops_a_hallucinated_machine_then_uses_rules():
    t = _llm_talk(json.dumps({"intent": "change", "constraints": [{"kind": "machine_unavailable", "machine": "CURE-08"}]}))
    got = await t.classify("9 號機今晚要保養", None, NOW)
    assert got["dropped"] and got["fell_back"]
    assert [c.machine_id for c in got["constraints"]] == ["CURE-09"]


async def test_classify_question_from_the_model_changes_nothing():
    t = _llm_talk(json.dumps({"intent": "question", "constraints": []}))
    got = await t.classify("為什麼用 9 號機？", None, NOW)
    assert got["intent"] == "question" and got["constraints"] == []


# ================================================================ solver honours a press blackout
@pytest.fixture(scope="module")
def page11_inputs():
    from warroom.clock import SimClock
    from warroom.schedule import FallbackScheduler, seed_schedule
    from warroom.sim import Factory
    clock = SimClock(manual=True)
    sim = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
    for _ in range(30):
        sim.step(1.0)
    prev = seed_schedule(clock.now(), sim.orders(), sim.shifts(), sim.machines(), scheduler=FallbackScheduler(),
                         next_on={"CURE-07": "#A-311"})
    return clock.now(), sim.orders(), sim.shifts(), prev


def _overlaps(sched, press, a, b):
    return [j for j in sched.jobs if j.press_id == press and not j.frozen and j.mold_in < b and a < j.demold]


@pytest.mark.parametrize("solver", ["cp_sat", "heuristic"])
def test_solver_keeps_new_tires_out_of_a_blackout(page11_inputs, solver):
    from warroom.schedule import CpSatScheduler, HeuristicScheduler
    from warroom.schedule.solver import check_schedule
    now, orders, shifts, prev = page11_inputs
    base = SolveRequest(now=now, orders=orders, shifts=shifts, previous=prev, cure_overrides_h={"CURE-07": 36.5},
                        time_limit_s=10)
    s = CpSatScheduler() if solver == "cp_sat" else HeuristicScheduler()
    before = s.solve(base)
    assert before.feasible
    # the 9 h window starts an hour before press 9's next planned tire (which one depends on the seed plan)
    nxt = min((j for j in before.schedule.jobs if j.press_id == "CURE-09" and not j.frozen), key=lambda j: j.mold_in)
    a = nxt.mold_in - timedelta(hours=1)
    b = a + timedelta(hours=9)
    assert _overlaps(before.schedule, "CURE-09", a, b), "press 9 cures a tire in that window"
    req = base.model_copy(update={"press_blackouts": {"CURE-09": [(a, b)]}})
    after = s.solve(req)
    assert after.feasible
    assert _overlaps(after.schedule, "CURE-09", a, b) == []
    assert check_schedule(after.schedule, req) == []
    # the deterministic checker catches a schedule that ignores the blackout
    assert any("保養時段" in e for e in check_schedule(before.schedule, req))


# ================================================================ the broker retires a superseded card
def test_superseded_card_cannot_be_decided(tmp_path):
    from warroom.agent import JsonlAuditLog
    audit = JsonlAuditLog(tmp_path / "a.jsonl")
    b = ApprovalBrokerImpl(audit=audit)
    req = ApprovalRequest(id="AP-1", event_id="EV-1", created_at=NOW, risk=RiskLevel.R2_CHANGE, title="t",
                          reason="r", options=[ApprovalOption(id="A", label="x", tool=ToolName.CONFIRM_SCHEDULE,
                                                              args={})])
    b.request(req)
    out = b.supersede("AP-1", by="supervisor", reason="主管討論：9 號機今晚要保養")
    assert out.status is ApprovalStatus.SUPERSEDED
    with pytest.raises(ApprovalError):
        b.decide(ApprovalDecision(request_id="AP-1", decision=DecisionKind.APPROVE))
    with pytest.raises(ApprovalError):
        b.supersede("AP-1")
    rows = [e for e in audit.tail(0, 100) if e.kind is AuditKind.APPROVAL_DECISION]
    assert rows[-1].payload["decision"] == "superseded" and audit.verify_chain()


def test_contract_endpoints():
    assert ("POST", "/api/command", "CommandRequest", "CommandReply") in REST_ENDPOINTS
    assert ("POST", "/api/events/{event_id}/discuss", "DiscussRequest", "DiscussReply") in REST_ENDPOINTS


# ================================================================ end to end on the real runtime (rule mode)
def _rt(tmp_path, scenario: str) -> Runtime:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                 qdrant_path=tmp_path / "qdrant", scenario=scenario)
    return Runtime(s, run_loop=False)


class Plant:
    """A runtime ticking in the background (sim time moves while the agent thinks) + an API client."""

    def __init__(self, rt: Runtime):
        self.rt, self._stop, self._task = rt, False, None
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt), client=("127.0.0.1", 1)),
                                      base_url="http://local")

    async def __aenter__(self):
        await self.rt.start()

        async def tick():
            while not self._stop:
                await self.rt.tick(1.0, publish=False)
                await asyncio.sleep(0.002)
        self._task = asyncio.create_task(tick())
        return self

    async def __aexit__(self, *a):
        self._stop = True
        await self._task
        await self.http.aclose()
        await self.rt.stop()

    async def until(self, cond, timeout=120.0):
        t = 0.0
        while not cond():
            await asyncio.sleep(0.02)
            t += 0.02
            assert t < timeout, "timed out"

    def pending(self, event_id=None):
        return [p for p in self.rt.approvals.pending() if event_id is None or p.event_id == event_id]

    def audit(self, event_id):
        return [e for e in self.rt.audit.tail(0, 100000) if e.event_id == event_id]


def _a311_press(rt: Runtime, card) -> str | None:
    """Where the proposed schedule behind a card puts #A-311."""
    v = int(next(a for a in card.actions if "排程 v" in a).split("排程 v")[1].split(" ")[0].split("（")[0])
    jobs = rt.c.store.get(v).schedule.jobs
    return next((j.press_id for j in jobs if j.order_id == "#A-311" and not j.frozen), None)


@pytest.mark.timeout(600)
async def test_discuss_press9_maintenance_replans_and_v2_is_approvable(tmp_path):
    async with Plant(_rt(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)) as p:
        await p.until(lambda: p.pending())
        v1 = p.pending()[0]
        ev_id = v1.event_id
        assert v1.version == 1 and _a311_press(p.rt, v1) in set(F.SMALL_ONLY_PRESSES) - {"CURE-07"}   # page 11 (dataset EV-2): #A-311 leaves press 7 for a small press
        gates_before = sum(1 for e in p.audit(ev_id) if e.kind is AuditKind.GATE)

        # a question: answered from read tools, the plan does not change
        r = await p.http.post(f"/api/events/{ev_id}/discuss", json={"text": "為什麼用 9 號機？"})
        assert r.status_code == 200, r.text
        q = r.json()
        assert q["intent"] == "question" and not q["replanned"] and "計劃未變更" in q["reply"]
        assert [x.id for x in p.pending()] == [v1.id]

        # something code cannot enforce: honest, nothing changes
        r = await p.http.post(f"/api/events/{ev_id}/discuss", json={"text": "把 7 號機溫度調高一點"})
        assert r.json()["intent"] == "unsupported" and [x.id for x in p.pending()] == [v1.id]

        # the change: press 9 is under maintenance tonight
        r = await p.http.post(f"/api/events/{ev_id}/discuss", json={"text": "9 號機今晚要保養"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["intent"] == "change" and d["replanned"] and d["superseded"] == v1.id
        v2 = p.pending(ev_id)[0]
        assert v2.id == d["approval"]["id"] and v2.version == 2 and v2.supersedes == v1.id
        assert v2.constraints == ["9 號機 今晚不可用"]
        assert _a311_press(p.rt, v2) != "CURE-09"   # #A-311 never lands on the press under maintenance
        assert p.rt.approvals.get(v1.id).status is ApprovalStatus.SUPERSEDED

        # the superseded card cannot be approved; v2 can
        r = await p.http.post(f"/api/approvals/{v1.id}/decision",
                              json={"request_id": v1.id, "decision": "approve", "via": "dashboard"})
        assert r.status_code == 409, r.text
        r = await p.http.post(f"/api/approvals/{v2.id}/decision",
                              json={"request_id": v2.id, "decision": "approve", "option_id": "A", "via": "dashboard"})
        assert r.status_code == 200 and r.json()["status"] == "approved"
        ev = p.rt.state_machine.get(ev_id)
        await p.until(lambda: ev.state in (EventState.VERIFY, EventState.CLOSED, EventState.REPLAN))
        assert ev.replans == 0 and ev.discuss_replans == 1       # a discussion is not a failure

        rows = p.audit(ev_id)
        kinds = [(e.kind, e.payload) for e in rows]
        assert any(k is AuditKind.APPROVAL_DECISION and pl.get("decision") == "superseded" for k, pl in kinds)
        assert any(k is AuditKind.SYSTEM and "constraints_added" in pl for k, pl in kinds)
        assert any(k is AuditKind.SYSTEM and pl.get("constraints_applied") for k, pl in kinds)
        assert any(k is AuditKind.SYSTEM and pl.get("constraint_check") and pl.get("ok") for k, pl in kinds)
        assert any(k is AuditKind.SYSTEM and pl.get("card_version") == 2 for k, pl in kinds)
        assert any(k is AuditKind.SYSTEM and "discuss_reply" in pl for k, pl in kinds)
        assert any(k is AuditKind.SYSTEM and "plan_summary" in pl for k, pl in kinds)
        # the gate checked the replanned writes again
        assert sum(1 for e in rows if e.kind is AuditKind.GATE) > gates_before
        assert p.rt.audit.verify_chain()


@pytest.mark.timeout(900)
async def test_each_constraint_is_honoured_and_the_cap_holds(tmp_path):
    async with Plant(_rt(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)) as p:
        await p.until(lambda: p.pending())
        ev_id = p.pending()[0].event_id

        async def say(text):
            r = await p.http.post(f"/api/events/{ev_id}/discuss", json={"text": text})
            assert r.status_code == 200, r.text
            return r.json()
        d = await say("不要跨組調人")
        card = p.pending(ev_id)[0]
        assert d["replanned"] and card.version == 2
        assert all(o.args.get("kind") != "staff_move" for o in card.options)        # no transfer offered
        d = await say("選 A")       # prefer option A
        card = p.pending(ev_id)[0]
        assert card.version == 3 and next(o for o in card.options if o.recommended).id == "A"
        d = await say("9 號機今晚要保養")
        card = p.pending(ev_id)[0]
        assert card.version == 4 and _a311_press(p.rt, card) != "CURE-09"
        assert set(card.constraints) >= {"不跨組調人", "9 號機 今晚不可用"}
        # cap: the agent keeps the last valid plan and says so
        d = await say("10 號機明天要保養")
        assert d["intent"] == "capped" and not d["replanned"] and f"v{card.version}" in d["reply"]
        assert [x.id for x in p.pending(ev_id)] == [card.id]
        ev = p.rt.state_machine.get(ev_id)
        assert ev.discuss_replans == MAX_DISCUSS_REPLANS and ev.replans == 0


@pytest.mark.timeout(600)
async def test_command_opens_an_event_through_gate_approval_and_audit(tmp_path):
    async with Plant(_rt(tmp_path, ScenarioName.NORMAL.value)) as p:
        r = await p.http.post("/api/command", json={"text": "通知維修組明早檢查 3 號機"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["accepted"]
        ev = p.rt.state_machine.get(body["event_id"])
        assert ev.source == "supervisor" and ev.command == "通知維修組明早檢查 3 號機" and ev.machine_id == "CURE-03"
        await p.until(lambda: p.pending(ev.id))
        card = p.pending(ev.id)[0]
        assert card.risk is RiskLevel.R3_CRITICAL           # free-text notice: the supervisor approves it
        assert "主管指令" in card.reason
        # a typed task is not a machine alarm
        assert next(t for t in p.rt.machine_tiles() if t.machine_id == "CURE-03").status.value != "alarm"
        r = await p.http.post(f"/api/approvals/{card.id}/decision",
                              json={"request_id": card.id, "decision": "approve", "via": "dashboard"})
        assert r.status_code == 200
        await p.until(lambda: ev.state is EventState.CLOSED)
        assert "已通知維修" in ev.outcome
        rows = p.audit(ev.id)
        cmd = next(e for e in rows if e.kind is AuditKind.SYSTEM and "command" in e.payload)
        assert cmd.payload["command"] == "通知維修組明早檢查 3 號機" and cmd.payload["source"] == "supervisor"
        assert cmd.actor == "supervisor"
        states = [e.payload["to"] for e in rows if e.kind is AuditKind.STATE_TRANSITION]
        assert states[:4] == ["detected", "classify", "plan", "simulate"] and "await_approval" in states
        assert states[-1] == "closed"
        assert any(e.kind is AuditKind.TOOL_CALL and e.actor == "tool:notify_teams" for e in rows)
        assert any(e.kind is AuditKind.GATE for e in rows)
        assert p.rt.audit.verify_chain()


@pytest.mark.timeout(300)
async def test_command_no_tool_can_do_is_explained_never_invented(tmp_path):
    async with Plant(_rt(tmp_path, ScenarioName.NORMAL.value)) as p:
        r = await p.http.post("/api/command", json={"text": "把 7 號機搬到 B 區"})
        body = r.json()
        assert r.status_code == 200 and body["accepted"] is False and "沒有" in body["reply"]
        ev = p.rt.state_machine.get(body["event_id"])
        await p.until(lambda: ev.state is EventState.CLOSED)
        assert ev.outcome.startswith("無法執行")
        rows = p.audit(ev.id)
        assert not any(e.kind in (AuditKind.TOOL_CALL, AuditKind.LLM_CALL) for e in rows)
        assert any(e.kind is AuditKind.SYSTEM and e.payload.get("command_refused") for e in rows)
        assert p.pending() == []


async def test_discuss_needs_the_supervisor_session(tmp_path):
    rt = _rt(tmp_path, ScenarioName.NORMAL.value)
    app = create_app(rt, supervisor_pin="12345678")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.168.10.23", 5)),
                                 base_url="http://lan") as c:
        r = await c.post("/api/command", json={"text": "通知維修組明早檢查 3 號機"})
        assert r.status_code == 401
        r = await c.post("/api/events/EV-x/discuss", json={"text": "9 號機今晚要保養"})
        assert r.status_code == 401


def test_discussion_window_survives_into_solve_args():
    """_apply_constraints writes code-built args; the gate's format check accepts them."""
    from warroom.agent.gate import RuleGate
    from warroom.contracts import PlanStep
    cons, _ = rule_constraints("9 號機今晚要保養，不要加班", NOW)
    blk = {c.machine_id: [[c.start.isoformat(), c.end.isoformat()]] for c in cons if c.machine_id}
    step = PlanStep(id="S3", tool=ToolName.SOLVE_SCHEDULE, purpose="x",
                    args={"press_blackouts": blk, "allow_overtime": False})
    gd = RuleGate().check(step, EventState.SIMULATE, True)
    assert gd.checks["format_ok"], gd.reasons
    bad = step.model_copy(update={"args": {"press_blackouts": {"A-BIGOPEN": blk["CURE-09"]}}})
    assert not RuleGate().check(bad, EventState.SIMULATE, True).checks["format_ok"]
    assert timedelta(hours=9) == cons[0].end - cons[0].start


# ================================================================ small talk / unclear: converse, never guess a machine
from warroom.agent.ask import AskAgent, COLD_REPLY, HELLO_REPLY, THANKS_REPLY, UNCLEAR_REPLY  # noqa: E402


class CountingTools:
    def __init__(self):
        self.calls = []

    async def acall(self, call):
        self.calls.append(call)
        from warroom.contracts import ToolResult
        return ToolResult(call_id=call.id, tool=call.tool, ok=True, data={"summary": {}})


def _ask_agent(mode, *contents):
    if mode == "rules":
        return AskAgent(Settings(llm_mode="mock"))
    return AskAgent(Settings(llm_mode="llm", base_url="http://x/v1", model="nemotron"), client=FakeLLM(*contents))


@pytest.mark.parametrize("mode", ["rules", "llm"])
@pytest.mark.parametrize("text,reply", [("哈摟", HELLO_REPLY), ("你好", HELLO_REPLY), ("謝謝", THANKS_REPLY)])
async def test_ask_small_talk_gets_a_reply_and_no_tool_call(tmp_path, mode, text, reply):
    from warroom.agent import JsonlAuditLog
    audit, tools = JsonlAuditLog(tmp_path / "a.jsonl"), CountingTools()
    agent = _ask_agent(mode)            # the model is never called: an empty FakeLLM would raise
    a = await agent.ask(text, tools=tools, audit=audit, context={"abnormal": ["A-BIGOPEN"]}, now=NOW)
    assert a.intent == "chitchat" and a.answer == reply and a.steps == [] and tools.calls == []
    rows = audit.tail(0, 50)
    assert rows[-1].payload["intent"] == "chitchat" and rows[-1].payload["tool_calls"] == 0
    assert not any(e.kind is AuditKind.TOOL_CALL for e in rows)


async def test_ask_gibberish_rules_never_guess_a_machine(tmp_path):
    from warroom.agent import JsonlAuditLog
    audit, tools = JsonlAuditLog(tmp_path / "a.jsonl"), CountingTools()
    a = await _ask_agent("rules").ask("asdkjh qwe", tools=tools, audit=audit, context={"abnormal": ["A-BIGOPEN"]}, now=NOW)
    assert a.intent == "unclear" and a.answer == UNCLEAR_REPLY and tools.calls == []


async def test_ask_gibberish_model_empty_plan_is_unclear(tmp_path):
    from warroom.agent import JsonlAuditLog
    audit, tools = JsonlAuditLog(tmp_path / "a.jsonl"), CountingTools()
    agent = _ask_agent("llm", json.dumps({"steps": []}))
    a = await agent.ask("asdkjh qwe", tools=tools, audit=audit, context={"abnormal": ["A-BIGOPEN"]}, now=NOW)
    assert a.intent == "unclear" and tools.calls == [] and not a.fell_back_to_mock


async def test_ask_cold_model_says_wait_instead_of_guessing(tmp_path):
    from warroom.agent import JsonlAuditLog
    audit, tools = JsonlAuditLog(tmp_path / "a.jsonl"), CountingTools()
    agent = _ask_agent("llm", TimeoutError("Request timed out."))
    a = await agent.ask("嗯嗯那個", tools=tools, audit=audit, context={"abnormal": ["A-BIGOPEN"]}, now=NOW)
    assert a.intent == "model_cold" and a.answer == COLD_REPLY and tools.calls == [] and a.fell_back_to_mock
    # a clear question still gets answered by the rules, and says the model was loading
    agent = _ask_agent("llm", TimeoutError("Request timed out."))
    a = await agent.ask("7 號機現在怎樣？", tools=tools, audit=audit, context={}, now=NOW)
    assert a.intent == "query" and tools.calls and "模型還在載入" in a.answer


@pytest.mark.parametrize("mode", ["rules", "llm"])
@pytest.mark.parametrize("text", ["哈摟", "你好", "謝謝"])
async def test_discuss_small_talk_changes_nothing(mode, text):
    t = PlanTalk(Settings(llm_mode="mock")) if mode == "rules" else _llm_talk()   # no model call
    got = await t.classify(text, None, NOW)
    assert got["intent"] == "chitchat" and got["constraints"] == []


async def test_discuss_gibberish_is_unclear_in_both_modes():
    assert (await PlanTalk(Settings(llm_mode="mock")).classify("asdkjh qwe", None, NOW))["intent"] == "unclear"
    got = await _llm_talk(json.dumps({"intent": "other", "constraints": []})).classify("asdkjh qwe", None, NOW)
    assert got["intent"] == "unclear" and got["by"] == "model"
    got = await _llm_talk("broken").classify("asdkjh qwe", None, NOW)
    assert got["intent"] == "unclear" and got["fell_back"]


async def test_model_misreading_an_explicit_change_is_overruled_by_code():
    """Seen live with Nemotron: 「9 號機今晚要保養」 classified as chitchat. Code decides: the rules read an
    explicit, enforceable change, so the change goes through (marked as a fallback)."""
    for wrong in ("chitchat", "question", "other"):
        got = await _llm_talk(json.dumps({"intent": wrong, "constraints": []})).classify("9 號機今晚要保養", None, NOW)
        assert got["intent"] == "change" and got["fell_back"] and got["by"] == "rules", wrong
        assert [c.machine_id for c in got["constraints"]] == ["CURE-09"]
        assert wrong in got["error"]
