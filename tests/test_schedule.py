"""Schedule module: Arrhenius cure (p.11), back-propagation (p.6), CP-SAT + backup solver,
version store with rollback (p.7), page-11 reschedule, page-6 capacity simulation."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from warroom import factory as F
from warroom.contracts import (
    CureFormulaArgs,
    Order,
    Scheduler,
    ScheduleStatus,
    ScheduleStore,
    SolveRequest,
    SolveResult,
    SolverName,
    SolveStatus,
    TeamId,
    TireSize,
)
from warroom.schedule import (
    CpSatScheduler,
    FallbackScheduler,
    FileScheduleStore,
    HeuristicScheduler,
    back_propagate,
    capacity_report,
)
from warroom.schedule.backprop import labor_for
from warroom.schedule.capacity import (
    POLICIES,
    lead_time_from_rates,
    simulated_lead_times,
    simulated_rows,
    weekly_per_press,
)
from warroom.schedule.demo import demo_inputs, reschedule
from warroom.schedule.solver import check_schedule
from warroom.schedule.staffing import complete_shifts, gap_label, is_short
from warroom.tools.cure import arrhenius_cure

NOW = datetime(2026, 10, 3, 2, 14, 25, tzinfo=F.TZ)     # page 11, 02:14:25 solver step
VERIFY = datetime(2026, 10, 3, 2, 31, 4, tzinfo=F.TZ)   # page 11, 02:31:04 replan


# ---------------------------------------------------------------- Arrhenius (page 11)
def test_arrhenius_page11_numbers():
    d = arrhenius_cure(CureFormulaArgs(size=TireSize.M, ref_temp_c=128.0, actual_temp_c=122.0, elapsed_h=10.0,
                                       activation_energy_kj_mol=85.0))
    assert d.rate_ratio == pytest.approx(0.679, abs=0.001)
    assert d.remaining_ref_h == pytest.approx(18.0)
    assert d.remaining_actual_h == pytest.approx(26.5, abs=0.05)
    assert round(d.total_cure_h, 1) == 36.5
    assert round(d.extra_h, 1) == 8.5
    assert "36.5" in d.formula


def test_arrhenius_dataset_ev2_numbers():
    """Team dataset EV-2 (audit_cure.csv): S-027 on V07, 864 min elapsed of a 24 h cure."""
    at122 = arrhenius_cure(CureFormulaArgs(size=TireSize.S, ref_temp_c=128.0, actual_temp_c=122.0, elapsed_h=14.4,
                                           activation_energy_kj_mol=85.0))
    assert at122.rate_ratio == pytest.approx(0.679, abs=0.001)
    assert at122.remaining_ref_h == pytest.approx(9.6)
    assert at122.total_cure_h * 60 == pytest.approx(1712.2, abs=0.5)      # dataset: total_equiv_min
    assert round(at122.total_cure_h, 1) == 28.5 and round(at122.extra_h, 1) == 4.5
    at1195 = arrhenius_cure(CureFormulaArgs(size=TireSize.S, ref_temp_c=128.0, actual_temp_c=119.5, elapsed_h=14.4,
                                            activation_energy_kj_mol=85.0))
    assert at1195.total_cure_h * 60 == pytest.approx(1864.1, abs=0.5)
    assert round(at1195.total_cure_h, 1) == 31.1


def test_arrhenius_constants_pinned():
    assert F.ACTIVATION_ENERGY_KJ_MOL == 85.0 and F.GAS_CONSTANT_J_MOL_K == 8.314
    assert F.CURE_HOURS[TireSize.M] == 28.0 and F.CURE_TEMP_NOMINAL_C == 128.0


def test_arrhenius_behaviour():
    same = arrhenius_cure(CureFormulaArgs(size=TireSize.L, actual_temp_c=128.0, elapsed_h=5))
    assert same.rate_ratio == pytest.approx(1.0) and same.total_cure_h == pytest.approx(34.0)
    hotter = arrhenius_cure(CureFormulaArgs(size=TireSize.S, actual_temp_c=130.0, elapsed_h=0))
    assert hotter.total_cure_h < 24.0
    lower_ea = arrhenius_cure(CureFormulaArgs(size=TireSize.M, actual_temp_c=122.0, elapsed_h=10,
                                              activation_energy_kj_mol=60.0))
    assert 28.0 < lower_ea.total_cure_h < 36.5          # Ea is configurable
    with pytest.raises(ValueError):
        arrhenius_cure(CureFormulaArgs(size=TireSize.M, actual_temp_c=122.0, elapsed_h=-1))


# ---------------------------------------------------------------- back-propagation (page 6)
@pytest.mark.parametrize("size,bond,feed,pieces", [(TireSize.S, 40, 100, 6), (TireSize.M, 60, 150, 8),
                                                    (TireSize.L, 90, 210, 10)])
def test_back_propagate_page6(size, bond, feed, pieces):
    T = datetime(2026, 10, 5, 17, 0, tzinfo=F.TZ)
    j = back_propagate("J1", "#X", size, "CURE-08" if size is not TireSize.S else "CURE-01", T)
    assert T - j.bond_start == timedelta(minutes=bond)
    assert T - j.a_feed_start == timedelta(minutes=feed)
    assert j.side_pieces == pieces and j.bead_pieces == pieces
    assert j.demold - j.mold_in == timedelta(hours=F.CURE_HOURS[size])
    tasks = {(n.task, n.team) for n in j.labor}
    assert ("bonding", TeamId.BOND) in tasks and ("a_feed", TeamId.BODY) in tasks
    assert ("mold_in", TeamId.CURING) in tasks          # 17:00 is curing 中班


def test_labor_night_op_goes_to_body_crew():
    T = datetime(2026, 10, 5, 2, 0, tzinfo=F.TZ)        # Mon 02:00 -> only the BODY night crew
    j = back_propagate("J1", "#X", TireSize.M, "CURE-07", T)
    mold_in = next(n for n in labor_for(j) if n.task == "mold_in")
    assert mold_in.team is TeamId.BODY and mold_in.end - mold_in.start == timedelta(minutes=30)


# ---------------------------------------------------------------- staffing rules
def test_shift_expansion_weekend_and_gap_rule():
    start = datetime(2026, 10, 3, 0, 0, tzinfo=F.TZ)   # Saturday
    shifts = complete_shifts([], start, start + timedelta(days=3))
    sat_night = next(s for s in shifts if s.team is TeamId.BODY and s.name == "夜班" and s.start.date() == start.date())
    assert sat_night.end == datetime(2026, 10, 4, 5, 0, tzinfo=F.TZ)            # Sat night to Sun 05:00
    assert not any(s.start.date() == start.date() + timedelta(days=1) for s in shifts)   # Sunday: nobody
    assert is_short(sat_night, 1) == 0 and is_short(sat_night, 2) == 1          # SOP-HR-01 §4
    assert gap_label(sat_night, 1) == "10/03 夜班缺 1 人"


# ---------------------------------------------------------------- solvers on generated jobs
def _orders(day):
    return [Order(id="#M-1", size=TireSize.M, qty=10, due=day + timedelta(days=14), mold_id="M1"),
            Order(id="#L-1", size=TireSize.L, qty=10, due=day + timedelta(days=21), mold_id="L1"),
            Order(id="#S-1", size=TireSize.S, qty=40, due=day + timedelta(days=10), mold_id="S1")]


@pytest.mark.parametrize("solver", [CpSatScheduler(), HeuristicScheduler(), FallbackScheduler()])
def test_solvers_generate_feasible_cure_centric_plan(solver):
    now = datetime(2026, 10, 5, 7, 0, tzinfo=F.TZ)     # Monday
    req = SolveRequest(now=now, orders=_orders(now.date()), shifts=[], unavailable_presses=["CURE-10"],
                       time_limit_s=3)
    res = solver.solve(req)
    assert isinstance(solver, Scheduler)
    assert res.schedule is not None and res.feasible, (res.status, res.gaps, res.schedule and res.schedule.warnings)
    assert check_schedule(res.schedule, req) == []
    jobs = res.schedule.jobs
    assert len(jobs) > 20
    for j in jobs:
        assert j.press_id != "CURE-10"
        if j.size is TireSize.S:
            assert j.press_id in F.SMALL_ONLY_PRESSES
        else:
            assert j.press_id in F.ML_PRESSES
        # weekend rule: nothing de-molds Sunday after 05:00 (no staff), feed lead respected
        assert not (j.demold.weekday() == 6 and j.demold.hour >= 5)
        assert j.mold_in - now >= timedelta(minutes=F.BACKPROP[j.size]["a_feed_min"])
        assert j.demold - j.mold_in == timedelta(hours=F.CURE_HOURS[j.size])


def test_cpsat_not_worse_than_heuristic():
    now = datetime(2026, 10, 5, 7, 0, tzinfo=F.TZ)
    req = SolveRequest(now=now, orders=_orders(now.date()), shifts=[], time_limit_s=3)
    a, b = CpSatScheduler().solve(req), HeuristicScheduler().solve(req)
    assert a.solver is SolverName.CP_SAT and a.status in (SolveStatus.OPTIMAL, SolveStatus.FEASIBLE)
    total = lambda r: sum((j.demold - now).total_seconds() for j in r.schedule.jobs)
    assert total(a) <= total(b) + 1


class _TimeoutSolver:
    def solve(self, req):
        return SolveResult(status=SolveStatus.TIMEOUT, solver=SolverName.CP_SAT, solve_ms=10_000, schedule=None,
                           feasible=False, deliveries_met=False)


class _RaisingSolver:
    def solve(self, req):
        raise RuntimeError("boom")


@pytest.mark.parametrize("primary", [_TimeoutSolver(), _RaisingSolver()])
def test_fallback_to_backup_solver(primary):
    now = datetime(2026, 10, 5, 7, 0, tzinfo=F.TZ)
    res = FallbackScheduler(primary=primary).solve(SolveRequest(now=now, orders=_orders(now.date()), shifts=[]))
    assert res.fell_back and res.solver is SolverName.HEURISTIC and res.schedule is not None and res.feasible
    assert "備援" in res.schedule.warnings[0]


def test_real_cpsat_tiny_time_limit_still_answers():
    now = datetime(2026, 10, 5, 7, 0, tzinfo=F.TZ)
    res = FallbackScheduler().solve(SolveRequest(now=now, orders=_orders(now.date()), shifts=[], time_limit_s=0.001))
    assert res.schedule is not None and res.feasible   # either CP-SAT made it or the backup answered


# ---------------------------------------------------------------- store (page 7)
def test_store_versions_and_rollback(tmp_path):
    _, _, seed = demo_inputs(NOW)
    st = FileScheduleStore(tmp_path / "s", initial=seed)
    assert isinstance(st, ScheduleStore)
    assert st.current().version == 1 and st.current().status is ScheduleStatus.ACTIVE
    v2 = st.propose(seed.model_copy(update={"id": "B"}), "test b", "EV-1")
    assert v2.status is ScheduleStatus.PROPOSED and v2.parent_version == 1 and st.current().version == 1
    st.activate(2)
    v3 = st.propose(seed.model_copy(update={"id": "C"}), "test c")
    st.activate(3)
    assert [v.status for v in st.versions()] == [ScheduleStatus.SUPERSEDED, ScheduleStatus.SUPERSEDED,
                                                 ScheduleStatus.ACTIVE]
    back = st.rollback()                                  # one click -> previous ACTIVE (2)
    assert back.version == 2 and st.current().schedule.id == "B"
    assert st.get(3).status is ScheduleStatus.ROLLED_BACK
    assert st.rollback().version == 1                     # and again -> 1
    # persisted: a new store on the same folder sees the same state
    st2 = FileScheduleStore(tmp_path / "s")
    assert st2.current().version == 1 and len(st2.versions()) == 3
    assert sum(v.status is ScheduleStatus.ACTIVE for v in st2.versions()) == 1
    with pytest.raises(ValueError):
        st2.activate(3)                                   # rolled-back versions are not re-activated
    assert st2.rollback(to_version=3).version == 3       # but an explicit rollback target works
    with pytest.raises(KeyError):
        st2.get(99)
    assert v3.event_id is None and v2.event_id == "EV-1"


def test_rollback_refuses_unapproved_proposed_version(tmp_path):
    """Rolling 'back' to a PROPOSED version would activate it without gate + approval."""
    _, _, seed = demo_inputs(NOW)
    st = FileScheduleStore(tmp_path / "s", initial=seed)
    v2 = st.propose(seed.model_copy(update={"id": "B"}), "pending R3 transfer", "EV-1")
    with pytest.raises(ValueError, match="proposed"):
        st.rollback(to_version=v2.version)
    assert st.current().version == 1 and st.get(2).status is ScheduleStatus.PROPOSED
    assert st.activate(2).version == 2                    # the approval path still works afterwards
    assert st.rollback().version == 1                     # and one-click rollback to an approved version too


# ---------------------------------------------------------------- page-11 demo
def test_page11_reschedule_keeps_A311_on_press7_then_moves_it(tmp_path):
    """Dataset EV-2 layout (issue #20): press 7 is small-only and cures a small tire (14.4 h of 24 h at 02:14).
    A 122 °C hold stretches it to 28.5 h; the next mold #A-311 (small) still fits on press 7, so nothing moves.
    Only when press 7 is taken out of service (02:31) does #A-311 go to another small press."""
    orders, shifts, seed = demo_inputs(NOW)
    a311 = next(j for j in seed.jobs if j.order_id == "#A-311")
    assert a311.press_id == "CURE-07" and not a311.frozen      # next mold on press 7
    assert a311.size is TireSize.S and a311.press_id in F.SMALL_ONLY_PRESSES
    store = FileScheduleStore(tmp_path / "s", initial=seed)
    # S1 says 28.5 h total for CURE-07 -> S3 solve
    res, ver = reschedule(store, FallbackScheduler(), NOW, orders, shifts, cure_overrides_h={"CURE-07": 28.5},
                          event_id="EV-20261003-021400-CURE-07")
    assert res.solver is SolverName.CP_SAT and not res.fell_back
    assert res.feasible and res.deliveries_met and res.late_orders == []
    assert [m.order_id for m in res.moves if m.from_press != m.to_press] == []           # nothing changes press
    assert next(j for j in res.schedule.jobs if j.order_id == "#A-311").press_id == "CURE-07"
    assert res.gaps == ["10/03 夜班缺 1 人"]
    assert [o.label for o in res.staffing_options] == ["夜班加班", "從 2 線調 1 人"]
    assert [o.kind for o in res.staffing_options] == ["overtime", "transfer"]
    assert res.schedule.staffing_changes[0].label == "從 2 線調 1 人"        # solver's pick (page 11 02:14:25)
    assert ver is not None and ver.status is ScheduleStatus.PROPOSED and "從 2 線調 1 人" in ver.reason
    cure07 = next(j for j in res.schedule.jobs if j.press_id == "CURE-07" and j.frozen)
    assert cure07.cure_h == pytest.approx(28.5)
    assert res.solve_ms < 10_000
    store.activate(ver.version)
    # 02:31 H1 rejected -> CURE-07 out of service; supervisor rejects the cross-team move
    res2, ver2 = reschedule(store, FallbackScheduler(), VERIFY, orders, shifts, unavailable_presses=["CURE-07"],
                            cure_overrides_h={"CURE-07": 28.5}, allow_cross_team=False)
    assert res2.feasible and res2.deliveries_met
    assert not any(j.press_id == "CURE-07" and not j.frozen for j in res2.schedule.jobs)
    moved = next(j for j in res2.schedule.jobs if j.order_id == "#A-311")
    assert moved.press_id != "CURE-07" and moved.press_id in F.SMALL_ONLY_PRESSES      # a small press takes it
    assert [c.label for c in res2.schedule.staffing_changes] == ["夜班加班"]
    assert any("CURE-07 停用" in w for w in res2.schedule.warnings)
    store.activate(ver2.version)
    assert store.rollback().version == ver.version        # one-click rollback to the 02:16 plan


def test_without_cure_extension_nothing_moves():
    orders, shifts, seed = demo_inputs(NOW)
    res = CpSatScheduler().solve(SolveRequest(now=NOW, orders=orders, shifts=shifts, previous=seed))
    assert res.feasible
    assert [m for m in res.moves if m.from_press != m.to_press] == []


# ---------------------------------------------------------------- capacity (page 6)
def test_capacity_rows_equal_page6_table():
    rows = capacity_report().rows
    assert [r.label for r in rows] == [t.label for t in F.CAPACITY_TARGETS]
    for r, t in zip(rows, F.CAPACITY_TARGETS):
        assert abs(r.small - t.small) <= 0.05 and abs(r.medium - t.medium) <= 0.05 and abs(r.large - t.large) <= 0.05


def test_capacity_lead_times_equal_page6_box():
    r = capacity_report()
    assert tuple(r.ml_100_weeks) == F.LEAD_TIME_TARGET_WEEKS["ML_100_each"]      # 13.3 -> 9.3
    assert tuple(r.small_100_weeks) == F.LEAD_TIME_TARGET_WEEKS["small"]         # 2.4 -> 2.0
    assert r.ml_100_weeks[1] / r.ml_100_weeks[0] < 0.75                          # about −30 %
    # the page-6 box follows from the table rates in closed form
    ml0, s0 = lead_time_from_rates(5, 3, 6)
    ml1, s1 = lead_time_from_rates(6, 4.9, 7)
    assert (round(ml0, 1), round(ml1, 1), round(s0, 1), round(s1, 1)) == (13.3, 9.3, 2.4, 2.0)
    assert "離散事件模擬檢核" in r.conditions                                       # sim shown as a labelled check


def test_simulation_current_matches_page6_exactly():
    cur = {r.label: r for r in simulated_rows()}["現況"]
    assert (cur.small, cur.medium, cur.large) == (6.0, 5.0, 3.0)


def test_simulation_ai_matches_page6_within_0_1():
    ai = {r.label: r for r in simulated_rows()}["AI 排出模時刻"]
    t = {t.label: t for t in F.CAPACITY_TARGETS}["AI 排出模時刻"]
    assert abs(ai.small - t.small) <= 0.1 and abs(ai.medium - t.medium) <= 0.1 and abs(ai.large - t.large) <= 0.1


def test_simulation_extended_shifts_known_deviation():
    """只延長班表: page 6 says 6 / 5.9 / 4.5; the stated conditions give 6 / 6.0 / 4.82 in our
    simulation. It is a secondary check, not the headline; locked so a change is visible."""
    ext = {r.label: r for r in simulated_rows()}["只延長班表"]
    assert ext.small == 6.0 and abs(ext.medium - 5.9) <= 0.1
    assert ext.large == pytest.approx(4.82, abs=0.01)


def test_simulation_monotone_and_lead_times_close():
    cur, ext, ai = simulated_rows()
    assert cur.large < ext.large < ai.large and cur.medium < ext.medium <= ai.medium
    (ml0, ml1), (s0, s1) = simulated_lead_times()
    assert abs(ml0 - 13.3) <= 0.15 and abs(ml1 - 9.3) <= 0.15
    assert abs(s0 - 2.4) <= 0.15 and abs(s1 - 2.0) <= 0.15


def test_weekly_per_press_policy_names():
    assert [p.label for p in POLICIES] == [t.label for t in F.CAPACITY_TARGETS]
    assert weekly_per_press(POLICIES[0], 34.0) == 3.0


# ---------------------------------------------------------------- with the real factory sim
def test_page11_against_factory_sim(tmp_path):
    sim_mod = pytest.importorskip("warroom.sim")
    from warroom.contracts import ScenarioName
    from warroom.schedule import seed_schedule
    try:
        sim = sim_mod.Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
        sim.machines()
    except NotImplementedError:
        pytest.skip("sim not implemented")
    now = sim.clock.now()
    seed = seed_schedule(now, sim.orders(), sim.shifts(), sim.machines(), next_on={"CURE-07": "#A-311"})
    nxt = min((j for j in seed.jobs if j.press_id == "CURE-07" and not j.frozen), key=lambda j: j.mold_in)
    assert nxt.order_id == "#A-311"
    store = FileScheduleStore(tmp_path / "s", initial=seed)
    res, ver = reschedule(store, FallbackScheduler(), now + timedelta(seconds=145), sim.orders(), sim.shifts(),
                          cure_overrides_h={"CURE-07": 28.5})
    assert res.feasible and res.deliveries_met and ver is not None
    a311 = next(m for m in res.moves if m.order_id == "#A-311")
    assert (a311.from_press, a311.to_press) == ("CURE-07", "CURE-07")      # small tire still fits on press 7
    assert "10/03 夜班缺 1 人" in res.gaps
    assert {o.label for o in res.staffing_options} >= {"夜班加班", "從 2 線調 1 人"}
    assert res.solve_ms < 10_000


# ---------------------------------------------------------------- review fixes (r2)
def _sim_seed():
    sim_mod = pytest.importorskip("warroom.sim")
    from warroom.clock import SimClock
    from warroom.contracts import ScenarioName
    from warroom.schedule import seed_schedule
    clock = SimClock(start=datetime(2026, 10, 3, 2, 14, 20, tzinfo=F.TZ), manual=True)
    sim = sim_mod.Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
    seed = seed_schedule(clock.now(), sim.orders(), sim.shifts(), sim.machines(), next_on={"CURE-07": "#A-311"})
    return clock, sim, seed


def test_solve_schedule_args_bounded_so_gate_rejects_bad_values():
    from pydantic import ValidationError

    from warroom.agent import RuleGate
    from warroom.contracts import EventState, GateVerdict, PlanStep, SolveScheduleArgs, ToolName
    for bad in ({"horizon_days": 0}, {"horizon_days": -3}, {"horizon_days": 15}, {"time_limit_s": -1},
                {"time_limit_s": 0}, {"cure_overrides_h": {"CURE-07": -5}}, {"cure_overrides_h": {"CURE-07": 500}}):
        with pytest.raises(ValidationError):
            SolveScheduleArgs.model_validate(bad)
        d = RuleGate().check(PlanStep(id="S3", tool=ToolName.SOLVE_SCHEDULE, args=bad, purpose="x"),
                             EventState.SIMULATE, True)
        assert d.verdict is not GateVerdict.ALLOW, bad
    ok = RuleGate().check(PlanStep(id="S3", tool=ToolName.SOLVE_SCHEDULE, args={"cure_overrides_h": {"CURE-07": 36.5}},
                                   purpose="x"), EventState.SIMULATE, True)
    assert ok.verdict is GateVerdict.ALLOW


def test_cpsat_skips_degenerate_request_and_fallback_answers():
    now = datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ)
    orders, shifts, seed = demo_inputs(now)
    for kw in ({"horizon_days": 0}, {"horizon_days": -3}, {"time_limit_s": -1.0}):
        req = SolveRequest(now=now, orders=orders, shifts=shifts, previous=seed, **kw)
        assert CpSatScheduler().solve(req).status is SolveStatus.ERROR
        res = FallbackScheduler().solve(req)
        assert res.fell_back and res.schedule is not None and res.solver is SolverName.HEURISTIC


