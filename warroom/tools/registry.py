from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from warroom.contracts import (AuditLog, Bus, Clock, Detector, FactorySim, Scheduler, ScheduleStore, SopKB,
                               ToolCall, ToolName, ToolResult, ToolSpec)


@dataclass
class ToolContext:
    """Dependency injection for tool implementations. Any field may be None in tests."""
    sim: FactorySim | None = None
    detector: Detector | None = None
    kb: SopKB | None = None
    scheduler: Scheduler | None = None
    store: ScheduleStore | None = None
    audit: AuditLog | None = None
    bus: Bus | None = None
    clock: Clock | None = None


class Registry:
    """Implements contracts.ToolRegistry. call() validates args against the *Args model,
    enforces timeout_s, never raises (ok=False + error). Write tools only *record* intent
    (schedule store activate, notification on the bus) — no PLC/actuator writes exist."""

    def __init__(self, ctx: ToolContext):
        raise NotImplementedError

    def register(self, spec: ToolSpec, fn: Callable[[ToolContext, dict[str, Any]], dict[str, Any]]) -> None:
        raise NotImplementedError

    def specs(self, names: list[ToolName] | None = None) -> list[ToolSpec]: raise NotImplementedError
    def openai_tools(self, names: list[ToolName]) -> list[dict[str, Any]]: raise NotImplementedError
    def call(self, call: ToolCall) -> ToolResult: raise NotImplementedError


def build_registry(ctx: ToolContext) -> Registry:
    """Registry with all 9 tools registered."""
    raise NotImplementedError
