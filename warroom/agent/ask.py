"""問戰情室 (ask the war room): the supervisor's read-only question path.

It reuses the event harness instead of adding a second agent: the same model, the same allow-listed
tools through the same Registry, the same append-only audit log. Three steps, two of them code:

  1. PLAN    the model (or the mock rules) returns an AskPlan: up to ASK_MAX_STEPS read steps.
  2. RUN     code runs each step that is open for questions (ASK_TOOLS). Anything else (a write tool,
             solve_schedule, a name off the allow-list, a step past the limit) is refused and logged
             INTERCEPTED. Every call and result is logged like an event's.
  3. ANSWER  the model writes 1-3 sentences from the tool results only. Code checks it: a number that
             is not in the results replaces the answer with the results themselves (template), and so
             does a model failure. A change request ("停掉", "重排"...) gets the results only.

Nothing here can write: ASK_TOOLS holds no write tool, and the API refuses the question path when the
LAN is down for anyone but the on-site seat, like approvals."""
from __future__ import annotations

import itertools
import json
import re
import time
from collections.abc import Callable
from typing import Any

from warroom import factory as F
from warroom.agent import budget, order_ask, plant
from warroom.agent.convo import AskMemory, ConvContext
from warroom.agent.commander import detect_server_kind, thinking_off
from warroom.agent.narrate import fmt_num, machine_label
from warroom.agent.state_machine import _brief
from warroom.agent.strict import failure_cause, strict_reason
from warroom.config import Settings
from warroom.contracts import (
    ASK_MAX_STEPS,
    ASK_TOOLS,
    WRITE_TOOLS,
    AskAnswer,
    AskPlan,
    AskStep,
    AskStepResult,
    AuditKind,
    AuditLog,
    LLMMode,
    ToolCall,
    ToolName,
    ToolResult,
)

ContextFn = Callable[[], dict[str, Any]]

_CATALOGUE = """\
- read_sensors {machine_ids: [id], minutes: float=30} 讀感測器：現值、30 分最低、趨勢
- predict_anomaly {machine_id, channel='temp_c', horizon_min=30} 時序預測：未來的穩定值與區間
- cure_formula {press_id} Arrhenius 硫化時間：尺寸、已硫化時數與現在溫度由程式從該硫化機現場讀取，只需給機台代號
- query_orders_shifts {order_ids?, press_ids?} 訂單交期、班表、人力缺口、指定硫化機的下一模；也回傳今日訂單（今日生產、今日交貨、遲交風險）
- sop_search {query, k=3} SOP 知識庫"""

_MACHINES = "；".join(
    [f"{m.id}={machine_label(m.id)}" for m in F.MACHINES if m.line is not F.Line.CURE]
    + [f"CURE-01..CURE-11 = 1..11 號硫化機（{F.ML_RANGE} 號可做中大胎）"])

PLAN_SYSTEM = f"""你是實心胎工廠戰情室的查詢助理。主管（跟你說話的人）傳來一句話。你先自己判斷這句話是什麼（intent），
再決定要查哪些資料；你不寫答案（答案由下一步依查詢結果寫），也不做任何變更。沒有程式先幫你分類，由你決定。
只輸出一個 JSON 物件：{{"intent": "...", "reply": "...", "steps": [{{"tool": "...", "args": {{...}}}}], "rationale": "一句話"}}。
intent：
- chitchat：打招呼、道謝、閒聊。steps 留空陣列；reply 寫一句簡短自然的回覆，稱主管「你」，不要提任何數字。
- question：問這座工廠的事（機台、訂單、交期、人力、班表、加班、出貨、核准、事件、通知、怎麼處理）。steps 列要查的唯讀工具；
  reply 留空字串。問核准、事件進度時 steps 可以留空（程式會附上進行中的事件）。
- change_request：要你停機、關機、重排、調人、加班、通知、核准、改設定這類「做」的事。你沒有任何寫入、排程或通知的權限：
  steps 只列能讓主管看清現況的唯讀查詢（例如停 7 號機 → read_sensors CURE-07），reply 留空；回覆會由程式提醒走事件核准流程。
- unclear：看不懂或不知道要查什麼。steps 留空陣列；reply 問一個簡短的確認問題（例如「你是要看 7 號機的溫度，還是它的排程？」）。
規則：
1. 最多 {ASK_MAX_STEPS} 步，只能用下列唯讀工具。
2. 機台用代號（例如 CURE-07）。問題沒指名機台時，查「目前異常機台」。
3. 問到人力、班表、加班、缺誰、交期、下一模、某張訂單（例如 SO-004、#A-311）→ query_orders_shifts（某張訂單就帶 order_ids）；
   問到怎麼處理、過熱怎麼辦 → sop_search，再加 read_sensors 讀該機台現況。
4. 只查回答這個問題需要的資料，不要順便多查：問溫度、讀值 → read_sensors；問會不會再掉、接下來 → read_sensors 加
   predict_anomaly；問硫化要多久、會延長多久 → cure_formula（只給 press_id）。問溫度不要去查排程或硫化時數。
5. 有 previous_question（同一段對話的上一題，previous_steps 是它查過的步驟）且這題是接續（例如「那 8 號呢？」）時，
   照上一題的意圖與步驟只換對象，不要擴大查詢；不是接續就不要理它。
工具：
{_CATALOGUE}
機台代號：{_MACHINES}"""

ANSWER_SYSTEM = """你是實心胎工廠戰情室的查詢助理。只根據「查詢結果」用繁體中文回答主管的問題，最多 3 句。
1. 第一句就直接回答主管問的那件事，並說出主詞（哪台機台、哪張訂單）：問溫度就答溫度，問何時完成就答預計完成，問有沒有就先答有或沒有，問怎麼處理就先講 SOP 的處置步驟。
2. 只用和問題有關的查詢結果；和問題無關的結果（例如問溫度時的硫化時數、排程、人力）不要提。
3. 數字照抄查詢結果，不估算、不補充結果裡沒有的事（沒有讀值就不要說溫度高低或正不正常）；不要加「目前無…資訊」「如需…請…」這類填充句。
4. 結果裡的「無」也是答案（「人力缺口：無」就是沒有缺人）。被問的資料真的不在結果裡時，具體說查不到什麼（例如「查詢結果裡沒有核准卡的資料」），不要只回「查不到」。
5. 你不能做任何變更；主管要求變更時，請他走事件核准流程。"""


INTENTS = ("chitchat", "question", "change_request", "unclear")


def ask_plan_schema() -> dict[str, Any]:
    """AskPlan plus the model's own reading of the message (issue #195: no keyword router in front of it)."""
    s = AskPlan.model_json_schema()
    s["properties"] = {"intent": {"type": "string", "enum": list(INTENTS)},
                       "reply": {"type": "string", "description": "chitchat / unclear only: the one-line reply"},
                       **s.get("properties", {})}
    s["required"] = ["intent", "steps"]
    return s


