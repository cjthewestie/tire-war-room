"""Commander completion cap: the live p.11 plan (1.29-1.35k tokens on Nemotron) no longer hits a 1200 cap,
and a plan cut at max_tokens (finish_reason == "length") is audited as cause "truncated", not a decode error."""
from __future__ import annotations

import asyncio
import json

import httpx

from tests.test_agent import make_sm
from tests.test_agent_page11 import page11_anomaly
from tests.test_agent_review_r3 import _plan_json, stable
from tests.test_strict_llm import _llm_calls
from warroom.agent.commander import OpenAICommander
from warroom.agent.strict import TruncatedOutput, failure_cause, strict_reason
from warroom.config import Settings
from warroom.contracts import EventState


def _cut_transport(body: str) -> httpx.MockTransport:
    """Streams body, then a final chunk with finish_reason "length", as Ollama / vLLM do at max_tokens."""
    def handler(req):
        chunks = [{"choices": [{"index": 0, "delta": {"content": body}, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
                  {"choices": [], "usage": {"prompt_tokens": 5762, "completion_tokens": 1200, "total_tokens": 6962}}]
        head = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m"}
        sse = "".join(f"data: {json.dumps(head | c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
    return httpx.MockTransport(handler)


def _commander(strict: bool, body: str) -> OpenAICommander:
    return OpenAICommander(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=not strict),
                           http_client=httpx.AsyncClient(transport=_cut_transport(body)))


def test_commander_max_tokens_default_and_env(monkeypatch):
    monkeypatch.delenv("COMMANDER_MAX_TOKENS", raising=False)
    s = Settings(_env_file=None)
    assert s.commander_max_tokens == 4096                       # ~3x the largest live p.11 plan (1348)
    assert OpenAICommander(s, http_client=httpx.AsyncClient()).max_tokens == 4096
    monkeypatch.setenv("COMMANDER_MAX_TOKENS", "2000")
    s = Settings(_env_file=None)
    assert OpenAICommander(s, http_client=httpx.AsyncClient()).max_tokens == 2000
    assert OpenAICommander(s, http_client=httpx.AsyncClient(), max_tokens=300).max_tokens == 300   # explicit wins


def test_truncated_cause_and_reason():
    assert failure_cause(TruncatedOutput("cut")) == "truncated"
    assert strict_reason(TruncatedOutput("cut")) == "模型輸出超過長度上限，未產生計劃（strict 模式不使用規則備援）"


async def test_length_cut_is_audited_as_truncated(tmp_path):
    # even a body that happens to parse is rejected when the server says it stopped at the cap
    for body in (_plan_json(False)[:-40], _plan_json(False)):
        cmd = _commander(False, body)
        *_, sm = make_sm(tmp_path, stable, commander=cmd, approval_timeout_s=0.01)
        await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
        call = _llm_calls(sm.audit)[0]
        assert call["plan_source"] == "mock_fallback" and call["fell_back_to_mock"] is True
        assert call["cause"] == "truncated" and call["error"].startswith("TruncatedOutput")
        assert call["completion_tokens"] == 1200


async def test_strict_length_cut_safe_stops_with_truncated_reason(tmp_path):
    *_, approvals, sm = make_sm(tmp_path, stable, commander=_commander(True, _plan_json(False)[:-40]),
                                approval_timeout_s=0.01)
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    call = _llm_calls(sm.audit)[0]
    assert call["cause"] == "truncated" and call["plan_source"] is None
    assert ev.state is EventState.SAFE_STOP and "模型輸出超過長度上限" in ev.outcome
    assert ev.plan is None and not approvals.pending()             # no rule plan ran
