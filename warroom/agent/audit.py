from __future__ import annotations

from pathlib import Path
from typing import Any

from warroom.contracts import AuditEntry, AuditKind


class JsonlAuditLog:
    """Implements contracts.AuditLog: append-only JSONL, sha256 hash chain, fsync on append.
    Publishes each entry on the bus (WsType.AUDIT) when bus is given."""

    def __init__(self, path: Path, bus=None, clock=None):
        raise NotImplementedError

    def append(self, kind: AuditKind, actor: str, payload: dict[str, Any],
               event_id: str | None = None) -> AuditEntry: raise NotImplementedError
    def tail(self, since_seq: int = 0, limit: int = 200) -> list[AuditEntry]: raise NotImplementedError
    def verify_chain(self) -> bool: raise NotImplementedError
