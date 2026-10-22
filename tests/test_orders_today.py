"""今日訂單 + 問戰情室的對話記憶 (issue #95).

Part 1 checks the rules of warroom/tools/orders_today.py on small hand-built inputs (test fixtures only, never in the
product): which orders are in production / due today, due date vs due time, when an order gets a finish time, orders
without a schedule, ACTIVE vs PROPOSED schedule. Part 2 asks the real tools on the page-11 plant: the six order
questions, follow-ups, ambiguity, isolation, reset, re-query after a change, and that no number in an answer is one
the tools did not return."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from warroom import factory as F
from warroom.agent import order_ask
from warroom.agent.ask import AskAgent
from warroom.agent.audit import JsonlAuditLog
from warroom.agent.convo import AskMemory, ConvContext
from warroom.api import Runtime, create_app
from warroom.clock import SimClock
from warroom.config import Settings
from warroom.contracts import AuditKind, Order, ScenarioName, TireSize
from warroom.schedule import FallbackScheduler, FileScheduleStore, back_propagate, seed_schedule
from warroom.sim import Factory
from warroom.tools import ToolContext, build_registry
from warroom.tools import orders_today as OT

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=F.TZ)
TODAY = NOW.date()


def _o(oid, qty=10, done=0, due=TODAY, due_at=None, status="待排程", source="seed", **kw):
    return {"id": oid, "size": TireSize.S, "qty": qty, "done": done, "due": due, "due_at": due_at,
            "customer": "客戶", "status": status, "rush": False, "source": source, **kw}


def _job(jid, oid, press, mold_in, cure_h=24.0):
    return back_propagate(jid, oid, TireSize.S, press, mold_in, cure_h=cure_h)


def _cure(oid, press, mold_in, demold):
    return {"press_id": press, "order_id": oid, "mold_in_at": mold_in, "planned_demold_at": demold,
            "cure_elapsed_h": 1.0, "cure_required_h": 24.0}


def _by_id(data):
    return {b["id"]: b for b in data["orders"]}


# ================================================================== part 1: the rules
def test_in_press_since_yesterday_is_production_today():
    """A tyre that went into a press yesterday and is still curing: the order is in production today."""
    c = _cure("SO-1", "CURE-03", NOW - timedelta(hours=10), NOW + timedelta(hours=14))
    d = OT.build(NOW, [_o("SO-1", status="生產中")], [], [c])
    assert d["production"] == ["SO-1"] and _by_id(d)["SO-1"]["machines_now"] == ["CURE-03"]
    assert _by_id(d)["SO-1"]["production_basis"][0].startswith("硫化中")


def test_schedule_activity_today_counts_even_if_later_today():
    j = _job("J1", "SO-2", "CURE-02", NOW + timedelta(hours=6))
    d = OT.build(NOW, [_o("SO-2", qty=1)], [j], [])
    assert d["production"] == ["SO-2"]


def test_no_activity_today_is_not_production_today():
    j = _job("J1", "SO-3", "CURE-02", NOW + timedelta(days=3))
    d = OT.build(NOW, [_o("SO-3", qty=1, due=TODAY + timedelta(days=9))], [j], [])
    assert d["production"] == [] and d["delivery"] == [] and d["counts"]["unique_today"] == 0


def test_production_and_delivery_are_apart_and_counted_once():
    c = _cure("SO-4", "CURE-03", NOW, NOW + timedelta(hours=20))
    d = OT.build(NOW, [_o("SO-4", due=TODAY, status="生產中"), _o("SO-5", due=TODAY)], [], [c])
    assert d["production"] == ["SO-4"] and d["delivery"] == ["SO-4", "SO-5"]
    assert d["counts"]["unique_today"] == 2
    text = OT.overview_text(d)
    assert "今日生產 1 張" in text and "今日交貨 2 張" in text and "合計 2 張不重複" in text
    assert text.count("SO-4（已列於今日生產）") == 1          # listed in full once, then only referred to


def test_duplicate_ids_are_counted_once():
    d = OT.build(NOW, [_o("SO-6"), _o("#SO-6"), _o("so-6")], [], [])
    assert d["counts"]["total"] == 1 and len(d["duplicates"]) == 2


def test_nothing_due_today_says_so_and_names_no_other_day():
    d = OT.build(NOW, [_o("SO-7", due=TODAY + timedelta(days=2))], [], [])
    text, shown, _ = order_ask.compose(order_ask.OrderIntent("delivery"), d, [])
    assert shown == [] and "沒有今日交貨的訂單" in text and "SO-7" not in text


def test_due_with_time_is_judged_and_said_on_the_time():
    due_at = NOW + timedelta(hours=2)
    j = _job("J1", "SO-8", "CURE-02", NOW + timedelta(hours=1), cure_h=5)          # de-molds 6 h from now
    d = OT.build(NOW, [_o("SO-8", qty=1, due_at=due_at)], [j], [])
    b = _by_id(d)["SO-8"]
    assert b["due_basis"] == "time" and b["risk"] == "late" and b["late_by_h"] == 4
    assert "晚 4 小時" in "；".join(b["reasons"])


def test_due_with_date_only_is_judged_on_the_day_and_never_claims_hours():
    j = _job("J1", "SO-9", "CURE-02", NOW + timedelta(hours=1), cure_h=24)         # de-molds tomorrow
    d = OT.build(NOW, [_o("SO-9", qty=1, due=TODAY)], [j], [])
    b = _by_id(d)["SO-9"]
    assert b["due_basis"] == "date" and b["risk"] == "late" and b["late_by_h"] is None
    assert "小時" not in "；".join(b["reasons"]) and "晚於交期日" in "；".join(b["reasons"])
    # a date-only due that is still today is not late at 23:00
    late_night = NOW.replace(hour=23)
    j2 = _job("J2", "SO-9", "CURE-02", late_night - timedelta(hours=2), cure_h=1)
    assert _by_id(OT.build(late_night, [_o("SO-9", qty=1, due=TODAY)], [j2], []))["SO-9"]["overdue"] is False


def test_overdue_is_kept_apart_from_will_be_late():
    d = OT.build(NOW, [_o("SO-10", due=TODAY - timedelta(days=1)),
                       _o("SO-11", qty=1, due=TODAY)], [_job("J", "SO-11", "CURE-02", NOW + timedelta(hours=1), 30)], [])
    assert d["overdue"] == ["SO-10"] and d["late"] == ["SO-11"]
    assert _by_id(d)["SO-10"]["risk_text"] == "已遲交" and _by_id(d)["SO-11"]["risk_text"] == "有遲交風險"


def test_finish_time_only_when_the_schedule_covers_every_tyre_left():
    jobs = [_job(f"J{i}", "SO-12", "CURE-02", NOW + timedelta(hours=1 + i), cure_h=24) for i in range(2)]
    b = _by_id(OT.build(NOW, [_o("SO-12", qty=5, due=TODAY + timedelta(days=30))], jobs, []))["SO-12"]
    assert b["eta"] is None and b["unscheduled"] == 3 and "2／5" in b["eta_unknown_reason"]
    assert b["next_demold"] == jobs[0].demold.isoformat()          # the next single tyre is a separate field
    full = jobs + [_job(f"K{i}", "SO-12", "CURE-03", NOW + timedelta(hours=9 + i), cure_h=24) for i in range(3)]
    b = _by_id(OT.build(NOW, [_o("SO-12", qty=5, due=TODAY + timedelta(days=30))], full, []))["SO-12"]
    assert b["eta"] == max(j.demold for j in full).isoformat() and b["unscheduled"] == 0


def test_world_order_has_no_schedule_machine_or_finish_time():
    o = _o("#W-0001", qty=8, done=3, status="生產中", source="world", due=TODAY + timedelta(days=5))
    b = _by_id(OT.build(NOW, [o], [], []))["#W-0001"]
    assert b["schedule_state"] == OT.NO_SCHEDULE and b["machines"] == [] and b["eta"] is None
    line = OT.order_line(b)
    assert "尚無排程" in line and "待確認" in line


def test_unknown_is_never_zero():
    b = _by_id(OT.build(NOW, [_o("SO-13", qty=4)], [], []))["SO-13"]
    assert b["eta"] is None and b["next_demold"] is None and b["unscheduled"] == 4   # known: nothing is scheduled
    done = _by_id(OT.build(NOW, [_o("SO-14", qty=4, done=4, status="完成")], [], []))["SO-14"]
    assert done["unscheduled"] is None and done["eta"] is None                       # finished: not applicable


def test_ids_in_text():
    assert OT.ids_in_text("#A-311 在哪台機器？") == ["A-311"]
    assert OT.ids_in_text("so-004 和 #W-0398") == ["SO-004", "W-0398"]
    assert OT.ids_in_text("7 號機 CURE-07 A-BIGOPEN 的溫度") == []        # machine ids are not orders


# ================================================================== part 2: the real plant
def _numbers(s: str) -> set[str]:
    nums = set(re.findall(r"\d+(?:\.\d+)?", s))
    return nums | {n.lstrip("0") or "0" for n in nums}


def assert_grounded(answer: str, data: dict, question: str = ""):
    """Every number in the answer appears in what the tool returned (or in the question)."""
    known = _numbers(json.dumps(data, ensure_ascii=False, default=str) + " " + question)
    bad = [n for n in _numbers(answer) if n not in known]
    assert not bad, f"numbers not from the tool result: {bad}\n{answer}"


def _build_plant(tmp_path):
    clock = SimClock(manual=True)
    sim = Factory(clock=clock, scenario=ScenarioName.PRESS7_TEMP_DROP)
    for _ in range(60):
        sim.step(1.0)
    sched = FallbackScheduler()
    store = FileScheduleStore(tmp_path / "sched", initial=seed_schedule(
        clock.now(), sim.orders(), sim.shifts(), sim.machines(), scheduler=sched, next_on={"CURE-07": "#A-311"}))
    ctx = ToolContext(sim=sim, scheduler=sched, store=store, clock=clock)
    return ctx, build_registry(ctx)


@pytest.fixture
def plant(tmp_path):
    return _build_plant(tmp_path)


@pytest.fixture
def audit(tmp_path):
    return JsonlAuditLog(tmp_path / "audit.jsonl")


def _agent() -> AskAgent:
    return AskAgent(Settings(llm_mode="mock"))


async def _ask(plant, audit, q, *, conv="c1", memory=None, agent=None, actor="supervisor"):
    ctx, reg = plant
    return await (agent or _agent()).ask(q, tools=reg, audit=audit, context={"abnormal": []}, now=ctx.clock.now(),
                                         actor=actor, conversation_id=conv, memory=memory)


def _data(plant) -> dict:
    ctx, reg = plant
    return OT.collect(ctx.sim, ctx.store, ctx.clock.now())


async def test_today_overview_lists_production_and_delivery_apart(plant, audit):
    a = await _ask(plant, audit, "今天有哪些訂單？", memory=AskMemory())
    d = _data(plant)
    assert a.intent == "query" and [s.tool for s in a.steps] == ["query_orders_shifts"]
    assert f"今日生產 {d['counts']['production']} 張" in a.answer and f"今日交貨 {d['counts']['delivery']} 張" in a.answer
    assert d["counts"]["total"] > 1 and "今日生產：" in a.answer and "今日交貨：" in a.answer
    assert_grounded(a.answer, d, "今天有哪些訂單？")


async def test_production_question_includes_orders_curing_across_days(plant, audit):
    a = await _ask(plant, audit, "今天安排生產哪些訂單？")
    d = _data(plant)
    in_press = {c.order_id for c in (plant[0].sim.machine(p).cure_job for p in F.CURE_PRESS_IDS) if c is not None}
    assert in_press and in_press <= {OT.norm_id(i) and i for i in d["production"]} | set(d["production"])
    for oid in in_press:
        assert oid in a.answer                          # an order curing now is not dropped
    assert_grounded(a.answer, d, "今天安排生產哪些訂單？")


async def test_delivery_question_with_nothing_due_today(plant, audit):
    d = _data(plant)
    assert d["delivery"] == []                          # the page-11 day has no order due on 10/03
    a = await _ask(plant, audit, "今天要交哪些訂單？")
    assert "沒有今日交貨的訂單" in a.answer
    assert not any(b["id"] in a.answer for b in d["orders"])        # no other day's order stands in for it


async def test_delivery_question_lists_an_order_due_today(plant, audit):
    ctx, _ = plant
    ctx.sim.add_order(Order(id="#T-1", size=TireSize.S, qty=3, done=0, due=ctx.clock.now().date(), customer="客戶"))
    a = await _ask(plant, audit, "今天要交哪些訂單？")
    assert "#T-1" in a.answer and "今日交貨 1 張" in a.answer


async def test_risk_question_separates_overdue_late_and_other(plant, audit):
    ctx, _ = plant
    ctx.sim.add_order(Order(id="#T-2", size=TireSize.S, qty=3, done=0, due=ctx.clock.now().date() - timedelta(days=1),
                            customer="客戶"))
    a = await _ask(plant, audit, "哪些訂單有遲交風險？")
    d = _data(plant)
    assert d["overdue"] == ["#T-2"] and "已遲交：#T-2" in a.answer
    assert "其他風險（例如尚未排程，不等於會遲交）" in a.answer
    assert_grounded(a.answer, d, "哪些訂單有遲交風險？")


async def test_named_order_progress_and_machine(plant, audit):
    d = _data(plant)
    a = await _ask(plant, audit, "#A-311 現在做到哪裡？")
    assert "#A-311" in a.answer and "已完成 0／剩 1" in a.answer
    w = await _ask(plant, audit, "#A-311 在哪台機器？預計何時完成？")
    b = OT.find(d, "#A-311")
    assert "7 號機" in w.answer and b["eta"] is not None
    assert f"{datetime.fromisoformat(b['eta']):%m/%d %H:%M}" in w.answer
    assert_grounded(a.answer + w.answer, d, "#A-311 現在做到哪裡？ #A-311 在哪台機器？預計何時完成？")


async def test_world_order_is_found_and_says_no_schedule(plant, audit):
    d = _data(plant)
    w = next(b for b in d["orders"] if b["source"] == "world" and b["status"] not in ("完成", "待出貨", "已出貨"))
    a = await _ask(plant, audit, f"{w['id']} 在哪台機器？")
    assert "尚無排程" in a.answer and "7 號機" not in a.answer


async def test_unknown_order_is_not_found_and_not_replaced(plant, audit):
    mem = AskMemory()
    await _ask(plant, audit, "#A-311 現在做到哪裡？", memory=mem)
    a = await _ask(plant, audit, "#Z-999 現在做到哪裡？", memory=mem)
    assert "查無訂單 Z-999" in a.answer and "#A-311" not in a.answer
    assert mem.get("supervisor", "c1").focus_order == "#A-311"       # the failed lookup did not wipe the focus


async def test_follow_up_resolves_to_the_order_just_talked_about(plant, audit):
    mem = AskMemory()
    await _ask(plant, audit, "#A-311 現在做到哪裡？", memory=mem)
    for q in ("它在哪台機器？", "那張單呢？", "現在完成多少？"):
        a = await _ask(plant, audit, q, memory=mem)
        assert a.intent == "query" and a.answer.startswith("#A-311") or "#A-311" in a.answer, q
    ents = [e for e in audit.tail(since_seq=0, limit=100_000) if e.payload.get("msg") == "order question"]
    assert [e.payload["resolved_via"] for e in ents[1:]] == ["focus", "focus", "focus"]


async def test_follow_up_requeries_after_a_change(plant, audit):
    ctx, _ = plant
    mem = AskMemory()
    ctx.sim.add_order(Order(id="#T-3", size=TireSize.S, qty=3, done=0, due=ctx.clock.now().date(), customer="客戶"))
    first = await _ask(plant, audit, "#T-3 現在做到哪裡？", memory=mem)
    assert "已完成 0／剩 3" in first.answer
    ctx.sim._orders["#T-3"] = ctx.sim._orders["#T-3"].model_copy(update={"qty": 5, "done": 2})
    second = await _ask(plant, audit, "現在完成多少？", memory=mem)
    assert "已完成 2／剩 3" in second.answer and "共 5 條" in second.answer      # the new figures, not the old ones


async def test_ambiguous_follow_up_asks_which_order(plant, audit):
    mem = AskMemory()
    await _ask(plant, audit, "今天有哪些訂單？", memory=mem)
    a = await _ask(plant, audit, "那張單呢？", memory=mem)
    assert a.intent == "needs_choice" and len(a.choices) > 1 and a.steps == []
    assert all(c in a.answer for c in a.choices)
    # naming one resolves it
    b = await _ask(plant, audit, f"{a.choices[0]} 現在做到哪裡？", memory=mem)
    assert b.intent == "query" and a.choices[0] in b.answer


async def test_pointing_at_nothing_asks_for_the_order_id(plant, audit):
    a = await _ask(plant, audit, "它在哪台機器？", memory=AskMemory())
    assert a.intent == "needs_order" and a.steps == [] and a.choices == []


async def test_conversations_are_isolated(plant, audit):
    mem = AskMemory()
    await _ask(plant, audit, "#A-311 現在做到哪裡？", conv="c1", memory=mem)
    other = await _ask(plant, audit, "它在哪台機器？", conv="c2", memory=mem)
    assert other.intent == "needs_order"
    other_user = await _ask(plant, audit, "它在哪台機器？", conv="c1", memory=mem, actor="someone-else")
    assert other_user.intent == "needs_order"
    mine = await _ask(plant, audit, "它在哪台機器？", conv="c1", memory=mem)
    assert mine.intent == "query" and "#A-311" in mine.answer


async def test_reset_forgets_every_conversation(plant, audit):
    mem = AskMemory()
    await _ask(plant, audit, "#A-311 現在做到哪裡？", memory=mem)
    assert mem.reset() == 1 and len(mem) == 0
    assert (await _ask(plant, audit, "它在哪台機器？", memory=mem)).intent == "needs_order"


def test_memory_holds_ids_only_and_expires():
    t = [0.0]
    mem = AskMemory(ttl_s=10, max_conversations=2, clock=lambda: t[0])
    mem.remember("u", "a", orders=["#A-311"], focus="#A-311", events=["EV-20261003-021420-CURE-07"], date_scope="2026-10-03")
    assert mem.get("u", "a").as_dict() == {"focus_order": "#A-311", "recent_orders": ["#A-311"],
                                          "event_ids": ["EV-20261003-021420-CURE-07"], "date_scope": "2026-10-03",
                                          "last_question": None, "last_machines": []}   # issue #182: no readings
    t[0] = 11
    assert mem.get("u", "a").empty()                                  # idle past the TTL: gone
    t[0] = 12
    for cid in "xyz":
        mem.remember("u", cid, orders=[cid]); t[0] += 1
    assert len(mem) == 2 and mem.get("u", "x").empty()                # bounded: the oldest idle one went
    assert mem.get("u", None).empty()                                 # no conversation id: nothing remembered


def test_classify_keeps_machine_and_staffing_questions_out_of_the_order_path():
    c = lambda q, mem=ConvContext(): order_ask.classify(q, has_machine=("號機" in q), memory=mem)   # noqa: E731
    assert c("7 號機現在怎樣？") is None and c("今晚夜班缺誰？") is None
    assert c("7 號機延誤嗎？") is None                                   # a machine question stays with the planner
    assert c("訂單的交期是什麼時候？") is None                            # not today's delivery list
    assert c("今天有哪些訂單？").kind == "today" and c("今天安排生產哪些訂單？").kind == "production"
    assert c("今天要交哪些訂單？").kind == "delivery" and c("哪些訂單有遲交風險？").kind == "risk"
    assert c("#A-311 現在做到哪裡？").kind == "status" and c("#A-311 在哪台機器？預計何時完成？").kind == "where"
    assert c("它在哪台機器？").pointed and c("那張單呢？").pointed


async def test_order_questions_never_call_the_model(plant, audit):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("the model was called for an order question")
    agent = AskAgent(Settings(llm_mode="llm", base_url="http://x/v1", model="nemotron"), client=Boom())
    a = await _ask(plant, audit, "今天有哪些訂單？", agent=agent)
    assert a.intent == "query" and a.steps[0].ok


async def test_query_fails_visibly_not_with_old_numbers(plant, audit, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sim down")
    monkeypatch.setattr(OT, "collect", boom)            # the order lookup fails
    a = await _ask(plant, audit, "今天有哪些訂單？")
    assert "查不到資料" in a.answer and "沒有用舊的數字代替" in a.answer and not re.search(r"\d+ 張", a.answer)


def test_without_a_schedule_every_open_order_says_no_schedule(plant):
    ctx, _ = plant
    d = OT.collect(ctx.sim, None, ctx.clock.now())
    open_ = [b for b in d["orders"] if b["status"] not in ("完成", "待出貨", "已出貨")]
    assert open_ and all(b["eta"] is None for b in open_)
    assert all(b["eta"] is None or b["machines_now"] for b in open_)    # only a tyre in a press gives a machine


def test_proposed_schedule_is_not_in_force(plant):
    ctx, _ = plant
    cur = ctx.store.current()
    emptied = cur.schedule.model_copy(update={"id": "SCH-PROPOSED", "jobs": []})
    ctx.store.propose(emptied, reason="waiting for approval")
    d = OT.collect(ctx.sim, ctx.store, ctx.clock.now())
    assert d["schedule_version"] == cur.version                       # still the ACTIVE one
    assert _by_id(d)["#A-311"]["machines"] == ["CURE-07"]            # the proposal's empty job list changed nothing


def test_next_jobs_still_come_back_for_the_other_questions(plant):
    ctx, reg = plant
    from warroom.contracts import ToolCall, ToolName
    r = reg.call(ToolCall(id="t", event_id="e", step_id="s", tool=ToolName.QUERY_ORDERS_SHIFTS, args={}))
    assert r.ok and "orders_today" in r.data and r.data["next_jobs"] and r.data["gaps"] is not None


# ---------------------------------------------------------------- API
def _settings(tmp_path) -> Settings:
    return Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                    qdrant_path=tmp_path / "qdrant", scenario=ScenarioName.PRESS7_TEMP_DROP.value, llm_mode="mock")


def test_api_orders_today_and_conversation(tmp_path):
    rt = Runtime(_settings(tmp_path), run_loop=False)
    if rt.c.tools is None:
        pytest.skip(f"tools not ready: {rt.degraded}")
    with TestClient(create_app(rt, supervisor_pin="2468")) as client:
        d = client.get("/api/orders/today").json()
        assert d["date"] == rt.clock.now().date().isoformat() and d["counts"]["total"] == len(d["orders"]) > 0
        # the order list is the dashboard's order list: every order the world shows is in it
        world_ids = {o["id"] for o in client.get("/api/world").json()["orders"]["items"]}
        assert world_ids == {b["id"] for b in d["orders"]}
        a = client.post("/api/ask", json={"question": "#A-311 現在做到哪裡？", "conversation_id": "page-1"}).json()
        assert a["conversation_id"] == "page-1" and "#A-311" in a["answer"]
        b = client.post("/api/ask", json={"question": "它在哪台機器？", "conversation_id": "page-1"}).json()
        assert b["intent"] == "query" and "7 號機" in b["answer"]
        c = client.post("/api/ask", json={"question": "它在哪台機器？", "conversation_id": "page-2"}).json()
        assert c["intent"] == "needs_order"
        d2 = client.post("/api/ask", json={"question": "它在哪台機器？"}).json()          # no conversation: no memory
        assert d2["intent"] == "needs_order" and d2["conversation_id"] is None


async def test_scenario_switch_clears_the_conversation(tmp_path):
    rt = Runtime(_settings(tmp_path), run_loop=False)
    if rt.c.tools is None:
        pytest.skip(f"tools not ready: {rt.degraded}")
    audit_len = len(rt.audit.tail(0, 1_000_000))
    rt.ask_memory.remember("supervisor", "page-1", orders=["#A-311"], focus="#A-311")
    assert not rt.ask_memory.get("supervisor", "page-1").empty()
    await rt.load_scenario(ScenarioName.NORMAL)
    assert rt.ask_memory.get("supervisor", "page-1").empty() and rt.ask_memory.generation == 1
    msgs = [e.payload.get("msg") for e in rt.audit.tail(audit_len, 1_000_000) if e.kind is AuditKind.SYSTEM]
    assert "ask memory cleared" in msgs