_SHORT_HORIZON_CHILD = r"""
import tempfile, sys
from pathlib import Path
from datetime import datetime
from warroom import factory as F
from warroom.clock import SimClock
from warroom.contracts import ScenarioName, SolveRequest
from warroom.sim import Factory
from warroom.schedule import CpSatScheduler, FallbackScheduler, seed_schedule
clock = SimClock(start=datetime(2026, 10, 3, 2, 14, 20, tzinfo=F.TZ), manual=True)
sim = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
seed = seed_schedule(clock.now(), sim.orders(), sim.shifts(), sim.machines(), next_on={"CURE-07": "#A-311"})
for h in (0, 1, 2):
    req = SolveRequest(now=clock.now(), orders=sim.orders(), shifts=sim.shifts(), previous=seed,
                       horizon_days=h, time_limit_s=3)
    r = FallbackScheduler().solve(req)
    assert r.schedule is not None, (h, r.status, r.gaps)
print("OK")
"""


def test_short_horizon_does_not_abort_the_process():
    """Regression: OR-Tools 9.15 SIGABRTs (QuickSolveWithHint) on an infeasible hinted model —
    a 0/1/2-day horizon against a 7-day previous plan took the whole server down. Run in a
    child process so an abort fails the test instead of killing pytest."""
    import subprocess
    import sys
    pytest.importorskip("warroom.sim")
    p = subprocess.run([sys.executable, "-c", _SHORT_HORIZON_CHILD], capture_output=True, text=True, timeout=180)
    assert p.returncode == 0 and "OK" in p.stdout, (p.returncode, p.stderr[-800:])


