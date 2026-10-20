"""warroom/sim: factory model, sensor streams, scenarios, fault injection, replay."""
from datetime import datetime, timedelta

import pytest

from warroom.clock import SimClock
from warroom.contracts import (
    CompoundStage,
    FactorySim,
    FaultKind,
    FaultSpec,
    MachineStatus,
    SafetyAction,
    SafetyActionKind,
    ScenarioName,
    TeamId,
    TireSize,
)
from warroom.factory import COMPOUND_MACHINES, CURE_PRESS_IDS, MACHINE_IDS, TZ
from warroom.sim import Factory, ReplaySource, get_scenario, list_scenarios, record_jsonl


def run_until(f: Factory, hhmmss: str, day: int | None = None):
    """Step 1 s at a time until the factory clock shows hh:mm:ss; returns that tick."""
    h, m, s = map(int, hhmmss.split(":"))
    start = f.clock.now()
    target = start.replace(hour=h, minute=m, second=s, microsecond=0)
    if day is not None:
        target = target.replace(day=day)
    if target <= start:
        target += timedelta(days=1)
    rs = []
    while f.clock.now() < target:
        rs = f.step(1)
    return {r.machine_id: r for r in rs}


def test_protocol_and_28_readings_per_step():
    f = Factory(backfill_min=0)
    assert isinstance(f, FactorySim)
    rs = f.step(1)
    assert [r.machine_id for r in rs] == list(MACHINE_IDS)
    assert all(r.ts == f.clock.now() and r.ts.tzinfo is not None for r in rs)
    rs2 = f.step(1)
    assert all(b.seq == a.seq + 1 for a, b in zip(rs, rs2))
    presses = [r for r in rs2 if r.machine_id in CURE_PRESS_IDS]
    assert all(r.steam_kgf_cm2 is not None and r.water_flow_lpm is None for r in presses)
    others = [r for r in rs2 if r.machine_id not in CURE_PRESS_IDS]
    assert all(r.water_flow_lpm is not None and r.steam_kgf_cm2 is None for r in others)


def test_deterministic_for_seed():
    a, b = Factory(seed=3, backfill_min=0), Factory(seed=3, backfill_min=0)
    for _ in range(50):
        assert a.step(1) == b.step(1)
    c = Factory(seed=4, backfill_min=0)
    assert c.step(1) != Factory(seed=3, backfill_min=0).step(1)


def test_normal_operation_stays_in_band():
    f = Factory(backfill_min=0)
    for _ in range(600):
        rs = f.step(1)
        for r in rs:
            if r.machine_id in CURE_PRESS_IDS:
                assert 127.4 <= r.temp_c <= 128.6
                assert 19.5 <= r.pressure_kgf_cm2 <= 20.5
                assert 4.9 <= r.steam_kgf_cm2 <= 5.1
            elif r.machine_id in COMPOUND_MACHINES:
                assert r.temp_c < 108.0       # well under 110/120 limits
    assert all(m.status is MachineStatus.RUNNING for m in f.machines())
    assert f.faults() == []


def test_backfill_gives_30_min_history():
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
    f.step(1)
    h = f.history("CURE-07", 30)
    assert 1795 <= len(h) <= 1802
    assert h[-1].ts == f.clock.now()
    assert len(f.history("CURE-07", 0)) == 1


def test_page2_dashboard_numbers():
    f = Factory(backfill_min=0)
    f.step(1)
    prog = {p.size: p for p in f.order_progress()}
    assert (prog[TireSize.S].done, prog[TireSize.S].total, prog[TireSize.S].remaining_h) == (38, 100, 208)
    assert (prog[TireSize.M].done, prog[TireSize.M].total, prog[TireSize.M].remaining_h) == (12, 100, 1241)
    assert (prog[TireSize.L].done, prog[TireSize.L].total, prog[TireSize.L].remaining_h) == (8, 100, 2162)
    sf = f.semi_finished()
    assert sf.tire_label == "大胎 #07" and sf.size is TireSize.L
    assert [(p.part, p.status_text) for p in sf.parts] == [("A", "預熱中"), ("B", "成形中"), ("C", "包布中"),
                                                            ("D", "已完成")]
    assert sf.planned_mold_in_at.hour == 17 and sf.planned_mold_in_at.tzinfo is not None


