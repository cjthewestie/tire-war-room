"""Issue #36 模擬事件: random plant events for a live demo.

CJ, 2026-10-20: 「我想要有模擬事件」. Off by default. When on, every `every_min` factory minutes one event is drawn
from a weighted list; the supervisor can also fire a chosen one at once. Nothing here moves a machine or wakes
the agent directly: a machine event becomes a FaultSpec for the sim (the same path as POST /api/sim/fault, so the
detector has to see it in the readings), and a plant event becomes a PlantInput (the same path as a scripted
rush order or sick call, so the state machine opens a plant event and every write goes through the Action Gate).
A material event (late delivery, stock-out) only changes the world's deliveries and stock and shows in its feed;
the card for a shortfall is the patrol's (#38 owns material_short).

Its own RNG (Random(seed)) is never the Factory's or the World's stream, so a scripted scenario with the
generator off runs exactly as before, and one seed gives one sequence of events.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from warroom.contracts import FaultKind, FaultSpec, MachineKind, MachineStatus, Order, PlantInput, TeamId, TireSize
from warroom.factory import CURE_PRESS_IDS, LINE_LABELS, MACHINE_BY_ID, TEAM_LABELS

DEFAULT_EVERY_MIN = 30.0
DEFAULT_SEED = 36
EVERY_MIN_RANGE = (5.0, 240.0)
LOG_KEEP = 20


@dataclass(frozen=True)
class EventDef:
    kind: str
    label: str
    weight: int
    source: str          # "fault" (machine, through the detector) | "plant" (state machine plant event)
    #                      | "world" (the world feed and stock only; a shortfall card is #38's patrol)


# Weights: machine trouble is the common case on a curing floor; order, people and material events less so.
CATALOG: tuple[EventDef, ...] = (
    EventDef("steam_low", "硫化機蒸汽壓不足", 3, "fault"),
    EventDef("water_low", "開煉機冷卻水不足", 2, "fault"),
    EventDef("steam_leak", "硫化機蒸汽洩漏", 1, "fault"),
    EventDef("sensor_glitch", "感測器壞資料（不叫醒 AI）", 1, "fault"),
    EventDef("rush_order", "急單插單", 2, "plant"),
    EventDef("staff_sick", "人員請病假", 2, "plant"),
    EventDef("delivery_late", "原料到貨延遲", 1, "world"),
    EventDef("stock_out", "原料缺料（一批驗退）", 1, "world"),
)
CATALOG_BY_KIND: dict[str, EventDef] = {e.kind: e for e in CATALOG}
WATER_MILLS = ("A-OPEN", "A-BIGOPEN", "B-OPEN")
GLITCH_MACHINES = ("A-MIX", "A-CAL", "B-FORM", "C-EXTR", *CURE_PRESS_IDS)
RUSH_CUSTOMERS = ("順風順水工業有限公司", "日日興物流股份有限公司", "五路財神企業社", "台灣勇腳機械有限公司", "金好運運輸商社")


@dataclass(frozen=True)
class WorldInput:
    """A material event: it changes the world's stock or a delivery and shows in the feed, nothing else."""
    kind: str            # delivery_late | stock_out
    material: str
    text: str
    po_id: str | None = None
    late_h: float | None = None


@dataclass
class Fired:
    at: datetime
    kind: str
    label: str
    text: str
    source: str          # "random" | "manual"
    ref: str | None = None


