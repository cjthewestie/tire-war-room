"""warroom/sim — factory model, per-second sensor stream, replay, fault injection, scenarios.
Owner: sim builder. Public: Factory, get_scenario, ReplaySource, record_jsonl."""
from warroom.sim.factory_sim import Factory
from warroom.sim.scenarios import get_scenario, list_scenarios
from warroom.sim.replay import ReplaySource, record_jsonl

__all__ = ["Factory", "get_scenario", "list_scenarios", "ReplaySource", "record_jsonl"]
