"""Issue #36 模擬事件: random plant events for a live demo.

CJ, 2026-10-20: 「我想要有模擬事件」. Off by default; one seed gives one sequence; every event goes through the
normal path (a machine event through the detector, a plant event through the state machine, writes through the
gate); page 11 plays out exactly as before while it is off.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

import httpx
import pytest

from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import (
    AuditKind,
    FaultSpec,
    PlantInput,
    ScenarioName,
)
from warroom.factory import CURE_PRESS_IDS
from warroom.sim import Factory
from warroom.sim.day_loop import LoopFactory
from warroom.sim.events import CATALOG, CATALOG_BY_KIND, SimEvents, WorldInput, curing

pytestmark = pytest.mark.timeout(300)
ROOT = pathlib.Path(__file__).resolve().parent.parent


def _settings(tmp_path, scenario: str) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock")


async def _runtime(tmp_path, scenario: str) -> Runtime:
    rt = Runtime(_settings(tmp_path, scenario), allow_fallback=False, run_loop=False)
    assert rt.degraded == {}
    await rt.start()
    return rt


# ------------------------------------------------------------------ the generator on its own
def test_off_by_default_and_never_due():
    ev = SimEvents()
    f = Factory(scenario=ScenarioName.NORMAL)
    assert ev.enabled is False
    for _ in range(24 * 60):
        f.step(60)
        assert not ev.due(f.now())


def _sequence(seed: int, n: int = 16) -> list[str]:
    f = Factory(scenario=ScenarioName.NORMAL)
    f.step(3600)
    ev = SimEvents()
    ev.configure(enabled=True, every_min=5, seed=seed)
    out = []
    for _ in range(n):
        kind = ev.pick()
        spec = ev.build(kind, f, f.now(), set())
        out.append(f"{kind}:{spec.model_dump_json() if hasattr(spec, 'model_dump_json') else repr(spec)}")
    return out


def test_same_seed_same_sequence():
    a, b, c = _sequence(36), _sequence(36), _sequence(37)
    assert a == b
    assert a != c
    kinds = {x.split(":", 1)[0] for x in _sequence(36, 60)}
    assert len(kinds) >= 6, kinds                     # the weighted list really mixes machine and plant events


def test_every_kind_builds_a_fault_or_plant_input():
    f = Factory(scenario=ScenarioName.NORMAL)
    f.step(3600)
    ev = SimEvents()
    for e in CATALOG:
        out = ev.build(e.kind, f, f.now(), set())
        assert isinstance(out, {"fault": FaultSpec, "plant": PlantInput, "world": WorldInput}[e.source]), e.kind
        if not isinstance(out, FaultSpec):
            assert out.text
    with pytest.raises(KeyError):
        ev.build("explode", f, f.now(), set())


def test_due_every_n_minutes_and_restarts_after_clock_moves_back():
    f = Factory(scenario=ScenarioName.NORMAL)
    ev = SimEvents()
    ev.configure(enabled=True, every_min=10)
    hits = 0
    for _ in range(61):
        hits += ev.due(f.now())
        f.step(60)
    assert hits == 6                                   # minutes 10, 20, ..., 60
    f.load_scenario(ScenarioName.NORMAL)               # clock back to 08:00: the wait restarts, no burst
    assert not ev.due(f.now()) and ev.next_at is not None


# ------------------------------------------------------------------ page 11 unchanged while off
async def _p11_fingerprint(tmp_path, monkeypatch=None) -> str:
    rt = await _runtime(tmp_path, "press7_temp_drop")
    h = hashlib.sha256()
    try:
        for _ in range(20 * 60):                         # 02:12 -> 02:32: detection, gate, leak, CURE_HOLD, verify
            await rt.tick(1.0, publish=False)
            for r in rt.sim.machines():
                if r.machine_id == "CURE-07":
                    h.update(r.model_dump_json().encode())
        await rt.wait_events(5)
        for e in rt.events():
            h.update(json.dumps([e.id, e.state.value, [x.text for x in e.timeline]], ensure_ascii=False).encode())
        for a in rt.safety_log:
            h.update(a.model_dump_json(exclude={"latency_ms"}).encode())      # wall-clock, differs run to run
        h.update(json.dumps([[p.id, p.title, p.risk.value] for p in rt.approvals.pending()], ensure_ascii=False).encode())
    finally:
        await rt.stop()
    return h.hexdigest()


async def test_page11_unchanged_with_generator_off(tmp_path, monkeypatch):
    """The page-11 run with the generator present (off) equals the run with it removed from the tick."""
    with_gen = await _p11_fingerprint(tmp_path / "a")
    monkeypatch.setattr(Runtime, "_random_event", lambda self: None)
    without = await _p11_fingerprint(tmp_path / "b")
    assert with_gen == without


# ------------------------------------------------------------------ the normal path
async def test_random_machine_event_goes_through_the_detector(tmp_path, monkeypatch):
    monkeypatch.setattr(SimEvents, "pick", lambda self: "steam_low")
    rt = await _runtime(tmp_path, "normal")
    try:
        rt.sim_events.configure(enabled=True, every_min=5, seed=1)
        for _ in range(9 * 60):
            await rt.tick(1.0, publish=False)
        fired = rt.sim_events.fired
        assert fired and fired[0].kind == "steam_low" and fired[0].source == "random"
        mid = fired[0].ref
        an = [a for a in rt.anomalies if a.machine_id == mid]
        assert an, "the detector saw it in the readings"
        assert any(e.machine_id == mid and e.source == "detector" for e in rt.events())
    finally:
        await rt.stop()
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(a["payload"].get("msg") == "fault injected" for a in audit)
    assert any(a["payload"].get("msg") == "模擬事件" and a["actor"] == "sim_events" for a in audit)


# ------------------------------------------------------------------ 觸發異常 always gets a card (10-21)
async def test_steam_faults_go_to_a_press_curing_a_tyre(tmp_path):
    """10-21: steam_low on an idle press safe-stopped with no card (3 of 5 demo fires): its cure cannot be extended.
    A steam fault goes to a press curing a tyre with an hour or more to go; a busy one is never stacked on."""
    rt = await _runtime(tmp_path, "normal")
    try:
        await rt.tick(1.0, publish=False)
        f = rt.sim
    finally:
        await rt.stop()
    working = {p for p in CURE_PRESS_IDS if curing(f, p)}
    assert working and set(CURE_PRESS_IDS) - working                       # the plant's NORMAL has idle presses too
    picks = set()
    for seed in range(40):
        ev = SimEvents()
        ev.configure(seed=seed)
        for kind in ("steam_low", "steam_leak"):
            picks.add(ev.build(kind, f, f.now(), set()).machine_id)
    assert picks <= working and len(picks) > 1, picks
    last = sorted(working)[-1]
    assert SimEvents().build("steam_low", f, f.now(), working - {last}).machine_id == last
    # every curing press busy: a free one all the same (never stacked), as before
    assert SimEvents().build("steam_low", f, f.now(), working).machine_id not in working


def test_day_loop_faults_never_go_to_a_replayed_machine():
    """一日循環 replays the dataset day on six machines (7 號 among them): a fault there never shows in the readings."""
    f = LoopFactory(scenario=ScenarioName.DAY_LOOP)
    f.step(60)
    hidden = f.replayed_machines()
    assert "A-BIGOPEN" in hidden and hidden & set(CURE_PRESS_IDS), hidden
    assert LoopFactory(scenario=ScenarioName.NORMAL).replayed_machines() == set()
    for seed in range(40):
        ev = SimEvents()
        ev.configure(seed=seed)
        for kind in ("steam_low", "steam_leak", "water_low"):
            mid = ev.build(kind, f, f.now(), set()).machine_id
            assert mid not in hidden, (seed, kind, mid)
            assert kind == "water_low" or curing(f, mid), (seed, kind, mid)


async def test_fired_steam_low_gets_a_card(tmp_path):
    rt = await _runtime(tmp_path, "normal")
    try:
        await rt.tick(1.0, publish=False)
        mid = rt.fire_sim_event("steam_low")["ref"]
        assert curing(rt.sim, mid)
        for _ in range(1200):
            await rt.tick(1.0, publish=False)
            if rt.approvals.pending():
                break
        assert [p.event_id for p in rt.approvals.pending()] == [e.id for e in rt.events() if e.machine_id == mid]
    finally:
        await rt.stop()


async def test_material_events_stay_in_the_world(tmp_path):
    """Late delivery and a stock-out change the world (feed + stock) only; a shortfall card is #38's patrol."""
    rt = await _runtime(tmp_path, "normal")
    try:
        await rt.tick(1.0, publish=False)
        w = rt.sim.world
        late = rt.fire_sim_event("delivery_late")
        out = rt.fire_sim_event("stock_out")
        key = out["ref"]
        assert w._stock[key] == 0.0
        for _ in range(30):
            await rt.tick(1.0, publish=False)
        kinds = [e.kind for e in w.events]
        assert "stockout" in kinds
        if late["kind"] == "delivery_late":
            assert "delivery_late" in kinds
        assert rt.events() == [] and rt.approvals.pending() == []      # no plant event, no card
    finally:
        await rt.stop()


