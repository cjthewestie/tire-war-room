"""Data validation (page 10 資料校核：範圍、跳動、鄰機比對). Bad data never wakes the AI."""
from __future__ import annotations

import math
import statistics

from warroom.contracts import Channel, DataQuality, SensorReading

# physically possible ranges per channel
RANGES: dict[Channel, tuple[float, float]] = {
    Channel.TEMP: (-20.0, 300.0),
    Channel.PRESSURE: (0.0, 200.0),
    Channel.WATER_FLOW: (0.0, 300.0),
    Channel.STEAM: (0.0, 20.0),
}
# largest physical change per second; a bigger step is a sensor glitch
JUMP_PER_S: dict[Channel, float] = {
    Channel.TEMP: 3.0,
    Channel.PRESSURE: 8.0,
    Channel.WATER_FLOW: 30.0,
    Channel.STEAM: 1.5,
}
NEIGHBOUR_TEMP_DEV_C = 30.0      # same-kind neighbours (presses on one header) never differ this much
NEIGHBOUR_SPREAD_C = 5.0
REDUNDANT_TOL_C = 1.5            # temp vs steam-implied (lag-modelled) temperature on a press


def channel_value(r: SensorReading, ch: Channel) -> float | None:
    return getattr(r, ch.value)


def check_channel(ch: Channel, value: float | None, prev_value: float | None = None,
                  dt_s: float = 1.0) -> DataQuality:
    if value is None:
        return DataQuality.MISSING
    if math.isnan(value):
        return DataQuality.OUT_OF_RANGE
    lo, hi = RANGES[ch]
    if not lo <= value <= hi:
        return DataQuality.OUT_OF_RANGE
    if prev_value is not None and abs(value - prev_value) > JUMP_PER_S[ch] * max(1.0, dt_s):
        return DataQuality.JUMP
    return DataQuality.OK


def validate_reading(reading: SensorReading, previous: SensorReading | None = None,
                     neighbours: list[SensorReading] | None = None,
                     expected_temp: float | None = None) -> DataQuality:
    """Range, jump and neighbour checks; the first problem found wins.

    - temp_c missing -> MISSING (temperature is the primary channel); other missing channels are skipped.
    - out of physical range -> OUT_OF_RANGE; step bigger than physics allows since `previous` -> JUMP.
    - `neighbours` (same-kind machines, e.g. presses on one steam header): temp differs from their
      median by > 30 °C while they agree with each other -> NEIGHBOR_MISMATCH.
    - `expected_temp` (redundant sensor model, e.g. a press's steam-implied temperature with the
      thermal lag applied): |temp - expected| > 1.5 °C -> NEIGHBOR_MISMATCH.
    Stale (frozen) values need a run of samples; StreamDetector checks that.
    """
    if reading.temp_c is None:
        return DataQuality.MISSING
    dt = 1.0
    if previous is not None:
        dt = max(1.0, (reading.ts - previous.ts).total_seconds())
    for ch in Channel:
        v = channel_value(reading, ch)
        if v is None:
            continue
        pv = channel_value(previous, ch) if previous is not None else None
        q = check_channel(ch, v, pv, dt)
        if q is not DataQuality.OK:
            return q
    if neighbours:
        temps = [n.temp_c for n in neighbours if n.temp_c is not None and n.machine_id != reading.machine_id]
        if len(temps) >= 2 and max(temps) - min(temps) <= NEIGHBOUR_SPREAD_C and \
                abs(reading.temp_c - statistics.median(temps)) > NEIGHBOUR_TEMP_DEV_C:
            return DataQuality.NEIGHBOR_MISMATCH
    if expected_temp is not None and abs(reading.temp_c - expected_temp) > REDUNDANT_TOL_C:
        return DataQuality.NEIGHBOR_MISMATCH
    return DataQuality.OK
