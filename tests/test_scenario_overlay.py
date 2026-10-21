"""情境疊加 (scenario overlay): an overlayable scenario applied on top of the running one, from now.

Clicking a different demo scenario while an event is open adds its faults / plant inputs instead of reloading the
plant: clock, events, approvals, schedule versions and audit stay. A step whose machine is busy goes to an idle
machine of the same kind; with none free it is skipped and says so. Story scenarios (page 11, 一日循環, 早班開工,
正常) are refused: the front end reloads them.
"""
from __future__ import annotations

import json

import httpx
import pytest

from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import TERMINAL_STATES, FaultKind, FaultSpec, ScenarioName
from warroom.sim.scenarios import OVERLAY_SCENARIOS

pytestmark = pytest.mark.timeout(300)


def _settings(tmp_path, scenario: str) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock")


async def _runtime(tmp_path, scenario: str) -> Runtime:
    rt = Runtime(_settings(tmp_path, scenario), allow_fallback=False, run_loop=False)
    assert rt.degraded == {}
    await rt.start()
    return rt


def _open(rt: Runtime, mid: str):
    return [e for e in rt.events() if e.machine_id == mid and e.state not in TERMINAL_STATES]


def test_classification():
    assert OVERLAY_SCENARIOS == {ScenarioName.CONCURRENT_3, ScenarioName.OPEN_MILL_OVERHEAT, ScenarioName.BAD_DATA,
                                 ScenarioName.RUSH_ORDER, ScenarioName.STAFFING}


