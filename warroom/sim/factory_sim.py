from __future__ import annotations

from datetime import datetime

from warroom.clock import SimClock
from warroom.contracts import (FaultSpec, MachineState, MachineStatus, Order, OrderProgress, SafetyAction,
                               Scenario, ScenarioName, SemiFinishedProgress, SensorReading, Shift)


class Factory:
    """Implements contracts.FactorySim. 28 machines from warroom.factory.MACHINES.

    Physics expectations (builder decides details, keep deterministic with `seed`):
    - curing presses hold 128°C ±0.3, 20 kgf/cm², steam ~ 5 kgf/cm²; compound machines 90–100°C.
    - faults (FaultSpec) bend the signals; STEAM_LEAK on CURE-07 makes 6 & 8 (same header H2) dip slightly.
    - apply_safety_action() changes status to STOPPED / holds the cure job.
    """

    def __init__(self, clock: SimClock | None = None, seed: int = 7, scenario: ScenarioName = ScenarioName.NORMAL):
        raise NotImplementedError

    def load_scenario(self, name: ScenarioName) -> Scenario: raise NotImplementedError
    def inject_fault(self, fault: FaultSpec) -> None: raise NotImplementedError
    def clear_faults(self, machine_id: str | None = None) -> None: raise NotImplementedError
    def step(self, dt_s: float = 1.0) -> list[SensorReading]: raise NotImplementedError
    def machines(self) -> list[MachineState]: raise NotImplementedError
    def machine(self, machine_id: str) -> MachineState: raise NotImplementedError
    def history(self, machine_id: str, minutes: float) -> list[SensorReading]: raise NotImplementedError
    def orders(self) -> list[Order]: raise NotImplementedError
    def shifts(self) -> list[Shift]: raise NotImplementedError
    def order_progress(self) -> list[OrderProgress]: raise NotImplementedError
    def semi_finished(self) -> SemiFinishedProgress: raise NotImplementedError
    def apply_safety_action(self, action: SafetyAction) -> None: raise NotImplementedError
    def set_machine_status(self, machine_id: str, status: MachineStatus, note: str | None = None) -> None:
        raise NotImplementedError
    def fault_started_at(self, machine_id: str) -> datetime | None: raise NotImplementedError
