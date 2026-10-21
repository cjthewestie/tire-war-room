"""The team dataset's production plan at one moment: what each press holds and what is queued (issue #20, Q1).

`orders.csv` (SO-001..009, 300 tyres) and `tire_unit.csv` (one row per tyre: press, cure start / end) are the
team's official plan. `plan_state(at)` reads both and answers, for the dataset time `at`:

- the orders, with `done` = tyres already de-molded at `at` (cure_end <= at);
- the tyre curing on every press (cure_start <= at < cure_end), with the hours it has run;
- the next tyres queued on every press (cure_start > at), in cure_start order.

A tyre with an alias in tire_unit.csv (S-033 = #A-311, the EV-2 rush tyre) becomes its own one-tyre order named by
the alias and is taken off the order it belongs to (SO-004: 20 -> 19), so the page-11 story keeps its #A-311.
Pure reading, no simulation: the Factory (warroom/sim/team_replay.py) shifts the dates onto the scenario day.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from warroom.contracts import Order, TireSize
from warroom.data.team_dataset import _ts, load_table
from warroom.data.team_ids import machine_id

SIZE = {"小胎": TireSize.S, "中胎": TireSize.M, "大胎": TireSize.L}
QUEUE_DEPTH = 3          # upcoming orders remembered per press (the solver plans the rest)


@dataclass(frozen=True)
class PlanUnit:
    unit_id: str
    order_id: str            # our order id: the alias ("#A-311") or the dataset order ("SO-004")
    size: TireSize
    press_id: str            # "CURE-07"
    cure_start: datetime
    cure_end: datetime


@dataclass
class PlanState:
    at: datetime
    orders: list[tuple[Order, date]] = field(default_factory=list)       # (order with dataset due, ...)
    running: dict[str, PlanUnit] = field(default_factory=dict)           # press -> tyre curing at `at`
    queues: dict[str, list[str]] = field(default_factory=dict)           # press -> next order ids


def plan_state(at: datetime, root=None) -> PlanState:
    units: list[PlanUnit] = []
    raw = load_table("tire_unit", root)
    for r in raw:
        press = machine_id(r["machine_id"])
        if press is None or not r["cure_start"]:
            continue
        units.append(PlanUnit(r["unit_id"], r["alias"] or r["order_id"], SIZE[r["size"]], press,
                              _ts(r["cure_start"]), _ts(r["cure_end"])))
    by_unit = {r["unit_id"]: r for r in raw}
    st = PlanState(at=at)
    # orders ---------------------------------------------------------------------------------------------
    aliased = {u.order_id: by_unit[u.unit_id] for u in units if u.order_id.startswith("#")}
    taken: dict[str, int] = {}
    for a in aliased.values():
        taken[a["order_id"]] = taken.get(a["order_id"], 0) + 1
    done: dict[str, int] = {}
    for u in units:
        if u.cure_end <= at:
            done[u.order_id] = done.get(u.order_id, 0) + 1
    for r in load_table("orders", root):
        oid = r["order_id"]
        qty = int(r["qty"]) - taken.get(oid, 0)
        o = Order(id=oid, size=SIZE[r["size"]], qty=qty, done=min(qty, done.get(oid, 0)),
                  due=_ts(r["due"]).date(), customer=r["customer"], mold_id=f"MD-{oid}")
        st.orders.append((o, o.due))
    for alias, r in aliased.items():
        base = next(o for o, _ in st.orders if o.id == r["order_id"])
        o = Order(id=alias, size=SIZE[r["size"]], qty=1, done=min(1, done.get(alias, 0)),
                  due=_ts(r["rush_due"] or r["finish"]).date(), customer=base.customer, mold_id=alias, priority=2)
        st.orders.append((o, o.due))
    # presses ----------------------------------------------------------------------------------------------
    for u in sorted(units, key=lambda u: u.cure_start):
        if u.cure_start <= at < u.cure_end:
            st.running[u.press_id] = u
        elif u.cure_start > at:
            q = st.queues.setdefault(u.press_id, [])
            if u.order_id not in q and len(q) < QUEUE_DEPTH:
                q.append(u.order_id)
    return st
