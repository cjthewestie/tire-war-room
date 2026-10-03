"""Degraded-mode stand-ins for sibling modules, used by Runtime only when a real module is
missing or fails to construct (and by tests that need a deterministic plant).

Each class satisfies the matching contracts Protocol with the simplest honest behaviour.
They are NOT the product: Runtime reports every fallback it used in `Runtime.degraded`,
surfaced at GET /api/health/components. Nothing here talks to a PLC, a model or the network.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from warroom import factory as F
from warroom.clock import SimClock
from warroom.contracts import (
    Anomaly,
    AnomalyKind,
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    AuditEntry,
    AuditKind,
    CapacityReport,
    CapacityRowOut,
    Channel,
    CompoundStage,
    CureJobState,
    DataQuality,
    DecisionKind,
    Event,
    EventState,
    FaultKind,
    FaultSpec,
    MachineState,
    MachineStatus,
    Order,
    OrderProgress,
    Prediction,
    SafetyAction,
    SafetyActionKind,
    SafetyRule,
    Scenario,
    ScenarioName,
    ScenarioStep,
    Schedule,
    ScheduleStatus,
    ScheduleVersion,
    SemiFinishedPart,
    SemiFinishedProgress,
    SensorReading,
    Severity,
    Shift,
    SolverName,
    SopHit,
    TimelineItem,
    TireSize,
    WsType,
)

SIZE_LABEL = {TireSize.S: "小胎", TireSize.M: "中胎", TireSize.L: "大胎"}
DEMO_DAY = datetime(2026, 10, 3, tzinfo=F.TZ)


# =====================================================================================
# Plant
# =====================================================================================
def _nominal(mid: str) -> dict[str, float]:
    m = F.MACHINE_BY_ID[mid]
    if m.line.value == "CURE":
        return {"temp_c": F.CURE_TEMP_NOMINAL_C, "pressure_kgf_cm2": F.CURE_PRESSURE_KGF_CM2,
                "steam_kgf_cm2": 5.0}
    if m.handles_compound:
        return {"temp_c": 95.0, "pressure_kgf_cm2": 5.0, "water_flow_lpm": 40.0}
    return {"temp_c": 35.0, "pressure_kgf_cm2": 5.0, "water_flow_lpm": 40.0}


def fallback_scenario(name: ScenarioName, start_at: datetime | None = None) -> Scenario:
    if name is ScenarioName.PRESS7_TEMP_DROP:
        start = start_at or DEMO_DAY.replace(hour=2, minute=13, second=30)
        return Scenario(name=name, title="7 號硫化機降溫（p.11）", start_at=start, steps=[
            ScenarioStep(at_offset_s=30, note="02:14:00 CURE-07 128→122°C", fault=FaultSpec(
                id="F-P7-1", machine_id=F.press_id(7), kind=FaultKind.TEMP_RAMP,
                params={"target": 122.0, "rate_c_per_min": 6.0})),
            ScenarioStep(at_offset_s=30 + 16 * 60, note="02:30 持續洩漏 → 119.5°C", fault=FaultSpec(
                id="F-P7-2", machine_id=F.press_id(7), kind=FaultKind.TEMP_RAMP,
                params={"target": 119.5, "rate_c_per_min": 3.0})),
        ], expected=["detect ≤60 s", "H1 rejected at 119.5°C"])
    if name is ScenarioName.OPEN_MILL_OVERHEAT:
        start = start_at or DEMO_DAY.replace(hour=10, minute=0)
        return Scenario(name=name, title="A 線大開煉機溫度過高（p.2）", start_at=start, steps=[
            ScenarioStep(at_offset_s=20, fault=FaultSpec(
                id="F-OM-1", machine_id="A-BIGOPEN", kind=FaultKind.WATER_FLOW_DROP, params={"target": 22.0})),
            ScenarioStep(at_offset_s=20, fault=FaultSpec(
                id="F-OM-2", machine_id="A-BIGOPEN", kind=FaultKind.TEMP_RAMP,
                params={"target": 116.0, "rate_c_per_min": 2.0})),
        ])
    start = start_at or DEMO_DAY.replace(hour=8)
    return Scenario(name=name, title=str(name), start_at=start, steps=[])


class FallbackFactory:
    """Minimal deterministic plant: nominal signals plus TEMP_RAMP/TEMP_STEP/WATER_FLOW_DROP/
    STEAM_PRESSURE_DROP faults. No noise, so tests are exact."""

    def __init__(self, clock: SimClock | None = None, seed: int = 7,
                 scenario: ScenarioName = ScenarioName.NORMAL):
        self.clock = clock or SimClock(start=DEMO_DAY.replace(hour=8), manual=True)
        self._values: dict[str, dict[str, float]] = {}
        self._faults: dict[str, list[tuple[FaultSpec, datetime]]] = {}
        self._hist: dict[str, deque[SensorReading]] = {}
        self._status: dict[str, MachineStatus] = {}
        self._notes: dict[str, str | None] = {}
        self._seq: dict[str, int] = {}
        self._fault_start: dict[str, datetime] = {}
        self._pending_steps: list[ScenarioStep] = []
        self._scenario_t0: datetime | None = None
        self.scenario: Scenario | None = None
        self._reset_plant()
        self.load_scenario(scenario)

    def _reset_plant(self) -> None:
        for mid in F.MACHINE_IDS:
            self._values[mid] = _nominal(mid)
            self._faults[mid] = []
            self._hist[mid] = deque(maxlen=3600)
            self._status[mid] = MachineStatus.RUNNING
            self._notes[mid] = None
            self._seq[mid] = 0
        self._fault_start.clear()

    # --- scenario / faults ---
    def load_scenario(self, name: ScenarioName) -> Scenario:
        sc = fallback_scenario(ScenarioName(name))
        self._reset_plant()
        self.clock.reset(sc.start_at)
        self.scenario = sc
        self._scenario_t0 = sc.start_at
        self._pending_steps = sorted(sc.steps, key=lambda s: s.at_offset_s)
        return sc

    def inject_fault(self, fault: FaultSpec) -> None:
        start = self.clock.now() + timedelta(seconds=fault.start_offset_s)
        self._faults.setdefault(fault.machine_id, []).append((fault, start))
        self._fault_start.setdefault(fault.machine_id, start)

    def clear_faults(self, machine_id: str | None = None) -> None:
        for mid in ([machine_id] if machine_id else list(self._faults)):
            self._faults[mid] = []
            self._fault_start.pop(mid, None)
            self._values[mid] = _nominal(mid)

    def fault_started_at(self, machine_id: str) -> datetime | None:
        return self._fault_start.get(machine_id)

    # --- physics ---
    def step(self, dt_s: float = 1.0) -> list[SensorReading]:
        now = self.clock.advance(dt_s) if getattr(self.clock, "manual", False) else self.clock.now()
        if self._scenario_t0 is not None:
            elapsed = (now - self._scenario_t0).total_seconds()
            while self._pending_steps and self._pending_steps[0].at_offset_s <= elapsed:
                st = self._pending_steps.pop(0)
                if st.fault:
                    f = st.fault.model_copy(update={"start_offset_s": 0.0})
                    self.inject_fault(f)
        out = []
        for mid in F.MACHINE_IDS:
            v = self._values[mid]
            active = [(f, s) for f, s in self._faults[mid] if s <= now and
                      (f.duration_s is None or (now - s).total_seconds() <= f.duration_s)]
            temp_kinds = (FaultKind.TEMP_RAMP, FaultKind.TEMP_STEP)
            newest_temp = next((f for f, _ in reversed(active) if f.kind in temp_kinds), None)
            for fault, start in active:
                if fault.kind in temp_kinds and fault is not newest_temp:
                    continue
                p = fault.params
                if fault.kind is FaultKind.TEMP_RAMP:
                    tgt, rate = p.get("target", v["temp_c"]), p.get("rate_c_per_min", 1.0) * dt_s / 60
                    cur = v["temp_c"]
                    v["temp_c"] = min(tgt, cur + rate) if tgt > cur else max(tgt, cur - rate)
                elif fault.kind is FaultKind.TEMP_STEP:
                    v["temp_c"] = p.get("target", v["temp_c"])
                elif fault.kind is FaultKind.WATER_FLOW_DROP and "water_flow_lpm" in v:
                    v["water_flow_lpm"] = p.get("target", v["water_flow_lpm"] * 0.5)
                elif fault.kind in (FaultKind.STEAM_PRESSURE_DROP, FaultKind.STEAM_LEAK) and "steam_kgf_cm2" in v:
                    v["steam_kgf_cm2"] = p.get("target", 4.0)
            self._seq[mid] += 1
            r = SensorReading(ts=now, machine_id=mid, seq=self._seq[mid], **{k: round(x, 2) for k, x in v.items()})
            self._hist[mid].append(r)
            out.append(r)
        return out

    # --- state ---
    def machine(self, machine_id: str) -> MachineState:
        m = F.MACHINE_BY_ID[machine_id]
        hist = self._hist[machine_id]
        cure = None
        if machine_id == F.press_id(7):
            now = self.clock.now()
            cure = CureJobState(job_id="J-0307", order_id="#A-307", size=TireSize.M, mold_id="M-307",
                                mold_in_at=now - timedelta(hours=10),
                                planned_demold_at=now + timedelta(hours=18),
                                cure_required_h=F.CURE_HOURS[TireSize.M], cure_elapsed_h=10.0)
        return MachineState(
            machine_id=m.id, line=m.line, name=m.name, short_name=m.short_name, status=self._status[m.id],
            last_reading=hist[-1] if hist else None, step_elapsed_min=14.0 if m.handles_compound else 0.0,
            compound_stage=CompoundStage.UNVULCANIZED if m.handles_compound else CompoundStage.NONE,
            cure_job=cure, note=self._notes[m.id])

    def machines(self) -> list[MachineState]:
        return [self.machine(mid) for mid in F.MACHINE_IDS]

    def history(self, machine_id: str, minutes: float) -> list[SensorReading]:
        cutoff = self.clock.now() - timedelta(minutes=minutes)
        return [r for r in self._hist[machine_id] if r.ts >= cutoff]

    def orders(self) -> list[Order]:
        today = self.clock.now().date()
        return [
            Order(id="#A-311", size=TireSize.M, qty=20, done=0, due=today + timedelta(days=3), mold_id="A-311"),
            Order(id="#A-307", size=TireSize.M, qty=40, done=12, due=today + timedelta(days=20)),
            Order(id="#L-07", size=TireSize.L, qty=100, done=8, due=today + timedelta(days=60)),
            Order(id="#S-38", size=TireSize.S, qty=100, done=38, due=today + timedelta(days=10)),
        ]

    def shifts(self) -> list[Shift]:
        d: date = self.clock.now().date()
        out = []
        for i, sd in enumerate(F.OPTIMISED_SHIFTS):
            start = datetime.combine(d, sd.start, tzinfo=F.TZ)
            end = datetime.combine(d + timedelta(days=1 if sd.end <= sd.start else 0), sd.end, tzinfo=F.TZ)
            out.append(Shift(id=f"SH-{i}", team=sd.team, name=sd.name, start=start, end=end,
                             headcount=sd.headcount, required=sd.headcount))
        return out

    def order_progress(self) -> list[OrderProgress]:
        return [OrderProgress(size=s, label=SIZE_LABEL[s], done=d, total=t, remaining_h=h)
                for s, (d, t, h) in F.DEMO_ORDER_PROGRESS.items()]

    def semi_finished(self) -> SemiFinishedProgress:
        mold_in = self.clock.now().replace(hour=17, minute=0, second=0, microsecond=0)
        return SemiFinishedProgress(tire_label="大胎 #07", order_id="#L-07", size=TireSize.L,
                                    planned_mold_in_at=mold_in, parts=[
            SemiFinishedPart(part="A", label=F.LINE_LABELS[F.Line.A], status_text="預熱中",
                             state="in_progress", machine_id="A-BIGOPEN"),
            SemiFinishedPart(part="B", label=F.LINE_LABELS[F.Line.B], status_text="成形中",
                             state="in_progress", machine_id="B-FORM"),
            SemiFinishedPart(part="C", label=F.LINE_LABELS[F.Line.C], status_text="包布中",
                             state="in_progress", machine_id="C-WRAP"),
            SemiFinishedPart(part="D", label=F.LINE_LABELS[F.Line.D], status_text="已完成", state="done",
                             machine_id="D-CUT"),
        ])

    def apply_safety_action(self, action: SafetyAction) -> None:
        if action.action in (SafetyActionKind.STOP_MACHINE, SafetyActionKind.HOLD_BATCH) or action.action is SafetyActionKind.CURE_HOLD:
            self._status[action.machine_id] = MachineStatus.STOPPED
        self._notes[action.machine_id] = action.message

    def set_machine_status(self, machine_id: str, status: MachineStatus, note: str | None = None) -> None:
        self._status[machine_id] = status
        self._notes[machine_id] = note


# =====================================================================================
# Detection, safety
# =====================================================================================
class FallbackDetector:
    """Threshold detector: press with cure job dropping ≥4°C under its first value, compound
    machine above 108°C or water flow below 70 % of its first value. One anomaly per episode."""

    def __init__(self, clock=None, window_s: int = 1800):
        self.clock = clock
        self._base: dict[str, dict[str, float]] = {}
        self._last: dict[str, SensorReading] = {}
        self._hist: dict[str, deque[SensorReading]] = {}
        self._flagged: set[str] = set()
        self._n = 0

    def validate(self, reading: SensorReading) -> DataQuality:
        if reading.temp_c is None:
            return DataQuality.MISSING
        if not -20 <= reading.temp_c <= 300:
            return DataQuality.OUT_OF_RANGE
        prev = self._last.get(reading.machine_id)
        if prev and prev.temp_c is not None and abs(reading.temp_c - prev.temp_c) > 15:
            return DataQuality.JUMP
        return DataQuality.OK

    def ingest(self, readings: list[SensorReading]) -> list[Anomaly]:
        out = []
        for r in readings:
            q = self.validate(r)
            if q is not DataQuality.OK:
                continue
            self._last[r.machine_id] = r
            self._hist.setdefault(r.machine_id, deque(maxlen=1800)).append(r)
            base = self._base.setdefault(r.machine_id, {k: v for k, v in r.model_dump().items()
                                                        if k.endswith(("_c", "_lpm", "_cm2")) and v is not None})
            if r.machine_id in self._flagged:
                continue
            m = F.MACHINE_BY_ID[r.machine_id]
            kind = None
            if m.line.value == "CURE" and r.temp_c is not None and r.temp_c <= base["temp_c"] - 4:
                kind, ch, val, bl = AnomalyKind.TEMP_DROP, Channel.TEMP, r.temp_c, base["temp_c"]
                msg = f"{int(m.short_name)} 號 {bl:.0f} → {val:.0f}°C"
            elif m.handles_compound and r.temp_c is not None and r.temp_c >= 108:
                kind, ch, val, bl = AnomalyKind.OVERHEAT, Channel.TEMP, r.temp_c, base["temp_c"]
                msg = f"{m.name} 膠溫 {val:.0f}°C 且持續上升"
            elif (r.water_flow_lpm is not None and "water_flow_lpm" in base and m.handles_compound
                  and r.water_flow_lpm < 0.7 * base["water_flow_lpm"] and r.temp_c and r.temp_c > base["temp_c"] + 5):
                # page 2: compound temperature rising because cooling water is short -> overheat event
                kind, ch, val, bl = AnomalyKind.OVERHEAT, Channel.TEMP, r.temp_c, base["temp_c"]
                msg = f"膠溫 {val:.0f}°C 且持續上升，水流偏低 {r.water_flow_lpm:.0f} L/min"
            if kind:
                self._flagged.add(r.machine_id)
                self._n += 1
                pred = self.predict(r.machine_id, ch)
                out.append(Anomaly(id=f"AN-{self._n:04d}-{r.machine_id}", ts=r.ts, machine_id=r.machine_id,
                                   kind=kind, channel=ch, value=val, baseline=bl, prediction=pred,
                                   severity=Severity.CRITICAL if kind is AnomalyKind.TEMP_DROP else Severity.WARN,
                                   message=msg))
        return out

    def predict(self, machine_id: str, channel: Channel = Channel.TEMP, horizon_s: float = 1800) -> Prediction:
        h = [getattr(r, channel.value) for r in self._hist.get(machine_id, []) if getattr(r, channel.value) is not None]
        if not h:
            raise KeyError(machine_id)
        tail = h[-60:]
        trend = (tail[-1] - tail[0]) / max(1, len(tail) - 1) * 60
        pred = tail[-1] + trend * horizon_s / 60 * 0.1
        return Prediction(machine_id=machine_id, channel=channel, now_value=tail[-1], predicted_value=round(pred, 1),
                          band_lo=round(pred - 1, 1), band_hi=round(pred + 1, 1), horizon_s=horizon_s,
                          trend_per_min=round(trend, 3), model="fallback-linear")

    def reset(self, machine_id: str | None = None) -> None:
        if machine_id:
            self._flagged.discard(machine_id)
        else:
            self._flagged.clear()
            self._base.clear()
            self._hist.clear()
            self._last.clear()


class FallbackSafetyCore:
    """The three deterministic PLC rules (page 5 / note 7), once per machine episode."""

    def __init__(self, rules: list[SafetyRule] | None = None, clock=None):
        self.rules: list[SafetyRule] = rules or []
        self.clock = clock
        self._fired: set[tuple[str, str]] = set()
        self._n = 0

    def check(self, readings: list[SensorReading], states: dict[str, MachineState]) -> list[SafetyAction]:
        out = []
        for r in readings:
            st = states.get(r.machine_id)
            if st is None or r.temp_c is None:
                continue
            rule = None
            if st.compound_stage is CompoundStage.UNVULCANIZED and r.temp_c >= F.COMPOUND_STOP_C:
                rule = ("compound_stop", SafetyActionKind.STOP_MACHINE, F.COMPOUND_STOP_C, "≥")
            elif st.compound_stage is CompoundStage.CURATIVE_ADDED and r.temp_c >= F.CURATIVE_STOP_C:
                rule = ("curative_stop", SafetyActionKind.STOP_MACHINE, F.CURATIVE_STOP_C, "≥")
            elif st.cure_job is not None and r.temp_c < F.CURE_LOW_STOP_C:
                rule = ("cure_low_hold", SafetyActionKind.CURE_HOLD, F.CURE_LOW_STOP_C, "<")
            if not rule or (r.machine_id, rule[0]) in self._fired:
                continue
            self._fired.add((r.machine_id, rule[0]))
            self._n += 1
            out.append(SafetyAction(id=f"SA-{self._n:04d}", ts=r.ts, machine_id=r.machine_id, rule_id=rule[0],
                                    action=rule[1], value=r.temp_c, threshold=rule[2], latency_ms=0.0,
                                    message=f"{st.name} {r.temp_c}°C {rule[3]} {rule[2]:.0f}°C：安全核心依規則處置"))
        return out


# =====================================================================================
# Audit, approvals, state machine
# =====================================================================================
class MemoryAuditLog:
    """Append-only, sha256 hash-chained, in memory (+ optional JSONL file)."""

    def __init__(self, path: Path | None = None, bus=None, clock=None):
        self.path, self.bus, self.clock = path, bus, clock
        self._entries: list[AuditEntry] = []

    def append(self, kind: AuditKind, actor: str, payload: dict[str, Any], event_id: str | None = None) -> AuditEntry:
        prev = self._entries[-1].hash if self._entries else "0" * 64
        ts = self.clock.now() if self.clock else datetime.now(F.TZ)
        body = {"seq": len(self._entries) + 1, "ts": ts.isoformat(), "kind": str(kind), "actor": actor,
                "event_id": event_id, "payload": payload}
        h = hashlib.sha256((prev + json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)).encode()).hexdigest()
        e = AuditEntry(seq=body["seq"], ts=ts, kind=kind, actor=actor, event_id=event_id,
                       payload=json.loads(json.dumps(payload, default=str)), prev_hash=prev, hash=h)
        self._entries.append(e)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(e.model_dump_json() + "\n")
        if self.bus:
            self.bus.publish(WsType.AUDIT, e)
        return e

    def tail(self, since_seq: int = 0, limit: int = 200) -> list[AuditEntry]:
        return [e for e in self._entries if e.seq > since_seq][:limit]

    def verify_chain(self) -> bool:
        prev = "0" * 64
        for e in self._entries:
            if e.prev_hash != prev:
                return False
            prev = e.hash
        return True


class MemoryApprovalBroker:
    def __init__(self, bus=None, audit=None, clock=None):
        self.bus, self.audit, self.clock = bus, audit, clock
        self._reqs: dict[str, ApprovalRequest] = {}
        self._done: dict[str, asyncio.Event] = {}

    def request(self, req: ApprovalRequest) -> ApprovalRequest:
        self._reqs[req.id] = req
        self._done[req.id] = asyncio.Event()
        if self.audit:
            self.audit.append(AuditKind.APPROVAL_REQUEST, "gate", req.model_dump(mode="json"), req.event_id)
        if self.bus:
            self.bus.publish(WsType.APPROVAL_REQUEST, req)
        return req

    def decide(self, decision: ApprovalDecision) -> ApprovalRequest:
        req = self._reqs[decision.request_id]          # KeyError -> 404
        if req.status is not ApprovalStatus.PENDING:
            raise ValueError(f"approval {req.id} already {req.status.value}")
        if decision.option_id and decision.option_id not in {o.id for o in req.options}:
            raise ValueError(f"unknown option {decision.option_id}")
        status = {DecisionKind.APPROVE: ApprovalStatus.APPROVED, DecisionKind.MODIFY: ApprovalStatus.MODIFIED,
                  DecisionKind.REJECT: ApprovalStatus.REJECTED}[decision.decision]
        req = req.model_copy(update={"status": status, "decision": decision})
        self._reqs[req.id] = req
        if self.audit:
            self.audit.append(AuditKind.APPROVAL_DECISION, decision.decided_by,
                              decision.model_dump(mode="json"), req.event_id)
        if self.bus:
            self.bus.publish(WsType.APPROVAL_RESOLVED, req)
        self._done[req.id].set()
        return req

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self._reqs.values() if r.status is ApprovalStatus.PENDING]

    def all(self) -> list[ApprovalRequest]:
        return list(self._reqs.values())

    def get(self, request_id: str) -> ApprovalRequest:
        return self._reqs[request_id]

    async def wait(self, request_id: str, timeout_s: float | None = None) -> ApprovalRequest:
        await asyncio.wait_for(self._done[request_id].wait(), timeout_s)
        return self._reqs[request_id]


class MinimalStateMachine:
    """Agent module unavailable: open the Event, classify, close it with an honest outcome.
    Never plans or writes anything."""

    def __init__(self, commander=None, tools=None, gate=None, approvals=None, audit=None, sim=None,
                 bus=None, clock=None):
        self.audit, self.bus, self.clock = audit, bus, clock
        self._events: dict[str, Event] = {}

    def _set(self, ev: Event, state: EventState, text: str) -> Event:
        ts = self.clock.now() if self.clock else datetime.now(F.TZ)
        ev = ev.model_copy(update={"state": state, "timeline": [*ev.timeline, TimelineItem(ts=ts, actor="系統", text=text)]})
        if state in (EventState.CLOSED, EventState.SAFE_STOP):
            ev = ev.model_copy(update={"closed_at": ts})
        self._events[ev.id] = ev
        if self.audit:
            self.audit.append(AuditKind.STATE_TRANSITION, "state_machine", {"to": state.value, "text": text}, ev.id)
        if self.bus:
            self.bus.publish(WsType.EVENT, ev)
        return ev

    async def handle(self, anomaly: Anomaly) -> Event:
        ts = self.clock.now() if self.clock else anomaly.ts
        ev = Event(id=f"EV-{ts:%Y%m%d-%H%M%S}-{anomaly.machine_id}", opened_at=ts, machine_id=anomaly.machine_id,
                   anomaly=anomaly, state=EventState.DETECTED,
                   timeline=[TimelineItem(ts=anomaly.ts, actor="事件偵測", text=anomaly.message)])
        self._events[ev.id] = ev
        if self.bus:
            self.bus.publish(WsType.EVENT, ev)
        await asyncio.sleep(0)
        ev = self._set(ev, EventState.CLASSIFY, "分類中（agent 模組未載入，降級模式）")
        await asyncio.sleep(0)
        ev = self._set(ev, EventState.CLOSED, "降級模式：僅記錄異常，未產生計畫")
        ev = ev.model_copy(update={"outcome": "degraded: agent module unavailable"})
        self._events[ev.id] = ev
        return ev

    def events(self) -> list[Event]:
        return list(self._events.values())

    def get(self, event_id: str) -> Event:
        return self._events[event_id]


# =====================================================================================
# Schedule, KB
# =====================================================================================
class MemoryScheduleStore:
    def __init__(self, root: Path | None = None, initial: Schedule | None = None, clock=None):
        now = clock.now() if clock else datetime.now(F.TZ)
        initial = initial or Schedule(id="SCH-0", created_at=now, horizon_start=now,
                                      horizon_end=now + timedelta(days=7), jobs=[], solver=SolverName.HEURISTIC)
        self._v: list[ScheduleVersion] = [ScheduleVersion(version=1, parent_version=None, status=ScheduleStatus.ACTIVE,
                                                          created_at=now, reason="initial", schedule=initial)]

    def _active(self) -> ScheduleVersion:
        return next(v for v in reversed(self._v) if v.status is ScheduleStatus.ACTIVE)

    def current(self) -> ScheduleVersion:
        return self._active()

    def get(self, version: int) -> ScheduleVersion:
        for v in self._v:
            if v.version == version:
                return v
        raise KeyError(version)

    def versions(self) -> list[ScheduleVersion]:
        return list(self._v)

    def propose(self, schedule: Schedule, reason: str, event_id: str | None = None) -> ScheduleVersion:
        v = ScheduleVersion(version=self._v[-1].version + 1, parent_version=self._active().version,
                            status=ScheduleStatus.PROPOSED, created_at=schedule.created_at, reason=reason,
                            event_id=event_id, schedule=schedule)
        self._v.append(v)
        return v

    def _replace(self, v: ScheduleVersion) -> None:
        self._v = [v if x.version == v.version else x for x in self._v]

    def activate(self, version: int) -> ScheduleVersion:
        cur = self._active()
        self._replace(cur.model_copy(update={"status": ScheduleStatus.SUPERSEDED}))
        v = self.get(version).model_copy(update={"status": ScheduleStatus.ACTIVE})
        self._replace(v)
        return v

    def rollback(self, to_version: int | None = None) -> ScheduleVersion:
        cur = self._active()
        target = to_version if to_version is not None else (cur.parent_version or cur.version)
        self.get(target)
        self._replace(cur.model_copy(update={"status": ScheduleStatus.ROLLED_BACK}))
        v = self.get(target).model_copy(update={"status": ScheduleStatus.ACTIVE})
        self._replace(v)
        return v


def fallback_capacity_report() -> CapacityReport:
    """Echoes the page-6 targets; used only when warroom.schedule.capacity_report is missing."""
    return CapacityReport(
        rows=[CapacityRowOut(label=r.label, small=r.small, medium=r.medium, large=r.large) for r in F.CAPACITY_TARGETS],
        ml_100_weeks=F.LEAD_TIME_TARGET_WEEKS["ML_100_each"], small_100_weeks=F.LEAD_TIME_TARGET_WEEKS["small"],
        conditions="FALLBACK (schedule module unavailable): proposal targets echoed, not simulated. "
                   + F.CAPACITY_SIM_CONDITIONS)


class KeywordSopKB:
    """Offline keyword/char-bigram search over warroom/kb/sop/*.md (no embeddings)."""

    def __init__(self, sop_dir: Path | None = None, **_: Any):
        self.sop_dir = Path(sop_dir) if sop_dir else Path(__file__).resolve().parent.parent / "kb" / "sop"
        self._docs: list[tuple[str, str, str, str]] = []

    def ingest(self) -> int:
        self._docs = []
        for p in sorted(self.sop_dir.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            first = text.splitlines()[0].lstrip("# ").strip() if text else p.stem
            m = re.match(r"(SOP-[A-Z]+-\d+)\s*(.*)", first)
            sop_id, title = (m.group(1), m.group(2)) if m else (p.stem, first)
            self._docs.append((sop_id, title, p.name, text))
        return len(self._docs)

    @staticmethod
    def _grams(s: str) -> set[str]:
        s = re.sub(r"\s+", "", s)
        return {s[i:i + 2] for i in range(len(s) - 1)} | set(re.findall(r"[A-Za-z0-9]+", s))

    def search(self, query: str, k: int = 3) -> list[SopHit]:
        if not self._docs:
            self.ingest()
        q = self._grams(query)
        scored = []
        for sop_id, title, src, text in self._docs:
            g = self._grams(title + text)
            score = len(q & g) / max(1, len(q)) + (1.0 if query in title else 0)
            scored.append(SopHit(sop_id=sop_id, title=title, source=src, score=round(score, 4), text=text[:1200]))
        return sorted(scored, key=lambda h: -h.score)[:k]
