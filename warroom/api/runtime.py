"""Runtime: wires every module and runs the plant loop.

Per sim second (tick):
  readings = sim.step(1) -> actions = safety.check(...) -> sim.apply_safety_action (red path, never
  awaits) -> anomalies = detector.ingest(readings) -> asyncio task state_machine.handle(a) per new
  anomaly -> bus.publish(MACHINES ...).

The clock is a manual SimClock: only sim.step() (or Runtime) moves it, so speed/pause/replay are
exact. A background task converts wall time into sim seconds at `speed` and calls tick().

Each sibling module is built for real; if it is missing or fails to construct, a stand-in from
warroom.api.fallbacks is used and the reason recorded in `Runtime.degraded` (GET
/api/health/components), unless allow_fallback=False.
"""
from __future__ import annotations

import asyncio
import heapq
import inspect
import itertools
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from warroom import factory as F
from warroom.api import fallbacks as FB
from warroom.bus import InProcessBus
from warroom.clock import SimClock
from warroom.config import Settings, get_settings
from warroom.contracts import (
    TERMINAL_STATES,
    Anomaly,
    ApprovalRequest,
    AuditKind,
    Channel,
    CompoundStage,
    DashboardSnapshot,
    Event,
    FaultSpec,
    FocusPanel,
    HeaderStatus,
    LLMMode,
    MachineDetail,
    MachineState,
    MachineStatus,
    MachineTile,
    MetricTile,
    PendingAction,
    RiskLevel,
    SafetyAction,
    ScenarioName,
    SensorReading,
    SolveRequest,
    TileStatus,
    ToolName,
    WsType,
)

log = logging.getLogger("warroom.runtime")

NORMAL_STATUSES = {MachineStatus.RUNNING, MachineStatus.IDLE}
CAUSE_TEXT = {
    "cooling_insufficient": "冷卻能力不足", "steam_pressure_low": "蒸汽壓不足", "steam_leak": "蒸汽持續洩漏",
    "sensor_drift": "感測器漂移", "heater_fault": "加熱板異常",
}
KIND_TEXT = {"overheat": "溫度過高", "temp_drop": "溫度下降", "pressure_low": "壓力偏低",
             "water_flow_low": "水流偏低", "steam_low": "蒸汽壓偏低", "sensor_fault": "感測器異常",
             "multi_machine": "多機同時異常"}


@dataclass
class Components:
    """Everything the Runtime drives. Built by ComponentBuilder; tests may inject their own."""
    clock: SimClock
    bus: InProcessBus
    sim: Any
    detector: Any
    safety: Any
    audit: Any
    approvals: Any
    state_machine: Any
    kb: Any = None
    scheduler: Any = None
    store: Any = None
    tools: Any = None
    gate: Any = None
    commander: Any = None
    capacity: Callable[[], Any] = FB.fallback_capacity_report
    degraded: dict[str, str] = field(default_factory=dict)


