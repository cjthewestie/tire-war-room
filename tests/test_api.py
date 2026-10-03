"""API module tests: every REST endpoint, the WebSocket, the runtime loop and scenario control.

Most tests run on the deterministic fallback plant (use_real=False) so they do not depend on
sibling modules being finished; test_real_* run against whatever real modules are present and
skip with the degraded list when a sibling is still a stub."""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from warroom import factory as F
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import (
    REST_ENDPOINTS,
    ApprovalOption,
    ApprovalRequest,
    AuditKind,
    DashboardSnapshot,
    RiskLevel,
    ScenarioName,
    Schedule,
    SolverName,
    ToolName,
    WsType,
)


def _settings(tmp_path, scenario="normal", **kw) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, **kw)


@pytest.fixture
def rt(tmp_path) -> Runtime:
    return Runtime(_settings(tmp_path), use_real=False, run_loop=False)


@pytest.fixture
def client(rt):
    with TestClient(create_app(rt)) as c:
        yield c


def step(client, seconds):
    r = client.post("/api/sim/step", json={"seconds": seconds})
    assert r.status_code == 200, r.text
    return r.json()


def scenario(client, name):
    r = client.post("/api/sim/control", json={"action": "scenario", "scenario": name})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- contract coverage
def test_every_contract_endpoint_is_routed(client):
    routes = {(m, r.path) for r in client.app.routes for m in getattr(r, "methods", None) or {"WS"}}
    for method, path, _req, _resp in REST_ENDPOINTS:
        if method == "WS":
            assert any(p == path for _, p in routes), path
        else:
            assert (method, path) in routes, (method, path)


