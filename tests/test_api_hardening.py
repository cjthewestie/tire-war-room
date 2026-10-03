"""API/runtime review fixes: live focus text, page-9 network tiers + warning light, page-10 ingest
(time-series DB, MQTT hop), restart keeps approved schedules, labor gaps by the solver's rule,
write access (PIN, server-set decided_by/via, demo flag), gated rollback, red path never waits."""
from __future__ import annotations

import asyncio
import os
import socket
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import (
    ApprovalDecision,
    ApprovalOption,
    ApprovalRequest,
    AuditKind,
    DecisionKind,
    EventState,
    MachineStatus,
    RiskLevel,
    ScenarioName,
    Schedule,
    ScheduleStatus,
    SolverName,
    TireSize,
    ToolName,
)

REAL_NEEDED = ("sim", "detector", "safety", "approvals", "audit", "tools", "gate", "commander", "state_machine",
               "store")
LAN_PHONE = ("192.168.10.23", 50000)


def _settings(tmp_path, scenario="normal", **kw) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock", **kw)


def _real(tmp_path, scenario="normal") -> Runtime:
    rt = Runtime(_settings(tmp_path, scenario), run_loop=False)
    missing = {k: v for k, v in rt.degraded.items() if k in REAL_NEEDED}
    if missing:
        pytest.skip(f"sibling modules not ready: {missing}")
    return rt


def _request(rt, rid="AP-1", event_id="EV-x") -> ApprovalRequest:
    return rt.approvals.request(ApprovalRequest(
        id=rid, event_id=event_id, created_at=rt.now(), risk=RiskLevel.R3_CRITICAL, title="7號機停用，重排中",
        reason="H1 不成立", options=[
            ApprovalOption(id="A", label="夜班加班", tool=ToolName.NOTIFY_TEAMS,
                           args={"teams": ["body"], "kind": "overtime", "message": "夜班加班"}, recommended=True),
            ApprovalOption(id="B", label="從 2 線調 1 人", tool=ToolName.NOTIFY_TEAMS,
                           args={"teams": ["side"], "kind": "staff_move", "message": "調 1 人"})]))


def _body(rid="AP-1", via="phone", **kw):
    return {"request_id": rid, "decision": "approve", "option_id": "A", "via": via, **kw}


# ---------------------------------------------------------------- 1. live centre-panel text
async def test_focus_detail_is_live_not_detection_time(tmp_path):
    rt = _real(tmp_path, ScenarioName.OPEN_MILL_OVERHEAT.value)
    await rt.start()
    try:
        f = None
        while rt.now().strftime("%H:%M:%S") < "14:26:00":
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0)
        f = rt.focus_panel()
        assert f is not None and f.machine_id == "A-BIGOPEN"
        temp_tile = next(t for t in f.tiles if t.label == "溫度")
        live = rt.sim.machine("A-BIGOPEN").last_reading.temp_c
        assert 110 <= live <= 114
        assert f"膠溫 {live:.0f}°C" in f.detail, f.detail           # the same reading as the tile
        assert temp_tile.value.startswith(f"{live:.0f}") or temp_tile.value.startswith(f"{live:.1f}")
        assert "持續上升" in f.detail and "冷卻能力不足" in f.detail
        assert "6 分後達 120°C" not in f.detail, f.detail            # the steady state is ~117.6 °C
        assert "未達 120°C 上限" in f.detail, f.detail
        assert "未加硫上限 120°C；已加硫另設 110°C 防焦燒" in f.detail
    finally:
        await rt.stop()


# ---------------------------------------------------------------- 2. page-9 network tiers
@pytest.fixture
def frt(tmp_path) -> Runtime:
    return Runtime(_settings(tmp_path), use_real=False, run_loop=False)


def test_online_phone_over_lan_with_pin(frt):
    _request(frt)
    app = create_app(frt, supervisor_pin="2468")
    with TestClient(app, client=LAN_PHONE) as phone:
        assert phone.post("/api/approvals/AP-1/decision", json=_body()).status_code == 401
        r = phone.post("/api/approvals/AP-1/decision", json=_body(decided_by="廠長 王大明"),
                       headers={"X-Supervisor-Pin": "2468"})
        assert r.status_code == 200, r.text
        assert r.json()["decision"]["decided_by"] == f"supervisor@{LAN_PHONE[0]}"    # not the body's claim
        assert r.json()["decision"]["via"] == "phone"


