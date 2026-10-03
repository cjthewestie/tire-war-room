"""warroom/detect: bad-data validation, non-LLM forecasting, detection latency, debounce."""
import time
from datetime import timedelta

import pytest

from warroom.contracts import (
    AnomalyKind,
    Channel,
    DataQuality,
    Detector,
    FaultKind,
    FaultSpec,
    ScenarioName,
    SensorReading,
    Severity,
)
from warroom.detect import StreamDetector, validate_reading
from warroom.factory import DETECTION_TARGET_S, TZ
from warroom.sim import Factory


def drive(f: Factory, d: StreamDetector, seconds: int):
    found = []
    for _ in range(seconds):
        found += d.ingest(f.step(1))
    return found


def setup(scenario: ScenarioName, seed: int = 7):
    f = Factory(scenario=scenario, seed=seed, backfill_min=0)
    return f, StreamDetector(fault_start_lookup=f.fault_started_at)


def _r(**kw):
    from datetime import datetime
    base = {"ts": datetime(2026, 10, 3, 2, 14, tzinfo=TZ), "machine_id": "CURE-07", "temp_c": 128.0,
            "pressure_kgf_cm2": 20.0, "steam_kgf_cm2": 5.0}
    base.update(kw)
    return SensorReading(**base)


# ----------------------------------------------------------------------------- validation
def test_validate_reading_rules():
    ok = _r()
    assert validate_reading(ok) is DataQuality.OK
    assert validate_reading(_r(temp_c=None)) is DataQuality.MISSING
    assert validate_reading(_r(temp_c=999.0)) is DataQuality.OUT_OF_RANGE
    assert validate_reading(_r(temp_c=float("nan"))) is DataQuality.OUT_OF_RANGE
    assert validate_reading(_r(steam_kgf_cm2=-1.0)) is DataQuality.OUT_OF_RANGE
    nxt = _r(ts=ok.ts + timedelta(seconds=1), temp_c=150.0)
    assert validate_reading(nxt, previous=ok) is DataQuality.JUMP
    late = _r(ts=ok.ts + timedelta(seconds=20), temp_c=150.0)
    assert validate_reading(late, previous=ok) is DataQuality.OK          # jump limit scales with the gap
    neigh = [_r(machine_id="CURE-06"), _r(machine_id="CURE-08", temp_c=127.8)]
    assert validate_reading(_r(temp_c=60.0), neighbours=neigh) is DataQuality.NEIGHBOR_MISMATCH
    assert validate_reading(_r(temp_c=120.0), neighbours=neigh) is DataQuality.OK   # a real 8 °C drop is fine
    assert validate_reading(_r(temp_c=124.0), expected_temp=128.0) is DataQuality.NEIGHBOR_MISMATCH


def test_detector_protocol_and_validate_is_pure():
    d = StreamDetector()
    assert isinstance(d, Detector)
    assert d.validate(_r(temp_c=500.0)) is DataQuality.OUT_OF_RANGE
    assert d.validate(_r()) is DataQuality.OK
    assert d._tracks == {}                                                # validate() does not mutate


# ----------------------------------------------------------------------------- no false alarms
@pytest.mark.parametrize("seed", [1, 7, 42])
def test_normal_has_no_false_positives(seed):
    f, d = setup(ScenarioName.NORMAL, seed)
    assert drive(f, d, 1200) == []
    assert sum(d.quality_counts.values()) == 0


def test_bad_data_never_wakes_the_ai():
    f, d = setup(ScenarioName.BAD_DATA)
    assert drive(f, d, 420) == []
    kinds = {q for (_, q) in d.quality_counts}
    assert {DataQuality.JUMP, DataQuality.MISSING, DataQuality.STALE, DataQuality.NEIGHBOR_MISMATCH} <= kinds
    assert d.quality_counts[("CURE-03", DataQuality.JUMP)] == 1          # the 250 °C spike
    assert d.quality_counts[("A-BIGOPEN", DataQuality.JUMP)] == 1        # the +25 °C spike
    assert d.quality_counts[("CURE-05", DataQuality.NEIGHBOR_MISMATCH)] > 0   # drift caught by steam model


def test_sensor_drift_alone_is_caught_before_it_looks_like_a_drop():
    f, d = setup(ScenarioName.NORMAL)
    drive(f, d, 60)
    f.inject_fault(FaultSpec(id="dr", machine_id="CURE-04", kind=FaultKind.SENSOR_DRIFT,
                             params={"rate_c_per_min": -0.3}))
    assert drive(f, d, 1800) == []                                        # 9 °C of fake drop, no wake-up
    assert d.last_quality["CURE-04"] is DataQuality.NEIGHBOR_MISMATCH


# ----------------------------------------------------------------------------- page 11
def test_press7_detected_within_a_minute_with_steady_state_forecast():
    f, d = setup(ScenarioName.PRESS7_TEMP_DROP)
    found = drive(f, d, 150)
    assert len(found) == 1
    a = found[0]
    assert a.machine_id == "CURE-07" and a.kind is AnomalyKind.TEMP_DROP
    assert a.detection_latency_s is not None and a.detection_latency_s <= DETECTION_TARGET_S
    assert a.ts.strftime("%H:%M:%S") == "02:14:00"
    assert a.message == "7 號 128 → 122°C，預測穩定在 122±1°C"
    p = a.prediction
    assert p.model == "exp-approach" and abs(p.predicted_value - 122.0) < 0.5
    assert p.band_lo <= 122.0 <= p.band_hi and p.band_hi - p.band_lo <= 2.5
    assert set(a.related_machines) == {"CURE-06", "CURE-08"}             # same steam header (p.11)
    assert abs(a.baseline - 128.0) < 0.3 and a.severity is Severity.WARN


