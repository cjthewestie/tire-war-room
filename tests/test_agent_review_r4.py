"""Review round 4 on warroom/agent: the page-7/11 decomposition reads S1..S5, a staffing option cannot
be modified into something the schedule does not record, a supervisor veto is not a failure, a no-op
schedule asks for no approval, a slip on a held press is blamed on the hold, the audit head is signed
and must name the last entry, a cure time or a press-out only passes with the schedule that carries
it, an approval whose schedule went stale is carried over only for identical content, and tool
errors reach the timeline in Chinese."""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_agent import auto_approve, make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import JsonlAuditLog, MockCommander
from warroom.agent.approvals import ApprovalBrokerImpl, ApprovalError
from warroom.agent.narrate import decompose_text, gate_reason_text, tool_error_text
from warroom.contracts import (
    Anomaly,
    AnomalyKind,
    ApprovalDecision,
    ApprovalOption,
    ApprovalRequest,
    AuditKind,
    Channel,
    CommanderRequest,
    DecisionKind,
    EventState,
    PlanStep,
    RiskLevel,
    Severity,
    ToolName,
)

stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0         # noqa: E731


def _writes(tools, tool):
    return [c.args for c in tools.calls if c.tool is tool]


# ---------------------------------------------------------------- 1: S1..S5 as on pages 7 and 11
def test_mock_plan_decomposes_exactly_as_pages_7_and_11():
    req = CommanderRequest(phase="plan", event_id="E", anomaly=page11_anomaly(), allowed_tools=list(ToolName),
                           context={"cure_job": {"size": "M", "setpoint_c": 128, "cure_elapsed_h": 10}})
    plan = MockCommander().build(req)
    assert [s.id for s in plan.steps] == ["S1", "S2", "S3", "S4", "S4b", "S5"]
    assert decompose_text(plan.steps) == \
        "S1 硫化公式、S2 訂單班表 → S3 排程（依賴 S1、S2）→ S4 確認（需核准）→ S5 通知"


# ---------------------------------------------------------------- 2: staffing options are not editable
def _staff_req():
    from warroom.factory import TZ
    from datetime import datetime
    now = datetime(2026, 10, 3, 2, 31, tzinfo=TZ)
    return ApprovalRequest(id="AP-X-1", event_id="E", created_at=now, risk=RiskLevel.R3_CRITICAL, title="t",
                           reason="r", options=[
                               ApprovalOption(id="A", label="夜班加班", tool=ToolName.NOTIFY_TEAMS,
                                              args={"teams": ["body"], "kind": "overtime", "message": "10/03 夜班加班（1 人）"}),
                               ApprovalOption(id="B", label="從 2 線調 1 人", tool=ToolName.NOTIFY_TEAMS,
                                              args={"teams": ["side", "body"], "kind": "staff_move", "message": "m"})])


def test_modify_of_a_staffing_option_is_refused():
    b = ApprovalBrokerImpl()
    b.request(_staff_req())
    with pytest.raises(ApprovalError, match="人力方案 A 不可修改"):
        b.decide(ApprovalDecision(request_id="AP-X-1", decision=DecisionKind.MODIFY, option_id="A",
                                  modified_args={"teams": ["curing"], "message": "夜班加班 2 人"}))
    assert b.pending()                                           # still waiting for a valid decision
    # repeating the option's own values is not an edit
    out = b.decide(ApprovalDecision(request_id="AP-X-1", decision=DecisionKind.APPROVE, option_id="A",
                                    modified_args={"teams": ["body"]}))
    assert out.decision.option_id == "A"


