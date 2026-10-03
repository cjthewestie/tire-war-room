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
def test_page11_reschedule_moves_A311_to_press9(tmp_path):
    orders, shifts, seed = demo_inputs(NOW)
    a311 = next(j for j in seed.jobs if j.order_id == "#A-311")
    assert a311.press_id == "CURE-07" and not a311.frozen      # next mold on press 7
    store = FileScheduleStore(tmp_path / "s", initial=seed)
    # S1 says 36.5 h total for CURE-07 -> S3 solve
    res, ver = reschedule(store, FallbackScheduler(), NOW, orders, shifts, cure_overrides_h={"CURE-07": 36.5},
                          event_id="EV-20261003-021400-CURE-07")
    assert res.solver is SolverName.CP_SAT and not res.fell_back
    assert res.feasible and res.deliveries_met and res.late_orders == []
    mv = next(m for m in res.moves if m.order_id == "#A-311")
    assert (mv.from_press, mv.to_press) == ("CURE-07", "CURE-09")
    assert [m.order_id for m in res.moves if m.from_press != m.to_press] == ["#A-311"]   # only #A-311 changes press
    assert res.gaps == ["10/03 夜班缺 1 人"]
    assert [o.label for o in res.staffing_options] == ["夜班加班", "從 2 線調 1 人"]
    assert [o.kind for o in res.staffing_options] == ["overtime", "transfer"]
    assert res.schedule.staffing_changes[0].label == "從 2 線調 1 人"        # solver's pick (page 11 02:14:25)
    assert ver is not None and ver.status is ScheduleStatus.PROPOSED and "CURE-09" in ver.reason
    cure07 = next(j for j in res.schedule.jobs if j.press_id == "CURE-07" and j.frozen)
    assert cure07.cure_h == pytest.approx(36.5)
    assert res.solve_ms < 10_000
    store.activate(ver.version)
    # 02:31 H1 rejected -> CURE-07 out of service; supervisor rejects the cross-team move
    res2, ver2 = reschedule(store, FallbackScheduler(), VERIFY, orders, shifts, unavailable_presses=["CURE-07"],
                            cure_overrides_h={"CURE-07": 36.5}, allow_cross_team=False)
    assert res2.feasible and res2.deliveries_met
    assert not any(j.press_id == "CURE-07" and not j.frozen for j in res2.schedule.jobs)
    assert next(j for j in res2.schedule.jobs if j.order_id == "#A-311").press_id == "CURE-09"
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
                          cure_overrides_h={"CURE-07": 36.5})
    assert res.feasible and res.deliveries_met and ver is not None
    a311 = next(m for m in res.moves if m.order_id == "#A-311")
    assert (a311.from_press, a311.to_press) == ("CURE-07", "CURE-09")      # not CURE-08: same steam pipe
    assert "10/03 夜班缺 1 人" in res.gaps
    assert {o.label for o in res.staffing_options} >= {"夜班加班", "從 2 線調 1 人"}
    assert res.solve_ms < 10_000
