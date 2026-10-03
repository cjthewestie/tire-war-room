from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

from warroom.contracts import SensorReading


def record_jsonl(readings: Iterable[SensorReading], path: Path) -> int:
    """Append readings as JSON lines; returns count."""
    raise NotImplementedError


class ReplaySource:
    """Replays a recorded JSONL stream (demo = data replay + fault injection, page 5)."""

    def __init__(self, path: Path, loop: bool = False):
        raise NotImplementedError

    def __iter__(self) -> Iterator[list[SensorReading]]:
        """Yields one list per second (all machines for that tick)."""
        raise NotImplementedError
