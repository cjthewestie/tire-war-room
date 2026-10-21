"""主 agent conversation (issue #54, CJ 2026-10-20): 「主 agent 要可以像一般 agent 一樣聊天、調用知識庫、還要可以派發，他就只是工具而已」.

A bounded tool-calling loop on Nemotron (max MAX_STEPS tool steps per turn, BUDGET_S seconds), with the last
MEMORY_TURNS turns of the 主 agent thread and a read-only context (plant status, open items, selected plan). Each
step the model returns one JSON object (schema below): call one tool, or reply.

Tools it may call:
- the read tools of 問戰情室 (ASK_TOOLS: read_sensors, predict_anomaly, cure_formula, query_orders_shifts,
  sop_search = the knowledge base), run through the same Registry and audited like any call;
- orchestrator tools: dispatch_machine_task / dispatch_order_task / dispatch_material_task / dispatch_staff_task
  (start ONE subagent's planning; its plan still needs 核准並派發 before any write) and discuss_waiting_plan (talk
  about the selected plan; it may become a new card version). These are not in ToolName (the 9-tool registry is
  unchanged) and the gate is not involved: none of them writes to the plant.
No write tool exists here: set_cure_time / confirm_schedule / notify_teams / solve_schedule are refused by code.

Code guards: gibberish or text with nothing recognisable gets 「這句我看不懂…」 with 0 tools and 0 dispatch; a
dispatch needs a concrete object (a machine, an order, a material, people/shift); numbers in the reply must come
from tool results, the context or the message (else the reply is replaced by the results). Every message gets a
reply. Rule mode (or a model failure) runs the same steps by keywords."""
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

from warroom.agent import plant
from warroom.agent.ask import _HELLO_RX, _NUM_RX, _THANKS_RX, machines_in, mock_plan, summarize
from warroom.agent.orchestrator import DISPATCHES, TYPES, rule_dispatch
from warroom.agent.state_machine import _brief
from warroom.contracts import ASK_TOOLS, ToolName, ToolResult

MAX_STEPS = 6
BUDGET_S = 60.0
MEMORY_TURNS = 8
READ_TOOLS = sorted(t.value for t in ASK_TOOLS)
ORCH_TOOLS = [*DISPATCHES, "discuss_waiting_plan"]
ALL_TOOLS = READ_TOOLS + ORCH_TOOLS

IDENTITY_REPLY = "我是主 agent，戰情室的調度員。可以陪你看狀況、查 SOP，需要動到現場的事我會派子 agent 擬計劃，你核准後才執行。"
HELLO_REPLY = "你好，我是主 agent。要查狀況或安排事情，直接說就好。"
THANKS_REPLY = "不客氣，有事再叫我。"
GIBBERISH_REPLY = "這句我看不懂，可以再說一次嗎？例如：「#M-201 會不會晚交？」"
UNCLEAR_REPLY = "我不太確定你要我做什麼。你是想查狀況，還是要我安排一件事？例如：「7 號機現在怎樣？」"
NO_OBJECT = "要派人做事，我需要知道是哪台機台、哪張訂單、哪種原料或哪個班。"

_IDENTITY_RX = re.compile(r"(?:你|妳|您)(?:是誰|是什麼|叫什麼|能做什麼|會做什麼|會什麼|可以做什麼)|介紹(?:一下)?(?:你|妳|您)自己|who are you", re.IGNORECASE)
_CJK_RX = re.compile(r"[㐀-鿿]")
_ORDER_RX = re.compile(r"#\s?[A-Z]-?\d{2,4}", re.IGNORECASE)
_MATERIAL_RX = re.compile(r"原料|橡膠|碳黑|鋼絲|尼龍|硫化劑|補料|缺料|進貨|庫存|採購")
_PEOPLE_RX = re.compile(r"人力|人手|調人|加班|請假|缺人|病假|夜班|早班|中班|排班|班表|組長")
_TASK_RX = re.compile(r"請|幫我|安排|通知|重排|調人|加班|補料|檢查|保養|處理|派|排程|停用|重新|改")
_QUESTION_RX = re.compile(r"[?？]|嗎|什麼|哪|多少|幾|怎樣|怎麼|如何|狀況|狀態|情況|是否|有沒有")
_CHANGE_RX = re.compile(r"保養|不要|不可|不能用|改成|改用|換成|延後|提前|今晚|明天|為什麼|為何|加班|調人|跨組")

