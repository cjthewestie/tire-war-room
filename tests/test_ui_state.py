"""Server-side state the dashboard and phone show (round-4 UI review).

- page 10: rejected readings are counted per machine in the header (bad data never wakes the AI)
- page 11: the close summary lists only what is in force at the end (no superseded v2, no cure time
  for a press that was taken out of service)
- pages 5/7: a phone 修改 that changes no option goes back as a reject-with-note and executes nothing
"""
from __future__ import annotations

import pytest

from tests.test_e2e import _pending, _run_until, _start

from warroom.contracts import EventState, MachineStatus

pytestmark = pytest.mark.timeout(300)


async def _step(rt, seconds: int) -> None:
    for _ in range(seconds):
        await rt.tick(1.0, publish=False)


async def test_header_counts_bad_data_per_machine(tmp_path):
    rt, client = await _start(tmp_path, "bad_data")
    try:
        await _step(rt, 180)
        h = (await client.get("/api/snapshot")).json()["header"]
        by = {b["machine_id"]: b for b in h["bad_data"]}
        assert {"CURE-03", "A-MIX", "B-OPEN"} <= set(by), h["bad_data"]
        assert h["bad_data_total"] == sum(b["count"] for b in h["bad_data"]) > 0
        assert by["A-MIX"]["last_quality"] == "missing" and by["B-OPEN"]["last_quality"] == "stale"
        assert rt.events() == []                       # nothing woke the commander
    finally:
        await client.aclose()
        await rt.stop()


async def test_header_bad_data_is_zero_when_normal(tmp_path):
    rt, client = await _start(tmp_path, "normal")
    try:
        await _step(rt, 180)
        h = (await client.get("/api/snapshot")).json()["header"]
        assert h["bad_data_total"] == 0 and h["bad_data"] == []
    finally:
        await client.aclose()
        await rt.stop()


async def test_page11_close_summary_is_final_state_only(tmp_path):
    rt, client = await _start(tmp_path, "press7_temp_drop")
    try:
        await _run_until(rt, "02:16:40")
        req = (await _pending(client))[0]
        rec = next(o for o in req["options"] if o["recommended"])
        r = await client.post(f"/api/approvals/{req['id']}/decision",
                              json={"request_id": req["id"], "decision": "approve", "option_id": rec["id"],
                                    "reason": "同意", "via": "phone"})
        assert r.status_code == 200
        await _run_until(rt, "02:31:30")
        req2 = (await _pending(client))[0]
        a = next(o for o in req2["options"] if o["label"] == "夜班加班")
        r = await client.post(f"/api/approvals/{req2['id']}/decision",
                              json={"request_id": req2["id"], "decision": "approve", "option_id": a["id"],
                                    "reason": "不跨組調人，改夜班加班", "via": "phone"})
        assert r.status_code == 200
        await _run_until(rt, "02:32:00")
        await rt.wait_events(10)
        ev = rt.events()[0]
        assert ev.state is EventState.CLOSED
        assert rt.sim.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
        assert "排程 v3 生效" in ev.outcome and "7 號機停用" in ev.outcome and "夜班加班" in ev.outcome
        assert "排程 v2" not in ev.outcome, ev.outcome
        assert "硫化 36.5 h" not in ev.outcome, ev.outcome
    finally:
        await client.aclose()
        await rt.stop()


async def test_phone_modify_without_other_option_executes_nothing(tmp_path):
    """What Phone.tsx sends for 修改 when the supervisor keeps the system's option and only writes a
    note: a reject with the note. Nothing runs, the schedule stays v1, and the replan drops the
    cross-team move."""
    rt, client = await _start(tmp_path, "press7_temp_drop")
    try:
        await _run_until(rt, "02:16:40")
        req = (await _pending(client))[0]
        rec = next(o for o in req["options"] if o["recommended"])
        assert rec["label"] == "從 2 線調 1 人"
        reason = "退回修改：不要調人（未改選方案，原方案不執行，請依修改內容重排）"
        r = await client.post(f"/api/approvals/{req['id']}/decision",
                              json={"request_id": req["id"], "decision": "reject", "option_id": None,
                                    "reason": reason, "via": "phone"})
        assert r.status_code == 200 and r.json()["status"] == "rejected"
        await _run_until(rt, "02:20:00")
        ev = rt.events()[0]
        text = " | ".join(t.text for t in ev.timeline)
        assert "[人工] 主管退回修改：不要調人" in text and "修改後核准" not in text
        assert (await client.get("/api/schedule")).json()["version"] == 1
        notified = [sr for sr in ev.steps if sr.step.tool.value == "notify_teams" and sr.state == "ok"]
        assert not any(sr.step.args.get("kind") == "staff_move" for sr in notified)
        pend = await _pending(client)
        assert pend and all(o["label"] != "從 2 線調 1 人" for o in pend[0]["options"])
    finally:
        await client.aclose()
        await rt.stop()


async def test_modify_with_other_option_says_option_changed(tmp_path):
    rt, client = await _start(tmp_path, "press7_temp_drop")
    try:
        await _run_until(rt, "02:16:40")
        req = (await _pending(client))[0]
        a = next(o for o in req["options"] if o["label"] == "夜班加班")
        r = await client.post(f"/api/approvals/{req['id']}/decision",
                              json={"request_id": req["id"], "decision": "modify", "option_id": a["id"],
                                    "reason": "修改：改選「夜班加班」；不跨組", "via": "phone"})
        assert r.status_code == 200
        await _run_until(rt, "02:17:00")
        text = " | ".join(t.text for t in rt.events()[0].timeline)
        assert "修改後核准" not in text
        assert "改選" in text and "夜班加班" in text
    finally:
        await client.aclose()
        await rt.stop()
