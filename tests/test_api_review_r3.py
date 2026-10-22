"""Review round 3 fixes for warroom/api: a re-run replays page 11 fresh (only a genuine restart
resumes), Excel orders/shifts reach the running system, the schedule impact only shows what is in
force, safety actions survive an audit failure, the PIN slow lane never refuses a paired phone,
and behind Docker NAT the on-site seat is a paired kiosk, not a source address."""
from __future__ import annotations

import asyncio
import errno
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from warroom import factory as F
from warroom.api import Runtime, create_app
from warroom.api.app import PinGuard
from warroom.config import ROOT, Settings
from warroom.contracts import (
    TERMINAL_STATES,
    ApprovalDecision,
    DecisionKind,
    MachineStatus,
    SafetyActionKind,
    ScenarioName,
    SensorReading,
    ToolCall,
    ToolName,
)

REAL_NEEDED = ("sim", "detector", "safety", "approvals", "audit", "tools", "gate", "commander", "state_machine",
               "store")
P11 = ScenarioName.PRESS7_TEMP_DROP.value


def _settings(tmp_path, scenario="normal", **kw) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock", **kw)


def _real(tmp_path, scenario="normal", **kw) -> Runtime:
    skw = {k: kw.pop(k) for k in list(kw) if k in Settings.model_fields}
    rt = Runtime(_settings(tmp_path, scenario, **skw), run_loop=False, **kw)
    missing = {k: v for k, v in rt.degraded.items() if k in REAL_NEEDED}
    if missing:
        pytest.skip(f"sibling modules not ready: {missing}")
    return rt


@pytest.fixture(autouse=True)
def _no_resume_env(monkeypatch):
    for k in ("WARROOM_RESUME", "WARROOM_LOOPBACK_LOCAL", "WARROOM_KIOSK_TOKEN", "WARROOM_LOCAL_HOSTS",
              "WARROOM_SUPERVISOR_PIN", "WARROOM_IN_CONTAINER", "ORDERS_XLSX"):
        monkeypatch.delenv(k, raising=False)


def _texts(rt: Runtime) -> list[str]:
    return [t.text for ev in rt.events() for t in ev.timeline]


async def _run_p11(rt: Runtime, seconds: int = 1500, decide=None) -> None:
    """The CLI demo loop: tick, auto-approve (or `decide(req)`), let event tasks run."""
    for _ in range(seconds):
        await rt.tick(1.0, publish=False)
        await asyncio.sleep(0)
        for req in rt.approvals.pending():
            if decide is not None:
                decide(req)
                continue
            opt = next((o for o in req.options if o.recommended), req.options[0] if req.options else None)
            rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                 option_id=opt.id if opt else None, reason="test", via="auto",
                                                 decided_at=rt.now()))
    await rt.wait_events(5)


def _assert_page11(rt: Runtime) -> None:
    texts = " ".join(_texts(rt))
    assert "#A-311 交期 3 天後" in texts, texts[:2000]               # S2
    assert "交期全部滿足" in texts, texts[:2000]                      # S3: round 1 keeps #A-311 on press 7
    assert any(a.machine_id == "CURE-07" and a.action is SafetyActionKind.CURE_HOLD for a in rt.safety_log)


# ---------------------------------------------------------------- 1. re-run replays page 11 fresh
async def test_page11_twice_across_two_runtimes_matches_page11_both_times(tmp_path, monkeypatch):
    # page 11's story (H1 rejected → 7 號機停用) at the real repair durations; with the demo's fast repair VERIFY
    # waits for the crew's 檢修 and the event closes 「已修復」 instead (tests/test_fast_repair.py)
    monkeypatch.setenv("REPAIR_FAST_S", "0")
    for run in (1, 2):
        rt = _real(tmp_path, P11)
        assert rt.resumed is False and rt.builder.reopened_store is False, run
        assert rt.persisted_holds() == {} and [v.version for v in rt.c.store.versions()] == [1], run
        assert rt.sim.machine("CURE-07").status is not MachineStatus.OUT_OF_SERVICE
        assert rt.clock.now() == datetime(2026, 10, 3, 2, 12, tzinfo=F.TZ)
        panel = {o.label: o for o in rt.snapshot().orders}
        # the dataset's tyres de-molded by 02:12 (M 9/100, L 7/100) scale the page-2 estimates (1241 h / 2162 h)
        assert panel["中胎"].remaining_h == round(1241 * 91 / 88, 1) and panel["大胎"].remaining_h == round(2162 * 93 / 92, 1), panel
        await rt.start()
        try:
            await _run_p11(rt)
            _assert_page11(rt)
            assert rt.sim.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
        finally:
            await rt.stop()
        assert not (tmp_path / "sched" / "running.json").exists()      # clean stop clears the marker


