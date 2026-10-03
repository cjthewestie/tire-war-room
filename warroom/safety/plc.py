"""PLC stand-in: the safety core as its own process, outside the app (page 10: 'Spark 故障 → 安全核心仍在 PLC 運作').

In the real plant these rules live in the PLC. In this build the same deterministic RuleSafetyCore runs
here as a separate OS process that shares nothing with the app, the agent or the LLM: it reads plant
telemetry frames and answers with the stop / hold / alarm actions the plant must apply. If the app
process crashes or the watchdog restarts it, this process keeps enforcing 煉膠 ≥120 °C and
已加硫 ≥110 °C stops for whatever telemetry source still reaches it.

Transports (one frame = one sample tick):
  stdio (default)   stdin:  {"readings": [SensorReading...], "states": {machine_id: MachineState}}
                    stdout: {"actions": [SafetyAction...]}         -- exactly one line per input line
  mqtt              subscribe <prefix>/frame, publish <prefix>/action (one message per action)

    uv run python -m warroom.safety.plc                       # stdio
    uv run python -m warroom.safety.plc --mqtt localhost:1883 # broker (mosquitto in deploy/spark)
    uv run python -m warroom.safety.plc --demo                # plant here, rules in a child process

Imports only warroom.contracts / warroom.factory / warroom.safety.core (and paho-mqtt in mqtt mode),
never warroom.api / agent / tools or an LLM client; tests/test_safety_plc.py checks it.
"""
from __future__ import annotations

import argparse
import json
import sys

from warroom.contracts import MachineState, SensorReading
from warroom.safety.core import RuleSafetyCore

FORBIDDEN_PREFIXES = ("warroom.api", "warroom.agent", "warroom.tools", "openai", "httpx")


def handle_frame(core: RuleSafetyCore, frame: dict) -> list[dict]:
    readings = [SensorReading.model_validate(r) for r in frame.get("readings", [])]
    states = {k: MachineState.model_validate(v) for k, v in (frame.get("states") or {}).items()}
    return [a.model_dump(mode="json") for a in core.check(readings, states)]


def forbidden_loaded() -> list[str]:
    return sorted(m for m in sys.modules if m.startswith(FORBIDDEN_PREFIXES))


def serve_stdio(core: RuleSafetyCore, inp=None, out=None) -> int:
    inp = inp or sys.stdin
    out = out or sys.stdout
    n = 0
    for line in inp:
        line = line.strip()
        if not line:
            continue
        if line == "__selfcheck__":
            out.write(json.dumps({"forbidden": forbidden_loaded()}) + "\n")
            out.flush()
            continue
        try:
            acts = handle_frame(core, json.loads(line))
            out.write(json.dumps({"actions": acts}, ensure_ascii=False) + "\n")
        except Exception as e:            # a bad frame must never kill the safety process
            out.write(json.dumps({"actions": [], "error": f"{type(e).__name__}: {e}"}) + "\n")
        out.flush()
        n += 1
    return n


def serve_mqtt(core: RuleSafetyCore, broker: str, prefix: str = "warroom/plc") -> None:  # pragma: no cover
    import paho.mqtt.client as mqtt

    host, _, port = broker.partition(":")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="warroom-safety-plc")

    def on_connect(c, _u, _f, _rc, _p=None):
        c.subscribe(f"{prefix}/frame", qos=1)

    def on_message(c, _u, msg):
        try:
            for a in handle_frame(core, json.loads(msg.payload)):
                c.publish(f"{prefix}/action", json.dumps(a, ensure_ascii=False), qos=1)
        except Exception as e:
            print(f"bad frame: {e}", file=sys.stderr)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host or "localhost", int(port or 1883))
    client.loop_forever(retry_first_connection=True)


class PlcProcess:
    """Parent-side handle: spawn the PLC stand-in as a child process and exchange stdio frames."""

    def __init__(self, python: str | None = None):
        import subprocess

        self.proc = subprocess.Popen([python or sys.executable, "-m", "warroom.safety.plc"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)

    @property
    def pid(self) -> int:
        return self.proc.pid

    def send(self, readings, states) -> list[dict]:
        frame = {"readings": [r.model_dump(mode="json") for r in readings],
                 "states": {k: v.model_dump(mode="json") for k, v in states.items()}}
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())["actions"]

    def selfcheck(self) -> list[str]:
        self.proc.stdin.write("__selfcheck__\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())["forbidden"]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)


def demo() -> int:
    """Plant (sim) in this process, rules in a child process, no app anywhere: A-BIGOPEN overheats
    past 120 °C and the PLC stand-in stops it."""
    from warroom.contracts import FaultKind, FaultSpec, SafetyAction
    from warroom.sim import Factory

    f = Factory(backfill_min=0)
    f.step(1)
    f.inject_fault(FaultSpec(id="hot", machine_id="A-BIGOPEN", kind=FaultKind.TEMP_RAMP,
                             params={"target": 130.0, "rate_c_per_min": 30.0}))
    plc = PlcProcess()
    print(f"plant pid (this process) runs the sim; PLC stand-in pid {plc.pid}; no app process")
    try:
        for _ in range(120):
            for a in plc.send(f.step(1), f.states()):
                act = SafetyAction.model_validate(a)
                f.apply_safety_action(act)
                print(f"{act.ts:%H:%M:%S} {act.machine_id} {act.action} {act.message} latency {act.latency_ms} ms")
    finally:
        plc.close()
    status = f.machine("A-BIGOPEN").status
    print(f"A-BIGOPEN status: {status}")
    return 0 if str(status) == "stopped" else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="warroom.safety.plc", description=__doc__.splitlines()[0])
    ap.add_argument("--mqtt", metavar="HOST:PORT", help="serve over MQTT instead of stdio")
    ap.add_argument("--prefix", default="warroom/plc")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args(argv)
    if args.demo:
        return demo()
    core = RuleSafetyCore()
    if args.mqtt:
        serve_mqtt(core, args.mqtt, args.prefix)
        return 0
    serve_stdio(core)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
