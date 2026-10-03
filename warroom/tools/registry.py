from __future__ import annotations

import asyncio
import concurrent.futures as cf
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from warroom.contracts import (
    AuditLog,
    Bus,
    Clock,
    Detector,
    FactorySim,
    Scheduler,
    ScheduleStore,
    SopKB,
    ToolCall,
    ToolKind,
    ToolName,
    ToolResult,
    ToolSpec,
)

# Tools run in a worker thread so timeout_s is enforced. Write tools (they publish on the asyncio bus,
# which is not thread-safe) and solve_schedule (the state machine already runs it off the loop, and
# CP-SAT has its own time limit + heuristic backup) run in the calling thread.
INLINE_TOOLS = frozenset({ToolName.SOLVE_SCHEDULE})

ToolFn = Callable[["ToolContext", dict[str, Any]], dict[str, Any]]


@dataclass
class ToolContext:
    """Dependency injection for tool implementations. Any field may be None in tests.
    orders_db (optional, kb.OrdersDB): when set, query_orders_shifts reads the Excel-backed DB
    instead of the live sim."""
    sim: FactorySim | None = None
    detector: Detector | None = None
    kb: SopKB | None = None
    scheduler: Scheduler | None = None
    store: ScheduleStore | None = None
    audit: AuditLog | None = None
    bus: Bus | None = None
    clock: Clock | None = None
    orders_db: Any = None