class ComponentBuilder:
    """Builds the long-lived parts once (bus, audit, kb, commander, scheduler) and the per-run parts
    (sim, detector, safety, store, approvals, tools, gate, state machine) on every reset."""

    PER_RUN = ("sim", "detector", "safety", "store", "approvals", "tools", "gate", "state_machine")

    def __init__(self, settings: Settings, allow_fallback: bool = True, use_real: bool = True):
        self.s = settings
        self.allow_fallback = allow_fallback
        self.use_real = use_real
        self.degraded: dict[str, str] = {}
        self.clock = SimClock(start=FB.DEMO_DAY.replace(hour=8), speed=settings.sim_speed, manual=True)
        self.bus = InProcessBus(clock=self.clock)
        self.audit = self._audit()
        self.kb = self._kb()
        self.commander = self._commander()
        self.scheduler = self._scheduler()
        self.capacity = self._capacity()

    def _t(self, name: str, real: Callable[[], Any], fallback: Callable[[], Any]) -> Any:
        if not self.use_real:
            self.degraded[name] = "use_real=False (test/degraded mode)"
            return fallback()
        try:
            return real()
        except Exception as e:  # NotImplementedError while a sibling is a stub, or a real failure
            if not self.allow_fallback:
                raise
            self.degraded[name] = f"{type(e).__name__}: {e}"[:300]
            log.warning("component %s unavailable (%s) -> fallback", name, self.degraded[name])
            return fallback()

    # --- long-lived ---
    def _audit(self):
        def real():
            from warroom.agent import JsonlAuditLog
            return JsonlAuditLog(self.s.audit_path, bus=self.bus, clock=self.clock)
        return self._t("audit", real, lambda: FB.MemoryAuditLog(None, bus=self.bus, clock=self.clock))

    def _kb(self):
        def real():
            from warroom.kb import QdrantSopKB
            return QdrantSopKB(path=None if self.s.qdrant_url else self.s.qdrant_path, url=self.s.qdrant_url,
                               sop_dir=self.s.sop_dir, embed_model=self.s.embed_model)
        return self._t("kb", real, lambda: FB.KeywordSopKB(self.s.sop_dir))

    def _commander(self):
        def real():
            from warroom.agent import make_commander
            return make_commander(self.s)
        return self._t("commander", real, lambda: None)

    def _scheduler(self):
        def real():
            from warroom.schedule import FallbackScheduler
            return FallbackScheduler()
        return self._t("scheduler", real, lambda: None)

    def _capacity(self):
        def real():
            from warroom.schedule import capacity_report
            capacity_report()            # raises while still a stub
            return capacity_report
        return self._t("capacity", real, lambda: FB.fallback_capacity_report)

    # --- per run ---
    def build(self, scenario: ScenarioName, sleep: Callable[[float], Any] | None = None) -> Components:
        for k in self.PER_RUN:
            self.degraded.pop(k, None)
        clock, bus = self.clock, self.bus

        def sim_real():
            from warroom.sim import Factory
            return Factory(clock=clock, seed=7, scenario=ScenarioName.NORMAL)
        sim = self._t("sim", sim_real, lambda: FB.FallbackFactory(clock=clock))
        sc = sim.load_scenario(scenario)
        if getattr(sc, "start_at", None) and abs((clock.now() - sc.start_at).total_seconds()) > 1:
            clock.reset(sc.start_at)

        def det_real():
            from warroom.detect import StreamDetector
            return StreamDetector(clock=clock)
        detector = self._t("detector", det_real, lambda: FB.FallbackDetector(clock=clock))

        def safety_real():
            from warroom.safety import RuleSafetyCore
            return RuleSafetyCore(clock=clock)
        safety = self._t("safety", safety_real, lambda: FB.FallbackSafetyCore(clock=clock))

        store = self._store(sim)

        def appr_real():
            from warroom.agent import ApprovalBrokerImpl
            return ApprovalBrokerImpl(bus=bus, audit=self.audit, clock=clock)
        approvals = self._t("approvals", appr_real,
                            lambda: FB.MemoryApprovalBroker(bus=bus, audit=self.audit, clock=clock))

        def tools_real():
            from warroom.tools import ToolContext, build_registry
            return build_registry(ToolContext(sim=sim, detector=detector, kb=self.kb, scheduler=self.scheduler,
                                              store=store, audit=self.audit, bus=bus, clock=clock))
        tools = self._t("tools", tools_real, lambda: None)

        def gate_real():
            from warroom.agent import RuleGate
            return RuleGate(store=store) if "store" in inspect.signature(RuleGate).parameters else RuleGate()
        gate = self._t("gate", gate_real, lambda: None)

        def sm_real():
            if None in (self.commander, tools, gate):
                raise RuntimeError("commander/tools/gate unavailable")
            from warroom.agent import StateMachine
            extra = {"store": store, "sleep": sleep, "approval_timeout_s": self.s.approval_timeout_s}
            params = inspect.signature(StateMachine).parameters
            extra = {k: v for k, v in extra.items() if k in params and v is not None}
            return StateMachine(self.commander, tools, gate, approvals, self.audit, sim=sim, bus=bus, clock=clock,
                                **extra)
        sm = self._t("state_machine", sm_real,
                     lambda: FB.MinimalStateMachine(audit=self.audit, bus=bus, clock=clock))

        return Components(clock=clock, bus=bus, sim=sim, detector=detector, safety=safety, audit=self.audit,
                          approvals=approvals, state_machine=sm, kb=self.kb, scheduler=self.scheduler, store=store,
                          tools=tools, gate=gate, commander=self.commander, capacity=self.capacity,
                          degraded=self.degraded)

    def _store(self, sim):
        root = self.s.schedule_dir / "live"

        def real():
            from warroom.schedule import FileScheduleStore
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True, exist_ok=True)
            return FileScheduleStore(root, initial=self._initial_schedule(sim))
        return self._t("store", real, lambda: FB.MemoryScheduleStore(None, initial=None, clock=self.clock))

    def _initial_schedule(self, sim):
        init = getattr(sim, "initial_schedule", None)
        if callable(init):
            return init()
        if self.scheduler is not None:
            try:   # in-progress cures frozen + page-11 pin: press 7's next mold is #A-311
                from warroom.schedule import seed_schedule
                orders = sim.orders()
                next_on = {"CURE-07": "#A-311"} if any(o.id == "#A-311" for o in orders) else None
                return seed_schedule(self.clock.now(), orders, sim.shifts(), sim.machines(),
                                     scheduler=self.scheduler, next_on=next_on)
            except Exception as e:  # noqa: BLE001 - fall back to a plain solve
                log.warning("seed_schedule failed (%s); plain solve", e)
            res = self.scheduler.solve(SolveRequest(now=self.clock.now(), orders=sim.orders(), shifts=sim.shifts()))
            if res.schedule is not None:
                return res.schedule
        return None


