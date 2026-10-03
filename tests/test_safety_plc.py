"""warroom/safety/plc.py: the safety core as a separate process (PLC stand-in), no app involved."""
import io
import json
import os

from warroom.contracts import FaultKind, FaultSpec, MachineStatus, SafetyAction, SafetyActionKind
from warroom.safety import RuleSafetyCore
from warroom.safety.plc import PlcProcess, serve_stdio
from warroom.sim import Factory


def test_plc_process_stops_overheat_without_the_app():
    f = Factory(backfill_min=0)
    f.step(1)
    f.inject_fault(FaultSpec(id="hot", machine_id="A-BIGOPEN", kind=FaultKind.TEMP_RAMP,
                             params={"target": 130.0, "rate_c_per_min": 30.0}))
    plc = PlcProcess()
    try:
        assert plc.pid != os.getpid()
        assert plc.selfcheck() == []                     # no warroom.api / agent / tools / LLM client loaded
        acts = []
        for _ in range(120):
            for a in plc.send(f.step(1), f.states()):
                act = SafetyAction.model_validate(a)
                f.apply_safety_action(act)
                acts.append(act)
        assert [(a.machine_id, a.rule_id, a.action) for a in acts] == [
            ("A-BIGOPEN", "SR-COMPOUND-120", SafetyActionKind.STOP_MACHINE)]
        assert acts[0].latency_ms < 200
        assert f.machine("A-BIGOPEN").status is MachineStatus.STOPPED
        assert plc.selfcheck() == []
    finally:
        plc.close()


def test_bad_frame_does_not_kill_the_plc():
    out = io.StringIO()
    n = serve_stdio(RuleSafetyCore(), io.StringIO('not json\n{"readings": [], "states": {}}\n'), out)
    lines = [json.loads(x) for x in out.getvalue().splitlines()]
    assert n == 2 and "error" in lines[0] and lines[1] == {"actions": []}