def test_seed_counts_curing_tires_and_has_no_baseline_warnings():
    """Baseline seed: in-progress tires count toward their order (no double counting -> no
    phantom #S-101 delay) and prep done ahead (one blank per press, p.6 principle 5) is not a
    warning; every mold-in has a staffed slot for its prep."""
    _, sim, seed = _sim_seed()
    s101 = next(o for o in sim.orders() if o.id == "#S-101")
    assert sum(1 for j in seed.jobs if j.order_id == "#S-101") == s101.qty - s101.done
    assert not any("既有延遲" in w or "備料" in w for w in seed.warnings), seed.warnings
    from warroom.schedule.backprop import labor_conflicts
    from warroom.schedule.staffing import complete_shifts
    shifts = complete_shifts(sim.shifts(), seed.horizon_start - timedelta(days=1), seed.horizon_end + timedelta(days=5))
    assert labor_conflicts(seed.jobs, shifts, horizon_end=seed.horizon_end) == []


def test_labor_conflicts_prep_ahead_rule():
    from warroom.schedule.backprop import labor_conflicts
    from warroom.schedule.staffing import complete_shifts
    sat = datetime(2026, 10, 3, tzinfo=F.TZ)            # Saturday
    shifts = complete_shifts([], sat - timedelta(days=2), sat + timedelta(days=4))
    mon6 = datetime(2026, 10, 5, 6, 0, tzinfo=F.TZ)
    prev_sat_10 = back_propagate("P1", "#M", TireSize.M, "CURE-08", sat.replace(hour=10), shifts=shifts, frozen=True)
    nxt = back_propagate("N1", "#M", TireSize.M, "CURE-08", mon6, shifts=shifts)
    # previous tire went in Sat 10:00: the Sat day shift can bond the Monday blank ahead
    assert labor_conflicts([prev_sat_10, nxt], shifts) == []
    # previous tire went in Sat 22:00: no bonding shift between then and Mon 06:00
    prev_sat_22 = back_propagate("P2", "#M", TireSize.M, "CURE-08", sat.replace(hour=22), shifts=shifts, frozen=True)
    bad = labor_conflicts([prev_sat_22, nxt], shifts)
    assert any("bonding" in b for b in bad)
    # outside the visible horizon: not counted
    assert labor_conflicts([prev_sat_22, nxt], shifts, horizon_end=mon6) == []


