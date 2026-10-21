"""NemoClaw 異常報告 (Kyle 10-21, option A): the incident report is WRITTEN BY THE LOCAL MODEL while the crew repairs.

Flow: 主管核准 → the draft starts in the background with the facts known then (anomaly, hypothesis, what ran, the
approved option, the worker tasks, the SOPs) → the model writes title / summary / cause / actions during the 10–15 s
repair → when the event closes (machine back to normal) CODE appends the result line (reading, repair done time,
recovery time) and the report is delivered at once, in step with the repair state. Progress messages stay code-written.

- IncidentReporter.draft(event, facts): the model through the same OpenAI-compatible settings as the commander (BASE_URL,
  MODEL, key, server kind, thinking off, JSON schema), in the ModelQueue at the lowest priority 「NemoClaw 報告」 (reports
  never take the queue's last slot, so safety / machine planning always has one), timeout ~15 s. Every number it writes
  must appear in the facts (the ungrounded-number check of ask.py / main_agent.py): a clause with an invented number is
  dropped; a part left empty, invalid JSON, a timeout or mock mode -> the code-written draft from the same facts
  (by "rules").
- IncidentReporter.finalize(draft, final_facts): the text with the code-appended 「結果：…」 line.
- ReportDesk: the hooks the main agent (orchestrator) calls — on_decided (approve / modify: start the draft) and on_ended
  (CLOSED: finalize and publish Event.ai_report, audit SYSTEM {"msg": "NemoClaw 異常報告", ...} as a main_agent row so
  the 主控台 shows it as a NemoClaw bubble). A draft not ready at close is finalized as soon as it is (delay noted)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from warroom.agent.narrate import CHANNEL_LABELS, NOTIFY_LABELS, expectation_text, fmt_num, machine_label
from warroom.contracts import (
    WRITE_TOOLS,
    AuditKind,
    Channel,
    Event,
    EventAiReport,
    EventState,
    NotifyKind,
    RiskLevel,
    ToolName,
    WsType,
)
from warroom.factory import MACHINE_BY_ID, TEAM_LABELS, TZ

ACTOR = "main_agent"
PRIORITY_REPORT = 5            # below questions (orchestrator.PRIORITY["question"] = 4)
QUEUE_LABEL = "NemoClaw 報告"
TIMEOUT_S = 15.0               # the model call
CLOSE_WAIT_S = 30.0            # after close: how long a slow draft may still run before the rules draft is used
RECOVER_WAIT_S = 30.0          # an Event with recovered_at (the recovery ramp): wait for it after close at most this
SETTLE_S = 3.0                 # after approval: let EXECUTE dispatch its tasks (workers named) before the facts are read
PARTS = ("title", "summary", "cause", "actions")
_CAP = {"title": 30, "summary": 80, "cause": 90, "action": 50}
UNIT = {Channel.TEMP: "°C", Channel.PRESSURE: " kgf/cm²", Channel.WATER_FLOW: " L/min", Channel.STEAM: " kgf/cm²"}
_NUM_RX = re.compile(r"\d+(?:\.\d+)?")
_CLAUSE_RX = re.compile(r"[^。；;]+[。；;]?")
_BACK_RX = re.compile(r"回到\s*(-?\d+(?:\.\d+)?)\s*(°C|kgf/cm²|L/min)?")

SYSTEM = """你是實心胎工廠戰情室的 NemoClaw。機台異常的處置已經由主管核准、現場正在維修；請依使用者訊息的 facts（系統紀錄）先寫好異常報告，修好後系統會補上結果。
只輸出一個 JSON 物件 {"title","summary","cause","actions"}：
- title：機台名稱＋異常，20 字內
- summary：一句話，50 字內：發生什麼事、怎麼處理
- cause：一句話，60 字內：原因判斷與依據
- actions：1～3 條已做的處置，每條 35 字內（自動通知、主管核准的方案、派給誰做什麼）
規則：繁體中文白話，寫給主管看；只寫 facts 裡有的事，不推測、不建議、不寫預防措施；數字只能照抄 facts 裡出現過的數字，不可計算或換算；不寫結果（維修完成、恢復正常、回到幾度由系統補上）；不提 JSON、模型或 AI。"""


def report_schema() -> dict[str, Any]:
    """Only constraints the Spark's vLLM (xgrammar) already serves for the dispatcher (string maxLength); 1–3 actions is
    enforced in code."""
    s = {"type": "string"}
    return {"type": "object", "additionalProperties": False, "required": list(PARTS),
            "properties": {"title": {**s, "maxLength": 24}, "summary": {**s, "maxLength": 60},
                           "cause": {**s, "maxLength": 70},
                           "actions": {"type": "array", "items": {**s, "maxLength": 40}}}}


# ---------------------------------------------------------------------------------- facts
def _hm(ts: datetime | None) -> str | None:
    return ts.astimezone(TZ).strftime("%H:%M") if ts is not None else None


def _write_line(sr: Any) -> str | None:
    """「通知維修（自動）→ 維修組：…」 for a write step that ran."""
    tool = sr.step.tool
    args, data = sr.step.args or {}, (sr.result.data if sr.result is not None else {}) or {}
    auto = sr.gate is not None and sr.gate.risk is RiskLevel.R1_NOTIFY
    how = "自動" if auto else "主管核准"
    if tool is ToolName.NOTIFY_TEAMS:
        try:
            what = NOTIFY_LABELS[NotifyKind(args.get("kind"))]
        except ValueError:
            what = "通知"
        teams = data.get("team_labels") or [TEAM_LABELS.get(t, t) for t in args.get("teams") or []]
        msg = str(args.get("message") or "")[:40]
        return f"{what}（{how}）→ {'、'.join(map(str, teams))}{'：' + msg if msg else ''}"
    if tool is ToolName.CONFIRM_SCHEDULE:
        v = data.get("version") or data.get("schedule_version") or args.get("schedule_version")
        return f"確認排程{' v' + str(v) if v else ''}（{how}）"
    if tool is ToolName.SET_CURE_TIME:
        h = data.get("total_cure_h") or args.get("total_cure_h")
        return f"設定{machine_label(str(args.get('press_id', '')))}硫化時間 {fmt_num(h)} h（{how}）"
    return None


def sop_refs(ev: Event, audit: Any = None, limit: int = 2) -> list[str]:
    """The SOPs this event looked up (its context search and any sop_search step): 「SOP-OM-01 開煉機過熱處置」."""
    hits: list[dict[str, Any]] = []
    for sr in [*(s for r in ev.prior_rounds for s in r.steps), *ev.steps]:
        if sr.step.tool is ToolName.SOP_SEARCH and sr.result is not None and sr.result.ok:
            hits += list((sr.result.data or {}).get("hits") or [])
    if audit is not None:
        with contextlib.suppress(Exception):
            for e in audit.tail(0, 3000):
                if e.event_id == ev.id and e.kind is AuditKind.TOOL_RESULT and e.actor == "tool:sop_search":
                    d = e.payload.get("data") if isinstance(e.payload, dict) else None
                    hits += list((d or {}).get("hits") or []) if isinstance(d, dict) else []
    out: dict[str, str] = {}
    for h in hits:
        sid = str(h.get("sop_id") or "")
        if not sid or sid in out:
            continue
        m = re.search(r"〔" + re.escape(sid) + r"\s*([^｜〕]+)", str(h.get("text") or ""))
        out[sid] = f"{sid} {(h.get('title') or (m.group(1) if m else '')).strip()}".strip()
    return list(out.values())[:limit]


def report_facts(ev: Event, req: Any = None, tasks: list[Any] | None = None, sops: list[str] | None = None
                 ) -> dict[str, Any]:
    """What the system recorded when the supervisor approved (the only source the model may use)."""
    a = ev.anomaly
    h = ev.hypotheses[-1] if ev.hypotheses else None
    f: dict[str, Any] = {"機台": machine_label(ev.machine_id), "發生時間": _hm(a.ts or ev.opened_at), "異常": a.message}
    if ev.alert_card is not None:
        f["標題"] = re.sub(r"^異常[：:]\s*", "", ev.alert_card.title)
        f["警示"] = ev.alert_card.summary
    if h is not None:
        f["判斷"] = f"{h.id} {h.statement}"
        if h.evidence:
            f["依據"] = h.evidence[:3]
        if h.expectation is not None:
            f["預期"] = expectation_text(h.expectation)
    if ev.goal:
        f["目標"] = ev.goal
    if ev.safety_actions:
        f["安全核心"] = [s.message for s in ev.safety_actions][:2]
    ran = [x for x in (_write_line(sr) for sr in ev.steps
                       if sr.step.tool in WRITE_TOOLS and sr.state == "ok") if x]
    if ran:
        f["已執行"] = ran[:4]
    d = getattr(req, "decision", None)
    if req is not None and d is not None:
        opt = next((o for o in req.options if o.id == d.option_id), None)
        ok = {"卡片": req.title, "方案": f"{opt.id} {opt.label}" if opt else None, "時間": _hm(d.decided_at),
              "交期影響": req.delivery_impact or None,
              "說明": d.reason or None}
        f["主管核准"] = {k: v for k, v in ok.items() if v}
    if tasks:
        f["派工"] = [f"{t.kind} {t.worker_name or '待派'}｜{str(t.title)[:36]}" for t in tasks][:4]
    if sops:
        f["SOP"] = sops
    return f


def final_facts(ev: Event, sim: Any = None) -> dict[str, Any]:
    """What code appends at close: the reading back to normal, the repair done time, the recovery time."""
    exp = ev.hypotheses[-1].expectation if ev.hypotheses else None
    ch = exp.channel if exp is not None else ev.anomaly.channel
    mid = exp.machine_id if exp is not None else ev.machine_id
    mdef = MACHINE_BY_ID.get(mid)
    reading = "膠溫" if ch is Channel.TEMP and mdef is not None and mdef.handles_compound else CHANNEL_LABELS.get(ch, "讀值")
    value: str | None = None
    m = _BACK_RX.search(ev.outcome or "")
    if m:
        value = f"{m.group(1)}{UNIT.get(ch, '')}"        # the verified reading of the state machine's outcome
    elif sim is not None and ev.repaired_at is not None:
        with contextlib.suppress(Exception):
            v = getattr(sim.machine(mid).last_reading, ch.value)
            if v is not None:
                value = f"{fmt_num(v)}{UNIT.get(ch, '')}"
    recovered = getattr(ev, "recovered_at", None) or ev.closed_at
    return {"機台": machine_label(mid), "讀值": reading, "值": value, "維修完成": _hm(ev.repaired_at),
            "恢復正常": _hm(recovered), "結果": ev.outcome}


def result_line(ff: dict[str, Any]) -> str:
    """「結果：A 線大開煉機 膠溫回到 100°C（維修完成 14:32、恢復正常 14:33）」"""
    when = "、".join(x for x in (f"維修完成 {ff['維修完成']}" if ff.get("維修完成") else "",
                                 f"恢復正常 {ff['恢復正常']}" if ff.get("恢復正常") else "") if x)
    if ff.get("值"):
        return f"結果：{ff['機台']} {ff['讀值']}回到 {ff['值']}" + (f"（{when}）" if when else "")
    out = re.sub(r"^(已修復|安全中止)[：:]\s*", "", str(ff.get("結果") or "事件結案"))
    return f"結果：{out}" + (f"（{when}）" if when and ff.get("維修完成") else "")


# ---------------------------------------------------------------------------------- the rules draft
def _clause(text: str) -> str:
    return re.split(r"[，；。]", text or "", maxsplit=1)[0].strip()


def rules_draft(facts: dict[str, Any]) -> dict[str, Any]:
    """The code-written draft from the same facts (mock mode, or the model failed)."""
    machine = str(facts.get("機台") or "")
    title = str(facts.get("標題") or f"{machine} 異常")
    ok = facts.get("主管核准") or {}
    plan = str(ok.get("方案") or "").split(" ", 1)[-1] if ok.get("方案") else ""
    judged = str(facts.get("判斷") or "").split(" ", 1)[-1]
    summary = _clause(str(facts.get("異常") or title)) + (f"，判斷為{judged}" if judged else "") + \
        (f"；主管核准「{plan}」，已派員處理" if plan else "；已派員處理")
    cause = judged or "原因待現場確認"
    if facts.get("依據"):
        cause += "；依據：" + "、".join(map(str, facts["依據"][:2]))
    actions = [str(x).split("：", 1)[0] for x in facts.get("已執行") or []]      # 「通知維修（自動）→ 維修組」
    if facts.get("派工"):
        actions.append("派工：" + "、".join(str(x).split("｜", 1)[0] for x in facts["派工"][:3]))
    if not actions and ok.get("方案"):
        actions.append(f"主管核准方案 {ok['方案']}")
    return {"title": title[:_CAP["title"]], "summary": summary[:_CAP["summary"]], "cause": cause[:_CAP["cause"]],
            "actions": [x[:_CAP["action"]] for x in (actions or ["依 SOP 派員處理"])[:3]], "result_pending": True}


# ---------------------------------------------------------------------------------- grounding
def known_numbers(facts: dict[str, Any]) -> set[str]:
    known = set(_NUM_RX.findall(json.dumps(facts, ensure_ascii=False)))
    return known | {n.split(".")[0] for n in known}            # "101.9" also vouches for "101"


def ground(raw: dict[str, Any], facts: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], str | None]:
    """The model's parts with every clause that holds a number not in the facts dropped.
    (draft or None, dropped numbers, why the whole draft is not usable)."""
    known = known_numbers(facts)
    dropped: list[str] = []

    def keep(text: Any, cap: int) -> str:
        out = []
        for c in _CLAUSE_RX.findall(str(text or "").strip()):
            bad = [n for n in _NUM_RX.findall(c) if n not in known]
            if bad:
                dropped.extend(bad)
            elif c.strip():
                out.append(c.strip())
        return "".join(out).strip("；; ")[:cap]

    acts = raw.get("actions")
    acts = [acts] if isinstance(acts, str) else acts if isinstance(acts, list) else []
    d = {"title": keep(raw.get("title"), _CAP["title"]), "summary": keep(raw.get("summary"), _CAP["summary"]),
         "cause": keep(raw.get("cause"), _CAP["cause"]),
         "actions": [x for x in (keep(a, _CAP["action"]) for a in acts[:3]) if x], "result_pending": True}
    empty = [k for k in PARTS if not d[k]]
    if empty:
        why = f"ungrounded numbers {dropped}" if dropped else "empty"
        return None, dropped, f"{why}: {'/'.join(empty)} left empty"
    return d, dropped, None


# ---------------------------------------------------------------------------------- the reporter
class IncidentReporter:
    """The model in LLM mode (through the ModelQueue), the rules draft otherwise or on any model failure."""

    def __init__(self, settings: Any = None, client: Any = None, queue: Any = None, server_kind: str | None = None,
                 timeout_s: float = TIMEOUT_S):
        self.settings, self.client, self.queue = settings, client, queue
        self.server_kind = server_kind
        self.timeout_s = timeout_s
        self.llm = settings is not None and str(getattr(settings, "llm_mode", "mock")) in ("llm", "LLMMode.LLM")
        self.model = getattr(settings, "model", None) if self.llm else None
        self._limit: asyncio.Semaphore | None = None

    async def _client(self) -> Any:
        if self.client is None:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(base_url=self.settings.base_url, api_key=self.settings.api_key_effective,
                                      timeout=self.timeout_s, max_retries=0)
        if self.server_kind is None:
            from warroom.agent.commander import detect_server_kind
            self.server_kind = await asyncio.to_thread(detect_server_kind, self.settings.base_url)
        return self.client

    def _slots(self) -> asyncio.Semaphore:
        """Reports never take the queue's last slot: with 3 slots at most 2 reports run, 1 stays for planning."""
        if self._limit is None:
            self._limit = asyncio.Semaphore(max(1, int(getattr(self.queue, "slots", 1)) - 1))
        return self._limit

    async def _ask(self, facts: dict[str, Any]) -> str:
        from warroom.agent.commander import thinking_off
        client = await self._client()
        extra = thinking_off(self.server_kind or "openai")
        r = await asyncio.wait_for(client.chat.completions.create(
            model=self.settings.model, temperature=0.0, max_tokens=400,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": json.dumps({"facts": facts}, ensure_ascii=False)}],
            response_format={"type": "json_schema", "json_schema": {"name": "IncidentReport", "schema": report_schema()}},
            **({"extra_body": extra} if extra else {})), self.timeout_s)
        return (r.choices[0].message.content or "").strip()

    async def draft(self, event: Event, facts: dict[str, Any]) -> dict[str, Any]:
        """{"title", "summary", "cause", "actions", "result_pending", "by", "ms", "model_ms", "model", "model_error",
        "dropped"}: the model's parts (numbers checked against the facts), or the rules draft."""
        t0 = time.perf_counter()
        base = rules_draft(facts)
        meta = {"model": self.model, "model_error": None, "dropped": [], "model_ms": None}
        if not self.llm:
            return {**base, "by": "rules", **meta, "ms": round((time.perf_counter() - t0) * 1000, 1)}
        raw = ""
        try:
            async with self._slots():
                if self.queue is not None:
                    async with self.queue.slot(PRIORITY_REPORT, "reporter", event.id, QUEUE_LABEL):
                        t1 = time.perf_counter()
                        raw = await self._ask(facts)
                else:
                    t1 = time.perf_counter()
                    raw = await self._ask(facts)
            meta["model_ms"] = round((time.perf_counter() - t1) * 1000, 1)
            from warroom.agent.commander import _strip_fences
            parsed = json.loads(_strip_fences(raw))
            if not isinstance(parsed, dict):
                raise TypeError("not a JSON object")
            d, dropped, why = ground(parsed, facts)
            meta["dropped"] = dropped
            if d is None:
                raise ValueError(why)
            return {**d, "by": "model", **meta, "ms": round((time.perf_counter() - t0) * 1000, 1)}
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - invalid JSON, timeout, unreachable, ungrounded: the rules draft
            meta["model_error"] = f"{type(e).__name__}: {e}"[:200] or type(e).__name__
            return {**base, "by": "rules", **meta, "ms": round((time.perf_counter() - t0) * 1000, 1), "raw": raw[:300]}

    @staticmethod
    def finalize(draft: dict[str, Any], final: dict[str, Any]) -> str:
        """The report text: the drafted parts, then the result line code writes from the final facts."""
        acts = [str(a).rstrip("。；; ") for a in draft.get("actions") or []]
        return "\n".join([f"【異常報告】{draft['title']}", f"摘要：{draft['summary']}", f"原因：{draft['cause']}",
                          f"處置：{'；'.join(acts)}", result_line(final)])