async def test_overlay_keeps_the_running_scenario(tmp_path):
    """Page 11 running with press 7's event open; 3 件同時異常 on top: press 7's event, its approvals, the clock and
    the schedule stay, and the three new machines open their own events."""
    rt = await _runtime(tmp_path, "press7_temp_drop")
    try:
        for _ in range(4 * 60):                          # 02:12 -> 02:16: detected, planned, card out
            await rt.tick(1.0, publish=False)
        p7 = _open(rt, "CURE-07")
        assert p7, "press 7's event is open"
        pending = {a.id for a in rt.approvals.pending()}
        version = rt.c.store.current().version
        t0 = rt.clock.now()
        res = await rt.overlay_scenario(ScenarioName.CONCURRENT_3)
        assert rt.clock.now() == t0                      # no reload: the clock did not jump back
        assert res["new_events"] == 3 and not any(s["skipped"] or s["substituted_for"] for s in res["steps"])
        assert [s["machine_id"] for s in res["steps"]] == ["CURE-03", "B-OPEN", "CURE-10"]
        assert rt.overlays == [ScenarioName.CONCURRENT_3] and rt.clock_info()["overlays"] == ["concurrent_3"]
        assert rt.c.store.current().version == version
        for _ in range(100):                              # the first fault starts at once; detection within ~60 s
            await rt.tick(1.0, publish=False)
        assert rt.scenario is ScenarioName.PRESS7_TEMP_DROP
        assert any(e.id == p7[0].id for e in rt.events())
        for mid in ("CURE-03", "B-OPEN", "CURE-10"):
            assert any(e.machine_id == mid for e in rt.events()), mid
        still = {a.id for a in rt.all_approvals()}
        assert pending <= still                           # press 7's cards were not retired
    finally:
        await rt.stop()
    audit = [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    ov = [a for a in audit if a["payload"].get("msg") == "scenario overlaid"]
    assert len(ov) == 1 and ov[0]["payload"]["on"] == "press7_temp_drop"
    assert sum(1 for a in audit if a["payload"].get("msg") == "scenario loaded") == 0


async def test_busy_target_goes_to_an_idle_machine_of_the_same_kind(tmp_path):
    rt = await _runtime(tmp_path, "normal")
    try:
        await rt.tick(1.0, publish=False)
        # press 3 and the B-line mill already have trouble: the overlay must not stack on them
        rt.inject_fault(FaultSpec(id="F-T-1", machine_id="CURE-03", kind=FaultKind.STEAM_PRESSURE_DROP,
                                  params={"target_c": 123.0}))
        rt.inject_fault(FaultSpec(id="F-T-2", machine_id="B-OPEN", kind=FaultKind.WATER_FLOW_DROP,
                                  params={"target_lpm": 15.0, "ramp_s": 10.0}))
        res = await rt.overlay_scenario(ScenarioName.CONCURRENT_3)
        steps = res["steps"]
        assert res["new_events"] == 3 and not any(s["skipped"] for s in steps)
        press, mill, ten = steps
        assert press["substituted_for"] == "CURE-03" and press["machine_id"] not in ("CURE-03", "CURE-10")
        assert press["machine_id"] in ("CURE-01", "CURE-02", "CURE-04", "CURE-05", "CURE-06", "CURE-07")  # small-only
        assert press["detail"].startswith("改用 ") and "（3 號機處理中）" in press["detail"]
        assert mill["substituted_for"] == "B-OPEN" and mill["machine_id"] in ("A-OPEN", "A-BIGOPEN")
        assert ten["machine_id"] == "CURE-10" and ten["substituted_for"] is None
        for _ in range(100):
            await rt.tick(1.0, publish=False)
        for mid in (press["machine_id"], mill["machine_id"], "CURE-10"):
            assert any(e.machine_id == mid for e in rt.events()), mid
    finally:
        await rt.stop()


async def test_no_idle_machine_skips_the_step_and_says_so(tmp_path):
    rt = await _runtime(tmp_path, "normal")
    try:
        await rt.tick(1.0, publish=False)
        for p in ("CURE-08", "CURE-09", "CURE-10", "CURE-11"):          # every shared M+L press is busy
            rt.inject_fault(FaultSpec(id=f"F-T-{p}", machine_id=p, kind=FaultKind.STEAM_PRESSURE_DROP,
                                      params={"target_c": 123.0}))
        res = await rt.overlay_scenario(ScenarioName.CONCURRENT_3)
        ten = res["steps"][2]
        assert ten["skipped"] and ten["machine_id"] == "CURE-10" and ten["detail"].startswith("略過")
        assert res["new_events"] == 2
        assert not any(f.id.startswith("F-C3-3") for f in rt.sim.faults(active_only=False))
    finally:
        await rt.stop()


async def test_api_overlay_refuses_story_scenarios_and_a_load_clears_overlays(tmp_path):
    rt = await _runtime(tmp_path, "open_mill_overheat")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
    try:
        await rt.tick(1.0, publish=False)
        for name in ("press7_temp_drop", "day_loop", "shift_start", "normal"):
            r = await client.post("/api/sim/control", json={"action": "overlay", "scenario": name})
            assert r.status_code == 409, (name, r.text)
        assert (await client.post("/api/sim/control", json={"action": "overlay"})).status_code == 422
        r = await client.post("/api/sim/control", json={"action": "overlay", "scenario": "concurrent_3"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scenario"] == "open_mill_overheat" and body["overlays"] == ["concurrent_3"]
        assert body["overlay"]["scenario"] == "concurrent_3" and body["overlay"]["new_events"] == 3
        r = await client.post("/api/sim/control", json={"action": "scenario", "scenario": "open_mill_overheat"})
        assert r.status_code == 200 and r.json()["overlays"] == [] and "overlay" not in r.json()
    finally:
        await client.aclose()
        await rt.stop()


async def test_rush_order_overlay_twice_gets_a_new_order_id(tmp_path):
    rt = await _runtime(tmp_path, "normal")
    try:
        await rt.tick(1.0, publish=False)
        a = await rt.overlay_scenario(ScenarioName.RUSH_ORDER)
        await rt.tick(1.0, publish=False)
        b = await rt.overlay_scenario(ScenarioName.RUSH_ORDER)
        await rt.tick(1.0, publish=False)
        assert a["new_events"] == 1 and a["steps"][0]["detail"] is None
        assert "#R-009-2" in b["steps"][0]["detail"]
        ids = {o.id for o in rt.sim.orders()}
        assert {"#R-009", "#R-009-2"} <= ids
        assert sum(1 for e in rt.events() if e.anomaly.kind.value == "order_rush") == 2
    finally:
        await rt.stop()