# ---------------------------------------------------------------- review r3: mold change, weekend, large tires
def _mold_gap_errors(sched):
    errs = []
    by: dict[str, list] = {}
    for j in sched.jobs:
        by.setdefault(j.press_id, []).append(j)
    for p, js in by.items():
        js.sort(key=lambda j: j.mold_in)
        for a, b in zip(js, js[1:]):
            if not b.frozen and a.mold_id != b.mold_id and b.mold_in - a.demold < timedelta(hours=F.MOLD_CHANGE_STOP_H):
                errs.append(f"{p} {a.id}->{b.id} {(b.mold_in - a.demold).total_seconds() / 3600:.1f} h")
    return errs


def test_mold_change_stops_press_8h_in_seed_and_page11_reschedules(tmp_path):
    """Page 6 principle 4: each mold change stops the press ~8 h. Consecutive tires with
    different molds on one press are at least MOLD_CHANGE_STOP_H apart — seed, S3 (cure
    extension) and the 02:31 replan (press 7 out)."""
    clock, sim, seed = _sim_seed()
    assert _mold_gap_errors(seed) == []
    store = FileScheduleStore(tmp_path, initial=seed)
    res, ver = reschedule(store, FallbackScheduler(), clock.now(), sim.orders(), sim.shifts(),
                          cure_overrides_h={"CURE-07": 28.5})
    assert res.feasible and ver is not None and _mold_gap_errors(res.schedule) == []
    a311 = next(j for j in res.schedule.jobs if j.order_id == "#A-311")
    assert a311.press_id == "CURE-07"
    store.activate(ver.version)
    res2, _ = reschedule(store, FallbackScheduler(), clock.now() + timedelta(minutes=17), sim.orders(), sim.shifts(),
                         unavailable_presses=["CURE-07"])
    assert res2.feasible and _mold_gap_errors(res2.schedule) == []


