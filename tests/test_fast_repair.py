"""Fast repair (Kyle 10-21): 觸發異常 → 異常事件啟動 → 主管核准 → 專人維修 → 維修完成（紅燈到綠燈只要 2–5 秒）→ 修正完成後通知主管覆核.

Settings.repair_fast_s > 0: a repair task (檢修 / 停用檢修 / 放慢投料 / 暫停投料) is assigned at once and done within
repair_fast_s WALL seconds at any sim speed; once every repair task of the event is done (after 主管核准) the fault is
cleared, the machine reads normal at once, VERIFY confirms right away, the event closes 「已修復：…」 with repaired_at and
review pending (「請主管覆核」), and POST /api/events/{id}/review marks it done. repair_fast_s=0 keeps the real durations.
The flows below run the real wall-clock loop, like the demo box."""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from warroom.api import Runtime
from warroom.api.app import create_app
from warroom.config import Settings
from warroom.contracts import AuditKind, EventState, MachineStatus, ScenarioName
from warroom.sim import Factory
from warroom.sim.work import REPAIR_KINDS, TaskRequest

FAST_S = 2.0
SLACK_S = 3.0          # approve -> closed may take this much longer than FAST_S (EXECUTE pacing, a loaded laptop)
PIN = "fast-repair-pin"
LAN = ("192.168.1.20", 5000)


def _settings(tmp_path, fast_s: float = FAST_S) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                    scenario="normal", llm_mode="mock", repair_fast_s=fast_s)


def _client(app, client=("127.0.0.1", 1)) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://local")


async def _fast_flow(tmp_path, scenario: str, speed: float, machine: str, check=None) -> None:
    """Load the scenario like the 觸發異常 button (preroll, demo speed), approve every card as it comes, and time the
    event on `machine` from its approval to CLOSED; then `check(rt, app, ev)`."""
    rt = Runtime(_settings(tmp_path), allow_fallback=False)          # run_loop: the real wall-clock loop
    app = create_app(rt, supervisor_pin=PIN)
    await rt.start()
    try:
        for _ in range(300):                                          # the demo box has its SOP index ready
            if rt._kb_ok is not None:
                break
            await asyncio.sleep(0.2)
        await rt.load_scenario(ScenarioName(scenario))
        await rt.preroll()
        rt.set_speed(speed)
        approved_at: float | None = None
        async with _client(app) as local:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 90:
                for a in (await local.get("/api/approvals", params={"status": "pending"})).json():
                    opt = next((o["id"] for o in a["options"] if o.get("recommended")),
                               a["options"][0]["id"] if a["options"] else None)
                    r = await local.post(f"/api/approvals/{a['id']}/decision",
                                         json={"request_id": a["id"], "decision": "approve", "option_id": opt})
                    assert r.status_code == 200, r.text
                    if rt.state_machine.get(a["event_id"]).machine_id == machine and approved_at is None:
                        approved_at = time.monotonic()
                ev = next((e for e in rt.events() if e.machine_id == machine), None)
                if ev is not None and ev.state in (EventState.CLOSED, EventState.SAFE_STOP):
                    break
                await asyncio.sleep(0.1)
            closed_at = time.monotonic()
        assert ev is not None and approved_at is not None, [e.id for e in rt.events()]
        assert ev.state is EventState.CLOSED, ev.outcome
        took = closed_at - approved_at
        assert took <= FAST_S + SLACK_S, f"approve -> closed took {took:.1f} s at {speed}x"
        assert ev.outcome.startswith("已修復："), ev.outcome
        assert ev.repaired_at is not None and ev.review is not None and ev.review.status == "pending"
        assert rt.sim.machine(machine).status is MachineStatus.RUNNING                  # green
        tasks = [t for t in rt.sim.world.tasks.for_event(ev.id) if t.kind in REPAIR_KINDS]
        assert tasks and all(t.status == "完成" and t.progress == 1.0 and t.worker_id for t in tasks), tasks
        if check is not None:
            await check(rt, app, ev)
    finally:
        await rt.stop()


def _system(rt, ev, msg):
    return [e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.SYSTEM and e.event_id == ev.id
            and e.payload.get("msg") == msg]


async def _review(rt, app, ev):
    """POST /api/events/{id}/review: LAN needs the PIN; pending -> done (by the session); 409 when none is pending."""
    url = f"/api/events/{ev.id}/review"
    async with _client(app, LAN) as phone, _client(app) as local:
        assert (await phone.post(url, json={"note": "x"})).status_code == 401           # LAN without the PIN
        assert rt.state_machine.get(ev.id).review.status == "pending"
        r = await phone.post(url, json={"note": "現場確認冷卻水正常"}, headers={"X-Supervisor-Pin": PIN})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == ev.id and body["review"]["status"] == "done"
        assert body["review"]["by"] == f"supervisor@{LAN[0]}" and body["review"]["done_at"]
        assert body["repaired_at"] and body["review"]["requested_at"]
        assert (await local.post(url)).status_code == 409                                # nothing pending now
        assert (await local.post("/api/events/EV-NOPE/review")).status_code == 404
        snap = (await local.get("/api/snapshot")).json()
        assert next(e for e in snap["events"] if e["id"] == ev.id)["review"]["status"] == "done"
        assert (await local.get("/api/health")).json()["repair_fast_s"] == FAST_S
    done = _system(rt, ev, "主管覆核")
    assert len(done) == 1 and done[0].actor == "supervisor" and done[0].payload["note"] == "現場確認冷卻水正常"
    assert done[0].seq > _system(rt, ev, "請主管覆核")[0].seq


