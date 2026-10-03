"""TTFT + first-alert-card latency (pages 9, 10).

Targets (warroom.factory): TTFT ≤ 0.5 s single stream at 32k context with prefix cache;
first alert card (≤100 tokens) on screen ≤ 2 s.

  uv run python -m bench.ttft                                   # Ollama defaults
  BASE_URL=http://spark:8000/v1 MODEL=nemotron uv run python -m bench.ttft --runs 10

Cold = unique nonce at the start of the prompt (defeats prefix cache). Warm = identical 32k prefix,
different final question (what the commander sees on every tool round of one event).
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, parse_json, run, save_result, \
    stream_chat, summarize
from bench.prompts import ANOMALIES, alert_card_messages, card_schema, long_prefix
from warroom.factory import ALERT_CARD_MAX_TOKENS, LATENCY_FIRST_ALERT_S, TTFT_TARGET_S


async def alert_card_runs(ep: Endpoint, runs: int) -> dict[str, Any]:
    fmt = json_schema_format("AlertCard", card_schema())
    calls = []
    cli = ep.client()
    for i in range(runs):
        a = ANOMALIES[i % 2]
        r = await stream_chat(ep, alert_card_messages(a), max_tokens=ALERT_CARD_MAX_TOKENS,
                              response_format=fmt, client=cli)
        d = r.to_dict()
        d["valid_card"] = isinstance(parse_json(r.text), dict) and {"title", "summary"} <= set(parse_json(r.text))
        calls.append(d)
    await cli.close()
    ok = [c for c in calls if c["ok"]]
    return {"calls": calls, "total_s": summarize([c["total_s"] for c in ok]),
            "ttft_s": summarize([c["ttft_s"] for c in ok]),
            "valid_card_rate": round(sum(c["valid_card"] for c in calls) / max(len(calls), 1), 3)}


async def calibrate_chars_per_token(ep: Endpoint, cli) -> float:
    probe = long_prefix(8000)
    r = await stream_chat(ep, [{"role": "user", "content": probe}], max_tokens=1, client=cli)
    if r.ok and r.prompt_tokens:
        return len(probe) / r.prompt_tokens
    return 2.0


async def long_context_runs(ep: Endpoint, context_tokens: int, runs: int) -> dict[str, Any]:
    cli = ep.client(timeout_s=600)
    cpt = await calibrate_chars_per_token(ep, cli)
    prefix = long_prefix(int(context_tokens * cpt * 0.97))
    a = ANOMALIES[0]
    questions = ["請只回答「收到」。", "請只回答「確認」。", "請只回答「OK」。", "請只回答「好」。"]

    cold = []
    for _ in range(max(1, min(runs, 3))):
        msgs = alert_card_messages(a, prefix=f"nonce {uuid.uuid4()}\n" + prefix)
        msgs[-1]["content"] = questions[0]
        cold.append((await stream_chat(ep, msgs, max_tokens=8, client=cli, timeout_s=600)).to_dict())

    base = alert_card_messages(a, prefix=prefix)
    base[-1]["content"] = questions[0]
    await stream_chat(ep, base, max_tokens=1, client=cli, timeout_s=600)   # fill the prefix cache
    warm = []
    for i in range(runs):
        msgs = alert_card_messages(a, prefix=prefix)
        msgs[-1]["content"] = questions[(i + 1) % len(questions)]
        warm.append((await stream_chat(ep, msgs, max_tokens=8, client=cli, timeout_s=600)).to_dict())
    await cli.close()
    return {"chars_per_token": round(cpt, 3),
            "prompt_tokens_measured": max((c["prompt_tokens"] or 0) for c in warm + cold),
            "cold": cold, "warm": warm,
            "cold_ttft_s": summarize([c["ttft_any_s"] for c in cold if c["ok"]]),
            "warm_ttft_s": summarize([c["ttft_any_s"] for c in warm if c["ok"]])}


async def main_async(args) -> dict[str, Any]:
    ep = Endpoint.from_env(args.base_url, args.model)
    info = await endpoint_info(ep)
    out: dict[str, Any] = {"endpoint": info, "targets": {"ttft_s": TTFT_TARGET_S,
                                                          "first_alert_s": LATENCY_FIRST_ALERT_S}}
    await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)   # load / warm the model
    out["alert_card"] = await alert_card_runs(ep, args.runs)
    verdicts = [Verdict("first_alert_card_p90", f"≤ {LATENCY_FIRST_ALERT_S} s",
                        out["alert_card"]["total_s"].get("p90"),
                        _le(out["alert_card"]["total_s"].get("p90"), LATENCY_FIRST_ALERT_S))]
    if not args.skip_long:
        lc = await long_context_runs(ep, args.context_tokens, args.runs)
        out["long_context"] = lc
        reached = lc["prompt_tokens_measured"] >= 0.9 * args.context_tokens
        verdicts.append(Verdict("ttft_warm_32k_p50", f"≤ {TTFT_TARGET_S} s", lc["warm_ttft_s"].get("p50"),
                                _le(lc["warm_ttft_s"].get("p50"), TTFT_TARGET_S) if reached else None,
                                note=f"prompt_tokens={lc['prompt_tokens_measured']}"
                                     + ("" if reached else " (context did not reach target: server truncated)")))
    out["verdicts"] = [v.__dict__ for v in verdicts]
    path = save_result("ttft", ep, out, args.out_dir)
    out["saved_to"] = str(path)
    return out


def _le(v, target) -> bool | None:
    return None if v is None else v <= target


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--context-tokens", type=int, default=32000)
    p.add_argument("--skip-long", action="store_true")
    p.add_argument("--out-dir", type=__import__("pathlib").Path)
    args = p.parse_args(argv)
    out = run(main_async(args))
    for v in out["verdicts"]:
        print(f"{v['name']:<24} target {v['target']:<8} measured {v['measured']}  pass={v['passed']}  {v['note']}")
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
