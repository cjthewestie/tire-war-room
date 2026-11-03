"""Offline replay of commander plans (docs/research/edge-agent-harness-2026-11.md, 建議 3a).

ReplayCommander has the OpenAICommander interface (mode=LLM, settings, plan(req, on_alert), last_raw / last_error /
last_cause / last_intercepted / last_plant_sha ...) but, instead of calling a model server, returns the raw model text
stored in a fixture, keyed by (phase, cause_code, round). That text goes through the same strict path as a live
answer: commander.parse_plan (JSON, allow-list split, CommanderPlan pydantic validation), and with
settings.llm_fallback_to_mock=False (--strict) an invalid answer raises, so the event safe-stops instead of quietly
running the rule plan. Wrap it in orchestrator.QueuedCommander to go through the model queue like the runtime does.

Fixture layout: tests/fixtures/plans/<scenario>/manifest.json lists the responses and pins the hashes the outputs
were checked against:
    {"scenario": "p11", "system_prompt_sha256": "...", "plant_sha256": "...",
     "responses": [{"phase": "plan", "cause_code": "temp_drop", "round": 0, "file": "plan.json", "source": "..."}],
     "variants": {"<name>": [<response entries replacing / adding keys>]}}
A response file holds {"output": "<raw model text>", ...metadata}. The key's cause_code is the anomaly kind for a
plan and the cause_code of the latest rejected hypothesis for a replan ("-" when none was rejected); round counts
calls with the same (phase, cause_code) in one event (the repeat-retry of a replan is round 1).

No network, no model client: nothing here can reach a model server, and nothing here can move a machine (the plan
it returns still meets the Action Gate and the supervisor's approval in the state machine)."""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from warroom.agent import plant
from warroom.agent.commander import SYSTEM_PROMPT, build_messages, fit_context, parse_plan, scan_intercepted
from warroom.agent.strict import failure_cause
from warroom.config import Settings
from warroom.contracts import CommanderPlan, CommanderRequest, LLMCallInfo, LLMMode

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "plans"
REPLAY_URL = "replay://tests/fixtures/plans"


def system_prompt_sha() -> str:
    return hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()


class StaleFixture(AssertionError):
    """The prompt the fixture was checked against is not the prompt in the code: re-record, do not pass."""


class ReplayMiss(LookupError):
    """The state machine asked for a (phase, cause_code, round) the fixture has no answer for."""


def request_key(req: CommanderRequest) -> tuple[str, str]:
    if req.phase == "plan":
        return "plan", req.anomaly.kind.value
    return "replan", (req.rejected[-1].cause_code if req.rejected else "-")


def load_scenario(name: str, variant: str | None = None, root: Path = FIXTURES) -> dict[str, Any]:
    """The manifest with each response's raw output read in, `variant` applied over the base responses."""
    d = root / name
    man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    entries = {(r["phase"], r["cause_code"], int(r["round"])): r for r in man["responses"]}
    if variant is not None:
        if variant not in man.get("variants", {}):
            raise KeyError(f"{d / 'manifest.json'} has no variant {variant!r}")
        entries.update({(r["phase"], r["cause_code"], int(r["round"])): r for r in man["variants"][variant]})
    responses = {}
    for k, r in entries.items():
        body = json.loads((d / r["file"]).read_text(encoding="utf-8"))
        responses[k] = {**r, "output": body["output"]}
    return {**man, "dir": d, "variant": variant, "responses": responses}


def check_hashes(scn: dict[str, Any]) -> None:
    """Raise StaleFixture with what changed when the pinned prompt hashes are not today's."""
    problems = []
    now_sys = system_prompt_sha()
    if scn.get("system_prompt_sha256") != now_sys:
        problems.append(f"commander.SYSTEM_PROMPT changed (fixture pinned {scn.get('system_prompt_sha256')}, "
                        f"code is now {now_sys})")
    now_plant = plant.active_sha()
    if scn.get("plant_sha256") != now_plant:
        problems.append(f"the active PLANT.md changed (fixture pinned {scn.get('plant_sha256')}, now {now_plant})")
    if problems:
        raise StaleFixture(
            f"plan replay fixture {scn['dir']} is stale: " + "; ".join(problems) + ". The prompt the model sees is "
            "different, so the recorded answers no longer show what the model would do. Re-record the outputs "
            "against the new prompt (or re-check the hand-written ones), then update system_prompt_sha256 / "
            "plant_sha256 in manifest.json. This is not a pass.")


