from __future__ import annotations

from warroom.contracts import CapacityReport


def capacity_report() -> CapacityReport:
    """Simulate weekly per-press output for 現況 / 只延長班表 / AI 排出模時刻 under page-6
    conditions and return rows matching factory.CAPACITY_TARGETS (±0.1) plus lead times
    13.3→9.3 weeks (M&L 100 each) and 2.4→2.0 (small)."""
    raise NotImplementedError
