"""Commander cooldown breaker (warroom/agent/breaker.py; research report section 5, proposal 5).

Consecutive model timeouts open the breaker: the next commander call gets the rule plan at once without waiting for
a ModelQueue slot or the model (fallback_reason="cooldown"), --strict ends with llm_unavailable instead, and one
1-token probe closes it again after the cooldown."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_agent import make_sm
from tests.test_agent_page11 import page11_anomaly
from tests.test_agent_review_r3 import _plan_json
from warroom.agent import budget
from warroom.agent.audit import JsonlAuditLog
from warroom.agent.breaker import BreakerState, ModelBreaker, counts_as_unavailable
from warroom.agent.budget import Budget
from warroom.agent.commander import OpenAICommander
from warroom.agent.orchestrator import ModelQueue, QueuedCommander
from warroom.agent.strict import LLMUnavailable, failure_cause, strict_reason
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import AuditKind, CommanderRequest, EventState, ToolName


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Server:
    """MockTransport for an OpenAI-compatible server. ``hang`` = every request times out (a stuck vLLM).
    Streams (commander plans) and non-stream requests (the breaker's probe) are counted apart."""

    def __init__(self, hang: bool = True):
        self.hang = hang
        self.plans = 0
        self.probes: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        if body.get("stream"):
            self.plans += 1
        else:
            self.probes.append(body)
        if self.hang:
            raise httpx.ReadTimeout("timed out", request=req)
        if not body.get("stream"):
            return httpx.Response(200, json={"id": "p", "object": "chat.completion", "created": 0, "model": "m",
                                             "choices": [{"index": 0, "finish_reason": "length",
                                                          "message": {"role": "assistant", "content": "o"}}],
                                             "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                                       "total_tokens": 2}})
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": _plan_json(False)}, "finish_reason": "stop"}]}
        sse = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture(autouse=True)
def _budget(tmp_path):
    """Default caps, fresh counts: these tests are not about the budget."""
    prev = budget.get()
    log = JsonlAuditLog(tmp_path / "budget-audit.jsonl")
    budget.install(Budget(None, audit=lambda: log, today=lambda: "2026-11-03"))
    yield
    budget.install(prev)


def _cmd(server: _Server, strict: bool = False, clock: _Clock | None = None, **kw) -> OpenAICommander:
    cmd = OpenAICommander(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=not strict, **kw),
                          http_client=server.client())
    if clock is not None:
        cmd.breaker._clock = clock
    return cmd


def _req(eid: str, phase: str = "plan") -> CommanderRequest:
    return CommanderRequest(phase=phase, event_id=eid, anomaly=page11_anomaly(), allowed_tools=[ToolName.READ_SENSORS],
                            context={}, rejected=[])


def _llm_calls(log) -> list[dict]:
    return [e.payload for e in log.tail(0, 0) if e.kind is AuditKind.LLM_CALL and e.actor == "commander"]


# ---------------------------------------------------------------- the state machine on its own
def test_breaker_opens_after_n_unavailable_failures_and_half_opens_after_cooldown():
    clk = _Clock()
    br = ModelBreaker(fails=2, cooldown_s=120, clock=clk)
    br.record_failure(TimeoutError("slow"))
    assert br.state is BreakerState.CLOSED and br.consecutive == 1
    br.record_failure(TimeoutError("slow"))
    assert br.state is BreakerState.OPEN and br.trips == 1 and br.view()["status"] == "degraded"
    clk.t += 119
    assert br.state is BreakerState.OPEN and 0 < br.remaining_s() <= 1
    clk.t += 1
    assert br.state is BreakerState.HALF_OPEN and br.view()["status"] == "ok"


def test_output_errors_reset_the_count_and_never_open():
    br = ModelBreaker(fails=2)
    br.record_failure(TimeoutError("slow"))
    br.record_failure(ValueError("bad json"))           # the server answered: not an availability failure
    br.record_failure(TimeoutError("slow"))
    assert br.state is BreakerState.CLOSED and br.consecutive == 1
    assert not counts_as_unavailable(ValueError("x")) and counts_as_unavailable(ConnectionError("refused"))


def test_disabled_breaker_always_admits():
    br = ModelBreaker(fails=0)
    for _ in range(5):
        br.record_failure(TimeoutError("slow"))
    assert asyncio.run(br.admit(None)) is True


def test_strict_reason_for_llm_unavailable():
    e = LLMUnavailable("cooldown")
    assert failure_cause(e) == "llm_unavailable"
    assert strict_reason(e) == "模型連續逾時，冷卻中暫停呼叫，未產生計劃（strict 模式不使用規則備援）"


# ---------------------------------------------------------------- acceptance: timeouts -> 3rd event at once
async def test_third_event_gets_the_rule_plan_within_1s_without_sending():
    srv = _Server(hang=True)
    cmd = _cmd(srv)
    qc = QueuedCommander(cmd, ModelQueue(slots=1))
    for eid in ("EV-1", "EV-2"):
        _plan, info = await qc.plan(_req(eid))
        assert info.fell_back_to_mock and cmd.last_cause == "timeout"
        assert qc.breaker_audit(eid)["breaker_state"] in ("closed", "open")
    assert srv.plans == 2 and cmd.breaker.state is BreakerState.OPEN

    t0 = time.perf_counter()
    plan, info = await qc.plan(_req("EV-3"))
    assert time.perf_counter() - t0 < 1.0
    assert srv.plans == 2 and not srv.probes                       # nothing sent: no plan, no probe while OPEN
    assert info.fell_back_to_mock and plan.steps                   # the rule plan
    assert qc.breaker_audit("EV-3") == {"breaker_state": "open", "fallback_reason": "cooldown", "cause": "cooldown",
                                        "error": cmd.last_error}
    assert cmd.last_cause == "cooldown" and cmd.last_raw == "" and cmd.last_intercepted == []


async def test_cooldown_skips_the_model_queue():
    """A held slot (a hung call) does not delay a cooldown rule plan: the breaker is checked before queueing."""
    srv = _Server(hang=True)
    cmd = _cmd(srv)
    q = ModelQueue(slots=1)
    qc = QueuedCommander(cmd, q)
    cmd.breaker.record_failure(TimeoutError("a"))
    cmd.breaker.record_failure(TimeoutError("b"))
    held = asyncio.Event()

    async def hold():
        async with q.slot(0, "other"):
            held.set()
            await asyncio.sleep(5)
    task = asyncio.create_task(hold())
    await held.wait()
    try:
        _plan, info = await asyncio.wait_for(qc.plan(_req("EV-9")), 1.0)
        assert info.fell_back_to_mock
    finally:
        task.cancel()


async def test_cooldown_is_audited_through_the_state_machine(tmp_path):
    srv = _Server(hang=True)
    cmd = _cmd(srv)
    cmd.breaker.record_failure(TimeoutError("a"))
    cmd.breaker.record_failure(TimeoutError("b"))
    *_, _approvals, sm = make_sm(tmp_path, commander=QueuedCommander(cmd, ModelQueue(slots=1)), approval_timeout_s=0.01)
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    calls = _llm_calls(sm.audit)
    assert calls and srv.plans == 0
    first = calls[0]
    assert first["plan_source"] == "mock_fallback" and first["fallback_reason"] == "cooldown"
    assert first["breaker_state"] == "open" and first["cause"] == "cooldown" and first["fallback_steps"]
    assert first["raw_chars"] == 0


async def test_ordinary_fallback_records_its_own_reason(tmp_path):
    srv = _Server(hang=True)
    cmd = _cmd(srv, llm_breaker_fails=5)
    *_, _approvals, sm = make_sm(tmp_path, commander=QueuedCommander(cmd, ModelQueue(slots=1)), approval_timeout_s=0.01)
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    first = _llm_calls(sm.audit)[0]
    assert first["plan_source"] == "mock_fallback" and first["fallback_reason"] == "timeout"
    assert first["breaker_state"] == "closed"


# ---------------------------------------------------------------- probe closes it again
async def test_probe_after_cooldown_closes_the_breaker():
    clk = _Clock()
    srv = _Server(hang=True)
    cmd = _cmd(srv, clock=clk)
    qc = QueuedCommander(cmd, ModelQueue(slots=1))
    await qc.plan(_req("EV-1"))
    await qc.plan(_req("EV-2"))
    assert cmd.breaker.state is BreakerState.OPEN

    clk.t += 121                                       # cooldown over, but the server still hangs
    _plan, info = await qc.plan(_req("EV-3"))
    assert len(srv.probes) == 1 and srv.plans == 2     # the probe failed: no real event was sent
    assert cmd.breaker.state is BreakerState.OPEN and info.fell_back_to_mock
    assert cmd.breaker.last_probe["ok"] is False

    srv.hang = False                                   # the server recovers
    clk.t += 121
    _plan, info = await qc.plan(_req("EV-4"))
    probe = srv.probes[-1]
    assert probe["max_tokens"] == 1 and not probe.get("stream")         # a 1-token completion, not GET /models
    assert cmd.breaker.state is BreakerState.CLOSED and cmd.breaker.last_probe["ok"] is True
    assert srv.plans == 3 and not info.fell_back_to_mock                # then the real plan went to the model
    assert qc.breaker_audit("EV-4") == {"breaker_state": "closed"}


async def test_probe_uses_a_short_timeout():
    seen: list = []

    class _C:
        async def create(self, **kw):
            seen.append(kw)
    cmd = _cmd(_Server(hang=False), llm_breaker_probe_timeout_s=3.0)
    cmd.client = type("X", (), {"chat": type("Y", (), {"completions": _C()})()})()
    assert await cmd.probe() is True
    assert seen[0]["timeout"] == 3.0 and seen[0]["max_tokens"] == 1


# ---------------------------------------------------------------- strict never falls back
async def test_strict_open_breaker_raises_llm_unavailable_no_rule_plan():
    srv = _Server(hang=True)
    cmd = _cmd(srv, strict=True)
    qc = QueuedCommander(cmd, ModelQueue(slots=1))
    for eid in ("EV-1", "EV-2"):
        with pytest.raises(Exception) as ei:
            await qc.plan(_req(eid))
        assert failure_cause(ei.value) == "timeout"
    with pytest.raises(LLMUnavailable):
        await qc.plan(_req("EV-3"))
    assert srv.plans == 2


async def test_strict_open_breaker_safe_stops_through_the_state_machine(tmp_path):
    srv = _Server(hang=True)
    cmd = _cmd(srv, strict=True)
    cmd.breaker.record_failure(TimeoutError("a"))
    cmd.breaker.record_failure(TimeoutError("b"))
    *_, approvals, sm = make_sm(tmp_path, commander=QueuedCommander(cmd, ModelQueue(slots=1)), approval_timeout_s=0.01)
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    calls = _llm_calls(sm.audit)
    assert len(calls) == 1 and calls[0]["cause"] == "llm_unavailable" and calls[0]["plan_source"] is None
    assert calls[0]["fell_back_to_mock"] is False and calls[0]["breaker_state"] == "open"
    assert ev.state is EventState.SAFE_STOP and ev.plan is None and not ev.steps
    assert not approvals.pending() and srv.plans == 0


# ---------------------------------------------------------------- budget: an incident's first PLAN still goes out
async def test_first_plan_is_not_blocked_by_the_budget_and_budget_refusals_do_not_trip(tmp_path):
    log = JsonlAuditLog(tmp_path / "b.jsonl")
    caps = type("C", (), {"budget_daily_tokens": 10, "budget_daily_model_s": 1e9, "budget_event_tokens": 1,
                          "budget_conversation_tokens": 10**9})()
    budget.install(Budget(caps, audit=lambda: log, today=lambda: "2026-11-03"))
    budget.get().record("chat", 5000, 0, 1)           # far past the day's cap
    srv = _Server(hang=False)
    cmd = _cmd(srv)
    qc = QueuedCommander(cmd, ModelQueue(slots=1))
    _plan, info = await qc.plan(_req("EV-7"))
    assert srv.plans == 1 and not info.fell_back_to_mock          # the incident's PLAN reached the model
    for _ in range(3):                                             # replans over the per-event cap: refused unsent
        _plan, info = await qc.plan(_req("EV-7", phase="replan"))
        assert info.fell_back_to_mock and cmd.last_cause == "budget"
    assert srv.plans == 1 and cmd.breaker.state is BreakerState.CLOSED and cmd.breaker.consecutive == 0


# ---------------------------------------------------------------- health
def test_health_components_marks_llm_degraded_while_open(tmp_path):
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "q")
    rt = Runtime(s, use_real=False, run_loop=False)
    srv = _Server(hang=True)
    cmd = _cmd(srv)
    rt.c.commander = cmd
    with TestClient(create_app(rt)) as c:
        h = c.get("/api/health/components").json()
        assert h["llm"]["state"] == "closed" and h["llm"]["status"] == "ok" and "llm" not in h["degraded"]
        cmd.breaker.record_failure(TimeoutError("a"))
        cmd.breaker.record_failure(TimeoutError("b"))
        h = c.get("/api/health/components").json()
        assert h["llm"]["state"] == "open" and h["llm"]["status"] == "degraded" and "cooldown" in h["degraded"]["llm"]
        assert c.get("/api/health").json()["llm_reachable"] is False       # no /models request while OPEN
    rt.c.commander = cmd
    rt._llm_probe = (5.0, True)
    assert cmd.breaker.models_probe == (5.0, True)                        # one shared probe state
