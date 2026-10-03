"""3 concurrent anomalies (page 10: 同時 3 件異常, vLLM 批次處理, 每件 ≤ 4 s).

Fires the commander's real call (bench.prompts.commander_messages: SYSTEM_PROMPT + CommanderRequest,
CommanderPlan response_format) for 3 different anomalies at the same instant (page-11 press 7, page-2
A-BIGOPEN, press 3 steam low) and measures each one's time to its first alert card -- the alert_card
field leading the plan stream, which is when the app's on_alert fires. The streams run to the end of the
plan, as in the app, so the three decodes really overlap. Every call is a new event (unique id + live
sensor values), so each one prefills its request cold; only the commander's system prompt is shared.

vLLM batches them; Ollama needs OLLAMA_NUM_PARALLEL>=3 or it queues them. The bench checks which happened:
if any call's first token arrives only after another call has finished, the round was queued and the
verdict FAILS -- meeting 4 s by serving them one after another is not the batching page 10 claims.

  uv run python -m bench.concurrent --runs 3
  (each round also reports every anomaly's full-plan wall time as plan_each_s)
"""
from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, run, save_result, stream_chat, summarize
from bench.prompts import ANOMALIES, COMMANDER_MAX_TOKENS, commander_format, commander_messages, live_anomaly
from warroom.factory import CONCURRENT_ANOMALIES, CONCURRENT_EACH_S

PROMPT_LABEL = ("commander: warroom.agent.commander.build_messages(CommanderRequest); alert = alert_card field "
                "of the CommanderPlan stream, stream runs to the end of the plan")


async def _commander(ep: Endpoint, a: dict[str, Any], stop_on_card: bool = False):
    return await stream_chat(ep, commander_messages(a), max_tokens=COMMANDER_MAX_TOKENS,
                             response_format=commander_format(), watch_alert_card=True,
                             stop_on_alert_card=stop_on_card)


async def one(ep: Endpoint, a: dict[str, Any], with_plan: bool, t_fire: float) -> dict[str, Any]:
    """One anomaly: the commander's plan stream. alert_wall_s = fire -> alert_card complete; the stream
    keeps decoding the rest of the plan (plan_wall_s), so concurrent decodes overlap as in the app.
    `with_plan` is kept for older callers; the plan is always streamed now."""
    a = live_anomaly(a)                       # new event id + live values: cold prefill after the system prefix
    send_off = time.perf_counter() - t_fire
    r = await _commander(ep, a)
    card_off = None if r.alert_card_s is None else round(send_off + r.alert_card_s, 4)
    alert = r.to_dict() | {"ok": r.ok and r.alert_card_s is not None}
    return {"machine_id": a["machine_id"], "event_id": a["event_id"], "alert": alert,
            "send_off_s": round(send_off, 4),
            "first_token_off_s": None if r.ttft_any_s is None else round(send_off + r.ttft_any_s, 4),
            "card_off_s": card_off,
            # batching is judged on the card: did any call's first token wait for another call's card?
            "end_off_s": card_off if card_off is not None else round(send_off + r.total_s, 4),
            "alert_wall_s": card_off,
            "plan": {"ok": r.ok, "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens},
            "plan_wall_s": round(send_off + r.total_s, 4)}


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
                           "n": CONCURRENT_ANOMALIES, "prompt": PROMPT_LABEL}
    await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)
    # prime the commander's system prompt once (boot preload does this in production)
    await _commander(ep, live_anomaly(ANOMALIES[0]), stop_on_card=True)
    seq = []
    for a in ANOMALIES[:CONCURRENT_ANOMALIES]:            # sequential baseline, cold (new event each)
        r = await _commander(ep, live_anomaly(a), stop_on_card=True)
        seq.append(round(r.alert_card_s, 4) if r.ok and r.alert_card_s is not None else None)
    out["sequential_each_s"] = seq
    rounds = []
    for _ in range(args.runs):
        t_fire = time.perf_counter()
        res = await asyncio.gather(*(one(ep, a, args.with_plan, t_fire) for a in ANOMALIES[:CONCURRENT_ANOMALIES]))
        rounds.append({"each": res, "batch_wall_s": round(time.perf_counter() - t_fire, 4),
                       "batching": batching(res)})
    out["rounds"] = rounds
    each = [e["alert_wall_s"] for r in rounds for e in r["each"] if e["alert"]["ok"]]
    out["prompt_tokens"] = summarize([e["plan"]["prompt_tokens"] for r in rounds for e in r["each"]])
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
                 note="commander plan stream, alert_card complete; cold, unique event per call" + (f"; QUEUED in {queued_rounds}/{len(b)} rounds, not batched"
                                                       if batched is False else "")),
         Verdict("concurrent_batched", "all rounds batched", f"{len(b) - queued_rounds}/{len(b)}", batched,
                 note=f"max first-token skew {max(skews) if skews else None} s")]
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
    p.add_argument("--with-plan", action="store_true", help="no-op: the commander's plan is always streamed")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    print(f"sequential each: {out['sequential_each_s']}  concurrent each: {out['alert_each_s']}")
    for v in out["verdicts"]:
        print(f"{v['name']}: target {v['target']} measured {v['measured']} pass={v['passed']}")
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