async def test_fire_api_rush_order_and_staff_sick(tmp_path):
    rt = await _runtime(tmp_path, "normal")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
    try:
        await rt.tick(1.0, publish=False)
        v = (await client.get("/api/sim/events")).json()
        assert v["enabled"] is False and {c["kind"] for c in v["catalog"]} == set(CATALOG_BY_KIND)
        r = await client.post("/api/sim/events/fire", json={"kind": "rush_order"})
        assert r.status_code == 200, r.text
        oid = r.json()["fired"]["ref"]
        r = await client.post("/api/sim/events/fire", json={"kind": "staff_sick"})
        assert r.status_code == 200, r.text
        assert (await client.post("/api/sim/events/fire", json={"kind": "explode"})).status_code == 404
        assert (await client.post("/api/sim/events", json={"every_min": 1})).status_code == 422
        r = await client.post("/api/sim/events", json={"enabled": True, "every_min": 15, "seed": 9})
        assert r.status_code == 200 and r.json()["enabled"] and r.json()["seed"] == 9
        await rt.tick(1.0, publish=False)
        kinds = sorted(e.anomaly.kind.value for e in rt.events())
        assert kinds == ["order_rush", "staffing_short"], kinds
        assert all(e.source == "plant" for e in rt.events())
        assert any(o.id == oid for o in rt.sim.orders())
        clock = rt.clock_info()["random_events"]
        assert clock["enabled"] and clock["every_min"] == 15 and clock["fired"] == 2
    finally:
        await client.aclose()
        await rt.stop()
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for a in audit if a["kind"] == AuditKind.SYSTEM.value and a["payload"].get("msg") == "模擬事件") == 2


