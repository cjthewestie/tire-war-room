"""每日事件紀錄 (issue #37): runs grouped by factory day (or by the demo day-loop marker), from the audit log only."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from warroom import factory as F
from warroom.api import Runtime, create_app
from warroom.api import daylog as DL
from warroom.config import Settings
from warroom.contracts import AuditEntry, AuditKind


def _e(seq: int, ts: datetime, kind: AuditKind, actor: str, payload: dict, event_id: str | None = None) -> AuditEntry:
    return AuditEntry(seq=seq, ts=ts, kind=kind, actor=actor, event_id=event_id, payload=payload, prev_hash="0", hash="0")


T0 = datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ)
EV = "EV-20261003-021400-CURE-07"


def _run_entries(start: int, ts: datetime, eid: str = EV) -> list[AuditEntry]:
    return [
        _e(start, ts, AuditKind.ANOMALY, "detector", {"message": "7 號 128 → 122°C"}, eid),
        _e(start + 1, ts, AuditKind.STATE_TRANSITION, "state_machine", {"from": None, "to": "detected"}, eid),
        _e(start + 2, ts + timedelta(seconds=5), AuditKind.STATE_TRANSITION, "state_machine", {"from": "detected", "to": "classify"}, eid),
        _e(start + 3, ts + timedelta(minutes=1), AuditKind.APPROVAL_REQUEST, "state_machine", {"id": "AP-1", "title": "核准排程"}, eid),
        _e(start + 4, ts + timedelta(minutes=2), AuditKind.APPROVAL_DECISION, "supervisor", {"request_id": "AP-1", "decision": "approve", "via": "phone"}, eid),
        _e(start + 5, ts + timedelta(minutes=3), AuditKind.STATE_TRANSITION, "state_machine", {"from": "verify", "to": "replan"}, eid),
        _e(start + 6, ts + timedelta(minutes=10), AuditKind.STATE_TRANSITION, "state_machine", {"from": "verify", "to": "closed"}, eid),
    ]


def test_groups_by_factory_date_and_builds_runs():
    es = _run_entries(1, T0) + [_e(20, T0 + timedelta(days=1), AuditKind.SYSTEM, "supervisor", {"msg": "ask", "question": "7 號機怎樣"}, "ASK-0001"),
                                _e(21, T0 + timedelta(days=1, seconds=2), AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": "正常"}, "ASK-0001")]
    days = DL.build(es)
    assert list(days) == ["2026-10-03", "2026-10-04"]
    r = days["2026-10-03"]["runs"][0]
    assert r["machine_id"] == "CURE-07" and r["type"] == "event"
    assert [p["state"] for p in r["path"]] == ["detected", "classify", "replan", "closed"]
    assert r["rounds"] == 2 and r["outcome"] == "closed" and r["duration_min"] == 10.0
    assert r["approvals"][0]["decision"] == "approve" and r["approvals"][0]["by"] == "supervisor" and r["pending_approvals"] == 0
    s = DL.summary(days["2026-10-03"])
    assert s["by_outcome"] == {"closed": 1} and s["avg_close_min"] == 10.0 and s["approvals_decided"] == 1
    ask = days["2026-10-04"]["runs"][0]
    assert ask["type"] == "ask" and ask["outcome"] == "answered" and ask["title"] == "7 號機怎樣"


def test_day_loop_marker_groups_by_loop():
    es = _run_entries(1, T0)
    es.append(_e(10, T0 + timedelta(hours=1), AuditKind.SYSTEM, "day_loop", {"msg": "新的一天（循環 1）", "loop": 1}))
    es += _run_entries(11, T0 + timedelta(hours=1, minutes=5), eid="EV-20261003-031900-CURE-07")
    days = DL.build(es)
    assert list(days) == ["loop-0", "loop-1"]
    assert days["loop-1"]["label"] == "新的一天（循環 1）"
    assert len(days["loop-0"]["runs"]) == 1 and len(days["loop-1"]["runs"]) == 1


def test_csv_has_header_and_one_row_per_run():
    days = DL.build(_run_entries(1, T0))
    text = DL.to_csv(days["2026-10-03"])
    lines = text.lstrip("﻿").strip().splitlines()
    assert lines[0].split(",")[:3] == ["id", "類型", "機台"]
    assert len(lines) == 2 and "detected→classify→replan→closed" in lines[1] and "結案" in lines[1]


@pytest.fixture
def client(tmp_path):
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant")
    with TestClient(create_app(Runtime(s, use_real=False, run_loop=False))) as c:
        yield c


def test_daylog_api_after_a_scenario(client):
    client.post("/api/sim/control", json={"action": "scenario", "scenario": "press7_temp_drop"})
    for _ in range(4):
        client.post("/api/sim/step", json={"seconds": 120})
    days = client.get("/api/daylog").json()["days"]
    assert days and days[0]["runs"] >= 1
    d = client.get("/api/daylog/current").json()
    ev = next(r for r in d["runs"] if r["machine_id"] == "CURE-07")
    assert ev["path"][0]["state"] == "detected" and ev["type"] == "event"
    r = client.get(f"/api/daylog/{d['key']}/csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "CURE-07" in r.text
    assert client.get("/api/daylog/nope").status_code == 404
