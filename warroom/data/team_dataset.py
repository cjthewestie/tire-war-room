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
    # issue #17 plant scenarios play working days of the roster (Sat/Sun are rest days there): the shift start on
    # the EV-2 Tuesday (S11 貼合組 is on sick leave that night), the sick call on a Monday, the rush order on a
    # Wednesday
    ScenarioName.SHIFT_START: date(2026, 10, 27),
    ScenarioName.STAFFING: date(2026, 10, 26),
    ScenarioName.RUSH_ORDER: date(2026, 10, 21),
    ScenarioName.DAY_LOOP: date(2026, 10, 21),        # issue #36: the sim date IS the dataset's quiet day
    ScenarioName.MORNING_RUSH: date(2026, 10, 23),    # issue #104: the sim date IS the dataset day (a full 早班)
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


# =====================================================================================================
# Sensor replay, SOP chunks, past-incident store (issue #12). Same files, same id map; nothing here writes.
#
# Replay channel mapping (dataset sensor -> contracts.SensorReading field); only these are replayed, the
# rest (AMP, RPM, steam flow F) have no channel and stay in the CSV:
#   <A03|A04>.T -> temp_c, <A03|A04>.FLOW -> water_flow_lpm, A06.T -> temp_c,
#   <V06|V07|V08>.T -> temp_c, <V..>.P -> steam_kgf_cm2 (re-scaled, see steam_from_bar_abs).
# pressure_kgf_cm2 (hydraulic) is not in the dataset; the sim keeps its own value.
# =====================================================================================================
SENSOR_FILE = "sensor_readings.csv.gz"     # sensor_readings.csv (13 MB) committed gzip-compressed
SOURCE_TAG = "team_dataset"
_FIELD = {"T": "temp_c", "FLOW": "water_flow_lpm", "P": "steam_kgf_cm2"}

# Steam: the dataset reports platen steam as bar(abs), 2.5 at the 128 °C setpoint and 1.8 at its 119.5 °C
# floor (EV-2 steady states). Our press model (warroom/sim/physics.py) is a linearised gauge scale, 5.0
# kgf/cm² at 128 °C and 4.3 °C per kgf/cm². To keep the detector's steam-vs-temp redundancy check meaningful,
# the dataset's own P<->T relation is mapped onto that scale linearly: 2.5 -> 5.0, 1.8 -> steam_for_temp(119.5).
DATASET_P_NOMINAL_BAR_ABS = 2.5
DATASET_P_FLOOR_BAR_ABS = 1.8
DATASET_T_FLOOR_C = 119.5


def steam_from_bar_abs(p_bar_abs: float) -> float:
    """Dataset steam pressure (bar abs) -> our model's steam_kgf_cm2 scale (see the note above)."""
    from warroom.sim.physics import STEAM_NOMINAL_KGF_CM2, steam_for_temp
    gain = (STEAM_NOMINAL_KGF_CM2 - steam_for_temp(DATASET_T_FLOOR_C)) / (
        DATASET_P_NOMINAL_BAR_ABS - DATASET_P_FLOOR_BAR_ABS)
    return round(STEAM_NOMINAL_KGF_CM2 + (p_bar_abs - DATASET_P_NOMINAL_BAR_ABS) * gain, 3)


@dataclass(frozen=True)
class ReplayWindow:
    """One 1 Hz window of sensor_readings. `start` is where playback begins (3 min before the fault, so the
    detector learns its baseline); `fault_at` is events.csv fault_start."""
    name: str
    event_id: str
    start: datetime
    end: datetime
    fault_at: datetime
    sensors: tuple[str, ...]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


HISTORY_LEAD_S = 1800.0   # the 1 Hz data starts 39/41 min before playback: enough for a 30-min history

# The dataset is 1 Hz only in these two windows (10 s elsewhere).
WINDOWS: dict[str, ReplayWindow] = {
    "ev1": ReplayWindow("ev1", "EV-1", _ts("2026-10-22 14:09:00"), _ts("2026-10-22 15:00:00"),
                        _ts("2026-10-22 14:12:00"), ("A03.T", "A03.FLOW", "A04.T", "A04.FLOW", "A06.T")),
    "ev2": ReplayWindow("ev2", "EV-2", _ts("2026-10-28 02:11:00"), _ts("2026-10-28 03:00:00"),
                        _ts("2026-10-28 02:14:00"), ("V06.T", "V06.P", "V07.T", "V07.P", "V08.T", "V08.P")),
}


def load_table(name: str, root: Path | None = None) -> list[dict[str, str]]:
    """Any CSV of the dataset ('orders', 'tire_unit', 'audit_cure', ...) as dicts (BOM stripped)."""
    return _rows(f"{name}.csv", Path(root) if root else DATASET_DIR)


def iter_sensor_rows(root: Path | None = None):
    """Rows of sensor_readings.csv.gz: ts, sensor_id, value, quality, scenario."""
    import gzip
    import io
    with gzip.open((Path(root) if root else DATASET_DIR) / SENSOR_FILE, "rb") as raw, \
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


