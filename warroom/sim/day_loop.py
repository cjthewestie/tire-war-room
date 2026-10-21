"""Issue #36 一日循環: one day of live data, played 00:00 -> 24:00 and then again from 00:00, for as long as the
demo runs. CJ, 2026-10-20: 「時間的話是要循環的…我大概只需要一天的模擬資料然後就一直循環這樣子跑實時的資料，可以參考朋友給的
實時 data 資料去模擬」.

The day is the team dataset's 2026-10-21: the only full day in sensor_readings.csv.gz with no scripted event
(10-22 holds EV-1, 10-28 EV-2, and the file has no rows for 10-23 .. 10-27). The six dataset machines (A03 大開煉機,
A04 小開煉機, A06 冷卻機, V06–V08 硫化機) read that day's recorded values (10 s steps); the other 22 machines, the cure
cycles, orders, goods, people and trucks are the normal simulator on the same date, with that day's roster.
The 30 minutes before 00:00 are the day's own last 30 minutes, so the history the dashboard opens with is the
same evening the loop comes back from.

`LoopFactory` is `TeamReplayFactory` with one more overlay: armed only in DAY_LOOP. Every other scenario
(page 11, page 2, the plant scenarios, NORMAL with or without WARROOM_REPLAY) runs through exactly the code it
ran before. The wrap itself (cancel open events, reset the detector, restore the start-of-day plant) is done by
the runtime, which owns those parts: `Runtime._wrap_day`. Here: `day_end`, `snapshot_day()` and `restore_day()`.
"""
from __future__ import annotations

import bisect
import copy
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from warroom.clock import SimClock
from warroom.contracts import Scenario, ScenarioName, SensorReading
from warroom.sim.team_replay import REPLAY_MODES, TeamReplayFactory

DAY_S = 24 * 3600


