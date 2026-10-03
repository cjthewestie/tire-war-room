"""Full-event latency (page 9: ≈22 s = 4 tool rounds + solve + diagnosis) for the page-11 press-7 event.

Rounds, each one real LLM call against the endpoint:
  R1 classify  -> first alert card (≤100 tokens)
  R2 plan      -> CommanderPlan JSON (H1 steam pressure low, S1..S5)
  R3 replan    -> CommanderPlan JSON after verify read 119.5°C (H1 rejected -> H2)
  R4 diagnosis -> supervisor phone card text
plus the deterministic compute the commander never guesses: Arrhenius cure formula and a CP-SAT solve.

  uv run python -m bench.full_event --runs 3
  uv run python -m bench.full_event --via-state-machine     # once warroom.agent/tools are implemented

--via-state-machine drives the real warroom StateMachine (LLM_MODE=llm) instead of the bench pipeline.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import time
from pathlib import Path
from typing import Any

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, parse_json, run, save_result, \
    stream_chat, summarize
from bench.prompts import ANOMALIES, alert_card_messages, card_schema, diagnosis_messages, plan_messages, \
    plan_schema
from warroom import factory as F
from warroom.contracts import CommanderPlan
from warroom.factory import ALERT_CARD_MAX_TOKENS, LATENCY_FULL_EVENT_S


def arrhenius_total_h(size_h: float = 28.0, done_h: float = 10.0, t_ref_c: float = 128.0,
                      t_new_c: float = 122.0, ea_kj: float = F.ACTIVATION_ENERGY_KJ_MOL) -> float:
    """Equivalent-cure total hours when the press drops from t_ref to t_new partway through (page 11)."""
    t1, t2 = t_ref_c + 273.15, t_new_c + 273.15
    factor = math.exp(ea_kj * 1000 / F.GAS_CONSTANT_J_MOL_K * (1 / t2 - 1 / t1))
    return done_h + (size_h - done_h) * factor


def cpsat_reschedule(time_limit_s: float = 2.0) -> dict[str, Any]:
    """Small real CP-SAT model: move the M/L jobs off CURE-07 onto the other M/L presses, minimise lateness."""
    from ortools.sat.python import cp_model
    presses = [p for p in F.ML_PRESSES if p != F.press_id(7)]
    jobs = [("J1", 34, 72), ("J2", 28, 40), ("J3", 34, 96), ("J4", 28, 60), ("J5", 34, 120)]   # synthetic load, not the demo orders
    horizon = 400
    m = cp_model.CpModel()
    assign, ivs = {}, {p: [] for p in presses}
    late = []
    for j, dur, due in jobs:
        start = m.NewIntVar(0, horizon, f"s{j}")
        end = m.NewIntVar(0, horizon, f"e{j}")
        lit = []
        for p in presses:
            b = m.NewBoolVar(f"{j}@{p}")
            ivs[p].append(m.NewOptionalIntervalVar(start, dur + 1, end, b, f"i{j}{p}"))
            assign[j, p] = b
            lit.append(b)
        m.AddExactlyOne(lit)
        lt = m.NewIntVar(0, horizon, f"l{j}")
        m.Add(lt >= end - due)
        late.append(lt)
    for p in presses:
        m.AddNoOverlap(ivs[p])
    m.Minimize(sum(late))
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = time_limit_s
    s.parameters.num_workers = 8
    t0 = time.perf_counter()
    st = s.Solve(m)
    wall = time.perf_counter() - t0
    placed = {j: p for (j, p), b in assign.items() if s.Value(b)} if st in (cp_model.OPTIMAL, cp_model.FEASIBLE) else {}
    return {"status": s.StatusName(st), "solve_s": round(wall, 4), "total_lateness_h": s.ObjectiveValue()
            if placed else None, "placement": placed}


async def bench_pipeline(ep: Endpoint, think: bool) -> dict[str, Any]:
    a = ANOMALIES[0]
    cli = ep.client(timeout_s=300)
    rounds: dict[str, Any] = {}
    t0 = time.perf_counter()

    r1 = await stream_chat(ep, alert_card_messages(a), max_tokens=ALERT_CARD_MAX_TOKENS,
                           response_format=json_schema_format("AlertCard", card_schema()), client=cli)
    rounds["R1_classify_alert"] = r1.to_dict()

    fmt = json_schema_format("CommanderPlan", plan_schema())
    r2 = await stream_chat(ep, plan_messages(a, "plan"), max_tokens=2000, response_format=fmt,
                           think=think, client=cli)
    rounds["R2_plan"] = r2.to_dict() | {"plan_valid": _valid_plan(r2.text)}

    tc = time.perf_counter()
    total_h = arrhenius_total_h()
    solve = cpsat_reschedule()
    compute = {"cure_formula": {"total_cure_h": round(total_h, 1), "extra_h": round(total_h - 28.0, 1)},
               "solve_schedule": solve, "compute_s": round(time.perf_counter() - tc, 4)}
    rounds["compute"] = compute

    verify = {"expectation": "CURE-07 temp_c >= 121 within 30 min", "observed_c": 119.5, "H1": "rejected",
              "safety_core": "CURE-07 CURE_HOLD already applied by rule",
              "neighbours": {F.press_id(6): 127.8, F.press_id(8): 127.9}}
    r3 = await stream_chat(ep, plan_messages(a, "replan", context={**compute, "verify": verify}),
                           max_tokens=2000, response_format=fmt, think=think, client=cli)
    rounds["R3_replan"] = r3.to_dict() | {"plan_valid": _valid_plan(r3.text)}

    r4 = await stream_chat(ep, diagnosis_messages(a, {**compute, "verify": verify}), max_tokens=300,
                           client=cli)
    rounds["R4_diagnosis"] = r4.to_dict()
    await cli.close()
    total = time.perf_counter() - t0
    llm_ok = all(rounds[k]["ok"] for k in ("R1_classify_alert", "R2_plan", "R3_replan", "R4_diagnosis"))
    return {"total_s": round(total, 3), "llm_ok": llm_ok, "rounds": rounds,
            "tokens_out": sum((rounds[k].get("completion_tokens") or 0) for k in rounds if k.startswith("R"))}


def _valid_plan(text: str) -> bool:
    d = parse_json(text)
    if not isinstance(d, dict):
        return False
    try:
        CommanderPlan.model_validate(d)
        return True
    except Exception:  # noqa: BLE001
        return False


async def via_state_machine(ep: Endpoint) -> dict[str, Any]:
    """Drive the real warroom StateMachine with the real commander over the endpoint. Returns
    {"skipped": reason} while the agent/tools modules are still stubs."""
    try:
        from warroom.config import Settings
        from warroom.contracts import Anomaly, AnomalyKind, Channel, LLMMode, Severity
        from warroom.agent import ApprovalBrokerImpl, JsonlAuditLog, RuleGate, StateMachine, make_commander
        from warroom.tools import ToolContext, build_registry
        from warroom.clock import SimClock
        from datetime import datetime
        import tempfile
        clock = SimClock(start=datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ), manual=True)
        settings = Settings(llm_mode=LLMMode.LLM, base_url=ep.base_url, model=ep.model,
                            llm_fallback_to_mock=False)
        tmp = Path(tempfile.mkdtemp())
        audit = JsonlAuditLog(tmp / "audit.jsonl", clock=clock)
        approvals = ApprovalBrokerImpl(audit=audit, clock=clock)
        try:
            from warroom.sim import Factory
            from warroom.contracts import ScenarioName
            sim = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
        except Exception:  # noqa: BLE001
            sim = None
        tools = build_registry(ToolContext(sim=sim, audit=audit, clock=clock))
        sm = StateMachine(make_commander(settings), tools, RuleGate(), approvals, audit, sim=sim, clock=clock)
        an = Anomaly(id="AN-bench", ts=clock.now(), machine_id=F.press_id(7), kind=AnomalyKind.TEMP_DROP,
                     channel=Channel.TEMP, value=122.0, baseline=128.0, severity=Severity.CRITICAL,
                     message=ANOMALIES[0]["message"])

        async def auto_approve():
            from warroom.contracts import ApprovalDecision, DecisionKind
            while True:
                for req in approvals.pending():
                    approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                      option_id=req.options[0].id if req.options else None,
                                                      via="auto", reason="bench auto-approve"))
                await asyncio.sleep(0.05)

        approver = asyncio.create_task(auto_approve())
        t0 = time.perf_counter()
        try:
            ev = await asyncio.wait_for(sm.handle(an), timeout=300)
        finally:
            approver.cancel()
        return {"total_s": round(time.perf_counter() - t0, 3), "final_state": ev.state.value,
                "replans": ev.replans, "steps": len(ev.steps)}
    except NotImplementedError as e:
        return {"skipped": f"warroom module still a stub: {e!r}"}
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"{type(e).__name__}: {e}"}


async def main_async(args) -> dict[str, Any]:
    ep = Endpoint.from_env(args.base_url, args.model)
    out: dict[str, Any] = {"endpoint": await endpoint_info(ep), "target_s": LATENCY_FULL_EVENT_S,
                           "think": args.think}
    await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)
    runs = [await bench_pipeline(ep, args.think) for _ in range(args.runs)]
    out["runs"] = runs
    out["total_s"] = summarize([r["total_s"] for r in runs if r["llm_ok"]])
    out["plan_valid_rate"] = round(sum(r["rounds"]["R2_plan"]["plan_valid"] + r["rounds"]["R3_replan"]["plan_valid"]
                                       for r in runs) / (2 * len(runs)), 3)
    p50 = out["total_s"].get("p50")
    verdicts = [Verdict("full_event_p50", f"≈ {LATENCY_FULL_EVENT_S} s (≤ target)", p50,
                        None if p50 is None else p50 <= LATENCY_FULL_EVENT_S)]
    if args.via_state_machine:
        out["state_machine"] = await via_state_machine(ep)
    out["verdicts"] = [v.__dict__ for v in verdicts]
    out["saved_to"] = str(save_result("full_event", ep, out, args.out_dir))
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--think", action="store_true", help="let the model reason on plan/replan rounds")
    p.add_argument("--via-state-machine", action="store_true")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    print(f"full event total: {out['total_s']}  plan_valid_rate={out['plan_valid_rate']}")
    if "state_machine" in out:
        print("state machine:", out["state_machine"])
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
