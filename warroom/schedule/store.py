from __future__ import annotations

from pathlib import Path

from warroom.contracts import Schedule, ScheduleVersion


class FileScheduleStore:
    """Implements contracts.ScheduleStore. Every version persisted as JSON under `root`;
    exactly one ACTIVE; rollback re-activates the previous ACTIVE (page 7 一鍵回復)."""

    def __init__(self, root: Path, initial: Schedule | None = None):
        raise NotImplementedError

    def current(self) -> ScheduleVersion: raise NotImplementedError
    def get(self, version: int) -> ScheduleVersion: raise NotImplementedError
    def versions(self) -> list[ScheduleVersion]: raise NotImplementedError
    def propose(self, schedule: Schedule, reason: str, event_id: str | None = None) -> ScheduleVersion:
        raise NotImplementedError
    def activate(self, version: int) -> ScheduleVersion: raise NotImplementedError
    def rollback(self, to_version: int | None = None) -> ScheduleVersion: raise NotImplementedError