# ---------------------------------------------------------------- health
def test_health(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True and h["llm_mode"] == "mock" and h["llm_reachable"] is False
    assert h["factory_time"].endswith("+08:00")
    c = client.get("/api/health/components").json()
    assert "sim" in c["degraded"] and c["audit_chain_ok"] is True


# ---------------------------------------------------------------- dashboard
def test_snapshot_page2_numbers(client):
    step(client, 3)
    s = DashboardSnapshot.model_validate(client.get("/api/snapshot").json())
    assert [m.machine_id for m in s.machines] == list(F.MACHINE_IDS)
    assert s.header.normal_count == 28 and s.header.abnormal_count == 0
    assert [(o.size.value, o.done, o.total, o.remaining_h) for o in s.orders] == \
        [("S", 38, 100, 208), ("M", 12, 100, 1241), ("L", 8, 100, 2162)]
    assert s.semi_finished.tire_label == "大胎 #07"
    assert [p.status_text for p in s.semi_finished.parts] == ["預熱中", "成形中", "包布中", "已完成"]
    assert s.focus is None and s.events == []


def test_machines_lines_and_detail(client):
    step(client, 5)
    ms = client.get("/api/machines").json()
    assert len(ms) == 28 and ms[0]["machine_id"] == "A-MIX"
    cure = client.get("/api/machines", params={"line": "CURE"}).json()
    assert [m["machine_id"] for m in cure] == list(F.CURE_PRESS_IDS)
    lines = client.get("/api/lines").json()
    assert [ln["line"] for ln in lines] == ["A", "B", "C", "D", "BOND", "CURE"]
    assert sum(len(ln["machines"]) for ln in lines) == 28
    d = client.get("/api/machines/CURE-07").json()
    assert d["state"]["machine_id"] == "CURE-07" and len(d["history"]) == 5
    assert [t["label"] for t in d["tiles"]] == ["溫度", "壓力", "時間", "蒸汽"]
    d2 = client.get("/api/machines/A-BIGOPEN").json()
    assert [t["label"] for t in d2["tiles"]] == ["溫度", "壓力", "時間", "水流"]
    assert client.get("/api/machines/NOPE").status_code == 404


# ---------------------------------------------------------------- scenarios -> events, safety, audit
def test_press7_scenario_detects_and_safety_acts(client, rt):
    info = scenario(client, "press7_temp_drop")
    assert info["factory_time"].startswith("2026-10-03T02:13:30")
    step(client, 90)
    evs = client.get("/api/events").json()
    assert len(evs) == 1 and evs[0]["machine_id"] == "CURE-07"
    an = client.get("/api/anomalies").json()[0]
    assert an["machine_id"] == "CURE-07" and an["detection_latency_s"] <= F.DETECTION_TARGET_S
    ev = client.get(f"/api/events/{evs[0]['id']}").json()
    assert ev["timeline"][0]["actor"] == "事件偵測"
    assert client.get("/api/events/EV-nope").status_code == 404
    fp = client.get(f"/api/events/{ev['id']}/focus").json()
    assert fp["title"].startswith("異常：7 號硫化機")
    # jump to 02:31: temp falls below 120 -> safety core (red path) acts without any LLM
    step(client, 17 * 60)
    sa = client.get("/api/safety").json()
    assert sa and sa[0]["machine_id"] == "CURE-07" and sa[0]["action"] == "cure_hold"
    kinds = [e["kind"] for e in client.get("/api/audit", params={"limit": 5000}).json()]
    assert "safety_action" in kinds and "anomaly" in kinds
    only = client.get("/api/audit", params={"kind": "safety_action"}).json()
    assert {e["kind"] for e in only} == {"safety_action"}
    snap = client.get("/api/snapshot").json()
    assert snap["header"]["abnormal_count"] == 1           # CURE-07 stopped/held
    assert rt.audit.verify_chain()


def test_open_mill_focus_panel_page2(client):
    scenario(client, "open_mill_overheat")
    step(client, 20 * 60)
    snap = client.get("/api/snapshot").json()
    f = snap["focus"]
    assert f["machine_id"] == "A-BIGOPEN"
    assert f["title"] == "異常：A 線大開煉機　溫度過高"
    assert "未加硫上限 120°C；已加硫另設 110°C 防焦燒" in f["detail"]
    assert f["schedule_impact"] == "無，大胎 #07 仍 17:00 入硫化機"
    tiles = {t["label"]: t for t in f["tiles"]}
    assert tiles["溫度"]["status"] == "alarm" and tiles["水流"]["value"] == "偏低"
    # UNVULCANIZED compound under 120 °C: safety core must NOT stop it (note 7)
    assert not [a for a in snap["safety_actions"] if a["machine_id"] == "A-BIGOPEN"]


# ---------------------------------------------------------------- approvals
def _request(rt, rid="AP-1", event_id="EV-x") -> ApprovalRequest:
    return rt.approvals.request(ApprovalRequest(
        id=rid, event_id=event_id, created_at=rt.now(), risk=RiskLevel.R3_CRITICAL, title="7號機停用，重排中",
        reason="H1 不成立：溫度續降至 119.5°C", options=[
            ApprovalOption(id="A", label="夜班加班", tool=ToolName.NOTIFY_TEAMS,
                           args={"teams": ["curing"], "kind": "overtime", "message": "夜班加班"}, recommended=True),
            ApprovalOption(id="B", label="從 2 線調 1 人", tool=ToolName.NOTIFY_TEAMS,
                           args={"teams": ["side"], "kind": "staff_move", "message": "調 1 人"}),
        ]))


def test_approvals_list_and_decide(client, rt):
    _request(rt)
    pend = client.get("/api/approvals", params={"status": "pending"}).json()
    assert [p["id"] for p in pend] == ["AP-1"]
    body = {"request_id": "AP-1", "decision": "modify", "option_id": "A", "reason": "不跨組調人，改夜班加班"}
    r = client.post("/api/approvals/AP-1/decision", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "modified" and out["decision"]["decided_at"].startswith("2026-10-03")
    assert client.post("/api/approvals/AP-1/decision", json=body).status_code == 409
    assert client.post("/api/approvals/NOPE/decision", json={**body, "request_id": "NOPE"}).status_code == 404
    assert client.post("/api/approvals/AP-1/decision", json={**body, "request_id": "X"}).status_code == 422
    assert client.get("/api/approvals", params={"status": "pending"}).json() == []
    assert [a["status"] for a in client.get("/api/approvals").json()] == ["modified"]
    dec = client.get("/api/audit", params={"kind": "approval_decision"}).json()
    assert dec[0]["payload"]["reason"] == "不跨組調人，改夜班加班"


def test_pending_approval_appears_in_focus(client, rt):
    scenario(client, "press7_temp_drop")
    step(client, 90)
    ev = client.get("/api/events").json()[0]
    _request(rt, event_id=ev["id"])
    f = client.get(f"/api/events/{ev['id']}/focus").json()
    assert [(p["option_id"], p["label"]) for p in f["pending"]] == [("A", "夜班加班"), ("B", "從 2 線調 1 人")]


# ---------------------------------------------------------------- schedule, capacity, orders, labor
def test_schedule_versions_and_rollback(client, rt):
    v1 = client.get("/api/schedule").json()
    assert v1["version"] == 1 and v1["status"] == "active"
    now = rt.now()
    s2 = Schedule(id="SCH-2", created_at=now, horizon_start=now, horizon_end=now, jobs=[], solver=SolverName.CP_SAT)
    v2 = rt.c.store.propose(s2, "test")
    rt.c.store.activate(v2.version)
    assert client.get("/api/schedule").json()["version"] == 2
    assert len(client.get("/api/schedule/versions").json()) == 2
    r = client.post("/api/schedule/rollback", json={})
    assert r.status_code == 200 and r.json()["version"] == 1
    assert client.get("/api/schedule").json()["version"] == 1
    assert client.post("/api/schedule/rollback", json={"version": 99}).status_code == 404
    assert client.get("/api/audit", params={"kind": "rollback"}).json()[0]["payload"] == \
        {"from_version": 2, "to_version": 1}


def test_capacity_orders_shifts_labor(client):
    cap = client.get("/api/capacity").json()
    assert [(r["small"], r["medium"], r["large"]) for r in cap["rows"]] == [(6, 5, 3), (6, 5.9, 4.5), (7, 6, 4.9)]
    orders = client.get("/api/orders").json()
    assert "#A-311" in [o["id"] for o in orders]
    shifts = client.get("/api/shifts").json()
    assert {s["team"] for s in shifts} >= {"curing", "body"}
    lab = client.get("/api/labor").json()
    assert set(lab) == {"shifts", "needs", "load", "demolds", "gaps"} and len(lab["load"]) == len(shifts)


# ---------------------------------------------------------------- audit, sop
def test_audit_tail_since_seq(client):
    all_ = client.get("/api/audit").json()
    assert all_ and all_[0]["kind"] == "system"
    last = all_[-1]["seq"]
    scenario(client, "normal")
    newer = client.get("/api/audit", params={"since_seq": last}).json()
    assert newer and all(e["seq"] > last for e in newer)
    assert client.get("/api/audit", params={"limit": 1}).json()[0]["seq"] == 1


def test_sop_search(client):
    hits = client.get("/api/sop/search", params={"q": "硫化機溫度下降", "k": 2}).json()
    assert 1 <= len(hits) <= 2 and hits[0]["sop_id"].startswith("SOP-")
    assert client.get("/api/sop/search").status_code == 422


# ---------------------------------------------------------------- sim control
def test_sim_control_and_faults(client, rt):
    assert client.post("/api/sim/control", json={"action": "pause"}).json()["paused"] is True
    assert client.post("/api/sim/control", json={"action": "play"}).json()["paused"] is False
    assert client.post("/api/sim/control", json={"action": "speed", "speed": 60}).json()["speed"] == 60
    assert client.post("/api/sim/control", json={"action": "speed"}).status_code == 422
    assert client.post("/api/sim/control", json={"action": "speed", "speed": -1}).status_code == 422
    assert client.post("/api/sim/control", json={"action": "scenario"}).status_code == 422
    assert client.post("/api/sim/control", json={"action": "bogus"}).status_code == 422
    names = {s["name"] for s in client.get("/api/sim/scenarios").json()}
    assert {"press7_temp_drop", "open_mill_overheat"} <= names
    r = client.post("/api/sim/fault", json={"machine_id": "CURE-03", "kind": "temp_step", "params": {"target": 110}})
    assert r.status_code == 200 and r.json()["fault"]["id"].startswith("F-API-")
    assert client.post("/api/sim/fault", json={"machine_id": "X", "kind": "temp_step"}).status_code == 404
    step(client, 2)
    assert client.get("/api/machines/CURE-03").json()["state"]["last_reading"]["temp_c"] == 110
    assert client.post("/api/sim/clear", json={"machine_id": "CURE-03"}).json()["ok"] is True
    t0 = client.get("/api/health").json()["factory_time"]
    assert client.post("/api/sim/control", json={"action": "reset"}).status_code == 200
    assert client.get("/api/health").json()["factory_time"] <= t0
    assert client.post("/api/sim/network", json={"network": "wan_down"}).json()["network"] == "wan_down"
    assert client.get("/api/snapshot").json()["header"]["network"] == "wan_down"
    assert client.post("/api/sim/step", json={"seconds": 0}).status_code == 422


# ---------------------------------------------------------------- websocket
def test_websocket_stream(client):
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and len(first["data"]["machines"]) == 28
        assert ws.receive_json()["type"] == "clock"
        scenario(client, "press7_temp_drop")
        step(client, 90)
        seen = set()
        for _ in range(400):
            msg = ws.receive_json()
            seen.add(msg["type"])
            if {"machines", "anomaly", "event"} <= seen:
                break
        assert {"machines", "anomaly", "event", "audit"} <= seen, seen


def test_websocket_two_clients_and_disconnect(client, rt):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        assert a.receive_json()["type"] == "snapshot" and b.receive_json()["type"] == "snapshot"
    # both closed: publishing must not fail and no subscriber queues leak
    step(client, 1)
    assert len(rt.bus._subs) == 0


# ---------------------------------------------------------------- web
def test_web_pages(client):
    for path in ("/", "/phone"):
        r = client.get(path)
        assert r.status_code == 200 and "<" in r.text


# ---------------------------------------------------------------- runtime loop
async def test_background_loop_advances_time(tmp_path):
    rt = Runtime(_settings(tmp_path, sim_speed=50), use_real=False, tick_wall_s=0.05)
    t0 = rt.now()
    await rt.start()
    await asyncio.sleep(0.5)
    await rt.stop()
    adv = (rt.now() - t0).total_seconds()
    assert 10 <= adv <= 40, adv
    assert rt.bus.history and rt.bus.history[-1].type in (WsType.MACHINES, WsType.CLOCK, WsType.SNAPSHOT)


async def test_pause_stops_time(tmp_path):
    rt = Runtime(_settings(tmp_path, sim_speed=50), use_real=False, tick_wall_s=0.05)
    rt.pause()
    t0 = rt.now()
    await rt.start()
    await asyncio.sleep(0.3)
    await rt.stop()
    assert rt.now() == t0


async def test_safety_runs_before_detection_in_same_tick(tmp_path):
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    from warroom.contracts import FaultKind, FaultSpec
    rt.inject_fault(FaultSpec(id="f", machine_id="A-MIX", kind=FaultKind.TEMP_STEP, params={"target": 125}))
    await rt.tick(1.0)
    assert [a.machine_id for a in rt.safety_log] == ["A-MIX"]
    kinds = [e.kind for e in rt.audit.tail(limit=1000)]
    assert kinds.index(AuditKind.SAFETY_ACTION) < kinds.index(AuditKind.ANOMALY)


async def test_crashing_state_machine_does_not_kill_loop(tmp_path):
    rt = Runtime(_settings(tmp_path, scenario="press7_temp_drop"), use_real=False, run_loop=False)

    async def boom(anomaly):
        raise RuntimeError("agent exploded")
    rt.c.state_machine.handle = boom
    await rt.run_seconds(90)
    await rt.wait_events()
    msgs = [e.payload.get("msg") for e in rt.audit.tail(limit=5000) if e.kind is AuditKind.SYSTEM]
    assert "event task crashed" in msgs
    await rt.run_seconds(5)       # still ticking


# ---------------------------------------------------------------- real modules (when siblings are done)
REAL_NEEDED = ("sim", "detector", "safety", "approvals", "audit", "tools", "gate", "commander", "state_machine")


def _real_runtime(tmp_path, scenario_name) -> Runtime:
    rt = Runtime(_settings(tmp_path, scenario=scenario_name), run_loop=False)
    missing = {k: v for k, v in rt.degraded.items() if k in REAL_NEEDED}
    if missing:
        pytest.skip(f"sibling modules not ready: {missing}")
    return rt


async def test_real_press7_end_to_end(tmp_path):
    rt = _real_runtime(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)
    await rt.start()
    try:
        approved = []
        for _ in range(25 * 60):
            await rt.tick(1.0, publish=False)
            for req in rt.approvals.pending():
                from warroom.contracts import ApprovalDecision, DecisionKind
                opt = next((o for o in req.options if "夜班" in o.label), req.options[0] if req.options else None)
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                     option_id=opt.id if opt else None, reason="test",
                                                     decided_at=rt.now()))
                approved.append(req.id)
            await asyncio.sleep(0)
        await rt.wait_events(10)
        evs = rt.events()
        assert any(e.machine_id == "CURE-07" for e in evs), [e.id for e in evs]
        assert any(a.machine_id == "CURE-07" for a in rt.safety_log)
        snap = rt.snapshot()
        assert snap.header.factory_time > datetime(2026, 1, 1, tzinfo=F.TZ)
        assert rt.audit.verify_chain()
    finally:
        await rt.stop()


