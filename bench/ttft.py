"""TTFT + first-alert-card latency (pages 9, 10).

Targets (warroom.factory): TTFT ≤ 0.5 s single stream at 32k context with prefix cache;
first alert card (≤100 tokens) on screen ≤ 2 s.

  uv run python -m bench.ttft                                   # Ollama defaults
  BASE_URL=http://spark:8000/v1 MODEL=nemotron uv run python -m bench.ttft --runs 10

First alert card: judged on COLD calls -- each call is a new detection with its own event id and live
sensor values, so only the shared system prefix can be cached (the real situation). WARM (one identical
prompt repeated, full cache hit) is reported beside it as info only.

32k TTFT: cold = unique nonce ahead of the 32k block (defeats its cache). Warm = identical 32k prefix,
different final question (what the commander sees on every tool round of one event); the page-10 target
says "with prefix cache", so that one is judged warm.
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, parse_json, run, save_result, \
    stream_chat, summarize
from bench.prompts import ANOMALIES, alert_card_messages, card_schema, live_anomaly, long_prefix
from warroom.factory import ALERT_CARD_MAX_TOKENS, LATENCY_FIRST_ALERT_S, TTFT_TARGET_S


def _card_call(r) -> dict[str, Any]:
    d = r.to_dict()
    card = parse_json(r.text)
    d["valid_card"] = isinstance(card, dict) and {"title", "summary"} <= set(card)
    return d


def _stats(calls: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [c for c in calls if c["ok"]]
    return {"total_s": summarize([c["total_s"] for c in ok]), "ttft_s": summarize([c["ttft_s"] for c in ok]),
            "valid_card_rate": round(sum(c["valid_card"] for c in calls) / max(len(calls), 1), 3)}


async def alert_card_runs(ep: Endpoint, runs: int) -> dict[str, Any]:
    """First alert card, cold and warm, reported separately.

    cold (judged): every call is a new detection -- unique event id + live sensor values in the anomaly
      block (prompts.live_anomaly), so only the shared system prefix can hit the prefix cache.
    warm (info only): one identical prompt repeated; a full prefix-cache hit, which never happens for a
      real new anomaly. Kept to show how much of the cold time is prefill.
    The system prefix is primed once first (what bench.preload / boot self-start does in production)."""
    fmt = json_schema_format("AlertCard", card_schema())
    cli = ep.client()
    prime = await stream_chat(ep, alert_card_messages(live_anomaly(ANOMALIES[0])),
                              max_tokens=ALERT_CARD_MAX_TOKENS, response_format=fmt, client=cli)
    cold, nonces = [], []
    for i in range(runs):
        a = live_anomaly(ANOMALIES[i % 2])
        nonces.append(a["event_id"])
        r = await stream_chat(ep, alert_card_messages(a), max_tokens=ALERT_CARD_MAX_TOKENS,
                              response_format=fmt, client=cli)
        cold.append(_card_call(r))
    fixed = alert_card_messages(live_anomaly(ANOMALIES[0], nonce="warm-fixed"))
    await stream_chat(ep, fixed, max_tokens=ALERT_CARD_MAX_TOKENS, response_format=fmt, client=cli)  # fill cache
    warm = []
    for _ in range(runs):
        r = await stream_chat(ep, fixed, max_tokens=ALERT_CARD_MAX_TOKENS, response_format=fmt, client=cli)
        warm.append(_card_call(r))
    await cli.close()
    cs, ws = _stats(cold), _stats(warm)
    return {"prime": prime.to_dict(), "cold_event_ids": nonces,
            "cold": {"calls": cold, **cs}, "warm": {"calls": warm, **ws},
            # top-level keys = the judged (cold) numbers, so older readers keep working
            "total_s": cs["total_s"], "ttft_s": cs["ttft_s"], "valid_card_rate": cs["valid_card_rate"]}


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
    ac = out["alert_card"]
    cold_p90 = ac["cold"]["total_s"].get("p90")
    warm_p90 = ac["warm"]["total_s"].get("p90")
    verdicts = [Verdict("first_alert_card_p90", f"≤ {LATENCY_FIRST_ALERT_S} s", cold_p90,
                        _le(cold_p90, LATENCY_FIRST_ALERT_S),
                        note=f"cold: unique event per call, n={ac['cold']['total_s'].get('n', 0)}"),
                Verdict("first_alert_card_warm_p90", "info (target judged cold)", warm_p90, None,
                        note="warm: identical prompt repeated, full prefix-cache hit")]
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
