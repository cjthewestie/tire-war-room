"""Report the state machine's tool and commander calls as NAT intermediate steps.

The state machine stays the same object; only its `tools` and `commander` are swapped for these
proxies, which forward every call unchanged and emit a START/END pair around it. An `Emitter` is
any callable taking a StepEvent, so tests can record events without NAT installed; nat_emitter()
pushes them into NAT's IntermediateStepManager for the current NAT context.
"""
from __future__ import annotations

import inspect
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from warroom.contracts import CommanderRequest, ToolCall, ToolResult


@dataclass
class StepEvent:
    kind: str                       # "TOOL_START" | "TOOL_END" | "LLM_START" | "LLM_END"
    name: str
    step_id: str
    ts: float
    input: Any = None
    output: Any = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


Emitter = Callable[[StepEvent], None]


class TracedTools:
    """ToolRegistry proxy: acall/call are forwarded; each call emits TOOL_START and TOOL_END."""

    def __init__(self, inner: Any, emit: Emitter):
        self._inner, self._emit = inner, emit

    def __getattr__(self, name: str) -> Any:          # specs(), openai_tools(), register(), ctx ...
        return getattr(self._inner, name)

    def _start(self, call: ToolCall) -> str:
        sid = str(uuid.uuid4())
        self._emit(StepEvent("TOOL_START", call.tool.value, sid, time.time(), input=call.args,
                             metadata={"event_id": call.event_id, "step_id": call.step_id, "attempt": call.attempt}))
        return sid

    def _end(self, sid: str, call: ToolCall, res: ToolResult) -> None:
        self._emit(StepEvent("TOOL_END", call.tool.value, sid, time.time(),
                             input=call.args, output=res.data if res.ok else {"error": res.error},
                             metadata={"event_id": call.event_id, "ok": res.ok, "duration_ms": res.duration_ms}))

    def call(self, call: ToolCall) -> ToolResult:
        sid = self._start(call)
        res = self._inner.call(call)
        self._end(sid, call, res)
        return res

    async def acall(self, call: ToolCall) -> ToolResult:
        sid = self._start(call)
        inner = getattr(self._inner, "acall", None)
        res = await inner(call) if inner is not None else self._inner.call(call)
        self._end(sid, call, res)
        return res


class TracedCommander:
    """CommanderClient proxy: plan() is forwarded; each call emits LLM_START and LLM_END with the
    model, phase, TTFT and token counts from LLMCallInfo."""

    def __init__(self, inner: Any, emit: Emitter):
        self._inner, self._emit = inner, emit
        try:
            self._streams = "on_alert" in inspect.signature(inner.plan).parameters
        except (TypeError, ValueError):
            self._streams = False

    def __getattr__(self, name: str) -> Any:          # mode, server_kind, last_raw ...
        return getattr(self._inner, name)

    async def plan(self, req: CommanderRequest, on_alert: Any = None):
        sid = str(uuid.uuid4())
        model = str(getattr(self._inner, "model", None) or getattr(getattr(self._inner, "settings", None), "model", "")
                    or getattr(self._inner, "mode", "commander"))
        self._emit(StepEvent("LLM_START", model, sid, time.time(),
                             input={"phase": req.phase, "event_id": req.event_id,
                                    "allowed_tools": [t.value for t in req.allowed_tools]},
                             metadata={"phase": req.phase}))
        if self._streams and on_alert is not None:
            plan, info = await self._inner.plan(req, on_alert=on_alert)
        else:
            plan, info = await self._inner.plan(req)
        self._emit(StepEvent("LLM_END", model, sid, time.time(),
                             input={"phase": req.phase},
                             output=plan.model_dump(mode="json"),
                             prompt_tokens=info.prompt_tokens or 0, completion_tokens=info.completion_tokens or 0,
                             metadata={"phase": req.phase, "mode": str(info.mode), "ttft_ms": info.ttft_ms,
                                       "total_ms": info.total_ms, "valid_json": info.valid_json,
                                       "fell_back_to_mock": info.fell_back_to_mock}))
        return plan, info


def instrument(state_machine: Any, emit: Emitter) -> None:
    """Swap the state machine's tools and commander for traced proxies (idempotent)."""
    if not isinstance(state_machine.tools, TracedTools):
        state_machine.tools = TracedTools(state_machine.tools, emit)
    if not isinstance(state_machine.commander, TracedCommander):
        state_machine.commander = TracedCommander(state_machine.commander, emit)


def nat_emitter() -> Emitter:
    """Emitter that pushes into NAT's event stream for the current NAT context. Imports NAT lazily."""
    from nat.builder.context import Context
    from nat.data_models.intermediate_step import (IntermediateStepPayload, IntermediateStepType, StreamEventData,
                                                   UsageInfo)
    from nat.data_models.token_usage import TokenUsageBaseModel

    mgr = Context.get().intermediate_step_manager
    starts: dict[str, float] = {}

    def emit(ev: StepEvent) -> None:
        usage = None
        if ev.kind == "LLM_END":
            usage = UsageInfo(token_usage=TokenUsageBaseModel(
                prompt_tokens=ev.prompt_tokens, completion_tokens=ev.completion_tokens,
                total_tokens=ev.prompt_tokens + ev.completion_tokens), num_llm_calls=1)
        if ev.kind.endswith("_START"):
            starts[ev.step_id] = ev.ts
        mgr.push_intermediate_step(IntermediateStepPayload(
            UUID=ev.step_id, event_type=IntermediateStepType(ev.kind), event_timestamp=ev.ts,
            span_event_timestamp=starts.pop(ev.step_id, None) if ev.kind.endswith("_END") else None,
            name=ev.name, metadata=ev.metadata,
            data=StreamEventData(input=ev.input, output=ev.output), usage_info=usage))

    return emit
