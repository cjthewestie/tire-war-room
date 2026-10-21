"""人員回報 (CJ, 2026-10-22: 「加回報」): in report mode (WORKER_REPORT=1 / the 後台 switch) code still assigns a worker
task, but only the assigned worker's 到達・開始 / 完成 on /worker (or the supervisor's 代為完成 with a reason) moves it;
VERIFY waits for those reports, then checks the reading; the event closes only after both. Auto mode (the demo
default) is unchanged (tests/test_fast_repair.py and the rest stay green)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import time

import httpx
import pytest

from warroom.api import Runtime
from warroom.api.app import create_app
from warroom.api.tasks import PHOTO_MAX_BYTES, decode_photo, worker_pins
from warroom.config import Settings
from warroom.contracts import AuditKind, EventState, MachineStatus, ScenarioName
from warroom.sim import Factory
from warroom.sim.work import TaskRequest

PIN = "worker-report-pin"
LAN = ("192.168.1.30", 5000)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _settings(tmp_path, fast_s: float = 2.0) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                    scenario="normal", llm_mode="mock", repair_fast_s=fast_s)


def _client(app, client=("127.0.0.1", 1)) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=client), base_url="http://local")


def _factory() -> Factory:
    f = Factory(seed=7, scenario=ScenarioName.OPEN_MILL_OVERHEAT)
    for _ in range(60):
        f.step(1.0)
    return f


def _maintenance(board, now, eid="EV-T"):
    return board.dispatch(TaskRequest(event_id=eid, approval_id="AP-T", step_id="S3", event_kind="overheat",
                                      tool="notify_teams", args={"kind": "maintenance", "machine_id": "A-BIGOPEN",
                                                                 "teams": ["maintenance"], "message": "檢修冷卻水路"}),
                          now)


# ------------------------------------------------------------------ the board on its own
def test_report_mode_assigns_but_never_advances_on_a_timer():
    f = _factory()
    board = f.world.tasks
    board.fast_s, board.speed, board.report_mode = 3.0, (lambda: 10.0), True
    (t,) = _maintenance(board, f.now())
    assert t.status == "前往中" and t.worker_id and t.mode == "report"          # code still picks the person
    assert t.walk_min >= 1.0 and t.work_min == 30.0                            # fast repair does not apply
    assert not board.advance_fast(f.now(), 60.0) and not board.fast_open()
    for _ in range(90 * 60):                                                   # 90 factory minutes: 3x work_min
        f.step(1.0)
    v = board.get(t.id)
    assert v.status == "前往中" and v.progress < 0.5 and v.done_at is None
    assert board.view().mode == "report"


def test_start_done_transitions_and_who_may_report():
    f = _factory()
    board = f.world.tasks
    board.report_mode = True
    (t,) = _maintenance(board, f.now())
    seen: list[tuple[str, str]] = []
    board.add_listener(lambda what, v, now: seen.append((what, v.status)))
    with pytest.raises(PermissionError):
        board.report(t.id, "start", "W-NOBODY", f.now())                     # not the assigned worker
    with pytest.raises(ValueError, match="需要「執行中」"):
        board.report(t.id, "done", t.worker_id, f.now())                     # 完成 before 開始
    with pytest.raises(KeyError):
        board.report("TK-9999", "start", t.worker_id, f.now())
    v = board.report(t.id, "start", t.worker_id, f.now(), note="到了")
    assert v.status == "執行中" and v.started_at == f.now() and v.report_note == "到了"
    with pytest.raises(ValueError):
        board.report(t.id, "start", t.worker_id, f.now())                    # 開始 twice
    v = board.report(t.id, "done", t.worker_id, f.now(), photo_sha256="ab" * 32)
    assert v.status == "完成" and v.reported_by == t.worker_id and v.photo_sha256 == "ab" * 32 and v.progress == 1.0
    with pytest.raises(ValueError):
        board.report(t.id, "done", t.worker_id, f.now())                     # already done
    assert seen == [("reported_start", "執行中"), ("reported_done", "完成")]


def test_report_is_refused_in_auto_mode():
    f = _factory()
    (t,) = _maintenance(f.world.tasks, f.now())
    with pytest.raises(ValueError, match="人員回報模式未開啟"):
        f.world.tasks.report(t.id, "start", t.worker_id, f.now())


def test_override_needs_a_reason():
    f = _factory()
    board = f.world.tasks
    board.report_mode = True
    (t,) = _maintenance(board, f.now())
    with pytest.raises(ValueError, match="原因"):
        board.override(t.id, "supervisor", "  ", f.now())
    v = board.override(t.id, "supervisor", "手機沒電", f.now())
    assert v.status == "完成" and v.reported_by == "supervisor" and v.override_reason == "手機沒電" and v.started_at
    with pytest.raises(ValueError, match="已經完成"):
        board.override(t.id, "supervisor", "again", f.now())


def test_overdue_after_twice_the_work_time():
    f = _factory()
    board = f.world.tasks
    board.report_mode = True
    (t,) = _maintenance(board, f.now())
    seen: list[str] = []
    board.add_listener(lambda what, v, now: seen.append(what))
    board.report(t.id, "start", t.worker_id, f.now())
    for _ in range(59 * 60):                                                   # 59 min < 2 x 30 min
        f.step(1.0)
    assert not board.get(t.id).overdue
    for _ in range(2 * 60):
        f.step(1.0)
    assert board.get(t.id).overdue and seen.count("overdue") == 1             # raised once


def test_photo_guard():
    raw, ext = decode_photo(_url("image/png", PNG))
    assert raw == PNG and ext == "png"
    assert decode_photo(_url("image/jpeg", JPEG))[1] == "jpg"
    for bad, why in [(_url("image/gif", b"GIF89a" + b"\x00" * 10), "只接受"),
                     (_url("image/png", JPEG), "不是它宣稱"),                    # wrong magic bytes
                     ("data:text/plain;base64,aGk=", "格式不對"),
                     (_url("image/jpeg", b"\xff\xd8\xff" + b"\x00" * PHOTO_MAX_BYTES), "太大")]:
        with pytest.raises(ValueError, match=why):
            decode_photo(bad)
    assert worker_pins("W-1:1234, W-2:99 ,bad,:x") == {"W-1": "1234", "W-2": "99"}


# ------------------------------------------------------------------ the API
async def _rt(tmp_path, monkeypatch, **env) -> Runtime:
    monkeypatch.setenv("WORKER_REPORT", "1")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rt = Runtime(_settings(tmp_path, fast_s=0.0), run_loop=False)
    await rt.start()
    await rt.tick(1.0, publish=False)
    return rt


async def test_api_start_done_auth_illegal_and_audit(tmp_path, monkeypatch):
    rt = await _rt(tmp_path, monkeypatch)
    try:
        board = rt.sim.world.tasks
        assert board.report_mode
        (t,) = _maintenance(board, rt.clock.now())
        wid = t.worker_id
        app = create_app(rt, supervisor_pin=PIN)
        start, done = f"/api/tasks/{t.id}/start", f"/api/tasks/{t.id}/done"
        async with _client(app) as local, _client(app, LAN) as phone:
            r = await phone.post(start, json={"worker_id": wid})
            assert r.status_code == 403 and "WARROOM_WORKER_PINS" in r.json()["detail"]   # no worker PINs set
            monkeypatch.setenv("WARROOM_WORKER_PINS", f"{wid}:4821,W-OTHER:1111")
            assert (await phone.post(start, json={"worker_id": wid}, headers={"X-Worker-Pin": "1111"})).status_code == 401
            r = await phone.post(start, json={"worker_id": "W-OTHER"}, headers={"X-Worker-Pin": "1111"})
            assert r.status_code == 403                                     # a real PIN, but not this task's worker
            r = await phone.post(done, json={"worker_id": wid}, headers={"X-Worker-Pin": "4821"})
            assert r.status_code == 409                                     # 完成 before 開始
            r = await phone.post(start, json={"worker_id": wid, "note": "到了"}, headers={"X-Worker-Pin": "4821"})
            assert r.status_code == 200 and r.json()["status"] == "執行中"
            r = await local.post(done, json={"worker_id": wid, "photo": _url("image/gif", b"GIF89a")})
            assert r.status_code == 422 and board.get(t.id).status == "執行中"   # bad photo: nothing changed
            r = await local.post(done, json={"worker_id": wid, "photo": _url("image/png", PNG)})
            assert r.status_code == 200 and r.json()["status"] == "完成"
            sha = hashlib.sha256(PNG).hexdigest()
            assert r.json()["photo_sha256"] == sha
            ph = await local.get(f"/api/tasks/{t.id}/photo")
            assert ph.status_code == 200 and ph.content == PNG
            assert next(iter((tmp_path / "task_photos").iterdir())).name == f"{t.id}-{sha[:16]}.png"
            assert (await local.post(start, json={"worker_id": wid})).status_code == 409
            assert (await local.post("/api/tasks/TK-9999/start", json={"worker_id": wid})).status_code == 404
        rows = rt.audit.tail(0, 100_000)
        mine = [e for e in rows if e.kind == AuditKind.TASK and e.payload.get("task_id") == t.id
                and e.payload.get("change") in ("reported_start", "reported_done")]
        assert [(e.actor, e.payload["change"]) for e in mine] == [(wid, "reported_start"), (wid, "reported_done")]
        assert mine[1].payload["photo_sha256"] == sha and mine[0].payload["note"] == "到了"
        assert mine[0].ts is not None and mine[1].payload["done_at"]
        refused = [e for e in rows if e.kind == AuditKind.INTERCEPTED]
        assert any(e.payload.get("msg") == "worker PIN rejected" for e in refused)
        assert any(e.payload.get("msg") == "task report refused" for e in refused)
        assert not any("4821" in str(e.payload) or "iVBOR" in str(e.payload) for e in rows)   # no PIN, no image
    finally:
        await rt.stop()


async def test_api_override_needs_supervisor_and_reason(tmp_path, monkeypatch):
    rt = await _rt(tmp_path, monkeypatch)
    try:
        (t,) = _maintenance(rt.sim.world.tasks, rt.clock.now())
        app = create_app(rt, supervisor_pin=PIN)
        url = f"/api/tasks/{t.id}/override"
        async with _client(app) as local, _client(app, LAN) as phone:
            assert (await phone.post(url, json={"reason": "手機沒電"})).status_code == 401          # LAN, no PIN
            assert (await local.post(url, json={"reason": "   "})).status_code == 422
            assert (await local.post(url, json={})).status_code == 422
            r = await phone.post(url, json={"reason": "手機沒電"}, headers={"X-Supervisor-Pin": PIN})
            assert r.status_code == 200 and r.json()["status"] == "完成" and r.json()["override_reason"] == "手機沒電"
            assert (await local.post(url, json={"reason": "x"})).status_code == 409
        row = next(e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.TASK
                   and e.payload.get("change") == "override_done")
        assert row.actor == f"supervisor@{LAN[0]}" and row.payload["override_reason"] == "手機沒電"
    finally:
        await rt.stop()


async def test_mode_switch_from_admin_is_audited(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKER_REPORT", raising=False)
    rt = Runtime(_settings(tmp_path), run_loop=False)
    await rt.start()
    try:
        await rt.tick(1.0, publish=False)
        app = create_app(rt, supervisor_pin=PIN)
        async with _client(app) as local, _client(app, LAN) as phone:
            assert (await local.get("/api/tasks/mode")).json()["mode"] == "auto"            # demo default
            assert not rt.sim.world.tasks.report_mode
            assert (await phone.post("/api/tasks/mode", json={"on": True})).status_code == 401
            assert (await local.post("/api/tasks/mode", json={"on": True})).json()["mode"] == "report"
            assert rt.sim.world.tasks.report_mode and rt.world_snapshot().tasks.mode == "report"
            await rt.load_scenario(ScenarioName.NORMAL)                                     # a new World keeps it
            await rt.tick(1.0, publish=False)
            assert rt.sim.world.tasks.report_mode
            assert (await local.post("/api/tasks/mode", json={"on": False})).json()["mode"] == "auto"
        rows = [e for e in rt.audit.tail(0, 100_000) if e.payload.get("msg") == "人員任務回報模式"]
        assert [e.payload["worker_report"] for e in rows] == [True, False] and rows[0].actor == "admin:supervisor"
    finally:
        await rt.stop()


# ------------------------------------------------------------------ the whole flow
@pytest.mark.parametrize("fast_s", [2.0, 0.0])
async def test_event_closes_only_after_the_done_report_and_verify(tmp_path, monkeypatch, fast_s):
    """open_mill_overheat in report mode, the real wall-clock loop: approve; the 檢修 task waits at 前往中 for as long
    as nobody reports (the event stays open); the worker's 開始 → 完成 clears the fault, VERIFY confirms the reading,
    then the event closes, and the timeline says who reported and what code checked."""
    monkeypatch.setenv("WORKER_REPORT", "1")
    rt = Runtime(_settings(tmp_path, fast_s=fast_s), allow_fallback=False)
    app = create_app(rt, supervisor_pin=PIN)
    await rt.start()
    try:
        await rt.load_scenario(ScenarioName.OPEN_MILL_OVERHEAT)
        await rt.preroll()
        rt.set_speed(20.0)
        async with _client(app) as local:
            task = None
            t0 = time.monotonic()
            while time.monotonic() - t0 < 60 and task is None:
                for a in (await local.get("/api/approvals", params={"status": "pending"})).json():
                    opt = next((o["id"] for o in a["options"] if o.get("recommended")),
                               a["options"][0]["id"] if a["options"] else None)
                    await local.post(f"/api/approvals/{a['id']}/decision",
                                     json={"request_id": a["id"], "decision": "approve", "option_id": opt})
                ev = next((e for e in rt.events() if e.machine_id == "A-BIGOPEN"), None)
                if ev is not None:
                    ts = rt.sim.world.tasks.for_event(ev.id)
                    if ts and all(t.status == "前往中" for t in ts):
                        task = next((t for t in ts if t.kind == "檢修"), None)
                await asyncio.sleep(0.1)
            assert task is not None, [(e.id, e.state) for e in rt.events()]
            await asyncio.sleep(3.0)                                    # 1 factory minute at 20x: nothing moves
            assert rt.sim.world.tasks.get(task.id).status == "前往中"
            ev = rt.state_machine.get(ev.id)
            assert ev.state not in (EventState.CLOSED, EventState.SAFE_STOP), ev.outcome
            assert rt.sim.machine("A-BIGOPEN").status is not MachineStatus.RUNNING
            board = rt.sim.world.tasks
            others = [t for t in board.for_event(ev.id) if t.id != task.id]
            for t in others:                                            # e.g. 放慢投料: reported done first
                assert t.status == "前往中", others
                for step in ("start", "done"):
                    assert (await local.post(f"/api/tasks/{t.id}/{step}", json={"worker_id": t.worker_id})).status_code == 200
            body = {"worker_id": task.worker_id}
            assert (await local.post(f"/api/tasks/{task.id}/start", json=body)).status_code == 200
            await asyncio.sleep(1.0)
            assert rt.state_machine.get(ev.id).state not in (EventState.CLOSED, EventState.SAFE_STOP)
            assert rt.state_machine.get(ev.id).repaired_at is None      # 檢修 still open: the fault is still there
            r = await local.post(f"/api/tasks/{task.id}/done", json={**body, "photo": _url("image/jpeg", JPEG)})
            assert r.status_code == 200, r.text
            if not fast_s:
                rt.set_speed(120.0)                 # real durations: the mill cools down on its own after the fix
            t1 = time.monotonic()
            while time.monotonic() - t1 < 30 and rt.state_machine.get(ev.id).state not in (EventState.CLOSED,
                                                                                         EventState.SAFE_STOP):
                await asyncio.sleep(0.1)
        ev = rt.state_machine.get(ev.id)
        assert ev.state is EventState.CLOSED, ([(t.id, t.kind, t.status) for t in rt.sim.world.tasks.for_event(ev.id)],
                                               [n.text for n in ev.timeline][-8:])
        assert ev.hypotheses[-1].status.value == "confirmed"
        notes = [n.text for n in ev.timeline]
        said = next(i for i, n in enumerate(notes) if "回報完成（附照片）" in n)
        checked = next(i for i, n in enumerate(notes) if "→ 程式確認讀值回到" in n)
        assert said < checked, notes[-12:]
        assert notes[checked].startswith(notes[said].removeprefix("[人工] ").split("：")[0])
        rows = rt.audit.tail(0, 100_000)
        done = next(e for e in rows if e.kind == AuditKind.TASK and e.payload.get("change") == "reported_done"
                    and e.payload.get("task_id") == task.id)
        closed = next(e for e in rows if e.kind == AuditKind.STATE_TRANSITION and e.event_id == ev.id
                      and e.payload.get("to") == "closed")
        assert done.seq < closed.seq and done.actor == task.worker_id
    finally:
        await rt.stop()
