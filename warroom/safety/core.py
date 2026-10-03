"""Safety core (red path, page 5): deterministic PLC-style rules. The LLM cannot call or
override it, and it does not wait for the LLM. No imports from agent / tools / api / LLM clients.

Rules (page 2, 3, 5, 11):
  SR-COMPOUND-120  煉膠 (unvulcanised) compound machine temp >= 120 °C  -> STOP_MACHINE
  SR-CURATIVE-110  已加硫 (curative added) compound machine temp >= 110 °C -> STOP_MACHINE (scorch guard)
  SR-CURE-LOW-120  curing press with a cure job below 120 °C           -> CURE_HOLD
  SR-CURE-HIGH-130 curing press with a cure job at/above 130 °C        -> ALARM_ONLY (outside 120–130)

PLC-style input filtering: a rule trips only after `confirm_samples` consecutive violating
samples (default 2), so one glitch sample (a spike) cannot stop a batch; physically impossible
or missing values never count. Each rule fires once per machine episode and re-arms after the
condition has been clear for `rearm_samples` consecutive samples.
"""
from __future__ import annotations

import math
import time

from warroom.contracts import (
    Channel,
    CompoundStage,
    MachineState,
    MachineStatus,
    SafetyAction,
    SafetyActionKind,
    SafetyRule,
    SensorReading,
)
from warroom.factory import (
    COMPOUND_MACHINES,
    COMPOUND_STOP_C,
    CURATIVE_STOP_C,
    CURE_LOW_STOP_C,
    CURE_PRESS_IDS,
    CURE_TEMP_MAX_C,
    MACHINE_BY_ID,
    MachineKind,
)

PHYSICAL_TEMP_RANGE = (-20.0, 300.0)
_STOPPING = {SafetyActionKind.STOP_MACHINE, SafetyActionKind.HOLD_BATCH, SafetyActionKind.CURE_HOLD}


def default_rules() -> list[SafetyRule]:
    """From warroom.factory: COMPOUND_STOP_C (≥120 on unvulcanized compound machines),
    CURATIVE_STOP_C (≥110 when compound_stage == CURATIVE_ADDED), CURE_LOW_STOP_C
    (<120 on a press while cure_job is set -> CURE_HOLD), CURE_TEMP_MAX_C (≥130 -> alarm)."""
    return [
        SafetyRule(id="SR-COMPOUND-120", description="煉膠（未加硫）≥120°C 停機", machine_ids=list(COMPOUND_MACHINES),
                   channel=Channel.TEMP, op=">=", threshold=COMPOUND_STOP_C,
                   compound_stage=CompoundStage.UNVULCANIZED, action=SafetyActionKind.STOP_MACHINE),
        SafetyRule(id="SR-CURATIVE-110", description="已加硫 ≥110°C 停機（防焦燒）", machine_ids=list(COMPOUND_MACHINES),
                   channel=Channel.TEMP, op=">=", threshold=CURATIVE_STOP_C,
                   compound_stage=CompoundStage.CURATIVE_ADDED, action=SafetyActionKind.STOP_MACHINE),
        SafetyRule(id="SR-CURE-LOW-120", description="硫化中 <120°C 扣模待 QC、禁止新入模", machine_ids=list(CURE_PRESS_IDS),
                   channel=Channel.TEMP, op="<", threshold=CURE_LOW_STOP_C, only_while_curing=True,
                   action=SafetyActionKind.CURE_HOLD),
        SafetyRule(id="SR-CURE-HIGH-130", description="硫化中 ≥130°C 警示（超出 120–130°C）",
                   machine_ids=list(CURE_PRESS_IDS), channel=Channel.TEMP, op=">=", threshold=CURE_TEMP_MAX_C,
                   only_while_curing=True, action=SafetyActionKind.ALARM_ONLY),
    ]


def _label(mid: str) -> str:
    d = MACHINE_BY_ID.get(mid)
    if d is None:
        return mid
    if d.kind is MachineKind.CURING_PRESS:
        return f"{int(d.short_name)} 號機"
    return f"{d.line.value} 線{d.name}" if d.line.value in "ABCD" else f"{d.name}"


