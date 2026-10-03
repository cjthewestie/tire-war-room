"""Event State Machine (page 5, page 7, page 11).

One Event per wake-up from the detector. States follow contracts.ALLOWED_TRANSITIONS:

  DETECTED → CLASSIFY → PLAN → SIMULATE → GATE → [EXECUTE (auto R1)] → [AWAIT_APPROVAL] → EXECUTE
  → VERIFY → CLOSED;  SIMULATE/GATE/AWAIT_APPROVAL/VERIFY → REPLAN → SIMULATE;  any → SAFE_STOP.

- Tools are opened by state (TOOLS_BY_STATE): every call goes through ``_call`` which refuses and
  logs (INTERCEPTED) a tool that is not open in the current state. Writes run only in EXECUTE.
- The commander (only LLM) is called in PLAN and REPLAN; it returns a CommanderPlan. "$S1.x"
  references in step args are resolved here from earlier ToolResult.data.
- VERIFY is code: it polls the machine reading and compares it with Hypothesis.expectation. A miss
  rejects the hypothesis and goes to REPLAN with the evidence (page 11: 119.5 °C → H1 不成立).
- Failure policy (page 7): a failed tool is retried MAX_TOOL_RETRIES times, then the SOP path
  (REPLAN with sop_path context); solver timeout → backup solver happens inside the scheduler and
  is shown on the timeline; MAX_CONSECUTIVE_FAILURES → SAFE_STOP, rolling the schedule store back
  to the version that was active when the event opened.
- Every transition, LLM call, tool call/result, gate decision and interception is audited; every
  change is published on the bus (WsType.EVENT / ALERT_CARD).

The runtime should call ``on_safety_action(action)`` for each SafetyAction from the safety core:
it audits it (SAFETY_ACTION) and attaches it to the machine's open event.
"""
from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from warroom.agent.narrate import (
    CHANNEL_LABELS,
    decompose_text,
    expectation_text,
    fmt_num,
    machine_label,
    result_text,
    staffing_label,
)
from warroom.contracts import (
    ALLOWED_TRANSITIONS,
    MAX_CONSECUTIVE_FAILURES,
    MAX_TOOL_RETRIES,
    TERMINAL_STATES,
    TOOLS_BY_STATE,
    WRITE_TOOLS,
    ActionGate,
    AlertCard,
    Anomaly,
    ApprovalBroker,
    ApprovalOption,
    ApprovalRequest,
    ApprovalStatus,
    AuditKind,
    AuditLog,
    Bus,
    Channel,
    Clock,
    CommanderClient,
    CommanderPlan,
    CommanderRequest,
    DecisionKind,
    Event,
    EventState,
    FactorySim,
    GateDecision,
    GateVerdict,
    Hypothesis,
    HypothesisStatus,
    Line,
    MachineStatus,
    NotifyKind,
    PlanStep,
    RiskLevel,
    SafetyAction,
    StepRun,
    TimelineItem,
    ToolCall,
    ToolName,
    ToolRegistry,
    ToolResult,
    WsType,
)
from warroom.factory import CURE_TEMP_MAX_C, CURE_TEMP_MIN_C, MACHINE_BY_ID, MACHINES, TZ, presses_on_header

RISK_ORDER = [RiskLevel.R0_READ, RiskLevel.R1_NOTIFY, RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL]
STAFFING_KINDS = (NotifyKind.OVERTIME.value, NotifyKind.STAFF_MOVE.value)
_REF = re.compile(r"^\$(S\d+)\.([\w.\[\]-]+)$")
_INLINE_REF = re.compile(r"\$\{(S\d+)\.([\w.\[\]-]+)\}")
_MISSING = object()


# =====================================================================================
# "$S1.total_cure_h" resolution
# =====================================================================================
def _lookup(data: Any, path: str) -> Any:
    cur = data
    for part in path.replace("[", ".").replace("]", "").split("."):
        if part == "":
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return _MISSING
    return cur


def resolve_refs(value: Any, results: dict[str, ToolResult]) -> Any:
    """Replace "$S1.path" (whole value) and "${S1.path}" (inside a string) with ToolResult.data
    values. Unresolvable references are left as-is, so the gate's format check rejects them."""
    if isinstance(value, str):
        m = _REF.match(value)
        if m:
            res = results.get(m.group(1))
            got = _lookup(res.data, m.group(2)) if res is not None and res.ok else _MISSING
            return value if got is _MISSING else got

        def sub(mm: re.Match) -> str:
            res = results.get(mm.group(1))
            got = _lookup(res.data, mm.group(2)) if res is not None and res.ok else _MISSING
            return mm.group(0) if got is _MISSING else fmt_num(got) if isinstance(got, (int, float)) else str(got)
        return _INLINE_REF.sub(sub, value)
    if isinstance(value, dict):
        return {resolve_refs(k, results) if isinstance(k, str) else k: resolve_refs(v, results)
                for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, results) for v in value]
    return value


def _brief(data: dict[str, Any], limit: int = 3000) -> dict[str, Any]:
    """Keep audit payloads small: drop raw sample arrays, keep summaries."""
    try:
        s = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return {"repr": repr(data)[:limit]}
    if len(s) <= limit:
        return data
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k == "readings" and isinstance(v, dict):
            out[k] = {m: f"{len(x)} samples" for m, x in v.items()}
        elif k == "schedule" and isinstance(v, dict):
            out[k] = {"id": v.get("id"), "jobs": len(v.get("jobs") or []),
                      "staffing_changes": v.get("staffing_changes")}
        else:
            vs = json.dumps(v, ensure_ascii=False, default=str)
            out[k] = v if len(vs) <= 800 else f"<{len(vs)} chars>"
    return out


# =====================================================================================
# Run state for one event
# =====================================================================================
@dataclass
class _Run:
    ev: Event
    prev_schedule_version: int | None = None
    plan: CommanderPlan | None = None
    plan_at: datetime | None = None
    results: dict[str, ToolResult] = field(default_factory=dict)
    feasible: bool = True
    approvals_made: int = 0
    done: list[str] = field(default_factory=list)            # 已做 (for the phone card)
    executed: list[str] = field(default_factory=list)        # outcome summary
    context_tools: list[str] = field(default_factory=list)   # phrases for the commander timeline row
    replan_tools: list[str] = field(default_factory=list)
    base_context: dict[str, Any] = field(default_factory=dict)


