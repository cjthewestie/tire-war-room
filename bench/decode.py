"""Single-stream decode speed (page 10: vLLM box "推測解碼 95–108 tok/s").

What is timed: the commander's FULL plan stream (bench.prompts.commander_messages, CommanderPlan
response_format, reasoning off), batch 1 -- one request in flight at a time, never overlapped. Each call is
a new detection (unique event id + live sensor values) and plan / replan rounds alternate, so the text
decoded is what the commander really emits in an event.

  decode tok/s per call = (completion_tokens - 1) / (total_s - ttft_any_s)

The first token comes out of prefill, so it is excluded from both the count and the clock; what is left is
pure decode, which is what speculative decoding speeds up (it barely moves TTFT or the alert-card time).

Verdict (production config only): median decode tok/s over the runs, judged >= 95 tok/s, the low edge of
the slide's 95–108 band. Above 108 still passes (the slide under-states it) and is said so in the note.
With --config <name> other than "full" the median is reported as info: that run is an ablation leg, not
the production stack the slide describes.

  bench/py.sh -m bench.decode --runs 10                         # production (judged)
  bench/py.sh -m bench.decode --runs 10 --config no_spec        # ablation leg (info)
  bench/py.sh -m bench.decode --ablation bench/results/spark-ablation-<ts>
      -> per-config decode p50 (+ tokens_per_watt tokens/s where present) and the spec-decode contribution
         (full vs no_spec, mtp vs no_spec) in tok/s and x speed-up.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, run, save_result, stream_chat, summarize
from bench.prompts import (
    ANOMALIES,
    COMMANDER_MAX_TOKENS,
    commander_format,
    commander_messages,
    live_anomaly,
    page11_rejected_h1,
)

# Page 10, vLLM box: "NVFP4・19 GB・32k 上下文 KV FP8・前綴快取 推測解碼 95–108 tok/s".
DECODE_TPS_BAND = (95.0, 108.0)
PRODUCTION_CONFIG = "full"
# Calls shorter than this many generated tokens give a decode rate dominated by timer noise.
MIN_DECODE_TOKENS = 32


def decode_tps(completion_tokens: int | None, ttft_any_s: float | None, total_s: float) -> float | None:
    """Decode-only tokens per second: first token (made by prefill) excluded from count and clock."""
    if not completion_tokens or completion_tokens < 2 or ttft_any_s is None:
        return None
    gen_s = total_s - ttft_any_s
    return round((completion_tokens - 1) / gen_s, 2) if gen_s > 0 else None


def _messages(i: int) -> tuple[str, list[dict[str, str]]]:
    a = live_anomaly(ANOMALIES[i % len(ANOMALIES)])
    if i % 2 and a["machine_id"] == ANOMALIES[0]["machine_id"]:      # page-11 replan round (H1 rejected)
        return "replan", commander_messages(a, "replan", rejected=page11_rejected_h1(),
                                            extra={"failed_expectation": {"hypothesis_id": "H1", "observed": 119.5}})
    return "plan", commander_messages(a, "plan")


async def single_stream(ep: Endpoint, runs: int) -> dict[str, Any]:
    """`runs` sequential full plan streams, batch 1. Returns per-call rows and the decode summary."""
    cli = ep.client(timeout_s=300)
    # prime the system prompt so the first judged call is not a cold model load
    await stream_chat(ep, _messages(0)[1], max_tokens=1, response_format=commander_format(), client=cli)
    calls = []
    for i in range(runs):
        phase, msgs = _messages(i)
        r = await stream_chat(ep, msgs, max_tokens=COMMANDER_MAX_TOKENS, response_format=commander_format(),
                              client=cli)
        tps = decode_tps(r.completion_tokens, r.ttft_any_s, r.total_s) if r.ok else None
        calls.append({"phase": phase, "ok": r.ok, "error": r.error, "ttft_any_s": r.ttft_any_s,
                      "total_s": round(r.total_s, 4), "completion_tokens": r.completion_tokens,
                      "prompt_tokens": r.prompt_tokens, "decode_tps": tps,
                      "counted": tps is not None and (r.completion_tokens or 0) >= MIN_DECODE_TOKENS})
    await cli.close()
    counted = [c for c in calls if c["counted"]]
    return {"calls": calls, "n_counted": len(counted), "n_errors": sum(not c["ok"] for c in calls),
            "decode_tps": summarize([c["decode_tps"] for c in counted]),
            "completion_tokens": summarize([c["completion_tokens"] for c in counted])}


def judge(p50: float | None, n: int, config: str | None) -> Verdict:
    lo, hi = DECODE_TPS_BAND
    production = config in (None, PRODUCTION_CONFIG)
    if not production:
        return Verdict("decode_tps_p50", f"info (ablation leg '{config}'; {lo:g}–{hi:g} tok/s judged on "
                       f"'{PRODUCTION_CONFIG}')", p50, None, note=f"batch 1, commander full plan stream, n={n}")
    if p50 is None:
        return Verdict("decode_tps_p50", f"≥ {lo:g} tok/s (slide {lo:g}–{hi:g})", None, None,
                       note="no call produced enough tokens to time decode")
    where = "inside the slide band" if p50 <= hi else f"above the slide's {hi:g}: the slide under-states it"
    if p50 < lo:
        where = "below the slide band"
    return Verdict("decode_tps_p50", f"≥ {lo:g} tok/s (slide {lo:g}–{hi:g})", p50, p50 >= lo,
                   note=f"batch 1, commander full plan stream, n={n}; {where}")


async def main_async(args) -> dict[str, Any]:
    ep = Endpoint.from_env(args.base_url, args.model)
    out: dict[str, Any] = {"endpoint": await endpoint_info(ep), "config": args.config,
                           "batch": 1, "target_band_tps": list(DECODE_TPS_BAND),
                           "prompt": "commander: warroom.agent.commander.build_messages(CommanderRequest), "
                                     "full plan stream, plan + replan rounds, unique event per call"}
    out |= await single_stream(ep, args.runs)
    v = judge(out["decode_tps"].get("p50"), out["n_counted"], args.config)
    out["verdicts"] = [v.__dict__]
    out["saved_to"] = str(save_result("decode", ep, out, args.out_dir))
    return out


# ------------------------------------------------------------------ ablation summary (spark_ablation.sh)
def _newest(d: Path, bench: str) -> dict[str, Any] | None:
    files = sorted(d.glob(f"{bench}-*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8")) | {"_file": files[-1].name}
    except (OSError, json.JSONDecodeError):
        return None


def ablation(root: Path) -> dict[str, Any]:
    """Per-config decode p50 and tokens/s from an ablation directory (one subfolder per config), plus the
    speculative-decoding contribution: full and mtp against no_spec, same NVFP4 + prefix cache."""
    per: dict[str, dict[str, Any]] = {}
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        dec, tpw = _newest(sub, "decode"), _newest(sub, "tokens_per_watt")
        if not (dec or tpw):
            continue
        per[sub.name] = {"decode_tps_p50": ((dec or {}).get("decode_tps") or {}).get("p50"),
                         "decode_n": (dec or {}).get("n_counted"),
                         "tokens_per_s_concurrent": ((tpw or {}).get("plug") or {}).get("tokens_per_s"),
                         "tokens_per_joule": ((tpw or {}).get("plug") or {}).get("tokens_per_joule"),
                         "files": [x["_file"] for x in (dec, tpw) if x]}

    def contrib(a: str, b: str) -> dict[str, Any] | None:
        x, y = (per.get(a) or {}).get("decode_tps_p50"), (per.get(b) or {}).get("decode_tps_p50")
        if x is None or y is None or not y:
            return None
        return {"with": a, "without": b, "delta_tps": round(x - y, 2), "speedup_x": round(x / y, 3)}

    contributions = {k: c for k, c in (("spec_decode_dspark", contrib("full", "no_spec")),
                                       ("spec_decode_mtp", contrib("mtp", "no_spec")),
                                       ("prefix_cache", contrib("full", "no_cache")),
                                       ("all_vs_fp8_control", contrib("full", "control"))) if c}
    verdicts = [judge(per.get(PRODUCTION_CONFIG, {}).get("decode_tps_p50"),
                      per.get(PRODUCTION_CONFIG, {}).get("decode_n") or 0, None).__dict__]
    for k, c in contributions.items():
        verdicts.append(Verdict(f"{k}_decode_gain", "info", c["delta_tps"], None,
                                note=f"{c['with']} vs {c['without']}: x{c['speedup_x']} decode tok/s").__dict__)
    return {"ablation_dir": str(root), "configs": per, "contributions": contributions, "verdicts": verdicts}


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--config", help="ablation leg name (full = production, judged; anything else = info)")
    p.add_argument("--ablation", type=Path, help="summarise an ablation directory instead of running")
    p.add_argument("--out-dir", type=Path)
    args = p.parse_args(argv)
    if args.ablation:
        out = ablation(args.ablation)
        out["saved_to"] = str(save_result("decode_ablation", None, out, args.out_dir or args.ablation,
                                          tag="spark"))
        for c, r in out["configs"].items():
            print(f"{c:<10} decode p50 {r['decode_tps_p50']} tok/s (n={r['decode_n']})  "
                  f"tokens/s@3 {r['tokens_per_s_concurrent']}  tokens/J {r['tokens_per_joule']}")
    else:
        out = run(main_async(args))
    for v in out["verdicts"]:
        print(f"{v['name']:<28} target {v['target']}  measured {v['measured']}  pass={v['passed']}  {v['note']}")
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
