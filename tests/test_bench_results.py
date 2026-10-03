"""bench/results fixes: open-book quiz has every source in the prompt, evidence-first answers, the macOS
no-root offline sandbox for the unplug test, and the report's 'Open against page 10' section."""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bench import accuracy, report, unplug_test
from bench.prompts import schedule_facts_context

QUIZ = Path(__file__).resolve().parent.parent / "warroom" / "kb" / "quiz" / "factory_quiz.jsonl"


def test_schedule_facts_carry_the_page6_numbers():
    t = schedule_facts_context()
    assert "大胎：開始貼合 T−1 時 30 分" in t           # FQ-017
    assert "中胎：開始貼合 T−1 時；A 線投料 T−2 時 30 分" in t   # FQ-018
    assert "小胎：開始貼合 T−40 分；A 線投料 T−1 時 40 分" in t
    assert "13.3 → 9.3 週" in t and "2.4 → 2.0 週" in t     # FQ-024 had no source before
    assert "AI 排出模時刻：7／6／4.9" in t


def test_open_book_prompt_contains_schedule_facts_closed_book_does_not():
    assert "13.3 → 9.3 週" in accuracy.system_prompt(open_book=True)
    assert "13.3 → 9.3 週" not in accuracy.system_prompt(open_book=False)


def test_every_open_book_mc_answer_text_is_in_the_prompt():
    """Each multiple-choice answer's key number appears somewhere in the open-book prompt."""
    prompt = accuracy.system_prompt(open_book=True).replace(" ", "")
    items = accuracy.load_quiz(QUIZ)
    for fid in ("FQ-017", "FQ-018", "FQ-024", "FQ-030"):
        it = next(i for i in items if i["id"] == fid)
        ans = it["choices"]["ABCDEFGH".index(it["answer_letter"])].replace(" ", "")
        assert ans.split("→")[-1] in prompt, (fid, ans)


def test_cite_schema_puts_evidence_before_answer():
    s = accuracy.mc_schema(4, cite=True)
    assert list(s["properties"]) == ["evidence", "answer"] and s["required"] == ["evidence", "answer"]
    assert list(accuracy.mc_schema(4)["properties"]) == ["answer"]
    msgs = accuracy.mc_messages({"question": "q", "choices": ["a", "b"]}, open_book=True, cite=True)
    assert "evidence" in msgs[1]["content"]


def test_parse_sandbox_denies_counts_only_our_pids():
    log = ("2026 E kernel[0:1] (Sandbox) Sandbox: Python(9175) deny(1) network-outbound remote:*:443\n"
           "2026 E kernel[0:1] (Sandbox) 3 duplicate reports for Sandbox: Python(9175) deny(1) network-outbound "
           "remote:*:443\n"
           "2026 E kernel[0:1] (Sandbox) Sandbox: Python(9175) deny(1) network-outbound\n"      # no remote: local
           "2026 E kernel[0:1] (Sandbox) 2 duplicate reports for Sandbox: Python(9175) deny(1) network-outbound\n"
           "2026 E kernel[0:1] (Sandbox) Sandbox: curl(42) deny(1) network-outbound remote:*:80\n")
    assert unplug_test.parse_sandbox_denies(log, {9175}) == 4
    assert unplug_test.sandbox_deny_detail(log, {9175})["no_remote_by_pid"] == {9175: 3}
    assert unplug_test.parse_sandbox_denies(log, {1}) == 0


def test_sandbox_argv_reruns_module_under_profile():
    argv = unplug_test.sandbox_argv(["--sandbox", "--watch", "0"])
    assert argv[:3] == ["sandbox-exec", "-p", unplug_test.SANDBOX_PROFILE]
    assert argv[3:6] == [sys.executable, "-m", "bench.unplug_test"] and argv[-1] == "0"
    assert '(deny network-outbound)' in unplug_test.SANDBOX_PROFILE


def test_sandboxed_block_verdict_note():
    out = {"block": {"applied": True, "method": "sandbox-exec"}, "sockets": {"outbound_connections": 0},
           "egress": {"firewall_dropped_packets": 0, "conntrack_available": False, "conntrack_outbound_flows": []},
           "internet_reachable_during_test": False, "app": {}}
    v = {x["name"]: x for x in unplug_test.verdicts(out, 0.4, False)}
    assert v["outbound_connections"]["passed"] is True and "sandbox-exec" in v["outbound_connections"]["note"]
    assert v["internet_blocked"]["passed"] is True