async def test_unclean_exit_resumes_clock_holds_and_store_without_replaying(tmp_path, monkeypatch):
    # page 11's story (H1 rejected → 7 號機停用) at the real repair durations; with the demo's fast repair VERIFY
    # waits for the crew's 檢修 and the event closes 「已修復」 instead (tests/test_fast_repair.py)
    monkeypatch.setenv("REPAIR_FAST_S", "0")
    rt = _real(tmp_path, P11)
    await rt.start()
    await _run_p11(rt)
    ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
    assert ev.state in TERMINAL_STATES
    rt._sync_holds()
    rt._persist_plant(force=True)
    t_crash = rt.clock.now()
    active = rt.c.store.current().version
    assert (tmp_path / "sched" / "running.json").is_file()           # no stop(): a crash / kill -9
    rt.ingest.close()

    rt2 = _real(tmp_path, P11)
    assert rt2.resumed is True and rt2.builder.reopened_store is True
    assert rt2.clock.now() == t_crash                                # the clock continues, no 02:12 replay
    assert rt2.c.store.current().version == active
    assert rt2.sim.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
    await rt2.start()
    try:
        await _run_p11(rt2, 600)
        assert not rt2.events(), [e.id for e in rt2.events()]        # the scripted fault is not replayed
        assert rt2.c.store.current().version == active
    finally:
        await rt2.stop()
    # an explicit override wins over a stale marker
    (tmp_path / "sched" / "running.json").write_text("{}", encoding="utf-8")
    rt3 = _real(tmp_path, P11, resume=False)
    assert rt3.resumed is False and [v.version for v in rt3.c.store.versions()] == [1]
    assert rt3.persisted_holds() == {}


def test_watchdog_resume_request_is_consumed(tmp_path):
    rt = _real(tmp_path, P11)
    rt._persist_plant(force=True)
    (tmp_path / "sched" / "resume.request").write_text("", encoding="utf-8")
    rt2 = _real(tmp_path, P11)
    assert rt2.resumed is True and not (tmp_path / "sched" / "resume.request").exists()
    rt3 = _real(tmp_path, P11)
    assert rt3.resumed is False


# ---------------------------------------------------------------- 2. Excel import is live
def test_excel_orders_and_shifts_reach_sim_tools_panel_and_schedule(tmp_path):
    xlsx = ROOT / "data" / "orders_seed.xlsx"
    if not xlsx.is_file():
        pytest.skip("data/orders_seed.xlsx missing")
    rt = _real(tmp_path, P11, orders_xlsx=xlsx)
    assert "orders_xlsx" not in rt.degraded
    by_id = {o.id: o for o in rt.sim.orders()}
    # the workbook names the plant's own orders (data/make_orders_seed.py) with its own customers
    assert {"SO-002", "SO-003", "#A-311"} <= set(by_id) and by_id["SO-002"].customer == "金旺來工業社"
    assert by_id["#A-311"].size.value == "S" and by_id["#A-311"].qty == 1
    assert any(s.id == "1003-body-夜班" for s in rt.sim.shifts())
    res = rt.c.tools.call(ToolCall(id="t1", tool=ToolName.QUERY_ORDERS_SHIFTS, args={}))
    assert res.ok and "金旺來工業社" in [o["customer"] for o in res.data["orders"]], res
    assert "10/03 夜班缺 1 人" in res.data["gaps"]
    panel = {o.label: (o.done, o.total) for o in rt.snapshot().orders}
    assert panel == {"小胎": (21, 100), "中胎": (9, 100), "大胎": (7, 100)}      # dataset tyres de-molded by 02:12
    ids = [j.id for j in rt.c.store.current().schedule.jobs]
    assert "#A-311" in {j.order_id for j in rt.c.store.current().schedule.jobs} and len(ids) == len(set(ids))


