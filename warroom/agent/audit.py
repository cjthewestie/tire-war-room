"""Append-only audit log (page 5 稽核日誌, page 7 操作紀錄).

One JSON object per line, HMAC-SHA256 hash-chained: entry.hash = HMAC(key, prev_hash + canonical
JSON of every other field, exactly as written). The key lives outside the log (env
WARROOM_AUDIT_KEY, else ``<log>.key`` created 0600 beside it, on the encrypted disk of page 10), so
someone who can edit the log cannot recompute a valid chain. Each append is fsync'd before
returning, and the head (last seq + hash) is anchored in ``<log>.head`` so whole lines cut off the
end are caught across restarts too.

Opening an existing log verifies it:
- a torn LAST line (power cut mid-write, the unplug demo) is moved to ``<log>.corrupt``, the file is
  cut back to the last complete entry, the chain continues from there and a SYSTEM entry records
  the recovery;
- any other break (an edited, deleted or reordered entry, a wrong key, a head anchor the file no
  longer reaches) archives the whole file as ``<log>.broken-N`` untouched and starts a new chain
  whose first entry says why. The log never silently drops to memory and never raises on content.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from warroom.contracts import AuditEntry, AuditKind, WsType
from warroom.factory import TZ

GENESIS_HASH = "0" * 64
KEY_ENV = "WARROOM_AUDIT_KEY"


def _canonical(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(prev_hash: str, body: dict[str, Any], key: bytes | None = None) -> str:
    """body = the entry as written (JSON) without its 'hash' field. key=None: plain sha256."""
    msg = (prev_hash + _canonical(body)).encode("utf-8")
    if key is None:
        return hashlib.sha256(msg).hexdigest()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def load_or_create_key(log_path: Path) -> bytes:
    """WARROOM_AUDIT_KEY (hex, or any string) wins; else ``<log>.key`` (32 random bytes, 0600)."""
    env = os.environ.get(KEY_ENV)
    if env:
        try:
            return bytes.fromhex(env)
        except ValueError:
            return env.encode("utf-8")
    kp = log_path.with_name(log_path.name + ".key")
    if kp.exists():
        return kp.read_bytes()
    key = secrets.token_bytes(32)
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key


class JsonlAuditLog:
    """Implements contracts.AuditLog: append-only JSONL, keyed hash chain, fsync on append, head
    anchor. Publishes each entry on the bus (WsType.AUDIT) when bus is given. Re-opening an
    existing file verifies it and continues its chain (see module docstring for recovery).
    ``recovery`` lists what was repaired or archived at open (also written as SYSTEM entries)."""

    def __init__(self, path: Path, bus=None, clock=None, keep_in_memory: int = 5000, key: bytes | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.head_path = self.path.with_name(self.path.name + ".head")
        self._bus = bus
        self._clock = clock
        self._lock = threading.Lock()
        self._keep = keep_in_memory
        self._key = key if key is not None else load_or_create_key(self.path)
        self._entries: list[AuditEntry] = []
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self.recovery: list[dict[str, Any]] = []
        if self.path.exists():
            self._open_existing()
        for note in self.recovery:
            self.append(AuditKind.SYSTEM, "audit", note)

    # ------------------------------------------------------------------ open / recover
    def _scan(self, data: bytes) -> tuple[list[AuditEntry], int, str | None]:
        """Verify the chain line by line. Returns (valid entries, byte offset after the last valid
        line, problem) where problem is None, 'torn' (only the last line is bad) or a message."""
        entries: list[AuditEntry] = []
        prev, expect_seq, pos = GENESIS_HASH, 1, 0
        while pos < len(data):
            nl = data.find(b"\n", pos)
            end = len(data) if nl < 0 else nl + 1
            raw_line = data[pos:end]
            is_last = not data[end:].strip()
            if not raw_line.strip():
                pos = end
                continue
            try:
                raw = json.loads(raw_line.decode("utf-8"))
                h = raw.pop("hash")
                if raw.get("seq") != expect_seq or raw.get("prev_hash") != prev:
                    raise ValueError(f"seq/prev_hash mismatch at seq {expect_seq}")
                if not hmac.compare_digest(entry_hash(prev, raw, self._key), h):
                    raise ValueError(f"hash mismatch at seq {expect_seq}")
                e = AuditEntry.model_validate({**raw, "hash": h})
            except Exception as ex:  # noqa: BLE001
                torn = is_last and (nl < 0 or isinstance(ex, (json.JSONDecodeError, UnicodeDecodeError)))
                return entries, pos, "torn" if torn else f"{type(ex).__name__}: {ex}"[:300]
            entries.append(e)
            prev, expect_seq, pos = h, expect_seq + 1, end
        return entries, pos, None

    def _read_head(self) -> dict[str, Any] | None:
        try:
            d = json.loads(self.head_path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and isinstance(d.get("seq"), int) else None
        except Exception:  # noqa: BLE001 - missing or half-written anchor: nothing to compare
            return None

    def _archive(self, why: str, last_seq: int) -> None:
        n = 1
        while (dest := self.path.with_name(f"{self.path.name}.broken-{n}")).exists():
            n += 1
        os.replace(self.path, dest)
        if self.head_path.exists():
            os.replace(self.head_path, dest.with_name(dest.name + ".head"))
        self.recovery.append({"msg": "audit chain failed verification at open; previous log archived untouched, "
                                     "new chain started", "reason": why, "archived_as": dest.name,
                              "verified_through_seq": last_seq})

    def _open_existing(self) -> None:
        data = self.path.read_bytes()
        entries, good_end, problem = self._scan(data)
        if problem == "torn":
            torn = data[good_end:]
            with open(self.path.with_name(self.path.name + ".corrupt"), "ab") as f:
                f.write(torn + (b"" if torn.endswith(b"\n") else b"\n"))
                f.flush()
                os.fsync(f.fileno())
            with open(self.path, "r+b") as f:
                f.truncate(good_end)
                f.flush()
                os.fsync(f.fileno())
            self.recovery.append({"msg": "recovered torn last line (power cut mid-write); moved to sidecar, "
                                         "chain continues from the last complete entry",
                                  "sidecar": self.path.name + ".corrupt", "bytes": len(torn),
                                  "continued_from_seq": entries[-1].seq if entries else 0})
            problem = None
        if problem is None:
            head = self._read_head()
            last = entries[-1] if entries else None
            if head is not None and head["seq"] > 0:
                at = entries[head["seq"] - 1] if head["seq"] <= len(entries) else None
                if at is None or at.hash != head.get("hash"):
                    problem = (f"head anchor seq {head['seq']} not found in the log "
                               f"(log ends at seq {last.seq if last else 0}): entries were cut off or replaced")
        if problem is not None:
            self._archive(problem, entries[-1].seq if entries else 0)
            return
        self._entries = entries[-self._keep:]
        if entries:
            self._seq, self._last_hash = entries[-1].seq, entries[-1].hash

    def _write_head(self) -> None:
        tmp = self.head_path.with_name(self.head_path.name + ".tmp")
        tmp.write_text(json.dumps({"seq": self._seq, "hash": self._last_hash}), encoding="utf-8")
        os.replace(tmp, self.head_path)

    # ------------------------------------------------------------------ API
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
            h = entry_hash(self._last_hash, body, self._key)
            line = json.dumps({**body, "hash": h}, ensure_ascii=False, separators=(",", ":"))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            entry = draft.model_copy(update={"hash": h})
            self._seq, self._last_hash = entry.seq, h
            self._write_head()
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
        """Re-read the file from disk and recompute every keyed hash. False on any edit, delete,
        reorder, rewrite without the key, or lines cut off the end."""
        if not self.path.exists():
            return self._seq == 0
        entries, _, problem = self._scan(self.path.read_bytes())
        if problem is not None:
            return False
        last_seq = entries[-1].seq if entries else 0
        return last_seq == self._seq and (not entries or entries[-1].hash == self._last_hash)

    def __len__(self) -> int:
        return self._seq
