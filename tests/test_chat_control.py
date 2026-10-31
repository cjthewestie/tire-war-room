"""Task control from the Agent page chat (CJ, 2026-10-22: 「A」): the same Agent Nemo lists every task and directs them
through existing supervisor actions only, each confirmed in chat first and audited; approval and Safety Core release
are never chat actions."""
from __future__ import annotations

import asyncio

from tests.test_main_agent_demo import Floor, Model, _settings
from tests.test_main_agent_query import Reader
from tests.test_plan_talk import Plant, _rt
from warroom.agent.main_agent import CONFIRM_Q, CONTROL, MainAgent, control_specs, tool_specs
from warroom.api import plant_query
from warroom.contracts import AuditKind


class FakeControl:
    def __init__(self, known=("EV-7",)):
        self.known, self.ran, self.asked = set(known), [], []

    def resolve(self, ref):
        if ref in self.known:
            return ref, "7 號硫化機那件"
        if "7" in ref and "EV-7" in self.known:
            return "EV-7", "7 號硫化機那件"
        return None

    def audit_request(self, action, eid):
        self.asked.append((action, eid))

    async def run(self, action, eid):
        self.ran.append((action, eid))
        return f"已{CONTROL[action]} {eid}"


async def _say(agent, text, ctl, conv="c"):
    f = Floor()
    return await agent.turn(text, context={"abnormal": []}, waiting_plan=None, run_tool=f.run_tool, propose=f.propose,
                            discuss=None, read_plant=Reader(), conversation=conv, control=ctl)


async def test_each_control_action_asks_first_and_runs_only_after_a_yes():
    for action in CONTROL:
        ctl = FakeControl()
        m = Model(("tool", action, {"event_id": "7 號機"}), ("say", "好的，已經處理了。"))
        agent = MainAgent(_settings(), client=m)
        q = await _say(agent, f"7 號機那件{CONTROL[action]}", ctl)
        assert q["reply"] == CONFIRM_Q.format(verb=CONTROL[action], label="7 號硫化機那件", eid="EV-7")
        assert not ctl.ran and ctl.asked == [(action, "EV-7")]               # nothing ran yet; the ask is audited
        done = await _say(agent, "好", ctl)
        assert ctl.ran == [(action, "EV-7")] and done["by"] == "control" and done["reply"] == f"已{CONTROL[action]} EV-7"


async def test_no_or_anything_else_drops_the_pending_action():
    ctl = FakeControl()
    m = Model(("tool", "interrupt_task", {"event_id": "EV-7"}), ("say", "x"), ("say", "原料還夠。"))
    agent = MainAgent(_settings(), client=m)
    await _say(agent, "中斷 EV-7", ctl)
    out = await _say(agent, "不要", ctl)
    assert out["reply"] == "好，不中斷。" and not ctl.ran
    out = await _say(agent, "好", ctl)                                          # nothing pending any more
    assert not ctl.ran


async def test_an_unknown_task_is_rejected_and_nothing_is_pending():
    ctl = FakeControl()
    m = Model(("tool", "interrupt_task", {"event_id": "EV-NOPE"}), ("say", "已中斷。"))
    agent = MainAgent(_settings(), client=m)
    out = await _say(agent, "中斷 EV-NOPE", ctl)
    assert out["reply"].startswith("找不到") and "pending" not in agent.notes and not ctl.asked
    await _say(agent, "好", ctl)
    assert not ctl.ran


def test_no_approval_is_reachable_from_chat():
    """Approving a plan stays the button. A machine's way back (release_safety_stop) is a chat action since CJ
    2026-10-22 「開放」, confirmed by the supervisor's 好 (tests/test_chat_release.py)."""
    names = {s["function"]["name"] for s in [*tool_specs(True), *control_specs()]}
    assert not {n for n in names if any(w in n for w in ("approve", "decide", "unlock"))}
    assert {n for n in names if "release" in n or "safety" in n} == {"release_safety_stop"}
    assert names & set(CONTROL) == set(CONTROL)


async def test_a_made_up_approve_tool_is_refused():
    ctl = FakeControl()
    m = Model(("tool", "approve_plan", {"event_id": "EV-7"}), ("say", "核准要按按鈕。"))
    out = await _say(MainAgent(_settings(), client=m), "幫我核准 7 號機", ctl)
    assert not ctl.ran and not ctl.asked and out["steps"][0]["ok"] is False


async def test_status_lists_every_open_event(tmp_path):
    async with Plant(_rt(tmp_path, "concurrent_3")) as p:
        await p.until(lambda: len([e for e in p.rt.events() if e.closed_at is None]) >= 3, 120)
        d = plant_query.query(p.rt, "tasks")
        open_ids = {e.id for e in p.rt.events() if e.closed_at is None}
        assert {t["id"] for t in d["tasks"]} == open_ids and d["count"] == len(open_ids)
        assert all(t["state"] and "open_min" in t for t in d["tasks"])


