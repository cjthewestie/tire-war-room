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
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
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
    ApprovalStatus,
    ApprovedAction,
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
             "multi_machine": "多機同時異常", "schedule_slip": "排程延誤"}


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
        if self.use_real:
            # Never swap the append-only log for memory: JsonlAuditLog repairs a torn last line and
            # archives a broken chain itself, so a failure here is a real disk fault -> fail closed.
            return real()
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
    def build(self, scenario: ScenarioName, sleep: Callable[[float], Any] | None = None,
              fresh_store: bool = True, staged: bool = False) -> Components:
        """fresh_store=False (process start) reopens the persisted schedule store of the same
        scenario, so a watchdog restart or an offline update keeps approved versions and the
        rollback history (page 10). An explicit scenario load or reset starts a fresh store.

        staged=True (live scenario switch): the fresh store is built in a staging folder and the
        live folder is left alone; Runtime commits it with commit_store() only after the whole
        build succeeded, or throws it away with discard_staging()."""
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

        store = self._store(sim, scenario, fresh_store, staged=staged)
        attach_sim = getattr(sim, "attach_plan", None)
        if callable(attach_sim):                # the plant loads presses from the ACTIVE schedule
            attach_sim(store)
        attach_plan = getattr(detector, "attach_plan", None)
        if callable(attach_plan):               # page 6: compare plan with actual every minute
            attach_plan(store=store, sim=sim)

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

    @property
    def live_root(self) -> Path:
        return self.s.schedule_dir / "live"

    @property
    def staging_root(self) -> Path:
        return self.s.schedule_dir / "live.staging"

    def discard_staging(self) -> None:
        shutil.rmtree(self.staging_root, ignore_errors=True)

    def commit_store(self, old_store: Any, new_store: Any) -> None:
        """Swap a staged fresh store in as the live one. The old store is detached first, under its
        own lock (so a solver thread of a cancelled event that is mid-write finishes first): its root
        points at a folder that no longer exists, so any late write from that thread fails instead of
        landing in the new live store (the cause of 'live has no ACTIVE version')."""
        live, staging = self.live_root, self.staging_root
        new_root = getattr(new_store, "root", None)
        if new_root is None or Path(new_root) != staging:
            return                                   # memory fallback store: nothing on disk to swap
        lock = getattr(old_store, "_lock", None)
        dead = self.s.schedule_dir / f"live.retired-{time.monotonic_ns()}"
        with (lock if lock is not None else _NoLock()):
            if old_store is not None and getattr(old_store, "root", None) is not None:
                try:
                    old_store.root = dead
                except Exception:  # noqa: BLE001
                    pass
            if live.exists():
                os.replace(live, dead)
            os.replace(staging, live)
            new_store.root = live
        shutil.rmtree(dead, ignore_errors=True)

    def _store(self, sim, scenario: ScenarioName | None = None, fresh: bool = True, staged: bool = False):
        root = self.staging_root if (staged and fresh) else self.live_root
        marker = root / "scenario.txt"

        def real():
            from warroom.schedule import FileScheduleStore
            if not fresh and scenario is not None and any(root.glob("v*.json")) and marker.is_file() and \
                    marker.read_text(encoding="utf-8").strip() == scenario.value:
                try:
                    store = FileScheduleStore(root)
                    self.reopened_store = True
                    return store
                except Exception as e:  # noqa: BLE001 - corrupt store: start fresh, say why
                    log.warning("schedule store %s unreadable (%s); starting fresh", root, e)
            self.reopened_store = False
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True, exist_ok=True)
            store = FileScheduleStore(root, initial=self._initial_schedule(sim))
            if scenario is not None:
                marker.write_text(scenario.value, encoding="utf-8")
            return store
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


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


HOLD_STATUSES = (MachineStatus.OUT_OF_SERVICE, MachineStatus.STOPPED)
HOLDS_FILE = "holds.json"


