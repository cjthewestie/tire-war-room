"""warroom/safety: deterministic red-path rules, once per episode, < 0.2 s, no LLM imports."""
import subprocess
import sys
import time
from datetime import datetime, timedelta

from warroom.contracts import (
    CompoundStage,
    MachineStatus,
    SafetyActionKind,
    SafetyCore,
    ScenarioName,
    SensorReading,
)
from warroom.factory import (
    COMPOUND_STOP_C,
    CURATIVE_STOP_C,
    CURE_LOW_STOP_C,
    LATENCY_SAFETY_S,
    MACHINE_IDS,
    TZ,
)
from warroom.safety import RuleSafetyCore, default_rules
from warroom.sim import Factory

T0 = datetime(2026, 10, 3, 2, 14, tzinfo=TZ)


def rd(mid: str, temp: float | None, k: int = 0) -> SensorReading:
    return SensorReading(ts=T0 + timedelta(seconds=k), machine_id=mid, temp_c=temp)


def states():
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP, backfill_min=0)
    f.step(1)
    return f.states()


def feed(core, mid, temps, st):
    acts = []
    for k, t in enumerate(temps):
        acts += core.check([rd(mid, t, k)], st)
    return acts


def test_default_rules_come_from_factory_constants():
    rules = {r.id: r for r in default_rules()}
    assert rules["SR-COMPOUND-120"].threshold == COMPOUND_STOP_C == 120
    assert rules["SR-COMPOUND-120"].compound_stage is CompoundStage.UNVULCANIZED
    assert rules["SR-CURATIVE-110"].threshold == CURATIVE_STOP_C == 110
    assert rules["SR-CURATIVE-110"].compound_stage is CompoundStage.CURATIVE_ADDED
    assert rules["SR-CURE-LOW-120"].threshold == CURE_LOW_STOP_C == 120
    assert rules["SR-CURE-LOW-120"].only_while_curing and rules["SR-CURE-LOW-120"].op == "<"
    assert rules["SR-CURE-HIGH-130"].action is SafetyActionKind.ALARM_ONLY
    assert isinstance(RuleSafetyCore(), SafetyCore)


def test_unvulcanized_112_does_not_stop_but_120_does():
    st = states()
    assert st["A-BIGOPEN"].compound_stage is CompoundStage.UNVULCANIZED
    core = RuleSafetyCore()
    assert feed(core, "A-BIGOPEN", [112.0, 115.0, 119.9, 119.9], st) == []       # page 2: 112 °C, no stop
    acts = feed(core, "A-BIGOPEN", [120.0, 120.4], st)
    assert len(acts) == 1 and acts[0].action is SafetyActionKind.STOP_MACHINE
    assert acts[0].rule_id == "SR-COMPOUND-120" and "未加硫" in acts[0].message


def test_curative_added_stops_at_110():
    st = states()
    assert st["A-SMALLOPEN"].compound_stage is CompoundStage.CURATIVE_ADDED
    core = RuleSafetyCore()
    assert feed(core, "A-SMALLOPEN", [109.9, 109.9, 109.9], st) == []
    acts = feed(core, "A-SMALLOPEN", [110.0, 110.2], st)
    assert [a.rule_id for a in acts] == ["SR-CURATIVE-110"] and "防焦燒" in acts[0].message
    # an unvulcanised machine at 110 is fine
    assert feed(RuleSafetyCore(), "A-BIGOPEN", [110.0, 111.0, 112.0], st) == []


def test_press_below_120_while_curing_gets_cure_hold():
    st = states()
    core = RuleSafetyCore()
    assert feed(core, "CURE-07", [122.0, 121.0, 120.0, 120.0], st) == []
    acts = feed(core, "CURE-07", [119.9, 119.5], st)
    assert len(acts) == 1 and acts[0].action is SafetyActionKind.CURE_HOLD
    assert acts[0].message == "7 號機 119.5°C < 120°C：安全核心停用並扣模"
    # no cure job -> rule does not apply
    idle = {k: v.model_copy(update={"cure_job": None}) for k, v in st.items()}
    assert feed(RuleSafetyCore(), "CURE-07", [100.0, 100.0, 100.0], idle) == []


