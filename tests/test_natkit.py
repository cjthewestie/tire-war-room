"""NeMo Agent Toolkit integration (warroom.natkit, deploy/nat) and the OpenShell sandbox policy
(deploy/openshell). NAT itself is an overlay, not a base dependency, so the plugin import test is
skipped unless NAT is installed; deploy/nat/run.sh runs the real `nat run` / `nat eval`."""
from __future__ import annotations

import collections
import ipaddress
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from warroom.natkit.tracing import StepEvent, TracedCommander, TracedTools, instrument

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- tracing proxies
class _Res:
    def __init__(self, ok=True):
        self.ok, self.data, self.error, self.duration_ms = ok, {"x": 1}, None if ok else "boom", 1.0


class _Call:
    class tool:                       # noqa: N801 - mimics ToolName enum member
        value = "read_sensors"
    args, event_id, step_id, attempt = {"machine_ids": ["CURE-07"]}, "EV-1", "S1", 1


class _Tools:
    def specs(self, names=None):
        return ["spec"]

    def call(self, call):
        return _Res()

    async def acall(self, call):
        return _Res(ok=False)


async def test_traced_tools_emit_start_end_pair_and_forward_everything():
    seen: list[StepEvent] = []
    t = TracedTools(_Tools(), seen.append)
    assert t.specs() == ["spec"]                       # untouched attributes pass through
    assert t.call(_Call()).ok is True
    res = await t.acall(_Call())
    assert res.ok is False
    assert [e.kind for e in seen] == ["TOOL_START", "TOOL_END", "TOOL_START", "TOOL_END"]
    assert seen[0].step_id == seen[1].step_id != seen[2].step_id
    assert seen[3].metadata["ok"] is False and seen[3].output == {"error": "boom"}


def test_instrument_is_idempotent():
    class Cmd:
        mode = "mock"

        async def plan(self, req, on_alert=None):
            raise AssertionError("not called")

    class SM:
        tools, commander = _Tools(), Cmd()
    sm = SM()
    instrument(sm, lambda e: None)
    first = sm.tools
    instrument(sm, lambda e: None)
    assert isinstance(sm.tools, TracedTools) and sm.tools is first
    assert isinstance(sm.commander, TracedCommander) and sm.commander.mode == "mock"


async def test_page11_event_through_runner_is_fully_traced(tmp_path):
    """The NAT workflow body: page-11 event through the real Runtime (mock commander). Every tool
    result in the audit log has exactly one TOOL_START/END pair, every commander call an LLM pair."""
    from warroom.natkit.runner import run_event
    seen: list[StepEvent] = []
    out = await run_event(emit=seen.append, workdir=tmp_path)
    assert out["final_state"] == "closed", out
    assert out["audit_chain_ok"] is True
    kinds = collections.Counter(e.kind for e in seen)
    assert kinds["TOOL_START"] == kinds["TOOL_END"] == out["tool_calls"] > 0
    assert kinds["LLM_START"] == kinds["LLM_END"] == out["llm_calls"] == 2
    tools = {e.name for e in seen if e.kind == "TOOL_START"}
    assert {"cure_formula", "solve_schedule", "sop_search"} <= tools
    assert [h["status"] for h in out["hypotheses"]][:1] == ["rejected"]          # H1 rejected at 119.5 °C
    assert [d["option"] for d in out["decisions"]][-1] == "夜班加班"


# --------------------------------------------------------------------------- NAT plugin wiring
def test_entry_point_and_configs_name_the_registered_workflow():
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())
    ep = py["project"]["entry-points"]["nat.components"]["warroom"]
    assert ep == "warroom.natkit.register"
    assert (ROOT / "warroom/natkit/register.py").exists()
    assert "nvidia-nat" not in " ".join(py["project"]["dependencies"])        # overlay only (pandas<3 pin)
    src = (ROOT / "warroom/natkit/register.py").read_text()
    name = re.search(r'name="([a-z_]+)"\)', src).group(1)
    for f in ("workflow.yml", "eval.yml"):
        cfg = yaml.safe_load((ROOT / "deploy/nat" / f).read_text())
        assert cfg["workflow"]["_type"] == name == "tire_war_room_event"
    ev = yaml.safe_load((ROOT / "deploy/nat/eval.yml").read_text())["eval"]["general"]
    assert ev["profiler"]["base_metrics"] is True
    assert ev["output"]["dir"].startswith("./data/runtime/")                   # gitignored
    assert "nvidia-nat" in (ROOT / "deploy/nat/requirements.txt").read_text()
    subprocess.run(["bash", "-n", str(ROOT / "deploy/nat/run.sh")], check=True)


def test_nat_plugin_registers_when_nat_is_installed():
    pytest.importorskip("nat")
    import warroom.natkit.register as reg
    from nat.cli.type_registry import GlobalTypeRegistry
    assert GlobalTypeRegistry.get().get_function(reg.TireWarRoomEventConfig) is not None


# --------------------------------------------------------------------------- OpenShell sandbox policy
PRIVATE = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


