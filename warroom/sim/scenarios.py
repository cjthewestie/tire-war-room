from __future__ import annotations

from datetime import datetime

from warroom.contracts import Scenario, ScenarioName


def get_scenario(name: ScenarioName, start_at: datetime | None = None) -> Scenario:
    """PRESS7_TEMP_DROP (page 11): CURE-07 curing MEDIUM tire at 128°C, 10 h into 28 h;
    02:14:00 temp falls 128→122 and looks stable (steam pressure low); ~02:31 keeps falling
    to 119.5°C (persistent leak) -> safety core acts, H1 rejected.
    OPEN_MILL_OVERHEAT (page 2): A-BIGOPEN compound temp rises to 112°C+, water flow low.
    BAD_DATA: spike/dropout on one sensor, must not create an Anomaly.
    CONCURRENT_3: three independent anomalies within a few seconds."""
    raise NotImplementedError


def list_scenarios() -> list[Scenario]:
    raise NotImplementedError
