"""Loader for the team's official synthetic dataset (data/team_dataset/*.csv; CJ, 2026-10-20:
「這份資料是隊友做的，當作團隊正式資料集」). Read only, cached, no pandas.

Dates: the dataset runs 2026-10-20 .. 2027-01-26 with EV-1 (A03 overheat) on 10/22 and EV-2 (V07 press drop) on
10/28. The sim keeps its own demo dates, so a sim date maps to a dataset date by a per-scenario day offset that
puts each scenario on its dataset event (page 11 10/03 <-> EV-2 10/28; page 2 10/22 <-> EV-1 10/22). Scenarios with
no dataset event use the page-11 offset. A mapped date outside the roster is moved by whole weeks into it, so
the weekly pattern (rest days) is kept.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path

from warroom.config import ROOT
from warroom.contracts import ScenarioName
from warroom.data.team_ids import machine_id
from warroom.factory import TZ

DATASET_DIR = ROOT / "data" / "team_dataset"
EV2_DATE = date(2026, 10, 28)       # dataset EV-2: V07 128 -> 122 -> 119.5 °C (page 11)
EV1_DATE = date(2026, 10, 22)       # dataset EV-1: A03 膠溫 112 °C (page 2)
SCENARIO_ANCHOR: dict[ScenarioName, date] = {
    ScenarioName.PRESS7_TEMP_DROP: EV2_DATE,
    ScenarioName.OPEN_MILL_OVERHEAT: EV1_DATE,
}


@dataclass(frozen=True)
class Staff:
    staff_id: str
    work_group: str
    shift: str            # 早 | 晚 (usual)
    can_support: str
    note: str


@dataclass(frozen=True)
class RosterRow:
    staff_id: str
    date: date
    code: str             # 早 晚 加 (on duty) | 例 休 (off) | 病 (sick)
    kind: str             # 正常 加班 例假 休息日 病假
    start: time | None
    end: time | None
    hours: float
    ot_hours: float
    note: str


@dataclass(frozen=True)
class MoldChange:
    machine_id: str       # warroom id
    start: datetime       # dataset time (unshifted, Asia/Taipei)
    end: datetime
    from_mold: str
    to_mold: str


@dataclass(frozen=True)
class Incident:
    incident_id: str
    date: date
    machine_id: str | None
    equipment: str
    symptom: str
    action: str
    downtime_min: int
    outcome: str


@dataclass(frozen=True)
class DailyHistory:
    date: date
    tires_done: int
    flex_util_pct: float
    ot_hours: float
    downtime_min: int


@dataclass(frozen=True)
class TeamDataset:
    staff: tuple[Staff, ...]
    roster: dict[tuple[str, date], RosterRow]
    roster_first: date
    roster_last: date
    mold_changes: tuple[MoldChange, ...]
    incidents: tuple[Incident, ...]
    daily: tuple[DailyHistory, ...]

    def dataset_date(self, sim_date: date, scenario: ScenarioName, scenario_start: date) -> date:
        """The dataset day that plays the role of `sim_date` in this scenario."""
        anchor = SCENARIO_ANCHOR.get(scenario, EV2_DATE)
        offset = anchor - scenario_start if scenario in SCENARIO_ANCHOR else EV2_DATE - date(2026, 10, 3)
        d = sim_date + offset
        while d < self.roster_first:
            d += timedelta(days=7)
        while d > self.roster_last:
            d -= timedelta(days=7)
        return d


def _rows(name: str, root: Path) -> list[dict[str, str]]:
    with open(root / name, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _t(s: str) -> time | None:
    return time.fromisoformat(s) if s else None


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)   # dataset times are factory local


@lru_cache(maxsize=4)
def load(root: Path = DATASET_DIR) -> TeamDataset | None:
    """The dataset, or None when the files are missing (the world then generates its people)."""
    if not (root / "staff.csv").is_file() or not (root / "roster.csv").is_file():
        return None
    staff = tuple(Staff(r["staff_id"], r["work_group"], r["shift"], r.get("can_support") or "", r.get("note") or "")
                  for r in _rows("staff.csv", root))
    roster: dict[tuple[str, date], RosterRow] = {}
    for r in _rows("roster.csv", root):
        d = date.fromisoformat(r["date"])
        roster[(r["staff_id"], d)] = RosterRow(r["staff_id"], d, r["shift_code"], r["shift_type"], _t(r["start"]),
                                               _t(r["end"]), float(r["hours"] or 0), float(r["ot_hours"] or 0),
                                               r.get("note") or "")
    days = [d for _, d in roster]
    molds = tuple(MoldChange(machine_id(r["machine_id"]) or r["machine_id"], _dt(r["start"]), _dt(r["end"]),
                             r["from_mold"], r["to_mold"])
                  for r in (_rows("mold_change.csv", root) if (root / "mold_change.csv").is_file() else []))
    incidents = tuple(Incident(r["incident_id"], date.fromisoformat(r["date"]), machine_id(r["equipment"]),
                               r["equipment"], r["symptom"], r["action"], int(r["downtime_min"] or 0), r["outcome"])
                      for r in (_rows("history_incident.csv", root) if (root / "history_incident.csv").is_file() else []))
    daily = tuple(DailyHistory(date.fromisoformat(r["date"]), int(r["tires_done"]), float(r["flex_util_pct"]),
                               float(r["ot_hours"]), int(r["downtime_min"]))
                  for r in (_rows("history_daily.csv", root) if (root / "history_daily.csv").is_file() else []))
    return TeamDataset(staff=staff, roster=roster, roster_first=min(days), roster_last=max(days), mold_changes=molds,
                       incidents=incidents, daily=daily)
