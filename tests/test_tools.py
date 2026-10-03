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


def test_confirm_rejects_stale_proposal(tmp_path):
    """Two events solve against the same active version; confirming the second after the first must
    not overwrite the first one's approved change (lost update)."""
    from warroom.schedule import FallbackScheduler, FileScheduleStore, seed_schedule
    from warroom.sim import Factory
    clock = SimClock(manual=True)
    sim = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
    sched = FallbackScheduler()
    store = FileScheduleStore(tmp_path, initial=seed_schedule(clock.now(), sim.orders(), sim.shifts(),
                                                              sim.machines(), scheduler=sched))
    reg = build_registry(ToolContext(sim=sim, scheduler=sched, store=store, clock=clock))
    base = store.current().version
    a = call(reg, "solve_schedule", cure_overrides_h={"CURE-10": 40.0}, time_limit_s=5)
    b = call(reg, "solve_schedule", cure_overrides_h={"CURE-03": 30.0}, time_limit_s=5)
    assert a.ok and b.ok, (a.error, b.error)
    va, vb = a.data["schedule_version"], b.data["schedule_version"]
    assert store.get(va).parent_version == store.get(vb).parent_version == base
    assert call(reg, "confirm_schedule", schedule_version=va, reason="first").ok
    stale = call(reg, "confirm_schedule", schedule_version=vb, reason="second")
    assert not stale.ok and "stale" in stale.error
    assert store.current().version == va                     # first event's change survives
    assert store.get(vb).status is ScheduleStatus.PROPOSED
    fresh = call(reg, "solve_schedule", cure_overrides_h={"CURE-03": 30.0}, time_limit_s=5)
    assert fresh.ok and store.get(fresh.data["schedule_version"]).parent_version == va
    assert call(reg, "confirm_schedule", schedule_version=fresh.data["schedule_version"], reason="rebased").ok


# ---------------------------------------------------------------- loop never blocked by a tool
async def test_acall_does_not_block_event_loop():
    """A slow read tool (cold KB) awaited via acall must leave the loop free (red path < 0.2 s)."""
    import asyncio
    import time

    from warroom.contracts import SopHit

    class SlowKB:
        def search(self, query, k=3):
            time.sleep(0.8)
            return [SopHit(sop_id="SOP-X", title="t", source="x.md", text="x", score=1.0)]

        def ingest(self):
            return 0

    reg = build_registry(ToolContext(kb=SlowKB()))
    gaps: list[float] = []
    stop = False

    async def ticker():
        last = time.perf_counter()
        while not stop:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    t = asyncio.create_task(ticker())
    res = await reg.acall(ToolCall(id="slow", tool=ToolName.SOP_SEARCH, args={"query": "硫化中斷"}))
    stop = True
    await t
    assert res.ok, res.error
    assert res.duration_ms >= 700
    assert max(gaps) < 0.2, f"loop blocked {max(gaps):.3f} s"


async def test_acall_timeout_and_errors_never_raise():
    import time

    from warroom.contracts import ToolSpec
    reg = build_registry(ToolContext())
    spec = TOOL_SPECS[ToolName.SOP_SEARCH].model_copy(update={"timeout_s": 0.2})
    assert isinstance(spec, ToolSpec)
    reg.register(spec, lambda ctx, args: time.sleep(1.0) or {})
    res = await reg.acall(ToolCall(id="to", tool=ToolName.SOP_SEARCH, args={"query": "x"}))
    assert not res.ok and "timeout" in (res.error or "")
    bad = await reg.acall(ToolCall(id="bad", tool=ToolName.CURE_FORMULA, args={"nope": 1}))
    assert not bad.ok and bad.error


async def test_runtime_tick_gap_stays_under_red_path_target_with_cold_kb(tmp_path):
    """Page 9: safety path < 0.2 s. A real press-7 event hitting a slow (cold) SOP search must not
    freeze the Runtime tick, so safety_pass keeps running while the event task waits on the KB."""
    import asyncio
    import time

    from warroom.api.runtime import Runtime
    from warroom.config import Settings

    rt = Runtime(Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                          qdrant_path=tmp_path / "qdrant", scenario="press7_temp_drop", llm_mode="mock"),
                 run_loop=False)
    if "tools" in rt.degraded or "state_machine" in rt.degraded:
        pytest.skip(f"degraded: {rt.degraded}")
    from warroom.contracts import SopHit
    hits: list[float] = []

    class SlowKB:
        def search(self, query, k=3):
            t0 = time.perf_counter()
            time.sleep(1.0)
            hits.append(time.perf_counter() - t0)
            # no real embedder here: loading fastembed/ONNX in a thread holds the GIL ~0.4 s, which
            # is a separate (boot-time) issue; this test pins the await-blocking bug only
            return [SopHit(sop_id="SOP-CI-03", title="硫化中斷處置", source="x.md", text="停用", score=0.9)]

        def ingest(self):
            return 0

    rt.c.tools.ctx.kb = SlowKB()
    max_gap = 0.0
    last = time.perf_counter()
    for _ in range(400):                       # past the 02:14:00 detection + commander steps
        await rt.tick(1.0, publish=False, hold_compute=False)
        await asyncio.sleep(0.005)
        now = time.perf_counter()
        max_gap = max(max_gap, now - last)
        last = now
    for task in list(rt._tasks.values()):
        task.cancel()
    assert hits, "event never reached sop_search"
    assert max_gap < 0.2, f"tick loop blocked {max_gap:.3f} s while sop_search ran"
