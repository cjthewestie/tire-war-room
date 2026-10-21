"""今天處理過的任務 is never cleared (CJ, 2026-10-22: 「今天處理過的任務都要記錄在agent 頁面不可以清掉」): built from the
audit FILE, it survives a scenario load, 重來, a runtime restart on the same file and the day-loop wrap."""
from __future__ import annotations

from tests.test_plan_talk import Plant, _rt
from warroom.api.tasklog import TaskLog


async def _one_task(p, text="#A-311 可能晚交，請重排保交期"):
    r = await p.http.post("/api/command", json={"text": text})
    eid = r.json()["event_id"]
    await p.until(lambda: p.pending(eid) or p.rt.state_machine.get(eid).state.value in ("closed", "safe_stop"))
    if p.pending(eid):
        req = p.pending(eid)[0]
        d = await p.http.post(f"/api/approvals/{req.id}/decision", json={"request_id": req.id, "decision": "approve"})
        assert d.status_code == 200
    await p.until(lambda: p.rt.state_machine.get(eid).state.value in ("closed", "safe_stop"))
    return eid


def _ids(rt):
    return [r["id"] for r in rt.tasklog.rows()]


async def test_rows_survive_reset_scenario_load_and_a_restart_on_the_same_file(tmp_path):
    async with Plant(_rt(tmp_path, "normal")) as p:
        eid = await _one_task(p)
        row = next(r for r in p.rt.tasklog.rows() if r["id"] == eid)
        assert row["final"] in ("結案", "安全停止") and row["closed_at"] and row["report"] == f"/report?event={eid}"
        assert row["order_id"] == "#A-311" and row["scenario"] in (None, "normal")
        if row["approvals"]:
            assert row["approvals"][0]["version"] == 1 and row["approvals"][0]["decision"] == "核准"
        assert (await p.http.post("/api/sim/control", json={"action": "reset"})).status_code == 200
        assert eid in _ids(p.rt)                                               # 重來 keeps it
        assert (await p.http.post("/api/sim/control", json={"action": "scenario", "scenario": "rush_order"})).status_code == 200
        assert eid in _ids(p.rt)
        days = (await p.http.get("/api/tasklog")).json()["days"]
        assert any(r["id"] == eid for d in days for g in d["groups"] for r in g["rows"])
    # a new runtime on the same audit file (a server restart)
    async with Plant(_rt(tmp_path, "normal")) as p2:
        assert eid in _ids(p2.rt)
        h = (await p2.http.post("/api/main/message", json={"text": "今天處理過什麼"})).json()
        assert h["steps"] and h["steps"][0]["tool"] == "query_plant"


def test_a_day_wrap_keeps_earlier_rows_and_tags_the_cycle(tmp_path):
    from warroom.agent.audit import JsonlAuditLog
    from warroom.contracts import AuditKind
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.append(AuditKind.SYSTEM, "supervisor", {"msg": "scenario loaded", "scenario": "day_loop"})
    log.append(AuditKind.ANOMALY, "detector", {"message": "7 號機溫度下降", "machine_id": "CURE-07"}, event_id="EV-1")
    log.append(AuditKind.STATE_TRANSITION, "state_machine", {"to": "closed"}, event_id="EV-1")
    log.append(AuditKind.SYSTEM, "day_loop", {"msg": "新的一天（循環 1）", "loop": 1})
    log.append(AuditKind.ANOMALY, "detector", {"message": "7 號機溫度下降", "machine_id": "CURE-07"}, event_id="EV-1")
    log.append(AuditKind.SYSTEM, "main_agent", {"orch": "interrupt", "reason": "主管中斷"}, event_id="EV-1")
    log.append(AuditKind.STATE_TRANSITION, "state_machine", {"to": "safe_stop"}, event_id="EV-1")
    rows = TaskLog(tmp_path / "audit.jsonl").rows()
    assert [(r["id"], r["loop"], r["final"]) for r in rows] == [("EV-1", None, "結案"), ("EV-1", 1, "主管中斷")]
    assert all(r["scenario"] == "day_loop" and r["machine_id"] == "CURE-07" for r in rows)


def test_reading_is_incremental_and_read_only(tmp_path):
    from warroom.agent.audit import JsonlAuditLog
    from warroom.contracts import AuditKind
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    tl = TaskLog(tmp_path / "audit.jsonl")
    log.append(AuditKind.ANOMALY, "detector", {"message": "a"}, event_id="EV-A")
    assert [r["id"] for r in tl.rows()] == ["EV-A"]
    log.append(AuditKind.ANOMALY, "detector", {"message": "b"}, event_id="EV-B")
    assert [r["id"] for r in tl.rows()] == ["EV-A", "EV-B"]
    assert not [m for m in dir(tl) if m.startswith(("delete", "remove", "clear"))]
