"""Warm the model server with the commander's real prompts (boot preload, deploy/bin/preload.sh).

Sends the alert-card and plan prompts for each demo anomaly once, so the shared system prefix sits in the
prefix cache and the first real anomaly pays only for its own tokens.
  BASE_URL=http://vllm:8000/v1 MODEL=nemotron python -m bench.preload
"""
from __future__ import annotations

import time

from bench.llm import Endpoint, json_schema_format, run, stream_chat
from bench.prompts import ANOMALIES, alert_card_messages, card_schema, plan_messages


async def preload(ep: Endpoint) -> list[float]:
    times = []
    for a in ANOMALIES:
        for msgs, fmt in ((alert_card_messages(a), json_schema_format("AlertCard", card_schema())),
                          (plan_messages(a), None)):
            r = await stream_chat(ep, msgs, max_tokens=1, response_format=fmt)
            if not r.ok:
                raise SystemExit(f"preload failed: {r.error}")
            times.append(round(r.total_s, 3))
    return times


def main() -> None:
    ep = Endpoint.from_env()
    t0 = time.perf_counter()
    times = run(preload(ep))
    print(f"preload ok: {len(times)} prompts in {time.perf_counter() - t0:.1f}s {times}")


if __name__ == "__main__":
    main()