async def test_state_machine_ignores_staffing_edits_from_a_lenient_broker(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    approvals.decide_orig = approvals.decide

    def lenient(d):                                    # a broker without the check (e.g. a fallback)
        import warroom.agent.approvals as ap
        saved, ap.staffing_modification = ap.staffing_modification, lambda o, m: []
        try:
            return approvals.decide_orig(d)
        finally:
            ap.staffing_modification = saved
    approvals.decide = lenient

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        r = approvals.pending()[0]
        approvals.decide(ApprovalDecision(request_id=r.id, decision=DecisionKind.MODIFY, option_id="A",
                                          modified_args={"teams": ["side"], "message": "夜班加班 2 人"}))
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    ot = [a for a in _writes(tools, ToolName.NOTIFY_TEAMS) if a["kind"] == "overtime"]
    assert ot and all(a["teams"] == ["curing"] and "2 人" not in a["message"] for a in ot)
    assert any(e.kind is AuditKind.INTERCEPTED and e.payload.get("ignored_modified_args") for e in audit.tail(0, 0))
    assert not any("否決跨組調人" in t.text and "修改" in t.text for t in ev.timeline)


# ---------------------------------------------------------------- 3: no-op schedule, held-press slip
def _slip(mid="CURE-07"):
    from tests.agent_fakes import at
    return Anomaly(id="AN-S", ts=at("02:40:00"), machine_id=mid, kind=AnomalyKind.SCHEDULE_SLIP,
                   channel=Channel.TEMP, value=4640.0, baseline=0.0, severity=Severity.WARN,
                   message="7 號機 #M-201 計畫 10/03 20:14 出模，仍在模內（stopped）")


def test_slip_on_a_held_press_is_blamed_on_the_hold():
    req = CommanderRequest(phase="plan", event_id="E", anomaly=_slip(), allowed_tools=list(ToolName),
                           context={"machine": {"status": "stopped"}})
    plan = MockCommander().build(req)
    assert plan.hypothesis.cause_code == "safety_hold"
    assert "非作業落後" in plan.hypothesis.statement and "作業落後計畫" not in plan.hypothesis.statement
    solve = next(s for s in plan.steps if s.tool is ToolName.SOLVE_SCHEDULE)
    assert solve.args["unavailable_presses"] == ["CURE-07"]
    running = MockCommander().build(req.model_copy(update={"context": {"machine": {"status": "running"}}}))
    assert running.hypothesis.cause_code == "schedule_slip"


async def test_noop_schedule_asks_no_approval(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    real = tools._solve_schedule

    def no_moves(a):
        d = real(a)
        d["moves"] = []
        d["schedule"]["staffing_changes"] = []
        d["staffing_options"], d["gaps"] = [], []
        return d
    tools._solve_schedule = no_moves
    ev = await asyncio.wait_for(sm.handle(_slip("CURE-03")), 10)
    assert not approvals.all()                                  # nothing to approve
    assert not _writes(tools, ToolName.CONFIRM_SCHEDULE) and store.active == 1
    assert any("無機台異動、無人力調動）→ 不需核准" in t.text for t in ev.timeline)
    assert ev.state is EventState.CLOSED and ev.consecutive_failures == 0


# ---------------------------------------------------------------- 4: audit head signed, names the last entry
def _five(p):
    log = JsonlAuditLog(p)
    for i in range(5):
        log.append(AuditKind.APPROVAL_DECISION if i == 4 else AuditKind.SYSTEM, "supervisor", {"i": i})
    return log


@pytest.mark.parametrize("mode", ["seq0", "earlier_unsigned", "earlier_forged_mac"])
def test_cut_log_with_rewritten_head_is_archived(tmp_path, monkeypatch, mode):
    monkeypatch.delenv("WARROOM_AUDIT_ANCHOR", raising=False)
    p = tmp_path / "audit.jsonl"
    _five(p)
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines[:3]) + "\n")                       # entries 4-5 (the decision) deleted
    last = json.loads(lines[2])
    head = {"seq0": {"seq": 0, "hash": "x"},
            "earlier_unsigned": {"seq": last["seq"], "hash": last["hash"]},
            "earlier_forged_mac": {"seq": last["seq"], "hash": last["hash"], "mac": "0" * 64}}[mode]
    (tmp_path / "audit.jsonl.head").write_text(json.dumps(head))
    log2 = JsonlAuditLog(p)
    assert log2.recovery and "archived" in log2.recovery[0]["msg"], log2.recovery
    assert (tmp_path / "audit.jsonl.broken-1").exists()
    assert len(log2) == 1 and log2.verify_chain()                 # new chain, first entry says why