async def test_sim_sleep_wakes_on_exact_sim_second(tmp_path):
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    t0 = rt.now()
    woke = []

    async def sleeper():
        await rt.sim_sleep(30)
        woke.append((rt.now() - t0).total_seconds())
    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)
    await rt.run_seconds(29)
    assert woke == []
    await rt.run_seconds(5)
    await task
    assert woke == [30.0]
    # a scenario reload cancels pending sleepers instead of leaking them
    t2 = asyncio.create_task(rt.sim_sleep(1000))
    await asyncio.sleep(0)
    await rt.load_scenario(ScenarioName.NORMAL)
    with pytest.raises(asyncio.CancelledError):
        await t2


async def test_real_open_mill_page2_focus(tmp_path):
    rt = _real_runtime(tmp_path, ScenarioName.OPEN_MILL_OVERHEAT.value)
    await rt.start()
    try:
        focus = None
        for _ in range(30 * 60):
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0)
            f = rt.focus_panel()
            if f and f.machine_id == "A-BIGOPEN" and f.pending:
                focus = f
                break
        assert focus is not None, "no page-2 focus panel with a pending approval"
        assert focus.title == "異常：A 線大開煉機　溫度過高"
        assert focus.auto_done, "R1 maintenance notify should show as 已自動"
        assert focus.schedule_impact.startswith("無")
        assert not [a for a in rt.safety_log if a.machine_id == "A-BIGOPEN"]
    finally:
        await rt.stop()


