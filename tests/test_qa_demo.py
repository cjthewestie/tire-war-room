"""QA 模式 live proofs and the walkthrough tracker (warroom/api/qa_demo.py).

Each proof's backend part: it never decides an approval, the audit proof only touches a copy, cleanup restores what
it changed, every run is bracketed by 「QA 展示」 audit rows; the tracker maps an event to the page changes it should
cause, ticked only from backend state."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from warroom.agent.audit import JsonlAuditLog
from warroom.api import Runtime, create_app
from warroom.api.qa_demo import PROOFS, TAG, WALKS, audit_tamper_check, llm_call_states
from warroom.config import Settings
from warroom.contracts import ApprovalStatus, AuditKind, ScenarioName


def _settings(tmp_path, scenario=ScenarioName.NORMAL.value) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=scenario, llm_mode="mock")


@pytest.fixture
def app_rt(tmp_path):
    rt = Runtime(_settings(tmp_path), run_loop=False)
    if rt.c.tools is None:
        pytest.skip(f"tools not ready: {rt.degraded}")
    return create_app(rt), rt


def _decided(rt) -> list[str]:
    return [a.id for a in rt.all_approvals() if a.status is not ApprovalStatus.PENDING]


def test_registry_lists_ten_proofs_and_seven_walks(app_rt):
    app, _ = app_rt
    with TestClient(app) as c:
        body = c.get("/api/qa/proofs").json()
    assert [p["id"] for p in body["proofs"]] == [p.id for p in PROOFS] and len(PROOFS) == 10
    for p in body["proofs"]:
        assert p["title"] and p["doubt"] and p["steps"] and p["expected"] and p["cleanup"]
    assert {w["id"] for w in body["walks"]} == {w.id for w in WALKS} and len(WALKS) == 7
    assert "busy" in body


@pytest.mark.parametrize("pid", ["no_rogue_write", "llm_only_plan", "hypothesis", "audit_tamper", "injection",
                                 "tools", "model", "rag_no_answer", "offline"])
def test_proof_runs_tagged_and_never_decides(app_rt, pid):
    app, rt = app_rt
    with TestClient(app) as c:
        decided0, n0 = _decided(rt), len(rt.events())
        res = c.post(f"/api/qa/proofs/{pid}/run")
        assert res.status_code == 200, res.text
        out = res.json()
        assert out["status"] in ("ok", "failed", "pending"), out
        assert out["steps"]
        assert _decided(rt) == decided0                                   # no approval decided by a proof
        if pid != "hypothesis":
            assert len(rt.events()) == n0                                 # read-only proofs open no event
        tags = [e for e in rt.audit.tail(limit=0) if e.payload.get("qa_demo") == pid]
        assert [e.payload["msg"] for e in tags] == ["start", "end"] and all(e.payload["tag"] == TAG for e in tags)
    if pid == "offline":
        assert rt.network == "online"                                    # cleanup restored the tier
    if pid == "tools":
        assert out["status"] == "ok" and len(out["data"]["tools"]) == 9
    if pid == "no_rogue_write":
        assert out["data"]["writes"] == [] and out["data"]["events_opened"] == 0


def test_audit_proof_touches_only_a_copy(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    for i in range(6):
        log.append(AuditKind.SYSTEM, "t", {"i": i})
    real = (tmp_path / "audit.jsonl").read_bytes()
    res = audit_tamper_check(log, workdir=tmp_path)
    assert res["before"]["ok"] and not res["after"]["ok"]
    assert res["after"]["fails_at_seq"] == res["tampered_seq"]
    assert (tmp_path / "audit.jsonl").read_bytes() == real and log.verify_chain()
    assert not list(tmp_path.glob("qa-audit-*"))                          # the temp copy is gone


def test_llm_calls_mapped_to_their_state():
    from types import SimpleNamespace as N
    from datetime import datetime
    t = datetime(2026, 10, 22)
    rows = [N(kind=AuditKind.STATE_TRANSITION, payload={"to": "plan"}, seq=1, ts=t, actor="sm"),
            N(kind=AuditKind.LLM_CALL, payload={"phase": "plan"}, seq=2, ts=t, actor="commander"),
            N(kind=AuditKind.STATE_TRANSITION, payload={"to": "verify"}, seq=3, ts=t, actor="sm"),
            N(kind=AuditKind.STATE_TRANSITION, payload={"to": "replan"}, seq=4, ts=t, actor="sm"),
            N(kind=AuditKind.LLM_CALL, payload={"phase": "replan"}, seq=5, ts=t, actor="commander")]
    assert [c["state"] for c in llm_call_states(rows)] == ["plan", "replan"]


def test_walkthrough_p11_tracker_ticks_from_backend_state(tmp_path):
    rt = Runtime(_settings(tmp_path), run_loop=False)
    if rt.c.tools is None:
        pytest.skip(f"tools not ready: {rt.degraded}")
    app = create_app(rt)
    with TestClient(app) as c:
        w = c.post("/api/qa/walk", json={"kind": "p11", "speed": 10}).json()
        assert w["kind"] == "p11" and w["event_id"] is None              # nothing has happened yet
        pages = [t["page"] for t in w["tracker"]]
        assert pages == ["主控台", "產線監控", "狀態機", "Agent", "手機", "3D 工廠", "稽核／報告"]
        assert not any(ch["done"] for t in w["tracker"] for ch in t["checks"])
        for _ in range(8):                                               # the detector fires, the commander plans
            assert c.post("/api/sim/step", json={"seconds": 30}).status_code == 200
        w2 = c.get(f"/api/qa/walk/{w['id']}").json()
    assert w2["event_id"] and w2["machine_id"] == "CURE-07"
    assert w2["states"][0]["state"] == "detected" and "plan" in w2["stages_reached"]
    done = {t["page"]: [ch["done"] for ch in t["checks"]] for t in w2["tracker"]}
    assert done["主控台"][0] and done["狀態機"][0] and done["狀態機"][1]
    for t in w2["tracker"]:
        for ch in t["checks"]:
            assert (ch["at"] is not None) == ch["done"]                 # every tick carries its time
    # the event is tagged for the durable task log, and the walkthrough waits for a person at the approval
    tagged = [e for e in rt.audit.tail(limit=0) if e.event_id == w2["event_id"] and e.payload.get("tag") == TAG]
    assert tagged
    if w2["waiting_approval"]:
        assert w2["paused_for"] == w2["waiting_approval"][0] and rt.paused
        assert all(a.status is ApprovalStatus.PENDING for a in rt.all_approvals() if a.event_id == w2["event_id"])
    rows = rt.tasklog.rows() if rt.tasklog is not None else []
    assert any(r.get("qa_demo") for r in rows if r["id"] == w2["event_id"]) or not rows


def test_walk_unknown_kind_404(app_rt):
    app, _ = app_rt
    with TestClient(app) as c:
        assert c.post("/api/qa/walk", json={"kind": "nope"}).status_code == 404
        assert c.get("/api/qa/walk/WALK-999").status_code == 404
        assert c.post("/api/qa/proofs/nope/run").status_code == 404


def test_safety_proof_cleans_up_its_fault_even_without_a_stop(app_rt, monkeypatch):
    """No sim loop here, so no Safety Core stop arrives: the proof says so and still removes the fault it injected."""
    import warroom.api.qa_demo as qd
    monkeypatch.setattr(qd, "PROOF_TIMEOUT_S", 1.0)
    app, rt = app_rt
    with TestClient(app) as c:
        decided0 = _decided(rt)
        out = c.post("/api/qa/proofs/safety_core/run").json()
    assert out["status"] == "failed" and not out["steps"][-1]["ok"]
    assert not [f for f in rt.sim.faults(active_only=False) if f.machine_id == "B-OPEN" and f.id.startswith("F-QA-")]
    assert _decided(rt) == decided0
    assert any(e.payload.get("msg") == "faults cleared" and e.payload.get("machine_id") == "B-OPEN" for e in rt.audit.tail(limit=0))