@pytest.mark.parametrize("solver", [HeuristicScheduler(), CpSatScheduler()], ids=["heuristic", "cpsat"])
def test_both_solvers_respect_mold_change_stop(solver):
    """Two orders with different molds sharing the small presses: every switch waits 8 h."""
    shifts = complete_shifts([], NOW - timedelta(days=1), NOW + timedelta(days=12))
    orders = [Order(id="#SA", size=TireSize.S, qty=12, done=0, due=NOW.date() + timedelta(days=4), mold_id="MA"),
              Order(id="#SB", size=TireSize.S, qty=12, done=0, due=NOW.date() + timedelta(days=5), mold_id="MB")]
    res = solver.solve(SolveRequest(now=NOW, orders=orders, shifts=shifts, time_limit_s=10))
    assert res.schedule is not None and res.feasible, res.gaps
    assert _mold_gap_errors(res.schedule) == []
    assert not any("換模" in w for w in res.schedule.warnings)


def test_check_schedule_flags_short_mold_change():
    shifts = complete_shifts([], NOW - timedelta(days=1), NOW + timedelta(days=12))
    mon = datetime(2026, 10, 5, 10, 0, tzinfo=F.TZ)
    a = back_propagate("J1", "#SA", TireSize.S, "CURE-01", mon, mold_id="MA", shifts=shifts)
    b = back_propagate("J2", "#SB", TireSize.S, "CURE-01", a.demold + timedelta(hours=1), mold_id="MB", shifts=shifts)
    from warroom.contracts import Schedule
    sched = Schedule(id="X", created_at=NOW, horizon_start=NOW, horizon_end=NOW + timedelta(days=7), jobs=[a, b],
                     solver=SolverName.HEURISTIC)
    errs = check_schedule(sched, SolveRequest(now=NOW, orders=[], shifts=shifts))
    assert any("換模停機不足" in e for e in errs), errs


