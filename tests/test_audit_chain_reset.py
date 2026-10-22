"""Two writers on one audit log keep one chain; a chain archived as broken-N still feeds the memory's observations."""
from __future__ import annotations

from types import SimpleNamespace

from warroom.agent.audit import JsonlAuditLog as Audit
from warroom.api.memory_desk import MemoryDesk
from warroom.contracts import AuditKind
from warroom.data import memory_log as ML

KEY = b"k" * 32


def test_two_writers_share_one_valid_chain(tmp_path):
    log = tmp_path / "audit.jsonl"
    a, b = Audit(log, key=KEY), Audit(log, key=KEY)
    for i in range(3):
        a.append(AuditKind.SYSTEM, "a", {"i": i})
        b.append(AuditKind.SYSTEM, "b", {"i": i})
    again = Audit(log, key=KEY)                      # opening verifies: nothing archived
    assert again.recovery == [] and len(again) == 6
    assert not list(tmp_path.glob("audit.jsonl.broken-*"))


def _desk(tmp_path, audit):
    rt = SimpleNamespace(settings=SimpleNamespace(audit_path=tmp_path / "audit.jsonl"), c=SimpleNamespace(), audit=audit)
    return MemoryDesk(rt)


def _anomaly(audit, msg):
    audit.append(AuditKind.ANOMALY, "detector", {"message": msg, "kind": "x", "machine_id": "CURE-07"},
                 event_id="EV-1-CURE-07")


def test_sync_keeps_going_after_the_chain_is_archived(tmp_path):
    log = tmp_path / "audit.jsonl"
    audit = Audit(log, key=KEY)
    for i in range(5):
        _anomaly(audit, f"old {i}")
    desk = _desk(tmp_path, audit)
    assert desk.sync() == 5
    log.with_name("audit.jsonl.head").write_text("{}")          # break the anchor: the next open archives the chain
    audit = Audit(log, key=KEY)
    assert audit.recovery and audit.recovery[0]["archived_as"] == "audit.jsonl.broken-1"
    _anomaly(audit, "new 0")
    _anomaly(audit, "new 1")
    added = desk.sync()
    obs = desk.obs.all()
    assert added == 2 and [o["id"] for o in obs[-2:]] == ["OB-1.0002", "OB-1.0003"]
    assert desk.sync() == 0 and len(desk.obs.all()) == 7         # idempotent
    rows, st = ML.read_rows(log), {}
    rebuilt = [o["id"] for _, r in ML.scan(rows, st) if (o := ML.observe(r, st["chain"]))]
    assert rebuilt == ["OB-1.0002", "OB-1.0003"]                # a rebuild gives the same ids
