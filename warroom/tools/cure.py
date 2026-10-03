from __future__ import annotations

from warroom.contracts import CureFormulaArgs, CureFormulaData


def arrhenius_cure(args: CureFormulaArgs) -> CureFormulaData:
    """rate_ratio = exp(-Ea/R * (1/T - 1/Tref)), T in K. remaining_ref_h = ref_cure_h - elapsed_h;
    remaining_actual_h = remaining_ref_h / rate_ratio; total = elapsed + remaining_actual.
    Page 11 check: M, 128°C ref, 122°C actual, 10 h elapsed, 28 h, 85 kJ/mol -> total 36.5 h, +8.5 h."""
    raise NotImplementedError