def _policy(host: str = "192.168.10.20") -> dict:
    return yaml.safe_load((ROOT / "deploy/openshell/warroom-policy.yaml").read_text().replace("${SPARK_HOST}", host))


def test_openshell_policy_follows_schema_and_stays_on_the_spark():
    p = _policy()
    assert p["version"] == 1
    assert set(p) <= {"version", "filesystem_policy", "landlock", "process", "network_policies",
                      "network_middlewares"}
    assert p["landlock"]["compatibility"] == "hard_requirement"
    assert set(p["filesystem_policy"]["read_write"]) == {"/app/data/runtime", "/tmp"}
    ports = set()
    for name, rule in p["network_policies"].items():
        assert rule["binaries"] == [{"path": "/opt/venv/bin/python*"}], name
        for ep in rule["endpoints"]:
            assert ep["host"] == "192.168.10.20"
            assert all(any(ipaddress.ip_network(n).subnet_of(pn) for pn in PRIVATE) for n in ep["allowed_ips"])
            ports.add(ep["port"])
    assert ports == {8001, 6333, 1883}                 # vLLM, Qdrant, MQTT; never 502 Modbus / 4840 OPC UA / web


def test_openshell_launcher_dry_run_fills_the_policy(tmp_path):
    r = subprocess.run(["bash", str(ROOT / "deploy/openshell/run_sandboxed.sh"), "--dry-run"], cwd=ROOT,
                       env={"PATH": "/usr/bin:/bin", "SPARK_HOST": "10.1.2.3"}, capture_output=True, text=True,
                       check=True)
    assert "${" not in r.stdout
    assert "openshell sandbox create --name warroom --from warroom-app:latest --policy" in r.stdout
    assert "BASE_URL=http://10.1.2.3:8001/v1" in r.stdout


# --------------------------------------------------------------------------- NAT on the Spark (offline)
def _run_sh(tmp_path, env_extra, *args):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", **env_extra}          # no uv on PATH, like the Spark
    return subprocess.run(["bash", str(ROOT / "deploy/nat/run.sh"), *args], env=env,
                          capture_output=True, text=True, timeout=30)


def _fake_nat(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\necho "NAT $* PYTHONPATH=$PYTHONPATH HF=$HF_HUB_OFFLINE"\n')
    path.chmod(0o755)
    return path


def test_run_sh_uses_the_spark_nat_venv_without_uv(tmp_path):
    _fake_nat(tmp_path / "opt/nat-venv/bin/nat")
    r = _run_sh(tmp_path, {"WARROOM_ROOT": str(tmp_path / "opt"), "LLM": "1",
                           "BASE_URL": "http://127.0.0.1:8001/v1", "MODEL": "nemotron"}, "eval")
    assert r.returncode == 0, r.stderr
    assert "NAT eval --config_file deploy/nat/eval.yml" in r.stdout
    assert "workflow.base_url http://127.0.0.1:8001/v1" in r.stdout and "workflow.model nemotron" in r.stdout
    assert f"PYTHONPATH={ROOT}" in r.stdout and "HF=1" in r.stdout          # release code first, offline Hub


def test_run_sh_override_and_clear_error_when_no_nat(tmp_path):
    nat = _fake_nat(tmp_path / "custom/nat")
    r = _run_sh(tmp_path, {"WARROOM_ROOT": str(tmp_path / "none"), "WARROOM_NAT": str(nat)}, "run")
    assert r.returncode == 0 and "NAT run --config_file deploy/nat/workflow.yml --input press7_temp_drop" in r.stdout
    r = _run_sh(tmp_path, {"WARROOM_ROOT": str(tmp_path / "none")}, "run")
    assert r.returncode == 127 and "install_spark.sh" in r.stderr


def test_spark_example_points_at_vllm_not_the_app():
    run_sh = (ROOT / "deploy/nat/run.sh").read_text()
    compose = (ROOT / "deploy/spark/docker-compose.yml").read_text()
    assert '8001:8000"' in compose                                            # vLLM published on :8001
    assert "BASE_URL=http://127.0.0.1:8001/v1 MODEL=nemotron" in run_sh
    assert "localhost:8000/v1" not in run_sh and ":8000/v1" not in run_sh
    assert "nemotron" in (ROOT / "deploy/spark/vllm-serve.sh").read_text()     # served model name


def test_install_spark_builds_the_nat_venv_while_online():
    inst = (ROOT / "deploy/bin/install_spark.sh").read_text()
    build = (ROOT / "deploy/nat/build_venv.sh").read_text()
    assert "deploy/nat/build_venv.sh" in inst and "NO_NAT_VENV" in inst
    assert inst.index("build_venv.sh") > inst.index("bench venv ok")          # after $ROOT/venv exists
    assert "deploy/nat/requirements.txt" in build and "--offline" in build and "nat.components" in build
    subprocess.run(["bash", "-n", str(ROOT / "deploy/nat/build_venv.sh")], check=True)
