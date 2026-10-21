"""Issue #36 一日循環 and the demo-speed fixes.

CJ, 2026-10-20: 「時間的話是要循環的…我大概只需要一天的模擬資料然後就一直循環這樣子跑實時的資料」 and 「就是設計今天這個從訂單進來
一直到東西出貨的一個流程…成功出貨或者是單子完成也要顯示」.

- the loop replays the team dataset's quiet day (2026-10-21) and wraps 24:00 -> 00:00 with no anomaly, the
  start-of-day plant restored and a 「新的一天（循環 n）」 audit marker
- one loop completes and ships at least one order (order_completed / shipment_departed, audited)
- every scripted scenario plays exactly as before: LoopFactory == Factory outside DAY_LOOP
- a demo button pre-rolls to 15 s before the first scripted step, and page 11 plays the same from there
"""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import httpx
import pytest

from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import ScenarioName, WsType
from warroom.sim import Factory
from warroom.sim.day_loop import LoopFactory

pytestmark = pytest.mark.timeout(600)


def _settings(tmp_path, scenario: str) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock")


def _audit(tmp_path) -> list[dict]:
    return [json.loads(x) for x in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]


def test_day_loop_is_the_default_scenario():
    assert Settings().scenario == "day_loop"


# ------------------------------------------------------------------ scripted scenarios unchanged
def _hash(f, steps: int = 900) -> str:
    h = hashlib.sha256()
    for _ in range(steps):
        for r in f.step(1):
            h.update(r.model_dump_json().encode())
    for m in f.machines():
        h.update(m.model_dump_json().encode())
    h.update(repr(f._rng.getstate()).encode())
    return h.hexdigest()


@pytest.mark.parametrize("scenario", [s for s in ScenarioName if s is not ScenarioName.DAY_LOOP])
def test_loop_factory_is_plain_factory_outside_the_loop(scenario):
    assert _hash(LoopFactory(scenario=scenario)) == _hash(Factory(scenario=scenario))


# ------------------------------------------------------------------ the loop
async def test_day_wraps_with_no_anomaly_resets_state_and_ships(tmp_path):
    rt = Runtime(_settings(tmp_path, "day_loop"), allow_fallback=False, run_loop=False)
    await rt.start()
    try:
        assert rt.clock.now().hour == 0 and rt.sim.loop_n == 1
        start = rt.world_snapshot()
        orders0 = sorted((o.id, o.status, o.done) for o in start.orders.items)
        goods0 = start.goods.counts
        await rt.jump_to("23:50")                          # the whole day, 10 s ticks through the normal path
        assert rt.clock.now().strftime("%H:%M") == "23:50" and rt.sim.loop_n == 1
        checked = False
        for _ in range(20 * 60):                           # across the seam, 1 s ticks
            await rt.tick(1.0, publish=False)
            if not checked and rt.sim.loop_n == 2:
                checked = True
                after = rt.world_snapshot()
                assert sorted((o.id, o.status, o.done) for o in after.orders.items) == orders0
                assert after.goods.counts == goods0
        assert checked and rt.sim.loop_n == 2 and rt.clock.now().strftime("%H:%M") == "00:10"
        assert rt.anomalies == [], [(a.machine_id, a.message) for a in rt.anomalies]
        assert rt.snapshot().header.bad_data_total == 0, rt.snapshot().header.bad_data   # no frozen-sensor flags
        assert rt.clock_info()["day_loop"]["loop"] == 2
    finally:
        await rt.stop()
    audit = _audit(tmp_path)
    marks = [a["payload"] for a in audit if a["actor"] == "day_loop"]
    assert [m["msg"] for m in marks] == ["新的一天（循環 2）"]
    done = [a["payload"] for a in audit if a["payload"].get("kind") == "order_completed"]
    shipped = [a["payload"] for a in audit if a["payload"].get("kind") == "shipment_departed"]
    assert done and shipped, (done, shipped)
    assert all(p["order_ids"] and p["qty"] and p["on_time"] is not None for p in done + shipped)


async def test_jump_only_in_the_loop(tmp_path):
    rt = Runtime(_settings(tmp_path, "normal"), allow_fallback=False, run_loop=False)
    await rt.start()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
    try:
        r = await client.post("/api/sim/control", json={"action": "jump", "at": "08:00"})
        assert r.status_code == 409
        assert (await client.post("/api/sim/control", json={"action": "jump", "at": "8am"})).status_code == 422
    finally:
        await client.aclose()
        await rt.stop()


