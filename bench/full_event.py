"""Full-event latency (page 9: ≈22 s = 4 tool rounds + solve + diagnosis) for the page-11 press-7 event.

Headline (--mode state-machine, the default runs both): the real war-room stack end to end. The
page-11 scenario through warroom.api.Runtime with every module real (sim, detector, safety core,
tools, CP-SAT scheduler, schedule store, Qdrant SOP KB, gate, approvals, audit, state machine) and
the LLM commander on the endpoint, no mock fallback. A scripted supervisor approves at 02:16:40 and
picks night overtime at 02:31:30. full_event_p50 = commander LLM wall + every tool's wall for the
event (sim-time waits for VERIFY and the supervisor are not wall time).

Breakdown (--mode pipeline): the commander's own call twice (plan, then replan with H1 rejected; the
alert card and phone card are timed inside those streams, as in the app) with the verify reading fixed
at 119.5 °C and a small synthetic CP-SAT model. It shows per-round
LLM latency; it is not the state machine and is reported as pipeline_p50 when both run.

  uv run python -m bench.full_event --runs 3                       # both; headline = state machine
  uv run python -m bench.full_event --mode state-machine --runs 3
  uv run python -m bench.full_event --mode pipeline --think        # cost of reasoning (pipeline only)
  uv run python -m bench.full_event --mode state-machine --runs 10 --label mac   # plan evaluation, see below

Plan evaluation (--label mac|spark, edge-agent-harness 建議 3b): over the --runs state-machine runs, the commander's
schema-valid rate, the Action Gate pass rate (decisions that were not REJECT), the simulate-feasible rate
(solve_schedule), SAFE_STOP and replan counts, from each event's own audit (plan_counts). Written to
bench/results/<YYYYMMDD>-<label>-planeval/ (planeval.json + README.md, the full full_event JSON beside them), labelled
with the machine. The offline counterpart, with recorded answers instead of a model, is tests/test_plan_replay.py.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bench.llm import (
    Endpoint,
    Verdict,
    endpoint_info,
    parse_json,
    run,
    save_result,
    stream_chat,
    summarize,
)
from bench.prompts import (
    ANOMALIES,
    COMMANDER_MAX_TOKENS,
    commander_format,
    commander_messages,
    live_anomaly,
    page11_rejected_h1,
)
from warroom import factory as F
from warroom.contracts import CommanderPlan
from warroom.factory import LATENCY_FULL_EVENT_S


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
    """Scripted breakdown on the commander's own call (bench.prompts.commander_messages): R2 plan (the
    alert card is timed inside its stream, as the app's on_alert sees it), compute, R3 replan with H1
    rejected at 119.5 °C. The app makes no separate alert-card or diagnosis call -- R1 and R4 are
    derived from the R2 / R3 streams and carry no LLM time of their own."""
    a = live_anomaly(ANOMALIES[0])
    cli = ep.client(timeout_s=300)
    rounds: dict[str, Any] = {}
    t0 = time.perf_counter()
    fmt = commander_format()

    r2 = await stream_chat(ep, commander_messages(a, "plan"), max_tokens=COMMANDER_MAX_TOKENS, response_format=fmt,
                           think=think, client=cli, watch_alert_card=True)
    rounds["R1_classify_alert"] = {"ok": r2.ok and r2.alert_card_s is not None, "derived_from": "R2_plan",
                                   "alert_card_s": r2.alert_card_s, "alert_card": r2.alert_card,
                                   "note": "no separate call: the alert card leads the R2 plan stream"}
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
    extra = {"failed_expectation": {"hypothesis_id": "H1", "observed": 119.5},
             "neighbours": {F.press_id(6): {"temp_c_now": 127.8}, F.press_id(7): {"temp_c_now": 119.5},
                            F.press_id(8): {"temp_c_now": 127.9}},
             "results": {**compute, "verify": verify}}
    r3 = await stream_chat(ep, commander_messages(a, "replan", extra=extra, rejected=page11_rejected_h1()),
                           max_tokens=COMMANDER_MAX_TOKENS, response_format=fmt, think=think, client=cli,
                           watch_alert_card=True)
    rounds["R3_replan"] = r3.to_dict() | {"plan_valid": _valid_plan(r3.text)}
    rounds["R4_diagnosis"] = {"ok": r3.ok and r3.alert_card_s is not None, "derived_from": "R3_replan",
                              "alert_card_s": r3.alert_card_s, "alert_card": r3.alert_card,
                              "note": "no separate call: the phone card is the R3 replan's alert card"}
    await cli.close()
    total = time.perf_counter() - t0
    llm_ok = all(rounds[k]["ok"] for k in ("R1_classify_alert", "R2_plan", "R3_replan", "R4_diagnosis"))
    return {"total_s": round(total, 3), "llm_ok": llm_ok, "rounds": rounds,
            "tokens_out": sum((rounds[k].get("completion_tokens") or 0) for k in ("R2_plan", "R3_replan"))}


def _valid_plan(text: str) -> bool:
    d = parse_json(text)
    if not isinstance(d, dict):
        return False
    try:
        CommanderPlan.model_validate(d)
        return True
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ plan evaluation (--label, edge-agent-harness 3b)
PLANEVAL_LABELS = ("mac", "spark")


def plan_counts(entries: list[Any], ev: Any) -> dict[str, Any]:
    """One event's plan-quality counts from its own audit rows (AuditEntry) and the Event:
      schema   commander answers that parsed and validated into the plan that ran (plan_source=commander) / all
      gate     Action Gate decisions that were not REJECT / all (consecutive repeats of one decision count once; a
               write the gate dropped counts as a reject)
      sim      solve_schedule results that came back ok and feasible / all solve_schedule results
      safe_stop transitions into SAFE_STOP, replans = Event.replans."""
    from warroom.contracts import AuditKind, EventState, GateVerdict
    calls = [e.payload for e in entries if e.kind is AuditKind.LLM_CALL and e.actor == "commander"]
    schema_ok = sum(1 for c in calls if c.get("plan_source") == "commander" and c.get("valid_json", True)
                    and not c.get("fell_back_to_mock") and not c.get("error"))
    gate_total = gate_ok = 0
    last = None
    for e in entries:
        if e.actor != "gate" or e.kind not in (AuditKind.GATE, AuditKind.INTERCEPTED):
            continue
        p = e.payload
        if "decision" in p:
            verdict = (p["decision"] or {}).get("verdict")
        elif p.get("dropped") == "rejected":
            verdict = GateVerdict.REJECT.value
        else:
            continue
        key = (p.get("step_id"), json.dumps(p.get("decision"), sort_keys=True, default=str), verdict)
        if key == last:
            continue
        last = key
        gate_total += 1
        gate_ok += verdict != GateVerdict.REJECT.value
    solves = [e.payload for e in entries if e.kind is AuditKind.TOOL_RESULT and e.actor == "tool:solve_schedule"]
    feasible = sum(1 for p in solves if p.get("ok") and (p.get("data") or {}).get("feasible"))
    safe_stops = sum(1 for e in entries if e.kind is AuditKind.STATE_TRANSITION
                     and e.payload.get("to") == EventState.SAFE_STOP.value)
    return {"schema": [schema_ok, len(calls)], "gate": [gate_ok, gate_total], "sim": [feasible, len(solves)],
            "safe_stop": safe_stops, "replans": int(getattr(ev, "replans", 0) or 0),
            "final_state": getattr(getattr(ev, "state", None), "value", None)}


def _rate(pairs: list[list[int]]) -> dict[str, Any]:
    ok, n = sum(p[0] for p in pairs), sum(p[1] for p in pairs)
    return {"ok": ok, "total": n, "rate": round(ok / n, 3) if n else None}


def planeval_summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Pool the per-run plan_counts of the runs that ran (skipped / fell-back runs are counted, not pooled)."""
    done = [r["planeval"] for r in runs if isinstance(r.get("planeval"), dict)]
    return {
        "runs": len(runs),
        "runs_measured": len(done),
        "runs_skipped": sum(1 for r in runs if "skipped" in r),
        "schema_valid": _rate([d["schema"] for d in done]),
        "gate_pass": _rate([d["gate"] for d in done]),
        "sim_feasible": _rate([d["sim"] for d in done]),
        "safe_stop_total": sum(d["safe_stop"] for d in done),
        "safe_stop_runs": sum(1 for d in done if d["safe_stop"]),
        "replans_total": sum(d["replans"] for d in done),
        "replans_per_run": round(sum(d["replans"] for d in done) / len(done), 3) if done else None,
        "closed": _rate([[d["final_state"] == "closed", 1] for d in done]),
        "per_run": done,
    }


