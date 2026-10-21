"""Issue #104 一早訂單湧入 (morning_rush), the finals demo, cut to ~3 real minutes at 30×: 07:00–07:07 eight orders
arrive (SO-012 can never make its due date, SO-017 is the rush order), 07:24 page 11 on press 7, fixed by the approved
repair; 10:00 the truck leaves. MORNING_RUSH_LEAK=1 adds page 11's leak (H1 rejected -> H2), MORNING_RUSH_SAFETY=1
the 08:30 Safety Core stop on B 線開煉機.

CJ, 2026-10-22: 「因為我demo 是以一天的開始就是訂單湧進來 然後事件 然後 我還沒想到完整展示流程」.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from warroom import factory as F
from warroom.config import Settings
from warroom.contracts import ApprovalDecision, DecisionKind, EventState, SafetyActionKind, ScenarioName
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
    assert _faults(sc) == {"CURE-07": [_at(7, 24)]}             # the 3-minute cut: steam drop only, no Safety Core step


def test_leak_and_safety_steps_are_toggleable(monkeypatch):
    monkeypatch.setenv("MORNING_RUSH_LEAK", "1")
    monkeypatch.setenv("MORNING_RUSH_SAFETY", "1")
    f = _faults(get_scenario(ScenarioName.MORNING_RUSH))
    assert f["CURE-07"] == [_at(7, 24), _at(7, 33, 40)] and f["B-OPEN"] == [_at(8, 30)]


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
        return rt.events(), rt.world_snapshot(), list(rt.safety_log), risk_at, preplan_at, rt.PREPLAN_EVERY_S
    finally:
        await rt.stop()


async def test_morning_rush_end_to_end(tmp_path, monkeypatch):
    """The 3-minute cut: the rush re-plan makes SO-012 a 交期風險 item and the pre-plan starts on it without anyone
    asking; page 11 fires at 07:24, its approved plan (repair included) closes it with H1 confirmed; the 10:00 truck
    leaves."""
    monkeypatch.delenv("MORNING_RUSH_LEAK", raising=False)
    monkeypatch.delenv("MORNING_RUSH_SAFETY", raising=False)
    events, world, safety, risk_at, preplan_at, every = await _run_day(tmp_path, _at(10, 1), 0.0)
    assert risk_at is not None and _at(7, 7) < risk_at < _at(7, 12), risk_at
    assert preplan_at is not None and preplan_at - risk_at <= timedelta(seconds=every), preplan_at
    cmd = next(e for e in events if (e.command or "").startswith(f"{MR_AT_RISK_ORDER} "))
    assert cmd.source == "supervisor" and cmd.opened_at < _at(7, 12)
    rush = next(e for e in events if e.anomaly.message.startswith(f"急單 {MR_RUSH_ORDER}"))
    assert _at(7, 7) <= rush.opened_at < _at(7, 8)
    p11 = next(e for e in events if e.machine_id == "CURE-07")
    assert _at(7, 24) < p11.opened_at < _at(7, 26)
    assert p11.state is EventState.CLOSED and p11.hypotheses[0].status.value == "confirmed"
    assert not [a for a in safety if a.machine_id == "B-OPEN"]
    left = [e for e in world.events if e.kind == "shipment_departed"]
    assert left and left[0].ts == _at(10, 0)


async def test_morning_rush_long_cut(tmp_path, monkeypatch):
    """MORNING_RUSH_LEAK=1 + MORNING_RUSH_SAFETY=1: H1 is rejected and H2 follows; the Safety Core stops B 線開煉機."""
    monkeypatch.setenv("MORNING_RUSH_LEAK", "1")
    monkeypatch.setenv("MORNING_RUSH_SAFETY", "1")
    events, _world, safety, *_ = await _run_day(tmp_path, _at(8, 45), 0.0)   # fast repair runs on the wall clock
    p11 = next(e for e in events if e.machine_id == "CURE-07")
    assert p11.hypotheses[0].status.value == "rejected" and len(p11.hypotheses) >= 2
    stops = [a for a in safety if a.machine_id == "B-OPEN" and a.action is SafetyActionKind.STOP_MACHINE]
    assert stops and _at(8, 30) < stops[0].ts < _at(8, 45)
    assert any(e.machine_id == "B-OPEN" and e.opened_at < stops[0].ts for e in events)
