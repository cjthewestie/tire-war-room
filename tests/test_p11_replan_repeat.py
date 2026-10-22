"""p.11 on the real model (Spark 10-21, 4/4 SAFE_STOP): after H1 「蒸汽壓不足」 was rejected at 119.5 °C, Lightning
proposed 「steam_pressure_low(_or_sensor_drift)」 again with the same 「CURE-07 ≥121°C」 test, and _repeats_rejected
safe-stopped the event. The replan now carries structured feedback in the user message, asks once more when the
answer repeats, and takes the rule commander's H2 when it repeats again (CJ, 2026-10-22: 「修 p.11」)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from tests.test_agent import auto_approve, make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import MockCommander
from warroom.agent.commander import SYSTEM_PROMPT, build_messages, build_user_message
from warroom.contracts import AuditKind, Channel, EventState, Expectation, HypothesisStatus, MachineStatus


class RepeatingCommander(MockCommander):
    """The Spark run's replan: the rejected test again under a new name, `repeats` times, then the rule plan."""

    def __init__(self, repeats: int, strict: bool = False):
        self.repeats = repeats
        self.reqs = []
        if strict:
            self.settings = SimpleNamespace(llm_fallback_to_mock=False)

    def build(self, req):
        self.reqs.append(req)
        p = super().build(req)
        if req.phase != "replan" or not req.rejected or self.repeats <= 0:
            return p
        self.repeats -= 1
        exp = Expectation(machine_id="CURE-07", channel=Channel.TEMP, op=">=", value=121, within_min=30)
        h = p.hypothesis.model_copy(update={"cause_code": "steam_pressure_low_or_sensor_drift",
                                            "statement": "蒸汽壓不足或感測器漂移，溫度會回升", "expectation": exp})
        return p.model_copy(update={"hypothesis": h})


async def _run(tmp_path, commander):
    _clock, audit, sim, _store, _tools, approvals, sm = make_sm(tmp_path, commander=commander)

    async def sup():
        await auto_approve(approvals, 1)
        await auto_approve(approvals, 1, option="A")
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    if ev.state is EventState.CLOSED:
        await t
    else:
        t.cancel()
    return ev, audit, sim


def _sys(audit, key):
    return [e.payload for e in audit.tail(0, 0) if e.kind is AuditKind.SYSTEM and key in e.payload]


async def test_repeat_twice_falls_back_to_rule_h2(tmp_path):
    cmd = RepeatingCommander(repeats=2)
    ev, audit, sim = await _run(tmp_path, cmd)
    assert ev.state is EventState.CLOSED, ev.outcome
    h1, h2 = ev.hypotheses
    assert h1.status is HypothesisStatus.REJECTED
    assert (h2.id, h2.cause_code) == ("H2", "steam_leak_or_sensor_drift")
    assert sim.status["CURE-07"] is MachineStatus.OUT_OF_SERVICE
    fb = _sys(audit, "fell_back_to_rules")
    assert len(fb) == 1 and fb[0]["fell_back_to_rules"] == "repeated_rejected_hypothesis"
    assert fb[0]["model_cause_code"] == "steam_pressure_low_or_sensor_drift" and fb[0]["rules_repeat"] is None
    assert len([p for p in _sys(audit, "action") if p["action"] == "retry_with_feedback"]) == 1
    assert any(t.text == "AI 兩次重提已否決的假設，改用規則假設 H2" for t in ev.timeline)
    # the retry is not a replan: one replan (H1 -> H2); 3 model calls (plan, replan, its retry), then the rules
    assert ev.replans == 1
    assert [r.phase for r in cmd.reqs] == ["plan", "replan", "replan"]
    calls = [e for e in audit.tail(0, 0) if e.kind is AuditKind.LLM_CALL]
    assert len(calls) == 3
    assert EventState.SAFE_STOP.value not in [e.payload["to"] for e in audit.tail(0, 0)
                                              if e.kind is AuditKind.STATE_TRANSITION]


