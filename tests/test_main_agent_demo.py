"""主 agent conversation, demo walk-through 10-21 (group A): what the checkers saw with a plan selected and with the real
model — a claimed change that never happened, a new task taken as a plan discussion, impossible plan changes, an order
question answered with no tool, Markdown and tool names in the reply."""
from __future__ import annotations

import json
from types import SimpleNamespace

from warroom.agent.ask import mock_plan, orders_in, summarize
from warroom.agent.main_agent import (
    GIBBERISH_REPLY,
    HELLO_REPLY,
    IDENTITY_REPLY,
    KB_LOADING,
    NOT_CHANGED,
    NOT_DONE,
    PLAN_EDIT_Q,
    SYSTEM,
    MainAgent,
    names_other,
    new_task,
    plan_edit,
    tidy,
)
from warroom.contracts import ToolName, ToolResult

# the page-11 round-1 card as /api/main/message passes it (title, actions, A／B options)
PLAN = ("7 號機溫度下降 128→122.3°C（v1）。要做的事：S4 排程 v2 生效（7 號硫化 28.5 h；無機台異動）；"
        "S4b 7 號機硫化總時數 28.5 h（公式 28.5 h）；人力：依所選方案（A 夜班加班／B 從 2 線調 1 人）。"
        "人力方案：A 夜班加班；B 從 2 線調 1 人（系統建議）")
ORDERS = [{"id": "#A-311", "size": "S", "qty": 4, "done": 0, "due": "2026-10-06"},
          {"id": "#S-101", "size": "S", "qty": 40, "done": 30, "due": "2026-10-07"}]
JOBS = [{"press_id": "CURE-07", "order_id": "#A-311", "mold_in": "2026-10-04T00:00:00+08:00",
         "demold": "2026-10-05T00:00:00+08:00"}]


def _settings():
    return SimpleNamespace(llm_mode="llm", model="nemotron-test", base_url="http://x/v1", llm_timeout_s=10.0,
                           api_key_effective="none")