async def test_runtime_drives_real_state_machine_page11(tmp_path, monkeypatch):
    """Real sim/detector/safety/agent/store through Runtime; while warroom.tools is still a stub the
    registry is the agent builder's page-11 FakeRegistry (reads come from the real sim)."""
    import warroom.tools as tools_mod
    try:
        tools_mod.build_registry(tools_mod.ToolContext())
        real_tools = True
    except NotImplementedError:
        real_tools = False
    if not real_tools:
        from tests.agent_fakes import FakeRegistry
        monkeypatch.setattr(tools_mod, "build_registry", lambda ctx: FakeRegistry(ctx.sim, ctx.store))
    rt = Runtime(_settings(tmp_path, scenario="press7_temp_drop"), run_loop=False)
    missing = {k: v for k, v in rt.degraded.items() if k in REAL_NEEDED}
    if missing:
        pytest.skip(f"sibling modules not ready: {missing}")
    from warroom.contracts import ApprovalDecision, DecisionKind, EventState
    await rt.start()
    decided = []
    try:
        app = create_app(rt)
        for _ in range(25 * 60):
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0)
            for req in rt.approvals.pending():
                opt = next((o for o in req.options if "夜班" in o.label), None)
                rt.approvals.decide(ApprovalDecision(
                    request_id=req.id, decision=DecisionKind.MODIFY if opt else DecisionKind.APPROVE,
                    option_id=opt.id if opt else None, reason="不跨組調人" if opt else "ok", decided_at=rt.now()))
                decided.append((rt.now().strftime("%H:%M"), req.title, [o.label for o in req.options]))
        await rt.wait_events(5)
        ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
        texts = " | ".join(t.text for t in ev.timeline)
        assert decided, "no approval reached the supervisor"
        assert any(h.status.value == "rejected" for h in ev.hypotheses), texts
        assert ev.safety_actions or any(a.machine_id == "CURE-07" for a in rt.safety_log)
        assert ev.state in (EventState.CLOSED, EventState.SAFE_STOP, EventState.VERIFY,
                            EventState.AWAIT_APPROVAL, EventState.EXECUTE), ev.state
        # the API serves the same live event
        with TestClient(app) as c:
            got = c.get(f"/api/events/{ev.id}").json()
            assert got["id"] == ev.id and got["timeline"]
            f = c.get(f"/api/events/{ev.id}/focus").json()
            assert f["title"].startswith("異常：7 號硫化機")
        assert rt.audit.verify_chain()
        print("\nDECIDED", decided, "\nSTATE", ev.state.value, "\nTIMELINE", texts)
    finally:
        await rt.stop()


async def test_real_press7_schedule_impact_not_page2_text(tmp_path):
    """Press-7 focus must say 計算中… until the solver returns, never the page-2 open-mill #07 line."""
    rt = _real_runtime(tmp_path, ScenarioName.PRESS7_TEMP_DROP.value)
    await rt.start()
    try:
        seen = []
        for _ in range(25 * 60):
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0)
            f = rt.focus_panel()
            if f and f.machine_id == "CURE-07" and (not seen or seen[-1] != f.schedule_impact):
                seen.append(f.schedule_impact)
            if rt.approvals.pending():
                break
        assert seen, "no CURE-07 focus panel"
        assert not any("大胎 #07" in s for s in seen), seen
        assert seen[0] == "計算中…", seen
        assert any("9 號" in s for s in seen), seen     # #A-311 moved to press 9 once solved
    finally:
        await rt.stop()
