"""Kyle 10-21 (3D factory): the crew finished the repair, or 回報處置完成 was pressed, and the machine stayed 異常 —
the way back to normal was read as a new 「溫度驟降」, because the detector's baseline restarted from the faulty
readings. A repaired machine goes back to its normal baseline; finished repair tasks clear the fault."""
from __future__ import annotations

from datetime import timedelta

from tests.agent_fakes import T0
from tests.test_plan_talk import _rt
from warroom.api.runtime import REPAIR_KINDS
from warroom.contracts import AuditKind, SensorReading
from warroom.detect.detector import StreamDetector


def _feed(det, start, minutes, temp_at):
    found = []
    for i in range(int(minutes * 60)):
        ts = start + timedelta(seconds=i)
        found += det.ingest([SensorReading(ts=ts, machine_id="A-BIGOPEN", temp_c=temp_at(i / 60.0),
                                           pressure_kgf_cm2=50.0, water_flow_lpm=40.0, seq=i)])
    return found


def _overheat_then_back(repaired: bool):
    det = StreamDetector()
    t = T0
    assert not _feed(det, t, 10, lambda m: 100.0)                                 # normal
    t += timedelta(minutes=10)
    assert _feed(det, t, 8, lambda m: 100.0 + min(12.0, m * 3.0))                # 100 → 112 °C: 過熱
    t += timedelta(minutes=8)
    det.reset("A-BIGOPEN", repaired=repaired)
    return _feed(det, t, 15, lambda m: max(100.0, 112.0 - m * 1.8))             # fixed: back to 100 °C


def test_the_way_back_after_a_repair_is_not_a_new_anomaly():
    assert _overheat_then_back(repaired=True) == []


def test_without_repaired_the_way_back_looks_like_a_drop():
    # the old behaviour, kept for a reset that is not a repair (the new level really is the new normal)
    assert _overheat_then_back(repaired=False)


def test_repair_kinds_are_the_machine_fixing_tasks():
    assert {"檢修", "停用檢修", "放慢投料", "暫停投料"} == set(REPAIR_KINDS)
    assert not {"換模準備", "設定硫化時間", "加班補位", "宣達"} & REPAIR_KINDS


async def test_clear_after_page2_overheat_raises_no_new_anomaly(tmp_path):
    rt = _rt(tmp_path, "open_mill_overheat")
    await rt.start()
    try:
        def anomalies():
            return [e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.ANOMALY
                    and (e.payload.get("machine_id") == "A-BIGOPEN")]
        for _ in range(3600):
            await rt.tick(1.0, publish=False)
            if anomalies():
                break
        assert anomalies(), "page-2 overheat was not detected"
        rt.clear_faults("A-BIGOPEN")                  # 3D 回報處置完成 / all repair tasks done
        before = len(anomalies())
        for _ in range(20 * 60):
            await rt.tick(1.0, publish=False)
        assert len(anomalies()) == before, [a.payload.get("message") for a in anomalies()[before:]]
    finally:
        await rt.stop()