def test_weekend_no_fire_out_in_baseline():
    """Page 6 principle 3 週末不斷火: every available press is loaded so it cures through Sunday
    (Saturday night sends the last round, it de-molds Monday 清晨 00–05 or later). No press sits
    cold over Sunday; the only idle allowed is the physics bound (a small tire de-molded Saturday
    morning waits for the Saturday-night slot that lands its de-mold on Monday)."""
    clock, sim, seed = _sim_seed()
    sat = clock.now().date()
    assert sat.weekday() == 5
    sun_5 = datetime.combine(sat + timedelta(days=1), datetime.min.time(), tzinfo=F.TZ) + timedelta(hours=5)
    mon_5 = sun_5 + timedelta(days=1)
    for p in F.CURE_PRESS_IDS:
        js = sorted((j for j in seed.jobs if j.press_id == p), key=lambda j: j.mold_in)
        # when the Saturday night shift ends (last staffed op until Monday) every press holds a cure
        assert any(j.mold_in <= sun_5 < j.demold for j in js), f"{p} fired out over the weekend"
        for a, b in zip(js, js[1:]):
            if a.frozen and a.demold >= sun_5:
                continue      # in-progress tire loaded before now, cure ends in the unstaffed Sunday
            if a.demold < mon_5 and b.mold_in > a.demold:
                idle = (b.mold_in - a.demold).total_seconds() / 3600
                # 21 h: a small tire de-molded Sat 05:12 must wait for Sun 00:00 (24 h cure -> Mon 00:00)
                assert idle <= 21, f"{p} idle {idle:.1f} h {a.demold:%a %H:%M} -> {b.mold_in:%a %H:%M}"
    # small presses de-mold in the Monday 00–05 window
    early = [j for j in seed.jobs if j.size is TireSize.S and j.demold.weekday() == 0 and j.demold.hour < 5]
    assert len(early) >= 5


