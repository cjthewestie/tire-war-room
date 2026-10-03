"""Back-propagation from the cure-press time T (page 6 table). Owner: schedule builder.

Page 6: 「先排出模時刻，再往前推投料與人力」. For one tire entering the press at T:
    開始貼合  T − 40 min / 1 h / 1 h 30   (S / M / L)
    A 線投料  T − 1 h 40 / 2 h 30 / 3 h 30
    胎側膠／鋼絲圈  6 / 8 / 10 pieces
Labor derived per job (each need is 1 person unless noted):
    a_feed     BODY        [a_feed_start, bond_start)
    side_bead  SIDE + BEAD_NYLON  [a_feed_start, bond_start)   (parts for the bonding)
    bonding    BOND        [bond_start, T)
    mold_in    curing team, or BODY night crew outside 06:00–23:00   [T, T+30 min)
    demold     same rule   [demold, demold+30 min)
"""
from __future__ import annotations

from datetime import datetime, timedelta

from warroom import factory as F
from warroom.contracts import LaborNeed, ScheduleJob, Shift, TeamId, TireSize
from warroom.schedule.staffing import OP_MIN, team_for_op


def back_propagate(job_id: str, order_id: str, size: TireSize, press_id: str, mold_in: datetime,
                   cure_h: float | None = None, mold_id: str | None = None,
                   shifts: list[Shift] | None = None, frozen: bool = False) -> ScheduleJob:
    """From T (mold_in) derive bond_start / a_feed_start / pieces via factory.BACKPROP and
    labor needs (de-mold + mold-in 30 min each by curing team or BODY night shift)."""
    if mold_in.tzinfo is None:
        raise ValueError("mold_in must be timezone-aware (factory.TZ)")
    if press_id not in F.CURE_PRESS_IDS:
        raise ValueError(f"unknown press {press_id}")
    bp = F.BACKPROP[size]
    cure = float(cure_h if cure_h is not None else F.CURE_HOURS[size])
    job = ScheduleJob(
        id=job_id, order_id=order_id, size=size, press_id=press_id, mold_id=mold_id,
        mold_in=mold_in, demold=mold_in + timedelta(hours=cure), cure_h=cure,
        bond_start=mold_in - timedelta(minutes=bp["bond_start_min"]),
        a_feed_start=mold_in - timedelta(minutes=bp["a_feed_min"]),
        side_pieces=bp["side_pieces"], bead_pieces=bp["bead_pieces"], frozen=frozen,
    )
    return job.model_copy(update={"labor": labor_for(job, shifts)})


def labor_for(job: ScheduleJob, shifts: list[Shift] | None = None) -> list[LaborNeed]:
    """Labor demand of one job. `shifts` (optional) decides who does the de-mold/mold-in;
    without it the premise hours are used (curing 06–23, BODY night otherwise)."""
    op = timedelta(minutes=OP_MIN)
    needs = [
        LaborNeed(team=TeamId.BODY, task="a_feed", start=job.a_feed_start, end=job.bond_start),
        LaborNeed(team=TeamId.SIDE, task="side_bead", start=job.a_feed_start, end=job.bond_start),
        LaborNeed(team=TeamId.BEAD_NYLON, task="side_bead", start=job.a_feed_start, end=job.bond_start),
        LaborNeed(team=TeamId.BOND, task="bonding", start=job.bond_start, end=job.mold_in),
        LaborNeed(team=team_for_op(job.mold_in, shifts), task="mold_in", start=job.mold_in, end=job.mold_in + op),
        LaborNeed(team=team_for_op(job.demold, shifts), task="demold", start=job.demold, end=job.demold + op),
    ]
    return needs


def labor_conflicts(jobs: list[ScheduleJob], shifts: list[Shift]) -> list[str]:
    """Labor needs that fall outside every shift of their team (e.g. bonding at 03:00 when the
    bonding team only works 08–17). Used as a soft check / warning for back-propagated feed."""
    out = []
    for j in jobs:
        if j.frozen:
            continue
        for n in j.labor:
            if n.task in ("mold_in", "demold"):
                ok = any(s.team is n.team and s.start <= n.start and n.end <= s.end for s in shifts)
            else:
                # prep work may span shifts of the same team; require the start to be staffed
                ok = any(s.team is n.team and s.start <= n.start < s.end for s in shifts)
            if not ok:
                out.append(f"{j.id} {n.task} {n.start:%m/%d %H:%M} {F.TEAM_LABELS[n.team]}無人")
    return out