def planeval_dir(label: str, root: Path | None = None, now: datetime | None = None) -> Path:
    from bench.llm import RESULTS_DIR
    if label not in PLANEVAL_LABELS:
        raise ValueError(f"--label must be one of {PLANEVAL_LABELS}, not {label!r}")
    now = now or datetime.now(UTC).astimezone()
    return (root or RESULTS_DIR) / f"{now:%Y%m%d}-{label}-planeval"


def write_planeval(label: str, out: dict[str, Any], root: Path | None = None, now: datetime | None = None) -> Path:
    """bench/results/<YYYYMMDD>-<label>-planeval/: planeval.json (everything) and README.md (what was run, how to read
    it). The numbers live only here, labelled with the machine; docs do not quote them."""
    from bench.llm import host_info
    now = now or datetime.now(UTC).astimezone()
    d = planeval_dir(label, root, now)
    d.mkdir(parents=True, exist_ok=True)
    pe = out["planeval"]
    ep = out.get("endpoint") or {}
    machine = {"mac": "Mac", "spark": "DGX Spark"}[label]
    payload = {"bench": "full_event --label", "label": label, "machine": machine, "recorded_at": now.isoformat(),
               "host": host_info(), "endpoint": ep, "scenario": "page 11 press-7 (state machine, real Runtime)",
               "planeval": pe, "full_event_result": out.get("saved_to")}
    (d / "planeval.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
                                     encoding="utf-8")

    def r(k: str) -> str:
        x = pe[k]
        return f"{x['ok']}/{x['total']}" + (f" ({x['rate']})" if x["rate"] is not None else "")
    rows = [("schema 合格（指揮官輸出通過解析與驗證）", r("schema_valid")),
            ("gate 通過（不是 REJECT 的決定）", r("gate_pass")),
            ("模擬可行（solve_schedule feasible）", r("sim_feasible")),
            ("SAFE_STOP 次數（有 SAFE_STOP 的 run 數）", f"{pe['safe_stop_total']} ({pe['safe_stop_runs']} runs)"),
            ("replan 次數（每 run 平均）", f"{pe['replans_total']} ({pe['replans_per_run']})"),
            ("CLOSED", r("closed"))]
    lines = [f"# 指揮官計畫合格率 — {machine}", "",
             (f"`bench/full_event.py --mode state-machine --runs {pe['runs']} --label {label}` 在 {machine} 上跑的結果，"
              f"{now:%Y-%m-%d %H:%M}。情境：p.11 7 號硫化機降溫，真的 Runtime 跑完整狀態機。"),
             "", f"- 模型：`{ep.get('model')}`，端點：`{ep.get('base_url')}`",
             f"- 跑了 {pe['runs']} 次，有量到 {pe['runs_measured']} 次，略過 {pe['runs_skipped']} 次", "",
             "| 項目 | 結果 |", "|---|---|", *[f"| {a} | {b} |" for a, b in rows], "",
             ("每一次 run 的計數在 `planeval.json` 的 `planeval.per_run`；完整的 full_event 結果在 "
              f"`{Path(str(out.get('saved_to') or '')).name}`。計數方式見 `bench/full_event.py` 的 `plan_counts`。"), ""]
    (d / "README.md").write_text("\n".join(lines), encoding="utf-8")
    return d


