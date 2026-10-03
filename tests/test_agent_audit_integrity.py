"""Audit log integrity: keyed chain, torn-line recovery after a power cut, head anchor, no
in-memory fallback in the runtime (review findings 7 and 14)."""
from __future__ import annotations

import json

from tests.agent_fakes import T0
from warroom.agent import JsonlAuditLog
from warroom.agent.audit import GENESIS_HASH, entry_hash
from warroom.clock import SimClock
from warroom.contracts import AuditKind


def _log(tmp_path, name="a.jsonl"):
    return JsonlAuditLog(tmp_path / name, clock=SimClock(start=T0, manual=True))


def test_rewrite_with_recomputed_unkeyed_hashes_is_detected(tmp_path):
    log = _log(tmp_path)
    for i in range(4):
        log.append(AuditKind.APPROVAL_DECISION, "supervisor", {"option_id": "A", "i": i})
    p = tmp_path / "a.jsonl"
    # attacker edits the decision and recomputes every hash with plain sha256 (no key)
    prev, out = GENESIS_HASH, []
    for line in p.read_text().splitlines():
        raw = json.loads(line)
        raw.pop("hash")
        raw["payload"]["option_id"] = "B"
        raw["prev_hash"] = prev
        prev = entry_hash(prev, raw)
        out.append(json.dumps({**raw, "hash": prev}, ensure_ascii=False, separators=(",", ":")))
    p.write_text("\n".join(out) + "\n")
    assert not log.verify_chain()
    # reopening: the forged file is archived untouched and a new chain says why
    log2 = _log(tmp_path)
    assert (tmp_path / "a.jsonl.broken-1").read_text().count('"B"') == 4
    first = log2.tail(0, 0)[0]
    assert first.seq == 1 and first.kind is AuditKind.SYSTEM and "failed verification" in first.payload["msg"]
    assert log2.verify_chain()


def test_torn_last_line_is_quarantined_and_chain_continues(tmp_path):
    log = _log(tmp_path)
    for i in range(3):
        log.append(AuditKind.SYSTEM, "x", {"i": i})
    p = tmp_path / "a.jsonl"
    with open(p, "a") as f:
        f.write('{"seq":4,"ts":"2026-10-03T08:00:00+08:00","kind":"sys')     # power cut mid-write
    log2 = _log(tmp_path)
    assert (tmp_path / "a.jsonl.corrupt").read_text().startswith('{"seq":4')
    e = log2.append(AuditKind.SYSTEM, "x", {"after": "restart"})
    entries = [json.loads(x) for x in p.read_text().splitlines()]
    assert [x["seq"] for x in entries] == [1, 2, 3, 4, 5]         # 4 = recovery note, 5 = new entry
    assert "torn" in entries[3]["payload"]["msg"] and e.seq == 5
    assert log2.verify_chain()
    assert _log(tmp_path).verify_chain()                            # and it reopens cleanly


def test_lines_cut_off_the_end_are_caught_at_reopen(tmp_path):
    log = _log(tmp_path)
    for i in range(4):
        log.append(AuditKind.SYSTEM, "x", {"i": i})
    p = tmp_path / "a.jsonl"
    p.write_text("\n".join(p.read_text().splitlines()[:2]) + "\n")
    log2 = _log(tmp_path)
    assert (tmp_path / "a.jsonl.broken-1").exists()
    assert "head anchor" in log2.tail(0, 0)[0].payload["reason"]


def test_runtime_keeps_a_persistent_audit_after_unplug(tmp_path):
    from warroom.api import Runtime
    from warroom.config import Settings
    s = Settings(audit_path=tmp_path / "a.jsonl", schedule_dir=tmp_path / "s", qdrant_path=tmp_path / "q",
                 scenario="normal", llm_mode="mock")
    rt = Runtime(s, run_loop=False)
    rt.audit.append(AuditKind.SYSTEM, "t", {"msg": "x"})
    with open(tmp_path / "a.jsonl", "a") as f:
        f.write('{"seq":2,"ts":"2026-10-03T08:00:00+08:00","kind":"sys')
    rt2 = Runtime(s, run_loop=False)
    assert type(rt2.audit) is JsonlAuditLog and "audit" not in rt2.degraded
    n = len((tmp_path / "a.jsonl").read_text().splitlines())
    rt2.audit.append(AuditKind.SYSTEM, "t", {"msg": "after restart"})
    assert len((tmp_path / "a.jsonl").read_text().splitlines()) == n + 1
    assert rt2.audit.verify_chain()
