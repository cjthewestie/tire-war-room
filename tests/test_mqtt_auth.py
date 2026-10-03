"""Page-10 broker on the Spark: no anonymous clients, a topic ACL, and an inbound nft allow-list so a LAN
host cannot publish forged press temperatures (review finding: MQTT on the LAN was unauthenticated and
the nft table filtered egress only)."""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from warroom import factory as F
from warroom.api.ingest import IngestPipeline, MqttBridge, mqtt_credentials_from_env
from warroom.contracts import SensorReading

ROOT = Path(__file__).resolve().parents[1]
SPARK = ROOT / "deploy" / "spark"
MOSQ = SPARK / "mosquitto"
NET = ROOT / "deploy" / "network"
IMAGE = "eclipse-mosquitto:2"


def _compose() -> dict:
    return yaml.safe_load((SPARK / "docker-compose.yml").read_text())["services"]


def _body(text: str) -> str:
    return "\n".join(ln.split("#")[0] for ln in text.splitlines() if not ln.lstrip().startswith("#"))


# ------------------------------------------------------------------------------------------ static wiring
def test_broker_refuses_anonymous_and_uses_acl():
    conf = _body((MOSQ / "mosquitto.conf").read_text())
    assert "allow_anonymous false" in conf
    assert "password_file /mosquitto/auth/passwd" in conf and "acl_file /mosquitto/auth/acl" in conf
    s = _compose()
    assert "no-auth" not in " ".join(s["mqtt"]["command"])
    assert s["mqtt"]["command"] == ["mosquitto", "-c", "/mosquitto/config/mosquitto.conf"]
    assert s["mqtt"]["depends_on"]["mqtt-init"]["condition"] == "service_completed_successfully"
    assert s["mqtt"]["ports"] == ["${MQTT_BIND:-127.0.0.1}:1883:1883"]       # loopback unless asked
    assert s["mqtt-init"]["network_mode"] == "none"


def test_acl_gateway_publish_only_on_telemetry():
    acl = " ".join(ln for ln in (MOSQ / "acl").read_text().splitlines() if not ln.lstrip().startswith("#")).split()
    text = " ".join(acl)
    assert "user warroom-app topic readwrite warroom/telemetry/#" in text
    assert "user gateway topic write warroom/telemetry/#" in text
    assert "pattern" not in acl and text.count("topic") == 2                  # nothing else granted


def test_app_logs_in_with_the_generated_password():
    app = _compose()["app"]
    env = app["environment"]
    assert env["WARROOM_MQTT_USERNAME"] == "warroom-app"
    assert env["WARROOM_MQTT_PASSWORD_FILE"] == "/app/mqtt-auth/app.pass"
    assert "${MQTT_DATA:-/opt/warroom/data/mqtt}:/app/mqtt-auth:ro" in app["volumes"]
    assert app["depends_on"]["mqtt-init"]["condition"] == "service_completed_successfully"


def test_credentials_from_env(tmp_path, monkeypatch):
    for k in ("WARROOM_MQTT_USERNAME", "WARROOM_MQTT_PASSWORD", "WARROOM_MQTT_PASSWORD_FILE"):
        monkeypatch.delenv(k, raising=False)
    assert mqtt_credentials_from_env() == {"username": None, "password": None}
    (tmp_path / "app.pass").write_text("s3cretpw\n")
    monkeypatch.setenv("WARROOM_MQTT_USERNAME", "warroom-app")
    monkeypatch.setenv("WARROOM_MQTT_PASSWORD_FILE", str(tmp_path / "app.pass"))
    assert mqtt_credentials_from_env() == {"username": "warroom-app", "password": "s3cretpw"}
    monkeypatch.setenv("WARROOM_MQTT_PASSWORD", "fromenv")
    assert mqtt_credentials_from_env()["password"] == "fromenv"