@dataclass
class SimEvents:
    """The generator's settings and its own RNG. The runtime keeps one across scenario loads."""
    enabled: bool = False
    every_min: float = DEFAULT_EVERY_MIN
    seed: int = DEFAULT_SEED
    next_at: datetime | None = None
    fired: list[Fired] = field(default_factory=list)
    n: int = 0
    rng: random.Random = field(default_factory=lambda: random.Random(DEFAULT_SEED))

    def configure(self, enabled: bool | None = None, every_min: float | None = None, seed: int | None = None) -> None:
        if every_min is not None:
            if not EVERY_MIN_RANGE[0] <= every_min <= EVERY_MIN_RANGE[1]:
                raise ValueError(f"every_min must be in {EVERY_MIN_RANGE}")
            self.every_min = float(every_min)
        if seed is not None:
            self.seed = int(seed)
        if seed is not None or (enabled and not self.enabled):
            self.rng = random.Random(self.seed)     # one seed, one sequence from the moment it is switched on
            self.n = 0
        if enabled is not None:
            self.enabled = bool(enabled)
        self.next_at = None

    def due(self, now: datetime) -> bool:
        """True when the next random event is due. The first one comes `every_min` after switching on; a clock
        that moved back (scenario load) restarts the wait."""
        if not self.enabled:
            return False
        if self.next_at is None or self.next_at - now > timedelta(minutes=self.every_min):
            self.next_at = now + timedelta(minutes=self.every_min)
            return False
        if now < self.next_at:
            return False
        self.next_at = now + timedelta(minutes=self.every_min)
        return True

    def pick(self) -> str:
        return self.rng.choices([e.kind for e in CATALOG], weights=[e.weight for e in CATALOG])[0]

    def build(self, kind: str, sim: Any, now: datetime, busy: set[str]) -> FaultSpec | PlantInput | WorldInput:
        """The FaultSpec or PlantInput for one event. `busy`: machines with an open event or an active fault
        (never stacked)."""
        if kind not in CATALOG_BY_KIND:
            raise KeyError(kind)
        rng = self.rng
        self.n += 1
        tag = f"{now:%H%M%S}-{self.n:02d}"
        if kind in ("steam_low", "steam_leak"):
            mid = rng.choice(working_pool(CURE_PRESS_IDS, sim, busy, curing))
            if kind == "steam_low":
                return FaultSpec(id=f"F-SIM-{tag}-{mid}", machine_id=mid, kind=FaultKind.STEAM_PRESSURE_DROP,
                                 params={"target_c": round(rng.uniform(121.5, 123.5), 1)})
            return FaultSpec(id=f"F-SIM-{tag}-{mid}", machine_id=mid, kind=FaultKind.STEAM_LEAK,
                             params={"rate_c_per_min": round(rng.uniform(0.25, 0.4), 2), "floor_kgf_cm2": 2.0,
                                     "header_drop": 0.1})
        if kind == "water_low":
            mid = rng.choice(working_pool(WATER_MILLS, sim, busy, running))
            return FaultSpec(id=f"F-SIM-{tag}-{mid}", machine_id=mid, kind=FaultKind.WATER_FLOW_DROP,
                             params={"target_lpm": round(rng.uniform(15.0, 20.0), 1), "ramp_s": 10.0})
        if kind == "sensor_glitch":
            mid = rng.choice(GLITCH_MACHINES)
            return FaultSpec(id=f"F-SIM-{tag}-{mid}", machine_id=mid, kind=FaultKind.SPIKE,
                             params={"delta": round(rng.uniform(20.0, 30.0), 1)})
        if kind == "rush_order":
            return self._rush(sim, now)
        if kind == "staff_sick":
            return self._sick(sim, now)
        world = getattr(sim, "world", None)
        from warroom.sim.world import MATERIALS
        if kind == "delivery_late":
            moving = world.in_transit() if world is not None else []
            if moving:
                po, key = rng.choice(moving)
                late = round(rng.uniform(4.0, 12.0))
                return WorldInput("delivery_late", key, f"{po} {_material_name(key)} 供應商來電：延遲約 {late} 小時到貨",
                                  po_id=po, late_h=float(late))
        m = rng.choice(MATERIALS)                   # stock_out, or a late delivery with nothing on the way
        return WorldInput("stock_out", m.key, f"{m.name} 一批驗退，庫存歸零（緊急調料）")

    def _rush(self, sim: Any, now: datetime) -> PlantInput:
        rng = self.rng
        existing = {o.id for o in sim.orders()} if hasattr(sim, "orders") else set()
        k = self.n
        oid = f"#R-{100 + k}"
        while oid in existing:
            k += 1
            oid = f"#R-{100 + k}"
        size = rng.choice((TireSize.S, TireSize.M, TireSize.M, TireSize.L))
        qty = rng.randint(2, 6)
        due = now.date() + timedelta(days=rng.randint(2, 4))
        cust = rng.choice(RUSH_CUSTOMERS)
        order = Order(id=oid, size=size, qty=qty, done=0, due=due, customer=cust, mold_id=f"MD-R{100 + k}", priority=3)
        size_zh = {TireSize.S: "小胎", TireSize.M: "中胎", TireSize.L: "大胎"}[size]
        return PlantInput(kind="rush_order", order=order, text=f"急單 {oid} 到：{cust} {size_zh} {qty} 條，交期 {due:%m/%d}")

    def _sick(self, sim: Any, now: datetime) -> PlantInput:
        rng = self.rng
        world = getattr(sim, "world", None)
        cands = []
        for w in getattr(world, "_workers", []) if world is not None else []:
            if w.staff_id and w.team in (TeamId.CURING, TeamId.BODY, TeamId.SIDE, TeamId.BOND) and \
                    (w.staff_id, now.date()) not in getattr(world, "_extra_absent", {}):
                cands.append(w)
        if cands:
            w = rng.choice(cands)
            team, shift, sid, group = w.team, w.sd.name, w.staff_id, w.group or TEAM_LABELS.get(w.team, w.team.value)
        else:                                       # no team-dataset roster: a curing operator on the day shift
            team, shift, sid, group = TeamId.CURING, "早班", None, TEAM_LABELS[TeamId.CURING]
        who = f"{sid}（{group}）" if sid else group
        return PlantInput(kind="absence", staff_id=sid, team=team, shift_name=shift, note="來電請病假",
                          text=f"{who}來電請病假，{TEAM_LABELS.get(team, team.value)}{shift}缺 1 人")

    def log(self, at: datetime, kind: str, text: str, source: str, ref: str | None = None) -> Fired:
        f = Fired(at=at, kind=kind, label=CATALOG_BY_KIND[kind].label, text=text, source=source, ref=ref)
        self.fired.append(f)
        del self.fired[:-LOG_KEEP]
        return f

    def view(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "every_min": self.every_min, "seed": self.seed,
            "next_at": self.next_at.isoformat() if self.enabled and self.next_at else None,
            "fired": [{"at": f.at.isoformat(), "kind": f.kind, "label": f.label, "text": f.text, "source": f.source,
                       "ref": f.ref} for f in reversed(self.fired)],
            "catalog": [{"kind": e.kind, "label": e.label, "weight": e.weight, "source": e.source} for e in CATALOG],
        }


