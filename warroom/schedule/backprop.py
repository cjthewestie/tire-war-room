from __future__ import annotations

from datetime import datetime

from warroom.contracts import LaborNeed, ScheduleJob, TireSize


def back_propagate(job_id: str, order_id: str, size: TireSize, press_id: str, mold_in: datetime,
                   cure_h: float | None = None, mold_id: str | None = None) -> ScheduleJob:
    """From T (mold_in) derive bond_start / a_feed_start / pieces via factory.BACKPROP and
    labor needs (de-mold + mold-in 30 min each by curing team or BODY night shift)."""
    raise NotImplementedError


def labor_for(job: ScheduleJob) -> list[LaborNeed]:
    raise NotImplementedError
