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
