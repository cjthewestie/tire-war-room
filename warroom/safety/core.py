from __future__ import annotations

from warroom.contracts import MachineState, SafetyAction, SafetyRule, SensorReading


def default_rules() -> list[SafetyRule]:
    """From warroom.factory: COMPOUND_STOP_C (≥120 on unvulcanized compound machines),
    CURATIVE_STOP_C (≥110 when compound_stage == CURATIVE_ADDED), CURE_LOW_STOP_C
    (<120 on a press while cure_job is set -> CURE_HOLD)."""
    raise NotImplementedError


class RuleSafetyCore:
    """Implements contracts.SafetyCore. check() must take < 0.2 s for 28 readings (target ≪ 1 ms).
    Emits each action once per machine episode (no repeated stops every second)."""

    def __init__(self, rules: list[SafetyRule] | None = None, clock=None):
        raise NotImplementedError

    def check(self, readings: list[SensorReading], states: dict[str, MachineState]) -> list[SafetyAction]:
        raise NotImplementedError
