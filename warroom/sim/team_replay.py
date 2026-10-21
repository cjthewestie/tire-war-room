"""Replay the team dataset's sensor curves through the plant (page 5: demo = data replay + fault injection).

`TeamReplayFactory` is the normal `Factory` with one difference: in the NORMAL scenario, the
machines of the chosen dataset window(s) read the dataset's recorded values instead of the
simulated ones. Every other machine, the cure cycles, orders and shifts stay simulated, so the
detector, safety core, state machine and dashboard run exactly as for a scripted scenario.

Modes (Settings.replay / env WARROOM_REPLAY, CLI --replay):
  team_ev1   EV-1: A03 大開煉機 112 °C rising, cooling water low (dataset 2026-10-22 14:09–15:00)
             -> A-BIGOPEN (+ A-SMALLOPEN, A-COOL)
  team_ev2   EV-2: V07 硫化機 128 -> 122 -> 119.5 °C, V06/V08 normal (2026-10-28 02:11–03:00)
             -> CURE-07 (+ CURE-06, CURE-08 on the same steam header H2)
  team_both  both windows from the same start (two concurrent events)

Time shift: the window's first second lands on the scenario start. The start keeps the NORMAL
scenario's date (2026-10-03, the page-11 day, so orders and shift gaps are the page-11 ones) and
takes the time of day of the (first) window, so a night event plays at night: team_ev2 starts
2026-10-03 02:11:00, team_ev1 14:09:00, team_both 02:11:00.

Path: `team_dataset.write_replay_jsonl` writes the shifted window to `cache_dir/<window>.jsonl`
and `ReplaySource` (warroom/sim/replay.py) plays it back tick by tick. The 30 min before the
start come from the dataset too (sparklines / read_sensors history). After a window ends, the
plant's own physics takes the machine over from the last replayed value.

Other scenarios (press7_temp_drop, open_mill_overheat, ...) are untouched: the overlay is armed
only in NORMAL, so the dashboard's scenario buttons still play the scripted page-11 / page-2 runs.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from warroom.clock import SimClock
from warroom.contracts import Scenario, ScenarioName, SensorReading
from warroom.sim.factory_sim import Factory
from warroom.sim.replay import ReplaySource
from warroom.sim.scenarios import get_scenario

REPLAY_MODES: dict[str, tuple[str, ...]] = {
    "team_ev1": ("ev1",),
    "team_ev2": ("ev2",),
    "team_both": ("ev2", "ev1"),
}
_FIELDS = ("temp_c", "water_flow_lpm", "steam_kgf_cm2")


def replay_start(mode: str, scenario: ScenarioName = ScenarioName.NORMAL) -> datetime:
    """Scenario date + the first window's time of day (see module doc)."""
    from warroom.data.team_dataset import WINDOWS
    first = WINDOWS[REPLAY_MODES[mode][0]].start
    day = get_scenario(scenario).start_at
    return day.replace(hour=first.hour, minute=first.minute, second=first.second, microsecond=0)


