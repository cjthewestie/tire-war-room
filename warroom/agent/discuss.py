"""Issue #10: talking with the agent about a plan that waits for approval (討論這個計劃).

One message from the supervisor goes through three steps, two of them code:

  1. CLASSIFY  Nemotron (JSON schema) says: a question, a change, or something else; a change comes
               with proposed constraints. Code validates every constraint (talk.validate_constraint:
               the press must be one the supervisor named, the window is computed from the words).
               Mock mode, a model failure, or a change the model could not encode -> keyword rules.
  2. ACT       question  -> 問戰情室 (read tools only), the plan does not change.
               change    -> StateMachine.discuss_replan: the card is superseded, REPLAN -> SIMULATE ->
                            GATE with the constraint enforced on every solve and checked in the result;
                            the new card (v2) says what changed. Max MAX_DISCUSS_REPLANS per event.
               anything else -> an honest "I can't enforce this", nothing changes.
  3. REPLY     plain Chinese, built by code from the new card and the checks.

Every message, classification, constraint and reply is in the audit log under the event's id."""
from __future__ import annotations

import itertools
import json
import re
import time
from typing import Any

from warroom.agent import plant
from warroom.agent.commander import detect_server_kind, thinking_off
from warroom.agent.strict import failure_cause, strict_reason
from warroom.agent.talk import CAN_DO, rule_constraints, rule_intent, same_constraint, validate_constraint
from warroom.config import Settings
from warroom.contracts import (
    MAX_DISCUSS_REPLANS,
    AuditKind,
    DiscussReply,
    EventState,
    LLMMode,
    PlanConstraint,
)
from warroom.factory import CURE_PRESS_IDS

CLASSIFY_SYSTEM = """你是實心胎工廠戰情室的計劃討論助理。主管正在看一張「等待核准」的處置計劃，他說了一句話。
判斷這句話是 chitchat（打招呼、道謝、閒聊）、question（問工廠或計劃的問題、要解釋，不改計劃）、change（要求改計劃的條件）或 other（看不懂、與工廠無關）。
change 時把條件轉成 constraints，只能用這四種：
- machine_unavailable：某台機某段時間不能用（保養、維修、停機）。machine 用代號：硫化機 CURE-01..CURE-11（「9 號機」= CURE-09）。when 用 tonight|tomorrow_night|tomorrow_morning|tomorrow|today|now|this_week|unspecified。
- no_overtime：不要加班。
- no_transfer：不要跨組調人。
- prefer_option：選某個方案，option = A|B|overtime|transfer。
不能轉成這四種的改動，不要編造，constraints 留空陣列。只輸出一個 JSON 物件。
例：「9 號機今晚要保養」→ change，machine_unavailable CURE-09 tonight；「不要跨組調人」→ change，no_transfer；「為什麼用 9 號機？」→ question；「哈摟」「謝謝」→ chitchat；「asdf」→ other。"""


def classify_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": ["chitchat", "question", "change", "other"]},
            "constraints": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["machine_unavailable", "no_overtime", "no_transfer",
                                                       "prefer_option"]},
                    "machine": {"type": "string"},
                    "when": {"type": "string", "enum": ["tonight", "tomorrow_night", "tomorrow_morning", "tomorrow",
                                                        "today", "now", "this_week", "unspecified"]},
                    "option": {"type": "string", "enum": ["A", "B", "overtime", "transfer", "none"]},
                },
                "required": ["kind"]}},
        },
        "required": ["intent", "constraints"],
    }


