"""Plan-replay scenarios built from the team dataset (docs/research/edge-agent-harness-2026-11.md, 建議 3b).

data/team_dataset/ is the team's official, synthetic dataset (read data/team_dataset/README.md). Its two events become
two scenarios for the offline agent harness (tests/test_agent.make_sm: FakeSim / FakeRegistry / RuleGate / the real
StateMachine):

  team_ev1  EV-1  A03 大開煉機 膠溫 112 °C rising, cooling water low (2026-10-22 14:26)
  team_ev2  EV-2  V07 硫化機 128 -> 122 -> 119.5 °C (2026-10-28 02:14)

Dataset equipment codes become our machine ids only through warroom.data.team_ids (A03 -> A-BIGOPEN, V07 -> CURE-07).
The event machine's temperature (and cooling-water flow, where the dataset has it) follows the dataset's own 1 Hz
curve, time-shifted so the moment the anomaly is raised lands on the harness clock T0 (2026-10-03 02:14:00); other
machines with a dataset sensor read theirs on the same shift, the rest hold a steady value. The anomaly is rebuilt
from that curve (no detector run): baseline = median of the hour before the fault started; it is raised at the first
moment, from the event time on, that the curve is raise_delta_c off the baseline; an overheat predicts a straight line
from the last 10 min, a drop predicts it stays where it is (page 11's 「預測穩定」).

The same builder is used to record (scripts/record_plans.py, live model) and to replay (tests/test_plan_replay.py), so
the state machine asks the same (phase, cause_code, round) questions both times. Nothing here can move a machine: the
harness tools are fakes, and every write still goes through the Action Gate."""
from __future__ import annotations

import csv
import gzip
import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from tests.agent_fakes import T0
from warroom.contracts import Anomaly, AnomalyKind, Channel, CompoundStage, Prediction, Severity
from warroom.data import team_ids
from warroom.factory import MACHINE_BY_ID, TZ

DATASET = Path(__file__).resolve().parents[2] / "data" / "team_dataset"


@dataclass(frozen=True)
class TeamScenario:
    name: str
    event_id: str                    # events.csv event_id
    kind: AnomalyKind
    about: str
    compound: dict[str, CompoundStage] = field(default_factory=dict)
    raise_delta_c: float = 5.0       # the anomaly is raised once the curve is this far from its baseline
    max_decisions: int = 3           # the scripted supervisor approves at most this many cards (recommended option)


SCENARIOS: dict[str, TeamScenario] = {
    "team_ev1": TeamScenario("team_ev1", "EV-1", AnomalyKind.OVERHEAT,
                             "team dataset EV-1: A03 大開煉機 (A-BIGOPEN) 膠溫 112 °C rising, cooling water low",
                             compound={"A-BIGOPEN": CompoundStage.UNVULCANIZED}),
    "team_ev2": TeamScenario("team_ev2", "EV-2", AnomalyKind.TEMP_DROP,
                             "team dataset EV-2: V07 硫化機 (CURE-07) 128 -> 122 -> 119.5 °C"),
}


