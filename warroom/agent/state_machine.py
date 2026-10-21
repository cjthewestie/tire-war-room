"""Event State Machine (page 5, page 7, page 11).

One Event per wake-up from the detector. States follow contracts.ALLOWED_TRANSITIONS:

  DETECTED → CLASSIFY → PLAN → SIMULATE → GATE → [EXECUTE (auto R1)] → [AWAIT_APPROVAL] → EXECUTE
  → VERIFY → CLOSED;  SIMULATE/GATE/AWAIT_APPROVAL/VERIFY → REPLAN → SIMULATE;  any → SAFE_STOP.

- Tools are opened by state (TOOLS_BY_STATE): every call goes through ``_call`` which refuses and
  logs (INTERCEPTED) a tool that is not open in the current state. Writes run only in EXECUTE.
- The commander (only LLM) is called in PLAN and REPLAN; it returns a CommanderPlan. "$S1.x"
  references in step args are resolved here from earlier ToolResult.data.
- VERIFY is code: it polls the machine reading and compares it with Hypothesis.expectation, which
  code builds or bounds (event machine, the anomaly's channel or temperature, a code threshold). A
  miss rejects the hypothesis and goes to REPLAN with the evidence (page 11: 119.5 °C → H1 不成立).
  A reading met only because the safety core stopped the machine is inconclusive, never confirmed. A
  plan that takes the event machine out of service is checked by its status (page 11 H2), an SOP
  hand-off or a plan-vs-actual event has no reading: those close with the hypothesis unconfirmed.
- A write that fails, is refused by the gate, or is skipped (confirm_schedule goes first in a round;
  if it fails, e.g. stale after another event's activation, nothing else of that round runs) is an
  action miss: counted as a failure, then REPLAN keeps the hypothesis and re-solves against the
  schedule active now. A supervisor reject or an infeasible solve also keeps the hypothesis. A
  supervisor reject is a decision, not a failure: it re-plans with the veto as context and never
  counts toward the safe stop; nor does a confirm that went stale because another event's schedule
  took effect first. An approval whose schedule went stale is carried over (recorded as
  "<id>-rebased", via auto) only when the re-solve carries exactly what was approved.
- A cure time (set_cure_time) or a press taken out (machine_out) passes the gate only when the same
  round confirms the version whose solve carried that change (or the active schedule already does);
  a confirm that changes nothing asks for no approval.
- Physics numbers never come from the LLM: cure_formula inputs are taken from the press's live cure
  job and the detector's prediction, and solve_schedule cure overrides take this event's formula
  total for that press; the gate ties set_cure_time and overrides to that press's formula result.
- Failure policy (page 7): a failed tool is retried MAX_TOOL_RETRIES times, then the SOP path
  (REPLAN with sop_path context); solver timeout → backup solver happens inside the scheduler and
  is shown on the timeline; MAX_CONSECUTIVE_FAILURES → SAFE_STOP. Safe stop keeps the schedule in
  force (never rolls back a supervisor-approved version); unapproved proposals are simply never
  confirmed. An approved round that executes cleanly resets the failure streak.
- The LLM path never lifts a machine status the safety core or a decision set (STOPPED /
  OUT_OF_SERVICE); see _set_status.
- Every transition, LLM call, tool call/result, gate decision and interception is audited; every
  change is published on the bus (WsType.EVENT / ALERT_CARD).

The runtime should call ``on_safety_action(action)`` for each SafetyAction from the safety core:
it audits it (SAFETY_ACTION) and attaches it to the machine's open event.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import itertools
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from warroom.agent.narrate import (
    CHANNEL_LABELS,
    order_shifts,
    decompose_text,
    expectation_text,
    fmt_num,
    machine_label,
    moves_text,
    result_text,
    staffing_label,
    gate_reason_text,
    tool_error_text,
    tool_label,
)
from warroom.contracts import (
    ALLOWED_TRANSITIONS,
    MAX_CONSECUTIVE_FAILURES,
    MAX_DISCUSS_REPLANS,
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
    AnomalyKind,
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
    EventReview,
    EventState,
    Expectation,
    FactorySim,
    GateDecision,
    GateVerdict,
    Hypothesis,
    HypothesisStatus,
    Line,
    MachineStatus,
    NotifyKind,
    PlanConstraint,
    PlanStep,
    PreviousRun,
    RiskLevel,
    SafetyAction,
    Severity,
    ScheduleStatus,
    StaffingChange,
    StepRound,
    StepRun,
    TeamId,
    TimelineItem,
    ToolCall,
    ToolName,
    ToolRegistry,
    ToolResult,
    WsType,
)
from warroom.agent.gate import cure_notice_press
from warroom.agent.strict import failure_cause, strict_reason
from warroom.factory import (
    CURE_PRESS_IDS,
    CURE_TEMP_MAX_C,
    CURE_TEMP_MIN_C,
    CURE_TEMP_NOMINAL_C,
    MACHINE_BY_ID,
    MACHINES,
    PROCESSES,
    TZ,
    presses_on_header,
)

# anomaly kinds with no sensor outcome to verify (page 6 box 4 plan vs actual): VERIFY checks that the
# approved writes ran; the hypothesis is never CONFIRMED by it
UNMEASURED_KINDS = frozenset({"schedule_slip", "supervisor_command", "shift_start", "order_rush", "staffing_short"})
PLANT_KINDS = frozenset({"shift_start", "order_rush", "staffing_short"})   # issue #17 plant events
COMMAND_MACHINE = "PLANT"      # issue #10: a supervisor command that names no machine is about the plant
# issue #17: a plant event's VERIFY waits (sim time) for the people to finish the tasks its approved steps became
TASK_VERIFY_MAX_MIN = 180.0
RISK_ORDER = [RiskLevel.R0_READ, RiskLevel.R1_NOTIFY, RiskLevel.R2_CHANGE, RiskLevel.R3_CRITICAL]
STAFFING_KINDS = (NotifyKind.OVERTIME.value, NotifyKind.STAFF_MOVE.value)
_REF = re.compile(r"^\$(S\d+[A-Za-z]?)\.([\w.\[\]-]+)$")          # S4, or a sub-step S4b
_INLINE_REF = re.compile(r"\$\{(S\d+[A-Za-z]?)\.([\w.\[\]-]+)\}")
_MISSING = object()

# Edge latency model (pages 10-11). Sim time does not move while the commander or solver runs in an
# accelerated demo (Runtime holds it), and the mock commander answers in microseconds, so without a
# model every row of the event would carry the detection second. The state machine paces itself in
# SIM time: each beat lasts max(measured, modelled) seconds, so the timeline reads like the Spark run
# on page 11 (02:14:00 detect, :01 safety, :03 H1, :09 decomposition, :11 S1/S2, :25 solver + gate).
# The plant keeps running during the pause, exactly as it would while the edge box is thinking.
MODEL_SENSOR_TICK_S = 1.0          # safety core reports on the next 1-s sensor tick (:00 -> :01)
MODEL_TOOL_ROUND_S = 1.0           # one read/compute tool round (sensors, SOP, formula, orders)
MODEL_SOLVE_S = 14.0               # OR-Tools solve incl. feasibility check (page 11 :11 -> :25)
MODEL_LLM_PLAN_S = 6.0             # commander JSON plan after the first streamed tokens (:03 -> :09)


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
    # False only when a solve of this round came back infeasible (-> replan). It is NOT the gate's
    # "simulation feasible": that is per write (_sim_ok) and needs a feasible solve covering it.
    feasible: bool = True
    auto_ids: set[str] = field(default_factory=set)          # writes the gate let through as R1 (auto)
    approvals_made: int = 0
    done: list[str] = field(default_factory=list)            # 已做 (for the phone card)
    executed: list[str] = field(default_factory=list)        # outcome summary
    context_tools: list[str] = field(default_factory=list)   # phrases for the commander timeline row
    replan_tools: list[str] = field(default_factory=list)
    base_context: dict[str, Any] = field(default_factory=dict)
    option_changes: dict[str, dict[str, Any]] = field(default_factory=dict)  # option id -> solver StaffingChange
    step_changes: dict[str, dict[str, Any]] = field(default_factory=dict)    # option step id -> StaffingChange
    staffing_done: list[dict[str, Any]] = field(default_factory=list)        # executed staffing notifies
    revocations: dict[str, dict[str, Any]] = field(default_factory=dict)     # revoke step id -> staffing_done entry
    activated: list[int] = field(default_factory=list)       # schedule versions THIS event confirmed (approved)
    formula_press: dict[str, str | None] = field(default_factory=dict)  # cure_formula step id -> press it was run for
    round_problems: list[dict[str, Any]] = field(default_factory=list)  # writes of this round that failed / were refused
    replan_why: str = ""                                     # why the current replan happened (narration)
    newly_rejected: str | None = None                        # hypothesis VERIFY rejected just before this replan
    sop_handoff: bool = False                                # tools failed: the plan hands the event to people (SOP)
    commander_error: str | None = None                       # why the latest commander call produced no plan
    current_req: ApprovalRequest | None = None               # the approval whose writes are executing now
    approved_sig: dict[str, Any] | None = None               # what that approval put in force (content signature)
    stale_approval: dict[str, Any] | None = None             # approved, but its schedule went stale before confirm
    rebasing: bool = False                                   # code re-solves that stale schedule, until the next decision
    # issue #10: plan discussion
    waiting_req: ApprovalRequest | None = None               # the card the event is waiting on now
    supersede: dict[str, Any] | None = None                  # {"old": card, "text", "by"} set by a discussion
    discuss_future: Any = None                               # asyncio.Future: the next card (or None) for discuss()
    card_snaps: dict[str, dict[str, Any]] = field(default_factory=dict)  # card id -> what it proposed (diff v1 -> v2)
    # fast repair (Settings.repair_fast_s): the runtime reported the crew's fix (on_repaired), then the machine back to
    # normal after its ramp (on_recovered, Settings.recover_fast_s); VERIFY waits for both on the wall clock
    repaired: asyncio.Event = field(default_factory=asyncio.Event)
    recovered: asyncio.Event = field(default_factory=asyncio.Event)
    recover_reading: Any = None                              # SensorReading at recovery (on_recovered)
    repair_reading: float | None = None                      # the reading VERIFY confirmed once the machine was back


class StateMachine:
    """Implements contracts.EventStateMachine (page 5/7/11). See module docstring.

    Extra keyword args (all optional, defaults suit the live runtime):
      store              ScheduleStore (safe-stop bookkeeping, staffing amendments).
      sleep              async fn(sim_seconds) used by VERIFY polling; default waits on the clock.
      approval_timeout_s wall seconds to wait for the supervisor before SAFE_STOP.
      verify_poll_s      sim seconds between VERIFY reads.
      verify_confirm_s   a miss must persist this long (sim s) before the hypothesis is rejected;
                         4.5 min of a 30-s mean below the expectation, so sensor noise near the threshold of a slow thermal
                         mass does not reject a 30-min hypothesis (hard limits are the safety core's).
      max_replans        hypothesis swaps allowed before SAFE_STOP.
      pace               pace each beat to max(measured, modelled edge latency) in sim time, so the
                         timeline keeps the page-11 rhythm (False: stamp everything at sim now).
    """

    def __init__(self, commander: CommanderClient, tools: ToolRegistry, gate: ActionGate,
                 approvals: ApprovalBroker, audit: AuditLog, sim: FactorySim | None = None,
                 bus: Bus | None = None, clock: Clock | None = None, *, store=None,
                 sleep: Callable[[float], Awaitable[None]] | None = None, approval_timeout_s: float = 600.0,
                 verify_poll_s: float = 5.0, verify_confirm_s: float = 270.0, max_replans: int = 3,
                 max_events_kept: int = 200, pace: bool = True):
        self.commander, self.tools, self.gate = commander, tools, gate
        self.approvals, self.audit, self.sim, self.bus = approvals, audit, sim, bus
        self.clock = clock or (getattr(sim, "clock", None) if sim is not None else None)
        self.store = store
        self._sleep_fn = sleep
        self.approval_timeout_s = approval_timeout_s
        self.verify_poll_s, self.verify_confirm_s = verify_poll_s, verify_confirm_s
        self.max_replans = max_replans
        self.pace = pace                         # sim-time latency model (see MODEL_* above)
        self._max_kept = max_events_kept
        self._events: dict[str, Event] = {}
        self._runs: dict[str, _Run] = {}         # open event id -> its run (issue #10: discussion)
        self._task_reports: dict[str, tuple[str, bool]] = {}  # event id -> (人員回報完成 line, a repair) for VERIFY
        self._plant_inputs: dict[str, Any] = {}  # issue #17: plant event id -> its PlantInput
        self._open: dict[str, str] = {}          # machine_id -> open event id
        self._activated_by: dict[int, str] = {}  # schedule version -> the event whose approval put it in force
        self._ids = itertools.count(1)
        # issue #54: the main agent (orchestrator) hooks a run's start and end; a supervisor interrupt sets the reason
        self.on_open: Callable[[Event], None] | None = None
        self.on_end: Callable[[Event], None] | None = None
        self._interrupt: dict[str, str] = {}
        self.compute_inflight = 0                # solver / LLM calls running now (Runtime may hold sim time)
        try:
            self._commander_streams = "on_alert" in inspect.signature(commander.plan).parameters
        except (TypeError, ValueError):
            self._commander_streams = False
        try:
            self._gate_takes_cross_team = "cross_team" in inspect.signature(gate.check).parameters
            self._gate_takes_evidence = "evidence" in inspect.signature(gate.check).parameters
        except (TypeError, ValueError):
            self._gate_takes_cross_team = self._gate_takes_evidence = False

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

    def on_repaired(self, event_id: str, machine_id: str, at: datetime) -> None:
        """Fast repair (runtime, Settings.repair_fast_s): every repair task of this event on `machine_id` is done and
        its fault was cleared. The runtime now ramps the readings back to nominal (Settings.recover_fast_s): the
        machine is 回溫中 (WARNING, amber), and VERIFY waits for on_recovered."""
        ev = self._events.get(event_id)
        if ev is None or ev.repaired_at is not None:
            return
        ev.repaired_at = at
        self._note(ev, "系統", f"[人工] 維修完成：{machine_label(machine_id)}故障排除", "human", ts=at)
        self._set_status(machine_id, MachineStatus.WARNING, "回溫中")
        run = self._runs.get(event_id)
        if run is not None:
            run.repaired.set()

    def on_recovered(self, event_id: str, machine_id: str, at: datetime, reading: Any = None) -> None:
        """Fast repair: the repaired machine's readings are back to nominal (the runtime's ramp ended; it audits
        「已恢復正常」). VERIFY tests that reading at once and the event closes green; an event that had already closed
        goes green and gets its incident report now."""
        ev = self._events.get(event_id)
        if ev is None or ev.recovered_at is not None:
            return
        ev.recovered_at = at
        t = getattr(reading, "temp_c", None)
        self._note(ev, "系統", f"已恢復正常：{machine_label(machine_id)}" + (f" {fmt_num(t)}°C" if t is not None else ""),
                   "info", ts=at)
        run = self._runs.get(event_id)
        if run is not None:
            run.recover_reading = reading
            run.recovered.set()
            return
        st = self._machine_state(machine_id)
        if st is not None and st.status is MachineStatus.WARNING:
            # the drawer's banner drops 「回溫中」: back to the event's message (_live_note turns it into the live line)
            self._set_status(machine_id, MachineStatus.RUNNING, ev.anomaly.message)
        if ev.state is EventState.CLOSED:
            self._report_ready(ev)

    def task_reported(self, event_id: str, text: str, at: datetime) -> None:
        """人員回報 (runtime, report mode): a worker's 開始 / 完成, the supervisor's 代為完成 or an overdue reminder goes
        on the event's timeline. A 完成 line is kept so VERIFY can say what the reading confirmed."""
        ev = self._events.get(event_id)
        if ev is None:
            return
        self._note(ev, "系統", f"[人工] {text}", "human", ts=at)
        if "回報完成" in text or "代為完成" in text:
            # VERIFY quotes the repair's report (檢修 …) over a later 宣達 / 設定 report of the same event
            from warroom.sim.work import REPAIR_KINDS
            repair = any(f"：{k}［" in text or f"的{k}（" in text for k in REPAIR_KINDS)
            prev = self._task_reports.get(event_id)
            if repair or prev is None or not prev[1]:
                head = text.split("（原因")[0] if "代為完成" in text else text.split("：")[0]
                self._task_reports[event_id] = (head, repair)

    def review(self, event_id: str, by: str, note: str | None = None) -> Event:
        """主管已讀報告 (POST /api/events/{id}/review): the supervisor read the incident report of an event closed
        after a repair. Nothing waited for it. KeyError: no such event; ValueError: it has no pending report."""
        ev = self._events[event_id]
        if ev.review is None or ev.review.status != "pending":
            raise ValueError(f"{event_id} has no pending report")
        ev.review = EventReview(status="done", requested_at=ev.review.requested_at, done_at=self._now(), by=by)
        self.audit.append(AuditKind.SYSTEM, "supervisor", {"msg": "主管已讀報告", "note": note}, event_id=ev.id)
        self._note(ev, "主管", "[人工] 主管已讀報告" + (f"：{note}" if note else ""), "human")
        return ev

    def _report_ready(self, ev: Event) -> None:
        """異常報告已產生: the event closed after its repair; its incident report is ready (review pending = 報告待讀
        for the supervisor). A notice, not a gate: the event is already closed and green."""
        if ev.review is not None:
            return
        ev.review = EventReview(status="pending", requested_at=self._now())
        self.audit.append(AuditKind.SYSTEM, "state_machine", {"msg": "異常報告已產生"}, event_id=ev.id)
        self._note(ev, "系統", "異常報告已產生：維修完成、已恢復正常，事件已結案", "info")

    async def handle(self, anomaly: Anomaly) -> Event:
        existing = self.open_event_for(anomaly.machine_id)
        if existing is not None:
            self.audit.append(AuditKind.ANOMALY, "detector", anomaly.model_dump(mode="json"), event_id=existing.id)
            self._note(existing, "事件偵測", f"追加異常：{anomaly.message}", "info", ts=anomaly.ts)
            return existing
        ev = self._open_event(anomaly)
        return await self._drive(ev, self._run)

    async def _drive(self, ev: Event, body: Callable[[_Run], Awaitable[None]]) -> Event:
        run = _Run(ev=ev, prev_schedule_version=self._active_version())
        self._runs[ev.id] = run
        self._hook(self.on_open, ev)
        try:
            await body(run)
        except asyncio.CancelledError:
            if ev.state not in TERMINAL_STATES:
                self._safe_stop(run, self._interrupt.get(ev.id, "事件處理被取消"))
            raise
        except Exception as e:  # noqa: BLE001 - never leave an event half-open
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"error": f"{type(e).__name__}: {e}"[:500], "state": ev.state.value}, event_id=ev.id)
            if ev.state not in TERMINAL_STATES:
                self._safe_stop(run, f"內部錯誤：{type(e).__name__}")
        finally:
            self._settle_discussion(run, None)
            self._runs.pop(ev.id, None)
            if self._open.get(ev.machine_id) == ev.id:
                del self._open[ev.machine_id]
            self._hook(self.on_end, ev)
            self._trim()
        return ev

    def _hook(self, fn: Callable[[Event], None] | None, ev: Event) -> None:
        if fn is not None:
            try:
                fn(ev)
            except Exception as e:  # noqa: BLE001 - the main agent's bookkeeping never breaks a run
                self.audit.append(AuditKind.SYSTEM, "state_machine", {"hook_error": f"{type(e).__name__}: {e}"[:300]},
                                  event_id=ev.id)

    def mark_interrupt(self, event_id: str, reason: str = "主管中斷") -> None:
        """Issue #54: the supervisor interrupts this run; the cancellation then safe-stops it with this reason."""
        self._interrupt[event_id] = reason

    def stop_run(self, event_id: str, reason: str = "主管中斷") -> None:
        """Issue #54: end a run that has no task to cancel (SAFE_STOP keeps the schedule in force)."""
        ev = self._events[event_id]
        if ev.state in TERMINAL_STATES:
            return
        run = self._runs.get(event_id) or _Run(ev=ev, prev_schedule_version=self._active_version())
        self._safe_stop(run, reason)
        self._hook(self.on_end, ev)

    # ------------------------------------------------------------------ issue #10: supervisor commands
    def open_command(self, text: str, actor: str = "supervisor") -> Event:
        """A task the supervisor typed opens an Event (source=supervisor). It is not a machine
        anomaly: it never raises an alarm, is not the 'open event' of any machine (detector anomalies
        never attach to it) and runs PLAN -> SIMULATE -> GATE -> (approval) -> EXECUTE like any event."""
        from warroom.agent.talk import command_machine
        mid = command_machine(text) or COMMAND_MACHINE
        now = self._now()
        base = f"EV-{now.astimezone(TZ):%Y%m%d-%H%M%S}-CMD"
        eid, n = base, 2
        while eid in self._events:
            eid, n = f"{base}-{n}", n + 1
        a = Anomaly(id=f"CMD-{eid[3:]}", ts=now, machine_id=mid, kind=AnomalyKind.SUPERVISOR_COMMAND,
                    channel=Channel.TEMP, value=0.0, baseline=0.0, severity=Severity.INFO,
                    message=f"主管指令：{text}")
        ev = Event(id=eid, opened_at=now, machine_id=mid, anomaly=a, state=EventState.DETECTED,
                   source="supervisor", command=text)
        from warroom.agent.talk import command_kind, rule_constraints
        if command_kind(text) == "schedule":       # 「9 號機明天保養，重排」: the same constraints a discussion makes
            ev.constraints = rule_constraints(text, now)[0]
        self._events[eid] = ev
        self.audit.append(AuditKind.STATE_TRANSITION, "state_machine",
                          {"from": None, "to": EventState.DETECTED.value, "reason": "supervisor command"}, event_id=eid)
        self.audit.append(AuditKind.SYSTEM, actor, {"command": text, "source": "supervisor", "machine_id": mid},
                          event_id=eid)
        self._note(ev, "主管", f"[人工] 主管指令：{text}", "human", ts=now)
        self._set_alert(ev, AlertCard(title="主管指令", summary=text[:60]), source="detector")
        return ev

    async def run_command(self, event_id: str) -> Event:
        ev = self._events[event_id]
        return await self._drive(ev, self._run_command)

    async def handle_command(self, text: str, actor: str = "supervisor") -> Event:
        return await self.run_command(self.open_command(text, actor).id)

    async def _run_command(self, run: _Run) -> None:
        from warroom.agent.talk import physical_request
        ev = run.ev
        text = ev.command or ""
        self._to(ev, EventState.CLASSIFY, "supervisor command: read context")
        refusal = physical_request(text)
        if refusal:
            # nothing on the allow-list can do it: say so, never invent an action (page 5: no actuator)
            self.audit.append(AuditKind.SYSTEM, "state_machine", {"command_refused": refusal, "command": text},
                              event_id=ev.id)
            ev.outcome = f"無法執行：{refusal}"
            self._note(ev, "指揮官", f"無法執行這個指令：{refusal}", "decision")
            self._to(ev, EventState.CLOSED, "command cannot be carried out by any tool")
            ev.closed_at = self._now()
            return
        run.base_context = await self._classify_command(run)
        self._to(ev, EventState.PLAN, "ask commander (supervisor command)")
        plan = await self._ask(run, "plan", {})
        if plan is None or plan.safe_stop:
            self._safe_stop(run, "指揮官要求安全中止" if plan else (run.commander_error or "指揮官無回應"))
            return
        await self._loop(run)

    # ------------------------------------------------------------------ issue #17: plant events
    def open_plant_event(self, pin: Any, at: datetime | None = None) -> Event:
        """A plant input the scenario scripted (the shift starts, a rush order arrives, someone calls in sick)
        opens an Event with source="plant". It is about the whole plant (machine PLANT), never a machine's open
        event, and runs CLASSIFY -> PLAN -> SIMULATE -> GATE -> (approval) -> EXECUTE -> VERIFY (the people finish
        the tasks) -> CLOSED like any event."""
        kind = {"shift_start": AnomalyKind.SHIFT_START, "rush_order": AnomalyKind.ORDER_RUSH,
                "absence": AnomalyKind.STAFFING_SHORT}[pin.kind]
        tag = {"shift_start": "PLAN", "rush_order": "RUSH", "absence": "STAFF"}[pin.kind]
        now = at or self._now()
        base = f"EV-{now.astimezone(TZ):%Y%m%d-%H%M%S}-{tag}"
        eid, n = base, 2
        while eid in self._events:
            eid, n = f"{base}-{n}", n + 1
        a = Anomaly(id=f"PLT-{eid[3:]}", ts=now, machine_id=COMMAND_MACHINE, kind=kind, channel=Channel.TEMP,
                    value=0.0, baseline=0.0, severity=Severity.WARN if pin.kind != "shift_start" else Severity.INFO,
                    message=pin.text)
        ev = Event(id=eid, opened_at=now, machine_id=COMMAND_MACHINE, anomaly=a, state=EventState.DETECTED,
                   source="plant")
        self._events[eid] = ev
        self._plant_inputs[eid] = pin
        self.audit.append(AuditKind.STATE_TRANSITION, "state_machine",
                          {"from": None, "to": EventState.DETECTED.value, "reason": "plant event"}, event_id=eid)
        self.audit.append(AuditKind.SYSTEM, "plant", {"plant_input": pin.model_dump(mode="json")}, event_id=eid)
        self._note(ev, "事件偵測", f"[工具] {pin.text} → 喚醒指揮官", "tool", ts=now)
        title = {"shift_start": "早班開工", "rush_order": "插單", "absence": "人力缺口"}[pin.kind]
        self._set_alert(ev, AlertCard(title=title, summary=pin.text[:60]), source="detector")
        return ev

    async def run_plant(self, event_id: str) -> Event:
        return await self._drive(self._events[event_id], self._run_plant)

    async def _run_plant(self, run: _Run) -> None:
        ev = run.ev
        self._to(ev, EventState.CLASSIFY, "plant event: read the SOP")
        s = await self._call(run, EventState.CLASSIFY, None, ToolName.SOP_SEARCH,
                             {"query": self.PLANT_SOP_QUERY.get(self._plant_kind(ev), ev.anomaly.message), "k": 2})
        # orders, shifts, leave and materials are read in PLAN (query_orders_shifts is a PLAN-state tool)
        self._to(ev, EventState.PLAN, "read orders, roster and materials; ask commander (plant event)")
        run.base_context = await self._classify_plant(run, s)
        plan = await self._ask(run, "plan", {})
        if plan is None or plan.safe_stop:
            self._safe_stop(run, "指揮官要求安全中止" if plan else (run.commander_error or "指揮官無回應"))
            return
        await self._loop(run)

    PLANT_SOP_QUERY = {"shift_start": "密煉機開機暖機與換批 暖機順序", "rush_order": "急單 插單 排程 交期",
                       "absence": "人力 缺人 調班 加班 支援"}

    PLANT_TOOLS = frozenset({ToolName.QUERY_ORDERS_SHIFTS, ToolName.SOLVE_SCHEDULE, ToolName.CONFIRM_SCHEDULE,
                             ToolName.NOTIFY_TEAMS})

    def _plant_plan_guard(self, run: _Run, plan: CommanderPlan) -> CommanderPlan:
        """A plant event has no machine fault: steps whose tool needs a machine (cure formula, cure time, sensors)
        are dropped by code (audited), with anything that depended on them; a notice naming no real machine loses
        machine_id. The model proposes; what runs is decided here."""
        keep: list[PlanStep] = []
        dropped: list[str] = []
        for st in plan.steps:
            if st.tool not in self.PLANT_TOOLS or any(d in dropped for d in st.depends_on):
                dropped.append(st.id)
                continue
            if st.tool is ToolName.NOTIFY_TEAMS and st.args.get("machine_id") not in MACHINE_BY_ID and "machine_id" in st.args:
                st = st.model_copy(update={"args": {k: v for k, v in st.args.items() if k != "machine_id"}})
            keep.append(st)
        kind = self._plant_kind(run.ev)
        if kind in ("shift_start", "rush_order") and not any(
                st.tool is ToolName.NOTIFY_TEAMS and st.args.get("kind") == NotifyKind.DISPATCH.value for st in keep):
            # the day's plan / the inserted order reaches the floor only through 派工 (R2, approval): when the model
            # left it out, code adds the fixed-text step so the supervisor still gets one card and people get tasks
            pin = self._plant_inputs.get(run.ev.id)
            teams = [TeamId.BODY.value, TeamId.CURING.value] if kind == "rush_order" else \
                [TeamId.BODY.value, TeamId.BIGOPEN.value, TeamId.SIDE.value, TeamId.BEAD_NYLON.value, TeamId.BOND.value,
                 TeamId.CURING.value]
            msg = (f"急單 {pin.order.id} 插單：依新排程補料、備模" if kind == "rush_order" and pin is not None and pin.order
                   else "今日計畫：依 SOP 暖機（密煉 → 開煉），首批膠料補到 A 線，依排程開工")
            confirm = [st.id for st in keep if st.tool is ToolName.CONFIRM_SCHEDULE]
            sid = f"S{len(keep) + 1}"
            while any(st.id == sid for st in keep):
                sid += "d"
            keep.append(PlanStep(id=sid, tool=ToolName.NOTIFY_TEAMS, purpose="派工（需主管核准）",
                                 depends_on=confirm[-1:] if kind == "rush_order" else [],
                                 args={"teams": teams, "kind": NotifyKind.DISPATCH.value, "message": msg}))
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"plant_plan_guard": "dispatch step added by code (fixed text)", "step_id": sid}, event_id=run.ev.id)
        if dropped:
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"plant_plan_guard": "steps that need a machine fault dropped", "dropped": dropped,
                               "tools": [st.tool.value for st in plan.steps if st.id in dropped]}, event_id=run.ev.id)
        return plan.model_copy(update={"steps": keep}) if dropped or keep != plan.steps else plan

    def _bind_plant(self, run: _Run, step: PlanStep) -> PlanStep:
        """Plant events: the schedule numbers come from code, never the model. A solve plans the normal 7-day
        horizon with no press override (there is no machine fault); a confirm activates the version this round's
        feasible solve produced. Changes are audited."""
        if run.ev.source != "plant":
            return step
        a = step.args
        if step.tool is ToolName.SOLVE_SCHEDULE:
            args = {k: a[k] for k in ("allow_overtime", "allow_cross_team", "time_limit_s", "press_blackouts") if k in a}
        elif step.tool is ToolName.CONFIRM_SCHEDULE:
            feas = [int(r.data["schedule_version"]) for r in run.results.values() if r.ok and r.tool is ToolName.SOLVE_SCHEDULE
                    and r.data.get("feasible") and isinstance(r.data.get("schedule_version"), int)]
            if not feas:
                return step
            args = {"schedule_version": feas[-1], "reason": str(a.get("reason") or run.ev.anomaly.message)[:120]}
        else:
            return step
        if args != a:
            self.audit.append(AuditKind.SYSTEM, "state_machine", {"plant_args_from_code": step.id, "tool": step.tool.value,
                                                                 "from": a, "to": args}, event_id=run.ev.id)
            return step.model_copy(update={"args": args})
        return step

    def _plant_kind(self, ev: Event) -> str:
        pin = self._plant_inputs.get(ev.id)
        return pin.kind if pin is not None else ""

    async def _classify_plant(self, run: _Run, s: ToolResult) -> dict[str, Any]:
        """Code reads what the plan needs (orders, shifts and gaps, who is on leave, materials, the SOP) with the
        allow-listed read tools, and writes the 已查 lines for the card from those results only."""
        ev = run.ev
        pin = self._plant_inputs.get(ev.id)
        kind = pin.kind if pin is not None else ""
        now = self._now()
        ctx: dict[str, Any] = {"source": "plant", "factory_time": now.isoformat(),
                               "plant_event": pin.model_dump(mode="json", exclude_none=True) if pin is not None else {}}
        args: dict[str, Any] = {"order_ids": [pin.order.id]} if kind == "rush_order" and pin.order else {}
        r = await self._call(run, EventState.PLAN, None, ToolName.QUERY_ORDERS_SHIFTS, args)
        run.context_tools.append("[工具] 查訂單、班表、出勤與原料" + ("" if r.ok else "（失敗）"))
        if r.ok:
            d = r.data
            ctx["orders"] = [{k: o.get(k) for k in ("id", "size", "qty", "done", "due", "priority")}
                             for o in d.get("orders") or []][:12]
            ctx["gaps"] = d.get("gaps") or []
            for k in ("leave", "materials", "today"):
                if d.get(k) is not None:
                    ctx[k] = d[k]
            run.done += self._plant_brief(kind, d, pin)
        if s.ok:
            ctx["sop"] = self._sop_brief(s.data)
            run.context_tools.append("[工具] 查 SOP")
            if kind == "shift_start":
                from warroom.sim.work import WARMUP_ORDER
                order = " → ".join(machine_label(m, short=True) for m, _ in WARMUP_ORDER)
                hit = next((h for h in ctx["sop"] if "暖機" in str(h.get("title", "")) + str(h.get("text", ""))), None)
                run.done.append(f"暖機順序（{hit['sop_id'] if hit and hit.get('sop_id') else 'SOP-02'}）：{order}，各 30 分鐘")
        return ctx

    @staticmethod
    def _plant_brief(kind: str, d: dict[str, Any], pin: Any) -> list[str]:
        """已查 lines for a plant event's card, from the query_orders_shifts result (code, not the model)."""
        out: list[str] = []
        today = d.get("today") or {}
        if kind == "shift_start":
            orders = d.get("orders") or []
            due_soon = [o for o in orders if o.get("due") and str(o["due"]) <= str(today.get("due_within_3d") or "")]
            out.append(f"今日訂單：{len(orders)} 筆進行中" + (f"，3 天內到期 {'、'.join(o['id'] for o in due_soon)}"
                                                             if due_soon else "") +
                       (f"；今日排 {today['mold_ins']} 模入模" if today.get("mold_ins") is not None else ""))
        leave = d.get("leave")
        if leave is not None:
            names = "、".join(f"{x.get('name')}（{x.get('group') or x.get('team_label')}：{x.get('note') or '請假'}）"
                             for x in leave)
            out.append(f"出勤：在廠 {today.get('on_duty', '?')} 人；請假 {len(leave)} 人" + (f"：{names}" if names else ""))
        mats = [m for m in d.get("materials") or [] if m.get("status") != "ok"]
        if kind == "shift_start" or mats:
            out.append("原料：" + ("、".join(f"{m['name']} {m['stock']:g} {m['unit']}（{ {'reorder': '已達再訂購點', 'low': '偏低', 'out': '缺料'}.get(m['status'], m['status'])}"
                                            + (f"，在途 {m['on_order']:g}" if m.get("on_order") else "") + "）" for m in mats)
                                  if mats else "6 項皆在再訂購點以上"))
        if kind == "rush_order" and pin is not None and pin.order is not None:
            o = pin.order
            out.append(f"急單 {o.id}：{o.customer} {o.size.value} {o.qty} 條，交期 {o.due:%m/%d}（已加入訂單簿）")
        if kind == "absence" and pin is not None:
            out.append(f"缺勤：{pin.text}")
        gaps = d.get("gaps") or []
        if gaps:
            out.append("班表缺口：" + "、".join(gaps[:3]))
        return out

    async def _classify_command(self, run: _Run) -> dict[str, Any]:
        ev = run.ev
        mid = ev.machine_id
        ctx: dict[str, Any] = {"source": "supervisor", "command": ev.command,
                               "factory_time": self._now().isoformat()}
        if mid in MACHINE_BY_ID:
            st = self._machine_state(mid)
            mdef = MACHINE_BY_ID[mid]
            ctx["machine"] = {"id": mid, "label": machine_label(mid), "line": mdef.line.value,
                              "status": st.status.value if st else None,
                              "steam_header": mdef.steam_header}
            r = await self._call(run, EventState.CLASSIFY, None, ToolName.READ_SENSORS,
                                 {"machine_ids": [mid], "minutes": 30.0})
            run.context_tools.append("[工具] 讀 30 分鐘讀值" + ("" if r.ok else "（失敗）"))
            if r.ok:
                ctx["sensors"] = r.data.get("summary", {})
        s = await self._call(run, EventState.CLASSIFY, None, ToolName.SOP_SEARCH, {"query": ev.command or "", "k": 2})
        if s.ok:
            ctx["sop"] = self._sop_brief(s.data)
            run.context_tools.append("[工具] 查 SOP")
        if ev.constraints:
            ctx["constraints"] = [c.label for c in ev.constraints]
        return ctx

    # ------------------------------------------------------------------ issue #10: plan discussion
    def waiting_card(self, event_id: str) -> ApprovalRequest | None:
        run = self._runs.get(event_id)
        if run is None or run.ev.state is not EventState.AWAIT_APPROVAL or run.waiting_req is None:
            return None
        try:
            req = self.approvals.get(run.waiting_req.id)
        except Exception:  # noqa: BLE001
            return None
        return req if req.status is ApprovalStatus.PENDING else None

    async def discuss_replan(self, event_id: str, constraints: list[PlanConstraint], text: str,
                             actor: str = "supervisor", timeout_s: float = 300.0) -> ApprovalRequest | None:
        """The supervisor changed the conditions of a waiting plan. The constraints are added to the
        event (code enforces them on every solve from now on), the waiting card is superseded
        (audited) and the event re-plans: REPLAN -> SIMULATE -> GATE. Returns the new card (v2...),
        or None when the new plan needs no approval or the event stopped. Raises ValueError when
        the event is not waiting at the gate or the discussion cap is reached."""
        run = self._runs.get(event_id)
        old = self.waiting_card(event_id)
        if run is None or old is None:
            raise ValueError("event is not waiting for approval")
        ev = run.ev
        if ev.discuss_replans >= MAX_DISCUSS_REPLANS:
            raise ValueError(f"discussion replan cap {MAX_DISCUSS_REPLANS} reached")
        now = self._now()
        added = [c.model_copy(update={"at": c.at or now}) for c in constraints]
        # a newer window for the same press replaces the older one (「延後到明天」)
        for c in added:
            if c.kind == "machine_unavailable":
                ev.constraints = [x for x in ev.constraints
                                  if not (x.kind == "machine_unavailable" and x.machine_id == c.machine_id)]
            elif c.kind == "prefer_option":
                ev.constraints = [x for x in ev.constraints if x.kind != "prefer_option"]
        ev.constraints = [*ev.constraints, *added]
        self.audit.append(AuditKind.SYSTEM, actor,
                          {"constraints_added": [c.model_dump(mode="json") for c in added], "text": text,
                           "approval_id": old.id, "constraints_in_force": [c.label for c in ev.constraints]},
                          event_id=ev.id)
        fut = asyncio.get_running_loop().create_future()
        run.discuss_future = fut
        run.supersede = {"old": old, "text": text, "by": actor, "added": added}
        self.approvals.supersede(old.id, by=actor, reason=f"主管討論：{text}")
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout_s)
        except TimeoutError:
            return None

    def _settle_discussion(self, run: _Run, req: ApprovalRequest | None) -> None:
        fut = run.discuss_future
        if fut is not None and not fut.done():
            fut.set_result(req)
        run.discuss_future = None

    # ------------------------------------------------------------------ helpers: time, bus, audit
    def _plant_sha(self) -> str | None:
        """sha256 of the PLANT.md version the commander's prompt carried (issue #37, refs #34)."""
        sha = getattr(self.commander, "last_plant_sha", None)
        if sha:
            return sha
        from warroom.agent import plant
        return plant.active_sha()

    def _now(self) -> datetime:
        return self.clock.now() if self.clock else datetime.now(TZ)

    def _approval_expires_at(self, now: datetime) -> datetime:
        """The card's expiry in FACTORY time. The wait itself is approval_timeout_s of WALL time
        (approvals.wait), so at SIM_SPEED x the card must show now + timeout x speed; showing
        now + timeout made a 10x run print an expiry 10x too early (F2, issue #6). A speed change
        or pause mid-wait can still shift it; the wall-time wait stays the authority."""
        speed = float(getattr(self.clock, "speed", 1.0) or 1.0) if self.clock is not None else 1.0
        return now + timedelta(seconds=self.approval_timeout_s * max(speed, 0.0))

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

    async def _pace(self, since: datetime, seconds: float) -> None:
        """Let sim time reach since+seconds (no-op when already past or pacing is off)."""
        if not self.pace or self.clock is None or seconds <= 0:
            return
        if getattr(self.clock, "paused", False):
            # a paused demo (e.g. the supervisor discussing a plan with time stopped) must not hang on a
            # latency model: the beat is only cosmetic, so it is skipped
            return
        remaining = (since + timedelta(seconds=seconds) - self._now()).total_seconds()
        if remaining > 1e-6:
            await self._sleep(remaining)

    def _publish(self, kind: WsType, data: Any) -> None:
        if self.bus is not None:
            try:
                self.bus.publish(kind, data)
            except Exception:  # noqa: BLE001
                pass

    def add_note(self, event_id: str, actor: str, text: str, tag: str = "human") -> Event | None:
        """A line on an event's timeline from outside a run (a supervisor's resolution step); publishes the event."""
        ev = self._events.get(event_id)
        if ev is not None:
            self._note(ev, actor, text, tag)
        return ev

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

    # The LLM path may never lift what the safety core (STOPPED) or a decision (OUT_OF_SERVICE) put
    # down: ALARM only over a machine that is still running, RUNNING only back from ALARM (or from the
    # fast repair's 回溫中 WARNING), WARNING only over ALARM / running. Taking a machine out of service is
    # always allowed (it only restricts further).
    _STATUS_FROM: dict[MachineStatus, frozenset[MachineStatus]] = {
        MachineStatus.ALARM: frozenset({MachineStatus.RUNNING, MachineStatus.WARNING, MachineStatus.IDLE}),
        MachineStatus.RUNNING: frozenset({MachineStatus.ALARM, MachineStatus.WARNING}),
        MachineStatus.WARNING: frozenset({MachineStatus.ALARM, MachineStatus.RUNNING, MachineStatus.IDLE}),
    }

    def _set_status(self, machine_id: str, status: MachineStatus, note: str | None = None) -> bool:
        if self.sim is None:
            return False
        allowed = self._STATUS_FROM.get(status)
        if allowed is not None:
            st = self._machine_state(machine_id)
            if st is not None and st.status not in allowed:
                return False                     # e.g. safety core STOPPED it: keep it stopped
        try:
            self.sim.set_machine_status(machine_id, status, note)
            return True
        except Exception:  # noqa: BLE001
            return False

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
        self._set_alert(ev, AlertCard(title=f"{machine_label(a.machine_id)}異常", summary=a.message),
                        source="detector")
        self._set_status(a.machine_id, MachineStatus.ALARM, a.message)
        return ev

    def _set_alert(self, ev: Event, card: AlertCard, source: str = "commander") -> None:
        """source: "detector" = instant no-LLM placeholder, "commander" = the LLM-written card (page 9 target)."""
        ev.alert_card = card
        self._publish(WsType.ALERT_CARD, {"event_id": ev.id, "card": card.model_dump(mode="json"), "source": source})

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
        await self._pace(ev.anomaly.ts, MODEL_SENSOR_TICK_S)
        self._safety_status_note(ev)
        self._to(ev, EventState.CLASSIFY, "gather sensor window, prediction and SOP")
        run.base_context = await self._classify_async(run)
        self._to(ev, EventState.PLAN, "ask commander")
        plan = await self._ask(run, "plan", {})
        if plan is None or plan.safe_stop:
            self._safe_stop(run, "指揮官要求安全中止" if plan else (run.commander_error or "指揮官無回應"))
            return
        await self._loop(run)

    async def _loop(self, run: _Run) -> None:
        ev = run.ev
        while True:
            run.round_problems = []
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

            reused = self._reuse_stale_approval(run, needs, options) if needs else None
            stale, run.stale_approval = run.stale_approval, None
            if not needs and run.supersede is not None:
                # the discussed plan needs no approval (e.g. nothing left to change): no new card
                run.supersede = None
                self._settle_discussion(run, None)
            if needs:
                if reused is not None:
                    res = reused
                else:
                    self._to(ev, EventState.AWAIT_APPROVAL, f"{req_risk.value} approval")
                    req = self._request_approval(run, needs, options, req_risk, stale=stale)
                    run.waiting_req = req
                    self._settle_discussion(run, req)
                    res = await self.approvals.wait(req.id, timeout_s=self.approval_timeout_s)
                    run.waiting_req = None
                run.rebasing = False                 # decided (or carried over): the hold follows this decision
                if res.status is ApprovalStatus.SUPERSEDED:
                    # issue #10: the supervisor discussed this plan; its card is retired (audited by the
                    # broker) and the plan is redone with the new conditions. A discussion is a decision,
                    # not a failure: it never counts toward the safe stop.
                    for sr in ev.steps:
                        if sr.state == "awaiting_approval":
                            sr.state = "skipped"
                    for sr in deferred:
                        sr.state = "skipped"
                    sup = run.supersede or {}
                    ev.discuss_replans += 1
                    chips = "、".join(c.label for c in sup.get("added") or []) or "新條件"
                    self._note(ev, "主管", f"[人工] 主管：「{sup.get('text', '')}」→ 條件：{chips}；"
                               f"第 {res.round} 輪 v{res.version} 作廢，依新條件重新規劃", "human")
                    ctx = {"supervisor_discussion": {
                        "text": sup.get("text"), "constraints": [c.label for c in ev.constraints],
                        "constraint_detail": [c.model_dump(mode="json", exclude={"text", "at"}) for c in ev.constraints],
                        "superseded_card": {"id": res.id, "version": res.version, "actions": res.actions,
                                            "options": [o.label for o in res.options]},
                        "note": "constraints are enforced by code on every solve_schedule; keep the hypothesis "
                                "and re-solve"}}
                    if not await self._replan(run, ctx, "主管討論 → 依新條件重排", discussion=True):
                        return
                    continue
                if res.status is ApprovalStatus.EXPIRED or res.decision is None:
                    self._note(ev, "系統", "主管逾時未回覆 → 不變更，維持原排程", "dynamic")
                    self._safe_stop(run, "核准逾時")
                    return
                if reused is None:
                    self._note_decision(run, res)
                if res.decision.decision is DecisionKind.REJECT:
                    for sr in ev.steps:
                        if sr.state == "awaiting_approval":
                            sr.state = "rejected"
                    for sr in deferred:
                        sr.state = "skipped"
                    # a veto is a decision, not a failure: page 7 counts tool / solve / verify
                    # failures toward the safe stop. The commander re-plans with the veto as
                    # context (bounded by max_replans and the approval timeout).
                    self.audit.append(AuditKind.SYSTEM, "state_machine",
                                      {"supervisor_reject": res.id, "counted_as_failure": False,
                                       "consecutive_failures": ev.consecutive_failures}, event_id=ev.id)
                    rej = {"reason": res.decision.reason, "approval_id": res.id,
                           "rejected_options": [o.label for o in res.options]}
                    if not await self._replan(run, {"supervisor_rejected": rej}, "主管否決 → 換方案"):
                        return
                    continue
                chosen = self._chosen_steps(run, needs, options, res) + deferred
                change = self._chosen_change(run, chosen)
                if change is not None:
                    chosen = self._dedupe_staffing(run, chosen, change)
                # the schedule put in force carries only staffing that was approved and notified:
                # the option picked here, or what is already in force (never the solver's own pick)
                self._amend_schedule_staffing(run, chosen, change, res)
                if ev.state is not EventState.EXECUTE:
                    self._to(ev, EventState.EXECUTE, f"approved by {res.decision.decided_by}"
                             + (f" (approval {res.id} reused: same content, rebased)" if reused is not None else ""))
                run.current_req = res
                run.approved_sig = self._content_sig(run, chosen, change)
                await self._execute(run, chosen, auto_tag=False)
                run.current_req = None
                if change is not None and not run.round_problems:
                    await self._execute(run, self._revoke_steps(run, change, res), auto_tag=False)
                if chosen and not run.round_problems and ev.consecutive_failures:
                    # an approved round that executed cleanly ends the failure streak (page 7 counts
                    # CONSECUTIVE failures)
                    self.audit.append(AuditKind.SYSTEM, "state_machine",
                                      {"failure_streak_reset": ev.consecutive_failures,
                                       "reason": "approved writes executed"}, event_id=ev.id)
                    ev.consecutive_failures = 0
            elif not auto:
                self._to(ev, EventState.EXECUTE, "no write steps")

            # ---------------- VERIFY
            self._to(ev, EventState.VERIFY, "compare expectation with readings (code)")
            hyp = ev.hypotheses[-1]
            if run.round_problems:
                # a write that failed, was skipped after a failed schedule confirm, or was refused by
                # the gate: the plan did not do what it claimed. That is a miss for the ACTION, not a
                # test of the hypothesis: count the failure and re-plan (re-solve against the schedule
                # that is active now), keeping the hypothesis.
                stale_only = run.stale_approval is not None and all(
                    (p["state"] == "failed" and "is stale" in str(p.get("error", ""))) or p["state"] == "skipped"
                    for p in run.round_problems)
                self._action_miss(run, note=not stale_only)
                if stale_only:
                    # another event's schedule took effect first (concurrent anomalies, page 10): not a
                    # failure of this plan; re-solve against the schedule in force now. Said as that on the
                    # timeline, not as 「動作未完成…執行失敗」 (on stage it read as a failed approval)
                    self.audit.append(AuditKind.SYSTEM, "state_machine",
                                      {"stale_schedule": run.stale_approval.get("version"),
                                       "counted_as_failure": False}, event_id=ev.id)
                    self._note(ev, "驗證（程式）", f"{self._first_in_force()}排程先生效，核准的動作尚未執行 → "
                               f"{machine_label(ev.machine_id, short=True)}依現行排程重算（不算失敗）", "dynamic")
                elif self._fail(run, "動作未完成"):
                    return
                if stale_only:
                    ok = self._resolve_stale(run, "排程已被另一事件更新 → 依現行排程重算")
                else:
                    ctx = {"write_failed": {"steps": run.round_problems, "active_version": self._active_version(),
                                            "at": self._now().isoformat()}}
                    ok = await self._replan(run, ctx, "動作未完成 → 重新規劃")
                if not ok:
                    return
                continue
            verdict, observed = await self._verify(run)
            if verdict == "confirmed":
                hyp.status = HypothesisStatus.CONFIRMED
                self._close(run)
                return
            if verdict in ("contained", "inconclusive", "handed_off", "unmeasured", "tasks_done", "tasks_open"):
                hyp.status = HypothesisStatus.ACTIVE          # not tested by a reading: never CONFIRMED
                self._close(run, verified=False, why={"inconclusive": "安全核心已停機，無法驗證",
                                                      "contained": "已停用該機，未以讀值驗證",
                                                      "handed_off": "已交現場依 SOP 處置",
                                                      "unmeasured": "無讀值可驗證，核准動作已執行",
                                                      "tasks_done": "核准動作已由人員執行完成",
                                                      "tasks_open": "人員任務逾時未全部完成"}[verdict],
                            clear_alarm=verdict == "unmeasured" and ev.source != "supervisor")
                return
            hyp.status = HypothesisStatus.REJECTED
            run.newly_rejected = hyp.id
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
        started = self._now()
        model_s = 0.0
        if tool not in WRITE_TOOLS and state is not EventState.VERIFY:
            model_s = MODEL_SOLVE_S if tool is ToolName.SOLVE_SCHEDULE else MODEL_TOOL_ROUND_S
        for attempt in range(1, MAX_TOOL_RETRIES + 2):
            call = ToolCall(id=f"{ev.id}:{step_id or 'ctx'}:{next(self._ids)}", event_id=ev.id, step_id=step_id,
                            tool=tool, args=args, attempt=attempt)
            self.audit.append(AuditKind.TOOL_CALL, f"tool:{tool.value}",
                              {"call_id": call.id, "step_id": step_id, "args": args, "attempt": attempt,
                               "state": state.value}, event_id=ev.id)
            try:
                if tool is ToolName.SOLVE_SCHEDULE:      # CP-SAT may take seconds: keep the loop free
                    self.compute_inflight += 1           # the Runtime holds sim time while > 0
                    try:
                        res = await self._invoke(tool, call)
                    finally:
                        self.compute_inflight -= 1
                else:
                    res = await self._invoke(tool, call)
            except Exception as e:  # noqa: BLE001 - registry should never raise; be safe anyway
                res = ToolResult(call_id=call.id, tool=tool, ok=False, error=f"{type(e).__name__}: {e}",
                                 attempt=attempt)
            self.audit.append(AuditKind.TOOL_RESULT, f"tool:{tool.value}",
                              {"call_id": call.id, "step_id": step_id, "ok": res.ok, "error": res.error,
                               "duration_ms": res.duration_ms, "attempt": attempt, "data": _brief(res.data)},
                              event_id=ev.id)
            if res.ok:
                if tool not in WRITE_TOOLS and not run.rebasing:
                    # a write has no modelled latency: pacing its sub-ms run waited for the next plant tick, which
                    # another event's model call holds (Runtime.compute_busy) — an approved round stalled 11 s
                    # between two writes, its fix (VERIFY) and its 綠燈 with it (concurrent_3, real model). Nor
                    # does the code re-solve of an approved round (_resolve_stale): its reads and solve ran paced
                    # on that held clock while the approved repair waited for its VERIFY
                    await self._pace(started, max(model_s * attempt, res.duration_ms / 1000.0))
                return res
            if attempt <= MAX_TOOL_RETRIES:
                self._note(ev, "工具", f"[工具] {tool_label(tool)}失敗（{tool_error_text(tool, res.error)}）→ 重試一次",
                           "dynamic")
        assert res is not None
        await self._pace(started, model_s)
        return res

    async def _invoke(self, tool: ToolName, call: ToolCall) -> ToolResult:
        """Never block the loop on a tool: a cold KB / slow Qdrant once froze the Runtime tick (and so
        safety_pass) for 3 s. Registry.acall awaits read tools off-loop; a registry without acall gets
        read/compute tools pushed to a thread. Write tools stay on the loop (the bus is loop-bound)."""
        acall = getattr(self.tools, "acall", None)
        if acall is not None:
            return await acall(call)
        if tool in WRITE_TOOLS:
            return self.tools.call(call)
        return await asyncio.to_thread(self.tools.call, call)

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
            first = ((r.data.get("readings") or {}).get(a.machine_id) or [None])[0]
            if isinstance(first, dict):           # start of the 30-min window: what "normal" read
                ctx["sensor_baseline"] = {a.machine_id: {c.value: first.get(c.value) for c in Channel
                                                         if first.get(c.value) is not None}}
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
        ctx = {**run.base_context, **extra}
        if ev.previous is not None:              # 「請 AI 重新規劃」: why the last run of this ended in SAFE_STOP
            ctx["previous_run"] = {"event_id": ev.previous.event_id, "stop_reason": ev.previous.reason_text}
        req = CommanderRequest(phase=phase, event_id=ev.id, anomaly=ev.anomaly, allowed_tools=allowed,
                               context=ctx, rejected=rejected)

        def on_alert(card: AlertCard) -> None:
            self._set_alert(ev, card)
        llm_started = self._now()
        self.compute_inflight += 1                   # LLM thinking time: Runtime may hold sim time
        try:
            if self._commander_streams:
                plan, info = await self.commander.plan(req, on_alert=on_alert)
            else:
                plan, info = await self.commander.plan(req)
                on_alert(plan.alert_card)
        except Exception as e:  # noqa: BLE001
            # --strict (llm_fallback_to_mock=False): no rule plan; the reason ends the event via SAFE_STOP
            strict = getattr(getattr(self.commander, "settings", None), "llm_fallback_to_mock", True) is False
            run.commander_error = strict_reason(e) if strict else f"指揮官呼叫失敗（{type(e).__name__}）"
            self.audit.append(AuditKind.LLM_CALL, "commander",
                              {"phase": phase, "error": f"{type(e).__name__}: {e}"[:500], "cause": failure_cause(e),
                               "fell_back_to_mock": False, "plan_source": None, "strict": strict,
                               "reason": run.commander_error, "plant_sha256": self._plant_sha()}, event_id=ev.id)
            self._note(ev, "指揮官", run.commander_error, "dynamic")
            return None
        finally:
            self.compute_inflight -= 1
        payload: dict[str, Any] = {"phase": phase, **info.model_dump(mode="json"), "plant_sha256": self._plant_sha()}
        if info.fell_back_to_mock:
            # the model's output was rejected: what runs is the deterministic fallback's plan, and the
            # record says so (never credit the mock's steps to the model), with why and what it sent
            raw = str(getattr(self.commander, "last_raw", "") or "")
            payload.update({"plan_source": "mock_fallback", "steps": [],
                            "fallback_steps": [s.id for s in plan.steps],
                            "fallback_hypothesis": plan.hypothesis.id,
                            "error": getattr(self.commander, "last_error", None),
                            "cause": getattr(self.commander, "last_cause", None),
                            "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                            "raw_chars": len(raw), "raw_excerpt": raw[:600]})
        else:
            payload.update({"plan_source": "commander", "hypothesis": plan.hypothesis.id,
                            "cause_code": plan.hypothesis.cause_code, "steps": [s.id for s in plan.steps],
                            "safe_stop": plan.safe_stop, "rationale": plan.rationale})
        self.audit.append(AuditKind.LLM_CALL, "commander", payload, event_id=ev.id)
        for s in getattr(self.commander, "last_intercepted", None) or []:
            self.audit.append(AuditKind.INTERCEPTED, "commander",
                              {"step": s, "reason": "tool not on the allow-list (dropped from plan)"
                               + ("; the rest of the model's plan was invalid, fallback plan used"
                                  if info.fell_back_to_mock else "")}, event_id=ev.id)
        # the repeat check runs on what the commander proposed, before code bounds the expectation
        # (bounding maps every cure-press temperature test onto the same line, so a genuinely new
        # hypothesis would otherwise look like the rejected one)
        again = self._repeats_rejected(plan.hypothesis, rejected)
        run.sop_handoff = phase == "replan" and bool(extra.get("sop_path"))
        exp, fixes = self._code_expectation(ev, plan, handoff=run.sop_handoff)
        if fixes:
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"expectation_bounded": fixes,
                               "from": plan.hypothesis.expectation.model_dump(mode="json")
                               if plan.hypothesis.expectation else None,
                               "to": exp.model_dump(mode="json") if exp else None}, event_id=ev.id)
        if exp != plan.hypothesis.expectation:
            plan = plan.model_copy(update={"hypothesis": plan.hypothesis.model_copy(update={"expectation": exp})})
        if again and not plan.safe_stop:
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"error": "commander re-proposed a rejected hypothesis", "match": again,
                               "cause_code": plan.hypothesis.cause_code}, event_id=ev.id)
            plan = plan.model_copy(update={"safe_stop": True})
        if ev.source == "plant":
            plan = self._plant_plan_guard(run, plan)
        hyp = plan.hypothesis.model_copy(update={"status": HypothesisStatus.ACTIVE})
        cur = ev.hypotheses[-1] if ev.hypotheses else None
        if phase == "replan" and cur is not None and cur.status is HypothesisStatus.ACTIVE \
                and hyp.cause_code == cur.cause_code:
            # same hypothesis re-planned (supervisor reject, failed write, infeasible solve): it was
            # never disproved, so it keeps its id and its place
            hyp = hyp.model_copy(update={"id": cur.id, "evidence": list(dict.fromkeys([*cur.evidence, *hyp.evidence]))})
            ev.hypotheses[-1] = hyp
        else:
            if hyp.id in {h.id for h in ev.hypotheses}:       # a real model may reuse "H1" on replan
                hyp = hyp.model_copy(update={"id": f"H{len(ev.hypotheses) + 1}"})
            if cur is not None and cur.status is HypothesisStatus.ACTIVE:
                cur.status = HypothesisStatus.PROPOSED            # superseded without a test
            ev.hypotheses.append(hyp)
        plan = plan.model_copy(update={"hypothesis": hyp})
        run.plan, run.plan_at = plan, self._now()
        run.results = {}
        if ev.steps:                                      # keep the earlier round visible (page 7 decomposition)
            prev_h = ev.plan.hypothesis.id if ev.plan is not None else None
            ev.prior_rounds.append(StepRound(round=len(ev.prior_rounds) + 1, hypothesis_id=prev_h,
                                             goal=ev.goal, steps=ev.steps))
        ev.plan, ev.goal = plan, plan.goal
        ev.steps = [StepRun(step=s, state="pending") for s in plan.steps]
        self._narrate_hypothesis(run, phase, rejected)
        if not plan.safe_stop:
            await self._pace(llm_started, max(MODEL_LLM_PLAN_S, info.total_ms / 1000.0))
            self._narrate_steps(run)
        return plan

    # ---- VERIFY must test the event, not whatever the LLM picked ----
    EXPECT_MARGIN_C = 1.0
    EXPECT_WITHIN_MIN = (5.0, 60.0)
    DEFAULT_WITHIN_MIN = 30.0

    @staticmethod
    def _takes_machine_out(ev: Event, plan: CommanderPlan) -> bool:
        """The plan contains the event machine (taken out of service, or removed from the schedule)."""
        for st in plan.steps:
            if st.tool is ToolName.NOTIFY_TEAMS and st.args.get("kind") == NotifyKind.MACHINE_OUT.value \
                    and st.args.get("machine_id") in (None, ev.machine_id):
                return True
            if st.tool is ToolName.SOLVE_SCHEDULE and ev.machine_id in (st.args.get("unavailable_presses") or []):
                return True
        return False

    def _allowed_channels(self, ev: Event) -> set[Channel]:
        mdef = MACHINE_BY_ID.get(ev.machine_id)
        out = {ev.anomaly.channel}
        if mdef is not None and (mdef.line is Line.CURE or mdef.handles_compound):
            out.add(Channel.TEMP)                 # the safety-relevant reading on presses and compound machines
        return out

    def _code_expectation(self, ev: Event, plan: CommanderPlan, handoff: bool = False) -> tuple[Any, list[str]]:
        """Code owns what VERIFY tests. A plan that takes the event machine out of service is checked
        by that status (expectation None, never CONFIRMED). Any other plan gets an expectation on the
        event machine and the anomaly's channel (or temperature on a press / compound machine), with
        a code-bound threshold; a missing one is built from the anomaly."""
        exp = plan.hypothesis.expectation
        if plan.safe_stop:
            return exp, []
        if handoff:     # page 7: tools failed twice -> SOP path, people take over; nothing to read
            return None, (["SOP hand-off: no reading to verify"] if exp is not None else [])
        if ev.anomaly.kind.value in UNMEASURED_KINDS:   # plan-vs-actual events: no sensor outcome to test
            return None, (["no measurable channel for this anomaly kind"] if exp is not None else [])
        if self._takes_machine_out(ev, plan):
            return None, (["containment plan: verified by machine status, not a reading"] if exp is not None else [])
        fixes: list[str] = []
        a = ev.anomaly
        if exp is None:
            exp = self._default_expectation(ev, a.channel, self.DEFAULT_WITHIN_MIN)
            fixes.append("no expectation -> built from the anomaly")
        if exp.channel not in self._allowed_channels(ev):
            fixes.append(f"channel {exp.channel.value} -> {a.channel.value}")
            exp = self._default_expectation(ev, a.channel, exp.within_min)
        exp2, more = self._bound_expectation(ev, exp)
        return exp2, fixes + more

    def _bound_for(self, ev: Event, channel: Channel) -> tuple[str | None, float | None]:
        """(op, threshold) code requires for a test on this channel of the event machine."""
        from warroom.contracts import CompoundStage
        from warroom.factory import COMPOUND_STOP_C, CURATIVE_STOP_C
        a = ev.anomaly
        mdef = MACHINE_BY_ID.get(ev.machine_id)
        if channel is Channel.TEMP and mdef is not None and mdef.line is Line.CURE:
            return ">=", CURE_TEMP_MIN_C + self.EXPECT_MARGIN_C                # recover into the cure band
        if channel is Channel.TEMP and mdef is not None and mdef.handles_compound:
            st = self._machine_state(ev.machine_id)
            limit = CURATIVE_STOP_C if st is not None and st.compound_stage is CompoundStage.CURATIVE_ADDED \
                else COMPOUND_STOP_C
            return "<=", limit - 2 * self.EXPECT_MARGIN_C                      # back under the stop line
        if channel is a.channel and a.baseline is not None and a.value != a.baseline:
            return (">=" if a.value < a.baseline else "<="), a.value           # at least no worse than now
        return None, None

    def _default_expectation(self, ev: Event, channel: Channel, within: float) -> Expectation:
        a = ev.anomaly
        op, bound = self._bound_for(ev, channel)
        if op is None:
            op, bound = (">=" if a.value < a.baseline else "<="), a.value
        return Expectation(machine_id=ev.machine_id, channel=channel, op=op, value=float(bound), within_min=within)

    def _bound_expectation(self, ev: Event, exp: Any) -> tuple[Any, list[str]]:
        """Pin the expectation to the event's machine and bound its threshold by code, so VERIFY
        cannot be satisfied trivially (another machine, the wrong direction, a threshold the
        faulty state already meets). Returns (expectation, list of fixes applied)."""
        a = ev.anomaly
        fixes: list[str] = []
        upd: dict[str, Any] = {}
        if exp.machine_id != ev.machine_id:
            fixes.append(f"machine {exp.machine_id} -> {ev.machine_id}")
            upd["machine_id"] = ev.machine_id
        lo_w, hi_w = self.EXPECT_WITHIN_MIN
        if not lo_w <= exp.within_min <= hi_w:
            upd["within_min"] = min(hi_w, max(lo_w, exp.within_min))
            fixes.append(f"within_min {exp.within_min:g} -> {upd['within_min']:g}")
        channel = exp.channel
        op, value = exp.op, exp.value
        if op == "between":                                  # keep the bound that counters the anomaly
            dropping = a.baseline is not None and a.value < a.baseline
            op, value = (">=", exp.value) if dropping else ("<=", exp.value_hi if exp.value_hi is not None
                                                             else exp.value)
            fixes.append(f"between -> {op}")
            upd["value_hi"] = None
        need_op, bound = self._bound_for(ev, channel)
        if need_op is not None:
            if op != need_op:
                fixes.append(f"op {op} -> {need_op}")
                op = need_op
            if bound is not None and (value < bound if op == ">=" else value > bound):
                fixes.append(f"value {value:g} -> {bound:g}")
                value = bound
        if op != exp.op:
            upd["op"] = op
        if value != exp.value:
            upd["value"] = float(value)
        return (exp.model_copy(update=upd) if upd else exp), fixes

    @staticmethod
    def _repeats_rejected(h: Hypothesis, rejected: list[Hypothesis]) -> str | None:
        """A rejected hypothesis comes back under a new name: same cause code, same statement, or
        the same test (machine, channel, op and threshold within 0.5)."""
        for r in rejected:
            if h.cause_code == r.cause_code:
                return f"cause_code {h.cause_code}"
            if h.statement.strip() and h.statement.strip() == r.statement.strip():
                return "statement"
            e1, e2 = h.expectation, r.expectation
            if e1 is not None and e2 is not None and (e1.machine_id, e1.channel, e1.op) == \
                    (e2.machine_id, e2.channel, e2.op) and abs(e1.value - e2.value) <= 0.5:
                return f"expectation of {r.id}"
        return None

    def _narrate_hypothesis(self, run: _Run, phase: str, rejected: list[Hypothesis]) -> None:
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
            via = " → ".join(run.replan_tools)
            if run.newly_rejected:
                text = f"推翻 {run.newly_rejected} → {h.id}：{h.statement}"
            else:
                # nothing was disproved: a supervisor reject, a failed write or an infeasible solve
                why = (run.replan_why or "換方案").split(" → ")[0]
                text = (f"{run.replan_why}（{h.id}：{h.statement}）" if run.replan_why.endswith("依現行排程重算")
                        else f"{why} → 換方案（{h.id}：{h.statement}）")
            run.newly_rejected = None
            if via:
                text += f"；{via}"
            text += f" → {plan.alert_card.title}"
            self._note(ev, "指揮官", text, "decision")

    def _narrate_steps(self, run: _Run) -> None:
        ev, plan = run.ev, run.plan
        assert plan is not None
        exp = expectation_text(plan.hypothesis.expectation)
        self._note(ev, "指揮官", f"拆步驟：{decompose_text(plan.steps)}" + (f"；預期：{exp}" if exp else ""), "info")

    def _resolve_stale(self, run: _Run, why: str) -> bool:
        """REPLAN by code: the supervisor approved this round, but its schedule went stale before the confirm (another
        event's version took effect first, concurrent anomalies, page 10) and nothing else of the round failed. The
        hypothesis and the plan still stand; only the schedule they were solved against moved, so the same steps run
        again (SIMULATE re-solves against the schedule in force, GATE carries the approval over when the content is
        the same, else the v2 card). No second commander call: with the real model it held the plant 10–18 s
        (concurrent_3, CURE-10) while the approved repair waited. Counts as a replan (max_replans bounds a loop of
        events invalidating each other). False when the event was safe-stopped."""
        ev, plan = run.ev, run.plan
        if ev.replans >= self.max_replans:
            self._safe_stop(run, f"已重新規劃 {ev.replans} 次仍未解決")
            return False
        self._to(ev, EventState.REPLAN, why)
        ev.replans += 1
        run.replan_why, run.rebasing = why, True
        h = ev.hypotheses[-1]
        self.audit.append(AuditKind.SYSTEM, "state_machine",
                          {"stale_resolve": "code", "commander_called": False, "hypothesis": h.id,
                           "steps": [s.id for s in plan.steps]}, event_id=ev.id)
        run.plan_at, run.results = self._now(), {}
        ev.prior_rounds.append(StepRound(round=len(ev.prior_rounds) + 1, hypothesis_id=h.id, goal=ev.goal,
                                         steps=ev.steps))
        ev.steps = [StepRun(step=s, state="pending") for s in plan.steps]
        self._note(ev, "系統", f"{why}（{h.id}：{h.statement}）；沿用原步驟，由程式重新求解", "dynamic")
        return True

    def approved_rebase(self, event_id: str) -> bool:
        """The event's round was approved and code is re-solving its stale schedule (_resolve_stale) until the next
        decision: the repair the supervisor approved goes on meanwhile (Runtime fast repair hold)."""
        run = self._runs.get(event_id)
        return run is not None and run.rebasing

    async def _replan(self, run: _Run, extra: dict[str, Any], why: str, discussion: bool = False) -> bool:
        """REPLAN: gather fresh context (neighbours on the same header/line, SOP), ask the commander
        for a new hypothesis. False when the event was safe-stopped. A discussion replan (issue #10)
        is bounded by MAX_DISCUSS_REPLANS instead of max_replans and keeps the plan's shape: when the
        model drops the schedule solve, the previous steps run again so the constraint is enforced."""
        ev = run.ev
        run.rebasing = False
        if not discussion and ev.replans >= self.max_replans:
            self._safe_stop(run, f"已重新規劃 {ev.replans} 次仍未解決")
            return False
        self._to(ev, EventState.REPLAN, why)
        if not discussion:
            ev.replans += 1
        run.replan_why = why
        prior = run.plan
        a = ev.anomaly
        run.replan_tools = []
        cur = ev.hypotheses[-1] if ev.hypotheses else None
        if cur is not None and cur.status is HypothesisStatus.ACTIVE:
            extra = {**extra, "current_hypothesis": {"id": cur.id, "cause_code": cur.cause_code,
                                                     "statement": cur.statement}}
        neigh = self._neighbours(a.machine_id) if a.machine_id in MACHINE_BY_ID else []
        r = await self._call(run, EventState.REPLAN, None, ToolName.READ_SENSORS,
                             {"machine_ids": neigh, "minutes": 30.0}) if neigh else \
            ToolResult(call_id="none", tool=ToolName.READ_SENSORS, ok=False, error="no machine")
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
            self._safe_stop(run, "指揮官要求安全中止" if plan else (run.commander_error or "指揮官無回應"))
            return False
        if discussion and prior is not None and any(st.tool is ToolName.SOLVE_SCHEDULE for st in prior.steps) \
                and not any(st.tool is ToolName.SOLVE_SCHEDULE for st in plan.steps):
            # the supervisor's condition is a schedule condition: a plan with no solve could not honour
            # it. Keep the model's hypothesis, run the previous steps again (constraints applied by code)
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"discussion_replan_guard": "model plan had no solve_schedule; previous steps reused",
                               "model_steps": [st.id + ":" + st.tool.value for st in plan.steps]}, event_id=ev.id)
            plan = plan.model_copy(update={"steps": prior.steps})
            run.plan = ev.plan = plan
            ev.steps = [StepRun(step=st, state="pending") for st in plan.steps]
            self._narrate_steps(run)
        return True

    # ------------------------------------------------------------------ SIMULATE
    async def _simulate(self, run: _Run) -> dict[str, Any] | None:
        """Run read/compute steps in dependency order. Returns a failure dict when a tool failed
        after its retry (→ SOP path); sets run.feasible."""
        ev, plan = run.ev, run.plan
        assert plan is not None
        run.feasible = True
        self._press_maintenance(run)
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
            resolved = self._bind_physics(run, resolved)
            resolved = self._bind_plant(run, resolved)
            resolved = self._apply_constraints(run, resolved)
            sr.step = resolved
            kw = {"evidence": self._evidence(run)} if (self._gate_takes_evidence and
                                                       step.tool is ToolName.SOLVE_SCHEDULE) else {}
            gd = self.gate.check(resolved, EventState.SIMULATE, True, **kw)
            sr.gate = gd
            if gd.verdict is GateVerdict.REJECT:
                sr.state = "rejected"
                self._log_reject(run, resolved, gd)
                continue
            sr.state = "running"
            self._publish(WsType.EVENT, ev)
            if step.tool is ToolName.SOLVE_SCHEDULE and line_bits:
                self._note(ev, "工具", "　".join(line_bits), "tool")   # S1/S2 land before the solve starts
                line_bits = []
            res = await self._call(run, EventState.SIMULATE, step.id, step.tool, resolved.args)
            sr.result = res
            if not res.ok:
                sr.state = "failed"
                self._note(ev, "工具", f"[工具] {step.id} {tool_label(step.tool)}重試仍失敗"
                           f"（{tool_error_text(step.tool, res.error)}）→ 走 SOP", "dynamic")
                return {"step_id": step.id, "tool": step.tool.value, "error": res.error}
            sr.state = "ok"
            run.results[step.id] = res
            if step.tool is ToolName.SOLVE_SCHEDULE:
                self._note(ev, "求解器", f"[工具] {step.id}：{result_text(step.tool, resolved.args, res.data)}", "tool")
                if res.data.get("fell_back"):
                    self._note(ev, "求解器", "求解逾時 → 切換備援求解器（heuristic）", "dynamic")
                if not res.data.get("feasible", False):
                    run.feasible = False
                elif ev.constraints:
                    viol = self._constraint_violations(run, res.data)
                    self.audit.append(AuditKind.GATE if viol else AuditKind.SYSTEM, "state_machine",
                                      {"constraint_check": step.id, "schedule_version": res.data.get("schedule_version"),
                                       "constraints": [c.label for c in ev.constraints], "ok": not viol,
                                       "violations": viol[:10]}, event_id=ev.id)
                    if viol:
                        run.feasible = False
                        self._note(ev, "系統", f"求解結果未遵守主管條件（{'；'.join(viol[:3])}）→ 不採用", "dynamic")
                    else:
                        self._note(ev, "系統", f"程式檢查：排程 v{res.data.get('schedule_version')} 遵守主管條件"
                                   f"（{'、'.join(c.label for c in ev.constraints)}）", "info")
            else:
                today = self._now().date()
                line_bits.append(f"[工具] {step.id}：{result_text(step.tool, resolved.args, res.data, today)}")
        if line_bits:
            self._note(ev, "工具", "　".join(line_bits), "tool")
        return None

    PRESS_FAULT_KINDS = frozenset({AnomalyKind.TEMP_DROP, AnomalyKind.STEAM_LOW, AnomalyKind.PRESSURE_LOW})

    def _press_maintenance(self, run: _Run) -> None:
        """A press temperature drop gets a maintenance notice (R1, auto), so a 檢修 task exists and the steam fault
        is repaired when it is done (the runtime clears a machine's fault once every repair task of the event on
        it is finished). Without it the H1 plan (延長硫化、重排) closed the event while presses 3 and 10 of
        concurrent_3 stayed at 122–123 °C 「注意」 for good. Code adds it when the plan has none (the rule plan
        and a real model's alike), once per event. A plan that takes the press out creates 停用檢修 itself: the
        earlier round's 檢修 task still open is withdrawn, so it cannot clear the fault while the press-out card
        waits (page 11: H1 檢修 → H2 7 號機停用)."""
        ev, plan = run.ev, run.plan
        mdef = MACHINE_BY_ID.get(ev.machine_id)
        if plan is None or plan.safe_stop or run.sop_handoff or ev.source != "detector" or mdef is None \
                or mdef.line is not Line.CURE or ev.anomaly.kind not in self.PRESS_FAULT_KINDS:
            return
        mid = ev.machine_id
        earlier = [sr for r in ev.prior_rounds for sr in r.steps if sr.step.tool is ToolName.NOTIFY_TEAMS
                   and sr.step.args.get("kind") == NotifyKind.MAINTENANCE.value
                   and sr.step.args.get("machine_id") in (None, mid) and sr.state == "ok"]
        if self._takes_machine_out(ev, plan):
            board = self._board()
            for sr in earlier:
                gone = board.cancel(ev.id, sr.step.id, self._now()) if board is not None else []
                if gone:
                    self.audit.append(AuditKind.TASK, "state_machine",
                                      {"cancelled": gone, "superseded_step": sr.step.id,
                                       "reason": "press taken out: 停用檢修 replaces the earlier 檢修"}, event_id=ev.id)
                    self._note(ev, "系統", f"{machine_label(mid)}改為停用檢修：收回前一輪「檢查蒸汽供應」的檢修任務", "info")
            return
        if earlier or any(sr.step.tool is ToolName.NOTIFY_TEAMS and sr.step.args.get("kind") == NotifyKind.MAINTENANCE.value
                          and sr.step.args.get("machine_id") in (None, mid) for sr in ev.steps):
            return
        used = {sr.step.id for sr in ev.steps}
        n = 1 + max((int(m.group(1)) for x in used if (m := re.fullmatch(r"S(\d+)[a-z]?", x))), default=0)
        while f"S{n}" in used:
            n += 1
        step = PlanStep(id=f"S{n}", tool=ToolName.NOTIFY_TEAMS, purpose="通知維修檢查蒸汽供應（自動）",
                        args={"teams": [TeamId.MAINTENANCE.value], "kind": NotifyKind.MAINTENANCE.value, "machine_id": mid,
                              "message": f"{machine_label(mid)}溫度下降，請檢查蒸汽供應與溫度計"})
        # a step of this round only (not of run.plan): a discussion replan that reuses the plan's steps does not
        # send the notice twice
        ev.steps.append(StepRun(step=step, state="pending"))
        self.audit.append(AuditKind.SYSTEM, "state_machine",
                          {"maintenance_notice_added": step.id, "machine_id": mid,
                           "reason": "press temperature drop: maintenance must check the steam supply (R1)"},
                          event_id=ev.id)

    def _log_reject(self, run: _Run, step: PlanStep, gd: GateDecision) -> None:
        kind = AuditKind.INTERCEPTED if gd.risk is None else AuditKind.GATE
        self.audit.append(kind, "gate", {"step_id": step.id, "tool": step.tool.value, "args": step.args,
                                         "decision": gd.model_dump(mode="json")}, event_id=run.ev.id)
        self._note(run.ev, "系統", f"動作把關攔截 {step.id} {tool_label(step.tool)}：{gate_reason_text(gd.reasons)}",
                   "dynamic")

    # ------------------------------------------------------------------ GATE
    def _cross_team(self, run: _Run, args: dict[str, Any]) -> bool | None:
        v = args.get("schedule_version")
        for res in run.results.values():
            if res.tool is ToolName.SOLVE_SCHEDULE and res.data.get("schedule_version") == v:
                sched = res.data.get("schedule") or {}
                return any(c.get("kind") == "transfer" for c in sched.get("staffing_changes") or [])
        return None

    @staticmethod
    def _evidence(run: _Run, provisional: list[PlanStep] | None = None) -> dict[str, Any]:
        """What THIS event's own tool results support, for the gate: schedule versions a feasible
        solve produced (confirm_schedule may only activate those), cure-formula totals per press
        (set_cure_time and solve_schedule.cure_overrides_h may only carry the formula's value for
        that press, never an AI guess), the event's machine (an R1 notice must be about it) and
        cure_notice = the cure time an auto notice may quote: an executed set_cure_time of this event
        (or, with ``provisional``, one awaiting approval in this round, so the notice waits for it)
        paired with that press's formula extra hours."""
        feasible_versions: list[int] = []
        cure_h: list[float] = []
        by_press: dict[str, list[float]] = {}
        for sid, res in run.results.items():
            if not res.ok:
                continue
            if res.tool is ToolName.SOLVE_SCHEDULE and res.data.get("feasible") and \
                    res.data.get("schedule_version") is not None:
                feasible_versions.append(int(res.data["schedule_version"]))
            elif res.tool is ToolName.CURE_FORMULA and res.data.get("total_cure_h") is not None:
                cure_h.append(float(res.data["total_cure_h"]))
                press = run.formula_press.get(sid)
                if press:
                    by_press.setdefault(press, []).append(float(res.data["total_cure_h"]))
        extra_by_press: dict[str, list[tuple[float, float]]] = {}
        for sid, res in run.results.items():
            press = run.formula_press.get(sid)
            if res.ok and res.tool is ToolName.CURE_FORMULA and press and res.data.get("extra_h") is not None \
                    and res.data.get("total_cure_h") is not None:
                extra_by_press.setdefault(press, []).append((float(res.data["total_cure_h"]), float(res.data["extra_h"])))
        by_id = {sr.step.id: sr.step for sr in run.ev.steps}
        set_steps = [by_id[sid].args for sid, res in run.results.items()
                     if res.ok and res.tool is ToolName.SET_CURE_TIME and sid in by_id]
        set_steps += [p.args for p in provisional or [] if p.tool is ToolName.SET_CURE_TIME]
        cure_notice: dict[str, dict[str, float]] = {}
        for a in set_steps:
            press, total = a.get("press_id"), a.get("total_cure_h")
            if not isinstance(total, (int, float)):
                continue
            extra = next((e for t, e in reversed(extra_by_press.get(str(press), [])) if abs(t - float(total)) <= 0.05), None)
            if extra is not None:
                cure_notice[str(press)] = {"total_h": float(total), "extra_h": extra}
        return {"feasible_versions": feasible_versions, "cure_formula_h": cure_h, "cure_formula_by_press": by_press,
                "event_machine_id": run.ev.machine_id, "cure_notice": cure_notice}

    def _sim_ok(self, run: _Run, step: PlanStep) -> tuple[bool, str]:
        """Page 5/7: a write passes the gate only when a simulation shows it is feasible. Here that is
        a feasible solve_schedule of THIS plan round that covers what the write changes:
          confirm_schedule   the version came from such a solve
          set_cure_time      the solve had cure_overrides_h for that press at that value
          machine_out (press) the solve had that press in unavailable_presses
          overtime/staff_move a feasible solve exists (its staffing options are where they come from)
        Notices (maintenance/info) change no capacity. Feed-rate notices (slow/pause feed) and taking
        a non-press machine out are not modelled by the press-centric solver: they always need the
        supervisor (R3) and the card says the schedule was not re-solved."""
        solves = [(sr.step.args, res.data) for sid, res in run.results.items()
                  if res.ok and res.tool is ToolName.SOLVE_SCHEDULE and res.data.get("feasible")
                  for sr in run.ev.steps if sr.step.id == sid]
        a, tool = step.args, step.tool
        if tool is ToolName.CONFIRM_SCHEDULE:
            v = a.get("schedule_version")
            return (any(d.get("schedule_version") == v for _, d in solves),
                    f"no feasible solve_schedule in this event produced schedule v{v}")
        if tool is ToolName.SET_CURE_TIME:
            press, h = a.get("press_id"), a.get("total_cure_h")
            ok = isinstance(h, (int, float)) and any(
                isinstance((sa.get("cure_overrides_h") or {}).get(press), (int, float))
                and abs(float(sa["cure_overrides_h"][press]) - float(h)) <= 0.05 for sa, _ in solves)
            return ok, f"no feasible solve_schedule in this event re-planned {press} with {fmt_num(h)} h cure"
        if tool is ToolName.NOTIFY_TEAMS:
            kind = a.get("kind")
            if kind == NotifyKind.MACHINE_OUT.value:
                mid = a.get("machine_id") or run.ev.machine_id
                if mid in CURE_PRESS_IDS:
                    return (any(mid in (sa.get("unavailable_presses") or []) for sa, _ in solves),
                            f"no feasible solve_schedule in this event re-planned without {mid}")
                return True, ""
            if kind in STAFFING_KINDS:
                return bool(solves), "staffing change with no feasible solve_schedule in this event"
        return True, ""

    # ---- a write and the schedule it needs are one decision (no split-solve bypass) ----
    def _covering_versions(self, run: _Run, step: PlanStep) -> set[int] | None:
        """Schedule versions whose solve covers this write: set_cure_time -> a feasible solve with
        that press's cure override at that value; machine_out of a press -> a feasible solve with the
        press in unavailable_presses. None: the write is not tied to a schedule."""
        a, tool = step.args, step.tool
        if tool is ToolName.SET_CURE_TIME:
            press, h = a.get("press_id"), a.get("total_cure_h")

            def covers(sa: dict[str, Any]) -> bool:
                o = (sa.get("cure_overrides_h") or {}).get(press)
                return isinstance(h, (int, float)) and isinstance(o, (int, float)) and abs(float(o) - float(h)) <= 0.05
        elif tool is ToolName.NOTIFY_TEAMS and a.get("kind") == NotifyKind.MACHINE_OUT.value \
                and (a.get("machine_id") or run.ev.machine_id) in CURE_PRESS_IDS:
            mid = a.get("machine_id") or run.ev.machine_id

            def covers(sa: dict[str, Any]) -> bool:
                return mid in (sa.get("unavailable_presses") or [])
        else:
            return None
        by_id = {sr.step.id: sr.step for sr in run.ev.steps}
        out: set[int] = set()
        for sid, res in run.results.items():
            if res.ok and res.tool is ToolName.SOLVE_SCHEDULE and res.data.get("feasible") and sid in by_id \
                    and isinstance(res.data.get("schedule_version"), int) and covers(by_id[sid].args):
                out.add(int(res.data["schedule_version"]))
        return out

    def _store(self):
        return self.store if self.store is not None else getattr(getattr(self.tools, "ctx", None), "store", None)

    def _in_force_covers(self, step: PlanStep) -> bool:
        """The schedule active now already carries what the write changes: the press's in-mold job at
        that cure time (set_cure_time), or no future mold-in on the press (machine_out)."""
        store = self._store()
        if store is None:
            return False
        try:
            jobs = store.current().schedule.jobs
        except Exception:  # noqa: BLE001
            return False
        a = step.args
        if step.tool is ToolName.SET_CURE_TIME:
            h = a.get("total_cure_h")
            return isinstance(h, (int, float)) and any(
                j.press_id == a.get("press_id") and j.frozen and abs(float(j.cure_h) - float(h)) <= 0.05 for j in jobs)
        if step.tool is ToolName.NOTIFY_TEAMS:
            mid = a.get("machine_id")
            now = self._now()
            return mid is not None and not any(j.press_id == mid and j.mold_in > now for j in jobs)
        return False

    def _enforce_coupling(self, run: _Run, auto: list[StepRun], deferred: list[StepRun],
                          needs: list[StepRun]) -> None:
        """Pages 5/7: a write passes only when the simulation behind it is the schedule that goes into
        force. set_cure_time and taking a press out pass only when THIS round confirms the version
        whose solve carried that change (or the active schedule already does). A rejected confirm (or
        none at all) therefore drops them before the card is built; anything that depends on a dropped
        write is dropped too."""
        live = auto + deferred + needs
        confirmed = {sr.step.args.get("schedule_version") for sr in live if sr.step.tool is ToolName.CONFIRM_SCHEDULE}
        for sr in list(live):
            cov = self._covering_versions(run, sr.step)
            if cov is None or (cov & confirmed) or self._in_force_covers(sr.step):
                continue
            what = "硫化時間" if sr.step.tool is ToolName.SET_CURE_TIME else "停用機台"
            why = (f"本輪生效的排程（v{'、v'.join(str(v) for v in sorted(x for x in confirmed if x is not None))}）"
                   if confirmed else "本輪沒有可生效的排程") + f"不含此{what}的模擬結果"
            self._drop_write(run, sr, auto, deferred, needs, "rejected", why,
                             f"write not covered by the schedule confirmed this round (covering solves: "
                             f"{sorted(cov)}, confirmed: {sorted(x for x in confirmed if isinstance(x, int))})")
        changed = True
        while changed:
            changed = False
            by_id = {sr.step.id: sr for sr in run.ev.steps}
            for sr in list(auto + deferred + needs):
                if any(by_id[d].state in ("rejected", "skipped", "failed") for d in sr.step.depends_on
                       if d in by_id and by_id[d].step.tool in WRITE_TOOLS):
                    self._drop_write(run, sr, auto, deferred, needs, "skipped", "依賴的寫入步驟未通過把關",
                                     "a write it depends on was refused")
                    changed = True

    def _drop_noop_confirms(self, run: _Run, auto: list[StepRun], deferred: list[StepRun],
                            needs: list[StepRun]) -> None:
        """A confirm whose solve changes nothing (no press move or re-time, no staffing change, no cure
        override, no press taken out) asks the supervisor to approve nothing: it is skipped, with the
        notices that would announce it, and the schedule in force stays. Not a failure."""
        noop: set[str] = set()
        for sr in list(needs + auto + deferred):
            if sr.step.tool is not ToolName.CONFIRM_SCHEDULE:
                continue
            v = sr.step.args.get("schedule_version")
            src = [(st, d) for st, d in self._solve_steps(run) if d.get("schedule_version") == v]
            if not src or any(d.get("moves") or (d.get("schedule") or {}).get("staffing_changes")
                              or st.args.get("cure_overrides_h") or st.args.get("unavailable_presses")
                              for st, d in src):
                continue
            noop.add(sr.step.id)
            self._remove_quietly(sr, auto, deferred, needs, run)
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"noop_confirm": sr.step.id, "schedule_version": v,
                               "reason": "solve changed nothing: no approval asked, schedule in force kept"},
                              event_id=run.ev.id)
            self._note(run.ev, "系統", f"排程 v{v} 與現行排程相同（無機台異動、無人力調動）→ 不需核准，維持現行排程", "info")
        changed = bool(noop)
        while changed:
            changed = False
            for sr in list(needs + auto + deferred):
                if any(d in noop for d in sr.step.depends_on):
                    noop.add(sr.step.id)
                    self._remove_quietly(sr, auto, deferred, needs, run)
                    changed = True

    @staticmethod
    def _remove_quietly(sr: StepRun, auto: list[StepRun], deferred: list[StepRun], needs: list[StepRun],
                        run: _Run) -> None:
        for lst in (auto, deferred, needs):
            if sr in lst:
                lst.remove(sr)
        run.auto_ids.discard(sr.step.id)
        sr.state = "skipped"

    def _drop_write(self, run: _Run, sr: StepRun, auto: list[StepRun], deferred: list[StepRun],
                    needs: list[StepRun], state: str, why_zh: str, why_en: str) -> None:
        for lst in (auto, deferred, needs):
            if sr in lst:
                lst.remove(sr)
        run.auto_ids.discard(sr.step.id)
        sr.state = state
        self.audit.append(AuditKind.GATE if state == "rejected" else AuditKind.SYSTEM, "gate",
                          {"step_id": sr.step.id, "tool": sr.step.tool.value, "args": sr.step.args,
                           "dropped": state, "reason": why_en}, event_id=run.ev.id)
        run.round_problems.append({"step_id": sr.step.id, "tool": sr.step.tool.value, "state": state, "why": why_en})
        self._note(run.ev, "系統", f"動作把關攔截 {sr.step.id}：{why_zh}", "dynamic")

    def _cure_temp(self, run: _Run) -> float:
        """The temperature the press is curing at now: the detector's stable prediction (page 11:
        122±1 °C), else the anomaly reading, to 0.5 °C."""
        from warroom.agent.commander import stable_cure_temp
        v, _ = stable_cure_temp(run.ev.anomaly)
        return round(float(v) * 2) / 2

    def _bind_physics(self, run: _Run, step: PlanStep) -> PlanStep:
        """Physics numbers come from the plant and the formula, never from the LLM (page 11):
        cure_formula gets the event press's live cure job (size, setpoint, hours cured, standard
        cure hours, Ea = 85 kJ/mol) and the predicted temperature; a solve's cure override for a
        press takes this event's formula total for that press. Changes are audited."""
        ev = run.ev
        mdef = MACHINE_BY_ID.get(ev.machine_id)
        is_press = mdef is not None and mdef.line is Line.CURE
        if step.tool is ToolName.CURE_FORMULA:
            job = run.base_context.get("cure_job") or {}
            args = dict(step.args)
            press: str | None = None
            if is_press and job.get("size"):
                from warroom.factory import ACTIVATION_ENERGY_KJ_MOL, CURE_HOURS
                from warroom.contracts import TireSize
                size = TireSize(job["size"])
                args = {"size": size.value, "ref_temp_c": float(job.get("setpoint_c") or CURE_TEMP_NOMINAL_C),
                        "actual_temp_c": self._cure_temp(run), "elapsed_h": float(job.get("cure_elapsed_h") or 0.0),
                        "ref_cure_h": float(CURE_HOURS[size]), "activation_energy_kj_mol": float(ACTIVATION_ENERGY_KJ_MOL)}
                press = ev.machine_id
            elif is_press and self.sim is None:
                press = ev.machine_id          # no plant to read (offline test rig): the commander's inputs stand
            run.formula_press[step.id] = press
            if args != step.args:
                self.audit.append(AuditKind.SYSTEM, "state_machine",
                                  {"cure_formula_args_from_plant": step.id, "from": step.args, "to": args,
                                   "press": press}, event_id=ev.id)
            return step.model_copy(update={"args": args})
        if step.tool is ToolName.SOLVE_SCHEDULE and is_press:
            # a real model sometimes leaves the event press out of cure_overrides_h (2 of 3 runs of page 11): the
            # schedule then keeps the recipe cure, the gate refuses set_cure_time and the card shrinks to 「通知維修」
            # with 「排程未變更」. This round's formula result for the press fills it, like the formula inputs above
            # (not for a press the solve takes out, nor when another solve of the plan already carries it).
            mid = ev.machine_id
            f = self._evidence(run)["cure_formula_by_press"].get(mid)
            over = step.args.get("cure_overrides_h")
            if f and mid not in (step.args.get("unavailable_presses") or []) \
                    and not (isinstance(over, dict) and mid in over) \
                    and not any(sr.step.tool is ToolName.SOLVE_SCHEDULE and sr.step.id != step.id
                                and mid in (sr.step.args.get("cure_overrides_h") or {}) for sr in ev.steps):
                over = {**(over if isinstance(over, dict) else {}), mid: f[-1]}
                self.audit.append(AuditKind.SYSTEM, "state_machine",
                                  {"cure_overrides_from_formula": {mid: {"from": None, "to": f[-1]}},
                                   "step_id": step.id, "reason": "solve had no cure override for the event press"},
                                  event_id=ev.id)
                step = step.model_copy(update={"args": {**step.args, "cure_overrides_h": over}})
        if step.tool is ToolName.SOLVE_SCHEDULE and isinstance(step.args.get("cure_overrides_h"), dict):
            by_press = self._evidence(run)["cure_formula_by_press"]
            over = dict(step.args["cure_overrides_h"])
            changed = {p: (h, by_press[p][-1]) for p, h in over.items()
                       if by_press.get(p) and (not isinstance(h, (int, float)) or abs(float(h) - by_press[p][-1]) > 1e-9)}
            if changed:
                for p, (_, f) in changed.items():
                    over[p] = f
                self.audit.append(AuditKind.SYSTEM, "state_machine",
                                  {"cure_overrides_from_formula": {p: {"from": h, "to": f} for p, (h, f) in changed.items()},
                                   "step_id": step.id}, event_id=ev.id)
                return step.model_copy(update={"args": {**step.args, "cure_overrides_h": over}})
        return step

    def _apply_constraints(self, run: _Run, step: PlanStep) -> PlanStep:
        """Issue #10: the supervisor's conditions go into every solve by CODE, whatever the plan said:
        a press unavailable in a window -> press_blackouts, no overtime -> allow_overtime=False, no
        cross-team move -> allow_cross_team=False. Audited."""
        ev = run.ev
        if step.tool is not ToolName.SOLVE_SCHEDULE or not ev.constraints:
            return step
        args = dict(step.args)
        blk: dict[str, list[list[str]]] = {p: [list(map(str, w)) for w in ws]
                                           for p, ws in (args.get("press_blackouts") or {}).items()}
        for c in ev.constraints:
            if c.kind == "machine_unavailable" and c.machine_id in CURE_PRESS_IDS and c.start and c.end:
                w = [c.start.isoformat(), c.end.isoformat()]
                if w not in blk.setdefault(c.machine_id, []):
                    blk[c.machine_id].append(w)
            elif c.kind == "no_overtime":
                args["allow_overtime"] = False
            elif c.kind == "no_transfer":
                args["allow_cross_team"] = False
        if blk:
            args["press_blackouts"] = blk
        if args != step.args:
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"constraints_applied": step.id, "constraints": [c.label for c in ev.constraints],
                               "from": {k: step.args.get(k) for k in ("press_blackouts", "allow_overtime", "allow_cross_team")},
                               "to": {k: args.get(k) for k in ("press_blackouts", "allow_overtime", "allow_cross_team")}},
                              event_id=ev.id)
        return step.model_copy(update={"args": args})

    def _constraint_violations(self, run: _Run, data: dict[str, Any]) -> list[str]:
        """Check the solver's RESULT honours every constraint (never trust the plan, nor the solver)."""
        out: list[str] = []
        sched = data.get("schedule") or {}
        jobs = sched.get("jobs") or []
        changes = list(sched.get("staffing_changes") or []) + list(data.get("staffing_options") or [])
        for c in run.ev.constraints:
            if c.kind == "machine_unavailable" and c.start and c.end and c.machine_id in CURE_PRESS_IDS:
                for j in jobs:
                    if j.get("press_id") != c.machine_id or j.get("frozen"):
                        continue
                    mi, de = datetime.fromisoformat(str(j["mold_in"])), datetime.fromisoformat(str(j["demold"]))
                    if mi < c.end and c.start < de:
                        out.append(f"{j.get('order_id')} {j.get('id')} 排在 {c.label} 時段")
            elif c.kind == "no_overtime" and any(x.get("kind") == "overtime" for x in changes):
                out.append("排程仍含加班")
            elif c.kind == "no_transfer" and any(x.get("kind") == "transfer" for x in changes):
                out.append("排程仍含跨組調人")
        return out

    def _check(self, run: _Run, step: PlanStep, provisional: list[PlanStep] | None = None,
               **kw: Any) -> GateDecision:
        ok, why = self._sim_ok(run, step)
        if self._gate_takes_evidence and step.tool in WRITE_TOOLS:
            ev = self._evidence(run, provisional)
            if not ok:
                ev["simulation_why"] = why
            kw["evidence"] = ev
        return self.gate.check(step, EventState.EXECUTE, ok and run.feasible, **kw)

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
                run.round_problems.append({"step_id": step.id, "tool": step.tool.value, "state": "skipped",
                                           "why": "an input step did not produce a result"})
                continue
            waits = any(d in needs or d in deferred for d in write_deps)
            resolved = step.model_copy(update={"args": resolve_refs(step.args, run.results)})
            resolved = self._bind_plant(run, resolved)
            sr.step = resolved
            kw = {}
            if step.tool is ToolName.CONFIRM_SCHEDULE and self._gate_takes_cross_team:
                kw["cross_team"] = self._cross_team(run, resolved.args)
            gd = self._check(run, resolved, provisional=[x.step for x in needs], **kw)
            sr.gate = gd
            self.audit.append(AuditKind.GATE, "gate", {"step_id": step.id, "decision": gd.model_dump(mode="json")},
                              event_id=ev.id)
            if gd.verdict is GateVerdict.ALLOW and step.tool is ToolName.NOTIFY_TEAMS:
                press = cure_notice_press(resolved.args)
                if press is not None and press not in self._evidence(run)["cure_notice"]:
                    # quotes a cure time that is still awaiting approval: it goes out only after that
                    # set_cure_time has run, whatever depends_on the commander wrote (re-gated then)
                    waits = True
            if gd.verdict is GateVerdict.REJECT:
                sr.state = "rejected"
                self._log_reject(run, resolved, gd)
                run.round_problems.append({"step_id": step.id, "tool": step.tool.value, "state": "rejected",
                                           "why": "；".join(gd.reasons)[:300]})
            elif gd.verdict is GateVerdict.ALLOW:
                (deferred if waits else auto).append(sr)
                run.auto_ids.add(step.id)
            else:
                sr.state = "awaiting_approval"
                needs.append(sr)
                if gd.risk is not None and RISK_ORDER.index(gd.risk) > RISK_ORDER.index(risk):
                    risk = gd.risk
        self._enforce_coupling(run, auto, deferred, needs)
        self._drop_noop_confirms(run, auto, deferred, needs)
        risk = RiskLevel.R0_READ
        for sr in needs:
            if sr.gate is not None and sr.gate.risk is not None and RISK_ORDER.index(sr.gate.risk) > RISK_ORDER.index(risk):
                risk = sr.gate.risk
        options = self._options(run, needs) if needs else []
        for o in options:
            od = self._check(run, PlanStep(id=f"opt-{o.id}", tool=o.tool, args=o.args, purpose=o.label))
            if od.risk is not None and RISK_ORDER.index(od.risk) > RISK_ORDER.index(risk):
                risk = od.risk
        if needs:
            self._note(ev, "系統", f"動作把關：格式正確、模擬可行 → {len(needs)} 項需主管核准（{risk.value}）"
                       + (f"，{len(auto)} 項自動執行" if auto else ""), "human")
        elif auto:
            self._note(ev, "系統", f"動作把關：格式正確、模擬可行 → {len(auto)} 項自動執行（R1）", "human")
        return auto, deferred, needs, options, risk

    def _options(self, run: _Run, needs: list[StepRun]) -> list[ApprovalOption]:
        """A/B options for the supervisor. Staffing alternatives come from the solver
        (staffing_options, overtime listed first); otherwise plan staffing steps; otherwise one
        option that approves the gated steps as proposed."""
        _, staffing = self._event_staffing(run)
        chosen_labels: set[str] = set()
        for res in run.results.values():
            if res.tool is ToolName.SOLVE_SCHEDULE:
                for c in (res.data.get("schedule") or {}).get("staffing_changes") or []:
                    chosen_labels.add(c.get("label"))
        opts: list[ApprovalOption] = []
        run.option_changes = {}
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
                run.option_changes[oid] = dict(c)
                opts.append(ApprovalOption(id=oid, label=c.get("label") or args["message"],
                                           tool=ToolName.NOTIFY_TEAMS, args=args,
                                           recommended=c.get("label") in chosen_labels))
        else:
            staff_steps = [sr for sr in needs if sr.step.tool is ToolName.NOTIFY_TEAMS
                           and sr.step.args.get("kind") in STAFFING_KINDS]
            for sr in staff_steps:
                opts.append(ApprovalOption(id=chr(ord("A") + len(opts)), label=self._action_text(run, sr.step, True),
                                           tool=sr.step.tool, args=sr.step.args, recommended=not opts))
        if not opts:
            # built in code from the validated args, never the commander's own wording (purpose)
            first = needs[0].step
            label = self._action_text(run, first, True) if len(needs) == 1 else \
                "核准全部：" + "；".join(self._action_text(run, sr.step, True) for sr in needs)
            opts.append(ApprovalOption(id="A", label=label, tool=first.tool, args=first.args, recommended=True))
        if opts and not any(o.recommended for o in opts):
            opts[0] = opts[0].model_copy(update={"recommended": True})
        pref = [c for c in run.ev.constraints if c.kind == "prefer_option"]
        if pref and len(opts) > 1:
            want = pref[-1].option
            kind = {"overtime": NotifyKind.OVERTIME.value, "transfer": NotifyKind.STAFF_MOVE.value}.get(str(want))
            hit = next((o for o in opts if o.id == want or (kind and o.args.get("kind") == kind)), None)
            if hit is not None:      # the supervisor's preference becomes the recommended option (code, not model)
                opts = [o.model_copy(update={"recommended": o.id == hit.id}) for o in opts]
        return opts

    # ---- approval-card content: only what THIS event's own results say ----
    def _solve_steps(self, run: _Run) -> list[tuple[PlanStep, dict[str, Any]]]:
        by_id = {sr.step.id: sr.step for sr in run.ev.steps}
        return [(by_id[sid], res.data) for sid, res in run.results.items()
                if res.tool is ToolName.SOLVE_SCHEDULE and res.ok and sid in by_id]

    def _shifts(self) -> list[Any] | None:
        if self.sim is None or not hasattr(self.sim, "shifts"):
            return None
        try:
            return list(self.sim.shifts())
        except Exception:  # noqa: BLE001
            return None

    def _event_staffing(self, run: _Run) -> tuple[list[str], list[dict[str, Any]]]:
        """(gaps, staffing options) for the card. Only the solver's gaps count (a broad orders/shifts
        query lists every short shift in the plant), and of those only the shifts in which this
        event's own changes land: a moved or re-timed job, or a job on a press the event re-cured or
        took out. A shortage elsewhere in the week stays off this event's card and its A/B options.
        With no shift data (no sim) every solver gap is kept."""
        shifts = self._shifts()
        gaps: list[str] = []
        staffing: list[dict[str, Any]] = []
        filled = {p["key"] for p in run.staffing_done if not p["revoked"]}
        for step, data in self._solve_steps(run):
            opts = list(data.get("staffing_options") or [])
            if shifts is None:
                gaps += [str(g) for g in data.get("gaps") or []]
                staffing += opts
                continue
            jobs = {j.get("id"): j for j in (data.get("schedule") or {}).get("jobs") or []}
            presses = set(step.args.get("unavailable_presses") or []) | set(step.args.get("cure_overrides_h") or {})
            ops: list[datetime] = []
            for mv in data.get("moves") or []:
                for t in (mv.get("to_mold_in"), (jobs.get(mv.get("job_id")) or {}).get("demold")):
                    if t:
                        ops.append(datetime.fromisoformat(str(t)))
            for j in jobs.values():
                if j.get("press_id") in presses:
                    ops += [datetime.fromisoformat(str(j[k])) for k in ("mold_in", "demold") if j.get(k)]
            for g in data.get("gaps") or []:
                g = str(g)
                g_opts = [c for c in opts if g.startswith(self._gap_prefix(c))]
                sh = next((s for s in shifts for c in g_opts
                           if s.name == c.get("shift_name") and s.team.value == c.get("to_team")
                           and str(s.start.date()) == str(c.get("shift_date"))), None)
                if sh is not None and run.ev.source != "plant" and not any(sh.start <= t < sh.end for t in ops) \
                        and not any(_staff_key(c) in filled for c in g_opts):
                    continue                                 # gap not touched by this event's changes
                # (a plant event is about the plant's own day: a sick call IS the gap, so every gap counts;
                # a gap an earlier round of this event already staffed stays this event's: the supervisor may
                # re-pick for it, page 11 round 2 B -> A, even when this round's re-plan moves nothing into it)
                gaps.append(g)
                staffing += g_opts
        return list(dict.fromkeys(gaps)), staffing

    @staticmethod
    def _gap_prefix(c: dict[str, Any]) -> str:
        try:
            d = datetime.fromisoformat(str(c.get("shift_date"))).strftime("%m/%d")
        except ValueError:
            return "\0"
        return f"{d} {c.get('shift_name') or ''}"

    def _delivery_text(self, run: _Run) -> str:
        """交期 row: the solver's verdict when this event re-solved; otherwise the schedule is
        unchanged (the page-2 mill names the tire still going in on time when the sim knows it)."""
        solves = self._solve_steps(run)
        if solves:
            data = solves[-1][1]
            late = data.get("late_orders") or []
            hours = self._late_hours(data)
            # how late, not just who (concurrent_3: 「延誤：#A-311」 hid that it was 129 h behind its due date)
            verdict = "不受影響" if data.get("deliveries_met") else "延誤：" + ("、".join(
                o + (f"（晚 {fmt_num(round(hours[o]) if hours[o] >= 10 else round(hours[o], 1))} 小時）" if hours.get(o) else "")
                for o in late) or "有")
            if run.ev.source == "plant":
                shifts = order_shifts(data.get("moves"), (data.get("schedule") or {}).get("jobs"))
                if shifts:
                    verdict += "；" + "、".join(shifts)
            return verdict
        if self.sim is not None and hasattr(self.sim, "semi_finished"):
            preheat = next((p[2] for p in PROCESSES if p[1] == "預熱"), None)
            if run.ev.machine_id == preheat:
                try:
                    semi = self.sim.semi_finished()
                    if semi.planned_mold_in_at:
                        return f"不受影響（{semi.tire_label} 仍 {semi.planned_mold_in_at:%H:%M} 入硫化機）"
                except Exception:  # noqa: BLE001
                    pass
        return "排程未變更（本事件未重排，未經排程模擬）"

    def _late_hours(self, data: dict[str, Any]) -> dict[str, float]:
        """Hours each late order finishes after the end of its due date in this solve: the solver's own late_h
        for a moved tyre, else the order's last de-mold in the schedule against its due date (sim orders)."""
        out: dict[str, float] = {}
        for m in data.get("moves") or []:
            if (m.get("late_h") or 0) > 0:
                out[str(m.get("order_id"))] = max(out.get(str(m.get("order_id")), 0.0), float(m["late_h"]))
        try:
            due = {o.id: o.due for o in self.sim.orders()} if self.sim is not None and hasattr(self.sim, "orders") else {}
        except Exception:  # noqa: BLE001 - a fake plant without orders
            due = {}
        for oid in data.get("late_orders") or []:
            if oid in out or oid not in due:
                continue
            ends = [datetime.fromisoformat(str(j["demold"])) for j in (data.get("schedule") or {}).get("jobs") or []
                    if j.get("order_id") == oid and j.get("demold")]
            if ends:
                end_of_due = datetime.combine(due[oid], datetime.max.time(), tzinfo=TZ)
                h = (max(ends) - end_of_due).total_seconds() / 3600
                if h > 0:
                    out[oid] = h
        return out

    def _action_text(self, run: _Run, step: PlanStep, short: bool = False) -> str:
        """One line per gated write, from its validated args (what will actually run), with the
        code-computed reference next to any physics number. short: option label (a notify shows its
        kind; teams and the full message are in the request's actions)."""
        a = step.args
        if step.tool is ToolName.SET_CURE_TIME:
            ref = self._evidence(run)["cure_formula_h"]
            txt = f"{machine_label(str(a.get('press_id')))}硫化總時數 {fmt_num(a.get('total_cure_h'))} h"
            return txt + (f"（公式 {fmt_num(ref[-1])} h）" if ref else "（無公式結果）")
        if step.tool is ToolName.CONFIRM_SCHEDULE:
            # what the solver was told (code-filled formula overrides, presses taken out) and what it moved
            v = a.get("schedule_version")
            bits: list[str] = []
            for sstep, data in self._solve_steps(run):
                if data.get("schedule_version") != v:
                    continue
                for p, h in (sstep.args.get("cure_overrides_h") or {}).items():
                    bits.append(f"{machine_label(str(p), short=True)}硫化 {fmt_num(h)} h")
                for p in sstep.args.get("unavailable_presses") or []:
                    bits.append(f"{machine_label(str(p), short=True)}停用")
                mv = moves_text(data.get("moves"))
                bits += mv or ["無機台異動"]
            return f"排程 v{v} 生效" + (f"（{'；'.join(bits)}）" if bits else "")
        if step.tool is ToolName.NOTIFY_TEAMS:
            from warroom.agent.narrate import NOTIFY_LABELS, TEAM_LABELS
            try:
                kind = NOTIFY_LABELS.get(NotifyKind(a.get("kind")), "通知")
            except ValueError:
                kind = "通知"
            teams = "、".join(TEAM_LABELS.get(t, str(t)) for t in a.get("teams") or [])
            mid = a.get("machine_id")
            where = f"［{machine_label(str(mid))}］" if mid else ""
            if short:
                return kind                      # page 2: 「放慢生料投入」 (teams + message in actions)
            return f"{kind} → {teams}{where}：{a.get('message', '')}"
        return f"{step.tool.value} {json.dumps(a, ensure_ascii=False)[:80]}"

    def _actions(self, run: _Run, needs: list[StepRun], options: list[ApprovalOption]) -> list[str]:
        """核准內容: every gated write that approving runs, with its key args. A staffing step that
        the A/B choice replaces is shown once as that choice."""
        staff_choice = any(o.args.get("kind") in STAFFING_KINDS for o in options) and len(options) > 1
        out: list[str] = []
        for prev in run.staffing_done:                   # an earlier round already filled this gap
            if not prev["revoked"]:
                out.append(f"人力：第 {prev.get('round', '?')} 輪已核准並執行「{prev['label']}」"
                           f"（本輪改選其他方案會撤回並通知）")
        for sr in needs:
            if staff_choice and sr.step.tool is ToolName.NOTIFY_TEAMS and sr.step.args.get("kind") in STAFFING_KINDS:
                continue
            out.append(f"{sr.step.id} {self._action_text(run, sr.step)}")
        if staff_choice:
            out.append("人力：依所選方案（" + "／".join(f"{o.id} {o.label}" for o in options) + "）")
        offered = [run.option_changes[o.id] for o in options if o.id in run.option_changes]
        for sr in needs:
            if sr.step.tool is ToolName.CONFIRM_SCHEDULE:
                left = self._unapproved_staffing(run, sr.step.args.get("schedule_version"), offered)
                if left:
                    out.append("人力：本次不含人力調動（" + "、".join(
                        f"{self._gap_prefix(c)} {c.get('label')}" for c in left) + " 未列入核准，該班缺口維持原狀）")
        return out

    def _first_in_force(self) -> str:
        """Whose schedule took effect first: 「3 號機」 for the event that activated the version in force, else
        「另一事件的」 (concurrent_3: 3 號 approved first, 10 號's approved schedule went stale)."""
        store = self._store()
        try:
            cur = store.current() if store is not None else None
        except Exception:  # noqa: BLE001 - empty store
            cur = None
        eid = (self._activated_by.get(cur.version) or cur.event_id) if cur is not None else None
        other = self._events.get(eid) if eid else None
        return machine_label(other.machine_id) if other is not None else "另一事件的"

    def _request_approval(self, run: _Run, needs: list[StepRun], options: list[ApprovalOption],
                          risk: RiskLevel, stale: dict[str, Any] | None = None) -> ApprovalRequest:
        ev, plan = run.ev, run.plan
        assert plan is not None
        sup, run.supersede = run.supersede, None
        old: ApprovalRequest | None = sup["old"] if sup else None
        # concurrent events: the supervisor approved this card, but another event's schedule took effect first and
        # the re-solve differs. The new card is the next VERSION of the approved one (what changed and why), not a
        # fresh round with the same title, which on stage read as a failed approval
        rebased = old is None and stale is not None and stale.get("req") is not None
        if rebased:
            old = stale["req"]
            options = self._rebased_options(old, options)
        if old is not None:          # issue #10: a new version of the same round replaces the discussed card
            rnd, version = old.round, old.version + 1
            rid = f"AP-{ev.id}-{rnd}v{version}"
        else:
            run.approvals_made += 1
            rnd, version = run.approvals_made, 1
            rid = f"AP-{ev.id}-{rnd}"
        rejected = [h for h in ev.hypotheses if h.status is HypothesisStatus.REJECTED]
        if ev.source == "supervisor" and not rejected:
            reason = f"主管指令：{ev.command}"
        elif rejected:
            last = rejected[-1]
            reason = f"{last.id} 不成立：" + (last.evidence[-1] if last.evidence else "驗證未達預期")
        else:
            reason = f"假設 {plan.hypothesis.id}：{plan.hypothesis.statement}"
        if ev.source == "supervisor" and rejected:
            reason = f"主管指令：{ev.command}；" + reason
        if ev.source == "plant" and not rejected:
            reason = ev.anomaly.message
        # 已做 = only what has already run (page 11: 安全核心已處置；#A-311 改 9 號). What approving
        # will do (one line per gated write) and staffing an earlier round already sent are 核准內容
        # lines (_actions); there is no 「將做：…」 summary, which could name an unconfirmed solve.
        done = list(dict.fromkeys(run.done))
        if ev.safety_actions or self._safety_acted(ev.machine_id):
            done.insert(0, "安全核心已處置")
        gaps, _ = self._event_staffing(run)
        delivery = self._delivery_text(run)
        now = self._now()
        snap = self._card_snap(run, needs, options, delivery)
        changes = self._card_changes(run.card_snaps.get(old.id) if old else None, snap) if old else []
        if rebased:
            prev = next((o for o in old.options if old.decision and o.id == old.decision.option_id), None)
            keep = next((o for o in options if prev is not None and o.label == prev.label and len(options) > 1), None)
            changes = [f"{self._first_in_force()}排程先生效；{machine_label(ev.machine_id, short=True)}依現行排程重算"
                       + ("：" + "；".join(changes) if changes else "（機台異動同上一版）")] \
                + ([f"人力沿用主管上次的選擇：{keep.id} {keep.label}"] if keep is not None else [])
        req = ApprovalRequest(
            id=rid, event_id=ev.id, step_id=needs[0].step.id, round=rnd, created_at=now,
            risk=risk, title=plan.alert_card.title, reason=reason, done=done, gaps=list(dict.fromkeys(gaps)),
            delivery_impact=delivery, options=options, actions=self._actions(run, needs, options),
            expires_at=self._approval_expires_at(now), version=version, supersedes=old.id if old else None,
            changes=changes, constraints=[c.label for c in ev.constraints])
        run.card_snaps[rid] = snap
        req = self.approvals.request(req)
        ev.approvals.append(req.id)
        if old is not None:
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"card_version": version, "approval_id": rid, "supersedes": old.id, "changes": changes,
                               "constraints": req.constraints}, event_id=ev.id)
            self._note(ev, "系統", f"新核准卡 v{version} 取代 v{old.version}："
                       + ("；".join(changes) if changes else "內容與上一版相同"), "human")
        self._post_summary(run, req)
        self._publish(WsType.EVENT, ev)
        return req

    @staticmethod
    def _rebased_options(old: ApprovalRequest, options: list[ApprovalOption]) -> list[ApprovalOption]:
        """Options of the re-approval card: a lone 「核准全部：…」 becomes 「照新排程執行」 (what changed is in the
        card's changes), and the staffing option the supervisor picked on the approved card stays the
        recommended one while the new solve still offers it."""
        prev = next((o for o in old.options if old.decision and o.id == old.decision.option_id), None)
        if len(options) == 1 and options[0].args.get("kind") not in STAFFING_KINDS:
            return [options[0].model_copy(update={"label": "照新排程執行"})]
        if prev is not None and any(o.label == prev.label for o in options):
            return [o.model_copy(update={"recommended": o.label == prev.label}) for o in options]
        return options

    # ---- issue #10: what a card proposes, what changed since the card it replaced, and the plain summary
    def _card_snap(self, run: _Run, needs: list[StepRun], options: list[ApprovalOption], delivery: str) -> dict[str, Any]:
        moves: dict[str, dict[str, Any]] = {}
        for sr in needs:
            if sr.step.tool is not ToolName.CONFIRM_SCHEDULE:
                continue
            v = sr.step.args.get("schedule_version")
            for _, d in self._solve_steps(run):
                if d.get("schedule_version") == v:
                    for m in d.get("moves") or []:
                        moves[str(m.get("job_id"))] = dict(m)
        staff = [o for o in options if o.args.get("kind") in STAFFING_KINDS]   # the 人力方案 (A/B), not 核准全部
        return {"moves": moves, "options": [o.label for o in staff],
                "recommended": next((o.label for o in staff if o.recommended), None),
                "delivery": delivery, "cure": sorted(f"{machine_label(str(sr.step.args.get('press_id')))} "
                                                     f"{fmt_num(sr.step.args.get('total_cure_h'))} h"
                                                     for sr in needs if sr.step.tool is ToolName.SET_CURE_TIME)}

    @staticmethod
    def _card_changes(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
        """Code-built diff between two card versions: tires that now go to another press, staffing
        options added / removed, the recommended option, the delivery verdict."""
        if old is None:
            return []
        out: list[str] = []
        per_order: dict[str, int] = {}
        for jid in list(dict.fromkeys([*old["moves"], *new["moves"]])):
            o, n = old["moves"].get(jid), new["moves"].get(jid)
            ref = o or n
            before = (o or {}).get("to_press") or (n or {}).get("from_press")
            after = (n or {}).get("to_press") or (o or {}).get("from_press")
            if before == after or not before or not after:
                continue
            order = str(ref.get("order_id"))
            per_order[order] = per_order.get(order, 0) + 1
            if per_order[order] > 1:
                continue
            when = "（維持原排程）" if n is None else ""
            if n is not None and n.get("to_mold_in"):
                try:
                    when = f"（{datetime.fromisoformat(str(n['to_mold_in'])):%m/%d %H:%M} 入模）"
                except ValueError:
                    when = ""
            out.append(f"{order}：{machine_label(str(before), short=True)} → {machine_label(str(after), short=True)}{when}")
        for order, k in per_order.items():
            if k > 1:
                out.append(f"{order} 共 {k} 模改機台")
        gone = [x for x in old["options"] if x not in new["options"]]
        added = [x for x in new["options"] if x not in old["options"]]
        if gone:
            out.append("人力方案移除：" + "、".join(gone))
        if added:
            out.append("人力方案新增：" + "、".join(added))
        if old.get("recommended") != new.get("recommended") and new.get("recommended") and not added:
            out.append(f"建議方案：{old.get('recommended') or '—'} → {new['recommended']}")
        if old.get("delivery") != new.get("delivery"):
            out.append(f"交期：{old.get('delivery')} → {new.get('delivery')}")
        if old.get("cure") != new.get("cure"):
            out.append("硫化時間：" + ("、".join(new.get("cure") or []) or "不設定"))
        return out

    def _post_summary(self, run: _Run, req: ApprovalRequest) -> None:
        """One line for the supervisor, built by code from the card (every number is a tool result):
        「<事件編碼>｜請核准：<要做的事>；人力：<建議方案>。」 Kyle 10-21: 寫給主管的就是最簡單、最白話，告訴他怎麼做，
        不要贅詞（最多附上事件編碼）—— the old 「我建議…因為…你可以說…」 read the same on every card."""
        ev = run.ev
        acts: list[str] = []
        for a in req.actions:
            a = re.sub(r"^S\d+[a-z]? ", "", a)
            if a.startswith("人力："):
                continue                                  # the staffing choice is said once, below
            a = re.sub(r"（公式[^）]*）", "", a)            # 「41.1 h（公式 41.1 h）」: the same number twice
            a = re.sub(r"^排程 v(\d+) 生效", r"排程第 \1 版", a)
            m = re.match(r"^(.+?) → (.+?)［.+?］：", a)    # 「通知維修 → 維修組［3 號機］：<message>」
            acts.append(f"{m.group(1)}（{m.group(2)}）" if m else a)
        # 「10 號機硫化總時數 41.1 h」 when the schedule line already says 「10 號硫化 41.1 h」
        acts = [a for a in acts if not ((m := re.match(r"^(\d+) 號機硫化總時數 ([\d.]+ h)$", a))
                                        and any(f"{m.group(1)} 號硫化 {m.group(2)}" in x for x in acts if x is not a))]
        rec = next((o for o in req.options if o.recommended), None)
        if rec is not None and len(req.options) > 1:
            acts.append(f"人力：{rec.id} {rec.label}")
        text = (f"{ev.id}{'（v' + str(req.version) + '）' if req.version > 1 else ''}｜"
                f"請核准：{'；'.join(dict.fromkeys(acts)) or req.title}。")
        self.audit.append(AuditKind.SYSTEM, "agent", {"plan_summary": text, "approval_id": req.id,
                                                      "version": req.version}, event_id=ev.id)
        self._note(ev, "指揮官", text, "decision")

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
            text = (f"[人工] 主管{d.reason}" if d.reason.startswith("退回修改") else
                    "[人工] 主管否決" + (f"：{d.reason}" if d.reason else "")) + "；決定與理由寫入紀錄"
        elif d.decision is DecisionKind.MODIFY and d.modified_args and \
                not (opt is not None and opt.args.get("kind") in STAFFING_KINDS):
            text = f"[人工] 主管修改後核准（{opt.label if opt else ''}）；決定與理由寫入紀錄"
        elif opt is not None and opt.args.get("kind") == NotifyKind.OVERTIME.value and \
                any(o.args.get("kind") == NotifyKind.STAFF_MOVE.value for o in others):
            text = f"[人工] 否決跨組調人，改選{opt.label}；決定與理由寫入紀錄"
        elif d.decision is DecisionKind.MODIFY and not d.modified_args:
            # nothing but the option changed: say so, never "修改後核准" of an unchanged plan
            text = f"[人工] 主管改選{(opt.id + ' ' + opt.label) if opt else '方案'}；決定與理由寫入紀錄"
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
            # the broker refuses edits of a staffing option; a broker that does not is not trusted
            # either: the notice says exactly what the schedule records (team, shift, headcount)
            from warroom.agent.approvals import staffing_modification
            ignored = staffing_modification(opt, d.modified_args)
            if ignored:
                self.audit.append(AuditKind.INTERCEPTED, "state_machine",
                                  {"approval_id": req.id, "option_id": opt.id, "ignored_modified_args": ignored,
                                   "reason": "a staffing option cannot be modified"}, event_id=run.ev.id)
            args = dict(opt.args)
            step = PlanStep(id=f"{req.id}-{opt.id}", tool=opt.tool, args=args, purpose=f"{opt.id} {opt.label}")
            if opt.id in run.option_changes:
                run.step_changes[step.id] = run.option_changes[opt.id]
            gd = self._check(run, step)
            sr = StepRun(step=step, state="pending", gate=gd)
            run.ev.steps.append(sr)
            if gd.verdict is GateVerdict.REJECT:
                sr.state = "rejected"
                self._log_reject(run, step, gd)
            else:
                chosen.append(sr)
        if d.modified_args and not is_staff_option:
            # re-gate modified steps (MODIFY, or APPROVE that carries args): a modification can never
            # widen permissions or replace a formula value
            for sr in list(chosen):
                gd = self._check(run, sr.step)
                if gd.verdict is GateVerdict.REJECT:
                    sr.state = "rejected"
                    self._log_reject(run, sr.step, gd)
                    chosen.remove(sr)
        return chosen

    # ------------------------------------------------------------------ stale approval -> rebase
    def _content_sig(self, run: _Run, steps: list[StepRun], change: dict[str, Any] | None) -> dict[str, Any]:
        """What an approval puts in force, independent of version numbers: cure overrides and presses
        taken out that the confirmed solve carried, its press moves, cure times set, presses notified
        out, other approval-gated notices, and the staffing decision."""
        solves = self._solve_steps(run)
        over: set[tuple[str, float]] = set()
        unavail: set[str] = set()
        moves: set[tuple[str, str]] = set()
        cure: set[tuple[str, float]] = set()
        outs: set[str] = set()
        notices: set[tuple[str, tuple[str, ...]]] = set()
        for sr in steps:
            a = sr.step.args
            if sr.step.tool is ToolName.CONFIRM_SCHEDULE:
                for st, d in solves:
                    if d.get("schedule_version") != a.get("schedule_version"):
                        continue
                    over |= {(str(p), round(float(h), 1)) for p, h in (st.args.get("cure_overrides_h") or {}).items()
                             if isinstance(h, (int, float))}
                    unavail |= {str(p) for p in st.args.get("unavailable_presses") or []}
                    moves |= {(str(m.get("job_id")), str(m.get("to_press"))) for m in d.get("moves") or []}
            elif sr.step.tool is ToolName.SET_CURE_TIME and isinstance(a.get("total_cure_h"), (int, float)):
                cure.add((str(a.get("press_id")), round(float(a["total_cure_h"]), 1)))
            elif sr.step.tool is ToolName.NOTIFY_TEAMS:
                kind = a.get("kind")
                if kind == NotifyKind.MACHINE_OUT.value:
                    outs.add(str(a.get("machine_id") or run.ev.machine_id))
                elif kind not in STAFFING_KINDS and kind not in (NotifyKind.INFO.value, NotifyKind.MAINTENANCE.value):
                    notices.add((str(kind), tuple(sorted(str(t) for t in a.get("teams") or []))))
        staff = (change.get("kind"), change.get("label"), _staff_key(change)) if change else None
        return {"over": over, "unavail": unavail, "moves": moves, "cure": cure, "outs": outs,
                "notices": notices, "staff": staff}

    def _reuse_stale_approval(self, run: _Run, needs: list[StepRun],
                              options: list[ApprovalOption]) -> ApprovalRequest | None:
        """Concurrent events (page 10): the supervisor approved a schedule that went stale before it
        could be confirmed (another event's version took effect first). The re-solve against the
        schedule now active is put in force under that same approval ONLY when it carries exactly what
        was approved (same cure overrides and times, same presses out, same notices, same staffing
        choice, press moves no more than approved). Anything else gets a new card."""
        st = run.stale_approval
        if not st or st.get("sig") is None:
            return None
        old: ApprovalRequest = st["req"]
        d = old.decision
        if d is None or d.decision is DecisionKind.REJECT:
            return None
        old_opt = next((o for o in old.options if o.id == d.option_id), None)
        new_opt = None
        if old_opt is not None:
            new_opt = next((o for o in options if o.label == old_opt.label and o.tool is old_opt.tool), None)
        if new_opt is None and len(options) == 1 and (old_opt is None or old_opt.args.get("kind") not in STAFFING_KINDS):
            new_opt = options[0]
        if new_opt is None:
            return None
        change = run.option_changes.get(new_opt.id)
        is_staff = new_opt.args.get("kind") in STAFFING_KINDS
        steps = [sr for sr in needs if not (is_staff and sr.step.tool is ToolName.NOTIFY_TEAMS
                                            and sr.step.args.get("kind") in STAFFING_KINDS)]
        new_sig = self._content_sig(run, steps, change)
        old_sig = st["sig"]
        same = all(new_sig[k] == old_sig[k] for k in ("over", "unavail", "cure", "outs", "notices", "staff")) \
            and new_sig["moves"] <= old_sig["moves"]
        new_v = next((sr.step.args.get("schedule_version") for sr in needs
                      if sr.step.tool is ToolName.CONFIRM_SCHEDULE), None)
        self.audit.append(AuditKind.SYSTEM, "state_machine",
                          {"stale_approval": old.id, "stale_version": st.get("version"), "rebased_version": new_v,
                           "same_content": same, "reused": same}, event_id=run.ev.id)
        if not same:
            self._note(run.ev, "系統", f"重新求解的排程 v{new_v} 與主管已核准的內容不同 → 需重新核准", "dynamic")
            return None
        # recorded as its own approval (request + decision via "auto", naming the original decider and
        # the approval it carries over), so the audit log and the ✓ 已核准 list show exactly what ran
        now = self._now()
        rid = f"{old.id}-rebased"
        n = 2
        while True:
            try:
                self.approvals.get(rid)
            except Exception:  # noqa: BLE001 - free id
                break
            rid, n = f"{old.id}-rebased{n}", n + 1
        req = old.model_copy(update={
            "id": rid, "step_id": needs[0].step.id, "created_at": now, "options": options,
            "actions": self._actions(run, needs, options), "status": ApprovalStatus.PENDING, "decision": None,
            "expires_at": self._approval_expires_at(now)})
        self.approvals.request(req)
        run.ev.approvals.append(rid)
        from warroom.contracts import ApprovalDecision
        self.approvals.decide(ApprovalDecision(
            request_id=rid, decision=d.decision, option_id=new_opt.id, decided_by=d.decided_by, via="auto",
            reason=f"沿用 {old.id}：排程 v{st.get('version')} 過期後依現行排程重解為 v{new_v}，內容相同", decided_at=now))
        reused = self.approvals.get(rid)
        for sr in needs:
            if sr.state == "awaiting_approval":
                sr.state = "pending"
        self._note(run.ev, "系統", f"排程 v{st.get('version')} 已過期 → 依現行排程重新求解為 v{new_v}，"
                   f"內容與主管已核准（{old_opt.label if old_opt else old.title}）相同 → 沿用核准 {old.id}，直接生效",
                   "human")
        return reused

    # ------------------------------------------------------------------ staffing decision follow-through
    def _chosen_change(self, run: _Run, chosen: list[StepRun]) -> dict[str, Any] | None:
        """The solver StaffingChange behind the staffing option the supervisor picked, if any."""
        for sr in chosen:
            c = run.step_changes.get(sr.step.id)
            if c is not None:
                return c
        return None

    def _dedupe_staffing(self, run: _Run, chosen: list[StepRun], change: dict[str, Any]) -> list[StepRun]:
        """Picking the same staffing again (same shift, same choice) does not notify the teams twice."""
        same = any(p["key"] == _staff_key(change) and p["kind"] == change.get("kind")
                   and p["label"] == change.get("label") and not p["revoked"] for p in run.staffing_done)
        if not same:
            return chosen
        out = []
        for sr in chosen:
            if run.step_changes.get(sr.step.id) is change:
                sr.state = "skipped"
                prev = next((p for p in run.staffing_done if p["key"] == _staff_key(change)
                             and p["label"] == change.get("label") and not p["revoked"]), {})
                self._note(run.ev, "系統", f"人力沿用第 {prev.get('round', '?')} 輪已執行的「{change.get('label')}」，"
                           "不重複通知", "info")
                continue
            out.append(sr)
        return out

    def _in_force_staffing(self, run: _Run, store, exclude_version: int | None = None) -> list[StaffingChange]:
        """Staffing already decided: the active schedule's staffing changes (approved earlier) and this
        event's executed, not revoked staffing notifies."""
        out: list[StaffingChange] = []
        try:
            cur = store.current()
            if cur.version != exclude_version:
                out += list(cur.schedule.staffing_changes)
        except Exception:  # noqa: BLE001 - empty store
            pass
        for prev in run.staffing_done:
            c = run.step_changes.get(prev.get("step_id"))
            if c is not None and not prev["revoked"]:
                try:
                    out.append(StaffingChange.model_validate(c))
                except Exception:  # noqa: BLE001
                    pass
        return out

    def _unapproved_staffing(self, run: _Run, version: Any, offered: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Staffing changes the solver put in schedule ``version`` that this card does not offer as an
        option and that are not already in force: approving the schedule will NOT put them in force."""
        store = self.store if self.store is not None else getattr(getattr(self.tools, "ctx", None), "store", None)
        in_force = {_staff_key(c.model_dump(mode="json")) for c in self._in_force_staffing(run, store)} \
            if store is not None else set()
        keys = {_staff_key(c) for c in offered}
        out = []
        for _, data in self._solve_steps(run):
            if data.get("schedule_version") != version:
                continue
            for c in (data.get("schedule") or {}).get("staffing_changes") or []:
                if _staff_key(c) not in keys and _staff_key(c) not in in_force:
                    out.append(c)
        return out

    def _amend_schedule_staffing(self, run: _Run, chosen: list[StepRun], change: dict[str, Any] | None,
                                 req: ApprovalRequest) -> None:
        """The schedule about to be confirmed carries exactly the staffing that was decided: the option
        the supervisor picked on this card (page 11 02:31:30: 否決跨組調人，改選夜班加班), and for every
        other shift whatever is already in force. A staffing change the solver picked on its own (e.g.
        a cross-team transfer for a shortage this card never offered) is taken out: it was neither
        approved nor notified (page 7: cross-team moves need human approval), so that shift keeps its
        gap, exactly as before the event."""
        store = self.store if self.store is not None else getattr(getattr(self.tools, "ctx", None), "store", None)
        if store is None:
            return
        new_c: StaffingChange | None = None
        if change is not None:
            try:
                new_c = StaffingChange.model_validate(change)
            except Exception:  # noqa: BLE001 - malformed solver output: nothing chosen to add
                new_c = None
        for sr in chosen:
            if sr.step.tool is not ToolName.CONFIRM_SCHEDULE:
                continue
            v = sr.step.args.get("schedule_version")
            try:
                ver = store.get(int(v))
            except Exception:  # noqa: BLE001
                continue
            if ver.status is not ScheduleStatus.PROPOSED:
                continue
            key = lambda c: _staff_key(c.model_dump(mode="json"))  # noqa: E731
            in_force = self._in_force_staffing(run, store, exclude_version=ver.version)
            old = list(ver.schedule.staffing_changes)
            staffing: list[StaffingChange] = [new_c] if new_c is not None else []
            handled = {key(new_c)} if new_c is not None else set()
            replaced: list[StaffingChange] = []      # same shift, the supervisor chose differently
            dropped: list[StaffingChange] = []       # never offered, not in force: not put in force
            for c in old:
                k = key(c)
                if new_c is not None and k == key(new_c):
                    if (c.kind, c.label) != (new_c.kind, new_c.label):
                        replaced.append(c)
                    continue
                if k in handled:
                    continue
                kept = [x for x in in_force if key(x) == k]
                if kept:
                    staffing += [x for x in kept if x not in staffing]
                    handled.add(k)
                    if not any((x.kind, x.label) == (c.kind, c.label) for x in kept):
                        replaced.append(c)
                else:
                    dropped.append(c)
            if staffing == old:
                continue                                   # the proposal already has exactly that staffing
            reason = ver.reason
            for c in replaced:
                to = next((x for x in staffing if key(x) == key(c)), None)
                if to is not None:
                    reason = reason.replace(f"→{c.label}", f"→{to.label}", 1)
            for c in dropped:
                pre = re.escape(self._gap_prefix(c.model_dump(mode="json")))
                reason, n = re.subn(rf"({pre}[^；]*?)→{re.escape(c.label)}", r"\1→未補（未經核准，不調動）", reason, count=1)
                if not n:
                    reason = reason.replace(f"→{c.label}", "→未補（未經核准，不調動）", 1)
            if new_c is not None and new_c.label not in reason:
                reason = f"{reason}；{new_c.shift_date:%m/%d} {new_c.shift_name}→{new_c.label}"
            sched = ver.schedule.model_copy(update={"staffing_changes": staffing})
            amend = getattr(store, "amend_proposed", None)
            if callable(amend):
                amend(ver.version, schedule=sched, reason=reason)
                target = ver.version
            else:                                          # store without amend: propose a corrected version
                target = store.propose(sched, reason, run.ev.id).version
                sr.step = sr.step.model_copy(update={"args": {**sr.step.args, "schedule_version": target}})
                for r in run.results.values():
                    if r.tool is ToolName.SOLVE_SCHEDULE and r.data.get("schedule_version") == ver.version:
                        r.data["schedule_version"] = target
            self.audit.append(AuditKind.SYSTEM, "state_machine",
                              {"schedule_amended": target, "from_version": ver.version,
                               "staffing": [c.model_dump(mode="json") for c in staffing],
                               "replaced": [c.model_dump(mode="json") for c in replaced],
                               "dropped_unapproved": [c.model_dump(mode="json") for c in dropped],
                               "approval_id": req.id, "decision_reason": req.decision.reason if req.decision else None},
                              event_id=run.ev.id)
            if new_c is not None and (replaced or new_c not in old):
                rep_txt = "、".join(c.label for c in replaced if key(c) == key(new_c))
                self._note(run.ev, "系統", f"排程 v{target} 人力依主管決定改為「{new_c.label}」"
                           + (f"（取代「{rep_txt}」）" if rep_txt else ""), "dynamic")
            if dropped:
                self._note(run.ev, "系統", f"排程 v{target} 不含未經核准的人力調動："
                           + "、".join(f"{self._gap_prefix(c.model_dump(mode='json'))} {c.label}" for c in dropped)
                           + "（未列入核准、未通知，該班缺口維持原狀）", "dynamic")

    def _revoke_steps(self, run: _Run, change: dict[str, Any], req: ApprovalRequest) -> list[StepRun]:
        """An earlier round's staffing for the same shift that the supervisor has now replaced is
        withdrawn with a compensating notice (R1 info), so only the decision actually taken stands."""
        key, out = _staff_key(change), []
        new_done = any(p["key"] == key and p["kind"] == change.get("kind") and p["label"] == change.get("label")
                       and not p["revoked"] for p in run.staffing_done)
        if not new_done:
            return out                                     # the new choice did not run: keep the old one
        for prev in run.staffing_done:
            if prev["revoked"] or prev["key"] != key or (prev["kind"], prev["label"]) == (change.get("kind"),
                                                                                      change.get("label")):
                continue
            n = len(run.revocations) + 1
            args = {"teams": list(prev["args"].get("teams") or []), "kind": NotifyKind.INFO.value,
                    "message": f"撤回：{prev['args'].get('message') or prev['label']}（主管改選{change.get('label')}）"}
            step = PlanStep(id=f"{req.id}-revoke-{n}", tool=ToolName.NOTIFY_TEAMS, args=args,
                            purpose=f"撤回{prev['label']}")
            gd = self.gate.check(step, EventState.EXECUTE, run.feasible)
            sr = StepRun(step=step, state="pending", gate=gd)
            run.ev.steps.append(sr)
            if gd.verdict is GateVerdict.REJECT:
                sr.state = "rejected"
                self._log_reject(run, step, gd)
                continue
            run.revocations[step.id] = prev
            self._note(run.ev, "系統", f"依主管改選「{change.get('label')}」→ "
                       f"撤回第 {prev.get('round', '?')} 輪已執行的「{prev['label']}」", "dynamic")
            out.append(sr)
        return out

    # ------------------------------------------------------------------ EXECUTE
    async def _execute(self, run: _Run, steps: list[StepRun], auto_tag: bool) -> None:
        """Run approved / auto writes. confirm_schedule goes first: when it fails (e.g. the proposal
        went stale because another event activated a schedule meanwhile) nothing else of this round
        runs, so a cure time, a staffing call or a notice never goes out against a schedule that
        did not take effect. Failures and skips land in run.round_problems (VERIFY counts them)."""
        ev = run.ev
        by_id = {x.step.id: x for x in ev.steps}
        ordered = sorted(steps, key=lambda x: 0 if x.step.tool is ToolName.CONFIRM_SCHEDULE else 1)
        halted: PlanStep | None = None
        for sr in ordered:
            step = sr.step
            if halted is not None:
                sr.state = "skipped"
                run.round_problems.append({"step_id": step.id, "tool": step.tool.value, "state": "skipped",
                                           "why": f"{halted.id} confirm_schedule failed"})
                continue
            if any(by_id[d].state != "ok" for d in step.depends_on
                   if d in by_id and by_id[d].step.tool in WRITE_TOOLS):
                sr.state = "skipped"
                run.round_problems.append({"step_id": step.id, "tool": step.tool.value, "state": "skipped",
                                           "why": "a write it depends on did not run"})
                continue
            cov = self._covering_versions(run, step)
            if cov is not None and not (cov & set(self._confirmed_this_round(run))) and not self._in_force_covers(step):
                # the schedule carrying this change did not go into force in this round: never put a
                # cure time / press-out in force against a schedule that does not have it
                sr.state = "skipped"
                run.round_problems.append({"step_id": step.id, "tool": step.tool.value, "state": "skipped",
                                           "why": "its schedule did not take effect in this round"})
                self._note(ev, "系統", f"{step.id} 未執行：對應的排程未在本輪生效", "dynamic")
                continue
            if step.id in run.auto_ids:
                # an auto (R1) write is checked again right before it runs, against what has actually
                # executed: a cure-time notice goes out only after its approved set_cure_time ran
                gd = self._check(run, step)
                if gd.verdict is not GateVerdict.ALLOW:
                    sr.gate, sr.state = gd, "rejected"
                    self._log_reject(run, step, gd)
                    run.round_problems.append({"step_id": step.id, "tool": step.tool.value, "state": "rejected",
                                               "why": "；".join(gd.reasons)[:300]})
                    continue
            sr.state = "running"
            res = await self._call(run, EventState.EXECUTE, step.id, step.tool, step.args)
            sr.result = res
            if not res.ok:
                sr.state = "failed"
                run.round_problems.append({"step_id": step.id, "tool": step.tool.value, "state": "failed",
                                           "error": (res.error or "")[:300]})
                self._note(ev, "工具", f"[工具] {step.id} {tool_label(step.tool)}未執行成功：{tool_error_text(step.tool, res.error)}",
                           "dynamic")
                if step.tool is ToolName.CONFIRM_SCHEDULE:
                    halted = step
                    rest = [x.step.id for x in ordered if x is not sr and x.state not in ("ok", "failed")]
                    approved = not auto_tag and run.current_req is not None
                    if approved and "is stale" in (res.error or ""):
                        run.stale_approval = {"req": run.current_req, "sig": run.approved_sig,
                                              "version": step.args.get("schedule_version")}
                    self._note(ev, "系統", ("✗ 已核准但未生效（排程已被另一事件更新）：" if approved else "")
                               + f"排程 v{step.args.get('schedule_version')} 未生效 → 本輪其餘動作不執行"
                               + (f"（{'、'.join(rest)}）" if rest else "") + "，依現行排程重新求解", "dynamic")
                continue
            sr.state = "ok"
            run.results[step.id] = res
            txt = result_text(step.tool, step.args, res.data)
            self._note(ev, "工具", ("✓ 已自動：" if auto_tag else "[工具] ") + txt, "tool")
            self._after_write(run, step)
            self._dispatch_tasks(run, step, res, auto_tag)

    @staticmethod
    def _confirmed_this_round(run: _Run) -> list[int]:
        """Versions a confirm_schedule of the current plan round activated."""
        out = []
        for res in run.results.values():
            if res.ok and res.tool is ToolName.CONFIRM_SCHEDULE and isinstance((res.data or {}).get("version"), int):
                out.append(int(res.data["version"]))
        return out

    def _action_miss(self, run: _Run, note: bool = True) -> None:
        ev = run.ev
        probs = run.round_problems
        self.audit.append(AuditKind.SYSTEM, "verifier",
                          {"hypothesis": ev.hypotheses[-1].id if ev.hypotheses else None, "ok": False,
                           "action_problems": probs}, event_id=ev.id)
        if not note:
            return
        n_fail = sum(1 for p in probs if p["state"] == "failed")
        n_rej = sum(1 for p in probs if p["state"] == "rejected")
        n_skip = sum(1 for p in probs if p["state"] == "skipped")
        bits = [f"{n_fail} 項動作執行失敗" if n_fail else "", f"{n_rej} 項動作被把關攔截" if n_rej else "",
                f"{n_skip} 項動作未執行" if n_skip else ""]
        self._note(ev, "驗證（程式）", "動作未完成：" + "；".join(b for b in bits if b) + " → 不算完成，重新規劃",
                   "dynamic")

    def _after_write(self, run: _Run, step: PlanStep) -> None:
        ev = run.ev
        if step.tool is ToolName.CONFIRM_SCHEDULE:
            v = step.args.get("schedule_version")
            for res in run.results.values():
                if res.tool is ToolName.SOLVE_SCHEDULE and res.data.get("schedule_version") == v:
                    run.done += moves_text(res.data.get("moves"))   # grouped per order; same-press shifts left out
            # the close summary lists what is in force at the end: a newer version supersedes the older one
            run.executed = [x for x in run.executed if not (x.startswith("排程 v") and x.endswith(" 生效"))]
            run.executed.append(f"排程 v{v} 生效")
            if isinstance(v, int):
                run.activated.append(v)
                self._activated_by[v] = ev.id
        elif step.tool is ToolName.SET_CURE_TIME:
            run.executed.append(f"{machine_label(str(step.args.get('press_id')))}硫化 {fmt_num(step.args.get('total_cure_h'))} h")
        elif step.tool is ToolName.NOTIFY_TEAMS:
            kind = step.args.get("kind")
            if kind == NotifyKind.MACHINE_OUT.value:
                mid = step.args.get("machine_id") or ev.machine_id
                self._set_status(mid, MachineStatus.OUT_OF_SERVICE, f"{machine_label(mid)}停用")
                # a cure time set earlier for this press no longer runs: the tire is held for QC
                run.executed = [x for x in run.executed if not x.startswith(f"{machine_label(mid)}硫化 ")]
                run.executed.append(f"{machine_label(mid)}停用")
            elif kind in STAFFING_KINDS:
                label = step.purpose.split(" ", 1)[-1]
                run.executed.append(label)
                change = run.step_changes.get(step.id)
                if change is not None:
                    run.staffing_done.append({"key": _staff_key(change), "kind": change.get("kind"),
                                              "label": change.get("label") or label, "args": dict(step.args),
                                              "step_id": step.id, "executed_label": label, "revoked": False,
                                              "round": run.approvals_made})
            elif step.id in run.revocations:
                prev = run.revocations[step.id]
                prev["revoked"] = True
                board = self._board()
                if board is not None and prev.get("step_id"):
                    gone = board.cancel(ev.id, prev["step_id"], self._now())   # issue #17: its tasks are void too
                    if gone:
                        self.audit.append(AuditKind.TASK, "state_machine", {"cancelled": gone, "revoked_step": prev["step_id"],
                                                                           "by_step": step.id}, event_id=ev.id)
                run.executed = [x for x in run.executed if x != prev["executed_label"]]
                run.executed.append(f"撤回{prev['label']}")
            elif kind == NotifyKind.MAINTENANCE.value:
                run.done.append("已通知維修")
                run.executed.append("已通知維修")
            elif kind in (NotifyKind.SLOW_FEED.value, NotifyKind.PAUSE_FEED.value):
                run.executed.append("放慢生料投入" if kind == NotifyKind.SLOW_FEED.value else "暫停投料")
            elif kind == NotifyKind.DISPATCH.value:
                run.executed.append("已派工")
            elif ev.source == "supervisor":
                # a typed task's notice (e.g. an info notice to maintenance): it ran, so say who was told
                from warroom.agent.narrate import TEAM_LABELS
                teams = "、".join(TEAM_LABELS.get(t, str(t)) for t in step.args.get("teams") or [])
                run.executed.append(f"已通知{teams or '相關組別'}")

    # ------------------------------------------------------------------ issue #17: approved steps -> worker tasks
    def _board(self) -> Any:
        """The plant's task board (World.tasks), when the sim has one."""
        return getattr(getattr(self.sim, "world", None), "tasks", None) if self.sim is not None else None

    def _dispatch_tasks(self, run: _Run, step: PlanStep, res: ToolResult, auto_tag: bool) -> None:
        """CODE turns an executed write into worker tasks (who, where, what, ETA) on the plant's task board, from
        the step's validated args and this event's own solve results. The model never writes a task. Audited."""
        board = self._board()
        if board is None:
            return
        from warroom.sim.work import TaskRequest
        ev = run.ev
        moves: list[dict[str, Any]] = []
        if step.tool is ToolName.CONFIRM_SCHEDULE:
            v = step.args.get("schedule_version")
            for _, d in self._solve_steps(run):
                if d.get("schedule_version") == v:
                    moves += list(d.get("moves") or [])
        pin = self._plant_inputs.get(ev.id)
        order_ids = [pin.order.id] if pin is not None and getattr(pin, "order", None) is not None else []
        req = TaskRequest(event_id=ev.id, approval_id=None if auto_tag or run.current_req is None else run.current_req.id,
                          step_id=step.id, event_kind=ev.anomaly.kind.value, tool=step.tool.value, args=dict(step.args),
                          result=dict(res.data or {}), moves=moves, staffing=run.step_changes.get(step.id),
                          order_ids=order_ids)
        try:
            tasks = board.dispatch(req, self._now())
        except Exception as e:  # noqa: BLE001 - task planning must never break the event
            self.audit.append(AuditKind.SYSTEM, "state_machine", {"task_dispatch_error": f"{type(e).__name__}: {e}"[:300],
                                                                 "step_id": step.id}, event_id=ev.id)
            return
        if not tasks:
            return
        self.audit.append(AuditKind.TASK, "state_machine",
                          {"created": [t.model_dump(mode="json", include={"id", "kind", "title", "team", "machine_id",
                                                                          "zone", "status", "worker_id", "eta",
                                                                          "order_ids", "after"}) for t in tasks],
                           "from_step": step.id, "tool": step.tool.value, "approval_id": req.approval_id,
                           "generated_by": "code"}, event_id=ev.id)
        if ev.source == "plant":
            self._note(ev, "系統", f"派工：{step.id} → {len(tasks)} 項人員任務（"
                       + "、".join(f"{t.kind}［{t.where.split('（')[0]}］" for t in tasks[:5])
                       + ("…" if len(tasks) > 5 else "") + "）", "human")

    async def _verify_tasks(self, run: _Run, h: Hypothesis) -> tuple[str, float | None]:
        """A plant event has no reading to test: VERIFY waits (sim time) until the people finished every task its
        approved steps became, or TASK_VERIFY_MAX_MIN passed, and reports how many are done."""
        ev = run.ev
        board = self._board()
        tasks = board.for_event(ev.id) if board is not None else []
        if not tasks:
            self.audit.append(AuditKind.SYSTEM, "verifier", {"hypothesis": h.id, "tasks": 0, "unmeasured": True},
                              event_id=ev.id)
            self._note(ev, "驗證（程式）", ("核准動作已執行完成" if run.executed else "沒有需要人員執行的動作")
                       + f"；{h.id} 無讀值可驗證", "info")
            return "unmeasured", None
        self._note(ev, "驗證（程式）", f"等待人員完成 {len(tasks)} 項任務（待派 → 前往中 → 執行中 → 完成）", "info")
        deadline = self._now() + timedelta(minutes=TASK_VERIFY_MAX_MIN)
        while True:
            tasks = board.for_event(ev.id)
            done = [t for t in tasks if t.status == "完成"]
            if len(done) == len(tasks) or self._now() >= deadline:
                break
            await self._sleep(self.verify_poll_s)
        ok = len(done) == len(tasks)
        last = max((t.done_at for t in done if t.done_at), default=None)
        self.audit.append(AuditKind.TASK, "verifier",
                          {"hypothesis": h.id, "tasks": len(tasks), "done": len(done), "ok": ok,
                           "open": [t.id for t in tasks if t.status != "完成"],
                           "last_done_at": last.isoformat() if last else None}, event_id=ev.id)
        if ok:
            self._note(ev, "驗證（程式）", f"人員任務 {len(done)}/{len(tasks)} 完成"
                       + (f"（最後一項 {last:%H:%M}）" if last else ""), "dynamic")
            return "tasks_done", None
        self._note(ev, "驗證（程式）", f"人員任務逾時：{len(done)}/{len(tasks)} 完成，其餘交主管追蹤", "dynamic")
        return "tasks_open", None

    # ------------------------------------------------------------------ VERIFY (code, no LLM)
    VERIFY_SMOOTH_S = 30.0     # verify compares a 30-second mean, so one noisy sample cannot flip the verdict

    def _read_value(self, machine_id: str, channel: Channel, since: datetime | None = None) -> float | None:
        """30-second mean of the machine's readings; `since` (fast repair): only readings from the fix on (the sim
        refreshes the reading of that second), None while there is none yet."""
        if self.sim is not None and hasattr(self.sim, "history"):
            try:
                hist = self.sim.history(machine_id, self.VERIFY_SMOOTH_S / 60.0)
                vals = [getattr(r, channel.value) for r in hist if getattr(r, channel.value, None) is not None
                        and (since is None or r.ts >= since)]
                if vals:
                    return round(sum(vals) / len(vals), 2)
                if since is not None:
                    return None
            except Exception:  # noqa: BLE001 - fall back to the latest reading
                pass
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

    async def _verify(self, run: _Run) -> tuple[str, float | None]:
        """Returns (verdict, last reading). verdict: "confirmed" (the reading met the code-bound
        expectation), "rejected" (it missed), "inconclusive" (it met it only because the safety core
        stopped / held the machine) or "contained" (no reading to test: the plan took the event's
        machine out of service, checked here by its status)."""
        ev, plan = run.ev, run.plan
        assert plan is not None
        h = ev.hypotheses[-1]
        exp = h.expectation
        if exp is None and ev.source == "plant":
            return await self._verify_tasks(run, h)
        if exp is None and ev.anomaly.kind.value in UNMEASURED_KINDS:
            self.audit.append(AuditKind.SYSTEM, "verifier", {"hypothesis": h.id, "expectation": None,
                                                             "unmeasured": True}, event_id=ev.id)
            if ev.source == "supervisor":
                self._note(ev, "驗證（程式）", ("主管指令已執行：" + "；".join(dict.fromkeys(run.executed))) if run.executed
                           else "主管指令沒有產生任何動作", "info")
            else:
                self._note(ev, "驗證（程式）", ("核准動作已執行完成" if run.executed else "排程無需變更")
                           + f"；{h.id} 無讀值可驗證", "info")
            return "unmeasured", None
        if exp is None and run.sop_handoff:
            self.audit.append(AuditKind.SYSTEM, "verifier", {"hypothesis": h.id, "expectation": None,
                                                             "handed_off": True}, event_id=ev.id)
            self._note(ev, "驗證（程式）", f"已依 SOP 交現場人工處置；{h.id} 未以讀值驗證", "dynamic")
            return "handed_off", None
        if exp is None:
            st = self._machine_state(ev.machine_id)
            held = st is not None and st.status in (MachineStatus.OUT_OF_SERVICE, MachineStatus.STOPPED)
            ok = held or self.sim is None
            self.audit.append(AuditKind.SYSTEM, "verifier",
                              {"hypothesis": h.id, "expectation": None, "contained": ok,
                               "machine_status": st.status.value if st else None}, event_id=ev.id)
            if ok:
                self._note(ev, "驗證（程式）", f"{machine_label(ev.machine_id)}已停用（狀態確認）；"
                           f"{h.id} 未以讀值驗證，交由維修確認", "dynamic")
                return "contained", None
            self._note(ev, "驗證（程式）", f"{machine_label(ev.machine_id)}未進入停用狀態 → {h.id} 未完成", "dynamic")
            return "rejected", None
        reported_at = await self._await_reports(run)
        start = reported_at or run.plan_at or self._now()
        deadline = start + timedelta(minutes=exp.within_min)
        violated_since: datetime | None = None
        last: float | None = None
        use_sim = self.sim is not None
        # Reads land on a fixed sim-clock grid (whole multiples of verify_poll_s), so the verdict time does
        # not depend on when the supervisor happened to approve (page 11: 02:31:00 = 02:14:00 + 17 min).
        await self._await_fix(run)
        wait = 0.0 if ev.repaired_at is not None else self._grid_wait()
        if wait > 0:
            await self._verify_wait(run, wait)
        while True:
            await self._await_fix(run)
            now = self._now()
            repaired = ev.repaired_at is not None
            back = run.recover_reading
            if back is not None and getattr(back, "machine_id", None) == exp.machine_id and \
                    getattr(back, exp.channel.value, None) is not None:
                v = getattr(back, exp.channel.value)     # fast repair: the reading the machine came back with
                run.recover_reading = None
            else:
                v = self._read_value(exp.machine_id, exp.channel, since=ev.recovered_at or ev.repaired_at) \
                    if use_sim else await self._read_value_via_tool(run, exp.machine_id, exp.channel)
            if v is not None:
                last = v
                if self._satisfied(exp, v):
                    violated_since = None
                    if repaired:
                        # fast repair: the crew's fix is in, the machine is back to normal and the reading meets
                        # the expectation: confirmed now, not at the within_min deadline
                        run.repair_reading = v
                        return self._verify_result(run, h, exp, True, v)
                else:
                    violated_since = violated_since or now
                    if (now - violated_since).total_seconds() >= self.verify_confirm_s:
                        return self._verify_result(run, h, exp, False, v)
            if now >= deadline:
                ok = violated_since is None and last is not None
                return self._verify_result(run, h, exp, ok, last)
            await self._verify_wait(run, self._grid_wait() or self.verify_poll_s)

    RECOVER_WAIT_S = 120.0     # wall seconds VERIFY waits for a repaired machine to be back (the ramp takes RECOVER_FAST_S)

    async def _await_fix(self, run: _Run) -> None:
        """Fast repair (runtime, Settings.repair_fast_s): while this event's crew is still repairing its machine, or the
        machine is on its way back to nominal after the fix (回溫中), VERIFY tests nothing. A reading before the fix
        would reject (or time out) the hypothesis the repair is about to fix — 12 wall s are 12 factory minutes at
        60x — and one on the way back would turn the light green before the machine is back. Both waits are on the
        WALL clock (on_repaired / on_recovered), never on the plant clock another event's model call may hold."""
        ev = run.ev
        while ev.repaired_at is None and self._repair_open(ev):
            await _wait_flag(run.repaired, 1.0)
        t0 = time.monotonic()
        while ev.repaired_at is not None and ev.recovered_at is None and time.monotonic() - t0 < self.RECOVER_WAIT_S:
            await _wait_flag(run.recovered, 1.0)

    def _repair_open(self, ev: Event) -> bool:
        """Fast repair: a repair task (檢修 / 停用檢修 / 放慢投料 / 暫停投料) of this event on its machine is not done yet
        (the runtime moves it on the wall clock and reports the fix with on_repaired)."""
        board = self._board()
        if board is None or not getattr(board, "fast_s", 0):
            return False
        from warroom.sim.work import REPAIR_KINDS
        try:
            return any(t.kind in REPAIR_KINDS and t.machine_id == ev.machine_id and t.status != "完成"
                       for t in board.for_event(ev.id))
        except Exception:  # noqa: BLE001 - a board without tasks: nothing to wait for
            return False

    async def _await_reports(self, run: _Run) -> datetime | None:
        """人員回報 mode (TaskBoard.report_mode): nobody's timer finishes a task, so VERIFY first waits until the people
        reported every task of this event done (or the supervisor completed it for them), at most TASK_VERIFY_MAX_MIN;
        then code tests the reading. Returns when the wait ended (the expectation's window starts there), None when
        there was nothing to wait for."""
        board = self._board()
        if board is None or not getattr(board, "report_mode", False):
            return None
        ev = run.ev
        open_ = [t for t in board.for_event(ev.id) if t.status != "完成"]
        if not open_:
            return None
        self._note(ev, "驗證（程式）", f"等待現場人員回報完成 {len(open_)} 項任務，再由程式確認讀值", "info")
        deadline = self._now() + timedelta(minutes=TASK_VERIFY_MAX_MIN)
        while any(t.status != "完成" for t in board.for_event(ev.id)) and self._now() < deadline:
            await self._sleep(1.0)
        left = [t.id for t in board.for_event(ev.id) if t.status != "完成"]
        self.audit.append(AuditKind.TASK, "verifier", {"waited_for_reports": [t.id for t in open_], "open": left,
                                                       "ok": not left}, event_id=ev.id)
        if left:
            self._note(ev, "驗證（程式）", f"人員回報逾時：{len(left)} 項未回報完成，交主管追蹤；改以目前讀值驗證",
                       "dynamic")
        return self._now()

    async def _verify_wait(self, run: _Run, seconds: float) -> None:
        """VERIFY's wait between reads (sim seconds). A repair reported meanwhile (on_repaired) cuts it short; after
        it, reads come every sim second until one taken after the fix is in."""
        if run.ev.repaired_at is not None:
            await self._sleep(min(seconds, 1.0))
            return
        sleeper = asyncio.ensure_future(self._sleep(seconds))
        woken = asyncio.ensure_future(run.repaired.wait())
        try:
            await asyncio.wait({sleeper, woken}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (sleeper, woken):
                if not t.done():
                    t.cancel()

    def _stopped_by_safety(self, ev: Event) -> bool:
        """The safety core stopped this machine or held its batch (a stopped mill cools by itself)."""
        from warroom.contracts import SafetyActionKind
        if any(a.action in (SafetyActionKind.STOP_MACHINE, SafetyActionKind.HOLD_BATCH) for a in ev.safety_actions):
            return True
        st = self._machine_state(ev.machine_id)
        return st is not None and st.status is MachineStatus.STOPPED

    def _grid_wait(self) -> float:
        """Sim seconds to the next whole multiple of verify_poll_s (0 when already on it)."""
        p = self.verify_poll_s
        if p <= 0:
            return 0.0
        rem = self._now().timestamp() % p
        return 0.0 if rem < 1e-6 or p - rem < 1e-6 else p - rem

    def _verify_result(self, run: _Run, h: Hypothesis, exp, ok: bool, v: float | None) -> tuple[str, float | None]:
        ev = run.ev
        unit = "°C" if exp.channel is Channel.TEMP else ""
        ch = CHANNEL_LABELS.get(exp.channel, "讀值")
        # met only because the safety core stopped the machine (a stopped mill cools whatever the
        # cause): that does not test the hypothesis
        inconclusive = ok and exp.op == "<=" and self._stopped_by_safety(ev)
        verdict = "inconclusive" if inconclusive else "confirmed" if ok else "rejected"
        self.audit.append(AuditKind.SYSTEM, "verifier",
                          {"hypothesis": h.id, "expectation": exp.model_dump(mode="json"), "observed": v, "ok": ok,
                           "verdict": verdict}, event_id=ev.id)
        if inconclusive:
            h.evidence.append(f"{ch} {fmt_num(v)}{unit}：安全核心已停機，無法驗證")
            self._note(ev, "驗證（程式）", f"比對預期：{ch} {fmt_num(v)}{unit}，但安全核心已停機 → {h.id} 無法驗證",
                       "dynamic")
        elif ok:
            h.evidence.append(f"{ch}維持 {fmt_num(v)}{unit}，符合預期 {expectation_text(exp)}")
            self._note(ev, "驗證（程式）", f"比對預期：{ch} {fmt_num(v)}{unit} → {h.id} 成立", "dynamic")
            said = self._task_reports.pop(ev.id, (None, False))[0]
            if said:                             # 人員回報: who said it was fixed, and what code checked
                self._note(ev, "驗證（程式）", f"{said} → 程式確認讀值回到 {fmt_num(v)}{unit}", "dynamic")
        else:
            h.evidence.append(f"{ch}續降至 {fmt_num(v)}{unit}" if exp.op == ">=" else f"{ch}達 {fmt_num(v)}{unit}")
            verb = "降到" if exp.op == ">=" else "升到"
            text = f"比對預期：{ch}{verb} {fmt_num(v)}{unit} → {h.id} 不成立"
            if ev.safety_actions or self._safety_acted(ev.machine_id):
                text += "；安全核心已依規則處置"
            self._note(ev, "驗證（程式）", text, "dynamic")
        return verdict, v

    # ------------------------------------------------------------------ terminal states
    def _close(self, run: _Run, verified: bool = True, why: str = "", clear_alarm: bool | None = None) -> None:
        """verified=False: the hypothesis was not tested by a reading (machine taken out, or the safety
        core stopped it). The event still closes, but the hypothesis is not CONFIRMED and the
        machine's alarm is not cleared."""
        ev = run.ev
        outcome = "；".join(dict.fromkeys(run.executed)) or "無需動作"
        h = ev.hypotheses[-1]
        head = f"{h.id} {h.statement}" + ("" if verified else f"（未驗證：{why}）")
        if ev.source == "plant":
            head = run.plan.goal if run.plan is not None else ev.anomaly.message
        if ev.source == "supervisor":
            head = f"主管指令「{ev.command}」"
            if not run.executed:
                outcome = "沒有可執行的動作" + (f"（{run.plan.rationale}）" if run.plan and run.plan.rationale else "")
        ev.outcome = f"{head}：{outcome}"
        if verified and ev.repaired_at is not None and run.repair_reading is not None:
            outcome = ev.outcome = self._repaired_outcome(ev, run.repair_reading)
        self._to(ev, EventState.CLOSED, ev.outcome)
        ev.closed_at = self._now()
        st = self._machine_state(ev.machine_id)
        if (verified if clear_alarm is None else clear_alarm) and st is not None and \
                st.status in (MachineStatus.ALARM, MachineStatus.WARNING):
            # from 回溫中 the drawer's banner goes back to the event's message (as from the red alarm)
            self._set_status(ev.machine_id, MachineStatus.RUNNING,
                             ev.anomaly.message if st.status is MachineStatus.WARNING else None)
        self._note(ev, "系統", f"事件結案：{outcome}" + ("" if verified else f"（{why}）"), "info")
        if ev.repaired_at is not None:
            self._report_ready(ev)

    def _repaired_outcome(self, ev: Event, v: float) -> str:
        """「已修復：A 線大開煉機 膠溫回到 100°C（維修完成 14:21、恢復 14:21）」"""
        exp = ev.hypotheses[-1].expectation if ev.hypotheses else None
        ch = exp.channel if exp is not None else Channel.TEMP
        mdef = MACHINE_BY_ID.get(ev.machine_id)
        label = "膠溫" if ch is Channel.TEMP and mdef is not None and mdef.handles_compound else \
            CHANNEL_LABELS.get(ch, "讀值")
        unit = "°C" if ch is Channel.TEMP else ""
        at = ev.repaired_at.astimezone(TZ) if ev.repaired_at is not None else self._now()
        back = f"、恢復 {ev.recovered_at.astimezone(TZ):%H:%M}" if ev.recovered_at is not None else ""
        return f"已修復：{machine_label(ev.machine_id)} {label}回到 {fmt_num(v)}{unit}（維修完成 {at:%H:%M}{back}）"

    def _safe_stop(self, run: _Run, reason: str) -> None:
        ev = run.ev
        if ev.state in TERMINAL_STATES:
            return
        kept = self._safe_stop_schedule(run, reason)
        for h in ev.hypotheses:
            if h.status is HypothesisStatus.ACTIVE:
                h.status = HypothesisStatus.PROPOSED
        ev.outcome = f"安全中止：{reason}；{kept}"
        ev.stop_reason, ev.stop_reason_text = reason, self.stop_reason_text(reason)
        self._to(ev, EventState.SAFE_STOP, reason)
        ev.closed_at = self._now()
        self._note(ev, "系統", ev.outcome, "dynamic")


    def stop_reason_text(self, reason: str) -> str:
        """The SAFE_STOP reason in the supervisor's words (shown on the card next to the resolution buttons)."""
        if reason == "核准逾時":
            s = self.approval_timeout_s
            return f"{s / 60:.0f} 分鐘內沒核准" if s >= 60 and s % 60 == 0 else f"{s:.0f} 秒內沒核准"
        m = re.match(r"連續 (\d+) 次失敗", reason)
        if m:
            return f"連續失敗 {m.group(1)} 次"
        m = re.match(r"已重新規劃 (\d+) 次仍未解決", reason)
        if m:
            return f"重新規劃 {m.group(1)} 次仍無法驗證"
        if reason == "指揮官要求安全中止":
            return "AI 判斷沒有安全的處置方案"
        if reason.startswith("指揮官") or "模型" in reason or "LLM" in reason:
            return "AI 模型沒有回應"
        if reason.startswith("內部錯誤"):
            return "系統內部錯誤"
        if reason == "事件處理被取消":
            return "處理被取消（切換情境或重新啟動）"
        return reason

    # ------------------------------------------------------------------ SAFE_STOP → 請 AI 重新規劃
    def open_replan(self, event_id: str, actor: str = "supervisor") -> Event:
        """「請 AI 重新規劃」 of an event that ended in SAFE_STOP: a NEW event for the same machine, supervisor command or
        plant input, opened here (DETECTED) and run by the caller (run_event / run_command / run_plant) through the
        normal state machine, gate and approval. Its `previous` carries the stopped event and why it stopped, and the
        commander gets it as context.previous_run. Nothing here touches a machine's status beyond what any new event
        does (_set_status never lifts STOPPED / OUT_OF_SERVICE). KeyError: no such event; ValueError: not safe-stopped,
        or the machine has another open event, or a plant input that is no longer kept."""
        old = self._events[event_id]
        if old.state is not EventState.SAFE_STOP:
            raise ValueError(f"{event_id} 不是安全停止的事件")
        prev = PreviousRun(event_id=old.id, reason=old.stop_reason or "",
                           reason_text=old.stop_reason_text or self.stop_reason_text(old.stop_reason or ""))
        if old.source == "supervisor":
            ev = self.open_command(old.command or old.anomaly.message, actor)
        elif old.source == "plant":
            pin = self._plant_inputs.get(old.id)
            if pin is None:
                raise ValueError("這件工廠事件的原始輸入已不在記憶體，請改用「交給現場處理」")
            ev = self.open_plant_event(pin)
        else:
            if self.open_event_for(old.machine_id) is not None:
                raise ValueError(f"{machine_label(old.machine_id)}已有處理中的事件")
            now = self._now()
            a = old.anomaly.model_copy(update={"id": f"{old.anomaly.id}-R{now.astimezone(TZ):%H%M%S}", "ts": now})
            ev = self._open_event(a)
        ev.previous = prev
        self.audit.append(AuditKind.SYSTEM, actor, {"msg": "請 AI 重新規劃", "previous_event_id": old.id,
                                                    "stop_reason": prev.reason, "stop_reason_text": prev.reason_text},
                          event_id=ev.id)
        self._note(ev, "主管", f"[人工] 主管要求 AI 重新規劃（上一輪 {old.id} 安全停止：{prev.reason_text}）", "human")
        return ev

    async def run_event(self, event_id: str) -> Event:
        """Run a detector event opened outside handle() (open_replan)."""
        return await self._drive(self._events[event_id], self._run)

    def _safe_stop_schedule(self, run: _Run, reason: str) -> str:
        """SAFE_STOP never rolls the store back. Every version this event activated was approved by
        the supervisor, and the store is shared: rolling back would also undo other events' approved
        versions (concurrent anomalies) or put a job back on a press the safety core is holding
        (page 11: #A-311 back on press 7). What safe stop discards is this event's own unapproved
        proposals, which are never confirmed; the schedule in force stays."""
        if self.store is None:
            return "維持原排程"
        try:
            cur = self.store.current().version
        except Exception as e:  # noqa: BLE001
            self.audit.append(AuditKind.SYSTEM, "state_machine", {"store_error": str(e)[:300]}, event_id=run.ev.id)
            return "維持原排程"
        discarded: list[int] = []
        try:
            discarded = [v.version for v in self.store.versions()
                         if v.status is ScheduleStatus.PROPOSED and v.event_id == run.ev.id]
        except Exception:  # noqa: BLE001 - a store without versions(): nothing to list
            pass
        held = self._jobs_on_held_presses(cur)
        if held:
            self._note(run.ev, "系統", "注意：現行排程 v{} 仍有 {} 模排在停機／停用的 {}，入模前需人工確認".format(
                cur, sum(held.values()), "、".join(machine_label(p) for p in held)), "dynamic")
        self.audit.append(AuditKind.SYSTEM, "state_machine",
                          {"safe_stop_schedule": "kept", "active_version": cur, "future_jobs_on_held_presses": held,
                           "version_at_open": run.prev_schedule_version, "approved_by_this_event": run.activated,
                           "discarded_proposals": discarded, "reason": reason}, event_id=run.ev.id)
        if run.activated and cur in run.activated:
            return f"維持主管已核准的排程 v{cur}（不回復）"
        if cur != run.prev_schedule_version and run.prev_schedule_version is not None:
            return f"維持現行排程 v{cur}（其他事件已核准）"
        return "維持原排程"


    def _jobs_on_held_presses(self, version: int) -> dict[str, int]:
        """{press: future mold-ins} in the active schedule on presses that are STOPPED / OUT_OF_SERVICE."""
        out: dict[str, int] = {}
        try:
            jobs = self.store.get(version).schedule.jobs
        except Exception:  # noqa: BLE001
            return out
        now = self._now()
        for j in jobs:
            st = self._machine_state(j.press_id)
            if st is not None and st.status in (MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE) \
                    and j.mold_in > now:
                out[j.press_id] = out.get(j.press_id, 0) + 1
        return out


async def _wait_flag(flag: asyncio.Event, timeout_s: float) -> None:
    """Wait (wall clock) until `flag` is set, at most timeout_s."""
    try:
        await asyncio.wait_for(flag.wait(), timeout_s)
    except TimeoutError:
        pass


def _staff_key(c: dict[str, Any]) -> tuple[str, str, str]:
    """Which staffing gap a change fills: (shift date, shift name, receiving team)."""
    return (str(c.get("shift_date") or ""), str(c.get("shift_name") or ""), str(c.get("to_team") or ""))


def _press_no(machine_id: str) -> int | None:
    from warroom.agent.narrate import press_no
    return press_no(machine_id)
