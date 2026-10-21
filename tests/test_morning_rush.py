"""Issue #104 一早訂單湧入 (morning_rush), the finals demo, cut to ~3 real minutes at 40×: 07:00–07:07 eight orders
arrive (SO-012 can never make its due date, SO-017 is the rush order), 07:12 page 11 on press 7 (H1 rejected -> H2,
fixed by H2's 停用檢修 also under fast repair); 10:00 the truck leaves. MORNING_RUSH_LEAK=0 drops the leak,
MORNING_RUSH_SAFETY=1 adds the 08:30 Safety Core stop on B 線開煉機 (no false 溫度下降 card while it is stopped).

CJ, 2026-10-22: 「因為我demo 是以一天的開始就是訂單湧進來 然後事件 然後 我還沒想到完整展示流程」.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from warroom import factory as F
from warroom.config import Settings
from warroom.contracts import ApprovalDecision, DecisionKind, EventState, MachineStatus, SafetyActionKind, ScenarioName
from warroom.sim import Factory
from warroom.sim.scenarios import (
    MR_AT_RISK_ORDER,
    MR_ORDERS,
    MR_RUSH_ORDER,
    get_scenario,
)

pytestmark = pytest.mark.timeout(300)
TZ = F.TZ
START = datetime(2026, 10, 23, 6, 59, tzinfo=TZ)


def _at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 23, hh, mm, ss, tzinfo=TZ)


def _run(f: Factory, until: datetime) -> None:
    while f.now() < until:
        f.step(1.0)


def _faults(sc) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = {}
    for st in sc.steps:
        if st.fault is not None:
            out.setdefault(st.fault.machine_id, []).append(START + timedelta(seconds=st.at_offset_s))
    return out


def test_scenario_loads_with_its_beats(monkeypatch):
    monkeypatch.delenv("MORNING_RUSH_LEAK", raising=False)
    monkeypatch.delenv("MORNING_RUSH_SAFETY", raising=False)
    sc = get_scenario(ScenarioName.MORNING_RUSH)
    assert sc.start_at == START
    assert not any(st.plant is not None and st.plant.kind == "shift_start" for st in sc.steps)
    orders = [st for st in sc.steps if st.order is not None]
    rush = [st for st in sc.steps if st.plant is not None and st.plant.kind == "rush_order"]
    assert len(orders) == 7 and len(rush) == 1 and rush[0].plant.order.id == MR_RUSH_ORDER
    assert 6 <= len(orders) + len(rush) <= 10
    assert all(_at(7, 0) <= START + timedelta(seconds=st.at_offset_s) <= _at(7, 7) for st in orders + rush)
    assert _faults(sc) == {"CURE-07": [_at(7, 12), _at(7, 21, 40)]}     # the 3-minute cut: no Safety Core step


def test_leak_and_safety_steps_are_toggleable(monkeypatch):
    monkeypatch.setenv("MORNING_RUSH_LEAK", "0")
    monkeypatch.setenv("MORNING_RUSH_SAFETY", "1")
    f = _faults(get_scenario(ScenarioName.MORNING_RUSH))
    assert f["CURE-07"] == [_at(7, 12)] and f["B-OPEN"] == [_at(8, 30)]


def test_orders_arrive_at_their_times_and_only_the_rush_order_is_a_plant_event():
    f = Factory(seed=7, scenario=ScenarioName.MORNING_RUSH)
    book0 = {o.id for o in f.orders()}
    assert not book0 & {oid for _, oid, *_ in MR_ORDERS}
    for off, oid, _cust, size, qty, days, prio, rush in MR_ORDERS:
        t = START + timedelta(seconds=off)
        _run(f, t - timedelta(seconds=1))
        assert oid not in {o.id for o in f.orders()}, f"{oid} early"
        _run(f, t)
        o = next(o for o in f.orders() if o.id == oid)
        assert (o.size, o.qty, o.due, o.priority) == (size, qty, START.date() + timedelta(days=days), prio)
        fired = f.drain_plant_inputs()
        if rush:
            assert [p.kind for _, p in fired] == ["rush_order"]
        else:
            assert all(p.kind != "rush_order" for _, p in fired)
            feed = [e for e in f.world.events if e.ref == oid]
            assert feed and feed[-1].kind == "order_new" and feed[-1].ts == t


def test_the_flood_is_deterministic():
    def feed() -> list[tuple]:
        f = Factory(seed=7, scenario=ScenarioName.MORNING_RUSH)
        _run(f, _at(7, 21))
        return [(e.ts, e.kind, e.text) for e in f.world.events]
    assert feed() == feed()


async def _run_day(tmp_path, until: datetime, fast_s: float):
    """Mock commander, every card approved as it appears. Returns (events, world, safety log, when SO-012 went at risk,
    when its pre-plan started)."""
    from warroom.api import Runtime
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario="morning_rush", llm_mode="mock", repair_fast_s=fast_s)
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    assert rt.degraded == {}
    await rt.start()
    rt.preplan_on = True
    risk_at = preplan_at = None
    try:
        while rt.now() < until:
            await rt.tick(1.0, publish=False)
            for req in rt.approvals.pending():
                opt = next((o for o in req.options if o.recommended), req.options[0])
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE, option_id=opt.id,
                                                     reason="test", via="auto", decided_at=rt.now()))
            if risk_at is None and MR_AT_RISK_ORDER in rt.at_risk_orders():
                risk_at = rt.now()
            if preplan_at is None and MR_AT_RISK_ORDER in rt._preplanned:
                preplan_at = rt.now()
        await rt.wait_events(5)
        return rt.events(), rt.world_snapshot(), list(rt.safety_log), risk_at, preplan_at, rt.PREPLAN_EVERY_S, rt
    finally:
        await rt.stop()


async def test_morning_rush_end_to_end(tmp_path, monkeypatch):
    """The 3-minute cut under fast repair: the rush re-plan makes SO-012 a 交期風險 item and the pre-plan starts on it
    without anyone asking; page 11 fires at 07:12; H1's approved 檢修 finishes fast but the press keeps leaking, so
    VERIFY still reads 119.5 °C and H1 is rejected; H2's 停用檢修 then fixes it fast; the 10:00 truck leaves."""
    monkeypatch.delenv("MORNING_RUSH_LEAK", raising=False)
    monkeypatch.delenv("MORNING_RUSH_SAFETY", raising=False)
    events, world, safety, risk_at, preplan_at, every, rt = await _run_day(tmp_path, _at(10, 1), 0.05)
    assert risk_at is not None and _at(7, 7) < risk_at < _at(7, 12), risk_at
    assert preplan_at is not None and preplan_at - risk_at <= timedelta(seconds=every), preplan_at
    cmd = next(e for e in events if (e.command or "").startswith(f"{MR_AT_RISK_ORDER} "))
    assert cmd.source == "supervisor" and cmd.opened_at < _at(7, 12)
    rush = next(e for e in events if e.anomaly.message.startswith(f"急單 {MR_RUSH_ORDER}"))
    assert _at(7, 7) <= rush.opened_at < _at(7, 8)
    p11 = next(e for e in events if e.machine_id == "CURE-07")
    assert _at(7, 12) < p11.opened_at < _at(7, 14)
    h1 = p11.hypotheses[0]
    assert h1.status.value == "rejected" and any("119.5" in x for x in h1.evidence), h1.evidence
    assert len(p11.hypotheses) >= 2 and p11.state is EventState.CLOSED
    tasks = [t for t in world.tasks.items if t.event_id == p11.id]
    assert {"檢修", "停用檢修"} <= {t.kind for t in tasks} and all(t.status == "完成" for t in tasks if t.kind.endswith("檢修"))
    assert not [f for f in rt.sim.faults(active_only=False) if f.machine_id == "CURE-07"]     # H2 fixed the press
    assert not [a for a in safety if a.machine_id == "B-OPEN"]
    left = [e for e in world.events if e.kind == "shipment_departed"]
    assert left and left[0].ts == _at(10, 0)
    # one pre-plan per change of SO-012's inputs (here: the start, and press 7 going out of service), never one per
    # schedule version; each reports 「已預先檢查：…」 instead of 「沒有可執行的動作」
    pre = [e for e in events if (e.command or "").startswith(f"{MR_AT_RISK_ORDER} ")]
    assert 1 <= len(pre) <= 2, [(e.id, e.opened_at) for e in pre]
    reports = [a for a in rt.audit.tail(0, 100000) if a.payload.get("orch") == "report" and a.event_id in {e.id for e in pre}]
    assert len(reports) == len(pre) and all(r.payload["text"].startswith(f"已預先檢查：{MR_AT_RISK_ORDER} 明天無法交齊（")
                                            for r in reports), [r.payload["text"] for r in reports]


