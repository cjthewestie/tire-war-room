"""Append-only audit log (page 5 稽核日誌, page 7 操作紀錄).

One JSON object per line, HMAC-SHA256 hash-chained: entry.hash = HMAC(key, prev_hash + canonical
JSON of every other field, exactly as written). Each append is fsync'd before returning, and the head
(last seq + hash) is anchored in ``<log>.head`` (and, when WARROOM_AUDIT_ANCHOR names a path outside
the data volume, mirrored there too) so whole lines cut off the end are caught across restarts.

What the key protects depends on where it lives (``key_source``):
- ``env`` (WARROOM_AUDIT_KEY) or ``file`` (WARROOM_AUDIT_KEY_FILE, e.g. a root-owned 0400 file or a
  systemd credential outside APP_DATA): someone who can edit the log but cannot read the key cannot
  recompute a valid chain.
- ``beside_log`` (fallback, ``<log>.key`` created 0600 next to the log): this only catches accidental
  damage and edits by someone without read access to the data directory. Anyone who can read the key
  can rewrite the chain. Set WARROOM_AUDIT_REQUIRE_KEY=1 (the Spark deployment should) to refuse to
  start on this fallback.

Opening an existing log verifies it:
- a torn LAST line (power cut mid-write, the unplug demo) is moved to ``<log>.corrupt``, the file is
  cut back to the last complete entry, the chain continues from there and a SYSTEM entry records
  the recovery;
- any other break (an edited, deleted or reordered entry, a wrong key, a head anchor the file no
  longer reaches, a MISSING head anchor beside a non-empty log) archives the whole file as
  ``<log>.broken-N`` untouched and starts a new chain whose first entry says why. A log that is gone
  while the external anchor still names entries is recorded the same way. The log never silently
  drops to memory and never raises on content.
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
KEY_FILE_ENV = "WARROOM_AUDIT_KEY_FILE"
REQUIRE_KEY_ENV = "WARROOM_AUDIT_REQUIRE_KEY"
ANCHOR_ENV = "WARROOM_AUDIT_ANCHOR"


class AuditKeyError(RuntimeError):
    """WARROOM_AUDIT_REQUIRE_KEY is set but no key from outside the data directory is configured."""


def _canonical(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def entry_hash(prev_hash: str, body: dict[str, Any], key: bytes | None = None) -> str:
    """body = the entry as written (JSON) without its 'hash' field. key=None: plain sha256."""
    msg = (prev_hash + _canonical(body)).encode("utf-8")
    if key is None:
        return hashlib.sha256(msg).hexdigest()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def resolve_key(log_path: Path) -> tuple[bytes, str]:
    """(key, source). WARROOM_AUDIT_KEY (hex, or any string) wins, then WARROOM_AUDIT_KEY_FILE (must
    exist), else ``<log>.key`` beside the log (32 random bytes, 0600) — refused when
    WARROOM_AUDIT_REQUIRE_KEY is set, because a key next to the log protects nothing against
    someone who can write the log's directory."""
    env = os.environ.get(KEY_ENV)
    if env:
        try:
            return bytes.fromhex(env), "env"
        except ValueError:
            return env.encode("utf-8"), "env"
    kf = os.environ.get(KEY_FILE_ENV)
    if kf:
        data = Path(kf).read_bytes().strip()
        if not data:
            raise AuditKeyError(f"{KEY_FILE_ENV}={kf} is empty")
        return data, "file"
    if os.environ.get(REQUIRE_KEY_ENV, "").strip().lower() in ("1", "true", "yes"):
        raise AuditKeyError(f"{REQUIRE_KEY_ENV} is set: provide {KEY_ENV} or {KEY_FILE_ENV} from outside the data "
                            "volume (the key must not sit beside the audit log)")
    kp = log_path.with_name(log_path.name + ".key")
    if kp.exists():
        return kp.read_bytes(), "beside_log"
    key = secrets.token_bytes(32)
    fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return key, "beside_log"


def load_or_create_key(log_path: Path) -> bytes:
    return resolve_key(log_path)[0]


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
        if key is not None:
            self._key, self.key_source = key, "explicit"
        else:
            self._key, self.key_source = resolve_key(self.path)
        anchor = os.environ.get(ANCHOR_ENV)
        self.anchor_path: Path | None = Path(anchor) if anchor else None
        self._entries: list[AuditEntry] = []
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self.recovery: list[dict[str, Any]] = []
        if self.path.exists():
            self._open_existing()
        else:
            ext = self._read_anchor(self.anchor_path)
            if ext is not None and ext["seq"] > 0:
                self.recovery.append({"msg": "audit log missing although the external anchor names entries; "
                                             "new chain started", "anchor_seq": ext["seq"],
                                      "anchor": str(self.anchor_path)})
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

    @staticmethod
    def _read_anchor(path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) and isinstance(d.get("seq"), int) else None
        except Exception:  # noqa: BLE001 - missing or unreadable: the caller decides what that means
            return None

    def _read_head(self) -> dict[str, Any] | None:
        return self._read_anchor(self.head_path)

    @staticmethod
    def _anchor_problem(name: str, head: dict[str, Any] | None, entries: list[AuditEntry]) -> str | None:
        last = entries[-1] if entries else None
        if head is None:
            # every append writes the anchor, so a non-empty log without one had it removed
            return (f"{name} anchor missing or unreadable beside a non-empty log (ends at seq {last.seq}): "
                    "entries may have been cut off") if entries else None
        if head["seq"] > 0:
            at = entries[head["seq"] - 1] if head["seq"] <= len(entries) else None
            if at is None or at.hash != head.get("hash"):
                return (f"{name} anchor seq {head['seq']} not found in the log "
                        f"(log ends at seq {last.seq if last else 0}): entries were cut off or replaced")
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
            problem = self._anchor_problem("head", self._read_head(), entries)
        if problem is None and self.anchor_path is not None:
            ext = self._read_anchor(self.anchor_path)
            if ext is not None or entries:
                problem = self._anchor_problem("external", ext, entries)
        if problem is not None:
            self._archive(problem, entries[-1].seq if entries else 0)
            return
        self._entries = entries[-self._keep:]
        if entries:
            self._seq, self._last_hash = entries[-1].seq, entries[-1].hash

    def _write_head(self) -> None:
        body = json.dumps({"seq": self._seq, "hash": self._last_hash})
        for p in (self.head_path, self.anchor_path):
            if p is None:
                continue
            try:
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text(body, encoding="utf-8")
                os.replace(tmp, p)
            except OSError:
                if p == self.head_path:
                    raise

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