def test_orders_and_shift_gap_page11():
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP, backfill_min=0)
    f.step(1)
    orders = {o.id: o for o in f.orders()}
    a311 = orders["#A-311"]
    assert a311.size is TireSize.S and a311.due == datetime(2026, 10, 6, tzinfo=TZ).date()   # 交期 3 天後
    gaps = [s for s in f.shifts() if s.required is not None and s.headcount < s.required]
    assert len(gaps) == 1
    g = gaps[0]
    assert g.team is TeamId.BODY and g.name == "夜班" and g.start.date() == datetime(2026, 10, 3, tzinfo=TZ).date()
    assert g.required - g.headcount == 1                                                       # 夜班缺 1 人
    assert g.start.hour == 20 and g.end == g.start + timedelta(hours=9)                        # 20:00–05:00
    # page 6 premise: curing team staggered 6–15 and 14–23
    curing = sorted({(s.start.hour, s.end.hour) for s in f.shifts() if s.team is TeamId.CURING})
    assert curing == [(6, 15), (14, 23)]
    # next mold on press 7 is #A-311 and press 7 (small-only, dataset EV-2) cures a small tire 14.4 h in at 02:14
    run_until(f, "02:14:00")
    job = f.machine("CURE-07").cure_job
    assert job.size is TireSize.S and abs(job.cure_elapsed_h - 14.4) < 1e-3 and job.cure_required_h == 24.0
    assert f._press["CURE-07"].queue[0] == "#A-311"


def test_press7_timeline_page11():
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
    base = run_until(f, "02:13:00")
    assert abs(base["CURE-07"].temp_c - 128) < 0.4
    at_detect = run_until(f, "02:14:00")
    assert abs(at_detect["CURE-07"].temp_c - 122) < 0.6
    assert at_detect["CURE-07"].steam_kgf_cm2 < 4.0
    assert f.fault_started_at("CURE-07") == datetime(2026, 10, 3, 2, 13, 15, tzinfo=TZ)
    before_leak = run_until(f, "02:22:30")
    assert abs(f.true_temp("CURE-07") - 122.0) < 0.1                 # H1 looked plausible: stable at 122
    assert abs(before_leak["CURE-06"].temp_c - 128) < 0.4
    verify = run_until(f, "02:31:00")
    assert abs(f.true_temp("CURE-07") - 119.5) < 0.15                # verify reads 119.5 -> H1 rejected
    assert verify["CURE-07"].temp_c < 120.0
    # same header H2: presses 6 and 8 dip slightly (evidence for a leak, not sensor drift)
    for p in ("CURE-06", "CURE-08"):
        assert 0.2 < 128.0 - f.true_temp(p) < 1.0
    for p in ("CURE-03", "CURE-10"):                                 # other headers untouched
        assert abs(f.true_temp(p) - 128.0) < 0.05


def _mean30(f: Factory, mid: str) -> float:
    h = f.history(mid, 0.5)
    return round(sum(r.temp_c for r in h) / len(h), 2)


def test_press7_verify_mean_page11():
    """The state machine's verify reads a 30-s mean on a 5-s grid against >= 121 and confirms a
    miss after 270 s. Pin the trajectory so that first miss is 02:26:30 and the verdict at
    02:31:00 (02:14:00 + 17 min) quotes page 11's 119.5 °C."""
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
    run_until(f, "02:26:25")
    assert _mean30(f, "CURE-07") >= 121.0
    run_until(f, "02:26:30")
    assert _mean30(f, "CURE-07") < 121.0
    run_until(f, "02:31:00")
    assert f"{_mean30(f, 'CURE-07'):.1f}" == "119.5"