# ------------------------------------------------------------------ demo speed: pre-roll keeps page 11
async def _p11(rt, until_hms: str) -> str:
    end = rt.now().replace(hour=int(until_hms[:2]), minute=int(until_hms[3:5]), second=0)
    while rt.now() < end:
        await rt.tick(1.0, publish=False)
    await rt.wait_events(5)
    h = hashlib.sha256()
    for e in rt.events():
        h.update(json.dumps([e.id, e.state.value, [(x.ts.isoformat(), x.text) for x in e.timeline]],
                            ensure_ascii=False).encode())
    for a in rt.safety_log:
        h.update(a.model_dump_json(exclude={"latency_ms"}).encode())
    h.update(json.dumps([[p.id, p.title, p.risk.value] for p in rt.approvals.pending()], ensure_ascii=False).encode())
    return h.hexdigest()


async def _switch(tmp_path, body: dict) -> tuple[Runtime, httpx.AsyncClient]:
    rt = Runtime(_settings(tmp_path, "normal"), allow_fallback=False, run_loop=False)
    await rt.start()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
    r = await client.post("/api/sim/control", json={"action": "scenario", "scenario": "press7_temp_drop", **body})
    assert r.status_code == 200, r.text
    return rt, client


async def test_preroll_lands_before_the_fault_and_page11_plays_the_same(tmp_path):
    """The demo button (speed 10, preroll) against the same switch without them: the page-11 run from 02:13:00 on
    (detection, hypotheses, plan, gate, card, every timeline line with its time) is identical."""
    rt, client = await _switch(tmp_path / "a", {})
    try:
        assert rt.now().strftime("%H:%M:%S") == "02:12:00"
        ref = await _p11(rt, "02:20")
    finally:
        await client.aclose()
        await rt.stop()
    rt, client = await _switch(tmp_path / "b", {"speed": 10, "preroll": True})
    try:
        assert rt.speed == 10
        assert rt.now().strftime("%H:%M:%S") == "02:13:00"           # fault at 02:13:15, 15 s ahead
        assert rt.anomalies == []
        assert await _p11(rt, "02:20") == ref
    finally:
        await client.aclose()
        await rt.stop()


async def test_scenario_switch_keeps_the_event_loop_responsive(tmp_path):
    """The build (plant + seed schedule solve) runs off the event loop: a health request answers meanwhile."""
    import asyncio
    import time
    rt = Runtime(_settings(tmp_path, "normal"), allow_fallback=False, run_loop=False)
    await rt.start()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
    try:
        sw = asyncio.create_task(client.post("/api/sim/control", json={"action": "scenario", "scenario": "rush_order"}))
        await asyncio.sleep(0.05)
        t0 = time.perf_counter()
        h = await client.get("/api/health")
        waited = time.perf_counter() - t0
        assert h.status_code == 200
        assert not sw.done() or waited < 0.5
        assert waited < 1.0, f"health waited {waited:.2f} s behind the scenario build"
        assert (await sw).status_code == 200
        assert rt.scenario is ScenarioName.RUSH_ORDER and rt.now() - rt.sim.scenario.start_at < timedelta(seconds=5)
    finally:
        await client.aclose()
        await rt.stop()


# ------------------------------------------------------------------ the dashboard's schedule follows a switch / a wrap
def _schedule_msgs(rt) -> list[dict]:
    return [m.data for m in rt.bus.history if m.type is WsType.SCHEDULE]


def _put_v2_in_force(rt) -> None:
    """What an approved event leaves behind: a second schedule version active."""
    store = rt.c.store
    store.activate(store.propose(store.current().schedule, "test: approved change").version)
    assert store.current().version == 2


async def test_scenario_switch_publishes_the_new_schedule(tmp_path):
    """The dashboard reads GET /api/schedule only at page load: a switch that swaps the store without a WS "schedule"
    left the previous scenario's version on screen (「SO-004 可能晚 253 小時」, 「10 號機 硫化延長中」)."""
    rt = Runtime(_settings(tmp_path, "concurrent_3"), allow_fallback=False, run_loop=False)
    await rt.start()
    try:
        _put_v2_in_force(rt)
        n = len(_schedule_msgs(rt))
        await rt.load_scenario(ScenarioName.PRESS7_TEMP_DROP)
        sent = _schedule_msgs(rt)[n:]
        assert sent and sent[-1] == rt.c.store.current().model_dump(mode="json")
        assert sent[-1]["version"] == 1
    finally:
        await rt.stop()


async def test_day_wrap_publishes_the_first_version(tmp_path):
    rt = Runtime(_settings(tmp_path, "day_loop"), allow_fallback=False, run_loop=False)
    await rt.start()
    try:
        await rt.tick(1.0, publish=False)                  # 00:00 snapshot of the day
        _put_v2_in_force(rt)
        n = len(_schedule_msgs(rt))
        await rt._wrap_day()
        sent = _schedule_msgs(rt)[n:]
        assert rt.c.store.current().version == 1
        assert sent and sent[-1] == rt.c.store.current().model_dump(mode="json")
    finally:
        await rt.stop()
