"""Collect the newest result of every benchmark per host+model into bench/results/SUMMARY.md.

  bench/py.sh -m bench.report
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from bench.llm import RESULTS_DIR
from bench.provenance import BENCH_DEPS, BENCH_DIR, code_fingerprint

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
            best[key] = d | {"_file": p.name, "_stale": freshness(d, p)}
    return best


def _git(*a: str) -> str | None:
    try:
        r = subprocess.run(["git", *a], cwd=BENCH_DIR.parent, capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


# A commit that touches bench code without changing what any bench measures (e.g. adding a field to the
# saved JSON) carries this trailer, so older unstamped results are not marked stale by it.
NO_NUMBERS_TRAILER = "Bench-Numbers: unchanged"


def schema_problem(d: dict[str, Any]) -> str | None:
    """A result written by an older version of its bench, in a shape the current bench no longer judges."""
    b = d.get("bench")
    if b == "ttft" and not isinstance((d.get("alert_card") or {}).get("cold"), dict):
        return "old ttft schema: no cold/warm split (alert card judged on repeated, cache-hit prompts)"
    if b == "concurrent" and "batched" not in d:
        return "old concurrent schema: no batching check (queued calls could pass)"
    return None


def freshness(d: dict[str, Any], path: Path | None = None) -> str | None:
    """None when the result was produced by the bench code that is in the tree now, else why it is stale.

    Stamped results (bench.llm.code_stamp) compare the fingerprint of their bench source files with the
    files on disk. Unstamped (older) results fall back to git: the run must be recorded after the last
    commit that touched those files, or be committed in that same commit."""
    prob = schema_problem(d)
    if prob:
        return prob
    b = d.get("bench", "")
    deps = BENCH_DEPS.get(b)
    if not deps:
        return None
    stamp = d.get("code") or {}
    if stamp.get("fingerprint"):
        now = code_fingerprint(b)
        if stamp["fingerprint"] != now:
            return (f"bench code changed since this run (ran at {stamp.get('git_sha')}"
                    f"{' + local edits' if stamp.get('deps_dirty') else ''}; {', '.join(deps)} differ now)")
        return None
    files = [f"bench/{f}" for f in deps]
    log = _git("log", "--format=%H %cI%n%B%x00", "--", *files)
    last = next((c.strip().split("\n", 1)[0] for c in (log or "").split("\x00")
                 if c.strip() and NO_NUMBERS_TRAILER not in c), None)
    if not last:
        return None                      # no git history here: cannot tell
    last_sha, last_t = last.split(" ", 1)
    try:
        if datetime.fromisoformat(d.get("recorded_at", "")) >= datetime.fromisoformat(last_t):
            return None
    except ValueError:
        pass
    if path is not None:
        try:
            rel = str(path.resolve().relative_to(BENCH_DIR.parent))
        except ValueError:
            rel = str(path)
        added = _git("log", "--diff-filter=A", "--format=%H", "--", rel)
        if added and added.splitlines()[-1] == last_sha:
            return None                  # committed together with the bench code that produced it
    return (f"unstamped run recorded {d.get('recorded_at', '?')[:19]}, before {last_sha[:7]} "
            f"({last_t[:19]}) changed {', '.join(files)}")


def rows(d: dict[str, Any]) -> list[tuple[str, str, str, str]]:
    out = _rows(d)
    stale = d.get("_stale")
    if stale:
        out = [(n, t, m, f"STALE, not counted (was {v.split(' (')[0]}): {stale}; re-run") for n, t, m, v in out]
    return out


def _rows(d: dict[str, Any]) -> list[tuple[str, str, str, str]]:
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
    for (b, _, _), d in sorted(best.items()):
        if d.get("_stale"):
            out.append(f"**Stale result, not counted: {b}** ({d['_file']}): {d['_stale']}. Re-run "
                       f"`bench/py.sh -m bench.{_module(b)}` and regenerate this summary.")
    if not spark:
        out.append("**No DGX Spark numbers yet.** Every row above is a Mac/Ollama stand-in. The page-10 control "
                   "group (FP8, no prefix cache, no speculative decoding) and the per-optimisation contributions "
                   "come from `bench/spark_ablation.sh`, which only runs on the Spark (vLLM) and has not been run.")
    for d in by("full_event"):
        sm = d.get("state_machine")
        if not (isinstance(sm, dict) and "closed_rate" in sm):
            out.append(f"**Full event never ran through the state machine.** {d['_file']} is the scripted 4-call "
                       "pipeline only (synthetic solve, fixed verify reading). Re-run `bench/py.sh -m "
                       "bench.full_event --runs 3`; its headline is the real Runtime.")
    for (b, _, _), d in sorted(best.items()):
        la = d.get("host_load_avg_1_5_15")
        cpus = (d.get("host") or {}).get("cpu_count") or 0
        if la and cpus and not d.get("_stale") and b.split("-")[0] in ("ttft", "concurrent", "full_event", "unplug"):
            if max(la[0], la[1]) > cpus / 2:
                out.append(f"**{b} measured on a busy host** ({d['_file']}: load average {la[0]}/{la[1]}/{la[2]} "
                           f"on {cpus} cores). Latency is inflated by other work sharing the box and the model "
                           "server; re-run on an idle machine before quoting it.")
    for d in by("full_event-think"):
        out.append(("(From a stale run.) " if d.get("_stale") else "") + f"**Reasoning must stay OFF.** With Nemotron reasoning ON the full event takes "
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
                   "meter on the Spark: `bench/py.sh -m bench.tokens_per_watt --duration 60 --watts <avg> "
                   "--idle-watts <idle>` (or `--meter-csv`).")
    unp = by("unplug")
    if not any((d.get("block") or {}).get("applied") for d in unp):
        out.append("**Unplug test without a real block.** No unplug result has the network actually blocked. On a Mac "
                   "without root: `bench/py.sh -m bench.unplug_test --sandbox --spawn-app`; on the Spark: "
                   "`sudo -E bench/py.sh -m bench.unplug_test --block --app-url http://127.0.0.1:8000`.")
    else:
        for d in unp:
            if (d.get("block") or {}).get("method") == "sandbox-exec":
                out.append(f"**Unplug block is process-level (Mac).** {d['_file']} ran under sandbox-exec: the bench "
                           "and the war-room app were offline; the model server (Ollama) was outside the sandbox and "
                           "only socket-watched. The physical cable pull with the nftables block is still a Spark run.")
                eg = d.get("egress") or {}
                n = eg.get("firewall_dropped_packets")
                if n:
                    by = eg.get("denies_by_pid")
                    roles = eg.get("pid_roles") or {}
                    who = (", ".join(f"pid {p} ({roles.get(str(p), roles.get(p, '?'))}) x{c}" for p, c in by.items())
                           if by else "pids not broken down in this result (pre-fix run)")
                    out.append(f"**Something tried to leave the factory {n} time(s)** in {d['_file']} (kernel "
                               f"Sandbox log, `deny(1) network-outbound`: {who}). Every attempt was blocked, so "
                               "nothing left, but '0 outbound attempts' is not met."
                               + (f" Remote(s): {', '.join(eg['deny_remotes'])}." if eg.get("deny_remotes") else ""))
                nr = sum((eg.get("no_remote_denies_by_pid") or {}).values())
                if nr and not n:
                    out.append(f"**No attempt to leave the factory** in {d['_file']}: the kernel logged {nr} "
                               "remote-less network-outbound deny(s), which accompany ordinary localhost sockets "
                               "and name no destination; they are not counted as egress.")
    return out


def _module(bench: str) -> str:
    base = bench.split("-")[0]
    return {"memory": "memory_check", "unplug": "unplug_test"}.get(base, base)


def build(results_dir: Path = RESULTS_DIR) -> str:
    lines = ["# Benchmark summary", "", "Generated by `bench/py.sh -m bench.report` from the newest JSON per "
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
