"""Issue #32: the patrol. Code looks over the plant's materials and wakes the commander only for a real shortfall.

CJ, 2026-10-20: 「24小時醒著沒錯但是有沒有需要去主動巡檢」.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from warroom.contracts import AuditKind, EventState, PlantInput, ScenarioName
from warroom.sim import Factory
from warroom.sim.patrol import HORIZON_H, PATROL_EVERY_S, MaterialPatrol
from warroom.sim.scenarios import PLANT_SCENARIOS, STOCK_CHECK_KEY, STOCK_CHECK_OFFSET_S, get_scenario
from warroom.sim.world import MATERIAL_BY_KEY


def _run(f: Factory, seconds: float, dt: float = 10.0) -> list[PlantInput]:
    out = []
    for _ in range(int(seconds / dt)):
        f.step(dt)
        out += [pin for _, pin in f.drain_plant_inputs()]
    return out


def test_scenario_defined():
    sc = get_scenario(ScenarioName.MATERIAL_SHORT)
    assert ScenarioName.MATERIAL_SHORT in PLANT_SCENARIOS
    assert [s.plant.kind for s in sc.steps if s.plant] == ["stock_check"]


def test_stock_count_opens_nothing_the_patrol_raises_it_once():
    f = Factory(seed=7, scenario=ScenarioName.MATERIAL_SHORT)
    assert _run(f, STOCK_CHECK_OFFSET_S - 10) == []
    found = _run(f, 3 * 3600)
    assert [p.kind for p in found] == ["material_short"]          # the count itself is not an event
    pin = found[0]
    assert pin.material_key == STOCK_CHECK_KEY and pin.note == MATERIAL_BY_KEY[STOCK_CHECK_KEY].name
    assert pin.text.startswith("巡檢：碳黑") and "小時後" in pin.text and "晚" in pin.text
    kinds = [e.kind for e in f.world.events]
    assert "stock_check" in kinds and "patrol" in kinds and "po_created" in kinds


@pytest.mark.parametrize("name", [n for n in ScenarioName if n is not ScenarioName.MATERIAL_SHORT])
def test_demo_scenarios_stay_quiet(name):
    """The patrol must not add a card to the page-11 / page-2 demos or the issue #17 plant events."""
    f = Factory(seed=7, scenario=name)
    assert [p for p in _run(f, 4 * 3600) if p.kind == "material_short"] == []


def test_patrol_rules():
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    w, t = f.world, f.world.now()
    p = MaterialPatrol(w, t)
    assert p.poll(t) == []                                         # not due yet
    cb = MATERIAL_BY_KEY["cb"]
    w._stock["cb"] = 0.0
    w._pos = [x for x in w._pos if x.key != "cb"]
    found = p.poll(t + timedelta(seconds=PATROL_EVERY_S))
    assert [x.material_key for x in found] == ["cb"] and "已用完" in found[0].text and "沒有在途採購" in found[0].text
    assert p.poll(t + timedelta(seconds=2 * PATROL_EVERY_S)) == []  # raised once
    w._stock["cb"] = cb.reorder_point + 1                           # recovered: re-armed, nothing to say
    assert p.poll(t + timedelta(seconds=3 * PATROL_EVERY_S)) == []
    w._stock["cb"] = 1.0                                            # short again: raised again
    assert [x.material_key for x in p.poll(t + timedelta(seconds=4 * PATROL_EVERY_S))] == ["cb"]


def test_a_delivery_before_it_runs_out_is_fine():
    f = Factory(seed=7, scenario=ScenarioName.NORMAL)
    w, t = f.world, f.world.now()
    p = MaterialPatrol(w, t)
    o = next(x for x in w.material_outlook() if x["key"] == "cb")
    w._stock["cb"] = o["use_per_h"] * (HORIZON_H - 2)              # runs out in 6 h ...
    w._purchase(t, MATERIAL_BY_KEY["cb"])
    po = w._pos[-1]
    po.status, po.eta = "在途", t + timedelta(hours=3)              # ... a truck is due in 3 h
    assert p.poll(t + timedelta(seconds=PATROL_EVERY_S)) == []


def test_patrol_can_be_turned_off():
    class Quiet(Factory):
        patrol_enabled = False
    f = Quiet(seed=7, scenario=ScenarioName.MATERIAL_SHORT)
    assert f.patrol is None and _run(f, STOCK_CHECK_OFFSET_S + 1800) == []


async def test_material_short_end_to_end(tmp_path):
    from tests.test_plant_events import _check_common, _run_scenario
    events, decided, world, audit = await _run_scenario(tmp_path, "material_short", minutes=150)
    ev, tasks = _check_common(events, decided, world, audit, "material_short")
    card = decided[0]
    assert card.done and card.done[0].startswith("巡檢：碳黑")       # the patrol's finding, from code
    assert any(x.startswith("原料") and "碳黑" in x for x in card.done)
    assert {t.kind for t in tasks} >= {"盤點"}
    assert ev.id.endswith("-MAT") and ev.alert_card and "碳黑" in ev.alert_card.title
    assert any(a["kind"] == AuditKind.SYSTEM.value and a["payload"].get("plant_input", {}).get("kind") == "material_short"
               for a in audit)
    assert ev.state is EventState.CLOSED