def test_press_over_130_alarms_only():
    st = states()
    acts = feed(RuleSafetyCore(), "CURE-03", [130.5, 131.0], st)
    assert [a.action for a in acts] == [SafetyActionKind.ALARM_ONLY]


def test_once_per_episode_and_rearm():
    st = states()
    core = RuleSafetyCore(rearm_samples=5)
    acts = feed(core, "CURE-07", [119.0] * 20, st)
    assert len(acts) == 1                                            # not every second
    assert feed(core, "CURE-07", [125.0] * 4 + [119.0] * 3, st) == []   # not yet re-armed
    assert feed(core, "CURE-07", [125.0] * 5, st) == []
    assert len(feed(core, "CURE-07", [119.0] * 3, st)) == 1          # new episode
    core.reset("CURE-07")
    assert len(feed(core, "CURE-07", [119.0, 119.0], st)) == 1


def test_single_spike_and_bad_values_never_trip():
    st = states()
    core = RuleSafetyCore()
    assert feed(core, "A-BIGOPEN", [100.0, 125.0, 100.0, 126.0, 100.0], st) == []   # one-sample glitches
    assert feed(core, "A-BIGOPEN", [None, float("nan"), 999.0, 999.0], st) == []
    assert RuleSafetyCore(confirm_samples=1).check([rd("A-BIGOPEN", 125.0)], st)   # configurable


def test_already_stopped_machine_is_not_stopped_again():
    st = states()
    st["CURE-07"] = st["CURE-07"].model_copy(update={"status": MachineStatus.OUT_OF_SERVICE})
    assert feed(RuleSafetyCore(), "CURE-07", [119.0, 119.0, 119.0], st) == []


def test_latency_well_under_200ms():
    """Page 9: reading -> action < 0.2 s, measured from the FIRST violating reading's ts (contract)."""
    st = states()
    core = RuleSafetyCore()
    worst = 0.0
    acts_all = []
    for k in range(200):
        # every machine ramps 1 °C/s from under its line (120 compound, 130 press): a plausible crossing
        readings = [rd(m, 125.0 + k if m.startswith("CURE") else 105.0 + k, k) for m in MACHINE_IDS]
        t0 = time.perf_counter()
        acts = core.check(readings, st)
        worst = max(worst, time.perf_counter() - t0)
        acts_all += acts
    assert acts_all
    for a in acts_all:
        assert a.latency_ms < LATENCY_SAFETY_S * 1000, a
    assert worst < LATENCY_SAFETY_S
    assert core.last_check_ms < 5.0


def test_jump_needs_confirm_and_latency_reports_the_wait():
    """A jump (or a cold start) is confirmed by a second sample; the 1 s it costs at 1 Hz is reported."""
    st = states()
    core = RuleSafetyCore()
    assert core.check([rd("A-BIGOPEN", 100.0, 0)], st) == []
    assert core.check([rd("A-BIGOPEN", 125.0, 1)], st) == []        # 25 °C/s: confirm first
    acts = core.check([rd("A-BIGOPEN", 125.5, 2)], st)
    assert len(acts) == 1 and 1000.0 <= acts[0].latency_ms < 1000.0 + LATENCY_SAFETY_S * 1000


def test_plausible_crossing_trips_on_first_violating_sample():
    st = states()
    core = RuleSafetyCore()
    for k, t in enumerate([117.0, 118.0, 119.0]):
        assert core.check([rd("A-BIGOPEN", t, k)], st) == []
    acts = core.check([rd("A-BIGOPEN", 120.1, 3)], st)
    assert len(acts) == 1 and acts[0].latency_ms < LATENCY_SAFETY_S * 1000


def test_intermittent_sensor_still_trips():
    """Review r3 #3: missing / garbage samples between good violating ones must not reset the confirm."""
    st = states()
    for bad in (None, 400.0, float("nan")):
        core = RuleSafetyCore()
        acts = []
        for k in range(120):
            acts += core.check([rd("A-BIGOPEN", 135.0 if k % 2 == 0 else bad, k)], st)
        assert [a.rule_id for a in acts] == ["SR-COMPOUND-120"], bad
    # a valid sample under the line still resets the confirm (spike filter unchanged)
    core = RuleSafetyCore()
    assert feed(core, "A-BIGOPEN", [100.0, 125.0, None, 100.0, 126.0, None, 100.0], st) == []


