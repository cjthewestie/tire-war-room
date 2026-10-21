"""Review round 2 fixes for warroom/api: restart keeps machine holds, PIN brute-force throttle,
atomic scenario switch, one forecast source, abnormal count from live readings."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.api.app import PinGuard
from warroom.config import Settings
from warroom.contracts import (
    TERMINAL_STATES,
    ApprovalDecision,
    AuditKind,
    DecisionKind,
    MachineStatus,
    ScenarioName,
    Schedule,
    SolverName,
    TileStatus,
)

REAL_NEEDED = ("sim", "detector", "safety", "approvals", "audit", "tools", "gate", "commander", "state_machine",
               "store")


def _settings(tmp_path, scenario="normal", **over) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock", **over)


def _real(tmp_path, scenario="normal", settings_over: dict | None = None, **kw) -> Runtime:
    rt = Runtime(_settings(tmp_path, scenario, **(settings_over or {})), run_loop=False, **kw)
    missing = {k: v for k, v in rt.degraded.items() if k in REAL_NEEDED}
    if missing:
        pytest.skip(f"sibling modules not ready: {missing}")
    return rt


async def _run_until(rt: Runtime, machine_id: str, done, max_s: int, approve: bool = True):
    ev = None
    for _ in range(max_s):
        await rt.tick(1.0, publish=False)
        await asyncio.sleep(0)
        if approve:
            for req in rt.approvals.pending():
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                     reason="test", decided_at=rt.now()))
        ev = next((e for e in rt.events() if e.machine_id == machine_id), None)
        if ev is not None and done(ev):
            break
    return ev


# ---------------------------------------------------------------- 1. restart keeps press 7 out
async def test_restart_keeps_press_out_of_service_and_rollback_blocked(tmp_path, monkeypatch):
    # page 11's story (H1 rejected → 7 號機停用) at the real repair durations; with the demo's fast repair VERIFY
    # waits for the crew's 檢修 and the event closes 「已修復」 instead (tests/test_fast_repair.py)
    monkeypatch.setenv("REPAIR_FAST_S", "0")
    rt = _real(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)
    await rt.start()
    try:
        ev = await _run_until(rt, "CURE-07", lambda e: e.state in TERMINAL_STATES, 60 * 60)
        assert ev is not None and ev.state in TERMINAL_STATES
        assert rt.sim.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
        pv = rt.rollback_preview(None)
        assert pv["feasible"] is False
        active = rt.c.store.current().version
    finally:
        await rt.stop()
    assert (tmp_path / "sched" / "live" / "holds.json").is_file()
    rt2 = Runtime(_settings(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value), run_loop=False, resume=True)
    assert rt2.builder.reopened_store is True and rt2.c.store.current().version == active
    assert rt2.sim.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
    pv2 = rt2.rollback_preview(None)
    assert pv2["feasible"] is False and any("7 號" in r and "停用" in r for r in pv2["reasons"]), pv2
    assert any(e.payload.get("msg") == "machine holds reapplied after restart"
               for e in rt2.audit.tail(0, 100_000))
    # even if the sim forgets it, the persisted hold still blocks the rollback
    rt2.sim.set_machine_status("CURE-07", MachineStatus.RUNNING)
    rt2._holds = {}                                          # pretend nothing synced yet
    (tmp_path / "sched" / "live" / "holds.json").write_text(
        '{"CURE-07": {"status": "out_of_service", "note": "7 號機停用"}}', encoding="utf-8")
    rt2._sync_holds = lambda: None                           # sim view must not overwrite the file here
    assert rt2.rollback_preview(None)["feasible"] is False
    # an explicit scenario load starts clean: no holds
    del rt2._sync_holds
    await rt2.load_scenario(ScenarioName.PRESS7_TEMP_DROP)
    assert rt2.persisted_holds() == {}


# ---------------------------------------------------------------- 2. PIN brute force
def test_pin_guard_locks_host_and_globally():
    t = [0.0]
    g = PinGuard(per_host_max=5, base_lock_s=30, max_lock_s=900, global_max=20, global_window_s=600,
                 global_lock_s=300, now=lambda: t[0])
    for i in range(4):
        assert g.failure("10.0.0.5") == (0.0, None)
    lock, new = g.failure("10.0.0.5")
    assert new == "host" and lock == 30 and g.locked_for("10.0.0.5") == 30
    assert g.locked_for("10.0.0.6") == 0
    t[0] = 31
    for _ in range(5):
        lock, new = g.failure("10.0.0.5")
    assert lock == 60                                          # doubles
    g.success("10.0.0.5")
    assert g.locked_for("10.0.0.5") == 0
    for h in range(10):                                        # 10 more from many addresses -> global
        g.failure(f"10.0.1.{h}")
    assert g.slow_lane()
    # slow lane: untrusted callers share one attempt per 10 s; a correct PIN is delayed, never refused
    assert g.admit("10.9.9.9") == 0                            # takes the shared slot
    assert g.admit("10.9.9.8") == 10
    assert g.admit("10.9.9.8", trusted=True) == 0              # a paired device skips the slow lane
    t[0] += 10
    assert g.admit("10.9.9.8") == 0
    t[0] += 300
    assert not g.slow_lane() and g.admit("10.9.9.7") == 0 and g.admit("10.9.9.6") == 0


def test_pin_brute_force_is_throttled_and_audited(tmp_path):
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    t = [0.0]
    app = create_app(rt, supervisor_pin="7351", pin_guard=PinGuard(now=lambda: t[0]))
    with TestClient(app, client=("192.168.86.77", 50000)) as c:
        codes = []
        for pin in range(0, 50):
            r = c.post("/api/sim/network", json={"network": "online"}, headers={"X-Supervisor-Pin": f"{pin:04d}"})
            codes.append(r.status_code)
        assert codes[:4] == [401] * 4 and codes[4] == 429 and set(codes[5:]) == {429}
        assert "Retry-After" in r.headers
        # the right PIN is refused while locked out
        r = c.post("/api/sim/network", json={"network": "online"}, headers={"X-Supervisor-Pin": "7351"})
        assert r.status_code == 429
        t[0] += 31
        r = c.post("/api/sim/network", json={"network": "online"}, headers={"X-Supervisor-Pin": "7351"})
        assert r.status_code == 200
    # X-Forwarded-For does not reset the per-address budget
    with TestClient(app, client=("192.168.86.78", 50000)) as c:
        codes = [c.post("/api/sim/network", json={"network": "online"},
                        headers={"X-Supervisor-Pin": "0000", "X-Forwarded-For": f"10.1.1.{i}"}).status_code
                 for i in range(6)]
        assert codes[4] == 429 and codes[5] == 429
    ents = [e for e in rt.audit.tail(0, 100_000) if e.kind is AuditKind.INTERCEPTED]
    assert sum(e.payload.get("msg") == "supervisor PIN rejected" for e in ents) >= 5
    assert any(e.payload.get("msg") == "supervisor PIN lockout" for e in ents)
    # localhost (the touch screen) is never locked out
    with TestClient(app) as c:
        assert c.post("/api/sim/network", json={"network": "online"}).status_code == 200


# ---------------------------------------------------------------- 3. atomic scenario switch
async def test_scenario_switch_detaches_old_store_and_clears_events(tmp_path):
    rt = _real(tmp_path, ScenarioName.CONCURRENT_3.value)
    await rt.start()
    try:
        for _ in range(3 * 3600):
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0)
            if sum(e.state not in TERMINAL_STATES for e in rt.events()) >= 2:
                break
        assert [e for e in rt.events() if e.state not in TERMINAL_STATES]
        old_store = rt.c.store
        await rt.load_scenario(ScenarioName.BAD_DATA)
        assert rt.events() == [] and rt.scenario is ScenarioName.BAD_DATA
        # a solver thread of a cancelled event writing late must not land in the new live store
        now = rt.now()
        late = Schedule(id="LATE", created_at=now, horizon_start=now, horizon_end=now + timedelta(days=1),
                        jobs=[], solver=SolverName.HEURISTIC)
        with pytest.raises(OSError):
            old_store.propose(late, "late write from a cancelled event")
        assert [v.version for v in rt.c.store.versions()] == [1]
        assert rt.c.store.current().version == 1
        h = rt.header()
        assert h.abnormal_count == 0, [t for t in rt.machine_tiles() if t.status.value not in ("running", "idle")]
        assert not (tmp_path / "sched" / "live.staging").exists()
    finally:
        await rt.stop()


async def test_failed_scenario_switch_keeps_previous_scenario(tmp_path, monkeypatch):
    rt = _real(tmp_path, ScenarioName.OPEN_MILL_OVERHEAT.value)
    for _ in range(5):
        await rt.tick(1.0, publish=False)
    before_t, before_sim, before_store = rt.now(), rt.c.sim, rt.c.store
    v_before = [v.version for v in before_store.versions()]

    def boom(*a, **k):        # fails AFTER build() already reset the shared clock to bad_data's start
        raise RuntimeError("schedule store live has no ACTIVE version")
    monkeypatch.setattr(rt.builder, "commit_store", boom)
    with TestClient(create_app(rt)) as c:
        r = c.post("/api/sim/control", json={"action": "scenario", "scenario": "bad_data"})
        assert r.status_code == 503 and "維持原情境 open_mill_overheat" in r.json()["detail"]["message"]
        snap = c.get("/api/snapshot").json()
    assert rt.scenario is ScenarioName.OPEN_MILL_OVERHEAT and rt.c.sim is before_sim
    assert abs((rt.now() - before_t).total_seconds()) < 5 * 60       # clock not moved to bad_data's start
    assert snap["header"]["factory_time"].startswith(before_t.strftime("%Y-%m-%dT%H"))
    assert [v.version for v in rt.c.store.versions()] == v_before and rt.c.store.current().version
    assert not (tmp_path / "sched" / "live.staging").exists()
    assert any(e.payload.get("msg") == "scenario load failed; previous scenario kept"
               for e in rt.audit.tail(0, 100_000))


# ---------------------------------------------------------------- 4 + 5. page-2 after closing
async def test_page2_closed_event_one_forecast_and_still_abnormal(tmp_path):
    # real repair durations (REPAIR_FAST_S=0): the event closes while the crew is still on the cooling water; with fast
    # repair it closes 「已修復」 instead (tests/test_fast_repair.py)
    rt = _real(tmp_path, ScenarioName.OPEN_MILL_OVERHEAT.value, settings_over={"repair_fast_s": 0.0})
    await rt.start()
    try:
        ev = await _run_until(rt, "A-BIGOPEN", lambda e: e.state in TERMINAL_STATES, 3 * 3600)
        assert ev is not None and ev.state in TERMINAL_STATES
        for _ in range(300):
            await rt.tick(1.0, publish=False)
        f = rt.focus_panel()
        tiles = {t.label: t for t in f.tiles}
        assert tiles["水流"].status is TileStatus.ALARM                  # cooling water never restored
        h = rt.header()
        assert h.abnormal_count == 1 and h.normal_count == 27, h
        tile = next(t for t in rt.machine_tiles() if t.machine_id == "A-BIGOPEN")
        assert tile.status is MachineStatus.WARNING
        # one forecast: no frozen wake-time time-to-limit in the panel or the drawer banner
        assert not f.detail.startswith(ev.anomaly.message) and f.detail.startswith("事件已結案；現況：")
        md = rt.machine_detail("A-BIGOPEN")
        for text in (f.detail, md.state.note or ""):
            assert "分後達 120°C" not in text, text
        live = rt.sim.machine("A-BIGOPEN").last_reading.temp_c
        assert f"膠溫 {live:.0f}°C" in f.detail and f"膠溫 {live:.0f}°C" in (md.state.note or "")
        assert md.prediction is not None and md.prediction.predicted_value < 120
    finally:
        await rt.stop()
