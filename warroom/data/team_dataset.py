"""Read-only access to the team's official dataset, data/team_dataset_v2/ (synthetic, 25 PASS checks).

CJ, 2026-10-20: 「這份資料是隊友做的，當作團隊正式資料集」 — use it as replay + reference data.

What lives here (no agent tool, no writes, nothing that can move a machine):
- `load_table(name)`         any CSV of the dataset as a list of dicts.
- `replay_window(...)`       the 1 Hz sensor window of EV-1 / EV-2 as `SensorReading` ticks on OUR
                             machine ids and channels, time-shifted onto any sim start; and
                             `write_replay_jsonl(...)` writes it for `warroom.sim.ReplaySource`.
- `load_sop_chunks()`        the 60 SOP chunks (12 SOPs) for the knowledge base.
- `IncidentStore`            the 8 past incidents (history_incident.csv), read-only, for later
                             agent memory. Extension point: a future read-only tool (e.g.
                             `past_incidents`) wraps `IncidentStore.for_machine()` / `search()`;
                             it is NOT registered anywhere yet.

Channel mapping for replay (dataset sensor -> SensorReading field). Only these are replayed; the
rest (AMP, RPM, steam flow F) have no channel in contracts.SensorReading and stay in the CSV:
  <A03|A04>.T -> temp_c, <A03|A04>.FLOW -> water_flow_lpm, A06.T -> temp_c,
  <V06|V07|V08>.T -> temp_c, <V..>.P -> steam_kgf_cm2 (re-scaled, see `steam_from_bar_abs`).
`pressure_kgf_cm2` (hydraulic) is not in the dataset; the sim keeps its own value.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from warroom.contracts import SensorReading
from warroom.data.team_ids import to_machine_id
from warroom.factory import TZ
from warroom.sim.physics import STEAM_NOMINAL_KGF_CM2, steam_for_temp

ROOT = Path(__file__).resolve().parent.parent.parent
DATASET_DIR = ROOT / "data" / "team_dataset_v2"
SENSOR_FILE = "sensor_readings.csv.gz"
SOURCE_TAG = "team_dataset"

_FIELD = {"T": "temp_c", "FLOW": "water_flow_lpm", "P": "steam_kgf_cm2"}

# Steam: the dataset reports platen steam as bar(abs), 2.5 at the 128 °C setpoint and 1.8 at its
# 119.5 °C floor (EV-2 steady states). Our press model (warroom/sim/physics.py) is a linearised
# gauge scale, 5.0 kgf/cm² at 128 °C and 4.3 °C per kgf/cm². To keep the detector's steam-vs-temp
# redundancy check meaningful we map the dataset's own P<->T relation onto that scale linearly:
# 2.5 bar(abs) -> 5.0, 1.8 bar(abs) -> steam_for_temp(119.5). Calibration from the data, not a guess.
DATASET_P_NOMINAL_BAR_ABS = 2.5
DATASET_P_FLOOR_BAR_ABS = 1.8
DATASET_T_FLOOR_C = 119.5
_STEAM_GAIN = (STEAM_NOMINAL_KGF_CM2 - steam_for_temp(DATASET_T_FLOOR_C)) / (
    DATASET_P_NOMINAL_BAR_ABS - DATASET_P_FLOOR_BAR_ABS)


def steam_from_bar_abs(p_bar_abs: float) -> float:
    """Dataset steam pressure (bar abs) -> our model's steam_kgf_cm2 scale (see the note above)."""
    return round(STEAM_NOMINAL_KGF_CM2 + (p_bar_abs - DATASET_P_NOMINAL_BAR_ABS) * _STEAM_GAIN, 3)


@dataclass(frozen=True)
class ReplayWindow:
    """One 1 Hz window of sensor_readings.csv. `start` is where playback begins (a few minutes
    before the fault so the detector learns its baseline); `fault_at` is events.csv fault_start."""
    name: str
    event_id: str
    start: datetime
    end: datetime
    fault_at: datetime
    sensors: tuple[str, ...]


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


HISTORY_LEAD_S = 1800.0   # the 1 Hz data starts 39/41 min before playback: enough for a 30-min history

# The dataset is 1 Hz only in these two windows (10 s elsewhere). Playback starts 3 min before
# the fault: our detector needs ~10 samples for a baseline and the p.11 script also gives 75 s.
WINDOWS: dict[str, ReplayWindow] = {
    "ev1": ReplayWindow("ev1", "EV-1", _dt("2026-10-22 14:09:00"), _dt("2026-10-22 15:00:00"),
                        _dt("2026-10-22 14:12:00"),
                        ("A03.T", "A03.FLOW", "A04.T", "A04.FLOW", "A06.T")),
    "ev2": ReplayWindow("ev2", "EV-2", _dt("2026-10-28 02:11:00"), _dt("2026-10-28 03:00:00"),
                        _dt("2026-10-28 02:14:00"),
                        ("V06.T", "V06.P", "V07.T", "V07.P", "V08.T", "V08.P")),
}


def dataset_dir(path: Path | None = None) -> Path:
    return Path(path) if path else DATASET_DIR


