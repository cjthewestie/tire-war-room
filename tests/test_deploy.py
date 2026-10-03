"""deploy/: compose, vLLM launcher flags, systemd units, firewall, watchdog, offline update + rollback.
Everything here runs on the Mac with fakes; the real Spark run is not covered by these tests."""
from __future__ import annotations

import configparser
import http.server
import os
import re
import socket
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from warroom.factory import MEMORY_BUDGET_GB

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SCRIPTS = sorted(list(DEPLOY.rglob("*.sh")) + list((ROOT / "bench").glob("*.sh")))
TEST_PIN = "t3st-pin-9876"


# ---------------------------------------------------------------- helpers
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):
        pass


@pytest.fixture
def healthy_url():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/health"
    srv.shutdown()


def sh(cmd: list[str], env: dict[str, str] | None = None, check: bool = True, timeout: float = 60):
    e = os.environ.copy()
    e["WARROOM_SUPERVISOR_PIN"] = TEST_PIN   # install_spark.sh refuses to finish without one
    e.update(env or {})
    r = subprocess.run(cmd, capture_output=True, text=True, env=e, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"{cmd} rc={r.returncode}\nstdout={r.stdout}\nstderr={r.stderr}")
    return r


# ---------------------------------------------------------------- static checks
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_shell_scripts_parse_and_are_executable(script: Path):
    sh(["bash", "-n", str(script)])
    assert os.access(script, os.X_OK), f"{script} not executable"


def test_spark_compose_structure():
    c = yaml.safe_load((DEPLOY / "spark" / "docker-compose.yml").read_text())
    s = c["services"]
    assert {"vllm", "qdrant", "app", "vllm-fp8"} <= set(s)
    assert c["networks"]["core"]["internal"] is True
    assert s["vllm"]["environment"]["HF_HUB_OFFLINE"] == "1"
    assert s["vllm"]["environment"]["VLLM_NO_USAGE_STATS"] == "1"
    # model server off the LAN by default; the OpenShell path binds the LAN IP behind nft-sandbox-services.nft
    assert s["vllm"]["ports"] == ["${VLLM_BIND:-127.0.0.1}:8001:8000"]
    assert s["qdrant"]["ports"] == ["${QDRANT_BIND:-127.0.0.1}:6333:6333"]
    assert s["qdrant"]["mem_limit"] == f"{MEMORY_BUDGET_GB['qdrant']}g"
    assert s["app"]["mem_limit"] == f"{MEMORY_BUDGET_GB['sched_app']}g"
    env = s["app"]["environment"]
    # app is on the host network (real client addresses for the write guard): services via their published ports
    assert env["BASE_URL"] == "http://${VLLM_BIND:-127.0.0.1}:8001/v1" and env["MODEL"] == "nemotron"
    assert env["QDRANT_URL"] == "http://${QDRANT_BIND:-127.0.0.1}:6333"
    assert env["LLM_FALLBACK_TO_MOCK"] == "true"
    assert s["vllm-fp8"]["profiles"] == ["control"]
    assert s["vllm-fp8"]["environment"]["QUANT"] == "fp8" and s["vllm-fp8"]["environment"]["SPEC"] == "off"
    # control group must not inherit production's :8001 (extends merges port lists)
    assert s["vllm-fp8"]["ports"] == ["127.0.0.1:8002:8000"]
    assert s["vllm-fp8"]["environment"]["HF_HUB_OFFLINE"] == "1"
    # page 9: vLLM fraction + Qdrant + app + DGX OS must stay <= 50 GB of the 128 GB pool
    util = float(s["vllm"]["environment"]["GPU_MEM_UTIL"].split(":-")[1].rstrip("}"))
    b = MEMORY_BUDGET_GB
    assert util * 128 + b["qdrant"] + b["sched_app"] + b["dgx_os"] <= 50
    for svc in ("vllm", "qdrant", "app"):
        assert s[svc]["restart"] == "unless-stopped"


def test_mac_compose_points_at_native_ollama():
    c = yaml.safe_load((DEPLOY / "mac" / "docker-compose.yml").read_text())
    assert "host.docker.internal" in c["services"]["app"]["environment"]["BASE_URL"]


def vllm_cmd(**env) -> str:
    r = sh(["bash", str(DEPLOY / "spark" / "vllm-serve.sh")], env={"DRY_RUN": "1", **env})
    return r.stdout


