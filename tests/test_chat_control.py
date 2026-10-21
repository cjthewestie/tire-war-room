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


def test_no_approval_or_safety_release_is_reachable_from_chat():
    names = {s["function"]["name"] for s in [*tool_specs(True), *control_specs()]}
    assert not {n for n in names if any(w in n for w in ("approve", "release", "decide", "unlock", "safety"))}
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
    from warroom.agent.main_agent import APPROVE_REPLY
    f = Floor()
    m = Model(("tool", "propose_plan", {"task": "核准 10 號機"}), ("say", "已擬計劃送你核准。"))
    out = await MainAgent(_settings(), client=m).turn("幫我核准 10 號機的計劃", context={"abnormal": []}, waiting_plan=None,
                                                      run_tool=f.run_tool, propose=f.propose, discuss=None, control=FakeControl())
    assert not f.proposed and out["event_id"] is None and out["reply"] == APPROVE_REPLY
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