class ReplayCommander:
    """OpenAICommander's interface, answers from a fixture. strict=True (default) is --strict: no rule fallback."""
    mode = LLMMode.LLM

    def __init__(self, scenario: dict[str, Any], strict: bool = True, verify_hashes: bool = True):
        if verify_hashes:
            check_hashes(scenario)
        self.scenario = scenario
        # the context-budget fields are the code defaults, never .env, so the replay is the same on every machine
        d = Settings.model_fields
        self.max_tokens = d["commander_max_tokens"].default
        self.settings = SimpleNamespace(llm_fallback_to_mock=not strict, base_url=REPLAY_URL, model="replay",
                                        llm_context_tokens=d["llm_context_tokens"].default,
                                        prompt_token_factor=d["prompt_token_factor"].default)
        self.model = f"replay:{scenario['scenario']}" + (f"/{scenario['variant']}" if scenario.get("variant") else "")
        self._rounds: dict[str, Counter] = {}
        self.served: list[tuple[str, str, int, str]] = []    # (phase, cause_code, round, file) per call
        self.misses: list[tuple[str, str, int]] = []
        self.trims: list[list[str]] = []                     # TRIM_STEPS fit_context applied, per call that fit
        self.last_intercepted: list[dict[str, Any]] = []
        self.last_raw = ""
        self.last_error: str | None = None
        self.last_cause: str | None = None
        self.plant_override: str | None = None
        self.last_plant_sha: str | None = None
        self.last_prompt_est: int | None = None
        self.last_trimmed: list[str] = []

    async def plan(self, req: CommanderRequest, on_alert=None) -> tuple[CommanderPlan, LLMCallInfo]:
        t0 = time.perf_counter()
        self.last_intercepted, self.last_raw, self.last_error, self.last_cause = [], "", None, None
        self.last_prompt_est, self.last_trimmed = None, []
        caught: list[dict[str, Any]] = []
        # build the request as the live client does: the prompt it would send must still be buildable
        messages = build_messages(req, self.plant_override)
        assert messages[0]["content"].startswith(SYSTEM_PROMPT)
        self.last_plant_sha = (plant.sha256(self.plant_override) if self.plant_override is not None
                               else plant.active_sha())
        phase, cause = request_key(req)
        rounds = self._rounds.setdefault(req.event_id, Counter())
        n = rounds[(phase, cause)]
        rounds[(phase, cause)] += 1
        try:
            # same context budget as the live client: a prompt that no longer fits raises PromptTooLong (too_long),
            # and TRIM_STEPS applied show up in last_trimmed / LLMCallInfo.trimmed
            _sent, self.last_prompt_est, self.last_trimmed = fit_context(req, messages[0]["content"], self.settings,
                                                                         self.max_tokens)
            self.trims.append(list(self.last_trimmed))
            hit = self.scenario["responses"].get((phase, cause, n))
            if hit is None:
                self.misses.append((phase, cause, n))
                raise ReplayMiss(f"no replay answer for (phase={phase}, cause_code={cause}, round={n}) in "
                                 f"{self.scenario['dir']}")
            self.served.append((phase, cause, n, hit["file"]))
            text = hit["output"]
            self.last_raw = text
            plan, self.last_intercepted = parse_plan(text, caught)
            if on_alert:
                on_alert(plan.alert_card)
            return plan, LLMCallInfo(mode=LLMMode.LLM, model=self.model, base_url=REPLAY_URL, ttft_ms=0.0,
                                     total_ms=(time.perf_counter() - t0) * 1000, valid_json=True,
                                     prompt_tokens_est=self.last_prompt_est, trimmed=list(self.last_trimmed))
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"[:500]
            self.last_cause = failure_cause(e)
            self.last_intercepted = caught or scan_intercepted(self.last_raw)
            if not self.settings.llm_fallback_to_mock:
                raise
            from warroom.agent.commander import MockCommander
            plan, _ = await MockCommander().plan(req, on_alert=on_alert)
            return plan, LLMCallInfo(mode=LLMMode.LLM, model=self.model, base_url=REPLAY_URL,
                                     total_ms=(time.perf_counter() - t0) * 1000, valid_json=False,
                                     fell_back_to_mock=True)

    async def ping(self) -> bool:
        return True