def test_monday_early_window_is_staffed_for_demold():
    from warroom.schedule.staffing import MONDAY_EARLY_NAME, op_windows
    sat = datetime(2026, 10, 3, tzinfo=F.TZ)
    shifts = complete_shifts([], sat, sat + timedelta(days=3))
    mon = [s for s in shifts if s.name == MONDAY_EARLY_NAME]
    assert len(mon) == 1 and mon[0].start.weekday() == 0 and mon[0].start.hour == 0 and mon[0].end.hour == 5
    assert any(w.start <= mon[0].start and mon[0].end <= w.end for w in op_windows(shifts))


def test_large_tires_run_every_week_on_their_presses():
    """Page 6 sim: M, L 2 presses each — the seed generates L work every week (not only M by
    EDD), and the presses curing L now keep curing L."""
    clock, sim, seed = _sim_seed()
    now = clock.now()
    new = [j for j in seed.jobs if not j.frozen and j.mold_in >= now]
    n_l = sum(1 for j in new if j.size is TireSize.L and j.order_id != "#A-311")
    n_m = sum(1 for j in new if j.size is TireSize.M)
    assert n_l >= 6 and n_m >= 6, (n_l, n_m)
    for p in ("CURE-08", "CURE-10"):     # curing #L-301 now
        assert any(j.size is TireSize.L for j in new if j.press_id == p and j.mold_in < now + timedelta(days=7)), p


