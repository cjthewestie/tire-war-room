"""Offline audit checker, trace-order rules and the periodic self-check (research report 2026-11, 建議 4 / G8).

- scripts/audit_check.py: one character changed in an entry's hash -> exit 1; a clean log -> exit 0; read-only;
- audit_rules: R-a, R-c, R-d, R-e each fire on one hand-made violation; a real page-11 trace and a runtime
  rollback (writes through the gate since 建議 1) break none; the afb2553 Spark captures are skipped;
- Runtime.verify_audit_once: a tampered log gives a SYSTEM entry, the warning light and an alert card, and
  the chain is re-read in a worker thread, never on the event loop."""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import threading
from pathlib import Path

import pytest

from tests.test_agent_page11 import build, page11_anomaly, supervisor
from warroom.agent import JsonlAuditLog
from warroom.agent.audit import verify_file
from warroom.agent.audit_rules import RULES, analyze, check_rules, legacy_reason
from warroom.config import ROOT
from warroom.contracts import MAX_DISCUSS_REPLANS, AuditKind, DecisionKind, EventState, WsType

sys.path.insert(0, str(ROOT / "scripts"))
import audit_check

EV = "EV-1"


@pytest.fixture(autouse=True)
def _no_key_env(monkeypatch):
    for k in ("WARROOM_AUDIT_KEY", "WARROOM_AUDIT_KEY_FILE", "WARROOM_AUDIT_REQUIRE_KEY", "WARROOM_AUDIT_ANCHOR"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------- helpers
class _Trace:
    """Hand-made audit entries (dicts, as the checker reads them)."""

    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, kind: str, actor: str, payload: dict, event_id: str | None = EV) -> _Trace:
        self.rows.append({"seq": len(self.rows) + 1, "kind": kind, "actor": actor, "event_id": event_id,
                          "payload": payload})
        return self

    def to(self, frm, to):
        return self.add("state_transition", "state_machine", {"from": frm, "to": to})

    def gate(self, step, tool, verdict, risk):
        return self.add("gate", "gate", {"step_id": step, "decision": {"step_id": step, "tool": tool,
                                                                       "verdict": verdict, "risk": risk}})

    def call(self, step, tool, **extra):
        return self.add("tool_call", f"tool:{tool}", {"call_id": f"c{len(self.rows)}", "step_id": step,
                                                       "attempt": 1, "state": "execute", **extra})

    def decide(self, decision, request_id="AP-EV-1-1", option_id=None, reason=""):
        return self.add("approval_decision", "supervisor",
                        {"request_id": request_id, "decision": decision, "option_id": option_id, "reason": reason})

    def discuss(self, request_id):
        """state_machine.discuss: SYSTEM constraints_added naming the card, then the supersede."""
        self.add("system", "supervisor", {"constraints_added": [], "text": "改", "approval_id": request_id})
        return self.decide("superseded", request_id, reason="主管討論：改")


def _rules(trace: _Trace) -> list[str]:
    return [v["rule"] for v in check_rules(trace.rows)]


def _write_log(tmp_path: Path, n: int = 6) -> Path:
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    for i in range(n):
        log.append(AuditKind.SYSTEM, "test", {"i": i})
    return log.path