def test_press7_debounced_until_reset_then_leak_detected():
    f, d = setup(ScenarioName.PRESS7_TEMP_DROP)
    first = drive(f, d, 150)
    assert [a.machine_id for a in first] == ["CURE-07"]
    # through 02:31 the temperature keeps falling, but the episode is already open
    later = drive(f, d, 19 * 60 - 150)
    assert later == []
    d.reset("CURE-07")
    f2, d2 = setup(ScenarioName.PRESS7_TEMP_DROP)
    drive(f2, d2, 600)                      # 02:22 — stable at 122 after the first anomaly
    d2.reset("CURE-07")                     # state machine closes the first episode
    leak = drive(f2, d2, 9 * 60)
    assert [a.machine_id for a in leak] == ["CURE-07"]
    assert leak[0].kind is AnomalyKind.TEMP_DROP and leak[0].severity is Severity.CRITICAL
    assert "持續下降" in leak[0].message


def test_predict_api():
    f, d = setup(ScenarioName.PRESS7_TEMP_DROP)
    drive(f, d, 150)
    p = d.predict("CURE-07", Channel.TEMP, 1800)
    assert p.machine_id == "CURE-07" and p.channel is Channel.TEMP and p.horizon_s == 1800
    assert abs(p.predicted_value - 122) < 0.6
    q = d.predict("CURE-03")
    assert abs(q.predicted_value - 128) < 0.6 and q.band_lo < q.predicted_value < q.band_hi
    with pytest.raises(KeyError):
        StreamDetector().predict("CURE-07")


# ----------------------------------------------------------------------------- page 2
def test_open_mill_overheat_detected_with_cause():
    f, d = setup(ScenarioName.OPEN_MILL_OVERHEAT)
    found = drive(f, d, 300)
    assert len(found) == 1
    a = found[0]
    assert a.machine_id == "A-BIGOPEN" and a.kind is AnomalyKind.OVERHEAT
    assert a.detection_latency_s <= DETECTION_TARGET_S
    assert "水流偏低" in a.message and "120°C 上限" in a.message
    assert a.prediction.trend_per_min > 1.0 and a.severity is Severity.CRITICAL
    assert a.message.startswith("A 線大開煉機")


def test_flow_drop_without_heating_reports_water_flow_low():
    f, d = setup(ScenarioName.NORMAL)
    drive(f, d, 60)
    f.inject_fault(FaultSpec(id="w", machine_id="A-WIND", kind=FaultKind.WATER_FLOW_DROP,
                             params={"target_lpm": 2.0, "ramp_s": 5}))
    found = drive(f, d, 120)
    assert [a.kind for a in found] == [AnomalyKind.WATER_FLOW_LOW]
    assert found[0].detection_latency_s <= DETECTION_TARGET_S


# ----------------------------------------------------------------------------- concurrency, multi-machine
def test_three_concurrent_anomalies_each_within_a_minute():
    f, d = setup(ScenarioName.CONCURRENT_3)
    found = drive(f, d, 200)
    assert sorted(a.machine_id for a in found) == ["B-OPEN", "CURE-03", "CURE-10"]
    assert all(a.detection_latency_s <= DETECTION_TARGET_S for a in found)
    assert len({a.id for a in found}) == 3


def test_header_wide_steam_loss_is_one_multi_machine_event():
    f, d = setup(ScenarioName.NORMAL)
    drive(f, d, 60)
    for p in ("CURE-06", "CURE-07", "CURE-08"):
        f.inject_fault(FaultSpec(id=f"h-{p}", machine_id=p, kind=FaultKind.STEAM_PRESSURE_DROP,
                                 params={"target_c": 121.0}))
    found = drive(f, d, 120)
    assert len(found) == 1 and found[0].kind is AnomalyKind.MULTI_MACHINE
    assert set([found[0].machine_id] + found[0].related_machines) == {"CURE-06", "CURE-07", "CURE-08"}
    assert "H2" in found[0].message


def test_accelerated_time_still_detects():
    f = Factory(scenario=ScenarioName.PRESS7_TEMP_DROP, backfill_min=0)
    d = StreamDetector(fault_start_lookup=f.fault_started_at)
    found = []
    for _ in range(30):
        found += d.ingest(f.step(10))
    assert [a.machine_id for a in found] == ["CURE-07"]
    assert found[0].detection_latency_s <= DETECTION_TARGET_S + 10


def test_ingest_is_fast():
    f, d = setup(ScenarioName.PRESS7_TEMP_DROP)
    ticks = [f.step(1) for _ in range(200)]
    t0 = time.perf_counter()
    for t in ticks:
        d.ingest(t)
    per_tick_ms = (time.perf_counter() - t0) * 1000 / len(ticks)
    assert per_tick_ms < 20.0, per_tick_ms
