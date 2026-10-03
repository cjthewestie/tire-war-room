from __future__ import annotations

from warroom.contracts import Scheduler, SolveRequest, SolveResult


class CpSatScheduler:
    """OR-Tools CP-SAT. Decision: press + mold-in per job. Hard: M/L only on factory.ML_PRESSES,
    small preferably on SMALL_ONLY_PRESSES, one tire per press, unavailable presses excluded,
    de-mold/mold-in inside staffed windows. Soft: lateness, mold changes, cross-team moves,
    level-loading. Page 6 principles 1–5."""

    def __init__(self, num_workers: int = 8):
        raise NotImplementedError

    def solve(self, req: SolveRequest) -> SolveResult: raise NotImplementedError


class HeuristicScheduler:
    """Backup solver (page 7): greedy earliest-staffed-slot dispatch. Always returns quickly."""

    def solve(self, req: SolveRequest) -> SolveResult: raise NotImplementedError


class FallbackScheduler:
    """CP-SAT first; on TIMEOUT/ERROR/INFEASIBLE use backup and set fell_back=True."""

    def __init__(self, primary: Scheduler | None = None, backup: Scheduler | None = None):
        raise NotImplementedError

    def solve(self, req: SolveRequest) -> SolveResult: raise NotImplementedError
