"""Offline replay regression of commander plans (docs/research/edge-agent-harness-2026-11.md, 建議 3a).

The commander's answers come from tests/fixtures/plans/<scenario>/ (raw model text) through ReplayCommander, wrapped
in orchestrator.QueuedCommander like the runtime does, in --strict mode (no rule fallback). The state machine then
runs the whole event: parse + pydantic validation, simulate, Action Gate, approval, execute, verify. A good fixture
must end CLOSED (or waiting for approval) with no SAFE_STOP, no gate REJECT and every plan from the commander; the
wrong fixture (p.11 replan re-proposing H1, the Spark 10-21 failure) must not pass that check. When the pinned
SYSTEM_PROMPT / PLANT.md hash is not the code's, the replay stops with a message to re-record."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import record_plans as RP
from tests.agent_fakes import T0
from tests.fakes import team_scenarios as TS
from tests.fakes.replay_commander import (
    FIXTURES,
    ReplayCommander,
    StaleFixture,
    check_hashes,
    load_scenario,
    request_key,
    system_prompt_sha,
)
from tests.test_agent import auto_approve, make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import plant
from warroom.agent.commander import MockCommander, parse_plan
from warroom.agent.orchestrator import ModelQueue, QueuedCommander
from warroom.agent.strict import failure_cause
from warroom.contracts import (
    AnomalyKind,
    AuditKind,
    EventState,
    GateVerdict,
    HypothesisStatus,
    LLMCallInfo,
    LLMMode,
    MachineStatus,
)

OK_END = (EventState.CLOSED, EventState.AWAIT_APPROVAL)


@pytest.fixture(autouse=True)
def seed_plant(tmp_path):
    """The seed PLANT.md is active (never data/runtime's), as when the fixtures were pinned."""
    before = plant._current
    plant.configure(tmp_path / "runtime")
    yield
    plant._current = before


def replay_failures(ev, audit, cmd: ReplayCommander) -> list[str]:
    """Why this replay is not a pass ([] = it is)."""
    rows = [e for e in audit.tail(0, 0) if e.event_id == ev.id]
    out = []
    if ev.state not in OK_END:
        out.append(f"ended {ev.state.value} ({ev.outcome}), not closed / awaiting approval")
    if EventState.SAFE_STOP.value in [e.payload["to"] for e in rows if e.kind is AuditKind.STATE_TRANSITION]:
        out.append("went through SAFE_STOP")
    rejects = [e.payload["step_id"] for e in rows if e.kind in (AuditKind.GATE, AuditKind.INTERCEPTED)
               and e.actor == "gate" and e.payload.get("decision", {}).get("verdict") == GateVerdict.REJECT.value]
    if rejects:
        out.append(f"Action Gate rejected {rejects}")
    calls = [e.payload for e in rows if e.kind is AuditKind.LLM_CALL and e.actor == "commander"]
    if not calls:
        out.append("no commander call")
    bad = [(c.get("phase"), c.get("plan_source"), c.get("error")) for c in calls if c.get("plan_source") != "commander"]
    if bad:
        out.append(f"plan not from the commander: {bad}")
    rules = [e.payload["fell_back_to_rules"] for e in rows if e.kind is AuditKind.SYSTEM
             and "fell_back_to_rules" in e.payload]
    if rules:
        out.append(f"fell back to the rule plan: {rules}")
    if cmd.misses:
        out.append(f"fixture has no answer for {cmd.misses}")
    return out


async def _p11(tmp_path, variant=None, strict=True):
    cmd = ReplayCommander(load_scenario("p11", variant), strict=strict)
    _clock, audit, sim, _store, _tools, approvals, sm = make_sm(tmp_path, commander=QueuedCommander(cmd, ModelQueue()))

    async def sup():
        await auto_approve(approvals, 1)
        await auto_approve(approvals, 1, option="A")
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    if ev.state is EventState.CLOSED:
        await t
    else:
        t.cancel()
    return ev, audit, sim, cmd


async def test_p11_replay_passes(tmp_path):
    ev, audit, sim, cmd = await _p11(tmp_path)
    assert replay_failures(ev, audit, cmd) == []
    assert ev.state is EventState.CLOSED
    h1, h2 = ev.hypotheses
    assert (h1.cause_code, h1.status) == ("steam_pressure_low", HypothesisStatus.REJECTED)
    assert h2.cause_code == "steam_leak_or_sensor_drift"
    assert sim.status["CURE-07"] is MachineStatus.OUT_OF_SERVICE
    # each answer came from the fixture, through the queue, keyed (phase, cause_code, round)
    assert [s[:3] for s in cmd.served] == [("plan", "temp_drop", 0), ("replan", "steam_pressure_low", 0)]
    calls = [e.payload for e in audit.tail(0, 0) if e.kind is AuditKind.LLM_CALL]
    assert [(c["phase"], c["plan_source"], c["hypothesis"]) for c in calls] == [
        ("plan", "commander", "H1"), ("replan", "commander", "H2")]
    assert all(c["plant_sha256"] == plant.active_sha() and c["model"] == "replay:p11" for c in calls)
    # the replayed prompts fit the context budget without trimming (a longer prompt would show up here)
    assert cmd.trims == [[], []]


async def test_p11_wrong_fixture_is_red_strict(tmp_path):
    """The Spark 10-21 failure: the replan re-proposes H1. --strict has no rule plan, so it safe-stops."""
    ev, audit, _sim, cmd = await _p11(tmp_path, "repeat_h1")
    fails = replay_failures(ev, audit, cmd)
    assert ev.state is EventState.SAFE_STOP
    assert any("SAFE_STOP" in f for f in fails), fails
    assert [s[:3] for s in cmd.served] == [("plan", "temp_drop", 0), ("replan", "steam_pressure_low", 0),
                                           ("replan", "steam_pressure_low", 1)]


async def test_p11_wrong_fixture_is_red_with_rule_fallback(tmp_path):
    """Without --strict the event still closes, on the rule commander's H2: the replay must not count that as a pass."""
    ev, audit, _sim, cmd = await _p11(tmp_path, "repeat_h1", strict=False)
    assert ev.state is EventState.CLOSED
    fails = replay_failures(ev, audit, cmd)
    assert any("rule plan" in f for f in fails), fails


async def test_p11_invalid_output_safe_stops(tmp_path):
    """The raw text goes through parse_plan + pydantic: a broken answer is not quietly accepted."""
    scn = load_scenario("p11")
    k = ("replan", "steam_pressure_low", 0)
    scn["responses"][k] = {**scn["responses"][k], "output": scn["responses"][k]["output"][:-40]}
    cmd = ReplayCommander(scn)
    _clock, audit, _sim, _store, _tools, approvals, sm = make_sm(tmp_path, commander=QueuedCommander(cmd, ModelQueue()))
    t = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    t.cancel()
    assert ev.state is EventState.SAFE_STOP
    assert cmd.last_cause == "invalid_output"
    assert any("plan not from the commander" in f for f in replay_failures(ev, audit, cmd))


async def test_recorded_spark_command_plan_replays(tmp_path):
    """The one commander answer recorded with its raw output (Spark, Lightning, 2026-10-21): 「重排保交期 #M-201」."""
    scn = load_scenario("cmd_m201")
    cmd = ReplayCommander(scn)
    _clock, audit, _sim, _store, _tools, approvals, sm = make_sm(tmp_path, commander=QueuedCommander(cmd, ModelQueue()))
    ev = sm.open_command("重排保交期 #M-201", "supervisor")
    task = asyncio.create_task(sm.run_command(ev.id))
    async def until_waiting():
        while not approvals.pending() and not task.done():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(until_waiting(), 10)
    assert ev.state in OK_END, ev.outcome
    assert replay_failures(ev, audit, cmd) == []
    if approvals.pending():
        await auto_approve(approvals, 1)
    await asyncio.wait_for(task, 10)
    assert replay_failures(ev, audit, cmd) == []
    assert ev.state is EventState.CLOSED
    assert [s[:3] for s in cmd.served] == [("plan", "supervisor_command", 0)]
    call = next(e.payload for e in audit.tail(0, 0) if e.kind is AuditKind.LLM_CALL)
    assert call["steps"] == ["S1", "S2", "S3"] and call["plan_source"] == "commander"
    # provenance: the recorded file says which run it came from and which prompt it was recorded under
    body = json.loads((scn["dir"] / "plan.json").read_text(encoding="utf-8"))
    assert "llm-trace.jsonl" in body["source"] and len(body["recorded_system_prompt_sha256"]) == 64


def test_stale_prompt_hash_says_rerecord():
    scn = load_scenario("p11")
    check_hashes(scn)                                   # today's prompt: pinned and current agree
    stale = {**scn, "system_prompt_sha256": "0" * 64}
    with pytest.raises(StaleFixture, match=r"SYSTEM_PROMPT changed.*Re-record.*This is not a pass") as e:
        ReplayCommander(stale)
    assert system_prompt_sha() in str(e.value) and "p11" in str(e.value)
    with pytest.raises(StaleFixture, match="PLANT.md changed"):
        check_hashes({**scn, "plant_sha256": "f" * 64})


def test_every_fixture_is_pinned_to_todays_prompt():
    """One clear failure per stale scenario (instead of a confusing state-machine assertion further down)."""
    names = sorted(p.parent.name for p in FIXTURES.glob("*/manifest.json"))
    assert [n for n in names if not n.startswith("team_")] == ["cmd_m201", "p11"]
    assert set(TEAM_FIXTURES) <= set(TS.SCENARIOS), "a team_* fixture with no scenario in tests/fakes/team_scenarios.py"
    for n in names:
        check_hashes(load_scenario(n))


def test_request_key():
    from warroom.contracts import CommanderRequest, Hypothesis
    a = page11_anomaly()
    req = CommanderRequest(phase="plan", event_id="EV-1", anomaly=a, allowed_tools=[])
    assert request_key(req) == ("plan", "temp_drop")
    h = Hypothesis(id="H1", statement="x", cause_code="steam_pressure_low", status=HypothesisStatus.REJECTED)
    assert request_key(req.model_copy(update={"phase": "replan", "rejected": [h]})) == ("replan", "steam_pressure_low")
    assert request_key(req.model_copy(update={"phase": "replan"})) == ("replan", "-")


# ---------------------------------------------------------------------------------------------------------------------
# 建議 3b: team-dataset scenarios (tests/fakes/team_scenarios.py), recorded live by scripts/record_plans.py, and the
# plan-evaluation counts bench/full_event.py --label pools.
TEAM_FIXTURES = sorted(p.parent.name for p in FIXTURES.glob("team_*/manifest.json"))


def test_team_scenarios_come_from_the_dataset_through_team_ids():
    from warroom.data import team_ids
    ev1, ev2 = TS.world("team_ev1"), TS.world("team_ev2")
    assert ev1.machine_id == team_ids.machine_id("A03") == "A-BIGOPEN"
    assert ev2.machine_id == team_ids.machine_id("V07") == "CURE-07"
    a1, a2 = ev1.anomaly(), ev2.anomaly()
    assert (a1.kind, a2.kind) == (AnomalyKind.OVERHEAT, AnomalyKind.TEMP_DROP)
    # events.csv: EV-1 A03 膠溫 112 °C at 14:26; EV-2 V07 128 -> 122 -> 119.5 °C
    assert a1.value == 112.0 and a1.baseline < 100 and a1.prediction.trend_per_min > 0
    assert a2.baseline == 128.0 and a2.value <= 123.0
    # the harness reads the dataset's own curve, time-shifted onto the harness clock
    assert ev2.temp("CURE-07", T0) == a2.value
    assert ev2.temp("CURE-07", T0 + timedelta(minutes=40)) < 121.0          # settles near 119.5
    assert ev2.temp("CURE-06", T0) > 126.0 and ev2.temp("CURE-01", T0) == 128.0
    assert ev1.flow("A-BIGOPEN", T0) < 30.0 and ev1.flow("CURE-07", T0) is None   # cooling water low on EV-1


@pytest.mark.parametrize("name", TEAM_FIXTURES or [pytest.param("-", marks=pytest.mark.skip(
    reason="no team scenario recorded yet: run scripts/record_plans.py --label mac on the Mac"))])
async def test_recorded_team_scenario_replays(tmp_path, name):
    """Every recorded team scenario replays offline: same questions, no SAFE_STOP, no gate REJECT, plans from the
    commander, and the same anomaly the recording started from (a changed dataset or harness is a re-record)."""
    scn = load_scenario(name)
    assert scn["anomaly"] == TS.world(name).anomaly().model_dump(mode="json"), \
        f"{name}: the anomaly rebuilt from data/team_dataset differs from the recorded one; re-record"
    cmd = ReplayCommander(scn)
    ev, audit, _sim, _dec = await TS.run_event(name, QueuedCommander(cmd, ModelQueue()), tmp_path)
    assert replay_failures(ev, audit, cmd) == []
    assert ev.state.value == scn["recorded"]["final_state"]
    assert [list(s[:3]) for s in cmd.served] == [[r["phase"], r["cause_code"], r["round"]] for r in scn["responses"]]


class ScriptedLive:
    """Stands in for OpenAICommander offline: answers with the rule commander's plan as raw JSON text, through the
    same strict parse. Only for testing the record -> replay loop: it is not a model and nothing it records is
    committed."""
    mode = LLMMode.LLM

    def __init__(self, broken: bool = False):
        self.mock, self.broken = MockCommander(), broken
        self.settings = SimpleNamespace(llm_fallback_to_mock=False, base_url="scripted://", model="scripted")
        self.last_raw, self.last_error, self.last_cause, self.last_intercepted = "", None, None, []

    async def plan(self, req, on_alert=None):
        plan, _ = await self.mock.plan(req)
        text = plan.model_dump_json()
        self.last_raw = text[:-40] if self.broken else text
        self.last_error = self.last_cause = None
        try:
            parsed, self.last_intercepted = parse_plan(self.last_raw, [])
        except Exception as e:
            self.last_error, self.last_cause = f"{type(e).__name__}: {e}", failure_cause(e)
            raise
        if on_alert:
            on_alert(parsed.alert_card)
        return parsed, LLMCallInfo(mode=LLMMode.LLM, model="scripted", base_url="scripted://", valid_json=True,
                                   total_ms=0.0)


@pytest.mark.parametrize("name", sorted(TS.SCENARIOS))
async def test_record_then_replay_offline(tmp_path, name):
    """scripts/record_plans.py: a passing live run is written in the 3a fixture format and replays offline."""
    out = tmp_path / "fixtures"
    r = await RP.record(name, ScriptedLive(), out, label="mac", model="scripted", base_url="scripted://",
                        workdir=tmp_path / "work")
    assert r["ok"], r["failures"]
    man = json.loads((out / name / "manifest.json").read_text(encoding="utf-8"))
    assert man["system_prompt_sha256"] == system_prompt_sha() and man["plant_sha256"] == plant.active_sha()
    assert man["team_event"] == TS.SCENARIOS[name].event_id and man["recorded"]["label"] == "mac"
    assert man["anomaly"] == TS.world(name).anomaly().model_dump(mode="json")
    for e in man["responses"]:
        body = json.loads((out / name / e["file"]).read_text(encoding="utf-8"))
        assert body["source"].startswith("recorded: scripts/record_plans.py, mac") and body["output"].startswith("{")
    # the written fixture replays through ReplayCommander like a committed one
    cmd = ReplayCommander(load_scenario(name, root=out))
    ev, audit, _sim, _dec = await TS.run_event(name, QueuedCommander(cmd, ModelQueue()), tmp_path / "again")
    assert replay_failures(ev, audit, cmd) == [] and ev.state.value == man["recorded"]["final_state"]
    assert len(cmd.served) == len(man["responses"]) >= 1


async def test_record_refuses_a_failing_run(tmp_path, monkeypatch):
    """A live answer that does not parse safe-stops the event (--strict): no fixture, the raw answer kept aside."""
    monkeypatch.setattr(RP, "FAILED_DIR", tmp_path / "failed")
    out = tmp_path / "fixtures"
    r = await RP.record("team_ev1", ScriptedLive(broken=True), out, label="mac", model="scripted",
                        base_url="scripted://", workdir=tmp_path / "work")
    assert not r["ok"] and r["final_state"] == "safe_stop"
    assert any("SAFE_STOP" in f for f in r["failures"]), r["failures"]
    assert not (out / "team_ev1").exists()
    kept = json.loads(Path(r["failed_raw"]).read_text(encoding="utf-8"))
    assert kept["raw_calls"][0]["output"] and not kept["raw_calls"][0]["ok"]
    with pytest.raises(ValueError, match="--label"):
        await RP.record("team_ev1", ScriptedLive(), out, label="laptop", model="m", base_url="u")


async def test_plan_counts_from_a_replayed_event(tmp_path):
    """bench/full_event.py --label: the per-run counts come from the event's own audit rows."""
    from bench.full_event import plan_counts, planeval_summary, write_planeval
    ev, audit, _sim, _cmd = await _p11(tmp_path / "good")
    good = plan_counts([e for e in audit.tail(0, 0) if e.event_id == ev.id], ev)
    assert good["schema"] == [2, 2] and good["safe_stop"] == 0 and good["replans"] == 1
    assert good["gate"][1] > 0 and good["gate"][0] == good["gate"][1]          # nothing rejected
    assert good["sim"][1] > 0 and good["final_state"] == "closed"
    ev2, audit2, _s, _c = await _p11(tmp_path / "bad", "repeat_h1")
    bad = plan_counts([e for e in audit2.tail(0, 0) if e.event_id == ev2.id], ev2)
    assert bad["safe_stop"] == 1 and bad["final_state"] == "safe_stop"

    pe = planeval_summary([{"planeval": good}, {"planeval": bad}, {"skipped": "endpoint down"}])
    assert (pe["runs"], pe["runs_measured"], pe["runs_skipped"]) == (3, 2, 1)
    assert pe["safe_stop_runs"] == 1 and pe["closed"] == {"ok": 1, "total": 2, "rate": 0.5}
    assert pe["schema_valid"]["total"] == good["schema"][1] + bad["schema"][1]
    d = write_planeval("mac", {"planeval": pe, "endpoint": {"model": "m", "base_url": "u"}, "saved_to": "x.json"},
                       root=tmp_path / "results")
    assert d.name.endswith("-mac-planeval")
    assert json.loads((d / "planeval.json").read_text(encoding="utf-8"))["machine"] == "Mac"
    assert "Mac" in (d / "README.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError):
        write_planeval("laptop", {"planeval": pe}, root=tmp_path / "results")
