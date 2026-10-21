"""The whole-plant world (issue #11): orders over time, materials, dock, people, energy, quality, maintenance.
It must read the Factory and never change it: machine physics, cure cycles and the Factory RNG stay exactly
as they were, so page 11 and every other scenario play out the same."""
from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from warroom.api.app import create_app
from warroom.api.runtime import Runtime
from warroom.config import Settings
from warroom.contracts import MachineStatus, ScenarioName, TeamId, WsType
from warroom.data import team_dataset
from warroom.data.team_ids import MACHINE_ID, WORK_GROUP_MACHINES, machine_id
from warroom.sim import Factory
from warroom.sim.world import MATERIALS, ORDER_STAGES, WORLD_SECTIONS, ZONE_LABELS, World


def _hash_run(f: Factory, steps: int) -> str:
    h = hashlib.sha256()
    for _ in range(steps):
        for r in f.step(1):
            h.update(r.model_dump_json().encode())
    for m in f.machines():
        h.update(m.model_dump_json().encode())
    h.update(repr(f._rng.getstate()).encode())
    return h.hexdigest()


@pytest.mark.parametrize("scenario", list(ScenarioName))
def test_world_does_not_change_the_plant(scenario, monkeypatch):
    """Same readings, machine states and Factory RNG state with the world running and with it switched off."""
    with_world = _hash_run(Factory(scenario=scenario), 1500)
    monkeypatch.setattr(World, "advance", lambda self, now: None)
    without = _hash_run(Factory(scenario=scenario), 1500)
    assert with_world == without


def test_world_is_deterministic():
    """Same seed and same call sequence -> the same world (like the Factory itself)."""
    a, b = Factory(scenario=ScenarioName.NORMAL), Factory(scenario=ScenarioName.NORMAL)
    for f in (a, b):
        for _ in range(6 * 60):
            f.step(60)
    assert a.world.snapshot().model_dump() == b.world.snapshot().model_dump()
    c = Factory(scenario=ScenarioName.NORMAL, seed=8)
    for _ in range(6 * 60):
        c.step(60)
    assert c.world.snapshot().model_dump() != a.world.snapshot().model_dump()


def test_snapshot_has_every_domain():
    f = Factory(scenario=ScenarioName.NORMAL)
    f.step(600)
    s = f.world.snapshot()
    assert set(WORLD_SECTIONS) <= set(s.model_dump())
    assert set(s.orders.pipeline) == set(ORDER_STAGES)
    assert all(s.orders.pipeline[k] >= 1 for k in ORDER_STAGES[:2] + ORDER_STAGES[3:])   # seeded history
    assert {m.key for m in s.materials} == {m.key for m in MATERIALS}
    assert all(0 <= m.stock <= m.capacity and m.reorder_point < m.capacity for m in s.materials)
    assert s.people.total >= 20 and s.people.on_duty > 0
    assert all(w.zone in ZONE_LABELS for w in s.people.workers)
    assert s.energy.total_kw > s.energy.base_kw and set(s.energy.by_line) == {"A", "B", "C", "D", "BOND", "CURE"}
    assert s.maintenance.upcoming, "planned maintenance windows ahead"
    # the seed order book is the Factory's own (scheduler / agent), unchanged ids and quantities
    seed = {o.id: (o.qty, o.done) for o in s.orders.items if o.source == "seed"}
    assert seed == {o.id: (o.qty, o.done) for o in f.orders()}


def test_three_days_of_plant_life():
    """Orders arrive, materials run down and get reordered and received, trucks ship finished orders."""
    f = Factory(scenario=ScenarioName.NORMAL)
    seen: set[str] = set()
    f.world.add_listener(lambda ev: seen.add(ev.kind))
    stock0 = {m.key: m.stock for m in f.world.snapshot().materials}
    for _ in range(72):
        f.step(3600)
    s = f.world.snapshot()
    assert {"order_new", "po_created", "truck_arrive", "shipment"} <= seen, seen
    assert seen & {"delivery", "delivery_short"}
    assert any(o.status == "已出貨" for o in s.orders.items)
    assert any(o.source == "world" and o.created_at and o.created_at > f.world.t0 for o in s.orders.items)
    assert any(m.stock < stock0[m.key] for m in s.materials)
    assert any(p.status == "已收貨" for p in s.purchase_orders)
    assert {e.direction for e in s.dock.log} == {"inbound", "outbound"}
    assert s.quality.recent and all(i.result in ("合格", "不良") for i in s.quality.recent)