class RuleSafetyCore:
    """Implements contracts.SafetyCore. check() is pure arithmetic over a pre-built index:
    tens of microseconds for 28 readings (target < 0.2 s)."""

    def __init__(self, rules: list[SafetyRule] | None = None, clock=None, confirm_samples: int = 2,
                 rearm_samples: int = 30):
        self.rules: list[SafetyRule] = list(rules) if rules is not None else default_rules()
        self.clock = clock
        self.confirm_samples = max(1, confirm_samples)
        self.rearm_samples = max(1, rearm_samples)
        self._by_machine: dict[str, list[SafetyRule]] = {}
        for rule in self.rules:
            for mid in rule.machine_ids:
                self._by_machine.setdefault(mid, []).append(rule)
        self._count: dict[tuple[str, str], int] = {}
        self._clear: dict[tuple[str, str], int] = {}
        self._latched: set[tuple[str, str]] = set()
        self.actions: list[SafetyAction] = []
        self.last_check_ms: float = 0.0

    def _applies(self, rule: SafetyRule, st: MachineState | None) -> bool:
        if rule.compound_stage is not None and (st is None or st.compound_stage is not rule.compound_stage):
            return False
        return not (rule.only_while_curing and (st is None or st.cure_job is None))

    def check(self, readings: list[SensorReading], states: dict[str, MachineState]) -> list[SafetyAction]:
        t0 = time.perf_counter()
        out: list[SafetyAction] = []
        for r in readings:
            rules = self._by_machine.get(r.machine_id)
            if not rules:
                continue
            st = states.get(r.machine_id)
            for rule in rules:
                key = (rule.id, r.machine_id)
                v = getattr(r, rule.channel.value)
                valid = v is not None and math.isfinite(v) and PHYSICAL_TEMP_RANGE[0] <= v <= PHYSICAL_TEMP_RANGE[1]
                violated = valid and self._applies(rule, st) and (
                    v >= rule.threshold if rule.op == ">=" else v < rule.threshold)
                if violated:
                    self._clear[key] = 0
                    n = self._count[key] = self._count.get(key, 0) + 1
                    if n >= self.confirm_samples and key not in self._latched:
                        self._latched.add(key)
                        already = st is not None and st.status in (MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE)
                        if rule.action in _STOPPING and already:
                            continue      # plant already stopped / taken out: nothing more to do
                        out.append(self._action(rule, r, v, t0))
                else:
                    self._count[key] = 0
                    if key in self._latched and (valid or v is None):
                        c = self._clear[key] = self._clear.get(key, 0) + 1
                        if c >= self.rearm_samples:
                            self._latched.discard(key)
                            self._clear[key] = 0
        self.last_check_ms = (time.perf_counter() - t0) * 1000.0
        self.actions.extend(out)
        return out

    def _action(self, rule: SafetyRule, r: SensorReading, v: float, t0: float) -> SafetyAction:
        lab = _label(r.machine_id)
        if rule.action is SafetyActionKind.CURE_HOLD:
            shown = math.floor(v * 10.0) / 10.0      # 119.97 must not print as "120.0 < 120"
            msg = f"{lab} {shown:.1f}°C < {rule.threshold:.0f}°C：安全核心停用並扣模"
        elif rule.action is SafetyActionKind.ALARM_ONLY:
            msg = f"{lab} {v:.1f}°C ≥ {rule.threshold:.0f}°C：超出硫化溫度 120–130°C，警示"
        elif rule.compound_stage is CompoundStage.CURATIVE_ADDED:
            msg = f"{lab} 膠溫 {v:.1f}°C ≥ {rule.threshold:.0f}°C（已加硫，防焦燒）：安全核心停機並扣批"
        else:
            msg = f"{lab} 膠溫 {v:.1f}°C ≥ {rule.threshold:.0f}°C（未加硫上限）：安全核心停機並扣批"
        return SafetyAction(id=f"SA-{r.ts:%Y%m%d-%H%M%S}-{r.machine_id}-{rule.id}", ts=r.ts, machine_id=r.machine_id,
                            rule_id=rule.id, action=rule.action, value=round(v, 2), threshold=rule.threshold,
                            message=msg, latency_ms=round((time.perf_counter() - t0) * 1000.0, 4))

    def reset(self, machine_id: str | None = None) -> None:
        """Re-arm rules (e.g. after maintenance put the machine back)."""
        keys = [k for k in self._latched if machine_id is None or k[1] == machine_id]
        for k in keys:
            self._latched.discard(k)
            self._count.pop(k, None)
            self._clear.pop(k, None)
