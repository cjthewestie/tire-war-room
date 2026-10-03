"""Safety core (red path, page 5): deterministic PLC-style rules. The LLM cannot call or
override it, and it does not wait for the LLM. No imports from agent / tools / api / LLM clients.

Rules (page 2, 3, 5, 11):
  SR-COMPOUND-120  煉膠 (unvulcanised) compound machine temp >= 120 °C  -> STOP_MACHINE
  SR-CURATIVE-110  已加硫 (curative added) compound machine temp >= 110 °C -> STOP_MACHINE (scorch guard)
  SR-CURE-LOW-120  curing press with a cure job below 120 °C           -> CURE_HOLD
  SR-CURE-HIGH-130 curing press with a cure job at/above 130 °C        -> ALARM_ONLY (outside 120–130)

PLC-style input filtering, chosen so the red path stays inside page 9's < 0.2 s (reading -> action):
  * A violating sample that is physically plausible -- it follows a valid sample of the same machine
    taken at most `history_s` earlier, at a rate of change <= `max_rate_c_per_s` (5 °C/s; a mill or a
    press cannot move faster) -- trips on that sample. Reading -> action is then the rule loop's own
    time, microseconds.
  * A violating sample that jumps (a spike, or the first sample with no history) needs `confirm_samples`
    (default 2) violating samples in a row. At the plant's 1 Hz sampling that confirm costs one sample
    period, so such a stop lands about 1 s after the first violating reading -- stated here and
    reported honestly in SafetyAction.latency_ms, not hidden.
  * Missing, non-finite or physically impossible values are SKIPPED: they neither confirm a violation
    nor reset the confirm counter nor count towards re-arming. Only a valid, non-violating sample
    resets the counter. An intermittent sensor on a mill that is really over the line still trips.
  * Fail-safe on data loss: if a machine's last valid value was at or near a stopping rule's limit
    (within `near_margin_c`) and no valid value has arrived for `data_loss_s` seconds, the core raises
    ALARM_ONLY (rule SR-DATA-LOSS, warning light) once per loss episode.

SafetyAction.latency_ms = (decision time) - (timestamp of the FIRST violating reading of the episode),
where decision time = the triggering reading's ts + the rule loop's elapsed time (contracts.py:
"reading ts -> action decided"). Each rule fires once per machine episode and re-arms after the
condition has been clear for `rearm_samples` consecutive valid samples.

This class is transport-free; warroom/safety/plc.py runs it as a separate process (PLC stand-in) so it
does not share a process with the app, the agent or the LLM.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta

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
                 rearm_samples: int = 30, max_rate_c_per_s: float = 5.0, history_s: float = 5.0,
                 data_loss_s: float = 5.0, near_margin_c: float = 5.0):
        self.rules: list[SafetyRule] = list(rules) if rules is not None else default_rules()
        self.clock = clock
        self.confirm_samples = max(1, confirm_samples)
        self.rearm_samples = max(1, rearm_samples)
        self.max_rate_c_per_s = max_rate_c_per_s
        self.history_s = history_s
        self.data_loss_s = data_loss_s
        self.near_margin_c = near_margin_c
        self._by_machine: dict[str, list[SafetyRule]] = {}
        for rule in self.rules:
            for mid in rule.machine_ids:
                self._by_machine.setdefault(mid, []).append(rule)
        self._count: dict[tuple[str, str], int] = {}
        self._first_ts: dict[tuple[str, str], datetime] = {}
        self._clear: dict[tuple[str, str], int] = {}
        self._latched: set[tuple[str, str]] = set()
        self._last_valid: dict[tuple[str, str], tuple[datetime, float]] = {}   # (machine, channel) -> (ts, v)
        self._loss_alarmed: set[str] = set()
        self.actions: list[SafetyAction] = []
        self.last_check_ms: float = 0.0

    def _applies(self, rule: SafetyRule, st: MachineState | None) -> bool:
        if rule.compound_stage is not None and (st is None or st.compound_stage is not rule.compound_stage):
            return False
        return not (rule.only_while_curing and (st is None or st.cure_job is None))

    @staticmethod
    def _valid(v) -> bool:
        return v is not None and math.isfinite(v) and PHYSICAL_TEMP_RANGE[0] <= v <= PHYSICAL_TEMP_RANGE[1]

    def _plausible(self, prev: tuple[datetime, float] | None, ts: datetime, v: float) -> bool:
        if prev is None:
            return False
        dt = (ts - prev[0]).total_seconds()
        if dt <= 0 or dt > self.history_s:
            return False
        return abs(v - prev[1]) / dt <= self.max_rate_c_per_s

    def _near(self, rule: SafetyRule, v: float) -> bool:
        return v >= rule.threshold - self.near_margin_c if rule.op == ">=" else v < rule.threshold + self.near_margin_c

    def check(self, readings: list[SensorReading], states: dict[str, MachineState]) -> list[SafetyAction]:
        t0 = time.perf_counter()
        out: list[SafetyAction] = []
        for r in readings:
            rules = self._by_machine.get(r.machine_id)
            if not rules:
                continue
            st = states.get(r.machine_id)
            stopped = st is not None and st.status in (MachineStatus.STOPPED, MachineStatus.OUT_OF_SERVICE)
            for channel in {rule.channel for rule in rules}:
                ck = (r.machine_id, channel.value)
                v = getattr(r, channel.value)
                prev = self._last_valid.get(ck)
                if not self._valid(v):
                    # skipped: no confirm, no reset, no re-arm. Fail-safe if the last good value was near a limit.
                    if (prev is not None and r.machine_id not in self._loss_alarmed and not stopped
                            and (r.ts - prev[0]).total_seconds() >= self.data_loss_s):
                        near = [ru for ru in rules if ru.channel is channel and ru.action in _STOPPING
                                and self._applies(ru, st) and self._near(ru, prev[1])
                                and (ru.id, r.machine_id) not in self._latched]
                        if near:
                            self._loss_alarmed.add(r.machine_id)
                            out.append(self._loss_action(near[0], r, prev, t0))
                    continue
                self._loss_alarmed.discard(r.machine_id)
                plausible = self._plausible(prev, r.ts, v)
                self._last_valid[ck] = (r.ts, v)
                for rule in rules:
                    if rule.channel is not channel:
                        continue
                    key = (rule.id, r.machine_id)
                    violated = self._applies(rule, st) and (
                        v >= rule.threshold if rule.op == ">=" else v < rule.threshold)
                    if violated:
                        self._clear[key] = 0
                        n = self._count[key] = self._count.get(key, 0) + 1
                        if n == 1:
                            self._first_ts[key] = r.ts
                        needed = 1 if plausible else self.confirm_samples
                        if n >= needed and key not in self._latched:
                            self._latched.add(key)
                            if rule.action in _STOPPING and stopped:
                                continue      # plant already stopped / taken out: nothing more to do
                            out.append(self._action(rule, r, v, t0, self._first_ts.get(key, r.ts)))
                    else:
                        self._count[key] = 0
                        if key in self._latched:
                            c = self._clear[key] = self._clear.get(key, 0) + 1
                            if c >= self.rearm_samples:
                                self._latched.discard(key)
                                self._clear[key] = 0
        self.last_check_ms = (time.perf_counter() - t0) * 1000.0
        self.actions.extend(out)
        return out

    @staticmethod
    def _latency_ms(r: SensorReading, first_ts: datetime, t0: float) -> float:
        """decision time (trigger reading ts + loop time) - ts of the first violating reading."""
        return round((r.ts - first_ts).total_seconds() * 1000.0 + (time.perf_counter() - t0) * 1000.0, 4)

    def _loss_action(self, rule: SafetyRule, r: SensorReading, prev: tuple[datetime, float], t0: float) -> SafetyAction:
        gap = (r.ts - prev[0]).total_seconds()
        msg = (f"{_label(r.machine_id)} 溫度訊號中斷 {gap:.0f} 秒（最後 {prev[1]:.1f}°C，接近 {rule.threshold:.0f}°C 限值）："
               f"安全核心警示，請現場確認")
        return SafetyAction(id=f"SA-{r.ts:%Y%m%d-%H%M%S}-{r.machine_id}-SR-DATA-LOSS", ts=r.ts,
                            machine_id=r.machine_id, rule_id="SR-DATA-LOSS", action=SafetyActionKind.ALARM_ONLY,
                            value=round(prev[1], 2), threshold=rule.threshold, message=msg,
                            latency_ms=self._latency_ms(r, prev[0] + timedelta(seconds=self.data_loss_s), t0))

    def _action(self, rule: SafetyRule, r: SensorReading, v: float, t0: float, first_ts: datetime) -> SafetyAction:
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
                            message=msg, latency_ms=self._latency_ms(r, first_ts, t0))

    def reset(self, machine_id: str | None = None) -> None:
        """Re-arm rules (e.g. after maintenance put the machine back)."""
        keys = [k for k in self._latched if machine_id is None or k[1] == machine_id]
        for k in keys:
            self._latched.discard(k)
            self._count.pop(k, None)
            self._clear.pop(k, None)
            self._first_ts.pop(k, None)
        if machine_id is None:
            self._loss_alarmed.clear()
        else:
            self._loss_alarmed.discard(machine_id)