@pytest.mark.parametrize("speed", [10.0, 60.0])
async def test_open_mill_overheat_red_to_green_in_seconds_then_review(tmp_path, speed):
    async def check(rt, app, ev):
        assert "A 線大開煉機 膠溫回到" in ev.outcome and "（維修完成 " in ev.outcome
        temp = rt.sim.machine("A-BIGOPEN").last_reading.temp_c
        assert abs(temp - 100.0) < 1.5, temp                    # snapped back to nominal, no slow cool-down
        rows = rt.audit.tail(0, 100_000)
        fixed = _system(rt, ev, "維修完成，故障排除")
        asked = _system(rt, ev, "請主管覆核")
        assert len(fixed) == 1 and fixed[0].payload["machine_id"] == "A-BIGOPEN" and len(asked) == 1
        decided = [e for e in rows if e.kind == AuditKind.APPROVAL_DECISION and e.event_id == ev.id]
        assert decided and decided[0].seq < fixed[0].seq < asked[0].seq     # the fix lands after 主管核准
        assert any(e.kind == AuditKind.TASK and e.event_id == ev.id and e.payload.get("change") == "done" for e in rows)
        if speed == 10.0:
            await _review(rt, app, ev)
    await _fast_flow(tmp_path, "open_mill_overheat", speed, "A-BIGOPEN", check)


async def test_concurrent_3_b_open_fast_repair(tmp_path):
    async def check(rt, app, ev):
        assert "B 線開煉機 膠溫回到" in ev.outcome
        assert {e.machine_id for e in rt.events()} >= {"CURE-03", "B-OPEN", "CURE-10"}
    await _fast_flow(tmp_path, "concurrent_3", 5.0, "B-OPEN", check)


# ------------------------------------------------------------------ the task board on its own
def _maintenance(board, now, eid="EV-T"):
    return board.dispatch(TaskRequest(event_id=eid, approval_id="AP-T", step_id="S3", event_kind="overheat",
                                      tool="notify_teams", args={"kind": "maintenance", "machine_id": "A-BIGOPEN",
                                                                 "teams": ["maintenance"], "message": "檢修冷卻水路"}),
                          now)


def _factory() -> Factory:
    f = Factory(seed=7, scenario=ScenarioName.OPEN_MILL_OVERHEAT)
    for _ in range(60):
        f.step(1.0)
    return f


def test_fast_task_is_assigned_at_once_and_done_on_the_wall_clock():
    f = _factory()
    board = f.world.tasks
    board.fast_s, board.speed = 3.0, lambda: 10.0
    (t,) = _maintenance(board, f.now())
    assert t.kind == "檢修" and t.status == "前往中" and t.worker_id                    # assigned at once
    assert abs(t.walk_min + t.work_min - 0.5) < 0.06                                   # 3 wall s at 10x = 30 factory s
    assert board.advance_fast(f.now(), 1.0)                                            # still open
    v = next(x for x in board.view().items if x.id == t.id)
    assert v.status == "執行中" and 0.3 < v.progress < 0.5
    assert not board.advance_fast(f.now(), 1.8)                                        # 2.8 s: done within 3 s
    v = next(x for x in board.view().items if x.id == t.id)
    assert v.status == "完成" and v.progress == 1.0 and v.done_at is not None


def test_repair_fast_s_zero_keeps_the_real_durations():
    f = _factory()
    board = f.world.tasks                                                              # fast_s 0: the default
    (t,) = _maintenance(board, f.now())
    assert t.work_min == 30.0 and t.walk_min >= 1.0
    assert not board.advance_fast(f.now(), 60.0)
    for _ in range(10 * 60):                                                           # 10 factory minutes
        f.step(1.0)
    assert next(x for x in board.view().items if x.id == t.id).status != "完成"


async def test_runtime_hands_repair_fast_s_to_the_board(tmp_path):
    rt = Runtime(_settings(tmp_path, fast_s=0.0), run_loop=False)
    await rt.start()
    try:
        await rt.tick(1.0, publish=False)
        assert rt.sim.world.tasks.fast_s == 0.0
        async with _client(create_app(rt)) as c:
            assert (await c.get("/api/health")).json()["repair_fast_s"] == 0.0
    finally:
        await rt.stop()


def test_snap_to_nominal_puts_the_reading_back_at_once():
    f = Factory(seed=7, scenario=ScenarioName.OPEN_MILL_OVERHEAT)
    for _ in range(12 * 60):                                       # cooling water failed at +135 s: well over 100 °C
        f.step(1.0)
    assert f.machine("A-BIGOPEN").last_reading.temp_c > 108
    f.clear_faults("A-BIGOPEN")
    f.snap_to_nominal("A-BIGOPEN")
    st = f.machine("A-BIGOPEN")
    assert st.last_reading.temp_c == 100.0 and st.last_reading.water_flow_lpm == 40.0
    assert f.history("A-BIGOPEN", 0)[0].temp_c == 100.0
    f.step(1.0)
    assert abs(f.machine("A-BIGOPEN").last_reading.temp_c - 100.0) < 0.5