class _NullAudit:
    """Swallows what a cancelled event task writes during shutdown (it was already audited as
    interrupted; a cancellation is not a factory decision)."""

    def append(self, *a, **k):
        return None

    def __getattr__(self, name):
        return lambda *a, **k: None


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
        self.c: Components = components or self.builder.build(self.scenario, sleep=self.sim_sleep, fresh_store=False)
        from warroom.api.ingest import IngestPipeline
        self.ingest = IngestPipeline.from_env(self.settings.audit_path.parent)
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
        self._switching = False                  # a scenario load is swapping components: no ticks
        self._switch_lock: asyncio.Lock | None = None
        self._holds: dict[str, dict[str, Any]] = {}
        self._oob_seen: dict[str, datetime] = {}
        if components is None and getattr(self.builder, "reopened_store", False):
            self._reapply_holds()                # watchdog restart: press 7 stays out of service
        self._holds = self._sim_holds()

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
        try:
            self._sync_holds()
        except Exception:  # noqa: BLE001 - never block a shutdown
            log.exception("persist holds")
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._loop_task = None
        await self._cancel_events(shutdown=True)

    async def _cancel_events(self, shutdown: bool = False) -> None:
        """shutdown=True (process stop / watchdog restart): open events are recorded as INTERRUPTED
        in the audit and left as they were. The schedule store and audit are detached from the state
        machine first, so cancelling the tasks cannot safe-stop the events or roll back an approved
        schedule. A scenario load (shutdown=False) cancels them normally."""
        tasks = [t for t in self._tasks.values() if not t.done()]
        if shutdown and tasks:
            open_evs = [e for e in self.events() if e.state not in TERMINAL_STATES]
            cur = None
            try:
                cur = self.c.store.current().version if self.c.store is not None else None
            except Exception:  # noqa: BLE001
                pass
            for ev in open_evs:
                self.audit.append(AuditKind.SYSTEM, "system",
                                  {"msg": "event interrupted by shutdown", "state": ev.state.value,
                                   "schedule_version_kept": cur}, event_id=ev.id)
        saved: dict[str, Any] = {}
        sm = self.state_machine
        if shutdown and tasks:
            for attr, stand_in in (("store", None), ("audit", _NullAudit())):
                if hasattr(sm, attr):
                    try:
                        saved[attr] = getattr(sm, attr)
                        setattr(sm, attr, stand_in)
                    except Exception:  # noqa: BLE001
                        saved.pop(attr, None)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for attr, v in saved.items():
            setattr(sm, attr, v)
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
                # accelerated demo: the simulated plant's clock waits for the LLM / solver (wall time
                # is not owed). No sim second passes, so no reading goes unchecked: every reading the
                # sim produces still goes through tick() -> safety_pass() first.
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
        try:
            self._sync_holds()
        except Exception:  # noqa: BLE001 - persistence must never break the loop
            log.exception("persist holds")
        if self.network == "lan_down" or getattr(self, "_light_on", False):
            try:
                self.sync_warning_light()
            except Exception:  # noqa: BLE001 - the light must never break the loop
                log.exception("warning light")
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
        """One sim step: readings -> RED path (safety core, synchronous) -> MQTT hop + validation +
        time-series DB -> detection -> wake events.

        hold_compute: while the LLM or solver is running, the NEXT sim second waits for it, so an
        accelerated demo (or a test driving tick() flat out) keeps the page-11 beats. The wait sits
        at the END of the tick, after this second's readings went through the safety core: the red
        path never waits on the LLM (page 5); only the simulated plant's clock does."""
        if self._switching:                     # components are being swapped (load_scenario)
            return
        before = self.clock.now()
        readings: list[SensorReading] = self.sim.step(dt_s)
        if self.clock.now() == before:          # sim did not move the clock itself
            self.clock.advance(dt_s)
        self.ticks += 1
        self.safety_pass(readings)              # RED path first: nothing below can delay it
        readings = self.ingest.transport(readings)
        self.ingest.record(readings)
        self._track_baseline(readings)
        for an in self.c.detector.ingest(readings):
            self._on_anomaly(an)
        self._track_after_close()
        self._wake_sleepers()
        if publish:
            self._publish_periodic(force=True)
        await asyncio.sleep(0)                   # let event tasks progress between sim seconds
        if hold_compute:
            await self._hold_for_compute()

    async def _hold_for_compute(self) -> float:
        """The sim plant's clock waits (wall time, bounded) while the commander or solver works."""
        t0 = time.monotonic()
        while self.compute_busy() and time.monotonic() - t0 < self.compute_hold_max_s:
            await asyncio.sleep(0.01)
        return time.monotonic() - t0

    def safety_pass(self, readings: list[SensorReading]) -> list[SafetyAction]:
        """RED path: deterministic and synchronous, never awaits. Runs on every batch of readings
        before any hold, MQTT hop, detection or LLM work (page 5: acts directly, not via the LLM)."""
        states = {m.machine_id: m for m in self.sim.machines()}
        acts = list(self.c.safety.check(readings, states))
        for a in acts:
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
        return acts

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
        """Switch scenario as one step: stop the plant loop, cancel and await every event task, build
        the new components with the schedule store in a staging folder, then swap. If the build
        fails, the old components, clock and live store are kept as they were (never a new clock with
        the old events) and the error is raised."""
        if self.builder is None:
            raise RuntimeError("runtime was built from injected components; cannot rebuild")
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        async with self._switch_lock:
            new_scenario = ScenarioName(name)
            was_running = self.running
            self._switching = True
            try:
                if self._loop_task is not None:
                    self._loop_task.cancel()
                    try:
                        await self._loop_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
                    self._loop_task = None
                for _ in range(20):              # a cancelled task may not spawn more, but be sure
                    if not any(not t.done() for t in self._tasks.values()):
                        break
                    await self._cancel_events()
                self._tasks.clear()
                self._wake_sleepers(cancel=True)
                saved_now = self.clock.now()
                saved_degraded = dict(self.builder.degraded)
                try:
                    comps = self.builder.build(new_scenario, sleep=self.sim_sleep, fresh_store=True, staged=True)
                    self.builder.commit_store(self.c.store, comps.store)
                except Exception as e:
                    self.builder.discard_staging()
                    self.builder.degraded.clear()
                    self.builder.degraded.update(saved_degraded)
                    if self.clock.now() != saved_now:
                        self.clock.reset(saved_now)
                    log.exception("scenario load %s failed; previous scenario kept", new_scenario.value)
                    try:
                        self.audit.append(AuditKind.SYSTEM, "system",
                                          {"msg": "scenario load failed; previous scenario kept",
                                           "scenario": new_scenario.value, "kept": self.scenario.value,
                                           "error": f"{type(e).__name__}: {e}"[:300]})
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                self.scenario = new_scenario
                self.c = comps
                self.ingest.reset()
                self.safety_log.clear()
                self.anomalies.clear()
                self._baseline.clear()
                self._oob_seen.clear()
                self._holds = self._sim_holds()
                self.__dict__.pop("_wip", None)
                self.audit.append(AuditKind.SYSTEM, "supervisor",
                                  {"msg": "scenario loaded", "scenario": self.scenario.value,
                                   "factory_time": self.clock.now().isoformat(), "degraded": self.degraded})
            finally:
                self._switching = False
                if was_running and self.run_loop and not self.running:
                    self._loop_task = asyncio.create_task(self._loop(), name="warroom-loop")
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
        seen = self._oob_seen.get(m.machine_id)
        if m.status in NORMAL_STATUSES and seen is not None and \
                (self.clock.now() - seen).total_seconds() <= self.OOB_RELEASE_S:
            # its event closed but the readings are not back in band (e.g. cooling water still low):
            # still 異常（處置中）, so the header never says 正常 next to a red tile
            return MachineStatus.WARNING
        return m.status

    OOB_RELEASE_S = 60.0       # back in band this long (sim s) after a closed event -> 正常 again

    def _track_after_close(self) -> None:
        """Per tick, for machines whose latest event is closed: remember when a reading was last out
        of band. Only those machines: the plain tile rule is too twitchy on small channels to drive
        the header for the whole plant, and a machine with an open event is ALARM anyway."""
        latest: dict[str, Event] = {}
        for ev in self.events():
            cur = latest.get(ev.machine_id)
            if cur is None or ev.opened_at > cur.opened_at:
                latest[ev.machine_id] = ev
        now = self.clock.now()
        for mid, ev in latest.items():
            if ev.state not in TERMINAL_STATES:
                continue
            try:
                m = self.sim.machine(mid)
            except Exception:  # noqa: BLE001
                continue
            if self._out_of_band(m):
                self._oob_seen[mid] = now

    def _out_of_band(self, m: MachineState) -> bool:
        """Same rule as the red tiles (tiles_for): a live reading past the alarm line."""
        if m.last_reading is None:
            return False
        try:
            return any(t.status is TileStatus.ALARM for t in self.tiles_for(m))
        except Exception:  # noqa: BLE001
            return False

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
        return [self._live_note(m.model_copy(update={"status": self._effective_status(m)})) for m in ms]

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
        m = self._live_note(m.model_copy(update={"status": self._effective_status(m)}))
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
        where = self._where(ev.machine_id)
        title = f"異常：{where}　{KIND_TEXT.get(ev.anomaly.kind.value, ev.anomaly.kind.value)}"
        hyp = next((h for h in reversed(ev.hypotheses) if h.status.value in ("active", "confirmed")), None) or \
            (ev.plan.hypothesis if ev.plan else None)
        detail = self._live_detail(ev, m, where)
        if m.compound_stage is not CompoundStage.NONE:
            detail += f"（未加硫上限 {F.COMPOUND_STOP_C:.0f}°C；已加硫另設 {F.CURATIVE_STOP_C:.0f}°C 防焦燒）"
        elif d.line.value == "CURE":
            detail += f"（硫化溫度下限 {F.CURE_LOW_STOP_C:.0f}°C）"
        if hyp is not None:
            detail += f"；原因：{CAUSE_TEXT.get(hyp.cause_code, hyp.statement)}"
        auto_done, pending = self._actions_for(ev)
        return FocusPanel(event_id=ev.id, machine_id=ev.machine_id, title=title, detail=detail,
                          tiles=self.tiles_for(m, ev), auto_done=auto_done, pending=pending,
                          schedule_impact=self.schedule_impact(ev), state=ev.state, timeline=ev.timeline,
                          approved=self._approved_for(ev))

    @staticmethod
    def _where(machine_id: str) -> str:
        d = F.MACHINE_BY_ID[machine_id]
        if d.line.value == "CURE":
            return f"{int(d.short_name)} 號{d.name}"
        if d.line.value == "BOND":
            return f"貼合{d.name}"
        return f"{d.line.value} 線{d.name}"

    def _live_note(self, m: MachineState) -> MachineState:
        """The drawer banner (MachineState.note): the state machine stores the detector's wake-time
        message there, whose forecast (e.g. '約 6 分後達 120°C') can be disproved minutes later.
        Replace it with the same live sentence and forecast the focus panel uses, labelled."""
        if not m.note:
            return m
        ev = next((e for e in sorted(self.events(), key=lambda e: e.opened_at, reverse=True)
                   if e.machine_id == m.machine_id and e.anomaly.message and m.note.startswith(e.anomaly.message)),
                  None)
        if ev is None or ev.anomaly.channel is not Channel.TEMP or m.last_reading is None or \
                m.last_reading.temp_c is None:
            return m
        live = self._live_detail(ev, m, self._where(m.machine_id))
        if ev.state not in TERMINAL_STATES:
            live = f"事件處置中（{ev.anomaly.ts:%H:%M} 喚醒）；現況：{live}"
        return m.model_copy(update={"note": live})

    def _live_detail(self, ev: Event, m: MachineState, where: str) -> str:
        """Centre-panel sentence built from the LIVE reading at render time (page 2: 膠溫 112°C 且持續
        上升), not the frozen detection-time message. The forecast comes from the detector's
        prediction and stays consistent with it: a rise that settles below the limit says so."""
        r = m.last_reading
        if ev.anomaly.channel is not Channel.TEMP or r is None or r.temp_c is None:
            return ev.anomaly.message
        if ev.state in TERMINAL_STATES:
            # never the frozen wake-time message (its time-to-limit may be disproved by now): the live
            # reading and the current forecast, labelled as the state after closing
            closed = "事件已結案" if ev.state.value == "closed" else "已安全停止"
            return f"{closed}；現況：" + self._live_sentence(m, where)
        return self._live_sentence(m, where)

    def _live_sentence(self, m: MachineState, where: str) -> str:
        r = m.last_reading
        d = F.MACHINE_BY_ID[m.machine_id]
        is_press = d.line.value == "CURE"
        label = "膠溫" if m.compound_stage is not CompoundStage.NONE else ("硫化溫度" if is_press else "溫度")
        t = r.temp_c
        try:
            pred = self.c.detector.predict(m.machine_id, Channel.TEMP, 1800)
        except Exception:  # noqa: BLE001 - not enough data yet
            pred = None
        slope = pred.trend_per_min if pred is not None else 0.0
        if slope > 0.1:
            trend = f"且持續上升（+{slope:.1f}°C/分）"
        elif slope < -0.1:
            trend = f"持續下降（{slope:.1f}°C/分）"
        else:
            trend = "已趨穩定"
        parts = [f"{where} {label} {t:.0f}°C {trend}"]
        base = self._baseline.get(m.machine_id, {})
        wf, wf0 = r.water_flow_lpm, base.get(Channel.WATER_FLOW.value)
        if wf is not None and wf0 and wf < 0.85 * wf0:
            parts[0] += f"，水流偏低 {wf:.0f} L/min（基準 {wf0:.0f}）"
        st, st0 = r.steam_kgf_cm2, base.get(Channel.STEAM.value)
        if is_press and st is not None and st0 and st < 0.85 * st0:
            parts[0] += f"，蒸汽壓偏低 {st:.1f} kgf/cm²（基準 {st0:.1f}）"
        if pred is not None:
            if m.compound_stage is CompoundStage.CURATIVE_ADDED:
                hi = F.CURATIVE_STOP_C
            elif m.compound_stage is CompoundStage.UNVULCANIZED:
                hi = F.COMPOUND_STOP_C
            else:
                hi = None
            p = pred.predicted_value
            if hi is not None and slope > 0.1:
                if p >= hi and t < hi:
                    parts.append(f"預測約 {max(1, round((hi - t) / slope))} 分後達 {hi:.0f}°C 上限")
                elif t < hi:
                    parts.append(f"預測 30 分內穩定於約 {p:.0f}°C，未達 {hi:.0f}°C 上限")
            elif is_press and m.cure_job is not None and slope < -0.1:
                lo = F.CURE_LOW_STOP_C
                if p < lo <= t:
                    parts.append(f"預測約 {max(1, round((t - lo) / -slope))} 分後低於 {lo:.0f}°C 下限")
                elif t >= lo:
                    parts.append(f"預測 30 分內穩定於約 {p:.0f}°C")
        return "；".join(parts)

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

    @staticmethod
    def _approved_label(sr: Any) -> str:
        import re
        p = re.sub(r"[（(]需主管核准[）)]", "", sr.step.purpose).strip()
        p = re.sub(r"^[A-Z] ", "", p)                    # approval-option step "A 夜班加班"
        a, d = sr.step.args, (sr.result.data if sr.result and sr.result.ok else {}) or {}
        if sr.step.tool is ToolName.SET_CURE_TIME:
            h = d.get("total_cure_h", a.get("total_cure_h"))
            if isinstance(h, (int, float)):
                p += f"（{h:.1f} h）"
        elif sr.step.tool is ToolName.CONFIRM_SCHEDULE:
            v = d.get("version", a.get("schedule_version"))
            if isinstance(v, int):
                p += f"（排程 v{v}）"
        return p

    def _approved_for(self, ev: Event) -> list[ApprovedAction]:
        """✓ 已核准: the R2/R3 writes the supervisor approved and that ran, per approval, across every
        plan round (prior_rounds + the current steps). An approval whose writes have not run yet still
        shows once, by the option or title the supervisor approved."""
        reqs = sorted((r for r in self.approvals.all() if r.event_id == ev.id and r.status in
                       (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED)), key=lambda r: r.created_at)
        rounds = [r.steps for r in getattr(ev, "prior_rounds", [])] + [ev.steps]
        out: list[ApprovedAction] = []
        used: set[tuple[int, str]] = set()
        ri = 0
        for req in reqs:
            d = req.decision
            who = d.decided_by if d else "supervisor"
            opt_id = d.option_id if d else None

            def owns(sr: Any) -> bool:      # the option step it spawned, or the gated step it was raised for
                return sr.step.id.startswith(f"{req.id}-") or (
                    sr.step.id == req.step_id and sr.gate is not None
                    and sr.gate.risk in (RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL))
            j = next((k for k in range(ri, len(rounds)) if any(owns(sr) for sr in rounds[k])), None)
            ran: list[Any] = []
            if j is not None:
                ri = j
                ran = [sr for sr in rounds[j] if sr.state == "ok" and sr.gate is not None
                       and sr.gate.risk in (RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL)
                       and (sr.step.id.startswith(f"{req.id}-") or not sr.step.id.startswith("AP-"))
                       and (j, sr.step.id) not in used]
            for sr in ran:
                used.add((j, sr.step.id))
                out.append(ApprovedAction(approval_id=req.id, option_id=opt_id, label=self._approved_label(sr),
                                          decided_by=who, status=req.status))
            if not ran:
                opt = next((o for o in req.options if o.id == opt_id), None) or \
                    (req.options[0] if len(req.options) == 1 else None)
                out.append(ApprovedAction(approval_id=req.id, option_id=opt_id,
                                          label=opt.label if opt else req.title, decided_by=who, status=req.status))
        return out

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
                return "無，重排後交期全部滿足" if data.get("deliveries_met", True) else \
                    f"延誤：{'、'.join(data.get('late_orders') or [])}"
        # No solver result yet. A press event, or any plan that schedules a solve, is still being
        # computed; the page-2 "大胎 #07 仍 17:00 入硫化機" sentence belongs only to the
        # event on the big open mill, where #07's A part is preheating (open_mill_overheat).
        solve_runs = [sr for sr in ev.steps if sr.step.tool is ToolName.SOLVE_SCHEDULE]
        plans_solve = bool(solve_runs) or bool(ev.plan and any(
            st.tool is ToolName.SOLVE_SCHEDULE for st in ev.plan.steps))
        is_press = F.MACHINE_BY_ID[ev.machine_id].line.value == "CURE"
        if plans_solve or (is_press and ev.plan is None):
            if ev.state in TERMINAL_STATES or (solve_runs and all(
                    sr.state in ("failed", "skipped", "rejected") for sr in solve_runs)):
                return "保留原排程（未重排）"
            return "計算中…"
        preheat = next((p[2] for p in F.PROCESSES if p[1] == "預熱"), None)
        if ev.machine_id != preheat:        # #07's A part is preheating on the big open mill (page 2)
            return "無，排程未變更"
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
                            llm_mode=self.llm_mode, network=self.network, warning_light=self.warning_light())

    def warning_light(self) -> bool:
        """Page 9 tier 3: with the LAN down the phone cannot be reached, so the on-site warning light
        calls a person to the touch screen whenever an approval is waiting or an event is open."""
        if self.network != "lan_down":
            return False
        if list(self.approvals.pending()):
            return True
        return any(e.state not in TERMINAL_STATES for e in self.events())

    def sync_warning_light(self) -> bool:
        """Publish + audit a change of the warning-light state (called from the loop and the API)."""
        on = self.warning_light()
        if on != getattr(self, "_light_on", False):
            self._light_on = on
            self.audit.append(AuditKind.SYSTEM, "system", {"msg": "warning light", "on": on, "network": self.network})
            self.bus.publish(WsType.SNAPSHOT, self.snapshot())
        return on

    def snapshot(self) -> DashboardSnapshot:
        evs = sorted(self.events(), key=lambda e: e.opened_at, reverse=True)
        open_ = [e for e in evs if e.state not in TERMINAL_STATES]
        closed = [e for e in evs if e.state in TERMINAL_STATES][:5]
        from warroom.api import progress as P
        wip = self.__dict__.setdefault("_wip", P.WipTracker())
        return DashboardSnapshot(header=self.header(), orders=P.order_progress(self.sim, self.events()),
                                 semi_finished=wip.semi_finished(self.sim, self.focus_event()),
                                 focus=self.focus_panel(),
                                 machines=self.machine_tiles(), events=open_ + closed,
                                 pending_approvals=list(self.approvals.pending()),
                                 safety_actions=list(reversed(self.safety_log[-20:])))

    def _active_schedule(self):
        if self.c.store is None:
            return None
        try:
            return self.c.store.current().schedule
        except Exception:  # noqa: BLE001
            return None

    def effective_shifts(self) -> list[Any]:
        """The sim's shifts with the ACTIVE schedule's staffing changes applied (approved overtime or
        transfer: 夜班加班 adds the person to that shift; a transfer also takes one from the source
        team's shift that day, when it has one). This is what /api/shifts and the 缺 N 人 marks use."""
        shifts = list(self.sim.shifts())
        sched = self._active_schedule()
        changes = list(getattr(sched, "staffing_changes", None) or [])
        if not changes:
            return shifts
        out = []
        for sh in shifts:
            add = 0
            for c in changes:
                if c.shift_date == sh.start.date() and c.shift_name == sh.name:
                    if c.to_team == sh.team:
                        add += c.people
                    elif c.kind == "transfer" and c.from_team == sh.team:
                        add -= c.people
            out.append(sh.model_copy(update={"headcount": max(0, sh.headcount + add)}) if add else sh)
        return out

    def staffing_gap_rows(self, shifts: list[Any] | None = None) -> list[tuple[Any, int, int]]:
        """[(shift, ops, people_short)] by the SAME rule the solver reports gaps with
        (warroom.schedule.staffing.staffing_gaps over the active schedule's de-mold / mold-in ops),
        on the effective shifts. Only shifts that have not ended yet."""
        sched = self._active_schedule()
        shifts = shifts if shifts is not None else self.effective_shifts()
        if sched is None or not sched.jobs:
            return []
        try:
            from warroom.schedule.staffing import complete_shifts, op_windows, shift_for_op, staffing_gaps
        except Exception:  # noqa: BLE001 - schedule module unavailable
            return []
        now = self.clock.now()
        full = complete_shifts(shifts, min(sched.horizon_start, now) - timedelta(days=1),
                               max(sched.horizon_end, now) + timedelta(days=1))
        windows = op_windows(full)
        ops: list[tuple[str, str, datetime]] = []
        for j in sched.jobs:
            if not j.frozen:
                ops.append(("mold_in", j.press_id, j.mold_in))
            t = j.demold
            if j.frozen and shift_for_op(t, full) is None:
                # in-progress cure: the de-mold waits for the next staffed window (solver: release)
                nxt = next((w.start for w in windows if w.start >= t), None)
                t = nxt or t
            ops.append(("demold", j.press_id, t))
        return [(sh, n, short) for sh, n, short in staffing_gaps(ops, full) if sh.end > now]

    # ------------------------------------------------------------------ war-room machine holds
    # A press taken out of service (machine_out notify) or held / stopped by the safety core is a
    # factory decision, not sim state. It is persisted next to the schedule store (same lifecycle:
    # kept on a watchdog restart, cleared by an explicit scenario load) and reapplied on startup,
    # so a restart cannot quietly re-enable a press or a rollback onto it (page 7 / page 10).
    def _holds_path(self) -> Path | None:
        root = getattr(self.c.store, "root", None)
        return Path(root) / HOLDS_FILE if root is not None else None

    def _sim_holds(self) -> dict[str, dict[str, Any]]:
        try:
            ms = self.sim.machines()
        except Exception:  # noqa: BLE001
            return {}
        return {m.machine_id: {"status": m.status.value, "note": m.note}
                for m in ms if m.status in HOLD_STATUSES}

    def persisted_holds(self) -> dict[str, dict[str, Any]]:
        path = self._holds_path()
        if path is None or not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {k: v for k, v in raw.items() if k in F.MACHINE_BY_ID and isinstance(v, dict)
                    and v.get("status") in {st.value for st in HOLD_STATUSES}}
        except Exception as e:  # noqa: BLE001 - unreadable file: report, keep running
            log.warning("holds file %s unreadable: %s", path, e)
            return {}

    def _sync_holds(self) -> None:
        cur = self._sim_holds()
        if cur == self._holds:
            return
        path = self._holds_path()
        if path is not None and path.parent.is_dir():
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        added = sorted(set(cur) - set(self._holds))
        released = sorted(set(self._holds) - set(cur))
        self._holds = cur
        if added or released:
            self.audit.append(AuditKind.SYSTEM, "system", {"msg": "machine holds persisted", "holds": cur,
                                                           "added": added, "released": released})

    def _reapply_holds(self) -> None:
        holds = self.persisted_holds()
        for mid, h in holds.items():
            try:
                self.sim.set_machine_status(mid, MachineStatus(h["status"]), h.get("note"))
            except Exception as e:  # noqa: BLE001
                log.warning("could not reapply hold on %s: %s", mid, e)
        if holds:
            self.audit.append(AuditKind.SYSTEM, "system", {"msg": "machine holds reapplied after restart",
                                                           "holds": holds})

    # ------------------------------------------------------------------ schedule rollback (gated)
    ROLLBACK_BLOCKING = (MachineStatus.OUT_OF_SERVICE, MachineStatus.STOPPED, MachineStatus.OFFLINE)

    def rollback_target(self, version: int | None) -> Any:
        """The version a rollback would re-activate. Raises KeyError (unknown) or ValueError (none)."""
        store = self.c.store
        cur = store.current()
        if version is None:
            log_ = list(getattr(store, "_activation_log", []) or [])
            prior = [v for v in log_ if v != cur.version]
            if not prior:
                prior = [v.version for v in store.versions()
                         if v.version < cur.version and v.status.value in ("superseded", "rolled_back")]
            if not prior:
                raise ValueError("no earlier active version to roll back to")
            version = prior[-1]
        return store.get(version)

    def rollback_preview(self, version: int | None = None) -> dict[str, Any]:
        """Deterministic gate for a schedule rollback (page 7 一鍵回復): only a version that was
        approved and ACTIVE before may come back, and only if it does not load a press that is now
        out of service / stopped / offline or a press under the safety core's cure hold with new
        mold-ins. Returns the diff the supervisor confirms against."""
        store = self.c.store
        cur = store.current()
        target = self.rollback_target(version)
        now = self.clock.now()
        reasons: list[str] = []
        if target.version == cur.version:
            reasons.append(f"v{target.version} 已是生效中的排程")
        elif target.status.value not in ("superseded", "rolled_back"):
            reasons.append(f"v{target.version} 狀態為 {target.status.value}，從未核准生效；只能回復曾生效的版本")
        blocked: dict[str, str] = {}
        try:
            self._sync_holds()
        except Exception:  # noqa: BLE001
            log.exception("persist holds")
        for mid, h in self.persisted_holds().items():      # survives a restart even if the sim forgot
            if mid in F.CURE_PRESS_IDS:
                blocked[mid] = h["status"]
        for m in self.sim.machines():
            if m.machine_id not in F.CURE_PRESS_IDS:
                continue
            if m.status in self.ROLLBACK_BLOCKING:      # safety-core CURE_HOLD shows as STOPPED
                blocked[m.machine_id] = m.status.value
        from warroom.agent.narrate import machine_label
        hits: dict[str, int] = {}
        for j in target.schedule.jobs:
            if j.press_id in blocked and not j.frozen and j.mold_in >= now:
                hits[j.press_id] = hits.get(j.press_id, 0) + 1
        for pid, n in sorted(hits.items()):
            why = {"out_of_service": "停用", "stopped": "安全核心停機／扣模", "offline": "離線"}.get(blocked[pid], blocked[pid])
            reasons.append(f"{machine_label(pid)}目前{why}，v{target.version} 仍排了 {n} 模新入模")
        cur_jobs = {j.id: j for j in cur.schedule.jobs}
        tgt_jobs = {j.id: j for j in target.schedule.jobs}
        moved = []
        for jid, tj in tgt_jobs.items():
            cj = cur_jobs.get(jid)
            if cj is not None and (cj.press_id != tj.press_id or cj.mold_in != tj.mold_in) and tj.mold_in >= now:
                moved.append({"job_id": jid, "order_id": tj.order_id, "from_press": cj.press_id,
                              "to_press": tj.press_id, "to_mold_in": tj.mold_in.isoformat()})
        cross = any(c.kind == "transfer" for c in target.schedule.staffing_changes)
        risk = RiskLevel.R3_CRITICAL if cross else RiskLevel.R2_CHANGE
        return {"from_version": cur.version, "to_version": target.version, "target_status": target.status.value,
                "target_reason": target.reason, "feasible": not reasons, "reasons": reasons, "risk": risk.value,
                "moved": moved[:20], "moved_count": len(moved),
                "added": len(set(tgt_jobs) - set(cur_jobs)), "removed": len(set(cur_jobs) - set(tgt_jobs)),
                "staffing_changes": [c.label for c in target.schedule.staffing_changes],
                "blocked_presses": blocked}

    def labor_view(self) -> dict[str, Any]:
        """Schedule & labor (page 6): shifts (with approved staffing changes applied), labor needs of
        the active schedule, per-shift load and the gaps by the solver's own rule."""
        shifts = self.effective_shifts()
        sched = self._active_schedule()
        jobs = list(sched.jobs) if sched is not None else []
        needs = [n for j in jobs for n in j.labor]
        gap_rows = self.staffing_gap_rows(shifts)
        short_by_id = {sh.id: short for sh, _, short in gap_rows}
        load = []
        for sh in shifts:
            inside = [n for n in needs if n.team == sh.team and sh.start <= n.start < sh.end]
            gap = short_by_id.get(sh.id, 0)
            load.append({"shift_id": sh.id, "team": sh.team.value, "team_label": F.TEAM_LABELS[sh.team],
                         "name": sh.name, "start": sh.start.isoformat(), "end": sh.end.isoformat(),
                         "headcount": sh.headcount, "required": sh.headcount + gap, "tasks": len(inside),
                         "gap": gap})
        try:
            from warroom.schedule.staffing import gap_label
        except Exception:  # noqa: BLE001
            def gap_label(sh, n):
                return f"{sh.start:%m/%d} {sh.name}缺 {n} 人"
        demolds = [{"job_id": j.id, "order_id": j.order_id, "press_id": j.press_id, "size": j.size.value,
                    "mold_in": j.mold_in.isoformat(), "demold": j.demold.isoformat()} for j in jobs]
        return {"shifts": [s.model_dump(mode="json") for s in shifts],
                "needs": [n.model_dump(mode="json") for n in needs], "load": load, "demolds": demolds,
                "staffing_changes": [c.model_dump(mode="json") for c in (sched.staffing_changes if sched else [])],
                "gaps": [f"{F.TEAM_LABELS[sh.team]} {gap_label(sh, short)}" for sh, _, short in gap_rows]}

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
