"""Append-only audit log (page 5 稽核日誌, page 7 操作紀錄).

One JSON object per line, sha256 hash-chained: entry.hash = sha256(prev_hash + canonical JSON of
every other field, exactly as written). Editing, deleting or reordering any line breaks the chain;
verify_chain() re-reads the file from disk to detect it. Each append is fsync'd before returning.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from warroom.contracts import AuditEntry, AuditKind, WsType
from warroom.factory import TZ

GENESIS_HASH = "0" * 64


def _canonical(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(prev_hash: str, body: dict[str, Any]) -> str:
    """body = the entry as written (JSON) without its 'hash' field."""
    return hashlib.sha256((prev_hash + _canonical(body)).encode("utf-8")).hexdigest()


class JsonlAuditLog:
    """Implements contracts.AuditLog: append-only JSONL, sha256 hash chain, fsync on append.
    Publishes each entry on the bus (WsType.AUDIT) when bus is given. Re-opening an existing file
    continues its chain (seq and prev_hash pick up from the last line)."""

    def __init__(self, path: Path, bus=None, clock=None, keep_in_memory: int = 5000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._bus = bus
        self._clock = clock
        self._lock = threading.Lock()
        self._keep = keep_in_memory
        self._entries: list[AuditEntry] = []
        self._seq = 0
        self._last_hash = GENESIS_HASH
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = AuditEntry.model_validate_json(line)
                    self._entries.append(e)
                    self._seq, self._last_hash = e.seq, e.hash
            del self._entries[: max(0, len(self._entries) - self._keep)]

    def _now(self) -> datetime:
        return self._clock.now() if self._clock else datetime.now(TZ)

    def append(self, kind: AuditKind, actor: str, payload: dict[str, Any],
               event_id: str | None = None) -> AuditEntry:
        with self._lock:
            draft = AuditEntry(seq=self._seq + 1, ts=self._now(), kind=AuditKind(kind), actor=actor,
                               event_id=event_id, payload=json.loads(_canonical(payload or {})),
                               prev_hash=self._last_hash, hash="")
            body = json.loads(draft.model_dump_json())
            body.pop("hash")
            h = entry_hash(self._last_hash, body)
            line = json.dumps({**body, "hash": h}, ensure_ascii=False, separators=(",", ":"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            entry = draft.model_copy(update={"hash": h})
            self._seq, self._last_hash = entry.seq, h
            self._entries.append(entry)
            if len(self._entries) > self._keep:
                del self._entries[: len(self._entries) - self._keep]
        if self._bus is not None:
            try:
                self._bus.publish(WsType.AUDIT, entry)
            except Exception:  # noqa: BLE001 - logging must never fail because of a subscriber
                pass
        return entry

    def tail(self, since_seq: int = 0, limit: int = 200) -> list[AuditEntry]:
        """since_seq=0: the newest `limit` entries. since_seq>0: the next `limit` entries after it,
        oldest first, so a poller never skips any."""
        with self._lock:
            newer = [e for e in self._entries if e.seq > since_seq]
        if not limit:
            return newer
        return newer[-limit:] if since_seq <= 0 else newer[:limit]

    def verify_chain(self) -> bool:
        """Re-read the file from disk and recompute every hash. False on any edit/delete/reorder."""
        if not self.path.exists():
            return self._seq == 0
        prev, expect_seq = GENESIS_HASH, 1
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                h = raw.pop("hash")
            except Exception:  # noqa: BLE001
                return False
            if raw.get("seq") != expect_seq or raw.get("prev_hash") != prev or entry_hash(prev, raw) != h:
                return False
            prev, expect_seq = h, expect_seq + 1
        # a valid prefix with lines cut off the end is still tampering: compare with what we wrote
        return expect_seq - 1 == self._seq

    def __len__(self) -> int:
        return self._seq
