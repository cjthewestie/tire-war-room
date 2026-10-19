"""Foundation checks: every module imports, contracts are self-consistent, factory numbers
match the proposal, and web/src/contracts.ts stays in sync with the Python enums/endpoints."""
import importlib
import math
import pathlib
import re

import pytest

from warroom import contracts as C
from warroom import factory as F

ROOT = pathlib.Path(__file__).resolve().parent.parent

MODULES = [
    "warroom.contracts", "warroom.factory", "warroom.clock", "warroom.bus", "warroom.config",
    "warroom.sim", "warroom.detect", "warroom.safety", "warroom.tools", "warroom.schedule",
    "warroom.kb", "warroom.agent", "warroom.api",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    importlib.import_module(name)


def test_28_machines_and_lines():
    assert len(F.MACHINES) == 28
    assert len(set(F.MACHINE_IDS)) == 28
    counts = {ln: sum(1 for m in F.MACHINES if m.line is ln) for ln in C.Line}
    assert counts == {C.Line.A: 7, C.Line.B: 4, C.Line.C: 4, C.Line.D: 1, C.Line.BOND: 1, C.Line.CURE: 11}
    assert len(F.SMALL_ONLY_PRESSES) == 7 and len(F.ML_PRESSES) == 4
    assert "CURE-07" in F.ML_PRESSES and "CURE-09" in F.ML_PRESSES       # page 11
    assert F.presses_on_header("H2") == ["CURE-06", "CURE-07", "CURE-08"]  # page 11 same pipe
    assert len(F.PROCESSES) == 26


def test_state_machine_transitions_closed():
    for s in C.EventState:
        assert s in C.ALLOWED_TRANSITIONS
        for t in C.ALLOWED_TRANSITIONS[s]:
            assert t in C.ALLOWED_TRANSITIONS
    for s in C.TERMINAL_STATES:
        assert not C.ALLOWED_TRANSITIONS[s]
    # happy path from page 5 is walkable
    path = [C.EventState.DETECTED, C.EventState.CLASSIFY, C.EventState.PLAN, C.EventState.SIMULATE,
            C.EventState.GATE, C.EventState.AWAIT_APPROVAL, C.EventState.EXECUTE, C.EventState.VERIFY,
            C.EventState.REPLAN, C.EventState.SIMULATE, C.EventState.GATE, C.EventState.EXECUTE,
            C.EventState.VERIFY, C.EventState.CLOSED]
    for a, b in zip(path, path[1:]):
        assert b in C.ALLOWED_TRANSITIONS[a], (a, b)


def test_tool_allow_list():
    assert C.READ_COMPUTE_TOOLS | C.WRITE_TOOLS == set(C.ToolName)
    assert len(C.READ_COMPUTE_TOOLS) == 6 and len(C.WRITE_TOOLS) == 3
    for state, tools in C.TOOLS_BY_STATE.items():
        if state is not C.EventState.EXECUTE:
            assert not (tools & C.WRITE_TOOLS), state
    assert len(C.RiskLevel) == 4


def test_commander_schema_and_roundtrip():
    schema = C.CommanderPlan.model_json_schema()
    assert schema["type"] == "object" and "steps" in schema["properties"]
    plan = C.CommanderPlan(
        goal="7 號降溫，大胎不能延誤",
        hypothesis=C.Hypothesis(id="H1", statement="蒸汽壓不足，溫度會穩定", cause_code="steam_pressure_low",
                                expectation=C.Expectation(machine_id="CURE-07", channel=C.Channel.TEMP,
                                                          op=">=", value=121, within_min=30)),
        steps=[C.PlanStep(id="S1", tool=C.ToolName.CURE_FORMULA, purpose="算延長硫化",
                          args={"size": "M", "actual_temp_c": 122, "elapsed_h": 10}),
               C.PlanStep(id="S3", tool=C.ToolName.SOLVE_SCHEDULE, depends_on=["S1"], purpose="重排",
                          args={"cure_overrides_h": {"CURE-07": "$S1.total_cure_h"}})],
        alert_card=C.AlertCard(title="7 號機溫度下降 128→122°C", summary="指揮官已介入"),
    )
    again = C.CommanderPlan.model_validate_json(plan.model_dump_json())
    assert again == plan


def test_arrhenius_page11_numbers_from_constants():
    # The tools builder implements this; here we only pin the constants so the numbers hold.
    t, tref = 122 + 273.15, F.CURE_TEMP_NOMINAL_C + 273.15
    ratio = math.exp(-F.ACTIVATION_ENERGY_KJ_MOL * 1000 / F.GAS_CONSTANT_J_MOL_K * (1 / t - 1 / tref))
    total = 10 + (F.CURE_HOURS[C.TireSize.M] - 10) / ratio
    assert round(total, 1) == 36.5 and round(total - F.CURE_HOURS[C.TireSize.M], 1) == 8.5


def test_safety_does_not_import_llm_or_agent():
    banned = re.compile(r"^\s*(from|import)\s+(warroom\.(agent|tools|api)|openai|httpx)", re.M)
    for p in (ROOT / "warroom" / "safety").rglob("*.py"):
        assert not banned.search(p.read_text()), p


def test_ts_contracts_in_sync():
    ts = (ROOT / "web" / "src" / "contracts.ts").read_text()
    for enum in (C.WsType, C.EventState, C.MachineStatus, C.RiskLevel, C.ToolName, C.ApprovalStatus,
                 C.DecisionKind, C.Line, C.TireSize, C.TileStatus, C.LLMMode, C.ScenarioName):
        for v in enum:
            assert f'"{v.value}"' in ts, f"{enum.__name__}.{v.name} missing from contracts.ts"
    for method, path, _req, _resp in C.REST_ENDPOINTS:
        assert path in ts, f"{method} {path} missing from contracts.ts"
    for mid in F.MACHINE_IDS:
        assert f'"{mid}"' in ts, mid
    m = re.search(r"export const WRITE_TOOLS[^=]*=\s*\[([^\]]*)\]", ts)
    assert m, "WRITE_TOOLS missing from contracts.ts"
    assert set(re.findall(r'"(\w+)"', m.group(1))) == {t.value for t in C.WRITE_TOOLS}


def test_bus_and_clock():
    import asyncio
    from datetime import datetime, timedelta

    from warroom.bus import InProcessBus
    from warroom.clock import SimClock

    clk = SimClock(datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ), manual=True)
    clk.advance(60)
    assert clk.now() == datetime(2026, 10, 3, 2, 15, tzinfo=F.TZ)

    async def run():
        bus = InProcessBus(clock=clk)
        got = []

        async def reader():
            async for m in bus.subscribe():
                got.append(m)
                return

        task = asyncio.create_task(reader())
        await asyncio.sleep(0)
        bus.publish(C.WsType.CLOCK, {"factory_time": clk.now().isoformat(), "speed": 1, "paused": False})
        await asyncio.wait_for(task, 1)
        return got

    got = asyncio.run(run())
    assert got[0].type is C.WsType.CLOCK
    assert clk.now() - timedelta(seconds=60) == datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ)
