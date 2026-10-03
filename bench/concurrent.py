"""3 concurrent anomalies (page 10: 同時 3 件異常, vLLM 批次處理, 每件 ≤ 4 s).

Fires the first alert card for 3 different anomalies at the same instant (page-11 press 7, page-2
A-BIGOPEN, press 3 steam low) and measures each one's wall time. vLLM batches them; Ollama needs
OLLAMA_NUM_PARALLEL>=3 or it queues them (the result says which happened via per-call start skew).

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
from bench.prompts import ANOMALIES, alert_card_messages, card_schema, plan_messages, plan_schema
from warroom.factory import ALERT_CARD_MAX_TOKENS, CONCURRENT_ANOMALIES, CONCURRENT_EACH_S


async def one(ep: Endpoint, a: dict[str, Any], with_plan: bool, t_fire: float) -> dict[str, Any]:
    r = await stream_chat(ep, alert_card_messages(a), max_tokens=ALERT_CARD_MAX_TOKENS,
                          response_format=json_schema_format("AlertCard", card_schema()))
    d = {"machine_id": a["machine_id"], "alert": r.to_dict(), "alert_wall_s": round(time.perf_counter() - t_fire, 4)}
    if with_plan:
        p = await stream_chat(ep, plan_messages(a), max_tokens=2000,
                              response_format=json_schema_format("CommanderPlan", plan_schema()))
        d["plan"] = p.to_dict()
        d["plan_wall_s"] = round(time.perf_counter() - t_fire, 4)
    return d


async def main_async(args) -> dict[str, Any]:
    ep = Endpoint.from_env(args.base_url, args.model)
    out: dict[str, Any] = {"endpoint": await endpoint_info(ep), "target_each_s": CONCURRENT_EACH_S,
                           "n": CONCURRENT_ANOMALIES}
    await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)
    seq = []
    for a in ANOMALIES[:CONCURRENT_ANOMALIES]:            # sequential baseline, same prompts
        t = time.perf_counter()
        r = await stream_chat(ep, alert_card_messages(a), max_tokens=ALERT_CARD_MAX_TOKENS,
                              response_format=json_schema_format("AlertCard", card_schema()))
        seq.append(round(time.perf_counter() - t, 4) if r.ok else None)
    out["sequential_each_s"] = seq
    rounds = []
    for _ in range(args.runs):
        t_fire = time.perf_counter()
        res = await asyncio.gather(*(one(ep, a, args.with_plan, t_fire) for a in ANOMALIES[:CONCURRENT_ANOMALIES]))
        rounds.append({"each": res, "batch_wall_s": round(time.perf_counter() - t_fire, 4)})
    out["rounds"] = rounds
    each = [e["alert_wall_s"] for r in rounds for e in r["each"] if e["alert"]["ok"]]
    out["alert_each_s"] = summarize(each)
    worst = max(each) if each else None
    v = [Verdict("concurrent_3_worst_alert", f"each ≤ {CONCURRENT_EACH_S} s", worst,
                 None if worst is None else worst <= CONCURRENT_EACH_S)]
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
