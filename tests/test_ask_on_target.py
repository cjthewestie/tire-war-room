"""問戰情室 answers what was asked (issue #182, CJ: 「絕對不能讓 ai 答非所問，要適當的回答」).

Spark audit, 10-03, real model:
  - 「SO-004 什摸時候完成」 -> 「查不到。」 although the tool result held SO-004 (0/19, due 10-15);
  - 「現在7號機溫度」 then 「那8號呢」 -> a 4-step lookup (orders, sensors, forecast, cure_formula with 7 號's 115.11 °C)
    and an answer about cure hours and 「目前無排機偏好資訊」.
Plus: an invented number no longer swaps the answer for every tool's summary, and a question about a change
(「今天有加班嗎？」) gets a normal answer, only a change request gets facts only."""
from __future__ import annotations

import pytest

from tests.test_ask import _entries, _llm_agent, _mock_agent, _plan, audit, plant  # noqa: F401 - fixtures
from warroom.agent import order_ask
from warroom.agent.ask import (
    ANSWER_SYSTEM,
    PLAN_SYSTEM,
    change_request,
    follow_up_machines,
    orders_in,
    summarize,
)
from warroom.agent.convo import AskMemory, ConvContext
from warroom.contracts import AuditKind, ToolCall, ToolName


# ---------------------------------------------------------------- case 1: SO-004 什摸時候完成
@pytest.mark.parametrize("text, ids", [
    ("SO-004 什摸時候完成", ["SO-004"]),
    ("so-004 呢", ["SO-004"]),
    ("#SO004 進度", ["SO-004"]),
    ("#a311 會不會晚交？ # A-311", ["#A-311"]),
    ("A-311 在哪", ["#A-311"]),
    ("CURE-07 下一模", []),                       # a machine, never an order
    ("BOND-01 溫度", []),
])
def test_orders_in_reads_dataset_ids(text, ids):
    assert orders_in(text) == ids


@pytest.mark.parametrize("q", ["SO-004 什摸時候完成", "SO-004 什麼時候完成", "SO-004 甚麼時候好", "SO-004 幾點好",
                               "SO-004 何時做好"])
def test_when_done_is_a_where_question(q):
    intent = order_ask.classify(q, has_machine=False, memory=ConvContext())
    assert intent is not None and intent.kind == "where" and intent.ids == ("SO-004",)