SUPERVISOR_SCRIPT = (            # page 11: approve the solver's plan at 02:16:40, then at 02:31:30 reject the
    ("02:16:40", "approve", None, "同意"),               # cross-team move and pick night overtime (option A)
    ("02:31:30", "modify", "夜班加班", "不跨組調人，改夜班加班"),
)


async def via_state_machine(ep: Endpoint, max_wall_s: float = 900.0,
                            workdir: Path | None = None) -> dict[str, Any]:
    """The headline full event: the page-11 press-7 scenario through the real Runtime with every
    module real (sim, detector, safety core, tools, CP-SAT scheduler, schedule store, Qdrant SOP KB,
    gate, approvals, audit, state machine) and the LLM commander on the endpoint (no mock fallback).
    The supervisor decides on the page-11 script in sim time.

    The plant runs one sim second per tick and holds while the commander or solver works (the
    Runtime's accelerated-demo rule), so sim-time waits (VERIFY's 15 min, the supervisor) cost no wall
    time. compute_s = commander LLM wall + every tool's wall for this event: the page-9 ≈22 s
    (tool rounds + solve + diagnosis), with no human or verify wait in it. Returns {"skipped": why}
    when the stack cannot be built."""
    import tempfile
    try:
        from warroom.api import Runtime
        from warroom.config import Settings
        from warroom.contracts import (
            TERMINAL_STATES,
            ApprovalDecision,
            AuditKind,
            DecisionKind,
            LLMMode,
            ScenarioName,
        )
        tmp = workdir or Path(tempfile.mkdtemp(prefix="bench-full-event-"))
        settings = Settings(llm_mode=LLMMode.LLM, base_url=ep.base_url, model=ep.model,
                            api_key=ep.api_key, llm_fallback_to_mock=False,
                            audit_path=tmp / "audit.jsonl", schedule_dir=tmp / "sched",
                            qdrant_path=tmp / "qdrant", scenario=ScenarioName.PRESS7_TEMP_DROP.value)
        rt = Runtime(settings, allow_fallback=False, run_loop=False)
    except NotImplementedError as e:
        return {"skipped": f"warroom module still a stub: {e!r}"}
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"{type(e).__name__}: {e}"[:500]}

    def at(hms: str):
        h, m, s = map(int, hms.split(":"))
        return rt.now().replace(hour=h, minute=m, second=s, microsecond=0)

    script = [(at(t), kind, label, reason) for t, kind, label, reason in SUPERVISOR_SCRIPT]
    decisions: list[dict[str, Any]] = []
    held_wall = 0.0
    try:
        await rt.start()
        rt._ingest_kb()                                     # index the SOPs before the event, outside the clock
        t_wall0 = time.perf_counter()
        t_event0: float | None = None
        ev = None
        deadline = at("03:30:00")
        while rt.now() < deadline and time.perf_counter() - t_wall0 < max_wall_s:
            await rt.tick(1.0, publish=False, hold_compute=False)
            th = time.perf_counter()
            await rt._hold_for_compute()
            held_wall += time.perf_counter() - th
            evs = [e for e in rt.events() if e.machine_id == F.press_id(7)]
            if evs and t_event0 is None:
                t_event0 = time.perf_counter()
            ev = evs[0] if evs else None
            for req in rt.approvals.pending():
                if not script or rt.now() < script[0][0]:
                    continue
                _, kind, label, reason = script.pop(0)
                opt = next((o for o in req.options if o.label == label), None) if label else \
                    next((o for o in req.options if o.recommended), req.options[-1] if req.options else None)
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind(kind),
                                                     option_id=opt.id if opt else None, reason=reason, via="phone"))
                decisions.append({"at": rt.now().strftime("%H:%M:%S"), "title": req.title, "decision": kind,
                                  "option": opt.label if opt else None,
                                  "offered": [o.label for o in req.options]})
            if ev is not None and ev.state in TERMINAL_STATES:
                break
        wall = time.perf_counter() - (t_event0 or t_wall0)
        if ev is None:
            return {"skipped": "no press-7 event was raised", "sim_end": rt.now().isoformat()}

        entries = [e for e in rt.audit.tail(0, limit=0) if e.event_id == ev.id]
        llm = [e.payload for e in entries if e.kind is AuditKind.LLM_CALL]
        tool_entries = [e for e in entries if e.kind is AuditKind.TOOL_RESULT]
        tools = [e.payload for e in tool_entries]
        llm_s = sum((p.get("total_ms") or 0) for p in llm) / 1000
        tool_s = sum((p.get("duration_ms") or 0) for p in tools) / 1000
        solve_s = sum((e.payload.get("duration_ms") or 0) for e in tool_entries
                      if e.actor == "tool:solve_schedule") / 1000
        timeline = " | ".join(t.text for t in ev.timeline)
        return {
            "final_state": ev.state.value,
            "closed": ev.state.value == "closed",
            "compute_s": round(llm_s + tool_s, 3),
            "llm_s": round(llm_s, 3),
            "tools_s": round(tool_s, 3),
            "solve_s": round(solve_s, 3),
            "held_wall_s": round(held_wall, 3),
            "wall_s_incl_sim_ticks": round(wall, 3),
            "sim_span": f"{ev.anomaly.ts:%H:%M:%S}->{rt.now():%H:%M:%S}",
            "replans": ev.replans,
            "hypotheses": [{"id": h.id, "cause": h.cause_code, "status": h.status.value} for h in ev.hypotheses],
            "llm_calls": [{k: p.get(k) for k in ("phase", "ttft_ms", "total_ms", "prompt_tokens",
                                                  "completion_tokens", "valid_json", "fell_back_to_mock", "error",
                                                  "hypothesis", "steps", "safe_stop")} for p in llm],
            "llm_fell_back": any(p.get("fell_back_to_mock") for p in llm),
            "tool_calls": len(tools),
            "tools_failed": sum(1 for p in tools if not p.get("ok")),
            "decisions": decisions,
            "planeval": plan_counts(entries, ev),
            "page11_checks": {
                "h1_rejected": "H1 不成立" in timeline,
                "cure_36_5h": "36.5" in timeline,
                "a311_to_press9": "#A-311" in timeline and "9 號機" in timeline,
                "press7_out_of_service": rt.sim.machine(F.press_id(7)).status.value == "out_of_service",
                "audit_chain_ok": rt.audit.verify_chain(),
            },
            "workdir": str(tmp),
        }
    except Exception as e:  # noqa: BLE001
        return {"skipped": f"{type(e).__name__}: {e}"[:500]}
    finally:
        await rt.stop()