def load_table(name: str, path: Path | None = None) -> list[dict[str, str]]:
    """One CSV of the dataset ('orders', 'history_incident', ...) as dicts (BOM stripped)."""
    p = dataset_dir(path) / f"{name}.csv"
    with p.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def iter_sensor_rows(path: Path | None = None) -> Iterator[dict[str, str]]:
    """Rows of sensor_readings.csv.gz: ts, sensor_id, value, quality, scenario."""
    with gzip.open(dataset_dir(path) / SENSOR_FILE, "rb") as raw, \
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as fh:
        yield from csv.DictReader(fh)


@lru_cache(maxsize=4)
def _window_values(name: str, path_key: str) -> dict[datetime, dict[str, dict[str, float]]]:
    """ts -> machine_id -> {field: value} for one window (cached; the gz file is read once)."""
    w = WINDOWS[name]
    lo = (w.start - timedelta(seconds=HISTORY_LEAD_S)).strftime("%Y-%m-%d %H:%M:%S")
    hi = w.end.strftime("%Y-%m-%d %H:%M:%S")
    out: dict[datetime, dict[str, dict[str, float]]] = {}
    for row in iter_sensor_rows(Path(path_key) if path_key else None):
        if row["sensor_id"] not in w.sensors or not lo <= row["ts"] <= hi:
            continue
        mid = to_machine_id(row["sensor_id"])
        field = _FIELD[row["sensor_id"].split(".", 1)[1]]
        v = float(row["value"])
        if field == "steam_kgf_cm2":
            v = steam_from_bar_abs(v)
        out.setdefault(_dt(row["ts"]), {}).setdefault(mid, {})[field] = v
    return out


def replay_window(name: str, start_at: datetime | None = None, path: Path | None = None,
                  lead_s: float = 0.0) -> list[list[SensorReading]]:
    """The window as ticks (one list per second) of partial SensorReadings on our machine ids.
    start_at shifts the window so its `start` lands on start_at (the sim clock); None keeps the
    dataset's own timestamps. lead_s (<= HISTORY_LEAD_S) also returns that many seconds before
    `start` (history for sparklines). Only replayed channels are set; the others are None."""
    w = WINDOWS[name]
    shift = (start_at - w.start) if start_at is not None else timedelta(0)
    first = w.start - timedelta(seconds=min(max(lead_s, 0.0), HISTORY_LEAD_S))
    vals = _window_values(name, str(path) if path else "")
    ticks: list[list[SensorReading]] = []
    for ts in sorted(t for t in vals if t >= first):
        ticks.append([SensorReading(ts=ts + shift, machine_id=mid, **fields)
                      for mid, fields in sorted(vals[ts].items())])
    return ticks


def write_replay_jsonl(name: str, out: Path, start_at: datetime | None = None, path: Path | None = None,
                       lead_s: float = 0.0) -> int:
    """Write the window as JSONL in the shape warroom.sim.record_jsonl writes, so
    warroom.sim.ReplaySource plays it back tick by tick. Overwrites out. Returns readings written."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out.open("w", encoding="utf-8") as fh:
        for tick in replay_window(name, start_at, path, lead_s):
            for r in tick:
                fh.write(r.model_dump_json(exclude_none=True))
                fh.write("\n")
                n += 1
    return n


# ---------------------------------------------------------------- SOP chunks
@dataclass(frozen=True)
class TeamSopChunk:
    chunk_id: str
    sop_id: str
    title: str
    section: str
    text: str
    equipment: tuple[str, ...]
    keywords: tuple[str, ...]


def load_sop_chunks(path: Path | None = None) -> list[TeamSopChunk]:
    """The 60 chunks of sop_chunks.jsonl, in file order."""
    p = dataset_dir(path) / "sop_chunks.jsonl"
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        out.append(TeamSopChunk(d["chunk_id"], d["sop_id"], d["title"], d["section"], d["text"],
                                tuple(d.get("equipment") or ()), tuple(d.get("keywords") or ())))
    return out


# ---------------------------------------------------------------- past incidents (read-only)
@dataclass(frozen=True)
class Incident:
    incident_id: str
    date: str
    equipment: str          # dataset label, e.g. "V09 硫化機"
    machine_id: str | None  # ours, e.g. "CURE-09"
    symptom: str
    action: str
    downtime_min: int
    outcome: str


class IncidentStore:
    """history_incident.csv, read-only. Built once; no method writes anything.

    Extension point for agent memory: a future read-only tool can wrap for_machine()/search()
    (register it in warroom/tools and ASK_TOOLS); nothing calls this from the agent today."""

    def __init__(self, path: Path | None = None):
        self._rows: tuple[Incident, ...] = tuple(
            Incident(r["incident_id"], r["date"], r["equipment"], to_machine_id(r["equipment"]), r["symptom"],
                     r["action"], int(r["downtime_min"]), r["outcome"])
            for r in load_table("history_incident", path))

    def all(self) -> list[Incident]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, incident_id: str) -> Incident | None:
        return next((i for i in self._rows if i.incident_id == incident_id), None)

    def for_machine(self, machine_id: str) -> list[Incident]:
        """Past incidents on one of our machines ('CURE-09'), newest first."""
        return sorted((i for i in self._rows if i.machine_id == machine_id), key=lambda i: i.date, reverse=True)

    def search(self, text: str) -> list[Incident]:
        """Incidents whose symptom/action/outcome/equipment contains any whitespace-separated term."""
        terms = [t for t in text.split() if t]
        return [i for i in self._rows
                if any(t in f"{i.equipment} {i.symptom} {i.action} {i.outcome}" for t in terms)]
