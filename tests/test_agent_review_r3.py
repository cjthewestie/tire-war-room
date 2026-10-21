"""Review round 3 on warroom/agent:
1. a schedule never puts a staffing change in force that the card did not offer and nobody was
   notified of (the solver's own cross-team pick is taken out; the shift keeps its gap);
2. an auto (R1) notice can quote cure hours only from THIS event's approved set_cure_time and cure
   formula, and only after that write ran, whatever depends_on the commander wrote;
3. a write passes the gate's "simulation feasible" check only when a feasible solve of this event
   covers it (no solve, no write);
4. an out-of-scope tool in a plan that is otherwise invalid is still intercepted and logged, and the
   audit marks the fallback plan as the fallback's, with the error and a hash of the raw output."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tests.test_agent import auto_approve, make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import MockCommander, RuleGate
from warroom.agent.commander import OpenAICommander
from warroom.config import Settings
from warroom.contracts import (
    ApprovalDecision,
    AuditKind,
    DecisionKind,
    EventState,
    GateVerdict,
    MachineStatus,
    NotifyKind,
    PlanStep,
    RiskLevel,
    ToolName,
)

stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0         # noqa: E731
always_low = lambda m, now: 119.5 if m == "CURE-07" else 128.0     # noqa: E731


def _writes(tools, tool):
    return [c.args for c in tools.calls if c.tool is tool]


# ---------------------------------------------------------------- 1: no implicit staffing
async def test_unoffered_transfer_is_not_put_in_force(tmp_path):
    """The solver adds a cross-team transfer for a shortage this card does not offer (no staffing
    options): approving the single '核准全部' option must not activate it."""
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    orig = tools._solve_schedule

    def solve(a):
        d = orig(a)
        d["staffing_options"], d["gaps"] = [], []          # card offers nothing; schedule still has the transfer
        return d
    tools._solve_schedule = solve
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    card = approvals.all()[0]
    assert len(card.options) == 1 and card.options[0].label.startswith("核准全部")
    assert any("本次不含人力調動" in a and "從 2 線調 1 人" in a for a in card.actions), card.actions
    assert ev.state is EventState.CLOSED and store.active == 2
    assert store.current().schedule.staffing_changes == []          # transfer not in force
    assert "未補（未經核准，不調動）" in store.current().reason or "從 2 線" not in store.current().reason
    assert not [a for a in _writes(tools, ToolName.NOTIFY_TEAMS) if a["kind"] in ("staff_move", "overtime")]
    amended = [e.payload for e in audit.tail(0, 0) if e.kind is AuditKind.SYSTEM and "dropped_unapproved" in e.payload]
    assert amended and amended[0]["dropped_unapproved"][0]["kind"] == "transfer"
    assert any("不含未經核准的人力調動" in t.text for t in ev.timeline)


async def test_chosen_staffing_is_the_only_staffing_in_force(tmp_path):
    """Page 11 round 1 with the A/B card: picking A 夜班加班 puts overtime (and only overtime) in force,
    and the overtime notice is sent."""
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    sup = asyncio.create_task(auto_approve(approvals, 1, option="A"))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert [o.label for o in approvals.all()[0].options] == ["夜班加班", "從 2 線調 1 人"]
    assert [c.label for c in store.current().schedule.staffing_changes] == ["夜班加班"]
    assert [a["kind"] for a in _writes(tools, ToolName.NOTIFY_TEAMS) if a["kind"] in ("staff_move", "overtime")] \
        == ["overtime"]


@pytest.mark.timeout(300)
async def test_concurrent_cure03_approval_activates_no_silent_transfer(tmp_path):
    """Real Runtime, concurrent_3: whatever a card approves, every transfer in the active schedule
    was sent to the teams as a staff_move notice."""
    from warroom.api import Runtime
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                 qdrant_path=tmp_path / "qdrant", scenario="concurrent_3", llm_mode="mock")
    rt = Runtime(s, allow_fallback=False, run_loop=False)
    await rt.start()
    rt._ingest_kb()
    seen: set[str] = set()
    try:
        for _ in range(20 * 60):
            await rt.tick(1.0, publish=False)
            for p in rt.approvals.pending():
                if p.id in seen:
                    continue
                seen.add(p.id)
                rec = next((o.id for o in p.options if o.recommended), p.options[0].id)
                rt.approvals.decide(ApprovalDecision(request_id=p.id, decision=DecisionKind.APPROVE, option_id=rec,
                                                     reason="test", via="dashboard"))
            if len(seen) >= 3 and not rt.approvals.pending() and all(
                    e.state in (EventState.CLOSED, EventState.SAFE_STOP) for e in rt.events()):
                break
        entries = rt.audit.tail(0, 0)
        moves = [e.payload for e in entries if e.kind is AuditKind.TOOL_CALL
                 and e.payload.get("args", {}).get("kind") == NotifyKind.STAFF_MOVE.value]
        transfers = [c for c in rt.c.store.current().schedule.staffing_changes if c.kind == "transfer"]
        assert seen
        assert len(transfers) <= len(moves), (transfers, moves)
    finally:
        await rt.stop()


# ---------------------------------------------------------------- 2: R1 notice numbers
def _notice(press="CURE-07", msg="7 號機延長硫化至 60 h（+32 h），已依新排程調整下一模"):
    return PlanStep(id="S9", tool=ToolName.NOTIFY_TEAMS, purpose="x",
                    args={"teams": ["curing", "body"], "kind": "info", "machine_id": press, "message": msg})


@pytest.mark.parametrize("press,msg,ev", [
    ("CURE-07", "7 號機延長硫化至 60 h（+32 h），已依新排程調整下一模", None),
    ("CURE-07", "7 號機延長硫化至 0.5 h（+0 h），已依新排程調整下一模", None),
    ("CURE-03", "3 號機延長硫化至 80 h（+56 h），已依新排程調整下一模", None),
    ("CURE-07", "7 號機延長硫化至 60 h（+32 h），已依新排程調整下一模",
     {"event_machine_id": "CURE-07", "cure_notice": {"CURE-07": {"total_h": 36.5, "extra_h": 8.5}}}),
    ("CURE-03", "3 號機延長硫化至 36.5 h（+8.5 h），已依新排程調整下一模",
     {"event_machine_id": "CURE-07", "cure_notice": {"CURE-03": {"total_h": 36.5, "extra_h": 8.5}}}),
    ("CURE-03", "3 號機異常處置中，請留意後續通知", {"event_machine_id": "CURE-07"}),
])
def test_r1_numbers_and_machine_need_event_evidence(press, msg, ev):
    kw = {"evidence": ev} if ev is not None else {}
    d = RuleGate().check(_notice(press, msg), EventState.EXECUTE, True, **kw)
    assert d.risk is RiskLevel.R3_CRITICAL and d.verdict is GateVerdict.NEEDS_APPROVAL, d.reasons


def test_r1_cure_notice_with_matching_evidence_is_auto():
    ev = {"event_machine_id": "CURE-07", "cure_notice": {"CURE-07": {"total_h": 36.5, "extra_h": 8.5}}}
    d = RuleGate().check(_notice(msg="7 號機延長硫化至 36.5 h（+8.5 h），已依新排程調整下一模"), EventState.EXECUTE, True,
                         evidence=ev)
    assert d.risk is RiskLevel.R1_NOTIFY and d.verdict is GateVerdict.ALLOW, d.reasons


class R1CureNotice(MockCommander):
    def build(self, req):
        p = super().build(req)
        steps = [PlanStep(id="S1", tool=ToolName.READ_SENSORS, args={"machine_ids": ["CURE-07"]}, purpose="讀"),
                 PlanStep(id="S2", tool=ToolName.NOTIFY_TEAMS, purpose="通知",
                          args={"teams": ["curing", "body"], "kind": "info", "machine_id": "CURE-07",
                                "message": "7 號機延長硫化至 60 h（+32 h），已依新排程調整下一模"})]
        return p.model_copy(update={"steps": steps})


async def test_ai_cure_hours_in_a_notice_never_go_out_on_their_own(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=R1CureNotice())
    seen = {}

    async def sup():
        for _ in range(2):
            while not approvals.pending():
                await asyncio.sleep(0.001)
            seen.setdefault("before", list(_writes(tools, ToolName.NOTIFY_TEAMS)))
            approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                              reason="數字不對"))
    t = asyncio.create_task(sup())
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    # held for the supervisor, not sent as 已自動 (only the R1 maintenance notice code adds for a press drop ran)
    assert [a["kind"] for a in seen["before"]] == ["maintenance"]
    assert approvals.all()[0].risk is RiskLevel.R3_CRITICAL
    assert [a["kind"] for a in _writes(tools, ToolName.NOTIFY_TEAMS)] == ["maintenance"]


class NoDependsOn(MockCommander):
    """Page-11 plan, but the cure notice lists no depends_on (it would otherwise run at once as R1)."""
    def build(self, req):
        p = super().build(req)
        steps = [s.model_copy(update={"depends_on": []}) if s.tool is ToolName.NOTIFY_TEAMS else s for s in p.steps]
        return p.model_copy(update={"steps": steps})


async def test_cure_notice_waits_for_its_approved_set_cure_time(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=NoDependsOn())
    seen = {}

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        seen["before"] = list(_writes(tools, ToolName.NOTIFY_TEAMS))
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.APPROVE))
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    assert [a["kind"] for a in seen["before"]] == ["maintenance"]     # the cure notice waited; maintenance is R1
    order = [(c.tool, c.args.get("kind")) for c in tools.calls if c.tool in (ToolName.SET_CURE_TIME, ToolName.NOTIFY_TEAMS)]
    i_set = order.index((ToolName.SET_CURE_TIME, None))
    i_info = order.index((ToolName.NOTIFY_TEAMS, "info"))
    assert i_set < i_info
    info = [a for a in _writes(tools, ToolName.NOTIFY_TEAMS) if a["kind"] == "info"][0]
    assert "36.5 h（+8.5 h）" in info["message"]
    assert ev.state is EventState.CLOSED


# ---------------------------------------------------------------- 3: no simulation, no write
class NoSimWrites(MockCommander):
    def build(self, req):
        p = super().build(req)
        if req.phase != "plan":
            return p
        steps = [PlanStep(id="S1", tool=ToolName.CURE_FORMULA, purpose="公式",
                          args={"size": "M", "actual_temp_c": 122, "elapsed_h": 10, "ref_cure_h": 28}),
                 PlanStep(id="S2", tool=ToolName.SET_CURE_TIME, depends_on=["S1"], purpose="設定",
                          args={"press_id": "CURE-07", "total_cure_h": "$S1.total_cure_h", "reason": "r"}),
                 PlanStep(id="S3", tool=ToolName.NOTIFY_TEAMS, purpose="停用",
                          args={"teams": ["curing"], "kind": "machine_out", "machine_id": "CURE-03",
                                "message": "3 號機停用"})]
        return p.model_copy(update={"steps": steps})


async def test_writes_without_a_covering_solve_are_blocked(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=NoSimWrites())
    sup = asyncio.create_task(auto_approve(approvals, 3))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    sup.cancel()
    gates = [e.payload for e in audit.tail(0, 0) if e.kind is AuditKind.GATE and "decision" in e.payload]
    s2 = next(g["decision"] for g in gates if g["decision"]["step_id"] == "S2" and g["decision"]["tool"] == "set_cure_time")
    s3 = next(g["decision"] for g in gates if g["decision"]["step_id"] == "S3" and g["decision"]["tool"] == "notify_teams")
    for d in (s2, s3):
        assert d["verdict"] == "reject" and d["checks"]["simulation_feasible"] is False, d
    assert any("no feasible solve_schedule" in r for r in s2["reasons"]), s2["reasons"]
    # nothing of that plan ran: no 36.5 h without a schedule check, CURE-03 still in service
    assert not [a for a in _writes(tools, ToolName.NOTIFY_TEAMS) if a.get("machine_id") == "CURE-03"]
    assert sim.status["CURE-03"] is not MachineStatus.OUT_OF_SERVICE
    assert not any(c.delivery_impact == "不受影響（排程未變更）" for c in approvals.all())


def test_delivery_row_never_claims_unaffected_without_a_solve(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    from warroom.agent.state_machine import _Run
    from warroom.contracts import Event
    ev = Event(id="E", opened_at=clock.now(), machine_id="CURE-03", anomaly=page11_anomaly(), state=EventState.GATE)
    assert not sm._delivery_text(_Run(ev=ev)).startswith("不受影響")


# ---------------------------------------------------------------- 4: interception on fallback
def _plan_json(stray_key: bool) -> str:
    p = {"alert_card": {"title": "7 號機溫度下降", "summary": "x"}, "goal": "g",
         "hypothesis": {"id": "H1", "statement": "s", "cause_code": "steam_pressure_low"},
         "steps": [{"id": "S1", "tool": "write_plc", "args": {"machine_id": "CURE-07", "setpoint": 140},
                    "purpose": "寫 PLC"},
                   {"id": "S2", "tool": "read_sensors", "args": {"machine_ids": ["CURE-07"]}, "purpose": "讀"}]}
    if stray_key:
        p["steps"][1]["risk"] = "R0"
    return json.dumps(p, ensure_ascii=False)


def _transport(body: str):
    def handler(req):
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "m",
                 "choices": [{"index": 0, "delta": {"content": body}, "finish_reason": None}]}
        sse = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("body", [_plan_json(True), _plan_json(True)[:-40]])   # invalid plan / truncated JSON
async def test_out_of_scope_tool_logged_even_when_plan_falls_back(tmp_path, body):
    cmd = OpenAICommander(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=True),
                          http_client=httpx.AsyncClient(transport=_transport(body)))
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=cmd, approval_timeout_s=0.01)
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    ents = audit.tail(0, 0)
    inter = [e.payload for e in ents if e.kind is AuditKind.INTERCEPTED and e.actor == "commander"]
    assert any(p["step"].get("tool") == "write_plc" for p in inter), inter
    llm = [e.payload for e in ents if e.kind is AuditKind.LLM_CALL][0]
    assert llm["fell_back_to_mock"] and llm["plan_source"] == "mock_fallback"
    assert llm["steps"] == [] and llm["fallback_steps"]                 # mock's steps not credited to the model
    assert llm["error"] and len(llm["raw_sha256"]) == 64 and "write_plc" in llm["raw_excerpt"]


async def test_valid_llm_plan_is_credited_to_the_commander(tmp_path):
    cmd = OpenAICommander(Settings(llm_mode="llm", base_url="http://x/v1", llm_fallback_to_mock=True),
                          http_client=httpx.AsyncClient(transport=_transport(_plan_json(False))))
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=cmd, approval_timeout_s=0.01)
    await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    ents = audit.tail(0, 0)
    llm = [e.payload for e in ents if e.kind is AuditKind.LLM_CALL][0]
    assert llm["plan_source"] == "commander" and llm["steps"] == ["S2"] and cmd.last_error is None
    assert sum(1 for e in ents if e.kind is AuditKind.INTERCEPTED and e.actor == "commander") == 1
