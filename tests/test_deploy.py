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
    assert s["vllm"]["ports"] == ["127.0.0.1:8001:8000"]          # model server never on the LAN
    assert s["qdrant"]["ports"] == ["127.0.0.1:6333:6333"]
    assert s["qdrant"]["mem_limit"] == f"{MEMORY_BUDGET_GB['qdrant']}g"
    assert s["app"]["mem_limit"] == f"{MEMORY_BUDGET_GB['sched_app']}g"
    env = s["app"]["environment"]
    assert env["BASE_URL"] == "http://vllm:8000/v1" and env["MODEL"] == "nemotron"
    assert env["QDRANT_URL"] == "http://qdrant:6333"
    assert env["LLM_FALLBACK_TO_MOCK"] == "true"
    assert s["vllm-fp8"]["profiles"] == ["control"]
    assert s["vllm-fp8"]["environment"]["QUANT"] == "fp8" and s["vllm-fp8"]["environment"]["SPEC"] == "off"
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
  for a in "$@"; do [ "$a" = "--images" ] && {{ printf 'vllm/vllm-openai:v0.28.0\\nqdrant/qdrant:v1.15.4\\nwarroom-app:latest\\n'; exit 0; }}; done
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
    docker = _fake_docker(tmp_path, {"vllm/vllm-openai:v0.28.0", "qdrant/qdrant:v1.15.4", "warroom-app:latest"})
    r = sh(["bash", str(DEPLOY / "bin" / "install_spark.sh"), "t1"],
           env={"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(sysd), "NO_SYSTEMCTL": "1", "DOCKER": str(docker)},
           timeout=120)
    calls = (tmp_path / "docker-calls").read_text()
    assert "pull vllm qdrant" in calls and "build app" in calls
    assert calls.index("pull vllm qdrant") < calls.index("config --images")
    assert r.stdout.count("image ok:") == 3


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
