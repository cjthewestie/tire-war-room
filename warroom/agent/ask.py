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
    + [(f"CURE-01..CURE-11 = 1..11 號硫化機（{F.ML_RANGE} 號可做中大胎），"
        f"{int(F.SMALL_ONLY_PRESSES[0][5:])}–{int(F.SMALL_ONLY_PRESSES[-1][5:])} 號只做小胎")])

PLAN_SYSTEM = f"""你是實心胎工廠戰情室的查詢助理。主管問一個問題，你只決定要查哪些資料，不回答、不做任何變更。
只輸出一個 JSON 物件：{{"steps": [{{"tool": "...", "args": {{...}}}}], "rationale": "一句話"}}。
規則：
1. 最多 {ASK_MAX_STEPS} 步，只能用下列唯讀工具；你沒有任何寫入、排程或通知的權限。
0. 不是工廠的問題（打招呼、道謝、閒聊、看不懂的字）時，steps 留空陣列，不要查任何機台。
2. 機台用代號（例如 CURE-07）。問題沒指名機台時，查「目前異常機台」。
3. 問到人力、班表、交期、下一模 → query_orders_shifts；問到怎麼處理 → sop_search。
工具：
{_CATALOGUE}
機台代號：{_MACHINES}"""

ANSWER_SYSTEM = """你是實心胎工廠戰情室的查詢助理。只根據「查詢結果」用繁體中文回答主管，最多 3 句。
數字照抄查詢結果，不估算、不補充結果裡沒有的事；查不到就直說查不到。你不能做任何變更，需要變更時請主管走事件核准流程。"""


def ask_plan_schema() -> dict[str, Any]:
    s = AskPlan.model_json_schema()
    s["required"] = ["steps"]
    return s


# ------------------------------------------------------------------ small talk / unclear (no tool call)
_HELLO_RX = re.compile(r"^(哈[摟囉嘍喽啰]|hello|hi|hey|嗨|你好|您好|妳好|大家好|早安|午安|晚安|早|安安|在嗎|在不在|有人嗎|哈哈+|嘿)[\s!！~～。.?？啊呀喔哦耶囉嗎]*$", re.I)
_THANKS_RX = re.compile(r"^(謝謝|感謝|多謝|謝啦|3q|thx|thanks|thank you|辛苦了|好的|好喔|好|ok|okay|收到|了解|瞭解|知道了|讚|沒事|沒問題)[\s!！~～。.啦喔哦了你您]*$", re.I)
HELP_EXAMPLES = "「7 號機現在怎樣？」「今晚夜班缺誰？」「大開煉機過熱怎麼處理？」"
HELLO_REPLY = "你好，我是戰情室助理。可以問我機台狀況、訂單、班表，或討論待核准的計劃。"
THANKS_REPLY = "不客氣，有需要再叫我。可以問我機台狀況、訂單、班表，或討論待核准的計劃。"
UNCLEAR_REPLY = f"我不確定要查什麼，所以沒有查任何資料。你可以這樣問：{HELP_EXAMPLES}"
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
ORDER_RX = re.compile(r"#\s?[A-Z]-?\d{2,4}", re.IGNORECASE)


def orders_in(text: str) -> list[str]:
    """Order ids named in a text, written the way the plant writes them: '#A-311', '#a311', '# A-311' -> '#A-311'."""
    out: list[str] = []
    for m in ORDER_RX.findall(text):
        s = re.sub(r"\s", "", m).upper()
        if "-" not in s:
            s = f"{s[:2]}-{s[2:]}"
        if s not in out:
            out.append(s)
    return out


_CHANGE_RX = re.compile(r"停掉|停機|關掉|重排|調人|調 ?\d* ?人|加班|核准|改成|設定|通知")
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
    orders = re.search(r"訂單|交期|班|人力|缺|排程|下一模|延誤|遲交|晚交|會不會晚|出貨|交貨", q)
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


# ------------------------------------------------------------------ result -> one line
def summarize(tool: ToolName, data: dict[str, Any], args: dict[str, Any] | None = None) -> str:
    """One Chinese line per tool result; the answer is built only from these lines. `args` lets an order
    question get its own orders' line (「#A-311 會不會晚交？」) instead of the nearest due date."""
    try:
        if tool is ToolName.QUERY_ORDERS_SHIFTS and (args or {}).get("order_ids"):
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
            bits.append("人力缺口：" + ("、".join(gaps) if gaps else "無"))
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
    have = {str(o.get("id")): o for o in data.get("orders") or []}
    bits = []
    for oid in want:
        o = have.get(oid)
        if o is None:
            bits.append(f"沒有這張訂單：{oid}")
            continue
        jobs = [j for j in data.get("next_jobs") or [] if j.get("order_id") == oid][:2]
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


def ungrounded_numbers(answer: str, question: str, results: list[AskStepResult]) -> list[str]:
    """Numbers in the answer that appear neither in the tool summaries nor in the question.
    Code checks the model: one invented number and the model's answer is not used."""
    source = question + " " + " ".join(r.summary for r in results if r.ok)
    known = set(_NUM_RX.findall(source))
    known |= {n.split(".")[0] for n in known}            # "122.3" also vouches for "122"
    return [n for n in _NUM_RX.findall(answer) if n not in known]