def test_missing_data_does_not_rearm():
    st = states()
    core = RuleSafetyCore(rearm_samples=3)
    for k, t in enumerate([118.0, 119.0, 121.0]):
        core.check([rd("A-BIGOPEN", t, k)], st)
    assert len(core.actions) == 1
    for k in range(3, 20):
        core.check([rd("A-BIGOPEN", None, k)], st)
    assert len(core.check([rd("A-BIGOPEN", 121.0, 20)], st)) == 0      # still latched: same episode


def test_data_loss_near_limit_raises_alarm_once():
    st = states()
    core = RuleSafetyCore(data_loss_s=5)
    core.check([rd("A-BIGOPEN", 117.0, 0)], st)                          # within 5 °C of 120
    acts = []
    for k in range(1, 20):
        acts += core.check([rd("A-BIGOPEN", None, k)], st)
    assert [(a.rule_id, a.action) for a in acts] == [("SR-DATA-LOSS", SafetyActionKind.ALARM_ONLY)]
    assert acts[0].ts == T0 + timedelta(seconds=5)
    # far from the limit: no alarm
    core = RuleSafetyCore(data_loss_s=5)
    core.check([rd("A-BIGOPEN", 90.0, 0)], st)
    assert all(not core.check([rd("A-BIGOPEN", None, k)], st) for k in range(1, 20))


def test_page11_red_path_acts_before_verify():
    """02:14:01 still 120–130 -> no stop; leak -> CURE_HOLD before 02:31:00, no LLM involved."""
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP, backfill_min=0)
    core = RuleSafetyCore()
    acts = []
    while f.clock.now() < datetime(2026, 10, 3, 2, 31, tzinfo=TZ):
        rs = f.step(1)
        for a in core.check(rs, f.states()):
            f.apply_safety_action(a)
            acts.append(a)
        if f.clock.now() == datetime(2026, 10, 3, 2, 14, 1, tzinfo=TZ):
            assert acts == []
    assert [(a.machine_id, a.action) for a in acts] == [("CURE-07", SafetyActionKind.CURE_HOLD)]
    assert datetime(2026, 10, 3, 2, 28, tzinfo=TZ) < acts[0].ts < datetime(2026, 10, 3, 2, 31, tzinfo=TZ)
    assert f.machine("CURE-07").status is MachineStatus.STOPPED
    assert f.safety_log == acts


def test_page2_overheat_never_stops_the_mill():
    f = Factory(scenario=ScenarioName.OPEN_MILL_OVERHEAT, backfill_min=0)
    core = RuleSafetyCore()
    for _ in range(40 * 60):
        assert core.check(f.step(1), f.states()) == []
    assert f.machine("A-BIGOPEN").status is MachineStatus.RUNNING


def test_overheat_past_limit_is_stopped_directly():
    from warroom.contracts import FaultKind, FaultSpec
    f = Factory(backfill_min=0)
    f.step(1)
    f.inject_fault(FaultSpec(id="hot", machine_id="A-BIGOPEN", kind=FaultKind.TEMP_RAMP,
                             params={"target": 130.0, "rate_c_per_min": 30.0}))
    core = RuleSafetyCore()
    acts = []
    for _ in range(120):
        for a in core.check(f.step(1), f.states()):
            f.apply_safety_action(a)
            acts.append(a)
    assert [(a.machine_id, a.rule_id) for a in acts] == [("A-BIGOPEN", "SR-COMPOUND-120")]
    assert f.machine("A-BIGOPEN").status is MachineStatus.STOPPED


def test_safety_imports_no_llm_agent_tools_or_api():
    code = ("import sys, warroom.safety; bad=[m for m in sys.modules if m.split('.')[0] in ('openai','httpx') "
            "or m.startswith(('warroom.agent','warroom.tools','warroom.api'))]; print(bad); assert not bad")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