# ------------------------------------------------------------------ rings: every machine reports its batch progress
async def test_machine_tiles_carry_step_progress(tmp_path):
    rt = await _runtime(tmp_path, "normal")
    try:
        for _ in range(120):
            await rt.tick(1.0, publish=False)
        tiles = {t.machine_id: t for t in rt.machine_tiles()}
        running = [t for t in tiles.values() if t.status.value == "running" and not t.machine_id.startswith("CURE-")]
        assert len(running) >= 10 and all(t.step_progress is not None and 0 <= t.step_progress <= 1 for t in running)
        presses = [t for t in tiles.values() if t.machine_id.startswith("CURE-") and t.step_progress is not None]
        assert presses and all(0 <= t.step_progress <= 1 for t in presses)
    finally:
        await rt.stop()


def test_dev_sh_never_overwrites_the_app_port():
    """`PORT=8000 ./start.sh ollama`: dev.sh keeps Ollama's port in its own variable."""
    sh = (ROOT / "deploy" / "mac" / "dev.sh").read_text(encoding="utf-8")
    assert "OLLAMA_PORT=" in sh
    assert not any(ln.strip().startswith(("PORT=", "export PORT")) for ln in sh.splitlines())
    assert "$PORT" not in sh.replace("$OLLAMA_PORT", "")