async def test_api_interrupt_from_chat_needs_the_yes_and_is_audited(tmp_path, monkeypatch):
    import warroom.agent.main_agent as MA
    settings = type("S", (), {"llm_mode": "llm", "model": "m", "base_url": "x", "llm_timeout_s": 5, "api_key_effective": "n"})()
    async with Plant(_rt(tmp_path, "press7_temp_drop")) as p:
        await p.until(lambda: any(e.machine_id == "CURE-07" and e.closed_at is None for e in p.rt.events()), 120)
        eid = next(e.id for e in p.rt.events() if e.machine_id == "CURE-07")
        agent = MA.MainAgent(settings, client=Model(("tool", "interrupt_task", {"event_id": "7 號機"}), ("say", "好。")))
        monkeypatch.setattr(MA, "MainAgent", lambda *_a, **_k: agent)
        q = (await p.http.post("/api/main/message", json={"text": "7 號機那件中斷", "conversation_id": "k"})).json()
        assert "要中斷" in q["reply"] and eid in q["reply"]
        assert p.rt.state_machine.get(eid).state.value not in ("closed", "safe_stop")
        await p.http.post("/api/main/message", json={"text": "好", "conversation_id": "k"})
        await asyncio.sleep(0.2)
        assert p.rt.state_machine.get(eid).state.value == "safe_stop"
        rows = [e for e in p.rt.audit.tail(0, 1_000_000) if e.actor.endswith("via Agent Nemo") and e.event_id == eid]
        assert [r.payload["confirmed"] for r in rows] == [False, True] and rows[0].kind is AuditKind.SYSTEM
        assert not p.rt.approvals.get(p.rt.state_machine.get(eid).approvals[0]).decision if p.rt.state_machine.get(eid).approvals else True



async def test_a_control_request_the_model_did_not_turn_into_a_tool_is_asked_by_code():
    """Real run 10-22: 「中斷 3 號機那件」 got a text-written read_sensors call and no question."""
    ctl = FakeControl(known=("EV-3",))
    ctl.resolve = lambda ref: ("EV-3", "3 號硫化機那件") if "3" in ref else None
    m = Model(("say", "我先查一下 3 號機。"))
    out = await _say(MainAgent(_settings(), client=m), "中斷 3 號機那件", ctl)
    assert out["reply"] == CONFIRM_Q.format(verb="中斷", label="3 號硫化機那件", eid="EV-3") and not ctl.ran


async def test_asking_to_approve_never_proposes_a_plan():
    """#195 (CJ 10-22: 「取消對話前的程式檢查」): with a model the words are its call, but no tool approves anything;
    the rule mode still answers with the button."""
    from warroom.agent.main_agent import APPROVE_REPLY
    f = Floor()
    m = Model(("say", "核准要你自己按核准卡。"))
    agent = MainAgent(_settings(), client=m)
    await agent.turn("幫我核准 10 號機的計劃", context={"abnormal": []}, waiting_plan=None,
                     run_tool=f.run_tool, propose=f.propose, discuss=None, control=FakeControl())
    assert not [n for n in m.offered() if "approv" in n or "核准" in n]
    rules = await MainAgent(None).turn("幫我核准 10 號機的計劃", context={"abnormal": []}, waiting_plan=None,
                                       run_tool=f.run_tool, propose=f.propose, discuss=None)
    assert not f.proposed and rules["reply"] == APPROVE_REPLY


async def test_an_xml_written_read_call_is_run():
    m = Model(("say", '<function=read_sensors>；<parameter=machine_ids>；["CURE-03"]；</parameter>；</function>'),
              ("say", "3 號機 122.3°C。"))
    f = Floor()
    out = await MainAgent(_settings(), client=m).turn("3 號機現在怎樣", context={"abnormal": []}, waiting_plan=None,
                                                      run_tool=f.run_tool, propose=f.propose, discuss=None)
    assert f.tools and f.tools[0] == ("read_sensors", {"machine_ids": ["CURE-03"]}) and out["reply"] == "3 號機 122.3°C。"



async def test_an_action_that_cannot_run_on_that_task_says_so_before_asking():
    ctl = FakeControl()
    ctl.blocked = lambda action, eid: "這件在等你核准，沒有「直接重新規劃」的現成做法。" if action == "replan_task" else None
    m = Model(("tool", "replan_task", {"event_id": "EV-7"}), ("say", "好。"))
    agent = MainAgent(_settings(), client=m)
    out = await _say(agent, "7 號機那件重新規劃", ctl)
    assert out["reply"].startswith("這件在等你核准") and "pending" not in agent.notes and not ctl.asked


# ---------------------------------------------------------------- bare commands (CJ 10-22: 「重新排」 got a lookup fallback)
class Picked(FakeControl):
    """resolve("") is the selected task (or None); candidates() the open ones."""

    def __init__(self, selected=None, cands=()):
        super().__init__(known=tuple(c[0] for c in cands) + ((selected,) if selected else ()))
        self.selected, self.cands = selected, list(cands)

    def resolve(self, ref):
        if ref in self.known:
            return ref, "7 號硫化機那件"
        if self.selected:
            return self.selected, "7 號硫化機那件"
        return self.cands[0] if len(self.cands) == 1 else None

    def candidates(self):
        return self.cands