def test_page11_night_shift_from_team_dataset():
    """Page 11 (10/03 02:12) plays the dataset's EV-2 night (10/28 02:14): 貼合組 S11 is on sick leave per the team
    roster, the rest of that night shift is at work, the day shift is off, the night supervisor is in the war room."""
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
    s = f.world.snapshot()
    by_id = {w.id: w for w in s.people.workers}
    assert by_id["S11"].state == "請假" and by_id["S11"].source == "team_dataset" and "病假" in (by_id["S11"].note or "")
    assert by_id["S12"].state not in ("下班", "請假") and by_id["S12"].team_label == "貼合組"
    assert all(by_id[i].state == "下班" for i in ("S01", "S02", "S05", "S13"))       # 早班 staff
    sup = [w for w in s.people.workers if w.team is TeamId.SUPERVISOR and w.state not in ("下班", "請假")]
    assert len(sup) == 1 and sup[0].zone == "war_room" and sup[0].source == "generated"


def test_dataset_worker_names_use_the_team_names_on_screen():
    """A dataset worker has only a staff id; the task list, the report's 派工 and the 3D bubble read 「S14（硫化機組）」, not
    the dataset's 「S14（加硫組）」 (10-22 稽核: one name per team on every screen)."""
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
    w = f.world
    assert w.worker_name("S14") == "S14（硫化機組）" and w.worker_name("S18") == "S18（胎側膠組）"
    assert w.worker_name("S03") == "S03（胎體／胎面膠組）" and w.worker_name("NOPE") is None
    named = next(x for x in w._workers if not x.group)                         # a generated worker keeps their name
    assert w.worker_name(named.id) == named.name
    people = {x.id: x for x in w.snapshot().people.workers}                    # the staff list and the leave list too
    assert people["S14"].team_label == "硫化機組" and people["S18"].team_label == "胎側膠組"
    assert people["S03"].team_label == "胎體／胎面膠組"
    assert {x["id"]: x["team_label"] for x in w.leave_now()}.get("S11") == "貼合組"
    assert not [x for x in w.snapshot().people.workers if x.team_label in ("加硫組", "胎邊組", "打膠組", "成形組")]


def test_team_dataset_sources_and_dates():
    ds = team_dataset.load()
    assert ds is not None and len(ds.staff) == 20
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
    s = f.world.snapshot()
    assert sum(w.source == "team_dataset" for w in s.people.workers) == 20
    # dataset dates shifted onto the sim clock: EV-2 10/28 <-> page 11 10/03; EV-1 10/22 <-> page 2 10/22
    assert ds.dataset_date(f.now().date(), ScenarioName.PRESS7_TEMP_DROP, f.now().date()) == team_dataset.EV2_DATE
    g = Factory(scenario=ScenarioName.OPEN_MILL_OVERHEAT)
    assert ds.dataset_date(g.now().date(), ScenarioName.OPEN_MILL_OVERHEAT, g.now().date()) == team_dataset.EV1_DATE
    molds = [m for m in s.maintenance.upcoming if m.kind == "mold_change"]
    assert molds and molds[0].source == "team_dataset" and molds[0].machine_id.startswith("CURE-")
    assert len(s.quality.incidents) == 8 and all(i.date < f.now().date() for i in s.quality.incidents)
    assert {i.machine_id for i in s.quality.incidents} >= {"A-BIGOPEN", "CURE-09"}
    assert s.quality.daily and all(d.date < f.now().date() for d in s.quality.daily)


def test_team_id_mapping_covers_every_machine():
    from warroom.factory import MACHINE_IDS
    assert sorted(MACHINE_ID.values()) == sorted(MACHINE_IDS)
    assert machine_id("A03 大開煉機") == "A-BIGOPEN" and machine_id("V07 硫化機") == "CURE-07" and machine_id("人工") is None
    assert all(set(ms) <= set(MACHINE_IDS) for ms in WORK_GROUP_MACHINES.values())


def test_page11_abnormal_cure_is_flagged_for_inspection():
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP)
    job = f._press["CURE-07"].job
    assert job is not None
    for _ in range(20 * 60):
        f.step(1)
    assert job.job_id in f.world._job_abnormal     # 122 °C, then below 120: its tyre is inspected as at risk


