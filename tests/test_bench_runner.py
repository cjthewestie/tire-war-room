"""The benches must run on the DGX Spark host: no uv there, PyPI blocked by the boot firewall.

bench/py.sh picks the interpreter (WARROOM_PY, then /opt/warroom/venv built by install_spark.sh, then uv),
bench/spark_ablation.sh goes through it, and .python-version carries no platform (a macOS pin made uv fail on
linux/arm64 with "No interpreter found" / "Exec format error").
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_SH = ROOT / "bench" / "py.sh"
ABLATION = ROOT / "bench" / "spark_ablation.sh"
INSTALL = ROOT / "deploy" / "bin" / "install_spark.sh"


def run(cmd, env=None, cwd=ROOT, timeout=120):
    e = os.environ.copy()
    e.update(env or {})
    return subprocess.run(cmd, capture_output=True, text=True, env=e, cwd=cwd, timeout=timeout)


def fake_python(path: Path, log: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/usr/bin/env bash\necho "$@ FASTEMBED=${{FASTEMBED_CACHE_PATH:-}} HF=${{HF_HUB_OFFLINE:-}}" >> {log}\n'
                    f'exec {sys.executable} "$@"\n')
    path.chmod(0o755)
    return path


def test_python_version_has_no_platform_pin():
    pin = (ROOT / ".python-version").read_text().strip()
    assert pin == "3.12", pin                     # not cpython-3.12-macos-aarch64-none


def test_py_sh_prefers_override_then_spark_venv(tmp_path):
    log = tmp_path / "calls"
    override = fake_python(tmp_path / "override" / "python", log)
    r = run([str(PY_SH), "-c", "print('hi')"], env={"WARROOM_PY": str(override), "WARROOM_ROOT": str(tmp_path / "x")})
    assert r.returncode == 0 and r.stdout.strip() == "hi"
    root = tmp_path / "opt"
    fake_python(root / "venv" / "bin" / "python", log)
    (root / "fastembed").mkdir()
    env = {"WARROOM_ROOT": str(root), "WARROOM_PY": "", "FASTEMBED_CACHE_PATH": "", "HF_HUB_OFFLINE": ""}
    r = run([str(PY_SH), "-c", "import bench.ttft; print('ok')"], env={**env, "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr
    last = log.read_text().strip().splitlines()[-1]
    assert f"FASTEMBED={root / 'fastembed'}" in last and "HF=1" in last


def test_py_sh_without_any_interpreter_says_how_to_fix(tmp_path):
    r = run([str(PY_SH), "-c", "pass"], env={"WARROOM_ROOT": str(tmp_path), "WARROOM_PY": "", "PATH": "/usr/bin:/bin"})
    assert r.returncode == 127 and "install_spark.sh" in r.stderr


def test_ablation_uses_py_sh_not_uv():
    text = ABLATION.read_text()
    assert "uv run" not in text
    assert text.count('"$PY" -m bench.') >= 6


def test_ablation_preflight_fails_before_touching_vllm(tmp_path):
    bad = tmp_path / "py"
    bad.write_text("#!/usr/bin/env bash\nexit 1\n")
    bad.chmod(0o755)
    docker_log = tmp_path / "docker-calls"
    fake_docker = tmp_path / "bin" / "docker"
    fake_docker.parent.mkdir()
    fake_docker.write_text(f'#!/usr/bin/env bash\necho "$@" >> {docker_log}\n')
    fake_docker.chmod(0o755)
    r = run(["bash", str(ABLATION)], env={"PY": str(bad), "PATH": f"{fake_docker.parent}:{os.environ['PATH']}"})
    assert r.returncode == 1 and "bench interpreter not usable" in r.stderr
    assert not docker_log.exists()                 # vllm never restarted
    r = run(["bash", str(ABLATION)], env={"PY": str(PY_SH), "PREFLIGHT_ONLY": "1"})
    assert r.returncode == 0 and "preflight ok" in r.stdout, r.stderr


def _fake_uv(path: Path, log: Path) -> Path:
    """Stands in for `uv sync`: records its args/env and lays down $UV_PROJECT_ENVIRONMENT/bin/python."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""#!/usr/bin/env bash
echo "$PWD|$@|$UV_PROJECT_ENVIRONMENT|$UV_CACHE_DIR" >> {log}
mkdir -p "$UV_PROJECT_ENVIRONMENT/bin"
printf '#!/usr/bin/env bash\\nexec {sys.executable} "$@"\\n' > "$UV_PROJECT_ENVIRONMENT/bin/python"
chmod +x "$UV_PROJECT_ENVIRONMENT/bin/python"
""")
    path.chmod(0o755)
    return path


def test_install_builds_bench_venv_offline_capable(tmp_path):
    root, sysd, log = tmp_path / "opt", tmp_path / "systemd", tmp_path / "uv-calls"
    sysd.mkdir()
    uv = _fake_uv(tmp_path / "uvbin" / "uv", log)
    r = run(["bash", str(INSTALL), "t1"],
            env={"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(sysd), "NO_SYSTEMCTL": "1", "NO_IMAGES": "1",
                 "NO_MODELS": "1", "NO_BENCH_VENV": "0", "NO_NAT_VENV": "1", "SKIP_PULL": "1", "WARROOM_SUPERVISOR_PIN": "a1b2c3d4e5", "UV": str(uv)}, timeout=240)
    assert r.returncode == 0, r.stderr
    cwd, args, venv, cache = log.read_text().strip().split("|")
    assert Path(cwd).resolve() == (root / "releases" / "t1").resolve()
    assert "sync --frozen --no-dev --no-install-project --offline" in args
    assert venv == str(root / "venv") and cache == str(root / ".uv-cache")
    assert "bench venv ok" in r.stdout
    # the release's own runner now resolves to that venv
    r = run([str(root / "current" / "bench" / "py.sh"), "-c", "import sys; print(sys.prefix)"],
            env={"WARROOM_ROOT": str(root), "WARROOM_PY": "", "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr


def test_install_copies_uv_from_image_when_host_has_none(tmp_path):
    root, sysd, log = tmp_path / "opt", tmp_path / "systemd", tmp_path / "uv-calls"
    sysd.mkdir()
    src_uv = _fake_uv(tmp_path / "image" / "uv", log)
    docker = tmp_path / "docker"
    docker.write_text(f"""#!/usr/bin/env bash
