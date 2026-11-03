"""Model cooldown breaker for the commander (research report section 5, proposal 5 / G11).

One local model, a small number of slots and a 60 s timeout: when the model server hangs, every queued event would
wait the full timeout. The breaker counts consecutive *availability* failures of real commander calls (timeout,
connection error, 5xx) and, after ``fails`` of them in a row, opens for ``cooldown_s`` seconds:

  CLOSED     calls go to the model as before.
  OPEN       no call is sent. ``QueuedCommander`` checks this BEFORE taking a ModelQueue slot and gives the rule plan
             at once (``fallback_reason="cooldown"``) or, --strict, raises ``LLMUnavailable`` (SAFE_STOP path).
  HALF_OPEN  the cooldown is over. The next commander call first sends one tiny probe (a 1-token completion with a
             short timeout, never a real event and never only GET /models: a vLLM that hangs usually still answers
             /models). Probe ok -> CLOSED and the call goes on; probe failed -> OPEN for another ``cooldown_s``.

Output problems (invalid JSON, a cut plan) mean the server answered, so they reset the count. Calls refused before
sending (budget, prompt too long) are not counted at all. The breaker also keeps the /models reachability cache the
Runtime health check uses (``models_probe``), so there is one place that says whether the model is usable.

No tool, no write path, no machine access: it only decides whether a model request is sent.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


def counts_as_unavailable(e: BaseException) -> bool:
    """True for a failure that says the model server is not answering (timeout, connection, 5xx)."""
    try:
        import openai
        if isinstance(e, openai.APIConnectionError):           # APITimeoutError is a subclass
            return True
        if isinstance(e, openai.APIStatusError):
            return int(getattr(e, "status_code", 0) or 0) >= 500
    except ImportError:  # pragma: no cover - openai is a dependency
        pass
    if isinstance(e, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True
    name, msg = type(e).__name__.lower(), str(e).lower()
    return "timeout" in name or "timed out" in msg


class ModelBreaker:
    """CLOSED / OPEN / HALF_OPEN breaker. ``fails`` <= 0 disables it (always CLOSED)."""

    def __init__(self, fails: int = 2, cooldown_s: float = 120.0, clock: Callable[[], float] = time.monotonic):
        self.fails = int(fails)
        self.cooldown_s = float(cooldown_s)
        self._clock = clock
        self._state = BreakerState.CLOSED
        self.consecutive = 0
        self.opened_at: float | None = None
        self.last_failure: str | None = None
        self.last_probe: dict[str, Any] | None = None
        self.trips = 0
        self._probing = False
        # (monotonic ts, ok) of the Runtime's GET /models health check (was Runtime._llm_probe)
        self.models_probe: tuple[float, bool] = (-1e9, False)

    # ------------------------------------------------------------------ state
    @property
    def enabled(self) -> bool:
        return self.fails > 0

    @property
    def state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self.opened_at is not None \
                and self._clock() - self.opened_at >= self.cooldown_s:
            self._state = BreakerState.HALF_OPEN
        return self._state

    def remaining_s(self) -> float:
        if self.state is not BreakerState.OPEN or self.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown_s - (self._clock() - self.opened_at))

    def view(self) -> dict[str, Any]:
        st = self.state
        return {"state": st.value, "status": "degraded" if st is BreakerState.OPEN else "ok",
                "consecutive_failures": self.consecutive, "fails_to_open": self.fails, "cooldown_s": self.cooldown_s,
                "remaining_s": round(self.remaining_s(), 1), "trips": self.trips,
                "last_failure": self.last_failure, "last_probe": self.last_probe}

    # ------------------------------------------------------------------ outcomes of real calls
    def record_success(self) -> None:
        self.consecutive = 0
        self.last_failure = None
        self._state, self.opened_at = BreakerState.CLOSED, None

    def record_failure(self, e: BaseException) -> None:
        """A real commander call failed. Only availability failures count; anything else means the server answered."""
        if not counts_as_unavailable(e):
            self.record_success()
            return
        self.consecutive += 1
        self.last_failure = f"{type(e).__name__}: {e}"[:200]
        if self.enabled and (self.consecutive >= self.fails or self._state is BreakerState.HALF_OPEN):
            self._open()

    def _open(self) -> None:
        if self._state is not BreakerState.OPEN:
            self.trips += 1
        self._state, self.opened_at = BreakerState.OPEN, self._clock()

    # ------------------------------------------------------------------ admission
    async def admit(self, probe: Callable[[], Awaitable[bool]] | None) -> bool:
        """True when a commander call may be sent now. HALF_OPEN runs ``probe`` once (other callers meanwhile are
        refused, as if still OPEN). Without a probe a HALF_OPEN breaker lets the next real call through."""
        if not self.enabled:
            return True
        st = self.state
        if st is BreakerState.CLOSED:
            return True
        if st is BreakerState.OPEN or self._probing:
            return False
        if probe is None:
            return True
        self._probing = True
        t0 = time.perf_counter()
        try:
            try:
                ok = bool(await probe())
                err = None
            except Exception as e:  # noqa: BLE001 - a failed probe is a result, not an error
                ok, err = False, f"{type(e).__name__}: {e}"[:200]
        finally:
            self._probing = False
        self.last_probe = {"ok": ok, "ms": round((time.perf_counter() - t0) * 1000, 1), "error": err}
        if ok:
            self.record_success()
            return True
        self.last_failure = err or "probe failed"
        self._open()
        return False