def test_open_mill_overheat_page2():
    f = Factory(scenario=ScenarioName.OPEN_MILL_OVERHEAT, backfill_min=0)
    at = run_until(f, "14:26:00")
    r = at["A-BIGOPEN"]
    assert 111.0 <= r.temp_c <= 113.0                                # 膠溫 112°C
    assert r.water_flow_lpm < 20.0                                   # 水流偏低
    st = f.machine("A-BIGOPEN")
    assert abs(st.step_elapsed_min - 14.0) < 0.05                    # 時間 14 分
    assert st.compound_stage is CompoundStage.UNVULCANIZED
    assert abs(r.pressure_kgf_cm2 - 3.0) < 0.3                       # 壓力 正常
    assert f.semi_finished().parts[0].state == "alert"
    later = run_until(f, "14:55:00")
    assert 116.0 < later["A-BIGOPEN"].temp_c < 119.5                 # levels off below the 120 limit


def test_inject_and_clear_faults():
    f = Factory(backfill_min=0)
    for _ in range(5):
        f.step(1)
    assert f.fault_started_at("A-MIX") is None
    f.inject_fault(FaultSpec(id="x", machine_id="A-MIX", kind=FaultKind.TEMP_RAMP,
                             params={"target": 118.0, "rate_c_per_min": 6.0}))
    t0 = f.clock.now()
    for _ in range(60):
        f.step(1)
    assert abs(f.true_temp("A-MIX") - 111.0) < 0.3                   # 105 + 6 °C/min × 1 min
    assert f.fault_started_at("A-MIX") == t0
    f.clear_faults("A-MIX")
    assert f.fault_started_at("A-MIX") is None and f.faults() == []
    with pytest.raises(KeyError):
        f.inject_fault(FaultSpec(id="y", machine_id="NOPE", kind=FaultKind.SPIKE))


def test_fault_duration_and_spike_and_dropout():
    f = Factory(backfill_min=0)
    f.step(1)
    f.inject_fault(FaultSpec(id="s", machine_id="CURE-02", kind=FaultKind.SPIKE, params={"value": 250.0}))
    f.inject_fault(FaultSpec(id="d", machine_id="A-MIX", kind=FaultKind.SENSOR_DROPOUT, duration_s=3,
                             params={"frozen": 0.0}))
    seen = [f.step(1) for _ in range(6)]
    c2 = [next(r for r in rs if r.machine_id == "CURE-02").temp_c for rs in seen]
    assert c2[0] == 250.0 and all(abs(v - 128) < 0.5 for v in c2[1:])
    mix = [next(r for r in rs if r.machine_id == "A-MIX").temp_c for rs in seen]
    assert mix[:2] == [None, None] and mix[3] is not None
    assert abs(f.true_temp("CURE-02") - 128) < 0.3                   # process untouched by sensor faults


def test_sensor_drift_biases_reading_not_process():
    f = Factory(backfill_min=0)
    f.step(1)
    f.inject_fault(FaultSpec(id="d", machine_id="CURE-05", kind=FaultKind.SENSOR_DRIFT,
                             params={"rate_c_per_min": -1.0, "max_c": -3.0}))
    for _ in range(300):
        rs = f.step(1)
    r = next(x for x in rs if x.machine_id == "CURE-05")
    assert abs(r.temp_c - 125.0) < 0.4 and abs(f.true_temp("CURE-05") - 128) < 0.3
    assert abs(r.steam_kgf_cm2 - 5.0) < 0.05


def test_safety_action_and_status_changes():
    f = Factory(backfill_min=0)
    f.step(1)
    a = SafetyAction(id="a", ts=f.clock.now(), machine_id="CURE-07", rule_id="SR-CURE-LOW-120",
                     action=SafetyActionKind.CURE_HOLD, value=119.5, threshold=120, message="m", latency_ms=0.1)
    f.apply_safety_action(a)
    st = f.machine("CURE-07")
    assert st.status is MachineStatus.STOPPED and st.cure_job is not None and "扣模" in st.note
    f.set_machine_status("CURE-07", MachineStatus.OUT_OF_SERVICE, "7 號機停用")
    assert f.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
    stop = a.model_copy(update={"machine_id": "A-BIGOPEN", "action": SafetyActionKind.STOP_MACHINE})
    f.apply_safety_action(stop)
    for _ in range(300):
        f.step(1)
    assert f.machine("A-BIGOPEN").status is MachineStatus.STOPPED
    assert f.true_temp("A-BIGOPEN") < 90.0                           # a stopped mill cools down
    job = f.set_cure_required("CURE-09", 36.5)
    assert job.cure_required_h == 36.5 and job.planned_demold_at == job.mold_in_at + timedelta(hours=36.5)
    f.set_active_event("CURE-09", "EV-1")
    assert f.machine("CURE-09").active_event_id == "EV-1"


