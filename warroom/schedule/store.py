"""Schedule version store with one-click rollback (page 7). Owner: schedule builder.

Every version is one JSON file `v0001.json` under `root` (data/runtime/schedules/ in the app).
Exactly one version is ACTIVE. propose() adds a PROPOSED version whose parent is the current
ACTIVE one; activate() supersedes the old ACTIVE; rollback() re-activates the previous ACTIVE
(or a named, previously active version) and marks the current one ROLLED_BACK. Nothing is ever deleted.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from warroom import factory as F
from warroom.contracts import Schedule, ScheduleStatus, ScheduleVersion, SolverName


class FileScheduleStore:
    """Implements contracts.ScheduleStore. Every version persisted as JSON under `root`;
    exactly one ACTIVE; rollback re-activates the previous ACTIVE (page 7 一鍵回復)."""

    def __init__(self, root: Path, initial: Schedule | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._v: dict[int, ScheduleVersion] = {}
        self._activation_log: list[int] = []     # order of activations, for rollback
        for f in sorted(self.root.glob("v*.json")):
            v = ScheduleVersion.model_validate_json(f.read_text(encoding="utf-8"))
            self._v[v.version] = v
        log = self.root / "activations.json"
        if log.exists():
            self._activation_log = [int(x) for x in json.loads(log.read_text(encoding="utf-8"))]
        if not self._v:
            if initial is None:
                now = datetime.now(F.TZ).replace(microsecond=0)
                initial = Schedule(id="SCH-EMPTY", created_at=now, horizon_start=now,
                                   horizon_end=now + timedelta(days=7), jobs=[], solver=SolverName.HEURISTIC,
                                   warnings=["empty initial schedule"])
            v = ScheduleVersion(version=1, parent_version=None, status=ScheduleStatus.ACTIVE,
                                created_at=initial.created_at, reason="initial", schedule=initial)
            self._write(v)
            self._activation_log = [1]
            self._write_log()
        elif not any(v.status is ScheduleStatus.ACTIVE for v in self._v.values()):
            raise RuntimeError(f"schedule store {self.root} has no ACTIVE version")

    # ---- persistence ----
    def _write(self, v: ScheduleVersion) -> None:
        self._v[v.version] = v
        path = self.root / f"v{v.version:04d}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(v.model_dump_json(indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _write_log(self) -> None:
        tmp = self.root / "activations.json.tmp"
        tmp.write_text(json.dumps(self._activation_log), encoding="utf-8")
        os.replace(tmp, self.root / "activations.json")

    # ---- protocol ----
    def current(self) -> ScheduleVersion:
        with self._lock:
            return next(v for v in self._v.values() if v.status is ScheduleStatus.ACTIVE)

    def get(self, version: int) -> ScheduleVersion:
        with self._lock:
            if version not in self._v:
                raise KeyError(f"schedule version {version} not found")
            return self._v[version]

    def versions(self) -> list[ScheduleVersion]:
        with self._lock:
            return [self._v[k] for k in sorted(self._v)]

    def propose(self, schedule: Schedule, reason: str, event_id: str | None = None) -> ScheduleVersion:
        with self._lock:
            v = ScheduleVersion(version=max(self._v) + 1, parent_version=self.current().version,
                                status=ScheduleStatus.PROPOSED, created_at=schedule.created_at, reason=reason,
                                event_id=event_id, schedule=schedule)
            self._write(v)
            return v

    def activate(self, version: int) -> ScheduleVersion:
        with self._lock:
            target = self.get(version)
            cur = self.current()
            if target.version == cur.version:
                return cur
            if target.status is not ScheduleStatus.PROPOSED:
                raise ValueError(f"version {version} is {target.status.value}; only PROPOSED can be activated "
                                 "(use rollback for older versions)")
            self._write(cur.model_copy(update={"status": ScheduleStatus.SUPERSEDED}))
            new = target.model_copy(update={"status": ScheduleStatus.ACTIVE})
            self._write(new)
            self._activation_log.append(new.version)
            self._write_log()
            return new

    def rollback(self, to_version: int | None = None) -> ScheduleVersion:
        """None = the ACTIVE version before the current one (一鍵回復).

        Only a version that was ACTIVE before (SUPERSEDED or ROLLED_BACK) can be rolled back to.
        A PROPOSED or DRAFT version has not passed the gate and approval, so rolling "back" to it
        would activate an unapproved schedule; that raises ValueError (the API answers 409)."""
        with self._lock:
            cur = self.current()
            if to_version is None:
                prior = [v for v in self._activation_log if v != cur.version]
                if not prior:
                    raise ValueError("no earlier active version to roll back to")
                to_version = prior[-1]
            target = self.get(to_version)
            if target.version == cur.version:
                return cur
            if target.status not in (ScheduleStatus.SUPERSEDED, ScheduleStatus.ROLLED_BACK):
                raise ValueError(f"version {target.version} is {target.status.value}; rollback only re-activates a "
                                 "version that was approved and active before (superseded or rolled_back)")
            self._write(cur.model_copy(update={"status": ScheduleStatus.ROLLED_BACK}))
            new = target.model_copy(update={"status": ScheduleStatus.ACTIVE})
            self._write(new)
            # drop the rolled-back version from the activation chain so a second rollback
            # goes one step further back
            self._activation_log = [x for x in self._activation_log if x != cur.version]
            if not self._activation_log or self._activation_log[-1] != new.version:
                self._activation_log.append(new.version)
            self._write_log()
            return new
