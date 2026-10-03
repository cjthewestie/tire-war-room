"""Warm the model server with the commander's real call (boot preload, deploy/bin/preload.sh).

Sends warroom.agent.commander.build_messages(CommanderRequest) -- the commander's byte-identical
SYSTEM_PROMPT plus a plan and a replan request for each demo anomaly, with the CommanderPlan
response_format -- once each, so the system prompt sits in vLLM's prefix cache and the xgrammar
grammar for the plan schema is compiled. The first real anomaly then pays only for its own user message.
  BASE_URL=http://vllm:8000/v1 MODEL=nemotron python -m bench.preload
"""
from __future__ import annotations

import time

from bench.llm import Endpoint, run, stream_chat
from bench.prompts import ANOMALIES, commander_format, commander_messages, live_anomaly, page11_rejected_h1


def preload_messages() -> list[list[dict[str, str]]]:
    out = []
    for a in ANOMALIES:
        la = live_anomaly(a, nonce=f"preload-{a['machine_id']}")
        out.append(commander_messages(la, "plan"))
        out.append(commander_messages(la, "replan", rejected=page11_rejected_h1() if a is ANOMALIES[0] else None))
    return out


async def preload(ep: Endpoint) -> list[float]:
    times = []
    fmt = commander_format()
    for msgs in preload_messages():
        r = await stream_chat(ep, msgs, max_tokens=1, response_format=fmt)
        if not r.ok:
            raise SystemExit(f"preload failed: {r.error}")
        times.append(round(r.total_s, 3))
    return times


def main() -> None:
    ep = Endpoint.from_env()
    t0 = time.perf_counter()
    times = run(preload(ep))
    print(f"preload ok: {len(times)} commander prompts in {time.perf_counter() - t0:.1f}s {times}")


if __name__ == "__main__":
    main()