def test_vllm_launcher_production_flags():
    out = vllm_cmd()
    assert "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 " in out
    for flag in ("--kv-cache-dtype fp8", "--enable-prefix-caching", "--max-model-len 32768",
                 "--structured-outputs-config.backend xgrammar", "--reasoning-parser nemotron_v3",
                 "--moe-backend marlin", "--mamba-cache-mode align", "--max-num-seqs 4",
                 "--speculative_config.model nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark",
                 "--served-model-name"):
        assert flag in out, flag
    util = float(re.search(r"--gpu-memory-utilization (\S+)", out).group(1))
    model_server_budget = sum(MEMORY_BUDGET_GB[k] for k in ("model_nvfp4", "draft", "kv_state", "vllm_cuda"))
    assert util * 128 <= model_server_budget * 1.25      # stays near the 30 GB page-9 budget, not 0.85*128


def test_vllm_launcher_control_group_flags():
    out = vllm_cmd(QUANT="fp8", SPEC="off", PREFIX_CACHE="0", KV_DTYPE="auto")
    assert "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16" in out and "--quantization fp8" in out
    assert "--no-enable-prefix-caching" in out and "speculative" not in out
    assert "--moe-backend marlin" not in out


def test_vllm_launcher_mtp_and_bad_values():
    assert '"method":"mtp"' in vllm_cmd(SPEC="mtp").replace("\\", "")
    r = sh(["bash", str(DEPLOY / "spark" / "vllm-serve.sh")], env={"DRY_RUN": "1", "QUANT": "int3"}, check=False)
    assert r.returncode == 2


def test_systemd_units():
    units = {}
    for p in sorted((DEPLOY / "systemd").glob("*.service")):
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string(p.read_text())
        units[p.name] = cp
        assert {"Unit", "Service", "Install"} <= set(cp.sections()), p.name
        assert cp["Install"]["WantedBy"] == "multi-user.target"
        for key in ("ExecStart", "ExecStartPre"):
            if key in cp["Service"]:
                path = cp["Service"][key].split()[0]
                if path.startswith("/opt/warroom/current/"):
                    assert (ROOT / path.removeprefix("/opt/warroom/current/")).exists(), path
    assert set(units) == {"warroom-firewall.service", "warroom-stack.service", "warroom-preload.service",
                          "warroom-watchdog.service"}
    wd = units["warroom-watchdog.service"]["Service"]
    assert wd["Restart"] == "always" and wd["Type"] == "notify" and int(wd["WatchdogSec"]) >= 30
    assert "warroom-firewall.service" in units["warroom-stack.service"]["Unit"]["After"]
    assert "warroom-stack.service" in units["warroom-preload.service"]["Unit"]["Requires"]


def test_firewall_rules_drop_non_lan_egress():
    nft = (DEPLOY / "network" / "nftables-warroom.nft").read_text()
    assert "table inet warroom_offline" in nft
    assert "hook output" in nft and "hook forward" in nft
    assert nft.count(" drop") >= 2 and "10.0.0.0/8" in nft and "192.168.0.0/16" in nft
    pf = (DEPLOY / "network" / "pf-warroom.conf").read_text()
    assert "block drop out" in pf


# ---------------------------------------------------------------- watchdog
def test_watchdog_restarts_only_the_failing_service(tmp_path, healthy_url):
    fake = tmp_path / "compose"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/calls\n")
    fake.chmod(0o755)
    dead = f"http://127.0.0.1:{free_port()}/api/health"
    env = {"APP_URL": dead, "LLM_URL": healthy_url, "QDRANT_URL": healthy_url, "FAIL_LIMIT": "2",
           "STATE_DIR": str(tmp_path / "state"), "COMPOSE": str(fake), "LOG": str(tmp_path / "log"),
           "LLM_GRACE_S": "0"}
    wd = str(DEPLOY / "bin" / "watchdog.sh")
    sh(["bash", wd], env=env, check=False)
    assert not (tmp_path / "calls").exists()              # 1 failure < FAIL_LIMIT
    sh(["bash", wd], env=env, check=False)
    assert (tmp_path / "calls").read_text().split() == ["restart", "app"]
    assert "restarting app" in (tmp_path / "log").read_text()
    # the counter resets after a restart, healthy services never restart
    env["APP_URL"] = healthy_url
    assert sh(["bash", wd], env=env, check=False).returncode == 0
    assert (tmp_path / "calls").read_text().split() == ["restart", "app"]


