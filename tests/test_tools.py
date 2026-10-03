"""warroom/tools: allow-list registry, arg validation, never-raise, and the real tools against the
real sim / detector / scheduler / store / kb (page-11 numbers)."""
from __future__ import annotations

import pytest

from warroom import factory as F
from warroom.clock import SimClock
from warroom.contracts import (
    WRITE_TOOLS,
    CureFormulaData,
    Prediction,
    QueryOrdersShiftsData,
    ReadSensorsData,
    ScenarioName,
    ScheduleStatus,
    SolveResult,
    ToolCall,
    ToolKind,
    ToolName,
    ToolRegistry,
)
from warroom.tools import TOOL_SPECS, Registry, ToolContext, build_registry


def call(reg, tool, **args):
    return reg.call(ToolCall(id=f"t-{tool}", tool=ToolName(tool), args=args))


@pytest.fixture(scope="module")
def plant(tmp_path_factory):
    from warroom.detect import StreamDetector
    from warroom.schedule import FallbackScheduler, FileScheduleStore, seed_schedule
    from warroom.sim import Factory
    clock = SimClock(manual=True)
    sim = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
    det = StreamDetector(clock=clock)
    for _ in range(140):                      # run to 02:14:20, past the page-11 detection
        det.ingest(sim.step(1.0))
    sched = FallbackScheduler()
    store = FileScheduleStore(tmp_path_factory.mktemp("sched"), initial=seed_schedule(
        clock.now(), sim.orders(), sim.shifts(), sim.machines(), scheduler=sched, next_on={"CURE-07": "#A-311"}))
    ctx = ToolContext(sim=sim, detector=det, scheduler=sched, store=store, clock=clock)
    return ctx, build_registry(ctx)


def test_specs_cover_allow_list_and_no_actuator_tools():
    assert set(TOOL_SPECS) == set(ToolName)
    assert {n for n, s in TOOL_SPECS.items() if s.kind is ToolKind.WRITE} == set(WRITE_TOOLS)
    for s in TOOL_SPECS.values():
        assert s.params_schema.get("properties") is not None
        assert s.allowed_states, s.name
    names = " ".join(n.value for n in ToolName)
    assert "plc" not in names and "actuator" not in names


def test_registry_protocol_and_openai_tools():
    reg = build_registry(ToolContext())
    assert isinstance(reg, ToolRegistry)
    tools = reg.openai_tools([ToolName.CURE_FORMULA, ToolName.SOLVE_SCHEDULE])
    assert [t["function"]["name"] for t in tools] == ["cure_formula", "solve_schedule"]
    assert tools[0]["type"] == "function" and "actual_temp_c" in tools[0]["function"]["parameters"]["properties"]


def test_call_never_raises():
    reg = build_registry(ToolContext())
    bad = call(reg, "cure_formula", size="M")                 # missing args
    assert not bad.ok and "invalid args" in bad.error
    no_sim = call(reg, "read_sensors", machine_ids=["CURE-07"])
    assert not no_sim.ok and "unavailable" in no_sim.error
    empty = Registry(ToolContext())
    assert not call(empty, "cure_formula", size="M", actual_temp_c=122, elapsed_h=10).ok


def test_cure_formula_page11():
    reg = build_registry(ToolContext())
    r = call(reg, "cure_formula", size="M", ref_temp_c=128, actual_temp_c=122, elapsed_h=10)
    assert r.ok
    d = CureFormulaData.model_validate(r.data)
    assert round(d.total_cure_h, 1) == 36.5 and round(d.extra_h, 1) == 8.5


def test_read_sensors_and_predict(plant):
    _, reg = plant
    r = call(reg, "read_sensors", machine_ids=["CURE-07", "CURE-06", "CURE-08"], minutes=30)
    assert r.ok, r.error
    d = ReadSensorsData.model_validate(r.data)
    assert len(d.readings["CURE-07"]) <= 181
    assert d.summary["CURE-07"]["temp_c_now"] < 125 < d.summary["CURE-06"]["temp_c_now"]
    assert "steam_kgf_cm2_now" in d.summary["CURE-07"]
    assert not call(reg, "read_sensors", machine_ids=["CURE-99"]).ok
    p = call(reg, "predict_anomaly", machine_id="CURE-07", horizon_min=30)
    assert p.ok, p.error
    pred = Prediction.model_validate(p.data)
    assert 119 < pred.predicted_value < 125


def test_query_orders_shifts_from_plant(plant):
    ctx, reg = plant
    r = call(reg, "query_orders_shifts", press_ids=["CURE-07"])
    assert r.ok, r.error
    extra = r.data.pop("gap_details")
    d = QueryOrdersShiftsData.model_validate(r.data)
    assert d.next_jobs[0].order_id == "#A-311"
    assert "10/03 夜班缺 1 人" in d.gaps and extra[0]["short"] == 1
    a311 = next(o for o in d.orders if o.id == "#A-311")
    assert (a311.due - ctx.clock.now().date()).days == 3


def test_solve_confirm_page11(plant):
    ctx, reg = plant
    before = ctx.store.current().version
    r = call(reg, "solve_schedule", cure_overrides_h={"CURE-07": 36.5}, time_limit_s=10)
    assert r.ok, r.error
    v = r.data.pop("schedule_version")
    res = SolveResult.model_validate(r.data)
    assert res.feasible and res.deliveries_met
    real_moves = [(m.order_id, m.to_press) for m in res.moves if m.from_press != m.to_press]
    assert ("#A-311", "CURE-09") in real_moves
    assert {o.label for o in res.staffing_options} == {"夜班加班", "從 2 線調 1 人"}
    assert ctx.store.get(v).status is ScheduleStatus.PROPOSED and ctx.store.current().version == before
    assert not call(reg, "solve_schedule", unavailable_presses=["A-MIX"]).ok      # not a press
    c = call(reg, "confirm_schedule", schedule_version=v, reason="test")
    assert c.ok and ctx.store.current().version == v and c.data["previous_version"] == before
    again = call(reg, "confirm_schedule", schedule_version=v, reason="twice")
    assert not again.ok and "not proposed" in again.error


def test_write_tools_record_intent(plant):
    ctx, reg = plant
    s = call(reg, "set_cure_time", press_id="CURE-07", total_cure_h=36.5, reason="Arrhenius")
    assert s.ok and s.data["recorded"] is True
    assert ctx.sim.machine("CURE-07").cure_job.cure_required_h == 36.5
    assert not call(reg, "set_cure_time", press_id="A-MIX", total_cure_h=10, reason="x").ok
    n = call(reg, "notify_teams", teams=["maintenance"], kind="maintenance", message="檢修冷卻水路",
             machine_id="A-BIGOPEN")
    assert n.ok and n.data["delivered"] == ["maintenance"] and n.data["team_labels"] == [F.TEAM_LABELS[
        __import__("warroom.contracts", fromlist=["TeamId"]).TeamId.MAINTENANCE]]
    assert not call(reg, "notify_teams", teams=["maintenance"], kind="plc_write", message="x").ok


def test_sop_search_real_kb(tmp_path):
    from warroom.kb import QdrantSopKB
    kb = QdrantSopKB(path=":memory:")
    kb.ingest()
    reg = build_registry(ToolContext(kb=kb))
    r = call(reg, "sop_search", query="硫化中斷處置", k=3)
    assert r.ok, r.error
    assert r.data["hits"][0]["title"] == "硫化中斷處置"
