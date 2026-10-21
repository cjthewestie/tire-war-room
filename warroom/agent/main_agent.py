"""主 agent conversation: ONE agent with a tool-calling loop (CJ, 2026-10-21: 「我只需要一個幫忙分類的，現在先不要派發，
主要就是一個 agent 做 tool calling」). No subagents, no mode buttons: the model's tool choice is the classification.

A bounded loop on Nemotron with native tool calling (OpenAI `tools=`; vLLM --enable-auto-tool-choice): at most
MAX_STEPS model calls that use tools per turn, BUDGET_S seconds in all, the last MEMORY_TURNS supervisor messages as
reference and a read-only context (plant status, open items, selected plan).

Tools:
- the read tools of 問戰情室 (ASK_TOOLS: read_sensors, predict_anomaly, cure_formula, query_orders_shifts,
  sop_search = the knowledge base), run through the same Registry and audited like any call;
- propose_plan {task}: something should be DONE on the floor → opens a supervisor-command event (the existing
  下新任務 path: PLAN → SIMULATE → GATE → approval → EXECUTE). Nothing runs before the supervisor approves;
- discuss_waiting_plan {text}: only offered while a waiting plan is selected.
None of them writes to the plant; set_cure_time / confirm_schedule / notify_teams / solve_schedule are refused by code.

Code guards (the model is inside the loop, not in charge of the facts):
- machine ids from the model are normalised by code (「7」「CURE-7」「7號機」 → CURE-07);
- read tools all failed → the reply says nothing was read (never 「狀態正常」 from no data);
- numbers in the reply must come from tool results, the context or the message;
- a plant question answered with no tool gets one reminder, then code reads by the rules;
- a request to do something with no propose_plan gets one reminder, then code proposes (never claims one it didn't);
  with a plan selected, a request that names an order or machine the plan does not is such a new task, not a discussion;
- after a discussion the reply is code's, from its result (「已改成第 N 版，請重新核准」 or what it said); any other reply
  that says 已改／已更新／已擬計劃／已送 is replaced (demo walk-through 10-21: 「這份待核准計畫現在已更新」, event_id=null);
- greetings, 你是誰 and 謝謝 get the fixed replies without a model call; a model reply is plain text, at most 3 sentences.
Rule mode (or a model failure) runs the same tools by keywords."""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from warroom import factory as F
from warroom.agent import plant
from warroom.agent.ask import (
    _HELLO_RX,
    _NUM_RX,
    _THANKS_RX,
    ORDER_RX,
    machines_in,
    mock_plan,
    orders_in,
    summarize,
)
from warroom.agent.state_machine import _brief
from warroom.contracts import ASK_TOOLS, ToolName, ToolResult

MAX_STEPS = 6
MAX_CALLS_PER_STEP = 4
MAX_SOP_SEARCHES = 2   # real run 10-21: 「硫化機溫度掉下來要怎麼處理？」 → 6 sop_search with reworded queries, then no words
BUDGET_S = 60.0
MEMORY_TURNS = 8
READ_TOOLS = sorted(t.value for t in ASK_TOOLS)
PROPOSE = "propose_plan"
DISCUSS = "discuss_waiting_plan"
ALL_TOOLS = [*READ_TOOLS, PROPOSE, DISCUSS]

IDENTITY_REPLY = "我是戰情室的 agent。可以陪你看狀況、查 SOP；需要動到現場的事，我會擬計劃送你核准，核准後才執行。"
HELLO_REPLY = "你好，我是戰情室的 agent。要查狀況或安排事情，直接說就好。"
THANKS_REPLY = "不客氣，有事再叫我。"
GIBBERISH_REPLY = "這句我看不懂，可以再說一次嗎？例如：「#A-311 會不會晚交？」"
UNCLEAR_REPLY = "我不太確定你要我做什麼。你是想查狀況，還是要我安排一件事？例如：「7 號機現在怎樣？」"
NO_OBJECT = "要安排事情，我需要知道是哪台機台、哪張訂單、哪種原料或哪個班。"
NOTHING_READ = "我剛剛去查，但沒有讀到資料（{err}），所以不能判斷{what}現在的狀況。你可以換個說法，或直接看產線監控。"
# what discuss_waiting_plan can really do to a waiting plan (warroom/agent/talk.py: machine_unavailable, no_overtime,
# no_transfer, prefer_option). 「計劃改」 got 「調低到 118°C、加快降溫、縮短硫化時間」 (real run 2026-10-21): none of them exist
PLAN_CHANGES = "某台硫化機某時段不排（例如「9 號機今晚要保養」）、不要加班、不要跨組調人、改選 A 或 B 方案"
PLAN_EDIT_Q = f"這份計劃能改的是：{PLAN_CHANGES}；也可以直接核准或駁回。你要改哪一項？"
NOT_CHANGED = "計劃沒有改。" + PLAN_EDIT_Q
NOT_DONE = "我還沒有改任何東西，也沒有擬計劃。要安排的話，直接說要做什麼和對象，例如「請安排 9 號機明天保養」。"
NO_FACTS = "這個我沒有查到資料，不能給你數字。要我查哪台機台或哪張訂單？例如：「#A-311 會不會晚交？」"
NO_SUCH_ORDER = "沒有這張訂單：{what}，所以查不到交期。你要查的是哪一張？例如：「#A-311 會不會晚交？」"
KB_LOADING = "知識庫還在載入，請稍候再問。"
# process sense (page 11): a temperature drop is the anomaly; the plant never cures below 120 °C (SR-CURE-LOW-120)
PROCESS_RULE = "溫度下降是異常：不可以建議降溫、把溫度降到 120°C 以下或縮短硫化時間"

