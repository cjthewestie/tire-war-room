"""問戰情室 (ask the war room): read-only Q&A on the event harness.

The model only picks read tools; code runs ASK_TOOLS alone, refuses and logs everything else
(write tools, solve_schedule, names off the allow-list, steps past the limit), and every call lands
in the same hash-chained audit log under an ASK-nnnn id."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from warroom.agent.ask import AskAgent, machines_in, mock_plan, ungrounded_numbers
from warroom.agent.audit import JsonlAuditLog
from warroom.api import Runtime, create_app
from warroom.clock import SimClock
from warroom.config import Settings
from warroom.contracts import (
    ASK_MAX_STEPS,
    ASK_TOOLS,
    REST_ENDPOINTS,
    WRITE_TOOLS,
    AuditKind,
    ScenarioName,
    ToolName,
)
from warroom.tools import ToolContext, build_registry

LAN_PHONE = ("192.168.10.23", 50000)


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    """Page 11 at 02:14:20: press 7 has dropped to ~122 °C and the detector has fired."""
    from warroom.detect import StreamDetector
    from warroom.schedule import FallbackScheduler, FileScheduleStore, seed_schedule
    from warroom.sim import Factory
    clock = SimClock(manual=True)
    sim = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
    det = StreamDetector(clock=clock)
    for _ in range(140):
        det.ingest(sim.step(1.0))
    sched = FallbackScheduler()
    store = FileScheduleStore(tmp_path_factory.mktemp("sched"), initial=seed_schedule(
        clock.now(), sim.orders(), sim.shifts(), sim.machines(), scheduler=sched, next_on={"CURE-07": "#A-311"}))
    ctx = ToolContext(sim=sim, detector=det, scheduler=sched, store=store, clock=clock)
    return ctx, build_registry(ctx)


@pytest.fixture
def audit(tmp_path):
    return JsonlAuditLog(tmp_path / "audit.jsonl")


class FakeLLM:
    """Stands in for AsyncOpenAI: returns the queued contents in order; an Exception is raised."""

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


def _llm_agent(*contents) -> AskAgent:
    return AskAgent(Settings(llm_mode="llm", base_url="http://x/v1", model="nemotron"), client=FakeLLM(*contents))


def _mock_agent() -> AskAgent:
    return AskAgent(Settings(llm_mode="mock"))


def _plan(*steps, intent: str | None = None, reply: str = "") -> str:
    """The model's plan. `intent` is the model's own reading (issue #195); left out, steps mean a question."""
    return json.dumps({**({"intent": intent, "reply": reply} if intent else {}),
                       "steps": [{"tool": t, "args": a} for t, a in steps], "rationale": "test"})


def _entries(audit, ask_id, kind=None):
    return [e for e in audit.tail(since_seq=0, limit=100_000)
            if e.event_id == ask_id and (kind is None or e.kind == kind)]


# ---------------------------------------------------------------- the allow-list for questions
def test_ask_tools_are_read_only():
    assert not ASK_TOOLS & WRITE_TOOLS
    assert ToolName.SOLVE_SCHEDULE not in ASK_TOOLS        # it stores a PROPOSED version
    assert ("POST", "/api/ask", "AskRequest", "AskAnswer") in REST_ENDPOINTS


@pytest.mark.parametrize("q, ids", [
    ("7 號機現在怎樣？", ["CURE-07"]),
    ("7號硫化機溫度", ["CURE-07"]),
    ("CURE-09 下一模是什麼", ["CURE-09"]),
    ("A 線大開煉機過熱怎麼處理", ["A-BIGOPEN"]),
    ("A線開煉機溫度", ["A-OPEN"]),
    ("大開煉機過熱怎麼處理", ["A-BIGOPEN"]),             # unique name, no line needed
    ("開煉機溫度", []),                                 # A and B both have one: ambiguous
    ("今晚夜班缺誰", []),
])
def test_machines_in_question(q, ids):
    assert machines_in(q) == ids


def test_mock_plan_picks_tools_by_question():
    tools = lambda q: [s.tool for s in mock_plan(q, {"abnormal": []}).steps]
    assert tools("7 號機會不會再掉？") == ["read_sensors", "predict_anomaly"]
    assert tools("今晚夜班缺誰") == ["query_orders_shifts"]
    assert tools("A 線大開煉機過熱怎麼處理") == ["read_sensors", "sop_search"]
    # no machine named: the abnormal machines from the context
    assert mock_plan("現在狀況如何", {"abnormal": ["CURE-07"]}).steps[0].args["machine_ids"] == ["CURE-07"]
    # ...but a staffing or SOP question does not drag the abnormal machines in
    assert [s.tool for s in mock_plan("今晚夜班缺誰", {"abnormal": ["CURE-07"]}).steps] == ["query_orders_shifts"]


# ---------------------------------------------------------------- mock mode, real tools
async def test_mock_answer_page11_press7(plant, audit):
    ctx, reg = plant
    a = await _mock_agent().ask("7 號機現在怎樣？", tools=reg, audit=audit, context={"abnormal": ["CURE-07"]},
                                now=ctx.clock.now())
    assert a.id == "ASK-0001" and a.mode == "mock" and not a.intercepted
    assert [s.tool for s in a.steps] == ["read_sensors"] and a.steps[0].ok
    assert "7 號機" in a.answer and "溫度 12" in a.answer       # ~122 °C after the page-11 drop
    calls = _entries(audit, a.id, AuditKind.TOOL_CALL)
    assert [e.payload["state"] for e in calls] == ["ASK"]
    assert _entries(audit, a.id, AuditKind.SYSTEM)[-1].payload["answer"] == a.answer
    assert audit.verify_chain()


async def test_change_request_gets_read_only_note(plant, audit):
    ctx, reg = plant
    a = await _mock_agent().ask("把 7 號機停掉", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert [s.tool for s in a.steps] == ["read_sensors"] and not a.intercepted
    assert a.answer.endswith("（問戰情室只能查詢，不能變更；停機、重排、調人請走事件核准流程）")


async def test_mock_answer_staffing_gap(plant, audit):
    ctx, reg = plant
    a = await _mock_agent().ask("今晚夜班缺誰？", tools=reg, audit=audit, context={"abnormal": []},
                                now=ctx.clock.now())
    assert a.steps[0].tool == "query_orders_shifts" and a.steps[0].ok
    assert "人力缺口" in a.answer


# ---------------------------------------------------------------- the model asks for too much
async def test_write_tools_and_side_effects_are_intercepted(plant, audit):
    ctx, reg = plant
    versions_before = len(ctx.store.versions())
    agent = _llm_agent(
        _plan(("read_sensors", {"machine_ids": ["CURE-07"]}),
              ("notify_teams", {"teams": ["body"], "kind": "staff_move", "message": "調人"}),
              ("confirm_schedule", {"schedule_version": 2, "reason": "x"}),
              ("solve_schedule", {"unavailable_presses": ["CURE-07"]}),
              ("plc_write", {"machine_id": "CURE-07", "value": 0})),
        "7 號機現在約 122°C。")
    a = await agent.ask("7 號機怎樣，順便處理一下", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert [s.tool for s in a.steps] == ["read_sensors"]
    assert {i["tool"]: i["reason"] for i in a.intercepted} == {
        "notify_teams": "write tool: questions are read-only",
        "confirm_schedule": "write tool: questions are read-only",
        "solve_schedule": "not open for questions (side effect)",
        "plc_write": "not on the allow-list"}
    assert len(_entries(audit, a.id, AuditKind.INTERCEPTED)) == 4
    assert {e.payload["args"].get("machine_ids", [None])[0] for e in _entries(audit, a.id, AuditKind.TOOL_CALL)} \
        == {"CURE-07"}
    assert len(ctx.store.versions()) == versions_before      # nothing proposed, nothing activated
    assert a.answer.startswith("7 號機現在約 122°C。") and "已攔下 4 個" in a.answer
    assert audit.verify_chain()


async def test_step_limit(plant, audit):
    ctx, reg = plant
    steps = [("read_sensors", {"machine_ids": [f"CURE-{i:02d}"]}) for i in range(1, 7)]
    a = await _llm_agent(_plan(*steps), "ok").ask("全部硫化機", tools=reg, audit=audit, context={},
                                                 now=ctx.clock.now())
    assert len(a.steps) == ASK_MAX_STEPS
    assert [i["reason"] for i in a.intercepted] == [f"more than {ASK_MAX_STEPS} steps"] * 2


# ---------------------------------------------------------------- model failures
async def test_invalid_plan_falls_back_to_rules(plant, audit):
    ctx, reg = plant
    a = await _llm_agent("not json").ask("7 號機現在怎樣？", tools=reg, audit=audit, context={},
                                        now=ctx.clock.now())
    assert a.fell_back_to_mock and [s.tool for s in a.steps] == ["read_sensors"]
    assert a.answer.startswith("查詢結果：")                   # template, no second model call
    plan_call = _entries(audit, a.id, AuditKind.LLM_CALL)[0].payload
    assert plan_call["fell_back_to_mock"] and plan_call["error"]


async def test_answer_failure_uses_template(plant, audit):
    ctx, reg = plant
    a = await _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07"]})), TimeoutError("slow")).ask(
        "7 號機？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.fell_back_to_mock and a.answer.startswith("查詢結果：7 號機")


async def test_plan_request_is_constrained_and_reasoning_off(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(), "查不到。")
    agent.server_kind = "vllm"
    await agent.ask("x", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    plan_kw = agent.client.calls[0]
    assert plan_kw["response_format"]["json_schema"]["name"] == "AskPlan"
    assert plan_kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert plan_kw["temperature"] == 0.0


# ---------------------------------------------------------------- API
def _settings(tmp_path, scenario="normal") -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock")


def test_api_ask_local_and_lan_down(tmp_path):
    rt = Runtime(_settings(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value), run_loop=False)
    if rt.c.tools is None:
        pytest.skip(f"tools not ready: {rt.degraded}")
    app = create_app(rt, supervisor_pin="2468")
    with TestClient(app) as local, TestClient(app, client=LAN_PHONE) as phone:
        r = local.post("/api/ask", json={"question": "今晚夜班缺誰？"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"].startswith("ASK-") and body["steps"][0]["tool"] == "query_orders_shifts"
        audit = local.get("/api/audit", params={"event_id": body["id"]}).json()
        assert {e["kind"] for e in audit} >= {"system", "llm_call", "tool_call", "tool_result"}
        # a LAN phone needs the PIN, like any supervisor request
        assert phone.post("/api/ask", json={"question": "x"}).status_code == 401
        assert phone.post("/api/ask", json={"question": "今晚夜班缺誰？"},
                          headers={"X-Supervisor-Pin": "2468"}).status_code == 200
        # LAN down: only the on-site seat may ask
        local.post("/api/sim/network", json={"network": "lan_down"})
        assert phone.post("/api/ask", json={"question": "x"},
                          headers={"X-Supervisor-Pin": "2468"}).status_code == 409
        assert local.post("/api/ask", json={"question": "今晚夜班缺誰？"}).status_code == 200
        # empty and over-long questions are refused before any model call
        assert local.post("/api/ask", json={"question": ""}).status_code == 422
        assert local.post("/api/ask", json={"question": "字" * 301}).status_code == 422


# ---------------------------------------------------------------- code checks the model's answer
def test_ungrounded_numbers():
    from warroom.contracts import AskStepResult
    r = [AskStepResult(tool="read_sensors", ok=True, summary="7 號機：溫度 122.3°C、趨勢 -0.20°C/分")]
    assert ungrounded_numbers("7 號機 122°C，趨勢 -0.20°C/分", "7 號機？", r) == []
    assert ungrounded_numbers("30 分鐘內降到 110°C", "7 號機？", r) == ["30", "110"]


async def test_invented_number_replaces_model_answer(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07"]})), "7 號機 30 分鐘後會降到 110°C。")
    a = await agent.ask("7 號機？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer.startswith("查詢結果：7 號機")                 # the results, not the invention
    ans = _entries(audit, a.id, AuditKind.LLM_CALL)[-1].payload
    assert "110" in ans["ungrounded"] and "ungrounded" in ans["error"]


async def test_change_request_never_gets_a_model_answer(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07"]}), intent="change_request"))   # no answer queued
    a = await agent.ask("把 7 號機停掉並重排", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert len(agent.client.calls) == 1                           # plan only
    assert a.answer.startswith("查詢結果：7 號機") and a.answer.endswith("請走事件核准流程）")