def test_missing_excel_is_reported_and_sim_seed_kept(tmp_path):
    rt = _real(tmp_path, P11, orders_xlsx=tmp_path / "nope.xlsx")
    assert "orders_xlsx" in rt.degraded
    assert "SO-002" in {o.id for o in rt.sim.orders()}


# ---------------------------------------------------------------- 3 + 7. schedule impact
async def test_impact_matches_phone_in_round2_and_shows_kept_version_after_safe_stop(tmp_path, monkeypatch):
    # page 11's story (H1 rejected → 7 號機停用) at the real repair durations; with the demo's fast repair VERIFY
    # waits for the crew's 檢修 and the event closes 「已修復」 instead (tests/test_fast_repair.py)
    monkeypatch.setenv("REPAIR_FAST_S", "0")
    from warroom.agent.narrate import moves_text
    rt = _real(tmp_path, P11)
    round2_seen: list[tuple[str, list[str]]] = []

    def decide(req):
        if rt.now() < datetime(2026, 10, 3, 2, 31, tzinfo=F.TZ):        # round 1: approve
            opt = next((o for o in req.options if o.recommended), req.options[0] if req.options else None)
            rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                 option_id=opt.id if opt else None, reason="t", decided_at=rt.now()))
            return
        ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
        solves, _, _ = rt._schedule_rounds(ev)
        round2_seen.append((rt.focus_panel(ev).schedule_impact, moves_text(solves[-1][1].get("moves"))))
        rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.REJECT, reason="不跨組調人",
                                             decided_at=rt.now()))

    await rt.start()
    try:
        await _run_p11(rt, 1800, decide=decide)
        ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
        assert round2_seen, "no round-2 approval"
        impact2, phone_moves = round2_seen[0]
        assert "待核准" in impact2, impact2
        for phrase in phone_moves:                       # same grouping as the phone card (count per press)
            assert phrase in impact2, (phrase, impact2)
        solves, activated, _ = rt._schedule_rounds(ev)
        assert activated, "round 1 activated nothing"
        assert ev.state in TERMINAL_STATES
        final = rt.focus_panel(ev).schedule_impact
        v_kept = activated[-1]
        assert rt.c.store.current().version == v_kept
        kept_moves = moves_text(dict(solves)[v_kept].get("moves"))
        assert "#A-311 改 9 號" not in kept_moves        # round 1 never moves #A-311 (dataset EV-2)
        assert "待核准" not in final and f"v{v_kept}" in final, final
        for phrase in kept_moves:
            assert phrase in final, (phrase, final)
        if solves[-1][0] not in activated:               # the rejected proposal is named as not in force
            assert f"v{solves[-1][0]} 未生效" in final, final
            for phrase in moves_text(solves[-1][1].get("moves")):
                if phrase not in kept_moves:
                    assert phrase not in final, (phrase, final)
    finally:
        await rt.stop()