def test_watchdog_respects_llm_boot_grace(tmp_path, healthy_url):
    fake = tmp_path / "compose"
    fake.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/calls\n")
    fake.chmod(0o755)
    env = {"APP_URL": healthy_url, "LLM_URL": f"http://127.0.0.1:{free_port()}/health", "QDRANT_URL": healthy_url,
           "FAIL_LIMIT": "1", "STATE_DIR": str(tmp_path / "s"), "COMPOSE": str(fake), "LOG": str(tmp_path / "log"),
           "LLM_GRACE_S": "999999999"}
    sh(["bash", str(DEPLOY / "bin" / "watchdog.sh")], env=env, check=False)
    assert not (tmp_path / "calls").exists()
    assert "boot grace" in (tmp_path / "log").read_text()


def _fake_compose(tmp_path: Path, health: str = "") -> Path:
    """compose stand-in: logs restarts; `ps -q` prints a container id; also acts as `docker inspect`."""
    fake = tmp_path / "compose"
    fake.write_text(f"""#!/bin/sh
case "$1" in
  ps) echo cid123 ;;
  inspect) echo "{health}" ;;
  *) echo "$@" >> {tmp_path}/calls ;;
esac
""")
    fake.chmod(0o755)
    return fake


def test_watchdog_waits_out_vllm_reload_after_its_own_restart(tmp_path, healthy_url):
    """A crash late in uptime must not become a restart loop: after restarting vllm the watchdog gives it
    LLM_GRACE_S to load, measured from that restart, not from boot."""
    fake = _fake_compose(tmp_path)
    env = {"APP_URL": healthy_url, "LLM_URL": f"http://127.0.0.1:{free_port()}/health", "QDRANT_URL": healthy_url,
           "FAIL_LIMIT": "3", "STATE_DIR": str(tmp_path / "s"), "COMPOSE": str(fake), "DOCKER": str(fake),
           "LOG": str(tmp_path / "log"), "LLM_GRACE_S": "600"}
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "llm.restarted").write_text("0\n")     # last restart long ago; boot uptime >> 600 s here
    wd = str(DEPLOY / "bin" / "watchdog.sh")
    for _ in range(9):
        sh(["bash", wd], env=env, check=False)
    calls = (tmp_path / "calls").read_text().split("\n") if (tmp_path / "calls").exists() else []
    assert [c for c in calls if c] == ["restart vllm"]      # was 3 restarts in 9 probes before the fix
    assert "restart grace" in (tmp_path / "log").read_text()


def test_watchdog_skips_vllm_while_container_health_starting(tmp_path, healthy_url):
    fake = _fake_compose(tmp_path, health="starting")
    env = {"APP_URL": healthy_url, "LLM_URL": f"http://127.0.0.1:{free_port()}/health", "QDRANT_URL": healthy_url,
           "FAIL_LIMIT": "1", "STATE_DIR": str(tmp_path / "s"), "COMPOSE": str(fake), "DOCKER": str(fake),
           "LOG": str(tmp_path / "log"), "LLM_GRACE_S": "0"}
    sh(["bash", str(DEPLOY / "bin" / "watchdog.sh")], env=env, check=False)
    assert not (tmp_path / "calls").exists()
    assert "health=starting" in (tmp_path / "log").read_text()


def test_watchdog_pause_file_blocks_restarts_until_stale(tmp_path):
    fake = _fake_compose(tmp_path)
    state = tmp_path / "s"
    state.mkdir()
    dead = f"http://127.0.0.1:{free_port()}/x"
    env = {"APP_URL": dead, "LLM_URL": dead, "QDRANT_URL": dead, "FAIL_LIMIT": "1", "STATE_DIR": str(state),
           "COMPOSE": str(fake), "DOCKER": str(fake), "LOG": str(tmp_path / "log"), "LLM_GRACE_S": "0"}
    (state / "pause").touch()
    sh(["bash", str(DEPLOY / "bin" / "watchdog.sh")], env=env, check=False)
    assert not (tmp_path / "calls").exists()
    assert "paused by" in (tmp_path / "log").read_text()
    old = 1_000_000_000
    os.utime(state / "pause", (old, old))                   # a crashed bench run must not disable it forever
    sh(["bash", str(DEPLOY / "bin" / "watchdog.sh")], env=env, check=False)
    assert "restart app" in (tmp_path / "calls").read_text()


