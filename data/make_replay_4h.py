#!/usr/bin/env python3
"""連續 4 小時、28 台機台、每秒一筆的感測器資料（repo 的 SensorReading JSONL，可被 warroom.sim.ReplaySource 重播）。

時段：2026-10-22 12:30–16:30（含事件 1：14:12 冷卻水異常，14:26 大開煉機膠溫約 112°C，全程未達 120°C）。
A03/A04/A06 與 V06–V08 的曲線沿用合成資料腳本（gen_signals.py）；其餘機台用 repo 的額定值與雜訊（warroom.sim.physics）。
蒸汽壓採 repo 的線性換算（128°C 對應 5.0 kgf/cm²），不是飽和蒸汽表；否則 repo 偵測器會判成讀值異常。

用法（在 repo 根目錄）：
    uv run python data/make_replay_4h.py                      # 輸出 data/runtime/replay_20261022_4h.jsonl
    uv run python data/make_replay_4h.py <輸出檔> [seed]
"""
from __future__ import annotations

import datetime as dt
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from warroom.factory import MACHINES, TZ                      # noqa: E402
from warroom.sim import physics as P                          # noqa: E402
from warroom.contracts import MachineKind                     # noqa: E402

START = dt.datetime(2026, 10, 22, 12, 30, tzinfo=TZ)
HOURS = 4
EV1_START = dt.datetime(2026, 10, 22, 14, 12, tzinfo=TZ)      # 異常起點（與 gen_signals.EV1_START 相同）

# 與合成資料腳本不同的兩處（為了不讓 repo 偵測器誤報；原值 A03 雜訊 0.6、基準起伏 ±3°C）：
#   1) A03 膠溫雜訊降為 0.25，且上限壓在 119.5°C（否則雜訊會讓讀值短暫跨過 120°C 而觸發安全核心停機）
#   2) A03 基準起伏降為 ±1°C（±3°C 的慢速起伏在事件前就會被偵測器當成「溫度下降」異常）
SINE_AMP_C = 1.0
A03_TEMP_CAP_C = 119.5

# 合成資料腳本的雜訊（σ），覆蓋 repo 額定雜訊
SYN_NOISE = {("A-BIGOPEN", "temp_c"): 0.25, ("A-BIGOPEN", "water_flow_lpm"): 0.7,
             ("A-SMALLOPEN", "temp_c"): 0.5, ("A-SMALLOPEN", "water_flow_lpm"): 0.6, ("A-COOL", "temp_c"): 0.5,
             ("CURE-06", "temp_c"): 0.3, ("CURE-07", "temp_c"): 0.3, ("CURE-08", "temp_c"): 0.3}


def true_values(mid: str, ts: dt.datetime) -> dict[str, float | None]:
    """無雜訊的真值（單位同 repo：°C、kgf/cm²、L/min）。"""
    m = next(x for x in MACHINES if x.id == mid)
    n = P.NOMINALS[mid]
    v = {"temp_c": n.temp_c, "pressure_kgf_cm2": n.pressure, "water_flow_lpm": n.flow_lpm, "steam_kgf_cm2": None}
    if m.kind is MachineKind.CURING_PRESS:
        v["water_flow_lpm"] = None
        v["steam_kgf_cm2"] = P.STEAM_NOMINAL_KGF_CM2
        return v
    s = (ts - EV1_START).total_seconds() / 60.0                 # 異常起算分鐘
    if mid == "A-BIGOPEN":                                      # gen_signals：A03
        base = 95 + SINE_AMP_C * math.sin(ts.timestamp() / 900.0)
        t = base
        f = 38.0
        if s >= 0:
            t = min(96 + 19 * (1 - math.exp(-s / 9.0)) + 0.06 * s, 118.8)
            f = 38 - 14 * (1 - math.exp(-s / 6.0))
        v["temp_c"], v["water_flow_lpm"] = t, f
    elif mid == "A-SMALLOPEN":                                  # A04：+8 分後緩升
        v["temp_c"] = 90 + (3.5 * (1 - math.exp(-(s - 8) / 15.0)) if s >= 8 else 0)
        v["water_flow_lpm"] = 30.0
    elif mid == "A-COOL":                                       # A06：+10 分後緩升
        v["temp_c"] = 45 + (2.0 * (1 - math.exp(-(s - 10) / 20.0)) if s >= 10 else 0)
    return v


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "runtime" / "replay_20261022_4h.jsonl"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    out.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    ids = [m.id for m in MACHINES]
    seqs = dict.fromkeys(ids, 0)
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for sec in range(HOURS * 3600):
            ts = START + dt.timedelta(seconds=sec)
            stamp = ts.isoformat()
            for mid in ids:
                tv = true_values(mid, ts)
                rec = {"ts": stamp, "machine_id": mid, "seq": seqs[mid]}
                seqs[mid] += 1
                for ch, val in tv.items():
                    if val is None:
                        continue
                    sd = SYN_NOISE.get((mid, ch))
                    if sd is None:
                        pressv = next(x for x in MACHINES if x.id == mid).kind is MachineKind.CURING_PRESS
                        sd = P.PRESS_TEMP_NOISE if (ch == "temp_c" and pressv) else P.NOISE[ch]
                    x = val + rng.gauss(0, sd)
                    if mid == "A-BIGOPEN" and ch == "temp_c":
                        x = min(x, A03_TEMP_CAP_C)
                    if ch in ("water_flow_lpm", "steam_kgf_cm2"):
                        x = max(0.0, x)
                    rec[ch] = round(x, 3)
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                n += 1
    print(f"wrote {out}: {n} readings, {len(ids)} machines, {HOURS} h, seed={seed}")


if __name__ == "__main__":
    main()