async def test_preplan_is_not_rerun_for_a_new_eta_alone(tmp_path, monkeypatch):
    """A pre-plan that found nothing to change runs again only when the order's due date, quantity or the presses
    that can cure it change; a new ETA (a schedule version bump) alone does not start another one."""
    monkeypatch.delenv("MORNING_RUSH_LEAK", raising=False)
    from warroom.api import Runtime
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario="morning_rush", llm_mode="mock")
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    await rt.start()
    try:
        while rt.now() < _at(7, 9):                               # the rush re-plan (SO-012 in it) is approved
            await rt.tick(1.0, publish=False)
            for req in rt.approvals.pending():
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                                     option_id=req.options[0].id if req.options else None,
                                                     reason="test", via="auto", decided_at=rt.now()))
        await rt.wait_events(30)
        sig = {"SO-012": "eta 1"}
        rt.at_risk_orders = lambda: dict(sig)

        async def preplan() -> int:
            rt._preplan_at = None
            rt._preplan_orders()
            mine = [e for e in rt.events() if (e.command or "").startswith("SO-012 ")]
            for _ in range(600):                                   # the run needs sim seconds to finish
                if all(e.state in (EventState.CLOSED, EventState.SAFE_STOP) for e in rt.events() if e.id in {m.id for m in mine}):
                    break
                await rt.tick(1.0, publish=False)
            return len(mine)
        assert await preplan() == 1
        ev = next(e for e in rt.events() if (e.command or "").startswith("SO-012 "))
        assert ev.state is EventState.CLOSED and not ev.approvals                   # nothing to change (mock)
        sig["SO-012"] = "eta 2"                                                     # a new ETA only
        assert await preplan() == 1
        o = next(o for o in rt.sim.orders() if o.id == "SO-012")
        rt.sim._orders["SO-012"] = o.model_copy(update={"qty": o.qty + 2})         # the customer adds two tyres
        assert await preplan() == 2
    finally:
        await rt.stop()


