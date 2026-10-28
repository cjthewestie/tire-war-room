"""後台 效能與追蹤 (issue #199): the bench-file parser, the audit-log trace and the load-test job + routes.
Every number on the page must come from a file in bench/results/ or from the running app's audit log."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.api import admin_perf as PF
from warroom.config import Settings
from warroom.contracts import AuditKind

PIN = "perf-pin-1234"
SPARK_HOST = {"hostname": "aitopatom-3ef3", "platform": "Linux-7.0.0-1019-nvidia-aarch64-with-glibc2.39",
              "machine": "aarch64", "ram_gb": 121.6, "cpu_count": 20}
MAC_HOST = {"hostname": "mac-studio.lan", "platform": "macOS-26.4-arm64-arm-64bit", "machine": "arm64", "cpu_count": 16}


def _w(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")


def _v(name, measured, passed, target="t", note=""):
    return {"name": name, "target": target, "measured": measured, "passed": passed, "note": note}


@pytest.fixture
def results(tmp_path, monkeypatch) -> Path:
    """A small bench/results/ tree shaped like the real one, under a fake repo root."""
    monkeypatch.setattr(PF, "REPO", tmp_path)
    r = tmp_path / "bench" / "results"
    _w(r / "spark-lightning-20261021" / "bench" / "decode-lightning-20261021-191933.json",
       {"bench": "decode", "recorded_at": "2026-10-21T19:19:33+08:00", "host": SPARK_HOST,
        "endpoint": {"model": "lightning", "kind": "vllm"}, "host_load_avg_1_5_15": [2.4, 1.7, 1.5],
        "verdicts": [_v("decode_tps_p50", 126.405, True)]})
    # a newer run whose model server was down: no number, must not hide the real one
    _w(r / "spark-bolt-20261021" / "decode-bolt-20261021-202120.json",
       {"bench": "decode", "recorded_at": "2026-10-21T20:21:20+08:00", "host": SPARK_HOST,
        "endpoint": {"model": "bolt"}, "verdicts": [_v("decode_tps_p50", None, None)]})
    _w(r / "spark-lightning-20261021" / "bench" / "ttft-lightning-20261021-192043.json",
       {"bench": "ttft", "recorded_at": "2026-10-21T19:20:43+08:00", "host": SPARK_HOST, "endpoint": {"model": "lightning"},
        "verdicts": [_v("first_alert_card_p90", 2.1132, False), _v("ttft_warm_32k_p90", 1.6338, False),
                     _v("ttft_cold_32k_p50", 6.7122, None)]})
    _w(r / "ttft-mac-20261003-235014.json",
       {"bench": "ttft", "recorded_at": "2026-10-03T23:50:14+08:00", "host": MAC_HOST, "host_load_avg_1_5_15": [7.55, 10.92, 33.6],
        "verdicts": [_v("first_alert_card_p90", 1.845, True)]})
    _w(r / "full_event-mac-20261003-164430.json",
       {"bench": "full_event", "recorded_at": "2026-10-03T16:44:30+08:00", "host": MAC_HOST, "think": True,
        "verdicts": [_v("full_event_p50", 38.7, False)]})
    _w(r / "memory-local-20261003-235148.json",
       {"bench": "memory", "recorded_at": "2026-10-03T23:51:48+08:00", "host": MAC_HOST, "stack_gb": 27.35,
        "judgement": {"band_gb": [36.0, 50.0], "total_gb": 37.35, "verdict": "UNJUDGED", "os_basis": "budget", "os_gb": 10.0,
                      "context": {"min": 16384, "required": 32768}, "reasons": ["ctx 16384 < 32768"]}})
    _w(r / "tokens_per_watt-mac-20261003-203456.json",
       {"bench": "tokens_per_watt", "recorded_at": "2026-10-03T20:34:56+08:00", "host": MAC_HOST,
        "plug": {"tokens_per_s": 99.786, "tokens_per_joule": None}})
    _w(r / "mac-p11-fix-20261022" / "runs" / "summary.json",
       [{"run": i, "end": "closed", "h2_card_s": 140.0 + i} for i in range(1, 6)])
    _w(r / "mac-p11-fix-20261022" / "before-fix" / "summary-no-pin-no-bind.json", {"bench": "decode", "host": MAC_HOST,
                                                                                    "verdicts": [_v("decode_tps_p50", 1.0, False)]})
    (r / "spark-lightning-20261021" / "p11").mkdir(parents=True, exist_ok=True)
    (r / "spark-lightning-20261021" / "p11" / "summary.txt").write_text(
        "p11-afb2553 LLM:plan/x > END:safe_stop\n   plan    prompt=6405 out=1182 ttft=1.17s total=10.17s\n"
        "p11-afb2553-r1 LLM:plan/y > END:safe_stop\n   plan    prompt=6405 out=1208 ttft=1.13s total=10.96s\n", encoding="utf-8")
    _w(r / "spark-bolt-20261021" / "compare.json", {"summary": {"bolt_replay": {
        "main": {"calls": 26, "total_s": {"median": 3.29}, "prompt_tokens": {"median": 3340.5}},
        "commander": {"calls": 1, "ttft_s": {"median": 3.48}, "decode_tps": {"median": 84.1}, "total_s": {"median": 12.1}}}}})
    (r / "SUMMARY.md").write_text(
        "| bench | metric | target | measured | verdict | file |\n|---|---|---|---|---|---|\n"
        "| full_event-think | full_event_p50 | ≈ 22.0 s | 38.727 | STALE, not counted (was FAIL): old | full_event-mac-20261003-164430.json |\n",
        encoding="utf-8")
    return r


def _row(b, key):
    return next(r for r in b["rows"] if r["key"] == key)


def test_bench_rows_per_host_from_files(results):
    b = PF.bench(results)
    dec = _row(b, "decode_tps_p50")
    assert dec["cells"]["DGX Spark"]["measured"] == 126.405 and dec["cells"]["DGX Spark"]["verdict"] == "PASS"
    assert dec["cells"]["DGX Spark"]["source"].endswith("decode-lightning-20261021-191933.json")   # not the empty Bolt run
    assert dec["cells"]["DGX Spark"]["date"] == "2026-10-21"
    assert dec["cells"]["Mac"] is None                                                               # → 未量 on the page
    fa = _row(b, "first_alert_card_p90")
    assert fa["target"] == "≤ 2 s"                                                                   # from warroom/factory.py
    assert (fa["cells"]["DGX Spark"]["verdict"], fa["cells"]["Mac"]["verdict"]) == ("FAIL", "PASS")
    assert "主機忙" in fa["cells"]["Mac"]["note"]
    assert _row(b, "ttft_cold_32k_p50")["cells"]["DGX Spark"]["verdict"] == "參考"
    assert _row(b, "full_event_p50")["cells"]["Mac"]["verdict"] == "過期"                            # STALE in SUMMARY.md
    mem = _row(b, "stack_plus_os_gb")["cells"]["Mac"]
    assert mem["measured"] == 37.35 and mem["verdict"] == "未判定"
    assert _row(b, "tokens_per_joule")["cells"]["Mac"]["verdict"] == "未量"
    assert _row(b, "concurrent_3_worst_alert")["cells"] == {"DGX Spark": None, "Mac": None}
    assert b["counts"]["DGX Spark"]["PASS"] == 1


def test_bench_p11_and_model_compare(results):
    b = PF.bench(results)
    p11 = _row(b, "p11_closed")["cells"]
    assert (p11["DGX Spark"]["measured"], p11["DGX Spark"]["verdict"], p11["DGX Spark"]["code"]) == ("0/2", "FAIL", "afb2553")
    assert (p11["Mac"]["measured"], p11["Mac"]["verdict"], p11["Mac"]["date"]) == ("5/5", "PASS", "2026-10-22")
    assert _row(b, "commander_ttft_app")["cells"]["DGX Spark"]["measured"] == 1.13
    bolt = next(m for m in b["models"] if m["run"].startswith("Bolt"))
    assert (bolt["main_p50_s"], bolt["commander_ttft_s"], bolt["host"]) == (3.29, 3.48, "DGX Spark")
    assert all("before-fix" not in (c or {}).get("source", "") for r in b["rows"] for c in r["cells"].values())


def test_bench_survives_broken_files(results):
    (results / "bad.json").write_text("{not json", encoding="utf-8")
    (results / "turns.json").write_text("[1, 2]", encoding="utf-8")
    assert PF.bench(results)["files"] >= 1


# ---------------------------------------------------------------- trace
def _line(seq, kind, actor, payload, ts="2026-10-23T10:00:00+08:00", event_id=None) -> bytes:
    return json.dumps({"seq": seq, "ts": ts, "kind": kind, "actor": actor, "event_id": event_id, "payload": payload},
                      ensure_ascii=False).encode()


def _audit_lines() -> list[bytes]:
    rows = [
        _line(1, "llm_call", "commander", {"phase": "plan", "total_ms": 9000, "ttft_ms": 1200, "prompt_tokens": 6800,
                                           "completion_tokens": 1000, "model": "lightning"}, event_id="EV-1"),
        _line(2, "llm_call", "commander", {"phase": "plan", "total_ms": 11000, "ttft_ms": 1400, "prompt_tokens": 7000,
                                           "completion_tokens": 1100, "model": "lightning"}, event_id="EV-2"),
        _line(3, "llm_call", "main_agent", {"phase": "chat", "total_ms": 3000, "prompt_tokens": 3400, "completion_tokens": 40,
                                            "conversation": "task:EV-2|supervisor", "tool_calls": [{"name": "x"}]}),
        _line(4, "llm_call", "ask", {"phase": "answer", "total_ms": 0, "skipped": True}),
        _line(5, "llm_call", "commander", {"phase": "replan", "total_ms": 500, "error": "timeout"}, event_id="EV-1"),
        _line(6, "tool_call", "tool:solve_schedule", {"args": {}}),
        _line(7, "tool_result", "tool:solve_schedule", {"duration_ms": 1700.0, "ok": True}),
        _line(8, "tool_result", "tool:solve_schedule", {"duration_ms": 3800.0, "ok": False}),
        _line(9, "tool_result", "tool:read_sensors", {"duration_ms": 11.5, "ok": True}),
        _line(10, "system", "main_agent", {"orch": "queue", "who": "commander", "label": "機台處理", "waited_ms": 0.3}),
        _line(11, "system", "main_agent", {"orch": "queue", "who": "ask", "label": "問戰情室", "waited_ms": 4579.4}),
        _line(12, "llm_call", "commander", {"phase": "plan", "total_ms": 99999}, ts="2026-10-22T10:00:00+08:00"),
    ]
    return rows[::-1]                  # newest first, as AA.source gives it


def test_trace_phases_tools_queue():
    t = PF.trace(_audit_lines(), limit=200, day="2026-10-23", session_no=lambda e: ("2026-10-23", 3) if e == "EV-2" else None)
    assert t["totals"]["calls"] == 5 and t["totals"]["errors"] == 1               # the 10-22 call is another day
    assert t["totals"]["tokens_in"] == 6800 + 7000 + 3400
    plan = next(p for p in t["phases"] if p["key"] == "commander/plan")
    assert (plan["calls"], plan["ms"]["p50"], plan["ms"]["p90"], plan["ttft_ms"]["p50"]) == (2, 9000, 11000, 1200)
    assert plan["label"] == "指揮官・擬計劃"
    ans = next(p for p in t["phases"] if p["key"] == "ask/answer")
    assert ans["skipped"] == 1 and ans["ms"]["n"] == 0
    ss = next(x for x in t["tools"] if x["tool"] == "solve_schedule")
    assert (ss["n"], ss["p50"], ss["max"], ss["errors"]) == (2, 1700, 3800, 1)
    assert t["queue"]["n"] == 2 and t["queue"]["over_1s"] == 1 and t["queue"]["max"] == 4579.4
    chat = next(c for c in t["last"] if c["phase"] == "chat")
    assert chat["event_id"] == "EV-2" and chat["session"] == "Nemo-1023-#03" and chat["tool_calls"] == 1
    assert t["last"][0]["seq"] == 5 and t["last"][0]["error"] == "timeout"


def test_trace_recent_limit():
    t = PF.trace(_audit_lines(), limit=2)
    assert t["scope"] == "recent" and t["totals"]["calls"] == 2
    assert [c["seq"] for c in t["last"]] == [12, 5]


# ---------------------------------------------------------------- load job
def test_load_refusals():
    assert "規則模式" in PF.LoadJob.refuse("mock", "http://127.0.0.1:8000/v1", None)
    assert "不在本機" in PF.LoadJob.refuse("llm", "https://integrate.api.nvidia.com/v1", None)
    busy = {"running": [{"who": "commander"}], "waiting": []}
    assert "佇列" in PF.LoadJob.refuse("llm", "http://127.0.0.1:8000/v1", busy)
    assert PF.LoadJob.refuse("llm", "http://localhost:8000/v1", {"running": [], "waiting": []}) is None


def test_load_job_runs_one_at_a_time(tmp_path, monkeypatch):
    monkeypatch.setattr(PF, "REPO", tmp_path)
    out = tmp_path / "res.json"
    out.write_text(json.dumps({"verdicts": [_v("concurrent_3_worst_alert", 6.5, False)]}), encoding="utf-8")
    script = f"import time; time.sleep(0.4); print('saved {out.as_posix()}')"
    j = PF.LoadJob(results=tmp_path)
    seen = []
    assert j.start("http://127.0.0.1:8000/v1", "lightning", "t", seen.append, cmd=[sys.executable, "-c", script])
    assert not j.start("http://127.0.0.1:8000/v1", "lightning", "t", cmd=[sys.executable, "-c", script])
    for _ in range(100):
        if j.state != "running":
            break
        time.sleep(0.05)
    assert j.state == "done" and j.file == "res.json" and seen == [j]
    assert j.verdicts == [{"name": "concurrent_3_worst_alert", "measured": 6.5, "target": "t", "verdict": "FAIL"}]


def test_load_job_failure_is_reported(tmp_path):
    j = PF.LoadJob(results=tmp_path)
    j.start("http://127.0.0.1:8000/v1", "m", "t", cmd=[sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])
    for _ in range(100):
        if j.state != "running":
            break
        time.sleep(0.05)
    assert j.state == "failed" and "exit 3" in j.error


# ---------------------------------------------------------------- routes
@pytest.fixture
def rt(tmp_path) -> Runtime:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 sop_dir=tmp_path / "sop", api_key="sk-secret-should-never-leak")
    return Runtime(s, use_real=False, run_loop=False)


def test_routes_need_factory_network_and_read_only(rt):
    with TestClient(create_app(rt, supervisor_pin=PIN), client=("8.8.8.8", 50000)) as c:
        for path in ("/api/admin/bench", "/api/admin/trace", "/api/admin/bench/load"):
            assert c.get(path, headers={"X-Supervisor-Pin": PIN}).status_code == 403
    with TestClient(create_app(rt, supervisor_pin=PIN), client=("testclient", 50000)) as c:
        b = c.get("/api/admin/bench")
        assert b.status_code == 200 and {r["key"] for r in b.json()["rows"]} >= {"decode_tps_p50", "tokens_per_joule"}
        assert b.json()["load"]["state"] == "idle"
        rt.audit.append(AuditKind.LLM_CALL, "commander", {"phase": "plan", "total_ms": 1234.0, "prompt_tokens": 10,
                                                          "completion_tokens": 5, "model": "m"}, event_id="EV-X")
        t = c.get("/api/admin/trace?scope=recent").json()
        assert t["totals"]["calls"] >= 1 and t["last"][0]["ms"] == 1234.0
        assert "open_events" in t["now"] and t["now"]["llm_mode"] == "mock"
        assert c.get("/api/admin/trace?scope=bogus").status_code == 422
        r = c.post("/api/admin/bench/load")                      # mock mode: refused, nothing started
        assert r.status_code == 409 and "規則模式" in r.json()["detail"]
        for body in (b.text, json.dumps(t)):
            assert "sk-secret" not in body
