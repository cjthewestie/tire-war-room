"""Panels ① and ② follow the plant: a lost press grows remaining_h; a press event's tire drives WIP."""
from __future__ import annotations

from types import SimpleNamespace

from warroom import factory as F
from warroom.api.progress import WipTracker, order_progress
from warroom.contracts import EventState, MachineStatus, ScenarioName, TireSize
from warroom.sim import Factory


def _sim(scenario=ScenarioName.PRESS7_TEMP_DROP) -> Factory:
    return Factory(scenario=scenario)


def test_orders_match_page2_when_all_presses_up():
    got = {o.size: (o.done, o.total, o.remaining_h, o.note) for o in order_progress(_sim(), [])}
    for s in TireSize:
        d, t, h = F.DEMO_ORDER_PROGRESS[s]
        assert got[s] == (d, t, h, None)


def test_press7_out_of_service_grows_small_remaining():
    """Press 7 is a small-only press (dataset V07): losing it leaves 6 of 7 small presses; M/L keep all 4."""
    sim = _sim()
    sim.set_machine_status("CURE-07", MachineStatus.OUT_OF_SERVICE, "7 號機停用")
    got = {o.size: o for o in order_progress(sim, [])}
    for s in (TireSize.M, TireSize.L):
        assert got[s].remaining_h == F.DEMO_ORDER_PROGRESS[s][2] and got[s].note is None
    assert got[TireSize.S].remaining_h == round(F.DEMO_ORDER_PROGRESS[TireSize.S][2] * 7 / 6, 1)
    assert "7 號機停用" in got[TireSize.S].note and "6/7" in got[TireSize.S].note


def test_wip_follows_press_event_tire_and_survives_out_of_service():
    sim = _sim()
    wip = WipTracker()
    assert wip.semi_finished(sim, None).tire_label == "大胎 #07"
    job = sim.machine("CURE-07").cure_job
    assert job is not None and job.size is TireSize.S
    ev = SimpleNamespace(id="EV-1", machine_id="CURE-07", opened_at=sim.now(), state=EventState.PLAN)
    semi = wip.semi_finished(sim, ev)
    assert semi.order_id == job.order_id and "7 號機" in semi.tire_label and semi.tire_label.startswith("小胎")
    assert "硫化中" in semi.note and semi.note_state == "alert"
    sim.set_machine_status("CURE-07", MachineStatus.OUT_OF_SERVICE, "7 號機停用")
    semi = wip.semi_finished(sim, ev)
    assert semi.order_id == job.order_id and "已停用" in semi.note


def test_wip_for_upstream_event_keeps_page2_tire():
    sim = _sim(ScenarioName.OPEN_MILL_OVERHEAT)
    ev = SimpleNamespace(id="EV-1", machine_id="A-BIGOPEN", opened_at=sim.now(), state=EventState.PLAN)
    assert WipTracker().semi_finished(sim, ev).tire_label == "大胎 #07"
