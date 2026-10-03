"""Fakes for the agent tests: a scripted plant (FakeSim), a tool registry with page-11 data
(FakeRegistry) and an in-memory schedule store. They stand in for the sim / tools / schedule
modules behind the same contracts.Protocols, so the agent is tested without its siblings."""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from warroom.clock import SimClock
from warroom.contracts import (
    CompoundStage,
    CureJobState,
    MachineState,
    MachineStatus,
    Schedule,
    ScheduleStatus,
    ScheduleVersion,
    SensorReading,
    SolverName,
    TireSize,
    ToolCall,
    ToolName,
    ToolResult,
    ToolSpec,
)
from warroom.factory import GAS_CONSTANT_J_MOL_K, MACHINE_BY_ID, MACHINES, TZ

T0 = datetime(2026, 10, 3, 2, 14, 0, tzinfo=TZ)     # page 11 02:14:00


def at(hms: str) -> datetime:
    h, m, s = map(int, hms.split(":"))
    return T0.replace(hour=h, minute=m, second=s)


class FakeSim:
    """Machine readings come from temp_fn(machine_id, now)."""

    def __init__(self, clock: SimClock, temp_fn: Callable[[str, datetime], float | None],
                 compound_stage: dict[str, CompoundStage] | None = None):
        self.clock = clock
        self.temp_fn = temp_fn
        self.status: dict[str, MachineStatus] = {m.id: MachineStatus.RUNNING for m in MACHINES}
        self.notes: dict[str, str | None] = {}
        self.status_log: list[tuple[str, MachineStatus]] = []
        self.compound = compound_stage or {}

    def machine(self, machine_id: str) -> MachineState:
        m = MACHINE_BY_ID[machine_id]
        now = self.clock.now()
        reading = SensorReading(ts=now, machine_id=machine_id, temp_c=self.temp_fn(machine_id, now),
                                water_flow_lpm=40.0)
        job = None
        if machine_id == "CURE-07":
            mold_in = now - timedelta(hours=10)
            job = CureJobState(job_id="J-07", order_id="#B-207", size=TireSize.M, mold_id="M-07",
                               mold_in_at=mold_in, planned_demold_at=mold_in + timedelta(hours=28),
                               setpoint_c=128.0, cure_required_h=28.0, cure_elapsed_h=10.0)
        return MachineState(machine_id=machine_id, line=m.line, name=m.name, short_name=m.short_name,
                            status=self.status[machine_id], last_reading=reading,
                            compound_stage=self.compound.get(machine_id, CompoundStage.NONE), cure_job=job,
                            note=self.notes.get(machine_id))

    def machines(self):
        return [self.machine(m.id) for m in MACHINES]

    def set_machine_status(self, machine_id, status, note=None):
        self.status[machine_id] = status
        self.notes[machine_id] = note
        self.status_log.append((machine_id, status))

    def history(self, machine_id, minutes):
        return [self.machine(machine_id).last_reading]


def arrhenius(args: dict[str, Any]) -> dict[str, Any]:
    ea = args.get("activation_energy_kj_mol", 85.0) * 1000
    t, tref = args["actual_temp_c"] + 273.15, args.get("ref_temp_c", 128.0) + 273.15
    ratio = math.exp(-ea / GAS_CONSTANT_J_MOL_K * (1 / t - 1 / tref))
    ref_h = args.get("ref_cure_h") or {"S": 24, "M": 28, "L": 34}[args["size"]]
    rem_ref = ref_h - args["elapsed_h"]
    rem_act = rem_ref / ratio
    total = args["elapsed_h"] + rem_act
    return {"rate_ratio": round(ratio, 3), "remaining_ref_h": round(rem_ref, 1), "remaining_actual_h": round(rem_act, 1),
            "total_cure_h": round(total, 1), "extra_h": round(total - ref_h, 1), "formula": "arrhenius"}


class FakeStore:
    def __init__(self, clock):
        self.clock = clock
        sched = Schedule(id="base", created_at=T0, horizon_start=T0, horizon_end=T0 + timedelta(days=7), jobs=[],
                         solver=SolverName.CP_SAT)
        self.vs = {1: ScheduleVersion(version=1, parent_version=None, status=ScheduleStatus.ACTIVE, created_at=T0,
                                      reason="seed", schedule=sched)}
        self.active = 1
        self.rollbacks: list[int] = []

    def current(self):
        return self.vs[self.active]

    def get(self, v):
        return self.vs[v]

    def versions(self):
        return list(self.vs.values())

    def propose(self, schedule, reason, event_id=None):
        v = max(self.vs) + 1
        self.vs[v] = ScheduleVersion(version=v, parent_version=self.active, status=ScheduleStatus.PROPOSED,
                                     created_at=self.clock.now(), reason=reason, event_id=event_id, schedule=schedule)
        return self.vs[v]

    def amend_proposed(self, v, schedule=None, reason=None):
        upd = {k: x for k, x in (("schedule", schedule), ("reason", reason)) if x is not None}
        self.vs[v] = self.vs[v].model_copy(update=upd)
        return self.vs[v]

    def activate(self, v):
        self.active = v
        return self.vs[v]

    def rollback(self, to_version=None):
        self.rollbacks.append(to_version)
        self.active = to_version or 1
        return self.vs[self.active]


