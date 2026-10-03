"""End to end, every module real (sim, detector, safety core, tools, scheduler, store, kb, agent,
api): the page-11 and page-2 scenarios driven tick by tick through the Runtime, with the supervisor
deciding over the REST API exactly as the phone does. Mock commander (no model needed)."""
from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest

from warroom import factory as F
from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import AuditKind, EventState, MachineStatus

pytestmark = pytest.mark.timeout(300)


def _settings(tmp_path, scenario: str) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock")


def _at(rt: Runtime, hms: str) -> datetime:
    h, m, s = map(int, hms.split(":"))
    return rt.now().replace(hour=h, minute=m, second=s, microsecond=0)


async def _run_until(rt: Runtime, hms: str) -> None:
    end = _at(rt, hms)
    while rt.now() < end:
        await rt.tick(1.0, publish=False)


async def _start(tmp_path, scenario: str):
    rt = Runtime(_settings(tmp_path, scenario), allow_fallback=False, run_loop=False)
    assert rt.degraded == {}, rt.degraded               # nothing stubbed or faked in the live path
    await rt.start()
    rt._ingest_kb()                                      # deterministic: index SOPs before the event
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt)), base_url="http://test")
    return rt, client


async def _pending(client) -> list[dict]:
    r = await client.get("/api/approvals", params={"status": "pending"})
    assert r.status_code == 200
    return r.json()


