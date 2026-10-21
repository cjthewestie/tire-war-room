"""Pre-planned 核准區 items (CJ, 2026-10-21: 「流程應該改成有事項需要計劃的時候要已經計劃好了等我點下面的核准或是討論」): an
at-risk order gets its plan drafted before anyone opens it, once per item, at the pre-plan priority, at most PREPLAN_MAX
at a time. Only WHEN planning starts changes; the run is the normal PLAN → GATE → approval path."""
from __future__ import annotations

import asyncio
import time

from tests.test_plan_talk import Plant, _rt
from warroom.agent.orchestrator import PRIORITY
from warroom.contracts import TERMINAL_STATES


async def _until(cond, timeout=60.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_at_risk_orders_are_planned_before_anyone_clicks(tmp_path):
    rt = _rt(tmp_path, "normal")
    rt.preplan_on = True
    risky = {"#A-311": "eta x", "#M-201": "eta y", "SO-004": "eta z"}
    rt.at_risk_orders = lambda: dict(risky)
    async with Plant(rt) as p:
        t0 = time.monotonic()
        assert await _until(lambda: len(rt._preplanned) == rt.PREPLAN_MAX)
        first = dict(rt._preplanned)
        assert len(first) == 2                                            # the cap: two at once
        for _sig, eid in first.values():
            ev = rt.state_machine.get(eid)
            assert ev.command.endswith("可能晚交，請重排保交期") and rt.orch.queue.tags[eid][1] == PRIORITY["preplan"]
        assert PRIORITY["machine"] < PRIORITY["preplan"] < PRIORITY["smalltalk"]
        done = lambda: all(rt.state_machine.get(e).state.value == "await_approval" or rt.state_machine.get(e).state in TERMINAL_STATES
                           for _s, e in rt._preplanned.values())
        assert await _until(lambda: done() and len(rt._preplanned) == 3, 120)
        ready_s = time.monotonic() - t0
        cards = {a.event_id for a in rt.approvals.pending()}
        assert any(e in cards for _s, e in rt._preplanned.values()), "at least one card is waiting with 核准 / 討論"
        # one plan per item: nothing new while nothing changed
        n = len(rt.events())
        rt._preplan_at = None
        rt._preplan_orders()
        assert len(rt.events()) == n
        # a supervisor's own request for an order is adopted, never planned twice
        await p.http.post("/api/command", json={"text": "#Z-999 可能晚交，請重排保交期"})
        risky["#Z-999"] = "eta new"
        rt._preplan_at = None
        rt._preplan_orders()
        assert sum(1 for e in rt.events() if (e.command or "").startswith("#Z-999 ")) == 1
        print(f"pre-plans ready in {ready_s:.1f} s (rule mode)")


def test_preplan_is_off_in_test_runtimes_by_default(tmp_path):
    assert _rt(tmp_path, "normal").preplan_on is False
