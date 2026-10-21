"""Factory simulator: 28 machines, per-second sensor stream, cure cycles, orders, shifts, WIP,
fault injection and scripted scenarios. Implements contracts.FactorySim.

Time: every reading is stamped with the injected SimClock. With a manual clock, step(dt)
advances the clock by dt; with a running clock (speed-accelerated), step() integrates over the
real sim time elapsed since the previous step and ignores dt. Physics is integrated in ≤1 s
sub-steps, so accelerated time (e.g. 60 sim-s per step) keeps the same dynamics.
"""
from __future__ import annotations

import logging
import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, ClassVar

from warroom.clock import SimClock
from warroom.contracts import (
    CureJobState,
    FaultKind,
    FaultSpec,
    MachineKind,
    MachineState,
    MachineStatus,
    Order,
    OrderProgress,
    PlantInput,
    SafetyAction,
    SafetyActionKind,
    Scenario,
    ScenarioName,
    ScenarioStep,
    Schedule,
    ScheduleJob,
    SemiFinishedPart,
    SemiFinishedProgress,
    SensorReading,
    Shift,
    TeamId,
    TireSize,
)
from warroom.factory import (
    CURE_HOURS,
    CURE_TEMP_NOMINAL_C,
    DEMO_ORDER_PROGRESS,
    DEMOLD_MOLDIN_MIN,
    MACHINE_BY_ID,
    MACHINE_IDS,
    MACHINES,
    ML_PRESSES,
    OPTIMISED_SHIFTS,
    TZ,
    presses_on_header,
)
from warroom.sim import physics as P
from warroom.sim.scenarios import get_scenario
from warroom.sim.world import World

log = logging.getLogger("warroom.sim")
HISTORY_S = 2 * 3600            # raw history kept per machine
MAX_SUBSTEPS = 3600
SIZE_LABEL = {TireSize.S: "小胎", TireSize.M: "中胎", TireSize.L: "大胎"}
AMBIENT_C = 30.0

# (ts, temp, pressure, flow, steam, seq)
_Row = tuple[datetime, float | None, float | None, float | None, float | None, int]


@dataclass
class _Fault:
    spec: FaultSpec
    start: datetime
    end: datetime | None
    consumed: bool = False            # SPIKE: one sample only
    frozen: tuple | None = None       # SENSOR_DROPOUT frozen snapshot

    def active(self, t: datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end) and not self.consumed

    def elapsed_s(self, t: datetime) -> float:
        return max(0.0, (t - self.start).total_seconds())


@dataclass
class _Press:
    job: CureJobState | None = None
    idle_until: datetime | None = None
    queue: list[str] = field(default_factory=list)     # upcoming order ids


@dataclass
class _Mach:
    mid: str
    nominal: P.Nominal
    temp: float
    pressure: float
    flow: float | None
    steam: float | None
    status: MachineStatus = MachineStatus.RUNNING
    note: str | None = None
    active_event_id: str | None = None
    batch_epoch: datetime | None = None
    seq: int = 0
    last: SensorReading | None = None
    hist: deque = field(default_factory=deque)


