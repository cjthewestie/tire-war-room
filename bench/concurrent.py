"""3 concurrent anomalies (page 10: 同時 3 件異常, vLLM 批次處理, 每件 ≤ 4 s).

Fires the first alert card for 3 different anomalies at the same instant (page-11 press 7, page-2
A-BIGOPEN, press 3 steam low) and measures each one's wall time. Every call is a new event (unique id +
live sensor values), so each one prefills its anomaly block cold; only the system prefix is shared.

vLLM batches them; Ollama needs OLLAMA_NUM_PARALLEL>=3 or it queues them. The bench checks which happened:
if any call's first token arrives only after another call has finished, the round was queued and the
verdict FAILS -- meeting 4 s by serving them one after another is not the batching page 10 claims.

  uv run python -m bench.concurrent --runs 3
  uv run python -m bench.concurrent --with-plan        # also time each anomaly's CommanderPlan
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, run, save_result, stream_chat, \
    summarize
from bench.prompts import ANOMALIES, alert_card_messages, card_schema, live_anomaly, plan_messages, \
    plan_schema
from warroom.factory import ALERT_CARD_MAX_TOKENS, CONCURRENT_ANOMALIES, CONCURRENT_EACH_S


async def one(ep: Endpoint, a: dict[str, Any], with_plan: bool, t_fire: float) -> dict[str, Any]:
    a = live_anomaly(a)                       # new event id + live values: cold prefill after the system prefix
    send_off = time.perf_counter() - t_fire
    r = await stream_chat(ep, alert_card_messages(a), max_tokens=ALERT_CARD_MAX_TOKENS,
                          response_format=json_schema_format("AlertCard", card_schema()))
    d = {"machine_id": a["machine_id"], "event_id": a["event_id"], "alert": r.to_dict(),
         "send_off_s": round(send_off, 4),
         "first_token_off_s": None if r.ttft_any_s is None else round(send_off + r.ttft_any_s, 4),
         "end_off_s": round(send_off + r.total_s, 4),
         "alert_wall_s": round(time.perf_counter() - t_fire, 4)}
    if with_plan:
        p = await stream_chat(ep, plan_messages(a), max_tokens=2000,
                              response_format=json_schema_format("CommanderPlan", plan_schema()))
        d["plan"] = p.to_dict()
        d["plan_wall_s"] = round(time.perf_counter() - t_fire, 4)
    return d


def batching(each: list[dict[str, Any]], tol_s: float = 0.0) -> dict[str, Any]:
    """Did the server run the calls together, or one after another?

    Batched: every call's first token arrives before any other call has finished (prefills run together,
    decodes overlap). Queued: some call's first token only arrives after another call already finished --
    the server served them in series. `serialized_pairs` counts (earlier, later) pairs where
    later.first_token > earlier.end + tol_s."""
    ok = [e for e in each if e["alert"]["ok"] and e["first_token_off_s"] is not None]
    pairs = [(x["machine_id"], y["machine_id"]) for x in ok for y in ok
             if x is not y and y["first_token_off_s"] > x["end_off_s"] + tol_s]
    ft = [e["first_token_off_s"] for e in ok]
    sends = [e["send_off_s"] for e in ok]
    return {"n_ok": len(ok), "serialized_pairs": pairs,
            "send_skew_s": round(max(sends) - min(sends), 4) if sends else None,
            "first_token_skew_s": round(max(ft) - min(ft), 4) if ft else None,
            "batched": None if len(ok) < 2 else not pairs}


async def main_async(args) -> dict[str, Any]:
    ep = Endpoint.from_env(args.base_url, args.model)
    out: dict[str, Any] = {"endpoint": await endpoint_info(ep), "target_each_s": CONCURRENT_EACH_S,
                           "n": CONCURRENT_ANOMALIES}
    fmt = json_schema_format("AlertCard", card_schema())
    await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)
    # prime the shared system prefix once (boot preload does this in production)
    await stream_chat(ep, alert_card_messages(live_anomaly(ANOMALIES[0])), max_tokens=ALERT_CARD_MAX_TOKENS,
                      response_format=fmt)
    seq = []
    for a in ANOMALIES[:CONCURRENT_ANOMALIES]:            # sequential baseline, cold (new event each)
        t = time.perf_counter()
        r = await stream_chat(ep, alert_card_messages(live_anomaly(a)), max_tokens=ALERT_CARD_MAX_TOKENS,
                              response_format=fmt)
        seq.append(round(time.perf_counter() - t, 4) if r.ok else None)
    out["sequential_each_s"] = seq
    rounds = []
    for _ in range(args.runs):
        t_fire = time.perf_counter()
        res = await asyncio.gather(*(one(ep, a, args.with_plan, t_fire) for a in ANOMALIES[:CONCURRENT_ANOMALIES]))
        rounds.append({"each": res, "batch_wall_s": round(time.perf_counter() - t_fire, 4),
                       "batching": batching(res)})
    out["rounds"] = rounds
    each = [e["alert_wall_s"] for r in rounds for e in r["each"] if e["alert"]["ok"]]
    out["alert_each_s"] = summarize(each)
    worst = max(each) if each else None
    b = [r["batching"]["batched"] for r in rounds if r["batching"]["batched"] is not None]
    batched = None if not b else all(b)
    queued_rounds = sum(1 for x in b if not x)
    skews = [r["batching"]["first_token_skew_s"] for r in rounds if r["batching"]["first_token_skew_s"] is not None]
    out["batched"] = batched
    within = None if worst is None else worst <= CONCURRENT_EACH_S
    v = [Verdict("concurrent_3_worst_alert", f"each ≤ {CONCURRENT_EACH_S} s, batched", worst,
                 None if within is None else (within and batched is not False),
                 note="cold, unique event per call" + (f"; QUEUED in {queued_rounds}/{len(b)} rounds, not batched"
                                                       if batched is False else "")),
         Verdict("concurrent_batched", "all rounds batched", f"{len(b) - queued_rounds}/{len(b)}", batched,
                 note=f"max first-token skew {max(skews) if skews else None} s")]
    if args.with_plan:
        pw = [e["plan_wall_s"] for r in rounds for e in r["each"] if e.get("plan", {}).get("ok")]
        out["plan_each_s"] = summarize(pw)
    out["verdicts"] = [x.__dict__ for x in v]
    out["saved_to"] = str(save_result("concurrent", ep, out, args.out_dir))
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--with-plan", action="store_true")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    print(f"sequential each: {out['sequential_each_s']}  concurrent each: {out['alert_each_s']}")
    for v in out["verdicts"]:
        print(f"{v['name']}: target {v['target']} measured {v['measured']} pass={v['passed']}")
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