def template_answer(results: list[AskStepResult]) -> str:
    ok = [r.summary for r in results if r.ok and r.summary]
    if not ok:
        return "查不到資料：本次查詢的工具都沒有成功。"
    return "查詢結果：" + "。".join(ok) + "。"


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

    async def _llm_plan(self, question: str, context: dict[str, Any]) -> AskPlan:
        user = json.dumps({"question": question, "now": context}, ensure_ascii=False, default=str)
        r = await budget.create(  # metered; over the day's budget it raises and the keyword rules plan
            self.client, "qa", model=self.settings.model, temperature=0.0, max_tokens=400,
            messages=[{"role": "system", "content": PLAN_SYSTEM + plant.section()}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": "AskPlan", "schema": ask_plan_schema()}},
            **self._extra())
        text = (r.choices[0].message.content or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        return AskPlan.model_validate_json(text)

    async def _llm_answer(self, question: str, results: list[AskStepResult]) -> str:
        lines = "\n".join(f"- {r.tool}: {r.summary if r.ok else '失敗：' + (r.error or '')}" for r in results)
        r = await budget.create(
            self.client, "qa", model=self.settings.model, temperature=0.0, max_tokens=300,
            messages=[{"role": "system", "content": ANSWER_SYSTEM + plant.section()},
                      {"role": "user", "content": f"問題：{question}\n查詢結果：\n{lines or '（無）'}"}],
            **self._extra())
        text = (r.choices[0].message.content or "").strip()
        if not text:
            raise ValueError("empty answer")
        return text

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
        if memory is not None and intent.events:
            memory.remember(actor, conversation_id, events=list(intent.events))
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
        chat = smalltalk_reply(question)
        if chat is not None:      # a greeting / thanks: answered by code, no model call, no tool call
            audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": chat, "intent": "chitchat",
                                                   "tool_calls": 0}, event_id=ask_id)
            return AskAnswer(id=ask_id, question=question, answer=chat, mode=self.mode, factory_time=now,
                             intent="chitchat", total_ms=round((time.perf_counter() - t0) * 1000, 1))
        conv = memory.get(actor, conversation_id) if memory is not None else ConvContext()
        intent = order_ask.classify(question, has_machine=bool(machines_in(question)), memory=conv)
        if intent is not None:
            return await self._order_question(question, intent, conv, tools=tools, audit=audit, now=now, actor=actor,
                                              conversation_id=conversation_id, memory=memory, ask_id=ask_id, t0=t0)
        cold = False

        # 1. PLAN
        tp = time.perf_counter()
        error: str | None = None
        if self.mode is LLMMode.LLM:
            try:
                plan = await self._llm_plan(question, context)
            except Exception as e:  # noqa: BLE001 - model down / invalid JSON
                error = f"{type(e).__name__}: {e}"[:300]
                if not self.settings.llm_fallback_to_mock:
                    return self._strict_answer(e, "plan", error, ask_id, question, audit, now, t0, tp)
                plan, fell_back = mock_plan(question, context), True
                cold = "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower()
        else:
            plan = mock_plan(question, context)
        audit.append(AuditKind.LLM_CALL, "ask", {
            "phase": "plan", "mode": self.mode.value, "model": self.settings.model if self.mode is LLMMode.LLM else None,
            "total_ms": round((time.perf_counter() - tp) * 1000, 1), "fell_back_to_mock": fell_back, "error": error,
            "steps": [s.model_dump(mode="json") for s in plan.steps], "rationale": plan.rationale,
            "plant_sha256": plant.active_sha()}, event_id=ask_id)

        # 2. RUN: only ASK_TOOLS, at most ASK_MAX_STEPS
        press_hint = next((m for m in machines_in(question) if m in F.CURE_PRESS_IDS), None)
        results: list[AskStepResult] = []
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
            results.append(AskStepResult(tool=tool.value, args=step.args, ok=res.ok, error=res.error,
                                         summary=summarize(tool, res.data, step.args) if res.ok else ""))

        if not plan.steps and not intercepted:
            # nothing to look up: say so (or that the model is still loading), never guess a machine
            intent = "model_cold" if cold else "unclear"
            answer = COLD_REPLY if cold else UNCLEAR_REPLY
            audit.append(AuditKind.SYSTEM, "ask", {"msg": "answered", "answer": answer, "intent": intent,
                                                   "tool_calls": 0}, event_id=ask_id)
            return AskAnswer(id=ask_id, question=question, answer=answer, mode=self.mode, fell_back_to_mock=fell_back,
                             factory_time=now, intent=intent, total_ms=round((time.perf_counter() - t0) * 1000, 1))

        # 3. ANSWER
        ta = time.perf_counter()
        error = None
        answer_fell_back = False
        ungrounded: list[str] = []
        change = bool(_CHANGE_RX.search(question))
        # a change request gets the facts only: the model never phrases what "will happen" to the plant
        if self.mode is LLMMode.LLM and not fell_back and not change:
            try:
                answer = await self._llm_answer(question, results)
                ungrounded = ungrounded_numbers(answer, question, results)
                if ungrounded:
                    error = f"ungrounded numbers {ungrounded}: model answer replaced by the results"
                    answer = template_answer(results)
            except Exception as e:  # noqa: BLE001 - model down / empty answer
                error = f"{type(e).__name__}: {e}"[:300]
                if not self.settings.llm_fallback_to_mock:
                    return self._strict_answer(e, "answer", error, ask_id, question, audit, now, t0, ta, results)
                answer, answer_fell_back = template_answer(results), True
        else:
            answer = template_answer(results)
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