def test_signed_head_that_is_not_the_last_entry_is_caught(tmp_path, monkeypatch):
    """A head the log itself signed, but saved two entries earlier and put back after a cut."""
    monkeypatch.delenv("WARROOM_AUDIT_ANCHOR", raising=False)
    p = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(p)
    for i in range(2):
        log.append(AuditKind.SYSTEM, "x", {"i": i})
    old_head = (tmp_path / "audit.jsonl.head").read_text()
    for i in range(3):
        log.append(AuditKind.SYSTEM, "x", {"i": i})
    (tmp_path / "audit.jsonl.head").write_text(old_head)          # log keeps 5 entries, head says 2
    log2 = JsonlAuditLog(p)
    assert log2.recovery and "does not name the log's last entry" in log2.recovery[0]["reason"]


def test_head_one_behind_after_power_cut_is_accepted_and_recorded(tmp_path, monkeypatch):
    monkeypatch.delenv("WARROOM_AUDIT_ANCHOR", raising=False)
    p = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(p)
    for i in range(3):
        log.append(AuditKind.SYSTEM, "x", {"i": i})
    head3 = (tmp_path / "audit.jsonl.head").read_text()
    log.append(AuditKind.SYSTEM, "x", {"i": 3})
    (tmp_path / "audit.jsonl.head").write_text(head3)             # line fsync'd, anchor write lost
    log2 = JsonlAuditLog(p)
    assert len(log2) == 5 and log2.verify_chain()                 # 4 kept + the recovery note
    assert "one entry behind" in log2.recovery[0]["msg"]
    assert not (tmp_path / "audit.jsonl.broken-1").exists()
    log3 = JsonlAuditLog(p)                                       # anchor re-written: clean reopen
    assert log3.recovery == [] and len(log3) == 5


def test_clean_reopen_keeps_chain(tmp_path, monkeypatch):
    monkeypatch.delenv("WARROOM_AUDIT_ANCHOR", raising=False)
    p = tmp_path / "audit.jsonl"
    _five(p)
    log2 = JsonlAuditLog(p)
    assert log2.recovery == [] and len(log2) == 5 and log2.verify_chain()


# ---------------------------------------------------------------- 5/6/7: no split-solve bypass
class SplitCure(MockCommander):
    """S3 solves WITH the 36.5 h override (covers set_cure_time); S7 solves WITHOUT it; S4 confirms S7."""

    def build(self, req):
        p = super().build(req)
        if req.phase != "plan":
            return p
        by = {s.id: s for s in p.steps}
        s7 = PlanStep(id="S7", tool=ToolName.SOLVE_SCHEDULE, depends_on=["S2"], purpose="重排", args={"time_limit_s": 10.0})
        s4 = by["S4"].model_copy(update={"args": {"schedule_version": "$S7.schedule_version", "reason": "重排"},
                                          "depends_on": ["S7"]})
        return p.model_copy(update={"steps": [by["S1"], by["S2"], by["S3"], s7, s4, by["S4b"], by["S5"]]})


async def test_cure_time_needs_the_confirmed_schedule_to_carry_it(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=SplitCure())
    sup = asyncio.create_task(auto_approve(approvals, 3))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    sup.cancel()
    first = approvals.all()[0]
    assert not any(a.startswith("S4b") for a in first.actions), first.actions      # never on the card
    r1 = ev.prior_rounds[0].steps if ev.prior_rounds else ev.steps
    s4b = next(sr for sr in r1 if sr.step.id == "S4b")
    assert s4b.state == "rejected"
    dropped = [e for e in audit.tail(0, 0) if e.payload.get("dropped") == "rejected" and e.payload.get("step_id") == "S4b"]
    assert dropped and "not covered by the schedule confirmed this round" in dropped[0].payload["reason"]
    # the cure time only went out in a later round, right after the schedule that carries it
    order = [c.tool for c in tools.calls if c.tool in (ToolName.CONFIRM_SCHEDULE, ToolName.SET_CURE_TIME)]
    for i, t in enumerate(order):
        if t is ToolName.SET_CURE_TIME:
            assert ToolName.CONFIRM_SCHEDULE in order[:i], order
    assert not any("S5" == sr.step.id and sr.state == "ok" for sr in r1)          # its notice was dropped too