echo "$@" >> {tmp_path}/docker-calls
case "$1" in
  create) echo cid123 ;;
  cp) cp {src_uv} "$3" ;;
  rm) ;;
esac
""")
    docker.chmod(0o755)
    r = run(["bash", str(INSTALL), "t1"],
            env={"WARROOM_ROOT": str(root), "SYSTEMD_DIR": str(sysd), "NO_SYSTEMCTL": "1", "NO_IMAGES": "1",
                 "NO_MODELS": "1", "NO_BENCH_VENV": "0", "NO_NAT_VENV": "1", "SKIP_PULL": "1", "WARROOM_SUPERVISOR_PIN": "a1b2c3d4e5", "DOCKER": str(docker),
                 "UV": "", "PATH": "/usr/bin:/bin"}, timeout=240)
    assert r.returncode == 0, r.stderr
    calls = (tmp_path / "docker-calls").read_text()
    assert "create ghcr.io/astral-sh/uv:0.9" in calls and "cp cid123:/uv" in calls and "rm cid123" in calls
    assert (root / "bin" / "uv").exists() and log.exists()


# ---------------------------------------------------------------- page-10 control group: weights + exit status
NVFP4 = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4"
DSPARK = NVFP4 + "-DSpark"
BF16 = "nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16"


def _weights(cache: Path, *ids: str) -> None:
    for m in ids:
        snap = cache / "hub" / ("models--" + m.replace("/", "--")) / "snapshots" / "r1"
        snap.mkdir(parents=True, exist_ok=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors").write_text("x")


def _ablation_env(tmp_path: Path, cache: Path) -> dict:
    """Fake compose records QUANT and the config; fake curl is healthy unless the served QUANT is fp8."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    state, log = tmp_path / "quant", tmp_path / "compose-calls"
    compose = bindir / "fake-compose"
    compose.write_text(f'#!/usr/bin/env bash\necho "QUANT=${{QUANT:-prod}} $@" >> {log}\n'
                       f'printf %s "${{QUANT:-prod}}" > {state}\n')
    compose.chmod(0o755)
    curl = bindir / "curl"
    curl.write_text(f'#!/usr/bin/env bash\n[[ "$(cat {state} 2>/dev/null)" != fp8 ]]\n')
    curl.chmod(0o755)
    py = bindir / "py"                       # every bench "succeeds" and writes nothing
    py.write_text("#!/usr/bin/env bash\nexit 0\n")
    py.chmod(0o755)
    return {"PY": str(py), "COMPOSE": str(compose), "HF_CACHE": str(cache), "WAIT_TRIES": "2", "WAIT_SLEEP": "0",
            "PAUSE_FILE": str(tmp_path / "no-such-dir" / "pause"), "PATH": f"{bindir}:{os.environ['PATH']}"}


def _cleanup_results(before: set) -> None:
    import shutil
    for d in set((ROOT / "bench" / "results").glob("spark-ablation-*")) - before:
        shutil.rmtree(d, ignore_errors=True)


def test_ablation_refuses_control_without_bf16_before_touching_vllm(tmp_path):
    cache = tmp_path / "hf"
    _weights(cache, NVFP4, DSPARK)                    # production only: the default install before the fix
    env = _ablation_env(tmp_path, cache)
    before = set((ROOT / "bench" / "results").glob("spark-ablation-*"))
    try:
        r = run(["bash", str(ABLATION)], env=env)
        assert r.returncode == 1, r.stderr
        assert f"weights MISSING: {BF16}" in r.stderr and "WITH_CONTROL=1" in r.stderr
        assert not (tmp_path / "compose-calls").exists()        # vllm never recreated
        # without control in CONFIGS the production weights are enough
        r = run(["bash", str(ABLATION)], env={**env, "CONFIGS": "full"})
        assert r.returncode == 0, r.stderr
    finally:
        _cleanup_results(before)


def test_ablation_exits_nonzero_when_a_config_never_comes_up(tmp_path):
    cache = tmp_path / "hf"
    _weights(cache, NVFP4, DSPARK, BF16)
    env = _ablation_env(tmp_path, cache)
    before = set((ROOT / "bench" / "results").glob("spark-ablation-*"))
    try:
        r = run(["bash", str(ABLATION)], env={**env, "CONFIGS": "full control"})
        assert r.returncode == 1
        assert "vllm did not come up for control" in r.stderr and "FAILED: vllm never came up for: control" in r.stderr
        assert "accuracy gap has no Spark number" in r.stderr
        calls = (tmp_path / "compose-calls").read_text().splitlines()
        assert calls[0].startswith("QUANT=nvfp4") and calls[1].startswith("QUANT=fp8") and calls[-1].startswith("QUANT=prod")
        r = run(["bash", str(ABLATION)], env={**env, "CONFIGS": "full bogus"})
        assert r.returncode == 2 and "unknown config 'bogus'" in r.stderr
    finally:
        _cleanup_results(before)
