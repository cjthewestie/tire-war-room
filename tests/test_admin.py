"""後台 (issue #37): access rule (factory network + supervisor), RAG maintenance, memory notes, runs.
Every admin write must land in the audit log; no response may carry an API key."""
from __future__ import annotations

import base64
import re
import time

import pytest
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.api.app import PinGuard
from warroom.config import Settings
from warroom.contracts import AuditKind

PIN = "admin-pin-1234"


def _b64(s: str | bytes) -> str:
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()


@pytest.fixture
def rt(tmp_path) -> Runtime:
    sop = tmp_path / "sop"
    sop.mkdir()
    (sop / "base.md").write_text("# SOP-T-001 測試程序\n## 步驟\n先關蒸汽閥，再通知維修組。\n", encoding="utf-8")
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 sop_dir=sop, api_key="sk-secret-should-never-leak", nvidia_api_key="nvapi-secret-never")
    return Runtime(s, use_real=False, run_loop=False)


def _client(rt, host: str = "testclient", **kw) -> TestClient:
    return TestClient(create_app(rt, supervisor_pin=PIN, **kw), client=(host, 50000))


@pytest.fixture
def local(rt):
    with _client(rt) as c:
        yield c


def _admin_audit(rt) -> list:
    return [e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.SYSTEM and e.actor.startswith("admin:")]


# ---------------------------------------------------------------- access
def test_public_address_refused_before_pin(rt):
    with _client(rt, host="8.8.8.8") as c:
        r = c.get("/api/admin/status", headers={"X-Supervisor-Pin": PIN})
        assert r.status_code == 403
        assert c.post("/api/admin/memory/notes", json={"machine_id": "CURE-07", "text": "x"},
                      headers={"X-Supervisor-Pin": PIN}).status_code == 403


def test_forwarded_public_hop_refused(local):
    r = local.get("/api/admin/status", headers={"X-Forwarded-For": "1.1.1.1"})
    assert r.status_code == 403


def test_lan_needs_pin(rt):
    with _client(rt, host="192.168.1.20") as c:
        assert c.get("/api/admin/status").status_code == 401
        assert c.get("/api/admin/status", headers={"X-Supervisor-Pin": "wrong"}).status_code == 401
        assert c.get("/api/admin/status", headers={"X-Supervisor-Pin": PIN}).status_code == 200
        assert c.get("/api/admin/rag").status_code in (200, 401)   # paired-device cookie may carry, PIN still checked
        r = c.post("/api/admin/memory/notes", json={"machine_id": "CURE-07", "text": "lan note"})
        assert r.status_code == 401


def test_every_admin_route_is_guarded(rt):
    with _client(rt, host="10.0.0.5", pin_guard=PinGuard(per_host_max=100, global_max=100)) as c:
        for method, path in [("get", "/api/admin/status"), ("get", "/api/admin/runs"), ("get", "/api/admin/rag"),
                             ("get", "/api/admin/rag/search?q=x"), ("get", "/api/admin/rag/reindex"),
                             ("post", "/api/admin/rag/reindex"), ("get", "/api/admin/memory"),
                             ("delete", "/api/admin/rag/docs/base.md?confirm=base.md")]:
            assert getattr(c, method)(path).status_code == 401, path


def test_status_has_no_api_key(local):
    r = local.get("/api/admin/status")
    assert r.status_code == 200
    body = r.text
    assert "sk-secret" not in body and "nvapi-secret" not in body and "api_key" not in body
    j = r.json()
    assert j["audit_chain_ok"] is True
    assert j["llm_mode"] == "mock" and j["llm_host"] is None


# ---------------------------------------------------------------- RAG
def test_rag_list_upload_delete_audited(local, rt, tmp_path):
    r = local.get("/api/admin/rag").json()
    assert [d["file"] for d in r["docs"]] == ["base.md"]
    assert r["docs"][0]["sop_id"] == "SOP-T-001" and r["docs"][0]["chunks"] == 1

    up = local.post("/api/admin/rag/docs", json={"name": "new-sop.md", "content_b64": _b64("# SOP-T-002 新程序\n## 一\n檢查水流。\n")})
    assert up.status_code == 200, up.text
    assert (tmp_path / "sop" / "new-sop.md").exists()
    # no silent overwrite
    assert local.post("/api/admin/rag/docs", json={"name": "new-sop.md", "content_b64": _b64("x")}).status_code == 409
    files = [d["file"] for d in local.get("/api/admin/rag").json()["docs"]]
    assert files == ["base.md", "new-sop.md"]

    # delete needs the file name as confirmation
    assert local.delete("/api/admin/rag/docs/new-sop.md").status_code == 400
    assert local.delete("/api/admin/rag/docs/new-sop.md?confirm=new-sop.md").status_code == 200
    assert not (tmp_path / "sop" / "new-sop.md").exists()

    acts = [e.payload["admin"] for e in _admin_audit(rt)]
    assert acts == ["sop_upload", "sop_delete"]


