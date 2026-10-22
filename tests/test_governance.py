"""治理層 (CJ, 2026-10-22: 「補治理」): the model usage budget and Agent Nemo's loop guard (warroom/agent/budget.py)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from tests.test_agent_page11 import page11_anomaly
from tests.test_agent_review_r3 import _plan_json
from warroom.agent import budget
from warroom.agent.audit import JsonlAuditLog
from warroom.agent.budget import EVENT_REASON, LOOP_STOP, Budget, BudgetExceeded, LoopGuard
from warroom.agent.commander import OpenAICommander
from warroom.agent.main_agent import MAX_STEPS, MainAgent
from warroom.agent.strict import failure_cause, strict_reason
from warroom.config import Settings
from warroom.contracts import AuditKind, CommanderRequest, ToolName, ToolResult


def _caps(**kw) -> SimpleNamespace:
    d = {"budget_daily_tokens": 1000, "budget_daily_model_s": 1e9, "budget_event_tokens": 500,
         "budget_conversation_tokens": 10**9}
    return SimpleNamespace(**(d | kw))


@pytest.fixture
def log(tmp_path):
    return JsonlAuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def b(log):
    prev = budget.get()
    yield budget.install(Budget(_caps(), audit=lambda: log, today=lambda: "2026-10-22"))
    budget.install(prev)


@pytest.fixture
def big(log):
    """Default caps: the loop guard tests are not about the budget."""
    prev = budget.get()
    yield budget.install(Budget(None, audit=lambda: log, today=lambda: "2026-10-22"))
    budget.install(prev)


def _rows(log, key: str) -> list[dict]:
    return [e.payload for e in log.tail(0, 0) if e.kind is AuditKind.SYSTEM and e.actor == "budget" and key in e.payload]


class _Client:
    """chat.completions.create returning a fixed usage (or none)."""

    def __init__(self, usage=(100, 20), content="好"):
        self.n = 0
        self.usage = usage
        self.content = content
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.n += 1
        u = SimpleNamespace(prompt_tokens=self.usage[0], completion_tokens=self.usage[1]) if self.usage else None
        return SimpleNamespace(usage=u, choices=[SimpleNamespace(message=SimpleNamespace(content=self.content, tool_calls=None))])


# ---------------------------------------------------------------- metering
async def test_metering_adds_up_by_kind_event_and_conversation(b, log):
    c = _Client()
    await budget.create(c, "chat", conversation="A", messages=[{"role": "user", "content": "x"}])
    await budget.create(c, "chat", conversation="A", messages=[])
    await budget.create(c, "discuss", event_id="EV-1", messages=[])
    v = b.view()
    assert v["totals"]["tokens"] == 360 and v["totals"]["calls"] == 3
    assert v["by_kind"]["chat"]["tokens_in"] == 200 and v["by_kind"]["chat"]["tokens_out"] == 40
    assert b.by_conversation["A"] == 240 and b.by_event["EV-1"] == 120
    assert len(_rows(log, "meter")) == 3 and _rows(log, "meter")[0]["meter"]["day"] == "2026-10-22"


async def test_no_usage_falls_back_to_an_estimate(b):
    await budget.create(_Client(usage=None, content="溫度正常"), "chat", messages=[{"role": "user", "content": "七號機溫度"}])
    k = b.view()["by_kind"]["chat"]
    assert k["tokens_in"] == 5 and k["tokens_out"] == 4 and k["estimated"] == 1


# ---------------------------------------------------------------- degradation order
def test_caps_trigger_each_degradation_level_in_order(b, log):
    assert b.blocked("casual") is None and b.blocked("chat") is None and b.level() == "normal"
    b.record("chat", 790, 0, 1)
    assert b.level() == "normal" and b.blocked("casual") is None
    b.record("chat", 10, 0, 1)                                         # 80 %
    assert b.level() == "casual_code"
    assert b.blocked("casual") and b.blocked("summary") and b.blocked("preplan") is None and b.blocked("chat") is None
    b.record("chat", 100, 0, 1)                                        # 90 %
    assert b.level() == "preplan_paused" and b.blocked("preplan") and b.blocked("chat") is None
    b.record("chat", 100, 0, 1)                                        # 100 %
    assert b.level() == "chat_rules"
    for k in ("chat", "discuss", "qa", "dispatch"):
        assert b.blocked(k)
    assert b.blocked("incident") is None and b.blocked("task") is None
    assert [r["cap"] for r in _rows(log, "cap")] == ["casual_code", "preplan_paused", "chat_rules", "incident_over"]
    b.record("chat", 1, 0, 1)
    assert len(_rows(log, "cap")) == 4                                 # each crossing announced once


def test_model_seconds_cap_counts_too(b):
    b.daily_seconds = 10.0
    b.record("chat", 1, 1, 9.5)
    assert b.level() == "preplan_paused" and b.blocked("preplan")


def test_per_conversation_cap(b):
    b.conversation_tokens = 50
    b.record("chat", 60, 0, 1, conversation="A")
    assert b.blocked("chat", conversation="A") and b.blocked("chat", conversation="B") is None


def test_real_day_boundary_resets(b):
    b.record("chat", 900, 0, 1)
    b._today = lambda: "2026-10-23"
    assert b.view()["totals"]["tokens"] == 0 and b.level() == "normal"


# ---------------------------------------------------------------- commander: incidents never blocked, per-event cutoff
def _stream_transport(seen: list):
    def handler(req):
        seen.append(1)
        chunks = [{"choices": [{"index": 0, "delta": {"content": _plan_json(False)}, "finish_reason": "stop"}]},
                  {"choices": [], "usage": {"prompt_tokens": 400, "completion_tokens": 100, "total_tokens": 500}}]
        head = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
        sse = "".join(f"data: {json.dumps(head | c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
    return httpx.MockTransport(handler)


def _cmd(seen, strict=False):
    return OpenAICommander(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=not strict),
                           http_client=httpx.AsyncClient(transport=_stream_transport(seen)))


def _req(phase="plan", eid="EV-7"):
    return CommanderRequest(phase=phase, event_id=eid, anomaly=page11_anomaly(), allowed_tools=[ToolName.READ_SENSORS],
                            context={}, rejected=[])


async def test_incident_is_never_blocked_by_the_daily_cap(b, log):
    b.record("chat", 5000, 0, 1)                                       # 500 % of the day
    seen: list = []
    plan, info = await _cmd(seen).plan(_req())
    assert seen and not info.fell_back_to_mock                         # the model was called
    assert b.by_event["EV-7"] == 500 and b.view()["by_kind"]["incident"]["calls"] == 1
    assert any(r["cap"] == "incident_over" for r in _rows(log, "cap"))  # warning only


async def test_per_event_cap_cuts_replan_model_calls(b, log):
    seen: list = []
    cmd = _cmd(seen)
    _plan, info = await cmd.plan(_req("plan"))                         # 500 tokens: the event is at its cap
    assert len(seen) == 1 and not info.fell_back_to_mock
    _plan, info = await cmd.plan(_req("replan"))
    assert len(seen) == 1                                              # no model call
    assert info.fell_back_to_mock and cmd.last_cause == "budget" and EVENT_REASON in cmd.last_error
    assert [r["cap"] for r in _rows(log, "cap")] == ["event"]
    _plan, info = await cmd.plan(_req("replan", eid="EV-8"))           # another event still gets the model
    assert len(seen) == 2 and not info.fell_back_to_mock


async def test_a_replayed_event_id_starts_its_count_again(b):
    # every 3 件同時異常 load replays EV-20261003-100133-B-OPEN: its new PLAN must not inherit the last run's use
    seen: list = []
    cmd = _cmd(seen)
    await cmd.plan(_req("plan"))
    await cmd.plan(_req("replan"))                                     # cut
    _plan, info = await cmd.plan(_req("plan"))                         # the scenario was reloaded
    assert b.by_event["EV-7"] == 500 and not info.fell_back_to_mock and len(seen) == 2


async def test_per_event_cap_strict_safe_stop_reason(b):
    seen: list = []
    cmd = _cmd(seen, strict=True)
    await cmd.plan(_req("plan"))
    with pytest.raises(BudgetExceeded) as e:
        await cmd.plan(_req("replan"))
    assert failure_cause(e.value) == "budget"
    assert strict_reason(e.value).startswith(EVENT_REASON)


# ---------------------------------------------------------------- restart
async def test_restart_recomputes_from_the_audit(b, log, tmp_path):
    c = _Client()
    await budget.create(c, "chat", conversation="A", messages=[])
    await budget.create(c, "discuss", event_id="EV-1", messages=[])
    b.record("chat", 700, 0, 2)                                        # crosses 80 %
    again = Budget(_caps(), today=lambda: "2026-10-22")
    assert again.load(tmp_path / "audit.jsonl") == 3
    assert again.view()["totals"] == b.view()["totals"] and again.by_event == b.by_event
    assert "casual_code" in again.crossed                              # not announced twice after a restart
    tomorrow = Budget(_caps(), today=lambda: "2026-10-23")
    assert tomorrow.load(tmp_path / "audit.jsonl") == 0 and tomorrow.tokens == 0


# ---------------------------------------------------------------- usage endpoint
def test_usage_endpoint_needs_admin(tmp_path):
    from fastapi.testclient import TestClient

    from warroom.api import Runtime, create_app
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "q")
    rt = Runtime(s, use_real=False, run_loop=False)
    rt.budget.record("chat", 1200, 300, 2.0, conversation="c1")
    app = create_app(rt, supervisor_pin="pin-1234")
    with TestClient(app, client=("8.8.8.8", 50000)) as c:
        assert c.get("/api/admin/usage", headers={"X-Supervisor-Pin": "pin-1234"}).status_code == 403
    with TestClient(app, client=("192.168.1.20", 50000)) as c:
        assert c.get("/api/admin/usage").status_code == 401
    with TestClient(app, client=("testclient", 50000)) as c:
        v = c.get("/api/admin/usage").json()
    assert v["totals"]["tokens"] == 1500 and v["by_kind"]["chat"]["calls"] == 1
    assert v["caps"]["daily_tokens"] == 3_000_000 and v["level"] == "normal"
    assert v["top_conversations"][0]["conversation"] == "c1"


# ---------------------------------------------------------------- loop guard
def _settings():
    return SimpleNamespace(llm_mode="llm", model="nemotron-test", base_url="http://x/v1", llm_timeout_s=10.0,
                           api_key_effective="none")


class _Script:
    """A mock model: each step's tool calls from `steps` (name, args); then plain text."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.offered: list[list[str]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.offered.append([t["function"]["name"] for t in kw.get("tools") or []])
        if "tools" in kw and self.steps:
            calls = [SimpleNamespace(id=f"c{len(self.offered)}{j}", function=SimpleNamespace(
                name=n, arguments=json.dumps(a, ensure_ascii=False))) for j, (n, a) in enumerate(self.steps.pop(0))]
            return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="", tool_calls=calls))])
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="查不到。", tool_calls=None))])


