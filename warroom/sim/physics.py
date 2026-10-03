"""Plant physics constants and per-machine nominal values for the simulator.

The detector imports the press steam->temperature model from here so its redundant-sensor
check (page 10 資料校核：鄰機比對) uses the same physics as the plant. Values are demo
assumptions, not factory measurements; the proposal fixes only the limits in warroom.factory.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from warroom.contracts import CompoundStage, MachineKind
from warroom.factory import CURE_PRESSURE_KGF_CM2, CURE_TEMP_NOMINAL_C, MACHINES

# ---- curing presses: steam-heated platens ----
STEAM_NOMINAL_KGF_CM2 = 5.0      # steam header gauge pressure at setpoint
STEAM_TEMP_GAIN_C = 4.3          # °C of platen temperature per kgf/cm² of steam (linearised)
STEAM_TAU_S = 5.0                # steam pressure first-order time constant
THERMAL_TAU_S = 15.0             # platen temperature lag behind steam-implied temperature
LEAK_HEADER_DROP_KGF_CM2 = 0.1   # a leak on one press pulls its header down a little (6 & 8 dip)
STEAM_FLOOR_KGF_CM2 = 2.0        # a leaking press bottoms out here (temp ~115 °C)

# ---- compound machines: friction heat removed by cooling water ----
COMPOUND_TAU_S = 300.0           # compound temperature time constant
FLOW_HEAT_GAIN_C_PER_LPM = 0.8   # °C rise per L/min of lost cooling water at steady state
FLOW_RAMP_S = 10.0               # a valve/pump failure takes ~10 s to show in the flow meter

# ---- sensor noise (1 sigma) ----
NOISE = {"temp_c": 0.08, "pressure_kgf_cm2": 0.05, "water_flow_lpm": 0.3, "steam_kgf_cm2": 0.01}
PRESS_TEMP_NOISE = 0.08


def implied_press_temp(steam_kgf_cm2: float, setpoint_c: float = CURE_TEMP_NOMINAL_C) -> float:
    """Platen temperature the steam pressure can hold at steady state."""
    return setpoint_c + STEAM_TEMP_GAIN_C * (steam_kgf_cm2 - STEAM_NOMINAL_KGF_CM2)


def steam_for_temp(temp_c: float, setpoint_c: float = CURE_TEMP_NOMINAL_C) -> float:
    return STEAM_NOMINAL_KGF_CM2 + (temp_c - setpoint_c) / STEAM_TEMP_GAIN_C


def approach(x: float, target: float, dt_s: float, tau_s: float) -> float:
    """Exact first-order step: x moves toward target over dt with time constant tau."""
    if dt_s <= 0:
        return x
    return target + (x - target) * math.exp(-dt_s / tau_s)


@dataclass(frozen=True)
class Nominal:
    temp_c: float
    pressure: float
    flow_lpm: float | None
    cycle_min: float               # batch / step length shown as tile 時間
    stage: CompoundStage = CompoundStage.NONE


# Compound stage per machine (safety rules depend on it, page 2/5). Curatives (sulfur) are
# taken to be added from 保溫儲料 onward on line A, at 成形 on line B and in C's 押出包膠.
# A-BIGOPEN (預熱) holds unvulcanised compound, so 112 °C does not stop it (page 2).
NOMINALS: dict[str, Nominal] = {
    "A-MIX": Nominal(105.0, 6.0, 60.0, 6, CompoundStage.UNVULCANIZED),
    "A-OPEN": Nominal(95.0, 3.0, 40.0, 15, CompoundStage.UNVULCANIZED),
    "A-BIGOPEN": Nominal(100.0, 3.0, 40.0, 30, CompoundStage.UNVULCANIZED),
    "A-SMALLOPEN": Nominal(90.0, 3.0, 35.0, 30, CompoundStage.CURATIVE_ADDED),
    "A-CAL": Nominal(92.0, 15.0, 35.0, 20, CompoundStage.CURATIVE_ADDED),
    "A-COOL": Nominal(35.0, 2.0, 80.0, 20),
    "A-WIND": Nominal(30.0, 1.0, 10.0, 20),
    "B-MIX": Nominal(104.0, 6.0, 60.0, 6, CompoundStage.UNVULCANIZED),
    "B-OPEN": Nominal(94.0, 3.0, 40.0, 15, CompoundStage.UNVULCANIZED),
    "B-FORM": Nominal(92.0, 20.0, 30.0, 25, CompoundStage.CURATIVE_ADDED),
    "B-ASSY": Nominal(40.0, 20.0, 15.0, 25),
    "C-PAYOFF": Nominal(28.0, 1.0, 5.0, 10),
    "C-EXTR": Nominal(88.0, 12.0, 30.0, 10, CompoundStage.CURATIVE_ADDED),
    "C-WIND": Nominal(30.0, 1.0, 5.0, 10),
    "C-WRAP": Nominal(45.0, 5.0, 10.0, 15),
    "D-CUT": Nominal(26.0, 4.0, 5.0, 10),
    "BOND-01": Nominal(60.0, 20.0, 20.0, 40),
}
for _m in MACHINES:
    if _m.kind is MachineKind.CURING_PRESS:
        NOMINALS[_m.id] = Nominal(CURE_TEMP_NOMINAL_C, CURE_PRESSURE_KGF_CM2, None, 0)

COMPOUND_STAGES: dict[str, CompoundStage] = {k: v.stage for k, v in NOMINALS.items()}