_IDENTITY_RX = re.compile(r"(?:你|妳|您)(?:是誰|是什麼|叫什麼|能做什麼|會做什麼|會什麼|可以做什麼)|介紹(?:一下)?(?:你|妳|您)自己|who are you", re.IGNORECASE)
_CJK_RX = re.compile(r"[㐀-鿿]")
_ORDER_RX = ORDER_RX
_MATERIAL_RX = re.compile(r"原料|橡膠|碳黑|鋼絲|尼龍|硫化劑|補料|缺料|進貨|庫存|採購")
_PEOPLE_RX = re.compile(r"人力|人手|調人|加班|請假|缺人|病假|夜班|早班|中班|排班|班表|組長")
_TASK_RX = re.compile(r"請|幫我|安排|通知|重排|調人|加班|補料|檢查|保養|處理|派|排程|停用|重新|改")
_QUESTION_RX = re.compile(r"[?？]|嗎|什麼|哪|多少|幾|怎樣|怎麼|如何|狀況|狀態|情況|是否|有沒有")
_CHANGE_RX = re.compile(r"保養|不要|不可|不能用|改成|改用|換成|延後|提前|今晚|明天|為什麼|為何|加班|調人|跨組")
_PRESS_ID_RX = re.compile(r"(?:CURE[-_ ]?)?0*(\d{1,2})(?:\s*號.*)?", re.IGNORECASE)
# a message that leans on the previous one (「那 8 號呢？」「剛剛那台」); only then do earlier messages ride along
_UNKNOWN_ID_RX = re.compile(r"unknown machine id '([^']+)'")
NO_SUCH_MACHINE = "廠裡沒有{what}（硫化機是 1–11 號，另有 A／B 線開煉機等），所以查不到。你要查的是哪一台？"
# a short message (「怎改」「好」「改」) or an answer to the agent's own question also leans on the previous turn
# (CJ 2026-10-21: 「計畫改」 → 「怎改」 got the self-introduction again: the model saw 「怎改」 with no conversation)
SHORT_FOLLOW_UP = 6
_ASKED_RX = re.compile(r"[?？][^。！!?？]*$|請告訴我|請問|要不要|還是")
# a concrete change to the selected plan (「降到120」「延後一小時」「換 8 號」), not a question about it. Only with Chinese
# words: the digits of 「vu04y94u.3s83uv,」 counted as an edit and the agent answered 「目標溫度已改成 122°C」 (real run)
_PLAN_EDIT_RX = re.compile(r"\d|降|升|調|改成|改到|改用|換|延後|提前|不要|不可|不能")
# 「計畫改」「怎改」「改一下」: wants a change but says none, so the agent asks what to change (no discuss tool offered:
# it called discuss_waiting_plan with nothing and answered 「目標溫度已改成 122°C」, real run 2026-10-21)
_VAGUE_EDIT_RX = re.compile(r"^(?:這個|這份)?(?:計[劃畫])?(?:要)?(?:怎麼?|如何)?(?:改|修改|調整)(?:一下|看看)?(?:計[劃畫])?[?？]?$")
_LAST_Q_RX = re.compile(r"[^。！!?？\n]*[?？]\s*$")
_GREETING = r"(?:早安|早上好|午安|下午好|晚安|晚上好|你好|您好|哈囉|嗨)"
# a short acknowledgement before a greeting or a self-introduction (「收到，主管。我是戰情室的 agent…」) goes with it;
# only before a real one: 「7 號機正常，我是根據讀值判斷的。」 keeps 「7 號機正常」
_INTRO_RX = re.compile(r"^\s*(?:(?:[^。！!？?\n]{1,8}?[，,。！!]\s*){1,2}?(?=" + _GREETING + r"|我是[^。！!\n]{0,30}?agent))?"
                       r"(?:" + _GREETING + r"[！!，,。～~\s]*)?(?:我是[^。！!\n]{0,30}?agent[^。！!\n]*[。！!]\s*)?",
                       re.IGNORECASE)
_FOLLOW_UP_RX = re.compile(r"^(那|還有|然後|另外|再)|呢[?？]?\s*$|剛剛|剛才|上面|前面|這台|那台|它|他們|同一")
# a reply that says something was changed or sent (「這份待核准計畫現在已更新」「已擬計劃送你核准」) is only true after a
# propose_plan or a discussion that replanned; otherwise code writes the reply (real run 2026-10-21, event_id=null)
_CLAIM_RX = re.compile(r"已(?:經)?(?:幫你|替你|為你)?(?:改(?!善)|更新|修改|調整|重排|擬|送)")
# a question that asks for no change (「現在狀況怎樣？」): 「AI 指揮官已擬好停用 7 號機的計劃」 describes the plant, it claims
# nothing for this turn (review 10-21: it was replaced by 「計劃沒有改。這份計劃能改的是…」)
_CHANGE_WORD_RX = re.compile(r"改|換|調|不要|不可|不能|加班|重排|安排|擬|送|降|升|延後|提前|保養|停")
# advice that breaks process sense for a temperature-drop plan (「調低到 118°C」「加快降溫」「縮短硫化時間」)
_UNSAFE_RX = re.compile(r"調低|降低.{0,6}溫|降溫|加快.{0,4}降|縮短.{0,6}(?:硫化|時間)|(?:硫化|時間).{0,4}縮短")
# 「（discuss_waiting_plan 回覆：…）」: a tool name in the reply
_TOOL_PAREN_RX = re.compile(r"\s*[（(][^（）()]*[A-Za-z]+_[A-Za-z_]+[^（）()]*[）)]")
_SENTENCE_RX = re.compile(r"[^。！!？?]+[。！!？?]*")
MAX_SENTENCES = 3