@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("sandbox-exec"), reason="macOS only")
def test_sandbox_profile_really_blocks_internet_but_not_localhost():
    code = ("import socket\n"
            "srv = socket.socket(); srv.bind(('127.0.0.1', 0)); srv.listen(1)\n"
            "socket.create_connection(srv.getsockname(), timeout=2).close()\n"
            "try:\n    socket.create_connection(('1.1.1.1', 443), timeout=3); print('OPEN')\n"
            "except OSError as e:\n    print('BLOCKED', e.errno)\n")
    r = subprocess.run(["sandbox-exec", "-p", unplug_test.SANDBOX_PROFILE, sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.startswith("BLOCKED"), r.stdout


def _write(d: Path, name: str, data: dict) -> None:
    (d / name).write_text(json.dumps(data), encoding="utf-8")


def test_report_open_items(tmp_path):
    host = {"hostname": "mac-studio.lan"}
    _write(tmp_path, "full_event-x-1.json", {"bench": "full_event", "host": host, "think": True,
           "recorded_at": "1", "endpoint": {"model": "m", "kind": "ollama"}, "plan_valid_rate": 0.0,
           "verdicts": [{"name": "full_event_p50", "target": "x", "measured": 38.7, "passed": False, "note": ""}]})
    _write(tmp_path, "tokens_per_watt-x-1.json", {"bench": "tokens_per_watt", "host": host, "recorded_at": "1",
           "endpoint": {"model": "m", "kind": "ollama"}, "plug": {"tokens_per_s": 100, "tokens_per_joule": None}})
    _write(tmp_path, "unplug-x-1.json", {"bench": "unplug", "host": host, "recorded_at": "1",
           "endpoint": {"model": "m", "kind": "ollama"}, "block": {"applied": True, "method": "sandbox-exec"}})
    text = report.build(tmp_path)
    assert "## Open against page 10" in text
    assert "No DGX Spark numbers yet" in text and "Reasoning must stay OFF" in text and "38.700" in text
    assert "tokens/W not published" in text and "process-level (Mac)" in text
    assert "Unplug test without a real block" not in text


def test_finalize_sandboxed_fills_deny_count_and_rejudges(tmp_path, monkeypatch):
    res = {"block": {"applied": True, "method": "sandbox-exec"}, "sockets": {"outbound_connections": 0},
           "egress": {"sandbox_pids": [11, 12], "window": ["2026-10-03 18:00:00", "2026-10-03 18:01:00"],
                      "firewall_dropped_packets": None, "conntrack_available": False, "conntrack_outbound_flows": []},
           "internet_reachable_during_test": False,
           "verdicts": [{"name": "alert_offline", "measured": 0.9}]}
    path = tmp_path / "unplug.json"
    path.write_text(json.dumps(res), encoding="utf-8")
    seen = {}

    def fake_count(pids, since, until, detail=None):
        seen["args"] = (pids, since, until)
        return 0, ""
    monkeypatch.setattr(unplug_test, "count_sandbox_denies", fake_count)
    out = unplug_test.finalize_sandboxed(path)
    assert seen["args"] == ({11, 12}, "2026-10-03 18:00:00", "2026-10-03 18:01:00")
    v = {x["name"]: x for x in out["verdicts"]}
    assert v["outbound_connections"]["passed"] is True and v["alert_offline"]["passed"] is True
    assert json.loads(path.read_text())["egress"]["firewall_dropped_packets"] == 0
    monkeypatch.setattr(unplug_test, "count_sandbox_denies", lambda *a, **k: (2, ""))
    v = {x["name"]: x for x in unplug_test.finalize_sandboxed(path)["verdicts"]}
    assert v["outbound_connections"]["passed"] is False and v["outbound_connections"]["measured"] == 2


def test_report_names_blocked_attempts_from_sandbox_run(tmp_path):
    _write(tmp_path, "unplug-x-1.json", {"bench": "unplug", "host": {"hostname": "mac"}, "recorded_at": "1",
           "endpoint": {"model": "m", "kind": "ollama"}, "block": {"applied": True, "method": "sandbox-exec"},
           "egress": {"firewall_dropped_packets": 2, "sandbox_pids": [5, 6]}})
    text = report.build(tmp_path)
    assert "tried to leave the factory 2 time(s)" in text and "not broken down" in text


def test_sandbox_deny_detail_names_only_the_pids_that_tried():
    log = ("2026-10-03 18:48:12.100 E kernel[0:1] (Sandbox) Sandbox: Python(9621) deny(1) network-outbound "
           "remote:*:443\n"
           "2026-10-03 18:48:36.200 E kernel[0:1] (Sandbox) Sandbox: Python(9621) deny(1) network-outbound "
           "remote:*:443\n"
           "2026-10-03 18:49:50.000 E kernel[0:1] (Sandbox) Sandbox: Python(9618) deny(1) network-outbound "
           "1.1.1.1:443\n")
    d = unplug_test.sandbox_deny_detail(log, {9618, 9621}, before="2026-10-03 18:49:00.000")
    assert d["by_pid"] == {9621: 2} and d["remotes"] == ["remote:*:443"]     # the probe after the window is not counted


def test_report_attributes_denies_per_pid(tmp_path):
    _write(tmp_path, "unplug-x-1.json", {"bench": "unplug", "host": {"hostname": "mac"}, "recorded_at": "1",
           "endpoint": {"model": "m", "kind": "ollama"}, "block": {"applied": True, "method": "sandbox-exec"},
           "egress": {"firewall_dropped_packets": 2, "sandbox_pids": [9618, 9621], "denies_by_pid": {"9621": 2},
                      "pid_roles": {"9618": "bench", "9621": "war-room app"}}})
    text = report.build(tmp_path)
    assert "pid 9621 (war-room app) x2" in text and "9618" not in text


def test_report_marks_old_schema_and_changed_code_stale(tmp_path, monkeypatch):
    host = {"hostname": "mac"}
    ep = {"model": "m", "kind": "ollama"}
    v = [{"name": "first_alert_card_p90", "target": "≤ 2.0 s", "measured": 1.19, "passed": True, "note": ""}]
    _write(tmp_path, "ttft-x-1.json", {"bench": "ttft", "host": host, "recorded_at": "1", "endpoint": ep,
           "alert_card": {"total_s": {}}, "verdicts": v})
    _write(tmp_path, "concurrent-x-1.json", {"bench": "concurrent", "host": host, "recorded_at": "1",
           "endpoint": ep, "verdicts": [dict(v[0], name="concurrent_3_worst_alert")]})
    text = report.build(tmp_path)
    assert "STALE, not counted (was PASS): old ttft schema" in text
    assert "STALE, not counted (was PASS): old concurrent schema" in text
    # stamped result: current while the bench files are unchanged, stale once they differ
    from bench.provenance import code_stamp
    _write(tmp_path, "ttft-x-1.json", {"bench": "ttft", "host": host, "recorded_at": "1", "endpoint": ep,
           "code": code_stamp("ttft"), "alert_card": {"cold": {}}, "verdicts": v})
    assert "| ttft | first_alert_card_p90 | ≤ 2.0 s | 1.190 | PASS |" in report.build(tmp_path)
    monkeypatch.setattr(report, "code_fingerprint", lambda b: "different")
    assert "STALE, not counted (was PASS): bench code changed" in report.build(tmp_path)


def test_warroom_import_turns_onnxruntime_telemetry_off_before_it_loads():
    """onnxruntime's POSIX telemetry dials :443 from native code; importing warroom must switch it off first."""
    code = ("import os, sys; os.environ.pop('ORT_DISABLE_TELEMETRY', None); import warroom; "
            "assert 'onnxruntime' not in sys.modules; print(os.environ.get('ORT_DISABLE_TELEMETRY'))")
    env = {k: v for k, v in __import__("os").environ.items() if k != "ORT_DISABLE_TELEMETRY"}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "1"


def test_internet_probe_runs_in_its_own_pid_inside_the_sandbox(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 1)
    monkeypatch.setenv(unplug_test.SANDBOX_ENV, "1")
    monkeypatch.setattr(unplug_test.subprocess, "run", fake_run)
    assert unplug_test.internet_reachable(0.1) is False
    assert seen["argv"][0] == sys.executable and "_probe" in seen["argv"][-1]
