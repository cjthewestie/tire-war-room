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

import pytest

from tests.fakes.replay_commander import (
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
from warroom.agent.orchestrator import ModelQueue, QueuedCommander
from warroom.contracts import AuditKind, EventState, GateVerdict, HypothesisStatus, MachineStatus

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
    while not approvals.pending() and not task.done():
        await asyncio.sleep(0.001)
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
    from tests.fakes.replay_commander import FIXTURES
    names = sorted(p.parent.name for p in FIXTURES.glob("*/manifest.json"))
    assert names == ["cmd_m201", "p11"]
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
