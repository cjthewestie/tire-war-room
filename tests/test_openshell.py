"""deploy/openshell: the sandboxed app reaches vLLM/Qdrant/MQTT on the Spark's LAN IP (never loopback,
which inside the sandbox's namespace is the sandbox), the compose app is stopped first, the nft table
keeps the LAN off :8001/:6333, and forwarded loopback clients need the supervisor PIN.
The real OpenShell run on the Spark is not covered here (no openshell CLI on the Mac)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from warroom.api import Runtime, create_app
from warroom.config import Settings
from warroom.contracts import ApprovalOption, ApprovalRequest, RiskLevel, ToolName

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "openshell" / "run_sandboxed.sh"


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if k not in ("SPARK_HOST", "WARROOM_SUPERVISOR_PIN")}
    e.update(env)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=e, timeout=30)


@pytest.mark.parametrize("host", ["", "127.0.0.1", "127.0.1.1", "localhost", "0.0.0.0"])
def test_refuses_loopback_or_missing_spark_host(host):
    r = run({"SPARK_HOST": host} if host else {}, "--dry-run")
    assert r.returncode != 0 and "LAN IP" in r.stderr


def test_real_run_requires_pin():
    r = run({"SPARK_HOST": "192.168.10.20"})
    assert r.returncode != 0 and "WARROOM_SUPERVISOR_PIN" in r.stderr


def test_dry_run_wires_lan_services_stops_app_and_hides_pin():
    r = run({"SPARK_HOST": "192.168.10.20", "WARROOM_SUPERVISOR_PIN": "s3cret-pin-99"}, "--dry-run")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "s3cret-pin-99" not in out and "WARROOM_SUPERVISOR_PIN=<set>" in out
    for url in ("BASE_URL=http://192.168.10.20:8001/v1", "QDRANT_URL=http://192.168.10.20:6333",
                "WARROOM_MQTT_URL=mqtt://192.168.10.20:1883"):
        assert url in out
    assert "127.0.0.1:8001" not in out and "127.0.0.1:6333" not in out
    assert "WARROOM_LOOPBACK_LOCAL=0" in out
    assert "docker compose -f deploy/spark/docker-compose.yml stop app" in out
    lines = out.splitlines()
    stop = next(i for i, ln in enumerate(lines) if ln.startswith("docker compose"))
    create = next(i for i, ln in enumerate(lines) if ln.startswith("openshell sandbox create"))
    nft = next(i for i, ln in enumerate(lines) if ln.startswith("sudo nft -f"))
    assert nft < stop < create                       # services fenced, compose app gone, then the sandbox


def test_filled_policy_and_nft_table():
    r = run({"SPARK_HOST": "192.168.10.20", "SANDBOX_SRC": "10.42.0.0/16"}, "--dry-run")
    assert r.returncode == 0, r.stderr
    policy = yaml.safe_load((ROOT / "data/runtime/openshell/warroom-policy.yaml").read_text())
    eps = [ep for p in policy["network_policies"].values() for ep in p["endpoints"]]
    assert {(ep["host"], ep["port"]) for ep in eps} == {("192.168.10.20", 8001), ("192.168.10.20", 6333),
                                                        ("192.168.10.20", 1883)}
    assert all(ep["allowed_ips"] == ["192.168.10.20/32"] for ep in eps)
    nft = (ROOT / "data/runtime/openshell/nft-sandbox-services.nft").read_text()
    body = "\n".join(ln for ln in nft.splitlines() if not ln.lstrip().startswith("#"))
    assert "${" not in body
    assert "elements = { 10.42.0.0/16, 192.168.10.20 }" in body
    assert "ip daddr 192.168.10.20 tcp dport { 8001, 6333 } ip saddr != @sandbox_src" in body
    assert "ct original ip daddr 192.168.10.20 ct original proto-dst { 8001, 6333 }" in body


def test_compose_service_binds_default_loopback_overridable():
    s = yaml.safe_load((ROOT / "deploy/spark/docker-compose.yml").read_text())["services"]
    assert s["vllm"]["ports"] == ["${VLLM_BIND:-127.0.0.1}:8001:8000"]
    assert s["qdrant"]["ports"] == ["${QDRANT_BIND:-127.0.0.1}:6333:6333"]
    assert s["mqtt"]["ports"] == ["${MQTT_BIND:-127.0.0.1}:1883:1883"]


# ---------------------------------------------------------------- forwarded loopback is not the local seat
def _req(rt):
    rt.approvals.request(ApprovalRequest(
        id="AP-1", event_id="EV-x", created_at=rt.now(), risk=RiskLevel.R3_CRITICAL, title="7號機停用，重排中",
        reason="H1 不成立", options=[ApprovalOption(id="A", label="夜班加班", tool=ToolName.NOTIFY_TEAMS,
                                                  args={"teams": ["body"], "kind": "overtime", "message": "夜班加班"})]))


BODY = {"request_id": "AP-1", "decision": "approve", "option_id": "A", "via": "touchscreen"}


@pytest.fixture
def rt(tmp_path) -> Runtime:
    s = Settings(audit_path=tmp_path / "audit.jsonl", schedule_dir=tmp_path / "sched",
                 qdrant_path=tmp_path / "qdrant")
    return Runtime(s, use_real=False, run_loop=False)


def test_loopback_needs_pin_when_loopback_local_off(rt, monkeypatch):
    monkeypatch.setenv("WARROOM_LOOPBACK_LOCAL", "0")
    _req(rt)
    app = create_app(rt, supervisor_pin="24681357")
    with TestClient(app, client=("127.0.0.1", 40000)) as fwd:
        assert fwd.post("/api/approvals/AP-1/decision", json=BODY).status_code == 401
        r = fwd.post("/api/approvals/AP-1/decision", json=BODY, headers={"X-Supervisor-Pin": "24681357"})
        assert r.status_code == 200, r.text
        assert r.json()["decision"]["decided_by"] == "supervisor@127.0.0.1"   # authenticated remote, not the local seat


def test_loopback_still_local_by_default(rt, monkeypatch):
    monkeypatch.delenv("WARROOM_LOOPBACK_LOCAL", raising=False)
    _req(rt)
    app = create_app(rt, supervisor_pin="24681357")
    with TestClient(app, client=("127.0.0.1", 40000)) as local:
        r = local.post("/api/approvals/AP-1/decision", json=BODY)
        assert r.status_code == 200, r.text
        assert r.json()["decision"]["decided_by"] == "supervisor"