def _flip_hash_char(path: Path, seq: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    d = json.loads(lines[seq - 1])
    h = d["hash"]
    d["hash"] = ("1" if h[0] != "1" else "2") + h[1:]
    lines[seq - 1] = json.dumps(d, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- the CLI: chain
def test_clean_log_passes_and_one_changed_hash_character_exits_1(tmp_path, capsys):
    path = _write_log(tmp_path)
    assert audit_check.main([str(path)]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["ok"] and rep["chain"]["hmac_checked"] and rep["chain"]["head"] == "ok"
    assert rep["chain"]["key_source"] == "beside_log" and rep["chain"]["checked"] == 6

    _flip_hash_char(path, 3)
    before = path.read_bytes()
    assert audit_check.main([str(path)]) == 1
    rep = json.loads(capsys.readouterr().out)
    assert not rep["ok"] and rep["chain"]["broken_at"] == 3 and "seq 3" in rep["chain"]["problem"]
    # read-only: nothing repaired, archived or appended (opening it with JsonlAuditLog would archive it)
    assert path.read_bytes() == before and not list(tmp_path.glob("*.broken-*"))


def test_changed_hash_is_caught_without_the_key_and_with_an_explicit_key_file(tmp_path, capsys):
    path = _write_log(tmp_path)
    key = path.with_name(path.name + ".key")
    moved = tmp_path / "elsewhere.key"
    shutil.move(key, moved)
    _flip_hash_char(path, 2)
    # no key: seq order and prev_hash links only, still enough for a changed entry_hash
    rep = verify_file(path, None)
    assert not rep["ok"] and not rep["hmac_checked"] and rep["broken_at"] == 3
    assert audit_check.main([str(path), "--key-file", str(moved)]) == 1
    assert json.loads(capsys.readouterr().out)["chain"]["key_source"] == "key_file"
    assert audit_check.main([str(path), "--key-file", str(tmp_path / "missing.key")]) == 2


def test_raw_beside_log_key_works_as_key_file_even_with_whitespace_bytes(tmp_path, capsys):
    """A <log>.key is 32 raw bytes and is used unstripped by the log; --key-file must not strip it into a
    wrong key (about 1 in 20 random keys start or end with a whitespace byte)."""
    path = tmp_path / "audit.jsonl"
    key = b"\n" + bytes(range(1, 31)) + b" "
    log = JsonlAuditLog(path, key=key)
    for i in range(3):
        log.append(AuditKind.SYSTEM, "test", {"i": i})
    kf = tmp_path / "raw.key"
    kf.write_bytes(key)
    assert audit_check.main([str(path), "--key-file", str(kf)]) == 0
    capsys.readouterr()
    # a text key file with a trailing newline is stripped (as WARROOM_AUDIT_KEY_FILE is)
    p2 = tmp_path / "b" / "audit.jsonl"
    log2 = JsonlAuditLog(p2, key=b"plain-text-key")
    log2.append(AuditKind.SYSTEM, "test", {"i": 0})
    text = tmp_path / "text.key"
    text.write_bytes(b"plain-text-key\n")
    assert audit_check.main([str(p2), "--key-file", str(text)]) == 0


def test_a_cut_off_last_entry_fails_on_the_signed_head(tmp_path):
    path = _write_log(tmp_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    key = path.with_name(path.name + ".key").read_bytes()
    rep = verify_file(path, key)
    assert not rep["ok"] and "head anchor seq 6" in rep["problem"]


def test_wrong_key_fails(tmp_path, capsys):
    path = _write_log(tmp_path)
    other = tmp_path / "other.key"
    other.write_bytes(b"not the key")
    assert audit_check.main([str(path), "--key-file", str(other)]) == 1


# ---------------------------------------------------------------- the rules, one hand-made violation each
def test_rules_are_the_four_of_the_report():
    assert set(RULES) == {"R-a", "R-c", "R-d", "R-e"}


def test_ra_write_needs_a_gate_allow_for_that_step():
    ok = _Trace().gate("S1", "notify_teams", "allow", "R1").call("S1", "notify_teams")
    assert _rules(ok) == []
    no_gate = _Trace().call("S1", "notify_teams")
    assert _rules(no_gate) == ["R-a"]
    other_step = _Trace().gate("S1", "notify_teams", "allow", "R1").call("S2", "notify_teams")
    assert _rules(other_step) == ["R-a"]
    other_event = _Trace().gate("S1", "notify_teams", "allow", "R1")
    other_event.add("tool_call", "tool:notify_teams", {"step_id": "S1"}, event_id="EV-2")
    assert _rules(other_event) == ["R-a"]
    rejected = _Trace().gate("S1", "set_cure_time", "reject", "R2").call("S1", "set_cure_time")
    assert _rules(rejected) == ["R-a"]
    wrong_tool = _Trace().gate("S1", "notify_teams", "allow", "R1").call("S1", "confirm_schedule")
    assert _rules(wrong_tool) == ["R-a"]
    no_step = _Trace().call(None, "notify_teams")
    assert _rules(no_step) == ["R-a"]
    reads = _Trace().call(None, "read_sensors").call("S9", "solve_schedule")   # reads need no gate
    assert _rules(reads) == []


def test_ra_needs_approval_must_be_approved_before_the_write():
    approved = _Trace().gate("S4", "confirm_schedule", "needs_approval", "R3").decide("approve") \
        .call("S4", "confirm_schedule")
    assert _rules(approved) == []
    modified = _Trace().gate("S4", "confirm_schedule", "needs_approval", "R3").decide("modify") \
        .call("S4", "confirm_schedule")
    assert _rules(modified) == []
    not_yet = _Trace().gate("S4", "confirm_schedule", "needs_approval", "R3").call("S4", "confirm_schedule") \
        .decide("approve")
    assert _rules(not_yet) == ["R-a"]
    refused = _Trace().gate("S4", "confirm_schedule", "needs_approval", "R3").decide("reject") \
        .call("S4", "confirm_schedule")
    assert _rules(refused) == ["R-a"]
    # an approval from before the step was gated again (a new round) does not cover it
    stale = _Trace().decide("approve").gate("S4", "confirm_schedule", "needs_approval", "R3") \
        .call("S4", "confirm_schedule")
    assert _rules(stale) == ["R-a"]


def test_ra_an_approved_option_covers_its_own_step_only():
    ok = _Trace().decide("approve", "AP-EV-1-2", "A").call("AP-EV-1-2-A", "notify_teams")
    assert _rules(ok) == []
    other = _Trace().decide("approve", "AP-EV-1-2", "A").call("AP-EV-1-2-B", "notify_teams")
    assert _rules(other) == ["R-a"]
    rejected = _Trace().decide("reject", "AP-EV-1-2", None).call("AP-EV-1-2-A", "notify_teams")
    assert _rules(rejected) == ["R-a"]


def test_ra_pre_gate_rollback_notice_is_flagged():
    """Before 建議 1 a rollback's staffing notice called the tool directly (no GATE entry): R-a catches it."""
    t = _Trace()
    t.add("tool_call", "tool:notify_teams", {"call_id": "rb", "step_id": "AP-RB-1-撤回-1003-overtime",
                                             "by": "supervisor"}, event_id="EV-1")
    assert _rules(t) == ["R-a"]


def test_rc_no_write_after_safe_stop():
    t = _Trace().gate("S5", "notify_teams", "allow", "R1").to("replan", "safe_stop").call("S5", "notify_teams")
    assert _rules(t) == ["R-c"]
    # another event is not affected; a supervisor's rollback notice later (gated_call, "by") is left to R-a
    t2 = _Trace().to("replan", "safe_stop")
    t2.add("gate", "gate", {"step_id": "S5", "decision": {"tool": "notify_teams", "verdict": "allow"}}, event_id="EV-2")
    t2.add("tool_call", "tool:notify_teams", {"step_id": "S5"}, event_id="EV-2")
    t2.gate("AP-RB-1-x", "notify_teams", "allow", "R1").call("AP-RB-1-x", "notify_teams", by="supervisor")
    assert _rules(t2) == []


def test_rd_r1_notify_after_a_failed_verify_needs_a_new_simulate():
    bad = _Trace().to("execute", "verify").add("system", "verifier", {"hypothesis": "H1", "ok": False}) \
        .to("verify", "replan").gate("S9", "notify_teams", "allow", "R1").call("S9", "notify_teams")
    assert _rules(bad) == ["R-d"]
    good = _Trace().to("execute", "verify").add("system", "verifier", {"hypothesis": "H1", "ok": False}) \
        .to("verify", "replan").to("replan", "simulate").to("simulate", "gate") \
        .gate("S9", "notify_teams", "allow", "R1").to("gate", "execute").call("S9", "notify_teams")
    assert _rules(good) == []
    # an approved (R2/R3) write is not an automatic notice: R-d is about R1 only
    approved = _Trace().add("system", "verifier", {"ok": False}).gate("S9", "notify_teams", "needs_approval", "R3") \
        .decide("approve").call("S9", "notify_teams")
    assert _rules(approved) == []
    passed = _Trace().add("system", "verifier", {"ok": True}).gate("S9", "notify_teams", "allow", "R1") \
        .call("S9", "notify_teams")
    assert _rules(passed) == []


def test_re_discussion_replans_are_capped():
    t = _Trace()
    for n in range(MAX_DISCUSS_REPLANS):
        t.discuss(f"AP-EV-1-1-v{n}")
    assert _rules(t) == []
    # a card superseded because the supervisor interrupted the event is not a discussion re-plan
    t.decide("superseded", "AP-EV-1-1-vI", reason="主管中斷")
    assert _rules(t) == []
    t.discuss("AP-EV-1-1-vX")
    assert _rules(t) == ["R-e"]
    t.discuss("AP-EV-1-1-vY")
    assert _rules(t) == ["R-e"]                      # reported once per event
    # the reason alone (no SYSTEM entry in the capture) still counts
    r = _Trace()
    for n in range(MAX_DISCUSS_REPLANS + 1):
        r.decide("superseded", f"AP-EV-1-1-v{n}", reason="主管討論：延後")
    assert _rules(r) == ["R-e"]


# ---------------------------------------------------------------- real traces break no rule
async def test_page11_trace_breaks_no_rule_and_the_file_verifies(tmp_path):
    clock, _bus, audit, _sim, _store, _tools, approvals, sm = build(tmp_path)
    sup = asyncio.create_task(supervisor(clock, approvals, [
        ("02:16:40", DecisionKind.APPROVE, None, ""),
        ("02:31:30", DecisionKind.APPROVE, "A", "跨組調人影響 2 線，改夜班加班")]))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED
    rep = audit_check.check(audit.path)
    assert rep["ok"], rep
    assert rep["rules"]["writes"] >= 5 and rep["rules"]["violations"] == []


def test_runtime_rollback_trace_breaks_no_rule(tmp_path):
    """建議 1 is merged: the rollback notices go GATE -> TOOL_CALL -> TOOL_RESULT, so R-a holds (no xfail)."""
    from tests.test_gated_writes import test_runtime_rollback_notice_goes_through_the_gate as rollback
    rollback(tmp_path)
    rep = audit_check.check(tmp_path / "audit.jsonl")
    assert rep["chain"]["ok"], rep["chain"]
    assert rep["rules"]["violations"] == [], rep["rules"]["violations"]


MAC_RUNS = sorted((ROOT / "bench/results/mac-p11-fix-20261022/runs").glob("run*-audit.jsonl"))
SPARK_P11 = sorted((ROOT / "bench/results/spark-lightning-20261021/p11").glob("*-audit.jsonl"))


@pytest.mark.parametrize("path", MAC_RUNS, ids=lambda p: p.name)
def test_recorded_mac_p11_runs_break_no_rule(path):
    rep = audit_check.check(path)
    assert rep["chain"]["excerpt"] and rep["chain"]["ok"], rep["chain"]     # one run's rows, links intact
    assert rep["rules"]["skipped"] is None and rep["rules"]["violations"] == []


@pytest.mark.parametrize("path", SPARK_P11, ids=lambda p: p.name)
def test_afb2553_captures_are_skipped_as_legacy(path):
    assert legacy_reason(path)
    rep = analyze([], path=path)
    assert rep["skipped"] and "afb2553" in rep["skipped"] and rep["violations"] == []
    assert audit_check.main([str(path)]) == 0


def test_recorded_runs_exist():
    assert MAC_RUNS and SPARK_P11


# ---------------------------------------------------------------- the Runtime's periodic self-check
def _runtime(tmp_path):
    from tests.test_api_review_r4 import _real
    return _real(tmp_path)


def _system(rt, msg):
    return [e for e in rt.audit.tail(0, 10_000) if e.kind is AuditKind.SYSTEM and e.payload.get("msg") == msg]


async def test_tampered_log_gives_system_entry_warning_light_and_alert(tmp_path):
    rt = _runtime(tmp_path)
    for i in range(3):
        rt.audit.append(AuditKind.SYSTEM, "test", {"i": i})
    seen: list[threading.Thread] = []
    real = rt.audit.verify_report

    def spy():
        seen.append(threading.current_thread())
        return real()
    rt.audit.verify_report = spy

    rep = await rt.verify_audit_once()
    assert rep is not None and rep["ok"] is True and not rt.warning_light()
    assert seen and seen[-1] is not threading.main_thread()          # never on the event loop

    _flip_hash_char(rt.audit.path, 2)
    rep = await rt.verify_audit_once()
    assert rep["ok"] is False and rep["broken_at"] == 2, rep
    assert seen[-1] is not threading.main_thread()
    failed = _system(rt, "audit chain verification failed")
    assert len(failed) == 1 and failed[0].payload["broken_at"] == 2
    assert rt.warning_light() and _system(rt, "warning light")[-1].payload["on"] is True
    cards = [m for m in rt.bus.history if m.type is WsType.ALERT_CARD and m.data.get("source") == "system"]
    assert cards and cards[-1].data["event_id"] is None and "稽核" in cards[-1].data["card"]["title"]
    assert rt.snapshot().header.warning_light is True

    await rt.verify_audit_once()                                         # still broken: recorded once
    assert len(_system(rt, "audit chain verification failed")) == 1 and rt.warning_light()


async def test_large_log_is_not_reread_periodically(tmp_path):
    rt = _runtime(tmp_path)
    rt.audit.append(AuditKind.SYSTEM, "test", {"i": 0})
    rt.audit_verify_max_bytes = 1
    called = []
    rt.audit.verify_report = lambda: called.append(1) or {"ok": True}
    rep = await rt.verify_audit_once()
    assert rep["ok"] is None and rep["skipped"] == "too_large" and called == []
    await rt.verify_audit_once()
    assert len(_system(rt, "audit self-check skipped: log too large for the periodic check")) == 1
    assert not rt.warning_light()


async def test_background_loop_runs_the_check(tmp_path):
    rt = _runtime(tmp_path)
    rt.audit_verify_interval_s = 0.01
    task = asyncio.create_task(rt._audit_verify_loop())
    try:
        for _ in range(200):
            if rt.audit_integrity is not None:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert rt.audit_integrity is not None and rt.audit_integrity["ok"] is True


def test_verify_report_while_another_thread_appends(tmp_path):
    """The periodic check reads the file in a worker thread while the loop keeps appending: no false break."""
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.append(AuditKind.SYSTEM, "test", {"i": -1})
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set() and i < 400:
            log.append(AuditKind.SYSTEM, "test", {"i": i, "pad": "x" * 200})
            i += 1
    t = threading.Thread(target=writer)
    t.start()
    try:
        reports = [log.verify_report() for _ in range(40)]
    finally:
        stop.set()
        t.join()
    assert all(r["ok"] for r in reports), [r for r in reports if not r["ok"]][:1]
    assert log.verify_chain()