async def test_so004_when_done_goes_to_the_model_with_the_order_facts(plant, audit):
    """issue #195: with a model the order question is the model's (no code fast path in front); its facts are the order
    list's own line (progress, machines, due, 預計完成), so it is never a bare 查不到."""
    ctx, reg = plant
    agent = _llm_agent(_plan(("query_orders_shifts", {"order_ids": ["SO-004"]}), intent="question"),
                       "SO-004 還沒開始做，預計完成時間要等排程。")
    a = await agent.ask("SO-004 什摸時候完成", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert len(agent.client.calls) == 2 and a.answer == "SO-004 還沒開始做，預計完成時間要等排程。"
    facts = agent.client.calls[1]["messages"][1]["content"]
    assert "SO-004" in facts and "預計完成" in facts and "查無訂單" not in facts
    assert [s.tool for s in a.steps] == ["query_orders_shifts"] and a.steps[0].args == {"order_ids": ["SO-004"]}


async def test_so004_when_done_without_a_model_is_answered_by_code(plant, audit):
    """Rule mode (LLM_MODE=mock) keeps the keyword routes as the fallback: the order fast path answers."""
    ctx, reg = plant
    a = await _mock_agent().ask("SO-004 什摸時候完成", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer.startswith("SO-004 的機台與預計完成") and "查不到" not in a.answer


async def test_order_ids_match_however_written(plant):
    """The tool and the summary compare ids the way people write them: '#so-004' is SO-004."""
    ctx, reg = plant
    res = await reg.acall(ToolCall(id="t1", event_id="t", step_id="Q1", tool=ToolName.QUERY_ORDERS_SHIFTS,
                                   args={"order_ids": ["#so-004"]}))
    assert res.ok and [o["id"] for o in res.data["orders"]] == ["SO-004"]
    line = summarize(ToolName.QUERY_ORDERS_SHIFTS, res.data, {"order_ids": ["#so-004"]})
    assert "SO-004" in line and "預計完成" in line and "查無訂單" not in line and "沒有這張訂單" not in line
    missing = summarize(ToolName.QUERY_ORDERS_SHIFTS, res.data, {"order_ids": ["SO-999"]})
    assert "SO-999" in missing and ("查無訂單" in missing or "沒有這張訂單" in missing)


async def test_bare_not_found_with_data_gets_one_rewrite(plant, audit):
    """「今晚夜班缺誰？」 -> 「查不到。」 while the result says 人力缺口：…: the model is asked once more, with why."""
    ctx, reg = plant
    agent = _llm_agent(_plan(("query_orders_shifts", {})), "查不到。", "今晚夜班的人力缺口如查詢結果所列。")
    a = await agent.ask("今晚夜班缺誰？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer == "今晚夜班的人力缺口如查詢結果所列。"
    retry = agent.client.calls[2]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "查不到。"} and "查詢結果裡有資料" in retry[-1]["content"]
    ans = _entries(audit, a.id, AuditKind.LLM_CALL)[-1].payload
    assert ans["repair"] == "retry" and ans["repair_detail"]["first_answer"] == "查不到。"


# ---------------------------------------------------------------- case 2: 現在7號機溫度 -> 那8號呢
@pytest.mark.parametrize("q, mids", [
    ("那8號呢", ["CURE-08"]), ("那 8 號機呢？", ["CURE-08"]), ("8號呢", ["CURE-08"]), ("換 CURE-09", ["CURE-09"]),
    ("那 A 線大開煉機呢", ["A-BIGOPEN"]),
    ("8號會不會掉？", []), ("那8號下一模是什麼", []), ("那張單呢", []), ("現在狀況", []),
])
def test_follow_up_machines(q, mids):
    assert follow_up_machines(q) == mids


async def test_follow_up_within_a_session_reaches_the_model(plant, audit):
    """「那 8 號呢」 in the same conversation: the model plans it, given the previous question and its steps (this
    conversation only); the answer is told what the previous question was."""
    ctx, reg = plant
    mem = AskMemory()
    agent = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07"], "minutes": 30}), intent="question"),
                       "7 號機溫度偏低。",
                       _plan(("read_sensors", {"machine_ids": ["CURE-08"], "minutes": 30}), intent="question"),
                       "8 號機溫度正常。")
    kw = {"tools": reg, "audit": audit, "context": {"abnormal": ["CURE-07"]}, "now": ctx.clock.now(),
          "conversation_id": "task-A", "memory": mem}
    await agent.ask("現在7號機溫度", **kw)
    a = await agent.ask("那8號呢", **kw)
    plan_in = agent.client.calls[2]["messages"][1]["content"]
    assert '"previous_question": "現在7號機溫度"' in plan_in and "CURE-07" in plan_in
    assert [(s.tool, s.args) for s in a.steps] == [("read_sensors", {"machine_ids": ["CURE-08"], "minutes": 30})]
    assert a.answer == "8 號機溫度正常。"
    assert "上一題：「現在7號機溫度」" in agent.client.calls[3]["messages"][1]["content"]
    assert _entries(audit, a.id, AuditKind.LLM_CALL)[0].payload["planner"] == "llm"
    # another task's conversation has no previous question
    other = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-08"]}), intent="question"), "8 號機溫度正常。")
    await other.ask("那8號呢", **{**kw, "conversation_id": "task-B"})
    assert "previous_question" not in other.client.calls[0]["messages"][1]["content"]


async def test_follow_up_history_stays_in_its_conversation(plant, audit):
    """One conversation's question never steers another's (each task / page has its own conversation_id), and a
    question without a conversation_id has no history at all."""
    ctx, reg = plant
    mem = AskMemory()
    agent = _mock_agent()
    kw = {"tools": reg, "audit": audit, "context": {}, "now": ctx.clock.now(), "memory": mem}
    await agent.ask("7號機會不會再掉？", conversation_id="task-A", **kw)
    same = await agent.ask("那8號呢", conversation_id="task-A", **kw)
    other = await agent.ask("那8號呢", conversation_id="task-B", **kw)
    none = await agent.ask("那8號呢", conversation_id=None, **kw)
    assert [s.tool for s in same.steps] == ["read_sensors", "predict_anomaly"]
    assert [s.tool for s in other.steps] == ["read_sensors"] and [s.tool for s in none.steps] == ["read_sensors"]
    assert mem.get("supervisor", "task-B").last_question == "那8號呢"
    assert mem.get("supervisor", "task-A").last_question == "7號機會不會再掉？"
    assert {e.payload.get("msg") for e in _entries(audit, other.id, AuditKind.SYSTEM)} >= {"ask"}
    assert not [e for e in _entries(audit, other.id, AuditKind.SYSTEM) if e.payload.get("msg") == "follow-up"]


SPARK_PLAN_FOR_8 = _plan(("query_orders_shifts", {"press_ids": ["CURE-08"]}),
                         ("read_sensors", {"machine_ids": ["CURE-08"], "minutes": 30}),
                         ("predict_anomaly", {"machine_id": "CURE-08", "channel": "temp_c", "horizon_min": 30}),
                         ("cure_formula", {"actual_temp_c": 115.11, "elapsed_h": 2, "size": "M"}))


async def test_the_planner_gets_no_readings(plant, audit):
    """The planner is not handed readings to copy into args (Spark 10-03: 7 號's 115.11 °C went into 8 號's plan)."""
    ctx, reg = plant
    agent = _llm_agent(SPARK_PLAN_FOR_8, "8 號機溫度正常。")
    await agent.ask("那8號呢", tools=reg, audit=audit, context={"abnormal_temp_c": {"CURE-07": 115.11}},
                    now=ctx.clock.now())
    assert "115.11" not in agent.client.calls[0]["messages"][1]["content"]


async def test_cure_formula_for_8_gets_8s_plant_inputs(plant, audit):
    """When cure time is asked, the model's 115.11 °C (7 號's reading) is replaced by 8 號's own, from the plant."""
    ctx, reg = plant
    agent = _llm_agent(SPARK_PLAN_FOR_8, "8 號機硫化時數如結果。")
    a = await agent.ask("8號硫化還要多久", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert "cure_formula" in [s.tool for s in a.steps]
    calls = [e.payload["args"] for e in _entries(audit, a.id, AuditKind.TOOL_CALL)]
    assert all(c.get("actual_temp_c") != 115.11 for c in calls)
    bound = [e.payload for e in _entries(audit, a.id, AuditKind.SYSTEM)
             if "cure_formula_args_from_plant" in e.payload or "cure_formula_unbound" in e.payload]
    assert bound and (bound[0].get("press") == "CURE-08" or "8 號" in bound[0].get("error", ""))


def test_prompts_ask_for_the_asked_thing_only():
    assert "previous_question" in PLAN_SYSTEM and "只換對象" in PLAN_SYSTEM
    assert "第一句就直接回答" in ANSWER_SYSTEM and "目前無…資訊" in ANSWER_SYSTEM and "不要只回「查不到」" in ANSWER_SYSTEM


# ---------------------------------------------------------------- 3: an invented number, answered on topic
async def test_invented_number_gets_a_rewrite(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07"]})),
                       "7 號機 30 分鐘後會降到 110°C。", "7 號機溫度偏低，還在下降。")
    a = await agent.ask("7 號機？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer == "7 號機溫度偏低，還在下降。"
    assert "不在查詢結果裡：110" in agent.client.calls[2]["messages"][-1]["content"]
    ans = _entries(audit, a.id, AuditKind.LLM_CALL)[-1].payload
    assert ans["repair"] == "retry" and ans["ungrounded"] == ["110"]


async def test_invented_sentence_is_dropped(plant, audit):
    ctx, reg = plant
    bad = "7 號機溫度偏低。30 分鐘後會降到 110°C。"
    agent = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07"]})), bad, bad)
    a = await agent.ask("7 號機？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer == "7 號機溫度偏低。"
    assert _entries(audit, a.id, AuditKind.LLM_CALL)[-1].payload["repair"] == "dropped_sentences"


async def test_last_resort_template_has_only_the_asked_machine(plant, audit):
    ctx, reg = plant
    bad = "7 號機 30 分鐘後會降到 110°C。"
    agent = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07", "CURE-08"]}),
                             ("query_orders_shifts", {})), bad, bad)
    a = await agent.ask("7 號機溫度？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer.startswith("查詢結果：7 號機：溫度")
    assert "8 號機" not in a.answer and "人力缺口" not in a.answer and "110" not in a.answer
    assert _entries(audit, a.id, AuditKind.LLM_CALL)[-1].payload["repair"] == "template_relevant"


# ---------------------------------------------------------------- 4: a change request vs a question about a change
@pytest.mark.parametrize("q, req", [
    ("把 7 號機停掉", True), ("幫我重排", True), ("通知維修組", True), ("可以幫我重排嗎？", True), ("核准", True),
    ("核准卡在等什麼？", False), ("今天有加班嗎？", False), ("誰通知了維修組？", False), ("幫我查今天有加班嗎", False),
    ("7 號機現在怎樣", False), ("重排的進度", False),
])
def test_change_request_vs_question(q, req):
    assert change_request(q) is req


async def test_question_about_overtime_gets_a_model_answer(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(("query_orders_shifts", {})), "今天各班都不缺人，查詢結果裡沒有排加班。")
    a = await agent.ask("今天有加班嗎？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert len(agent.client.calls) == 2 and a.answer == "今天各班都不缺人，查詢結果裡沒有排加班。"


async def test_claimed_missing_data_gets_one_more_look(plant, audit):
    """Spark 10-22: 「今晚夜班缺誰？」 -> 「查詢結果裡沒有缺人資訊」 while the result said 人力缺口：無."""
    ctx, reg = plant
    agent = _llm_agent(_plan(("query_orders_shifts", {})), "查詢結果裡沒有缺人資訊。", "今晚夜班沒有缺人。")
    a = await agent.ask("今晚夜班缺誰？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.answer == "今晚夜班沒有缺人。" and "「無」也是答案" in agent.client.calls[2]["messages"][-1]["content"]
    # a rewrite that still finds nothing stands: it says what is missing (no third call)
    agent = _llm_agent(_plan(("query_orders_shifts", {})), "查詢結果裡沒有誰通知維修組的資料。",
                       "查詢結果裡沒有誰通知維修組的紀錄。")
    b = await agent.ask("誰通知了維修組？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert b.answer == "查詢結果裡沒有誰通知維修組的紀錄。" and len(agent.client.calls) == 3


async def test_approval_question_sees_the_open_events(plant, audit):
    """「核准卡在等什麼？」: the open events and their state (code-written, from the plant context) reach the answer,
    and their digits count as grounded."""
    ctx, reg = plant
    ev = {"id": "EV-20261003-021400-CURE-07", "machine_id": "CURE-07", "state": "await_approval", "goal": "恢復 7 號機"}
    agent = _llm_agent(_plan(("query_orders_shifts", {})),
                       "EV-20261003-021400-CURE-07（7 號機）在等待主管核准。")
    a = await agent.ask("核准卡在等什麼？", tools=reg, audit=audit, context={"open_events": [ev]},
                        now=ctx.clock.now())
    assert a.answer == "EV-20261003-021400-CURE-07（7 號機）在等待主管核准。"
    assert "等待主管核准" in agent.client.calls[1]["messages"][1]["content"]
    assert _entries(audit, a.id, AuditKind.LLM_CALL)[-1].payload["repair"] is None


async def test_change_request_still_gets_facts_only(plant, audit):
    """The model reads 「幫我把 7 號機停掉」 as a change request: facts only, the approval-flow note, no model answer."""
    ctx, reg = plant
    agent = _llm_agent(_plan(("read_sensors", {"machine_ids": ["CURE-07"]}), intent="change_request"))
    a = await agent.ask("幫我把 7 號機停掉", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert len(agent.client.calls) == 1 and a.answer.endswith("請走事件核准流程）")


async def test_model_planning_nothing_for_a_question_gets_the_rules_steps(plant, audit):
    """The model said 「question」 and planned no read (Spark 10-22: 「今晚夜班缺誰？」): its plan is incomplete, so the
    keyword planner fills the steps (a repair of the model's output, audited)."""
    ctx, reg = plant
    agent = _llm_agent(_plan(intent="question"), "今晚夜班沒有人力缺口。")
    a = await agent.ask("今晚夜班缺誰？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert [s.tool for s in a.steps] == ["query_orders_shifts"] and a.answer == "今晚夜班沒有人力缺口。"
    plan = _entries(audit, a.id, AuditKind.LLM_CALL)[0].payload
    assert plan["planner"] == "rules" and "keyword rules used" in plan["error"]


async def test_event_question_with_no_step_answers_from_the_open_events(plant, audit):
    ctx, reg = plant
    ev = {"id": "EV-20261003-021400-CURE-07", "machine_id": "CURE-07", "state": "await_approval", "goal": ""}
    agent = _llm_agent(_plan(intent="question"), "7 號機的事件在等你核准。")
    a = await agent.ask("核准卡在等什麼？", tools=reg, audit=audit, context={"open_events": [ev]}, now=ctx.clock.now())
    assert a.steps == [] and a.answer == "7 號機的事件在等你核准。"


# ---------------------------------------------------------------- issue #195: the model reads the message itself
async def test_greeting_is_the_models(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(intent="chitchat", reply="你好，有什麼要我看的嗎？"))
    a = await agent.ask("哈摟", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert len(agent.client.calls) == 1 and a.intent == "chitchat" and a.answer == "你好，有什麼要我看的嗎？" and not a.steps
    agent = _llm_agent(_plan(intent="chitchat", reply="不客氣！"))
    assert (await agent.ask("謝謝", tools=reg, audit=audit, context={}, now=ctx.clock.now())).answer == "不客氣！"


async def test_chitchat_with_an_invented_number_gets_the_canned_line(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(intent="chitchat", reply="你好！7 號機現在 128°C。"))
    a = await agent.ask("你好", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert "128" not in a.answer and a.intent == "chitchat"


async def test_unclear_gets_the_models_clarifying_question(plant, audit):
    ctx, reg = plant
    agent = _llm_agent(_plan(intent="unclear", reply="你是要看 7 號機的溫度，還是它的排程？"))
    a = await agent.ask("那個", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.intent == "unclear" and a.answer == "你是要看 7 號機的溫度，還是它的排程？"


async def test_stop_request_is_refused_with_the_reason(plant, audit):
    """「把 7 號機停掉」 read by the model as a change request, with nothing to read: the refusal says why and where."""
    ctx, reg = plant
    agent = _llm_agent(_plan(intent="change_request"))
    a = await agent.ask("把 7 號機停掉", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.intent == "change_request" and "不能變更" in a.answer and "核准" in a.answer


async def test_without_a_model_the_keyword_routes_stay(plant, audit):
    """Rule mode: the greeting is code's canned line (the fallback, not a pre-chat check with a model)."""
    ctx, reg = plant
    a = await _mock_agent().ask("哈摟", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    assert a.intent == "chitchat" and a.answer.startswith("你好")