class StateMachine:
    """Implements contracts.EventStateMachine (page 5/7/11). See module docstring.

    Extra keyword args (all optional, defaults suit the live runtime):
      store              ScheduleStore, for rollback on SAFE_STOP (keep the original schedule).
      sleep              async fn(sim_seconds) used by VERIFY polling; default waits on the clock.
      approval_timeout_s wall seconds to wait for the supervisor before SAFE_STOP.
      verify_poll_s      sim seconds between VERIFY reads.
      verify_confirm_s   a miss must persist this long (sim s) before the hypothesis is rejected;
                         5 min by default so sensor noise around the threshold of a slow thermal
                         mass does not reject a 30-min hypothesis (hard limits are the safety core's).
      max_replans        hypothesis swaps allowed before SAFE_STOP.
    """

    def __init__(self, commander: CommanderClient, tools: ToolRegistry, gate: ActionGate,
                 approvals: ApprovalBroker, audit: AuditLog, sim: FactorySim | None = None,
                 bus: Bus | None = None, clock: Clock | None = None, *, store=None,
                 sleep: Callable[[float], Awaitable[None]] | None = None, approval_timeout_s: float = 600.0,
                 verify_poll_s: float = 5.0, verify_confirm_s: float = 300.0, max_replans: int = 3,
                 max_events_kept: int = 200):
        self.commander, self.tools, self.gate = commander, tools, gate
        self.approvals, self.audit, self.sim, self.bus = approvals, audit, sim, bus
        self.clock = clock or (getattr(sim, "clock", None) if sim is not None else None)
        self.store = store
        self._sleep_fn = sleep
        self.approval_timeout_s = approval_timeout_s
        self.verify_poll_s, self.verify_confirm_s = verify_poll_s, verify_confirm_s
        self.max_replans = max_replans
        self._max_kept = max_events_kept
        self._events: dict[str, Event] = {}
        self._open: dict[str, str] = {}          # machine_id -> open event id
        self._ids = itertools.count(1)
        try:
            self._commander_streams = "on_alert" in inspect.signature(commander.plan).parameters
        except (TypeError, ValueError):
            self._commander_streams = False
        try:
            self._gate_takes_cross_team = "cross_team" in inspect.signature(gate.check).parameters
        except (TypeError, ValueError):
            self._gate_takes_cross_team = False

    # ------------------------------------------------------------------ public API
    def events(self) -> list[Event]:
        return list(self._events.values())

    def get(self, event_id: str) -> Event:
        return self._events[event_id]

    def open_event_for(self, machine_id: str) -> Event | None:
        eid = self._open.get(machine_id)
        return self._events.get(eid) if eid else None

    def on_safety_action(self, action: SafetyAction) -> None:
        """Red path record: audit the safety core's action and attach it to the open event."""
        ev = self.open_event_for(action.machine_id)
        self.audit.append(AuditKind.SAFETY_ACTION, "safety", action.model_dump(mode="json"),
                          event_id=ev.id if ev else None)
        if ev is not None:
            ev.safety_actions.append(action)
            self._note(ev, "安全核心", f"[規則] {action.message}（直接處置，不等 AI）", "safety", ts=action.ts)

    async def handle(self, anomaly: Anomaly) -> Event:
        existing = self.open_event_for(anomaly.machine_id)
        if existing is not None:
            self.audit.append(AuditKind.ANOMALY, "detector", anomaly.model_dump(mode="json"), event_id=existing.id)
            self._note(existing, "事件偵測", f"追加異常：{anomaly.message}", "info", ts=anomaly.ts)
            return existing
        ev = self._open_event(anomaly)
        run = _Run(ev=ev, prev_schedule_version=self._active_version())
        try:
            await self._run(run)
        except asyncio.CancelledError:
            if ev.state not in TERMINAL_STATES:
                self._safe_stop(run, "事件處理被取消")
            raise
        except Exception as e:  # noqa: BLE001 - never leave an event half-open
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"error": f"{type(e).__name__}: {e}"[:500], "state": ev.state.value}, event_id=ev.id)
            if ev.state not in TERMINAL_STATES:
                self._safe_stop(run, f"內部錯誤：{type(e).__name__}")
        finally:
            if self._open.get(ev.machine_id) == ev.id:
                del self._open[ev.machine_id]
            self._trim()
        return ev

    # ------------------------------------------------------------------ helpers: time, bus, audit
    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now(TZ)

    async def _sleep(self, seconds: float) -> None:
        if self._sleep_fn is not None:
            await self._sleep_fn(seconds)
            return
        if self.clock is None:
            await asyncio.sleep(seconds)
            return
        target = self.clock.now() + timedelta(seconds=seconds)
        speed = float(getattr(self.clock, "speed", 1.0) or 1.0)
        while self.clock.now() < target:
            remaining = (target - self.clock.now()).total_seconds()
            await asyncio.sleep(min(0.25, max(0.01, remaining / speed)))

    def _publish(self, kind: WsType, data: Any) -> None:
        if self.bus is not None:
            try:
                self.bus.publish(kind, data)
            except Exception:  # noqa: BLE001
                pass

    def _note(self, ev: Event, actor: str, text: str, tag: str = "info", ts: datetime | None = None) -> None:
        ev.timeline.append(TimelineItem(ts=ts or self._now(), actor=actor, text=text, tag=tag))
        self._publish(WsType.EVENT, ev)

    def _to(self, ev: Event, new: EventState, reason: str = "") -> None:
        old = ev.state
        if new not in ALLOWED_TRANSITIONS[old]:
            raise RuntimeError(f"illegal transition {old.value} -> {new.value}")
        ev.state = new
        self.audit.append(AuditKind.STATE_TRANSITION, "state_machine",
                          {"from": old.value, "to": new.value, "reason": reason}, event_id=ev.id)
        self._publish(WsType.EVENT, ev)

    def _active_version(self) -> int | None:
        if self.store is None:
            return None
        try:
            return self.store.current().version
        except Exception:  # noqa: BLE001
            return None

    def _trim(self) -> None:
        if len(self._events) <= self._max_kept:
            return
        for eid in [e.id for e in self._events.values() if e.state in TERMINAL_STATES][: len(self._events) -
                                                                                         self._max_kept]:
            del self._events[eid]

    def _set_status(self, machine_id: str, status: MachineStatus, note: str | None = None) -> None:
        if self.sim is None:
            return
        try:
            self.sim.set_machine_status(machine_id, status, note)
        except Exception:  # noqa: BLE001
            pass

    def _machine_state(self, machine_id: str):
        if self.sim is None:
            return None
        try:
            return self.sim.machine(machine_id)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ DETECTED
    def _open_event(self, a: Anomaly) -> Event:
        base = f"EV-{a.ts.astimezone(TZ):%Y%m%d-%H%M%S}-{a.machine_id}"
        eid, n = base, 2
        while eid in self._events:
            eid, n = f"{base}-{n}", n + 1
        ev = Event(id=eid, opened_at=self._now(), machine_id=a.machine_id, anomaly=a, state=EventState.DETECTED)
        self._events[eid] = ev
        self._open[a.machine_id] = eid
        self.audit.append(AuditKind.ANOMALY, "detector", a.model_dump(mode="json"), event_id=eid)
        self.audit.append(AuditKind.STATE_TRANSITION, "state_machine",
                          {"from": None, "to": EventState.DETECTED.value, "reason": "detector wake-up"}, event_id=eid)
        self._note(ev, "事件偵測", f"[工具] {a.message} → 喚醒指揮官", "tool", ts=a.ts)
        # Instant card from detector data (no LLM) so the screen shows something at once; the
        # commander's card replaces it as soon as it streams in.
        self._set_alert(ev, AlertCard(title=f"{machine_label(a.machine_id)}異常", summary=a.message))
        self._set_status(a.machine_id, MachineStatus.ALARM, a.message)
        self._safety_status_note(ev)
        return ev

    def _set_alert(self, ev: Event, card: AlertCard) -> None:
        ev.alert_card = card
        self._publish(WsType.ALERT_CARD, {"event_id": ev.id, "card": card.model_dump(mode="json")})

    def _safety_status_note(self, ev: Event) -> None:
        """Report what the deterministic safety core is doing for this machine (it acts on its own)."""
        st = self._machine_state(ev.machine_id)
        if st is None:
            return
        mdef = MACHINE_BY_ID.get(ev.machine_id)
        temp = st.last_reading.temp_c if st.last_reading else None
        if st.status is MachineStatus.STOPPED:
            text = "已依規則停機（不等 AI）"
        elif mdef is not None and mdef.line is Line.CURE and temp is not None:
            if temp >= CURE_TEMP_MIN_C:
                text = (f"仍在 {CURE_TEMP_MIN_C:.0f}–{CURE_TEMP_MAX_C:.0f}°C，不停機"
                        f"（低於 {CURE_TEMP_MIN_C:.0f}°C 直接處置，不等 AI）")
            else:
                text = f"低於 {CURE_TEMP_MIN_C:.0f}°C，依規則扣模（不等 AI）"
        elif mdef is not None and mdef.handles_compound and temp is not None:
            from warroom.contracts import CompoundStage
            from warroom.factory import COMPOUND_STOP_C, CURATIVE_STOP_C
            limit = CURATIVE_STOP_C if st.compound_stage is CompoundStage.CURATIVE_ADDED else COMPOUND_STOP_C
            text = (f"膠溫 {fmt_num(temp)}°C 未達 {limit:.0f}°C 停機線，不停機（達標直接處置，不等 AI）"
                    if temp < limit else f"膠溫 {fmt_num(temp)}°C ≥ {limit:.0f}°C，依規則停機（不等 AI）")
        else:
            return
        self._note(ev, "安全核心", text, "safety")

    # ------------------------------------------------------------------ main loop
    async def _run(self, run: _Run) -> None:
        ev = run.ev
        self._to(ev, EventState.CLASSIFY, "gather sensor window, prediction and SOP")
        run.base_context = await self._classify_async(run)
        self._to(ev, EventState.PLAN, "ask commander")
        plan = await self._ask(run, "plan", {})
        if plan is None or plan.safe_stop:
            self._safe_stop(run, "指揮官要求安全中止" if plan else "指揮官無回應")
            return

        while True:
            # ---------------- SIMULATE
            self._to(ev, EventState.SIMULATE, "run read/compute steps")
            failure = await self._simulate(run)
            if failure is not None:
                if self._fail(run, f"工具失敗：{failure['tool']}"):
                    return
                if not await self._replan(run, {"sop_path": True, "failure": failure}, "工具重試仍失敗 → SOP 路徑"):
                    return
                continue
            if not run.feasible:
                if self._fail(run, "模擬不可行"):
                    return
                if not await self._replan(run, {"infeasible": True}, "模擬不可行 → 換方案"):
                    return
                continue

            # ---------------- GATE
            self._to(ev, EventState.GATE, "check write steps")
            auto, deferred, needs, options, req_risk = self._gate(run)

            if auto:
                self._to(ev, EventState.EXECUTE, "R1 auto writes")
                await self._execute(run, auto, auto_tag=True)

            if needs:
                self._to(ev, EventState.AWAIT_APPROVAL, f"{req_risk.value} approval")
                req = self._request_approval(run, needs, options, req_risk)
                res = await self.approvals.wait(req.id, timeout_s=self.approval_timeout_s)
                if res.status is ApprovalStatus.EXPIRED or res.decision is None:
                    self._note(ev, "系統", "主管逾時未回覆 → 不變更，維持原排程", "dynamic")
                    self._safe_stop(run, "核准逾時")
                    return
                self._note_decision(run, res)
                if res.decision.decision is DecisionKind.REJECT:
                    for sr in ev.steps:
                        if sr.state == "awaiting_approval":
                            sr.state = "rejected"
                    for sr in deferred:
                        sr.state = "skipped"
                    if self._fail(run, "主管否決"):
                        return
                    if not await self._replan(run, {"supervisor_rejected": {"reason": res.decision.reason}},
                                              "主管否決 → 換方案"):
                        return
                    continue
                chosen = self._chosen_steps(run, needs, options, res) + deferred
                self._to(ev, EventState.EXECUTE, f"approved by {res.decision.decided_by}")
                await self._execute(run, chosen, auto_tag=False)
            elif not auto:
                self._to(ev, EventState.EXECUTE, "no write steps")

            # ---------------- VERIFY
            self._to(ev, EventState.VERIFY, "compare expectation with readings (code)")
            ok, observed = await self._verify(run)
            hyp = ev.hypotheses[-1]
            if ok:
                hyp.status = HypothesisStatus.CONFIRMED if hyp.expectation else HypothesisStatus.ACTIVE
                self._close(run)
                return
            hyp.status = HypothesisStatus.REJECTED
            if self._fail(run, f"{hyp.id} 不成立"):
                return
            ctx = {"failed_expectation": {"hypothesis_id": hyp.id, "statement": hyp.statement,
                                          "expectation": hyp.expectation.model_dump(mode="json")
                                          if hyp.expectation else None,
                                          "observed": observed, "at": self._now().isoformat()}}
            if not await self._replan(run, ctx, f"{hyp.id} 不成立 → 換假設"):
                return

    def _fail(self, run: _Run, why: str) -> bool:
        """Count a failure; True when the event has been safe-stopped."""
        ev = run.ev
        ev.consecutive_failures += 1
        self.audit.append(AuditKind.SYSTEM, "state_machine",
                          {"failure": why, "consecutive_failures": ev.consecutive_failures}, event_id=ev.id)
        if ev.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self._note(ev, "系統", f"連續 {ev.consecutive_failures} 次失敗（{why}）→ 安全中止，維持原排程", "dynamic")
            self._safe_stop(run, f"連續 {ev.consecutive_failures} 次失敗")
            return True
        return False

    # ------------------------------------------------------------------ tool calls
    async def _call(self, run: _Run, state: EventState, step_id: str | None, tool: ToolName,
                    args: dict[str, Any]) -> ToolResult:
        ev = run.ev
        if state is not ev.state or tool not in TOOLS_BY_STATE.get(state, frozenset()):
            self.audit.append(AuditKind.INTERCEPTED, "state_machine",
                              {"tool": tool.value, "state": state.value, "step_id": step_id, "args": args,
                               "reason": "tool not open in this state"}, event_id=ev.id)
            return ToolResult(call_id=f"blocked-{next(self._ids)}", tool=tool, ok=False,
                              error=f"{tool.value} not open in {state.value}")
        res: ToolResult | None = None
        for attempt in range(1, MAX_TOOL_RETRIES + 2):
            call = ToolCall(id=f"{ev.id}:{step_id or 'ctx'}:{next(self._ids)}", event_id=ev.id, step_id=step_id,
                            tool=tool, args=args, attempt=attempt)
            self.audit.append(AuditKind.TOOL_CALL, f"tool:{tool.value}",
                              {"call_id": call.id, "step_id": step_id, "args": args, "attempt": attempt,
                               "state": state.value}, event_id=ev.id)
            try:
                if tool is ToolName.SOLVE_SCHEDULE:      # CP-SAT may take seconds: keep the loop free
                    res = await asyncio.to_thread(self.tools.call, call)
                else:
                    res = self.tools.call(call)
            except Exception as e:  # noqa: BLE001 - registry should never raise; be safe anyway
                res = ToolResult(call_id=call.id, tool=tool, ok=False, error=f"{type(e).__name__}: {e}",
                                 attempt=attempt)
            self.audit.append(AuditKind.TOOL_RESULT, f"tool:{tool.value}",
                              {"call_id": call.id, "step_id": step_id, "ok": res.ok, "error": res.error,
                               "duration_ms": res.duration_ms, "attempt": attempt, "data": _brief(res.data)},
                              event_id=ev.id)
            if res.ok:
                return res
            if attempt <= MAX_TOOL_RETRIES:
                self._note(ev, "工具", f"[工具] {tool.value} 失敗（{(res.error or '')[:60]}）→ 重試一次", "dynamic")
        assert res is not None
        return res

    # ------------------------------------------------------------------ CLASSIFY / context
    async def _classify_async(self, run: _Run) -> dict[str, Any]:
        a = run.ev.anomaly
        ctx: dict[str, Any] = {}
        st = self._machine_state(a.machine_id)
        mdef = MACHINE_BY_ID.get(a.machine_id)
        ctx["machine"] = {"id": a.machine_id, "label": machine_label(a.machine_id),
                          "line": mdef.line.value if mdef else None,
                          "status": st.status.value if st else None,
                          "compound_stage": st.compound_stage.value if st else None,
                          "steam_header": mdef.steam_header if mdef else None}
        if st is not None and st.cure_job is not None:
            ctx["cure_job"] = st.cure_job.model_dump(mode="json")
        ch = CHANNEL_LABELS.get(a.channel, "讀值")
        r = await self._call(run, EventState.CLASSIFY, None, ToolName.READ_SENSORS,
                             {"machine_ids": [a.machine_id], "minutes": 30.0})
        run.context_tools.append(f"[工具] 讀 30 分鐘{ch}" + ("" if r.ok else "（失敗）"))
        if r.ok:
            ctx["sensors"] = r.data.get("summary", {})
        if a.prediction is None:
            p = await self._call(run, EventState.CLASSIFY, None, ToolName.PREDICT_ANOMALY,
                                 {"machine_id": a.machine_id, "channel": a.channel.value, "horizon_min": 30.0})
            if p.ok:
                ctx["prediction"] = p.data
                run.context_tools.append("[工具] 異常預測")
        s = await self._call(run, EventState.CLASSIFY, None, ToolName.SOP_SEARCH,
                             {"query": self._sop_query(a, "plan"), "k": 3})
        run.context_tools.append("[工具] 查 SOP" + ("" if s.ok else "（失敗）"))
        if s.ok:
            ctx["sop"] = self._sop_brief(s.data)
        return ctx

    @staticmethod
    def _sop_brief(data: dict[str, Any]) -> list[dict[str, Any]]:
        hits = data.get("hits") if isinstance(data, dict) else None
        return [{"sop_id": h.get("sop_id"), "title": h.get("title"), "score": h.get("score"),
                 "text": str(h.get("text", ""))[:400]} for h in (hits or [])[:3]]

    @staticmethod
    def _sop_query(a: Anomaly, phase: str) -> str:
        mdef = MACHINE_BY_ID.get(a.machine_id)
        if mdef is not None and mdef.line is Line.CURE:
            return "硫化機 溫度下降 蒸汽壓不足 延長硫化" if phase == "plan" else "硫化中斷處置"
        if mdef is not None and mdef.handles_compound:
            return "開煉機 膠溫過高 冷卻水流偏低" if phase == "plan" else "膠料過熱 暫停投料 處置"
        name = mdef.name if mdef else a.machine_id
        return f"{name} {a.kind.value} 異常處置"

    @staticmethod
    def _neighbours(machine_id: str) -> list[str]:
        mdef = MACHINE_BY_ID.get(machine_id)
        if mdef is None:
            return [machine_id]
        if mdef.line is Line.CURE and mdef.steam_header:
            return presses_on_header(mdef.steam_header)
        return [m.id for m in MACHINES if m.line is mdef.line]

    # ------------------------------------------------------------------ PLAN / REPLAN
    async def _ask(self, run: _Run, phase: str, extra: dict[str, Any]) -> CommanderPlan | None:
        ev = run.ev
        allowed = sorted(TOOLS_BY_STATE[EventState.SIMULATE] | WRITE_TOOLS, key=lambda t: t.value)
        rejected = [h for h in ev.hypotheses if h.status is HypothesisStatus.REJECTED]
        req = CommanderRequest(phase=phase, event_id=ev.id, anomaly=ev.anomaly, allowed_tools=allowed,
                               context={**run.base_context, **extra}, rejected=rejected)

        def on_alert(card: AlertCard) -> None:
            self._set_alert(ev, card)
        try:
            if self._commander_streams:
                plan, info = await self.commander.plan(req, on_alert=on_alert)
            else:
                plan, info = await self.commander.plan(req)
                on_alert(plan.alert_card)
        except Exception as e:  # noqa: BLE001
            self.audit.append(AuditKind.LLM_CALL, "commander",
                              {"phase": phase, "error": f"{type(e).__name__}: {e}"[:500]}, event_id=ev.id)
            self._note(ev, "指揮官", f"指揮官呼叫失敗（{type(e).__name__}）", "dynamic")
            return None
        self.audit.append(AuditKind.LLM_CALL, "commander",
                          {"phase": phase, **info.model_dump(mode="json"), "hypothesis": plan.hypothesis.id,
                           "cause_code": plan.hypothesis.cause_code, "steps": [s.id for s in plan.steps],
                           "safe_stop": plan.safe_stop, "rationale": plan.rationale}, event_id=ev.id)
        for s in getattr(self.commander, "last_intercepted", None) or []:
            self.audit.append(AuditKind.INTERCEPTED, "commander",
                              {"step": s, "reason": "tool not on the allow-list (dropped from plan)"}, event_id=ev.id)
        if plan.hypothesis.cause_code in {h.cause_code for h in rejected} and not plan.safe_stop:
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"error": "commander re-proposed a rejected hypothesis",
                               "cause_code": plan.hypothesis.cause_code}, event_id=ev.id)
            plan = plan.model_copy(update={"safe_stop": True})
        hyp = plan.hypothesis.model_copy(update={"status": HypothesisStatus.ACTIVE})
        ev.hypotheses.append(hyp)
        plan = plan.model_copy(update={"hypothesis": hyp})
        run.plan, run.plan_at = plan, self._now()
        run.results = {}
        ev.plan, ev.goal = plan, plan.goal
        ev.steps = [StepRun(step=s, state="pending") for s in plan.steps]
        self._narrate_plan(run, phase, rejected)
        return plan

    def _narrate_plan(self, run: _Run, phase: str, rejected: list[Hypothesis]) -> None:
        ev, plan = run.ev, run.plan
        assert plan is not None
        h = plan.hypothesis
        if plan.safe_stop:
            self._note(ev, "指揮官", f"{h.statement} → 請求安全中止", "decision")
            return
        if phase == "plan":
            tools = "、".join(run.context_tools)
            self._note(ev, "指揮官", f"{tools} → 假設 {h.id}：{h.statement}" if tools else f"假設 {h.id}：{h.statement}",
                       "decision")
        else:
            rej = rejected[-1].id if rejected else "?"
            via = " → ".join(run.replan_tools)
            text = f"推翻 {rej} → {h.id}：{h.statement}"
            if via:
                text += f"；{via}"
            text += f" → {plan.alert_card.title}"
            self._note(ev, "指揮官", text, "decision")
        exp = expectation_text(h.expectation)
        self._note(ev, "指揮官", f"拆步驟：{decompose_text(plan.steps)}" + (f"；預期：{exp}" if exp else ""), "info")

    async def _replan(self, run: _Run, extra: dict[str, Any], why: str) -> bool:
        """REPLAN: gather fresh context (neighbours on the same header/line, SOP), ask the commander
        for a new hypothesis. False when the event was safe-stopped."""
        ev = run.ev
        if ev.replans >= self.max_replans:
            self._safe_stop(run, f"已換 {ev.replans} 次假設仍未解決")
            return False
        self._to(ev, EventState.REPLAN, why)
        ev.replans += 1
        a = ev.anomaly
        run.replan_tools = []
        neigh = self._neighbours(a.machine_id)
        r = await self._call(run, EventState.REPLAN, None, ToolName.READ_SENSORS,
                             {"machine_ids": neigh, "minutes": 30.0})
        others = [m for m in neigh if m != a.machine_id]
        same = "同管線" if MACHINE_BY_ID.get(a.machine_id) and MACHINE_BY_ID[a.machine_id].line is Line.CURE else "同產線"
        if r.ok:
            extra = {**extra, "neighbours": r.data.get("summary", {})}
            if others:
                nums = "、".join(str(n) if (n := _press_no(m)) else machine_label(m, short=True) for m in others)
                run.replan_tools.append(f"[工具] 對照 {nums}{' 號' if _press_no(others[0]) else ''}{same}")
        s = await self._call(run, EventState.REPLAN, None, ToolName.SOP_SEARCH,
                             {"query": self._sop_query(a, "replan"), "k": 3})
        if s.ok:
            hits = self._sop_brief(s.data)
            extra = {**extra, "sop": hits}
            if hits:
                run.replan_tools.append(f"[工具] SOP「{hits[0]['title']}」")
        plan = await self._ask(run, "replan", extra)
        if plan is None or plan.safe_stop:
            self._safe_stop(run, "指揮官要求安全中止" if plan else "指揮官無回應")
            return False
        return True

    # ------------------------------------------------------------------ SIMULATE
    async def _simulate(self, run: _Run) -> dict[str, Any] | None:
        """Run read/compute steps in dependency order. Returns a failure dict when a tool failed
        after its retry (→ SOP path); sets run.feasible."""
        ev, plan = run.ev, run.plan
        assert plan is not None
        run.feasible = True
        by_id = {sr.step.id: sr for sr in ev.steps}
        reads = [sr for sr in ev.steps if sr.step.tool not in WRITE_TOOLS]
        line_bits: list[str] = []
        for sr in reads:
            step = sr.step
            if any(by_id.get(d) is None or by_id[d].state != "ok" for d in step.depends_on
                   if by_id.get(d) is None or by_id[d].step.tool not in WRITE_TOOLS):
                sr.state = "skipped"
                continue
            resolved = step.model_copy(update={"args": resolve_refs(step.args, run.results)})
            gd = self.gate.check(resolved, EventState.SIMULATE, True)
            sr.gate = gd
            if gd.verdict is GateVerdict.REJECT:
                sr.state = "rejected"
                self._log_reject(run, resolved, gd)
                continue
            sr.state = "running"
            self._publish(WsType.EVENT, ev)
            res = await self._call(run, EventState.SIMULATE, step.id, step.tool, resolved.args)
            sr.result = res
            if not res.ok:
                sr.state = "failed"
                self._note(ev, "工具", f"[工具] {step.id} {step.tool.value} 重試仍失敗 → 走 SOP", "dynamic")
                return {"step_id": step.id, "tool": step.tool.value, "error": res.error}
            sr.state = "ok"
            run.results[step.id] = res
            if step.tool is ToolName.SOLVE_SCHEDULE:
                if line_bits:
                    self._note(ev, "工具", "　".join(line_bits), "tool")
                    line_bits = []
                self._note(ev, "求解器", f"[工具] {step.id}：{result_text(step.tool, resolved.args, res.data)}", "tool")
                if res.data.get("fell_back"):
                    self._note(ev, "求解器", "求解逾時 → 切換備援求解器（heuristic）", "dynamic")
                if not res.data.get("feasible", False):
                    run.feasible = False
            else:
                today = self._now().date()
                line_bits.append(f"[工具] {step.id}：{result_text(step.tool, resolved.args, res.data, today)}")
        if line_bits:
            self._note(ev, "工具", "　".join(line_bits), "tool")
        return None

    def _log_reject(self, run: _Run, step: PlanStep, gd: GateDecision) -> None:
        kind = AuditKind.INTERCEPTED if gd.risk is None else AuditKind.GATE
        self.audit.append(kind, "gate", {"step_id": step.id, "tool": step.tool.value, "args": step.args,
                                         "decision": gd.model_dump(mode="json")}, event_id=run.ev.id)
        self._note(run.ev, "系統", f"動作把關攔截 {step.id} {step.tool.value}：{'；'.join(gd.reasons)[:80]}",
                   "dynamic")

    # ------------------------------------------------------------------ GATE
    def _cross_team(self, run: _Run, args: dict[str, Any]) -> bool | None:
        v = args.get("schedule_version")
        for res in run.results.values():
            if res.tool is ToolName.SOLVE_SCHEDULE and res.data.get("schedule_version") == v:
                sched = res.data.get("schedule") or {}
                return any(c.get("kind") == "transfer" for c in sched.get("staffing_changes") or [])
        return None

    def _gate(self, run: _Run
              ) -> tuple[list[StepRun], list[StepRun], list[StepRun], list[ApprovalOption], RiskLevel]:
        """Returns (auto now, auto deferred until approval, needs approval, options, request risk).
        An auto (R1) write that depends on a write awaiting approval waits for that approval."""
        ev = run.ev
        by_id = {sr.step.id: sr for sr in ev.steps}
        auto: list[StepRun] = []
        deferred: list[StepRun] = []
        needs: list[StepRun] = []
        risk = RiskLevel.R0_READ
        for sr in ev.steps:
            step = sr.step
            if step.tool not in WRITE_TOOLS:
                continue
            bad_dep = [d for d in step.depends_on if d in by_id and by_id[d].step.tool not in WRITE_TOOLS
                       and by_id[d].state != "ok"]
            write_deps = [by_id[d] for d in step.depends_on if d in by_id and by_id[d].step.tool in WRITE_TOOLS]
            if bad_dep or any(d.state in ("rejected", "skipped", "failed") for d in write_deps):
                sr.state = "skipped"
                continue
            waits = any(d in needs or d in deferred for d in write_deps)
            resolved = step.model_copy(update={"args": resolve_refs(step.args, run.results)})
            sr.step = resolved
            kw = {}
            if step.tool is ToolName.CONFIRM_SCHEDULE and self._gate_takes_cross_team:
                kw["cross_team"] = self._cross_team(run, resolved.args)
            gd = self.gate.check(resolved, EventState.EXECUTE, run.feasible, **kw)
            sr.gate = gd
            self.audit.append(AuditKind.GATE, "gate", {"step_id": step.id, "decision": gd.model_dump(mode="json")},
                              event_id=ev.id)
            if gd.verdict is GateVerdict.REJECT:
                sr.state = "rejected"
                self._log_reject(run, resolved, gd)
            elif gd.verdict is GateVerdict.ALLOW:
                (deferred if waits else auto).append(sr)
            else:
                sr.state = "awaiting_approval"
                needs.append(sr)
                if gd.risk is not None and RISK_ORDER.index(gd.risk) > RISK_ORDER.index(risk):
                    risk = gd.risk
        options = self._options(run, needs) if needs else []
        for o in options:
            od = self.gate.check(PlanStep(id=f"opt-{o.id}", tool=o.tool, args=o.args, purpose=o.label),
                                 EventState.EXECUTE, run.feasible)
            if od.risk is not None and RISK_ORDER.index(od.risk) > RISK_ORDER.index(risk):
                risk = od.risk
        if needs:
            self._note(ev, "系統", f"動作把關：格式正確、模擬可行 → {len(needs)} 項需主管核准（{risk.value}）"
                       + (f"，{len(auto)} 項自動執行" if auto else ""), "info")
        elif auto:
            self._note(ev, "系統", f"動作把關：格式正確、模擬可行 → {len(auto)} 項自動執行（R1）", "info")
        return auto, deferred, needs, options, risk

    def _options(self, run: _Run, needs: list[StepRun]) -> list[ApprovalOption]:
        """A/B options for the supervisor. Staffing alternatives come from the solver
        (staffing_options, overtime listed first); otherwise plan staffing steps; otherwise one
        option that approves the gated steps as proposed."""
        staffing: list[dict[str, Any]] = []
        chosen_labels: set[str] = set()
        for res in run.results.values():
            if res.tool is ToolName.SOLVE_SCHEDULE:
                staffing += list(res.data.get("staffing_options") or [])
                for c in (res.data.get("schedule") or {}).get("staffing_changes") or []:
                    chosen_labels.add(c.get("label"))
        opts: list[ApprovalOption] = []
        if staffing:
            seen = set()
            staffing.sort(key=lambda c: 0 if c.get("kind") == "overtime" else 1)
            for c in staffing:
                key = (c.get("kind"), c.get("label"))
                if key in seen:
                    continue
                seen.add(key)
                oid = chr(ord("A") + len(opts))
                when = ""
                if c.get("shift_date"):
                    try:
                        when = datetime.fromisoformat(str(c["shift_date"])).strftime("%m/%d ")
                    except ValueError:
                        when = ""
                if c.get("kind") == "transfer":
                    teams = [t for t in (c.get("from_team"), c.get("to_team")) if t]
                    args = {"teams": teams, "kind": NotifyKind.STAFF_MOVE.value,
                            "message": f"{when}{staffing_label(c)}（{c.get('people', 1)} 人）"}
                else:
                    args = {"teams": [c.get("to_team")], "kind": NotifyKind.OVERTIME.value,
                            "message": f"{when}{staffing_label(c)}（{c.get('people', 1)} 人）"}
                opts.append(ApprovalOption(id=oid, label=c.get("label") or args["message"],
                                           tool=ToolName.NOTIFY_TEAMS, args=args,
                                           recommended=c.get("label") in chosen_labels))
        else:
            staff_steps = [sr for sr in needs if sr.step.tool is ToolName.NOTIFY_TEAMS
                           and sr.step.args.get("kind") in STAFFING_KINDS]
            for sr in staff_steps:
                opts.append(ApprovalOption(id=chr(ord("A") + len(opts)), label=sr.step.purpose,
                                           tool=sr.step.tool, args=sr.step.args, recommended=not opts))
        if not opts:
            first = needs[0].step
            label = first.purpose if len(needs) == 1 else "核准以上動作"
            label = label.replace("（需主管核准）", "")
            opts.append(ApprovalOption(id="A", label=label, tool=first.tool, args=first.args, recommended=True))
        if opts and not any(o.recommended for o in opts):
            opts[0] = opts[0].model_copy(update={"recommended": True})
        return opts

    def _request_approval(self, run: _Run, needs: list[StepRun], options: list[ApprovalOption],
                          risk: RiskLevel) -> ApprovalRequest:
        ev, plan = run.ev, run.plan
        assert plan is not None
        run.approvals_made += 1
        rnd = run.approvals_made
        rejected = [h for h in ev.hypotheses if h.status is HypothesisStatus.REJECTED]
        if rejected:
            last = rejected[-1]
            reason = f"{last.id} 不成立：" + (last.evidence[-1] if last.evidence else "驗證未達預期")
        else:
            reason = f"假設 {plan.hypothesis.id}：{plan.hypothesis.statement}"
        done = list(dict.fromkeys(run.done))
        if ev.safety_actions or self._safety_acted(ev.machine_id):
            done.insert(0, "安全核心已處置")
        gaps: list[str] = []
        delivery = ""
        for res in run.results.values():
            gaps += [str(g) for g in res.data.get("gaps") or []]
            if res.tool is ToolName.SOLVE_SCHEDULE:
                late = res.data.get("late_orders") or []
                delivery = "不受影響" if res.data.get("deliveries_met") else f"延誤：{'、'.join(late) or '有'}"
        now = self._now()
        req = ApprovalRequest(
            id=f"AP-{ev.id}-{rnd}", event_id=ev.id, step_id=needs[0].step.id, round=rnd, created_at=now,
            risk=risk, title=plan.alert_card.title, reason=reason, done=done, gaps=list(dict.fromkeys(gaps)),
            delivery_impact=delivery, options=options,
            expires_at=now + timedelta(seconds=self.approval_timeout_s))
        req = self.approvals.request(req)
        ev.approvals.append(req.id)
        self._publish(WsType.EVENT, ev)
        return req

    def _safety_acted(self, machine_id: str) -> bool:
        st = self._machine_state(machine_id)
        return st is not None and st.status is MachineStatus.STOPPED

    def _note_decision(self, run: _Run, req: ApprovalRequest) -> None:
        d = req.decision
        assert d is not None
        ev = run.ev
        opt = next((o for o in req.options if o.id == d.option_id), None)
        others = [o for o in req.options if opt is None or o.id != opt.id]
        if d.decision is DecisionKind.REJECT:
            text = "[人工] 主管否決" + (f"：{d.reason}" if d.reason else "") + "；決定與理由寫入紀錄"
        elif opt is not None and opt.args.get("kind") == NotifyKind.OVERTIME.value and \
                any(o.args.get("kind") == NotifyKind.STAFF_MOVE.value for o in others):
            text = f"[人工] 否決跨組調人，改選{opt.label}；決定與理由寫入紀錄"
        elif d.decision is DecisionKind.MODIFY:
            text = f"[人工] 主管修改後核准（{opt.label if opt else ''}）；決定與理由寫入紀錄"
        elif len(req.options) > 1 and opt is not None:
            text = f"動作把關確認可行 → [人工] 主管核准（{opt.id} {opt.label}）；寫入稽核日誌"
        else:
            text = "動作把關確認可行 → [人工] 主管核准；寫入稽核日誌"
        if d.reason and d.decision is not DecisionKind.REJECT:
            text += f"（理由：{d.reason}）"
        self._note(ev, "主管", text, "human", ts=d.decided_at)

    def _chosen_steps(self, run: _Run, needs: list[StepRun], options: list[ApprovalOption],
                      req: ApprovalRequest) -> list[StepRun]:
        """Approved writes: every gated step, with staffing steps replaced by the chosen option."""
        d = req.decision
        assert d is not None
        opt = next((o for o in options if o.id == d.option_id), None)
        is_staff_option = opt is not None and opt.args.get("kind") in STAFFING_KINDS
        chosen: list[StepRun] = []
        for sr in needs:
            if is_staff_option and sr.step.tool is ToolName.NOTIFY_TEAMS and sr.step.args.get("kind") in STAFFING_KINDS:
                sr.state = "skipped"
                continue
            if opt is not None and not is_staff_option and d.modified_args and sr.step.tool is opt.tool:
                sr.step = sr.step.model_copy(update={"args": {**sr.step.args, **d.modified_args}})
            chosen.append(sr)
        if is_staff_option and opt is not None:
            args = {**opt.args, **(d.modified_args or {})}
            step = PlanStep(id=f"{req.id}-{opt.id}", tool=opt.tool, args=args, purpose=f"{opt.id} {opt.label}")
            gd = self.gate.check(step, EventState.EXECUTE, run.feasible)
            sr = StepRun(step=step, state="pending", gate=gd)
            run.ev.steps.append(sr)
            if gd.verdict is GateVerdict.REJECT:
                sr.state = "rejected"
                self._log_reject(run, step, gd)
            else:
                chosen.append(sr)
        if d.decision is DecisionKind.MODIFY and d.modified_args:
            # re-gate modified steps: a modification can never widen permissions
            for sr in list(chosen):
                gd = self.gate.check(sr.step, EventState.EXECUTE, run.feasible)
                if gd.verdict is GateVerdict.REJECT:
                    sr.state = "rejected"
                    self._log_reject(run, sr.step, gd)
                    chosen.remove(sr)
        return chosen

    # ------------------------------------------------------------------ EXECUTE
    async def _execute(self, run: _Run, steps: list[StepRun], auto_tag: bool) -> None:
        ev = run.ev
        by_id = {x.step.id: x for x in ev.steps}
        for sr in steps:
            step = sr.step
            if any(by_id[d].state != "ok" for d in step.depends_on
                   if d in by_id and by_id[d].step.tool in WRITE_TOOLS):
                sr.state = "skipped"
                continue
            sr.state = "running"
            res = await self._call(run, EventState.EXECUTE, step.id, step.tool, step.args)
            sr.result = res
            if not res.ok:
                sr.state = "failed"
                self._note(ev, "工具", f"[工具] {step.id} {step.tool.value} 執行失敗：{(res.error or '')[:60]}", "dynamic")
                continue
            sr.state = "ok"
            run.results[step.id] = res
            txt = result_text(step.tool, step.args, res.data)
            self._note(ev, "工具", ("✓ 已自動：" if auto_tag else "[工具] ") + txt, "tool")
            self._after_write(run, step)

    def _after_write(self, run: _Run, step: PlanStep) -> None:
        ev = run.ev
        if step.tool is ToolName.CONFIRM_SCHEDULE:
            v = step.args.get("schedule_version")
            for res in run.results.values():
                if res.tool is ToolName.SOLVE_SCHEDULE and res.data.get("schedule_version") == v:
                    for mv in res.data.get("moves") or []:
                        n = _press_no(str(mv.get("to_press")))
                        run.done.append(f"{mv.get('order_id')} 改 {n} 號" if n else f"{mv.get('order_id')} 已重排")
            run.executed.append(f"排程 v{v} 生效")
        elif step.tool is ToolName.SET_CURE_TIME:
            run.executed.append(f"{machine_label(str(step.args.get('press_id')))}硫化 {fmt_num(step.args.get('total_cure_h'))} h")
        elif step.tool is ToolName.NOTIFY_TEAMS:
            kind = step.args.get("kind")
            if kind == NotifyKind.MACHINE_OUT.value:
                mid = step.args.get("machine_id") or ev.machine_id
                self._set_status(mid, MachineStatus.OUT_OF_SERVICE, f"{machine_label(mid)}停用")
                run.executed.append(f"{machine_label(mid)}停用")
            elif kind in STAFFING_KINDS:
                run.executed.append(step.purpose.split(" ", 1)[-1])
            elif kind == NotifyKind.MAINTENANCE.value:
                run.done.append("已通知維修")
                run.executed.append("已通知維修")
            elif kind in (NotifyKind.SLOW_FEED.value, NotifyKind.PAUSE_FEED.value):
                run.executed.append("放慢生料投入" if kind == NotifyKind.SLOW_FEED.value else "暫停投料")

    # ------------------------------------------------------------------ VERIFY (code, no LLM)
    def _read_value(self, machine_id: str, channel: Channel) -> float | None:
        st = self._machine_state(machine_id)
        if st is not None and st.last_reading is not None:
            return getattr(st.last_reading, channel.value, None)
        return None

    async def _read_value_via_tool(self, run: _Run, machine_id: str, channel: Channel) -> float | None:
        r = await self._call(run, EventState.VERIFY, "verify", ToolName.READ_SENSORS,
                             {"machine_ids": [machine_id], "minutes": 0})
        if not r.ok:
            return None
        s = (r.data.get("summary") or {}).get(machine_id) or {}
        v = s.get(f"{channel.value}_now")
        if v is None:
            samples = (r.data.get("readings") or {}).get(machine_id) or []
            v = samples[-1].get(channel.value) if samples else None
        return v

    @staticmethod
    def _satisfied(exp, v: float) -> bool:
        if exp.op == ">=":
            return v >= exp.value
        if exp.op == "<=":
            return v <= exp.value
        return exp.value <= v <= (exp.value_hi if exp.value_hi is not None else exp.value)

    async def _verify(self, run: _Run) -> tuple[bool, float | None]:
        ev, plan = run.ev, run.plan
        assert plan is not None
        h = ev.hypotheses[-1]
        exp = h.expectation
        failed_writes = [sr for sr in ev.steps if sr.state == "failed"]
        if exp is None:
            ok = not failed_writes
            self.audit.append(AuditKind.SYSTEM, "verifier",
                              {"hypothesis": h.id, "expectation": None, "ok": ok,
                               "failed_steps": [sr.step.id for sr in failed_writes]}, event_id=ev.id)
            self._note(ev, "驗證（程式）", "無數值預期：動作已執行完成" if ok else
                       f"{len(failed_writes)} 項動作執行失敗", "dynamic" if not ok else "info")
            return ok, None
        start = run.plan_at or self._now()
        deadline = start + timedelta(minutes=exp.within_min)
        violated_since: datetime | None = None
        last: float | None = None
        use_sim = self.sim is not None
        while True:
            now = self._now()
            v = self._read_value(exp.machine_id, exp.channel) if use_sim else \
                await self._read_value_via_tool(run, exp.machine_id, exp.channel)
            if v is not None:
                last = v
                if self._satisfied(exp, v):
                    violated_since = None
                else:
                    violated_since = violated_since or now
                    if (now - violated_since).total_seconds() >= self.verify_confirm_s:
                        return self._verify_result(run, h, exp, False, v)
            if now >= deadline:
                ok = violated_since is None and last is not None
                return self._verify_result(run, h, exp, ok, last)
            await self._sleep(self.verify_poll_s)

    def _verify_result(self, run: _Run, h: Hypothesis, exp, ok: bool, v: float | None) -> tuple[bool, float | None]:
        ev = run.ev
        unit = "°C" if exp.channel is Channel.TEMP else ""
        ch = CHANNEL_LABELS.get(exp.channel, "讀值")
        self.audit.append(AuditKind.SYSTEM, "verifier",
                          {"hypothesis": h.id, "expectation": exp.model_dump(mode="json"), "observed": v, "ok": ok},
                          event_id=ev.id)
        if ok:
            h.evidence.append(f"{ch}維持 {fmt_num(v)}{unit}，符合預期 {expectation_text(exp)}")
            self._note(ev, "驗證（程式）", f"比對預期：{ch} {fmt_num(v)}{unit} → {h.id} 成立", "dynamic")
        else:
            h.evidence.append(f"{ch}續降至 {fmt_num(v)}{unit}" if exp.op == ">=" else f"{ch}達 {fmt_num(v)}{unit}")
            verb = "降到" if exp.op == ">=" else "升到"
            text = f"比對預期：{ch}{verb} {fmt_num(v)}{unit} → {h.id} 不成立"
            if ev.safety_actions or self._safety_acted(ev.machine_id):
                text += "；安全核心已依規則處置"
            self._note(ev, "驗證（程式）", text, "dynamic")
        return ok, v

    # ------------------------------------------------------------------ terminal states
    def _close(self, run: _Run) -> None:
        ev = run.ev
        outcome = "；".join(dict.fromkeys(run.executed)) or "無需動作"
        h = ev.hypotheses[-1]
        ev.outcome = f"{h.id} {h.statement}：{outcome}"
        self._to(ev, EventState.CLOSED, ev.outcome)
        ev.closed_at = self._now()
        st = self._machine_state(ev.machine_id)
        if st is not None and st.status is MachineStatus.ALARM:
            self._set_status(ev.machine_id, MachineStatus.RUNNING, None)
        self._note(ev, "系統", f"事件結案：{outcome}", "info")

    def _safe_stop(self, run: _Run, reason: str) -> None:
        ev = run.ev
        if ev.state in TERMINAL_STATES:
            return
        rolled = ""
        if self.store is not None and run.prev_schedule_version is not None:
            try:
                cur = self.store.current().version
                if cur != run.prev_schedule_version:
                    self.store.rollback(run.prev_schedule_version)
                    self.audit.append(AuditKind.ROLLBACK, "state_machine",
                                      {"from_version": cur, "to_version": run.prev_schedule_version,
                                       "reason": reason}, event_id=ev.id)
                    rolled = f"（排程已回復 v{run.prev_schedule_version}）"
            except Exception as e:  # noqa: BLE001
                self.audit.append(AuditKind.SYSTEM, "state_machine", {"rollback_error": str(e)[:300]},
                                  event_id=ev.id)
        for h in ev.hypotheses:
            if h.status is HypothesisStatus.ACTIVE:
                h.status = HypothesisStatus.PROPOSED
        ev.outcome = f"安全中止：{reason}；維持原排程{rolled}"
        self._to(ev, EventState.SAFE_STOP, reason)
        ev.closed_at = self._now()
        self._note(ev, "系統", ev.outcome, "dynamic")


def _press_no(machine_id: str) -> int | None:
    from warroom.agent.narrate import press_no
    return press_no(machine_id)