class Registry:
    """Implements contracts.ToolRegistry. call() validates args against the *Args model,
    enforces timeout_s, never raises (ok=False + error). Write tools only *record* intent
    (schedule store activate, notification on the bus) — no PLC/actuator writes exist.

    Only names in contracts.ToolName can be registered (the allow-list); anything else is refused
    at register() and at call()."""

    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self._specs: dict[ToolName, ToolSpec] = {}
        self._fns: dict[ToolName, ToolFn] = {}
        self._pool = cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix="tool")
        self._busy_lock = threading.Lock()
        self.inflight = 0      # long compute calls running now; the Runtime holds sim time while > 0

    def register(self, spec: ToolSpec, fn: ToolFn) -> None:
        name = ToolName(spec.name)          # raises if not on the allow-list
        self._specs[name] = spec
        self._fns[name] = fn

    def specs(self, names: list[ToolName] | None = None) -> list[ToolSpec]:
        if names is None:
            return list(self._specs.values())
        return [self._specs[ToolName(n)] for n in names if ToolName(n) in self._specs]

    def openai_tools(self, names: list[ToolName]) -> list[dict[str, Any]]:
        return [{"type": "function",
                 "function": {"name": s.name.value, "description": s.description, "parameters": s.params_schema}}
                for s in self.specs(names)]

    def _prepare(self, call: ToolCall, t0: float) -> ToolResult | tuple[ToolName, ToolFn, ToolSpec, dict[str, Any]]:
        try:
            name = ToolName(call.tool)
        except ValueError:
            return self._fail(call, t0, f"tool {call.tool!r} is not on the allow-list")
        fn, spec = self._fns.get(name), self._specs.get(name)
        if fn is None or spec is None:
            return self._fail(call, t0, f"tool {name.value} not registered")
        args = dict(call.args)
        if name is ToolName.SOLVE_SCHEDULE:        # keep CP-SAT inside the tool's own budget
            try:
                args["time_limit_s"] = min(float(args.get("time_limit_s", 10.0)), spec.timeout_s / 2)
            except (TypeError, ValueError):
                pass
        return name, fn, spec, args

    @staticmethod
    def _fail(call: ToolCall, t0: float, msg: str) -> ToolResult:
        return ToolResult(call_id=call.id, tool=call.tool, ok=False, error=msg[:500],
                          duration_ms=round((time.perf_counter() - t0) * 1000, 2), attempt=call.attempt)

    def _finish(self, call: ToolCall, t0: float, name: ToolName, spec: ToolSpec,
                data: dict[str, Any] | None, exc: BaseException | None) -> ToolResult:
        if exc is not None:
            if isinstance(exc, (cf.TimeoutError, asyncio.TimeoutError, TimeoutError)):
                return self._fail(call, t0, f"timeout after {spec.timeout_s:g} s")
            if isinstance(exc, ValidationError):
                errs = "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in exc.errors()[:5])
                return self._fail(call, t0, f"invalid args: {errs}")
            return self._fail(call, t0, f"{type(exc).__name__}: {exc}")
        return ToolResult(call_id=call.id, tool=name, ok=True, data=data or {},
                          duration_ms=round((time.perf_counter() - t0) * 1000, 2), attempt=call.attempt)

    def _run_inline(self, fn: ToolFn, args: dict[str, Any]) -> dict[str, Any]:
        with self._busy_lock:
            self.inflight += 1
        try:
            return fn(self.ctx, args)
        finally:
            with self._busy_lock:
                self.inflight -= 1

    def call(self, call: ToolCall) -> ToolResult:
        """Synchronous call. Blocks the calling thread for up to timeout_s — never call this from the
        asyncio loop thread for a read tool; use acall() there so the safety tick keeps running."""
        t0 = time.perf_counter()
        prep = self._prepare(call, t0)
        if isinstance(prep, ToolResult):
            return prep
        name, fn, spec, args = prep
        data: dict[str, Any] | None = None
        exc: BaseException | None = None
        try:
            if spec.kind is ToolKind.WRITE or name in INLINE_TOOLS:
                data = self._run_inline(fn, args)
            else:
                data = self._pool.submit(fn, self.ctx, args).result(timeout=spec.timeout_s)
        except Exception as e:  # noqa: BLE001 - never raise to the state machine
            exc = e
        return self._finish(call, t0, name, spec, data, exc)

    async def acall(self, call: ToolCall) -> ToolResult:
        """Async call for the event loop: never blocks the loop on a read/compute tool, so the
        Runtime's tick (and safety_pass) keeps its <0.2 s cadence while an event runs on a cold KB.
        - read/compute tools: run on the tool pool, awaited with timeout_s;
        - solve_schedule: run in a worker thread (CP-SAT has its own time limit);
        - write tools: run on the loop thread, because they publish on the loop-bound bus; they only
          record intent (store activate, notification) and return in milliseconds.
        Never raises."""
        t0 = time.perf_counter()
        prep = self._prepare(call, t0)
        if isinstance(prep, ToolResult):
            return prep
        name, fn, spec, args = prep
        data: dict[str, Any] | None = None
        exc: BaseException | None = None
        if spec.kind is ToolKind.WRITE:
            try:
                data = self._run_inline(fn, args)
            except Exception as e:  # noqa: BLE001 - never raise to the state machine
                exc = e
            return self._finish(call, t0, name, spec, data, exc)
        # Off-loop work counts as in flight: the Runtime then holds the SIM plant clock (after the
        # tick's safety_pass) exactly as it did when the call blocked the loop, so the page-11 beats
        # keep their sim timing — but the loop itself stays free.
        with self._busy_lock:
            self.inflight += 1
        try:
            loop = asyncio.get_running_loop()
            pool = None if name in INLINE_TOOLS else self._pool
            data = await asyncio.wait_for(loop.run_in_executor(pool, fn, self.ctx, args), spec.timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - never raise to the state machine
            exc = e
        finally:
            with self._busy_lock:
                self.inflight -= 1
        return self._finish(call, t0, name, spec, data, exc)

def build_registry(ctx: ToolContext) -> Registry:
    """Registry with all 9 tools registered."""
    from warroom.tools import impl
    from warroom.tools.specs import TOOL_SPECS
    fns: dict[ToolName, ToolFn] = {
        ToolName.READ_SENSORS: impl.read_sensors,
        ToolName.PREDICT_ANOMALY: impl.predict_anomaly,
        ToolName.CURE_FORMULA: impl.cure_formula,
        ToolName.SOLVE_SCHEDULE: impl.solve_schedule,
        ToolName.SOP_SEARCH: impl.sop_search,
        ToolName.QUERY_ORDERS_SHIFTS: impl.query_orders_shifts,
        ToolName.CONFIRM_SCHEDULE: impl.confirm_schedule,
        ToolName.SET_CURE_TIME: impl.set_cure_time,
        ToolName.NOTIFY_TEAMS: impl.notify_teams,
    }
    reg = Registry(ctx)
    for name, fn in fns.items():
        reg.register(TOOL_SPECS[name], fn)
    return reg
