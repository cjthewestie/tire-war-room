from __future__ import annotations

from warroom.config import Settings


class Runtime:
    """Wires every module. Main loop per sim second:
      readings = sim.step(1) -> actions = safety.check(...) -> sim.apply_safety_action (red path,
      never waits on LLM) -> anomalies = detector.ingest(readings) -> for each: asyncio task
      state_machine.handle(a) -> bus.publish(MACHINES ...). Builds DashboardSnapshot."""

    def __init__(self, settings: Settings | None = None):
        raise NotImplementedError

    async def start(self) -> None: raise NotImplementedError
    async def stop(self) -> None: raise NotImplementedError
    async def tick(self, dt_s: float = 1.0) -> None: raise NotImplementedError
    def snapshot(self): raise NotImplementedError
