"""Record the commander's plans for the team-dataset scenarios into replay fixtures (edge-agent-harness 建議 3b).

Each scenario in tests/fakes/team_scenarios.py (team_ev1 = dataset EV-1 A03 大開煉機, team_ev2 = EV-2 V07 硫化機;
machine codes mapped only through warroom/data/team_ids.py) runs once through the offline agent harness with the LIVE
commander (OpenAICommander, --strict: no rule fallback). Every plan / replan answer is kept as the raw model text,
keyed (phase, cause_code, round) the way tests/fakes/replay_commander.py looks it up. Before anything is written to
tests/fixtures/plans/<scenario>/, the recording is replayed offline through ReplayCommander and must pass the same
check as tests/test_plan_replay.py (no SAFE_STOP, no gate REJECT, every plan from the commander, the same questions
asked). A recording that fails either run is NOT written as a fixture; its raw answers go to
data/runtime/record_plans/ (never committed) so you can read what the model did.

On the Mac (Ollama):

  uv run python scripts/record_plans.py --label mac --model <ollama model> \\
      --base-url http://127.0.0.1:11434/v1                     # all scenarios
  uv run python scripts/record_plans.py --label mac --scenario team_ev2 ...

To keep a full trace of every request as well, put bench/results/spark-lightning-20261021/llm_trace_proxy.py in
between: set its UP to the model server (Ollama: http://127.0.0.1:11434), run it, and pass
--base-url http://127.0.0.1:8198/v1. The proxy never records request headers (API keys).

Then: uv run pytest -q tests/test_plan_replay.py. The fixture pins the SYSTEM_PROMPT and PLANT.md hashes it was recorded
under; when either changes the replay stops and asks for a re-record. This script writes fixtures only; it records no
performance numbers (those go through bench/full_event.py --label into bench/results/).

Nothing here can move a machine: the harness tools are fakes (tests/agent_fakes.py), every write still goes through
the Action Gate, and the model only answers plan requests."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fakes.replay_commander import (
    FIXTURES,
    ReplayCommander,
    load_scenario,
    request_key,
    system_prompt_sha,
)
from tests.fakes.team_scenarios import SCENARIOS, run_event, world
from warroom.agent import plant
from warroom.agent.orchestrator import ModelQueue, QueuedCommander

FAILED_DIR = ROOT / "data" / "runtime" / "record_plans"
LABELS = ("mac", "spark")


class RecordingCommander:
    """Wraps the live commander; keeps each call's raw text under the replay key. Everything else is forwarded."""

    def __init__(self, inner: Any):
        self._inner = inner
        self._rounds: dict[str, Counter] = {}
        self.calls: list[dict[str, Any]] = []
        self.misses: list[tuple[str, str, int]] = []         # replay_failures() reads it; a live call never misses

    async def plan(self, req, on_alert=None):
        phase, cause = request_key(req)
        rounds = self._rounds.setdefault(req.event_id, Counter())
        n = rounds[(phase, cause)]
        rounds[(phase, cause)] += 1
        rec: dict[str, Any] = {"phase": phase, "cause_code": cause, "round": n}
        try:
            plan, info = await (self._inner.plan(req) if on_alert is None else self._inner.plan(req, on_alert=on_alert))
            rec.update(ok=not info.fell_back_to_mock, plan_hypothesis=plan.hypothesis.cause_code,
                       model=info.model, total_ms=info.total_ms)
            return plan, info
        except Exception as e:
            rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:500])
            raise
        finally:
            rec["output"] = str(getattr(self._inner, "last_raw", "") or "")
            rec.setdefault("error", getattr(self._inner, "last_error", None))
            self.calls.append(rec)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _file(c: dict[str, Any]) -> str:
    return f"{c['phase']}_{c['cause_code']}_{c['round']}.json"


def write_fixture(name: str, rec: RecordingCommander, out: Path, *, label: str, model: str, base_url: str,
                  ev: Any, decisions: list[dict[str, Any]]) -> Path:
    """tests/fixtures/plans/<name>/ in the 3a format (manifest.json + one file per answer)."""
    w = world(name)
    d = out / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    now = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    sys_sha, plant_sha = system_prompt_sha(), plant.active_sha()
    responses = []
    for c in rec.calls:
        body = {"source": f"recorded: scripts/record_plans.py, {label}, model {model} at {base_url}, {now}",
                "phase": c["phase"], "label": label, "model": model, "recorded_at": now,
                "recorded_system_prompt_sha256": sys_sha, "recorded_plant_sha256": plant_sha,
                "output": c["output"]}
        (d / _file(c)).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        responses.append({"phase": c["phase"], "cause_code": c["cause_code"], "round": c["round"], "file": _file(c)})
    manifest = {
        "scenario": name,
        "about": w.scn.about,
        "team_event": w.scn.event_id,
        "harness": "tests/fakes/team_scenarios.py (tests/test_agent.make_sm, scripted supervisor: recommended option)",
        "anomaly": w.anomaly().model_dump(mode="json"),
        "system_prompt_sha256": sys_sha,
        "plant_sha256": plant_sha,
        "recorded": {"label": label, "model": model, "base_url": base_url, "recorded_at": now,
                     "final_state": ev.state.value, "replans": ev.replans,
                     "hypotheses": [h.cause_code for h in ev.hypotheses], "decisions": decisions},
        "responses": responses,
    }
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return d