def _ts(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M" if len(s.strip()) == 16 else "%Y-%m-%d %H:%M:%S") \
        .replace(tzinfo=TZ)


@lru_cache(maxsize=1)
def events() -> dict[str, dict[str, str]]:
    with (DATASET / "events.csv").open(encoding="utf-8-sig", newline="") as f:
        return {r["event_id"]: r for r in csv.DictReader(f)}


@lru_cache(maxsize=4)
def _curves(day: str) -> dict[str, tuple[tuple[datetime, ...], tuple[float, ...]]]:
    """sensor_id -> (timestamps, values) of the .T (temperature) and .FLOW sensors on one dataset day, good quality."""
    raw: dict[str, list[tuple[datetime, float]]] = {}
    with gzip.open(DATASET / "sensor_readings.csv.gz", "rt", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not r["ts"].startswith(day) or r["quality"] != "good":
                continue
            if r["sensor_id"].endswith((".T", ".FLOW")):
                raw.setdefault(r["sensor_id"], []).append((_ts(r["ts"]), float(r["value"])))
    out = {}
    for sid, rows in raw.items():
        rows.sort()
        out[sid] = (tuple(t for t, _ in rows), tuple(v for _, v in rows))
    return out


def _at(curve, t: datetime) -> float:
    ts, vs = curve
    i = bisect_right(ts, t) - 1
    return vs[max(0, min(i, len(vs) - 1))]


def _sensor_by_machine(curves) -> dict[str, str]:
    """our machine id -> its dataset temperature sensor (through team_ids only)."""
    out = {}
    for sid in curves:
        if sid.endswith(".T"):
            mid = team_ids.machine_id(sid)
            if mid is not None:
                out[mid] = sid
    return out


class TeamWorld:
    """One scenario: the anomaly the harness starts from, and the temperature function FakeSim reads."""

    def __init__(self, scn: TeamScenario):
        self.scn = scn
        row = events()[scn.event_id]
        self.event_row = row
        self.machine_id = team_ids.machine_id(row["equipment"])
        if self.machine_id is None:
            raise ValueError(f"{scn.event_id}: equipment {row['equipment']!r} has no machine in warroom.data.team_ids")
        self.event_ts = _ts(row["time"])
        self.fault_start = _ts(row["fault_start"])
        self.curves = _curves(self.event_ts.strftime("%Y-%m-%d"))
        self.sensors = _sensor_by_machine(self.curves)
        if self.machine_id not in self.sensors:
            raise ValueError(f"{scn.event_id}: no dataset temperature sensor maps to {self.machine_id}")
        ts, vs = self.curves[self.sensors[self.machine_id]]
        before = [v for t, v in zip(ts, vs) if self.fault_start - timedelta(hours=1) <= t < self.fault_start]
        self.baseline = round(statistics.median(before), 1) if before else round(_at((ts, vs), self.fault_start), 1)
        self.raised_at = next((t for t, v in zip(ts, vs) if t >= self.event_ts
                               and abs(v - self.baseline) >= scn.raise_delta_c), None)
        if self.raised_at is None:
            raise ValueError(f"{scn.event_id}: the curve never moves {scn.raise_delta_c} °C off its baseline")
        self.shift = self.raised_at - T0                  # dataset time = harness time + shift

    def temp(self, machine_id: str, now: datetime) -> float:
        sid = self.sensors.get(machine_id)
        if sid is None:
            return 128.0 if machine_id.startswith("CURE-") else 80.0
        return round(_at(self.curves[sid], now + self.shift), 1)

    def flow(self, machine_id: str, now: datetime) -> float | None:
        sid = self.sensors.get(machine_id, "")[:-2] + ".FLOW"
        return round(_at(self.curves[sid], now + self.shift), 1) if sid in self.curves else None

    def anomaly(self) -> Anomaly:
        mid, curve = self.machine_id, self.curves[self.sensors[self.machine_id]]
        value, baseline = round(_at(curve, self.raised_at), 1), self.baseline
        trend = round((value - _at(curve, self.raised_at - timedelta(minutes=10))) / 10.0, 2)
        if self.scn.kind is AnomalyKind.TEMP_DROP:
            trend = 0.0                                   # page 11: a drop is predicted to stay where it is
        predicted = round(value + trend * 30, 1)
        pred = Prediction(machine_id=mid, channel=Channel.TEMP, now_value=value, predicted_value=predicted,
                          band_lo=round(predicted - 1.0, 1), band_hi=round(predicted + 1.0, 1), horizon_s=1800,
                          trend_per_min=trend)
        m = MACHINE_BY_ID[mid]
        if self.scn.kind is AnomalyKind.OVERHEAT:
            msg = f"{m.line} 線{m.name} 膠溫 {value:.0f}°C 持續上升（基準 {baseline:.0f}°C）"
        else:
            msg = f"{m.short_name} 號 {baseline:.0f} → {value:.0f}°C，預測穩定在 {predicted:.0f}±1°C"
        return Anomaly(id=f"AN-{self.scn.event_id}", ts=T0, machine_id=mid, kind=self.scn.kind, channel=Channel.TEMP,
                       value=value, baseline=baseline, prediction=pred, severity=Severity.WARN, message=msg)


def world(name: str) -> TeamWorld:
    return TeamWorld(SCENARIOS[name])


async def run_event(name: str, commander: Any, tmp_path: Path, timeout_s: float = 30.0):
    """Run one team scenario through the offline harness with `commander` (already queue-wrapped or not). The scripted
    supervisor approves each card on its recommended option, at most max_decisions times. Returns
    (event, audit, sim, decisions)."""
    import asyncio

    from tests.test_agent import make_sm
    from warroom.contracts import ApprovalDecision, DecisionKind

    w = world(name)
    clock, audit, sim, _store, _tools, approvals, sm = make_sm(tmp_path, w.temp, commander=commander,
                                                               compound=dict(w.scn.compound))
    plain = sim.machine

    def machine(machine_id):                    # FakeSim fixes the water flow at 40 L/min: read the dataset's instead
        st = plain(machine_id)
        flow = w.flow(machine_id, clock.now())
        if flow is None:
            return st
        return st.model_copy(update={"last_reading": st.last_reading.model_copy(update={"water_flow_lpm": flow})})
    sim.machine = machine
    decisions: list[dict[str, Any]] = []

    async def supervisor():
        while len(decisions) < w.scn.max_decisions:
            while not approvals.pending():
                await asyncio.sleep(0.001)
            req = approvals.pending()[0]
            opt = next((o for o in req.options if o.recommended), req.options[0] if req.options else None)
            approvals.decide(ApprovalDecision(request_id=req.id, decision=DecisionKind.APPROVE,
                                              option_id=opt.id if opt else None))
            decisions.append({"title": req.title, "option": opt.label if opt else None})

    sup = asyncio.create_task(supervisor())
    try:
        ev = await asyncio.wait_for(sm.handle(w.anomaly()), timeout_s)
    finally:
        sup.cancel()
    return ev, audit, sim, decisions