class RejectedConfirm(MockCommander):
    """The confirm references a step id the resolver does not know: the gate rejects it. The cure time
    beside it must not go out on its own (round-4 finding 7)."""

    def build(self, req):
        p = super().build(req)
        if req.phase != "plan":
            return p
        steps = [s.model_copy(update={"args": {**s.args, "schedule_version": "$S3X.schedule_version"}})
                 if s.tool is ToolName.CONFIRM_SCHEDULE else s for s in p.steps]
        return p.model_copy(update={"steps": steps})


async def test_cure_time_does_not_run_when_the_confirm_is_gate_rejected(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=RejectedConfirm())
    sup = asyncio.create_task(auto_approve(approvals, 3))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    sup.cancel()
    r1 = ev.prior_rounds[0].steps if ev.prior_rounds else ev.steps
    states = {sr.step.id: sr.state for sr in r1}
    assert states["S4"] == "rejected" and states["S4b"] == "rejected" and states["S5"] == "skipped", states
    # round 1 had nothing left to approve (no card); a cure time only ever runs right after the
    # confirm of the schedule that carries it (a later round)
    order = [c.tool for c in tools.calls if c.tool in (ToolName.CONFIRM_SCHEDULE, ToolName.SET_CURE_TIME)]
    for i, t in enumerate(order):
        if t is ToolName.SET_CURE_TIME:
            assert ToolName.CONFIRM_SCHEDULE in order[:i], order
    round1_writes = [sr for sr in r1 if sr.state == "ok" and sr.step.tool is ToolName.SET_CURE_TIME]
    assert not round1_writes


async def test_press_out_needs_the_confirmed_schedule_to_exclude_the_press(tmp_path):
    """Real Runtime (scenario press7_temp_drop): S3 solves without press 7, S7 with it; confirm S7 +
    machine_out press 7. The press-out is refused before the card; press 7 is never marked out of
    service with a schedule that still loads it."""
    from warroom.api import Runtime
    from warroom.config import Settings
    from warroom.contracts import ApprovalStatus, MachineStatus

    class SplitOut(MockCommander):
        def build(self, req):
            p = super().build(req)
            if req.phase != "plan" or req.anomaly.machine_id != "CURE-07":
                return p
            s3 = PlanStep(id="S3", tool=ToolName.SOLVE_SCHEDULE, purpose="x",
                          args={"unavailable_presses": ["CURE-07"], "time_limit_s": 10.0})
            s7 = PlanStep(id="S7", tool=ToolName.SOLVE_SCHEDULE, purpose="y", args={"time_limit_s": 10.0})
            s4 = PlanStep(id="S4", tool=ToolName.CONFIRM_SCHEDULE, depends_on=["S7"], purpose="z",
                          args={"schedule_version": "$S7.schedule_version", "reason": "重排"})
            s6 = PlanStep(id="S6", tool=ToolName.NOTIFY_TEAMS, depends_on=["S4"], purpose="w",
                          args={"teams": ["curing", "maintenance"], "kind": "machine_out", "machine_id": "CURE-07",
                                "message": "7 號機停用"})
            return p.model_copy(update={"steps": [s3, s7, s4, s6]})

    tmp = Path(tempfile.mkdtemp(dir=tmp_path))
    s = Settings(audit_path=tmp / "audit.jsonl", schedule_dir=tmp / "sched", qdrant_path=tmp / "qdrant",
                 scenario="press7_temp_drop", llm_mode="mock")
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    await rt.start()
    rt.state_machine.commander = SplitOut()
    try:
        card = None
        for _ in range(40 * 60):
            await rt.tick(1.0, publish=False)
            if rt.approvals.pending():
                card = rt.approvals.pending()[0]
                break
        assert card is not None
        assert not any("停用" in a for a in card.actions), card.actions
        ev = rt.events()[0]
        s6 = next(sr for sr in ev.steps if sr.step.id == "S6")
        assert s6.state == "rejected"
        rt.approvals.decide(ApprovalDecision(request_id=card.id, decision=DecisionKind.APPROVE,
                                             option_id=card.options[0].id, via="dashboard"))
        for _ in range(30):
            await rt.tick(1.0, publish=False)
        assert rt.approvals.get(card.id).status is ApprovalStatus.APPROVED
        m = rt.sim.machine("CURE-07")
        cur = rt.c.store.current()
        future7 = [j for j in cur.schedule.jobs if j.press_id == "CURE-07" and j.mold_in > rt.now()]
        assert not (m.status is MachineStatus.OUT_OF_SERVICE and future7)
    finally:
        await rt.stop()


