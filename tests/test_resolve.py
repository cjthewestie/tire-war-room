"""Resolution paths (CJ, 2026-10-22: 「安全核心要設計可以被解決 安全停止也要有可以走向解決的按鈕」).

(A) a Safety Core stop is released only by the supervisor, only after the reading is back in range for the core's own
re-arm count and the field check is done; the agent has no way to it. (B) a SAFE_STOP gets 請 AI 重新規劃 / 交給現場處理 /
確認結案, each producing the right event, tasks and audit."""
from __future__ import annotations

import asyncio
import pathlib
import re

import pytest
import httpx

from warroom.api import Runtime, create_app
from warroom.api.daylog import build as daylog_build
from warroom.config import Settings
from warroom.contracts import (
    AuditKind,
    EventState,
    FaultKind,
    FaultSpec,
    MachineStatus,
    SafetyRelease,
    ToolName,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
MID = "A-BIGOPEN"
LAN_PHONE = ("192.168.10.23", 50000)


def _client(rt: Runtime, host: str = "127.0.0.1", **kw) -> httpx.AsyncClient:
    """No lifespan: the test drives the runtime's ticks itself, on its own loop."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(rt, **kw), client=(host, 1)),
                             base_url="http://local")


def _rt(tmp_path, **kw) -> Runtime:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched", qdrant_path=tmp_path / "qdrant",
                 scenario="normal", llm_mode="mock", repair_fast_s=0, **kw)
    rt = Runtime(s, run_loop=False)
    if "safety" in rt.degraded or "sim" in rt.degraded:
        pytest.skip(f"sibling modules not ready: {rt.degraded}")
    return rt


async def _trip(rt: Runtime):
    """A-BIGOPEN overheats past 120 °C: the Safety Core stops it (SR-COMPOUND-120) and a case opens."""
    rt.inject_fault(FaultSpec(id="hot", machine_id=MID, kind=FaultKind.TEMP_RAMP,
                              params={"target": 135.0, "rate_c_per_min": 30.0}))
    for _ in range(600):
        await rt.tick(1.0, publish=False)
        c = rt.cases.open_for(MID)
        if c is not None:
            return c
    raise AssertionError("the Safety Core never stopped A-BIGOPEN")


def _finish_tasks(rt: Runtime, event_id: str) -> None:
    board = rt._board()
    for t in board._tasks.values():
        if t.event_id == event_id:
            t.status = "完成"


async def _until_reading_ok(rt: Runtime, c, limit: int = 900) -> None:
    for _ in range(limit):
        if c.reading_ok:
            return
        await rt.tick(1.0, publish=False)
    raise AssertionError(f"reading never back in range: {c.reading} {c.safe_samples}/{c.safe_needed}")


# ------------------------------------------------------------------ (A) Safety Core
async def test_safety_case_opens_with_rule_reading_and_latency(tmp_path):
    rt = _rt(tmp_path)
    c = await _trip(rt)
    assert c.rule_id == "SR-COMPOUND-120" and c.value >= 120 and c.latency_ms >= 0
    assert rt.sim.machine(MID).status is MachineStatus.STOPPED
    assert c.decisions == ["resume"] and c.safe_needed == rt.c.safety.rearm_samples
    assert any(x.id == c.id for x in rt.snapshot().safety_cases)


async def test_release_blocked_while_reading_unsafe(tmp_path):
    rt = _rt(tmp_path)
    c = await _trip(rt)
    assert not c.reading_ok
    with pytest.raises(ValueError, match="讀值還沒回到安全範圍"):
        rt.safety_release(c.id, SafetyRelease(field_confirmed=True, reason="現場已看過"), "supervisor")
    async with _client(rt) as local:
        r = await local.post(f"/api/safety/{c.id}/release", json={"decision": "resume", "field_confirmed": True,
                                                              "reason": "現場已看過"})
        assert r.status_code == 409 and "讀值" in r.json()["detail"]
    assert rt.sim.machine(MID).status is MachineStatus.STOPPED


async def test_release_needs_field_check_done_or_confirmed_with_reason(tmp_path):
    rt = _rt(tmp_path)
    c = await _trip(rt)
    await _until_reading_ok(rt, c)
    with pytest.raises(ValueError, match="請先派現場檢查"):
        rt.safety_release(c.id, SafetyRelease(), "supervisor")
    with pytest.raises(ValueError, match="理由"):
        rt.safety_release(c.id, SafetyRelease(field_confirmed=True, reason="  "), "supervisor")
    rt.safety_dispatch(c.id, "supervisor")
    assert len(c.task_ids) == 2 and c.sop_ids[:2] == ["SOP-SD-01", "SOP-OM-01"]
    tasks = rt._board().for_event(c.id)
    assert {t.kind for t in tasks} == {"檢修", "品管"}
    with pytest.raises(ValueError, match="現場檢查還沒完成"):
        rt.safety_release(c.id, SafetyRelease(), "supervisor")
    assert rt.sim.machine(MID).status is MachineStatus.STOPPED


async def test_release_works_for_the_supervisor_and_is_audited(tmp_path):
    rt = _rt(tmp_path)
    c = await _trip(rt)
    async with _client(rt, supervisor_pin="24682468") as local, \
            _client(rt, LAN_PHONE[0], supervisor_pin="24682468") as phone:
        assert (await local.post(f"/api/safety/{c.id}/dispatch")).status_code == 200
        _finish_tasks(rt, c.id)
        await _until_reading_ok(rt, c)
        # a LAN client without the supervisor session cannot release
        assert (await phone.post(f"/api/safety/{c.id}/release", json={"decision": "resume"})).status_code == 401
        r = await phone.post(f"/api/safety/{c.id}/release", json={"decision": "resume"},
                       headers={"X-Supervisor-Pin": "24682468"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "resolved" and r.json()["resolved_by"] == f"supervisor@{LAN_PHONE[0]}"
    assert rt.sim.machine(MID).status is MachineStatus.RUNNING
    rel = [e for e in rt.audit.tail(0, 100_000) if e.kind == AuditKind.SAFETY_RELEASE]
    assert len(rel) == 1 and rel[0].actor == f"supervisor@{LAN_PHONE[0]}"
    p = rel[0].payload
    assert p["rule_ids"] == ["SR-COMPOUND-120"] and p["decision"] == "resume" and p["status_before"] == "stopped"
    assert p["status_after"] == "running" and p["safe_samples"] >= p["safe_needed"]
    with pytest.raises(ValueError):                       # once only
        rt.safety_release(c.id, SafetyRelease(), "supervisor")


async def test_cure_low_hold_releases_by_qc_decision(tmp_path):
    rt = _rt(tmp_path)
    from warroom.contracts import SafetyAction, SafetyActionKind
    press = next(m.machine_id for m in rt.sim.machines() if m.machine_id.startswith("CURE-") and m.cure_job)
    a = SafetyAction(id="SA-test-CURE", ts=rt.now(), machine_id=press, rule_id="SR-CURE-LOW-120",
                     action=SafetyActionKind.CURE_HOLD, value=119.0, threshold=120.0, message="test", latency_ms=0.1)
    rt.sim.apply_safety_action(a)
    c = rt.cases.on_action(a, None)
    assert c.decisions == ["qc_pass", "scrap"]
    with pytest.raises(ValueError, match="解除方式"):
        rt.safety_release(c.id, SafetyRelease(decision="resume", field_confirmed=True, reason="x"), "supervisor")
    # 報廢 needs no ② (the tyre leaves the press), still the field check or a confirmed reason
    done = rt.safety_release(c.id, SafetyRelease(decision="scrap", field_confirmed=True, reason="QC 判定報廢"),
                             "supervisor")
    assert done.resolution == "scrap" and rt.sim.machine(press).cure_job is None
    assert rt.sim.machine(press).status is MachineStatus.IDLE


def test_release_is_not_reachable_from_the_agent():
    """The release is a human supervisor action: not a tool, and nothing the model drives imports or calls it."""
    assert not [t for t in ToolName if "release" in t.value or "safety" in t.value]
    banned = re.compile(r"warroom\.api\.resolve|import resolve|safety_release|/api/safety|stop_replan|stop_close|"
                        r"stop_handoff|safety_dispatch")
    for folder in ("agent", "tools", "safety"):
        for p in (ROOT / "warroom" / folder).rglob("*.py"):
            assert not banned.search(p.read_text()), p
    # the LLM path still never lifts a Safety Core stop
    from warroom.agent.state_machine import StateMachine
    assert MachineStatus.STOPPED not in StateMachine._STATUS_FROM[MachineStatus.RUNNING]


async def test_state_machine_cannot_lift_the_stop(tmp_path):
    rt = _rt(tmp_path)
    await _trip(rt)
    assert rt.state_machine._set_status(MID, MachineStatus.RUNNING) is False
    assert rt.sim.machine(MID).status is MachineStatus.STOPPED


# ------------------------------------------------------------------ (B) SAFE_STOP
def _stopped_command(rt: Runtime, reason: str = "核准逾時"):
    sm = rt.state_machine
    ev = sm.open_command("7 號機明天保養，重排", "supervisor")
    sm.stop_run(ev.id, reason)
    assert ev.state is EventState.SAFE_STOP
    return ev


async def test_stop_reason_in_plain_words(tmp_path):
    rt = _rt(tmp_path)
    sm = rt.state_machine
    assert _stopped_command(rt).stop_reason_text == "10 分鐘內沒核准"
    assert sm.stop_reason_text("連續 3 次失敗") == "連續失敗 3 次"
    assert sm.stop_reason_text("已重新規劃 3 次仍未解決") == "重新規劃 3 次仍無法驗證"
    assert sm.stop_reason_text("指揮官無回應") == "AI 模型沒有回應"
    assert sm.stop_reason_text("主管中斷") == "主管中斷"


async def test_handoff_then_close_after_tasks(tmp_path):
    rt = _rt(tmp_path)
    ev = _stopped_command(rt)
    out = rt.stop_handoff(ev.id, "supervisor")
    assert out.follow_up.status == "manual" and out.follow_up.task_ids
    assert {t.id for t in rt._board().for_event(ev.id)} == set(out.follow_up.task_ids)
    with pytest.raises(ValueError, match="現場任務還沒完成"):
        rt.stop_close(ev.id, "supervisor")
    _finish_tasks(rt, ev.id)
    out = rt.stop_close(ev.id, "supervisor", "現場已排除")
    assert out.follow_up.status == "closed" and out.follow_up.closed_by == "supervisor"
    sys = [e.payload for e in rt.audit.tail(0, 100_000) if e.event_id == ev.id and e.kind == AuditKind.SYSTEM]
    assert [p["follow_up"] for p in sys if "follow_up" in p] == ["manual", "closed"]
    assert any(e.kind == AuditKind.TASK and e.event_id == ev.id for e in rt.audit.tail(0, 100_000))
    day = next(iter(daylog_build(rt.audit.tail(0, 100_000)).values()))
    run = next(r for r in day["runs"] if r["id"] == ev.id)
    assert run["outcome"] == "safe_stop" and run["follow_up"]["status"] == "closed"
    with pytest.raises(ValueError):
        rt.stop_close(ev.id, "supervisor")


async def test_close_directly_is_a_supervisor_acknowledge(tmp_path):
    rt = _rt(tmp_path)
    ev = _stopped_command(rt, "主管中斷")
    out = rt.stop_close(ev.id, "supervisor")
    assert out.follow_up.status == "closed"
    # an acknowledged stop leaves the snapshot's always-kept list; an unresolved one stays however old
    ev2 = _stopped_command(rt)
    ids = [e.id for e in rt.snapshot().events]
    assert ev2.id in ids


async def test_replan_carries_the_previous_reason(tmp_path):
    rt = _rt(tmp_path)
    sm = rt.state_machine
    seen = []
    orig = sm.commander.plan

    async def spy(req, **kw):
        seen.append(req)
        return await orig(req, **kw)
    sm.commander.plan = spy
    ev = _stopped_command(rt)
    new = rt.stop_replan(ev.id, "supervisor")
    assert new.id != ev.id and new.previous.event_id == ev.id and new.previous.reason_text == "10 分鐘內沒核准"
    assert ev.follow_up.status == "replanned" and ev.follow_up.new_event_id == new.id
    for _ in range(60):                       # the new run reads the plant in sim time: keep the plant ticking
        if seen:
            break
        await rt.tick(1.0, publish=False)
        await asyncio.sleep(0.05)
    assert seen and seen[0].event_id == new.id
    assert seen[0].context["previous_run"] == {"event_id": ev.id, "stop_reason": "10 分鐘內沒核准"}
    assert any(e.kind == AuditKind.STATE_TRANSITION and e.event_id == new.id for e in rt.audit.tail(0, 100_000))
    with pytest.raises(ValueError, match="已經處理過"):
        rt.stop_replan(ev.id, "supervisor")


async def test_stop_buttons_need_the_supervisor_session(tmp_path):
    rt = _rt(tmp_path)
    ev = _stopped_command(rt)
    async with _client(rt, LAN_PHONE[0]) as phone:    # no PIN configured: remote writes refused
        for path in ("replan", "handoff", "close"):
            assert (await phone.post(f"/api/events/{ev.id}/{path}")).status_code == 403
    assert ev.follow_up is None


async def test_handoff_tasks_are_not_held_behind_an_unapproved_card(tmp_path):
    """Fast repair holds a task until 主管核准; an ended (SAFE_STOP) run waits for no decision, so its 交給現場處理 tasks
    start at once (live :8140: TK stayed 待派 behind the timed-out R3 card)."""
    rt = _rt(tmp_path)
    ev = _stopped_command(rt)
    assert rt._awaits_decision(ev.id) is False
