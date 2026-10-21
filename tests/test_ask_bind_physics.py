"""Spark 10-21 §5: 問戰情室 let the model fill cure_formula's actual_temp_c (165 °C while 7 號 read 128 °C) and the
made-up hours passed the number check. The physics inputs are now bound by code from the press's live cure job and
reading, on both paths (問戰情室 and Agent Nemo, which share AskAgent._run)."""
from __future__ import annotations

from tests.test_ask import _entries, _llm_agent, _plan, audit, plant  # noqa: F401 - fixtures
from warroom.agent.ask import AskAgent, bind_cure_formula
from warroom.agent.main_agent import tool_specs
from warroom.contracts import AuditKind, ToolName


def _live(ctx, press="CURE-07"):
    st = ctx.sim.machine(press)
    return st.cure_job, round(float(st.last_reading.temp_c), 1)


async def test_ask_binds_cure_formula_from_the_plant(plant, audit):  # noqa: F811
    ctx, reg = plant
    job, temp = _live(ctx)
    agent = _llm_agent(_plan(("cure_formula", {"size": "L", "actual_temp_c": 165, "elapsed_h": 3})),
                       "7 號機要再硫化一段時間。")
    a = await agent.ask("7 號機現在要硫化多久？", tools=reg, audit=audit, context={}, now=ctx.clock.now())
    call = _entries(audit, a.id, AuditKind.TOOL_CALL)[0].payload["args"]
    assert call["actual_temp_c"] == temp != 165
    assert call["size"] == job.size.value and call["elapsed_h"] == round(job.cure_elapsed_h, 2)
    assert call["ref_temp_c"] == job.setpoint_c
    bound = [e.payload for e in _entries(audit, a.id, AuditKind.SYSTEM) if "cure_formula_args_from_plant" in e.payload]
    assert bound and bound[0]["from"]["actual_temp_c"] == 165 and bound[0]["press"] == "CURE-07"
    # the step's line says the inputs used, so they are grounded numbers for the answer
    assert a.steps[0].ok and "7 號機" in a.steps[0].summary and f"{temp:g}°C" in a.steps[0].summary


async def test_main_agent_path_binds_by_press_id(plant, audit):  # noqa: F811
    ctx, reg = plant
    _job, temp = _live(ctx)
    res = await AskAgent._run(reg, audit, "TURN-1", 1, ToolName.CURE_FORMULA,
                              {"press_id": "7", "actual_temp_c": 118, "size": "S", "elapsed_h": 1})
    assert res.ok, res.error
    assert res.data["inputs"]["press_id"] == "CURE-07" and res.data["inputs"]["actual_temp_c"] == temp


async def test_no_press_is_an_error_not_a_guess(plant, audit):  # noqa: F811
    _ctx, reg = plant
    res = await AskAgent._run(reg, audit, "TURN-2", 1, ToolName.CURE_FORMULA, {"size": "M", "actual_temp_c": 165})
    assert not res.ok and "press_id" in res.error


def test_offline_rig_keeps_args():
    args, inputs, err = bind_cure_formula(None, {"size": "M", "actual_temp_c": 122, "elapsed_h": 10, "press_id": "x"})
    assert (args, inputs, err) == ({"size": "M", "actual_temp_c": 122, "elapsed_h": 10}, None, None)


def test_main_agent_cure_formula_takes_only_a_press():
    spec = next(s for s in tool_specs(False) if s["function"]["name"] == "cure_formula")["function"]["parameters"]
    assert set(spec["properties"]) == {"press_id"} and spec["required"] == ["press_id"]