class LoopFactory(TeamReplayFactory):
    """The plant the runtime builds when no dataset replay mode is set: plain Factory behaviour in every scenario
    except DAY_LOOP, which replays the quiet dataset day on the six dataset machines."""

    def __init__(self, clock: SimClock | None = None, seed: int = 7, scenario: ScenarioName = ScenarioName.NORMAL,
                 backfill_min: float = 30.0, dataset_path: Path | None = None, mode: str | None = None):
        self.loop_mode = mode                    # a WARROOM_REPLAY mode for NORMAL (team_ev1 ...), or None
        self.day0: datetime | None = None
        self.loop_n = 1
        self._day_snap: dict[str, Any] | None = None
        if mode is not None:
            super().__init__(mode, clock=clock, seed=seed, scenario=scenario, backfill_min=backfill_min,
                             dataset_path=dataset_path)
            return
        # no NORMAL replay: TeamReplayFactory's fields, with nothing armed outside DAY_LOOP
        self.replay_mode = ""
        self.replay_windows = ()
        self._cache_dir = None
        self._dataset_path = dataset_path
        self._replay = {}
        self._replay_times = []
        self._replay_pos = 0
        self.replay_files = {}
        self.replay_shift = {}
        super(TeamReplayFactory, self).__init__(clock=clock, seed=seed, scenario=scenario, backfill_min=backfill_min)

    def load_scenario(self, name: ScenarioName, start_at: datetime | None = None) -> Scenario:
        name = ScenarioName(name)
        self.day0, self.loop_n, self._day_snap = None, 1, None
        if name is not ScenarioName.DAY_LOOP:
            if self.loop_mode in REPLAY_MODES:
                return super().load_scenario(name, start_at)
            sc = super(TeamReplayFactory, self).load_scenario(name, start_at=start_at)
            self._replay, self._replay_times, self._replay_pos = {}, [], 0
            return sc
        sc = super(TeamReplayFactory, self).load_scenario(name, start_at=start_at)
        self._arm_day(sc.start_at)
        self.day0 = sc.start_at
        return sc

    def _arm_day(self, t0: datetime) -> None:
        from warroom.data.team_dataset import QUIET_DAY, day_ticks
        ticks = day_ticks(QUIET_DAY, str(self._dataset_path) if self._dataset_path else "")
        shift = t0 - datetime.combine(QUIET_DAY, datetime.min.time(), tzinfo=t0.tzinfo)
        lead = timedelta(seconds=self.backfill_min * 60.0)
        key = (QUIET_DAY, str(self._dataset_path or ""), len(ticks))
        series = _SHAPED.get(key)
        if series is None:                       # the same dataset day every load: shape it once per process
            series = _SHAPED[key] = _shape_day(ticks)
        rep: dict[datetime, list[SensorReading]] = {}
        for i, (ts, _) in enumerate(ticks):
            rows = [SensorReading(ts=ts + shift, machine_id=mid, **{ch: vals[i] for ch, vals in chans.items()})
                    for mid, chans in series.items()]
            rep[ts + shift] = rows
            if ts + shift >= t0 + timedelta(seconds=DAY_S) - lead:      # the evening before, for the history
                rep[ts + shift - timedelta(seconds=DAY_S)] = [r.model_copy(update={"ts": r.ts - timedelta(seconds=DAY_S)})
                                                              for r in rows]
        self._replay = {ts: {r.machine_id: r for r in rows} for ts, rows in rep.items()}   # type: ignore[assignment]
        self._replay_times = sorted(rep)
        self._replay_pos = 0
        self._backfill_day(t0)

    # The dataset is sampled every 10 s; the plant ticks every second. Between samples the replayed machines
    # follow the straight line between them (a sawtooth against the sim's own nominal would look like a fault).
    _CHANNELS = ("temp_c", "water_flow_lpm", "steam_kgf_cm2")

    def _interp(self, ts: datetime) -> dict[str, dict[str, float]]:
        times = self._replay_times
        i = bisect.bisect_right(times, ts)
        if i == 0:
            return {}
        a = times[i - 1]
        b = times[i] if i < len(times) else None
        ra, rb = self._replay[a], self._replay.get(b, {}) if b is not None else {}
        w = (ts - a).total_seconds() / (b - a).total_seconds() if b is not None and b > a else 0.0
        out: dict[str, dict[str, float]] = {}
        for mid, r in ra.items():
            nxt = rb.get(mid)
            f: dict[str, float] = {}
            for ch in self._CHANNELS:
                va = getattr(r, ch)
                if va is None:
                    continue
                vb = getattr(nxt, ch, None) if nxt is not None else None
                f[ch] = round(va + (vb - va) * w, 3) if vb is not None else va
            out[mid] = f
        return out

    def _backfill_day(self, t0: datetime) -> None:
        for mid, m in self._m.items():
            rows = list(m.hist)
            changed = False
            for k, row in enumerate(rows):
                hit = self._interp(row[0]).get(mid)
                if hit:
                    rows[k] = self._merge(row, SensorReading(ts=row[0], machine_id=mid, **hit))
                    changed = True
            if changed:
                m.hist.clear()
                m.hist.extend(rows)
                self._set_state(m, rows[-1])

    def step(self, dt_s: float = 1.0) -> list[SensorReading]:
        if self.day0 is None:
            return super().step(dt_s)
        out = super(TeamReplayFactory, self).step(dt_s)
        vals = self._interp(self._t)
        for i, r in enumerate(out):
            hit = vals.get(r.machine_id)
            if not hit:
                continue
            hit = {ch: round(v + _jitter(self._t, r.machine_id, ch), 3) for ch, v in hit.items()}
            m = self._m[r.machine_id]
            row = self._merge(m.hist[-1], SensorReading(ts=self._t, machine_id=r.machine_id, **hit))
            m.hist[-1] = row
            self._set_state(m, row)
            new = self._to_reading(r.machine_id, row)
            m.last = new
            out[i] = new
        return out

    # ------------------------------------------------------------------ the loop
    @property
    def day_end(self) -> datetime | None:
        return self.day0 + timedelta(seconds=DAY_S) if self.day0 is not None else None

    def _shared(self) -> dict[int, Any]:
        """Objects the snapshot must not copy: the shared clock, the schedule source, the dataset, the replay day."""
        memo: dict[int, Any] = {id(self): self, id(self.clock): self.clock, id(self._replay): self._replay,
                                id(self._replay_times): self._replay_times}
        if self._plan_source is not None:
            memo[id(self._plan_source)] = self._plan_source
        ds = getattr(self.world, "ds", None)
        if ds is not None:
            memo[id(ds)] = ds
        for fn in getattr(self.world, "_listeners", []):
            memo[id(fn)] = fn
        tasks = getattr(self.world, "tasks", None)
        for fn in getattr(tasks, "_listeners", []) or []:
            memo[id(fn)] = fn
        return memo

    def snapshot_day(self) -> None:
        """The start-of-day plant (taken once, at 00:00 of the first loop, after the schedule is attached)."""
        state = {k: v for k, v in self.__dict__.items() if k != "_day_snap"}
        self._day_snap = copy.deepcopy(state, self._shared())

    def restore_day(self) -> int:
        """Back to 00:00 with the start-of-day orders, goods, people, trucks and machines. Returns the new loop number."""
        if self._day_snap is None or self.day0 is None:
            raise RuntimeError("no start-of-day snapshot")
        n = self.loop_n + 1
        snap = self._day_snap
        fresh = copy.deepcopy(snap, self._shared())
        self.__dict__.update(fresh)
        self._day_snap, self.loop_n = snap, n
        self.clock.reset(self.day0)
        self._t = self.day0
        return n