def test_energy_follows_machine_state():
    f = Factory(scenario=ScenarioName.NORMAL)
    f.step(60)
    before = f.world.snapshot().energy.by_line["A"]
    f.set_machine_status("A-MIX", MachineStatus.STOPPED, "test")
    f.step(60)
    after = f.world.snapshot().energy.by_line["A"]
    assert before - after > 80          # a 110 kW mixer drops to standby


def test_peak_flag_on_weekday_evening():
    f = Factory(scenario=ScenarioName.OPEN_MILL_OVERHEAT)    # Thursday 14:18
    assert not f.world.snapshot().energy.peak
    f.step(2 * 3600)
    assert f.world.snapshot().energy.peak                     # 16:18 on a weekday


def test_repair_ticket_opens_and_closes():
    f = Factory(scenario=ScenarioName.NORMAL)
    f.set_machine_status("B-FORM", MachineStatus.OUT_OF_SERVICE, "油壓異常")
    f.step(30)
    s = f.world.snapshot()
    rep = [x for x in s.maintenance.in_progress if x.kind == "repair" and x.machine_id == "B-FORM"]
    assert rep and "油壓異常" in rep[0].title
    assert any(w.team is TeamId.MAINTENANCE and w.machine_id == "B-FORM" for w in s.people.workers)
    f.set_machine_status("B-FORM", MachineStatus.RUNNING)
    f.step(30)
    s = f.world.snapshot()
    assert not [x for x in s.maintenance.in_progress if x.kind == "repair"]
    assert any(x.kind == "repair" and x.status == "done" for x in s.maintenance.recent)


def test_scenario_load_rebuilds_the_world():
    f = Factory(scenario=ScenarioName.NORMAL)
    f.step(3600)
    old = f.world
    f.load_scenario(ScenarioName.PRESS7_TEMP_DROP)
    assert f.world is not old and f.world.now() == f.now() and f.world.rev == 0


def test_listener_errors_never_reach_the_plant():
    f = Factory(scenario=ScenarioName.NORMAL)

    def boom(_ev):
        raise RuntimeError("listener bug")
    f.world.add_listener(boom)
    for _ in range(24):
        f.step(3600)
    assert f.world.rev > 0 and f.world.events


# ---------------------------------------------------------------- API + WebSocket
@pytest.fixture
def rt(tmp_path) -> Runtime:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario="normal")
    return Runtime(s, run_loop=False)          # the real Factory (the fallback sim has no world)


def test_api_world_and_ws_deltas(rt):
    with TestClient(create_app(rt)) as c:
        r = c.get("/api/world")
        assert r.status_code == 200
        body = r.json()
        assert set(WORLD_SECTIONS) <= set(body) and body["people"]["total"] >= 20
        with c.websocket_connect("/ws") as ws:      # existing clients: snapshot + clock first, nothing new on connect
            types = [ws.receive_json()["type"] for _ in range(2)]
            assert types == ["snapshot", "clock"]
        assert c.post("/api/sim/step", json={"seconds": 120}).status_code == 200
        worlds = [m for m in rt.bus.history if m.type is WsType.WORLD]
        assert worlds and worlds[0].data["full"] is True
        assert c.post("/api/sim/step", json={"seconds": 120}).status_code == 200
        delta = [m for m in rt.bus.history if m.type is WsType.WORLD][-1]
        assert delta.data["full"] is False
        sections = set(delta.data) - {"full", "rev", "factory_time"}
        assert "energy" in sections and "maintenance" not in sections   # only what changed
        assert delta.data["rev"] > worlds[0].data["rev"]


def test_publish_order_keeps_machines_last(rt):
    """Existing clients and tests expect the periodic burst to end with machines / clock / snapshot."""
    rt.sim.step(30)
    rt._publish_periodic(force=True)
    assert rt.bus.history[-1].type in (WsType.MACHINES, WsType.CLOCK, WsType.SNAPSHOT)
    assert any(m.type is WsType.WORLD for m in rt.bus.history)


def test_world_grid_stays_on_factory_time():
    f = Factory(scenario=ScenarioName.NORMAL)
    f.step(25)
    assert f.world.now() == f.world.t0 + timedelta(seconds=20)