def test_delivery_claims_scoped_to_this_plan():
    """deliveries_met covers this plan's tires only: the approval text says so, and open orders
    whose rest cannot be cured by their due date at the page-6 rate are flagged in warnings."""
    from warroom.schedule.demo import describe
    clock, sim, seed = _sim_seed()
    store_res = FallbackScheduler().solve(SolveRequest(now=clock.now(), orders=sim.orders(), shifts=sim.shifts(),
                                                       previous=seed, cure_overrides_h={"CURE-07": 36.5}))
    text = describe(store_res)
    assert "交期全數達成" not in text and ("本週排程訂單交期滿足" in text or "延遲" in text)
    outlook = [w for w in store_res.schedule.warnings if w.startswith("本週排程外")]
    assert len(outlook) == 1 and "#L-301" in outlook[0]


# ---------------------------------------------------------------- review r4: slow press under H1
@pytest.mark.parametrize("solver", ["cpsat", "heuristic"])
def test_extended_cure_press_gets_no_tires_from_other_presses(tmp_path, solver):
    """While H1 says CURE-07 runs at 122 °C (S1: 24 h -> 28.5 h), no tire planned on another press
    is moved onto it (round 1 once showed '#M-201 由 CURE-09 改到 CURE-07'); only #A-311 (small) may
    move off it, and only to another small press, never the same-pipe press 6. Tires already planned
    on CURE-07 may stay, and the plan says on what premise."""
    from warroom.schedule.demo import describe
    clock, sim, seed = _sim_seed()
    prev = {j.id: j.press_id for j in seed.jobs}
    sched = CpSatScheduler() if solver == "cpsat" else HeuristicScheduler()
    store = FileScheduleStore(tmp_path / "s", initial=seed)
    res, ver = reschedule(store, sched, clock.now(), sim.orders(), sim.shifts(),
                          cure_overrides_h={"CURE-07": 28.5})
    assert res.feasible and res.deliveries_met and ver is not None
    on7 = [j for j in res.schedule.jobs if j.press_id == "CURE-07" and not j.frozen]
    assert all(prev.get(j.id) == "CURE-07" for j in on7), [(j.id, prev.get(j.id)) for j in on7]
    assert not [m for m in res.moves if m.to_press == "CURE-07" and m.from_press != "CURE-07"]
    a311 = next(m for m in res.moves if m.order_id == "#A-311")
    assert a311.from_press == "CURE-07" and a311.to_press in F.SMALL_ONLY_PRESSES and a311.to_press != "CURE-06"
    assert any(w.startswith("CURE-07 硫化延長中") for w in res.schedule.warnings)
    text = describe(res)
    assert "改到 CURE-07" not in text
    if a311.to_press != "CURE-07":
        assert f"#A-311 由 CURE-07 改到 {a311.to_press}" in text


def test_shortened_or_unchanged_cure_does_not_close_the_press(tmp_path):
    """Only a LONGER cure marks the press slow; an override at the recipe value changes nothing."""
    from warroom.schedule import solver as S
    clock, sim, seed = _sim_seed()
    for h in (24.0, 20.0):                                   # press 7 is a small press: recipe 24 h
        ctx = S._prepare(SolveRequest(now=clock.now(), orders=sim.orders(), shifts=sim.shifts(), previous=seed,
                                      cure_overrides_h={"CURE-07": h}))
        assert not ctx.slow
    ctx = S._prepare(SolveRequest(now=clock.now(), orders=sim.orders(), shifts=sim.shifts(), previous=seed,
                                  cure_overrides_h={"CURE-07": 28.5}))
    assert ctx.slow == {"CURE-07"}
    assert all("CURE-07" not in j.eligible for j in ctx.free if j.orig_press not in (None, "CURE-07"))


def test_describe_names_the_batch_of_a_multi_tire_order():
    from warroom.schedule.demo import _job_label
    assert _job_label("#M-201", "J-M-201-03", 5) == "#M-201 第 3 模"
    assert _job_label("#A-311", "J-A-311-01", 1) == "#A-311"
    assert _job_label("#M-201", "J-CURE-07-10021614", 5) == "#M-201"
