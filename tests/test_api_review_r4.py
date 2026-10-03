"""Review round 4 fixes for warroom/api: the page-9 弱網 tier delays only remote pushes, the Excel
order book runs the page-11 story (unique job ids, workbook = plant orders), a rollback never brings
back a cure time shorter than the formula and lists the staffing it revokes, panel ① takes its
stretch from the solver's plans (and changes nothing while the press is only held), and a
withdrawn staffing pick shows as ↺ 已撤回, never as ✓ 已核准."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.api.app import WeakLinkBuffer
from warroom.config import ROOT, Settings
from warroom.contracts import (
    ApprovalDecision,
    DecisionKind,
    MachineStatus,
    Schedule,
    ScenarioName,
    SolverName,
    StaffingChange,
    TeamId,
    WsMessage,
    WsType,
)

REAL_NEEDED = ("sim", "detector", "safety", "approvals", "audit", "tools", "gate", "commander", "state_machine",
               "store")
P11 = ScenarioName.PRESS7_TEMP_DROP.value
XLSX = ROOT / "data" / "orders_seed.xlsx"


def _settings(tmp_path, scenario="normal", **kw) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock", **kw)


def _real(tmp_path, scenario="normal", **kw) -> Runtime:
    skw = {k: kw.pop(k) for k in list(kw) if k in Settings.model_fields}
    rt = Runtime(_settings(tmp_path, scenario, **skw), run_loop=False, resume=False, **kw)
    missing = {k: v for k, v in rt.degraded.items() if k in REAL_NEEDED}
    if missing:
        pytest.skip(f"sibling modules not ready: {missing}")
    return rt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("WARROOM_RESUME", "WARROOM_LOOPBACK_LOCAL", "WARROOM_KIOSK_TOKEN", "WARROOM_LOCAL_HOSTS",
              "WARROOM_SUPERVISOR_PIN", "WARROOM_IN_CONTAINER", "ORDERS_XLSX", "WARROOM_WEAK_PUSH_DELAY_S"):
        monkeypatch.delenv(k, raising=False)


def _texts(rt: Runtime) -> str:
    return " | ".join(t.text for ev in rt.events() for t in ev.timeline)


async def _ticks(rt: Runtime, seconds: int, decide=None) -> None:
    for _ in range(seconds):
        await rt.tick(1.0, publish=False)
        await asyncio.sleep(0)
        if decide is not None:
            for req in rt.approvals.pending():
                decide(req)


def _pick(rt: Runtime, label_part: str, kind=DecisionKind.APPROVE, reason="test"):
    def decide(req):
        opt = next((o for o in req.options if label_part in o.label), None)
        if opt is None:
            return
        rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=kind, option_id=opt.id, reason=reason,
                                             via="phone", decided_at=rt.now()))
    return decide


# ---------------------------------------------------------------- 1. page 9 弱網
def _msg(t: WsType, n: int = 0) -> WsMessage:
    from datetime import datetime

    from warroom import factory as F
    return WsMessage(type=t, ts=datetime(2026, 10, 3, 2, 14, n, tzinfo=F.TZ), data={"n": n})


def test_weak_link_buffer_delays_batches_and_coalesces_full_state():
    b = WeakLinkBuffer(5.0)
    assert b.wait_s(0.0) is None and b.due(100.0) == []
    b.push(_msg(WsType.MACHINES, 1), 10.0)
    b.push(_msg(WsType.APPROVAL_REQUEST, 2), 11.0)
    b.push(_msg(WsType.MACHINES, 3), 12.0)
    b.push(_msg(WsType.AUDIT, 4), 13.0)
    assert b.wait_s(12.0) == pytest.approx(3.0)
    assert b.due(14.9) == []                                     # 5 s after the first held message
    out = b.due(15.0)
    assert [(m.type, m.data["n"]) for m in out] == [(WsType.APPROVAL_REQUEST, 2), (WsType.MACHINES, 3),
                                                     (WsType.AUDIT, 4)]   # events kept, machines newest only
    assert b.coalesced == 1 and len(b) == 0 and b.wait_s(16.0) is None


def test_weak_network_delays_remote_ws_push_but_not_the_on_site_screen(tmp_path):
    rt = _real(tmp_path)
    # TestClient arrives as host "testclient": remote once it is not in the local set
    with TestClient(create_app(rt, demo_controls=True, weak_push_delay_s=1.5, supervisor_pin="12345678",
                               local_hosts=frozenset({"127.0.0.1"}))) as c:
        pin = {"X-Supervisor-Pin": "12345678"}
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "snapshot" and ws.receive_json()["type"] == "clock"
            t0 = time.monotonic()
            r = c.post("/api/sim/network", json={"network": "weak"}, headers=pin)
            assert r.status_code == 200 and r.json()["push_delay_s"] == 1.5 and r.json()["phone_approvals"]
            got = [ws.receive_json(), ws.receive_json()]         # audit row + the tier change's snapshot
            assert time.monotonic() - t0 >= 1.4, "remote push was not delayed on a weak link"
            snap = next(m for m in got if m["type"] == "snapshot")
            assert snap["data"]["header"]["network"] == "weak"
            t0 = time.monotonic()
            c.post("/api/sim/network", json={"network": "online"}, headers=pin)
            got = [ws.receive_json(), ws.receive_json()]
            assert time.monotonic() - t0 < 1.4
            assert next(m for m in got if m["type"] == "snapshot")["data"]["header"]["network"] == "online"
        health = c.get("/api/health/components").json()
        assert health["weak_link"]["push_delay_s"] == 1.5 and health["weak_link"]["batches"] >= 1
    rt2 = _real(tmp_path / "local")
    with TestClient(create_app(rt2, demo_controls=True, weak_push_delay_s=5.0)) as c:   # testclient = on-site seat
        with c.websocket_connect("/ws") as ws:
            ws.receive_json(), ws.receive_json()
            t0 = time.monotonic()
            c.post("/api/sim/network", json={"network": "weak"})
            got = [ws.receive_json(), ws.receive_json()]
            assert time.monotonic() - t0 < 4.0
            assert next(m for m in got if m["type"] == "snapshot")["data"]["header"]["network"] == "weak"


# ---------------------------------------------------------------- 2. Excel order book runs page 11
def test_seeded_schedule_has_unique_job_ids_and_check_names_duplicates(tmp_path):
    from warroom.schedule.solver import check_schedule
    from warroom.contracts import SolveRequest
    rt = _real(tmp_path, P11, orders_xlsx=XLSX)
    sched = rt.c.store.current().schedule
    ids = [j.id for j in sched.jobs]
    assert len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1]
    dup = sched.model_copy(update={"jobs": list(sched.jobs) + [sched.jobs[-1]]})
    errs = check_schedule(dup, SolveRequest(now=rt.now(), orders=rt.sim.orders(), shifts=rt.sim.shifts()))
    assert any("重複" in e for e in errs) and not any("重疊" in e and sched.jobs[-1].id in e for e in errs), errs


def test_solver_text_never_says_deliveries_met_when_infeasible():
    from warroom.agent.narrate import solver_text
    t = solver_text({"feasible": False, "status": "feasible", "deliveries_met": True,
                     "schedule": {"warnings": ["檢核失敗：CURE-07 J-1 與 J-2 重疊"]}})
    assert "交期全部滿足" not in t and "模擬不可行" in t and "重疊" in t


def test_workbook_orders_match_the_plant(tmp_path):
    if not XLSX.is_file():
        pytest.skip("data/orders_seed.xlsx missing")
    rt = _real(tmp_path, P11, orders_xlsx=XLSX)
    booked = {o.id for o in rt.sim.orders()}
    in_press = {m.cure_job.order_id for m in rt.sim.machines() if m.cure_job is not None}
    assert in_press <= booked, in_press - booked
    a311 = next(o for o in rt.sim.orders() if o.id == "#A-311")
    assert a311.size.value == "L" and a311.qty == 1


async def test_page11_with_excel_reaches_round1_card_and_moves_a311_to_press9(tmp_path):
    if not XLSX.is_file():
        pytest.skip("data/orders_seed.xlsx missing")
    rt = _real(tmp_path, P11, orders_xlsx=XLSX)
    assert "orders_xlsx" not in rt.degraded
    await rt.start()
    try:
        await _ticks(rt, 160)                                    # 02:12 -> 02:14:40, nobody decides
        reqs = rt.approvals.pending()
        assert reqs, _texts(rt)[:3000]
        labels = [o.label for o in reqs[0].options]
        assert "夜班加班" in labels and "從 2 線調 1 人" in labels, labels
        texts = _texts(rt)
        assert "#A-311 移到 9 號機" in texts and "安全中止" not in texts and "不可行" not in texts, texts[:3000]
    finally:
        await rt.stop()


# ---------------------------------------------------------------- 3-5. page 11 B then A, rollback, panel ①
async def test_page11_rollback_progress_and_revoked_staffing(tmp_path):
    rt = _real(tmp_path, P11)
    await rt.start()
    try:
        # round 1: supervisor takes the solver's cross-team move (B)
        await _ticks(rt, 600, _pick(rt, "調"))
        assert rt.c.store.current().version == 2
        # 3. one-click rollback to v1 would plan press 7's tire at 28 h while the formula says 36.5 h
        pv = rt.rollback_preview(None)
        assert pv["to_version"] == 1 and pv["feasible"] is False, pv
        assert pv["cure_conflicts"] and pv["cure_conflicts"][0]["press_id"] == "CURE-07"
        assert any("36.5 h" in r and "28 h" in r for r in pv["reasons"]), pv["reasons"]
        assert pv["staffing_revoked"] and "從 2 線調 1 人" in pv["staffing_revoked"][0]
        assert all(m["from_press"] != m["to_press"] for m in pv["moved"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test") as c:
            r = await c.post("/api/schedule/rollback", json={"confirm": True})     # ASGI client = loopback seat
            assert r.status_code == 409 and "36.5" in r.json()["detail"]["message"], r.text
        assert rt.c.store.current().version == 2

        # 4. 02:28:50 safety core holds press 7; the round-2 card is not decided yet
        await _ticks(rt, 1140 - 600)                             # -> 02:31:00 + a few seconds
        await _ticks(rt, 40)
        assert rt.sim.machine("CURE-07").status is not MachineStatus.OUT_OF_SERVICE
        assert rt.approvals.pending(), "round-2 card should be waiting"
        panel = {o.label: o for o in rt.snapshot().orders}
        assert panel["中胎"].remaining_h == 1241 and panel["大胎"].remaining_h == 2162, panel
        assert "安全核心扣模" in (panel["中胎"].note or "") and "停用：" not in (panel["中胎"].note or "")

        # round 2: supervisor rejects the cross-team move and picks night overtime (A), page 11
        await _ticks(rt, 120, _pick(rt, "夜班加班", DecisionKind.MODIFY, "不跨組調人，改夜班加班"))
        await rt.wait_events(5)
        assert rt.sim.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
        panel = {o.label: o for o in rt.snapshot().orders}
        note = panel["中胎"].note or ""
        assert "依生效排程 v3" in note and "機時" in note and "×" not in note, note
        assert "本週已排訂單交期不受影響" in note and panel["中胎"].remaining_h > 1241

        # 5. the withdrawal is a consequence, not an approval
        focus = rt.snapshot().focus
        assert focus is not None
        assert not any("撤回" in a.label for a in focus.approved), [a.label for a in focus.approved]
        assert focus.revoked == ["從 2 線調 1 人（因主管改選夜班加班）"], focus.revoked
    finally:
        await rt.stop()


def test_approved_rollback_notifies_revoked_staffing(tmp_path):
    rt = _real(tmp_path)
    store, now = rt.c.store, rt.now()
    v1 = store.current()
    ot = StaffingChange(kind="overtime", shift_date=now.date(), shift_name="夜班", to_team=TeamId.BODY,
                        label="夜班加班")
    v2 = store.propose(v1.schedule.model_copy(update={"id": "S2", "staffing_changes": [ot]}), "v2 overtime")
    store.activate(v2.version)
    pv = rt.rollback_preview(None)
    assert pv["feasible"] and pv["staffing_revoked"] == [f"{now:%m/%d} 夜班 夜班加班（{_team(TeamId.BODY)}）"]
    with TestClient(create_app(rt, local_hosts=frozenset({"testclient"}))) as c:
        r = c.post("/api/schedule/rollback", json={"confirm": True})
        assert r.status_code == 200 and r.json()["version"] == v1.version
        appr = [a for a in c.get("/api/approvals").json() if a["id"].startswith("AP-RB-")][-1]
        assert any("撤回人力" in g for g in appr["gaps"]), appr
    notices = [e for e in rt.audit.tail(0, 10_000) if e.payload.get("msg") == "rollback staffing notice"]
    assert notices and "撤回" in notices[-1].payload["text"] and notices[-1].payload["delivered"] is True


def _team(t: TeamId) -> str:
    from warroom import factory as F
    return F.TEAM_LABELS[t]


def test_rollback_preview_does_not_list_same_press_retimes_as_moves(tmp_path):
    rt = _real(tmp_path)
    store, now = rt.c.store, rt.now()
    v1 = store.current()
    jobs = [j.model_copy(update={"mold_in": j.mold_in + timedelta(hours=1), "demold": j.demold + timedelta(hours=1)})
            if not j.frozen and j.mold_in > now else j for j in v1.schedule.jobs]
    v2 = store.propose(Schedule(id="S2", created_at=now, horizon_start=v1.schedule.horizon_start,
                                horizon_end=v1.schedule.horizon_end, jobs=jobs, solver=SolverName.HEURISTIC), "retime")
    store.activate(v2.version)
    pv = rt.rollback_preview(None)
    assert pv["moved"] == [] and pv["retimed_count"] > 0, pv