# ------------------------------------------------------------------ small talk / unclear (no tool call)
_HELLO_RX = re.compile(r"^(哈[摟囉嘍喽啰]|hello|hi|hey|嗨|你好|您好|妳好|大家好|早安|午安|晚安|早|安安|在嗎|在不在|有人嗎|哈哈+|嘿)[\s!！~～。.?？啊呀喔哦耶囉嗎]*$", re.I)
_THANKS_RX = re.compile(r"^(謝謝|感謝|多謝|謝啦|3q|thx|thanks|thank you|辛苦了|好的|好喔|好|ok|okay|收到|了解|瞭解|知道了|讚|沒事|沒問題)[\s!！~～。.啦喔哦了你您]*$", re.I)
HELP_EXAMPLES = "「7 號機現在怎樣？」「今晚夜班缺誰？」「大開煉機過熱怎麼處理？」"
HELLO_REPLY = "你好，我是戰情室助理。可以問我機台狀況、訂單、班表，或討論待核准的計劃。"
THANKS_REPLY = "不客氣，有需要再叫我。可以問我機台狀況、訂單、班表，或討論待核准的計劃。"
UNCLEAR_REPLY = f"我不確定要查什麼，所以沒有查任何資料。你可以這樣問：{HELP_EXAMPLES}"
CHANGE_REPLY = "問戰情室只能查詢，不能變更；停機、重排、調人這類變更請走事件核准流程（下新任務，計劃送你核准後才執行）。"
COLD_REPLY = (f"AI 模型還在載入（冷啟動，約半分鐘），這次來不及判讀你的問題，我沒有亂猜去查。請稍等再問一次，或換個明確的問法：{HELP_EXAMPLES}")


def smalltalk_reply(text: str) -> str | None:
    """A greeting or thanks gets a short reply and no tool call; anything else returns None."""
    t = text.strip()
    if _HELLO_RX.match(t):
        return HELLO_REPLY
    if _THANKS_RX.match(t):
        return THANKS_REPLY
    return None


_STATUS_RX = re.compile(r"狀況|狀態|怎樣|怎麼樣|如何|溫度|異常|問題|情況|哪台|機台|現在|目前|讀值|壓力|水流|蒸汽|預測|趨勢|好嗎|正常")


# ------------------------------------------------------------------ mock planner (no model)
# issue #182: the plant has '#A-311' and the team dataset's 'SO-004' / 'SO-017'; both are order ids ('#' optional when
# written with a hyphen). CURE-07 / BOND-01 are machines (4 letters), never orders.
ORDER_RX = re.compile(r"#\s?[A-Z]{1,2}-?\d{2,4}\b|(?<![A-Z0-9-])[A-Z]{1,2}-\d{2,4}\b", re.IGNORECASE)


def orders_in(text: str) -> list[str]:
    """Order ids named in a text, written the way the plant writes them: '#A-311', '#a311', '# A-311' -> '#A-311';
    'SO-004', 'so-004', '#SO004' -> 'SO-004' (one-letter ids carry the '#', the dataset's two-letter ones do not)."""
    out: list[str] = []
    for m in ORDER_RX.findall(text):
        s = re.sub(r"^([A-Z]+)(\d)", r"\1-\2", re.sub(r"\s", "", m).upper().lstrip("#"))
        s = f"#{s}" if len(s.split("-")[0]) == 1 else s
        if s not in out:
            out.append(s)
    return out


_CHANGE_RX = re.compile(r"停掉|停機|關掉|重排|調人|調 ?\d* ?人|加班|核准|改成|設定|通知")
# a request tells the plant to do it (「把 7 號機停掉」「幫我重排」「通知維修組」); a question asks about it
# (「核准卡在等什麼？」「今天有加班嗎？」「誰通知了維修組？」) and gets a normal grounded answer (issue #182)
_IMPERATIVE_RX = re.compile(r"^\s*(?:請(?!問)|麻煩(?!問)|現在就|直接)|把|(?:幫我|幫忙|給我)(?!查|看|確認|找|問|說)|馬上|立刻|趕快")
_QUESTION_RX = re.compile(r"[?？]\s*$|嗎|什麼|甚麼|什摸|啥|誰|哪|幾|多少|有沒有|有無|是否|是不是|為什麼|為何|怎麼|如何|了沒|沒有|狀態|進度|在等")


def change_request(question: str) -> bool:
    """A question that asks for a change (facts only, the approval flow note), not one that asks about a change."""
    if not _CHANGE_RX.search(question):
        return False
    return bool(_IMPERATIVE_RX.search(question)) or not _QUESTION_RX.search(question)
_PRESS_RX = re.compile(r"(\d{1,2})\s*號(?:硫化)?機?")
_ID_RX = re.compile(r"\b(?:CURE-\d{2}|[A-D]-[A-Z]+|BOND-01)\b")


def machines_in(question: str) -> list[str]:
    """Machine ids named in a question: 'CURE-07', '7 號機', '7號硫化機', 'A 線大開煉機'."""
    q = question.upper()
    found: list[str] = []

    def add(mid: str) -> None:
        if mid in F.MACHINE_BY_ID and mid not in found:
            found.append(mid)
    for mid in _ID_RX.findall(q):
        add(mid)
    for n in _PRESS_RX.findall(question):
        add(f"CURE-{int(n):02d}")
    compact = question.replace(" ", "")
    name_count: dict[str, int] = {}
    for m in F.MACHINES:
        name_count[m.name] = name_count.get(m.name, 0) + 1
    # longest first, and a matched name is cut out, so 大開煉機 never also reads as 開煉機
    for m in sorted((m for m in F.MACHINES if m.line is not F.Line.CURE), key=lambda m: -len(m.name)):
        label = machine_label(m.id).replace(" ", "")
        if label in compact:
            add(m.id)
            compact = compact.replace(label, "")
        elif name_count[m.name] == 1 and m.name in compact:   # 大開煉機 alone names A-BIGOPEN
            add(m.id)
            compact = compact.replace(m.name, "")
    return found


def mock_plan(question: str, context: dict[str, Any]) -> AskPlan:
    """Keyword rules: what a supervisor asks on the night shift."""
    q = question
    named = machines_in(q)
    # 「#A-311 會不會晚交？」 was answered 查不到 with no tool: the order words and the order ids it names
    orders = re.search(r"訂單|交期|班|人力|人手|缺|排程|下一模|延誤|遲交|晚交|會不會晚|出貨|交貨", q)
    order_ids = orders_in(q)
    sop = re.search(r"SOP|sop|怎麼處理|處置|怎麼辦|步驟|程序", q)
    # no machine named: the abnormal ones, but only for a status question (not staffing or SOP). Text
    # that is not a factory question names nothing: no machine is guessed (the plan stays empty)
    status = bool(_STATUS_RX.search(q))
    mids = named or ([] if orders or order_ids or sop or not status else list(context.get("abnormal", []))[:3])
    presses = [m for m in mids if m.startswith("CURE-")]
    steps: list[AskStep] = []
    if mids:
        steps.append(AskStep(tool=ToolName.READ_SENSORS.value, args={"machine_ids": mids, "minutes": 30}))
    if mids and re.search(r"預測|會不會|接下來|之後|趨勢|多久", q):
        steps.append(AskStep(tool=ToolName.PREDICT_ANOMALY.value, args={"machine_id": mids[0], "horizon_min": 30}))
    if orders or order_ids:
        args: dict[str, Any] = {"press_ids": presses} if presses else {}
        if order_ids:
            args["order_ids"] = order_ids
        steps.append(AskStep(tool=ToolName.QUERY_ORDERS_SHIFTS.value, args=args))
    if sop:
        steps.append(AskStep(tool=ToolName.SOP_SEARCH.value, args={"query": q, "k": 2}))
    if not steps and status:
        steps.append(AskStep(tool=ToolName.QUERY_ORDERS_SHIFTS.value, args={}))
    return AskPlan(steps=steps[:ASK_MAX_STEPS],
                   rationale="規則模式：依問題關鍵字選工具" if steps else "規則模式：看不出要查什麼，不猜機台")


