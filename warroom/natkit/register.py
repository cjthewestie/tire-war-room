"""NAT plugin: registers the war-room event workflow with NeMo Agent Toolkit.

Loaded by NAT through the "nat.components" entry point in pyproject.toml. The workflow input is a
scenario name (press7_temp_drop by default); the output is a JSON summary of the event. Every tool
call and commander call inside the state machine shows up in NAT's event stream, so `nat eval` with
a `profiler:` section produces the per-tool and per-LLM-call latency and token reports.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig

from warroom.contracts import LLMMode, ScenarioName


class TireWarRoomEventConfig(FunctionBaseConfig, name="tire_war_room_event"):
    """One anomaly event through the war-room state machine (classify → plan → simulate → gate →
    execute → verify → close), with the page-11 supervisor script."""

    llm_mode: Literal["mock", "llm"] = Field("mock", description="mock = deterministic commander, no model")
    base_url: str | None = Field(None, description="OpenAI-compatible endpoint (vLLM on the Spark, Ollama on a Mac)")
    model: str | None = Field(None, description="Served model name")
    api_key: str | None = Field(None, description="API key for the endpoint, if it needs one")
    default_scenario: str = Field(ScenarioName.PRESS7_TEMP_DROP.value)
    supervisor: Literal["scripted", "none"] = Field("scripted")
    sim_until: str = Field("03:30:00", description="Factory time at which the run stops if the event is still open")
    max_wall_s: float = Field(900.0)


@register_function(config_type=TireWarRoomEventConfig)
async def tire_war_room_event(config: TireWarRoomEventConfig, builder: Builder):
    from warroom.natkit.runner import run_event
    from warroom.natkit.tracing import nat_emitter

    async def _run(scenario: str) -> str:
        """Run one war-room event. Input: a scenario name (press7_temp_drop, open_mill_overheat);
        empty or unknown text runs the default scenario. Returns the event summary as JSON."""
        name = (scenario or "").strip()
        try:
            sc = ScenarioName(name)
        except ValueError:
            sc = ScenarioName(config.default_scenario)
        out = await run_event(emit=nat_emitter(), scenario=sc, llm_mode=LLMMode(config.llm_mode),
                              base_url=config.base_url, model=config.model, api_key=config.api_key,
                              supervisor=config.supervisor, sim_until=config.sim_until,
                              max_wall_s=config.max_wall_s)
        return json.dumps(out, ensure_ascii=False)

    yield FunctionInfo.from_fn(_run, description=TireWarRoomEventConfig.__doc__)