def test_wan_down_phone_still_approves_over_lan(frt):
    _request(frt)
    app = create_app(frt, supervisor_pin="2468")
    with TestClient(app) as local, TestClient(app, client=LAN_PHONE) as phone:
        assert local.post("/api/sim/network", json={"network": "wan_down"}).json()["phone_approvals"] is True
        snap = local.get("/api/snapshot").json()
        assert snap["header"]["network"] == "wan_down" and snap["header"]["warning_light"] is False
        r = phone.post("/api/approvals/AP-1/decision", json=_body(), headers={"X-Supervisor-Pin": "2468"})
        assert r.status_code == 200, r.text


def test_lan_down_only_touchscreen_and_warning_light(frt):
    _request(frt)
    app = create_app(frt, supervisor_pin="2468")
    with TestClient(app) as local, TestClient(app, client=LAN_PHONE) as phone:
        r = local.post("/api/sim/network", json={"network": "lan_down"}).json()
        assert r["warning_light"] is True and r["phone_approvals"] is False
        assert local.get("/api/snapshot").json()["header"]["warning_light"] is True
        # phone (even with the PIN) and a local client claiming via=phone are both refused
        assert phone.post("/api/approvals/AP-1/decision", json=_body(),
                          headers={"X-Supervisor-Pin": "2468"}).status_code == 409
        assert local.post("/api/approvals/AP-1/decision", json=_body(via="phone")).status_code == 409
        r = local.post("/api/approvals/AP-1/decision", json=_body(via="touchscreen"))
        assert r.status_code == 200 and r.json()["decision"]["via"] == "touchscreen"
        # nothing waits any more: the light goes off, and the change is audited
        assert local.get("/api/snapshot").json()["header"]["warning_light"] is False
        lights = [e["payload"]["on"] for e in local.get("/api/audit", params={"kind": "system"}).json()
                  if e["payload"].get("msg") == "warning light"]
        assert lights == [True, False]
        assert local.post("/api/sim/network", json={"network": "online"}).status_code == 200


# ---------------------------------------------------------------- 6. write access
def test_remote_writes_need_pin_and_auto_is_server_only(frt):
    _request(frt)
    app = create_app(frt)                    # no PIN configured: remote writes refused outright
    with TestClient(app, client=LAN_PHONE) as phone, TestClient(app) as local:
        assert phone.post("/api/approvals/AP-1/decision", json=_body()).status_code == 403
        assert phone.post("/api/schedule/rollback", json={"confirm": True}).status_code == 403
        assert phone.post("/api/sim/control", json={"action": "pause"}).status_code == 403
        assert phone.post("/api/sim/step", json={"seconds": 5}).status_code == 403
        # a forwarded LAN client behind a local proxy is still remote
        assert local.post("/api/sim/control", json={"action": "pause"},
                          headers={"X-Forwarded-For": LAN_PHONE[0]}).status_code == 403
        assert phone.get("/api/snapshot").status_code == 200          # reads stay open on the LAN
        assert local.post("/api/approvals/AP-1/decision", json=_body(via="auto")).status_code == 422
        r = local.post("/api/approvals/AP-1/decision", json=_body(via="dashboard", decided_by="廠長 王大明"))
        assert r.status_code == 200 and r.json()["decision"]["decided_by"] == "supervisor"
        dec = local.get("/api/audit", params={"kind": "approval_decision"}).json()[-1]
        assert dec["actor"] == "supervisor" and dec["payload"]["via"] == "dashboard"


def test_demo_controls_can_be_switched_off(frt):
    with TestClient(create_app(frt, demo_controls=False)) as c:
        assert c.post("/api/sim/step", json={"seconds": 5}).status_code == 403
        assert c.post("/api/sim/network", json={"network": "lan_down"}).status_code == 403
        assert c.get("/api/health/components").json()["access"]["demo_controls"] is False


