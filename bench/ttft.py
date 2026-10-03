"""TTFT + first-alert-card latency (pages 9, 10).

Targets (warroom.factory): TTFT ≤ 0.5 s single stream at 32k context with prefix cache;
first alert card (≤100 tokens) on screen ≤ 2 s.

  uv run python -m bench.ttft                                   # Ollama defaults
  BASE_URL=http://spark:8000/v1 MODEL=nemotron uv run python -m bench.ttft --runs 10

Every judged number times the COMMANDER'S OWN CALL (bench.prompts.commander_messages: the commander's
SYSTEM_PROMPT + a real CommanderRequest, CommanderPlan response_format). The first alert card is the
``alert_card`` field leading that plan stream, timed when the object is complete -- the moment the app's
on_alert fires (the app makes no separate alert-card call).

First alert card: judged on COLD calls -- each call is a new detection with its own event id and live
sensor values, so only the commander's system prompt can be cached (the real situation after boot
preload). WARM (one identical request repeated, full cache hit) is reported beside it as info only.
MICRO (info only): the short standalone alert-card prompt (bench.prompts.micro_alert_card_messages).

32k TTFT: the commander's request with a long shift-log/sensor history in its context. Cold = unique
event id ahead of the long block (defeats its cache). Warm = identical request up to and including the
long block, different trailing context key (what the commander sees on each round of one event); the
page-10 target says "with prefix cache", so that one is judged warm.
"""
from __future__ import annotations

import argparse
from typing import Any

from bench.llm import (
    Endpoint,
    Verdict,
    endpoint_info,
    json_schema_format,
    parse_json,
    run,
    save_result,
    stream_chat,
    summarize,
)
from bench.prompts import (
    ANOMALIES,
    COMMANDER_MAX_TOKENS,
    card_schema,
    commander_format,
    commander_messages,
    live_anomaly,
    long_prefix,
    micro_alert_card_messages,
)
from warroom.factory import ALERT_CARD_MAX_TOKENS, LATENCY_FIRST_ALERT_S, TTFT_TARGET_S


def _card_call(r) -> dict[str, Any]:
    """One commander call: card_s = time until the alert_card object is complete in the plan stream."""
    d = r.to_dict()
    d["card_s"] = None if r.alert_card_s is None else round(r.alert_card_s, 4)
    d["valid_card"] = isinstance(r.alert_card, dict) and {"title", "summary"} <= set(r.alert_card)
    return d


def _micro_call(r) -> dict[str, Any]:
    d = r.to_dict()
    card = parse_json(r.text)
    d["card_s"] = round(r.total_s, 4) if r.ok else None
    d["valid_card"] = isinstance(card, dict) and {"title", "summary"} <= set(card)
    return d


