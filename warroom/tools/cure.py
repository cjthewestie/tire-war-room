"""Arrhenius equivalent-cure calculator (page 11). Owner: schedule builder.

Physics, not AI: the number the commander passes to set_cure_time always comes from here.

Model (equivalent cure, ref. US3980743 cited on page 11):
    rate(T) / rate(Tref) = exp(-Ea/R * (1/T - 1/Tref)),  T in kelvin
A tire that needs `ref_cure_h` hours at Tref has `ref_cure_h - elapsed_h` equivalent hours left.
At the new temperature those take `remaining_ref_h / rate_ratio` wall-clock hours.

Assumptions (page 11 marks Ea as assumed; it needs factory calibration):
- Tref = the recipe setpoint, 128 °C for the page-11 medium tire. The hours already done were
  at Tref, and the press stays at `actual_temp_c` for the rest of the cure.
- Ea = 85 kJ/mol, R = 8.314 J/(mol·K).
No tuning was needed: with Tref = 128 °C exactly, page 11 reproduces — ratio 0.679,
9.6 h left at 128 °C -> 14.1 h at 122 °C, total 28.5 h, +4.5 h.
"""
from __future__ import annotations

import math

from warroom import factory as F
from warroom.contracts import CureFormulaArgs, CureFormulaData

KELVIN = 273.15


def rate_ratio(actual_temp_c: float, ref_temp_c: float = F.CURE_TEMP_NOMINAL_C,
               activation_energy_kj_mol: float = F.ACTIVATION_ENERGY_KJ_MOL) -> float:
    """Cure rate at actual_temp_c relative to ref_temp_c (1.0 = same speed, <1 = slower)."""
    ea = activation_energy_kj_mol * 1000.0
    t, tref = actual_temp_c + KELVIN, ref_temp_c + KELVIN
    return math.exp(-ea / F.GAS_CONSTANT_J_MOL_K * (1.0 / t - 1.0 / tref))


def arrhenius_cure(args: CureFormulaArgs) -> CureFormulaData:
    """rate_ratio = exp(-Ea/R * (1/T - 1/Tref)), T in K. remaining_ref_h = ref_cure_h - elapsed_h;
    remaining_actual_h = remaining_ref_h / rate_ratio; total = elapsed + remaining_actual.
    Page 11 check: S, 128°C ref, 122°C actual, 14.4 h elapsed, 24 h, 85 kJ/mol -> total 28.5 h, +4.5 h."""
    ref_cure_h = args.ref_cure_h if args.ref_cure_h is not None else F.CURE_HOURS[args.size]
    if args.elapsed_h < 0:
        raise ValueError("elapsed_h must be >= 0")
    if ref_cure_h <= 0:
        raise ValueError("ref_cure_h must be > 0")
    if args.activation_energy_kj_mol <= 0:
        raise ValueError("activation_energy_kj_mol must be > 0")
    if not -50.0 <= args.actual_temp_c <= 300.0 or not -50.0 <= args.ref_temp_c <= 300.0:
        raise ValueError("temperature out of physical range")
    ratio = rate_ratio(args.actual_temp_c, args.ref_temp_c, args.activation_energy_kj_mol)
    remaining_ref = max(0.0, ref_cure_h - args.elapsed_h)
    remaining_actual = remaining_ref / ratio
    total = args.elapsed_h + remaining_actual
    extra = total - ref_cure_h
    formula = (f"k(T)/k(Tref) = exp(−Ea/R·(1/T − 1/Tref)), Ea={args.activation_energy_kj_mol:g} kJ/mol（假設值）, "
               f"T={args.actual_temp_c:g}°C, Tref={args.ref_temp_c:g}°C → {ratio:.3f}; "
               f"剩餘 {remaining_ref:.1f} h@{args.ref_temp_c:g}°C ÷ {ratio:.3f} = {remaining_actual:.1f} h@"
               f"{args.actual_temp_c:g}°C; 總硫化 {total:.1f} h（{extra:+.1f} h）")
    return CureFormulaData(rate_ratio=round(ratio, 4), remaining_ref_h=round(remaining_ref, 2),
                           remaining_actual_h=round(remaining_actual, 2), total_cure_h=round(total, 2),
                           extra_h=round(extra, 2), formula=formula)
