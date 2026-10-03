"""Staffed windows and staffing gaps. Owner: schedule builder.

Who may de-mold / mold-in (page 6 optimised premise, SOP-HR-01):
- 硫化機組 (CURING), any of its shifts (錯班 06:00–15:00, 14:00–23:00);
- 胎體／胎面膠組 (BODY) night shift 20:00–05:00 「兼做出入模」.
One de-mold + mold-in takes factory.DEMOLD_MOLDIN_MIN (30 min) and must fit inside a window.

Op model: the 30-min change at time t on one press is de-mold [t, t+15) then mold-in
[t+15, t+30); a de-mold with no reload is only the first half. One crew does one half at a time.

Gap rule (SOP-HR-01 §4): night ops need ≥2 people present; 2+ de-molds at the same time
(「同時」, overlapping halves on different presses) need 3. A shift's stated `required` also
counts. Gap label: "10/03 夜班缺 1 人".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from warroom import factory as F
from warroom.contracts import Shift, StaffingChange, TeamId

OP_MIN = F.DEMOLD_MOLDIN_MIN
HALF_MIN = OP_MIN // 2      # de-mold half, then mold-in half
OP_TEAMS = (TeamId.CURING, TeamId.BODY)
NIGHT_MIN_PEOPLE = 2          # SOP-HR-01 §4
NIGHT_MULTI_OP_PEOPLE = 3     # SOP-HR-01 §4: ≥2 de-molds in one night shift
TRANSFER_FROM_TEAM = TeamId.SIDE     # page 11 「從 2 線調 1 人」: line 2 = B 胎側膠
TRANSFER_FROM_LABEL = "2 線"


def is_night(shift: Shift) -> bool:
    return shift.end.date() > shift.start.date() or shift.start.hour >= 18


def is_op_shift(shift: Shift) -> bool:
    """Shift whose crew performs de-mold / mold-in."""
    return shift.team is TeamId.CURING or (shift.team is TeamId.BODY and is_night(shift))


def expand_shift_defs(defs, start: datetime, end: datetime) -> list[Shift]:
    """Instantiate ShiftDefs on every date whose weekday is listed, covering [start, end).
    Sat night (20:00) naturally runs to Sun 05:00 (page 6 sim condition)."""
    out: list[Shift] = []
    d: date = (start - timedelta(days=1)).date()
    while datetime.combine(d, time(0), tzinfo=F.TZ) < end:
        for sd in defs:
            if d.weekday() not in sd.weekdays:
                continue
            s = datetime.combine(d, sd.start, tzinfo=F.TZ)
            e = datetime.combine(d + timedelta(days=1 if sd.end <= sd.start else 0), sd.end, tzinfo=F.TZ)
            if e <= start or s >= end:
                continue
            out.append(Shift(id=f"{sd.team.value}-{d:%m%d}-{sd.name}", team=sd.team, name=sd.name,
                             start=s, end=e, headcount=sd.headcount))
        d += timedelta(days=1)
    return out


def complete_shifts(given: list[Shift], start: datetime, end: datetime, defs=None) -> list[Shift]:
    """Given shifts win; days/teams they do not cover are filled from the optimised premise
    (factory.OPTIMISED_SHIFTS). The sim often passes only today's shifts."""
    defs = defs if defs is not None else F.OPTIMISED_SHIFTS
    keyed = {(s.team, s.start): s for s in given}
    for s in expand_shift_defs(defs, start, end):
        keyed.setdefault((s.team, s.start), s)
    return sorted(keyed.values(), key=lambda s: (s.start, s.team.value))


@dataclass
class Window:
    """A maximal staffed interval for de-mold/mold-in (merged op shifts)."""
    start: datetime
    end: datetime
    shifts: list[Shift] = field(default_factory=list)