SYSTEM = f"""你是實心胎工廠戰情室的 agent，用繁體中文、像同事一樣自然地跟主管對話。你有工具可以用（tool calling）。
怎麼做：
- 打招呼、問你是誰、閒聊：直接回答，不用工具。問你是誰就說你是戰情室的 agent，能查狀況、查 SOP、擬計劃送核准。
- 問工廠的事（機台、訂單、班表、原料、SOP、怎麼處理）：先用讀取工具查，看完結果再回答。可以一次呼叫多個工具。
- 主管要你「做」一件會動到現場的事（重排、通知、調人、加班、補料、保養、停用），而且說得出對象：呼叫 propose_plan。
  它會擬計劃送主管核准，核准前什麼都不會執行；回覆時說「已擬計劃送你核准」，不要說已經做完。
- 主管在談目前選的待核准計劃（加條件、問為什麼）：呼叫 discuss_waiting_plan。
規則：
1. 你不能直接改任何東西，沒有任何機台或 PLC 的寫入工具。
2. 數字和狀態只能來自工具結果或 context；工具失敗或查不到，就照實說查不到，不可以說「正常」。
3. 回覆 2–3 句，短，純文字（不要 Markdown、不要 ** 粗體、不要條列、不要寫工具名稱）。不要打招呼、不要自我介紹（不要說「早上好」「你好，我是…」「收到，主管。我是…」），只有主管先打招呼或問你是誰時才說。只回應 message 這一句；earlier_messages 是主管前面說的話，只在接著前文時參考（例如「那 8 號呢？」）。
   有 previous_turn 時，message 是在接上一輪（supervisor 是主管上一句，you_asked 是你上一句最後問主管的問題）：把 message 當成對你那個問題的回答，往下接，不要重複你上一句。
5. 有 waiting_plan（計劃標題、要做的事、A／B 人力方案），而 message 只是「計劃改」「怎改」「改一下」這類話：直接問要改哪一項，只能列這份計劃真的能改的：{PLAN_CHANGES}；也可以直接核准或駁回。不要再介紹一次計劃。
   {PROCESS_RULE}，也不可以說你會改溫度。
6. 有 waiting_plan，主管給了具體改法（例如「9 號機今晚要保養」「不要加班」「改選 A」）：呼叫 discuss_waiting_plan，text 寫成完整的一句。改法不在第 5 條裡（例如改溫度）也交給它，它會照實說能不能做；計劃有沒有改，以它的結果為準。
7. 主管要做的是一件新的事（例如「#A-311 可能晚交，請重排保交期」，對象不在 waiting_plan 裡）：呼叫 propose_plan，不要當成在改這份計劃。
4. 主管的話很短或看不懂（例如「?」）：依 context 猜他可能想知道什麼，簡短回應並問一句確認，不要只說看不懂。
機台代號：CURE-01..CURE-11 = 1..11 號硫化機（一定要補零，例如 7 號 = CURE-07）；A-BIGOPEN = A 線大開煉機；其他依 context。"""


def _fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": desc,
                                             "parameters": {"type": "object", "properties": props, "required": required}}}


def tool_specs(with_discuss: bool) -> list[dict[str, Any]]:
    ids = {"type": "array", "items": {"type": "string"}}
    specs = [
        _fn("read_sensors", "機台目前讀值（溫度、壓力等）與最近幾分鐘趨勢", {"machine_ids": ids, "minutes": {"type": "integer"}},
            ["machine_ids"]),
        _fn("predict_anomaly", "預測機台某通道未來幾分鐘的值", {"machine_id": {"type": "string"},
            "channel": {"type": "string", "description": "例如 temp_c"}, "horizon_min": {"type": "integer"}}, ["machine_id"]),
        _fn("cure_formula", "依胎型與實際溫度算硫化時間", {"size": {"type": "string", "enum": ["S", "M", "L"]},
            "actual_temp_c": {"type": "number"}, "elapsed_h": {"type": "number"}}, ["size", "actual_temp_c"]),
        _fn("query_orders_shifts", "訂單交期、班表、人力缺口、每台硫化機的下一模", {"order_ids": ids, "press_ids": ids}, []),
        _fn("sop_search", "查知識庫（SOP）。問「怎麼處理」一定先查", {"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"]),
        _fn(PROPOSE, "主管要做一件會動到現場的事：擬計劃送主管核准（核准前不執行）", {"task": {"type": "string",
            "description": "要完成的事，一句話，含對象"}}, ["task"]),
    ]
    if with_discuss:
        specs.append(_fn(DISCUSS, "主管在談目前選的待核准計劃：加條件或問為什麼", {"text": {"type": "string"}}, ["text"]))
    return specs


def normalize_machine(mid: Any) -> Any:
    """「7」「07」「CURE-7」「cure7」「7號機」 → CURE-07; anything else is kept (the tool reports an unknown id)."""
    if not isinstance(mid, (str, int)):
        return mid
    s = str(mid).strip()
    if s.upper() in F.MACHINE_BY_ID:
        return s.upper()
    m = _PRESS_ID_RX.fullmatch(s)
    if m and f"CURE-{int(m.group(1)):02d}" in F.MACHINE_BY_ID:
        return f"CURE-{int(m.group(1)):02d}"
    found = machines_in(s)
    return found[0] if found else s


def normalize_args(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args or {})
    for k in ("machine_ids", "press_ids"):
        if isinstance(out.get(k), list):
            out[k] = [normalize_machine(x) for x in out[k]]
        elif isinstance(out.get(k), (str, int)):
            out[k] = [normalize_machine(out[k])]
    if "machine_id" in out:
        out["machine_id"] = normalize_machine(out["machine_id"])
    if isinstance(out.get("order_ids"), (str, list)):
        ids = out["order_ids"] if isinstance(out["order_ids"], list) else [out["order_ids"]]
        # 「A-311」「#a311」 → #A-311 (the plant's spelling); anything else is kept and reported as no such order
        out["order_ids"] = [(orders_in(s if str(s).startswith("#") else f"#{s}") or [s])[0] for s in map(str, ids)]
    return out