def test_unreadable_password_file_falls_back_without_leaking(tmp_path, monkeypatch):
    monkeypatch.setenv("WARROOM_INGEST", "mqtt")
    monkeypatch.setenv("WARROOM_MQTT_URL", "mqtt://warroom-app:hunter2@127.0.0.1:1")
    monkeypatch.setenv("WARROOM_MQTT_USERNAME", "warroom-app")
    monkeypatch.delenv("WARROOM_MQTT_PASSWORD", raising=False)
    monkeypatch.setenv("WARROOM_MQTT_PASSWORD_FILE", str(tmp_path / "missing.pass"))
    monkeypatch.setenv("WARROOM_TSDB_PATH", str(tmp_path / "ts.sqlite"))
    p = IngestPipeline.from_env(tmp_path)
    assert p.bridge is None and "mqtt unavailable" in p.stats.last_error
    assert "hunter2" not in p.stats.last_error


def test_inbound_table_fences_8000_and_1883():
    nft = (NET / "nftables-warroom-inbound.nft").read_text()
    body = _body(nft)
    assert "table inet warroom_inbound" in body
    assert "hook input" in body and "hook forward" in body
    gw = body.split("set mqtt_gateways {")[1].split("}")[0]
    assert "elements" not in gw                                                # empty by default
    assert "tcp dport 1883 ip saddr @mqtt_gateways accept" in body
    assert "tcp dport 8000 ip saddr @app_clients accept" in body
    assert "tcp dport { 8000, 1883 }" in body and "drop" in body
    assert "ct direction reply accept" in body
    assert "ct original proto-dst 1883 ip saddr @mqtt_gateways accept" in body
    assert 'include "/etc/warroom/inbound.d/*.nft"' in body
    unit = (ROOT / "deploy/systemd/warroom-firewall.service").read_text()
    assert "/opt/warroom/current/deploy/network/nftables-warroom-inbound.nft" in unit
    assert "nft delete table inet warroom_inbound" in unit


def test_compose_comment_no_longer_points_at_egress_only_table():
    text = (SPARK / "docker-compose.yml").read_text()
    assert "LAN-bind only behind the nft allow-list" not in text
    assert "nftables-warroom-inbound.nft" in text or "mqtt_gateways" in text


def _allow(tmp_path, **env):
    e = {**os.environ, "INBOUND_DIR": str(tmp_path), "NO_RELOAD": "1"}
    e.pop("APP_CLIENTS", None)
    e.pop("MQTT_GATEWAYS", None)
    e.update(env)
    return subprocess.run(["bash", str(ROOT / "deploy/bin/set_inbound_allow.sh")], env=e,
                          capture_output=True, text=True)


def test_set_inbound_allow_writes_site_file(tmp_path):
    r = _allow(tmp_path, MQTT_GATEWAYS="192.168.10.5, 192.168.10.6", APP_CLIENTS="192.168.10.0/24")
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "allow.nft").read_text()
    assert "flush set inet warroom_inbound mqtt_gateways" in text
    assert "add element inet warroom_inbound mqtt_gateways { 192.168.10.5, 192.168.10.6 }" in text
    assert "add element inet warroom_inbound app_clients { 192.168.10.0/24 }" in text
    r = _allow(tmp_path)                                    # no gateway: set flushed, app default kept
    assert r.returncode == 0, r.stderr
    text = (tmp_path / "allow.nft").read_text()
    assert "flush set inet warroom_inbound mqtt_gateways" in text and "add element" not in text


@pytest.mark.parametrize("env", [{"MQTT_GATEWAYS": "0.0.0.0/0"}, {"MQTT_GATEWAYS": "gateway.local"},
                                 {"APP_CLIENTS": ""}, {"APP_CLIENTS": "192.168.10.0/33"}])
def test_set_inbound_allow_rejects_bad_values(tmp_path, env):
    r = _allow(tmp_path, **env)
    assert r.returncode != 0 and not (tmp_path / "allow.nft").exists()


