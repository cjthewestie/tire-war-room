"""Collect the newest result of every benchmark per host+model into bench/results/SUMMARY.md.

  uv run python -m bench.report
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bench.llm import RESULTS_DIR

ORDER = ["ttft", "concurrent", "full_event", "full_event-think", "accuracy", "accuracy-open-book",
         "accuracy-open-book-cite", "memory", "unplug", "tokens_per_watt"]


def latest(results_dir: Path) -> dict[tuple[str, str, str], dict[str, Any]]:
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for p in sorted(results_dir.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        model = (d.get("endpoint") or {}).get("model") or "+".join(
            r["endpoint"]["model"] for r in d.get("results", []) if "endpoint" in r) or "-"
        bench = (d.get("bench", p.stem.split("-")[0]) + ("-open-book" if d.get("open_book") else "")
                 + ("-cite" if d.get("cite") else "") + ("-think" if d.get("think") else ""))
        key = (bench, d.get("host", {}).get("hostname", "?"), model)
        if key not in best or d.get("recorded_at", "") >= best[key].get("recorded_at", ""):
            best[key] = d | {"_file": p.name}
    return best


def rows(d: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    out = []
    for v in d.get("verdicts", []):
        out.append((v["name"], str(v["target"]), _fmt(v["measured"]),
                    {True: "PASS", False: "FAIL", None: "n/a"}[v["passed"]] + (f" ({v['note']})" if v.get("note") else "")))
    b = d.get("bench")
    if b == "full_event":
        sm = d.get("state_machine")
        if isinstance(sm, dict) and "closed_rate" in sm:
            out.append(("state_machine_closed_rate", "1.0", _fmt(sm.get("closed_rate")),
                        "real Runtime, LLM commander, page-11 script"))
        if "plan_valid_rate" in d:
            out.append(("plan_valid_rate", "1.0", _fmt(d.get("plan_valid_rate")),
                        "reasoning ON" if d.get("think") else "reasoning OFF"))
    if b == "memory":
        out.append(("stack_gb", f"≤ {d['band_gb'][1]} GB incl. OS", _fmt(d["stack_gb"]),
                    "PASS" if d["stack_within_band"] else "FAIL"))
    if b == "tokens_per_watt":
        pl = d.get("plug", {})
        out.append(("tokens_per_s", "-", _fmt(pl.get("tokens_per_s")), "measured"))
        out.append(("tokens_per_joule", "publish", _fmt(pl.get("tokens_per_joule")),
                    "plug meter" if pl.get("avg_watts") else "n/a (no plug reading given)"))
        pk = d.get("package_power_powermetrics") or {}
        if pk.get("tokens_per_joule"):
            out.append(("tokens_per_joule_package", "(not wall power)", _fmt(pk["tokens_per_joule"]), "powermetrics"))
    if b == "accuracy":
        for r in d.get("results", []):
            out.append((f"quiz_{r['endpoint']['model']}", "-", f"{r['score_points']} pts", f"n={r['n']}"))
    return out


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def open_items(best: dict[tuple[str, str, str], dict[str, Any]]) -> list[str]:
    """Page-10 targets that failed or were never measured, stated plainly from the newest results."""
    out: list[str] = []
    ds = list(best.values())
    by = lambda b: [d for (k, _, _), d in best.items() if k == b]          # noqa: E731
    spark = [d for d in ds if (d.get("endpoint") or {}).get("kind") == "vllm"
             or "spark" in str(d.get("host", {}).get("hostname", "")).lower()]
    if not spark:
        out.append("**No DGX Spark numbers yet.** Every row above is a Mac/Ollama stand-in. The page-10 control "
                   "group (FP8, no prefix cache, no speculative decoding) and the per-optimisation contributions "
                   "come from `bench/spark_ablation.sh`, which only runs on the Spark (vLLM) and has not been run.")
    for d in by("full_event"):
        sm = d.get("state_machine")
        if not (isinstance(sm, dict) and "closed_rate" in sm):
            out.append(f"**Full event never ran through the state machine.** {d['_file']} is the scripted 4-call "
                       "pipeline only (synthetic solve, fixed verify reading). Re-run `uv run python -m "
                       "bench.full_event --runs 3`; its headline is the real Runtime.")
    for d in by("full_event-think"):
        out.append(f"**Reasoning must stay OFF.** With Nemotron reasoning ON the full event takes "
                   f"{_fmt(next((v['measured'] for v in d.get('verdicts', []) if v['name'] == 'full_event_p50'), None))}"
                   f" s p50 and plan_valid_rate is {_fmt(d.get('plan_valid_rate'))} ({d['_file']}), so it fails "
                   "both the ≈22 s and the valid-plan targets. The commander forces it off on every call "
                   "(`warroom/agent/commander.py` `thinking_off`: vLLM `enable_thinking=false`, Ollama "
                   "`reasoning_effort=none`); the demo must not turn it on.")
    for b in ("accuracy-open-book", "accuracy-open-book-cite", "accuracy"):
        for d in by(b):
            if d.get("gap_points") is not None and d["gap_points"] > 3.0:
                out.append(f"**Accuracy gap {b}: {d['gap_points']} points > 3** ({d['_file']}, n={d.get('n_items')}; "
                           f"one item = {d.get('points_per_item')} points).")
    tpw = by("tokens_per_watt")
    if not any((d.get("plug") or {}).get("tokens_per_joule") for d in tpw):
        out.append("**tokens/W not published.** No wall-plug reading has been recorded (tokens/s only). Needs a plug "
                   "meter on the Spark: `uv run python -m bench.tokens_per_watt --duration 60 --watts <avg> "
                   "--idle-watts <idle>` (or `--meter-csv`).")
    unp = by("unplug")
    if not any((d.get("block") or {}).get("applied") for d in unp):
        out.append("**Unplug test without a real block.** No unplug result has the network actually blocked. On a Mac "
                   "without root: `uv run python -m bench.unplug_test --sandbox --spawn-app`; on the Spark: "
                   "`sudo -E uv run python -m bench.unplug_test --block --app-url http://127.0.0.1:8000`.")
    else:
        for d in unp:
            if (d.get("block") or {}).get("method") == "sandbox-exec":
                out.append(f"**Unplug block is process-level (Mac).** {d['_file']} ran under sandbox-exec: the bench "
                           "and the war-room app were offline; the model server (Ollama) was outside the sandbox and "
                           "only socket-watched. The physical cable pull with the nftables block is still a Spark run.")
    return out


def build(results_dir: Path = RESULTS_DIR) -> str:
    lines = ["# Benchmark summary", "", "Generated by `uv run python -m bench.report` from the newest JSON per "
             "benchmark, host and model in bench/results/. Targets come from warroom.factory (pages 9-10).",
             "", "A Mac (Apple Silicon, Ollama/MLX) row is a stand-in for the DGX Spark (GB10, vLLM): it proves the "
             "pipeline and gives real numbers for this box, not the Spark's.", ""]
    best = latest(results_dir)
    items = sorted(best.items(), key=lambda kv: (kv[0][1], kv[0][2],
                   ORDER.index(kv[0][0]) if kv[0][0] in ORDER else 99))
    cur = None
    for (bench, host, model), d in items:
        if (host, model) != cur:
            cur = (host, model)
            hi = d.get("host", {})
            lines += ["", f"## {host} · {model}", "",
                      f"{hi.get('platform', '')} · {hi.get('ram_gb', '?')} GB RAM", "",
                      "| bench | metric | target | measured | verdict | file |", "|---|---|---|---|---|---|"]
        for name, target, measured, verdict in rows(d):
            lines.append(f"| {bench} | {name} | {target} | {measured} | {verdict} | {d['_file']} |")
    todo = open_items(best)
    if todo:
        lines += ["", "## Open against page 10", ""] + [f"- {t}" for t in todo]
    return "\n".join(lines) + "\n"


def main() -> None:
    text = build()
    out = RESULTS_DIR / "SUMMARY.md"
    out.write_text(text, encoding="utf-8")
    print(text)
    print("wrote", out)


if __name__ == "__main__":
    main()
