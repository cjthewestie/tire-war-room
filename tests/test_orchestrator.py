"""主 agent (orchestrator), issue #54: code routing, the supervisor-task dispatch (model JSON + keyword fallback),
the model queue's priority order, report back, a safe interrupt, and page 11 unchanged under the main agent."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.agent_fakes import at
from tests.test_agent_page11 import build, page11_anomaly, supervisor
from warroom.agent.orchestrator import (DISPATCHES, PRIORITY, ROUTES, Dispatcher, ModelQueue, Orchestrator,
                                        QueuedCommander, route, rule_dispatch)
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import (AnomalyKind, ApprovalStatus, AuditKind, DecisionKind, EventState, Severity, ToolName)


# ---------------------------------------------------------------- routing
def test_routing_table():
    assert route(AnomalyKind.TEMP_DROP.value, Severity.WARN) == ("machine", PRIORITY["machine"])
    assert route(AnomalyKind.TEMP_DROP.value, Severity.CRITICAL) == ("machine", PRIORITY["safety"])
    assert route(AnomalyKind.ORDER_RUSH.value)[0] == "order"
    assert route(AnomalyKind.SCHEDULE_SLIP.value)[0] == "order"
    assert route(AnomalyKind.STAFFING_SHORT.value)[0] == "staff"
    assert route(AnomalyKind.SHIFT_START.value)[0] == "shift"
    assert route("material_short")[0] == "material"
    # every machine anomaly kind routes to the machine subagent
    for k in ("overheat", "temp_drop", "pressure_low", "water_flow_low", "steam_low", "sensor_fault", "multi_machine"):
        assert ROUTES[k] == "machine"
    assert PRIORITY["safety"] < PRIORITY["machine"] < PRIORITY["order"] == PRIORITY["material"] < PRIORITY["staff"] < PRIORITY["question"]


def test_tool_list_unchanged():
    """A dispatch is an orchestrator-level choice, never a tool: the 9 tools stay as they are."""
    assert len(ToolName) == 9 and not any(t.value.startswith("dispatch") for t in ToolName)


# ---------------------------------------------------------------- dispatch: model JSON + keyword fallback
@pytest.mark.parametrize("text,want", [
    ("9 號機今晚要保養，請安排", "dispatch_machine_task"),
    ("#A-311 可能晚交，請重排保交期", "dispatch_order_task"),
    ("碳黑快用完了，請補料", "dispatch_material_task"),
    ("夜班缺一人，請調人", "dispatch_staff_task"),
    ("幫我看一下", "dispatch_machine_task"),
])
def test_rule_dispatch(text, want):
    assert rule_dispatch(text)[0] == want


class _FakeLLM:
    def __init__(self, content=None, raise_=None):
        self.content, self.raise_, self.calls = content, raise_, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        if self.raise_:
            raise self.raise_
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


def _settings():
    return SimpleNamespace(llm_mode="llm", model="nemotron-test", base_url="http://x/v1", llm_timeout_s=10.0,
                           api_key_effective="none")


async def test_model_dispatch_json_schema():
    llm = _FakeLLM(json.dumps({"dispatch": "dispatch_order_task", "reason": "交期問題"}))
    d = await Dispatcher(_settings(), client=llm).choose("幫我處理 #S-102")
    assert d["dispatch"] == "dispatch_order_task" and d["type"] == "order" and d["by"] == "model" and not d["fell_back"]
    rf = llm.calls[0]["response_format"]["json_schema"]["schema"]
    assert rf["properties"]["dispatch"]["enum"] == list(DISPATCHES)


@pytest.mark.parametrize("llm", [_FakeLLM(raise_=TimeoutError("slow")), _FakeLLM("not json"),
                                 _FakeLLM(json.dumps({"dispatch": "open_valve", "reason": "x"}))])
async def test_model_dispatch_falls_back_to_rules(llm):
    d = await Dispatcher(_settings(), client=llm).choose("夜班缺一人")
    assert d["by"] == "rules" and d["fell_back"] and d["dispatch"] == "dispatch_staff_task" and d["error"]


# ---------------------------------------------------------------- model queue: priority order, waits audited
async def test_queue_priority_order(tmp_path):
    audit = SimpleNamespace(rows=[], append=lambda kind, actor, payload, event_id=None: audit.rows.append(payload))
    q = ModelQueue(slots=1, audit=audit)
    order: list[str] = []
    gate = asyncio.Event()

    async def first():
        async with q.slot(PRIORITY["question"], "first"):
            await gate.wait()
            order.append("first")

    async def one(name, p):
        async with q.slot(p, name):
            order.append(name)

    t0 = asyncio.create_task(first())
    await asyncio.sleep(0.01)
    tasks = [asyncio.create_task(one(n, p)) for n, p in
             [("question", PRIORITY["question"]), ("staff", PRIORITY["staff"]), ("machine", PRIORITY["machine"]),
              ("safety", PRIORITY["safety"]), ("order", PRIORITY["order"])]]
    await asyncio.sleep(0.01)
    assert [w["who"] for w in q.view()["waiting"]] == ["safety", "machine", "order", "staff", "question"]
    gate.set()
    await asyncio.gather(t0, *tasks)
    assert order == ["first", "safety", "machine", "order", "staff", "question"]
    assert all(r["orch"] == "queue" and "waited_ms" in r for r in audit.rows) and len(audit.rows) == 6


# ---------------------------------------------------------------- report back, page 11 unchanged
def _orch(sm, audit, approvals, clock, store=None, tasks=None, board=None):
    q = ModelQueue(audit=audit, clock=clock)
    sm.commander = QueuedCommander(sm.commander, q)
    return Orchestrator(sm, audit, approvals=approvals, clock=clock, queue=q,
                        tasks=(lambda: tasks) if tasks is not None else None, board=board)


def _fingerprint(ev):
    return [(t.ts.isoformat(), t.actor, t.tag, t.text) for t in ev.timeline], ev.state, ev.outcome


async def _run_p11(tmp_path, with_orch: bool):
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    o = _orch(sm, audit, approvals, clock) if with_orch else None
    sup = asyncio.create_task(supervisor(clock, approvals, [
        ("02:16:40", DecisionKind.APPROVE, None, ""), ("02:31:30", DecisionKind.APPROVE, "A", "跨組調人影響 2 線，改夜班加班")]))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    return ev, audit, o, store


async def test_page11_unchanged_under_the_main_agent(tmp_path):
    plain, _, _, _ = await _run_p11(tmp_path / "a", False)
    orch, audit, o, _ = await _run_p11(tmp_path / "b", True)
    assert _fingerprint(plain) == _fingerprint(orch)
    assert plain.state is EventState.CLOSED
    rows = [e.payload for e in audit.tail(0, 100_000) if e.kind is AuditKind.SYSTEM and e.actor == "main_agent"]
    kinds = [r["orch"] for r in rows]
    assert kinds[0] == "dispatch" and kinds[-1] == "report" and kinds.count("queue") >= 2
    d, rep = rows[0], rows[-1]
    assert d["type"] == "machine" and d["by"] == "code" and "已請子 agent 規劃：機台處理" in d["text"]
    assert "機台處理子 agent 完成" in rep["text"] and rep["writes"] and rep["approvals"] and not rep["interrupted"]
    sub = o.subagents()[0]
    assert sub["status"] == "done" and sub["round"] == 2 and sub["tools_called"] >= 4


# ---------------------------------------------------------------- interrupt
class _Board:
    def __init__(self):
        self.tasks = [SimpleNamespace(id="TK-1", event_id=None, step_id="S5", status="執行中"),
                      SimpleNamespace(id="TK-2", event_id=None, step_id="S5", status="完成")]
        self.cancelled = []

    def for_event(self, eid):
        return [t for t in self.tasks if t.event_id == eid]

    def cancel(self, eid, step_id, now):
        gone = [t.id for t in self.tasks if t.event_id == eid and t.step_id == step_id and t.status != "完成"]
        self.cancelled += gone
        return gone


async def test_interrupt_supersedes_cancels_and_keeps_schedule(tmp_path):
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    tasks: dict[str, asyncio.Task] = {}
    board = _Board()
    o = _orch(sm, audit, approvals, clock, tasks=tasks, board=lambda: board)
    a = page11_anomaly()
    tasks[a.id] = asyncio.create_task(sm.handle(a))
    while not approvals.pending():
        await asyncio.sleep(0.001)
    ev = sm.events()[0]
    for t in board.tasks:
        t.event_id = ev.id
    version_before = store.current().version
    card = approvals.pending()[0]
    seq_before = audit.tail(0, 100_000)[-1].seq
    detail = await o.interrupt(ev.id, "supervisor")
    assert ev.state is EventState.SAFE_STOP and "主管中斷" in (ev.outcome or "")
    assert approvals.get(card.id).status is ApprovalStatus.SUPERSEDED and detail["superseded_approvals"] == [card.id]
    assert detail["cancelled_tasks"] == ["TK-1"] and board.cancelled == ["TK-1"]       # finished task kept
    assert store.current().version == version_before and detail["schedule_kept"] == version_before
    after = [e for e in audit.tail(0, 100_000) if e.seq > seq_before]
    assert not [e for e in after if e.kind in (AuditKind.TOOL_CALL, AuditKind.LLM_CALL)]      # no new tool / model call
    orch_rows = [e.payload for e in after if e.actor == "main_agent"]
    assert [r["orch"] for r in orch_rows] == ["report", "interrupt"] or [r["orch"] for r in orch_rows] == ["interrupt", "report"]
    rep = next(r for r in orch_rows if r["orch"] == "report")
    assert rep["interrupted"] and "已中斷" in rep["text"]
    assert o.subagents()[0]["status"] == "interrupted"
    with pytest.raises(ValueError):
        await o.interrupt(ev.id, "supervisor")


async def test_supervisor_task_dispatch_then_report(tmp_path):
    clock, bus, audit, sim, store, tools, approvals, sm = build(tmp_path)
    o = _orch(sm, audit, approvals, clock)
    holder = {}

    def start(text, actor):
        ev = sm.open_command(text, actor)
        holder["task"] = asyncio.create_task(sm.run_command(ev.id))
        return ev

    ev, d = await o.dispatch_command("#A-311 可能晚交，請重排保交期", "supervisor", start)
    assert d["by"] == "rules" and d["type"] == "order"
    while not approvals.pending() and not holder["task"].done():
        await asyncio.sleep(0.001)
    if approvals.pending():
        approvals.decide(__import__("warroom.contracts", fromlist=["ApprovalDecision"]).ApprovalDecision(
            request_id=approvals.pending()[0].id, decision=DecisionKind.APPROVE))
    await asyncio.wait_for(holder["task"], 10)
    rows = [e.payload for e in audit.tail(0, 100_000) if e.actor == "main_agent" and e.event_id == ev.id]
    disp = next(r for r in rows if r["orch"] == "dispatch")
    assert disp["type"] == "order" and disp["dispatch"] == "dispatch_order_task" and disp["by"] == "rules"
    assert rows[-1]["orch"] == "report" and "訂單處理子 agent" in rows[-1]["text"]


# ---------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path):
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant")
    rt = Runtime(s, use_real=False, run_loop=False)
    with TestClient(create_app(rt, supervisor_pin="pin-12345678")) as c:
        yield c, rt


async def test_runtime_end_to_end_dispatch_approve_report_and_interrupt(tmp_path):
    """Rule mode on the real runtime: a supervisor task → the main agent dispatches → the subagent plans →
    approval → report back; a second task is interrupted through the API."""
    from tests.test_plan_talk import Plant, _rt
    async with Plant(_rt(tmp_path, "normal")) as p:
        r = await p.http.post("/api/command", json={"text": "#M-201 可能晚交，請重排保交期"})
        assert r.status_code == 200, r.text
        eid = r.json()["event_id"]
        assert "主 agent 已請子 agent 規劃：訂單處理" in r.json()["reply"]
        await p.until(lambda: p.pending(eid) or p.rt.state_machine.get(eid).state.value in ("closed", "safe_stop"))
        if p.pending(eid):
            d = await p.http.post(f"/api/approvals/{p.pending(eid)[0].id}/decision",
                                  json={"request_id": p.pending(eid)[0].id, "decision": "approve"})
            assert d.status_code == 200, d.text
        await p.until(lambda: any(e.actor == "main_agent" and e.payload.get("orch") == "report" for e in p.audit(eid)))
        rows = [e.payload for e in p.audit(eid) if e.actor == "main_agent"]
        assert rows[0]["orch"] == "dispatch" and rows[0]["type"] == "order"
        assert "訂單處理子 agent" in rows[-1]["text"]
        subs = (await p.http.get("/api/subagents")).json()
        me = next(x for x in subs["items"] if x["id"] == eid)
        assert me["status"] in ("done", "waiting_tasks", "running") and me["parent"] == "main" and me["type_label"] == "訂單處理"
        # interrupt a second task while it waits for approval
        r2 = await p.http.post("/api/command", json={"text": "夜班缺一人，請調人並重排"})
        e2 = r2.json()["event_id"]
        await p.until(lambda: p.pending(e2) or p.rt.state_machine.get(e2).state.value in ("closed", "safe_stop"))
        assert p.rt.state_machine.get(e2).state.value not in ("closed", "safe_stop"), p.rt.state_machine.get(e2).outcome
        if True:
            card = p.pending(e2)[0].id if p.pending(e2) else None
            x = await p.http.post(f"/api/subagents/{e2}/interrupt")
            assert x.status_code == 200, x.text
            ev2 = p.rt.state_machine.get(e2)
            assert ev2.state.value == "safe_stop" and "主管中斷" in ev2.outcome
            if card:
                assert p.rt.approvals.get(card).status.value == "superseded"
            assert (await p.http.post(f"/api/subagents/{e2}/interrupt")).status_code == 409
            st = next(x for x in (await p.http.get("/api/subagents")).json()["items"] if x["id"] == e2)
            assert st["status"] == "interrupted"


def test_api_interrupt_rules(client, tmp_path):
    c, rt = client
    assert c.post("/api/subagents/EV-NOPE/interrupt").status_code == 404
    s = Settings(audit_path=tmp_path / "b.jsonl", schedule_dir=tmp_path / "s2", qdrant_path=tmp_path / "q2")
    with TestClient(create_app(Runtime(s, use_real=False, run_loop=False), supervisor_pin="pin-12345678"),
                    client=("192.168.1.30", 5000)) as remote:
        assert remote.post("/api/subagents/EV-X/interrupt").status_code == 401


def test_api_preclass_lists_detector_rules(client):
    c, rt = client
    c.post("/api/sim/control", json={"action": "scenario", "scenario": "press7_temp_drop"})
    for _ in range(4):
        c.post("/api/sim/step", json={"seconds": 120})
    j = c.get("/api/preclass").json()
    assert {"items", "bad_data", "bad_log"} <= set(j)
    if j["items"]:                                    # the fallback plant may raise no detector anomaly
        it = j["items"][0]
        assert it["route"]["type"] == "machine"


async def test_detector_preclass_fields():
    """The real detector fills preclass: rule, checks, validation, forecast."""
    from datetime import timedelta

    from tests.agent_fakes import T0
    from warroom.contracts import SensorReading
    from warroom.detect.detector import StreamDetector
    det = StreamDetector()
    found = []
    t = T0 - timedelta(minutes=20)
    for i in range(2400):
        ts = t + timedelta(seconds=i)
        temp = 128.0 if ts < T0 else max(122.0, 128.0 - (ts - T0).total_seconds() * 0.05)
        rs = [SensorReading(ts=ts, machine_id="A-BIGOPEN", temp_c=100.0 + (2.0 if ts >= T0 else 0) * min(1, (ts - T0).total_seconds() / 60) * 4,
                            pressure_kgf_cm2=50.0, water_flow_lpm=40.0, seq=i)]
        found += det.ingest(rs)
        if found:
            break
    assert found, "no anomaly"
    pc = found[0].preclass
    assert pc and pc["rule"] and pc["label"] and pc["checks"] and pc["validation"]["range"] == "通過"
    assert pc["kind"] == found[0].kind.value


# ---------------------------------------------------------------- one input, one 主 agent (PR #58 review)
from warroom.agent.main_chat import IDENTITY_REPLY, UNCLEAR_MAIN, MainChat, rule_intent  # noqa: E402


@pytest.mark.parametrize("text,waiting,want", [
    ("妳是誰", False, "chitchat"), ("你是誰？", True, "chitchat"), ("你好", False, "chitchat"), ("謝謝", False, "chitchat"),
    ("7 號機現在怎樣？", False, "question"), ("今晚夜班缺誰？", False, "question"),
    ("#M-201 可能晚交，請重排保交期", False, "task"), ("通知維修組明早檢查 3 號機", False, "task"),
    ("9 號機今晚要保養", True, "change"), ("不要跨組調人", True, "change"), ("9 號機今晚要保養", False, "task"),
    ("嗯嗯", False, "unclear"),
])
def test_main_rule_intent(text, waiting, want):
    intent, reply = rule_intent(text, waiting)
    assert intent == want, (text, intent)
    if text in ("妳是誰", "你是誰？"):
        assert reply == IDENTITY_REPLY
    if want == "unclear":
        assert reply == UNCLEAR_MAIN and "我不確定要查什麼" not in reply


async def test_main_identity_never_goes_to_the_model():
    llm = _FakeLLM(json.dumps({"intent": "question", "reply": ""}))
    c = await MainChat(_settings(), client=llm).classify("妳是誰", False)
    assert c["intent"] == "chitchat" and c["reply"] == IDENTITY_REPLY and not llm.calls


async def test_main_model_intent_and_fallback():
    c = await MainChat(_settings(), client=_FakeLLM(json.dumps({"intent": "change", "reply": ""}))).classify("改成明天", False)
    assert c["intent"] == "task" and c["by"] == "model"          # no waiting plan: a change is a new task
    c = await MainChat(_settings(), client=_FakeLLM(raise_=TimeoutError())).classify("7 號機現在怎樣？", False)
    assert c["intent"] == "question" and c["by"] == "rules" and c["fell_back"]


async def test_main_message_api(tmp_path):
    from tests.test_plan_talk import Plant, _rt
    async with Plant(_rt(tmp_path, "normal")) as p:
        n0 = len([e for e in p.rt.audit.tail(0, 100_000) if e.kind is AuditKind.TOOL_CALL])
        r = (await p.http.post("/api/main/message", json={"text": "妳是誰"})).json()
        assert r["intent"] == "chitchat" and r["reply"] == IDENTITY_REPLY and r["ask"] is None and r["event_id"] is None
        assert len([e for e in p.rt.audit.tail(0, 100_000) if e.kind is AuditKind.TOOL_CALL]) == n0     # zero tools
        q = (await p.http.post("/api/main/message", json={"text": "7 號機現在怎樣？"})).json()
        assert q["intent"] == "question" and q["ask"] and q["ask"]["answer"] and "我不確定要查什麼" not in q["ask"]["answer"]
        t = (await p.http.post("/api/main/message", json={"text": "#M-201 可能晚交，請重排保交期"})).json()
        assert t["intent"] == "task" and t["event_id"] and "已請子 agent 規劃" in t["reply"]
        eid = t["event_id"]
        await p.until(lambda: p.pending(eid))
        ch = (await p.http.post("/api/main/message", json={"text": "不要跨組調人", "event_id": eid})).json()
        assert ch["intent"] == "change" and ch["discuss"] and ch["event_id"] == eid
        await p.until(lambda: p.pending(eid))
        card = p.pending(eid)[0]
        d = await p.http.post(f"/api/approvals/{card.id}/decision", json={"request_id": card.id, "decision": "approve", "option_id": card.options[0].id if card.options else None})
        assert d.status_code == 200, d.text
        rows = [e.payload for e in p.audit(eid) if e.actor == "main_agent" and e.payload.get("orch") == "dispatch"]
        assert [x.get("phase") for x in rows][:1] == ["plan"] and any(x.get("phase") == "execute" and "核准，派發子 agent 執行" in x["text"] for x in rows)
        await p.until(lambda: any(e.actor == "main_agent" and e.payload.get("orch") == "report" for e in p.audit(eid)))


def test_main_question_about_the_plant_while_a_plan_waits():
    assert rule_intent("今晚夜班缺誰？", True)[0] == "question"
    assert rule_intent("為什麼用 9 號機？", True)[0] == "change"
