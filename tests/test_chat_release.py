"""A machine's way back from the chat (CJ, 2026-10-22: 「開放」): Agent Nemo asks 「要恢復 X 運轉嗎？」 and only the
supervisor's 「好」 runs it, through the SAME release path as the ③ 確認恢復運轉 button (every precondition) or the
恢復運轉 button for a repaired out-of-service machine. Same session rule; audited as "<supervisor> via Agent Nemo".
This departs from SOP-SD-01 §4 (on-site screen only) by CJ's decision; the commander and state machine still cannot."""
from __future__ import annotations

import warroom.agent.main_agent as MA
from tests.test_main_agent_demo import Model
from tests.test_resolve import LAN_PHONE, MID, _client, _finish_tasks, _rt, _trip, _until_reading_ok
from warroom.contracts import AuditKind, MachineStatus, ToolName

SETTINGS = type("S", (), {"llm_mode": "llm", "model": "m", "base_url": "x", "llm_timeout_s": 5, "api_key_effective": "n"})()


def _agent(monkeypatch, *script):
    agent = MA.MainAgent(SETTINGS, client=Model(*script))
    monkeypatch.setattr(MA, "MainAgent", lambda *_a, **_k: agent)
    return agent


async def _say(c, text, conv="r"):
    r = await c.post("/api/main/message", json={"text": text, "conversation_id": conv})
    return r.status_code, (r.json() if r.status_code == 200 else r.text)


async def test_release_from_chat_is_refused_with_the_count_until_both_checks_pass(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    c = await _trip(rt)
    _agent(monkeypatch, *[("tool", "release_safety_stop", {"event_id": "A 線大開煉機"}) if i % 2 == 0 else ("say", "好。")
                          for i in range(12)])
    async with _client(rt) as local:
        code, out = await _say(local, "恢復 A 線大開煉機")
        assert code == 200 and "還不能恢復" in out["reply"] and f"/{c.safe_needed}" in out["reply"]   # ② with its count
        assert rt.sim.machine(MID).status is MachineStatus.STOPPED
        await _until_reading_ok(rt, c)
        code, out = await _say(local, "恢復 A 線大開煉機")
        assert "請先派現場檢查" in out["reply"]                                               # ① not dispatched
        assert (await local.post(f"/api/safety/{c.id}/dispatch")).status_code == 200
        code, out = await _say(local, "恢復 A 線大開煉機")
        assert "現場檢查還沒完成" in out["reply"]
        assert rt.sim.machine(MID).status is MachineStatus.STOPPED
        _finish_tasks(rt, c.id)
        code, out = await _say(local, "恢復 A 線大開煉機")
        assert out["reply"].startswith("要恢復") and "回「好」" in out["reply"]
        assert rt.sim.machine(MID).status is MachineStatus.STOPPED                          # asking does nothing
        code, out = await _say(local, "好")
        assert out["by"] == "control" and "已恢復" in out["reply"]
    assert rt.sim.machine(MID).status is MachineStatus.RUNNING
    rows = rt.audit.tail(0, 100_000)
    rel = [e for e in rows if e.kind == AuditKind.SAFETY_RELEASE]
    assert len(rel) == 1 and rel[0].actor.endswith("via Agent Nemo") and rel[0].payload["decision"] == "resume"
    asked = [e.payload for e in rows if e.actor.endswith("via Agent Nemo") and e.payload.get("action") == "release_safety_stop"]
    assert [p["confirmed"] for p in asked] == [False, True]


async def test_chat_release_needs_the_supervisor_session(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    await _trip(rt)
    _agent(monkeypatch, ("tool", "release_safety_stop", {"event_id": "A 線大開煉機"}), ("say", "好。"))
    async with _client(rt, LAN_PHONE[0], supervisor_pin="24682468") as phone:
        code, _ = await _say(phone, "恢復 A 線大開煉機")
        assert code == 401                                            # LAN without the PIN: same as the button
    assert rt.sim.machine(MID).status is MachineStatus.STOPPED


async def test_the_model_alone_cannot_release(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    c = await _trip(rt)
    await _until_reading_ok(rt, c)
    rt.safety_dispatch(c.id, "supervisor")
    _finish_tasks(rt, c.id)
    # the model calls the tool twice and claims it is done: still only a question, nothing runs
    _agent(monkeypatch, ("tool", "release_safety_stop", {"event_id": "A 線大開煉機"}),
           ("tool", "release_safety_stop", {"event_id": "A 線大開煉機"}), ("say", "已經幫你恢復了。"))
    async with _client(rt) as local:
        _, out = await _say(local, "A 線大開煉機可以恢復嗎")
        assert "要恢復" in out["reply"]
        _, out = await _say(local, "那現在溫度多少")                    # not a yes: the pending release is dropped
    assert rt.sim.machine(MID).status is MachineStatus.STOPPED
    assert not [e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.SAFETY_RELEASE]
    assert not [t for t in ToolName if "release" in t.value]       # not a plant tool; the commander never gets it


async def test_approving_a_plan_stays_the_button(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    _agent(monkeypatch, ("say", "好，已核准。"))
    async with _client(rt) as local:
        _, out = await _say(local, "幫我核准 10 號機的計劃")
    assert out["reply"] == MA.APPROVE_REPLY


async def test_return_to_service_from_chat_only_after_repair(tmp_path, monkeypatch):
    rt = _rt(tmp_path)
    mid = "CURE-07"
    rt.sim.set_machine_status(mid, MachineStatus.OUT_OF_SERVICE, "停用檢修")
    _agent(monkeypatch, *[("tool", "release_safety_stop", {"event_id": "7 號機"}) if i % 2 == 0 else ("say", "好。")
                          for i in range(6)])
    async with _client(rt) as local:
        _, out = await _say(local, "恢復 7 號機運轉")
        assert "還沒檢修完成" in out["reply"]
        rt._repaired_out[mid] = "EV-test"                             # its 停用檢修 is done and the fault cleared
        _, out = await _say(local, "恢復 7 號機運轉")
        assert "檢修已經完成" in out["reply"] and rt.sim.machine(mid).status is MachineStatus.OUT_OF_SERVICE
        _, out = await _say(local, "確認")
        assert "已讓" in out["reply"]
    assert rt.sim.machine(mid).status is MachineStatus.RUNNING
    row = next(e for e in rt.audit.tail(0, 100_000) if e.payload.get("msg") == "恢復運轉")
    assert row.actor.endswith("via Agent Nemo") and row.event_id == "EV-test"