# ---------------------------------------------------------------- 6 + 8. gated rollback
def _job(press, mold_in, jid="J-T1", order="#M-201", size=TireSize.M):
    from warroom.schedule import back_propagate
    return back_propagate(jid, order, size, press, mold_in)


def test_rollback_refuses_proposed_and_out_of_service_press(tmp_path):
    rt = _real(tmp_path)
    store, now = rt.c.store, rt.now()
    sched = lambda sid, jobs: Schedule(id=sid, created_at=now, horizon_start=now,  # noqa: E731
                                       horizon_end=now + timedelta(days=7), jobs=jobs, solver=SolverName.CP_SAT)
    v2 = store.propose(sched("S2", [_job("CURE-07", now + timedelta(hours=5))]), "v2 loads press 7")
    store.activate(v2.version)
    v3 = store.propose(sched("S3", [_job("CURE-09", now + timedelta(hours=5))]), "v3 moves it to press 9")
    store.activate(v3.version)
    v4 = store.propose(sched("S4", []), "unapproved proposal")
    with TestClient(create_app(rt)) as c:
        # 6: a PROPOSED version never went through the gate + approval
        r = c.post("/api/schedule/rollback", json={"version": v4.version, "confirm": True})
        assert r.status_code == 409 and store.get(v4.version).status is ScheduleStatus.PROPOSED
        # 8: press 7 out of service -> v2 (new mold-in on press 7) is blocked, with the reason
        rt.sim.set_machine_status("CURE-07", MachineStatus.OUT_OF_SERVICE, "7 號機停用")
        pv = c.get("/api/schedule/rollback/preview").json()
        assert pv["to_version"] == v2.version and pv["feasible"] is False
        assert any("7 號" in x and "停用" in x for x in pv["reasons"]), pv["reasons"]
        r = c.post("/api/schedule/rollback", json={"confirm": True})
        assert r.status_code == 409 and "停用" in r.json()["detail"]["message"]
        assert store.current().version == v3.version
        gate = [e for e in c.get("/api/audit", params={"kind": "gate"}).json()
                if e["payload"].get("action") == "schedule_rollback"]
        assert gate[-1]["payload"]["decision"] == "reject"
        # back in service: needs confirm (428 + diff), then goes through the approval broker
        rt.sim.set_machine_status("CURE-07", MachineStatus.RUNNING)
        r = c.post("/api/schedule/rollback", json={})
        assert r.status_code == 428 and r.json()["detail"]["moved_count"] == 1
        r = c.post("/api/schedule/rollback", json={"confirm": True, "reason": "test"})
        assert r.status_code == 200 and r.json()["version"] == v2.version
        appr = [a for a in c.get("/api/approvals").json() if a["id"].startswith("AP-RB-")]
        assert appr and appr[-1]["status"] == "approved" and appr[-1]["risk"] == "R2"


# ---------------------------------------------------------------- 7. red path never waits
async def test_safety_runs_before_compute_hold(frt):
    order: list[str] = []
    real_check = frt.c.safety.check

    def check(readings, states):
        order.append("safety")
        return real_check(readings, states)
    frt.c.safety.check = check
    frt.c.state_machine.compute_inflight = 1          # the LLM is "thinking"
    frt.compute_hold_max_s = 0.3
    t0 = time.monotonic()
    await frt.tick(1.0, publish=False)
    waited = time.monotonic() - t0
    assert order == ["safety"]                         # checked before the hold, not after it
    assert 0.25 <= waited < 1.0                        # wall-clock bound (monotonic), not a sleep counter
    frt.c.state_machine.compute_inflight = 0


# ---------------------------------------------------------------- 3. ingest: time-series DB (+ MQTT)
async def test_readings_land_in_timeseries_db_before_detection(frt):
    await frt.run_seconds(5)
    n = frt.ingest.store.count()
    assert n >= 5 * 28 and frt.ingest.stats.stored == n
    rows = frt.ingest.store.query("CURE-07")
    assert len(rows) == 5 and rows[-1].ts == frt.now()