class TeamReplayFactory(Factory):
    """Factory whose NORMAL scenario plays team dataset windows on their mapped machines."""

    def __init__(self, mode: str, clock: SimClock | None = None, seed: int = 7,
                 scenario: ScenarioName = ScenarioName.NORMAL, backfill_min: float = 30.0,
                 cache_dir: Path | None = None, dataset_path: Path | None = None):
        if mode not in REPLAY_MODES:
            raise ValueError(f"unknown replay mode {mode!r}; one of {sorted(REPLAY_MODES)}")
        self.replay_mode = mode
        self.replay_windows = REPLAY_MODES[mode]
        self._cache_dir = Path(cache_dir) if cache_dir else None
        self._dataset_path = dataset_path
        self._replay: dict[datetime, list[SensorReading]] = {}
        self._replay_times: list[datetime] = []
        self._replay_pos = 0
        self.replay_files: dict[str, Path] = {}
        self.replay_shift: dict[str, timedelta] = {}
        super().__init__(clock=clock, seed=seed, scenario=scenario, backfill_min=backfill_min)

    # ------------------------------------------------------------------ scenario
    def load_scenario(self, name: ScenarioName, start_at: datetime | None = None) -> Scenario:
        name = ScenarioName(name)
        armed = name is ScenarioName.NORMAL
        if armed and start_at is None:
            start_at = replay_start(self.replay_mode, name)
        sc = super().load_scenario(name, start_at=start_at)
        self._replay, self._replay_times, self._replay_pos = {}, [], 0
        if armed:
            self._arm(sc.start_at)
        return sc

    def _plan_anchor(self, sc: Scenario) -> datetime:
        """Armed replay plays the plan as of the (first) window's start; other scenarios use the plain mapping."""
        if sc.name is ScenarioName.NORMAL and self.replay_windows:
            from warroom.data.team_dataset import WINDOWS
            return WINDOWS[self.replay_windows[0]].start
        return super()._plan_anchor(sc)

    def _arm(self, t0: datetime) -> None:
        import tempfile

        from warroom.data.team_dataset import WINDOWS, write_replay_jsonl
        cache = self._cache_dir or Path(tempfile.gettempdir()) / "warroom_team_replay"
        lead = self.backfill_min * 60.0
        by_ts: dict[datetime, dict[str, SensorReading]] = {}
        for w in self.replay_windows:
            out = cache / f"{w}.jsonl"
            write_replay_jsonl(w, out, start_at=t0, root=self._dataset_path, lead_s=lead)
            self.replay_files[w] = out
            self.replay_shift[w] = t0 - WINDOWS[w].start
            for tick in ReplaySource(out):
                for r in tick:
                    by_ts.setdefault(r.ts, {})[r.machine_id] = r
        self._replay = {ts: list(v.values()) for ts, v in by_ts.items()}
        self._replay_times = sorted(self._replay)
        self._backfill_from_replay(t0)
        # readings at or before t0 are history; playback starts with the first second after t0
        while self._replay_pos < len(self._replay_times) and self._replay_times[self._replay_pos] <= t0:
            self._replay_pos += 1

    def _backfill_from_replay(self, t0: datetime) -> None:
        """Swap the quiet backfill rows of replayed machines for the dataset's own history."""
        for mid, m in self._m.items():
            rows = list(m.hist)
            changed = False
            for k, row in enumerate(rows):
                ts = row[0]
                if ts > t0:
                    continue
                hit = next((r for r in self._replay.get(ts, ()) if r.machine_id == mid), None)
                if hit is None:
                    continue
                rows[k] = self._merge(row, hit)
                changed = True
            if changed:
                m.hist.clear()
                m.hist.extend(rows)
                self._set_state(m, rows[-1])

    # ------------------------------------------------------------------ step
    def step(self, dt_s: float = 1.0) -> list[SensorReading]:
        out = super().step(dt_s)
        if not self._replay_times:
            return out
        now = self._t
        latest: dict[str, SensorReading] = {}
        while self._replay_pos < len(self._replay_times) and self._replay_times[self._replay_pos] <= now:
            for r in self._replay[self._replay_times[self._replay_pos]]:
                latest[r.machine_id] = r          # a long step keeps the newest sample per machine
            self._replay_pos += 1
        if not latest:
            return out
        for i, r in enumerate(out):
            hit = latest.get(r.machine_id)
            if hit is None:
                continue
            m = self._m[r.machine_id]
            row = self._merge(m.hist[-1], hit)
            m.hist[-1] = row
            self._set_state(m, row)
            new = self._to_reading(r.machine_id, row)
            m.last = new
            out[i] = new
        return out

    @staticmethod
    def _merge(row, hit: SensorReading):
        ts, temp, pres, flow, steam, seq = row
        return (ts,
                hit.temp_c if hit.temp_c is not None else temp,
                pres,
                hit.water_flow_lpm if hit.water_flow_lpm is not None else flow,
                hit.steam_kgf_cm2 if hit.steam_kgf_cm2 is not None else steam,
                seq)

    @staticmethod
    def _set_state(m, row) -> None:
        """The plant state follows the replayed value, so physics continues from it afterwards."""
        _, temp, _, flow, steam, _ = row
        if temp is not None:
            m.temp = temp
        if flow is not None and m.flow is not None:
            m.flow = flow
        if steam is not None and m.steam is not None:
            m.steam = steam

    @property
    def replay_active(self) -> bool:
        return self._replay_pos < len(self._replay_times)

    def replayed_machines(self) -> set[str]:
        """The machines the dataset window still overwrites: a fault injected on them never shows in their readings
        (模擬事件 picks another one)."""
        return {r.machine_id for ts in self._replay_times[self._replay_pos:] for r in self._replay[ts]}