# ---------------------------------------------------------------- 4. red path vs audit failure
def test_audit_failure_never_drops_or_latches_away_safety_actions(tmp_path):
    rt = _real(tmp_path)
    readings = []
    for mid in ("A-MIX", "B-MIX"):
        m = rt.sim.machine(mid)
        assert m.compound_stage.value == "unvulcanized"
        readings.append(SensorReading(ts=rt.now(), machine_id=mid, temp_c=126.0, seq=10_000))
    real_append = rt.audit.append

    def broken(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    rt.audit.append = broken
    acts = rt.safety_pass(readings)                       # must not raise (1st sample: confirm pending)
    nxt = [r.model_copy(update={"seq": r.seq + 1}) for r in readings]
    acts += rt.safety_pass(nxt)                           # 2nd violating sample confirms both
    assert {a.machine_id for a in acts} == {"A-MIX", "B-MIX"}
    for mid in ("A-MIX", "B-MIX"):
        assert rt.sim.machine(mid).status is MachineStatus.STOPPED, mid
    assert all(a.latency_ms < 200 for a in acts)
    assert len(rt._safety_unrecorded) == 2 and rt.warning_light() is True
    rt.audit.append = real_append
    rt.safety_pass([])                                    # next pass retries the records
    assert rt._safety_unrecorded == [] and rt.warning_light() is False
    logged = [e for e in rt.audit.tail(0, 100_000) if e.kind.value == "safety_action"]
    assert {e.payload["machine_id"] for e in logged} >= {"A-MIX", "B-MIX"}


# ---------------------------------------------------------------- 5. PIN slow lane vs real phone
def test_wrong_pins_from_many_hosts_never_lock_out_the_paired_phone(tmp_path):
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    t = [0.0]
    pin = "Xk93-pq7Lm2"
    app = create_app(rt, supervisor_pin=pin, pin_guard=PinGuard(now=lambda: t[0]), container=False)
    body = {"network": "online"}
    with TestClient(app, client=("10.0.0.50", 50000)) as phone:
        r = phone.post("/api/sim/network", json=body, headers={"X-Supervisor-Pin": pin})
        assert r.status_code == 200 and phone.cookies.get("warroom_device")
        for h in range(4):                                # 4 other hosts x 5 wrong PINs -> slow lane
            with TestClient(app, client=(f"10.0.0.{60 + h}", 50000)) as bad:
                for _ in range(5):
                    bad.post("/api/sim/network", json=body, headers={"X-Supervisor-Pin": "guess"})
        assert app.state.access is not None
        r = phone.post("/api/sim/network", json=body, headers={"X-Supervisor-Pin": pin})
        assert r.status_code == 200, r.text                # paired phone: never refused
    with TestClient(app, client=("10.0.0.70", 50000)) as new_phone:
        r = new_phone.post("/api/sim/network", json=body, headers={"X-Supervisor-Pin": pin})
        assert r.status_code == 200, r.text                # unpaired, correct PIN: gets the slot
    with TestClient(app, client=("10.0.0.71", 50000)) as other:
        r = other.post("/api/sim/network", json=body, headers={"X-Supervisor-Pin": pin})
        assert r.status_code == 429 and int(r.headers["Retry-After"]) <= 11   # slowed, not locked out
        t[0] += 10
        r = other.post("/api/sim/network", json=body, headers={"X-Supervisor-Pin": pin})
        assert r.status_code == 200


# ---------------------------------------------------------------- 6. Docker NAT: kiosk, not source IP
def test_in_container_loopback_is_not_the_seat_and_kiosk_pairs(tmp_path):
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    app = create_app(rt, supervisor_pin="", container=True)
    tok = app.state.access.kiosk_token
    assert tok, "a kiosk token is generated when nobody could approve"
    body = {"network": "online"}
    with TestClient(app) as c:                             # 'testclient' = loopback-like peer
        r = c.post("/api/sim/network", json=body)
        assert r.status_code == 403 and "/kiosk" in r.json()["detail"]
        assert c.get("/kiosk", params={"token": "wrong"}).status_code == 401
        r = c.get("/kiosk", params={"token": tok}, follow_redirects=False)
        assert r.status_code == 303 and c.cookies.get("warroom_kiosk") == tok
        assert c.post("/api/sim/network", json=body).status_code == 200
    with TestClient(app, client=("192.168.65.1", 1)) as gw:   # Docker Desktop gateway: still not the seat
        assert gw.post("/api/sim/network", json=body).status_code == 403
    # outside a container, loopback stays the on-site seat
    app2 = create_app(rt, supervisor_pin="", container=False)
    assert app2.state.access.kiosk_token is None
    with TestClient(app2) as c:
        assert c.post("/api/sim/network", json=body).status_code == 200


def test_host_network_container_keeps_loopback_seat_when_told(tmp_path, monkeypatch):
    """Spark compose runs the app with network_mode: host and WARROOM_LOOPBACK_LOCAL=1: loopback is
    the real touchscreen there, so it stays the seat even though the app is in a container."""
    monkeypatch.setenv("WARROOM_LOOPBACK_LOCAL", "1")
    rt = Runtime(_settings(tmp_path), use_real=False, run_loop=False)
    app = create_app(rt, supervisor_pin="Xk93-pq7Lm2", container=True)
    with TestClient(app) as c:
        assert c.post("/api/sim/network", json={"network": "online"}).status_code == 200