async def _turn(client, run_tool, text="現在有什麼在等我核准？"):
    async def propose(task):
        raise AssertionError("not a task")
    return await MainAgent(_settings(), client=client).turn(text, context={}, waiting_plan=None, run_tool=run_tool,
                                                            propose=propose, discuss=None)


async def test_joys_case_stops_at_the_third_call(big, log):
    # Spark run 10-21: recall_today with 6 different keywords, all 0 results, until MAX_STEPS. recall_today is gone from
    # this build: the same shape with read_sensors, 6 different args, all empty
    client = _Script([[("read_sensors", {"machine_ids": [f"CURE-0{i}"]})] for i in range(1, 7)])
    ran = []

    async def run_tool(name, args, call_id):
        ran.append(args)
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=True, data={"summary": {}})
    out = await _turn(client, run_tool)
    assert len(ran) == 2 and len(client.offered) == 3                  # was 7 model calls (MAX_STEPS)
    assert client.offered[2] == []                                      # the 3rd call is the text-only answer
    assert [e.payload["rule"] for e in log.tail(0, 0) if e.actor == "loop_guard"] == ["B"]
    assert out["reply"] and out["by"] == "model"


async def test_rule_a_stops_one_dry_tool_beside_productive_ones(big, log):
    # each round: the same tool with new args (nothing found) next to a productive read
    client = _Script([[("read_sensors", {"machine_ids": [f"CURE-0{i}"]}), ("query_plant", {"topic": t})]
                      for i, t in ((1, "overview"), (2, "orders"), (3, "staffing"), (4, "energy"))])
    ran = []

    async def run_tool(name, args, call_id):
        ran.append(name)
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=True, data={"summary": {}})
    agent = MainAgent(_settings(), client=client)
    n = iter(range(100))

    async def read_plant(topic, call_id, **kw):
        return {"ok": True, "data": {"topic": topic, "n": next(n)}}

    async def propose(task):
        raise AssertionError("not a task")
    await agent.turn("現在工廠整體怎樣", context={}, waiting_plan=None, run_tool=run_tool, propose=propose, discuss=None,
                     read_plant=read_plant)
    assert ran == ["read_sensors", "read_sensors"]                      # the 3rd is not run
    assert "read_sensors" not in client.offered[3]                      # and no longer offered
    stops = [e.payload for e in log.tail(0, 0) if e.actor == "loop_guard"]
    assert stops[0]["rule"] == "A" and stops[0]["calls"] == 3 and stops[0]["text"] == LOOP_STOP