def test_cure_cycles_complete_and_orders_advance():
    f = Factory(backfill_min=0)
    before = {p.size: p for p in f.order_progress()}
    for _ in range(6 * 24):                                          # one day in 10-minute steps
        f.step(600)
    after = {p.size: p for p in f.order_progress()}
    assert after[TireSize.S].done > before[TireSize.S].done
    assert after[TireSize.S].remaining_h < before[TireSize.S].remaining_h
    assert sum(a.done for a in after.values()) > sum(b.done for b in before.values())
    # every press is curing again or in its 30-min de-mold/mold-in window
    for pid in CURE_PRESS_IDS:
        st = f.machine(pid)
        assert st.cure_job is not None or st.status is MachineStatus.IDLE


def test_accelerated_steps_match_fine_steps():
    a = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP, backfill_min=0)
    b = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP, backfill_min=0)
    for _ in range(150):
        a.step(1)
    for _ in range(15):
        b.step(10)
    assert a.clock.now() == b.clock.now()
    assert abs(a.true_temp("CURE-07") - b.true_temp("CURE-07")) < 0.05


def test_running_clock_integrates_elapsed_time():
    clk = SimClock(datetime(2026, 10, 3, 8, 0, tzinfo=TZ), speed=60.0)
    clk.pause()
    t0 = clk.now()
    f = Factory(clock=clk, backfill_min=0)                           # NORMAL keeps the clock's time
    assert f.clock.now() == t0 and abs((t0 - datetime(2026, 10, 3, 8, 0, tzinfo=TZ)).total_seconds()) < 1
    clk.advance(30)
    rs = f.step(1)                                                   # dt ignored: the clock already moved 30 s
    assert rs[0].ts == t0 + timedelta(seconds=30)
    assert f.history("A-MIX", 0)[0].ts == rs[0].ts


def test_load_scenario_resets_and_moves_clock():
    clk = SimClock(datetime(2026, 1, 1, tzinfo=TZ), manual=True)
    f = Factory(clock=clk, scenario=ScenarioName.PRESS7_TEMP_DROP, backfill_min=0)
    assert clk.now() == datetime(2026, 10, 3, 2, 12, tzinfo=TZ)
    f.step(100)
    sc = f.load_scenario(ScenarioName.OPEN_MILL_OVERHEAT)
    assert clk.now() == sc.start_at and f.faults() == []
    assert [s.fault.machine_id for s in sc.steps] == ["A-BIGOPEN"]


def test_scenarios_catalogue():
    names = {s.name for s in list_scenarios()}
    assert names == set(ScenarioName)
    p11 = get_scenario(ScenarioName.PRESS7_TEMP_DROP)
    assert p11.start_at.tzinfo is not None and p11.steps[0].fault.machine_id == "CURE-07"
    moved = get_scenario(ScenarioName.PRESS7_TEMP_DROP, start_at=datetime(2026, 11, 1, tzinfo=TZ))
    assert moved.start_at.month == 11 and moved.steps == p11.steps


def test_replay_roundtrip(tmp_path):
    f = Factory(backfill_min=0)
    path = tmp_path / "rec.jsonl"
    ticks = [f.step(1) for _ in range(5)]
    n = sum(record_jsonl(t, path) for t in ticks)
    assert n == 5 * 28
    replayed = list(ReplaySource(path))
    assert replayed == ticks
    looped = ReplaySource(path, loop=True)
    it = iter(looped)
    got = [next(it) for _ in range(12)]
    assert got[5] == ticks[0] and got[11] == ticks[1]
    with pytest.raises(FileNotFoundError):
        ReplaySource(tmp_path / "missing.jsonl")


