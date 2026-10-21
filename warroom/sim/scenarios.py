"""Scripted demo scenarios. Offsets are seconds after Scenario.start_at (factory time).

Fault params (FaultSpec.params, all floats):
  STEAM_PRESSURE_DROP  target_c (press settles there) | drop (kgf/cm²)
  STEAM_LEAK           rate_c_per_min (temperature fall it causes), floor_kgf_cm2, header_drop
  TEMP_RAMP            target, rate_c_per_min (linear) or tau_s (first order)
  TEMP_STEP            delta
  WATER_FLOW_DROP      target_lpm, ramp_s
  SENSOR_DRIFT         rate_c_per_min (reading bias, process unchanged), max_c
  SENSOR_DROPOUT       frozen (0 = channels go None, 1 = value freezes); use duration_s
  SPIKE                value (absolute) or delta; one sample only
On curing presses temperature faults act through the steam (the platen is steam heated).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from datetime import date

from warroom.contracts import (
    FaultKind,
    FaultSpec,
    Order,
    PlantInput,
    Scenario,
    ScenarioName,
    ScenarioStep,
    TeamId,
    TireSize,
)
from warroom.factory import TZ

# Page 11 runs on 10/03 (the phone card's gap is "10/03 夜班缺 1 人"); page 2 shows 10/22 14:26.
_DEFAULT_START: dict[ScenarioName, datetime] = {
    ScenarioName.NORMAL: datetime(2026, 10, 3, 8, 0, 0, tzinfo=TZ),
    ScenarioName.PRESS7_TEMP_DROP: datetime(2026, 10, 3, 2, 12, 0, tzinfo=TZ),
    ScenarioName.OPEN_MILL_OVERHEAT: datetime(2026, 10, 22, 14, 18, 0, tzinfo=TZ),
    ScenarioName.BAD_DATA: datetime(2026, 10, 3, 9, 0, 0, tzinfo=TZ),
    ScenarioName.CONCURRENT_3: datetime(2026, 10, 3, 10, 0, 0, tzinfo=TZ),
    # issue #17 plant events: three working days after the page-11 Saturday (Mon / Tue / Wed)
    ScenarioName.SHIFT_START: datetime(2026, 10, 5, 7, 50, 0, tzinfo=TZ),
    ScenarioName.STAFFING: datetime(2026, 10, 6, 7, 20, 0, tzinfo=TZ),
    ScenarioName.RUSH_ORDER: datetime(2026, 10, 7, 9, 50, 0, tzinfo=TZ),
    # issue #36 一日循環: the team dataset's one full quiet day (10/21, a Wednesday), from midnight
    ScenarioName.DAY_LOOP: datetime(2026, 10, 21, 0, 0, 0, tzinfo=TZ),
}
DAY_LOOP_SHIFT_START_S = 8 * 3600.0     # 08:00 開工 every loop
PLANT_SCENARIOS = frozenset({ScenarioName.SHIFT_START, ScenarioName.STAFFING, ScenarioName.RUSH_ORDER})
# 情境疊加 (scenario overlay): scenarios whose steps are pure FaultSpec / PlantInput with no story-bound world
# setup, so they can be applied on top of a running scenario relative to now (Runtime.overlay_scenario) instead of
# rebuilding the plant. The story scenarios always reload: page 11 seeds its night-shift gap and the schedule
# around press 7, 一日循環 replays the dataset day from 00:00, 早班開工 is the start of the day's plan, and 正常運轉
# has no steps. web/src/components/scenarioOverlay.ts mirrors this set.
OVERLAY_SCENARIOS = frozenset({ScenarioName.CONCURRENT_3, ScenarioName.OPEN_MILL_OVERHEAT, ScenarioName.BAD_DATA,
                               ScenarioName.RUSH_ORDER, ScenarioName.STAFFING})

# issue #17 timing: each plant event lands 10 min after the scenario start, so the dashboard shows the
# plant running normally first.
SHIFT_START_OFFSET_S = 600.0         # 08:00 早班開工
STAFFING_OFFSET_S = 600.0            # 07:30 the call comes in, 30 min into the curing 早班 (06:00–15:00)
RUSH_OFFSET_S = 600.0                # 10:00 the rush order arrives
# The rush order follows the team dataset's one tight-due order (orders.csv SO-009: 客戶G 中胎, 「唯一需週六加班
# 的訂單（交期緊）」), moved onto the sim clock: 2 tyres due 2 days after it arrives (assumed first lot).
RUSH_ORDER_ID = "#R-009"
RUSH_QTY = 2
RUSH_DUE_DAYS = 2
# The sick call: team dataset staff S13 (加硫組, 早) -> our curing team's 早班 loses one person.
SICK_STAFF_ID = "S13"


def _rush_order(start: datetime) -> Order:
    due: date = start.date() + timedelta(days=RUSH_DUE_DAYS)
    return Order(id=RUSH_ORDER_ID, size=TireSize.M, qty=RUSH_QTY, done=0, due=due, customer="客戶G",
                 mold_id="MD-R009", priority=3)


# Page 11 timing. The steam drop starts at 02:13:15; the detector confirms it ~45 s later
# (02:14:00) with "穩定在 122±1"; the leak starts 02:22:55 at 1/3 °C/min so CURE-07 crosses
# 120 °C at ~02:28:50 (safety core holds it). Verify (state machine) compares a 30-s mean on a
# 5-s sim grid against ">= 121"; that mean first misses at 02:26:30 and the miss is confirmed
# 270 s later, at 02:31:00 = 02:14:00 + 17 min, when it reads 119.50 °C (page 11: 119.5 °C).
P11_STEAM_DROP_OFFSET_S = 75.0
P11_LEAK_OFFSET_S = 655.0
P11_LEAK_RATE_C_PER_MIN = 1.0 / 3.0
# Page 2: cooling water fails 14:20:15; compound reaches ~112 °C near 14:26 and levels ~117.6.
P2_FLOW_DROP_OFFSET_S = 135.0


def _f(fid: str, mid: str, kind: FaultKind, duration_s: float | None = None, **params: float) -> FaultSpec:
    return FaultSpec(id=fid, machine_id=mid, kind=kind, duration_s=duration_s, params=params)


def _build(name: ScenarioName, start: datetime) -> Scenario:
    if name is ScenarioName.NORMAL:
        return Scenario(name=name, title="正常生產", start_at=start, steps=[],
                        expected=["28 台正常運轉，無異常喚醒"])
    if name is ScenarioName.PRESS7_TEMP_DROP:
        return Scenario(
            name=name, title="硫化機 7 號 128 → 122°C：先延長硫化，驗證失敗就換假設 (p.11)", start_at=start,
            steps=[
                ScenarioStep(at_offset_s=P11_STEAM_DROP_OFFSET_S, note="蒸汽壓不足：7 號 128 → 122°C",
                             fault=_f("F-P11-STEAM", "CURE-07", FaultKind.STEAM_PRESSURE_DROP, target_c=122.0)),
                ScenarioStep(at_offset_s=P11_LEAK_OFFSET_S, note="持續洩壓：溫度續降，02:31 達 119.5°C",
                             fault=_f("F-P11-LEAK", "CURE-07", FaultKind.STEAM_LEAK,
                                      rate_c_per_min=P11_LEAK_RATE_C_PER_MIN, floor_kgf_cm2=2.0,
                                      header_drop=0.1)),
            ],
            expected=[
                "02:14:00 事件偵測：7 號 128 → 122°C，預測穩定在 122±1°C → 喚醒指揮官",
                "02:14:01 安全核心：仍在 120–130°C，不停機",
                "~02:28:50 安全核心：7 號 < 120°C → CURE_HOLD（不等 AI）",
                "02:31:00 驗證：7 號 119.5°C → H1 不成立",
                "6、8 號同管線 (H2) 微降 ~0.4°C → 支持洩壓而非感測器漂移",
            ])
    if name is ScenarioName.OPEN_MILL_OVERHEAT:
        return Scenario(
            name=name, title="A 線大開煉機溫度過高：冷卻能力不足 (p.2)", start_at=start,
            steps=[ScenarioStep(at_offset_s=P2_FLOW_DROP_OFFSET_S, note="冷卻水量 40 → 18 L/min",
                                fault=_f("F-P2-FLOW", "A-BIGOPEN", FaultKind.WATER_FLOW_DROP,
                                         target_lpm=18.0, ramp_s=10.0))],
            expected=[
                "14:21 前事件偵測：大開煉機膠溫上升、水流偏低 → 喚醒指揮官",
                "~14:26 膠溫 112°C 持續上升（未加硫上限 120°C，不停機）；時間 14 分",
                "穩態約 117.6°C；未達 120°C，安全核心不停機",
            ])
    if name is ScenarioName.BAD_DATA:
        return Scenario(
            name=name, title="壞資料：讀值異常不叫醒 AI (p.10)", start_at=start,
            steps=[
                ScenarioStep(at_offset_s=60, note="3 號硫化機單點突波 250°C",
                             fault=_f("F-BD-SPIKE", "CURE-03", FaultKind.SPIKE, value=250.0)),
                ScenarioStep(at_offset_s=90, note="A 線密煉機斷訊 10 秒",
                             fault=_f("F-BD-DROP", "A-MIX", FaultKind.SENSOR_DROPOUT, duration_s=10, frozen=0.0)),
                ScenarioStep(at_offset_s=120, note="B 線開煉機讀值凍結 30 秒",
                             fault=_f("F-BD-FROZEN", "B-OPEN", FaultKind.SENSOR_DROPOUT, duration_s=30,
                                      frozen=1.0)),
                ScenarioStep(at_offset_s=150, note="5 號硫化機溫度計漂移 -0.5°C/分（蒸汽正常）",
                             fault=_f("F-BD-DRIFT", "CURE-05", FaultKind.SENSOR_DRIFT, rate_c_per_min=-0.5,
                                      max_c=-6.0)),
                ScenarioStep(at_offset_s=200, note="大開煉機單點突波 +25°C（不可停機）",
                             fault=_f("F-BD-SPIKE2", "A-BIGOPEN", FaultKind.SPIKE, delta=25.0)),
            ],
            expected=["無任何 Anomaly；壞資料計數增加；安全核心不因單點突波停機"])
    if name is ScenarioName.CONCURRENT_3:
        return Scenario(
            name=name, title="同時 3 件異常 (p.10 驗證)", start_at=start,
            steps=[
                ScenarioStep(at_offset_s=60, note="3 號硫化機蒸汽壓不足 128 → 123°C",
                             fault=_f("F-C3-1", "CURE-03", FaultKind.STEAM_PRESSURE_DROP, target_c=123.0)),
                ScenarioStep(at_offset_s=62, note="B 線開煉機冷卻水不足",
                             fault=_f("F-C3-2", "B-OPEN", FaultKind.WATER_FLOW_DROP, target_lpm=15.0,
                                      ramp_s=10.0)),
                ScenarioStep(at_offset_s=64, note="10 號硫化機蒸汽壓不足 128 → 122°C",
                             fault=_f("F-C3-3", "CURE-10", FaultKind.STEAM_PRESSURE_DROP, target_c=122.0)),
            ],
            expected=["3 件獨立 Anomaly，各自在 60 秒內：CURE-03、B-OPEN、CURE-10"])
    if name is ScenarioName.SHIFT_START:
        return Scenario(
            name=name, title="早班開工 08:00：今日訂單、出勤、暖機、原料 → 今日計畫", start_at=start,
            steps=[ScenarioStep(at_offset_s=SHIFT_START_OFFSET_S, note="08:00 早班開工",
                                plant=PlantInput(kind="shift_start", text="08:00 早班開工：排今日計畫"))],
            expected=["08:00 開工事件：查今日訂單、出勤（含請假）、SOP 暖機順序、原料", "一張「今日計畫」核准卡",
                      "核准後：暖機、補料、宣達任務由人員執行，全部完成才結案"])
    if name is ScenarioName.STAFFING:
        return Scenario(
            name=name, title="人力調度：S13 來電請病假，硫化機組早班缺 1 人", start_at=start,
            steps=[ScenarioStep(at_offset_s=STAFFING_OFFSET_S, note="07:30 S13 來電請病假",
                                plant=PlantInput(kind="absence", text=f"{SICK_STAFF_ID} 來電請病假，硫化機組早班缺 1 人",
                                                 staff_id=SICK_STAFF_ID, team=TeamId.CURING, shift_name="早班",
                                                 note="來電請病假"))],
            expected=["07:30 人力事件：硫化機組早班缺 1 人", "求解器提出 A 加班 / B 從 2 線調 1 人", "核准後被指派的人走到硫化區報到"])
    if name is ScenarioName.RUSH_ORDER:
        order = _rush_order(start)
        return Scenario(
            name=name, title=f"插單：急單 {RUSH_ORDER_ID} 中胎 {RUSH_QTY} 條，交期 {order.due:%m/%d}", start_at=start,
            steps=[ScenarioStep(at_offset_s=RUSH_OFFSET_S, note=f"10:00 急單 {RUSH_ORDER_ID} 到單",
                                plant=PlantInput(kind="rush_order", order=order,
                                                 text=f"急單 {RUSH_ORDER_ID} 到：{order.customer} 中胎 {RUSH_QTY} 條，"
                                                      f"交期 {order.due:%m/%d}"))],
            expected=["10:00 訂單事件：CP-SAT 重排", "卡片列出哪些訂單延後幾小時、有沒有誤交期",
                      "核准後補料、換模準備任務由人員執行；佇列重排"])
    if name is ScenarioName.DAY_LOOP:
        return Scenario(
            name=name, title="一日循環：團隊資料集 10/21 一整天，24:00 回到 00:00 重跑", start_at=start,
            steps=[ScenarioStep(at_offset_s=DAY_LOOP_SHIFT_START_S, note="08:00 早班開工",
                                plant=PlantInput(kind="shift_start", text="08:00 早班開工：排今日計畫"))],
            expected=["A03/A04/A06、V06–V08 播放團隊資料集 10/21 的實際曲線，其餘機台由模擬器在同一天運轉",
                      "08:00 開工核准卡；10:00、16:00 出貨車", "24:00 回到 00:00：無誤報，訂單／貨品／人員回到當天起點"])
    raise ValueError(f"unknown scenario {name}")


def get_scenario(name: ScenarioName, start_at: datetime | None = None) -> Scenario:
    """PRESS7_TEMP_DROP (page 11), OPEN_MILL_OVERHEAT (page 2), BAD_DATA (page 10),
    CONCURRENT_3 (page 10 bench), NORMAL. start_at overrides the default start; step
    offsets keep their meaning."""
    name = ScenarioName(name)
    start = (start_at or _DEFAULT_START[name]).astimezone(TZ)
    return _build(name, start)


def list_scenarios() -> list[Scenario]:
    return [get_scenario(n) for n in ScenarioName]


def scenario_time(s: Scenario, offset_s: float) -> datetime:
    return s.start_at + timedelta(seconds=offset_s)
