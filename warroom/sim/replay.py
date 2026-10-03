"""Record and replay sensor streams (page 5: demo = data replay + fault injection)."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from warroom.contracts import SensorReading


def record_jsonl(readings: Iterable[SensorReading], path: Path) -> int:
    """Append readings as JSON lines; returns count."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("a", encoding="utf-8") as fh:
        for r in readings:
            fh.write(r.model_dump_json())
            fh.write("\n")
            n += 1
    return n


class ReplaySource:
    """Replays a recorded JSONL stream. Consecutive lines with the same timestamp form one
    tick, so a file written by record_jsonl(factory.step()) replays tick by tick."""

    def __init__(self, path: Path, loop: bool = False):
        self.path = Path(path)
        self.loop = loop
        if not self.path.exists():
            raise FileNotFoundError(self.path)

    def _ticks(self) -> Iterator[list[SensorReading]]:
        batch: list[SensorReading] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                r = SensorReading.model_validate_json(line)
                if batch and r.ts != batch[0].ts:
                    yield batch
                    batch = []
                batch.append(r)
        if batch:
            yield batch

    def __iter__(self) -> Iterator[list[SensorReading]]:
        """Yields one list per second (all machines for that tick)."""
        while True:
            produced = False
            for tick in self._ticks():
                produced = True
                yield tick
            if not (self.loop and produced):
                return
