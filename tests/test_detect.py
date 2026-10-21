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


def _ramp_back(f: Factory, d: StreamDetector, mid: str, ramp_s: int, glitch=None) -> list:
    """維修完成 then 回溫中 the way the runtime runs it (_apply_repairs, _begin_recovery, _advance_recoveries): the fault
    cleared, the detector reset, the ramp started and moved to nominal over ramp_s readings in Runtime.tick's order
    (sim.step, then the ramp moves and validation is told, then the readings are checked: a reading comes from the ramp
    one step back); ramp_s=1: the whole ramp ran while the plant clock stood still (another event's model call), the
    next reading comes from its end. glitch(k, readings) may change the sensor readings of step k (the ramp itself
    stays as the sim runs it)."""
    f.clear_faults(mid)
    d.reset(mid, repaired=True)
    f.begin_recovery(mid)
    d.follow_recovery(mid, f.recover(mid, 0.0))
    if ramp_s == 1:
        d.follow_recovery(mid, f.recover(mid, 1.0), done=True)
    found = []
    for k in range(1, ramp_s + 1):
        readings = f.step(1)
        if ramp_s > 1:
            d.follow_recovery(mid, f.recover(mid, k / ramp_s), done=k == ramp_s)
        found += d.ingest(glitch(k, readings) if glitch else readings)
    return found


@pytest.mark.parametrize("scenario,mid,settle_s,nominal,deep", [
    (ScenarioName.PRESS7_TEMP_DROP, "CURE-07", 600, 128.0, None),    # 02:22, stable at 122 °C
    (ScenarioName.NORMAL, "CURE-02", 520, 128.0, 114.0),             # a deep steam drop: 14 °C back
    (ScenarioName.OPEN_MILL_OVERHEAT, "A-BIGOPEN", 720, 100.0, None),  # cooling water failed: well over 100 °C
])
@pytest.mark.parametrize("ramp_s", [1, 2, 4, 40, 240])
def test_the_way_back_after_a_fast_repair_is_not_bad_data(scenario, mid, settle_s, nominal, deep, ramp_s):
    """回溫中 (Settings.recover_fast_s): the repaired machine's readings are moved to nominal over 4 WALL seconds, i.e. 4, 40
    and 240 sim seconds at 1x, 10x and 60x, or at once (1: another event's model call held the sim clock for the whole
    ramp, concurrent_3 at 60x). Validation read that as bad data: faster than a platen follows its steam (與比對值不符;
    concurrent_3 at 10x: 9 readings of 10 號 擋下 after its repair) or a step bigger than physics (跳值; the mill at
    60x). The runtime tells validation where its ramp is (follow_recovery): a reading on it is the repair, not a
    sensor; once the ramp ended it is checked as before."""
    f, d = setup(scenario)
    if deep is not None:
        drive(f, d, 120)
        f.inject_fault(FaultSpec(id="deep", machine_id=mid, kind=FaultKind.STEAM_PRESSURE_DROP, params={"target_c": deep}))
    assert mid in [a.machine_id for a in drive(f, d, settle_s - (120 if deep is not None else 0))]
    assert not [k for k in d.quality_counts if k[0] == mid]
    found = _ramp_back(f, d, mid, ramp_s)
    found += drive(f, d, 600)
    assert not [a for a in found if a.machine_id == mid]
    assert not {k: n for k, n in d.quality_counts.items() if k[0] == mid}, dict(d.quality_counts)
    assert abs(f.machine(mid).last_reading.temp_c - nominal) < 0.5
    assert not d._tracks[mid].recovering                                # back: watched again
    f.inject_fault(FaultSpec(id="spike", machine_id=mid, kind=FaultKind.SPIKE, params={"delta": 25.0}))
    drive(f, d, 5)
    assert d.quality_counts[(mid, DataQuality.JUMP)] == 1               # a glitch is bad data again
    if mid.startswith("CURE-"):
        f.inject_fault(FaultSpec(id="dr", machine_id=mid, kind=FaultKind.SENSOR_DRIFT, params={"rate_c_per_min": -0.3}))
        assert drive(f, d, 600) == []
        assert d.last_quality[mid] is DataQuality.NEIGHBOR_MISMATCH     # the steam model checks it again


def _offset(mid: str, k0: int, per_step: float = 0.0, once: float = 0.0):
    """A sensor glitch from ramp step k0 on: a thermometer drifting per_step °C a reading, or one reading off by once."""
    def glitch(k: int, readings: list[SensorReading]) -> list[SensorReading]:
        if k < k0:
            return readings
        bias = per_step * (k - k0 + 1) + (once if k == k0 else 0.0)
        out = [r.model_copy(update={"temp_c": round(r.temp_c + bias, 2)}) if r.machine_id == mid else r
               for r in readings]
        glitch.ts.extend(r.ts for r in out if r.machine_id == mid)
        return out
    glitch.ts = []
    return glitch


@pytest.mark.parametrize("per_step", [-0.5, 0.5])
def test_a_drifting_thermometer_during_the_way_back_is_still_bad_data(per_step):
    """回溫中 is no blanket pass: the ramp stands in for the steam model, so a thermometer drifting off it (away from
    normal or ahead of the ramp toward it, 0.5 °C a reading, never a jump) is 與比對值不符 within a few readings and the
    model keeps following the ramp, not the bad sensor."""
    f, d = setup(ScenarioName.PRESS7_TEMP_DROP)
    drive(f, d, 600)
    found = _ramp_back(f, d, "CURE-07", 40, glitch=_offset("CURE-07", 10, per_step=per_step))
    assert not [a for a in found if a.machine_id == "CURE-07"]
    bad = [q for _, mid, ch, q in d.bad_data_log if mid == "CURE-07"]
    assert bad and set(bad) == {DataQuality.NEIGHBOR_MISMATCH}, bad
    assert len(bad) >= 25                                                  # every reading once it is 1.5 °C off
    assert abs(d._tracks["CURE-07"].expected_temp - f.machine("CURE-07").last_reading.temp_c) < 1.0


@pytest.mark.parametrize("mid,scenario,settle_s,toward", [
    ("CURE-07", ScenarioName.PRESS7_TEMP_DROP, 600, 6.0),        # 122 → 128 °C: a spike up to normal
    ("A-BIGOPEN", ScenarioName.OPEN_MILL_OVERHEAT, 720, -15.0),  # well over 100 °C → 100: a spike down to normal
])
def test_a_spike_during_the_way_back_is_still_bad_data(mid, scenario, settle_s, toward):
    """One reading during 回溫中 that jumps toward normal ahead of the ramp (Codex: the mill at 115 °C, one 100 °C sample)
    is a glitch, not the repair: 跳值, one reading, and the next one on the ramp is fine again."""
    f, d = setup(scenario)
    assert mid in [a.machine_id for a in drive(f, d, settle_s)]
    spike = _offset(mid, 10, once=toward)
    found = _ramp_back(f, d, mid, 40, glitch=spike)
    assert not [a for a in found if a.machine_id == mid]
    bad = [(ts, ch, q) for ts, m, ch, q in d.bad_data_log if m == mid]
    assert bad == [(spike.ts[0], Channel.TEMP, DataQuality.JUMP)], bad      # the spike itself, not the reading after
    assert d.last_quality[mid] is DataQuality.OK


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