def _broker_up(host="127.0.0.1", port=1883) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


@pytest.mark.skipif(not _broker_up(), reason="no MQTT broker on 127.0.0.1:1883 (docker run eclipse-mosquitto)")
async def test_mqtt_hop_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("WARROOM_INGEST", "mqtt")
    monkeypatch.setenv("WARROOM_MQTT_URL", "mqtt://127.0.0.1:1883")
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    try:
        assert rt.ingest.bridge is not None, rt.ingest.stats.last_error
        await rt.run_seconds(10)
        st = rt.ingest.stats
        assert st.mode == "mqtt" and st.mqtt_missed_ticks == 0, st
        assert st.received == st.published >= 10 * 28
        assert rt.ingest.store.count(via="mqtt") == st.stored
    finally:
        rt.ingest.close()


async def test_mqtt_unreachable_falls_back_to_direct(tmp_path, monkeypatch):
    monkeypatch.setenv("WARROOM_INGEST", "mqtt")
    monkeypatch.setenv("WARROOM_MQTT_URL", "mqtt://127.0.0.1:1")
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    assert rt.ingest.bridge is None and "mqtt unavailable" in (rt.ingest.stats.last_error or "")
    await rt.run_seconds(3)
    assert rt.ingest.store.count() >= 3 * 28


# ---------------------------------------------------------------- 4 + 5. page 11: labor gaps, restart
async def test_page11_overtime_clears_gap_and_restart_keeps_schedule(tmp_path):
    rt = _real(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)
    with TestClient(create_app(rt)) as c:
        gaps0 = c.get("/api/labor").json()["gaps"]
    assert gaps0 == ["胎體／胎面膠組 10/03 夜班缺 1 人"], gaps0        # the solver's own gap, nothing else
    await rt.start()
    try:
        for _ in range(30 * 60):
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0)
            for req in rt.approvals.pending():
                opt = next((o for o in req.options if "夜班" in o.label), None)
                rt.approvals.decide(ApprovalDecision(
                    request_id=req.id, decision=DecisionKind.MODIFY if opt else DecisionKind.APPROVE,
                    option_id=opt.id if opt else None, reason="不跨組調人", decided_at=rt.now()))
            ev = next((e for e in rt.events() if e.machine_id == "CURE-07"), None)
            if ev is not None and ev.state is EventState.VERIFY and rt.c.store.current().version >= 2:
                break
        assert ev is not None and ev.state is EventState.VERIFY, ev and ev.state
        active = rt.c.store.current()
        assert any(ch.label == "夜班加班" for ch in active.schedule.staffing_changes)
        lab = rt.labor_view()
        assert not any("10/03 夜班" in g for g in lab["gaps"]), lab["gaps"]
        night = next(s for s in rt.effective_shifts() if s.id == "body-20261003-夜班")
        assert night.headcount == 2
        assert not [sh for sh, _, n in rt.staffing_gap_rows() if sh.id == "body-20261003-夜班"]
        versions_before = [(v.version, v.status) for v in rt.c.store.versions()]
        seq_before = rt.audit.tail(0, 100_000)[-1].seq
    finally:
        await rt.stop()                                  # graceful stop = watchdog restart / offline update
    after = rt.audit.tail(seq_before, 100_000)
    assert not [e for e in after if e.kind is AuditKind.ROLLBACK], [e.payload for e in after]
    assert not [e for e in after if e.kind is AuditKind.STATE_TRANSITION]
    assert any(e.payload.get("msg") == "event interrupted by shutdown" for e in after)
    rt2 = Runtime(_settings(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value), run_loop=False)
    assert rt2.builder.reopened_store is True
    assert [(v.version, v.status) for v in rt2.c.store.versions()] == versions_before
    assert rt2.c.store.current().version == active.version
    # an explicit scenario load still starts from a fresh store
    await rt2.load_scenario(ScenarioName.PRESS7_TEMP_DROP)
    assert [v.version for v in rt2.c.store.versions()] == [1]
    assert os.path.exists(tmp_path / "sched" / "live" / "scenario.txt")