# ---------------------------------------------------------------- 8: stale approval carried over only if identical
async def test_stale_approval_with_different_content_asks_again(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    real_confirm, real_solve = tools._confirm_schedule, tools._solve_schedule
    state = {"confirm": 0, "solve": 0}

    def flaky(a):
        state["confirm"] += 1
        if state["confirm"] <= 2:
            raise RuntimeError("schedule v2 is stale: solved against v1, but v9 is active now")
        return real_confirm(a)

    def solve(a):
        state["solve"] += 1
        d = real_solve(a)
        if state["solve"] >= 2:                                   # the re-solve moves #A-311 elsewhere
            d["moves"] = [{**d["moves"][0], "to_press": "CURE-10"}]
        return d
    tools._confirm_schedule, tools._solve_schedule = flaky, solve
    real_call = tools.call

    def call(c):
        from warroom.contracts import ToolResult
        try:
            return real_call(c)
        except RuntimeError as e:
            return ToolResult(call_id=c.id, tool=c.tool, ok=False, error=f"ToolError: {e}", attempt=c.attempt)
    tools.call = call
    sup = asyncio.create_task(auto_approve(approvals, 2))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    reqs = approvals.all()
    assert len(reqs) == 2 and not reqs[1].id.endswith("-rebased")          # a real second card
    assert reqs[1].decision.via != "auto"
    assert any("與主管已核准的內容不同 → 需重新核准" in t.text for t in ev.timeline)
    assert any("✗ 已核准但未生效（排程已被另一事件更新）" in t.text for t in ev.timeline)


# ---------------------------------------------------------------- 9: Chinese tool errors on the timeline
def test_tool_errors_and_gate_reasons_are_translated():
    t = tool_error_text(ToolName.CONFIRM_SCHEDULE,
                        "ToolError: schedule v3 is stale: solved against v1, but v2 is active now; re-run solve_schedule")
    assert t == "排程 v3 已過期：求解時依據 v1，期間 v2 已生效，需依現行排程重新求解"
    assert "ToolError" not in tool_error_text(ToolName.SOLVE_SCHEDULE, "ToolError: boom")
    assert gate_reason_text(["args contain an unresolved $S reference",
                             "simulation not feasible: writes are blocked (x)"]) == "引用的前一步結果不存在；沒有對應的可行模擬"


async def test_stale_confirm_timeline_has_no_raw_english(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    real_confirm = tools._confirm_schedule
    state = {"n": 0}

    def flaky(a):
        state["n"] += 1
        if state["n"] <= 2:
            raise RuntimeError("schedule v2 is stale: solved against v1, but v9 is active now; re-run solve_schedule")
        return real_confirm(a)
    tools._confirm_schedule = flaky
    real_call = tools.call

    def call(c):
        from warroom.contracts import ToolResult
        try:
            return real_call(c)
        except RuntimeError as e:
            return ToolResult(call_id=c.id, tool=c.tool, ok=False, error=f"ToolError: {e}", attempt=c.attempt)
    tools.call = call
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    texts = [t.text for t in ev.timeline]
    assert not any("ToolError" in x or "confirm_schedule" in x or "is stale" in x for x in texts), texts
    assert any("排程 v2 已過期：求解時依據 v1，期間 v9 已生效" in x for x in texts)
    # the raw message is kept in the audit log
    assert any("is stale" in str(e.payload.get("error")) for e in audit.tail(0, 0) if e.kind is AuditKind.TOOL_RESULT)
