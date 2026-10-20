"""One input, one 主 agent (issue #54, amended). CJ, 2026-10-20: 「agent 回答很奇怪，為什麼要只查不改，他應該就是個 orchestrator，
下方應該要有核准，核准完要請主 agent 派一個 subagent 完成任務並回報」.

The supervisor no longer picks a mode. The main agent classifies each message itself:
  chitchat  greeting / thanks / who-are-you  -> a short direct reply, zero tools
  question  wants facts                       -> the read-only 查詢 path (warroom/agent/ask.py)
  change    a change to the selected waiting plan -> plan discussion / replan (warroom/agent/discuss.py)
  task      something to get done             -> a plan proposal: a subagent drafts it, the supervisor approves it
  unclear   none of these                     -> one clarifying question from the main agent
Identity and small talk are matched by rules first (never 「我不確定要查什麼」 for a greeting); everything else goes to
Nemotron as a JSON schema in LLM mode, with keyword rules as the fallback."""
from __future__ import annotations

import json
import re
import time
from typing import Any

from warroom.agent.ask import THANKS_REPLY, smalltalk_reply

INTENTS = ("chitchat", "question", "change", "task", "unclear")
IDENTITY_REPLY = "我是主 agent（戰情室的 orchestrator），負責把計劃派給子 agent 執行並回報。你可以直接說要做的事，或問工廠現在的狀況。"
HELLO_MAIN_REPLY = "你好，我是主 agent。要我規劃一件事，或查工廠狀況，直接說就好。"
UNCLEAR_MAIN = "我想確認一下：你是要我查資料，還是要我規劃一件事？例如「7 號機現在怎樣？」或「#M-201 可能晚交，請重排」。"

_IDENTITY_RX = re.compile(r"^\s*(?:請問)?\s*(?:你|妳|您)(?:是誰|是什麼|叫什麼|能做什麼|會做什麼|會什麼|可以做什麼|的工作)|介紹(?:一下)?(?:你|妳|您)自己|who are you", re.IGNORECASE)
_CHANGE_RX = re.compile(r"保養|不要|不可|不能用|別|改成|改用|換成|延後|提前|今晚|明天|為什麼|為何|加班|調人|跨組|不加班")
_QUESTION_RX = re.compile(r"[?？]|嗎|什麼|哪[台張個些裡位]|多少|幾[台條人點]|怎樣|怎麼樣|如何|狀況|狀態|情況|是否|有沒有")
_TASK_RX = re.compile(r"請|幫我|安排|通知|重排|調人|加班|補料|檢查|保養|處理|派|排程|停用|確認|重新")

SYSTEM = """你是實心胎工廠戰情室的「主 agent」（orchestrator）。主管說了一句話，你只判斷它屬於哪一類，不做事。
只輸出一個 JSON 物件 {"intent": ..., "reply": ...}：
- chitchat：打招呼、道謝、問你是誰。reply 用繁體中文一句話回答（你是主 agent，負責把計劃派給子 agent 執行並回報）。
- question：想知道工廠現況或資料（只查）。reply 留空字串。
- change：要修改「目前等待核准的計劃」（has_waiting_plan 為 true 時才可能）。reply 留空字串。
- task：要完成一件事（重排、通知、調人、補料、保養安排…）。reply 留空字串。
- unclear：看不出來。reply 用一句話反問主管要查資料還是要規劃一件事。"""


def schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["intent", "reply"],
            "properties": {"intent": {"type": "string", "enum": list(INTENTS)}, "reply": {"type": "string", "maxLength": 120}}}


def rule_intent(text: str, has_waiting_plan: bool) -> tuple[str, str | None]:
    """(intent, direct reply or None) by rules only."""
    t = text.strip()
    if not t:
        return "unclear", UNCLEAR_MAIN
    if _IDENTITY_RX.search(t):
        return "chitchat", IDENTITY_REPLY
    st = smalltalk_reply(t)
    if st is not None:
        return "chitchat", THANKS_REPLY if st == THANKS_REPLY else HELLO_MAIN_REPLY
    asks = bool(re.search(r"[?？]\s*$", t))
    if has_waiting_plan and (re.search(r"為什麼|為何", t) or (_CHANGE_RX.search(t) and not asks)):
        return "change", None              # about the waiting plan: a condition, or why it was planned this way
    if asks:
        return "question", None
    if _TASK_RX.search(t) and not re.search(r"[?？]$", t):
        return "task", None
    if _QUESTION_RX.search(t):
        return "question", None
    if has_waiting_plan and len(t) <= 40:
        return "change", None
    return "unclear", UNCLEAR_MAIN


class MainChat:
    def __init__(self, settings: Any = None, client: Any = None):
        self.settings = settings
        self.client = client
        self.server_kind = "openai"
        self.llm = settings is not None and str(getattr(settings, "llm_mode", "mock")) == "llm"
        if self.llm and client is None:
            from openai import AsyncOpenAI

            from warroom.agent.commander import detect_server_kind
            self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key_effective,
                                      timeout=min(20.0, settings.llm_timeout_s), max_retries=0)
            self.server_kind = detect_server_kind(settings.base_url)

    async def classify(self, text: str, has_waiting_plan: bool, plan_title: str | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        intent, reply = rule_intent(text, has_waiting_plan)
        out = {"intent": intent, "reply": reply, "by": "rules", "fell_back": False, "error": None}
        # identity / small talk are always answered by rules: never a model detour, never a tool
        if intent != "chitchat" and self.llm and self.client is not None:
            from warroom.agent import plant
            from warroom.agent.commander import thinking_off
            extra = thinking_off(self.server_kind)
            try:
                r = await self.client.chat.completions.create(
                    model=self.settings.model, temperature=0.0, max_tokens=120,
                    messages=[{"role": "system", "content": SYSTEM + plant.section()},
                              {"role": "user", "content": json.dumps({"message": text, "has_waiting_plan": has_waiting_plan,
                                                                       "waiting_plan": plan_title}, ensure_ascii=False)}],
                    response_format={"type": "json_schema", "json_schema": {"name": "MainIntent", "schema": schema()}},
                    **({"extra_body": extra} if extra else {}))
                raw = (r.choices[0].message.content or "").strip()
                d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw))
                mi = d.get("intent")
                if mi not in INTENTS:
                    raise ValueError(f"intent {mi!r}")
                if mi == "change" and not has_waiting_plan:
                    mi = "task"
                rep = str(d.get("reply") or "").strip() or None
                if mi == "chitchat":
                    rep = rep or IDENTITY_REPLY
                elif mi == "unclear":
                    rep = rep or UNCLEAR_MAIN
                else:
                    rep = None
                out = {"intent": mi, "reply": rep, "by": "model", "fell_back": False, "error": None}
            except Exception as e:  # noqa: BLE001 - the rule result stands, the failure is recorded
                out = {"intent": intent, "reply": reply, "by": "rules", "fell_back": True, "error": f"{type(e).__name__}: {e}"[:200]}
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return out