def _stats(calls: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [c for c in calls if c["ok"]]
    return {"card_s": summarize([c["card_s"] for c in ok]), "ttft_s": summarize([c["ttft_s"] for c in ok]),
            "prompt_tokens": summarize([c["prompt_tokens"] for c in ok]),
            "valid_card_rate": round(sum(c["valid_card"] for c in calls) / max(len(calls), 1), 3)}


async def _commander_card(ep: Endpoint, msgs, cli):
    return await stream_chat(ep, msgs, max_tokens=COMMANDER_MAX_TOKENS, response_format=commander_format(),
                             client=cli, watch_alert_card=True, stop_on_alert_card=True)


async def alert_card_runs(ep: Endpoint, runs: int, micro: bool = True) -> dict[str, Any]:
    """First alert card from the commander's real plan stream, cold and warm, reported separately.

    cold (judged): every call is a new detection -- unique event id + live sensor values in the
      CommanderRequest (prompts.live_anomaly), so only the commander's SYSTEM_PROMPT can hit the cache.
    warm (info only): one identical request repeated; a full prefix-cache hit, which never happens for a
      real new anomaly. Kept to show how much of the cold time is prefill.
    micro (info only): the short standalone alert-card prompt; not a call the app makes.
    The system prefix is primed once first (what bench.preload / boot self-start does in production).
    The stream is closed once the card is complete; the card time does not depend on what follows."""
    cli = ep.client()
    # prime the system prompt (1 token, run to the end so usage reports the commander's prompt size)
    prime = await stream_chat(ep, commander_messages(live_anomaly(ANOMALIES[0])), max_tokens=1,
                              response_format=commander_format(), client=cli)
    cold, nonces = [], []
    for i in range(runs):
        a = live_anomaly(ANOMALIES[i % 2])
        nonces.append(a["event_id"])
        cold.append(_card_call(await _commander_card(ep, commander_messages(a), cli)))
    fixed = commander_messages(live_anomaly(ANOMALIES[0], nonce="warm-fixed"))
    await _commander_card(ep, fixed, cli)  # fill cache
    warm = [_card_call(await _commander_card(ep, fixed, cli)) for _ in range(runs)]
    out: dict[str, Any] = {"prompt": "commander: warroom.agent.commander.build_messages(CommanderRequest), "
                                     "card = alert_card field of the CommanderPlan stream",
                           "commander_prompt_tokens": prime.prompt_tokens}
    if micro:
        fmt = json_schema_format("AlertCard", card_schema())
        mc = []
        for i in range(runs):
            r = await stream_chat(ep, micro_alert_card_messages(live_anomaly(ANOMALIES[i % 2])),
                                  max_tokens=ALERT_CARD_MAX_TOKENS, response_format=fmt, client=cli)
            mc.append(_micro_call(r))
        out["micro"] = {"label": "MICRO-BENCHMARK: short standalone alert-card prompt, not the app's call",
                        "calls": mc, **_stats(mc)}
    await cli.close()
    cs, ws = _stats(cold), _stats(warm)
    return out | {"prime": prime.to_dict(), "cold_event_ids": nonces,
                  "cold": {"calls": cold, **cs}, "warm": {"calls": warm, **ws},
                  # top-level keys = the judged (cold) numbers
                  "card_s": cs["card_s"], "ttft_s": cs["ttft_s"], "valid_card_rate": cs["valid_card_rate"]}


async def calibrate_chars_per_token(ep: Endpoint, cli) -> float:
    probe = long_prefix(8000)
    r = await stream_chat(ep, [{"role": "user", "content": probe}], max_tokens=1, client=cli)
    if r.ok and r.prompt_tokens:
        return len(probe) / r.prompt_tokens
    return 2.0


async def long_context_runs(ep: Endpoint, context_tokens: int, runs: int) -> dict[str, Any]:
    """32k TTFT on the commander's own request: the long block rides in CommanderRequest.context."""
    cli = ep.client(timeout_s=600)
    cpt = await calibrate_chars_per_token(ep, cli)
    a0 = ANOMALIES[0]
    base_req = await stream_chat(ep, commander_messages(live_anomaly(a0, nonce="size-probe")), max_tokens=1,
                                 client=cli, timeout_s=600)
    base_tokens = base_req.prompt_tokens or 0
    prefix = long_prefix(max(1000, int((context_tokens - base_tokens) * cpt * 0.97)))
    fmt = commander_format()

    cold = []
    for _ in range(max(1, min(runs, 3))):
        msgs = commander_messages(live_anomaly(a0), extra={"history": prefix, "round": 0})
        cold.append((await stream_chat(ep, msgs, max_tokens=8, response_format=fmt, client=cli,
                                       timeout_s=600)).to_dict())

    a = live_anomaly(a0, nonce="long-warm")
    await stream_chat(ep, commander_messages(a, extra={"history": prefix, "round": 0}), max_tokens=1,
                      response_format=fmt, client=cli, timeout_s=600)   # fill the prefix cache
    warm = []
    for i in range(runs):
        msgs = commander_messages(a, extra={"history": prefix, "round": i + 1})
        warm.append((await stream_chat(ep, msgs, max_tokens=8, response_format=fmt, client=cli,
                                       timeout_s=600)).to_dict())
    await cli.close()
    return {"chars_per_token": round(cpt, 3), "commander_base_prompt_tokens": base_tokens,
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
    out["alert_card"] = await alert_card_runs(ep, args.runs, micro=not getattr(args, "no_micro", False))
    ac = out["alert_card"]
    cold_p90 = ac["cold"]["card_s"].get("p90")
    warm_p90 = ac["warm"]["card_s"].get("p90")
    ptoks = ac.get("commander_prompt_tokens")
    verdicts = [Verdict("first_alert_card_p90", f"≤ {LATENCY_FIRST_ALERT_S} s", cold_p90,
                        _le(cold_p90, LATENCY_FIRST_ALERT_S),
                        note=f"commander plan stream, alert_card complete; cold: unique event per call, "
                             f"n={ac['cold']['card_s'].get('n', 0)}, prompt_tokens≈{ptoks}"),
                Verdict("first_alert_card_warm_p90", "info (target judged cold)", warm_p90, None,
                        note="warm: identical commander request repeated, full prefix-cache hit")]
    if "micro" in ac:
        verdicts.append(Verdict("micro_alert_card_p90", "info (micro-benchmark, not the app's call)",
                                ac["micro"]["card_s"].get("p90"), None,
                                note="short standalone alert-card prompt"))
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
    p.add_argument("--no-micro", action="store_true", help="skip the short-prompt micro-benchmark")
    p.add_argument("--out-dir", type=__import__("pathlib").Path)
    args = p.parse_args(argv)
    out = run(main_async(args))
    for v in out["verdicts"]:
        print(f"{v['name']:<24} target {v['target']:<8} measured {v['measured']}  pass={v['passed']}  {v['note']}")
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
