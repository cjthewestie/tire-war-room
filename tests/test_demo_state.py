"""The guided demo's shared cue state (warroom/api/demo.py): transitions, the unskippable approval wait, the WS push,
and that it is presentation control only (on-site seat or supervisor PIN; the plant only sees sim pause / resume)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.api import demo as D
from warroom.config import Settings
from warroom.contracts import WsType

CUES = ["start", "console", "event", "phone", "worker", "agent", "pitch", "warn", "end"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def cmd(action, **kw):
    return D.DemoCommand(action=action, **kw)


def started(clock=None):
    st = D.DemoState(now=clock or Clock())
    st.apply(cmd("start", cues=CUES, driver="3d"), approved=0)
    return st


def test_start_continue_exit():
    st = started()
    assert st.view()["running"] and st.cue_id == "start" and st.cue_index == 0 and st.driver == "3d"
    st.apply(cmd("continue"), 0)
    assert (st.cue_index, st.cue_id, st.waiting_for) == (1, "console", None)
    rev = st.rev
    st.apply(cmd("exit"), 0)
    assert not st.running and st.rev == rev + 1
    with pytest.raises(ValueError):
        st.apply(cmd("continue"), 0)        # nothing to continue once it ended


def test_pause_freezes_elapsed():
    c = Clock()
    st = started(c)
    c.t += 10
    st.apply(cmd("pause"), 0)
    c.t += 30
    assert st.view()["paused"] and st.view()["elapsed_s"] == 10
    st.apply(cmd("resume"), 0)
    c.t += 5
    assert not st.paused and st.view()["elapsed_s"] == 15


def test_approval_wait_is_not_skippable():
    st = started()
    st.apply(cmd("cue", cue_id="phone", cue_index=3, waiting_for="approval"), approved=2)
    with pytest.raises(ValueError, match="approval"):
        st.apply(cmd("continue"), approved=2)          # no new approval since the wait began
    assert st.cue_id == "phone"
    st.apply(cmd("continue"), approved=3)              # the supervisor approved: the beat may move on
    assert st.cue_id == "worker" and st.waiting_for is None


def test_continue_wait_and_driver_cue():
    st = started()
    st.apply(cmd("cue", cue_id="console", waiting_for="continue"), 0)
    assert st.cue_index == 1 and st.waiting_for == "continue"
    st.apply(cmd("continue"), 0)                       # 按 A 繼續
    assert st.cue_id == "event"
    st.apply(cmd("cue", cue_index=8), 0)
    with pytest.raises(ValueError):
        st.apply(cmd("continue"), 0)                   # past the last cue


# ---------------------------------------------------------------- API


def _rt(tmp_path) -> Runtime:
    return Runtime(Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                            qdrant_path=tmp_path / "qdrant", scenario="normal"), use_real=False, run_loop=False)


def test_endpoint_round_trip_and_ws_push(tmp_path):
    rt = _rt(tmp_path)
    with TestClient(create_app(rt, demo_controls=True)) as c:
        assert c.get("/api/demo/state").json()["running"] is False
        r = c.post("/api/demo/state", json={"action": "start", "cues": CUES, "driver": "warroom"})
        assert r.status_code == 200 and r.json()["cue_id"] == "start"
        last = [m for m in rt.bus.history if m.type is WsType.DEMO][-1]
        assert last.data["driver"] == "warroom" and last.data["running"]
        with c.websocket_connect("/ws") as ws:       # a screen opened mid-demo gets the current cue on connect
            types = [ws.receive_json()["type"] for _ in range(3)]
        assert "demo" in types
        assert c.post("/api/demo/state", json={"action": "continue"}).json()["cue_id"] == "console"
        assert c.post("/api/demo/state", json={"action": "bogus"}).status_code == 422


def test_approval_wait_refused_over_http(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    granted = {"n": 0}
    monkeypatch.setattr(D, "approvals_granted", lambda r: granted["n"])
    with TestClient(create_app(rt, demo_controls=True)) as c:
        c.post("/api/demo/state", json={"action": "start", "cues": CUES})
        c.post("/api/demo/state", json={"action": "cue", "cue_id": "phone", "cue_index": 3, "waiting_for": "approval"})
        r = c.post("/api/demo/state", json={"action": "continue"})
        assert r.status_code == 409 and "approval" in r.text
        granted["n"] = 1
        assert c.post("/api/demo/state", json={"action": "continue"}).json()["cue_id"] == "worker"


def test_presentation_only_pause_is_the_one_plant_effect(tmp_path):
    rt = _rt(tmp_path)
    with TestClient(create_app(rt, demo_controls=True)) as c:
        before = c.get("/api/snapshot").json()
        clock0 = rt.clock_info()
        c.post("/api/demo/state", json={"action": "start", "cues": CUES})
        c.post("/api/demo/state", json={"action": "cue", "cue_index": 2, "waiting_for": "event"})
        c.post("/api/demo/state", json={"action": "continue"})
        after = c.get("/api/snapshot").json()
        # cues never touch the plant: same scenario, same events, same approvals, same machines, same clock
        assert after["events"] == before["events"] and after["pending_approvals"] == before["pending_approvals"]
        assert after["machines"] == before["machines"] and rt.clock_info() == clock0
        assert not rt.paused
        c.post("/api/demo/state", json={"action": "pause"})
        assert rt.paused and rt.scenario.value == "normal"
        c.post("/api/demo/state", json={"action": "resume"})
        assert not rt.paused


def test_remote_without_pin_cannot_drive_the_demo(tmp_path):
    rt = _rt(tmp_path)
    app = create_app(rt, demo_controls=True, supervisor_pin="12345678", container=False)
    with TestClient(app, client=("10.0.0.50", 50000)) as lan:
        assert lan.get("/api/demo/state").status_code == 200            # reading the cue is fine
        assert lan.post("/api/demo/state", json={"action": "start", "cues": CUES}).status_code == 401
        ok = lan.post("/api/demo/state", json={"action": "start", "cues": CUES}, headers={"X-Supervisor-Pin": "12345678"})
        assert ok.status_code == 200                                     # the supervisor's phone may press continue
    off = create_app(_rt(tmp_path / "b"), demo_controls=False)
    with TestClient(off) as c:
        assert c.post("/api/demo/state", json={"action": "start"}).status_code == 403
