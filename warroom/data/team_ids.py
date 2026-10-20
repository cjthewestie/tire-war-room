"""One explicit mapping from the team dataset's ids to ours (warroom/factory.py, contracts.TeamId).

Standalone on purpose: the sensor-replay work (branch feat/12-team-dataset) needs the same table; keep one copy
when the two branches meet. Dataset ids come from data/team_dataset/*.csv and the original tire_step.csv /
sensor_meta.csv (equipment "A03 大開煉機" -> its leading id "A03").
"""
from __future__ import annotations

from warroom.contracts import TeamId
from warroom.factory import MACHINE_IDS

# dataset equipment id -> warroom machine id (all 28; tire_step.csv lists exactly these)
MACHINE_ID: dict[str, str] = {
    "A01": "A-MIX", "A02": "A-OPEN", "A03": "A-BIGOPEN", "A04": "A-SMALLOPEN", "A05": "A-CAL", "A06": "A-COOL",
    "A07": "A-WIND",
    "B01": "B-MIX", "B02": "B-OPEN", "B03": "B-FORM", "B04": "B-ASSY",
    "C01": "C-PAYOFF", "C02": "C-EXTR", "C03": "C-WIND", "C04": "C-WRAP",
    "D01": "D-CUT",
    "L01": "BOND-01",
    **{f"V{i:02d}": f"CURE-{i:02d}" for i in range(1, 12)},
}
DATASET_MACHINE_ID: dict[str, str] = {v: k for k, v in MACHINE_ID.items()}

# dataset work group -> closest warroom team, and the machines that group works (from tire_step.csv)
WORK_GROUP_TEAM: dict[str, TeamId] = {
    "打膠組": TeamId.BODY,         # 混煉 A01 + B01 (compound for the tyre body)
    "成形組": TeamId.BIGOPEN,      # A02–A07 incl. A03 大開煉機
    "胎邊組": TeamId.SIDE,         # B02–B04
    "鋼絲圈組": TeamId.BEAD_NYLON,  # C01–C04
    "尼龍布組": TeamId.BEAD_NYLON,  # D01
    "貼合組": TeamId.BOND,         # L01 貼合 + 入模 on the presses
    "加硫組": TeamId.CURING,       # 出模 on the presses
    "修邊組": TeamId.QC,           # 整修 (manual finishing): sits with final inspection in our team set
}
WORK_GROUP_MACHINES: dict[str, tuple[str, ...]] = {
    "打膠組": ("A-MIX", "B-MIX"),
    "成形組": ("A-OPEN", "A-BIGOPEN", "A-SMALLOPEN", "A-CAL", "A-COOL", "A-WIND"),
    "胎邊組": ("B-OPEN", "B-FORM", "B-ASSY"),
    "鋼絲圈組": ("C-PAYOFF", "C-EXTR", "C-WIND", "C-WRAP"),
    "尼龍布組": ("D-CUT",),
    "貼合組": ("BOND-01",),
    "加硫組": tuple(f"CURE-{i:02d}" for i in range(1, 12)),
    "修邊組": (),                  # manual finishing area by the presses
}


def machine_id(dataset_equipment: str) -> str | None:
    """'A03 大開煉機', 'A03' or a sensor id 'V07.T' -> our id ('A-BIGOPEN', 'CURE-07'); None for
    non-machines ('輸送帶', '人工', '靜置')."""
    if not dataset_equipment or not dataset_equipment.strip():
        return None
    return MACHINE_ID.get(dataset_equipment.split()[0].split(".", 1)[0].upper())


# Matches the dataset: it makes V08–V11 the medium/large presses and V01–V07
# small-only (its EV-2 tire S-027 on V07 is small); factory.py now matches: CURE-08..11 are M/L (issue #20).
# The ids map 1:1 by number, so V07 (EV-2) is CURE-07.
UNMAPPED: frozenset[str] = frozenset(MACHINE_IDS) - frozenset(MACHINE_ID.values())   # tests: must be empty
