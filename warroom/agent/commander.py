from __future__ import annotations

from warroom.config import Settings
from warroom.contracts import CommanderClient, CommanderPlan, CommanderRequest, LLMCallInfo, LLMMode


class OpenAICommander:
    """OpenAI-compatible chat client (Ollama / vLLM / NVIDIA cloud via BASE_URL, MODEL).
    Constrained output = CommanderPlan.model_json_schema(): vLLM extra_body={"guided_json": schema}
    (xgrammar), else response_format={"type":"json_schema",...}. Validates with pydantic; on
    invalid JSON / timeout falls back to MockCommander when settings.llm_fallback_to_mock."""
    mode = LLMMode.LLM

    def __init__(self, settings: Settings):
        raise NotImplementedError

    async def plan(self, req: CommanderRequest) -> tuple[CommanderPlan, LLMCallInfo]: raise NotImplementedError


class MockCommander:
    """Deterministic rule commander. Must reproduce page 11 exactly: plan phase -> H1
    steam_pressure_low, expectation CURE-07 temp_c >= 121 within 30 min, steps S1 cure_formula,
    S2 query_orders_shifts, S3 solve_schedule(dep S1,S2), S4 confirm_schedule, S5 notify_teams;
    replan with H1 rejected -> H2 steam_leak/sensor_drift, read_sensors CURE-06/08, sop_search
    "硫化中斷處置", CURE-07 out of service, reschedule. Page 2: cooling_insufficient,
    notify maintenance (R1) + slow_feed (R3)."""
    mode = LLMMode.MOCK

    async def plan(self, req: CommanderRequest) -> tuple[CommanderPlan, LLMCallInfo]: raise NotImplementedError


def make_commander(settings: Settings) -> CommanderClient:
    raise NotImplementedError
