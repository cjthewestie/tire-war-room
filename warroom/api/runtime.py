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
import hashlib
import heapq
import inspect
import itertools
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from warroom import factory as F
from warroom.agent.convo import AskMemory
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
    BadDataMark,
    Channel,
    CompoundStage,
    DashboardSnapshot,
    Event,
    EventState,
    FaultKind,
    FaultSpec,
    FocusPanel,
    HeaderStatus,
    LLMMode,
    MachineDetail,
    MachineState,
    MachineStatus,
    MachineTile,
    MetricTile,
    NotifyKind,
    PendingAction,
    RiskLevel,
    SafetyAction,
    ScenarioName,
    Schedule,
    SensorReading,
    SolveRequest,
    TileStatus,
    ToolName,
    WsType,
)
from warroom.sim.work import REPAIR_KINDS

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
        self.orders_db = self._orders_db()
        self.reopened_store = False
        # issue #54: one local model, one priority queue (safety > machine > orders/materials > staffing > questions)
        from warroom.agent.orchestrator import ModelQueue
        self.model_queue = ModelQueue(slots=max(1, int(getattr(self.s, "model_slots", 1))), audit=self.audit,
                                      clock=self.clock)

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

    def _orders_db(self):
        """Page 10: orders and shifts are imported from Excel (no MES). With Settings.orders_xlsx set,
        the workbook is loaded into an in-memory OrdersDB and every per-run sim is wrapped so the
        scheduler, the order panel and the commander's orders/shifts query read the workbook."""
        path = self.s.orders_xlsx
        if not path:
            return None

        def real():
            from warroom.kb import OrdersDB
            p = Path(path)
            if not p.is_file():
                raise FileNotFoundError(f"ORDERS_XLSX not found: {p}")
            db = OrdersDB(":memory:", clock=self.clock, seed=False, xlsx=p)
            if not db.orders():
                raise ValueError(f"ORDERS_XLSX {p} has no orders sheet rows")
            return db
        return self._t("orders_xlsx", real, lambda: None)

    # --- per run ---
    def build(self, scenario: ScenarioName, sleep: Callable[[float], Any] | None = None,
              fresh_store: bool = True, staged: bool = False) -> Components:
        """fresh_store=False (a RESUME: crash / watchdog restart, see Runtime._resume_wanted) reopens
        the persisted schedule store of the same scenario, so approved versions and the rollback
        history survive (page 10). A plain process start, a scenario load or a reset starts fresh.

        staged=True (live scenario switch): the fresh store is built in a staging folder and the
        live folder is left alone; Runtime commits it with commit_store() only after the whole
        build succeeded, or throws it away with discard_staging()."""
        for k in self.PER_RUN:
            self.degraded.pop(k, None)
        clock, bus = self.clock, self.bus

        def sim_real():
            replay = (self.s.replay or "").strip().lower()
            if replay:                          # team dataset replay (NORMAL scenario only)
                from warroom.sim.team_replay import TeamReplayFactory
                return TeamReplayFactory(replay, clock=clock, seed=7, scenario=ScenarioName.NORMAL,
                                         cache_dir=self.s.audit_path.parent / "team_replay")
            # issue #36: Factory plus the 一日循環 overlay, armed only in DAY_LOOP (every other scenario is plain Factory)
            from warroom.sim.day_loop import LoopFactory
            return LoopFactory(clock=clock, seed=7, scenario=ScenarioName.NORMAL)
        sim = self._t("sim", sim_real, lambda: FB.FallbackFactory(clock=clock))
        if self.orders_db is not None:
            sim = ImportedPlanSim(sim, self.orders_db)
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
            from warroom.agent.orchestrator import QueuedCommander
            return StateMachine(QueuedCommander(self.commander, self.model_queue), tools, gate, approvals, self.audit,
                                sim=sim, bus=bus, clock=clock, **extra)
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
                now, shifts, machines = self.clock.now(), sim.shifts(), sim.machines()
                key = self._seed_key(now, orders, shifts, machines, next_on)
                hit = self._seed_cache_get(key)
                if hit is not None:
                    return hit
                sch = seed_schedule(now, orders, shifts, machines, scheduler=self.scheduler, next_on=next_on)
                self._seed_cache_put(key, sch)
                return sch
            except Exception as e:  # noqa: BLE001 - fall back to a plain solve
                log.warning("seed_schedule failed (%s); plain solve", e)
            res = self.scheduler.solve(SolveRequest(now=self.clock.now(), orders=sim.orders(), shifts=sim.shifts()))
            if res.schedule is not None:
                return res.schedule
        return None

    # Scenario switches froze the demo for 8-10 s: every load re-solved the same seed schedule with CP-SAT.
    # Same scenario = same start time, orders, shifts and in-progress cures, so the first solve is kept
    # (memory + a file under schedule_dir that survives restarts) and later loads reuse it. Any change in
    # those inputs (new dataset, other start time) gives a new key and a fresh solve.
    @property
    def seed_cache_dir(self) -> Path:
        return self.s.schedule_dir / "seed_cache"

    @staticmethod
    def _seed_key(now: datetime, orders, shifts, machines, next_on) -> str:
        cures = [(m.machine_id, m.cure_job.model_dump(mode="json")) for m in machines if m.cure_job is not None]
        blob = json.dumps({"now": now.isoformat(), "orders": [o.model_dump(mode="json") for o in orders],
                           "shifts": [s.model_dump(mode="json") for s in shifts or []], "cures": cures,
                           "next_on": next_on, "code": _solver_code_hash()}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    def _seed_cache_get(self, key: str) -> Schedule | None:
        mem = self.__dict__.setdefault("_seed_mem", {})
        if key in mem:
            return mem[key].model_copy(deep=True)
        f = self.seed_cache_dir / f"{key}.json"
        try:
            sch = Schedule.model_validate_json(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - missing or unreadable: solve again
            return None
        mem[key] = sch
        return sch.model_copy(deep=True)

    def _seed_cache_put(self, key: str, sch: Schedule) -> None:
        self.__dict__.setdefault("_seed_mem", {})[key] = sch.model_copy(deep=True)
        try:
            self.seed_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.seed_cache_dir / f"{key}.tmp"
            tmp.write_text(sch.model_dump_json(), encoding="utf-8")
            tmp.replace(self.seed_cache_dir / f"{key}.json")
        except OSError as e:
            log.warning("seed schedule cache not written (%s)", e)


_SOLVER_HASH: str | None = None


def _solver_code_hash() -> str:
    """A change to the scheduler code retires every cached seed schedule."""
    global _SOLVER_HASH
    if _SOLVER_HASH is None:
        h = hashlib.sha256()
        for f in sorted((Path(__file__).resolve().parent.parent / "schedule").glob("*.py")):
            h.update(f.read_bytes())
        _SOLVER_HASH = h.hexdigest()[:16]
    return _SOLVER_HASH

class ImportedPlanSim:
    """The plant sim with orders and shifts from the imported Excel workbook (page 10, no MES).
    Everything else (machines, readings, faults, cures) is the sim's own; only the plan data the
    scheduler, the order panel and query_orders_shifts read comes from the workbook."""

    def __init__(self, sim: Any, db: Any):
        object.__setattr__(self, "_sim", sim)
        object.__setattr__(self, "_db", db)

    def orders(self):
        return self._db.orders()

    def shifts(self):
        return self._db.shifts()

    def order_progress(self):
        return self._db.order_progress()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sim, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._sim, name, value)


def _is_revoke(step_id: str) -> bool:
    """The state machine's compensating notice for replaced staffing: '<approval id>-revoke-<n>'."""
    return "-revoke-" in step_id


def _needs_approval(sr: Any) -> bool:
    """A write step of a plan that needs 主管核准 (R2/R3): by its gate decision, or for a step the gate never saw (its
    round stopped first), by what the gate would rule from the tool and the notice kind."""
    if sr.gate is not None and sr.gate.risk is not None:
        return sr.gate.risk in (RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL)
    if sr.step.tool in (ToolName.CONFIRM_SCHEDULE, ToolName.SET_CURE_TIME):
        return True
    if sr.step.tool is not ToolName.NOTIFY_TEAMS:
        return False
    from warroom.agent.gate import NOTIFY_RISK
    try:
        return NOTIFY_RISK[NotifyKind(sr.step.args.get("kind"))] is not RiskLevel.R1_NOTIFY
    except (KeyError, ValueError):
        return True                         # not a valid notice: never an auto (R1) one


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t is not None else None


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


HOLD_STATUSES = (MachineStatus.OUT_OF_SERVICE, MachineStatus.STOPPED)
HOLDS_FILE = "holds.json"
PLANT_FILE = "plant.json"            # last factory time, next to the store (same lifecycle)
RUNNING_MARKER = "running.json"      # in schedule_dir: written by start(), removed by stop()
RESUME_REQUEST = "resume.request"    # in schedule_dir: a watchdog asks the next start to resume
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


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
                 tick_wall_s: float = 0.2, max_steps_per_iter: int = 600, run_loop: bool = True,
                 resume: bool | None = None):
        """resume: None = decide from the environment (see _resume_wanted). A plain process start
        replays the scenario from its scripted start on a FRESH store with no machine holds; only a
        resume (crash, kill, watchdog restart) reopens the store, reapplies the holds and continues
        the factory clock from the last persisted time instead of replaying the script."""
        self.settings = settings or get_settings()
        self.strict = not allow_fallback
        if self.strict:      # --strict: a model failure is surfaced, never answered by the rule fallback
            self.settings = self.settings.model_copy(update={"llm_fallback_to_mock": False})
        self.builder = None if components else (builder or ComponentBuilder(self.settings, allow_fallback, use_real))
        try:
            self.scenario = ScenarioName(self.settings.scenario)
        except ValueError:
            self.scenario = ScenarioName.NORMAL
        self._sleepers: list[tuple[datetime, int, asyncio.Future]] = []
        self._sleeper_seq = itertools.count()
        self.resumed = False
        want_resume = False if components is not None else self._resume_wanted(resume)
        self.c: Components = components or self.builder.build(self.scenario, sleep=self.sim_sleep,
                                                              fresh_store=not want_resume)
        from warroom.api.ingest import IngestPipeline
        self.ingest = IngestPipeline.from_env(self.settings.audit_path.parent)
        self._orch: Any = None
        self._repaired: dict[str, str] = {}     # machine id -> event id: repair tasks all done, fault to clear
        self._repaired_out: dict[str, str] = {}   # #104 machine id -> event id: 檢修完成 while out of service
        self._recovering: dict[str, list] = {}  # machine id -> [event id, wall s ramped]: 回溫中 after a fast repair
        # 人員回報 (CJ 2026-10-22: 「加回報」): workers report 開始 / 完成 on /worker instead of the simulator's timers.
        # Off (自動・展示) by default; WORKER_REPORT=1 or the 後台 switch (set_worker_report) turns it on.
        self.worker_report = os.environ.get("WORKER_REPORT", "0").strip().lower() in ("1", "true", "yes", "on")
        self._fast_wall = time.monotonic()       # fast repair: wall clock of the last advance (paused time skipped)
        self._orch_c: Any = None
        self._cases: Any = None                  # SafetyCases of the current components (warroom/api/resolve.py)
        self._cases_c: Any = None
        self.speed = float(self.settings.sim_speed)
        self.paused = False
        self.network = "online"
        self.tick_wall_s = tick_wall_s
        self.max_steps_per_iter = max_steps_per_iter
        self.run_loop = run_loop
        # pre-plan the 核准區 ② items (CJ 2026-10-21: 「流程應該改成有事項需要計劃的時候要已經計劃好了等我點下面的核准或是
        # 討論」): on in the served app; tests that drive tick() opt in (WARROOM_PREPLAN=0/1 overrides)
        self.preplan_on = os.environ.get("WARROOM_PREPLAN", "1" if run_loop else "0") == "1"
        self._preplanned: dict[str, tuple[str, str]] = {}   # order id -> (risk signature, event id)
        self._preplan_keys: dict[str, tuple] = {}             # order id -> inputs at its last pre-plan (_preplan_inputs)
        self._preplan_at: datetime | None = None
        self.started_wall = time.monotonic()
        self.safety_log: list[SafetyAction] = []
        self.anomalies: list[Anomaly] = []
        self._tasks: dict[str, asyncio.Task] = {}
        _ = self.orch                          # issue #54: hook the main agent onto the state machine now
        # model usage budget (warroom/agent/budget.py): today's counters recomputed from the audit file, so a restart
        # keeps them; resets at the real-day boundary only (not at a 一日循環 loop wrap)
        from warroom.agent import budget
        self.budget = budget.install(budget.Budget(self.settings, audit=lambda: self.audit,
                                                   is_preplan=lambda eid: eid in self.orch.preplan_ids))
        try:
            self.budget.load(getattr(self.audit, "path", None))   # a memory log (tests) has none
        except Exception:  # noqa: BLE001 - an unreadable log starts the day's counters at zero
            log.exception("budget recompute")
        self._loop_task: asyncio.Task | None = None
        self._baseline: dict[str, dict[str, float]] = {}
        self._last_machines_pub = 0.0
        self._last_snapshot_pub = 0.0
        self._last_world_pub = 0.0
        self._fast_world_until = 0.0             # fast repair running: WORLD deltas every 0.25 s until then
        self._world_obj: Any = None              # the sim's World the last WORLD message described
        self._world_rev = -1
        self._world_sent: dict[str, str] = {}    # section -> JSON last published (WORLD deltas)
        self._kb_ok: bool | None = None
        self._llm_probe: tuple[float, bool] = (-1e9, False)
        self.ticks = 0
        self.compute_hold_max_s = 120.0          # never freeze the plant longer than this per tick
        self._switching = False                  # a scenario load is swapping components: no ticks
        self._switch_lock: asyncio.Lock | None = None
        # issue #95: 問戰情室 conversations (order / event ids only). In this process; cleared with every scenario
        # load / reset and day-loop wrap, so a new scenario never answers about the old one's orders or events.
        self.ask_memory = AskMemory()
        self.memory_resets: list[Callable[[], None]] = []    # Agent Nemo's chat memory forgets with the ask memory
        self._holds: dict[str, dict[str, Any]] = {}
        self._oob_seen: dict[str, datetime] = {}
        self._safety_unrecorded: list[SafetyAction] = []   # applied, but audit/attach failed: retried
        self._last_plant_persist = -1e9
        from warroom.sim.events import SimEvents
        self.sim_events = SimEvents()            # issue #36 模擬事件: off by default, kept across scenario loads
        self.overlays: list[ScenarioName] = []   # 情境疊加: scenarios overlaid since the last load (cleared by a load)
        if components is None and want_resume and getattr(self.builder, "reopened_store", False):
            self.resumed = True
            self._restore_plant()                # factory clock continues; the script is not replayed
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

    # ------------------------------------------------------------------ resume vs fresh start
    def _resume_wanted(self, resume: bool | None) -> bool:
        """Resume only on a genuine restart: an explicit resume=True, WARROOM_RESUME=1, a
        resume.request file a watchdog left in schedule_dir (consumed here), or a running.json marker
        left by a process that never reached stop() (crash, kill -9, power loss). A clean stop removes
        the marker, so the next start (a re-run before the judges, a boot after a clean shutdown)
        replays the scenario from a fresh store."""
        sched_dir = Path(self.settings.schedule_dir)
        req = sched_dir / RESUME_REQUEST
        requested = req.is_file()
        if requested:
            try:
                req.unlink()
            except OSError:
                pass
        if resume is not None:
            return resume
        env = os.environ.get("WARROOM_RESUME", "").strip().lower()
        if env in _TRUE:
            return True
        if env in _FALSE:
            return False
        return requested or (sched_dir / RUNNING_MARKER).is_file()

    def _plant_path(self) -> Path | None:
        root = getattr(self.c.store, "root", None)
        return Path(root) / PLANT_FILE if root is not None else None

    def _persist_plant(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_plant_persist < 2.0:
            return
        path = self._plant_path()
        if path is None or not path.parent.is_dir():
            return
        self._last_plant_persist = now
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"factory_time": self.clock.now().isoformat(), "scenario": self.scenario.value},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _restore_plant(self) -> None:
        """Resume: continue the factory clock from the last persisted time. The scripted scenario
        already played out before the restart, so the plant is reloaded calm (NORMAL) at that time;
        its faults are not replayed on top of the decisions already taken (press holds, the schedule)."""
        path = self._plant_path()
        t: datetime | None = None
        try:
            if path is not None and path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if raw.get("scenario") == self.scenario.value:
                    t = datetime.fromisoformat(raw["factory_time"]).astimezone(F.TZ)
        except Exception as e:  # noqa: BLE001 - unreadable: keep the scenario start, say why
            log.warning("plant state %s unreadable: %s", path, e)
        if t is None:
            self.audit.append(AuditKind.SYSTEM, "system", {"msg": "resumed after restart; no persisted factory "
                                                                  "time, scenario start kept"})
            return
        load = self.sim.load_scenario
        try:
            if "start_at" in inspect.signature(load).parameters:
                load(ScenarioName.NORMAL, start_at=t)
            else:
                load(ScenarioName.NORMAL)
        except Exception as e:  # noqa: BLE001
            log.warning("could not reload the plant for resume: %s", e)
        if abs((self.clock.now() - t).total_seconds()) > 1:
            self.clock.reset(t)
        self.audit.append(AuditKind.SYSTEM, "system", {"msg": "resumed after restart", "factory_time": t.isoformat(),
                                                       "scenario": self.scenario.value,
                                                       "note": "scripted faults not replayed; plant reloaded calm"})

    def _write_running_marker(self) -> None:
        d = Path(self.settings.schedule_dir)
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / RUNNING_MARKER).write_text(json.dumps({"pid": os.getpid(), "scenario": self.scenario.value}),
                                            encoding="utf-8")
        except OSError as e:
            log.warning("running marker not written: %s", e)

    def _clear_running_marker(self) -> None:
        try:
            (Path(self.settings.schedule_dir) / RUNNING_MARKER).unlink(missing_ok=True)
        except OSError as e:
            log.warning("running marker not removed: %s", e)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.started_wall = time.monotonic()
        if self.builder is not None:
            self._write_running_marker()
            try:
                self._persist_plant(force=True)
            except Exception:  # noqa: BLE001
                log.exception("persist plant")
        self.audit.append(AuditKind.SYSTEM, "system", {"msg": "runtime start", "scenario": self.scenario.value,
                                                       "resumed": self.resumed, "degraded": self.degraded,
                                                       "llm_mode": self.llm_mode.value})
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
            self._persist_plant(force=True)
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
        if self.builder is not None:
            self._clear_running_marker()         # clean stop: the next start replays fresh

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
            self._advance_fast_repairs(running=not self.paused)
            if self.paused:
                continue
            if self.speed > 1 and self.compute_busy():
                # accelerated demo: the simulated plant's clock waits for the LLM / solver (wall time
                # is not owed). No sim second passes, so no reading goes unchecked: every reading the
                # sim produces still goes through tick() -> safety_pass() first.
                try:
                    self._publish_periodic()
                except Exception:  # noqa: BLE001 - a read model must never stop the plant loop
                    log.exception("publish failed")
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
            self._persist_plant()
        except Exception:  # noqa: BLE001 - persistence must never break the loop
            log.exception("persist holds")
        if self.network == "lan_down" or self._safety_unrecorded or getattr(self, "_light_on", False):
            try:
                self.sync_warning_light()
            except Exception:  # noqa: BLE001 - the light must never break the loop
                log.exception("warning light")
        try:
            self._publish_world(now, force)
        except Exception:  # noqa: BLE001 - the plant view must never break the loop
            log.exception("world publish")
        if force or now - self._last_machines_pub >= 0.5:
            self._last_machines_pub = now
            self.bus.publish(WsType.MACHINES, {"machines": [t.model_dump(mode="json") for t in self.machine_tiles()]})
            self.bus.publish(WsType.CLOCK, self.clock_info())
        if force or now - self._last_snapshot_pub >= 5.0:
            self._last_snapshot_pub = now
            self.bus.publish(WsType.SNAPSHOT, self.snapshot())

    def _publish_schedule(self) -> None:
        """WS "schedule" with the active version after the store was swapped (scenario load) or rolled back to v1 (24:00
        wrap). The dashboard reads GET /api/schedule only at page load, so without it the previous scenario's schedule
        stayed on screen (「SO-004 可能晚 253 小時」, 「10 號機 硫化延長中」 before anything was approved)."""
        store = self.c.store
        if store is None:
            return
        try:
            v = store.current()
        except Exception:  # noqa: BLE001 - a store without an ACTIVE version has nothing to show
            log.exception("schedule publish")
            return
        self.bus.publish(WsType.SCHEDULE, v)

    # ------------------------------------------------------------------ the whole plant (issue #11)
    def world_snapshot(self) -> Any:
        """WorldSnapshot of the sim's World (orders, materials, dock, people, energy, quality, maintenance),
        or None when the sim has none (fallback / replay sims)."""
        w = getattr(self.sim, "world", None)
        return w.snapshot() if w is not None else None

    def _publish_world(self, now: float, force: bool) -> None:
        """WORLD deltas: at most once a second (wall), only when the world moved (it steps every 10 factory
        seconds), only the sections that changed. A new World (scenario load) is sent in full. Nothing
        extra is sent on connect (existing clients expect snapshot + clock first): a client reads GET /api/world.
        Extension point for the agent: World.add_listener (warroom/sim/world.py) sees every world event;
        nothing here wakes the state machine yet."""
        w = getattr(self.sim, "world", None)
        if w is None:
            return
        # a fast repair runs on the wall clock, also while the factory clock is held (no world step, no new rev): its
        # crew and ring are sent anyway, or the 3D showed the crew 4–5 s late and the machine fixed before anyone came
        # (呱呱 10-22). Only the sections that changed go out.
        fast = callable(getattr(getattr(w, "tasks", None), "fast_open", None)) and w.tasks.fast_open()
        if w is self._world_obj and w.rev == self._world_rev and not fast:
            return
        if fast:
            self._fast_world_until = now + 1.0   # a fast repair's ring fills smoothly, up to its 完成
        if not force and now - self._last_world_pub < (0.25 if now < self._fast_world_until else 1.0):
            return
        from warroom.sim.world import WORLD_SECTIONS
        data = w.snapshot().model_dump(mode="json")
        full = w is not self._world_obj
        enc = {k: json.dumps(data[k], ensure_ascii=False, sort_keys=True) for k in WORLD_SECTIONS}
        changed = {k: data[k] for k in WORLD_SECTIONS if full or enc[k] != self._world_sent.get(k)}
        self._world_obj, self._world_rev, self._world_sent, self._last_world_pub = w, w.rev, enc, now
        self.bus.publish(WsType.WORLD, {"full": full, "rev": data["rev"], "factory_time": data["factory_time"], **changed})

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
        if getattr(self.sim, "day_end", None) is not None:      # issue #36 一日循環
            await self._day_loop_tick()
        before = self.clock.now()
        readings: list[SensorReading] = self.sim.step(dt_s)
        if self.clock.now() == before:          # sim did not move the clock itself
            self.clock.advance(dt_s)
        self._advance_fast_repairs()
        self._apply_repairs()
        self.ticks += 1
        self.memory_desk.on_tick()              # a real date boundary writes the day's summary (in the background)
        self.safety_pass(readings)              # RED path first: nothing below can delay it
        self._random_event()                    # issue #36: off by default
        self._plant_inputs()                    # issue #17: a scripted shift start / rush order / sick call
        if self.preplan_on:
            self._preplan_orders()
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
        t0 = time.perf_counter()
        states = {m.machine_id: m for m in self.sim.machines()}
        acts = list(self.c.safety.check(readings, states))
        applied: list[SafetyAction] = []
        # 1. act on the plant for EVERY action first. Nothing below (audit disk, bus, event attach)
        #    can drop or delay an action: the safety core has already latched them all.
        for a in acts:
            try:
                self.sim.apply_safety_action(a)
            except Exception:  # noqa: BLE001 - one bad apply must not skip the next machine
                log.exception("apply safety action %s on %s", a.action.value, a.machine_id)
            # latency up to the point the plant was acted on (batch received -> applied)
            applied.append(a.model_copy(update={"latency_ms": round((time.perf_counter() - t0) * 1000.0, 3)}))
        self.safety_log.extend(applied)
        del self.safety_log[:-100]
        # resolution path (CJ, 2026-10-22: 「安全核心要設計可以被解決」): a case per stopped machine, ② counted read-only
        try:
            cases = self.cases
            for a in applied:
                cases.on_action(a, self._open_event_id(a.machine_id))
            if cases.observe(readings) or applied:
                self._last_snapshot_pub = 0.0       # republish the snapshot on the next periodic publish
        except Exception:  # noqa: BLE001 - bookkeeping must never touch the red path
            log.exception("safety case bookkeeping")
        # 2. then record. A record that fails is queued, retried every pass, and lights the warning light.
        self._retry_safety_records()
        for a in applied:
            if self._safety_unrecorded or not self._record_safety(a):
                if a not in self._safety_unrecorded:
                    self._safety_unrecorded.append(a)
            try:
                self.bus.publish(WsType.SAFETY_ACTION, a)
            except Exception:  # noqa: BLE001
                log.exception("publish safety action")
        return applied

    def _record_safety(self, a: SafetyAction) -> bool:
        """Audit the action and attach it to the open event. Never raises."""
        try:
            on_safety = getattr(self.state_machine, "on_safety_action", None)
            if callable(on_safety):           # state machine audits it and attaches it to the open event
                on_safety(a)
            else:
                self.audit.append(AuditKind.SAFETY_ACTION, "safety", a.model_dump(mode="json"),
                                  self._open_event_id(a.machine_id))
            return True
        except Exception:  # noqa: BLE001 - the plant was already acted on; keep it for retry
            log.exception("record safety action %s on %s (queued for retry)", a.action.value, a.machine_id)
            return False

    def _retry_safety_records(self) -> None:
        """Retry records in order; stop at the first one that still fails (keeps the log in order)."""
        while self._safety_unrecorded:
            if not self._record_safety(self._safety_unrecorded[0]):
                return
            self._safety_unrecorded.pop(0)

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

    def _plant_inputs(self) -> None:
        """Issue #17: each plant input the sim fired (shift start, rush order, sick call) opens a plant event that
        runs as a tracked task (cancelled on a scenario load like any event)."""
        drain = getattr(self.sim, "drain_plant_inputs", None)
        sm = self.state_machine
        self._watch_world()
        if not callable(drain) or not callable(getattr(sm, "open_plant_event", None)):
            return
        self._watch_tasks()
        for at, pin in drain():
            ev = sm.open_plant_event(pin, at)
            task = asyncio.create_task(sm.run_plant(ev.id), name=f"plant-{ev.id}")
            self._tasks[ev.id] = task
            task.add_done_callback(lambda t, eid=ev.id: self._event_done(eid, t))

    def _watch_world(self) -> None:
        """Issue #36: an order completed or a shipment that left is audited (the world feed carries it over WS)."""
        w = getattr(self.sim, "world", None)
        if w is None or getattr(w, "_success_audited", False) or not callable(getattr(w, "add_listener", None)):
            return
        w._success_audited = True

        def on_event(ev: Any) -> None:
            # 「接記憶」: new and rush orders and late deliveries too, so the memory's observations rebuild from the audit
            if ev.kind in ("order_completed", "shipment_departed", "order_new", "order_rush", "delivery_late"):
                self.audit.append(AuditKind.SYSTEM, "world",
                                  {"msg": ev.text, "kind": ev.kind, "order_ids": ev.order_ids, "qty": ev.qty,
                                   "on_time": ev.on_time, "at": ev.ts.isoformat()})
                self._log_order(ev)
        w.add_listener(on_event)

    def _log_order(self, ev: Any) -> None:
        """The order event into the chat/order log on disk (warroom/data/history_log.py), one row per order id."""
        try:
            book = dict(getattr(getattr(self.sim, "world", None), "_orders", None) or {})     # world-generated orders
            book.update({o.id: o for o in (self.sim.orders() if callable(getattr(self.sim, "orders", None)) else [])})
            for oid in (ev.order_ids or ([ev.ref] if ev.ref else [None])):
                self.history_log.add_order(ev.ts, ev.kind, oid, ev.text, order=book.get(oid), qty=ev.qty,
                                           on_time=ev.on_time)
        except Exception:  # noqa: BLE001 - the log must never break the plant loop
            log.exception("order log write failed")

    def _watch_tasks(self) -> None:
        """Audit every worker-task status change of the current world's task board (once per World)."""
        board = getattr(getattr(self.sim, "world", None), "tasks", None)
        if board is None or getattr(board, "_audited", False):
            return
        board._audited = True
        board.fast_s = float(self.settings.repair_fast_s)    # fast repair (Kyle 10-21: 紅燈到綠燈只要 2–5 秒)
        board.speed = lambda: self.speed
        board.hold = self._awaits_decision                    # fast repair starts after 主管核准, not at the card
        board.report_mode = self.worker_report

        def on_task(what: str, t: Any, now: datetime) -> None:
            if what == "created":
                return                          # the state machine audits creation with its source step
            actor = "world"
            detail: dict[str, Any] = {"task_id": t.id, "status": t.status, "change": what, "kind": t.kind,
                                      "title": t.title, "worker_id": t.worker_id, "worker": t.worker_name,
                                      "where": t.where, "at": now.isoformat(), "mode": t.mode}
            if what in ("reported_start", "reported_done", "override_done"):
                actor = t.reported_by if what == "override_done" else (t.worker_id or "worker")
                detail.update({"note": t.report_note, "photo_sha256": t.photo_sha256,
                               "override_reason": t.override_reason, "started_at": _iso(t.started_at),
                               "done_at": _iso(t.done_at)})
            elif what == "overdue":
                detail["msg"] = f"逾時提醒：{t.kind} 執行超過預計 {t.work_min:g} 分鐘的 2 倍，尚未回報完成"
            self.audit.append(AuditKind.TASK, actor, detail, t.event_id)
            self._task_note(what, t, now)
            # Kyle 10-21: 「已經兩個人去修理…但沒有修好，這樣不行」. The crew finished every repair task of this
            # event on this machine: the fault is fixed (applied after this sim step, see _apply_repairs).
            if t.status == "完成" and t.kind in REPAIR_KINDS and t.machine_id and t.event_id:
                same = [x for x in board.for_event(t.event_id) if x.kind in REPAIR_KINDS and x.machine_id == t.machine_id]
                if same and all(x.status == "完成" for x in same):
                    self._repaired[t.machine_id] = t.event_id
        board.add_listener(on_task)

    def _task_note(self, what: str, t: Any, now: datetime) -> None:
        """人員回報 on the event's timeline: 「張雅婷 09:41 回報完成（附照片）」 (VERIFY adds the reading after it)."""
        note = getattr(self.state_machine, "task_reported", None)
        if not callable(note) or not t.event_id or what not in ("reported_start", "reported_done", "override_done",
                                                                "overdue"):
            return
        who = (t.worker_name or t.worker_id or "").split("（")[0]
        at = now.astimezone(F.TZ).strftime("%H:%M")
        text = {"reported_start": f"{who} {at} 回報到達・開始：{t.kind}［{t.where}］",
                "reported_done": f"{who} {at} 回報完成" + ("（附照片）" if t.photo_sha256 else "")
                                 + f"：{t.kind}［{t.where}］",
                "override_done": f"主管 {at} 代為完成 {who or t.id}的{t.kind}（原因：{t.override_reason}）",
                "overdue": f"逾時提醒：{who} 的{t.kind}已執行超過預計時間 2 倍，尚未回報完成"}[what]
        if t.report_note and what != "override_done":
            text += f"；備註：{t.report_note}"
        try:
            note(t.event_id, text, now)
        except Exception:   # a timeline note must never break the plant loop
            log.exception("task note")

    def set_worker_report(self, on: bool, by: str) -> bool:
        """後台 switch: 自動（展示） ↔ 人員回報. Applies to the current task board at once (and to every new World);
        audited. Open tasks keep their status; from then on they move by reports (or by the timers again)."""
        on = bool(on)
        before, self.worker_report = self.worker_report, on
        board = getattr(getattr(self.sim, "world", None), "tasks", None)
        if board is not None:
            board.report_mode = on
            self.sim.world.rev += 1
        self.audit.append(AuditKind.SYSTEM, by, {"msg": "人員任務回報模式", "worker_report": on, "before": before})
        return on

    def _advance_fast_repairs(self, running: bool = True) -> None:
        """Fast repair (Settings.repair_fast_s): move the repair tasks on the WALL clock (every loop turn and tick, so
        at any sim speed, also while another event's model call holds the plant clock), apply a fix that just
        finished, and ramp the repaired machines back to nominal (Settings.recover_fast_s). Paused time does not
        count."""
        now = time.monotonic()
        dt, self._fast_wall = now - self._fast_wall, now
        board = getattr(getattr(self.sim, "world", None), "tasks", None)
        if not running or self._switching or self.settings.repair_fast_s <= 0 or \
                not callable(getattr(board, "advance_fast", None)):
            return
        board.advance_fast(self.clock.now(), dt)
        self._apply_repairs()
        self._advance_recoveries(dt)

    def _advance_recoveries(self, dt: float) -> None:
        """回溫中 (Kyle 10-21 晚: 「機台恢復正常溫度後轉回綠燈」): every repaired machine's readings move from where the fix
        found them to nominal over Settings.recover_fast_s WALL seconds; at the end it is back to normal: audited
        「已恢復正常」 and the state machine is told (Event.recovered_at, VERIFY confirms, green)."""
        total = float(self.settings.recover_fast_s)
        for mid, rec in list(self._recovering.items()):
            rec[1] += max(0.0, dt)
            frac = 1.0 if total <= 0 else min(1.0, rec[1] / total)
            r = self.sim.recover(mid, frac)
            if r is not None and frac < 1.0:
                continue
            del self._recovering[mid]
            self._recovered(mid, rec[0], r or self.sim.machine(mid).last_reading)

    def _begin_recovery(self, mid: str, eid: str) -> None:
        begin = getattr(self.sim, "begin_recovery", None)
        if callable(begin):
            begin(mid)
            self._recovering[mid] = [eid, 0.0]
            if self.settings.recover_fast_s <= 0:
                self._advance_recoveries(0.0)
            return
        snap = getattr(self.sim, "snap_to_nominal", None)     # a sim without the ramp: back at once
        if callable(snap):
            snap(mid)
        self._recovered(mid, eid, self.sim.machine(mid).last_reading)

    def _recovered(self, mid: str, eid: str, reading: Any) -> None:
        self.audit.append(AuditKind.SYSTEM, "world", {"msg": "已恢復正常", "machine_id": mid,
                                                      "value": getattr(reading, "temp_c", None)}, eid)
        on_recovered = getattr(self.state_machine, "on_recovered", None)
        if callable(on_recovered):
            on_recovered(eid, mid, self.clock.now(), reading)

    def _awaits_decision(self, event_id: str | None) -> bool:
        """The event is still before the supervisor's decision: planning, gate, waiting card, replanning — or the
        auto (R1) writes that run in EXECUTE before the card (state machine: GATE → EXECUTE (auto R1) →
        AWAIT_APPROVAL), while a write of that round waits for the card (Spark timing 10-21: the R1 檢修 walked to
        7 號 13 s before the card) or its round stopped before its R2/R3 writes were gated — or an event that ended
        (SAFE_STOP) with no decision while a round of its plan had, or would have had once gated, an R2/R3 write
        (steam_low on an idle press: the solve was refused, the event stopped with no card and its R1 檢修 walked out
        on its own). Only a plan of R1 writes repairs with no decision. Not while code re-solves an approved round
        whose schedule another event's version made stale (StateMachine.approved_rebase): the approved repair goes on
        meanwhile."""
        ev = self._event_obj(event_id)
        if ev is None:
            return False
        if ev.state in TERMINAL_STATES and getattr(ev, "follow_up", None) is not None:
            # a SAFE_STOP the supervisor handed to the floor (交給現場處理): nobody decides on it any more, its tasks
            # run at once (#105 had this; main lost it in the merge, see docs/merge-compare/pr105.md)
            return False
        rebase = getattr(self.state_machine, "approved_rebase", None)
        if callable(rebase) and rebase(ev.id):
            return False
        if ev.state in (EventState.DETECTED, EventState.CLASSIFY, EventState.PLAN, EventState.SIMULATE,
                        EventState.GATE, EventState.AWAIT_APPROVAL, EventState.REPLAN):
            return True
        if any(r.event_id == ev.id and r.status in (ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED)
               for r in self.approvals.all()):
            return False
        if ev.state in TERMINAL_STATES:
            return ev.state is not EventState.CLOSED and \
                any(_needs_approval(sr) for sr in [*ev.steps, *(s for r in ev.prior_rounds for s in r.steps)])
        # EXECUTE / VERIFY: a no-op confirm the gate dropped (gated, skipped) needed no card, so that plan repairs
        return any(sr.state == "awaiting_approval" or (sr.gate is None and _needs_approval(sr)) for sr in ev.steps)

    def _apply_repairs(self) -> None:
        """Clear the faults of machines whose repair tasks all finished (once per machine and event). Fast repair
        (Settings.repair_fast_s > 0): the fix lands once the event is past the supervisor's decision (VERIFY, or it
        ended), so a 檢修 the gate let through before the card does not fix the machine before 主管核准; the state
        machine is told (Event.repaired_at, 回溫中) and the machine's readings ramp back to normal
        (_advance_recoveries)."""
        fast = self.settings.repair_fast_s > 0
        board = getattr(getattr(self.sim, "world", None), "tasks", None)
        for mid, eid in list(self._repaired.items()):
            same = [x for x in board.for_event(eid) if x.kind in REPAIR_KINDS and x.machine_id == mid] \
                if board is not None else []
            if not all(x.status == "完成" for x in same):
                del self._repaired[mid]          # another repair task of this event started since: wait for that one
                continue
            ev = self._event_obj(eid)
            if fast and ev is not None and ev.state not in TERMINAL_STATES and ev.state is not EventState.VERIFY:
                continue                         # 主管核准 first: the approved round's tasks run, then the fix lands
            del self._repaired[mid]
            takeout = getattr(self.sim, "needs_takeout", None)
            if callable(takeout) and takeout(mid) and not any(x.kind == "停用檢修" for x in same):
                # page 11: H1's 檢修 (check the steam supply) finds the press still leaking; only H2's 停用檢修 fixes
                # it, so VERIFY still sees 119.5 °C and the next hypothesis follows (fast repair or not)
                self.audit.append(AuditKind.SYSTEM, "world", {"msg": "檢修完成，洩壓仍在：需停用檢修", "machine_id": mid}, eid)
                continue
            if all(f.machine_id != mid for f in self.sim.faults(active_only=False)):
                continue                        # nothing injected on it (a replayed or already-cleared fault)
            self.sim.clear_faults(mid)
            self.c.detector.reset(mid, repaired=True)
            status_of = getattr(self.sim, "status_of", None)
            if callable(status_of) and status_of(mid) is MachineStatus.OUT_OF_SERVICE:
                self._repaired_out[mid] = eid      # 檢修完成: green, still out of service until 恢復運轉
            self.audit.append(AuditKind.SYSTEM, "world", {"msg": "維修完成，故障排除", "machine_id": mid}, eid)
            on_repaired = getattr(self.state_machine, "on_repaired", None)
            if fast and callable(on_repaired):
                on_repaired(eid, mid, self.clock.now())
            withdraw = getattr(self.state_machine, "withdraw_cure_extension", None)
            if not fast and callable(withdraw):
                # real durations: VERIFY reads the press as usual, but the cold-press cure extension is withdrawn too
                withdraw(eid, mid, self.clock.now())
            if fast:
                self._begin_recovery(mid, eid)   # a short ramp on the wall clock, not the slow cool-down

    # ------------------------------------------------------------------ resolution paths (warroom/api/resolve.py)
    @property
    def cases(self) -> Any:
        """The Safety Core cases of the current run (rebuilt with the components on a scenario load)."""
        if getattr(self, "_cases", None) is None or self._cases_c is not self.c:
            from warroom.api.resolve import SafetyCases
            self._cases = SafetyCases(self.c.safety, self.audit)
            self._cases_c = self.c
        return self._cases

    def _board(self) -> Any:
        return getattr(getattr(self.sim, "world", None), "tasks", None)

    def _after_resolution(self) -> None:
        self._last_snapshot_pub = 0.0
        try:
            self.bus.publish(WsType.SNAPSHOT, self.snapshot())
        except Exception:  # noqa: BLE001
            log.exception("snapshot publish after a resolution step")

    def safety_dispatch(self, case_id: str, actor: str) -> Any:
        """① 派現場檢查 (supervisor session only). KeyError: no such case; ValueError: not allowed now."""
        cases = self.cases
        c = cases.get(case_id)
        tasks = cases.dispatch(case_id, actor, self._board(), self.clock.now())
        sm = self.state_machine
        if c.event_id and callable(getattr(sm, "add_note", None)):
            sm.add_note(c.event_id, "主管", f"[人工] 安全核心處置：派現場檢查 {len(tasks)} 項（"
                        + "、".join(t.kind for t in tasks) + "，" + "、".join(c.sop_ids) + "）")
        self._after_resolution()
        return c

    def safety_release(self, case_id: str, body: Any, actor: str) -> Any:
        """③ 確認恢復運轉 / QC 放行 / 報廢 (supervisor session only; never the agent). KeyError / ValueError as above."""
        c = self.cases.release(case_id, body, actor, self.sim, self._board(), self.clock.now())
        if c.resolution != "scrap":
            # the core re-arms by itself after the same count of clear samples (② required it); the detector goes back
            # to the machine's normal baseline (as after a repair), so the way back to normal is not a new 「溫度驟降」
            try:
                self.c.detector.reset(c.machine_id, repaired=True)
            except Exception:  # noqa: BLE001
                log.exception("detector reset after release")
        sm = self.state_machine
        label = {"resume": "確認恢復運轉", "qc_pass": "QC 合格，放行", "scrap": "QC 報廢"}[c.resolution]
        if c.event_id and callable(getattr(sm, "add_note", None)):
            sm.add_note(c.event_id, "主管", f"[人工] 安全核心解除：{label}（讀值 {c.reading}°C，連續 {c.safe_samples} 筆在安全範圍"
                        + (f"；已現場確認：{c.reason}" if c.field_confirmed else "") + "）")
        self._after_resolution()
        return c

    def _stopped_event(self, event_id: str) -> Event:
        ev = self._event_obj(event_id)
        if ev is None:
            raise KeyError(event_id)
        if ev.state is not EventState.SAFE_STOP:
            raise ValueError(f"{event_id} 不是安全停止的事件")
        if ev.follow_up is not None and ev.follow_up.status in ("replanned", "closed"):
            raise ValueError("這件安全停止已經處理過了")
        return ev

    def stop_replan(self, event_id: str, actor: str) -> Event:
        """「請 AI 重新規劃」: a new event through the normal state machine, gate and approval; the stopped one is marked
        replanned (and leaves the waiting list)."""
        from warroom.contracts import StopFollowUp
        old = self._stopped_event(event_id)
        sm = self.state_machine
        if not callable(getattr(sm, "open_replan", None)):
            raise ValueError("agent unavailable (degraded mode)")
        ev = sm.open_replan(event_id, actor)
        run = sm.run_command if ev.source == "supervisor" else sm.run_plant if ev.source == "plant" else sm.run_event
        task = asyncio.create_task(run(ev.id), name=f"replan-{ev.id}")
        self._tasks[ev.id] = task
        task.add_done_callback(lambda t, eid=ev.id: self._event_done(eid, t))
        prev_tasks = old.follow_up.task_ids if old.follow_up else []
        old.follow_up = StopFollowUp(status="replanned", by=actor, at=self.clock.now(), new_event_id=ev.id,
                                     task_ids=prev_tasks)
        self.audit.append(AuditKind.SYSTEM, actor, {"msg": "安全停止：請 AI 重新規劃", "follow_up": "replanned",
                                                    "new_event_id": ev.id, "stop_reason": old.stop_reason,
                                                    "stop_reason_text": old.stop_reason_text}, old.id)
        sm.add_note(old.id, "主管", f"[人工] 請 AI 重新規劃 → {ev.id}")
        self._after_resolution()
        return ev

    def stop_handoff(self, event_id: str, actor: str) -> Event:
        """「交給現場處理」: worker tasks from the SOP for this machine; the event shows 「人工處理中」 until 確認結案."""
        from warroom.api.resolve import sop_for_stop
        from warroom.contracts import StopFollowUp
        ev = self._stopped_event(event_id)
        if ev.follow_up is not None:
            raise ValueError("已交給現場處理")
        board = self._board()
        if board is None or not callable(getattr(board, "add_specs", None)):
            raise ValueError("現場任務板不可用（模擬工廠沒有人員）")
        sops, specs = sop_for_stop(ev.machine_id, ev.alert_card.title if ev.alert_card else ev.anomaly.message)
        tasks = board.add_specs(ev.id, specs, self.clock.now())
        ev.follow_up = StopFollowUp(status="manual", by=actor, at=self.clock.now(), task_ids=[t.id for t in tasks],
                                    sop_ids=sops)
        self.audit.append(AuditKind.TASK, actor,
                          {"created": [t.model_dump(mode="json", include={"id", "kind", "title", "team", "machine_id",
                                                                          "zone", "status", "worker_id", "eta"})
                                       for t in tasks], "sop_ids": sops, "generated_by": "code",
                           "follow_up": "manual"}, ev.id)
        self.audit.append(AuditKind.SYSTEM, actor, {"msg": "安全停止：交給現場處理", "follow_up": "manual",
                                                    "task_ids": [t.id for t in tasks], "sop_ids": sops}, ev.id)
        self.state_machine.add_note(ev.id, "主管", f"[人工] 交給現場處理：{len(tasks)} 項任務（{'、'.join(sops)}）・人工處理中")
        self._after_resolution()
        return ev

    def stop_close(self, event_id: str, actor: str, note: str | None = None) -> Event:
        """「確認結案」: the supervisor acknowledges the SAFE_STOP (after the floor's tasks, if handed over). Audited, and
        the daylog shows it (warroom/api/daylog.py)."""
        from warroom.contracts import StopFollowUp
        ev = self._stopped_event(event_id)
        now = self.clock.now()
        if ev.follow_up is not None and ev.follow_up.status == "manual":
            board = self._board()
            live = {t.id: t.status for t in board.for_event(ev.id)} if board is not None else {}
            left = [t for t in ev.follow_up.task_ids if live.get(t, "完成") != "完成"]
            if left:
                raise ValueError(f"現場任務還沒完成（{len(ev.follow_up.task_ids) - len(left)}/{len(ev.follow_up.task_ids)}）")
            ev.follow_up = ev.follow_up.model_copy(update={"status": "closed", "closed_at": now, "closed_by": actor,
                                                           "note": note})
        else:
            ev.follow_up = StopFollowUp(status="closed", by=actor, at=now, closed_at=now, closed_by=actor, note=note)
        self.audit.append(AuditKind.SYSTEM, actor, {"msg": "安全停止：確認結案", "follow_up": "closed", "note": note,
                                                    "stop_reason": ev.stop_reason, "stop_reason_text": ev.stop_reason_text,
                                                    "task_ids": ev.follow_up.task_ids}, ev.id)
        self.state_machine.add_note(ev.id, "主管", "[人工] 主管確認結案" + (f"：{note}" if note else ""))
        self._after_resolution()
        return ev

    @property
    def orch(self) -> Any:
        """Issue #54: the main agent (orchestrator) over the current state machine (rebuilt with the components)."""
        if self._orch is None or self._orch_c is not self.c:
            from warroom.agent.orchestrator import Dispatcher, Orchestrator
            q = getattr(self.builder, "model_queue", None)
            self._orch = Orchestrator(self.c.state_machine, self.audit, approvals=self.c.approvals, bus=self.c.bus,
                                      clock=self.clock, queue=q, tasks=lambda: self._tasks,
                                      board=lambda: getattr(getattr(self.sim, "world", None), "tasks", None),
                                      dispatcher=Dispatcher(self.settings))
            self._orch.queue.on_change = self._orch.publish
            self._orch.precheck = lambda ev: self.precheck_line(self._preplan_order(ev))
            self._orch_c = self.c
            if hasattr(self.c.state_machine, "memory"):   # 「接記憶」: plans and replans read the machine's memory
                self.c.state_machine.memory = self.machine_memory
        return self._orch

    def start_command(self, text: str, actor: str = "supervisor") -> Event:
        """Issue #10: open a supervisor-command event now and run it as a tracked task (cancelled on a
        scenario load like any event)."""
        sm = self.state_machine
        ev = sm.open_command(text, actor)
        task = asyncio.create_task(sm.run_command(ev.id), name=f"command-{ev.id}")
        self._tasks[ev.id] = task
        task.add_done_callback(lambda t, eid=ev.id: self._event_done(eid, t))
        return ev

    PREPLAN_EVERY_S = 30.0      # sim seconds between looks at the at-risk orders
    PREPLAN_MAX = 2             # pre-plans drafting at once: incidents always go first in the ModelQueue anyway

    def at_risk_orders(self) -> dict[str, str]:
        """The 核准區 ② 交期風險 items, as web/src/agent/homeItems.ts builds them: an open order whose active schedule
        de-molds its last tyre after the due date (date-only due = 23:59), and the orders the solver's own warnings
        name. {order id: signature}; the signature changes when the item does (a new ETA or warning)."""
        out: dict[str, str] = {}
        w = self.world_snapshot()
        sched = self._active_schedule()
        jobs = list(sched.jobs) if sched is not None else []
        for o in (w.orders.items if w is not None else []):
            if o.status in ("已出貨", "完成", "待出貨"):
                continue
            mine = [j.demold for j in jobs if j.order_id == o.id]
            if not mine:
                continue
            due = datetime.combine(o.due, datetime.min.time().replace(hour=23, minute=59), tzinfo=F.TZ)
            if max(mine) > due:
                out[o.id] = f"eta {max(mine).isoformat()} due {o.due}"
        for warn in (sched.warnings if sched is not None else []):
            for oid in re.findall(r"#\w-\d+", warn):
                out.setdefault(oid, f"warning {warn[:80]}")
        return out

    def _preplan_orders(self) -> list[str]:
        """Draft a plan for each at-risk order before anyone opens it: one supervisor-command event per item (the same
        「<id> 可能晚交，請重排保交期」 the item's click used to send), at the pre-plan priority in the ModelQueue (below
        incidents, above small talk). One plan per item; a new one only when the item changed and the last one ended.
        Only WHEN planning starts changes: the run is the normal PLAN → GATE → approval path."""
        now = self.clock.now()
        if self._preplan_at is not None and (now - self._preplan_at).total_seconds() < self.PREPLAN_EVERY_S:
            return []
        self._preplan_at = now
        why = self.budget.blocked("preplan")   # past 90 % of the day's model budget: no new pre-plans
        if why:
            return []
        try:
            risky = self.at_risk_orders()
            sm = self.state_machine
            if not callable(getattr(sm, "open_command", None)) or self.c.tools is None:
                return []
            events = {e.id: e for e in self.events()}
        except Exception:  # noqa: BLE001 - a pre-plan is a convenience; the click still plans
            log.exception("pre-plan check")
            return []
        from warroom.agent.orchestrator import PRIORITY
        from warroom.contracts import TERMINAL_STATES
        # drafting = not yet at its card (a card waiting for the supervisor no longer takes a model slot)
        running = sum(1 for _sig, eid in self._preplanned.values() if eid in events
                      and events[eid].state not in TERMINAL_STATES and events[eid].state.value != "await_approval")
        started = []
        for oid, sig in risky.items():
            if running >= self.PREPLAN_MAX:
                break
            prior = self._preplanned.get(oid)
            mine = events.get(prior[1]) if prior else None
            if mine is not None and (prior[0] == sig or mine.state not in TERMINAL_STATES):
                continue                                         # planned already (or still drafting)
            inputs = self._preplan_inputs(oid)
            if mine is not None and not mine.approvals and self._preplan_keys.get(oid) == inputs:
                continue          # it found nothing to change: a new ETA alone (a schedule version bump) is no reason
            asked = [e for e in events.values() if e.source == "supervisor" and (e.command or "").startswith(f"{oid} ")]
            if any(e.state not in TERMINAL_STATES for e in asked) or (asked and mine is None and prior is None):
                # the supervisor (or an earlier pre-plan before a restart) already asked for this order
                self._preplanned[oid] = (sig, max(asked, key=lambda e: e.opened_at).id)
                continue
            ev = self.start_command(f"{oid} 可能晚交，請重排保交期", "agent_nemo")
            self.orch.preplan_ids.add(ev.id)
            t, _p = self.orch.queue.tags.get(ev.id, ("order", 0))
            self.orch.queue.tags[ev.id] = (t, PRIORITY["preplan"])    # also when the run opens later (Orchestrator._opened)
            self._preplanned[oid] = (sig, ev.id)
            self._preplan_keys[oid] = inputs
            self.audit.append(AuditKind.SYSTEM, "agent_nemo", {"msg": "pre-plan", "order_id": oid, "signature": sig},
                              event_id=ev.id)
            running += 1
            started.append(ev.id)
        return started

    def return_to_service(self, machine_id: str, actor: str) -> None:
        """#104 恢復運轉: the supervisor puts a repaired machine back in service (OUT_OF_SERVICE -> RUNNING). Human only:
        reachable from the supervisor-session route, never from a plan or a tool. Only after 檢修完成 (its repair tasks
        done and the fault cleared). Audited with the supervisor as actor. (#105 releases Safety Core STOPPED holds;
        OUT_OF_SERVICE is this one.)"""
        if machine_id not in F.MACHINE_BY_ID:
            raise KeyError(machine_id)
        status_of = getattr(self.sim, "status_of", None)
        if not callable(status_of) or status_of(machine_id) is not MachineStatus.OUT_OF_SERVICE:
            raise ValueError(f"{machine_id} 不是停用狀態")
        if machine_id not in self._repaired_out:
            raise ValueError(f"{machine_id} 還沒檢修完成，不能恢復運轉")
        eid = self._repaired_out.pop(machine_id)
        self.sim.set_machine_status(machine_id, MachineStatus.RUNNING, "主管恢復運轉")
        self.c.detector.reset(machine_id, repaired=True)
        self.audit.append(AuditKind.SYSTEM, actor, {"msg": "恢復運轉", "machine_id": machine_id, "after": "檢修完成"}, eid)
        self._publish_periodic(force=True)

    def _available_presses(self, size: Any) -> int:
        """Curing presses that can take this size and are not out of service (a Safety Core hold is temporary)."""
        pool = F.SMALL_ONLY_PRESSES if getattr(size, "value", size) == "S" else F.ML_PRESSES
        status_of = getattr(self.sim, "status_of", None)
        if not callable(status_of):
            return len(pool)
        return sum(1 for p in pool if status_of(p) is not MachineStatus.OUT_OF_SERVICE)

    def _preplan_inputs(self, order_id: str) -> tuple:
        """What a pre-plan's answer depends on: the order's due date, quantity and size, and the presses that can cure
        it. A pre-plan that found nothing to change runs again only when one of these changed (#104)."""
        o = next((x for x in (self.sim.orders() if callable(getattr(self.sim, "orders", None)) else []) if x.id == order_id),
                 None)
        if o is None:
            return (order_id,)
        return (o.due, o.qty, o.done, getattr(o.size, "value", o.size), self._available_presses(o.size))

    @staticmethod
    def _preplan_order(ev: Any) -> str:
        return (getattr(ev, "command", None) or "").split(" ", 1)[0]

    def precheck_line(self, order_id: str) -> str | None:
        """The report for a pre-plan that found nothing to change (CJ, 2026-10-22: 「計劃已經先擬好，只等核准或討論」):
        what was checked and what to do, e.g. 「已預先檢查：SO-012 明天無法交齊（7 台小胎機都滿），建議聯絡客戶延交或分批」."""
        o = next((x for x in (self.sim.orders() if callable(getattr(self.sim, "orders", None)) else []) if x.id == order_id),
                 None)
        if o is None:
            return None
        days = (o.due - self.clock.now().date()).days
        when = "今天" if days <= 0 else "明天" if days == 1 else f"{o.due:%m/%d} 前"
        kind = "小胎機" if getattr(o.size, "value", o.size) == "S" else "中大胎機"
        return f"已預先檢查：{order_id} {when}無法交齊（{self._available_presses(o.size)} 台{kind}都滿），建議聯絡客戶延交或分批"

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
            if ev.machine_id == machine_id and ev.state not in TERMINAL_STATES and \
                    getattr(ev, "source", "detector") != "supervisor":    # a typed task is not a machine alarm
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
                    # off the event loop (issue #36: the switch froze /ws and every request ~5 s): the plant and
                    # the seed schedule solve run in a worker thread. Nothing ticks meanwhile (_switching, loop
                    # cancelled); the shared clock is lock-guarded; the bus and audit are only constructed into.
                    comps = await asyncio.to_thread(self.builder.build, new_scenario, sleep=self.sim_sleep,
                                                    fresh_store=True, staged=True)
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
                self.overlays = []
                self._clear_ask_memory("scenario loaded")
                self.c = comps
                _ = self.orch                      # the main agent hooks the new state machine at once
                self.ingest.reset()
                self.safety_log.clear()
                self.anomalies.clear()
                self._baseline.clear()
                self._oob_seen.clear()
                self._repaired.clear()             # a fix still waiting for the old scenario's VERIFY
                self._repaired_out.clear()
                self._recovering.clear()
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
            self._publish_schedule()

    async def reset(self) -> None:
        await self.load_scenario(self.scenario)

    # ------------------------------------------------------------------ issue #36 一日循環
    async def _day_loop_tick(self) -> None:
        sim = self.sim
        if sim._day_snap is None:
            sim.snapshot_day()                  # 00:00 of the first loop: the plant every later loop starts from
        if self.clock.now() >= sim.day_end:
            await self._wrap_day()

    async def _wrap_day(self) -> None:
        """24:00 -> 00:00 of the same date, without rebuilding anything (no freeze): open events are cancelled
        (their approvals retire), the schedule goes back to the day's first version, the plant (machines, cure
        cycles, orders, goods, people, trucks) to its start-of-day snapshot, and the detector, validation and
        baselines start fresh so the seam raises nothing. The audit log keeps going, with a marker."""
        ended = self.clock.now()
        self._clear_ask_memory("day loop wrapped")
        await self._cancel_events()
        self._wake_sleepers(cancel=True)
        store = self.c.store
        try:
            if store is not None and store.current().version != 1:
                store.rollback(1)
        except Exception:  # noqa: BLE001 - a store without v1 history keeps what it has
            log.exception("day loop: schedule not reset")
        n = self.sim.restore_day()
        self.c.detector.reset(None)
        self.ingest.reset()
        self._baseline.clear()
        self._oob_seen.clear()
        self._world_obj, self._world_rev = None, -1            # next WORLD message is a full one
        self.audit.append(AuditKind.SYSTEM, "day_loop", {"msg": f"新的一天（循環 {n}）", "loop": n,
                                                         "ended_at": ended.isoformat(),
                                                         "factory_time": self.clock.now().isoformat()})
        day0 = getattr(self.sim, "day0", None) or ended
        self.memory_desk.on_wrap(day0.astimezone(F.TZ).date().isoformat(), n - 1)   # 循環 n-1's daily summary
        self._publish_periodic(force=True)
        self._publish_schedule()

    def day_loop_info(self) -> dict[str, Any] | None:
        sim = self.sim
        if getattr(sim, "day_end", None) is None:
            return None
        return {"loop": sim.loop_n, "day": sim.day0.date().isoformat(), "source_day": "2026-10-21"}

    JUMP_STEP_S = 10.0                          # the world's own grid; a 24 h jump is ~8.6k ticks

    async def jump_to(self, hhmm: str) -> float:
        """一日循環: run the plant forward to the next HH:MM (through the 24:00 wrap when it is earlier today), in
        10 s ticks through the normal path. Returns the factory seconds run."""
        if getattr(self.sim, "day_end", None) is None:
            raise RuntimeError("jump works in the 一日循環 (day_loop) only")
        h, m = map(int, hhmm.split(":"))
        now = self.clock.now()
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        left = (target - now).total_seconds() % (24 * 3600)
        done = 0.0
        while done < left:
            dt = min(self.JUMP_STEP_S, left - done)
            await self.tick(dt, publish=False, hold_compute=False)
            done += dt
        self._publish_periodic(force=True)
        self.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "跳到時間", "at": hhmm, "seconds": done})
        return done

    PREROLL_LEAD_S = 15.0

    async def preroll(self) -> float:
        """Issue #36: a demo button lands ~15 factory s before the scenario's first scripted step instead of minutes
        before it. The plant runs those seconds headless through the normal tick (safety core, detector, world),
        so what happens next is exactly what would have happened at 1x. Returns the seconds run."""
        sc = getattr(self.sim, "scenario", None)
        if getattr(sc, "name", None) is ScenarioName.DAY_LOOP:
            return 0.0                          # the loop is live time: its 08:00 step is not a "first fault"
        offsets = [s.at_offset_s for s in getattr(sc, "steps", []) or []]
        lead = max(0.0, min(offsets) - self.PREROLL_LEAD_S) if offsets else 0.0
        if lead >= 1.0:
            await self.run_seconds(int(lead))
        return float(int(lead)) if lead >= 1.0 else 0.0

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

    # ------------------------------------------------------------------ issue #36 模擬事件
    def _random_event(self) -> None:
        try:
            if self.sim_events.due(self.clock.now()):
                self.fire_sim_event(self.sim_events.pick(), source="random")
        except Exception:  # noqa: BLE001 - a demo generator must never stop the plant loop
            log.exception("random event failed")

    def fire_sim_event(self, kind: str, source: str = "manual") -> dict[str, Any]:
        """Inject one 模擬事件. A machine event is a FaultSpec in the sim (the detector must see it in the readings,
        exactly like POST /api/sim/fault); a plant event is a PlantInput (the next tick opens a plant event in the
        state machine, exactly like a scripted rush order). Nothing here calls a tool or the gate."""
        from warroom.contracts import FaultSpec
        from warroom.sim.events import fault_text
        now = self.clock.now()
        out = self.sim_events.build(kind, self.sim, now, self._busy_machines())
        from warroom.sim.events import WorldInput
        if isinstance(out, FaultSpec):
            self.inject_fault(out)
            fired = self.sim_events.log(now, kind, fault_text(out), source, out.machine_id)
        elif isinstance(out, WorldInput):        # material: world feed and stock only (cards: #38 patrol)
            world = getattr(self.sim, "world", None)
            if world is None:
                raise RuntimeError("this sim has no world")
            if out.kind == "delivery_late" and out.po_id:
                world.delay_delivery(now, out.po_id, out.late_h or 6.0, out.text)
            else:
                world.empty_stock(now, out.material)
            fired = self.sim_events.log(now, out.kind, out.text, source, out.po_id or out.material)
        else:
            apply = getattr(self.sim, "apply_plant_input", None)
            if not callable(apply):
                raise RuntimeError("this sim takes no plant events")
            apply(out, now)
            ref = out.order.id if out.order else out.staff_id
            fired = self.sim_events.log(now, kind, out.text, source, ref)
        self.audit.append(AuditKind.SYSTEM, "supervisor" if source == "manual" else "sim_events",
                          {"msg": "模擬事件", "kind": fired.kind, "text": fired.text, "source": source,
                           "seed": self.sim_events.seed})
        return {"at": fired.at.isoformat(), "kind": fired.kind, "label": fired.label, "text": fired.text,
                "source": source, "ref": fired.ref}

    def _busy_machines(self) -> set[str]:
        """Machines with an open event or an active fault (a new fault is never stacked on them)."""
        return {m for m in F.MACHINE_IDS if self._open_event_id(m)} | \
            {f.machine_id for f in (self.sim.faults() if callable(getattr(self.sim, "faults", None)) else [])}

    # ------------------------------------------------------------------ 情境疊加 (scenario overlay)
    async def overlay_scenario(self, name: ScenarioName) -> dict[str, Any]:
        """Apply an overlayable scenario's steps on top of the running one, relative to now: the first step starts
        at once and the others keep their spacing (offset - first offset). Clock, events, approvals, schedule
        versions and audit are kept; nothing is rebuilt. A fault whose machine is busy (open event, active or
        coming fault, stopped / out of service) goes to an idle machine of the same kind (events.substitute_machine)
        so the overlay still opens its full set of events; with none free the step is skipped and says so. Faults go
        through inject_fault and plant inputs through the sim's plant-input queue, the same paths as
        POST /api/sim/fault and a scripted rush order. Story scenarios (page 11, 一日循環, 早班開工, 正常) raise
        ValueError: they always reload."""
        from warroom.contracts import OverlayResult, OverlayStep
        from warroom.sim.events import machine_label, machine_short, substitute_machine
        from warroom.sim.scenarios import OVERLAY_SCENARIOS, get_scenario
        name = ScenarioName(name)
        if name not in OVERLAY_SCENARIOS:
            raise ValueError(f"{name.value} 是故事情境，只能重新載入")
        if self._switch_lock is None:
            self._switch_lock = asyncio.Lock()
        async with self._switch_lock:              # never in the middle of a scenario load
            now = self.clock.now()
            base = get_scenario(name)
            first = min((s.at_offset_s for s in base.steps), default=0.0)
            sc = get_scenario(name, start_at=now - timedelta(seconds=first))    # dates (a rush order's due) from now
            unusable = {MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE, MachineStatus.OFFLINE}
            states = self.sim.states() if callable(getattr(self.sim, "states", None)) else {}
            scheduled = getattr(self.sim, "scheduled_fault_machines", None)
            busy = self._busy_machines() | (scheduled() if callable(scheduled) else set()) | \
                {m for m, s in states.items() if s.status in unusable}
            taken = {s.fault.machine_id for s in sc.steps if s.fault is not None}   # this overlay's own targets
            n_ov = len(self.overlays) + 1
            out: list[OverlayStep] = []
            for st in sc.steps:
                at = now + timedelta(seconds=st.at_offset_s - first)
                note = st.note or ""
                if st.fault is not None:
                    f = st.fault
                    mid, sub, detail = f.machine_id, None, None
                    if mid in busy:
                        sub = substitute_machine(mid, busy | taken, f.kind)
                        if sub is None:
                            out.append(OverlayStep(note=note, machine_id=mid, skipped=True, at=at,
                                                   detail=f"略過：{machine_short(mid)}處理中，沒有空的同類機台"))
                            continue
                        detail = f"改用 {machine_label(sub)}（{machine_short(mid)}處理中）"
                        mid, sub = sub, mid
                        taken.add(mid)
                    spec = f.model_copy(update={"id": f"{f.id}-OV{n_ov}-{now:%H%M%S}", "machine_id": mid,
                                                "start_offset_s": f.start_offset_s + st.at_offset_s - first})
                    self.inject_fault(spec)
                    wakes = f.kind not in (FaultKind.SPIKE, FaultKind.SENSOR_DROPOUT, FaultKind.SENSOR_DRIFT)
                    if sub is not None and note:
                        note = f"{note}（改用 {machine_label(mid)}）"
                    out.append(OverlayStep(note=note, machine_id=mid, substituted_for=sub, detail=detail,
                                           wakes=wakes, at=at))
                elif st.plant is not None:
                    pin, detail = self._overlay_plant_input(st.plant, at)
                    sched = getattr(self.sim, "schedule_plant_input", None)
                    if not callable(sched) or pin is None:
                        out.append(OverlayStep(note=note, skipped=True, wakes=False, at=at,
                                               detail=detail or "略過：這個模擬器不收工廠事件"))
                        continue
                    sched(pin, at)
                    out.append(OverlayStep(note=pin.text if detail else note, detail=detail, at=at))
            res = OverlayResult(scenario=name, title=sc.title, at=now, steps=out,
                                new_events=sum(1 for s in out if s.wakes and not s.skipped))
            self.overlays.append(name)
            self.audit.append(AuditKind.SYSTEM, "supervisor",
                              {"msg": "scenario overlaid", "scenario": name.value, "on": self.scenario.value,
                               "factory_time": now.isoformat(), "new_events": res.new_events,
                               "steps": [s.model_dump(mode="json", exclude_none=True) for s in out]})
        self._publish_periodic(force=True)
        return res.model_dump(mode="json")

    def _overlay_plant_input(self, pin: Any, at: datetime) -> tuple[Any, str | None]:
        """A plant step for an overlay: a rush order whose id is already on the book gets the next free id, a sick
        call for someone already off that day goes to another person of the same team and shift. Returns the input
        (None = skip) and the substitution note."""
        if pin.kind == "rush_order" and pin.order is not None:
            ids = {o.id for o in self.sim.orders()} if callable(getattr(self.sim, "orders", None)) else set()
            oid = pin.order.id
            if oid not in ids:
                return pin, None
            n = 2
            while f"{oid}-{n}" in ids:
                n += 1
            new = f"{oid}-{n}"
            order = pin.order.model_copy(update={"id": new, "mold_id": f"{pin.order.mold_id}-{n}"})
            return pin.model_copy(update={"order": order, "text": pin.text.replace(oid, new)}), \
                f"訂單號改用 {new}（{oid} 已在訂單上）"
        if pin.kind == "absence" and pin.staff_id:
            world = getattr(self.sim, "world", None)
            off = getattr(world, "_extra_absent", {}) if world is not None else {}
            if (pin.staff_id, at.date()) not in off:
                return pin, None
            for w in getattr(world, "_workers", []):
                if w.staff_id and w.staff_id != pin.staff_id and w.team == pin.team and \
                        getattr(w.sd, "name", None) == pin.shift_name and (w.staff_id, at.date()) not in off:
                    return pin.model_copy(update={"staff_id": w.staff_id,
                                                  "text": pin.text.replace(pin.staff_id, w.staff_id)}), \
                        f"改由 {w.staff_id} 請病假（{pin.staff_id} 今天已請假）"
            return None, f"略過：{pin.shift_name}沒有其他可請假的人"
        return pin, None

    def clear_faults(self, machine_id: str | None = None) -> None:
        self.sim.clear_faults(machine_id)
        self.c.detector.reset(machine_id, repaired=machine_id is not None)
        self.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "faults cleared", "machine_id": machine_id})

    def clock_info(self) -> dict[str, Any]:
        ev = self.sim_events
        return {"factory_time": self.clock.now().isoformat(), "speed": self.speed, "paused": self.paused,
                "scenario": self.scenario.value, "day_loop": self.day_loop_info(),
                "overlays": [o.value for o in self.overlays],
                "random_events": {"enabled": ev.enabled, "every_min": ev.every_min, "seed": ev.seed,
                                  "fired": len(ev.fired), "last": ev.fired[-1].text if ev.fired else None}}

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

    @property
    def memory_notes(self) -> Any:
        """The supervisor's per-machine notes from 後台 (warroom/data/memory_notes.py), next to the audit log."""
        mn = self.__dict__.get("_memory_notes")
        if mn is None:
            from warroom.data.memory_notes import MemoryNotes
            mn = self.__dict__["_memory_notes"] = MemoryNotes(Path(self.settings.audit_path).parent / "memory_notes.json")
        return mn

    def machine_memory(self, machine_id: str, exclude: str | None = None) -> dict[str, Any] | None:
        """One machine's notes and last past events, compact (warroom/data/machine_memory.py), or None. Read-only."""
        from warroom.data import machine_memory as MM
        return MM.for_machine(machine_id, self.memory_notes, self.audit, self.events(), exclude,
                              files=self.memory_desk.files)

    @property
    def memory_desk(self) -> Any:
        """Daily / weekly / monthly summaries and observations (warroom/api/memory_desk.py), all derived from the audit."""
        d = self.__dict__.get("_memory_desk")
        if d is None:
            from warroom.api.memory_desk import MemoryDesk
            d = self.__dict__["_memory_desk"] = MemoryDesk(self)
        return d

    @property
    def history_log(self) -> Any:
        """Chat turns and order events on disk (data/runtime/history.sqlite), append-only: kept across restarts and
        resets; a reset only starts a new chat session id."""
        h = self.__dict__.get("_history_log")
        if h is None:
            from warroom.data.history_log import HistoryLog
            h = self.__dict__["_history_log"] = HistoryLog(Path(self.settings.audit_path).parent / "history.sqlite")
            self.memory_resets.append(h.new_session)
        return h

    @property
    def tasklog(self) -> Any:
        """今天處理過的任務 from the audit file (warroom/api/tasklog.py), never cleared."""
        tl = self.__dict__.get("_tasklog")
        if tl is None:
            from warroom.api.tasklog import TaskLog
            path = getattr(self.audit, "path", None)
            tl = TaskLog(path) if path is not None and str(path) else TaskLog(entries=lambda: self.audit.tail(0, 0))
            self.__dict__["_tasklog"] = tl
        return tl

    def _clear_ask_memory(self, why: str) -> None:
        """Forget every 問戰情室 conversation (issue #95); the audit log says how many and why."""
        try:
            n = self.ask_memory.reset()
            for forget in self.memory_resets:
                forget()
            if n:
                self.audit.append(AuditKind.SYSTEM, "system", {"msg": "ask memory cleared", "why": why,
                                                               "conversations": n, "generation": self.ask_memory.generation})
        except Exception:  # noqa: BLE001 - forgetting must never block a scenario switch
            log.exception("ask memory not cleared")

    def orders_today(self) -> dict[str, Any]:
        """Today's orders from the one rule set (warroom/tools/orders_today.py): the dashboard's order list, the
        ACTIVE schedule and the tyres in a press now. Read only; behind GET /api/orders/today and 問戰情室."""
        from warroom.tools import orders_today
        return orders_today.collect(self.sim, self.c.store, self.clock.now())

    def ask_context(self) -> dict[str, Any]:
        """What the 問戰情室 planner sees: factory time, abnormal machines, open events."""
        abnormal = [t for t in self.machine_tiles() if t.status not in NORMAL_STATUSES]
        return {"factory_time": self.clock.now().isoformat(),
                "abnormal": [t.machine_id for t in abnormal],
                "abnormal_temp_c": {t.machine_id: t.temp_c for t in abnormal},
                "open_events": [{"id": e.id, "machine_id": e.machine_id, "state": e.state.value, "goal": e.goal}
                                for e in self.events() if e.closed_at is None]}

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
        # WARNING with an open event = the fast repair's 回溫中 (amber, StateMachine.on_repaired), not the red alarm
        if m.status in (MachineStatus.RUNNING, MachineStatus.IDLE) and self._open_event_id(m.machine_id):
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
                                   temp_c=m.last_reading.temp_c if m.last_reading else None,
                                   step_progress=self._step_progress(m),
                                   repaired=m.machine_id in self._repaired_out and m.status is MachineStatus.OUT_OF_SERVICE))
        return out

    @staticmethod
    def _step_progress(m: MachineState) -> float | None:
        """Issue #36: the 3D ring above a machine. A press: cure elapsed / required; any other machine: minutes
        into its batch / the batch length (physics.NOMINALS cycle_min). None when there is no batch."""
        if m.cure_job is not None:
            req = m.cure_job.cure_required_h
            return round(min(1.0, max(0.0, m.cure_job.cure_elapsed_h / req)), 3) if req > 0 else None
        if m.batch_id is None:
            return None
        from warroom.sim.physics import NOMINALS
        nom = NOMINALS.get(m.machine_id)
        if nom is None or nom.cycle_min <= 0:
            return None
        return round(min(1.0, max(0.0, m.step_elapsed_min / nom.cycle_min)), 3)

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
        if ev.machine_id not in F.MACHINE_BY_ID:
            # a plant event (issue #17) or a supervisor command about the whole plant: no machine, no tiles
            auto_done, pending = self._actions_for(ev)
            kind = {"shift_start": "早班開工", "order_rush": "插單", "staffing_short": "人力缺口",
                    "supervisor_command": "主管指令"}.get(ev.anomaly.kind.value, "工廠事件")
            return FocusPanel(event_id=ev.id, machine_id=ev.machine_id, title=f"{kind}：{ev.goal or ev.anomaly.message}"[:60],
                              detail=ev.anomaly.message, tiles=[], auto_done=auto_done, pending=pending,
                              schedule_impact=self.schedule_impact(ev), state=ev.state, timeline=ev.timeline,
                              approved=self._approved_for(ev), revoked=self._revoked_for(ev))
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
                          approved=self._approved_for(ev), revoked=self._revoked_for(ev))

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
        done_ids: set[str] = set()
        ri = 0
        for req in reqs:
            d = req.decision
            who = d.decided_by if d else "supervisor"
            opt_id = d.option_id if d else None

            def owns(sr: Any) -> bool:      # the option step it spawned, or the gated step it was raised for
                return sr.step.id.startswith(f"{req.id}-") or (
                    sr.step.id == req.step_id and sr.gate is not None
                    and sr.gate.risk in (RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL))
            # an approval carried over after a stale schedule (state machine: "<id>-rebased") ran in a
            # later round than the one it carries over
            start = ri + 1 if "-rebased" in req.id and req.id.split("-rebased")[0] in done_ids else ri
            # card n is raised in plan round n or later (a discussion or a card-less replan adds plan rounds, never
            # cards), so the search starts at its own round: from the previous card's round, round 2's card (gated
            # step "S4" again) matched round 1, whose confirm went stale, and read 「未生效」 while its v4 was in force
            start = max(start, req.round - 1)
            # v2 of an approved card whose confirm went stale (group B 10-21: same round, 「另一事件的排程先生效」) ran in
            # the plan round after v1's, not in v1's own
            if getattr(req, "supersedes", None) and req.supersedes in done_ids:
                start = max(start, ri + 1)
            done_ids.add(req.id)
            j = next((k for k in range(start, len(rounds)) if any(owns(sr) for sr in rounds[k])), None)
            ran: list[Any] = []
            if j is not None:
                ri = j
                ran = [sr for sr in rounds[j] if sr.state == "ok" and sr.gate is not None
                       and sr.gate.risk in (RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL)
                       and (sr.step.id.startswith(f"{req.id}-") or not sr.step.id.startswith("AP-"))
                       and (j, sr.step.id) not in used]
            for sr in ran:
                used.add((j, sr.step.id))
                if _is_revoke(sr.step.id):
                    continue              # a compensating withdrawal: ↺ 已撤回 (_revoked_for), never ✓ 已核准
                out.append(ApprovedAction(approval_id=req.id, option_id=opt_id, label=self._approved_label(sr),
                                          decided_by=who, status=req.status))
            if not ran:
                opt = next((o for o in req.options if o.id == opt_id), None) or \
                    (req.options[0] if len(req.options) == 1 else None)
                label = opt.label if opt else req.title
                if j is not None and any(owns(sr) and sr.state == "failed" and sr.step.tool is ToolName.CONFIRM_SCHEDULE
                                         for sr in rounds[j]):
                    # approved, but its schedule went stale before it could take effect (another event's
                    # version won): never shown as if it were in force
                    label += "（✗ 未生效：排程已被另一事件更新）"
                out.append(ApprovedAction(approval_id=req.id, option_id=opt_id,
                                          label=label, decided_by=who, status=req.status))
        return out

    def _revoked_for(self, ev: Event) -> list[str]:
        """↺ 已撤回: staffing an earlier round ran that the supervisor's later choice replaced (the
        state machine sends a compensating notice). Shown with its cause, never as an approval."""
        import re
        out = []
        for sr in self._all_steps(ev):
            if not _is_revoke(sr.step.id) or sr.state != "ok":
                continue
            what = sr.step.purpose.removeprefix("撤回").strip() or "先前人力調動"
            m = re.search(r"（主管改選(.+?)）", str(sr.step.args.get("message") or ""))
            out.append(f"{what}（因主管改選{m.group(1)}）" if m else what)
        return out

    @staticmethod
    def _all_steps(ev: Event) -> list[Any]:
        """Every StepRun of the event, oldest round first (a replan moves earlier rounds' steps to
        ev.prior_rounds when the event model carries them)."""
        out: list[Any] = []
        for rnd in getattr(ev, "prior_rounds", None) or []:
            out += list(getattr(rnd, "steps", None) or [])
        return out + list(ev.steps)

    @staticmethod
    def _confirm_version(sr: Any, before: list[Any]) -> int | None:
        """The schedule version a confirm_schedule step activates ("$S3.schedule_version" resolved
        against the closest earlier step with that id)."""
        v = sr.result.data.get("version") if sr.result and sr.result.ok and sr.result.data else None
        if isinstance(v, int):
            return v
        v = sr.step.args.get("schedule_version")
        if isinstance(v, str) and v.startswith("$"):
            ref, _, path = v[1:].partition(".")
            src = next((b for b in reversed(before) if b.step.id == ref and b.result and b.result.ok), None)
            v = (src.result.data or {}).get(path or "schedule_version") if src else None
        return v if isinstance(v, int) else None

    def _schedule_rounds(self, ev: Event) -> tuple[list[tuple[int | None, dict]], list[int], set[int]]:
        """(solves [(version, data)], versions this event put into force, versions refused)."""
        steps = self._all_steps(ev)
        solves: list[tuple[int | None, dict]] = []
        activated: list[int] = []
        refused: set[int] = set()
        for i, sr in enumerate(steps):
            if sr.step.tool is ToolName.SOLVE_SCHEDULE and sr.result and sr.result.ok:
                data = sr.result.data or {}
                v = data.get("schedule_version")
                solves.append((v if isinstance(v, int) else None, data))
            elif sr.step.tool is ToolName.CONFIRM_SCHEDULE:
                v = self._confirm_version(sr, steps[:i])
                if v is None:
                    continue
                if sr.state == "ok":
                    if v not in activated:
                        activated.append(v)
                elif sr.state in ("failed", "skipped", "rejected"):
                    refused.add(v)
        return solves, activated, refused

    @staticmethod
    def _moves_phrase(data: dict) -> str:
        from warroom.agent.narrate import moves_text
        mv = moves_text(data.get("moves"))            # same grouping as the phone card (count per press)
        return "、".join(mv) if mv else "機台不變"

    @staticmethod
    def _delivery_tail(data: dict) -> str:
        late = data.get("late_orders") or []
        return "交期不受影響" if data.get("deliveries_met", not late) else f"延誤：{'、'.join(late)}"

    def schedule_impact(self, ev: Event) -> str:
        """③ 排程影響. Only what this event put into force counts as the schedule; a proposal is
        shown as 待核准 while it can still be approved, and never after it was refused or the event
        ended (safe stop keeps the approved version, not the discarded proposal)."""
        solves, activated, refused = self._schedule_rounds(ev)
        if solves:
            by_v = {v: d for v, d in solves if v is not None}
            terminal = ev.state in TERMINAL_STATES
            lv, ldata = solves[-1]
            proposal_open = not terminal and lv not in activated and lv not in refused
            parts: list[str] = []
            if activated:
                av = activated[-1]
                adata = by_v.get(av)
                desc = self._moves_phrase(adata) if adata is not None else "已重排"
                keep = terminal and (lv not in activated)
                head = f"維持已核准 v{av}" if keep else f"已生效 v{av}"
                parts.append(f"{head}：{desc}")
                if not proposal_open:
                    parts.append(self._delivery_tail(adata) if adata is not None else "交期不受影響")
                    if keep and lv is not None:
                        parts.append(f"v{lv} 未生效")
            if proposal_open:
                tag = f"待核准 v{lv}" if lv is not None else "待核准"
                parts.append(f"{tag}：{self._moves_phrase(ldata)}")
                parts.append(self._delivery_tail(ldata))
            elif not terminal and any(sr.step.tool is ToolName.SOLVE_SCHEDULE and sr.state in ("pending", "running")
                                      for sr in ev.steps):
                parts.append("重排計算中…")
            if parts:
                return "；".join(parts)
            # solved, nothing put into force, nothing open: the original schedule stands
            return "保留原排程（未重排）"
        # No solver result yet. A press event, or any plan that schedules a solve, is still being
        # computed; the page-2 "大胎 #07 仍 17:00 入硫化機" sentence belongs only to the
        # event on the big open mill, where #07's A part is preheating (open_mill_overheat).
        solve_runs = [sr for sr in ev.steps if sr.step.tool is ToolName.SOLVE_SCHEDULE]
        plans_solve = bool(solve_runs) or bool(ev.plan and any(
            st.tool is ToolName.SOLVE_SCHEDULE for st in ev.plan.steps))
        is_press = ev.machine_id in F.MACHINE_BY_ID and F.MACHINE_BY_ID[ev.machine_id].line.value == "CURE"
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
        marks = self.bad_data_marks()
        return HeaderStatus(normal_count=normal, abnormal_count=len(tiles) - normal, factory_time=self.clock.now(),
                            llm_mode=self.llm_mode, network=self.network, warning_light=self.warning_light(),
                            bad_data_total=sum(m.count for m in marks), bad_data=marks)

    BAD_DATA_WINDOW_MIN = 30.0

    def bad_data_marks(self) -> list[BadDataMark]:
        """Page 10: readings the validation layer rejected in the last 30 sim-minutes, per machine
        (one count per rejected reading, whatever the number of bad channels in it)."""
        log = getattr(self.c.detector, "bad_data_log", None)
        if not log:
            return []
        since = self.clock.now() - timedelta(minutes=self.BAD_DATA_WINDOW_MIN)
        seen: set[tuple[Any, str]] = set()
        agg: dict[str, list[Any]] = {}
        try:
            for ts, mid, _ch, q in list(log):
                if ts < since or (ts, mid) in seen:
                    continue
                seen.add((ts, mid))
                a = agg.setdefault(mid, [0, q, ts])
                a[0] += 1
                if ts >= a[2]:
                    a[1], a[2] = q, ts
        except Exception:  # noqa: BLE001 - a fallback detector with another log shape shows no marks
            return []
        order = {m: i for i, m in enumerate(F.MACHINE_IDS)}
        return [BadDataMark(machine_id=m, count=a[0], last_quality=a[1], last_at=a[2])
                for m, a in sorted(agg.items(), key=lambda kv: order.get(kv[0], 99))]

    def warning_light(self) -> bool:
        """Page 9 tier 3: with the LAN down the phone cannot be reached, so the on-site warning light
        calls a person to the touch screen whenever an approval is waiting or an event is open."""
        if self._safety_unrecorded:            # a safety action acted on the plant but is not audited yet
            return True
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
        # a SAFE_STOP nobody resolved yet stays on the list however old (its buttons are the way out)
        closed += [e for e in evs if e.state is EventState.SAFE_STOP and e not in closed
                   and (e.follow_up is None or e.follow_up.status == "manual")]
        from warroom.api import progress as P
        wip = self.__dict__.setdefault("_wip", P.WipTracker())
        return DashboardSnapshot(header=self.header(), orders=P.order_progress(self.sim, self.events(), self.c.store),
                                 semi_finished=wip.semi_finished(self.sim, self.focus_event()),
                                 focus=self.focus_panel(),
                                 machines=self.machine_tiles(), events=open_ + closed,
                                 pending_approvals=list(self.approvals.pending()),
                                 safety_actions=list(reversed(self.safety_log[-20:])),
                                 safety_cases=self._case_view())

    def _case_view(self) -> list[Any]:
        try:
            return self.cases.view(self._board())
        except Exception:  # noqa: BLE001 - the snapshot must never fail on this
            log.exception("safety case view")
            return []

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
        cure_conflicts = self._rollback_cure_conflicts(target)
        for c in cure_conflicts:
            reasons.append(c["text"])
        cur_jobs = {j.id: j for j in cur.schedule.jobs}
        tgt_jobs = {j.id: j for j in target.schedule.jobs}
        moved, retimed = [], 0
        for jid, tj in tgt_jobs.items():
            cj = cur_jobs.get(jid)
            if cj is None or tj.mold_in < now:
                continue
            if cj.press_id != tj.press_id:
                moved.append({"job_id": jid, "order_id": tj.order_id, "from_press": cj.press_id,
                              "to_press": tj.press_id, "to_mold_in": tj.mold_in.isoformat()})
            elif cj.mold_in != tj.mold_in:
                retimed += 1                    # same press, new time: counted, never shown as a 'move'
        revoked, restored = self._rollback_staffing_diff(cur.schedule, target.schedule)
        cross = any(c.kind == "transfer" for c in target.schedule.staffing_changes)
        cross = cross or any(c.kind == "transfer" for c in revoked)     # undoing a cross-team move is one too
        risk = RiskLevel.R3_CRITICAL if cross else RiskLevel.R2_CHANGE
        return {"from_version": cur.version, "to_version": target.version, "target_status": target.status.value,
                "target_reason": target.reason, "feasible": not reasons, "reasons": reasons, "risk": risk.value,
                "moved": moved[:20], "moved_count": len(moved), "retimed_count": retimed,
                "added": len(set(tgt_jobs) - set(cur_jobs)), "removed": len(set(cur_jobs) - set(tgt_jobs)),
                "staffing_changes": [c.label for c in target.schedule.staffing_changes],
                "staffing_revoked": [self._staffing_text(c) for c in revoked],
                "staffing_restored": [self._staffing_text(c) for c in restored],
                "cure_conflicts": cure_conflicts, "blocked_presses": blocked}

    def _rollback_cure_conflicts(self, target: Any) -> list[dict[str, Any]]:
        """A tire already in a press cures for what the plant (cure formula / approved set_cure_time)
        says, whatever version is in force. A version that plans that tire shorter would de-mold it
        early and put the next mold-in before the real de-mold, so the rollback is refused with the
        numbers (page 11: physics numbers come from the formula, never from an older plan)."""
        from warroom.agent.narrate import machine_label
        plan = {j.id: j for j in target.schedule.jobs}
        by_press_in: dict[tuple[str, datetime], Any] = {(j.press_id, j.mold_in): j for j in target.schedule.jobs}
        out = []
        try:
            machines = self.sim.machines()
        except Exception:  # noqa: BLE001
            return out
        for m in machines:
            cj = m.cure_job
            if cj is None or m.machine_id not in F.CURE_PRESS_IDS:
                continue
            tj = plan.get(cj.job_id) or by_press_in.get((m.machine_id, cj.mold_in_at))
            if tj is None or tj.cure_h + 0.05 >= cj.cure_required_h:
                continue
            real_demold = cj.mold_in_at + timedelta(hours=cj.cure_required_h)
            nxt = min((j for j in target.schedule.jobs if j.press_id == m.machine_id and j.mold_in > tj.mold_in),
                      key=lambda j: j.mold_in, default=None)
            text = (f"v{target.version} 排 {machine_label(m.machine_id)} 在模胎 {cj.order_id} 硫化 {tj.cure_h:g} h，"
                    f"但依硫化公式／已核准設定需 {cj.cure_required_h:.1f} h（{real_demold:%m/%d %H:%M} 才出模）")
            if nxt is not None and nxt.mold_in < real_demold:
                text += f"；v{target.version} 的下一模 {nxt.order_id} {nxt.mold_in:%m/%d %H:%M} 入模會早於出模"
            out.append({"press_id": m.machine_id, "job_id": cj.job_id, "plan_cure_h": tj.cure_h,
                        "required_cure_h": round(cj.cure_required_h, 2), "real_demold": real_demold.isoformat(),
                        "text": text + "。請改以重排處理"})
        return out

    @staticmethod
    def _staffing_key(c: Any) -> tuple:
        return (c.kind, c.shift_date, c.shift_name, getattr(c.to_team, "value", c.to_team),
                getattr(c.from_team, "value", c.from_team), c.people)

    def _rollback_staffing_diff(self, cur: Any, target: Any) -> tuple[list[Any], list[Any]]:
        """(changes in force now that the rollback revokes, changes the rollback brings back)."""
        now = self.clock.now()
        cur_c = list(getattr(cur, "staffing_changes", None) or [])
        tgt_c = list(getattr(target, "staffing_changes", None) or [])
        tk = {self._staffing_key(c) for c in tgt_c}
        ck = {self._staffing_key(c) for c in cur_c}
        live = lambda c: c.shift_date >= (now - timedelta(days=1)).date()   # noqa: E731 - past shifts are history
        return ([c for c in cur_c if self._staffing_key(c) not in tk and live(c)],
                [c for c in tgt_c if self._staffing_key(c) not in ck and live(c)])

    @staticmethod
    def _staffing_text(c: Any) -> str:
        team = F.TEAM_LABELS.get(c.to_team, getattr(c.to_team, "value", str(c.to_team)))
        return f"{c.shift_date:%m/%d} {c.shift_name} {c.label}（{team}）"

    def after_rollback(self, pv: dict[str, Any], version: Any, actor: str, approval_id: str) -> None:
        """What an approved rollback changes for people: every staffing change it revokes or brings
        back is sent to the affected teams (R1 info notice; the rollback itself was the approval), and
        the event that produced the rolled-back version gets a timeline row, so its outcome no longer
        reads as if that version were still in force."""
        from warroom.contracts import NotifyKind, TimelineItem, ToolCall
        store = self.c.store
        cur = store.get(pv["from_version"])
        revoked, restored = self._rollback_staffing_diff(cur.schedule, version.schedule)
        for verb, changes in (("撤回", revoked), ("恢復", restored)):
            for c in changes:
                teams = [c.to_team] + ([c.from_team] if c.kind == "transfer" and c.from_team else [])
                msg = (f"排程回復 v{pv['from_version']} → v{version.version}：{verb} {c.shift_date:%m/%d} "
                       f"{c.shift_name}{c.label}")
                res = None
                if self.c.tools is not None:
                    res = self.c.tools.call(ToolCall(id=f"{approval_id}-{verb}-{c.shift_date:%m%d}-{c.kind}",
                                                     tool=ToolName.NOTIFY_TEAMS,
                                                     args={"teams": [t.value for t in teams],
                                                           "kind": NotifyKind.INFO.value, "message": msg}))
                self.audit.append(AuditKind.SYSTEM, actor,
                                  {"msg": "rollback staffing notice", "text": msg, "approval_id": approval_id,
                                   "teams": [t.value for t in teams], "delivered": bool(res and res.ok)},
                                  event_id=cur.event_id)
        eid = cur.event_id
        sm = self.c.state_machine
        if not eid or sm is None:
            return
        try:
            ev = sm.get(eid)
        except Exception:  # noqa: BLE001 - not an agent event (manual / seed version)
            return
        extra = "；".join(f"撤回 {t}" for t in pv.get("staffing_revoked") or [])
        ev.timeline.append(TimelineItem(ts=self.clock.now(), actor="主管", tag="human",
                                        text=f"排程回復 v{pv['from_version']} → v{version.version}（{actor} 核准）"
                                             + (f"；{extra}" if extra else "")))
        note = f"排程已回復 v{version.version}（v{pv['from_version']} 不再生效）"
        ev.outcome = f"{ev.outcome}；{note}" if ev.outcome else note
        self.bus.publish(WsType.EVENT, ev)

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