SMOOTH_N = 7          # samples (70 s): the dataset's sample-to-sample noise over 10 s reads as a °C/min trend
SEAM_N = 60           # samples (10 min): the day's last 10 minutes fade into its first sample
_SHAPED: dict[tuple, dict[str, dict[str, list[float]]]] = {}   # read only once built (see _arm_day)
# A03 大開煉機 wanders 91.3–98.7 °C over the recorded day (labelled normal, scenario 0). Our detector, tuned on the
# sim's 1 Hz noise, reads its slowest 6 °C climb as an overheat (dev >= 4 °C from a baseline frozen at onset). The
# loop keeps the day's level and shape but scales a temperature's swing round its daily mean to at most ±2 °C.
MAX_SWING_C = 2.0


def _shape_day(ticks) -> dict[str, dict[str, list[float]]]:
    """machine -> channel -> one value per dataset sample (10 s), smoothed with a centred moving average taken
    round the clock (the day is a loop), and with the last 10 minutes faded into 00:00, so 23:59:50 -> 00:00:00
    is continuous. The level and the slow swings of the day stay as recorded."""
    raw: dict[str, dict[str, list[float]]] = {}
    n = len(ticks)
    for i, (_, machines) in enumerate(ticks):
        for mid, fields in machines:
            for ch, v in fields:
                raw.setdefault(mid, {}).setdefault(ch, [float("nan")] * n)[i] = v
    out: dict[str, dict[str, list[float]]] = {}
    h = SMOOTH_N // 2
    for mid, chans in raw.items():
        for ch, vals in chans.items():
            last = next(v for v in vals if not math.isnan(v))
            filled = []
            for v in vals:                       # hold the last value over a gap
                last = last if math.isnan(v) else v
                filled.append(last)
            sm = [sum(filled[(i + k) % n] for k in range(-h, h + 1)) / SMOOTH_N for i in range(n)]
            if ch == "temp_c":
                mu = sum(sm) / n
                swing = max(abs(v - mu) for v in sm)
                if swing > MAX_SWING_C:          # A03 only: its ±3.7 °C day is halved, see MAX_SWING_C
                    k = MAX_SWING_C / swing
                    sm = [mu + (v - mu) * k for v in sm]
            for i in range(n - SEAM_N, n):
                a = (i - (n - SEAM_N) + 1) / SEAM_N
                sm[i] = (1 - a) * sm[i] + a * sm[0]
            out.setdefault(mid, {})[ch] = [round(v, 3) for v in sm]
    return out


# A real sensor never repeats one value for 15 s; a line between two equal 10 s samples does, and the validation
# layer flags that as a frozen sensor (STALE). Each replayed second gets a tiny, repeatable wobble (no RNG drawn).
JITTER = {"temp_c": 0.03, "water_flow_lpm": 0.05, "steam_kgf_cm2": 0.004}


def _jitter(ts: datetime, mid: str, ch: str) -> float:
    import zlib
    h = zlib.crc32(f"{ts.timestamp():.0f}|{mid}|{ch}".encode()) / 0xFFFFFFFF
    return (h * 2.0 - 1.0) * JITTER.get(ch, 0.0)
