"""Issue #33: the commander keeps its request inside the model's context window (Settings.llm_context_tokens minus
commander_max_tokens). Per-event context is trimmed in TRIM_STEPS order; the system prompt, anomaly, allowed_tools,
rejected hypotheses and supervisor constraints never are; a request that still does not fit is not sent and goes
through the normal failure policy (cause "too_long"). A demo-sized request is sent byte-identical to before.
Concurrent events sharing one commander each audit their own estimate."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.test_agent import _llm_plan_json, _req_plan, _sse
from tests.test_agent_page11 import page11_anomaly
from warroom.agent.commander import (
    SYSTEM_PROMPT,
    TRIM_KEEP_ITEMS,
    OpenAICommander,
    approx_tokens,
    build_messages,
    fit_context,
)
from warroom.agent.strict import PromptTooLong, failure_cause, strict_reason
from warroom.config import Settings
from warroom.contracts import LLMMode

LONG = "疏水閥卡滯時先關閉進汽閥，確認壓力歸零後再拆檢。" * 400          # ≈ 9,600 CJK characters


def _settings(**kw) -> Settings:
    return Settings(llm_mode=LLMMode.LLM, base_url="http://localhost:11434/v1", model="nemotron-test", **kw)


def _sop(text: str) -> list[dict]:
    return [{"sop_id": f"SOP-0{i}", "title": f"標題 {i}", "score": 0.5, "text": text} for i in range(1, 4)]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_demo_sized_request_is_sent_unchanged():
    req = _req_plan(page11_anomaly(), context={"machine": {"id": "CURE-07"}, "sop": _sop("硫化中斷處置：先確認蒸汽壓力。")})
    system, user = (m["content"] for m in build_messages(req))
    sent, est, trimmed = fit_context(req, system, _settings(), 4096)
    assert sent is req and trimmed == []
    assert est == approx_tokens(system) + approx_tokens(user)


def test_sop_text_is_cut_first_and_ids_stay():
    nb = {"CURE-08": {"temp_c_now": 127.9, "temp_c_min": 127.5}}
    req = _req_plan(page11_anomaly(), context={"sop": _sop(LONG), "neighbours": nb})
    sent, est, trimmed = fit_context(req, SYSTEM_PROMPT, _settings(), 4096)
    assert trimmed == ["context.sop.text"]                       # enough on its own: nothing else is touched
    assert [h["sop_id"] for h in sent.context["sop"]] == ["SOP-01", "SOP-02", "SOP-03"]
    assert all("text" not in h and h["title"] for h in sent.context["sop"])
    assert sent.context["neighbours"] == nb
    assert est * 2.0 <= 32768 - 4096
    assert req.context["sop"][0]["text"] == LONG                 # the caller's request is not changed


def test_steps_follow_the_order_and_never_touch_the_fixed_parts():
    materials = [{"key": f"M{i}", "name": LONG[:500], "status": "ok" if i % 2 else "low"} for i in range(30)]
    ctx = {"sop": _sop(LONG), "materials": materials, "orders": [{"id": f"#O-{i}"} for i in range(12)],
           "constraints": ["CURE-09 今晚保養"], "command": "把 #A-311 移到 9 號機",
           "plant_event": {"kind": "rush_order", "order_id": "#R-1"}}
    req = _req_plan(page11_anomaly(), context=ctx)
    sent, est, trimmed = fit_context(req, SYSTEM_PROMPT, _settings(), 4096)
    assert trimmed == ["context.sop.text", "context.materials"]   # no neighbours here; stopped once it fits
    assert len(sent.context["materials"]) == TRIM_KEEP_ITEMS
    assert all(m["status"] == "low" for m in sent.context["materials"])   # short materials kept first
    assert len(sent.context["orders"]) == 12                      # not reached
    for k in ("constraints", "command", "plant_event"):
        assert sent.context[k] == ctx[k]
    assert sent.anomaly == req.anomaly and sent.rejected == req.rejected and sent.allowed_tools == req.allowed_tools


def test_every_step_then_too_long():
    sd = {"text": "不要跨組調人", "constraints": ["不跨組調人"], "constraint_detail": [{"kind": "no_transfer"}],
          "superseded_card": {"id": "AP-1", "version": 2}}
    ctx = {"sop": _sop("x"), "neighbours": {"CURE-08": {"temp_c_now": 127.9, "trend_c_per_min": -0.1}},
           "materials": [{"key": f"M{i}", "status": "ok"} for i in range(30)],
           "orders": [{"id": f"#O-{i}"} for i in range(12)], "supervisor_discussion": sd}
    with pytest.raises(PromptTooLong) as ei:                      # a 100-token budget never fits the system prompt
        fit_context(_req_plan(page11_anomaly(), context=ctx), SYSTEM_PROMPT, _settings(llm_context_tokens=4196), 4096)
    e = ei.value
    assert e.trimmed == ["context.sop.text", "context.neighbours", "context.materials", "context.orders",
                         "context.supervisor_discussion.detail"]
    assert e.estimate > approx_tokens(SYSTEM_PROMPT)
    assert failure_cause(e) == "too_long"
    assert strict_reason(e).startswith("提示超過模型的 context 上限")


async def test_normal_call_sends_the_same_messages_and_records_the_estimate():
    seen = {}
    text = _llm_plan_json()

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=_sse([text[i:i + 40] for i in range(0, len(text), 40)]),
                              headers={"content-type": "text/event-stream"})

    cmd = OpenAICommander(_settings(llm_fallback_to_mock=False), http_client=_client(handler))
    req = _req_plan(page11_anomaly())
    plan, info = await cmd.plan(req)
    assert seen["body"]["messages"] == build_messages(req)        # byte-identical to the request before #33
    assert info.trimmed == [] and info.prompt_tokens_est == approx_tokens(
        seen["body"]["messages"][0]["content"]) + approx_tokens(seen["body"]["messages"][1]["content"])
    assert info.prompt_tokens == 900                              # the server's number, next to the estimate
    assert {"prompt_tokens_est", "trimmed"} <= info.model_dump().keys()   # so the LLM_CALL audit carries both


async def test_too_long_request_is_not_sent_and_falls_back():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=_sse([_llm_plan_json()]), headers={"content-type": "text/event-stream"})

    cmd = OpenAICommander(_settings(llm_context_tokens=4196), http_client=_client(handler))
    plan, info = await cmd.plan(_req_plan(page11_anomaly()))
    assert calls == []                                            # nothing was sent to the model
    assert info.fell_back_to_mock and not info.valid_json and plan.steps   # the rule plan runs instead
    assert cmd.last_cause == "too_long" and info.prompt_tokens_est > 100 and info.trimmed == []


async def test_too_long_in_strict_mode_raises():
    cmd = OpenAICommander(_settings(llm_context_tokens=4196, llm_fallback_to_mock=False),
                          http_client=_client(lambda r: httpx.Response(500)))
    with pytest.raises(PromptTooLong):
        await cmd.plan(_req_plan(page11_anomaly()))
    assert cmd.last_cause == "too_long"


async def test_concurrent_events_each_audit_their_own_estimate():
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if "SOP-01" in json.loads(request.content)["messages"][1]["content"]:
            await release.wait()                                  # answers after the short one is sent
        else:
            release.set()
        return httpx.Response(200, content=_sse([_llm_plan_json()]), headers={"content-type": "text/event-stream"})

    cmd = OpenAICommander(_settings(llm_fallback_to_mock=False), http_client=_client(handler))
    long_req, short_req = _req_plan(page11_anomaly(), context={"sop": _sop(LONG)}), _req_plan(page11_anomaly())
    first = asyncio.create_task(cmd.plan(long_req))
    await asyncio.sleep(0)                                        # the long call is in flight first
    (_, a), (_, b) = await asyncio.gather(first, cmd.plan(short_req))
    for req, info in ((long_req, a), (short_req, b)):             # one commander, two events: no cross-talk
        _, est, trimmed = fit_context(req, build_messages(req)[0]["content"], cmd.settings, cmd.max_tokens)
        assert (info.prompt_tokens_est, info.trimmed) == (est, trimmed)
    assert a.trimmed == ["context.sop.text"] and b.trimmed == []