# --------------------------------------------------------------------- live broker (docker, skipped if none)
def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    if not _docker_ok():
        pytest.skip(f"docker or image {IMAGE} not available")
    auth = tmp_path_factory.mktemp("mqtt-auth")
    run = ["docker", "run", "--rm", "--user", "0", "-e", "MQTT_GATEWAY_PASSWORD=gateway-secret-42",
           "-v", f"{MOSQ / 'init-auth.sh'}:/mosquitto/init-auth.sh:ro", "-v", f"{MOSQ / 'acl'}:/mosquitto/acl.src:ro",
           "-v", f"{auth}:/mosquitto/auth", "--entrypoint", "/bin/sh", IMAGE, "/mosquitto/init-auth.sh"]
    r = subprocess.run(run, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "users warroom-app, gateway" in r.stdout
    cid = subprocess.run(["docker", "run", "-d", "--rm", "-p", "127.0.0.1::1883",
                          "-v", f"{MOSQ / 'mosquitto.conf'}:/mosquitto/config/mosquitto.conf:ro",
                          "-v", f"{auth}:/mosquitto/auth:ro", IMAGE,
                          "mosquitto", "-c", "/mosquitto/config/mosquitto.conf"],
                         capture_output=True, text=True, timeout=60, check=True).stdout.strip()
    try:
        port = int(subprocess.run(["docker", "port", cid, "1883/tcp"], capture_output=True, text=True,
                                  check=True).stdout.split(":")[-1])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.2)
        time.sleep(0.5)
        yield {"port": port, "app_pw": (auth / "app.pass").read_text().strip(), "auth": auth}
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def _reading(mid: str, seq: int) -> SensorReading:
    return SensorReading(ts=datetime(2026, 10, 3, 2, 14, tzinfo=F.TZ), machine_id=mid, seq=seq, temp_c=122.0)


def test_live_anonymous_and_wrong_password_refused(broker):
    url = f"mqtt://127.0.0.1:{broker['port']}"
    with pytest.raises(ConnectionError, match="Not authorized"):
        MqttBridge(url, client_id="anon", connect_timeout_s=2)
    with pytest.raises(ConnectionError, match="Not authorized"):
        MqttBridge(url, client_id="forger", connect_timeout_s=2, username="gateway", password="guess")


def test_live_app_round_trip_and_gateway_cannot_read(broker):
    url = f"mqtt://127.0.0.1:{broker['port']}"
    app = MqttBridge(url, client_id="app", username="warroom-app", password=broker["app_pw"])
    try:
        sent = [_reading("CURE-07", 1), _reading("A-BIGOPEN", 1)]
        assert app.publish(sent) == 2
        assert app.collect(sent, timeout_s=3) == sent
        # gateway: may publish telemetry (the app receives it) but never reads the feed back
        gw = MqttBridge(url, client_id="gw", connect_timeout_s=2, username="gateway", password="gateway-secret-42")
        try:
            fed = [_reading("CURE-07", 2)]
            assert gw.publish(fed) == 1
            assert app.collect(fed, timeout_s=3) == fed
            assert gw.collect(fed, timeout_s=1) is None
        finally:
            gw.close()
    finally:
        app.close()


def test_live_pipeline_from_env_logs_in(broker, tmp_path, monkeypatch):
    monkeypatch.setenv("WARROOM_INGEST", "mqtt")
    monkeypatch.setenv("WARROOM_MQTT_URL", f"mqtt://127.0.0.1:{broker['port']}")
    monkeypatch.setenv("WARROOM_MQTT_USERNAME", "warroom-app")
    monkeypatch.delenv("WARROOM_MQTT_PASSWORD", raising=False)
    (tmp_path / "app.pass").write_text(broker["app_pw"])
    monkeypatch.setenv("WARROOM_MQTT_PASSWORD_FILE", str(tmp_path / "app.pass"))
    monkeypatch.setenv("WARROOM_TSDB_PATH", str(tmp_path / "ts.sqlite"))
    p = IngestPipeline.from_env(tmp_path)
    try:
        assert p.bridge is not None, p.stats.last_error
        sent = [_reading(m, 5) for m in F.MACHINE_IDS]
        back = p.transport(sent)
        assert p.stats.mqtt_missed_ticks == 0 and p.stats.received == len(F.MACHINE_IDS)
        assert back == sent
    finally:
        p.bridge.close() if p.bridge else None