def _material_name(key: str) -> str:
    from warroom.sim.world import MATERIAL_BY_KEY
    m = MATERIAL_BY_KEY.get(key)
    return m.name if m else key


def free_machines(pool: tuple[str, ...] | list[str], busy: set[str]) -> list[str]:
    """The machines of `pool` with no open event and no active fault (never stacked)."""
    return [m for m in pool if m not in busy]


CURE_LEFT_MIN_H = 1.0     # a press picked for a steam fault has at least this much of its cure to go


def curing(sim: Any, machine_id: str) -> bool:
    """The press is curing a tyre now (running, its cure job not about to end). A steam fault on an idle press has no
    cure to extend: the plan's solve is refused, the event safe-stops with no card (3 of 5 demo fires, 10-21)."""
    st = sim.machine(machine_id)
    job = st.cure_job
    return st.status is MachineStatus.RUNNING and job is not None and \
        job.cure_required_h - job.cure_elapsed_h >= CURE_LEFT_MIN_H


def running(sim: Any, machine_id: str) -> bool:
    """The mill is running: a cooling-water fault goes to a working mill, as a steam fault to a press curing a tyre."""
    return sim.machine(machine_id).status is MachineStatus.RUNNING


def working_pool(pool: tuple[str, ...], sim: Any, busy: set[str], working: Any) -> list[str]:
    """Where a machine fault goes so the demo gets its card: a free machine of `pool` that is working (`working`) and
    whose readings the plant produces (not a dataset replay, 一日循環 / team_ev*, which would hide the fault). Falls back
    to any free machine the plant produces, then any free one, then the whole pool."""
    free = free_machines(pool, busy)
    replayed = getattr(sim, "replayed_machines", None)
    hidden = replayed() if callable(replayed) else set()
    live = [m for m in free if m not in hidden]
    return [m for m in live if working(sim, m)] or live or free or list(pool)


