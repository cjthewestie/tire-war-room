"""F2 (issue #6): the approval wait is approval_timeout_s of WALL time, but the card showed
now + timeout in FACTORY time. At SIM_SPEED 10 the card said "expired" after 1/10 of the real
window, while approving was still (correctly) accepted. The card must show the real deadline."""
from __future__ import annotations

from datetime import timedelta

from tests.agent_fakes import T0
from warroom.agent import StateMachine
from warroom.clock import SimClock


def _sm(speed: float, timeout_s: float = 600.0) -> StateMachine:
    sm = StateMachine.__new__(StateMachine)          # only the clock and timeout matter here
    sm.clock = SimClock(start=T0, speed=speed, manual=True)
    sm.approval_timeout_s = timeout_s
    return sm


def test_card_expiry_matches_wall_timeout_at_10x():
    # 600 wall seconds at 10x is 6000 factory seconds; the old code showed only 600
    assert _sm(10.0)._approval_expires_at(T0) == T0 + timedelta(seconds=6000)


def test_card_expiry_at_1x_is_unchanged():
    assert _sm(1.0)._approval_expires_at(T0) == T0 + timedelta(seconds=600)


def test_card_expiry_at_60x():
    assert _sm(60.0, timeout_s=120)._approval_expires_at(T0) == T0 + timedelta(seconds=7200)


def test_card_expiry_without_a_clock_uses_the_timeout():
    sm = _sm(1.0)
    sm.clock = None
    assert sm._approval_expires_at(T0) == T0 + timedelta(seconds=600)


def test_joys_case_no_longer_shows_expired():
    """Joy's run (10x): card created at 02:20:25, approved 240 factory s later (24 wall s). The card
    used to say 02:24:25 = already expired; now it says 04:00:25, and 02:28:25 is well inside it."""
    created = T0.replace(hour=2, minute=20, second=25)
    approved_at = created + timedelta(seconds=240)
    assert approved_at < _sm(10.0)._approval_expires_at(created)