class Model:
    """A native tool-calling model from a script: ("tool", name, args) or ("say", text)."""

    def __init__(self, *script):
        self.script, self.calls = list(script), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append({**kw, "messages": list(kw.get("messages") or [])})   # the loop keeps appending to its list
        step = self.script.pop(0) if self.script else ("say", "")
        if step[0] == "tool" and "tools" in kw:
            call = SimpleNamespace(id=f"c{len(self.calls)}", function=SimpleNamespace(
                name=step[1], arguments=json.dumps(step[2], ensure_ascii=False)))
            msg = SimpleNamespace(content="", tool_calls=[call])
        else:
            msg = SimpleNamespace(content=step[1] if step[0] == "say" else "", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    def offered(self, i=0):
        return [t["function"]["name"] for t in self.calls[i].get("tools") or []]


class Floor:
    def __init__(self, discuss_result=None, sop_error=None):
        self.tools, self.proposed, self.discussed = [], [], []
        self.discuss_result = discuss_result or {"reply": "這個要求我沒辦法用程式保證做到。計劃沒有改。", "replanned": False}
        self.sop_error = sop_error

    async def run_tool(self, name, args, call_id):
        self.tools.append((name, args))
        if name == "sop_search" and self.sop_error:
            return ToolResult(call_id=call_id, tool=ToolName(name), ok=False, error=self.sop_error)
        if name == "query_orders_shifts":
            ids = args.get("order_ids") or []
            data = {"orders": [o for o in ORDERS if not ids or o["id"] in ids], "gaps": [],
                    "next_jobs": [j for j in JOBS if not ids or j["order_id"] in ids]}
        elif name == "read_sensors":
            data = {"summary": {"CURE-07": {"temp_c_now": 122.3}}}
        else:
            data = {"hits": [{"sop_id": "SOP-CP-02", "title": "硫化機溫度下降處置", "text": "確認蒸汽壓"}]}
        return ToolResult(call_id=call_id, tool=ToolName(name), ok=True, data=data)

    async def propose(self, task):
        self.proposed.append(task)
        return {"event_id": f"EV-{len(self.proposed)}"}

    async def discuss(self, text):
        self.discussed.append(text)
        return self.discuss_result

    async def turn(self, agent, text, plan=None):
        return await agent.turn(text, context={"abnormal": ["CURE-07"]}, waiting_plan=plan, run_tool=self.run_tool,
                                propose=self.propose, discuss=self.discuss if plan else None)


# ---------------------------------------------------------------- A1: no claimed change without one
async def test_gibberish_with_a_plan_selected_claims_nothing():
    """Real run: 「vu04y94u.3s83uv,」 with the 7 號機 card → 「目標溫度已改成 122°C，這份待核准計畫現在已更新」, event_id=null."""
    m = Model(("say", "目標溫度已改成 122°C，這份待核准計畫現在已更新。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "vu04y94u.3s83uv,", PLAN)
    assert out["reply"] == GIBBERISH_REPLY and out["event_id"] is None and not f.discussed
    assert "discuss_waiting_plan" not in m.offered()          # no words to change a plan with
    assert not plan_edit("vu04y94u.3s83uv,") and plan_edit("降到 120") and not plan_edit("7 號機現在怎樣")
    m = Model(("tool", "propose_plan", {"task": "7 號機目標溫度改成 122°C"}), ("say", "已擬計劃送你核准。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "vu04y94u.3s83uv,", PLAN)
    assert not f.proposed and out["event_id"] is None and out["reply"] == GIBBERISH_REPLY


async def test_a_discussion_that_changed_nothing_says_so():
    m = Model(("tool", "discuss_waiting_plan", {"text": "目標溫度改成 120°C"}), ("say", "好的，這份待核准計畫現在已更新。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "降到 120", PLAN)
    assert f.discussed == ["目標溫度改成 120°C"]
    assert out["reply"] == "這個要求我沒辦法用程式保證做到。計劃沒有改。" and "已更新" not in out["reply"]


async def test_a_discussion_that_replanned_asks_to_approve_the_new_version():
    f = Floor({"reply": "好，已依「不加班」重排。新核准卡 v2 取代 v1：…", "replanned": True,
               "approval": {"version": 2}, "constraints": [{"label": "不加班"}]})
    m = Model(("tool", "discuss_waiting_plan", {"text": "不要加班"}), ("say", "已經幫你改好了，計劃現在不加班。"))
    out = await f.turn(MainAgent(_settings(), client=m), "不要加班", PLAN)
    assert out["reply"] == "好，已改成第 2 版（不加班），請重新核准。"


async def test_a_claimed_plan_without_propose_is_replaced():
    m = Model(("say", "好，已擬計劃送你核准。"))
    out = await Floor().turn(MainAgent(_settings(), client=m), "9 號機明天保養")
    assert out["reply"] == NOT_DONE and out["event_id"] is None
    m = Model(("say", "這份待核准計畫現在已更新。"))
    out = await Floor().turn(MainAgent(_settings(), client=m), "嗯嗯", PLAN)
    assert out["reply"] == NOT_CHANGED


async def test_a_status_question_keeps_the_models_words():
    """Review 10-21: a question that asks for no change describes the plant (「已擬好…計劃」 is the commander's), and a
    number from the context (「7 號機」 = CURE-07) is no reason to answer 「這個我沒有查到資料」."""
    say = "7 號機溫度續降，AI 指揮官已擬好停用 7 號機的計劃，等你核准。"
    out = await Floor().turn(MainAgent(_settings(), client=Model(("say", say))), "現在狀況怎樣？", PLAN)
    assert out["reply"] == say
    say = "有 1 台異常：7 號機 122.3°C，AI 指揮官處理中。"
    out = await Floor().turn(MainAgent(_settings(), client=Model(("say", say))), "工廠現在還好嗎")
    assert out["reply"] == say
    m = Model(("say", "好，已改成不加班。"), ("say", "好，已改成不加班。"))   # a question that asks for a change
    out = await Floor().turn(MainAgent(_settings(), client=m), "可以改成不加班嗎？", PLAN)
    assert out["reply"] == NOT_CHANGED


# ---------------------------------------------------------------- A2: a new task while a card is selected
def test_new_task_while_a_plan_is_selected():
    assert new_task("#M-201 可能晚交，請重排保交期", PLAN)
    assert not new_task("不要跨組調人", PLAN)                      # a change to this plan
    assert not new_task("9 號機今晚要保養，請重排", PLAN)          # a constraint discuss can encode
    assert not new_task("請安排 7 號機檢查蒸汽", PLAN)             # the plan's own machine
    assert not new_task("#M-201 可能晚交，請重排保交期", None)
    assert names_other("#A-311 會不會晚交？", PLAN) and not names_other("7 號機現在怎樣", PLAN)


async def test_new_task_with_a_plan_selected_is_proposed():
    """homeSelection selects the first card, so ChatBar sends its event_id with every message."""
    m = Model(("say", "好的，我了解。"), ("say", "好的，我了解。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "#M-201 可能晚交，請重排保交期", PLAN)
    assert f.proposed == ["#M-201 可能晚交，請重排保交期"] and out["event_id"] == "EV-1" and not f.discussed
    assert "discuss_waiting_plan" not in m.offered() and "propose_plan" in m.offered()
    f = Floor()                                                  # rule mode: the same, never a discussion
    out = await f.turn(MainAgent(), "#M-201 可能晚交，請重排保交期", PLAN)
    assert f.proposed and not f.discussed and "已擬計劃送你核准" in out["reply"]


# ---------------------------------------------------------------- A3 / A4: what a plan can change
def test_prompt_lists_only_supported_plan_changes():
    assert "目標溫度" not in SYSTEM and "執行時間" not in SYSTEM and "換機台" not in SYSTEM
    assert "不要加班" in SYSTEM and "不要跨組調人" in SYSTEM and "改選 A" in SYSTEM and "不可以建議降溫" in SYSTEM


async def test_vague_edit_never_offers_impossible_changes():
    """Real run: 「計劃改」 → 「調低到 118°C、加快降溫、縮短硫化時間」."""
    m = Model(("say", "可以調低到 118°C，或加快降溫、縮短硫化時間。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "計劃改", PLAN)
    assert out["reply"] == PLAN_EDIT_Q and "discuss_waiting_plan" not in m.offered()
    m = Model(("say", "可以加快降溫，或縮短硫化時間。"))           # no number: the process-sense guard
    out = await Floor().turn(MainAgent(_settings(), client=m), "怎改", PLAN)
    assert out["reply"] == PLAN_EDIT_Q


async def test_an_edit_answered_without_discussing_gets_the_supported_changes():
    """「降到 120」, reminded once to discuss, still answers with words and an invented number."""
    m = Model(("say", "好，目標溫度改成 118°C。"), ("say", "好，目標溫度改成 118°C。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "降到 120", PLAN)
    assert out["reply"] == PLAN_EDIT_Q and not f.discussed and len(m.calls) == 2


async def test_rule_mode_plan_edits():
    for text in ("計劃改", "怎改"):
        f = Floor()
        out = await f.turn(MainAgent(), text, PLAN)
        assert out["reply"] == PLAN_EDIT_Q and not f.discussed, text
    f = Floor()
    out = await f.turn(MainAgent(), "降到 120", PLAN)
    assert f.discussed == ["降到 120"] and out["reply"] == "這個要求我沒辦法用程式保證做到。計劃沒有改。"


# ---------------------------------------------------------------- A5: a new scenario, a new conversation
async def test_clear_forgets_the_last_rehearsal():
    agent = MainAgent(_settings(), client=Model(("say", "好。")))
    await Floor().turn(agent, "降到 120 怎麼樣")
    agent.clear()
    m = Model(("say", "7 號機在 122.3°C。"))
    agent.client = m
    await Floor().turn(agent, "那 7 號機呢？")
    assert "降到 120" not in m.calls[0]["messages"][1]["content"]


# ---------------------------------------------------------------- A6: order questions read the orders
def test_order_question_reads_the_order():
    assert orders_in("#a311 會不會晚交？ # A-311") == ["#A-311"]
    steps = mock_plan("#A-311 會不會晚交？", {"abnormal": ["CURE-07"]}).steps
    assert [(s.tool, s.args) for s in steps] == [("query_orders_shifts", {"order_ids": ["#A-311"]})]
    for q in ("什麼時候出貨？", "交貨會晚嗎"):
        assert [s.tool for s in mock_plan(q, {"abnormal": []}).steps] == ["query_orders_shifts"], q
    line = summarize(ToolName.QUERY_ORDERS_SHIFTS, {"orders": ORDERS[:1], "next_jobs": JOBS}, {"order_ids": ["#A-311"]})
    assert line == "#A-311（S，0/4，交期 2026-10-06）：7 號 10-04 00:00 入模・10-05 00:00 出模"
    assert summarize(ToolName.QUERY_ORDERS_SHIFTS, {"orders": []}, {"order_ids": ["#M-201"]}) == "沒有這張訂單：#M-201"


async def test_order_question_rule_mode_and_unknown_order():
    f = Floor()
    out = await f.turn(MainAgent(), "#A-311 會不會晚交？", PLAN)
    assert f.tools == [("query_orders_shifts", {"order_ids": ["#A-311"]})] and "交期 2026-10-06" in out["reply"]
    f = Floor()
    out = await f.turn(MainAgent(), "#M-201 會不會晚交？")
    assert out["reply"].startswith("沒有這張訂單：#M-201")
    m = Model(("tool", "query_orders_shifts", {"order_ids": ["M-201"]}), ("say", "#M-201 交期是 10/06。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "#M-201 會不會晚交？")
    assert f.tools[0][1] == {"order_ids": ["#M-201"]} and out["reply"].startswith("沒有這張訂單：#M-201")


async def test_order_question_with_a_plan_selected_is_looked_up():
    """The real model answered 「#A-311 會不會晚交？」 without reading; #A-311 is not on the 7 號機 card."""
    m = Model(("say", "應該不會晚交。"), ("say", "應該不會晚交。"))
    f = Floor()
    out = await f.turn(MainAgent(_settings(), client=m), "#A-311 會不會晚交？", PLAN)
    assert [t for t, _ in f.tools] == ["query_orders_shifts"] and "交期 2026-10-06" in out["reply"]
    assert GIBBERISH_REPLY.count("#A-311") == 1 and "#M-201" not in GIBBERISH_REPLY


# ---------------------------------------------------------------- A7: plain short replies
def test_tidy_reply():
    r = tidy("收到，主管。我是戰情室的 agent，可以幫你查資料。**7 號機**現在 122.3°C（discuss_waiting_plan 回覆：計劃未變更）。"
             "\n\n第二段。\n\n第三段。\n\n第四段。\n\n第五段。\n\n第六段。", keep_intro=False)
    assert r == "7 號機現在 122.3°C。第二段。第三段。第四段。第五段。"        # at most 5 sentences (was 3 before 10-21)
    assert tidy("好，已擬計劃送你核准。", keep_intro=False) == "好，已擬計劃送你核准。"
    assert tidy("- 先確認蒸汽壓\n- 再看水流", keep_intro=False) == "先確認蒸汽壓；再看水流"
    assert tidy("7 號機正常，我是根據讀值判斷的。", keep_intro=False) == "7 號機正常，我是根據讀值判斷的。"


async def test_model_reply_is_tidied():
    m = Model(("tool", "read_sensors", {"machine_ids": ["7"]}),
              ("say", "收到，主管。我是戰情室的 agent。**7 號機**現在 122.3°C（read_sensors 結果）。"))
    out = await Floor().turn(MainAgent(_settings(), client=m), "7 號機現在怎樣？")
    assert out["reply"] == "7 號機現在 122.3°C。"


# ---------------------------------------------------------------- A8
async def test_results_reply_and_grounding_sources():
    f = Floor()
    out = await f.turn(MainAgent(), "不要跨組調人", PLAN)              # a discussion is not 「查到：」
    assert out["reply"] == "這個要求我沒辦法用程式保證做到。計劃沒有改。"
    assert "。。" not in MainAgent._results_reply([{"tool": "read_sensors", "ok": True, "summary": "7 號機：溫度 122.3°C。"}])
    agent = MainAgent(_settings(), client=Model(("say", "好，要我查什麼？")))
    await Floor().turn(agent, "7 號機如果降到 118 會怎樣")
    agent.client = Model(("say", "118°C 低於硫化下限。"))
    out = await Floor().turn(agent, "那呢")                          # 118 came from the supervisor's earlier message
    assert out["reply"] == "118°C 低於硫化下限。"


async def test_canned_replies_skip_the_model():
    m = Model()
    agent = MainAgent(_settings(), client=m)
    f = Floor()
    assert (await f.turn(agent, "你好"))["reply"] == HELLO_REPLY
    assert (await f.turn(agent, "妳是誰"))["reply"] == IDENTITY_REPLY
    assert (await f.turn(agent, "謝謝"))["reply"]
    assert not m.calls
    agent.memory.append(("7 號機要處理嗎", "要不要我擬計劃送你核准？"))
    agent.client = m2 = Model(("say", "好，我先查一下。"))
    await f.turn(agent, "好")                                         # an answer to its own question goes to the model
    assert m2.calls


async def test_sop_timeout_while_the_knowledge_base_loads():
    m = Model(("tool", "sop_search", {"query": "溫度下降"}), ("say", "timeout after 10 s"))
    f = Floor(sop_error="timeout after 10 s")
    out = await f.turn(MainAgent(_settings(), client=m), "硫化機溫度掉下來怎麼處理？")
    assert out["reply"] == KB_LOADING and out["steps"][0]["summary"] == "失敗：知識庫還在載入"