async def test_morning_rush_long_cut(tmp_path, monkeypatch):
    """MORNING_RUSH_SAFETY=1: the Safety Core stops B 線開煉機 and, while it is stopped and cooling, no 溫度下降 card."""
    monkeypatch.delenv("MORNING_RUSH_LEAK", raising=False)
    monkeypatch.setenv("MORNING_RUSH_SAFETY", "1")
    events, _world, safety, *_rest = await _run_day(tmp_path, _at(9, 45), 0.0)
    stops = [a for a in safety if a.machine_id == "B-OPEN" and a.action is SafetyActionKind.STOP_MACHINE]
    assert stops and _at(8, 30) < stops[0].ts < _at(8, 45)
    bopen = [e for e in events if e.machine_id == "B-OPEN"]
    assert len(bopen) == 1 and bopen[0].opened_at < stops[0].ts, [(e.id, e.anomaly.message) for e in bopen]


def test_detector_ignores_a_stopped_machine_cooling():
    """A stopped (or out-of-service) machine cools to room temperature: no 溫度下降 anomaly. Running, the same drop is one."""
    from warroom.detect.detector import StreamDetector
    from warroom.contracts import FaultKind, FaultSpec, MachineStatus

    def run(stop: bool) -> list:
        f = Factory(seed=7, scenario=ScenarioName.NORMAL)
        det = StreamDetector()
        det.attach_plan(sim=f)
        for _ in range(300):
            det.ingest(f.step(1.0))
        if stop:
            f.set_machine_status("B-OPEN", MachineStatus.STOPPED)
        else:
            f.inject_fault(FaultSpec(id="F-T", machine_id="B-OPEN", kind=FaultKind.TEMP_RAMP,
                                     params={"target": 40.0, "rate_c_per_min": 5.0}))
        found = []
        for _ in range(900):
            found += [a for a in det.ingest(f.step(1.0)) if a.machine_id == "B-OPEN"]
        assert f.true_temp("B-OPEN") < 60
        return found
    assert run(stop=True) == []
    assert run(stop=False)


