from __future__ import annotations

from warroom.contracts import Anomaly, Channel, DataQuality, Prediction, SensorReading


class StreamDetector:
    """Implements contracts.Detector. Per-machine rolling window, trend + forecast with a band.

    Must flag: CURE-07 128→122°C within 60 s of fault start with prediction "stable at 122±1";
    A-BIGOPEN rising toward 120°C with low water flow. One Anomaly per machine episode
    (debounced) until reset(machine_id)."""

    def __init__(self, clock=None, window_s: int = 1800):
        raise NotImplementedError

    def validate(self, reading: SensorReading) -> DataQuality: raise NotImplementedError
    def ingest(self, readings: list[SensorReading]) -> list[Anomaly]: raise NotImplementedError
    def predict(self, machine_id: str, channel: Channel = Channel.TEMP, horizon_s: float = 1800) -> Prediction:
        raise NotImplementedError
    def reset(self, machine_id: str | None = None) -> None: raise NotImplementedError
