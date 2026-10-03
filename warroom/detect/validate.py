from __future__ import annotations

from warroom.contracts import DataQuality, SensorReading


def validate_reading(reading: SensorReading, previous: SensorReading | None = None,
                     neighbours: list[SensorReading] | None = None) -> DataQuality:
    """Range, jump, stale and neighbour checks (page 10 資料校核: 範圍、跳動、鄰機比對).
    Anything not OK must never wake the commander."""
    raise NotImplementedError
