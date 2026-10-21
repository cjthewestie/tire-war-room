"""Audit N3: the commander returns no steps (or only read/compute steps, no write), the press's
temperature settles at 122 °C (H1 expectation >= 121 met). Does the event close CONFIRMED with
「無需動作」 while the cure time was never extended? Temporary audit test; reuses make_sm and the
NullExpectation pattern of tests/test_agent_review_r2.py."""
from __future__ import annotations

import asyncio

import pytest

from tests.test_agent import make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.agent import MockCommander
from warroom.contracts import AuditKind, EventState, HypothesisStatus, ToolName

stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0     # noqa: E731


class EmptySteps(MockCommander):
    """Keeps H1 and its expectation (CURE-07 temp >= 121), drops every step."""

    def build(self, req):
        p = super().build(req)
        return p.model_copy(update={"steps": []})


class ReadOnlySteps(MockCommander):
    """Keeps only the read/compute steps (S1 cure_formula, S2 query_orders_shifts): nothing to write."""

    def build(self, req):
        p = super().build(req)
        return p.model_copy(update={"steps": [s for s in p.steps if s.tool in (ToolName.CURE_FORMULA,
                                                                             ToolName.QUERY_ORDERS_SHIFTS)]})


@pytest.mark.parametrize("commander", [EmptySteps, ReadOnlySteps])
async def test_n3_stable_temp_without_writes_must_not_close_confirmed(tmp_path, commander):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, commander=commander())
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    h = ev.hypotheses[-1]
    cure_writes = tools.calls_of(ToolName.SET_CURE_TIME)
    formula = [c for c in tools.calls_of(ToolName.CURE_FORMULA)]
    verdicts = [e.payload.get("verdict") for e in audit.tail(0, 0)
                if e.kind is AuditKind.SYSTEM and e.actor == "verifier"]
    trans = [e.payload["to"] for e in audit.tail(0, 0) if e.kind is AuditKind.STATE_TRANSITION]
    print(f"\n[N3 {commander.__name__}] state={ev.state.value} hyp={h.id}:{h.status.value} outcome={ev.outcome!r} "
          f"set_cure_time={len(cure_writes)} cure_formula={len(formula)} approvals={len(approvals.all())} "
          f"verdicts={verdicts} transitions={trans}")
    silently_closed = (ev.state is EventState.CLOSED and h.status is HypothesisStatus.CONFIRMED
                       and not cure_writes)
    assert not silently_closed, \
        (f"CLOSED + {h.id} CONFIRMED + outcome {ev.outcome!r}, CURE-07 at 122°C (setpoint 128) "
         f"but set_cure_time never called")