async def test_repeat_once_retry_answers(tmp_path):
    cmd = RepeatingCommander(repeats=1)
    ev, audit, _sim = await _run(tmp_path, cmd)
    assert ev.state is EventState.CLOSED, ev.outcome
    assert ev.hypotheses[1].cause_code == "steam_leak_or_sensor_drift"
    assert not _sys(audit, "fell_back_to_rules")
    assert not any("改用規則假設" in t.text for t in ev.timeline)
    # the retry's user message says what the last answer repeated
    retry = json.loads(build_user_message(cmd.reqs[2]))
    rep = retry["replan_feedback"]["your_last_answer_repeated"]
    assert rep["cause_code"] == "steam_pressure_low_or_sensor_drift" and rep["match"] == "expectation of H1"
    assert "your_last_answer_repeated" not in json.loads(build_user_message(cmd.reqs[1]))["replan_feedback"]


async def test_replan_user_message_carries_feedback(tmp_path):
    cmd = RepeatingCommander(repeats=0)
    ev, _audit, _sim = await _run(tmp_path, cmd)
    assert ev.state is EventState.CLOSED
    plan_msg, replan_msg = (json.loads(build_user_message(r)) for r in cmd.reqs[:2])
    assert "replan_feedback" not in plan_msg            # the plan message is unchanged
    fb = replan_msg["replan_feedback"]
    assert fb["rejected"][0]["id"] == "H1" and fb["rejected"][0]["observed"] == 119.5
    assert fb["rejected"][0]["test"] == "CURE-07 temp_c ≥121°C"
    assert "119.5" in fb["rejected"][0]["evidence"]
    assert "新假設的預期不可再用 CURE-07 temp_c ≥121°C 的回溫；請改用其他可驗證的預期（例如停用機台狀態、蒸汽／壓力通道、或 SOP 交接）" \
        in fb["instruction"]
    assert fb["untried_cause_codes"][:2] == ["steam_leak", "sensor_drift"]
    assert "steam_pressure_low" not in fb["untried_cause_codes"]
    # the guidance is in the user message only: the system prompt is the same on every call
    for r in cmd.reqs[:2]:
        assert build_messages(r)[0]["content"].startswith(SYSTEM_PROMPT)
    assert "replan_feedback" not in SYSTEM_PROMPT and "不可再用" not in SYSTEM_PROMPT


async def test_strict_repeat_twice_still_safe_stops(tmp_path):
    """--strict has no rule plan: two repeats end in SAFE_STOP, as before."""
    ev, audit, _sim = await _run(tmp_path, RepeatingCommander(repeats=2, strict=True))
    assert ev.state is EventState.SAFE_STOP
    assert not _sys(audit, "fell_back_to_rules")
    assert any(p.get("error") == "commander re-proposed a rejected hypothesis" and "action" not in p
               for p in _sys(audit, "error"))


class MacLightning(MockCommander):
    """Mac 10-22 real runs: H1's test asked ≥123 °C (prediction 122±1), and H2 re-solved without CURE-09 (the system
    prompt's example press) next to 「7 號機停用」."""

    def build(self, req):
        p = super().build(req)
        if req.phase == "plan":
            exp = p.hypothesis.expectation.model_copy(update={"value": 123.0})
            return p.model_copy(update={"hypothesis": p.hypothesis.model_copy(update={"expectation": exp})})
        steps = [s.model_copy(update={"args": {**s.args, "unavailable_presses": ["CURE-09"]}})
                 if s.tool.value == "solve_schedule" else s for s in p.steps]
        return p.model_copy(update={"steps": steps})


async def test_press_test_is_the_cure_band_and_takeout_binds_the_event_press(tmp_path):
    ev, audit, sim = await _run(tmp_path, MacLightning())
    assert ev.state is EventState.CLOSED, ev.outcome
    h1 = ev.hypotheses[0]
    assert (h1.expectation.op, h1.expectation.value) == (">=", 121.0)
    assert any("119.5" in e for e in h1.evidence)            # rejected by the leak, not at 122 °C right away
    bound = _sys(audit, "unavailable_presses_bound")
    assert bound and bound[0]["unavailable_presses_bound"][0]["to"] == ["CURE-07"]
    assert sim.status["CURE-07"] is MachineStatus.OUT_OF_SERVICE
