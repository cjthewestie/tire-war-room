"""主 agent (orchestrator), issue #54. CJ, 2026-10-20: 「我希望主控台的 agent 叫做主 agent，是 orchestrator，負責針對我計劃區
的內容派發子 agent 完成任務；正在進行中的子 agent 會在 agent 頁面顯示進度，隨時也可以討論跟中斷，然後有結果會回報給主控台 agent。」

The main agent is CODE, not an LLM loop. It does routing, priority and escalation; each subagent is one existing
state-machine run (an Event) with its own gate and approvals, so the safety core is unchanged:

- Routing. A detector anomaly or a plant event is routed by its kind (ROUTES). A supervisor's free-text task is the
  one place a model chooses: Nemotron picks ONE dispatch from a fixed list (DISPATCHES) as a JSON schema; the rule
  fallback picks by keywords. A dispatch is R0: it only starts a run. It is not one of the 9 tools (ToolName is
  unchanged) and every write inside the run still goes through that run's gate and approval card.
- Priority. Every model call waits in one ModelQueue: safety > machine faults > orders / materials > staffing >
  questions. Each call's wait is audited (actor main_agent, {"orch": "queue"}).
- Report back. When a run ends, the main agent writes a short code-built summary (outcome, what changed, approvals)
  as its own message ({"orch": "report"}), shown in the 主控台 conversation.
- Interrupt. POST /api/subagents/{id}/interrupt stops a run safely: the task is cancelled (no new tool call), its
  waiting approval cards are superseded (an unapproved write never runs), the workers' open tasks are cancelled, the
  schedule in force is kept (SAFE_STOP never rolls back), and the event ends in SAFE_STOP 「主管中斷」. Audited.

All audit rows are kind=system, actor="main_agent", payload {"orch": dispatch | report | interrupt | queue, ...}."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from warroom.contracts import (
    TERMINAL_STATES,
    WRITE_TOOLS,
    AnomalyKind,
    AuditKind,
    Event,
    EventState,
    Severity,
    WsType,
)
from warroom.factory import TZ

ACTOR = "main_agent"
_WRITE_LABEL = {"confirm_schedule": "確認排程", "set_cure_time": "設定硫化時間", "notify_teams": "通知"}
_DECISION = {"approve": "核准", "reject": "駁回", "modify": "修改", "superseded": "作廢", "expired": "逾時"}

# subagent types (Chinese labels for the UI) and their model-queue priority (lower = sooner)
TYPES: dict[str, str] = {"machine": "機台處理", "order": "訂單處理", "material": "原料處理", "staff": "人力調度",
                         "shift": "早班開工", "task": "主管任務"}
PRIORITY: dict[str, int] = {"safety": 0, "machine": 1, "order": 2, "material": 2, "staff": 3, "shift": 3, "task": 2,
                            "question": 4}
PRIORITY_LABEL = {0: "安全", 1: "機台故障", 2: "訂單／原料", 3: "人力", 4: "提問"}

# code routing: anomaly kind -> subagent type (machine faults, orders, staffing, the shift start)
ROUTES: dict[str, str] = {
    AnomalyKind.OVERHEAT.value: "machine", AnomalyKind.TEMP_DROP.value: "machine",
    AnomalyKind.PRESSURE_LOW.value: "machine", AnomalyKind.WATER_FLOW_LOW.value: "machine",
    AnomalyKind.STEAM_LOW.value: "machine", AnomalyKind.SENSOR_FAULT.value: "machine",
    AnomalyKind.MULTI_MACHINE.value: "machine", AnomalyKind.SCHEDULE_SLIP.value: "order",
    AnomalyKind.ORDER_RUSH.value: "order", AnomalyKind.STAFFING_SHORT.value: "staff",
    AnomalyKind.SHIFT_START.value: "shift",
    "material_short": "material", "material_low": "material",   # #38 material patrol (may not be merged)
}

# the fixed dispatch list the model may choose from for a supervisor task (orchestrator level, not tools)
DISPATCHES: dict[str, str] = {"dispatch_machine_task": "machine", "dispatch_order_task": "order",
                              "dispatch_material_task": "material", "dispatch_staff_task": "staff"}
DISPATCH_SYSTEM = """你是實心胎工廠戰情室的「主 agent」。主管交辦一件事，你只決定要派給哪一個子 agent，不做事、不回答。
只輸出一個 JSON 物件 {"dispatch": ..., "reason": ...}：
- dispatch_machine_task：機台、維修、保養、溫度、壓力、蒸汽、停機相關
- dispatch_order_task：訂單、交期、急單、插單、重排、出貨相關
- dispatch_material_task：原料、缺料、補料、進貨、庫存、採購相關
- dispatch_staff_task：人力、排班、加班、調人、請假、缺人相關
reason 用繁體中文一句話，20 字內。"""
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("material", re.compile(r"原料|缺料|補料|進貨|庫存|採購|橡膠|碳黑|鋼絲|尼龍布|硫化劑")),
    ("staff", re.compile(r"人力|排班|加班|調人|請假|缺人|病假|班表|夜班缺|人手")),
    ("order", re.compile(r"訂單|交期|急單|插單|重排|出貨|#\w-\d+|晚交|客戶")),
    ("machine", re.compile(r"號機|機台|維修|保養|溫度|壓力|蒸汽|停機|檢查|冷卻|水流|煉機|硫化機")),
]


def route(ev_or_kind: Event | str, severity: Severity | str | None = None) -> tuple[str, int]:
    """(subagent type, model priority) for a code-routed run. A critical machine anomaly is the safety tier."""
    if isinstance(ev_or_kind, Event):
        kind, severity = ev_or_kind.anomaly.kind.value, ev_or_kind.anomaly.severity
    else:
        kind = ev_or_kind
    t = ROUTES.get(str(kind), "machine")
    p = PRIORITY[t]
    if t == "machine" and str(severity) == Severity.CRITICAL.value:
        p = PRIORITY["safety"]
    return t, p


def rule_dispatch(text: str) -> tuple[str, str]:
    """Keyword fallback for a supervisor task: (dispatch name, reason)."""
    for t, rx in _RULES:
        m = rx.search(text)
        if m:
            name = next(k for k, v in DISPATCHES.items() if v == t)
            return name, f"關鍵字「{m.group(0)}」"
    return "dispatch_machine_task", "沒有關鍵字，預設給機台處理"


def dispatch_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["dispatch", "reason"],
            "properties": {"dispatch": {"type": "string", "enum": list(DISPATCHES)},
                           "reason": {"type": "string", "maxLength": 60}}}


# ---------------------------------------------------------------------------------- model queue
@dataclass(order=True)
class _Ticket:
    priority: int
    seq: int
    who: str = field(compare=False)
    event_id: str | None = field(compare=False, default=None)
    label: str = field(compare=False, default="")
    since: float = field(compare=False, default=0.0)
    since_iso: str = field(compare=False, default="")


class ModelQueue:
    """One local model: calls take a slot in priority order (then arrival). Waits are audited."""

    def __init__(self, slots: int = 1, audit: Any = None, clock: Any = None,
                 on_change: Callable[[], None] | None = None):
        self.slots = slots
        self.audit, self.clock = audit, clock
        self.on_change = on_change
        self._seq = itertools.count(1)
        self._waiting: list[_Ticket] = []
        self._running: list[_Ticket] = []
        self._cond: asyncio.Condition | None = None
        self.tags: dict[str, tuple[str, int]] = {}      # event id -> (subagent type, priority)

    def _now_iso(self) -> str:
        try:
            return self.clock.now().isoformat()
        except Exception:  # noqa: BLE001 - a clockless queue still works
            return datetime.now(TZ).isoformat()

    def priority_of(self, event_id: str | None, default: int = 2) -> int:
        return self.tags.get(event_id or "", ("", default))[1]

    @contextlib.asynccontextmanager
    async def slot(self, priority: int, who: str, event_id: str | None = None, label: str = ""):
        if self._cond is None:
            self._cond = asyncio.Condition()
        t = _Ticket(priority, next(self._seq), who, event_id, label, time.perf_counter(), self._now_iso())
        ahead = len(self._waiting) + len(self._running)
        async with self._cond:
            self._waiting.append(t)
            self._waiting.sort()
            self._changed()
            try:
                await self._cond.wait_for(lambda: len(self._running) < self.slots and self._waiting[0] is t)
            except BaseException:
                self._waiting.remove(t)
                self._cond.notify_all()
                self._changed()
                raise
            self._waiting.remove(t)
            self._running.append(t)
            self._changed()
        waited = (time.perf_counter() - t.since) * 1000
        if self.audit is not None:
            self.audit.append(AuditKind.SYSTEM, ACTOR, {"orch": "queue", "who": who, "priority": priority,
                                                        "priority_label": PRIORITY_LABEL.get(priority, str(priority)),
                                                        "waited_ms": round(waited, 1), "ahead": ahead, "label": label},
                              event_id=event_id)
        try:
            yield waited
        finally:
            async with self._cond:
                self._running.remove(t)
                self._cond.notify_all()
            self._changed()

    def _changed(self) -> None:
        if self.on_change:
            try:
                self.on_change()
            except Exception:  # noqa: BLE001, S110 - a UI push must never break a model call
                pass

    def view(self) -> dict[str, Any]:
        def v(t: _Ticket, state: str) -> dict[str, Any]:
            return {"who": t.who, "event_id": t.event_id, "label": t.label, "priority": t.priority,
                    "priority_label": PRIORITY_LABEL.get(t.priority, str(t.priority)), "since": t.since_iso, "state": state}
        return {"slots": self.slots, "running": [v(t, "running") for t in self._running],
                "waiting": [v(t, "waiting") for t in sorted(self._waiting)]}


class QueuedCommander:
    """Wraps the commander so every plan / replan call takes its event's slot in the ModelQueue.
    Same plan() signature (on_alert streaming kept); everything else is forwarded unchanged."""

    def __init__(self, inner: Any, queue: ModelQueue):
        self._inner, self._queue = inner, queue

    async def plan(self, req, on_alert=None):
        t, p = self._queue.tags.get(req.event_id) or route(req.anomaly.kind.value, req.anomaly.severity)
        async with self._queue.slot(p, "commander", req.event_id, TYPES.get(t, t)):
            if on_alert is None:
                return await self._inner.plan(req)
            return await self._inner.plan(req, on_alert=on_alert)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


# ---------------------------------------------------------------------------------- dispatcher (model + rules)
class Dispatcher:
    """Chooses ONE dispatch for a supervisor task: the model (JSON schema) in LLM mode, keywords otherwise or on
    any model failure."""

    def __init__(self, settings: Any = None, client: Any = None):
        self.settings = settings
        self.client = client
        self.server_kind = "openai"
        self.llm = settings is not None and str(getattr(settings, "llm_mode", "mock")) in ("llm", "LLMMode.LLM")
        if self.llm and client is None:
            from openai import AsyncOpenAI

            from warroom.agent.commander import detect_server_kind
            self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key_effective,
                                      timeout=min(20.0, settings.llm_timeout_s), max_retries=0)
            self.server_kind = detect_server_kind(settings.base_url)

    async def choose(self, text: str) -> dict[str, Any]:
        t0 = time.perf_counter()
        if self.llm and self.client is not None:
            from warroom.agent import plant
            from warroom.agent.commander import thinking_off
            extra = thinking_off(self.server_kind)
            try:
                r = await self.client.chat.completions.create(
                    model=self.settings.model, temperature=0.0, max_tokens=120,
                    messages=[{"role": "system", "content": DISPATCH_SYSTEM + plant.section()},
                              {"role": "user", "content": json.dumps({"task": text}, ensure_ascii=False)}],
                    response_format={"type": "json_schema", "json_schema": {"name": "Dispatch", "schema": dispatch_schema()}},
                    **({"extra_body": extra} if extra else {}))
                raw = (r.choices[0].message.content or "").strip()
                d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw))
                if d.get("dispatch") not in DISPATCHES:
                    raise ValueError(f"dispatch {d.get('dispatch')!r} is not on the list")
                return {"dispatch": d["dispatch"], "type": DISPATCHES[d["dispatch"]], "reason": str(d.get("reason", ""))[:60],
                        "by": "model", "fell_back": False, "error": None, "ms": round((time.perf_counter() - t0) * 1000, 1),
                        "model": self.settings.model}
            except Exception as e:  # noqa: BLE001 - keyword fallback, reason recorded
                name, why = rule_dispatch(text)
                return {"dispatch": name, "type": DISPATCHES[name], "reason": why, "by": "rules", "fell_back": True,
                        "error": f"{type(e).__name__}: {e}"[:200], "ms": round((time.perf_counter() - t0) * 1000, 1),
                        "model": self.settings.model}
        name, why = rule_dispatch(text)
        return {"dispatch": name, "type": DISPATCHES[name], "reason": why, "by": "rules", "fell_back": False,
                "error": None, "ms": round((time.perf_counter() - t0) * 1000, 1), "model": None}


# ---------------------------------------------------------------------------------- the main agent
@dataclass
class SubAgent:
    event_id: str
    type: str
    priority: int
    via: str                     # code | model | rules
    reason: str
    dispatched_at: str
    interrupted: bool = False
    interrupt_by: str | None = None
    reported: bool = False


class Orchestrator:
    """The main agent over one StateMachine. `tasks()` returns the runtime's asyncio tasks by key (anomaly id or
    event id); `board()` the plant's worker task board (or None)."""

    def __init__(self, sm: Any, audit: Any, *, approvals: Any = None, bus: Any = None, clock: Any = None,
                 queue: ModelQueue | None = None, tasks: Callable[[], dict[str, asyncio.Task]] | None = None,
                 board: Callable[[], Any] | None = None, dispatcher: Dispatcher | None = None):
        self.sm, self.audit, self.approvals, self.bus, self.clock = sm, audit, approvals, bus, clock
        self.queue = queue or ModelQueue(audit=audit, clock=clock)
        self._tasks = tasks or dict
        self._board = board or (lambda: None)
        self.dispatcher = dispatcher or Dispatcher()
        self.subs: dict[str, SubAgent] = {}
        self._pending_dispatch: list[dict[str, Any]] = []      # supervisor dispatch decided, event not open yet
        if hasattr(sm, "on_open"):
            sm.on_open = self._opened
        if hasattr(sm, "on_end"):
            sm.on_end = self._ended

    # ---------------------------------------------------------------- helpers
    def _now(self) -> datetime:
        return self.clock.now() if self.clock is not None else datetime.now(TZ)

    def publish(self) -> None:
        if self.bus is not None:
            try:
                self.bus.publish(WsType.SUBAGENTS, {"items": self.subagents(), "queue": self.queue.view()})
            except Exception:  # noqa: BLE001, S110 - a UI push is best effort
                pass

    def _audit(self, payload: dict[str, Any], event_id: str | None) -> None:
        self.audit.append(AuditKind.SYSTEM, ACTOR, payload, event_id=event_id)

    # ---------------------------------------------------------------- dispatch
    def _opened(self, ev: Event) -> None:
        """StateMachine hook: a run starts. Code-routed runs are dispatched here; a supervisor task carries the
        decision made before it opened."""
        if ev.id in self.subs:
            return
        if ev.source == "supervisor" and self._pending_dispatch:
            d = self._pending_dispatch.pop(0)
            t, p, via, why = d["type"], PRIORITY[d["type"]], d["by"], d["reason"]
            extra = {"dispatch": d["dispatch"], "task": ev.command, "model": d.get("model"),
                     "fell_back": d.get("fell_back"), "error": d.get("error"), "ms": d.get("ms")}
        else:
            t, p = route(ev)
            via, why = "code", f"依偵測種類 {ev.anomaly.kind.value} 分派"
            extra = {"kind": ev.anomaly.kind.value, "severity": ev.anomaly.severity.value}
        sub = SubAgent(ev.id, t, p, via, why, self._now().isoformat())
        self.subs[ev.id] = sub
        self.queue.tags[ev.id] = (t, p)
        self._audit({"orch": "dispatch", "phase": "plan", "type": t, "type_label": TYPES[t], "priority": p, "by": via,
                     "reason": why, "machine_id": ev.machine_id,
                     # plain event words, no dispatch (CJ 2026-10-21: 「現在先不要派發，主要就是一個 agent 做 tool calling」)
                     "text": f"{self._what(ev).strip('「」')}：開始處理，計劃擬好會送你核准", **extra}, ev.id)
        self.publish()

    async def dispatch_command(self, text: str, actor: str, start: Callable[[str, str], Event]) -> tuple[Event, dict[str, Any]]:
        """A supervisor's free-text task: pick ONE dispatch (model in its queue slot, or rules), then start the run
        (`start` = Runtime.start_command). The dispatch is R0: it only starts a run."""
        async with self.queue.slot(PRIORITY["task"], "dispatch", None, "Agent Nemo 派發"):
            d = await self.dispatcher.choose(text)
        self._pending_dispatch.append(d)
        ev = start(text, actor)
        if ev.id not in self.subs:          # a state machine without the on_open hook
            self._opened(ev)
        return ev, d

    def start_task(self, text: str, actor: str, start: Callable[[str, str], Event], decision: dict[str, Any]) -> Event:
        """The 主 agent's conversation already chose the subagent type (its dispatch tool): start that run."""
        self._pending_dispatch.append(decision)
        ev = start(text, actor)
        if ev.id not in self.subs:
            self._opened(ev)
        return ev

    @staticmethod
    def _what(ev: Event) -> str:
        if ev.source == "supervisor":
            return f"「{(ev.command or '')[:24]}」"
        from warroom.agent.narrate import machine_label
        try:
            return f"{machine_label(ev.machine_id)}・{ev.anomaly.message[:24]}" if ev.source == "detector" else ev.anomaly.message[:28]
        except Exception:  # noqa: BLE001
            return ev.anomaly.message[:28]

    @staticmethod
    def _name(ev: Event | None) -> str:
        """What a line is about, so the 主控台 says which one (「已核准 10 號機，…」, not 「已核准，機台處理 agent…」)."""
        if ev is None:
            return ""
        if ev.source == "supervisor":
            cmd = (ev.command or "").strip()
            return f"主管指令「{cmd[:14]}{'…' if len(cmd) > 14 else ''}」"
        from warroom.agent.narrate import machine_label
        try:
            return machine_label(ev.machine_id) if ev.source == "detector" else ev.anomaly.message[:16]
        except Exception:  # noqa: BLE001
            return str(ev.machine_id or "")

    def _event(self, event_id: str) -> Event | None:
        try:
            return self.sm.get(event_id)
        except Exception:  # noqa: BLE001 - a state machine without the event: the line names no machine
            return None

    def on_decided(self, req: Any) -> None:
        """The supervisor approved a plan (核准並派發): the main agent dispatches its subagent to execute it — the
        run continues with the writes this approval covers, each still through its gate. A rejection is noted."""
        ev_id = getattr(req, "event_id", None)
        d = getattr(req, "decision", None)
        if not ev_id or d is None:
            return
        sub = self.subs.get(ev_id)
        t = sub.type if sub else "machine"
        kind = d.decision.value
        name = self._name(self._event(ev_id))
        gap = " " if name[:1].isascii() and name[:1].isalnum() else ""
        # plain event words (「已核准 7 號機方案 A，執行中」), never a dispatch to a typed agent (CJ 2026-10-21: 「好改」)
        if kind in ("approve", "modify"):
            text = f"已核准{gap}{name}{'方案 ' + d.option_id if d.option_id else '的計劃'}，執行中"
        elif kind == "reject":
            text = f"已駁回{gap}{name}{'的' if name else ''}計劃，不執行"
        else:
            return
        self._audit({"orch": "dispatch", "phase": "execute" if kind != "reject" else "rejected", "type": t,
                     "type_label": TYPES.get(t, t), "by": "supervisor", "decision": kind, "option": d.option_id,
                     "approval_id": req.id, "text": text}, ev_id)
        self.publish()

    # ---------------------------------------------------------------- report back
    def _ended(self, ev: Event) -> None:
        """StateMachine hook: a run ended. The main agent reports a code-built summary once."""
        sub = self.subs.get(ev.id)
        if sub is None:
            self._opened(ev)
            sub = self.subs[ev.id]
        if sub.reported or ev.state not in TERMINAL_STATES:
            return
        sub.reported = True
        text, facts = self.summary(ev, sub)
        self._audit({"orch": "report", "type": sub.type, "type_label": TYPES[sub.type], "state": ev.state.value,
                     "text": text, **facts}, ev.id)
        self.publish()

    def summary(self, ev: Event, sub: SubAgent) -> tuple[str, dict[str, Any]]:
        writes = []
        for sr in [*(s for r in ev.prior_rounds for s in r.steps), *ev.steps]:
            tool = str(getattr(sr.step.tool, "value", sr.step.tool))
            if tool in {t.value for t in WRITE_TOOLS} and str(getattr(sr.state, "value", sr.state)) == "ok":
                sid = sr.step.id
                if sid.startswith("AP-"):                      # an approval-option step: 「核准 B」
                    opt = sid.rsplit("-", 1)[-1]
                    sid = f"方案 {opt}" if len(opt) == 1 else "核准步驟"
                writes.append(f"{_WRITE_LABEL.get(tool, tool)}（{sid}）")
        decided = []
        if self.approvals is not None:
            for rid in ev.approvals:
                try:
                    r = self.approvals.get(rid)
                except Exception:  # noqa: BLE001, S112 - an unknown card id: nothing to report
                    continue
                if r.decision is not None:
                    decided.append(f"{_DECISION.get(r.decision.decision.value, r.decision.decision.value)}"
                                   f"{'方案 ' + r.decision.option_id if r.decision.option_id else ''}")
        repaired = ev.source == "detector" and ev.state is EventState.CLOSED and not sub.interrupted
        head = "已中斷" if sub.interrupted else "已修復" if repaired else "完成" if ev.state is EventState.CLOSED else "已停止"
        outcome = re.sub(r"^(安全中止|已中斷)[：:]\s*", "", (ev.outcome or "").strip())   # never 「安全中止：安全中止：…」
        name = self._name(ev)
        if ev.source == "supervisor" and (ev.command or "").strip()[:10] in outcome:
            name = ""                                       # the outcome already quotes the command
        done_at = f"（{ev.closed_at.astimezone(TZ):%H:%M} 結案）" if ev.closed_at and ev.state is EventState.CLOSED else ""
        parts = [f"{name or '任務'}{head}：{outcome or '無需動作'}{done_at}"]   # 「7 號機已修復：溫度回到 128°C（02:14 結案）」
        parts.append(f"改了：{'、'.join(writes)}" if writes else "沒有寫入任何變更")
        parts.append(f"核准：{'、'.join(decided)}" if decided else "沒有核准")
        return "。".join(parts), {"writes": writes, "approvals": decided, "outcome": ev.outcome, "rounds": len(ev.prior_rounds) + 1,
                                  "interrupted": sub.interrupted}

    # ---------------------------------------------------------------- interrupt
    def _task_for(self, ev: Event) -> asyncio.Task | None:
        tasks = self._tasks()
        return tasks.get(ev.id) or tasks.get(ev.anomaly.id)

    async def interrupt(self, event_id: str, by: str, reason: str = "主管中斷") -> dict[str, Any]:
        ev = self.sm.get(event_id)          # KeyError -> 404
        if ev.state in TERMINAL_STATES:
            raise ValueError(f"{event_id} 已經結束（{ev.state.value}）")
        sub = self.subs.get(event_id)
        if sub is None:
            self._opened(ev)
            sub = self.subs[event_id]
        sub.interrupted, sub.interrupt_by = True, by
        mark = getattr(self.sm, "mark_interrupt", None)
        if callable(mark):
            mark(event_id, reason)
        task = self._task_for(ev)
        cancelled_run = False
        if task is not None and not task.done():
            task.cancel()                                   # no new tool call; _drive safe-stops with `reason`
            with contextlib.suppress(Exception):
                await asyncio.wait([task], timeout=5.0)
            cancelled_run = True
        superseded = []
        if self.approvals is not None:
            for r in list(self.approvals.pending()):
                if r.event_id == event_id:
                    with contextlib.suppress(Exception):
                        self.approvals.supersede(r.id, by=by, reason=reason)
                        superseded.append(r.id)
        cancelled_tasks: list[str] = []
        board = self._board()
        if board is not None:
            steps = {t.step_id for t in board.for_event(event_id) if t.status != "完成"}
            for sid in steps:
                cancelled_tasks += board.cancel(event_id, sid, self._now())
        if ev.state not in TERMINAL_STATES:                 # no task (or it did not stop): end it here
            stop = getattr(self.sm, "stop_run", None)
            if callable(stop):
                stop(event_id, reason)
        kept = None
        store = getattr(self.sm, "store", None)
        with contextlib.suppress(Exception):
            kept = store.current().version if store is not None else None
        detail = {"orch": "interrupt", "by": by, "reason": reason, "cancelled_run": cancelled_run,
                  "superseded_approvals": superseded, "cancelled_tasks": cancelled_tasks, "schedule_kept": kept,
                  "state": ev.state.value, "type": sub.type,
                  "text": f"主管中斷{self._name(ev)}：停止新的工具呼叫"
                          f"{f'，作廢核准卡 {len(superseded)} 張' if superseded else ''}"
                          f"{f'，取消人員任務 {len(cancelled_tasks)} 件' if cancelled_tasks else ''}"
                          f"，排程維持{f' v{kept}' if kept else '原樣'}"}
        self._audit(detail, event_id)
        if ev.state in TERMINAL_STATES:
            self._ended(ev)
        self.publish()
        return detail

    # ---------------------------------------------------------------- read
    def _status(self, ev: Event, sub: SubAgent | None, waiting: set[str], running: set[str]) -> str:
        board = self._board()
        if ev.state in TERMINAL_STATES:
            if sub is not None and sub.interrupted:
                return "interrupted"
            if ev.state is EventState.CLOSED and board is not None and any(t.status != "完成" for t in board.for_event(ev.id)):
                return "waiting_tasks"                       # the plan is done; people are still carrying it out
            return "done" if ev.state is EventState.CLOSED else "failed"
        if ev.id in waiting or ev.id in running:
            return "waiting_model"
        if ev.state is EventState.AWAIT_APPROVAL:
            return "waiting_approval"
        if board is not None and ev.state in (EventState.EXECUTE, EventState.VERIFY) \
                and any(t.status != "完成" for t in board.for_event(ev.id)):
            return "waiting_tasks"
        if ev.state is EventState.DETECTED:
            return "queued"
        return "running"

    def subagents(self) -> list[dict[str, Any]]:
        q = self.queue.view()
        waiting = {w["event_id"] for w in q["waiting"] if w["event_id"]}
        running = {w["event_id"] for w in q["running"] if w["event_id"]}
        board = self._board()
        out = []
        for ev in sorted(self.sm.events(), key=lambda e: e.opened_at, reverse=True):
            sub = self.subs.get(ev.id)
            t, p = (sub.type, sub.priority) if sub else route(ev)
            steps = [*(s for r in ev.prior_rounds for s in r.steps), *ev.steps]
            tasks = board.for_event(ev.id) if board is not None else []
            out.append({
                "id": ev.id, "type": t, "type_label": TYPES.get(t, t), "parent": "main", "priority": p,
                "priority_label": PRIORITY_LABEL.get(p, str(p)),
                "status": self._status(ev, sub, waiting, running), "state": ev.state.value,
                "round": len(ev.prior_rounds) + 1, "replans": ev.replans,
                "tools_called": sum(1 for s in steps if s.result is not None),
                "tasks_done": sum(1 for x in tasks if x.status == "完成"), "tasks_total": len(tasks),
                "title": ev.command or (ev.alert_card.title if ev.alert_card else ev.anomaly.message),
                "machine_id": ev.machine_id, "source": ev.source,
                "via": sub.via if sub else "code", "reason": sub.reason if sub else "",
                "started": ev.opened_at.isoformat(), "updated": (ev.closed_at or (ev.timeline[-1].ts if ev.timeline else ev.opened_at)).isoformat(),
                "interrupted": bool(sub and sub.interrupted), "outcome": ev.outcome,
            })
        return out