@lru_cache(maxsize=4)
def _window_values(name: str, root_key: str) -> dict[datetime, dict[str, dict[str, float]]]:
    """ts -> machine_id -> {field: value} for one window (cached; the gz file is read once per window)."""
    w = WINDOWS[name]
    lo = (w.start - timedelta(seconds=HISTORY_LEAD_S)).strftime("%Y-%m-%d %H:%M:%S")
    hi = w.end.strftime("%Y-%m-%d %H:%M:%S")
    out: dict[datetime, dict[str, dict[str, float]]] = {}
    for row in iter_sensor_rows(Path(root_key) if root_key else None):
        if row["sensor_id"] not in w.sensors or not lo <= row["ts"] <= hi:
            continue
        field = _FIELD[row["sensor_id"].split(".", 1)[1]]
        v = float(row["value"])
        if field == "steam_kgf_cm2":
            v = steam_from_bar_abs(v)
        out.setdefault(_ts(row["ts"]), {}).setdefault(machine_id(row["sensor_id"]), {})[field] = v
    return out


def replay_window(name: str, start_at: datetime | None = None, root: Path | None = None, lead_s: float = 0.0):
    """The window as ticks (one list per second) of partial SensorReadings on our machine ids. start_at
    shifts the window so its `start` lands on start_at (the sim clock); None keeps the dataset's own times.
    lead_s (<= HISTORY_LEAD_S) also returns that many seconds before `start`. Unreplayed channels are None."""
    from warroom.contracts import SensorReading
    w = WINDOWS[name]
    shift = (start_at - w.start) if start_at is not None else timedelta(0)
    first = w.start - timedelta(seconds=min(max(lead_s, 0.0), HISTORY_LEAD_S))
    vals = _window_values(name, str(root) if root else "")
    return [[SensorReading(ts=ts + shift, machine_id=mid, **fields) for mid, fields in sorted(vals[ts].items())]
            for ts in sorted(t for t in vals if t >= first)]


# Issue #36 一日循環: the one full quiet day in sensor_readings (10 s steps, scenario 0). 10-22 holds EV-1 and
# 10-28 EV-2; the file has no rows for 10-23 .. 10-27.
QUIET_DAY = date(2026, 10, 21)


@lru_cache(maxsize=2)
def day_ticks(day: date = QUIET_DAY, root_key: str = "") -> tuple[tuple[datetime, tuple[tuple[str, tuple[tuple[str, float], ...]], ...]], ...]:
    """One whole dataset day as (ts, ((machine_id, ((field, value), ...)), ...)) in time order, on our machine ids
    and our units (steam re-scaled). Only the channels we model (temp, cooling water, steam). Cached, immutable."""
    key = day.isoformat()
    out: dict[datetime, dict[str, dict[str, float]]] = {}
    for row in iter_sensor_rows(Path(root_key) if root_key else None):
        if not row["ts"].startswith(key) or not row["value"]:
            continue
        field = _FIELD.get(row["sensor_id"].split(".", 1)[1])
        mid = machine_id(row["sensor_id"])
        if field is None or mid is None:
            continue
        v = float(row["value"])
        if field == "steam_kgf_cm2":
            v = steam_from_bar_abs(v)
        out.setdefault(_ts(row["ts"]), {}).setdefault(mid, {})[field] = v
    return tuple((ts, tuple((m, tuple(sorted(f.items()))) for m, f in sorted(out[ts].items()))) for ts in sorted(out))


def write_replay_jsonl(name: str, out: Path, start_at: datetime | None = None, root: Path | None = None,
                       lead_s: float = 0.0) -> int:
    """Write the window as JSONL in the shape warroom.sim.record_jsonl writes, so warroom.sim.ReplaySource
    plays it back tick by tick. Overwrites out. Returns the number of readings written."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for tick in replay_window(name, start_at, root, lead_s):
            for r in tick:
                fh.write(r.model_dump_json(exclude_none=True))
                fh.write("\n")
                n += 1
    return n


@dataclass(frozen=True)
class TeamSopChunk:
    chunk_id: str
    sop_id: str
    title: str
    section: str
    text: str
    equipment: tuple[str, ...]
    keywords: tuple[str, ...]


def load_sop_chunks(root: Path | None = None) -> list[TeamSopChunk]:
    """The 60 chunks of sop_chunks.jsonl (12 SOPs), in file order; [] when the file is absent."""
    import json
    p = (Path(root) if root else DATASET_DIR) / "sop_chunks.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            out.append(TeamSopChunk(d["chunk_id"], d["sop_id"], d["title"], d["section"], d["text"],
                                    tuple(d.get("equipment") or ()), tuple(d.get("keywords") or ())))
    return out


class IncidentStore:
    """The 8 past incidents (history_incident.csv, loaded by `load()` above), read-only.

    Extension point for agent memory: a future read-only tool (e.g. `past_incidents`) wraps for_machine() /
    search() and is registered in warroom/tools + ASK_TOOLS. Nothing in the agent calls this today."""

    def __init__(self, root: Path | None = None):
        ds = load(Path(root) if root else DATASET_DIR)
        self._rows: tuple[Incident, ...] = ds.incidents if ds else ()

    def all(self) -> list[Incident]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, incident_id: str) -> Incident | None:
        return next((i for i in self._rows if i.incident_id == incident_id), None)

    def for_machine(self, mid: str) -> list[Incident]:
        """Past incidents on one of our machines ('CURE-09'), newest first."""
        return sorted((i for i in self._rows if i.machine_id == mid), key=lambda i: i.date, reverse=True)

    def search(self, text: str) -> list[Incident]:
        """Incidents whose equipment/symptom/action/outcome contains any whitespace-separated term."""
        terms = [t for t in text.split() if t]
        return [i for i in self._rows if any(t in f"{i.equipment} {i.symptom} {i.action} {i.outcome}" for t in terms)]