class Runtime:
    """Owns the components, the background loop and every read model the API serves."""

    def __init__(self, settings: Settings | None = None, *, components: Components | None = None,
                 builder: ComponentBuilder | None = None, allow_fallback: bool = True, use_real: bool = True,
                 tick_wall_s: float = 0.2, max_steps_per_iter: int = 600, run_loop: bool = True):
        self.settings = settings or get_settings()
        self.builder = None if components else (builder or ComponentBuilder(self.settings, allow_fallback, use_real))
        try:
            self.scenario = ScenarioName(self.settings.scenario)
        except ValueError:
            self.scenario = ScenarioName.NORMAL
        self._sleepers: list[tuple[datetime, int, asyncio.Future]] = []
        self._sleeper_seq = itertools.count()
        self.c: Components = components or self.builder.build(self.scenario, sleep=self.sim_sleep)
        self.speed = float(self.settings.sim_speed)
        self.paused = False
        self.network = "online"
        self.tick_wall_s = tick_wall_s
        self.max_steps_per_iter = max_steps_per_iter
        self.run_loop = run_loop
        self.started_wall = time.monotonic()
        self.safety_log: list[SafetyAction] = []
        self.anomalies: list[Anomaly] = []
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop_task: asyncio.Task | None = None
        self._baseline: dict[str, dict[str, float]] = {}
        self._last_machines_pub = 0.0
        self._last_snapshot_pub = 0.0
        self._kb_ok: bool | None = None
        self._llm_probe: tuple[float, bool] = (-1e9, False)
        self.ticks = 0
        self.compute_hold_max_s = 120.0          # never freeze the plant longer than this per tick

    # ------------------------------------------------------------------ shortcuts
    clock = property(lambda self: self.c.clock)
    bus = property(lambda self: self.c.bus)
    sim = property(lambda self: self.c.sim)
    audit = property(lambda self: self.c.audit)
    approvals = property(lambda self: self.c.approvals)
    state_machine = property(lambda self: self.c.state_machine)
    degraded = property(lambda self: dict(self.c.degraded))

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.started_wall = time.monotonic()
        self.audit.append(AuditKind.SYSTEM, "system", {"msg": "runtime start", "scenario": self.scenario.value,
                                                       "degraded": self.degraded, "llm_mode": self.llm_mode.value})
        if self.c.kb is not None:
            asyncio.get_running_loop().run_in_executor(None, self._ingest_kb)
        if self.run_loop and not self.running:
            self._loop_task = asyncio.create_task(self._loop(), name="warroom-loop")

    def _ingest_kb(self) -> None:
        try:
            self._kb_ok = self.c.kb.ingest() > 0
        except Exception as e:  # noqa: BLE001 - reported via health
            log.warning("kb ingest failed: %s", e)
            self._kb_ok = False

    async def stop(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loop_task = None
        await self._cancel_events()

    async def _cancel_events(self) -> None:
        tasks = [t for t in self._tasks.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _loop(self) -> None:
        owed = 0.0
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.tick_wall_s)
            now = time.monotonic()
            wall, last = now - last, now
            if self.paused:
                continue
            if self.speed > 1 and self.compute_busy():
                # accelerated demo: the plant waits for the LLM / solver (wall time is not owed)
                self._publish_periodic()
                continue
            owed += wall * self.speed
            n = min(int(owed), self.max_steps_per_iter)
            owed = 0.0 if int(owed) > self.max_steps_per_iter else owed - n
            try:
                for _ in range(n):
                    if self.speed > 1 and self.compute_busy():
                        break
                    await self.tick(1.0, publish=False, hold_compute=False)
                self._publish_periodic()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the plant loop alive; the watchdog restarts the process
                log.exception("tick failed")

    def _publish_periodic(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_machines_pub >= 0.5:
            self._last_machines_pub = now
            self.bus.publish(WsType.MACHINES, {"machines": [t.model_dump(mode="json") for t in self.machine_tiles()]})
            self.bus.publish(WsType.CLOCK, self.clock_info())
        if force or now - self._last_snapshot_pub >= 5.0:
            self._last_snapshot_pub = now
            self.bus.publish(WsType.SNAPSHOT, self.snapshot())

    # ------------------------------------------------------------------ the pipeline
    def compute_busy(self) -> bool:
        """True while the commander (LLM) or the solver is working on an event."""
        return getattr(self.state_machine, "compute_inflight", 0) > 0 or getattr(self.c.tools, "inflight", 0) > 0

    async def tick(self, dt_s: float = 1.0, *, publish: bool = True, hold_compute: bool = True) -> None:
        """One sim step. hold_compute: while the LLM or solver is running, sim time waits for it, so
        an accelerated demo (or a test driving tick() flat out) keeps the page-11 beats: decisions
        land seconds after detection instead of the plant racing ahead during a 3 s solve."""
        if hold_compute:
            waited = 0.0
            while self.compute_busy() and waited < self.compute_hold_max_s:
                await asyncio.sleep(0.01)
                waited += 0.01
        before = self.clock.now()
        readings: list[SensorReading] = self.sim.step(dt_s)
        if self.clock.now() == before:          # sim did not move the clock itself
            self.clock.advance(dt_s)
        self.ticks += 1
        # RED path: deterministic and synchronous, before any detection or LLM work.
        states = {m.machine_id: m for m in self.sim.machines()}
        for a in self.c.safety.check(readings, states):
            self.sim.apply_safety_action(a)
            self.safety_log.append(a)
            del self.safety_log[:-100]
            on_safety = getattr(self.state_machine, "on_safety_action", None)
            if callable(on_safety):           # state machine audits it and attaches it to the open event
                on_safety(a)
            else:
                self.audit.append(AuditKind.SAFETY_ACTION, "safety", a.model_dump(mode="json"),
                                  self._open_event_id(a.machine_id))
            self.bus.publish(WsType.SAFETY_ACTION, a)
        self._track_baseline(readings)
        for an in self.c.detector.ingest(readings):
            self._on_anomaly(an)
        self._wake_sleepers()
        if publish:
            self._publish_periodic(force=True)
        await asyncio.sleep(0)                   # let event tasks progress between sim seconds

    def _on_anomaly(self, an: Anomaly) -> None:
        if an.detection_latency_s is None:
            try:
                started = self.sim.fault_started_at(an.machine_id)
            except Exception:  # noqa: BLE001
                started = None
            if started:
                an = an.model_copy(update={"detection_latency_s": round((an.ts - started).total_seconds(), 1)})
        self.anomalies.append(an)
        del self.anomalies[:-100]
        self.bus.publish(WsType.ANOMALY, an)
        if callable(getattr(self.state_machine, "on_safety_action", None)):
            # full state machine: it audits the anomaly and attaches repeats to the open event itself
            self._spawn(an)
            return
        open_id = self._open_event_id(an.machine_id)
        if open_id:
            self.audit.append(AuditKind.ANOMALY, "detector",
                              {**an.model_dump(mode="json"), "note": "attached to open event, no new wake"}, open_id)
            return
        self.audit.append(AuditKind.ANOMALY, "detector", an.model_dump(mode="json"))
        self._spawn(an)

    def _spawn(self, an: Anomaly) -> None:
        task = asyncio.create_task(self.state_machine.handle(an), name=f"event-{an.id}")
        self._tasks[an.id] = task
        task.add_done_callback(lambda t, aid=an.id: self._event_done(aid, t))

    async def sim_sleep(self, seconds: float) -> None:
        """Sleep in SIM time: resolves on the first tick at or after now+seconds (paused = no progress).
        Handed to the state machine so VERIFY reads happen at exact sim seconds at any speed."""
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.clock.now() + timedelta(seconds=seconds), next(self._sleeper_seq), fut))
        await fut

    def _wake_sleepers(self, cancel: bool = False) -> None:
        now = self.clock.now()
        while self._sleepers and (cancel or self._sleepers[0][0] <= now):
            _, _, fut = heapq.heappop(self._sleepers)
            if not fut.done():
                fut.cancel() if cancel else fut.set_result(None)

    def _event_done(self, anomaly_id: str, t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("event task for %s crashed: %r", anomaly_id, exc)
            try:
                self.audit.append(AuditKind.SYSTEM, "system", {"msg": "event task crashed", "anomaly_id": anomaly_id,
                                                               "error": repr(exc)[:500]})
            except Exception:  # noqa: BLE001
                pass

    async def wait_events(self, timeout_s: float = 10.0) -> None:
        """Wait until every running event task finishes (tests / CLI demo)."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout_s)

    def _open_event_id(self, machine_id: str) -> str | None:
        for ev in self.events():
            if ev.machine_id == machine_id and ev.state not in TERMINAL_STATES:
                return ev.id
        return None

    def _track_baseline(self, readings: list[SensorReading]) -> None:
        for r in readings:
            if r.machine_id not in self._baseline:
                self._baseline[r.machine_id] = {c.value: getattr(r, c.value) for c in Channel
                                                if getattr(r, c.value) is not None}

    # ------------------------------------------------------------------ control
    async def run_seconds(self, seconds: float) -> None:
        """Advance sim time deterministically (demo jump / tests)."""
        whole = int(seconds)
        for _ in range(whole):
            await self.tick(1.0, publish=False)
        if seconds - whole > 0:
            await self.tick(seconds - whole, publish=False)
        self._publish_periodic(force=True)

    async def load_scenario(self, name: ScenarioName) -> None:
        if self.builder is None:
            raise RuntimeError("runtime was built from injected components; cannot rebuild")
        await self._cancel_events()
        self._wake_sleepers(cancel=True)
        self.scenario = ScenarioName(name)
        self.c = self.builder.build(self.scenario, sleep=self.sim_sleep)
        self.safety_log.clear()
        self.anomalies.clear()
        self._baseline.clear()
        self.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "scenario loaded", "scenario": self.scenario.value,
                                                           "factory_time": self.clock.now().isoformat(),
                                                           "degraded": self.degraded})
        self._publish_periodic(force=True)

    async def reset(self) -> None:
        await self.load_scenario(self.scenario)

    def set_speed(self, speed: float) -> None:
        if not 0 < speed <= 3600:
            raise ValueError("speed must be in (0, 3600]")
        self.speed = speed
        self.clock.speed = speed

    def pause(self) -> None:
        self.paused = True

    def play(self) -> None:
        self.paused = False

    def inject_fault(self, fault: FaultSpec) -> None:
        if fault.machine_id not in F.MACHINE_BY_ID:
            raise KeyError(fault.machine_id)
        self.sim.inject_fault(fault)
        self.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "fault injected", "fault": fault.model_dump(mode="json")})

    def clear_faults(self, machine_id: str | None = None) -> None:
        self.sim.clear_faults(machine_id)
        self.c.detector.reset(machine_id)
        self.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "faults cleared", "machine_id": machine_id})

    def clock_info(self) -> dict[str, Any]:
        return {"factory_time": self.clock.now().isoformat(), "speed": self.speed, "paused": self.paused,
                "scenario": self.scenario.value}

    # ------------------------------------------------------------------ read models
    @property
    def llm_mode(self) -> LLMMode:
        mode = getattr(self.c.commander, "mode", None)
        return LLMMode(mode) if mode else LLMMode(self.settings.llm_mode)

    def events(self) -> list[Event]:
        try:
            return list(self.state_machine.events())
        except Exception:  # noqa: BLE001
            return []

    def all_approvals(self) -> list[ApprovalRequest]:
        a = self.approvals
        if hasattr(a, "all"):
            return list(a.all())
        seen: dict[str, ApprovalRequest] = {}
        for ev in self.events():
            for rid in ev.approvals:
                try:
                    seen[rid] = a.get(rid)
                except Exception:  # noqa: BLE001
                    pass
        for r in a.pending():
            seen[r.id] = r
        return sorted(seen.values(), key=lambda r: r.created_at)

    def _effective_status(self, m: MachineState) -> MachineStatus:
        if m.status in (MachineStatus.RUNNING, MachineStatus.IDLE, MachineStatus.WARNING) and \
                self._open_event_id(m.machine_id):
            return MachineStatus.ALARM
        return m.status

    def machine_tiles(self) -> list[MachineTile]:
        by_id = {m.machine_id: m for m in self.sim.machines()}
        out = []
        for d in F.MACHINES:
            m = by_id[d.id]
            out.append(MachineTile(machine_id=d.id, line=d.line, short_name=d.short_name,
                                   status=self._effective_status(m),
                                   temp_c=m.last_reading.temp_c if m.last_reading else None))
        return out

    def machine_states(self, line: str | None = None) -> list[MachineState]:
        order = {mid: i for i, mid in enumerate(F.MACHINE_IDS)}
        ms = sorted(self.sim.machines(), key=lambda m: order.get(m.machine_id, 999))
        if line:
            ms = [m for m in ms if m.line.value == line]
        return [m.model_copy(update={"status": self._effective_status(m)}) for m in ms]

    def lines(self) -> list[dict[str, Any]]:
        tiles = self.machine_tiles()
        return [{"line": ln.value, "label": label,
                 "machines": [t.model_dump(mode="json") for t in tiles if t.line == ln]}
                for ln, label in F.LINE_LABELS.items()]

    def tiles_for(self, m: MachineState, ev: Event | None = None) -> list[MetricTile]:
        r = m.last_reading
        base = self._baseline.get(m.machine_id, {})
        alarm_ch = ev.anomaly.channel if ev else None
        is_press = F.MACHINE_BY_ID[m.machine_id].line.value == "CURE"
        tiles: list[MetricTile] = []
        t = r.temp_c if r else None
        if t is None:
            tiles.append(MetricTile(label="溫度", value="—", status=TileStatus.WARN))
        else:
            hot = (m.compound_stage is CompoundStage.UNVULCANIZED and t >= F.COMPOUND_STOP_C - 10) or \
                  (m.compound_stage is CompoundStage.CURATIVE_ADDED and t >= F.CURATIVE_STOP_C - 5)
            cold = m.cure_job is not None and t < F.CURE_TEMP_NOMINAL_C - 3
            st = TileStatus.ALARM if (alarm_ch is Channel.TEMP or hot or cold) else TileStatus.NORMAL
            txt = f"{t:.0f}°C" if abs(t - round(t)) < 0.05 else f"{t:.1f}°C"
            tiles.append(MetricTile(label="溫度", value=txt, status=st))
        tiles.append(self._level_tile("壓力", r.pressure_kgf_cm2 if r else None, base.get(Channel.PRESSURE.value),
                                      alarm_ch is Channel.PRESSURE))
        tiles.append(MetricTile(label="時間", value=f"{m.step_elapsed_min:.0f}分", status=TileStatus.NORMAL))
        if is_press:
            tiles.append(self._level_tile("蒸汽", r.steam_kgf_cm2 if r else None, base.get(Channel.STEAM.value),
                                          alarm_ch is Channel.STEAM))
        else:
            tiles.append(self._level_tile("水流", r.water_flow_lpm if r else None, base.get(Channel.WATER_FLOW.value),
                                          alarm_ch is Channel.WATER_FLOW))
        return tiles

    @staticmethod
    def _level_tile(label, value, baseline, flagged) -> MetricTile:
        if value is None:
            return MetricTile(label=label, value="—", status=TileStatus.NORMAL)
        if flagged or (baseline and value < 0.85 * baseline):
            return MetricTile(label=label, value="偏低", status=TileStatus.ALARM)
        if baseline and value > 1.15 * baseline:
            return MetricTile(label=label, value="偏高", status=TileStatus.WARN)
        return MetricTile(label=label, value="正常", status=TileStatus.NORMAL)

    def machine_detail(self, machine_id: str, minutes: float = 30.0) -> MachineDetail:
        m = self.sim.machine(machine_id)
        m = m.model_copy(update={"status": self._effective_status(m)})
        ev = self._event_obj(self._open_event_id(machine_id))
        hist = self.sim.history(machine_id, minutes)
        hist = hist[::max(1, len(hist) // 180)]
        try:
            pred = self.c.detector.predict(machine_id, Channel.TEMP, 1800)
        except Exception:  # noqa: BLE001 - not enough data yet
            pred = None
        return MachineDetail(state=m, tiles=self.tiles_for(m, ev), history=hist, prediction=pred)

    def _event_obj(self, event_id: str | None) -> Event | None:
        if not event_id:
            return None
        return next((ev for ev in self.events() if ev.id == event_id), None)

    def focus_event(self) -> Event | None:
        evs = self.events()
        if not evs:
            return None
        open_ = [e for e in evs if e.state not in TERMINAL_STATES]
        return max(open_ or evs, key=lambda e: e.opened_at)

    def focus_panel(self, ev: Event | None = None) -> FocusPanel | None:
        ev = ev or self.focus_event()
        if ev is None:
            return None
        d = F.MACHINE_BY_ID[ev.machine_id]
        m = self.sim.machine(ev.machine_id)
        if d.line.value == "CURE":
            where = f"{int(d.short_name)} 號{d.name}"
        elif d.line.value == "BOND":
            where = f"貼合{d.name}"
        else:
            where = f"{d.line.value} 線{d.name}"
        title = f"異常：{where}　{KIND_TEXT.get(ev.anomaly.kind.value, ev.anomaly.kind.value)}"
        hyp = next((h for h in reversed(ev.hypotheses) if h.status.value in ("active", "confirmed")), None) or \
            (ev.plan.hypothesis if ev.plan else None)
        detail = ev.anomaly.message
        if m.compound_stage is not CompoundStage.NONE:
            detail += f"（未加硫上限 {F.COMPOUND_STOP_C:.0f}°C；已加硫另設 {F.CURATIVE_STOP_C:.0f}°C 防焦燒）"
        elif d.line.value == "CURE":
            detail += f"（硫化溫度下限 {F.CURE_LOW_STOP_C:.0f}°C）"
        if hyp is not None:
            detail += f"；原因：{CAUSE_TEXT.get(hyp.cause_code, hyp.statement)}"
        auto_done, pending = self._actions_for(ev)
        return FocusPanel(event_id=ev.id, machine_id=ev.machine_id, title=title, detail=detail,
                          tiles=self.tiles_for(m, ev), auto_done=auto_done, pending=pending,
                          schedule_impact=self.schedule_impact(ev), state=ev.state, timeline=ev.timeline)

    def _actions_for(self, ev: Event) -> tuple[list[str], list[PendingAction]]:
        auto: list[str] = []
        for sr in ev.steps:
            if sr.step.tool in (ToolName.NOTIFY_TEAMS, ToolName.SET_CURE_TIME, ToolName.CONFIRM_SCHEDULE) and \
                    sr.state == "ok" and (sr.gate is None or sr.gate.risk in (RiskLevel.R0_READ, RiskLevel.R1_NOTIFY)):
                auto.append(sr.step.purpose)
        for sa in ev.safety_actions or [a for a in self.safety_log if a.machine_id == ev.machine_id]:
            txt = f"安全核心已處置：{sa.message}"
            if txt not in auto:
                auto.append(txt)
        pending: list[PendingAction] = []
        for req in self.approvals.pending():
            if req.event_id != ev.id:
                continue
            if req.options:
                pending += [PendingAction(approval_id=req.id, option_id=o.id, label=o.label) for o in req.options]
            else:
                pending.append(PendingAction(approval_id=req.id, label=req.title))
        return auto, pending

    def schedule_impact(self, ev: Event) -> str:
        for sr in reversed(ev.steps):
            if sr.step.tool is ToolName.SOLVE_SCHEDULE and sr.result and sr.result.ok:
                data = sr.result.data
                moves = [mv for mv in data.get("moves") or [] if mv.get("from_press") != mv.get("to_press")]
                if moves:
                    from warroom.agent.narrate import machine_label
                    parts = [f"{mv.get('order_id')} 移至 {machine_label(str(mv.get('to_press')))}" for mv in moves[:3]]
                    late = data.get("late_orders") or []
                    tail = "交期不受影響" if data.get("deliveries_met", not late) else f"延誤：{'、'.join(late)}"
                    return "；".join(parts) + f"；{tail}"
        try:
            semi = self.sim.semi_finished()
        except Exception:  # noqa: BLE001
            return "無"
        if semi.planned_mold_in_at:
            return f"無，{semi.tire_label} 仍 {semi.planned_mold_in_at:%H:%M} 入硫化機"
        return "無"

    def header(self) -> HeaderStatus:
        tiles = self.machine_tiles()
        normal = sum(1 for t in tiles if t.status in NORMAL_STATUSES)
        return HeaderStatus(normal_count=normal, abnormal_count=len(tiles) - normal, factory_time=self.clock.now(),
                            llm_mode=self.llm_mode, network=self.network)

    def snapshot(self) -> DashboardSnapshot:
        evs = sorted(self.events(), key=lambda e: e.opened_at, reverse=True)
        open_ = [e for e in evs if e.state not in TERMINAL_STATES]
        closed = [e for e in evs if e.state in TERMINAL_STATES][:5]
        return DashboardSnapshot(header=self.header(), orders=self.sim.order_progress(),
                                 semi_finished=self.sim.semi_finished(), focus=self.focus_panel(),
                                 machines=self.machine_tiles(), events=open_ + closed,
                                 pending_approvals=list(self.approvals.pending()),
                                 safety_actions=list(reversed(self.safety_log[-20:])))

    def labor_view(self) -> dict[str, Any]:
        """Schedule & labor (page 6): shifts, labor needs of the active schedule, per-shift load and gaps."""
        shifts = self.sim.shifts()
        jobs, needs = [], []
        if self.c.store is not None:
            try:
                jobs = self.c.store.current().schedule.jobs
                needs = [n for j in jobs for n in j.labor]
            except Exception:  # noqa: BLE001
                pass
        load = []
        for sh in shifts:
            inside = [n for n in needs if n.team == sh.team and sh.start <= n.start < sh.end]
            peak = max((sum(x.people for x in inside if x.start <= n.start < x.end) for n in inside), default=0)
            req = max(sh.required or 0, peak)
            load.append({"shift_id": sh.id, "team": sh.team.value, "team_label": F.TEAM_LABELS[sh.team],
                         "name": sh.name, "start": sh.start.isoformat(), "end": sh.end.isoformat(),
                         "headcount": sh.headcount, "required": req, "tasks": len(inside),
                         "gap": max(0, req - sh.headcount)})
        demolds = [{"job_id": j.id, "order_id": j.order_id, "press_id": j.press_id, "size": j.size.value,
                    "mold_in": j.mold_in.isoformat(), "demold": j.demold.isoformat()} for j in jobs]
        return {"shifts": [s.model_dump(mode="json") for s in shifts],
                "needs": [n.model_dump(mode="json") for n in needs], "load": load, "demolds": demolds,
                "gaps": [f"{x['team_label']} {x['name']} 缺 {x['gap']} 人" for x in load if x["gap"]]}

    # ------------------------------------------------------------------ health
    def kb_ok(self) -> bool:
        return bool(self._kb_ok) and "kb" not in self.c.degraded

    async def llm_reachable(self) -> bool:
        if self.llm_mode is LLMMode.MOCK:
            return False
        ts, ok = self._llm_probe
        if time.monotonic() - ts < 10:
            return ok
        try:
            import httpx
            async with httpx.AsyncClient(timeout=1.5) as cli:
                r = await cli.get(self.settings.base_url.rstrip("/") + "/models",
                                  headers={"Authorization": f"Bearer {self.settings.api_key_effective}"})
                ok = r.status_code < 500
        except Exception:  # noqa: BLE001 - offline is a valid state
            ok = False
        self._llm_probe = (time.monotonic(), ok)
        return ok

    def uptime_s(self) -> float:
        return time.monotonic() - self.started_wall

    def now(self) -> datetime:
        return self.clock.now()