def op_windows(shifts: list[Shift], exclude: set[str] | None = None) -> list[Window]:
    exclude = exclude or set()
    iv = sorted((s for s in shifts if is_op_shift(s) and s.id not in exclude), key=lambda s: s.start)
    out: list[Window] = []
    for s in iv:
        if out and s.start <= out[-1].end:
            out[-1].end = max(out[-1].end, s.end)
            out[-1].shifts.append(s)
        else:
            out.append(Window(s.start, s.end, [s]))
    return out


def shift_for_op(t: datetime, shifts: list[Shift]) -> Shift | None:
    """The op shift covering an op starting at t (CURING preferred, then BODY night)."""
    end = t + timedelta(minutes=OP_MIN)
    cands = [s for s in shifts if is_op_shift(s) and s.start <= t and end <= s.end]
    cands.sort(key=lambda s: (s.team is not TeamId.CURING, s.start))
    return cands[0] if cands else None


def team_for_op(t: datetime, shifts: list[Shift] | None = None) -> TeamId:
    if shifts:
        s = shift_for_op(t, shifts)
        if s is not None:
            return s.team
    # premise hours: curing 06:00–23:00, BODY night otherwise
    return TeamId.CURING if 6 <= t.hour < 23 else TeamId.BODY


def is_short(shift: Shift, concurrent_ops: int = 1) -> int:
    """People missing in `shift` if it carries de-mold/mold-in ops, at most `concurrent_ops`
    of them at the same time (0 = no ops: only the stated `required` counts)."""
    need = shift.required or 0
    if concurrent_ops >= 1 and is_night(shift):
        need = max(need, NIGHT_MULTI_OP_PEOPLE if concurrent_ops >= 2 else NIGHT_MIN_PEOPLE)
    return max(0, need - shift.headcount)


def gap_label(shift: Shift, people: int) -> str:
    return f"{shift.start:%m/%d} {shift.name}缺 {people} 人"


def op_halves(kind: str, t: datetime) -> tuple[datetime, datetime]:
    """Crew-busy interval of a de-mold or mold-in whose 30-min change slot starts at t."""
    h = timedelta(minutes=HALF_MIN)
    return (t, t + h) if kind == "demold" else (t + h, t + 2 * h)


def staffing_gaps(ops: list[tuple[str, str, datetime]], shifts: list[Shift]) -> list[tuple[Shift, int, int]]:
    """[(shift, ops_in_shift, people_short)] for every op shift that has ops and is short.
    `ops` = (kind 'demold'|'mold_in', press_id, change-slot start)."""
    by_shift: dict[str, list[tuple[str, str, datetime]]] = {}
    by_id = {s.id: s for s in shifts}
    for kind, press, t in sorted(set(ops), key=lambda x: (x[2], x[1], x[0])):
        s = shift_for_op(t, shifts)
        if s is not None:
            by_shift.setdefault(s.id, []).append((kind, press, t))
    out = []
    for sid, lst in by_shift.items():
        iv = [(press, *op_halves(kind, t)) for kind, press, t in lst]
        peak = max(len({p for p, a, b in iv if a <= a0 < b}) for _, a0, _ in iv)
        short = is_short(by_id[sid], peak)
        if short > 0:
            out.append((by_id[sid], len({(p, t) for _, p, t in lst}), short))
    out.sort(key=lambda x: x[0].start)
    return out


def staffing_options(shift: Shift, people: int, allow_overtime: bool, allow_cross_team: bool) -> list[StaffingChange]:
    """Options for the supervisor, in card order: A 夜班加班 (R2), B 從 2 線調 1 人 (R3)."""
    opts: list[StaffingChange] = []
    if allow_overtime:
        opts.append(StaffingChange(kind="overtime", shift_date=shift.start.date(), shift_name=shift.name,
                                   to_team=shift.team, people=people, label=f"{shift.name}加班"))
    if allow_cross_team:
        opts.append(StaffingChange(kind="transfer", shift_date=shift.start.date(), shift_name=shift.name,
                                   to_team=shift.team, from_team=TRANSFER_FROM_TEAM, from_label=TRANSFER_FROM_LABEL,
                                   people=people, label=f"從 {TRANSFER_FROM_LABEL}調 {people} 人"))
    return opts