SYSTEM = """你是實心胎工廠戰情室的「主 agent」（調度員），用繁體中文、像同事一樣自然地跟主管對話。
每次只輸出一個 JSON 物件：
- 要查資料或派工作時：{"action": "tool", "tool": "<工具>", "args": {...}, "reply": ""}
- 要回覆主管時：{"action": "reply", "tool": "", "args": {}, "reply": "<2–3 句短回覆>"}
工具：
- read_sensors {machine_ids: [id], minutes: 30}：機台讀值
- predict_anomaly {machine_id, channel: "temp_c", horizon_min: 30}：預測
- cure_formula {size: "S"|"M"|"L", actual_temp_c, elapsed_h}：硫化時間
- query_orders_shifts {order_ids?, press_ids?}：訂單交期、班表、人力缺口、下一模
- sop_search {query, k: 2}：知識庫（SOP），回答「怎麼處理」一定先查
- dispatch_machine_task / dispatch_order_task / dispatch_material_task / dispatch_staff_task {task: "<要完成的事>"}：
  派一個子 agent 擬計劃（它擬好會送主管核准，核准後才執行）。只有主管要「做一件事」且說得出對象（機台、訂單、原料、人）時才派。
- discuss_waiting_plan {text}：主管在談「目前選的待核准計劃」（加條件、問為什麼）時用。
規則：
0. 只回應 message 這一句；earlier_messages 是主管前面說過的話，只在 message 接著前文時參考（例如「那 8 號呢？」）。
1. 你不能直接改任何東西；要動到現場只能派子 agent，計劃要主管按「核准並派發」。
2. 數字只能來自工具結果或 context，不要自己估。
3. 打招呼、問你是誰、閒聊：直接 reply，不呼叫工具；問你是誰就說「我是主 agent」＋你能做什麼（查狀況、查 SOP、派子 agent 擬計劃）；「你好」就打招呼回去，問主管要查什麼或安排什麼。看不懂的字：請主管再說一次，不要派工作。
4. 回覆要短：2–3 句，不要長篇解釋。
5. 主管用「請／幫我／安排／重排／通知／調人／補料」要你做事，而且說得出對象時，一定要呼叫對應的 dispatch 工具（可先查資料），
   然後回覆「已請 X 子 agent 擬計劃，擬好會送你核准」。例：
   「#M-201 可能晚交，請重排保交期」→ dispatch_order_task；「9 號機明天保養，幫我安排」→ dispatch_machine_task；
   「碳黑快沒了，請補料」→ dispatch_material_task；「夜班缺一人，請調人」→ dispatch_staff_task。
機台代號：CURE-01..CURE-11 = 1..11 號硫化機；A-BIGOPEN = A 線大開煉機；其他依 context。"""


def step_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["action", "tool", "args", "reply"],
            "properties": {"action": {"type": "string", "enum": ["tool", "reply"]},
                           "tool": {"type": "string", "enum": ["", *ALL_TOOLS]},
                           "args": {"type": "object"}, "reply": {"type": "string", "maxLength": 300}}}


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


def ungrounded(reply: str, source: str) -> list[str]:
    known = set(_NUM_RX.findall(source))
    known |= {n.split(".")[0] for n in known}
    return [n for n in _NUM_RX.findall(reply) if n not in known]