# ------------------------------------------------------------------ follow-ups: 「那 8 號呢？」 (issue #182)
_FOLLOW_FILLER_RX = re.compile(r"[那麼么換還有再看來和跟與及也呢勒咧嘞哩的?？。!！~～,，、\s]*")


def follow_up_machines(question: str) -> list[str]:
    """The machines of a bare follow-up (「那8號呢」「8號機呢？」「換 CURE-09」): the question is only machine names and
    filler. A question that says anything more (「8號會不會掉？」) is not a bare follow-up: [] (the usual planner)."""
    mids = machines_in(question)
    if not mids:
        return []
    rest = _PRESS_RX.sub("", question)
    rest = re.sub(_ID_RX.pattern, "", rest, flags=re.IGNORECASE)
    compact = rest.replace(" ", "")
    for m in sorted(F.MACHINES, key=lambda m: -len(m.name)):
        compact = compact.replace(machine_label(m.id).replace(" ", ""), "").replace(m.name, "")
    compact = re.sub(r"[A-D]線", "", compact)
    return mids if _FOLLOW_FILLER_RX.fullmatch(compact) else []


def _step_machines(steps: list[AskStep] | list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for s in steps:
        args = (s.args if isinstance(s, AskStep) else s.get("args")) or {}
        for k in ("machine_ids", "press_ids"):
            out += [str(x) for x in args.get(k) or []]
        for k in ("machine_id", "press_id"):
            if args.get(k):
                out.append(str(args[k]))
    return [m for m in dict.fromkeys(out) if m in F.MACHINE_BY_ID]


def follow_up_plan(conv: ConvContext, mids: list[str]) -> AskPlan | None:
    """The last machine question's read steps again, for the follow-up's machines. Only the machine args change; a step
    that cannot apply to them (cure_formula for a mill) is left out. None: nothing to repeat."""
    if not conv.last_steps or not mids:
        return None
    presses = [m for m in mids if m in F.CURE_PRESS_IDS]
    steps: list[AskStep] = []
    for s in conv.last_steps:
        tool, args = s.get("tool"), dict(s.get("args") or {})
        if tool == ToolName.READ_SENSORS.value:
            args["machine_ids"] = list(mids)
        elif tool == ToolName.PREDICT_ANOMALY.value:
            args["machine_id"] = mids[0]
        elif tool == ToolName.CURE_FORMULA.value:
            if not presses:
                continue
            args = {"press_id": presses[0]}
        elif tool == ToolName.QUERY_ORDERS_SHIFTS.value and "press_ids" in args:
            if not presses:
                continue
            args["press_ids"] = presses
        steps.append(AskStep(tool=str(tool), args=args))
    if not steps:
        return None
    labels = "、".join(machine_label(m) for m in mids)
    return AskPlan(steps=steps[:ASK_MAX_STEPS], rationale=f"接續上一題「{conv.last_question}」，只把對象換成 {labels}")


# ------------------------------------------------------------------ the plan holds only what the question needs
# Spark 10-22: for 「現在7號機溫度」 the real model planned sensors + forecast + cure_formula + orders. A step is kept only
# when the question asks for its kind of fact; a plan the rules would empty stays as the model gave it (issue #182)
_NEEDS: dict[str, re.Pattern[str]] = {
    ToolName.PREDICT_ANOMALY.value: re.compile(r"預測|會不會|接下來|之後|趨勢|多久|再掉|還會|穩不穩|風險"),
    ToolName.CURE_FORMULA.value: re.compile(r"硫化|多久|幾小時|時數|延長|完成|做好|出模|補償|公式"),
    ToolName.QUERY_ORDERS_SHIFTS.value: re.compile(
        r"訂單|交期|班|人力|人手|缺|排程|下一模|入模|延誤|遲交|晚交|出貨|交貨|影響|單|生產|工單|進度"),
    ToolName.SOP_SEARCH.value: re.compile(r"SOP|sop|處理|處置|怎麼辦|步驟|程序|規定|標準|該怎麼"),
}


def prune_plan(question: str, plan: AskPlan) -> tuple[AskPlan, list[dict[str, Any]]]:
    """(the plan with only the steps the question needs, the steps left out)."""
    named = bool(machines_in(question)) or bool(_STATUS_RX.search(question))
    keep: list[AskStep] = []
    dropped: list[dict[str, Any]] = []
    for st in plan.steps:
        rx = _NEEDS.get(st.tool)
        need = (named or not _NEEDS[ToolName.QUERY_ORDERS_SHIFTS.value].search(question))             if st.tool == ToolName.READ_SENSORS.value else (rx is None or bool(rx.search(question)))
        (keep if need else dropped).append(st if need else st.model_dump(mode="json"))
    if not keep or not dropped:
        return plan, []
    return plan.model_copy(update={"steps": keep}), dropped


# ------------------------------------------------------------------ what the answer may use
_EVENT_Q_RX = re.compile(r"核准|事件|通知|卡在|在等|處理中|異常|維修|待辦")
_STATE_ZH = {"detected": "剛偵測到", "classify": "判讀中", "plan": "規劃中", "simulate": "模擬中", "gate": "安全檢查中",
             "await_approval": "等待主管核准", "execute": "執行中", "verify": "驗證中", "replan": "重新規劃中",
             "closed": "已結案", "safe_stop": "安全停止"}


def context_facts(question: str, context: dict[str, Any]) -> list[str]:
    """Code-written lines from the plant context the answer may use, only when the question is about events /
    approvals (「核准卡在等什麼？」): the open events and their state. Nothing else is added."""
    if not _EVENT_Q_RX.search(question):
        return []
    evs = context.get("open_events") or []
    if not evs:
        return ["進行中事件：無（目前沒有任何事件在等待核准或處理中）"]
    return ["進行中事件：" + "；".join(
        f"{e.get('id')}（{machine_label(e['machine_id']) if e.get('machine_id') else '無機台'}，"
        f"{_STATE_ZH.get(str(e.get('state')), str(e.get('state')))}"
        + (f"，目標：{e['goal']}" if e.get("goal") else "") + "）" for e in evs[:5])]


def relevant_results(question: str, results: list[AskStepResult], mids: list[str] | None = None) -> list[AskStepResult]:
    """The results about what the question names (its machines / orders); all of them when it names neither.
    A machine's part of a multi-machine reading is cut out, so 8 號's answer never carries 7 號's numbers."""
    mids = mids or machines_in(question)
    oids = {o.lstrip("#") for o in orders_in(question)}
    if not mids and not oids:
        return [r for r in results if r.ok]
    out: list[AskStepResult] = []
    for r in results:
        if not r.ok:
            continue
        a = r.args or {}
        named = set(_step_machines([{"args": a}]))
        if oids and {str(o).lstrip("#").upper() for o in a.get("order_ids") or []} & oids:
            out.append(r)
        elif mids and named & set(mids):
            if r.tool == ToolName.READ_SENSORS.value and len(named) > 1:
                parts = [p for p in r.summary.split("；") if any(p.startswith(machine_label(m)) for m in mids)]
                r = r.model_copy(update={"summary": "；".join(parts) or r.summary})
            out.append(r)
    return out or [r for r in results if r.ok]


# ------------------------------------------------------------------ result -> one line
def summarize(tool: ToolName, data: dict[str, Any], args: dict[str, Any] | None = None) -> str:
    """One Chinese line per tool result; the answer is built only from these lines. `args` lets an order
    question get its own orders' line (「#A-311 會不會晚交？」) instead of the nearest due date."""
    try:
        if tool is ToolName.QUERY_ORDERS_SHIFTS and (args or {}).get("order_ids"):
            today = data.get("orders_today")
            if today and today.get("orders") is not None:
                # issue #195: the model answers order questions now; its facts are the one rule set's lines (progress,
                # machines, due, 預計完成, risk) — the same text the code answer used (order_ask.compose)
                ids = [str(o) for o in args["order_ids"]]
                return order_ask.compose(order_ask.OrderIntent("status", ids=tuple(ids)), today, ids)[0]
            return _orders_line(data, list(args["order_ids"]))
        if tool is ToolName.READ_SENSORS:
            parts = []
            for mid, s in (data.get("summary") or {}).items():
                bits = []
                if "temp_c_now" in s:
                    bits.append(f"溫度 {fmt_num(s['temp_c_now'])}°C")
                if "temp_c_min" in s:
                    bits.append(f"30 分最低 {fmt_num(s['temp_c_min'])}°C")
                if "trend_c_per_min" in s:
                    bits.append(f"趨勢 {s['trend_c_per_min']:+.2f}°C/分")
                if "steam_kgf_cm2_now" in s:
                    bits.append(f"蒸汽壓 {fmt_num(s['steam_kgf_cm2_now'])} kgf/cm²")
                if "water_flow_lpm_now" in s:
                    bits.append(f"水流 {fmt_num(s['water_flow_lpm_now'])} L/分")
                parts.append(f"{machine_label(mid)}：" + "、".join(bits or [f"{k} {v}" for k, v in list(s.items())[:3]]))
            return "；".join(parts) or "沒有讀值"
        if tool is ToolName.PREDICT_ANOMALY:
            mins = round(float(data.get("horizon_s", 1800)) / 60)
            return (f"{machine_label(data['machine_id'])} 預測 {mins} 分後 {fmt_num(data['predicted_value'])}"
                    f"（區間 {fmt_num(data['band_lo'])}–{fmt_num(data['band_hi'])}，現值 {fmt_num(data['now_value'])}）")
        if tool is ToolName.CURE_FORMULA:
            line = f"硫化總時數 {fmt_num(data['total_cure_h'])} h（多 {fmt_num(data['extra_h'])} h）"
            inp = data.get("inputs") or {}
            if inp.get("press_id"):        # the plant's inputs (bind_cure_formula), so the answer can say them
                line = (f"{machine_label(inp['press_id'])}（{inp['size']} 胎，已硫化 {fmt_num(inp['elapsed_h'])} h，"
                        f"現在 {fmt_num(inp['actual_temp_c'])}°C）" + line)
            return line
        if tool is ToolName.QUERY_ORDERS_SHIFTS:
            bits = []
            gaps = data.get("gaps") or []
            bits.append("人力缺口：" + ("、".join(gaps) if gaps else "無（各班排班人數都達到需求，沒有缺人）"))
            jobs = data.get("next_jobs") or []
            if jobs:
                bits.append("下一模（共 %d 筆，列前 3）：" % len(jobs) + "、".join(
                    f"{machine_label(j['press_id'], short=True)} {j['order_id']} {str(j['mold_in'])[5:16].replace('T', ' ')} 入模"
                    for j in jobs[:3]))
            today = data.get("orders_today")
            if today:      # issue #95: the whole order list's counts (one rule set), not the one nearest-due order
                c = today["counts"]
                bits.append(f"今日訂單：生產 {c['production']} 張、交貨 {c['delivery']} 張；已遲交 {c['overdue']} 張、"
                            f"預估會遲交 {c['late']} 張、其他風險 {c['other_risk']} 張（共 {c['total']} 張）")
                if today["overdue"] or today["late"]:
                    bits.append("遲交相關：" + "、".join((today["overdue"] + today["late"])[:6]))
            else:
                orders = sorted(data.get("orders") or [], key=lambda o: str(o.get("due")))
                if orders:
                    o = orders[0]
                    bits.append(f"最近交期 {o['id']}（{o['size']}，{o.get('done', 0)}/{o['qty']}，{o['due']}）")
            return "；".join(bits)
        if tool is ToolName.SOP_SEARCH:
            hits = data.get("hits") or []
            if not hits:                      # the KB's no-answer gate: say so, never the nearest unrelated SOP
                return "知識庫沒有相關 SOP"
            # the text itself, so the answer can quote it instead of inventing steps; the source says house or team SOP
            return "SOP：" + "；".join(f"{h['sop_id']}〈{h['title']}〉{'（' + h['source_label'] + '）' if h.get('source_label') else ''}"
                                       f"{' '.join(str(h.get('text', '')).split())[:160]}" for h in hits[:2])
    except (KeyError, TypeError, ValueError):
        pass
    return json.dumps(_brief(data, 300), ensure_ascii=False, default=str)[:300]


def _orders_line(data: dict[str, Any], want: list[str]) -> str:
    """The asked orders: done/qty, due date and their next mold-in / de-mold; an id the plant does not have
    says 沒有這張訂單 (never another order's due date)."""
    key = lambda s: str(s).replace(" ", "").lstrip("#").upper()        # 'SO-004' == '#so-004' (issue #182)
    have = {key(o.get("id")): o for o in data.get("orders") or []}
    bits = []
    for asked in want:
        o = have.get(key(asked))
        if o is None:
            bits.append(f"沒有這張訂單：{asked}")
            continue
        oid = str(o.get("id"))
        jobs = [j for j in data.get("next_jobs") or [] if key(j.get("order_id")) == key(oid)][:2]
        when = "、".join(f"{machine_label(j['press_id'], short=True)} {str(j['mold_in'])[5:16].replace('T', ' ')} 入模"
                         f"・{str(j['demold'])[5:16].replace('T', ' ')} 出模" for j in jobs if j.get("demold"))
        bits.append(f"{oid}（{o.get('size', '')}，{o.get('done', 0)}/{o.get('qty', '')}，交期 {o.get('due')}）"
                    + (f"：{when}" if when else "：排程裡還沒有它的下一模"))
    return "；".join(bits)


_PRESS_ARG_RX = re.compile(r"(?:CURE[-_ ]?)?0*(\d{1,2})(?:\s*號.*)?", re.IGNORECASE)


def _press_arg(v: Any) -> str | None:
    """「CURE-07」「cure7」「7」「7 號機」 → CURE-07; anything else None."""
    m = _PRESS_ARG_RX.fullmatch(str(v).strip()) if v is not None else None
    mid = f"CURE-{int(m.group(1)):02d}" if m else None
    return mid if mid in F.CURE_PRESS_IDS else None


def bind_cure_formula(tools: Any, args: dict[str, Any], press_hint: str | None = None
                      ) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Spark 10-21: the model filled cure_formula's actual_temp_c itself (165 °C while 7 號 read 128 °C) and the made-up
    hours passed the number check. Physics inputs come from the plant, as in the state machine's _bind_physics: the
    press is press_id / machine_id in the args, else the question's press, else the one press with an open event;
    size, setpoint, hours cured and the temperature now are that press's live cure job and reading. Returns
    (args to call with, the inputs used or None, error). With no plant to read (offline rig) the args stand."""
    sim = getattr(getattr(tools, "ctx", None), "sim", None)
    rest = {k: v for k, v in (args or {}).items() if k not in ("press_id", "machine_id")}
    if sim is None or not callable(getattr(sim, "machine", None)):
        return rest, None, None
    press = _press_arg(args.get("press_id")) or _press_arg(args.get("machine_id")) or press_hint
    if press is None:
        try:
            open_presses = [m for m in F.CURE_PRESS_IDS if sim.machine(m).active_event_id]
        except Exception:  # noqa: BLE001
            open_presses = []
        press = open_presses[0] if len(open_presses) == 1 else None
    if press is None:
        return rest, None, "cure_formula 需要 press_id（哪一台硫化機）；尺寸、溫度、時數由程式從現場讀取"
    try:
        st = sim.machine(press)
    except Exception as e:  # noqa: BLE001
        return rest, None, f"讀不到 {press}：{type(e).__name__}"
    job, reading = st.cure_job, st.last_reading
    if job is None:
        return rest, None, f"{machine_label(press)}目前沒有在硫化的模次，無法計算硫化時間"
    if reading is None or reading.temp_c is None:
        return rest, None, f"{machine_label(press)}沒有溫度讀值，無法計算硫化時間"
    size = job.size.value if hasattr(job.size, "value") else str(job.size)
    bound = {"size": size, "ref_temp_c": float(job.setpoint_c), "actual_temp_c": round(float(reading.temp_c), 1),
             "elapsed_h": round(float(job.cure_elapsed_h), 2), "ref_cure_h": float(F.CURE_HOURS[job.size]),
             "activation_energy_kj_mol": float(F.ACTIVATION_ENERGY_KJ_MOL)}
    return bound, {"press_id": press, **bound}, None


_NUM_RX = re.compile(r"\d+(?:\.\d+)?")


def ungrounded_numbers(answer: str, question: str, results: list[AskStepResult], facts: list[str] | None = None
                       ) -> list[str]:
    """Numbers in the answer that appear neither in the tool summaries, the code's facts nor in the question.
    Code checks the model: an invented number never reaches the supervisor."""
    source = question + " " + " ".join(r.summary for r in results if r.ok) + " " + " ".join(facts or [])
    known = set(_NUM_RX.findall(source))
    known |= {n.split(".")[0] for n in known}            # "122.3" also vouches for "122"
    return [n for n in _NUM_RX.findall(answer) if n not in known]


def template_answer(results: list[AskStepResult], facts: list[str] | None = None) -> str:
    ok = [r.summary for r in results if r.ok and r.summary] + list(facts or [])
    if not ok:
        return "查不到資料：本次查詢的工具都沒有成功。"
    return "查詢結果：" + "。".join(ok) + "。"


_SENTENCE_RX = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_BARE_NONE_RX = re.compile(r"^[\s「」]*(?:查不到|查無資料|沒有資料|無資料|不知道|無法回答)[^\d]{0,6}$")


_CLAIMS_MISSING_RX = re.compile(r"查不到|查無|沒有[^，。；]{0,14}(?:資料|資訊|紀錄|數據)|無[^，。；]{0,6}(?:資料|資訊)")


def claims_missing(answer: str) -> bool:
    """The answer says the data is not there (「查詢結果裡沒有缺人資訊」)."""
    return bool(_CLAIMS_MISSING_RX.search(answer))


def bare_none(answer: str) -> bool:
    """「查不到。」 with nothing said about what: not an answer when the results hold data (issue #182)."""
    return bool(_BARE_NONE_RX.match(answer.strip()))


def drop_ungrounded(answer: str, question: str, results: list[AskStepResult], facts: list[str] | None = None) -> str:
    """The answer without its sentences that carry an invented number ('' when none is left)."""
    return "".join(s for s in _SENTENCE_RX.findall(answer)
                   if not ungrounded_numbers(s, question, results, facts)).strip()


# ------------------------------------------------------------------ the agent
class AskAgent:
    """Read-only Q&A on the event harness. `client` (an AsyncOpenAI) is injectable for tests."""

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

    def _extra(self) -> dict[str, Any]:
        extra = thinking_off(self.server_kind)
        return {"extra_body": extra} if extra else {}

    async def _llm_plan(self, question: str, context: dict[str, Any], previous: str | None = None,
                        previous_steps: list[dict[str, Any]] | None = None) -> tuple[AskPlan, str, str]:
        """(plan, intent, reply): the model reads the message itself (issue #195) and picks the read tools. It gets no
        readings to copy into args (Spark 10-03: 7 號's 115.11 °C went into 8 號's cure_formula); the previous question
        and its steps (this conversation only) let 「那 8 號呢？」 keep its intent."""
        now = {k: v for k, v in context.items() if k != "abnormal_temp_c"}
        user = json.dumps({"question": question, **({"previous_question": previous} if previous else {}),
                           **({"previous_steps": previous_steps} if previous and previous_steps else {}), "now": now},
                          ensure_ascii=False, default=str)
        r = await budget.create(  # metered; over the day's budget it raises and the keyword rules plan
            self.client, "qa", model=self.settings.model, temperature=0.0, max_tokens=400,
            messages=[{"role": "system", "content": PLAN_SYSTEM + plant.section()}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "AskPlan", "schema": ask_plan_schema()}},
            **self._extra())
        text = (r.choices[0].message.content or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"plan is not an object: {text[:120]!r}")
        intent = str(raw.pop("intent", "") or "")
        reply = str(raw.pop("reply", "") or "").strip()
        plan = AskPlan.model_validate(raw)
        if intent not in INTENTS:            # an older / partial answer: steps mean a question, none mean unclear
            intent = "question" if plan.steps else "unclear"
        return plan, intent, reply

    async def _llm_answer(self, question: str, results: list[AskStepResult], facts: list[str] | None = None,
                          feedback: tuple[str, str] | None = None) -> str:
        """`feedback` = (the model's last answer, what was wrong with it): one rewrite, from the same results."""
        lines = "\n".join([f"- {r.tool}: {r.summary if r.ok else '失敗：' + (r.error or '')}" for r in results]
                          + [f"- 現場: {f}" for f in facts or []])
        messages = [{"role": "system", "content": ANSWER_SYSTEM + plant.section()},
                    {"role": "user", "content": f"問題：{question}\n查詢結果：\n{lines or '（無）'}"}]
        if feedback:
            messages += [{"role": "assistant", "content": feedback[0]},
                         {"role": "user", "content": f"這個回答不能用：{feedback[1]}請只根據上面的查詢結果重寫，直接回答問題。"}]
        r = await budget.create(
            self.client, "qa", model=self.settings.model, temperature=0.0, max_tokens=300,
            messages=messages, **self._extra())
        text = (r.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("empty answer")
        return text

    async def _checked_answer(self, question: str, results: list[AskStepResult], facts: list[str],
                              mids: list[str]) -> tuple[str, dict[str, Any]]:
        """The model's answer, checked by code (issue #182). An invented number, or a bare 「查不到」 while the results
        hold data, gets one rewrite with the reason; a rewrite that still invents keeps only its sentences without an
        invented number; the last resort is the results about what was asked (not every tool's summary). Exceptions
        from the first call propagate (the caller's fallback / --strict)."""
        has_data = any(r.ok and r.summary for r in results) or bool(facts)
        rec: dict[str, Any] = {"ungrounded": [], "repair": None}

        def problem(text: str, first: bool = False) -> str | None:
            bad = ungrounded_numbers(text, question, results, facts)
            if bad:
                rec["ungrounded"] = sorted(set(rec["ungrounded"]) | set(bad))
                return f"這些數字不在查詢結果裡：{'、'.join(bad)}。"
            if has_data and bare_none(text):
                return "查詢結果裡有資料，只回「查不到」沒有回答問題；結果真的沒有被問的資料時，請具體說查不到什麼。"
            if first and has_data and claims_missing(text):
                # Spark 10-22: 「今晚夜班缺誰？」 -> 「查詢結果裡沒有缺人資訊」 while the result said 人力缺口：無. One more look;
                # a rewrite that still finds nothing stands (it says what is missing)
                return ("你說查不到，但查詢結果裡有資料。請再看一次：其中若有回答問題的內容（「無」也是答案），就直接用它回答；"
                        "真的沒有才說查不到什麼。")
            return None

        answer = await self._llm_answer(question, results, facts)
        why = problem(answer, first=True)
        if why is None:
            return answer, rec
        rec["first_answer"], rec["first_problem"] = answer, why
        try:
            retry = await self._llm_answer(question, results, facts, feedback=(answer, why))
        except Exception as e:  # noqa: BLE001 - the rewrite failed: repair the first answer instead
            rec["retry_error"] = f"{type(e).__name__}: {e}"[:200]
            retry = answer
        else:
            rec["retry_answer"] = retry
            if problem(retry) is None:
                rec["repair"] = "retry"
                return retry, rec
        kept = drop_ungrounded(retry, question, results, facts)
        if kept and not (has_data and bare_none(kept)):
            rec["repair"] = "dropped_sentences"
            return kept, rec
        rec["repair"] = "template_relevant"
        return template_answer(relevant_results(question, results, mids), facts), rec

    def _strict_answer(self, e: Exception, phase: str, error: str, ask_id: str, question: str, audit: AuditLog,
                       now: Any, t0: float, tp: float, results: list[AskStepResult] | None = None) -> AskAnswer:
        """--strict: the model failed, so say so; no keyword-rule plan, no template answer."""
        reason = strict_reason(e, "回答")
        audit.append(AuditKind.LLM_CALL, "ask", {
            "phase": phase, "mode": self.mode.value, "model": self.settings.model,
            "total_ms": round((time.perf_counter() - tp) * 1000, 1), "fell_back_to_mock": False, "error": error,
            "cause": failure_cause(e), "strict": True, "reason": reason}, event_id=ask_id)
        return AskAnswer(id=ask_id, question=question, answer=reason, steps=results or [], mode=self.mode,
                         factory_time=now, total_ms=round((time.perf_counter() - t0) * 1000, 1))

    async def ask(self, question: str, *, tools: Any, audit: AuditLog, context: dict[str, Any],
                  now: Any, actor: str = "supervisor", conversation_id: str | None = None,
                  memory: AskMemory | None = None) -> AskAnswer:
        """One question. `conversation_id` + `memory` give it a conversation: a follow-up (「那張單呢？」) is resolved
        against the order / event ids the conversation just talked about; without them every question stands alone."""
        ans = await self._ask(question, tools=tools, audit=audit, context=context, now=now, actor=actor,
                              conversation_id=conversation_id, memory=memory)
        return ans.model_copy(update={"conversation_id": conversation_id}) if conversation_id else ans

    async def _order_question(self, question: str, intent: Any, conv: ConvContext, *, tools: Any, audit: AuditLog,
                              now: Any, actor: str, conversation_id: str | None, memory: AskMemory | None,
                              ask_id: str, t0: float) -> AskAnswer:
        """issue #95: an order question. Code picks the order, calls the read tool again (the answer is never reused
        from an earlier turn), and writes the answer from its result; the model is not involved."""
        res = order_ask.resolve(intent, conv)
        audit.append(AuditKind.SYSTEM, "ask", {
            "msg": "order question", "kind": intent.kind, "named": list(intent.ids), "pointed": intent.pointed,
            "resolved_via": res.via, "resolved": res.ids, "needs": res.needs,
            "memory": conv.as_dict(), "memory_generation": memory.generation if memory is not None else None},
            event_id=ask_id)
        ms = lambda: round((time.perf_counter() - t0) * 1000, 1)        # noqa: E731
        if memory is not None:     # an order question leaves no machine steps for 「那 8 號呢？」 to repeat
            memory.remember(actor, conversation_id, events=list(intent.events), question=question)
        if res.needs:                                    # nothing is guessed: the user says which order
            text = order_ask.need_text(res)
            audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": text, "intent": f"orders:{res.needs}",
                                                   "tool_calls": 0}, event_id=ask_id)
            return AskAnswer(id=ask_id, question=question, answer=text, mode=self.mode, factory_time=now,
                             intent="needs_choice" if res.needs == "choice" else "needs_order",
                             choices=res.choices, total_ms=ms())
        args: dict[str, Any] = {"order_ids": res.ids} if res.ids else {}
        result = await self._run(tools, audit, ask_id, 1, ToolName.QUERY_ORDERS_SHIFTS, args)
        data = result.data.get("orders_today") if result.ok else None
        if not data:
            why = result.error or result.data.get("orders_today_error") or "沒有回傳今日訂單"
            text = f"查不到資料：今日訂單查詢沒有成功（{why}）。我沒有用舊的數字代替。"
            step = AskStepResult(tool=ToolName.QUERY_ORDERS_SHIFTS.value, args=args, ok=False, error=str(why), summary="")
        else:
            text, shown, focus = order_ask.compose(intent, data, res.ids)
            step = AskStepResult(tool=ToolName.QUERY_ORDERS_SHIFTS.value, args=args, ok=True, summary=text)
            if memory is not None and (shown or intent.kind not in order_ask.SINGLE):
                memory.remember(actor, conversation_id, orders=shown, focus=focus, date_scope=data.get("date"))
        audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": text, "intent": f"orders:{intent.kind}",
                                               "tool_calls": 1}, event_id=ask_id)
        return AskAnswer(id=ask_id, question=question, answer=text, steps=[step], mode=self.mode, factory_time=now,
                         total_ms=ms())

    async def _ask(self, question: str, *, tools: Any, audit: AuditLog, context: dict[str, Any],
                   now: Any, actor: str, conversation_id: str | None, memory: AskMemory | None) -> AskAnswer:
        t0 = time.perf_counter()
        ask_id = f"ASK-{next(self._ids):04d}"
        audit.append(AuditKind.SYSTEM, actor, {"msg": "ask", "question": question}, event_id=ask_id)
        fell_back = False
        conv = memory.get(actor, conversation_id) if memory is not None else ConvContext()
        ms = lambda: round((time.perf_counter() - t0) * 1000, 1)        # noqa: E731
        cold = False
        follow: AskPlan | None = None
        follow_mids: list[str] = []
        answer_q = question
        model_intent: str | None = None          # the model's own reading of the message (issue #195)
        model_reply = ""

        async def rules_route() -> AskAnswer | None:
            """The keyword routes, only when there is no model answer (LLM_MODE=mock, or the model failed): a greeting,
            an order question by code, 「那 X 呢」 repeating the last steps. None: the keyword planner takes it."""
            nonlocal follow, follow_mids, answer_q
            chat = smalltalk_reply(question)
            if chat is not None:
                audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": chat, "intent": "chitchat",
                                                       "tool_calls": 0, "by": "rules"}, event_id=ask_id)
                return AskAnswer(id=ask_id, question=question, answer=chat, mode=self.mode, factory_time=now,
                                 intent="chitchat", fell_back_to_mock=fell_back, total_ms=ms())
            oi = order_ask.classify(question, has_machine=bool(machines_in(question)), memory=conv)
            if oi is not None:
                ans = await self._order_question(question, oi, conv, tools=tools, audit=audit, now=now, actor=actor,
                                                 conversation_id=conversation_id, memory=memory, ask_id=ask_id, t0=t0)
                return ans.model_copy(update={"fell_back_to_mock": fell_back})
            follow_mids = follow_up_machines(question)
            follow = follow_up_plan(conv, follow_mids) if follow_mids else None
            if follow is not None:
                answer_q = (f"{question}（接續上一題「{conv.last_question}」，只是對象換成"
                            f"{'、'.join(machine_label(m) for m in follow_mids)}）")
                audit.append(AuditKind.SYSTEM, "ask", {"msg": "follow-up", "previous_question": conv.last_question,
                                                       "previous_steps": conv.last_steps, "machines": follow_mids},
                             event_id=ask_id)
            return None

        # 1. PLAN — with a model, the message goes to it as it is: no code check in front (CJ 10-22: 「取消對話前的程式
        # 檢查 完全讓 LLM 跟 system prompt 擋」). It says what the message is (intent) and which read tools answer it.
        tp = time.perf_counter()
        error: str | None = None
        plan: AskPlan | None = None
        if self.mode is LLMMode.LLM:
            try:
                plan, model_intent, model_reply = await self._llm_plan(question, context, previous=conv.last_question,
                                                                       previous_steps=conv.last_steps)
                if model_intent == "question" and not plan.steps and not context_facts(question, context) \
                        and (rules := mock_plan(question, context)).steps:
                    # the model said 「a question」 and planned nothing to read (Spark 10-22: 「今晚夜班缺誰？」): its own
                    # plan is incomplete, so the keyword planner fills the steps (a repair of the model's output)
                    error = f"model planned no step for a question ({plan.rationale}): keyword rules used"
                    plan = rules
            except Exception as e:  # noqa: BLE001 - model down / invalid JSON
                error = f"{type(e).__name__}: {e}"[:300]
                if not self.settings.llm_fallback_to_mock:
                    return self._strict_answer(e, "plan", error, ask_id, question, audit, now, t0, tp)
                fell_back = True
                cold = "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower()
                plan = None
        if plan is None:                                  # rule mode, or the model failed
            routed = await rules_route()
            if routed is not None:
                return routed
            plan = follow or mock_plan(question, context)
        audit.append(AuditKind.LLM_CALL, "ask", {
            "phase": "plan", "mode": self.mode.value,
            "model": self.settings.model if self.mode is LLMMode.LLM and not fell_back else None,
            "planner": "llm" if model_intent is not None and not error else (
                "follow-up rule" if follow is not None else "rules"),
            "intent": model_intent, "total_ms": round((time.perf_counter() - tp) * 1000, 1),
            "fell_back_to_mock": fell_back, "error": error,
            "steps": [s.model_dump(mode="json") for s in plan.steps], "rationale": plan.rationale,
            "plant_sha256": plant.active_sha()}, event_id=ask_id)
        if model_intent in ("chitchat", "unclear") and not plan.steps:
            # the model answered it itself: a greeting, or one clarifying question. A number in it that the question does
            # not hold is not used (the canned line instead)
            # (a machine number — 「7 號機」 in a clarifying question — is a name, not a reading)
            known = [json.dumps(context, ensure_ascii=False, default=str), " ".join(str(n) for n in range(1, 12))]
            text = model_reply if model_reply and not ungrounded_numbers(model_reply, question, [], known) else (
                HELLO_REPLY if model_intent == "chitchat" else UNCLEAR_REPLY)
            if memory is not None:
                memory.remember(actor, conversation_id, question=question, steps=[], machines=[])
            audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": text, "intent": model_intent,
                                                   "tool_calls": 0, "by": "model"}, event_id=ask_id)
            return AskAnswer(id=ask_id, question=question, answer=text, mode=self.mode, factory_time=now,
                             intent=model_intent, total_ms=ms())
        if model_intent is not None and conv.last_question and not follow:
            # the answer sees the previous question too, so 「那 8 號呢」 is answered as the same question for 8 號
            answer_q = f"{question}（同一段對話的上一題：「{conv.last_question}」；這題若是接續就照同樣的問法回答）"

        # 2. RUN: only ASK_TOOLS, at most ASK_MAX_STEPS
        press_hint = next((m for m in machines_in(question) if m in F.CURE_PRESS_IDS), None)
        results: list[AskStepResult] = []
        ran: list[AskStep] = []
        intercepted: list[dict[str, Any]] = []
        for i, step in enumerate(plan.steps, start=1):
            why = None
            try:
                tool = ToolName(step.tool)
            except ValueError:
                tool, why = None, "not on the allow-list"
            if why is None and tool not in ASK_TOOLS:
                why = "write tool: questions are read-only" if tool in WRITE_TOOLS \
                    else "not open for questions (side effect)"
            if why is None and i > ASK_MAX_STEPS:
                why = f"more than {ASK_MAX_STEPS} steps"
            if why is not None:
                rec = {"tool": step.tool, "args": step.args, "state": "ASK", "reason": why}
                intercepted.append(rec)
                audit.append(AuditKind.INTERCEPTED, "ask", rec, event_id=ask_id)
                continue
            res = await self._run(tools, audit, ask_id, i, tool, step.args, press=press_hint)
            ran.append(step)
            results.append(AskStepResult(tool=tool.value, args=step.args, ok=res.ok, error=res.error,
                                         summary=summarize(tool, res.data, step.args) if res.ok else ""))

        if memory is not None:
            # the root question is kept through a chain of follow-ups (7 號 -> 那 8 號呢 -> 那 9 號呢)
            memory.remember(actor, conversation_id, question=conv.last_question if follow is not None else question,
                            steps=[st.model_dump(mode="json") for st in ran], machines=_step_machines(ran))

        facts = context_facts(question, context)
        if not plan.steps and not intercepted and not facts:
            # nothing to look up: say so (or that the model is still loading), never guess a machine
            intent = "model_cold" if cold else "unclear"
            answer = COLD_REPLY if cold else UNCLEAR_REPLY
            if model_intent == "change_request":
                intent, answer = "change_request", CHANGE_REPLY
            audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": answer, "intent": intent,
                                                   "tool_calls": 0}, event_id=ask_id)
            return AskAnswer(id=ask_id, question=question, answer=answer, mode=self.mode, fell_back_to_mock=fell_back,
                             factory_time=now, intent=intent, total_ms=round((time.perf_counter() - t0) * 1000, 1))

        # 3. ANSWER
        ta = time.perf_counter()
        error = None
        answer_fell_back = False
        ungrounded: list[str] = []
        repair: dict[str, Any] = {}
        mids = follow_mids or machines_in(question)
        # a change request gets the facts only: the model never phrases what "will happen" to the plant. A question
        # about a change (「核准卡在等什麼？」「今天有加班嗎？」) is answered like any other (issue #182)
        # the model's reading decides (issue #195); the keyword test only without one (rule mode / model failed)
        change = model_intent == "change_request" if model_intent is not None else change_request(question)
        if self.mode is LLMMode.LLM and not fell_back and not change:
            try:
                answer, repair = await self._checked_answer(answer_q, results, facts, mids)
                ungrounded = repair.get("ungrounded") or []
                if repair.get("repair"):
                    why = f"ungrounded numbers {ungrounded}" if ungrounded else "bare 查不到"
                    error = f"{why}: answer repaired by {repair['repair']}"
            except Exception as e:  # noqa: BLE001 - model down / empty answer
                error = f"{type(e).__name__}: {e}"[:300]
                if not self.settings.llm_fallback_to_mock:
                    return self._strict_answer(e, "answer", error, ask_id, question, audit, now, t0, ta, results)
                answer, answer_fell_back = template_answer(relevant_results(question, results, mids), facts), True
        else:
            # mock mode and change requests: the facts, about what the question names (issue #182)
            answer = template_answer(relevant_results(question, results, mids), facts)
        fell_back = fell_back or answer_fell_back
        if cold:
            answer += "（AI 模型還在載入，這次依關鍵字規則查詢）"
        if intercepted:
            answer += f"（已攔下 {len(intercepted)} 個不允許的動作：查詢只能讀，不能變更）"
        elif change:
            answer += "（問戰情室只能查詢，不能變更；停機、重排、調人請走事件核准流程）"
        audit.append(AuditKind.LLM_CALL, "ask", {
            "phase": "answer", "mode": self.mode.value, "total_ms": round((time.perf_counter() - ta) * 1000, 1),
            "fell_back_to_mock": answer_fell_back, "error": error, "ungrounded": ungrounded,
            "repair": repair.get("repair"),
            "repair_detail": {k: v for k, v in repair.items() if k not in ("repair", "ungrounded")},
            "facts": facts,
            "skipped": "change request: facts only" if change and self.mode is LLMMode.LLM else None,
            "plant_sha256": plant.active_sha()},
            event_id=ask_id)
        audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": answer}, event_id=ask_id)
        return AskAnswer(id=ask_id, question=question, answer=answer, steps=results, intercepted=intercepted,
                         mode=self.mode, fell_back_to_mock=fell_back, factory_time=now,
                         total_ms=round((time.perf_counter() - t0) * 1000, 1))

    @staticmethod
    async def _run(tools: Any, audit: AuditLog, ask_id: str, i: int, tool: ToolName,
                   args: dict[str, Any], press: str | None = None) -> ToolResult:
        inputs = None
        if tool is ToolName.CURE_FORMULA:
            given = dict(args or {})
            args, inputs, err = bind_cure_formula(tools, given, press)
            if inputs is not None and args != given:
                audit.append(AuditKind.SYSTEM, "ask", {"cure_formula_args_from_plant": f"Q{i}", "from": given,
                                                       "to": args, "press": inputs["press_id"]}, event_id=ask_id)
            if err is not None:
                audit.append(AuditKind.SYSTEM, "ask", {"cure_formula_unbound": f"Q{i}", "args": given, "error": err},
                             event_id=ask_id)
                return ToolResult(call_id=f"{ask_id}:Q{i}", tool=tool, ok=False, error=err)
        call = ToolCall(id=f"{ask_id}:Q{i}", event_id=ask_id, step_id=f"Q{i}", tool=tool, args=args)
        audit.append(AuditKind.TOOL_CALL, f"tool:{tool.value}",
                     {"call_id": call.id, "step_id": call.step_id, "args": args, "attempt": 1, "state": "ASK"},
                     event_id=ask_id)
        try:
            res = await tools.acall(call)
        except Exception as e:  # noqa: BLE001 - the registry never raises; be safe anyway
            res = ToolResult(call_id=call.id, tool=tool, ok=False, error=f"{type(e).__name__}: {e}")
        audit.append(AuditKind.TOOL_RESULT, f"tool:{tool.value}",
                     {"call_id": call.id, "step_id": call.step_id, "ok": res.ok, "error": res.error,
                      "duration_ms": res.duration_ms, "attempt": 1, "data": _brief(res.data)}, event_id=ask_id)
        if inputs is not None and res.ok:
            res = res.model_copy(update={"data": {**res.data, "inputs": inputs}})
        return res