async def test_page11_fast_repair_keeps_h1_rejected_until_h2(tmp_path):
    """press7_temp_drop under fast repair: H1's 檢修 finishes in wall seconds but the scripted leak stays, so VERIFY
    reads 119.5 °C and H1 is rejected; H2's 停用檢修 is what fixes press 7 (fast), and the event closes."""
    from warroom.api import Runtime
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario="press7_temp_drop", llm_mode="mock", repair_fast_s=0.05)
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    await rt.start()
    approved: list[str] = []
    try:
        for _ in range(60 * 60):
            await rt.tick(1.0, publish=False)
            for req in rt.approvals.pending():
                opt = next((o for o in req.options if o.recommended), req.options[0])
                rt.approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE, option_id=opt.id,
                                                     reason="test", via="auto", decided_at=rt.now()))
                approved.append(req.id)
            ev = next((e for e in rt.events() if e.machine_id == "CURE-07"), None)
            if ev is not None and ev.state in (EventState.CLOSED, EventState.SAFE_STOP):
                break
        for _ in range(300):                 # H2's people finish after the event closed (fast repair: wall seconds)
            await rt.tick(1.0, publish=False)
            await asyncio.sleep(0.005)
        await rt.wait_events(5)
        ev = next(e for e in rt.events() if e.machine_id == "CURE-07")
        tasks = [t for t in rt.world_snapshot().tasks.items if t.event_id == ev.id]
        faults = [f for f in rt.sim.faults(active_only=False) if f.machine_id == "CURE-07"]
        tile = next(m for m in rt.machine_tiles() if m.machine_id == "CURE-07")
        # 檢修完成 shows green but the press stays out of service: only the supervisor's 恢復運轉 lifts it
        assert tile.status is MachineStatus.OUT_OF_SERVICE and tile.repaired
        import httpx
        from warroom.api.app import create_app
        app = create_app(rt, supervisor_pin="mr-pin")
        url = "/api/machines/CURE-07/return-to-service"

        def client(addr=("127.0.0.1", 1)):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=addr), base_url="http://local")
        async with client(("192.168.1.20", 5000)) as phone, client() as local:
            assert (await phone.post(url)).status_code == 401                         # LAN without the PIN
            assert (await local.post("/api/machines/CURE-01/return-to-service")).status_code == 409   # not out of service
            assert (await local.post("/api/machines/NOPE/return-to-service")).status_code == 404
            r = await phone.post(url, headers={"X-Supervisor-Pin": "mr-pin"})
            assert r.status_code == 200, r.text
            assert (await local.post(url)).status_code == 409                          # running again
        assert rt.sim.status_of("CURE-07") is MachineStatus.RUNNING
        assert not next(m for m in rt.machine_tiles() if m.machine_id == "CURE-07").repaired
        back = [e for e in rt.audit.tail(0, 100000) if e.payload.get("msg") == "恢復運轉"]
        assert len(back) == 1 and back[0].actor.startswith("supervisor") and back[0].event_id == ev.id
    finally:
        await rt.stop()
    h1 = ev.hypotheses[0]
    assert h1.status.value == "rejected" and any("119.5" in x for x in h1.evidence), h1.evidence
    assert len(ev.hypotheses) >= 2 and len(approved) >= 2 and ev.state is EventState.CLOSED
    assert any(t.kind == "停用檢修" and t.status == "完成" for t in tasks), [(t.kind, t.status) for t in tasks]
    assert not faults