@pytest.mark.parametrize("name", ["../evil.md", "a/b.md", "evil.sh", ".hidden.md", "x.md/"])
def test_upload_rejects_bad_names(local, name):
    r = local.post("/api/admin/rag/docs", json={"name": name, "content_b64": _b64("# x\n")})
    assert r.status_code in (400, 404, 422), (name, r.status_code)


def test_upload_rejects_fake_pdf_and_binary_text(local):
    assert local.post("/api/admin/rag/docs", json={"name": "a.pdf", "content_b64": _b64("not a pdf")}).status_code == 400
    assert local.post("/api/admin/rag/docs", json={"name": "a.txt", "content_b64": _b64(b"\xff\xfe\xfa")}).status_code == 400
    assert local.post("/api/admin/rag/docs", json={"name": "a.txt", "content_b64": "%%%"}).status_code == 400


def test_reindex_runs_in_thread_and_search(local, rt):
    r = local.post("/api/admin/rag/reindex")
    assert r.status_code == 200 and r.json()["state"] in ("running", "done")
    for _ in range(100):
        j = local.get("/api/admin/rag/reindex").json()
        if j["state"] != "running":
            break
        time.sleep(0.05)
    assert j["state"] == "done", j
    assert j["chunks"] >= 1
    hits = local.get("/api/admin/rag/search", params={"q": "蒸汽閥"}).json()
    assert hits["hits"] and hits["hits"][0]["sop_id"] == "SOP-T-001"
    assert "score" in hits["hits"][0]
    acts = [e.payload["admin"] for e in _admin_audit(rt)]
    assert "sop_reindex_start" in acts and "sop_reindex_done" in acts


# ---------------------------------------------------------------- memory
def test_memory_lists_incidents_and_adds_audited_note(local, rt, tmp_path):
    m = local.get("/api/admin/memory").json()
    assert len(m["incidents"]) == 8
    assert m["agent_reads_notes"] is True              # 「接記憶」: the commander and Agent Nemo read them now
    assert m["agent_uses"] == "Agent 規劃時會參考：機台筆記、最近 3 件過去事件"
    assert local.post("/api/admin/memory/notes", json={"machine_id": "NOPE-1", "text": "x"}).status_code == 400
    r = local.post("/api/admin/memory/notes", json={"machine_id": "CURE-07", "text": "上次 7 號機蒸汽閥卡住，先看閥"})
    assert r.status_code == 200, r.text
    m = local.get("/api/admin/memory").json()
    assert [n["machine_id"] for n in m["notes"]] == ["CURE-07"]
    assert (tmp_path / "memory_notes.json").exists()
    a = _admin_audit(rt)[-1]
    assert a.payload["admin"] == "memory_note_add" and a.payload["machine_id"] == "CURE-07"


def test_agent_code_reads_memory_notes_but_never_writes_them():
    """The agents read notes through Runtime.machine_memory / query_plant; only the 後台 POST (audited) writes."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "warroom"
    readers = [*(root / "agent").rglob("*.py"), root / "api" / "plant_query.py", root / "data" / "machine_memory.py"]
    for f in readers:
        src = f.read_text(encoding="utf-8")
        assert "MemoryNotes" not in src and not re.search(r"notes(?:\(\))?\.add\(", src), f


def test_memory_preview_is_what_the_commander_would_get(local, rt):
    assert local.get("/api/admin/memory/preview", params={"machine_id": "NOPE-1"}).status_code == 400
    assert local.get("/api/admin/memory/preview", params={"machine_id": "CURE-07"}).json()["machine_memory"] is None
    local.post("/api/admin/memory/notes", json={"machine_id": "CURE-07", "text": "7 號機蒸汽閥上週換過，疏水閥易卡"})
    local.post("/api/admin/memory/notes", json={"machine_id": "CURE-08", "text": "8 號機的事"})
    p = local.get("/api/admin/memory/preview", params={"machine_id": "CURE-07"}).json()["machine_memory"]
    assert p == rt.machine_memory("CURE-07")
    assert [n["text"] for n in p["notes"]] == ["7 號機蒸汽閥上週換過，疏水閥易卡"] and p["use"].startswith("機台筆記與過去事件")
    # CURE-03 has a team-dataset incident: past events come with no note written
    p3 = local.get("/api/admin/memory/preview", params={"machine_id": "CURE-03"}).json()["machine_memory"]
    assert [x["id"] for x in p3["past"]] == ["INC-003"] and p3["notes"] == []


# ---------------------------------------------------------------- runs
def test_runs_lists_events(local):
    local.post("/api/sim/control", json={"action": "scenario", "scenario": "press7_temp_drop"})
    local.post("/api/sim/step", json={"seconds": 600})
    j = local.get("/api/admin/runs").json()
    assert set(j["counts"]) == {"event", "command", "ask"}
    assert all({"id", "kind", "ts"} <= set(x) for x in j["runs"])