def _fake_docker(tmp_path: Path, present: set[str]) -> Path:
    fake = tmp_path / "docker"
    fake.write_text(f"""#!/bin/sh
echo "$@" >> {tmp_path}/docker-calls
if [ "$1" = compose ]; then
  for a in "$@"; do [ "$a" = "--images" ] && {{ printf 'vllm/vllm-openai:v0.28.0\\nqdrant/qdrant:v1.15.4\\neclipse-mosquitto:2\\nwarroom-app:latest\\n'; exit 0; }}; done
  exit 0
fi
if [ "$1" = image ] && [ "$2" = inspect ]; then
  case " {' '.join(sorted(present))} " in *" $3 "*) exit 0 ;; esac
  exit 1
fi
""")
    fake.chmod(0o755)
    return fake


def test_install_pulls_and_builds_images_before_enabling_units(tmp_path):
    root, sysd = tmp_path / "opt", tmp_path / "systemd"
    sysd.mkdir()
    docker = _fake_docker(tmp_path, {"vllm/vllm-openai:v0.28.0", "qdrant/qdrant:v1.15.4", "eclipse-mosquitto:2",
                                     "warroom-app:latest"})
    r = sh(["bash", str(DEPLOY / "bin" / "install_spark.sh"), "t1"],
           env={"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(sysd), "NO_SYSTEMCTL": "1", "DOCKER": str(docker)},
           timeout=120)
    calls = (tmp_path / "docker-calls").read_text()
    # pull every non-buildable service (mqtt was added later; a hard-coded list missed it), not a fixed list
    assert "pull --ignore-buildable" in calls and "build app" in calls
    assert calls.index("pull --ignore-buildable") < calls.index("config --images")
    assert r.stdout.count("image ok:") == 4


def test_install_pulls_every_service_compose_lists():
    import yaml
    compose = yaml.safe_load((DEPLOY / "spark" / "docker-compose.yml").read_text())
    pullable = {n for n, s in compose["services"].items() if "build" not in s}
    assert {"vllm", "qdrant", "mqtt"} <= pullable
    text = (DEPLOY / "bin" / "install_spark.sh").read_text()
    assert "pull --ignore-buildable" in text          # covers whatever services compose gains later


def test_bundle_carries_mosquitto_by_default():
    text = (DEPLOY / "update" / "make_bundle.sh").read_text()
    assert "${IMAGES-eclipse-mosquitto:2}" in text


def test_install_fails_when_an_image_is_missing(tmp_path):
    root, sysd = tmp_path / "opt", tmp_path / "systemd"
    sysd.mkdir()
    docker = _fake_docker(tmp_path, {"qdrant/qdrant:v1.15.4", "warroom-app:latest"})
    r = sh(["bash", str(DEPLOY / "bin" / "install_spark.sh"), "t1"],
           env={"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(sysd), "NO_SYSTEMCTL": "1", "DOCKER": str(docker)},
           check=False, timeout=120)
    assert r.returncode != 0 and "MISSING: vllm/vllm-openai:v0.28.0" in r.stderr
    assert not list(sysd.iterdir())                         # units not linked/enabled


# ---------------------------------------------------------------- offline update + rollback
def make_bundle(tmp_path: Path, ver: str) -> Path:
    r = sh(["bash", str(DEPLOY / "update" / "make_bundle.sh"), ver, str(tmp_path / "usb")],
           env={"SKIP_IMAGES": "1", "FROM_WORKTREE": "1"}, timeout=120)
    p = Path(r.stdout.strip().splitlines()[-1])
    assert p.exists()
    return p


def test_offline_update_and_one_click_rollback(tmp_path, healthy_url):
    root = tmp_path / "opt"
    env = {"WARROOM_ROOT": str(root), "RESTART_CMD": "true", "HEALTH_URL": healthy_url, "HEALTH_TIMEOUT_S": "10"}
    upd, rb = str(DEPLOY / "update" / "offline_update.sh"), str(DEPLOY / "update" / "rollback.sh")
    b1, b2 = make_bundle(tmp_path, "v1"), make_bundle(tmp_path, "v2")

    sh(["bash", upd, str(b1)], env=env)
    assert os.readlink(root / "current") == str(root / "releases" / "v1")
    assert (root / "current" / "deploy" / "spark" / "vllm-serve.sh").exists()
    assert "APP_IMAGE=warroom-app:v1" in (root / "current" / "deploy" / "spark" / ".env").read_text()

    # operator edits .env on the box; the next release keeps it, only APP_IMAGE moves
    envf = root / "releases" / "v1" / "deploy" / "spark" / ".env"
    envf.write_text(envf.read_text() + "LAN_BIND=192.168.10.5\n")
    sh(["bash", upd, str(b2)], env=env)
    assert os.readlink(root / "current") == str(root / "releases" / "v2")
    e2 = (root / "current" / "deploy" / "spark" / ".env").read_text()
    assert "APP_IMAGE=warroom-app:v2" in e2 and "LAN_BIND=192.168.10.5" in e2

    sh(["bash", rb], env=env)                                   # one click
    assert os.readlink(root / "current") == str(root / "releases" / "v1")
    sh(["bash", rb, "v2"], env=env)                             # or to a named release
    assert os.readlink(root / "current") == str(root / "releases" / "v2")


def test_offline_update_auto_rolls_back_when_unhealthy(tmp_path, healthy_url):
    root = tmp_path / "opt"
    ok_env = {"WARROOM_ROOT": str(root), "RESTART_CMD": "true", "HEALTH_URL": healthy_url, "HEALTH_TIMEOUT_S": "10"}
    sh(["bash", str(DEPLOY / "update" / "offline_update.sh"), str(make_bundle(tmp_path, "good"))], env=ok_env)
    bad = make_bundle(tmp_path, "bad")
    # new release never becomes healthy -> offline_update hands over to rollback.sh -> back on "good".
    # (the fake probe stays dead, so rollback.sh also reports unhealthy: exit 5; on the box it would be 4)
    probe_env = {**ok_env, "HEALTH_TIMEOUT_S": "2", "HEALTH_URL": f"http://127.0.0.1:{free_port()}/api/health"}
    r = sh(["bash", str(DEPLOY / "update" / "offline_update.sh"), str(bad)], env=probe_env, check=False)
    assert r.returncode in (4, 5)
    assert os.readlink(root / "current") == str(root / "releases" / "good")
    assert "rolling back" in r.stderr


def test_offline_update_rejects_tampered_bundle(tmp_path, healthy_url):
    import tarfile
    b = make_bundle(tmp_path, "v9")
    x = tmp_path / "x"
    x.mkdir()
    with tarfile.open(b) as t:
        t.extractall(x, filter="data")
    (x / "MANIFEST").write_text("version=evil\n")
    tampered = tmp_path / "tampered.tar"
    with tarfile.open(tampered, "w") as t:
        for p in x.iterdir():
            t.add(p, arcname=p.name)
    root = tmp_path / "opt"
    r = sh(["bash", str(DEPLOY / "update" / "offline_update.sh"), str(tampered)],
           env={"WARROOM_ROOT": str(root), "RESTART_CMD": "true", "HEALTH_URL": healthy_url}, check=False)
    assert r.returncode == 3 and not (root / "current").exists()


def test_install_script_layout(tmp_path):
    root = tmp_path / "opt"
    sysd = tmp_path / "systemd"
    sysd.mkdir()
    sh(["bash", str(DEPLOY / "bin" / "install_spark.sh"), "t1"],
       env={"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(sysd), "NO_SYSTEMCTL": "1", "NO_IMAGES": "1"},
       timeout=120)
    assert os.readlink(root / "current") == str(root / "releases" / "t1")
    for d in ("data/runtime", "data/qdrant", "hf"):
        assert (root / d).is_dir()
    assert (root / "current" / "deploy" / "spark" / ".env").exists()
    assert {p.name for p in sysd.iterdir()} >= {"warroom-stack.service", "warroom-watchdog.service"}


# ---------------------------------------------------------------- write access on the Spark stack
def test_spark_app_on_host_network_so_the_touchscreen_is_loopback():
    """Docker port publishing rewrites the client address (bridge gateway / 192.168.65.1), so behind `ports:`
    the on-box touchscreen was never loopback and got 403 on every approval. Host network keeps real addresses."""
    app = yaml.safe_load((DEPLOY / "spark" / "docker-compose.yml").read_text())["services"]["app"]
    assert app["network_mode"] == "host"
    assert "ports" not in app and "networks" not in app
    env = app["environment"]
    assert "WARROOM_LOCAL_HOSTS" not in env                 # never trust a rewritten source IP as "local"
    assert env["WARROOM_SUPERVISOR_PIN"].startswith("${WARROOM_SUPERVISOR_PIN:?")   # compose refuses to start without it
    assert env["WARROOM_MQTT_URL"] == "mqtt://${MQTT_BIND:-127.0.0.1}:1883"
    assert "vllm:8000" not in (DEPLOY / "bin" / "preload.sh").read_text()   # service names do not resolve on host net
    assert re.search(r"^WARROOM_SUPERVISOR_PIN=$", (DEPLOY / "spark" / ".env.example").read_text(), re.M)


def _install(tmp_path: Path, **env: str):
    root, sysd = tmp_path / "opt", tmp_path / "systemd"
    sysd.mkdir(exist_ok=True)
    return root, sysd, sh(["bash", str(DEPLOY / "bin" / "install_spark.sh"), "t1"],
                          env={"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(sysd), "NO_SYSTEMCTL": "1",
                               "NO_IMAGES": "1", "NO_MODELS": "1", **env}, check=False, timeout=120)


@pytest.mark.parametrize("pin", ["", "short7x", "has space1", "dollar$sign"])
def test_install_refuses_without_a_usable_supervisor_pin(tmp_path, pin):
    root, sysd, r = _install(tmp_path, WARROOM_SUPERVISOR_PIN=pin)
    assert r.returncode != 0 and "WARROOM_SUPERVISOR_PIN" in r.stderr and "install aborted" in r.stderr
    assert not (root / "current").exists() and not list(sysd.iterdir())


def test_install_writes_the_given_pin_into_env_and_keeps_it(tmp_path):
    root, _, r = _install(tmp_path, WARROOM_SUPERVISOR_PIN="Abc123xyz9")
    assert r.returncode == 0, r.stderr
    envf = root / "current" / "deploy" / "spark" / ".env"
    lines = [ln for ln in envf.read_text().splitlines() if ln.startswith("WARROOM_SUPERVISOR_PIN=")]
    assert lines == ["WARROOM_SUPERVISOR_PIN=Abc123xyz9"]
    assert envf.stat().st_mode & 0o077 == 0                  # secret: owner-only
    _, _, r2 = _install(tmp_path, WARROOM_SUPERVISOR_PIN="")  # rerun without the env var: .env's PIN is kept
    assert r2.returncode == 0, r2.stderr


@pytest.mark.skipif(os.environ.get("WARROOM_DOCKER_TESTS") != "1", reason="set WARROOM_DOCKER_TESTS=1 (needs docker + an app image)")
def test_live_compose_app_touchscreen_and_phone_can_approve(tmp_path):
    """Brings up the Spark compose `app` service itself (no vLLM: mock commander), then runs
    deploy/spark/smoke_access.sh from a host-network container: loopback approves a real pending request
    with no PIN, the LAN IP gets 401 without the PIN and through with it. APP_IMAGE picks the image."""
    image = os.environ.get("APP_IMAGE", "warroom-app:latest")
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o777)
    env = {"WARROOM_SUPERVISOR_PIN": TEST_PIN, "APP_IMAGE": image, "APP_DATA": str(data), "LLM_MODE": "mock",
           "SCENARIO": "open_mill_overheat"}
    compose = ["docker", "compose", "-p", "warroom-accesstest", "--project-directory", str(DEPLOY / "spark")]
    try:
        sh([*compose, "up", "-d", "--no-deps", "--no-build", "app"], env=env, timeout=180)
        r = sh(["docker", "run", "--rm", "--network", "host", "-v", f"{DEPLOY / 'spark' / 'smoke_access.sh'}:/smoke.sh:ro",
                "-e", f"WARROOM_SUPERVISOR_PIN={TEST_PIN}", "-e", "APPROVE=1", "-e", "WAIT_S=120",
                "--entrypoint", "bash", image, "/smoke.sh"], check=False, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "ok   touchscreen 127.0.0.1, no PIN: 404" in r.stdout
        assert re.search(r"ok   phone \S+, no PIN: 401", r.stdout)
        assert re.search(r"ok   touchscreen approves \S+: 200", r.stdout)
    finally:
        sh([*compose, "down"], env=env, check=False, timeout=120)