def is_gibberish(text: str) -> bool:
    """Nothing recognisable: no Chinese, no machine / order id, no known English greeting (e.g. Zhuyin keys typed
    with the IME off: 「vu04y94u.3s83uv,」)."""
    t = text.strip()
    if not t:
        return True
    if _CJK_RX.search(t) or _ORDER_RX.search(t) or machines_in(t) or _HELLO_RX.match(t) or _THANKS_RX.match(t):
        return False
    words = re.findall(r"[A-Za-z]{2,}", t)
    known = {"ok", "okay", "hi", "hello", "hey", "thanks", "thx", "sop", "cure", "who", "are", "you", "help"}
    return not words or not any(w.lower() in known for w in words)


def has_object(text: str) -> bool:
    return bool(machines_in(text) or _ORDER_RX.search(text) or _MATERIAL_RX.search(text) or _PEOPLE_RX.search(text)
                or re.search(r"\d{1,2}\s*號", text))


def wants_task(text: str) -> bool:
    """「請／幫我…」 with a concrete object, and not a question."""
    t = text.strip()
    return bool(re.search(r"請|幫我|安排|通知|重排|調人|加班|補料|派", t)) and has_object(t) and not re.search(r"[?？]\s*$", t)


def wants_lookup(text: str) -> bool:
    """A question about the plant (「大開煉機過熱怎麼處理」「7 號機現在怎樣」): answered from a read tool, never from memory."""
    t = text.strip()
    if _IDENTITY_RX.search(t) or _HELLO_RX.match(t) or _THANKS_RX.match(t) or wants_task(t):
        return False
    return bool(re.search(r"SOP|怎麼處理|處置", t) or (_QUESTION_RX.search(t) and has_object(t)))


def names_other(text: str, plan: str | None) -> bool:
    """The message names an order or a machine the selected plan does not (「#M-201 可能晚交，請重排保交期」 while the
    7 號機 card is selected): it is about something else, not this plan."""
    if not plan:
        return False
    mine = set(machines_in(text)) | set(orders_in(text))
    return bool(mine - (set(machines_in(plan)) | set(orders_in(plan))))


def new_task(text: str, plan: str | None) -> bool:
    """A new task while a plan is selected: 「請…」 with an object the plan does not name, and not one of the plan
    changes discuss can encode (「9 號機今晚要保養」 stays a change to this plan)."""
    from warroom.agent.talk import rule_intent
    t = text.strip()
    return plan is not None and wants_task(t) and names_other(t, plan) and rule_intent(t) != "change"


def plan_edit(text: str) -> bool:
    """A concrete change to the selected plan (「降到120」「不要加班」), in Chinese words, and not a question."""
    t = text.strip()
    return bool(_PLAN_EDIT_RX.search(t) and _CJK_RX.search(t) and not _QUESTION_RX.search(t)
                and not re.search(r"[?？]\s*$", t))


def tidy(reply: str, keep_intro: bool) -> str:
    """The model's words as one short plain reply: no Markdown bold, no tool names in brackets, no self-introduction
    after 「收到，主管。」, at most MAX_SENTENCES sentences (real runs 10-21: 「**7 號機**」「（discuss_waiting_plan 回覆：…）」,
    four paragraphs)."""
    r = reply.replace("**", "").replace("__", "")
    r = _TOOL_PAREN_RX.sub("", r)
    r = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.、)])\s+", "", r)                # list markers
    r = re.sub(r"(?<=[。！!？?：:；;，,])\s*\n+\s*", "", r)
    r = re.sub(r"\s*\n+\s*", "；", r.strip())
    if not keep_intro:
        r = _INTRO_RX.sub("", r, count=1).strip() or r
    parts = _SENTENCE_RX.findall(r)
    if len(parts) > MAX_SENTENCES:
        r = "".join(parts[:MAX_SENTENCES])
    return r.strip()


def ungrounded(reply: str, source: str) -> list[str]:
    known = set(_NUM_RX.findall(source))
    known |= {n.split(".")[0] for n in known}
    return [n for n in _NUM_RX.findall(reply) if n not in known]


