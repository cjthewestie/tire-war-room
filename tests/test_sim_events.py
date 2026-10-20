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

from tests.test_plant_events import _check_common
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import (
    ApprovalDecision,
    AuditKind,
    DecisionKind,
    EventState,
    FaultSpec,
    PlantInput,
    ScenarioName,
)
from warroom.sim import Factory
from warroom.sim.events import CATALOG, CATALOG_BY_KIND, SimEvents

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
        out.append(f"{kind}:{spec.model_dump_json()}")
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
        assert isinstance(out, FaultSpec if e.source == "fault" else PlantInput), e.kind
        if isinstance(out, PlantInput):
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


async def _drive_plant(rt: Runtime, minutes: int = 120):
    decided = []
    for _ in range(minutes * 60):
        await rt.tick(1.0, publish=False)
        for req in rt.approvals.pending():
            opt = next((o for o in req.options if o.recommended), req.options[0] if req.options else None)
            decided.append(rt.approvals.decide(ApprovalDecision(
                request_id=req.id, decision=DecisionKind.APPROVE, option_id=opt.id if opt else None, reason="test",
                via="auto", decided_at=rt.now())))
        evs = rt.events()
        if evs and all(e.state in (EventState.CLOSED, EventState.SAFE_STOP) for e in evs):
            break
    await rt.wait_events(5)
    return decided


@pytest.mark.parametrize("kind", ["material_short", "delivery_late"])
async def test_material_event_runs_the_plant_path(tmp_path, kind):
    rt = await _runtime(tmp_path, "normal")
    try:
        await rt.tick(1.0, publish=False)
        fired = rt.fire_sim_event(kind)
        assert fired["source"] == "manual" and fired["text"]
        decided = await _drive_plant(rt)
        world, events = rt.world_snapshot(), rt.events()
    finally:
        await rt.stop()
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    ev, tasks = _check_common(events, decided, world, audit, "material_short")
    assert {t.kind for t in tasks} >= {"盤點", "調料"}
    assert any(x.startswith("原料事件") for x in decided[0].done)
    assert any(e.kind in ("material_short", "delivery_late") for e in world.events)


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