async def test_page11_press7_full_timeline(tmp_path):
    rt, client = await _start(tmp_path, "press7_temp_drop")
    try:
        # 02:14:00 detection (within 1 sim-minute of the fault), commander wakes, plan + simulate + gate;
        # the state machine paces the page-11 beats in sim time, so the gate lands at 02:14:25
        await _run_until(rt, "02:14:30")
        an = next(a for a in rt.anomalies if a.machine_id == "CURE-07")
        assert an.ts == _at(rt, "02:14:00")
        assert an.detection_latency_s is not None and an.detection_latency_s <= 60
        assert not rt.safety_log                          # 02:14:01 still 120–130 °C: no stop
        pend = await _pending(client)
        assert len(pend) == 1
        req = pend[0]
        assert [o["label"] for o in req["options"]] == ["夜班加班", "從 2 線調 1 人"]
        assert req["delivery_impact"] == "不受影響"
        ev = (await client.get("/api/events")).json()[0]
        assert ev["state"] == EventState.AWAIT_APPROVAL.value
        text = " | ".join(t["text"] for t in ev["timeline"])
        assert "H1" in text and "蒸汽壓不足" in text
        assert "S1 硫化公式、S2 訂單班表 → S3 排程（依賴 S1、S2）" in text
        assert "總需 36.5 h（+8.5 h）" in text
        assert "下一模 #A-311 交期 3 天後" in text and "10/03 夜班缺 1 人" in text
        assert "#A-311 移到 9 號機" in text and "交期全部滿足" in text
        assert "動作把關" in text and "模擬可行" in text
        beats = [(t["actor"], t["ts"][11:19]) for t in ev["timeline"]]
        assert ("安全核心", "02:14:01") in beats and ("求解器", "02:14:25") in beats, beats
        assert any(a == "指揮官" and ts == "02:14:09" for a, ts in beats), beats
        assert next(t for t in ev["timeline"] if "動作把關" in t["text"])["tag"] == "human"

        # 02:16:40 supervisor approves the solver's plan from the phone
        await _run_until(rt, "02:16:40")
        rec = next(o for o in req["options"] if o["recommended"]) if any(o["recommended"] for o in req["options"]) \
            else req["options"][-1]
        r = await client.post(f"/api/approvals/{req['id']}/decision",
                              json={"request_id": req["id"], "decision": "approve", "option_id": rec["id"],
                                    "reason": "同意", "via": "phone"})
        assert r.status_code == 200, r.text
        await _run_until(rt, "02:16:42")                  # the event task executes the approved writes
        sched = (await client.get("/api/schedule")).json()
        assert sched["version"] == 2

        # safety core acts by rule (red path) before verify; verify (code) rejects H1 at ~119.5 °C
        await _run_until(rt, "02:31:30")
        assert any(a.machine_id == "CURE-07" and a.rule_id == "SR-CURE-LOW-120" for a in rt.safety_log)
        pend = await _pending(client)
        assert len(pend) == 1, " | ".join(f"{t.ts:%H:%M:%S} {t.text}" for t in rt.events()[0].timeline)
        req2 = pend[0]
        assert req2["title"] == "7 號機停用，重排中"
        assert [o["label"] for o in req2["options"]] == ["夜班加班", "從 2 線調 1 人"]
        ev = (await client.get(f"/api/events/{ev['id']}")).json()
        text = " | ".join(t["text"] for t in ev["timeline"])
        assert "H1 不成立" in text
        verify_line = next(t for t in ev["timeline"] if "H1 不成立" in t["text"])
        assert _at(rt, "02:30:30") <= datetime.fromisoformat(verify_line["ts"]) <= _at(rt, "02:31:30")
        temp = float(verify_line["text"].split("溫度降到 ")[1].split("°C")[0])
        assert 119.0 <= temp < 120.0
        assert "安全核心已依規則處置" in text
        assert "H2" in text and "6、8 號同管線" in text and "硫化中斷處置" in text

        # 02:31:30 supervisor rejects the cross-team move and picks night overtime (option A)
        opt_a = next(o for o in req2["options"] if o["label"] == "夜班加班")
        r = await client.post(f"/api/approvals/{req2['id']}/decision",
                              json={"request_id": req2["id"], "decision": "modify", "option_id": opt_a["id"],
                                    "reason": "不跨組調人，改夜班加班", "via": "phone"})
        assert r.status_code == 200, r.text
        await _run_until(rt, "02:32:00")
        await rt.wait_events(10)
        ev = (await client.get(f"/api/events/{ev['id']}")).json()
        assert ev["state"] == EventState.CLOSED.value
        text = " | ".join(t["text"] for t in ev["timeline"])
        assert "否決跨組調人，改選夜班加班" in text
        assert rt.sim.machine("CURE-07").status is MachineStatus.OUT_OF_SERVICE
        assert (await client.get("/api/schedule")).json()["version"] == 3

        # audit: append-only hash chain intact, approvals and the decision reason recorded
        health = (await client.get("/api/health/components")).json()
        assert health["audit_chain_ok"] is True
        assert rt.audit.verify_chain()
        kinds = [json.loads(line)["kind"] for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
        for k in (AuditKind.ANOMALY, AuditKind.SAFETY_ACTION, AuditKind.LLM_CALL, AuditKind.TOOL_CALL,
                  AuditKind.APPROVAL_REQUEST, AuditKind.APPROVAL_DECISION):
            assert k.value in kinds, k
        assert "不跨組調人" in (tmp_path / "audit.jsonl").read_text()

        # one-click rollback keeps the previous version
        r = await client.post("/api/schedule/rollback", json={})
        assert r.status_code == 200 and r.json()["version"] == 2
    finally:
        await client.aclose()
        await rt.stop()


async def test_page2_open_mill_overheat(tmp_path):
    rt, client = await _start(tmp_path, "open_mill_overheat")
    try:
        start = rt.now()
        for _ in range(15 * 60):
            await rt.tick(1.0, publish=False)
            if rt.approvals.pending():
                break
        an = next(a for a in rt.anomalies if a.machine_id == "A-BIGOPEN")
        assert an.detection_latency_s is not None and an.detection_latency_s <= 60
        pend = await _pending(client)
        assert len(pend) == 1 and pend[0]["risk"] == "R3"
        focus = (await client.get("/api/focus")).json()
        assert focus["title"].startswith("異常：A 線大開煉機")
        assert any("已通知維修" in x or "通知維修" in x for x in focus["auto_done"]), focus["auto_done"]
        assert any("放慢生料投入" in p["label"] for p in focus["pending"]), focus["pending"]
        assert focus["schedule_impact"] == "無，大胎 #07 仍 17:00 入硫化機"
        # 120 °C limit for unvulcanised compound: the safety core does not stop the mill
        assert not any(a.machine_id == "A-BIGOPEN" for a in rt.safety_log)
        assert rt.sim.machine("A-BIGOPEN").status is not MachineStatus.STOPPED
        snap = (await client.get("/api/snapshot")).json()
        assert snap["header"]["abnormal_count"] == 1 and snap["header"]["normal_count"] == len(F.MACHINES) - 1
        req = pend[0]
        r = await client.post(f"/api/approvals/{req['id']}/decision",
                              json={"request_id": req["id"], "decision": "approve",
                                    "option_id": req["options"][0]["id"] if req["options"] else None,
                                    "reason": "同意放慢投料", "via": "dashboard"})
        assert r.status_code == 200, r.text
        for _ in range(25 * 60):
            await rt.tick(1.0, publish=False)
            ev = rt.events()[0]
            if ev.state in (EventState.CLOSED, EventState.SAFE_STOP):
                break
        ev = rt.events()[0]
        text = " | ".join(t.text for t in ev.timeline)
        assert ev.state is EventState.CLOSED, text
        assert "已自動" in text and "放慢生料投入" in text
        assert rt.audit.verify_chain()
        assert (rt.now() - start).total_seconds() < 45 * 60
    finally:
        await client.aclose()
        await rt.stop()
