"""Simulation clock shared by sim, detect, agent and api. Implemented (architect-owned).

SimClock runs factory time at `speed` sim-seconds per wall-second and can be paused or
advanced manually (tests and scenario replay use advance())."""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from warroom.factory import TZ


class SimClock:
    def __init__(self, start: datetime | None = None, speed: float = 1.0, manual: bool = False):
        self._sim0 = (start or datetime.now(TZ)).astimezone(TZ)
        self._wall0 = time.monotonic()
        self.speed = speed
        self.manual = manual          # manual=True: time only moves with advance()
        self.paused = False
        self._offset = 0.0

    def now(self) -> datetime:
        if self.manual or self.paused:
            return self._sim0 + timedelta(seconds=self._offset)
        return self._sim0 + timedelta(seconds=self._offset + (time.monotonic() - self._wall0) * self.speed)

    def advance(self, seconds: float) -> datetime:
        self._offset += seconds
        return self.now()

    def _rebase(self) -> None:
        cur = self.now()
        self._sim0, self._offset, self._wall0 = cur, 0.0, time.monotonic()

    def set_speed(self, speed: float) -> None:
        self._rebase()
        self.speed = speed

    def pause(self) -> None:
        self._rebase()
        self.paused = True

    def play(self) -> None:
        self._rebase()
        self.paused = False

    def reset(self, start: datetime) -> None:
        self._sim0, self._offset, self._wall0 = start.astimezone(TZ), 0.0, time.monotonic()
