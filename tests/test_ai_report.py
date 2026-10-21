"""Agent Nemo 異常報告 (Kyle 10-21, option A; warroom/agent/reporter.py): the local model drafts the incident report during
the repair (fake client here: model JSON, invalid JSON / ungrounded number / timeout -> the rules draft), code appends
the result line at close; the main agent's hooks (approval decided -> draft starts; CLOSED -> Event.ai_report, audit
SYSTEM {"msg": "Agent Nemo 異常報告"} as a main_agent row for the 主控台 bubble); and the mock flow of open_mill_overheat."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx

from warroom.agent.orchestrator import PRIORITY, ModelQueue, Orchestrator
from warroom.agent.reporter import (
    PRIORITY_REPORT,
    QUEUE_LABEL,
    IncidentReporter,
    final_facts,
    ground,
    known_numbers,
    report_facts,
    result_line,
    rules_draft,
)
from warroom.bus import InProcessBus
from warroom.contracts import (
    AlertCard,
    Anomaly,
    AnomalyKind,
    ApprovalDecision,
    ApprovalOption,
    ApprovalRequest,
    ApprovalStatus,
    AuditKind,
    Channel,
    DecisionKind,
    Event,
    EventState,
    Expectation,
    Hypothesis,
    RiskLevel,
    ScenarioName,
    Severity,
    ToolName,
    WsType,
)
from warroom.factory import TZ

T0 = datetime(2026, 10, 22, 14, 20, 53, tzinfo=TZ)
EID = "EV-20261022-142053-A-BIGOPEN"


# ---------------------------------------------------------------- fixtures: an open-mill event at approval time
def _event(state: EventState = EventState.EXECUTE, cls: type[Event] = Event) -> Event:
    an = Anomaly(id="AN-1", ts=T0, machine_id="A-BIGOPEN", kind=AnomalyKind.OVERHEAT, channel=Channel.TEMP, value=101.88,
                 baseline=100.05, severity=Severity.CRITICAL,
                 message="A 線大開煉機 膠溫 102°C 持續上升（+2.9°C/分），水流偏低 18 L/min（基準 40）；預測約 6 分後達 120°C 上限")
    h = Hypothesis(id="H1", statement="冷卻能力不足（水流偏低）", cause_code="cooling_insufficient",
                   expectation=Expectation(machine_id="A-BIGOPEN", channel=Channel.TEMP, op="<=", value=118.0, within_min=20.0),
                   evidence=["膠溫 101.9°C 且持續上升（上限 120°C）", "冷卻水流偏低"])
    return cls(id=EID, opened_at=T0, machine_id="A-BIGOPEN", anomaly=an, state=state, hypotheses=[h],
               goal="A 線大開煉機降溫，避免整批膠料提早硬化",
               alert_card=AlertCard(title="異常：A 線大開煉機 溫度過高", summary="膠溫 101.9°C 且持續上升（上限 120°C）；原因：冷卻能力不足。"))


def _card(decision: DecisionKind | None = DecisionKind.APPROVE) -> ApprovalRequest:
    opt = ApprovalOption(id="A", label="放慢生料投入", tool=ToolName.NOTIFY_TEAMS, recommended=True,
                         args={"teams": ["bigopen"], "kind": "slow_feed", "machine_id": "A-BIGOPEN", "message": "請放慢生料投入"})
    req = ApprovalRequest(id=f"AP-{EID}-1", event_id=EID, step_id="S4", round=1, created_at=T0 + timedelta(seconds=12),
                          risk=RiskLevel.R3_CRITICAL, title="異常：A 線大開煉機 溫度過高", reason="假設 H1：冷卻能力不足",
                          delivery_impact="不受影響（大胎 #07 仍 17:00 入硫化機）", options=[opt])
    if decision is not None:
        req = req.model_copy(update={"status": ApprovalStatus.APPROVED, "decision": ApprovalDecision(
            request_id=req.id, decision=decision, option_id="A", decided_at=T0 + timedelta(seconds=20))})
    return req


TASKS = [SimpleNamespace(event_id=EID, kind="檢修", worker_name="陳志明", title="大開煉機：A 線大開煉機冷卻水流偏低，請檢修冷卻水路",
                         status="前往中"),
         SimpleNamespace(event_id=EID, kind="放慢投料", worker_name="S05（成形組）", title="大開煉機放慢投料（現場人工調整）",
                         status="前往中")]


def _facts() -> dict:
    return report_facts(_event(), _card(), TASKS, ["SOP-OM-01 開煉機過熱處置（大開煉機溫度過高）", "SOP-CW-01 冷卻水路檢修"])


def _settings():
    return SimpleNamespace(llm_mode="llm", model="lightning-test", base_url="http://x/v1", api_key_effective="none")


class _FakeLLM:
    def __init__(self, content: str | None = None, raise_: Exception | None = None, delay: float = 0.0):
        self.content, self.raise_, self.delay, self.calls = content, raise_, delay, []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raise_:
            raise self.raise_
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))])


GOOD = {"title": "A 線大開煉機膠溫過高", "summary": "膠溫 102°C 持續上升，判斷冷卻水流偏低，主管核准放慢生料投入並派員檢修。",
        "cause": "冷卻能力不足：水流 18 L/min，低於基準 40。",
        "actions": ["自動通知維修組檢修冷卻水路", "主管核准方案 A：放慢生料投入", "派工：陳志明檢修、成形組放慢投料"]}


# ---------------------------------------------------------------- the reporter
async def test_model_json_is_the_draft_and_the_request_matches_the_commander():
    llm = _FakeLLM(json.dumps(GOOD, ensure_ascii=False))
    rep = IncidentReporter(_settings(), client=llm, server_kind="vllm")
    d = await rep.draft(_event(), _facts())
    assert d["by"] == "model" and d["model_error"] is None and not d["dropped"] and d["result_pending"] is True
    assert {k: d[k] for k in GOOD} == {**GOOD, "summary": GOOD["summary"], "actions": GOOD["actions"]}
    kw = llm.calls[0]
    assert kw["model"] == "lightning-test" and kw["temperature"] == 0.0
    assert kw["response_format"]["type"] == "json_schema"
    assert kw["response_format"]["json_schema"]["schema"]["required"] == ["title", "summary", "cause", "actions"]
    assert kw["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}          # thinking off, like the commander
    facts = json.loads(kw["messages"][1]["content"])["facts"]
    assert facts["機台"] == "A 線大開煉機" and facts["主管核准"]["方案"] == "A 放慢生料投入"
    assert facts["派工"][0].startswith("檢修 陳志明") and facts["SOP"][0].startswith("SOP-OM-01")
    text = rep.finalize(d, {"機台": "A 線大開煉機", "讀值": "膠溫", "值": "100°C", "維修完成": "14:32", "恢復正常": "14:33"})
    lines = text.split("\n")
    assert lines[0] == "【異常報告】A 線大開煉機膠溫過高" and lines[1].startswith("摘要：膠溫 102°C") and lines[2].startswith("原因：")
    assert lines[3] == "處置：自動通知維修組檢修冷卻水路；主管核准方案 A：放慢生料投入；派工：陳志明檢修、成形組放慢投料"
    assert lines[4] == "結果：A 線大開煉機膠溫回到 100°C（14:32 維修完成、14:33 恢復正常）" and len(lines) <= 6


async def test_invalid_json_falls_back_to_the_rules_draft():
    for bad in ("not json", "[1, 2]", json.dumps({"title": "x"})):
        d = await IncidentReporter(_settings(), client=_FakeLLM(bad), server_kind="vllm").draft(_event(), _facts())
        assert d["by"] == "rules" and d["model_error"], bad
        assert d["title"] == "A 線大開煉機 溫度過高"


async def test_an_ungrounded_number_is_dropped_or_the_rules_draft_is_used():
    facts = _facts()
    # one clause invents 「35 分鐘」: that clause is dropped, the rest of the model's words stay
    one = {**GOOD, "summary": "膠溫 102°C 持續上升；預計 35 分鐘修好。"}
    d = await IncidentReporter(_settings(), client=_FakeLLM(json.dumps(one, ensure_ascii=False)), server_kind="vllm").draft(
        _event(), facts)
    assert d["by"] == "model" and d["summary"] == "膠溫 102°C 持續上升" and d["dropped"] == ["35"]
    # the whole cause rests on invented numbers: the cause would be empty -> the rules draft
    all_bad = {**GOOD, "cause": "冷卻水流只剩 7 L/min，比平常低 83%。"}
    d = await IncidentReporter(_settings(), client=_FakeLLM(json.dumps(all_bad, ensure_ascii=False)), server_kind="vllm").draft(
        _event(), facts)
    assert d["by"] == "rules" and "ungrounded numbers" in d["model_error"] and set(d["dropped"]) == {"7", "83"}
    # every number of the rules draft comes from the facts
    known = known_numbers(facts)
    rules = rules_draft(facts)
    import re
    assert all(n in known for part in ("title", "summary", "cause") for n in re.findall(r"\d+(?:\.\d+)?", rules[part]))
    assert ground(rules, facts)[0] is not None
    # a line break inside a part stays one line (the report is 5 lines, the page reads 「摘要：」 per line)
    assert ground({**GOOD, "summary": "膠溫 102°C\n持續上升"}, facts)[0]["summary"] == "膠溫 102°C 持續上升"


async def test_timeout_and_unreachable_fall_back_to_the_rules_draft():
    t0 = time.perf_counter()
    d = await IncidentReporter(_settings(), client=_FakeLLM(json.dumps(GOOD), delay=5.0), server_kind="vllm",
                               timeout_s=0.1).draft(_event(), _facts())
    assert d["by"] == "rules" and "Timeout" in d["model_error"] and time.perf_counter() - t0 < 2.0
    d = await IncidentReporter(_settings(), client=_FakeLLM(raise_=ConnectionError("no route")), server_kind="vllm").draft(
        _event(), _facts())
    assert d["by"] == "rules" and "ConnectionError" in d["model_error"]


async def test_mock_mode_never_calls_a_model():
    llm = _FakeLLM(json.dumps(GOOD))
    d = await IncidentReporter(SimpleNamespace(llm_mode="mock"), client=llm).draft(_event(), _facts())
    assert d["by"] == "rules" and not llm.calls and d["model"] is None
    d = await IncidentReporter(None).draft(_event(), _facts())
    assert d["by"] == "rules"


async def test_report_calls_wait_last_in_the_queue_and_take_one_slot_at_a_time():
    rows = []
    audit = SimpleNamespace(append=lambda kind, actor, payload, event_id=None: rows.append(payload))
    q = ModelQueue(slots=3, audit=audit)
    assert PRIORITY_REPORT > PRIORITY["question"] and PRIORITY_REPORT == max(PRIORITY.values())   # last in the queue
    llm = _FakeLLM(json.dumps(GOOD, ensure_ascii=False), delay=0.3)
    rep = IncidentReporter(_settings(), client=llm, queue=q, server_kind="vllm")
    jobs = [asyncio.create_task(rep.draft(_event(), _facts())) for _ in range(3)]
    await asyncio.sleep(0.1)
    assert len(q.view()["running"]) == 1                     # one report at a time: 2 of 3 slots stay for planning
    async with q.slot(PRIORITY["machine"], "commander", "EV-X", "機台處理"):
        pass                                                 # a machine plan gets a slot at once
    out = await asyncio.gather(*jobs)
    assert all(d["by"] == "model" for d in out)
    mine = [r for r in rows if r["who"] == "reporter"]
    assert len(mine) == 3 and all(r["priority"] == PRIORITY_REPORT and r["label"] == QUEUE_LABEL and
                                  r["priority_label"] == "報告" for r in mine)


def test_result_line_is_written_by_code():
    ev = _event(EventState.CLOSED)
    ev.repaired_at = T0 + timedelta(minutes=11, seconds=5)
    ev.closed_at = T0 + timedelta(minutes=12)
    ev.outcome = "已修復：A 線大開煉機 膠溫回到 100°C（維修完成 14:31）"
    assert result_line(final_facts(ev)) == "結果：A 線大開煉機膠溫回到 100°C（14:31 維修完成、14:32 恢復正常）"

    class WithRecovery(Event):                                # the recovery ramp's Event.recovered_at (other branch)
        recovered_at: datetime | None = None
    ev2 = _event(EventState.CLOSED, WithRecovery)
    ev2.repaired_at, ev2.closed_at = ev.repaired_at, T0 + timedelta(minutes=14)
    ev2.recovered_at, ev2.outcome = T0 + timedelta(minutes=13, seconds=10), ev.outcome
    assert result_line(final_facts(ev2)).endswith("（14:31 維修完成、14:34 恢復正常）")
    ev3 = _event(EventState.CLOSED)
    ev3.closed_at, ev3.outcome = T0 + timedelta(minutes=20), "H1 冷卻能力不足（水流偏低）：通知維修"
    assert result_line(final_facts(ev3)) == "結果：H1 冷卻能力不足（水流偏低）：通知維修"


# ---------------------------------------------------------------- the hooks on the main agent
class _SM:
    def __init__(self, ev: Event):
        self.ev, self.on_open, self.on_end, self.sim = ev, None, None, None

    def get(self, eid):
        if eid != self.ev.id:
            raise KeyError(eid)
        return self.ev

    def events(self):
        return [self.ev]


class _Audit:
    def __init__(self):
        self.rows: list[SimpleNamespace] = []

    def append(self, kind, actor, payload, event_id=None):
        self.rows.append(SimpleNamespace(seq=len(self.rows) + 1, kind=kind, actor=actor, payload=payload, event_id=event_id))

    def tail(self, since=0, limit=200):
        return list(self.rows)


class _SlowReporter(IncidentReporter):
    def __init__(self, delay: float):
        super().__init__(None)
        self.delay, self.started = delay, 0

    async def draft(self, event, facts):
        self.started += 1
        await asyncio.sleep(self.delay)
        return {**(await super().draft(event, facts)), "by": "model", "model": "lightning-test"}


def _orch(ev: Event, reporter: IncidentReporter | None = None):
    sm, audit, bus = _SM(ev), _Audit(), InProcessBus()
    o = Orchestrator(sm, audit, bus=bus, board=lambda: SimpleNamespace(for_event=lambda eid: TASKS), reporter=reporter)
    o._opened(ev)
    return o, sm, audit, bus


def _close(ev: Event, sm: _SM, recovered: bool = True) -> None:
    ev.state = EventState.CLOSED
    ev.repaired_at = T0 + timedelta(seconds=40)
    if recovered:                                    # the new flow closes once the readings are back (已恢復正常)
        ev.recovered_at = T0 + timedelta(seconds=44)
    ev.closed_at = T0 + timedelta(seconds=44)
    ev.outcome = "已修復：A 線大開煉機 膠溫回到 100°C（維修完成 14:21）"
    sm.on_end(ev)


async def test_approval_starts_the_draft_and_close_delivers_the_report():
    ev = _event(EventState.VERIFY)
    o, sm, audit, bus = _orch(ev)
    o.on_decided(_card(DecisionKind.REJECT))
    assert ev.id not in o.reports.jobs                       # a rejection drafts nothing
    o.on_decided(_card())
    job = o.reports.jobs[ev.id]
    assert job.task is not None                              # approval decided -> the draft runs in the background
    await job.task
    assert ev.ai_report is None                              # nothing is shown before the machine is back to normal
    _close(ev, sm)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    rep = ev.ai_report
    assert rep is not None and rep.by == "rules" and rep.late_ms is None and rep.drafted_ms is not None
    assert rep.text.splitlines()[-1] == "結果：A 線大開煉機膠溫回到 100°C（14:21 維修完成、14:21 恢復正常）"
    assert rep.summary and rep.summary in rep.text
    ma = [r for r in audit.rows if r.actor == "main_agent" and r.event_id == ev.id]
    kinds = [r.payload.get("orch") for r in ma]
    assert kinds[-2:] == ["report", "ai_report"]            # the 「完成」 line first, then the Agent Nemo report bubble
    row = ma[-1]
    assert row.kind is AuditKind.SYSTEM and row.payload["msg"] == "Agent Nemo 異常報告" and row.payload["text"] == rep.text
    assert row.payload["by"] == "rules" and "error" not in row.payload    # (an 「error」 key would show as a warning row)
    pub = [m for m in bus.history if m.type is WsType.EVENT]
    assert pub and pub[-1].data["ai_report"]["text"] == rep.text
    sm.on_end(ev)
    await asyncio.sleep(0)
    assert sum(1 for r in audit.rows if r.payload.get("orch") == "ai_report") == 1      # delivered once


async def test_a_draft_not_ready_at_close_is_delivered_as_soon_as_it_is():
    ev = _event(EventState.VERIFY)
    slow = _SlowReporter(0.4)
    o, sm, audit, _ = _orch(ev, slow)
    o.on_decided(_card())
    await asyncio.sleep(0.05)
    _close(ev, sm)
    await asyncio.sleep(0.05)
    assert ev.ai_report is None
    for _ in range(40):
        if ev.ai_report is not None:
            break
        await asyncio.sleep(0.05)
    assert ev.ai_report is not None and ev.ai_report.by == "model" and ev.ai_report.late_ms > 200
    row = next(r for r in audit.rows if r.payload.get("orch") == "ai_report")
    assert row.payload["late_ms"] == ev.ai_report.late_ms


async def test_a_later_card_restarts_the_draft_and_a_safe_stop_drops_it():
    ev = _event(EventState.VERIFY)
    slow = _SlowReporter(5.0)
    o, sm, audit, _ = _orch(ev, slow)
    o.on_decided(_card())
    first = o.reports.jobs[ev.id].task
    o.on_decided(_card(DecisionKind.MODIFY))
    await asyncio.sleep(0.05)
    assert first.cancelled() and o.reports.jobs[ev.id].task is not first
    ev.state = EventState.SAFE_STOP
    sm.on_end(ev)
    await asyncio.sleep(0.05)
    assert ev.ai_report is None and ev.id not in o.reports.jobs
    assert not any(r.payload.get("orch") == "ai_report" for r in audit.rows)


async def test_the_recovery_ramp_holds_the_report_until_recovered_at():
    class WithRecovery(Event):
        recovered_at: datetime | None = None
    ev = _event(EventState.VERIFY, WithRecovery)
    o, sm, _, _ = _orch(ev)
    o.on_decided(_card())
    await o.reports.jobs[ev.id].task
    _close(ev, sm, recovered=False)
    await asyncio.sleep(0.3)
    assert ev.ai_report is None                              # repaired, still ramping back
    ev.recovered_at = T0 + timedelta(seconds=50)
    for _ in range(20):
        if ev.ai_report is not None:
            break
        await asyncio.sleep(0.05)
    assert ev.ai_report is not None and ev.ai_report.text.endswith("（14:21 維修完成、14:21 恢復正常）")
    assert ev.ai_report.late_ms is None                      # the draft was ready: waiting for the ramp is not 「晚」


async def test_only_machine_anomalies_get_a_report():
    ev = _event(EventState.VERIFY).model_copy(update={"source": "plant"})
    o, *_ = _orch(ev)
    o.on_decided(_card())
    assert ev.id not in o.reports.jobs


# ---------------------------------------------------------------- mock end to end: open_mill_overheat
async def test_open_mill_overheat_report_lands_with_the_close(tmp_path):
    from warroom.api import Runtime
    from warroom.api.app import create_app
    from warroom.config import Settings
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario="normal", llm_mode="mock", repair_fast_s=2.0)
    rt = Runtime(s, allow_fallback=False)
    app = create_app(rt, supervisor_pin="ai-report-pin")
    await rt.start()
    try:
        for _ in range(300):
            if rt._kb_ok is not None:
                break
            await asyncio.sleep(0.2)
        await rt.load_scenario(ScenarioName.OPEN_MILL_OVERHEAT)
        await rt.preroll()
        rt.set_speed(10.0)
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://local") as c:
            ev, closed_wall, t0 = None, None, time.monotonic()
            while time.monotonic() - t0 < 90:
                for a in (await c.get("/api/approvals", params={"status": "pending"})).json():
                    r = await c.post(f"/api/approvals/{a['id']}/decision",
                                     json={"request_id": a["id"], "decision": "approve", "option_id": a["options"][0]["id"]})
                    assert r.status_code == 200, r.text
                ev = next((e for e in rt.events() if e.machine_id == "A-BIGOPEN"), None)
                if ev is not None and ev.state in (EventState.CLOSED, EventState.SAFE_STOP):
                    closed_wall = time.monotonic()
                    break
                await asyncio.sleep(0.05)
            assert ev is not None and ev.state is EventState.CLOSED, ev and ev.outcome
            while ev.ai_report is None and time.monotonic() - closed_wall < 5:
                await asyncio.sleep(0.02)
            lag = time.monotonic() - closed_wall
            rep = ev.ai_report
            assert rep is not None and rep.by == "rules" and lag < 0.5, lag     # with the close, no extra wait
            lines = rep.text.splitlines()
            assert lines[0] == "【異常報告】A 線大開煉機 溫度過高" and len(lines) == 5
            assert lines[-1].startswith("結果：A 線大開煉機膠溫回到 100°C（") and lines[-1].endswith(" 恢復正常）")
            assert "派工：檢修 " in rep.text
            facts = rt.orch.reports.jobs[ev.id].facts                         # what the model would have been given
            assert facts["主管核准"]["方案"].startswith("A ") and facts["派工"] and facts["已執行"]
            assert any(x.startswith("SOP-OM-01") for x in facts["SOP"])        # the context SOP search, from the audit
            body = (await c.get(f"/api/events/{ev.id}")).json()
            assert body["ai_report"]["text"] == rep.text and body["ai_report"]["by"] == "rules"
        rows = [e for e in rt.audit.tail(0, 100_000) if e.event_id == ev.id]
        decided = next(e for e in rows if e.kind is AuditKind.APPROVAL_DECISION)
        ai = [e for e in rows if e.actor == "main_agent" and e.payload.get("orch") == "ai_report"]
        done = next(e for e in rows if e.actor == "main_agent" and e.payload.get("orch") == "report")
        assert len(ai) == 1 and ai[0].kind is AuditKind.SYSTEM and ai[0].payload["msg"] == "Agent Nemo 異常報告"
        assert decided.seq < done.seq < ai[0].seq
    finally:
        await rt.stop()