class MainAgent:
    """`run_tool(tool, args, call_id)` runs a read tool through the Registry (audited by the caller);
    `dispatch(type, task)` starts a subagent and returns its event id; `discuss(text)` talks about the selected plan."""

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
                   dispatch: Callable[[str, str], Awaitable[dict[str, Any]]],
                   discuss: Callable[[str], Awaitable[dict[str, Any]]] | None,
                   slot: Callable[[], Any] | None = None) -> dict[str, Any]:
        t0 = time.perf_counter()
        tid = f"MAIN-{next(self._ids):04d}"
        steps: list[dict[str, Any]] = []
        out: dict[str, Any] = {"id": tid, "reply": "", "steps": steps, "event_id": None, "by": "rules",
                               "fell_back": False, "error": None, "intent": None}
        if is_gibberish(text):
            out.update(reply=GIBBERISH_REPLY, intent="unclear")
            return self._done(out, text, t0)
        if self.llm and self.client is not None:
            try:
                await asyncio.wait_for(self._model_loop(text, context, waiting_plan, out, run_tool, dispatch, discuss, tid,
                                                        slot), BUDGET_S)
                out["by"] = "model"
                return self._done(out, text, t0)
            except Exception as e:  # noqa: BLE001 - the rules take the turn; the reply says so
                out.update(fell_back=True, error=f"{type(e).__name__}: {e}"[:200])
                if out["event_id"]:                       # a dispatch already happened: never dispatch twice
                    out["reply"] = out["reply"] or self._dispatch_reply(steps)
                    return self._done(out, text, t0)
                steps.clear()
        await self._rule_turn(text, context, waiting_plan, out, run_tool, dispatch, discuss, tid)
        return self._done(out, text, t0)

    def _done(self, out: dict[str, Any], text: str, t0: float) -> dict[str, Any]:
        out["reply"] = (out["reply"] or UNCLEAR_REPLY).strip()
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        self.memory.append((text, out["reply"]))
        return out

    # ---------------------------------------------------------------- tools (shared by model and rules)
    async def _tool(self, name: str, args: dict[str, Any], text: str, out: dict[str, Any], tid: str,
                    run_tool, dispatch, discuss) -> str:
        steps = out["steps"]
        if name in DISPATCHES:
            task = str((args or {}).get("task") or text)
            if not has_object(task) and not has_object(text):
                steps.append({"tool": name, "args": args, "ok": False, "summary": "沒有具體對象，沒有派發", "label": "沒有派發"})
                return NO_OBJECT
            if out["event_id"]:
                return "這一輪已經派過一個子 agent 了。"
            d = await dispatch(DISPATCHES[name], task)
            out["event_id"] = d.get("event_id")
            label = f"派給{TYPES[DISPATCHES[name]]}"
            summ = f"已請{TYPES[DISPATCHES[name]]}子 agent 擬計劃（{d.get('event_id')}）；擬好會送主管核准，核准後才執行"
            steps.append({"tool": name, "args": {"task": task}, "ok": True, "summary": summ, "label": label,
                          "event_id": d.get("event_id")})
            return summ
        if name == "discuss_waiting_plan":
            if discuss is None:
                steps.append({"tool": name, "args": args, "ok": False, "summary": "目前沒有選待核准的計劃", "label": "沒有計劃可討論"})
                return "目前沒有選待核准的計劃。"
            r = await discuss(str((args or {}).get("text") or text))
            reply = str(r.get("reply") or "")
            steps.append({"tool": name, "args": args, "ok": True, "summary": reply[:200], "label": "討論計劃"})
            return reply
        if name not in READ_TOOLS:
            steps.append({"tool": name, "args": args, "ok": False, "summary": "不允許：主 agent 沒有寫入工具", "label": f"攔下 {name}"})
            return f"{name} 不能用：主 agent 只能查資料或派子 agent。"
        res = await run_tool(name, args or {}, f"{tid}:T{len(steps) + 1}")
        summ = summarize(ToolName(name), res.data) if res.ok else f"失敗：{res.error}"
        label = _label(name, args or {}, res)
        steps.append({"tool": name, "args": args, "ok": res.ok, "summary": summ, "label": label})
        return summ

    @staticmethod
    def _dispatch_reply(steps: list[dict[str, Any]]) -> str:
        d = next((s for s in steps if s["tool"] in DISPATCHES and s["ok"]), None)
        return f"好，已請{d['label'].removeprefix('派給')}子 agent 擬計劃，擬好會送你核准，核准後才執行。" if d else UNCLEAR_REPLY

    # ---------------------------------------------------------------- the model loop
    async def _model_loop(self, text, context, waiting_plan, out, run_tool, dispatch, discuss, tid, slot=None) -> None:
        msgs: list[dict[str, str]] = [{"role": "system", "content": SYSTEM + plant.section()}]
        # earlier turns ride along as reference inside the one user message, the supervisor's words only. Real runs
        # (2026-10-21): replayed as chat turns, Nemotron answered the previous topic (「你好」 → 7 號機讀值); with its own
        # answers alongside, it copied them (「大開煉機過熱怎麼處理」 → the greeting again).
        ctx = {"message": text, "earlier_messages": [q for q, _a in self.memory], "context": context, "waiting_plan": waiting_plan}
        msgs.append({"role": "user", "content": json.dumps(ctx, ensure_ascii=False, default=str)})
        from warroom.agent.commander import thinking_off
        extra = thinking_off(self.server_kind)
        source = text + " " + json.dumps(context, ensure_ascii=False, default=str) + " " + (waiting_plan or "")
        nudged = waiting_plan is not None             # a message about the selected plan is a discussion, not a new task
        called: set[tuple[Any, str]] = set()
        looked = waiting_plan is not None
        for i in range(MAX_STEPS + 1):
            # one model slot per call (never across a tool: a discussion calls the commander, which needs a slot too)
            async with (slot() if slot else contextlib.nullcontext()):
                r = await self.client.chat.completions.create(
                    model=self.settings.model, temperature=0.0, max_tokens=300, messages=msgs,
                    response_format={"type": "json_schema", "json_schema": {"name": "MainStep", "schema": step_schema()}},
                    **({"extra_body": extra} if extra else {}))
            raw = (r.choices[0].message.content or "").strip()
            d = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw))
            # Nemotron sometimes writes {"action": "reply", "tool": "read_sensors", "reply": ""}: that is a tool call
            call = (d.get("tool"), json.dumps(d.get("args") or {}, sort_keys=True, ensure_ascii=False))
            if d.get("tool") and call in called and i < MAX_STEPS:
                # the same call again: the result is already above, so ask for the reply instead of looping
                msgs += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"{d['tool']} 剛查過了，結果在上面。現在用 2–3 句回覆主管（action=reply）。"}]
                continue
            called.add(call)
            if (d.get("action") == "tool" or not str(d.get("reply") or "").strip()) and d.get("tool") and i < MAX_STEPS and not (out["event_id"] and d["tool"] in DISPATCHES):
                result = await self._tool(d["tool"], d.get("args") or {}, text, out, tid, run_tool, dispatch, discuss)
                source += " " + result
                msgs += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"工具 {d['tool']} 結果：{result}\n接著：再查一個工具，或用 2–3 句回覆主管。"}]
                continue
            reply = str(d.get("reply") or "").strip()
            if not reply and out["steps"]:            # out of steps with results in hand: the results speak
                reply, out["error"] = self._results_reply(out["steps"]) or self._dispatch_reply(out["steps"]), f"no reply after tools: {raw[:120]}"
            if not reply:
                raise ValueError(f"empty reply: {raw[:160]}")
            if not out["steps"] and wants_lookup(text) and waiting_plan is None:
                read = [st for st in mock_plan(text, context).steps if st.tool in READ_TOOLS]
                if not looked and read and i < MAX_STEPS:
                    # a plant question answered without looking: remind the model once which tool answers it
                    looked = True
                    msgs += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": f"主管在問工廠的事，不是閒聊。先呼叫 {read[0].tool} 查資料，再用 2–3 句回覆。"}]
                    continue
                if read:                              # still nothing read: code reads by the rules, the results speak
                    for st in read[:MAX_STEPS]:
                        await self._tool(st.tool, st.args, text, out, tid, run_tool, dispatch, discuss)
                    out.update(reply=self._results_reply(out["steps"]) or reply, fell_back=True,
                               error="model answered a plant question without a tool; read by rules")
                    return
            if not nudged and not out["event_id"] and wants_task(text) and i < MAX_STEPS:
                # the supervisor asked for something to be done: remind the model once that a dispatch does that
                nudged = True
                msgs += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": "主管是要你「做」這件事，不只是查。請呼叫對應的 dispatch_* 工具派子 agent 擬計劃，再回覆。"}]
                continue
            if not out["event_id"] and wants_task(text) and waiting_plan is None:
                # asked twice, still no dispatch call: code dispatches by the rules (never claim one that did not happen)
                name, _why = rule_dispatch(text)
                await self._tool(name, {"task": text}, text, out, tid, run_tool, dispatch, discuss)
                out.update(reply=self._dispatch_reply(out["steps"]), fell_back=True,
                           error="model did not call a dispatch tool; dispatched by rules")
                return
            bad = ungrounded(reply, source)
            if bad:                                   # a number the tools did not give: the results speak instead
                out["error"] = f"ungrounded numbers {bad}"
                reply = self._results_reply(out["steps"]) or reply
            if out["event_id"]:                       # the dispatch line is code's (never 「子 agent 已擬好計劃」 before it has)
                reply = self._dispatch_reply(out["steps"])
            out["reply"] = reply
            return
        raise TimeoutError("too many tool steps")

    @staticmethod
    def _results_reply(steps: list[dict[str, Any]]) -> str:
        """Short: a SOP is named, not pasted (the full lines stay in the reply's tool line)."""
        ok = []
        for s in steps:
            if not (s["ok"] and s["summary"]) or s["tool"] in DISPATCHES:
                continue
            sops = re.findall(r"(SOP-[A-Z]+-\d+)〈([^〉]+)〉", s["summary"]) if s["tool"] == "sop_search" else []
            ok.append("請照 " + "、".join(f"{a}〈{b}〉" for a, b in sops[:2]) + " 處理" if sops else s["summary"][:90].rstrip("；，、 "))
        return ("查到：" + "；".join(ok)[:220] + "。") if ok else ""

    # ---------------------------------------------------------------- rules (rule mode, or the model failed)
    async def _rule_turn(self, text, context, waiting_plan, out, run_tool, dispatch, discuss, tid) -> None:
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
        if waiting_plan and discuss is not None and (re.search(r"為什麼|為何", t) or (_CHANGE_RX.search(t) and not asks)):
            out["intent"] = "change_plan"
            out["reply"] = await self._tool("discuss_waiting_plan", {"text": t}, t, out, tid, run_tool, dispatch, discuss)
            return
        imperative = bool(re.search(r"請|幫我|安排|通知|重排|調人|加班|補料|派", t))
        if _TASK_RX.search(t) and not asks and has_object(t) and (imperative or not _QUESTION_RX.search(t)):
            name, _why = rule_dispatch(t)
            out["intent"] = "new_task"
            await self._tool(name, {"task": t}, t, out, tid, run_tool, dispatch, discuss)
            out["reply"] = self._dispatch_reply(out["steps"])
            return
        if _QUESTION_RX.search(t) or asks or re.search(r"SOP|怎麼處理|處置", t):
            out["intent"] = "question"
            plan = mock_plan(t, context)
            for s in plan.steps[:MAX_STEPS]:
                await self._tool(s.tool, s.args, t, out, tid, run_tool, dispatch, discuss)
            out["reply"] = self._results_reply(out["steps"]) or "這個我手上的資料查不到。你想知道哪台機台或哪張訂單？"
            return
        out.update(reply=UNCLEAR_REPLY, intent="unclear")


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
