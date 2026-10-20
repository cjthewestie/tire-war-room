"""--strict: a model timeout / failure is surfaced, never answered by the rule-based fallback.
Non-strict keeps the fallback. Default LLM timeout is 60 s."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_agent import make_sm
from tests.test_agent_page11 import page11_anomaly
from tests.test_agent_review_r3 import _plan_json, _transport, stable
from tests.test_ask import _plan, audit, plant  # noqa: F401 - fixtures
from tests.test_plan_talk import FakeLLM
from warroom.agent.ask import AskAgent
from warroom.agent.commander import OpenAICommander
from warroom.agent.discuss import PlanTalk
from warroom.agent.strict import failure_cause, strict_reason
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import AuditKind, EventState

REASON = "模型逾時，未產生計劃（strict 模式不使用規則備援）"


def _timeout_client() -> httpx.AsyncClient:
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _commander(strict: bool, http_client: httpx.AsyncClient) -> OpenAICommander:
    return OpenAICommander(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=not strict),
                           http_client=http_client)


async def _handle(tmp_path, cmd: OpenAICommander):
    *_, approvals, sm = make_sm(tmp_path, stable, commander=cmd, approval_timeout_s=0.01)
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    return sm.audit, approvals, ev


def _llm_calls(log, event_id=None) -> list[dict]:
    return [e.payload for e in log.tail(0, 0)
            if e.kind is AuditKind.LLM_CALL and (event_id is None or e.event_id == event_id)]


def test_default_llm_timeout_is_60s(monkeypatch):
    monkeypatch.delenv("LLM_TIMEOUT_S", raising=False)
    assert Settings(_env_file=None).llm_timeout_s == 60.0
    monkeypatch.setenv("LLM_TIMEOUT_S", "15")
    assert Settings(_env_file=None).llm_timeout_s == 15.0          # env override kept


def test_strict_reason_text():
    assert strict_reason(TimeoutError("slow")) == REASON
    assert failure_cause(ValueError("bad json")) == "invalid_output"
    assert strict_reason(ConnectionError("refused")).startswith("模型呼叫失敗（ConnectionError）")


async def test_strict_commander_timeout_safe_stops_with_no_rule_plan(tmp_path):
    log, approvals, ev = await _handle(tmp_path, _commander(True, _timeout_client()))
    llm = _llm_calls(log)
    assert len(llm) == 1 and llm[0]["fell_back_to_mock"] is False and llm[0]["plan_source"] is None
    assert llm[0]["cause"] == "timeout" and llm[0]["strict"] is True and llm[0]["reason"] == REASON
    assert ev.state is EventState.SAFE_STOP and REASON in ev.outcome
    assert ev.plan is None and not ev.steps                         # no rule plan ran
    assert not approvals.pending()


async def test_non_strict_commander_timeout_still_falls_back(tmp_path):
    log, _, _ = await _handle(tmp_path, _commander(False, _timeout_client()))
    llm = _llm_calls(log)[0]
    assert llm["fell_back_to_mock"] and llm["plan_source"] == "mock_fallback" and "Timeout" in llm["error"]


async def test_strict_commander_valid_plan_unchanged(tmp_path):
    cmd = _commander(True, httpx.AsyncClient(transport=_transport(_plan_json(False))))
    log, _, _ = await _handle(tmp_path, cmd)
    assert _llm_calls(log)[0]["plan_source"] == "commander"


async def test_strict_ask_returns_the_reason_not_a_rule_answer(plant, audit):  # noqa: F811
    ctx, reg = plant
    agent = AskAgent(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=False),
                     client=FakeLLM(TimeoutError("slow")))
    a = await agent.ask("7 號機現在怎樣？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer == "模型逾時，未產生回答（strict 模式不使用規則備援）"
    assert not a.fell_back_to_mock and a.steps == []
    call = _llm_calls(audit, a.id)[0]
    assert call["cause"] == "timeout" and call["fell_back_to_mock"] is False

    agent = AskAgent(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=False),
                     client=FakeLLM(_plan(("read_sensors", {"machine_ids": ["CURE-07"]})), TimeoutError("slow")))
    a = await agent.ask("7 號機？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer.startswith("模型逾時，未產生回答") and not a.fell_back_to_mock   # no template answer


async def test_strict_discuss_returns_the_reason_and_changes_nothing():
    t = PlanTalk(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=False),
                 client=FakeLLM(TimeoutError("slow")))
    logged = []
    log = SimpleNamespace(append=lambda kind, actor, payload, event_id=None: logged.append((kind, payload)))
    sm = SimpleNamespace(get=lambda _id: SimpleNamespace(), waiting_card=lambda _id: None)
    out = await t.discuss(sm=sm, event_id="EV-1", text="9 號機今晚要保養", actor="supervisor", ask_agent=None,
                          tools=None, audit=log, context={}, now=None)
    assert out.reply.startswith("模型逾時") and "strict" in out.reply and not out.constraints and not out.replanned
    assert any(k is AuditKind.LLM_CALL and p["cause"] == "timeout" and p["fell_back_to_mock"] is False
               for k, p in logged)


@pytest.mark.parametrize("allow_fallback,strict", [(True, False), (False, True)])
def test_health_reports_strict(tmp_path, allow_fallback, strict):
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "q")
    rt = Runtime(s, use_real=False, run_loop=False, allow_fallback=allow_fallback)
    assert rt.settings.llm_fallback_to_mock is not strict
    with TestClient(create_app(rt)) as c:
        assert c.get("/api/health").json()["strict"] is strict
