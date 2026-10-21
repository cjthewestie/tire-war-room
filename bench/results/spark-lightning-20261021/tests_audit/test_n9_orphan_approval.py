"""Audit N9: an event cancelled (or crashing) while it waits in AWAIT_APPROVAL must not leave its
approval card PENDING. Temporary audit test; reuses tests/test_agent.make_sm."""
from __future__ import annotations

import asyncio

import pytest

from tests.test_agent import make_sm
from tests.test_agent_page11 import page11_anomaly
from warroom.contracts import ApprovalStatus, EventState

stable = lambda m, now: 122.0 if m == "CURE-07" else 128.0     # noqa: E731


async def _until_pending(approvals):
    while not approvals.pending():
        await asyncio.sleep(0.001)


async def test_n9_cancel_in_await_approval_leaves_no_pending_card(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, approval_timeout_s=60.0)
    task = asyncio.create_task(sm.handle(page11_anomaly()))
    await asyncio.wait_for(_until_pending(approvals), 10)
    ev = sm.events()[0]
    assert ev.state is EventState.AWAIT_APPROVAL
    card = approvals.pending()[0]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    print(f"\n[N9-cancel] event={ev.state.value} card={card.id} status={approvals.get(card.id).status.value}")
    assert ev.state is EventState.SAFE_STOP
    assert approvals.get(card.id).status is not ApprovalStatus.PENDING, \
        f"orphan card {card.id} still PENDING after event {ev.id} -> {ev.state.value}"


async def test_n9_exception_in_await_approval_leaves_no_pending_card(tmp_path):
    clock, audit, sim, store, tools, approvals, sm = make_sm(tmp_path, stable, approval_timeout_s=60.0)

    async def broken_wait(request_id, timeout_s=None):
        raise RuntimeError("broker connection lost")
    approvals.wait = broken_wait
    ev = await asyncio.wait_for(sm.handle(page11_anomaly()), 10)
    cards = approvals.all()
    print(f"\n[N9-exc] event={ev.state.value} cards={[(c.id, c.status.value) for c in cards]}")
    assert ev.state is EventState.SAFE_STOP and cards
    assert not approvals.pending(), \
        f"orphan card(s) {[c.id for c in approvals.pending()]} still PENDING after event -> {ev.state.value}"
