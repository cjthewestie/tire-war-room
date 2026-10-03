"""Commander smoke test against a live OpenAI-compatible server (Ollama / vLLM / NVIDIA cloud).

  LLM_MODE=llm BASE_URL=http://localhost:11434/v1 MODEL=qwen2.5:3b uv run python -m warroom.agent.smoke
  ... --phase replan      # page-11 second round (H1 rejected at 119.5 °C)

Sends the page-11 request, streams the constrained CommanderPlan, prints TTFT, time to the alert
card, total time, token counts and the plan. Exit 1 when the model output had to fall back to mock.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime

from warroom.agent.commander import OpenAICommander
from warroom.config import Settings
from warroom.contracts import (Anomaly, AnomalyKind, Channel, CommanderRequest, Expectation, Hypothesis,
                               HypothesisStatus, LLMMode, Prediction, Severity, TOOLS_BY_STATE, WRITE_TOOLS, EventState)
from warroom.factory import TZ


def page11_request(phase: str) -> CommanderRequest:
    t0 = datetime(2026, 10, 3, 2, 14, tzinfo=TZ)
    a = Anomaly(id="AN-1", ts=t0, machine_id="CURE-07", kind=AnomalyKind.TEMP_DROP, channel=Channel.TEMP, value=122.0,
                baseline=128.0, severity=Severity.WARN, message="7 號 128 → 122°C，預測穩定在 122±1°C",
                prediction=Prediction(machine_id="CURE-07", channel=Channel.TEMP, now_value=122, predicted_value=122,
                                      band_lo=121, band_hi=123, horizon_s=1800, trend_per_min=0.0))
    ctx = {"machine": {"id": "CURE-07", "label": "7 號機", "line": "CURE", "steam_header": "H2"},
           "cure_job": {"order_id": "#B-207", "size": "M", "setpoint_c": 128.0, "cure_elapsed_h": 10.0},
           "sensors": {"CURE-07": {"temp_c_now": 122.0, "temp_c_min": 122.0, "trend_c_per_min": -0.2}},
           "sop": [{"sop_id": "SOP-CP-02", "title": "硫化機溫度異常", "text": "溫度低於設定值時確認蒸汽壓..."}]}
    rejected = []
    if phase == "replan":
        rejected = [Hypothesis(id="H1", statement="蒸汽壓不足，溫度會穩定", cause_code="steam_pressure_low",
                               status=HypothesisStatus.REJECTED,
                               expectation=Expectation(machine_id="CURE-07", channel=Channel.TEMP, op=">=", value=121,
                                                       within_min=30),
                               evidence=["溫度續降至 119.5°C"])]
        ctx |= {"failed_expectation": {"hypothesis_id": "H1", "observed": 119.5},
                "neighbours": {"CURE-06": {"temp_c_now": 128.1}, "CURE-07": {"temp_c_now": 119.5},
                               "CURE-08": {"temp_c_now": 127.9}},
                "sop": [{"sop_id": "SOP-CP-03", "title": "硫化中斷處置", "text": "硫化中溫度低於 120°C：停用該機..."}]}
    allowed = sorted(TOOLS_BY_STATE[EventState.SIMULATE] | WRITE_TOOLS, key=lambda t: t.value)
    return CommanderRequest(phase=phase, event_id="EV-SMOKE", anomaly=a, allowed_tools=allowed, context=ctx,
                            rejected=rejected)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["plan", "replan"], default="plan")
    ap.add_argument("--max-tokens", type=int, default=1200)
    args = ap.parse_args()
    s = Settings(llm_mode=LLMMode.LLM)
    cmd = OpenAICommander(s, max_tokens=args.max_tokens)
    print(f"base_url={s.base_url} model={s.model} server={cmd.server_kind} constrained=response_format")
    t0 = time.perf_counter()
    card_at: list[float] = []
    plan, info = await cmd.plan(page11_request(args.phase),
                                on_alert=lambda c: card_at.append((time.perf_counter() - t0) * 1000) or
                                print(f"[alert card @ {card_at[-1]:.0f} ms] {c.title} — {c.summary}"))
    print(f"ttft_ms={info.ttft_ms} total_ms={info.total_ms:.0f} prompt_tokens={info.prompt_tokens} "
          f"completion_tokens={info.completion_tokens} valid_json={info.valid_json} "
          f"fell_back_to_mock={info.fell_back_to_mock}")
    if info.fell_back_to_mock:
        print("fallback reason:", getattr(cmd, "last_error", "?"))
        print("raw:", cmd.last_raw[:800])
    if cmd.last_intercepted:
        print("intercepted steps:", cmd.last_intercepted)
    print(json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=1))
    return 1 if info.fell_back_to_mock else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