async def test_bare_replan_on_the_selected_task_asks_then_runs():
    ctl = Picked(selected="EV-7")
    # #195: the model reads 「重新排」 and calls the control; the question is code's, word for word
    m = Model(("tool", "replan_task", {"event_id": ""}), ("say", "我查了排程。"))
    agent = MainAgent(_settings(), client=m)
    q = await _say(agent, "重新排", ctl)
    assert q["reply"] == CONFIRM_Q.format(verb="重新規劃", label="7 號硫化機那件", eid="EV-7") and not ctl.ran
    done = await _say(agent, "好", ctl)
    assert ctl.ran == [("replan_task", "EV-7")] and done["by"] == "control"


async def test_bare_command_with_several_open_tasks_asks_which():
    ctl = Picked(cands=[("EV-3", "3 號硫化機那件"), ("EV-10", "10 號硫化機那件")])
    out = await _say(MainAgent(_settings(), client=Model(("say", "好。"))), "交給現場", ctl)
    assert out["reply"].startswith("要交給現場處理哪一件？") and "EV-3" in out["reply"] and "EV-10" in out["reply"]
    assert not ctl.asked and not ctl.ran


async def test_bare_replan_with_nothing_open_proposes_a_reschedule():
    ctl = Picked()
    f = Floor()
    out = await MainAgent(_settings(), client=Model(("say", "好。"))).turn(
        "重新排", context={"abnormal": []}, waiting_plan=None, run_tool=f.run_tool, propose=f.propose, discuss=None,
        control=ctl)
    assert f.proposed == ["重新排程"] and out["event_id"] and "送你核准" in out["reply"]


def test_a_reschedule_request_with_an_order_is_a_new_task_not_a_control():
    from warroom.agent.main_agent import control_intent
    assert control_intent("#A-311 可能晚交，請重排保交期") is None
    assert control_intent("重新排") == "replan_task" and control_intent("結案") == "close_task"
    assert control_intent("7 號機那件交給現場") == "hand_to_field" and control_intent("中斷") == "interrupt_task"


async def test_close_asks_then_runs():
    ctl = Picked(selected="EV-7")
    agent = MainAgent(_settings(), client=Model(("say", "好。")))
    q = await _say(agent, "結案", ctl)
    assert q["reply"] == CONFIRM_Q.format(verb="確認結案", label="7 號硫化機那件", eid="EV-7")
    await _say(agent, "確認", ctl)
    assert ctl.ran == [("close_task", "EV-7")]


async def test_a_lookup_whose_numbers_do_not_match_lists_the_data_instead_of_offering():
    m = Model(("tool", "query_plant", {"topic": "materials"}), ("say", "有 99 種原料。"), ("say", "還是 99 種原料。"))
    out = await _say(MainAgent(_settings(), client=m), "原料還夠嗎", None)
    assert out["reply"].startswith("我的整理和資料對不上，直接列出查到的資料") and "要我直接列出" not in out["reply"]


async def test_safe_stop_replan_handoff_and_close_through_chat(tmp_path, monkeypatch):
    import warroom.agent.main_agent as MA
    settings = type("S", (), {"llm_mode": "llm", "model": "m", "base_url": "x", "llm_timeout_s": 5, "api_key_effective": "n"})()
    async with Plant(_rt(tmp_path, "concurrent_3")) as p:
        await p.until(lambda: len([e for e in p.rt.events() if e.machine_id in ("CURE-03", "CURE-10")]) >= 2, 120)
        e3 = next(e.id for e in p.rt.events() if e.machine_id == "CURE-03")
        e10 = next(e.id for e in p.rt.events() if e.machine_id == "CURE-10")
        await p.rt.orch.interrupt(e3, "supervisor")
        await p.rt.orch.interrupt(e10, "supervisor")
        agent = MA.MainAgent(settings, client=Model(*[("say", "好。") for _ in range(8)]))
        monkeypatch.setattr(MA, "MainAgent", lambda *_a, **_k: agent)

        async def say(text, eid):
            return (await p.http.post("/api/main/message", json={"text": text, "event_id": eid, "conversation_id": "z"})).json()
        q = await say("重新排", e3)
        assert "要重新規劃" in q["reply"] and e3 in q["reply"]
        done = await say("好", e3)
        assert "重新規劃" in done["reply"] and p.rt.state_machine.get(e3).follow_up.status == "replanned"
        q = await say("交給現場", e10)
        assert "要交給現場處理" in q["reply"]
        await say("好", e10)
        ev10 = p.rt.state_machine.get(e10)
        assert ev10.follow_up.status == "manual" and not p.rt._awaits_decision(e10)   # hand-off tasks run at once
        q = await say("結案", e10)
        assert "要確認結案" in q["reply"]
        r = await say("好", e10)
        assert ("已確認結案" in r["reply"] and p.rt.state_machine.get(e10).follow_up.status == "closed") \
            or "現場任務還沒完成" in r["reply"]