# ---------------------------------------------------------------- plan-driven mold-in (page 6)
def _plan_with(f, extra_jobs, drop_press_jobs=()):
    """The plant's in-progress cures as frozen jobs plus `extra_jobs`."""
    from warroom.contracts import Schedule, SolverName
    from warroom.schedule import back_propagate
    jobs = []
    for m in f.machines():
        cj = m.cure_job
        if cj is not None:
            jobs.append(back_propagate(cj.job_id, cj.order_id, cj.size, m.machine_id, cj.mold_in_at,
                                       cure_h=cj.cure_required_h, mold_id=cj.mold_id, frozen=True))
    t0 = f.now()
    return Schedule(id="S-T", created_at=t0, horizon_start=t0, horizon_end=t0 + timedelta(days=7),
                    jobs=jobs + list(extra_jobs), solver=SolverName.HEURISTIC)


def test_plan_attached_press_stays_idle_until_planned_mold_in():
    """The plan's next job decides mold-in: no job -> the press stays idle after de-mold; a job at T
    -> it loads at T (not at de-mold + 30 min) with the plan's job id, order and cure_h."""
    from warroom.contracts import TireSize
    from warroom.schedule import back_propagate
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    cj6 = f.machine("CURE-06").cure_job               # small, de-molds 3 h after start
    T = cj6.planned_demold_at + timedelta(hours=5)
    job = back_propagate("J-PLAN-06", "#S-102", TireSize.S, "CURE-06", T, cure_h=25.0, mold_id="MD-S102")
    sched = _plan_with(f, [job])
    f.attach_plan(lambda: sched)
    f.step((cj6.planned_demold_at - f.now()).total_seconds() + 3600)        # de-molded 1 h ago
    m = f.machine("CURE-06")
    assert m.cure_job is None and m.status is MachineStatus.IDLE and "待排程入模" in (m.note or "")
    while f.now() < T + timedelta(minutes=1):
        f.step(60.0)
    cj = f.machine("CURE-06").cure_job
    assert cj is not None and cj.job_id == "J-PLAN-06" and cj.order_id == "#S-102"
    assert cj.mold_in_at == T and cj.cure_required_h == 25.0 and cj.planned_demold_at == T + timedelta(hours=25)
    # a press with nothing planned after its current cure stays idle (past its 30-min change)
    while f.now() < T + timedelta(hours=2):
        f.step(60.0)
    m5 = f.machine("CURE-05")
    assert m5.cure_job is None and m5.status is MachineStatus.IDLE and m5.note == "排程無下一模，待命"


def test_approved_press_move_reaches_the_plant():
    """Page 11: #A-311 (small) moved from press 7 to press 4 in the ACTIVE plan -> the plant loads it on 4, never 7."""
    from warroom.contracts import TireSize
    from warroom.schedule import back_propagate
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    free9 = f.machine("CURE-04").cure_job.planned_demold_at + timedelta(minutes=30)
    free7 = f.machine("CURE-07").cure_job.planned_demold_at + timedelta(minutes=30)
    on7 = back_propagate("J-A311-01", "#A-311", TireSize.S, "CURE-07", free7, mold_id="#A-311")
    on9 = back_propagate("J-A311-01", "#A-311", TireSize.S, "CURE-04", free9, mold_id="#A-311")
    v1, v2 = _plan_with(f, [on7]), _plan_with(f, [on9])
    active = {"s": v1}
    f.attach_plan(lambda: active["s"])
    active["s"] = v2                                   # supervisor approved the move
    end = max(free7, free9) + timedelta(hours=1)
    while f.now() < end:
        f.step(60.0)
    j9, j7 = f.machine("CURE-04").cure_job, f.machine("CURE-07").cure_job
    assert j9 is not None and j9.order_id == "#A-311" and j9.mold_in_at == free9
    assert j7 is None or j7.order_id != "#A-311"


def test_no_plan_keeps_the_sims_own_queue():
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    free7 = f.machine("CURE-07").cure_job.planned_demold_at + timedelta(minutes=30)
    while f.now() < free7 + timedelta(minutes=1):
        f.step(60.0)
    assert f.machine("CURE-07").cure_job.order_id == "#A-311"     # hard-coded queue, no plan attached