class MainAgent:
    """`run_tool(tool, args, call_id)` runs a read tool through the Registry (audited by the caller);
    `propose(task)` opens a supervisor-command event and returns {"event_id"}; `discuss(text)` talks about the
    selected plan."""

    def __init__(self, settings: Any = None, client: Any = None):
        self.settings = settings
        self.client = client
        self.server_kind = "openai"
        self.llm = settings is not None and str(getattr(settings, "llm_mode", "mock")) == "llm"
        if self.llm and client is None:
            from openai import AsyncOpenAI

            from warroom.agent.commander import detect_server_kind
            self.client = AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key_effective,
                                      timeout=min(25.0, settings.llm_timeout_s), max_retries=0)
            self.server_kind = detect_server_kind(settings.base_url)
        self.memory: deque[tuple[str, str]] = deque(maxlen=MEMORY_TURNS)
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------- one turn
    async def turn(self, text: str, *, context: dict[str, Any], waiting_plan: str | None,
                   run_tool: Callable[[str, dict[str, Any], str], Awaitable[ToolResult]],
                   propose: Callable[[str], Awaitable[dict[str, Any]]],
                   discuss: Callable[[str], Awaitable[dict[str, Any]]] | None,
                   slot: Callable[[], Any] | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        tid = f"MAIN-{next(self._ids):04d}"
        steps: list[dict[str, Any]] = []
        out: dict[str, Any] = {"id": tid, "reply": "", "steps": steps, "event_id": None, "by": "rules",
                               "fell_back": False, "error": None, "intent": None}
        # with a model every message goes to it (CJ 2026-10-21: 「應該是直接詢問大模型並且請他回答跟操作」 — 「?」 got the
        # canned 「這句我看不懂」); the canned line is only for rule mode
        if not (self.llm and self.client is not None) and is_gibberish(text):
            out.update(reply=GIBBERISH_REPLY, intent="unclear")
            return self._done(out, text, t0)
        if self.llm and self.client is not None and not self._canned(text):
            try:
                await asyncio.wait_for(self._model_loop(text, context, waiting_plan, out, run_tool, propose, discuss, tid,
                                                        slot), BUDGET_S)
                out["by"] = "model"
                return self._done(out, text, t0)
            except Exception as e:  # noqa: BLE001 - the rules take the turn; the reply says so
                out.update(fell_back=True, error=f"{type(e).__name__}: {e}"[:200])
                if out["event_id"]:                       # a plan was already proposed: never propose twice
                    out["reply"] = out["reply"] or self._propose_reply(steps)
                    return self._done(out, text, t0)
                steps.clear()
                out.pop("unknown", None)
                out.pop("unknown_orders", None)
        await self._rule_turn(text, context, waiting_plan, out, run_tool, propose, discuss, tid)
        return self._done(out, text, t0)

    def _canned(self, text: str) -> bool:
        """A greeting, 你是誰 or a thanks gets the fixed reply without the model: they took 14 s on the Spark while the
        commander was planning (real run 2026-10-21). 「好」「收到」 answering the agent's own question still go to it."""
        t = text.strip()
        prev = self.memory[-1] if self.memory else None
        asked = prev is not None and bool(_ASKED_RX.search(prev[1]))
        return bool((_IDENTITY_RX.search(t) and not has_object(t)) or _HELLO_RX.match(t)
                    or (_THANKS_RX.match(t) and not asked))

    def clear(self) -> None:
        """Forget the conversation (a scenario was loaded or reset: 「你好」 → 「您剛才說『降到 120』…」 from the
        previous rehearsal). Turn ids keep counting so the console never mixes an old turn with a new one."""
        self.memory.clear()

    def _done(self, out: dict[str, Any], text: str, t0: float) -> dict[str, Any]:
        out["reply"] = (out["reply"] or UNCLEAR_REPLY).strip()
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self.memory.append((text, out["reply"]))
        return out

    # ---------------------------------------------------------------- tools (shared by model and rules)
    async def _tool(self, name: str, args: dict[str, Any], text: str, out: dict[str, Any], tid: str,
                    run_tool, propose, discuss) -> str:
        steps = out["steps"]
        if name == PROPOSE:
            task = str((args or {}).get("task") or text)
            if is_gibberish(text) or (not has_object(task) and not has_object(text)):   # never a task from no words
                steps.append({"tool": name, "args": args, "ok": False, "summary": "沒有具體對象，沒有擬計劃", "label": "沒有擬計劃"})
                return NO_OBJECT
            if out["event_id"]:
                return "這一輪已經擬過一份計劃了。"
            d = await propose(task)
            out["event_id"] = d.get("event_id")
            summ = f"已開任務 {d.get('event_id')}，AI 指揮官擬好計劃會送主管核准，核准後才執行"
            steps.append({"tool": name, "args": {"task": task}, "ok": True, "summary": summ, "label": "擬計劃送核准",
                          "event_id": d.get("event_id")})
            return summ
        if name == DISCUSS:
            if discuss is None:
                steps.append({"tool": name, "args": args, "ok": False, "summary": "目前沒有選待核准的計劃", "label": "沒有計劃可討論"})
                return "目前沒有選待核准的計劃。"
            if not _CJK_RX.search(text):
                # 「vu04y94u.3s83uv,」: the model wrote its own change from no words; nothing the supervisor said
                steps.append({"tool": name, "args": args, "ok": False, "summary": "主管這句看不懂，沒有拿來改計劃",
                              "label": "沒有改計劃"})
                return "主管這句看不懂，不能拿來改計劃。請問主管要改哪一項。"
            r = await discuss(str((args or {}).get("text") or text))
            reply = str(r.get("reply") or "")
            approval = r.get("approval") or {}
            steps.append({"tool": name, "args": args, "ok": True, "summary": reply[:200], "label": "討論計劃",
                          "reply": reply, "replanned": bool(r.get("replanned")),
                          "version": approval.get("version") if isinstance(approval, dict) else None,
                          "chips": [str(c.get("label")) for c in r.get("constraints") or [] if isinstance(c, dict) and c.get("label")]})
            return reply
        if name not in READ_TOOLS:
            steps.append({"tool": name, "args": args, "ok": False, "summary": "不允許：沒有寫入工具", "label": f"攔下 {name}"})
            return f"{name} 不能用：只能查資料或擬計劃送核准。"
        args = normalize_args(args)
        res = await run_tool(name, args, f"{tid}:T{len(steps) + 1}")
        err = str(res.error or "")
        if name == "sop_search" and "timeout" in err.lower():
            err = "知識庫還在載入"                 # 「timeout after 10 s」 while the knowledge base loads, in English
        summ = summarize(ToolName(name), res.data, args) if res.ok else f"失敗：{err}"
        steps.append({"tool": name, "args": args, "ok": res.ok, "summary": summ, "label": _label(name, args, res)})
        if res.ok and name == "query_orders_shifts" and args.get("order_ids"):
            have = {str(o.get("id")) for o in (res.data or {}).get("orders") or [] if isinstance(o, dict)}
            missing = [o for o in args["order_ids"] if o not in have]
            if missing:                          # 「#M-201」: say there is no such order, never another order's due date
                out.setdefault("unknown_orders", []).extend(missing)
                return f"沒有這張訂單：{'、'.join(missing)}。直接告訴主管沒有這張訂單，不要改查別的。"
        bad = None if res.ok else _UNKNOWN_ID_RX.search(str(res.error or ""))
        if bad:
            # the asked machine does not exist: say so (「15 號機」 → it read 11, 10, 9, 8, 7 號 instead)
            out.setdefault("unknown", []).append(bad.group(1))
            return f"沒有這台機台：{bad.group(1)}（硫化機只有 CURE-01..CURE-11）。直接告訴主管沒有這台，不要改查別台。"
        return summ

    @staticmethod
    def _propose_reply(steps: list[dict[str, Any]]) -> str:
        d = next((s for s in steps if s["tool"] == PROPOSE and s["ok"]), None)
        return "好，已擬計劃送你核准（AI 指揮官正在排），核准後才執行。" if d else UNCLEAR_REPLY

    @staticmethod
    def _discuss_reply(steps: list[dict[str, Any]]) -> str | None:
        """The reply after a discussion is code's, from its result: a new card → 「已改成第 N 版，請重新核准」; no new
        card → what the discussion said (「計劃沒有改」), never the model's 「這份待核准計畫現在已更新」."""
        ds = [s for s in steps if s["tool"] == DISCUSS]
        if not ds:
            return None
        new = next((s for s in reversed(ds) if s.get("replanned") and s.get("version")), None)
        if new is not None:
            chips = "、".join(new.get("chips") or [])
            return f"好，已改成第 {new['version']} 版{f'（{chips}）' if chips else ''}，請重新核准。"
        said = next((s.get("reply") or s["summary"] for s in reversed(ds) if s["ok"]), None)
        return said or NOT_CHANGED

    @staticmethod
    def _unknown_reply(out: dict[str, Any]) -> str | None:
        """The asked machine or order does not exist: code answers (「15 號機」「#M-201」)."""
        ids = list(dict.fromkeys(out.get("unknown") or []))
        if ids:
            return NO_SUCH_MACHINE.format(what="、".join(_press_name(i) for i in ids))
        orders = list(dict.fromkeys(out.get("unknown_orders") or []))
        return NO_SUCH_ORDER.format(what="、".join(orders)) if orders else None

    @staticmethod
    def _nothing_read(steps: list[dict[str, Any]]) -> str | None:
        """Read tools were called and every one failed: the reply must say so (CJ 2026-10-21: 「7 號機」 → 「狀態正常」
        after two failed reads)."""
        reads = [s for s in steps if s["tool"] in READ_TOOLS]
        if not reads or any(s["ok"] for s in reads):
            return None
        if all(s["tool"] == "sop_search" and "知識庫還在載入" in s["summary"] for s in reads):
            return KB_LOADING
        ids = [m for s in reads for m in (s["args"].get("machine_ids") or [s["args"].get("machine_id")]) if m]
        from warroom.agent.narrate import machine_label
        what = "、".join(dict.fromkeys(machine_label(m) if m in F.MACHINE_BY_ID else str(m) for m in ids)) or "這件事"
        err = reads[-1]["summary"].removeprefix("失敗：")[:60]
        return NOTHING_READ.format(err=err, what=what)

    # ---------------------------------------------------------------- the model loop
    async def _model_loop(self, text, context, waiting_plan, out, run_tool, propose, discuss, tid, slot=None) -> None:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM + plant.section()}]
        # earlier turns ride along as reference inside the one user message, the supervisor's words only. Real runs
        # (2026-10-21): replayed as chat turns, Nemotron answered the previous topic (「你好」 → 7 號機讀值); with its own
        # answers alongside, it copied them (「大開煉機過熱怎麼處理」 → the greeting again). Always sent, it also re-answered
        # old topics (「15 號機現在怎樣？」 → read 7 號、11 號 and two SOPs), so only a follow-up message gets them.
        # A short message or an answer to its own question also gets the last turn: the supervisor's words and only the
        # question the agent ended on (its whole reply alongside was copied back word for word, real run 2026-10-21).
        t = text.strip()
        prev = self.memory[-1] if self.memory else None
        follow = prev is not None and bool(_FOLLOW_UP_RX.search(t) or len(t) <= SHORT_FOLLOW_UP or _ASKED_RX.search(prev[1]))
        earlier = [q for q, _a in self.memory] if follow else []
        ctx = {"message": text, "earlier_messages": earlier, "context": context, "waiting_plan": waiting_plan}
        if follow and prev is not None:
            asked = _LAST_Q_RX.search(prev[1].strip())
            ctx["previous_turn"] = {"supervisor": prev[0], "you_asked": asked.group(0).strip() if asked else None}
        msgs.append({"role": "user", "content": json.dumps(ctx, ensure_ascii=False, default=str)})
        from warroom.agent.commander import thinking_off
        extra = thinking_off(self.server_kind)
        vague = waiting_plan is not None and bool(_VAGUE_EDIT_RX.match(t))
        # a new task while a card is selected (「#M-201 可能晚交，請重排保交期」 with the 7 號機 card): not a discussion of
        # that plan, so the propose reminder and the code proposal stay on and discuss is not offered
        fresh = new_task(t, waiting_plan)
        other = waiting_plan is not None and names_other(t, waiting_plan)
        tools = tool_specs(with_discuss=discuss is not None and not vague and not fresh and bool(_CJK_RX.search(t)))
        # numbers the reply may use: the message, what rode along with it, the context, the plan and the tool results
        source = " ".join([text, *earlier, prev[0] if follow and prev is not None else "",
                           json.dumps(context, ensure_ascii=False, default=str), waiting_plan or "", PROCESS_RULE])
        nudged = waiting_plan is not None and not fresh   # a message about the selected plan is a discussion, not a new task
        looked = waiting_plan is not None and not other
        called: set[tuple[str, str]] = set()
        edits = discuss is not None and not fresh and plan_edit(t)
        editing = vague or edits                      # the message is about changing the selected plan
        for i in range(MAX_STEPS + 1):
            last = i >= MAX_STEPS
            # one model slot per call (never across a tool: a discussion calls the commander, which needs a slot too)
            async with (slot() if slot else contextlib.nullcontext()):
                r = await self.client.chat.completions.create(
                    model=self.settings.model, temperature=0.0, max_tokens=500, messages=msgs,
                    **({} if last else {"tools": tools, "tool_choice": "auto"}),
                    **({"extra_body": extra} if extra else {}))
            m = r.choices[0].message
            calls = list(m.tool_calls or [])[:MAX_CALLS_PER_STEP]
            if calls and not last:
                msgs.append({"role": "assistant", "content": m.content or "",
                             "tool_calls": [{"id": c.id, "type": "function",
                                             "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                                            for c in calls]})
                for c in calls:
                    try:
                        args = json.loads(c.function.arguments or "{}")
                    except ValueError:
                        args = {}
                    key = (c.function.name, json.dumps(normalize_args(args) if isinstance(args, dict) else {}, sort_keys=True,
                                                       ensure_ascii=False))
                    if key in called:                 # the same call again: the result is already above
                        result = "剛查過了，結果在上面。"
                    elif c.function.name == "sop_search" and sum(k[0] == "sop_search" for k in called) >= MAX_SOP_SEARCHES:
                        # reworded searches return the same SOP: stop the loop, answer from what is above
                        result = "SOP 這一輪已經查過，結果在上面。請直接依上面的 SOP 回覆主管（2–3 句），不要再查。"
                    else:
                        called.add(key)
                        result = await self._tool(c.function.name, args if isinstance(args, dict) else {}, text, out, tid,
                                                  run_tool, propose, discuss)
                        source += " " + result
                    msgs.append({"role": "tool", "tool_call_id": c.id, "content": result})
                unknown = self._unknown_reply(out)
                if unknown and not out["event_id"]:
                    # the machine or order asked about does not exist: code answers, the model does not look elsewhere
                    out.update(reply=unknown, error=f"unknown {out.get('unknown') or out.get('unknown_orders')}")
                    return
                continue
            reply = (m.content or "").strip()
            if not reply and out["steps"]:            # no words after the tools: the results speak
                reply, out["error"] = self._results_reply(out["steps"]) or self._propose_reply(out["steps"]), "no reply after tools"
            if not reply:
                raise ValueError("empty reply")
            if not out["steps"] and wants_lookup(text) and (waiting_plan is None or other):
                read = [st for st in mock_plan(text, context).steps if st.tool in READ_TOOLS]
                if not looked and read and not last:
                    # a plant question answered without looking: remind the model once which tool answers it
                    looked = True
                    msgs += [{"role": "assistant", "content": reply},
                             {"role": "user", "content": f"主管在問工廠的事，不是閒聊。先呼叫 {read[0].tool} 查資料，再用 2–3 句回覆。"}]
                    continue
                if read:                              # still nothing read: code reads by the rules, the results speak
                    for st in read[:MAX_STEPS]:
                        await self._tool(st.tool, st.args, text, out, tid, run_tool, propose, discuss)
                    out.update(reply=self._unknown_reply(out) or self._nothing_read(out["steps"])
                               or self._results_reply(out["steps"]) or NO_FACTS,
                               fell_back=True, error="model answered a plant question without a tool; read by rules")
                    return
            if not nudged and not out["event_id"] and wants_task(text) and not last:
                # the supervisor asked for something to be done: remind the model once that propose_plan does that
                nudged = True
                msgs += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "主管是要你「做」這件事，不只是查。請呼叫 propose_plan 擬計劃送核准，再回覆。"}]
                continue
            if not out["event_id"] and wants_task(text) and (waiting_plan is None or fresh):
                # asked twice, still no propose_plan: code proposes (never claim a plan that does not exist)
                await self._tool(PROPOSE, {"task": text}, text, out, tid, run_tool, propose, discuss)
                out.update(reply=self._propose_reply(out["steps"]), fell_back=True,
                           error="model did not call propose_plan; proposed by rules")
                return
            if edits and not last and not any(st["tool"] == DISCUSS for st in out["steps"]):
                # a concrete change to the waiting plan answered with words only (「降到120」 → 「您覺得如何？」): remind once
                edits = False
                msgs += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": "主管給了這份待核准計劃的具體改法。請呼叫 discuss_waiting_plan，text 寫成完整一句，再回覆。"}]
                continue
            code = self._code_reply(out, reply, text, source, waiting_plan, editing)
            if code is None and prev is not None and tidy(reply, True) == prev[1] and not last:
                # the same answer as last turn: the supervisor said something new, answer that
                msgs += [{"role": "assistant", "content": reply},
                         {"role": "user", "content": f"這跟你上一句一模一樣。主管這次說的是「{t}」，針對這句回應，不要重複。"}]
                continue
            # no greeting / self-introduction unless asked (it opened every reply with 「早上好！我是戰情室的 agent」)
            out["reply"] = code if code is not None else tidy(reply, keep_intro=bool(_IDENTITY_RX.search(t) or _HELLO_RX.match(t)))
            return
        raise TimeoutError("too many tool steps")

    def _code_reply(self, out: dict[str, Any], reply: str, text: str, source: str, waiting_plan: str | None,
                    about_edit: bool) -> str | None:
        """The reply code writes instead of the model's, or None to keep the model's (tidied) words. The model is inside
        the loop, not in charge of the facts: what was proposed, what a discussion changed, what was read."""
        steps = out["steps"]
        if out["event_id"]:                           # the proposal line is code's (never 「已完成」 before approval)
            return self._propose_reply(steps)
        said = self._discuss_reply(steps)
        if said is not None:                          # a discussion ran: its result says whether the plan changed
            return said
        nothing = self._nothing_read(steps)
        if nothing:                                   # every read failed: never a status from no data
            out["error"] = "all read tools failed"
            return nothing
        words = tidy(reply, True)
        bad = ungrounded(words, source)
        if bad:                                       # a number the tools did not give: the results speak instead
            out["error"] = f"ungrounded numbers {bad}"
            fixed = self._results_reply(steps) or (PLAN_EDIT_Q if waiting_plan and about_edit else "")
            if fixed:
                return fixed
            # nothing read and not a plan change: the words stay, as before (「現在狀況怎樣？」 → 「7 號機 122.3°C…」 from
            # the context: 「7」 is CURE-07, 「1 台」 a count; 「這個我沒有查到資料」 there was not true, review 10-21)
        if waiting_plan and about_edit and _UNSAFE_RX.search(words):
            out["error"] = "advice outside what the plan can change"
            return PLAN_EDIT_Q
        t = text.strip()
        asks_only = bool(_QUESTION_RX.search(t)) and not _CHANGE_WORD_RX.search(t) and not is_gibberish(t)
        if _CLAIM_RX.search(words) and not asks_only:  # 「已改／已更新／已擬計劃／已送」 with nothing proposed or replanned
            out["error"] = "claimed a change that was not made"
            return GIBBERISH_REPLY if is_gibberish(text) else NOT_CHANGED if waiting_plan else NOT_DONE
        return None

    @staticmethod
    def _results_reply(steps: list[dict[str, Any]]) -> str:
        """Short: a SOP is named, not pasted (the full lines stay in the reply's tool line). A discussion is not a read:
        its reply is used as it is (it got 「查到：」 in front)."""
        ok = []
        for s in steps:
            if not (s["ok"] and s["summary"]) or s["tool"] in (PROPOSE, DISCUSS):
                continue
            sops = re.findall(r"(SOP-[A-Z]+-\d+)〈([^〉]+)〉", s["summary"]) if s["tool"] == "sop_search" else []
            ok.append("請照 " + "、".join(f"{a}〈{b}〉" for a, b in sops[:2]) + " 處理" if sops else s["summary"][:90].rstrip("。；，、 "))
        ok = list(dict.fromkeys(ok))                # the same SOP found by several searches is named once
        return ("查到：" + "；".join(ok)[:220].rstrip("。；，、 ") + "。") if ok else ""

    # ---------------------------------------------------------------- rules (rule mode, or the model failed)
    async def _rule_turn(self, text, context, waiting_plan, out, run_tool, propose, discuss, tid) -> None:
        t = text.strip()
        if _IDENTITY_RX.search(t):
            out.update(reply=IDENTITY_REPLY, intent="identity")
            return
        if _HELLO_RX.match(t):
            out.update(reply=HELLO_REPLY, intent="chat")
            return
        if _THANKS_RX.match(t):
            out.update(reply=THANKS_REPLY, intent="chat")
            return
        asks = bool(re.search(r"[?？]\s*$", t))
        if waiting_plan and discuss is not None and not new_task(t, waiting_plan):
            if _VAGUE_EDIT_RX.match(t):
                # 「計劃改」「怎改」: ask which change, listing what this plan can change (it got UNCLEAR_REPLY with an
                # unrelated example)
                out.update(reply=PLAN_EDIT_Q, intent="change_plan")
                return
            if re.search(r"為什麼|為何", t) or (_CHANGE_RX.search(t) and not asks) or plan_edit(t):
                out["intent"] = "change_plan"
                await self._tool(DISCUSS, {"text": t}, t, out, tid, run_tool, propose, discuss)
                out["reply"] = self._discuss_reply(out["steps"]) or NOT_CHANGED
                return
        imperative = bool(re.search(r"請|幫我|安排|通知|重排|調人|加班|補料|派", t))
        if _TASK_RX.search(t) and not asks and has_object(t) and (imperative or not _QUESTION_RX.search(t)):
            out["intent"] = "new_task"
            await self._tool(PROPOSE, {"task": t}, t, out, tid, run_tool, propose, discuss)
            out["reply"] = self._propose_reply(out["steps"])
            return
        if _QUESTION_RX.search(t) or asks or re.search(r"SOP|怎麼處理|處置", t):
            out["intent"] = "question"
            plan = mock_plan(t, context)
            for s in plan.steps[:MAX_STEPS]:
                await self._tool(s.tool, s.args, t, out, tid, run_tool, propose, discuss)
            out["reply"] = (self._unknown_reply(out) or self._nothing_read(out["steps"]) or self._results_reply(out["steps"])
                            or "這個我手上的資料查不到。你想知道哪台機台或哪張訂單？")
            return
        out.update(reply=UNCLEAR_REPLY, intent="unclear")


def _press_name(mid: str) -> str:
    """「CURE-15」 → 「15 號機」 for an id that does not exist (machine_label only knows real machines)."""
    m = _PRESS_ID_RX.fullmatch(mid)
    return f"{int(m.group(1))} 號機" if m else mid


def _label(tool: str, args: dict[str, Any], res: ToolResult) -> str:
    """The one grey line a tool call becomes under the reply: 「查了 SOP-OM-01」「看了 7 號機讀值」."""
    if tool == "sop_search":
        hits = ((res.data or {}).get("hits") or []) if res.ok else []
        return f"查了 {hits[0]['sop_id']}" if hits else "查了 SOP"
    if tool == "read_sensors":
        ids = args.get("machine_ids") or []
        from warroom.agent.narrate import machine_label
        return f"看了{'、'.join(machine_label(m) for m in ids[:2])}讀值" if ids else "看了讀值"
    if tool == "query_orders_shifts":
        return "查了訂單與班表"
    if tool == "predict_anomaly":
        return "做了預測"
    if tool == "cure_formula":
        return "算了硫化時間"
    return tool


def brief(res: ToolResult) -> dict[str, Any]:
    return _brief(res.data or {})