async def record(name: str, live: Any, out: Path = FIXTURES, *, label: str, model: str, base_url: str,
                 workdir: Path | None = None, timeout_s: float = 900.0) -> dict[str, Any]:
    """Record one scenario with `live`, replay it offline, and write the fixture only when both runs pass.
    Returns {"scenario", "ok", "failures", "fixture" | "failed_raw"}."""
    from tests.test_plan_replay import replay_failures  # the exact pass/fail rule of the replay test

    if label not in LABELS:
        raise ValueError(f"--label must be one of {LABELS}, not {label!r}")
    work = workdir or Path(tempfile.mkdtemp(prefix=f"record-{name}-"))
    rec = RecordingCommander(live)
    ev, audit, _sim, decisions = await run_event(name, QueuedCommander(rec, ModelQueue()), _mk(work / "live"),
                                                 timeout_s=timeout_s)
    failures = [f"live run: {f}" for f in replay_failures(ev, audit, rec)]
    result: dict[str, Any] = {"scenario": name, "final_state": ev.state.value, "replans": ev.replans,
                              "calls": [(c["phase"], c["cause_code"], c["round"], c["ok"]) for c in rec.calls]}
    if not failures:
        staged = write_fixture(name, rec, work / "staged", label=label, model=model, base_url=base_url, ev=ev,
                               decisions=decisions)
        cmd = ReplayCommander(load_scenario(name, root=staged.parent))
        ev2, audit2, _sim2, _d2 = await run_event(name, QueuedCommander(cmd, ModelQueue()), _mk(work / "replay"))
        failures += [f"offline replay: {f}" for f in replay_failures(ev2, audit2, cmd)]
        want = [(c["phase"], c["cause_code"], c["round"]) for c in rec.calls]
        if [s[:3] for s in cmd.served] != want:
            failures.append(f"offline replay asked {[s[:3] for s in cmd.served]}, the live run asked {want}")
        if ev2.state is not ev.state:
            failures.append(f"offline replay ended {ev2.state.value}, the live run ended {ev.state.value}")
        if not failures:
            dest = out / name
            if dest.exists():
                shutil.rmtree(dest)
            out.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged, dest)
            return {**result, "ok": True, "failures": [], "fixture": str(dest)}
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    raw = FAILED_DIR / f"{name}-{label}-{datetime.now(UTC).astimezone():%Y%m%d-%H%M%S}.json"
    raw.write_text(json.dumps({**result, "failures": failures, "model": model, "base_url": base_url,
                               "raw_calls": rec.calls}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**result, "ok": False, "failures": failures, "failed_raw": str(raw)}


def _mk(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


async def main_async(args) -> int:
    from warroom.agent.commander import OpenAICommander
    from warroom.config import Settings
    from warroom.contracts import LLMMode

    work = Path(tempfile.mkdtemp(prefix="record-plans-"))
    before = plant._current
    plant.configure(work / "runtime")                     # the seed PLANT.md, as the replay test pins it
    try:
        kw = {k: v for k, v in (("base_url", args.base_url), ("model", args.model), ("api_key", args.api_key)) if v}
        settings = Settings(llm_mode=LLMMode.LLM, llm_fallback_to_mock=False, **kw)
        names = args.scenario or list(SCENARIOS)
        bad = 0
        for n in names:
            live = OpenAICommander(settings)
            r = await record(n, live, args.out, label=args.label, model=settings.model, base_url=settings.base_url,
                             workdir=_mk(work / n), timeout_s=args.timeout)
            bad += not r["ok"]
            print(f"{n}: {'recorded' if r['ok'] else 'NOT recorded'}  ended {r['final_state']}, replans {r['replans']}, "
                  f"calls {r['calls']}")
            for f in r["failures"]:
                print("   ", f)
            print("   ", r.get("fixture") or f"raw answers kept in {r['failed_raw']}")
        return 1 if bad else 0
    finally:
        plant._current = before


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", required=True, choices=LABELS, help="which machine served the model")
    p.add_argument("--scenario", action="append", choices=sorted(SCENARIOS), help="repeatable; default: all")
    p.add_argument("--base-url", help="OpenAI-compatible endpoint (default: BASE_URL from .env)")
    p.add_argument("--model", help="default: MODEL from .env")
    p.add_argument("--api-key")
    p.add_argument("--out", type=Path, default=FIXTURES, help="fixture root (default tests/fixtures/plans)")
    p.add_argument("--timeout", type=float, default=900.0, help="wall seconds per scenario")
    return asyncio.run(main_async(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