async def main_async(args) -> dict[str, Any]:
    ep = Endpoint.from_env(args.base_url, args.model)
    mode = "both" if args.via_state_machine and args.mode == "pipeline" else args.mode
    out: dict[str, Any] = {"endpoint": await endpoint_info(ep), "target_s": LATENCY_FULL_EVENT_S,
                           "think": args.think, "mode": mode, "loadavg_at_start": list(os.getloadavg())}
    await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)
    verdicts: list[Verdict] = []

    if mode in ("state-machine", "both"):
        if args.think:
            sm_runs = [{"skipped": "--think applies to the scripted pipeline only; the commander forces "
                                   "reasoning off"}]
        else:
            sm_runs = [await via_state_machine(ep) for _ in range(args.runs)]
        done = [r for r in sm_runs if "compute_s" in r and not r.get("llm_fell_back")]
        ok = [r for r in done if r.get("closed")]
        out["state_machine"] = {"runs": sm_runs, "compute_s": summarize([r["compute_s"] for r in ok]),
                                "compute_s_all_runs": summarize([r["compute_s"] for r in done]),
                                "llm_s": summarize([r["llm_s"] for r in ok]),
                                "closed_rate": round(len(ok) / len(sm_runs), 3) if sm_runs else None}
        target = f"≈ {LATENCY_FULL_EVENT_S} s (≤ target), event CLOSED"
        if ok:
            p50 = out["state_machine"]["compute_s"]["p50"]
            verdicts.append(Verdict("full_event_p50", target, p50, p50 <= LATENCY_FULL_EVENT_S,
                                    note=f"real state machine, {len(ok)}/{len(sm_runs)} closed"))
        elif done:                 # it ran end to end but never closed: that is a FAIL, not "not measured"
            ends = ", ".join(sorted({str(r.get("final_state")) for r in done}))
            verdicts.append(Verdict("full_event_p50", target, out["state_machine"]["compute_s_all_runs"]["p50"],
                                    False, note=f"real state machine, 0/{len(sm_runs)} closed (ended {ends}); "
                                                "measured = compute of the runs that did not close"))
        else:
            why = "; ".join(str(r.get("skipped")) for r in sm_runs)[:200]
            verdicts.append(Verdict("full_event_p50", target, None, None, note=f"state machine not run: {why}"))

    if mode in ("pipeline", "both"):
        runs = [await bench_pipeline(ep, args.think) for _ in range(args.runs)]
        out["runs"] = runs                                       # breakdown: per-round LLM latency
        out["total_s"] = summarize([r["total_s"] for r in runs if r["llm_ok"]])
        out["plan_valid_rate"] = round(sum(r["rounds"]["R2_plan"]["plan_valid"] + r["rounds"]["R3_replan"]["plan_valid"]
                                           for r in runs) / (2 * len(runs)), 3)
        p50 = out["total_s"].get("p50")
        name = "full_event_p50" if mode == "pipeline" else "pipeline_p50"
        verdicts.append(Verdict(name, f"≈ {LATENCY_FULL_EVENT_S} s (≤ target)", p50,
                                None if p50 is None else p50 <= LATENCY_FULL_EVENT_S,
                                note="scripted breakdown (commander plan + replan calls), synthetic solve; not the state machine"))
    out["verdicts"] = [v.__dict__ for v in verdicts]
    label = getattr(args, "label", None)
    if label:
        out["label"] = label
        out["planeval"] = planeval_summary(out.get("state_machine", {}).get("runs", []))
        if out["planeval"]["runs_measured"]:
            now = datetime.now(UTC).astimezone()
            pdir = planeval_dir(label, args.out_dir, now)        # the full result goes next to planeval.json
            out["saved_to"] = str(save_result("full_event", ep, out, pdir))
            out["planeval_dir"] = str(write_planeval(label, out, args.out_dir, now))
        else:                                # nothing measured (endpoint down, every run skipped): no labelled folder
            out["planeval_dir"] = None
            out["saved_to"] = str(save_result("full_event", ep, out, args.out_dir))
    else:
        out["saved_to"] = str(save_result("full_event", ep, out, args.out_dir))
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--think", action="store_true", help="let the model reason on plan/replan rounds")
    p.add_argument("--mode", choices=("both", "state-machine", "pipeline"), default="both",
                   help="state-machine = the headline (real Runtime); pipeline = scripted commander plan/replan breakdown")
    p.add_argument("--via-state-machine", action="store_true", help="kept for old scripts; same as --mode both")
    p.add_argument("--out-dir", type=Path)
    p.add_argument("--label", choices=PLANEVAL_LABELS,
                   help="plan evaluation over --runs state-machine runs (schema / gate / sim-feasible rates, SAFE_STOP "
                        "and replan counts) into <out-dir or bench/results>/<date>-<label>-planeval/")
    args = p.parse_args(argv)
    if args.label and args.mode == "pipeline" and not args.via_state_machine:
        p.error("--label needs the state-machine runs: use --mode state-machine or both")
    if args.label and args.think:
        p.error("--label measures the commander as the app runs it (reasoning off): drop --think")
    out = run(main_async(args))
    if "state_machine" in out:
        sm = out["state_machine"]
        print(f"full event (state machine) compute_s: {sm['compute_s']}  closed_rate={sm['closed_rate']}")
        for r in sm["runs"]:
            print("  run:", {k: r.get(k) for k in ("final_state", "compute_s", "llm_s", "tools_s", "solve_s",
                                                   "replans", "skipped")})
    if "total_s" in out:
        print(f"pipeline breakdown total: {out['total_s']}  plan_valid_rate={out['plan_valid_rate']}")
    if "planeval" in out:
        pe = out["planeval"]
        print(f"planeval ({out['label']}): schema {pe['schema_valid']['rate']}  gate {pe['gate_pass']['rate']}  "
              f"sim {pe['sim_feasible']['rate']}  safe_stop {pe['safe_stop_total']}  replans {pe['replans_total']}")
        print("planeval saved", out["planeval_dir"] or "nowhere: no run was measured, no planeval folder written")
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