class Factory:
    """Implements contracts.FactorySim. Deterministic for a given seed and call sequence."""

    def __init__(self, clock: SimClock | None = None, seed: int = 7, scenario: ScenarioName = ScenarioName.NORMAL,
                 backfill_min: float = 30.0):
        self.seed = seed
        self.backfill_min = backfill_min
        scenario = ScenarioName(scenario)
        keep_time = clock is not None and scenario is ScenarioName.NORMAL
        self.clock: SimClock = clock or SimClock(manual=True)
        self.safety_log: list[SafetyAction] = []
        self._plan_source: Callable[[], Schedule | None] | None = None
        self.load_scenario(scenario, start_at=self.clock.now() if keep_time else None)

    # ------------------------------------------------------------------ scenario / reset
    def load_scenario(self, name: ScenarioName, start_at: datetime | None = None) -> Scenario:
        """Reset the whole plant to the scenario's initial state and move the clock to its start."""
        sc = get_scenario(ScenarioName(name), start_at=start_at)
        self.scenario = sc
        self.clock.reset(sc.start_at)
        t0 = sc.start_at
        self._t = t0
        self._rng = random.Random(self.seed)
        self._faults: list[_Fault] = []
        self._pending: list[tuple[datetime, ScenarioStep]] = sorted(
            ((t0 + timedelta(seconds=s.at_offset_s), s) for s in sc.steps if s.fault is not None),
            key=lambda x: x[0])
        # issue #17: scripted plant inputs (shift start, rush order, sick call) change the order book or the
        # roster, never a machine; each one fired is handed to the runtime (drain_plant_inputs) as a plant event
        self._plant_pending: list[tuple[datetime, PlantInput]] = sorted(
            ((t0 + timedelta(seconds=s.at_offset_s), s.plant) for s in sc.steps if s.plant is not None),
            key=lambda x: x[0])
        self._plant_fired: list[tuple[datetime, PlantInput]] = []
        self._absences: list[tuple[TeamId, date, str, int]] = []
        self.safety_log = []
        self._m: dict[str, _Mach] = {}
        for i, d in enumerate(MACHINES):
            nom = P.NOMINALS[d.id]
            is_press = d.kind is MachineKind.CURING_PRESS
            m = _Mach(mid=d.id, nominal=nom, temp=nom.temp_c, pressure=nom.pressure,
                      flow=None if is_press else nom.flow_lpm,
                      steam=P.STEAM_NOMINAL_KGF_CM2 if is_press else None)
            # A-BIGOPEN batch started 6 min before start: page 2 shows 時間 14 分 at 14:26.
            stagger_min = 6.0 if d.id == "A-BIGOPEN" else (i * 7.0) % max(nom.cycle_min, 1.0)
            m.batch_epoch = t0 - timedelta(minutes=stagger_min)
            self._m[d.id] = m
        self._plan_at = self._plan_anchor(sc)
        self._plan_state = self._load_plan(self._plan_at)
        self._init_orders(t0)
        self._init_presses(t0)
        # page 11 seeds "10/03 夜班缺 1 人" on the scenario date; the issue #17 plant scenarios start clean (their
        # staffing gap, if any, is the scripted sick call)
        from warroom.sim.scenarios import PLANT_SCENARIOS
        self._gap_date: date | None = None if sc.name in PLANT_SCENARIOS or sc.name is ScenarioName.DAY_LOOP \
            else t0.date()
        if self.backfill_min > 0:
            self._backfill(t0, self.backfill_min)
        # the rest of the plant (orders over time, materials, dock, people, energy, quality, maintenance):
        # reads this Factory, never writes it, own RNG stream -> machine physics and _rng are unchanged
        self.world = World(self, t0, self.seed)
        return sc

    # issue #20 Q1: orders and what every press holds come from the team dataset (orders.csv, tire_unit.csv) as of
    # one dataset moment, shifted onto the scenario's clock. Page 11 plays the EV-2 night (10/28 02:12 -> 10/03),
    # page 2 the EV-1 afternoon (10/22), every other scenario the same night at its own time of day.
    # (concurrent_3 injects faults on presses 3 and 10 at 10:00: the dataset day on which both are curing then)
    _PLAN_DAY = {ScenarioName.OPEN_MILL_OVERHEAT: date(2026, 10, 22), ScenarioName.DAY_LOOP: date(2026, 10, 21),
                 ScenarioName.CONCURRENT_3: date(2026, 10, 27)}
    _PLAN_DAY_DEFAULT = date(2026, 10, 28)

    def _plan_anchor(self, sc: Scenario) -> datetime:
        day = self._PLAN_DAY.get(sc.name, self._PLAN_DAY_DEFAULT)
        t = sc.start_at
        return datetime(day.year, day.month, day.day, t.hour, t.minute, t.second, tzinfo=t.tzinfo)

    def _load_plan(self, at: datetime):
        try:
            from warroom.data.team_plan import plan_state
            st = plan_state(at, getattr(self, "_dataset_path", None))
            return st if st.orders else None
        except (OSError, KeyError, ValueError):       # dataset missing / unreadable: the built-in demo book
            return None

    def _init_orders(self, t0: datetime) -> None:
        st = self._plan_state
        if st is None:
            return self._init_orders_demo(t0)
        shift = t0 - self._plan_at
        self._orders = {o.id: o.model_copy(update={"due": (datetime.combine(due, datetime.min.time()) + shift).date()})
                        for o, due in st.orders}
        self._remaining0 = {z: sum(o.qty - o.done for o in self._orders.values() if o.size is z) for z in TireSize}

    def _init_presses(self, t0: datetime) -> None:
        st = self._plan_state
        if st is None:
            return self._init_presses_demo(t0)
        shift = t0 - self._plan_at
        self._press = {}
        self._in_press = {oid: 0 for oid in self._orders}
        self._plan_started = set()
        for pid in (m for m in self._m if m.startswith("CURE-")):
            u = st.running.get(pid)
            ps = _Press(queue=list(st.queues.get(pid, [])))
            if u is not None:
                job = self._make_job(pid, self._orders[u.order_id], u.cure_start + shift)
                ps.job = job
                self._in_press[u.order_id] += 1
                self._plan_started.add(job.job_id)
            else:
                ps.idle_until = t0
                self._m[pid].status = MachineStatus.IDLE
            self._press[pid] = ps

    def _init_orders_demo(self, t0: datetime) -> None:
        d0 = t0.date()
        s_done, _, _ = DEMO_ORDER_PROGRESS[TireSize.S]
        m_done, _, _ = DEMO_ORDER_PROGRESS[TireSize.M]
        l_done, _, _ = DEMO_ORDER_PROGRESS[TireSize.L]
        # S/M/L 100 each (page 2), done 38/12/8. #A-311 is the next mold on press 7, due in 3 days (p.11).
        orders = [
            Order(id="#S-101", size=TireSize.S, qty=60, done=s_done, due=d0 + timedelta(days=5), customer="客戶甲",
                  mold_id="MD-S101"),
            Order(id="#S-102", size=TireSize.S, qty=39, done=0, due=d0 + timedelta(days=12), customer="客戶乙",
                  mold_id="MD-S102"),
            Order(id="#M-201", size=TireSize.M, qty=50, done=m_done, due=d0 + timedelta(days=21), customer="客戶丙",
                  mold_id="MD-M201"),
            Order(id="#M-202", size=TireSize.M, qty=50, done=0, due=d0 + timedelta(days=45), customer="客戶丁",
                  mold_id="MD-M202"),
            Order(id="#L-301", size=TireSize.L, qty=59, done=l_done, due=d0 + timedelta(days=30), customer="客戶戊",
                  mold_id="MD-L301"),
            Order(id="#A-311", size=TireSize.S, qty=1, done=0, due=d0 + timedelta(days=3), customer="客戶己",
                  mold_id="#A-311", priority=2),
            Order(id="#L-302", size=TireSize.L, qty=41, done=0, due=d0 + timedelta(days=60), customer="客戶庚",
                  mold_id="MD-L302"),
        ]
        self._orders: dict[str, Order] = {o.id: o for o in orders}
        self._remaining0 = {s: DEMO_ORDER_PROGRESS[s][1] - DEMO_ORDER_PROGRESS[s][0] for s in TireSize}

    def _init_presses_demo(self, t0: datetime) -> None:
        # press -> (order, elapsed h at start). CURE-07 (small-only, dataset EV-2 tire S-027) reaches exactly
        # 14.4 h of a 24 h small cure at 02:14:00 in the page-11 scenario (start 02:12:00).
        plan: dict[str, tuple[str, float]] = {
            "CURE-01": ("#S-101", 2.0), "CURE-02": ("#S-101", 6.0), "CURE-03": ("#S-101", 10.0),
            "CURE-04": ("#S-101", 14.0), "CURE-05": ("#S-101", 18.0), "CURE-06": ("#S-101", 21.0),
            "CURE-07": ("#S-101", 14.4 - 2.0 / 60.0),
            "CURE-11": ("#M-201", 10.0), "CURE-08": ("#L-301", 20.0),
            "CURE-09": ("#M-201", 24.0), "CURE-10": ("#L-301", 6.0),
        }
        queues = {"CURE-07": ["#A-311", "#S-101"], "CURE-08": ["#L-301"], "CURE-09": ["#M-201"],
                  "CURE-10": ["#L-301"], "CURE-11": ["#M-201"]}
        self._press: dict[str, _Press] = {}
        self._in_press: dict[str, int] = {o: 0 for o in self._orders}
        self._plan_started: set[str] = set()       # schedule job ids already loaded into a press
        for pid, (oid, el_h) in plan.items():
            o = self._orders[oid]
            mold_in = t0 - timedelta(hours=el_h)
            job = self._make_job(pid, o, mold_in)
            self._press[pid] = _Press(job=job, queue=list(queues.get(pid, [])))
            self._in_press[oid] += 1
            self._plan_started.add(job.job_id)

    def _make_job(self, pid: str, o: Order, mold_in: datetime) -> CureJobState:
        req = CURE_HOURS[o.size]
        return CureJobState(job_id=f"J-{pid}-{mold_in:%m%d%H%M}", order_id=o.id, size=o.size,
                            mold_id=o.mold_id or f"MD-{o.id}", mold_in_at=mold_in,
                            planned_demold_at=mold_in + timedelta(hours=req), setpoint_c=CURE_TEMP_NOMINAL_C,
                            cure_required_h=req, cure_elapsed_h=0.0)

    def _backfill(self, t0: datetime, minutes: float) -> None:
        """Quiet normal history before the scenario starts (read_sensors 30-min window, sparklines)."""
        rng = random.Random(self.seed + 1)
        n = int(minutes * 60)
        for m in self._m.values():
            for k in range(n, 0, -1):
                ts = t0 - timedelta(seconds=k)
                m.seq += 1
                m.hist.append(self._noisy_row(m, ts, rng))

    # ------------------------------------------------------------------ faults
    def inject_fault(self, fault: FaultSpec, at: datetime | None = None) -> None:
        if fault.machine_id not in MACHINE_BY_ID:
            raise KeyError(f"unknown machine {fault.machine_id}")
        start = (at or self.clock.now()) + timedelta(seconds=fault.start_offset_s)
        end = start + timedelta(seconds=fault.duration_s) if fault.duration_s is not None else None
        self._faults.append(_Fault(spec=fault, start=start, end=end))

    def clear_faults(self, machine_id: str | None = None) -> None:
        if machine_id is None:
            self._faults, self._pending = [], []
            return
        # #76: a scripted fault of the same kind that has not started yet would break the fixed machine again right
        # away, so it goes too. A later fault of another kind is a new failure and stays (p.11: 回報處置完成 on 7 號
        # at 02:16 clears 蒸汽壓不足 but keeps the 02:22:55 持續洩壓, which the round-2 card is about).
        kinds = {f.spec.kind for f in self._faults if f.spec.machine_id == machine_id}
        self._faults = [f for f in self._faults if f.spec.machine_id != machine_id]
        self._pending = [(at, st) for at, st in self._pending
                         if st.fault is None or st.fault.machine_id != machine_id or st.fault.kind not in kinds]

    def faults(self, active_only: bool = True) -> list[FaultSpec]:
        t = self._t
        return [f.spec for f in self._faults if (f.active(t) or not active_only)]

    def fault_started_at(self, machine_id: str) -> datetime | None:
        t = self._t
        starts = [f.start for f in self._faults
                  if f.spec.machine_id == machine_id and f.start <= t and (f.end is None or t < f.end)]
        return min(starts) if starts else None

    def _active(self, mid: str, t: datetime, kinds: set[FaultKind] | None = None) -> list[_Fault]:
        return [f for f in self._faults if f.spec.machine_id == mid and f.active(t)
                and (kinds is None or f.spec.kind in kinds)]

    # ------------------------------------------------------------------ physics
    _PROCESS_KINDS: ClassVar[frozenset[FaultKind]] = frozenset({FaultKind.TEMP_RAMP, FaultKind.TEMP_STEP, FaultKind.WATER_FLOW_DROP,
                      FaultKind.STEAM_PRESSURE_DROP, FaultKind.STEAM_LEAK})

    def _steam_target(self, mid: str, t: datetime) -> float:
        target = P.STEAM_NOMINAL_KGF_CM2
        floor = 0.0
        for f in self._active(mid, t, self._PROCESS_KINDS):
            p, el = f.spec.params, f.elapsed_s(t)
            k = f.spec.kind
            if k is FaultKind.STEAM_PRESSURE_DROP:
                drop = p.get("drop")
                if drop is None:
                    drop = (CURE_TEMP_NOMINAL_C - p.get("target_c", 122.0)) / P.STEAM_TEMP_GAIN_C
                target -= drop
            elif k is FaultKind.STEAM_LEAK:
                target -= p.get("rate_c_per_min", 0.3) / P.STEAM_TEMP_GAIN_C * el / 60.0
                floor = max(floor, p.get("floor_kgf_cm2", P.STEAM_FLOOR_KGF_CM2))
            elif k is FaultKind.TEMP_STEP:
                target += p.get("delta", 0.0) / P.STEAM_TEMP_GAIN_C
            elif k is FaultKind.TEMP_RAMP:
                goal = p.get("target", CURE_TEMP_NOMINAL_C)
                if "rate_c_per_min" in p:
                    span = goal - CURE_TEMP_NOMINAL_C
                    moved = math.copysign(min(abs(span), p["rate_c_per_min"] * el / 60.0), span)
                else:
                    tau = p.get("tau_s", 60.0)
                    moved = (goal - CURE_TEMP_NOMINAL_C) * (1 - math.exp(-el / tau))
                target += moved / P.STEAM_TEMP_GAIN_C
        # a leak on another press of the same header pulls this one down a little (page 11: 6 & 8)
        header = MACHINE_BY_ID[mid].steam_header
        if header:
            for other in presses_on_header(header):
                if other == mid:
                    continue
                for f in self._active(other, t, {FaultKind.STEAM_LEAK}):
                    target -= f.spec.params.get("header_drop", P.LEAK_HEADER_DROP_KGF_CM2)
        return max(floor, target)

    def _flow_target(self, m: _Mach, t: datetime) -> float | None:
        if m.flow is None:
            return None
        flow = m.nominal.flow_lpm or 0.0
        for f in self._active(m.mid, t, {FaultKind.WATER_FLOW_DROP}):
            p = f.spec.params
            goal = p.get("target_lpm", flow * 0.45)
            frac = min(1.0, f.elapsed_s(t) / max(p.get("ramp_s", P.FLOW_RAMP_S), 1e-6))
            flow = flow + (goal - flow) * frac
        return flow

    def _integrate(self, m: _Mach, t: datetime, dt: float) -> None:
        if dt <= 0:
            return
        if m.steam is not None:                 # curing press: steam -> platen temperature
            m.steam = P.approach(m.steam, self._steam_target(m.mid, t), dt, P.STEAM_TAU_S)
            m.temp = P.approach(m.temp, P.implied_press_temp(m.steam), dt, P.THERMAL_TAU_S)
            return
        nom = m.nominal
        m.flow = self._flow_target(m, t)
        stopped = m.status in (MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE)
        target = AMBIENT_C if stopped else nom.temp_c + P.FLOW_HEAT_GAIN_C_PER_LPM * ((nom.flow_lpm or 0) - (m.flow or 0))
        ramps = self._active(m.mid, t, {FaultKind.TEMP_RAMP, FaultKind.TEMP_STEP})
        linear = None
        for f in ramps:
            p = f.spec.params
            if f.spec.kind is FaultKind.TEMP_STEP:
                target += p.get("delta", 0.0)
            elif "rate_c_per_min" in p:
                linear = (p.get("target", target), p["rate_c_per_min"])
            else:
                target = p.get("target", target)
        if linear is not None and not stopped:
            goal, rate = linear
            step = abs(rate) * dt / 60.0
            m.temp = min(goal, m.temp + step) if goal >= m.temp else max(goal, m.temp - step)
        else:
            tau = P.COMPOUND_TAU_S
            for f in ramps:
                if f.spec.kind is FaultKind.TEMP_STEP:
                    tau = 10.0       # a step reaches its new level within ~30 s
                elif "tau_s" in f.spec.params:
                    tau = f.spec.params["tau_s"]
            m.temp = P.approach(m.temp, target, dt, tau)

    def _noisy_row(self, m: _Mach, ts: datetime, rng: random.Random) -> _Row:
        g = rng.gauss
        temp = m.temp + g(0, P.PRESS_TEMP_NOISE if m.steam is not None else P.NOISE["temp_c"])
        pres = m.pressure + g(0, P.NOISE["pressure_kgf_cm2"])
        flow = None if m.flow is None else max(0.0, m.flow + g(0, P.NOISE["water_flow_lpm"]))
        steam = None if m.steam is None else max(0.0, m.steam + g(0, P.NOISE["steam_kgf_cm2"]))
        return (ts, round(temp, 2), round(pres, 2), None if flow is None else round(flow, 2),
                None if steam is None else round(steam, 3), m.seq)

    def _apply_sensor_faults(self, m: _Mach, row: _Row, t: datetime) -> _Row:
        ts, temp, pres, flow, steam, seq = row
        for f in self._active(m.mid, t, {FaultKind.SENSOR_DRIFT, FaultKind.SENSOR_DROPOUT, FaultKind.SPIKE}):
            p = f.spec.params
            if f.spec.kind is FaultKind.SENSOR_DRIFT and temp is not None:
                bias = p.get("rate_c_per_min", -0.5) * f.elapsed_s(t) / 60.0
                cap = p.get("max_c")
                if cap is not None:
                    bias = max(bias, cap) if cap < 0 else min(bias, cap)
                temp = round(temp + bias, 2)
            elif f.spec.kind is FaultKind.SENSOR_DROPOUT:
                if p.get("frozen", 0.0) >= 0.5:
                    if f.frozen is None:
                        f.frozen = (temp, pres, flow, steam)
                    temp, pres, flow, steam = f.frozen
                else:
                    temp = pres = flow = steam = None
            elif f.spec.kind is FaultKind.SPIKE:
                if temp is not None:
                    temp = round(p["value"] if "value" in p else temp + p.get("delta", 50.0), 2)
                f.consumed = True
        return (ts, temp, pres, flow, steam, seq)

    # ------------------------------------------------------------------ step
    def step(self, dt_s: float = 1.0) -> list[SensorReading]:
        """Advance the plant and return one reading per machine (factory.MACHINES order)."""
        prev = self._t
        if self.clock.manual and not self.clock.paused:
            self.clock.advance(dt_s)
        now = self.clock.now()
        dt = max(0.0, (now - prev).total_seconds())
        # scheduled scenario faults start at their scripted time, even inside a long step
        while self._pending and self._pending[0][0] <= now:
            at, st = self._pending.pop(0)
            self.inject_fault(st.fault, at=at)          # type: ignore[arg-type]
        while self._plant_pending and self._plant_pending[0][0] <= now:
            at, pin = self._plant_pending.pop(0)
            self.apply_plant_input(pin, at)
        n_sub = min(MAX_SUBSTEPS, max(1, math.ceil(dt))) if dt > 0 else 0
        h = dt / n_sub if n_sub else 0.0
        for k in range(n_sub):
            t_k = prev + timedelta(seconds=h * (k + 1))
            for m in self._m.values():
                self._integrate(m, t_k, h)
        self._t = now
        self._advance_cure(now)
        out: list[SensorReading] = []
        cutoff = now - timedelta(seconds=HISTORY_S)
        for mid in MACHINE_IDS:
            m = self._m[mid]
            m.seq += 1
            row = self._apply_sensor_faults(m, self._noisy_row(m, now, self._rng), now)
            m.hist.append(row)
            while m.hist and m.hist[0][0] < cutoff:
                m.hist.popleft()
            r = self._to_reading(mid, row)
            m.last = r
            out.append(r)
        try:
            self.world.advance(now)
        except Exception:  # noqa: BLE001 - the world view must never break the plant loop or the red path
            log.exception("world advance failed")
        return out

    @staticmethod
    def _to_reading(mid: str, row: _Row) -> SensorReading:
        ts, temp, pres, flow, steam, seq = row
        return SensorReading(ts=ts, machine_id=mid, temp_c=temp, pressure_kgf_cm2=pres, water_flow_lpm=flow,
                             steam_kgf_cm2=steam, seq=seq)

    # ------------------------------------------------------------------ cure cycles
    # ---- plan-driven mold-in (page 6: the cure-press schedule decides every de-mold / mold-in)
    def attach_plan(self, source: Any) -> None:
        """Drive every mold-in from the ACTIVE schedule. `source` is a ScheduleStore (its
        current().schedule is read) or a callable returning a Schedule (or None).

        With a plan attached, a free press loads the plan's next not-yet-started job on that press,
        at max(planned T, de-mold + 30 min), with the plan's order, mold and cure_h; and stays idle
        when the plan has nothing for it. So an approved move of #A-311 off press 7 and the staffed
        de-mold windows reach the plant. Without a plan the sim keeps its own queue (tests, replay)."""
        if source is None:
            self._plan_source = None
        elif callable(source):
            self._plan_source = source
        else:
            store = source

            def _cur() -> Schedule | None:
                return store.current().schedule
            self._plan_source = _cur

    def _plan(self) -> Schedule | None:
        if self._plan_source is None:
            return None
        try:
            return self._plan_source()
        except Exception:  # noqa: BLE001 - no active version (store being swapped): no new mold-ins
            return None

    def next_planned_job(self, press_id: str) -> ScheduleJob | None:
        """The plan's next job for this press that the plant has not loaded yet."""
        sched = self._plan()
        if sched is None:
            return None
        cands = [j for j in sched.jobs
                 if j.press_id == press_id and not j.frozen and j.id not in self._plan_started]
        return min(cands, key=lambda j: (j.mold_in, j.id)) if cands else None

    def _start_from_plan(self, pid: str, ps: _Press, now: datetime) -> None:
        m = self._m[pid]
        if m.status is not MachineStatus.IDLE:
            return                     # stopped / held / out of service: no new mold-in
        if ps.idle_until is not None and now < ps.idle_until:
            return                     # de-mold / mold change still in progress (30 min)
        j = self.next_planned_job(pid)
        if j is None:
            m.note = "排程無下一模，待命"
            return
        at = j.mold_in if ps.idle_until is None else max(j.mold_in, ps.idle_until)
        if now < at:
            m.note = f"待排程入模 {j.mold_in:%m/%d %H:%M}（{j.order_id}）"
            return
        o = self._orders.get(j.order_id)
        mold = j.mold_id or (o.mold_id if o is not None else None) or f"MD-{j.order_id}"
        ps.job = CureJobState(job_id=j.id, order_id=j.order_id, size=j.size, mold_id=mold, mold_in_at=at,
                              planned_demold_at=at + timedelta(hours=j.cure_h), setpoint_c=CURE_TEMP_NOMINAL_C,
                              cure_required_h=j.cure_h, cure_elapsed_h=0.0)
        self._plan_started.add(j.id)
        if j.order_id in self._in_press:
            self._in_press[j.order_id] += 1
        if j.order_id in ps.queue:
            ps.queue.remove(j.order_id)
        ps.idle_until = None
        m.status, m.note = MachineStatus.RUNNING, None

    def _advance_cure(self, now: datetime) -> None:
        planned = self._plan_source is not None
        for pid, ps in self._press.items():
            m = self._m[pid]
            if ps.job is None:
                if planned:
                    self._start_from_plan(pid, ps, now)
                elif ps.idle_until is not None and now >= ps.idle_until and m.status is MachineStatus.IDLE:
                    self._start_next(pid, ps, ps.idle_until)
                continue
            if m.status not in (MachineStatus.RUNNING, MachineStatus.WARNING, MachineStatus.ALARM):
                continue        # held / stopped / out of service: tire stays in the mold
            el_h = (now - ps.job.mold_in_at).total_seconds() / 3600.0
            if el_h >= ps.job.cure_required_h:
                demold_at = ps.job.mold_in_at + timedelta(hours=ps.job.cure_required_h)
                o = self._orders.get(ps.job.order_id)
                if o is not None:
                    self._orders[o.id] = o.model_copy(update={"done": min(o.qty, o.done + 1)})
                    self._in_press[o.id] = max(0, self._in_press[o.id] - 1)
                ps.job = None
                ps.idle_until = demold_at + timedelta(minutes=DEMOLD_MOLDIN_MIN)
                m.status = MachineStatus.IDLE
                m.note = "出模，換模入模中"
                if planned:
                    if now >= ps.idle_until:
                        self._start_from_plan(pid, ps, now)
                elif now >= ps.idle_until:
                    self._start_next(pid, ps, ps.idle_until)

    # #A-311 (small, dataset S-033) is pinned as the next mold of press 7 (page 11); the other small presses
    # must not pick it up first just because it has the highest priority.
    RESERVED_NEXT: dict[str, str] = {"#A-311": "CURE-07"}

    def _start_next(self, pid: str, ps: _Press, at: datetime) -> None:
        """No plan attached: the sim's own queue, then order priority / due date."""
        sizes = (TireSize.M, TireSize.L) if pid in ML_PRESSES else (TireSize.S,)
        candidates = list(ps.queue) + [o.id for o in sorted(self._orders.values(), key=lambda o: (-o.priority, o.due))
                                       if o.size in sizes]
        for oid in candidates:
            o = self._orders.get(oid)
            if o is None or o.size not in sizes:
                continue
            if self.RESERVED_NEXT.get(oid, pid) != pid:     # a pinned next mold stays on its own press
                continue
            if o.done + self._in_press[oid] >= o.qty:
                continue
            if oid in ps.queue:
                ps.queue.remove(oid)
            ps.job = self._make_job(pid, o, at)
            self._in_press[oid] += 1
            ps.idle_until = None
            m = self._m[pid]
            m.status, m.note = MachineStatus.RUNNING, None
            return
        ps.idle_until = None      # nothing left to cure: stays idle

    def set_cure_required(self, press_id: str, total_cure_h: float) -> CureJobState:
        """Apply an approved cure-time change (set_cure_time write tool) to the running job."""
        ps = self._press[press_id]
        if ps.job is None:
            raise ValueError(f"{press_id} has no cure job")
        ps.job = ps.job.model_copy(update={
            "cure_required_h": float(total_cure_h),
            "planned_demold_at": ps.job.mold_in_at + timedelta(hours=float(total_cure_h))})
        return ps.job

    # ------------------------------------------------------------------ state views
    def _cure_job_view(self, pid: str) -> CureJobState | None:
        ps = self._press.get(pid)
        if ps is None or ps.job is None:
            return None
        el = (self._t - ps.job.mold_in_at).total_seconds() / 3600.0
        return ps.job.model_copy(update={"cure_elapsed_h": round(el, 4)})

    def machine(self, machine_id: str) -> MachineState:
        d = MACHINE_BY_ID[machine_id]
        m = self._m[machine_id]
        job = self._cure_job_view(machine_id)
        if job is not None:
            elapsed_min = job.cure_elapsed_h * 60.0
            batch = job.job_id
        elif d.kind is MachineKind.CURING_PRESS:
            elapsed_min, batch = 0.0, None
        else:
            cyc = max(m.nominal.cycle_min, 1.0) * 60.0
            since = (self._t - m.batch_epoch).total_seconds() if m.batch_epoch else 0.0
            elapsed_min = (since % cyc) / 60.0
            batch = f"{machine_id}-B{int(since // cyc) + 1:04d}"
        return MachineState(machine_id=machine_id, line=d.line, name=d.name, short_name=d.short_name,
                            status=m.status, last_reading=m.last, step_elapsed_min=round(elapsed_min, 2),
                            compound_stage=m.nominal.stage, batch_id=batch, cure_job=job,
                            active_event_id=m.active_event_id, note=m.note)

    def machines(self) -> list[MachineState]:
        return [self.machine(mid) for mid in MACHINE_IDS]

    def states(self) -> dict[str, MachineState]:
        """machine_id -> MachineState, the shape SafetyCore.check() takes."""
        return {mid: self.machine(mid) for mid in MACHINE_IDS}

    def history(self, machine_id: str, minutes: float) -> list[SensorReading]:
        m = self._m[machine_id]
        if not m.hist:
            return []
        if minutes <= 0:
            return [self._to_reading(machine_id, m.hist[-1])]
        cutoff = self._t - timedelta(minutes=minutes)
        return [self._to_reading(machine_id, r) for r in m.hist if r[0] >= cutoff]

    # ------------------------------------------------------------------ orders / shifts / WIP
    def orders(self) -> list[Order]:
        return list(self._orders.values())

    def order_progress(self) -> list[OrderProgress]:
        out = []
        for size in (TireSize.S, TireSize.M, TireSize.L):
            done = sum(o.done for o in self._orders.values() if o.size is size)
            total = sum(o.qty for o in self._orders.values() if o.size is size)
            base_done, base_total, base_h = DEMO_ORDER_PROGRESS[size]
            rem0 = max(1, base_total - base_done)
            remaining_h = base_h * max(0, total - done) / rem0     # page-2 estimate scaled by tires left
            out.append(OrderProgress(size=size, label=SIZE_LABEL[size], done=done, total=total,
                                     remaining_h=round(remaining_h, 1)))
        return out

    def shifts(self) -> list[Shift]:
        """OPTIMISED_SHIFTS (page 6 premise) from yesterday to +7 days. The night shift of the
        scenario start date is one operator short (page 11: 10/03 夜班缺 1 人)."""
        d0 = self._t.date()
        out: list[Shift] = []
        for k in range(-1, 8):
            d = d0 + timedelta(days=k)
            for sd in OPTIMISED_SHIFTS:
                if d.weekday() not in sd.weekdays:
                    continue
                start = datetime.combine(d, sd.start, tzinfo=TZ)
                end = datetime.combine(d, sd.end, tzinfo=TZ)
                if end <= start:
                    end += timedelta(days=1)
                head = sd.headcount
                if d == self._gap_date and sd.team is TeamId.BODY and sd.name == "夜班":
                    head = sd.headcount - 1
                head -= sum(n for team, day, name, n in self._absences if team is sd.team and day == d and name == sd.name)
                out.append(Shift(id=f"{sd.team.value}-{d:%Y%m%d}-{sd.name}", team=sd.team, name=sd.name,
                                 start=start, end=end, headcount=head, required=sd.headcount))
        return out

    def semi_finished(self) -> SemiFinishedProgress:
        """Page 2 panel ②: 大胎 #07 parts A–D, mold-in 17:00."""
        t = self._t
        mold_in = datetime.combine(t.date(), datetime.min.time(), tzinfo=TZ).replace(hour=17)
        if t > mold_in:
            mold_in += timedelta(days=1)

        def state(mid: str, base: str) -> str:
            m = self._m[mid]
            if m.status in (MachineStatus.WARNING, MachineStatus.ALARM, MachineStatus.STOPPED) or \
                    self._active(mid, t, self._PROCESS_KINDS):
                return "alert"
            return base

        parts = [
            SemiFinishedPart(part="A", label="A 胎體／胎面膠", status_text="預熱中",
                             state=state("A-BIGOPEN", "in_progress"), machine_id="A-BIGOPEN"),  # type: ignore[arg-type]
            SemiFinishedPart(part="B", label="B 胎側膠", status_text="成形中",
                             state=state("B-FORM", "in_progress"), machine_id="B-FORM"),  # type: ignore[arg-type]
            SemiFinishedPart(part="C", label="C 鋼絲圈", status_text="包布中",
                             state=state("C-WRAP", "in_progress"), machine_id="C-WRAP"),  # type: ignore[arg-type]
            SemiFinishedPart(part="D", label="D 尼龍布", status_text="已完成", state="done", machine_id="D-CUT"),
        ]
        return SemiFinishedProgress(tire_label="大胎 #07", order_id="#L-301", size=TireSize.L, parts=parts,
                                    planned_mold_in_at=mold_in)

    # ------------------------------------------------------------------ actions on the plant
    def apply_safety_action(self, action: SafetyAction) -> None:
        """Red path: the safety core's decision takes effect in the plant immediately."""
        m = self._m[action.machine_id]
        self.safety_log.append(action)
        if action.action is SafetyActionKind.STOP_MACHINE:
            m.status, m.note = MachineStatus.STOPPED, f"安全核心停機：{action.message}"
        elif action.action is SafetyActionKind.HOLD_BATCH:
            m.status, m.note = MachineStatus.STOPPED, f"批次扣留：{action.message}"
        elif action.action is SafetyActionKind.CURE_HOLD:
            m.status, m.note = MachineStatus.STOPPED, f"扣模待 QC、禁止新入模：{action.message}"
        elif action.action is SafetyActionKind.ALARM_ONLY:
            if m.status in (MachineStatus.RUNNING, MachineStatus.WARNING, MachineStatus.IDLE):
                m.status = MachineStatus.ALARM
            m.note = f"警示：{action.message}"

    # ------------------------------------------------------------------ issue #17: plant inputs (not machine faults)
    def apply_plant_input(self, pin: PlantInput, at: datetime | None = None) -> None:
        """A rush order joins the order book (the scheduler sees it on the next solve); a sick call takes a
        person off a shift (factory shifts() headcount, the world's roster). The shift start changes nothing.
        Machines are never touched."""
        at = at or self._t
        if pin.kind == "rush_order" and pin.order is not None:
            self.add_order(pin.order)
        elif pin.kind == "absence" and pin.team is not None and pin.shift_name:
            self._absences.append((TeamId(pin.team), at.date(), pin.shift_name, max(1, pin.people)))
        self._plant_fired.append((at, pin))
        try:
            self.world.on_plant_input(pin, at)
        except Exception:  # noqa: BLE001 - the world view must never break the plant loop
            log.exception("world plant input failed")

    def drain_plant_inputs(self) -> list[tuple[datetime, PlantInput]]:
        """Plant inputs fired since the last call (the runtime opens a plant event for each)."""
        out, self._plant_fired = self._plant_fired, []
        return out

    def add_order(self, order: Order) -> None:
        if order.id in self._orders:
            return
        self._orders[order.id] = order
        self._in_press.setdefault(order.id, 0)

    def set_machine_status(self, machine_id: str, status: MachineStatus, note: str | None = None) -> None:
        m = self._m[machine_id]
        m.status = MachineStatus(status)
        if note is not None:
            m.note = note

    def set_active_event(self, machine_id: str, event_id: str | None) -> None:
        self._m[machine_id].active_event_id = event_id

    # handy for tests / bench
    def true_temp(self, machine_id: str) -> float:
        return self._m[machine_id].temp

    def now(self) -> datetime:
        return self._t