class PlanTalk:
    """`client` (an AsyncOpenAI) is injectable for tests."""

    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.mode = LLMMode(settings.llm_mode)
        self._ids = itertools.count(1)
        self.client = client
        self.server_kind = "openai"
        if self.mode is LLMMode.LLM and client is None:
            from openai import AsyncOpenAI
            self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key_effective,
                                      timeout=settings.llm_timeout_s, max_retries=0)
            self.server_kind = detect_server_kind(settings.base_url)

    # ------------------------------------------------------------------ 1. classify
    async def _llm_classify(self, text: str, card: Any) -> dict[str, Any]:
        plan = {"title": card.title, "actions": card.actions, "options": [f"{o.id} {o.label}" for o in card.options]} \
            if card is not None else None
        user = json.dumps({"message": text, "waiting_plan": plan}, ensure_ascii=False, default=str)
        extra = thinking_off(self.server_kind)
        r = await self.client.chat.completions.create(
            model=self.settings.model, temperature=0.0, max_tokens=300,
            messages=[{"role": "system", "content": CLASSIFY_SYSTEM + plant.section()}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "PlanTalk", "schema": classify_schema()}},
            **({"extra_body": extra} if extra else {}))
        raw = (r.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        out = json.loads(raw)
        if not isinstance(out, dict) or out.get("intent") not in ("chitchat", "question", "change", "other"):
            raise ValueError(f"bad classification {raw[:200]!r}")
        out["_raw"] = raw[:600]
        return out

    async def classify(self, text: str, card: Any, now: Any) -> dict[str, Any]:
        """{intent, constraints, why, by, fell_back, error, model_output, dropped}"""
        res: dict[str, Any] = {"by": "rules", "fell_back": False, "error": None, "model_output": None, "dropped": []}
        from warroom.agent.ask import smalltalk_reply
        if smalltalk_reply(text) is not None:          # a greeting / thanks: code answers, no model, no tool
            return {**res, "intent": "chitchat", "constraints": [], "why": None}
        if self.mode is LLMMode.LLM:
            try:
                got = await self._llm_classify(text, card)
                res["model_output"] = got.pop("_raw", None)
                cons: list[PlanConstraint] = []
                for raw in got.get("constraints") or []:
                    c, why = validate_constraint(raw if isinstance(raw, dict) else {}, text, now)
                    if c is not None and not any(same_constraint(c, x) for x in cons):
                        cons.append(c)
                    elif why:
                        res["dropped"].append({"constraint": raw, "why": why})
                intent = got["intent"]
                rules_say = rule_intent(text)
                if intent != "change" and rules_say == "change" and rule_constraints(text, now)[0]:
                    # code decides: words the rules read as an explicit, enforceable change (「9 號機今晚要
                    # 保養」) are never dropped because the model misread them as small talk or a question
                    res["fell_back"] = True
                    res["error"] = f"model said {intent!r} for an explicit change: rules used"
                    intent = "change-by-rules"
                elif intent == "chitchat" and rules_say != "chitchat" and rules_say != "unclear":
                    res["fell_back"] = True
                    res["error"] = f"model said chitchat for a factory message: rules used ({rules_say})"
                    intent = "change-by-rules"
                if intent == "change" and cons:
                    return {**res, "intent": "change", "constraints": cons, "why": None, "by": "model"}
                if intent == "question":
                    return {**res, "intent": "question", "constraints": [], "why": None, "by": "model"}
                if intent == "chitchat":
                    return {**res, "intent": "chitchat", "constraints": [], "why": None, "by": "model"}
                if intent == "other":
                    ri = rule_intent(text)
                    return {**res, "intent": "unsupported" if ri == "unsupported" else "unclear", "constraints": [],
                            "why": None, "by": "model"}
                # a change the model could not encode (or encoded wrongly): the rules get one try
                res["fell_back"] = True
                res["error"] = res["error"] or ("model change had no valid constraint"
                                                + (f": {res['dropped']}" if res["dropped"] else ""))
            except Exception as e:  # noqa: BLE001 - model down / invalid JSON: rules
                if not self.settings.llm_fallback_to_mock:
                    raise
                res["fell_back"], res["error"] = True, f"{type(e).__name__}: {e}"[:300]
        intent = rule_intent(text)
        cons, why = rule_constraints(text, now) if intent in ("change", "unsupported") else ([], None)
        if intent == "change" and not cons:
            intent = "unsupported"
        return {**res, "intent": intent, "constraints": cons, "why": why}

    # ------------------------------------------------------------------ 2 + 3. act and reply
    async def discuss(self, *, sm: Any, event_id: str, text: str, actor: str, ask_agent: Any, tools: Any,
                      audit: Any, context: dict[str, Any], now: Any) -> DiscussReply:
        did = f"DIS-{next(self._ids):04d}"
        ev = sm.get(event_id)
        audit.append(AuditKind.SYSTEM, actor, {"discuss": {"id": did, "text": text}}, event_id=event_id)
        card = sm.waiting_card(event_id)
        t0 = time.perf_counter()
        try:
            cls = await self.classify(text, card, now)
        except Exception as e:  # noqa: BLE001 - only with llm_fallback_to_mock=False (--strict): no rules
            reason = strict_reason(e, "回覆，計劃未變更")
            audit.append(AuditKind.LLM_CALL, "discuss", {
                "phase": "classify", "id": did, "mode": self.mode.value, "model": self.settings.model,
                "total_ms": round((time.perf_counter() - t0) * 1000, 1), "fell_back_to_mock": False,
                "error": f"{type(e).__name__}: {e}"[:300], "cause": failure_cause(e), "strict": True,
                "reason": reason}, event_id=event_id)
            return DiscussReply(id=did, event_id=event_id, text=text, intent="unclear", reply=reason,
                                classified_by="model", factory_time=now)
        audit.append(AuditKind.LLM_CALL, "discuss", {
            "phase": "classify", "id": did, "mode": self.mode.value,
            "model": self.settings.model if self.mode is LLMMode.LLM else None,
            "total_ms": round((time.perf_counter() - t0) * 1000, 1), "intent": cls["intent"],
            "classified_by": cls["by"], "fell_back_to_mock": cls["fell_back"], "error": cls["error"],
            "constraints": [c.model_dump(mode="json", exclude={"text"}) for c in cls["constraints"]],
            "dropped": cls["dropped"], "model_output": cls["model_output"], "plant_sha256": plant.active_sha()},
            event_id=event_id)
        out = DiscussReply(id=did, event_id=event_id, text=text, intent="question", reply="",
                           classified_by=cls["by"], fell_back=cls["fell_back"], factory_time=now)
        intent = cls["intent"]
        if intent in ("chitchat", "unclear"):
            from warroom.agent.ask import HELLO_REPLY, smalltalk_reply
            out.intent = intent
            hint = "「9 號機今晚要保養」「不要加班」，或問「為什麼用 9 號機？」" if card is not None else \
                "「7 號機現在怎樣？」「今晚夜班缺誰？」"
            if intent == "chitchat":
                out.reply = smalltalk_reply(text) or HELLO_REPLY
                if card is not None:
                    out.reply += f"目前有一張等待核准的計劃 v{card.version}，你可以說{hint}"
            else:
                out.reply = f"我不確定你的意思，所以沒有查資料，也沒有改計劃。你可以說{hint}"
        elif intent == "question":
            ans = await ask_agent.ask(text, tools=tools, audit=audit, context=context, now=now, actor=actor)
            out.ask = ans
            plan = ""
            if card is not None:       # what the waiting plan says and why: the card's own (code-built) lines
                acts = "；".join(a for a in card.actions[:2])
                plan = (f"目前計劃 v{card.version}：{acts}。依據：{card.reason}"
                        + (f"；交期{card.delivery_impact}" if card.delivery_impact else "") + "。")
            out.reply = plan + ans.answer + ("（計劃未變更）" if card is not None else "")
        elif intent == "change" and card is None:
            out.intent = "not_waiting"
            busy = ev.state in (EventState.REPLAN, EventState.SIMULATE, EventState.GATE, EventState.PLAN)
            out.reply = ("我正在依上一個條件重排，等新的核准卡出來再說一次。" if busy else
                         f"這個事件目前沒有等待核准的計劃（{ev.state.value}），沒有可以改的；你可以用「下新任務」交辦。")
        elif intent == "change" and ev.discuss_replans >= MAX_DISCUSS_REPLANS:
            out.intent = "capped"
            out.reply = (f"這個計劃已依討論重排 {ev.discuss_replans} 次（上限 {MAX_DISCUSS_REPLANS} 次），"
                         f"我維持目前的 v{card.version}，不再重排。請核准或駁回（駁回時寫理由）。")
        elif intent == "change":
            new = [c for c in cls["constraints"] if not any(same_constraint(c, x) for x in ev.constraints)]
            out.intent = "change"
            if not new:
                out.reply = ("這個條件已經在計劃裡了（" + "、".join(c.label for c in ev.constraints) +
                             f"），v{card.version} 已經遵守，不需要重排。")
            else:
                out.constraints = new
                out.superseded = card.id
                try:
                    req = await sm.discuss_replan(event_id, new, text, actor)
                except ValueError as e:
                    out.intent, out.reply = "not_waiting", f"現在不能重排：{e}"
                    req = None
                else:
                    out.replanned = True
                    out.approval = req
                    out.reply = self._replan_reply(sm, ev, card, req, new)
                    out.changes = list(req.changes) if req is not None else []
        else:
            out.intent = "unsupported"
            why = cls.get("why")
            out.reply = ("這個要求我沒辦法用程式保證做到" + (f"：{why}" if why else "") + "。計劃沒有改。" + CAN_DO)
        audit.append(AuditKind.SYSTEM, "agent", {"discuss_reply": {
            "id": did, "intent": out.intent, "reply": out.reply, "replanned": out.replanned,
            "constraints": [c.label for c in out.constraints], "superseded": out.superseded,
            "approval_id": out.approval.id if out.approval else None,
            "version": out.approval.version if out.approval else None, "changes": out.changes,
            "classified_by": out.classified_by, "fell_back": out.fell_back}}, event_id=event_id)
        return out

    @staticmethod
    def _replan_reply(sm: Any, ev: Any, old: Any, req: Any, new: list[PlanConstraint]) -> str:
        chips = "、".join(c.label for c in new)
        if req is None:
            if ev.state.value in ("closed", "safe_stop"):
                return f"已依「{chips}」重新規劃，但事件已結束：{ev.outcome or ev.state.value}。"
            return f"已依「{chips}」重新規劃；新計劃沒有需要你核准的變更。"
        changed = "；".join(req.changes) if req.changes else "排程內容與上一版相同"
        checks = []
        for c in new:
            if c.kind == "machine_unavailable" and c.machine_id in CURE_PRESS_IDS:
                checks.append(f"{c.label.replace('不可用', '')}沒有排任何模次")
            elif c.kind == "no_overtime":
                checks.append("沒有加班")
            elif c.kind == "no_transfer":
                checks.append("沒有跨組調人")
            elif c.kind == "prefer_option":
                rec = next((o for o in req.options if o.recommended), None)
                checks.append(f"建議方案為 {rec.id} {rec.label}" if rec else "這張卡沒有可選方案")
        left = MAX_DISCUSS_REPLANS - ev.discuss_replans
        return (f"好，已依「{chips}」重排。新核准卡 v{req.version} 取代 v{old.version}：{changed}。"
                f"程式已檢查新排程：{'、'.join(checks)}。你可以核准 v{req.version}，或繼續說"
                + (f"（還可討論重排 {left} 次）。" if left > 0 else "（已達討論重排上限，之後維持這一版）。"))