class FakeRegistry:
    """Page-11 tool data. fail: tool -> number of calls that should fail (ok=False)."""

    def __init__(self, sim: FakeSim, store: FakeStore | None = None, fail: dict[ToolName, int] | None = None,
                 feasible: bool = True, fell_back: bool = False):
        self.sim, self.store = sim, store
        self.fail = dict(fail or {})
        self.feasible, self.fell_back = feasible, fell_back
        self.calls: list[ToolCall] = []

    def specs(self, names=None) -> list[ToolSpec]:
        return []

    def openai_tools(self, names):
        return []

    def call(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        if self.fail.get(call.tool, 0) > 0:
            self.fail[call.tool] -= 1
            return ToolResult(call_id=call.id, tool=call.tool, ok=False, error="simulated failure", attempt=call.attempt)
        data = getattr(self, f"_{call.tool.value}")(call.args)
        return ToolResult(call_id=call.id, tool=call.tool, ok=True, data=data, attempt=call.attempt)

    def calls_of(self, tool: ToolName) -> list[ToolCall]:
        return [c for c in self.calls if c.tool is tool]

    # ---- tools
    def _read_sensors(self, a):
        summ = {m: {"temp_c_now": self.sim.machine(m).last_reading.temp_c} for m in a["machine_ids"]}
        return {"readings": {m: [] for m in a["machine_ids"]}, "summary": summ}

    def _predict_anomaly(self, a):
        return {"predicted_value": 122.0, "band_lo": 121.0, "band_hi": 123.0}

    def _cure_formula(self, a):
        return arrhenius(a)

    def _sop_search(self, a):
        title = "硫化中斷處置" if "中斷" in a["query"] else ("開煉機溫度過高" if "開煉" in a["query"] else "硫化機溫度異常")
        return {"hits": [{"sop_id": "SOP-CP-03", "title": title, "source": "x.md", "score": 0.9, "text": "..."}]}

    def _query_orders_shifts(self, a):
        today = self.sim.clock.now().date()
        return {"orders": [{"id": "#A-311", "size": "M", "qty": 1, "done": 0,
                            "due": (today + timedelta(days=3)).isoformat()}],
                "shifts": [], "gaps": ["10/03 夜班缺 1 人"],
                "next_jobs": [{"id": "J-311", "order_id": "#A-311", "size": "M", "press_id": "CURE-07"}]}

    def _solve_schedule(self, a):
        now = self.sim.clock.now()
        transfer = {"kind": "transfer", "shift_date": now.date().isoformat(), "shift_name": "夜班", "to_team": "curing",
                    "from_team": "side", "from_label": "2 線", "people": 1, "label": "從 2 線調 1 人"}
        overtime = {"kind": "overtime", "shift_date": now.date().isoformat(), "shift_name": "夜班", "to_team": "curing",
                    "people": 1, "label": "夜班加班"}
        sched = Schedule(id=f"s-{len(self.calls)}", created_at=now, horizon_start=now,
                         horizon_end=now + timedelta(days=7), jobs=[], solver=SolverName.CP_SAT,
                         staffing_changes=[transfer])
        version = self.store.propose(sched, "solve").version if self.store else 2
        return {"status": "optimal" if self.feasible else "infeasible", "solver": "cp-sat", "solve_ms": 12.0,
                "schedule": sched.model_dump(mode="json"), "feasible": self.feasible, "deliveries_met": self.feasible,
                "late_orders": [], "fell_back": self.fell_back,
                "moves": [{"job_id": "J-311", "order_id": "#A-311", "from_press": "CURE-07", "to_press": "CURE-09",
                           "from_mold_in": now.isoformat(), "to_mold_in": now.isoformat(), "late_h": 0}],
                "staffing_options": [transfer, overtime], "gaps": ["10/03 夜班缺 1 人"], "schedule_version": version}

    def _confirm_schedule(self, a):
        if self.store:
            self.store.activate(a["schedule_version"])
        return {"version": a["schedule_version"], "status": "active"}

    def _set_cure_time(self, a):
        return {"press_id": a["press_id"], "total_cure_h": a["total_cure_h"]}

    def _notify_teams(self, a):
        return {"delivered": a["teams"], "kind": a["kind"]}