def same_kind_pool(machine_id: str, fault_kind: FaultKind | None = None) -> tuple[str, ...]:
    """Machines that can stand in for `machine_id` under the same fault: a cooling-water fault only on the water
    cooled mills, a curing press only on a press of the same class (small-only 1–7 / shared M+L 8–11, so the cure
    and its setpoint stay alike), anything else on a machine of the same kind."""
    d = MACHINE_BY_ID[machine_id]
    if fault_kind is FaultKind.WATER_FLOW_DROP:
        return WATER_MILLS
    if d.kind is MachineKind.CURING_PRESS:
        return tuple(p for p in CURE_PRESS_IDS if MACHINE_BY_ID[p].small_only == d.small_only)
    return tuple(m for m, x in MACHINE_BY_ID.items() if x.kind is d.kind)


def substitute_machine(machine_id: str, busy: set[str], fault_kind: FaultKind | None = None) -> str | None:
    """情境疊加: an idle machine of the same kind for a step whose target is busy, or None. Nearest first; a press
    on a steam header that a busy press shares comes last (page 11 reads 6 and 8 against 7 as the same pipe)."""
    pool = free_machines([m for m in same_kind_pool(machine_id, fault_kind) if m != machine_id], busy)
    if not pool:
        return None
    d = MACHINE_BY_ID[machine_id]
    busy_headers = {MACHINE_BY_ID[m].steam_header for m in busy if m in MACHINE_BY_ID} - {None}
    return min(pool, key=lambda m: (MACHINE_BY_ID[m].steam_header in busy_headers,
                                    abs(MACHINE_BY_ID[m].order - d.order), m))


def machine_label(machine_id: str) -> str:
    """「7 號硫化機」, 「B 線開煉機」, 「貼合油壓機」 (the dashboard's machineLabel)."""
    d = MACHINE_BY_ID[machine_id]
    return f"{int(d.short_name)} 號{d.name}" if d.kind is MachineKind.CURING_PRESS else \
        f"{d.line.value} 線{d.name}" if d.line.value in ("A", "B", "C", "D") else f"{LINE_LABELS[d.line]}{d.name}"


def machine_short(machine_id: str) -> str:
    """「7 號機」 for a press, the full label otherwise (the 「改用 …（7 號機處理中）」 note)."""
    d = MACHINE_BY_ID[machine_id]
    return f"{int(d.short_name)} 號機" if d.kind is MachineKind.CURING_PRESS else machine_label(machine_id)


def fault_text(spec: FaultSpec) -> str:
    """One line for the fired-events log."""
    where = machine_label(spec.machine_id)
    p = spec.params
    return {
        FaultKind.STEAM_PRESSURE_DROP: f"{where}蒸汽壓不足，溫度降向 {p.get('target_c', 0):g}°C",
        FaultKind.STEAM_LEAK: f"{where}蒸汽洩漏，溫度每分鐘降 {p.get('rate_c_per_min', 0):g}°C",
        FaultKind.WATER_FLOW_DROP: f"{where}冷卻水量降到 {p.get('target_lpm', 0):g} L/min",
        FaultKind.SPIKE: f"{where}讀值單點突波 +{p.get('delta', 0):g}°C（壞資料）",
    }.get(spec.kind, f"{where}：{spec.kind.value}")