async def test_rule_b_two_dry_rounds_force_the_final_round(big, log):
    # different tools, nothing new from any of them, two rounds in a row
    client = _Script([[("read_sensors", {"machine_ids": ["CURE-07"]})], [("sop_search", {"query": "x"})],
                      [("predict_anomaly", {"machine_id": "CURE-07"})]])

    async def run_tool(name, args, call_id):
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=False, error="沒有資料")
    await _turn(client, run_tool, "七號機怎麼了")
    assert client.offered[2] == []                                      # 3rd model call: text only
    assert [e.payload["rule"] for e in log.tail(0, 0) if e.actor == "loop_guard"] == ["B"]


async def test_productive_multi_tool_turn_is_not_affected(big, log):
    client = _Script([[("read_sensors", {"machine_ids": [f"CURE-0{i}"]})] for i in range(1, 6)])

    async def run_tool(name, args, call_id):
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=True,
                          data={"summary": {args["machine_ids"][0]: {"temp_c": 140}}})
    await _turn(client, run_tool, "一到五號機溫度")
    assert len(client.offered) == 6 and client.offered[4]               # 5 tool rounds, tools still offered
    assert not [e for e in log.tail(0, 0) if e.actor == "loop_guard"]


def test_loop_guard_units():
    g = LoopGuard("T")
    assert g.allow("x") and not g.saw("x", "") and g.allow("x") and not g.saw("x", '{"hits": []}')
    assert not g.allow("x") and "x" in g.stopped
    g2 = LoopGuard("T")
    g2.saw("y", "溫度 140")
    assert not g2.saw("y", "溫度 140")                                  # same content again: nothing new
    assert MAX_STEPS >= 3
