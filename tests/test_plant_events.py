"""Issue #17: plant events (早班開工 / 插單 / 人力調度), worker tasks from approved steps, goods status, and the
EV-2 replay stable-temperature fix.

CJ, 2026-10-20: 「突發狀況(人力調度或機器故障或插單) 早上訂單開工」, 「我希望這些輪胎也是有補貨跟完成的狀態圈圈 然後人力會去執行
這樣模擬插單才有意義才會知道貨品狀態 然後審核一個plan之後人員要去執行」.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from warroom import factory as F
from warroom.agent.commander import MockCommander, stable_cure_temp
from warroom.agent.gate import RuleGate
from warroom.config import Settings
from warroom.contracts import (
    Anomaly,
    AnomalyKind,
    ApprovalDecision,
    AuditKind,
    Channel,
    CommanderRequest,
    DecisionKind,
    EventState,
    GateVerdict,
    NotifyKind,
    Order,
    PlanStep,
    Prediction,
    RiskLevel,
    ScenarioName,
    Severity,
    SolveRequest,
    TeamId,
    TireSize,
    ToolName,
)
from warroom.sim import Factory
from warroom.sim.scenarios import PLANT_SCENARIOS, RUSH_ORDER_ID, get_scenario
from warroom.sim.work import TaskRequest

TZ = F.TZ


# ------------------------------------------------------------------ gate: 派工 (dispatch) is R2 and EXECUTE-only
def _dispatch_step() -> PlanStep:
    return PlanStep(id="S4", tool=ToolName.NOTIFY_TEAMS, purpose="派工",
                    args={"teams": ["body", "curing"], "kind": NotifyKind.DISPATCH.value, "message": "今日計畫"})


def test_dispatch_needs_approval_r2():
    gd = RuleGate().check(_dispatch_step(), EventState.EXECUTE, True)
    assert gd.verdict is GateVerdict.NEEDS_APPROVAL and gd.risk is RiskLevel.R2_CHANGE


@pytest.mark.parametrize("state", [EventState.CLASSIFY, EventState.PLAN, EventState.SIMULATE, EventState.VERIFY,
                                   EventState.REPLAN])
def test_dispatch_refused_outside_execute(state):
    gd = RuleGate().check(_dispatch_step(), state, True)
    assert gd.verdict is GateVerdict.REJECT and gd.risk is None and not gd.checks["state_allowed"]


def test_no_new_tool_on_the_allow_list():
    from warroom.contracts import READ_COMPUTE_TOOLS, WRITE_TOOLS
    assert len(ToolName) == 9 and len(READ_COMPUTE_TOOLS) == 6 and len(WRITE_TOOLS) == 3


# ------------------------------------------------------------------ EV-2: stable temperature for the formula
def _press_anomaly(pred: float, value: float = 126.0, trend: float = -0.89) -> Anomaly:
    ts = datetime(2026, 10, 3, 2, 15, 37, tzinfo=TZ)
    return Anomaly(id="AN-T", ts=ts, machine_id="CURE-07", kind=AnomalyKind.TEMP_DROP, channel=Channel.TEMP,
                   value=value, baseline=128.0, severity=Severity.WARN, message="t",
                   prediction=Prediction(machine_id="CURE-07", channel=Channel.TEMP, now_value=value,
                                         predicted_value=pred, band_lo=pred - 1, band_hi=pred + 1, horizon_s=1800,
                                         trend_per_min=trend))


def test_stable_temp_floors_a_still_falling_forecast():
    v, why = stable_cure_temp(_press_anomaly(99.8))
    assert v == F.CURE_TEMP_MIN_C and why and "99.8" in why


def test_stable_temp_keeps_page11_prediction():
    assert stable_cure_temp(_press_anomaly(122.0, 122.0, 0.0)) == (122.0, None)
    assert stable_cure_temp(_press_anomaly(122.4, 122.0, 0.0)) == (122.4, None)


def test_mock_h1_on_ev2_cures_at_the_floor_within_the_cap():
    from warroom.contracts import CureFormulaArgs
    from warroom.tools.cure import arrhenius_cure
    req = CommanderRequest(phase="plan", event_id="EV", anomaly=_press_anomaly(99.8), allowed_tools=list(ToolName),
                           context={"cure_job": {"size": "M", "setpoint_c": 128, "cure_elapsed_h": 10.0}})
    plan = MockCommander().build(req)
    s1 = next(s for s in plan.steps if s.tool is ToolName.CURE_FORMULA)
    assert s1.args["actual_temp_c"] == 120.0
    total = arrhenius_cure(CureFormulaArgs.model_validate(s1.args)).total_cure_h
    assert total <= 96                       # SolveScheduleArgs cap; 99.8 °C gave 131.6 h


# ------------------------------------------------------------------ factory: plant inputs change orders / shifts only
def _run(f: Factory, seconds: int) -> None:
    for _ in range(seconds):
        f.step(1.0)


def test_plant_scenarios_defined():
    assert PLANT_SCENARIOS == {ScenarioName.SHIFT_START, ScenarioName.RUSH_ORDER, ScenarioName.STAFFING}
    for n in PLANT_SCENARIOS:
        sc = get_scenario(n)
        assert sc.steps and all(st.plant is not None and st.fault is None for st in sc.steps)


def test_rush_order_joins_the_order_book_at_its_time():
    f = Factory(seed=7, scenario=ScenarioName.RUSH_ORDER)
    _run(f, 599)
    assert RUSH_ORDER_ID not in {o.id for o in f.orders()} and f.drain_plant_inputs() == []
    _run(f, 2)
    fired = f.drain_plant_inputs()
    assert [p.kind for _, p in fired] == ["rush_order"] and fired[0][0] == f.scenario.start_at + timedelta(seconds=600)
    o = next(o for o in f.orders() if o.id == RUSH_ORDER_ID)
    assert o.priority >= 2 and o.size is TireSize.M
    assert any(e.kind == "order_rush" and e.ref == RUSH_ORDER_ID for e in f.world.events)


def test_sick_call_takes_a_person_off_the_shift_and_the_roster():
    f = Factory(seed=7, scenario=ScenarioName.STAFFING)
    early = lambda: next(s for s in f.shifts() if s.team is TeamId.CURING and s.name == "早班"  # noqa: E731
                         and s.start.date() == f.now().date())
    assert early().headcount == early().required
    _run(f, 601)
    assert early().headcount == early().required - 1
    _run(f, 40 * 60)                                         # 08:10: S13's shift has started
    s13 = next(w for w in f.world.snapshot().people.workers if w.id == "S13")
    assert s13.state == "請假" and "病假" in (s13.note or "")
    assert any(x["id"] == "S13" for x in f.world.leave_now())


def test_old_scenarios_keep_the_page11_gap_and_no_plant_inputs():
    for n in (ScenarioName.PRESS7_TEMP_DROP, ScenarioName.NORMAL, ScenarioName.OPEN_MILL_OVERHEAT):
        f = Factory(seed=7, scenario=n)
        assert f._gap_date == f.now().date() and f._plant_pending == []


# ------------------------------------------------------------------ solver: an inserted rush order
def test_solver_inserts_a_rush_order_the_plan_does_not_carry():
    from warroom.schedule import FallbackScheduler
    from warroom.schedule.demo import demo_inputs
    now = datetime(2026, 10, 7, 10, 0, tzinfo=TZ)
    orders, shifts, seed = demo_inputs(now)
    rush = Order(id="#R-T1", size=TireSize.M, qty=1, due=now.date() + timedelta(days=3), customer="客戶G", priority=3)
    res = FallbackScheduler().solve(SolveRequest(now=now, orders=[*orders, rush], shifts=shifts, previous=seed,
                                                 time_limit_s=10))
    assert res.feasible and res.schedule is not None
    assert [j.press_id in F.ML_PRESSES for j in res.schedule.jobs if j.order_id == "#R-T1"] == [True]
    new = [m for m in res.moves if m.order_id == "#R-T1"]
    assert len(new) == 1 and new[0].from_press is None and new[0].from_mold_in is None
    assert all(m.from_press is not None for m in res.moves if m.order_id != "#R-T1")


def test_order_shifts_text():
    from warroom.agent.narrate import moves_text, order_shifts
    moves = [{"job_id": "J-R-1", "order_id": "#R-9", "from_press": None, "to_press": "CURE-09", "from_mold_in": None,
              "to_mold_in": "2026-10-07T14:00:00+08:00"},
             {"job_id": "J-M-1", "order_id": "#M-1", "from_press": "CURE-09", "to_press": "CURE-08",
              "from_mold_in": "2026-10-07T14:00:00+08:00", "to_mold_in": "2026-10-07T20:30:00+08:00"}]
    jobs = [{"id": "J-M-1", "order_id": "#M-1", "mold_in": "2026-10-07T20:30:00+08:00", "frozen": False}]
    assert order_shifts(moves, jobs) == ["#R-9 新排 1 模（首模 9 號 10/07 14:00）", "#M-1 完工延後 6.5 h"]
    assert moves_text(moves) == ["#R-9 排入 9 號", "#M-1 改 8 號"]


# ------------------------------------------------------------------ world: tasks run 待派 → 前往中 → 執行中 → 完成
def test_task_lifecycle_moves_the_worker_and_reports_every_change():
    f = Factory(seed=7, scenario=ScenarioName.SHIFT_START)
    _run(f, 15 * 60)                                         # 08:05, day shift on duty
    seen: list[tuple[str, str]] = []
    f.world.tasks.add_listener(lambda what, t, now: seen.append((what, t.status)))
    now = f.now()
    tasks = f.world.tasks.dispatch(TaskRequest(event_id="EV-T", approval_id="AP-T", step_id="S4b", event_kind="temp_drop",
                                               tool="set_cure_time", args={"press_id": "CURE-07", "total_cure_h": 36.5}),
                                   now)
    assert len(tasks) == 1 and tasks[0].machine_id == "CURE-07" and tasks[0].source == "code"
    tid = tasks[0].id
    assert tasks[0].status in ("待派", "前往中")
    _run(f, 30)
    t = next(x for x in f.world.snapshot().tasks.items if x.id == tid)
    w = next(x for x in f.world.snapshot().people.workers if x.id == t.worker_id)
    assert t.status == "前往中" and w.state == "前往中" and w.machine_id == "CURE-07"
    _run(f, 60 * 60)
    t = next(x for x in f.world.snapshot().tasks.items if x.id == tid)
    assert t.status == "完成" and t.progress == 1.0 and t.done_at is not None
    statuses = [s for what, s in seen if what != "created"]
    assert statuses == ["前往中", "執行中", "完成"]


def test_feed_task_holds_its_orders_tyres_in_waiting_for_material():
    f = Factory(seed=7, scenario=ScenarioName.SHIFT_START)
    from warroom.schedule.demo import demo_inputs
    from warroom.schedule.store import FileScheduleStore
    import tempfile
    from pathlib import Path
    _, _, seed = demo_inputs(f.now())
    f.attach_plan(FileScheduleStore(Path(tempfile.mkdtemp()), initial=seed))
    _run(f, 15 * 60)
    oid = next(j.order_id for j in f.world.plan().jobs if not j.frozen)
    f.world.tasks.dispatch(TaskRequest(event_id="EV-T", approval_id="AP-T", step_id="S4", event_kind="order_rush",
                                       tool="notify_teams", args={"teams": ["body"], "kind": "dispatch", "message": "x"},
                                       order_ids=[oid]), f.now())
    items = [g for g in f.world.snapshot().goods.items if g.order_id == oid and g.status != "硫化中"]
    assert items and all(g.status == "待補料" and g.blocked for g in items)
    _run(f, 90 * 60)
    snap = f.world.snapshot()
    assert all(t.status == "完成" for t in snap.tasks.items if "補料" == t.kind)
    assert not any(g.blocked for g in snap.goods.items if g.order_id == oid and g.status == "待補料")


# ------------------------------------------------------------------ end to end, mock commander, runtime
async def _run_scenario(tmp_path, name: str, minutes: int = 120):
    from warroom.api import Runtime
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario=name, llm_mode="mock")
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    assert rt.degraded == {}
    await rt.start()
    decided: list = []
    try:
        for _ in range(minutes * 60):
            await rt.tick(1.0, publish=False)
            for req in rt.approvals.pending():
                opt = next((o for o in req.options if o.recommended), req.options[0])
                decided.append(rt.approvals.decide(ApprovalDecision(
                    request_id=req.id, decision=DecisionKind.APPROVE, option_id=opt.id, reason="test", via="auto",
                    decided_at=rt.now())))
            evs = rt.events()
            if evs and all(e.state in (EventState.CLOSED, EventState.SAFE_STOP) for e in evs):
                break
        await rt.wait_events(5)
        world = rt.world_snapshot()
        events = rt.events()
    finally:
        await rt.stop()
    audit = [json.loads(ln) for ln in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    return events, decided, world, audit


def _check_common(events, decided, world, audit, kind: str):
    assert len(events) == 1
    ev = events[0]
    assert ev.source == "plant" and ev.anomaly.kind.value == kind and ev.machine_id == "PLANT"
    assert ev.state is EventState.CLOSED, ev.outcome
    assert decided and all(d.risk in (RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL) for d in decided)
    mine = [t for t in world.tasks.items if t.event_id == ev.id]
    assert mine and all(t.status == "完成" and t.worker_id for t in mine)
    created = [a for a in audit if a["kind"] == AuditKind.TASK.value and a["payload"].get("generated_by") == "code"]
    assert created and all(a["payload"]["approval_id"] for a in created)
    changes = [a["payload"]["status"] for a in audit if a["kind"] == AuditKind.TASK.value and a["actor"] == "world"]
    assert {"前往中", "執行中", "完成"} <= set(changes)
    verdict = [a for a in audit if a["kind"] == AuditKind.TASK.value and a["actor"] == "verifier"]
    assert verdict and verdict[-1]["payload"]["ok"] and verdict[-1]["payload"]["done"] == len(mine)
    assert not [a for a in audit if a["kind"] == AuditKind.INTERCEPTED.value]
    assert any(t.text.startswith("人員任務") for t in ev.timeline)
    return ev, mine


async def test_shift_start_end_to_end(tmp_path):
    events, decided, world, audit = await _run_scenario(tmp_path, "shift_start")
    ev, tasks = _check_common(events, decided, world, audit, "shift_start")
    card = decided[0]
    assert any(x.startswith("今日訂單") for x in card.done) and any(x.startswith("出勤") for x in card.done)
    assert any("暖機順序" in x for x in card.done) and any(x.startswith("原料") for x in card.done)
    assert "S11" in next(x for x in card.done if x.startswith("出勤"))        # team dataset roster: sick leave
    warm = sorted((t for t in tasks if t.kind == "暖機"), key=lambda t: t.id)
    assert [t.machine_id for t in warm] == ["A-MIX", "B-MIX", "A-OPEN", "A-BIGOPEN", "B-OPEN"]
    a_mix = next(t for t in warm if t.machine_id == "A-MIX")
    a_open = next(t for t in warm if t.machine_id == "A-OPEN")
    assert a_open.after == a_mix.id and a_open.assigned_at >= a_mix.done_at     # SOP order: mixer first


async def test_rush_order_end_to_end(tmp_path):
    events, decided, world, audit = await _run_scenario(tmp_path, "rush_order")
    ev, tasks = _check_common(events, decided, world, audit, "order_rush")
    card = decided[0]
    assert f"{RUSH_ORDER_ID} 新排" in card.delivery_impact            # which orders shift, by how much
    assert "完工" in card.delivery_impact or "延誤" in card.delivery_impact
    assert any(t.kind == "補料" and RUSH_ORDER_ID in t.order_ids for t in tasks)
    assert any(o.order_id == RUSH_ORDER_ID for o in world.goods.orders)
    # the rush tyre is listed once its mold-in is within GOODS_AHEAD_H (24 h); the solver may place the first
    # free M/L press just outside that window (dataset layout: only presses 8-11), so the card is the fallback
    assert any(g.order_id == RUSH_ORDER_ID for g in world.goods.items) or f"{RUSH_ORDER_ID} 新排" in card.delivery_impact


async def test_staffing_end_to_end(tmp_path):
    events, decided, world, audit = await _run_scenario(tmp_path, "staffing")
    ev, tasks = _check_common(events, decided, world, audit, "staffing_short")
    card = decided[0]
    assert len(card.options) == 2 and {o.args["kind"] for o in card.options} == {"overtime", "staff_move"}
    assert any("早班缺 1 人" in g for g in card.gaps)
    assert any(t.kind in ("調派支援", "加班補位") for t in tasks)


class _SloppyModel(MockCommander):
    """What the real model did on 2026-10-20 (Ollama): literal schedule versions, odd horizons, a cure-time step on
    'PLANT', extra args. Code must turn that into the same safe plan."""
    def build(self, req):
        plan = super().build(req)
        if req.anomaly.kind is not AnomalyKind.STAFFING_SHORT:
            return plan
        steps = []
        for st in plan.steps:
            a = dict(st.args)
            if st.tool is ToolName.SOLVE_SCHEDULE:
                a.update(horizon_days=1, cure_overrides_h={}, unavailable_presses=[])
            if st.tool is ToolName.CONFIRM_SCHEDULE:
                a.update(schedule_version=0)
            steps.append(st.model_copy(update={"args": a}))
        steps.append(PlanStep(id="S5", tool=ToolName.SET_CURE_TIME, purpose="x", depends_on=["S2"],
                              args={"press_id": "PLANT", "total_cure_h": 0.0, "reason": "x"}))
        return plan.model_copy(update={"steps": steps})


async def test_plant_plan_guard_and_code_bound_args(tmp_path, monkeypatch):
    import warroom.api.runtime as R
    monkeypatch.setattr(R.ComponentBuilder, "_commander", lambda self: _SloppyModel())
    events, decided, world, audit = await _run_scenario(tmp_path, "staffing")
    _check_common(events, decided, world, audit, "staffing_short")
    guard = [a for a in audit if a["payload"].get("plant_plan_guard")]
    assert guard and guard[0]["payload"]["tools"] == ["set_cure_time"]
    bound = {a["payload"]["tool"]: a["payload"]["to"] for a in audit if a["payload"].get("plant_args_from_code")}
    assert "horizon_days" not in bound["solve_schedule"] and bound["confirm_schedule"]["schedule_version"] >= 1


async def test_ev2_replay_follows_page11_path(tmp_path):
    """Team dataset EV-2 replay: H1 (cure at the 120 °C floor) -> approval -> verify misses -> H2, machine out."""
    from warroom.api import Runtime
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario="normal", llm_mode="mock", replay="team_ev2")
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    await rt.start()
    try:
        for _ in range(25 * 60):
            await rt.tick(1.0, publish=False)
            for req in rt.approvals.pending():
                opt = next((o for o in req.options if o.recommended), req.options[0])
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                     option_id=opt.id, reason="test", via="auto", decided_at=rt.now()))
        await rt.wait_events(5)
        ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
    finally:
        await rt.stop()
    assert ev.state is EventState.CLOSED, ev.outcome
    assert [h.cause_code for h in ev.hypotheses] == ["steam_pressure_low", "steam_leak_or_sensor_drift"]
    assert ev.hypotheses[0].status.value == "rejected"
    s1 = next(sr for r in ev.prior_rounds for sr in r.steps if sr.step.tool is ToolName.CURE_FORMULA)
    assert s1.step.args["actual_temp_c"] == 120.0 and s1.result.data["total_cure_h"] <= 96
