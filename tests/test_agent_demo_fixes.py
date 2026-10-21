"""Demo-run fixes (10-21 walk-through, group B): a real model's solve without the cure override still
gets the formula value; a press H1 notifies maintenance so the steam fault gets a repair task; a card that
must be re-approved because another event's schedule took effect first is a new version of the same card."""
from __future__ import annotations

import asyncio

from tests.test_agent import auto_approve, make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import MockCommander
from warroom.contracts import AuditKind, EventState, NotifyKind, ToolName

stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0         # noqa: E731


class NoOverride(MockCommander):
    """What the real model sent in 2 of 3 page-11 runs: S3 solve_schedule without cure_overrides_h."""

    def build(self, req):
        p = super().build(req)
        if req.phase != "plan":
            return p
        steps = [s.model_copy(update={"args": {k: v for k, v in s.args.items() if k != "cure_overrides_h"}})
                 if s.tool is ToolName.SOLVE_SCHEDULE else s for s in p.steps]
        return p.model_copy(update={"steps": steps})


async def test_solve_without_cure_override_gets_the_formula_value(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=NoOverride())
    sup = asyncio.create_task(auto_approve(approvals, 1))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await sup
    assert ev.state is EventState.CLOSED
    assert tools.calls_of(ToolName.SOLVE_SCHEDULE)[0].args["cure_overrides_h"] == {"CURE-07": 36.5}
    filled = [e.payload for e in audit.tail(0, 0) if "cure_overrides_from_formula" in e.payload]
    assert filled and filled[0]["cure_overrides_from_formula"] == {"CURE-07": {"from": None, "to": 36.5}}
    card = approvals.all()[0]
    # the card keeps the cure time and the schedule, not just 「通知維修」 with 「排程未變更」
    assert any(a.startswith("S4b") and "36.5 h" in a for a in card.actions), card.actions
    assert any("7 號硫化 36.5 h" in a for a in card.actions), card.actions
    assert card.delivery_impact == "不受影響"
    assert [c.args["total_cure_h"] for c in tools.calls_of(ToolName.SET_CURE_TIME)] == [36.5]


async def test_solve_that_takes_the_press_out_gets_no_cure_override(tmp_path):
    """Round 2 (H2, press out of service) has no formula result and takes the press out: nothing is filled."""
    from tests.test_agent_page11 import press7_temp
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, press7_temp, commander=NoOverride())
    sup = asyncio.create_task(auto_approve(approvals, 2))
    await asyncio.wait_for(sm.handle(page11_anomaly()), 20)
    await sup
    solves = tools.calls_of(ToolName.SOLVE_SCHEDULE)
    out = [c.args for c in solves if "CURE-07" in (c.args.get("unavailable_presses") or [])]
    assert out and all("CURE-07" not in (a.get("cure_overrides_h") or {}) for a in out)
    assert not any(e.kind is AuditKind.GATE and e.payload.get("step_id") == "S4b"
                   and e.payload.get("dropped") == "rejected" for e in audit.tail(0, 0))
    assert NotifyKind.MACHINE_OUT.value in [c.args["kind"] for c in tools.calls_of(ToolName.NOTIFY_TEAMS)]


# ---------------------------------------------------------------- a press H1 notifies maintenance
class _Board:
    """Stands in for World.tasks: records what the state machine dispatches and withdraws."""

    def __init__(self):
        self.dispatched: list[tuple[str | None, str | None]] = []
        self.cancelled: list[str] = []

    def dispatch(self, req, now):
        self.dispatched.append((req.step_id, req.args.get("kind")))
        return []

    def cancel(self, event_id, step_id, now):
        hits = [s for s, k in self.dispatched if s == step_id and k == NotifyKind.MAINTENANCE.value]
        self.cancelled += hits
        return [f"TK-{step_id}"] if hits else []


def _with_board(sim):
    from types import SimpleNamespace
    board = _Board()
    sim.world = SimpleNamespace(tasks=board)
    return board


async def test_press_h1_notifies_maintenance_once_without_an_option(tmp_path):
    """The rule plan (and a real model's) has no maintenance step for a press: code adds the R1 notice, it runs
    before the card (no extra option), and a supervisor reject that re-plans H1 does not send it again."""
    from warroom.contracts import ApprovalDecision, DecisionKind
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable)
    board = _with_board(sim)

    async def sup():
        while not approvals.pending():
            await asyncio.sleep(0.001)
        approvals.decide(ApprovalDecision(request_id=approvals.pending()[0].id, decision=DecisionKind.REJECT,
                                          reason="不跨組"))
        await auto_approve(approvals, 1)
    t = asyncio.create_task(sup())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    await t
    maint = [c for c in tools.calls_of(ToolName.NOTIFY_TEAMS) if c.args["kind"] == NotifyKind.MAINTENANCE.value]
    assert len(maint) == 1, [c.args for c in maint]
    assert maint[0].args == {"teams": ["maintenance"], "kind": "maintenance", "machine_id": "CURE-07",
                             "message": "7 號機溫度下降，請檢查蒸汽供應與溫度計"}
    assert maint[0].step_id == "S6"
    first = approvals.all()[0]
    assert [o.label for o in first.options] == ["夜班加班", "從 2 線調 1 人"]        # no option added
    assert not any("通知維修" in a for a in first.actions)
    assert "已通知維修" in first.done
    assert (maint[0].step_id, NotifyKind.MAINTENANCE.value) in board.dispatched     # a 檢修 task for the crew
    assert ev.state is EventState.CLOSED and "已通知維修" in ev.outcome
    assert not board.cancelled


async def test_press_out_withdraws_the_open_h1_repair_task(tmp_path):
    """Page 11: H1's 檢修 must not finish (and clear the fault) while the 7 號機停用 card waits; the press-out
    notice creates 停用檢修 instead."""
    from tests.test_agent_page11 import press7_temp
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, press7_temp)
    board = _with_board(sim)
    sup = asyncio.create_task(auto_approve(approvals, 2))
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 20)
    await sup
    assert board.cancelled == ["S6"]
    assert any("改為停用檢修" in t.text for t in ev.timeline)
    kinds = [c.args["kind"] for c in tools.calls_of(ToolName.NOTIFY_TEAMS)]
    assert kinds.count(NotifyKind.MAINTENANCE.value) == 1 and NotifyKind.MACHINE_OUT.value in kinds


async def test_mill_event_gets_no_press_maintenance_step(tmp_path):
    from tests.test_agent import mill_anomaly
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, lambda m, now: 115.0)
    sup = asyncio.create_task(auto_approve(approvals, 3))
    await asyncio.wait_for(sm.handle(mill_anomaly()), 10)
    sup.cancel()
    assert not any(e.payload.get("maintenance_notice_added") for e in audit.tail(0, 0))
