"""後台 稽核紀錄 / 系統參數 / 系統健康: the audit viewer filters and pages newest first and never writes (except
its own audit of an export), the params read the LIVE settings and constants, the health cards say each
subsystem in words, and every new route needs the factory network + supervisor."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from warroom.agent.audit import JsonlAuditLog
from warroom.api import Runtime, create_app
from warroom.api import admin_audit as AA
from warroom.api import admin_params as AP
from warroom.api.admin import _audit_len
from warroom.api.app import PinGuard
from warroom.config import Settings
from warroom.contracts import AuditKind

PIN = "admin-pin-1234"
NEW_ROUTES = ["/api/admin/audit", "/api/admin/audit/verify", "/api/admin/audit/export", "/api/admin/params"]


def _rt(tmp_path, **kw) -> Runtime:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 **kw)
    return Runtime(s, use_real=False, run_loop=False)


def _client(rt, host: str = "testclient", **kw) -> TestClient:
    return TestClient(create_app(rt, supervisor_pin=PIN, **kw), client=(host, 50000))


@pytest.fixture
def rt(tmp_path) -> Runtime:
    r = _rt(tmp_path)
    for i in range(250):
        kind = (AuditKind.LLM_CALL, AuditKind.TOOL_CALL, AuditKind.STATE_TRANSITION, AuditKind.SYSTEM)[i % 4]
        payload = {AuditKind.LLM_CALL: {"phase": "plan", "model": "nemo-test", "prompt_tokens": 1000 + i,
                                        "completion_tokens": 300, "total_ms": 4000.0 + i, "plant_sha256": "ab" * 32},
                   AuditKind.TOOL_CALL: {"args": {"machine_id": "CURE-07"}},
                   AuditKind.STATE_TRANSITION: {"from": "plan", "to": "simulate"},
                   AuditKind.SYSTEM: {"msg": f"note {i}" + (" 蒸汽閥" if i == 103 else "")}}[kind]
        r.audit.append(kind, "commander" if kind == AuditKind.LLM_CALL else "state_machine", payload,
                       event_id=f"EV-{i % 3}")
    r.audit.append(AuditKind.SYSTEM, "safety_filter", {"msg": "blocked", "side": "input", "category": "credentials",
                                                       "matched": "密***"})
    return r


@pytest.fixture
def local(rt):
    with _client(rt) as c:
        yield c


# ---------------------------------------------------------------- access
def test_new_routes_need_admin(rt):
    with _client(rt, host="8.8.8.8") as c:
        for path in NEW_ROUTES:
            assert c.get(path, headers={"X-Supervisor-Pin": PIN}).status_code == 403, path
    with _client(rt, host="10.0.0.5", pin_guard=PinGuard(per_host_max=100, global_max=100)) as c:
        for path in NEW_ROUTES:
            assert c.get(path).status_code == 401, path
            assert c.get(path, headers={"X-Supervisor-Pin": PIN}).status_code == 200, path


def test_audit_viewer_is_read_only(local):
    app = local.app
    audit_routes = [r for r in app.routes if getattr(r, "path", "").startswith("/api/admin/audit")]
    assert audit_routes
    assert all(getattr(r, "methods", set()) <= {"GET", "HEAD"} for r in audit_routes)
    for m in ("delete", "put", "post", "patch"):
        assert getattr(local, m)("/api/admin/audit").status_code == 405


# ---------------------------------------------------------------- 稽核紀錄
def test_audit_pages_newest_first(local, rt):
    last = _audit_len(rt.audit)
    a = local.get("/api/admin/audit").json()
    seqs = [x["seq"] for x in a["rows"]]
    assert len(seqs) == 100 and seqs[0] == last and seqs == sorted(seqs, reverse=True)
    b = local.get(f"/api/admin/audit?cursor={a['next_cursor']}").json()
    assert b["rows"][0]["seq"] == seqs[-1] - 1
    seen = seqs + [x["seq"] for x in b["rows"]]
    cur = b["next_cursor"]
    while cur:
        p = local.get(f"/api/admin/audit?cursor={cur}").json()
        seen += [x["seq"] for x in p["rows"]]
        cur = p["next_cursor"]
    assert sorted(seen) == list(range(1, last + 1))
    assert {k["id"] for k in a["kinds"]} >= {"llm_call", "tool_call", "gate", "safety_action", "system"}


def test_audit_filters(local, rt):
    rows = local.get("/api/admin/audit?kind=llm_call&limit=500").json()["rows"]
    assert len(rows) == 63 and all(r["kind"] == "llm_call" and r["kind_label"] == "模型呼叫" for r in rows)
    llm = rows[0]["llm"]
    assert llm["model"] == "nemo-test" and llm["prompt_tokens"] and llm["total_ms"] and llm["plant_sha256"] == "ab" * 32
    assert "nemo-test" in rows[0]["summary"] and rows[0]["hash"] and rows[0]["prev_hash"]
    ev = local.get("/api/admin/audit?event=EV-1&kind=tool_call&limit=500").json()["rows"]
    assert ev and all(r["event_id"] == "EV-1" and r["kind"] == "tool_call" for r in ev)
    hit = [e.seq for e in rt.audit.tail(0, 10**9) if "蒸汽閥" in json.dumps(e.payload, ensure_ascii=False)]
    assert [r["seq"] for r in local.get("/api/admin/audit?q=蒸汽閥").json()["rows"]] == hit and len(hit) == 1
    sf = local.get("/api/admin/audit?actor=safety_filter").json()["rows"]
    assert len(sf) == 1 and sf[0]["filter"] == {"category": "credentials", "matched": "密***", "side": "input"}
    assert local.get("/api/admin/audit?kind=nonsense").status_code == 400


def test_audit_time_range(local, rt):
    ts = [e.ts for e in rt.audit.tail(0, 10**9)]
    t0 = ts[0].isoformat()
    assert local.get("/api/admin/audit", params={"from": t0, "to": t0}).json()["rows"]
    future = ts[-1].replace(year=ts[-1].year + 1).isoformat()
    assert local.get("/api/admin/audit", params={"from": future}).json()["rows"] == []


def test_export_is_audited_and_filtered(local, rt):
    before = _audit_len(rt.audit)
    r = local.get("/api/admin/audit/export?kind=state_transition")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert lines and all(x["kind"] == "state_transition" for x in lines)
    assert [x["seq"] for x in lines] == sorted(x["seq"] for x in lines)          # oldest first
    assert _audit_len(rt.audit) == before + 1
    e = rt.audit.tail(0, 10**9)[-1]
    assert e.actor.startswith("admin:") and e.payload["admin"] == "audit_export" and e.payload["rows"] == len(lines)


def test_file_scan_is_fast_and_paged(tmp_path):
    """10k+ entries on disk: a page reads from the end and stops early."""
    p = tmp_path / "audit.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for i in range(1, 12_001):
            f.write(json.dumps({"seq": i, "ts": "2026-10-22T10:00:00+08:00", "kind": "system" if i % 10 else "llm_call",
                                "actor": "x", "event_id": None, "payload": {"msg": f"m{i}"}, "prev_hash": "0", "hash": "1"},
                               separators=(",", ":")) + "\n")
    log = type("L", (), {"path": p})()
    newest, oldest = AA.source(log)
    t = time.perf_counter()
    a = AA.page(newest(), AA.Filter())
    assert time.perf_counter() - t < 0.5
    assert a["rows"][0]["seq"] == 12_000 and a["scanned"] <= 101 and a["next_cursor"] == 11_901
    b = AA.page(newest(), AA.Filter(kind="llm_call"), cursor=a["next_cursor"])
    assert b["rows"][0]["seq"] == 11_900 and len(b["rows"]) == 100
    lines, cut = AA.export_lines(oldest(), AA.Filter(kind="llm_call"), limit=50)
    assert len(lines) == 50 and cut


def test_verify_report_names_the_broken_entry(tmp_path):
    log = JsonlAuditLog(tmp_path / "a.jsonl", key=b"k" * 32)
    for i in range(5):
        log.append(AuditKind.SYSTEM, "t", {"i": i})
    assert log.verify_report() == {"ok": True, "checked": 5, "last_seq": 5, "broken_at": None, "problem": None}
    lines = log.path.read_text(encoding="utf-8").splitlines()
    lines[2] = lines[2].replace('"i":2', '"i":9')
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep = log.verify_report()
    assert rep["ok"] is False and rep["broken_at"] == 3 and rep["last_seq"] == 2


# ---------------------------------------------------------------- 系統參數
def _rows(j) -> dict[str, dict]:
    return {f"{g['id']}.{r['key']}": r for g in j["groups"] for r in g["rows"]}


def test_params_read_live_values(tmp_path, monkeypatch):
    r = _rt(tmp_path, llm_timeout_s=17.5, approval_timeout_s=123.0, model="live-model")
    from warroom.safety import RuleSafetyCore
    r.c.safety = RuleSafetyCore(confirm_samples=3)
    with _client(r) as c:
        rows = _rows(c.get("/api/admin/params").json())
        assert rows["model.llm_timeout_s"]["value"] == 17.5
        assert rows["commander.approval_timeout_s"]["value"] == 123.0
        assert rows["model.model"]["value"] == "live-model"
        assert rows["model.llm_timeout_s"]["source"].startswith("env LLM_TIMEOUT_S（warroom/config.py:")
        assert rows["nemo.MAX_STEPS"]["value"] == 6 and rows["nemo.MAX_STEPS"]["source"].startswith("warroom/agent/main_agent.py:")
        assert rows["safety.COMPOUND_STOP_C"]["value"] == 120.0
        assert rows["safety.SR-CURE-LOW-120"]["enabled"] and rows["safety.confirm_samples"]["value"] == 3
        assert all(x["runtime"] is False for x in rows.values())
        tools = [k for k in rows if k.startswith("tools.") and k != "tools.TOOLS_BY_STATE"]
        assert len(tools) == 9
        # a constant changed in the running process shows at once (read at request time, not copied)
        from warroom.agent import main_agent
        monkeypatch.setattr(main_agent, "MAX_STEPS", 9)
        r.settings.llm_timeout_s = 42.0
        rows = _rows(c.get("/api/admin/params").json())
        assert rows["nemo.MAX_STEPS"]["value"] == 9 and rows["model.llm_timeout_s"]["value"] == 42.0
        # a value missing on this build reads 未啟用, never a guess
        monkeypatch.delattr(main_agent, "MEMORY_TURNS")
        m = _rows(c.get("/api/admin/params").json())["nemo.MEMORY_TURNS"]
        assert m["enabled"] is False and m["value"] is None


def test_params_never_leak_secrets(tmp_path):
    r = _rt(tmp_path, api_key="sk-secret-never", nvidia_api_key="nvapi-secret-never")
    with _client(r) as c:
        body = c.get("/api/admin/params").text
        assert "sk-secret" not in body and "nvapi-secret" not in body and PIN not in body


# ---------------------------------------------------------------- 系統健康
def _base(**kw):
    st = {"llm_mode": "llm", "model": "m", "llm_host": "h:1", "llm_reachable": True, "llm_latency": {"median_ms": 4200, "calls": 5},
          "kb_ok": True, "kb_mode": "vector", "team_mode": "vector", "kb_docs": 5, "kb_chunks": 40, "team_docs": 3,
          "team_chunks": 20, "audit_chain_ok": True, "audit_entries": 10, "audit_last_seq": 10, "network": "online",
          "scenario": "day_loop", "factory_time": "2026-10-22T10:00:00+08:00", "speed": 4.0, "paused": False,
          "day_loop": {"loop": 2}, "safety_rules": ["SR-A", "SR-B"], "safety_actions_today": 0, "memory_notes": 1,
          "conversations": 2, "degraded": {}, "uptime_s": 3725, "version": "0.1.0", "commit": "abc"}
    st.update(kw)
    return {c["id"]: c for c in AP.health(st)}


def test_health_cards_all_green():
    cards = _base()
    assert list(cards) == ["llm", "kb", "audit", "net", "sim", "safety", "memory", "service"]
    assert all(c["light"] == "ok" for c in cards.values())
    assert all(c["meaning"] and c["fix"] for c in cards.values())
    assert "4.2 秒" in " ".join(cards["llm"]["facts"])
    assert cards["sim"]["facts"][-1] == "一日循環第 2 輪"
    assert "1 小時 2 分" in cards["service"]["headline"]


@pytest.mark.parametrize(("kw", "card", "light", "words"), [
    ({"llm_reachable": False}, "llm", "bad", "連不上"),
    ({"llm_mode": "mock"}, "llm", "off", "規則模式"),
    ({"kb_mode": "lexical"}, "kb", "warn", "只有字詞"),
    ({"kb_ok": False}, "kb", "bad", "不可用"),
    ({"audit_chain_ok": False}, "audit", "bad", "✗"),
    ({"network": "weak"}, "net", "warn", "弱網"),
    ({"network": "wan_down"}, "net", "warn", "外網"),
    ({"network": "lan_down"}, "net", "bad", "廠內網路斷線"),
    ({"safety_actions_today": 2}, "safety", "warn", "2 條規則"),
    ({"safety_rules": []}, "safety", "bad", "沒有規則"),
    ({"paused": True}, "sim", "warn", "已暫停"),
])
def test_health_card_lights(kw, card, light, words):
    c = _base(**kw)[card]
    assert c["light"] == light and words in c["headline"] + c["meaning"]


def test_health_degraded_in_words_not_keys():
    c = _base(degraded={"kb": "ImportError: x", "scheduler": "boom"})["service"]
    assert c["light"] == "warn" and "SOP 知識庫" in c["meaning"] and "排程求解器" in c["meaning"]
    assert "{}" not in c["meaning"] and c["degraded"][0] == {"name": "SOP 知識庫", "why": "ImportError: x"}
    lan = _base(network="lan_down")["net"]
    assert "現場觸控螢幕" in lan["meaning"]


def test_status_carries_health(local):
    j = local.get("/api/admin/status").json()
    ids = [c["id"] for c in j["health"]]
    assert ids == ["llm", "kb", "audit", "net", "sim", "safety", "memory", "service"]
    assert j["version"] and "degraded" in j
