"""Per-machine memory notes the supervisor writes in 後台 (issue #37), one JSON file.

Read-only to the agent for now: nothing in warroom/agent reads this file, and nothing wires it into a
prompt. A future read tool would wrap `for_machine()` the same way IncidentStore is meant to be wrapped.
Writes go through `add()` only (the admin API audits each one); the file is replaced atomically."""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

MAX_TEXT = 500


@dataclass(frozen=True)
class MemoryNote:
    id: str
    machine_id: str
    text: str
    author: str
    created_at: str        # factory time, ISO


class MemoryNotes:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> list[MemoryNote]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [MemoryNote(**{k: str(d.get(k, "")) for k in MemoryNote.__dataclass_fields__}) for d in raw
                if isinstance(d, dict)]

    def all(self) -> list[MemoryNote]:
        with self._lock:
            return self._read()

    def for_machine(self, machine_id: str) -> list[MemoryNote]:
        """Notes on one machine, newest first."""
        return sorted((n for n in self.all() if n.machine_id == machine_id), key=lambda n: n.created_at, reverse=True)

    def add(self, machine_id: str, text: str, author: str, now: datetime) -> MemoryNote:
        text = text.strip()
        if not text or len(text) > MAX_TEXT:
            raise ValueError(f"note text must be 1..{MAX_TEXT} characters")
        note = MemoryNote(id=f"MN-{uuid.uuid4().hex[:8]}", machine_id=machine_id, text=text, author=author,
                          created_at=now.isoformat())
        with self._lock:
            notes = self._read() + [note]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps([asdict(n) for n in notes], ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        return note