# ---------------------------------------------------------------------------------- the hooks
@dataclass
class _Job:
    event_id: str
    task: asyncio.Task | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    finishing: bool = False
    published: bool = False


class ReportDesk:
    """The main agent's NemoClaw report desk: on_decided starts a draft, on_ended delivers the report."""

    def __init__(self, orch: Any, reporter: IncidentReporter | None = None):
        self.o = orch
        disp = getattr(orch, "dispatcher", None)
        settings = getattr(disp, "settings", None)
        self.reporter = reporter or IncidentReporter(
            settings, queue=getattr(orch, "queue", None),
            server_kind=getattr(disp, "server_kind", None) if getattr(disp, "llm", False) else None)
        self.jobs: dict[str, _Job] = {}

    def _event(self, event_id: str) -> Event | None:
        try:
            return self.o.sm.get(event_id)
        except Exception:  # noqa: BLE001
            return None

    def _tasks(self, ev: Event) -> list[Any]:
        with contextlib.suppress(Exception):
            board = self.o._board()
            return list(board.for_event(ev.id)) if board is not None else []
        return []

    def _now(self) -> datetime:
        return self.o._now()

    # ---------------------------------------------------------------- approval decided: start the draft
    def on_decided(self, req: Any) -> None:
        d = getattr(req, "decision", None)
        eid = getattr(req, "event_id", None)
        if not eid or d is None or getattr(d.decision, "value", d.decision) not in ("approve", "modify"):
            return
        ev = self._event(eid)
        if ev is None or ev.source != "detector" or ev.state in (EventState.CLOSED, EventState.SAFE_STOP):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                                   # no event loop (a sync caller): nothing can run the draft
        old = self.jobs.get(eid)
        if old is not None and old.task is not None and not old.task.done() and not old.finishing:
            old.task.cancel()                        # a later card of the same event: draft from the newer facts
        job = _Job(eid)
        job.task = loop.create_task(self._draft(ev, req, job), name=f"ai-report-{eid}")
        self.jobs[eid] = job

    async def _draft(self, ev: Event, req: Any, job: _Job) -> dict[str, Any]:
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < SETTLE_S and ev.state in (EventState.AWAIT_APPROVAL, EventState.EXECUTE):
            await asyncio.sleep(0.2)                 # EXECUTE dispatches the approved writes' tasks (workers named)
        job.facts = report_facts(ev, req, self._tasks(ev), sop_refs(ev, self.o.audit))
        out = await self.reporter.draft(ev, job.facts)
        out["drafted_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out

    # ---------------------------------------------------------------- event ended: deliver
    def on_ended(self, ev: Event) -> None:
        job = self.jobs.get(ev.id)
        if job is None or job.published or job.finishing:
            return
        if ev.state is not EventState.CLOSED:        # safe stop / interrupt / scenario switch: no NemoClaw report
            if job.task is not None:
                job.task.cancel()
            self.jobs.pop(ev.id, None)
            return
        job.finishing = True
        closed = time.perf_counter()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._publish(ev, job, self._result(job), 0.0)
            return
        if job.task is not None and job.task.done() and not self._wait_recovery(ev):
            # ready: delivered right away, just after the main agent's 「完成」 line of this same close
            loop.call_soon(lambda: self._publish(ev, job, self._result(job), 0.0))
            return
        loop.create_task(self._finish(ev, job, closed), name=f"ai-report-close-{ev.id}")

    @staticmethod
    def _wait_recovery(ev: Event) -> bool:
        """The recovery ramp (Event.recovered_at, when the contract has it): repaired, not yet back to normal."""
        return "recovered_at" in type(ev).model_fields and ev.repaired_at is not None and \
            getattr(ev, "recovered_at", None) is None

    def _result(self, job: _Job) -> dict[str, Any]:
        t = job.task
        if t is not None and t.done() and not t.cancelled() and t.exception() is None:
            return t.result()
        return {**rules_draft(job.facts), "by": "rules", "model": self.reporter.model, "dropped": [], "model_ms": None,
                "model_error": "draft cancelled" if t is not None and t.cancelled() else "draft failed",
                "drafted_ms": None}

    async def _finish(self, ev: Event, job: _Job, closed: float) -> None:
        t0 = time.perf_counter()
        while self._wait_recovery(ev) and time.perf_counter() - t0 < RECOVER_WAIT_S:
            await asyncio.sleep(0.2)
        if job.task is not None and not job.task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait({job.task}, timeout=CLOSE_WAIT_S)
            if not job.task.done():
                job.task.cancel()
                with contextlib.suppress(BaseException):
                    await job.task
        if not job.facts:                            # cancelled before the facts were read
            job.facts = report_facts(ev, None, self._tasks(ev), sop_refs(ev, self.o.audit))
        self._publish(ev, job, self._result(job), round((time.perf_counter() - closed) * 1000, 1))

    def _publish(self, ev: Event, job: _Job, draft: dict[str, Any], late_ms: float) -> None:
        if job.published:
            return
        job.published = True
        sim = getattr(self.o.sm, "sim", None)
        text = self.reporter.finalize(draft, final_facts(ev, sim))
        rep = EventAiReport(text=text, summary=draft["summary"], by=draft["by"], drafted_ms=draft.get("drafted_ms"),
                            at=self._now(), late_ms=late_ms or None)
        ev.ai_report = rep
        self.o.audit.append(AuditKind.SYSTEM, ACTOR, {
            "orch": "ai_report", "msg": "NemoClaw 異常報告", "text": text, "summary": rep.summary, "by": rep.by,
            "drafted_ms": rep.drafted_ms, "late_ms": rep.late_ms, "model": draft.get("model"),
            "model_ms": draft.get("model_ms"), "model_error": draft.get("model_error"),
            "dropped": draft.get("dropped") or [], **({"model_raw": draft["raw"]} if draft.get("raw") else {})},
            event_id=ev.id)
        bus = getattr(self.o, "bus", None) or getattr(self.o.sm, "bus", None)
        if bus is not None:
            with contextlib.suppress(Exception):
                bus.publish(WsType.EVENT, ev)
