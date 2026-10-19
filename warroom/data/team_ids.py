"""Team dataset V2 equipment codes -> our machine ids (warroom/factory.py MACHINES).

The team's official dataset (data/team_dataset_v2/, CJ 2026-10-20: 「這份資料是隊友做的，當作團隊正式資料集」)
names the same 28 machines with its own codes. This is the ONE place that translation lives;
the factory-world sim (branch feat/11-factory-world) uses the same dict and function.

Mapping rule: same line, same process, same order inside the line (dataset 3_機台清單 sheet vs
factory.MACHINES). V-numbers keep their press number.

Known difference, kept on purpose: the dataset makes V08–V11 the 4 medium/large presses and
V01–V07 small-only (its V07 event tire S-027 is a small tire); our factory.py fixes CURE-07..10
as M/L (page 11 has press 7 curing a medium tire). The id maps 1:1 by number anyway (V07 ->
CURE-07), so the page-11 press is the dataset's EV-2 press. Read sizes from our factory.py,
not from the dataset, when they matter.
"""
from __future__ import annotations

from warroom.factory import MACHINE_IDS

TEAM_MACHINE_IDS: dict[str, str] = {
    # A 主線 胎體／胎面膠
    "A01": "A-MIX",        # 密煉機 混煉
    "A02": "A-OPEN",       # 開煉機 壓片
    "A03": "A-BIGOPEN",    # 大開煉機 預熱            (EV-1, 112 °C)
    "A04": "A-SMALLOPEN",  # 小開煉機 保溫儲料
    "A05": "A-CAL",        # 壓延機 押出
    "A06": "A-COOL",       # 冷卻機 冷卻
    "A07": "A-WIND",       # 捲取設備 捲取
    # B 支線 胎側膠
    "B01": "B-MIX",        # 密煉機 混煉
    "B02": "B-OPEN",       # 開煉機 壓片
    "B03": "B-FORM",       # 油壓機 成形
    "B04": "B-ASSY",       # 油壓機 組裝
    # C 支線 鋼絲圈
    "C01": "C-PAYOFF",     # 放線架 拉線
    "C02": "C-EXTR",       # 押出機 押出包膠
    "C03": "C-WIND",       # 纏繞機 拉圈
    "C04": "C-WRAP",       # 氣囊 包布
    # D 支線 尼龍布
    "D01": "D-CUT",        # 裁切機 裁切
    # 貼合
    "L01": "BOND-01",      # 油壓機 貼合
    # 硫化 11 台
    **{f"V{i:02d}": f"CURE-{i:02d}" for i in range(1, 12)},   # V06/V07/V08 = steam header H2 (EV-2)
}


def to_machine_id(team_id: str) -> str | None:
    """'A03' -> 'A-BIGOPEN'; also accepts a sensor id ('V07.T') or the dataset's equipment label
    ('V07 硫化機'). None for anything that is not one of the 28 machines (輸送帶, 人工, 靜置)."""
    code = team_id.strip().split(" ", 1)[0].split(".", 1)[0].upper()
    return TEAM_MACHINE_IDS.get(code)


def to_team_id(machine_id: str) -> str | None:
    """Reverse lookup: 'CURE-07' -> 'V07'."""
    return _REVERSE.get(machine_id)


_REVERSE = {v: k for k, v in TEAM_MACHINE_IDS.items()}
UNMAPPED: frozenset[str] = frozenset(MACHINE_IDS) - frozenset(TEAM_MACHINE_IDS.values())   # tests: must be empty
