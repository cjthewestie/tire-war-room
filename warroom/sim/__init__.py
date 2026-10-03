"""warroom/sim — factory model, per-second sensor stream, replay, fault injection, scenarios.
Owner: sim builder. Public: Factory, get_scenario, ReplaySource, record_jsonl."""
from warroom.sim.factory_sim import Factory
from warroom.sim.replay import ReplaySource, record_jsonl
from warroom.sim.scenarios import get_scenario, list_scenarios

__all__ = ["Factory", "ReplaySource", "get_scenario", "list_scenarios", "record_jsonl"]
