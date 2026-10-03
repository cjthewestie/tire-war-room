from __future__ import annotations

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

    def call(self, call: ToolCall) -> ToolResult:
        t0 = time.perf_counter()

        def fail(msg: str) -> ToolResult:
            return ToolResult(call_id=call.id, tool=call.tool, ok=False, error=msg[:500],
                              duration_ms=round((time.perf_counter() - t0) * 1000, 2), attempt=call.attempt)
        try:
            name = ToolName(call.tool)
        except ValueError:
            return fail(f"tool {call.tool!r} is not on the allow-list")
        fn, spec = self._fns.get(name), self._specs.get(name)
        if fn is None or spec is None:
            return fail(f"tool {name.value} not registered")
        args = dict(call.args)
        if name is ToolName.SOLVE_SCHEDULE:        # keep CP-SAT inside the tool's own budget
            try:
                args["time_limit_s"] = min(float(args.get("time_limit_s", 10.0)), spec.timeout_s / 2)
            except (TypeError, ValueError):
                pass
        try:
            if spec.kind is ToolKind.WRITE or name in INLINE_TOOLS:
                with self._busy_lock:
                    self.inflight += 1
                try:
                    data = fn(self.ctx, args)
                finally:
                    with self._busy_lock:
                        self.inflight -= 1
            else:
                data = self._pool.submit(fn, self.ctx, args).result(timeout=spec.timeout_s)
        except cf.TimeoutError:
            return fail(f"timeout after {spec.timeout_s:g} s")
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(str(x) for x in er['loc'])}: {er['msg']}" for er in e.errors()[:5])
            return fail(f"invalid args: {errs}")
        except Exception as e:  # noqa: BLE001 - never raise to the state machine
            return fail(f"{type(e).__name__}: {e}")
        return ToolResult(call_id=call.id, tool=name, ok=True, data=data or {},
                          duration_ms=round((time.perf_counter() - t0) * 1000, 2), attempt=call.attempt)


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
