"""A watchdog restart of the app must resume, not replay the scenario from scratch.

`docker compose restart app` sends SIGTERM; the app's clean stop() removes running.json, so without
deploy/bin/watchdog.sh dropping resume.request first, the restarted app wiped the approved schedule,
the press-7 hold and the factory clock, and replayed the 02:14 event. This drives the real server
process end to end: page-11 scenario, approve round 1, wait for the safety hold, let the watchdog
restart it (its fake compose sends the SIGTERM), start it again and check that nothing was lost.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "deploy" / "bin" / "watchdog.sh"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=5) as r:
        return json.loads(r.read())


def _post(base: str, path: str, body: dict):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _wait(pred, timeout: float, what: str):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        try:
            last = pred()
            if last:
                return last
        except Exception as e:  # noqa: BLE001 - server still coming up
            last = e
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}: {last!r}")


def _serve(tmp: Path, port: int, log_name: str) -> subprocess.Popen:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WARROOM_")}
    env.update({"AUDIT_PATH": str(tmp / "audit.jsonl"), "SCHEDULE_DIR": str(tmp / "sched"),
                "LLM_MODE": "mock", "WARROOM_INGEST": "direct", "WARROOM_DEMO_CONTROLS": "1",
                "QDRANT_PATH": str(tmp / "qdrant")})
    env.pop("WARROOM_SUPERVISOR_PIN", None)
    log = open(tmp / log_name, "w")  # noqa: SIM115 - closed with the process
    return subprocess.Popen([sys.executable, "-m", "warroom.api", "serve", "--scenario", "press7_temp_drop",
                             "--port", str(port), "--host", "127.0.0.1", "--speed", "30"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def _stop(p: subprocess.Popen) -> None:
    if p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(20)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)


@pytest.mark.timeout(240)
def test_sigterm_restart_by_watchdog_keeps_schedule_holds_and_clock(tmp_path):
    port = _port()
    base = f"http://127.0.0.1:{port}"
    p1 = _serve(tmp_path, port, "serve1.log")
    try:
        _wait(lambda: _get(base, "/api/health")["sim_running"], 60, "server up")

        def approve_first():
            pend = _get(base, "/api/approvals?status=pending")
            if not pend:
                return None
            req = pend[0]
            opt = (req.get("options") or [{}])[0].get("id")
            _post(base, f"/api/approvals/{req['id']}/decision",
                  {"request_id": req["id"], "decision": "approve", "option_id": opt,
                   "reason": "test", "via": "dashboard"})
            return req["id"]

        _wait(approve_first, 90, "first approval request")
        _wait(lambda: any(v["status"] == "active" and v["version"] >= 2
                          for v in _get(base, "/api/schedule/versions")), 60, "approved schedule active")
        holds = tmp_path / "sched" / "live" / "holds.json"     # the store root is schedule_dir/live
        _wait(lambda: holds.is_file() and "CURE-07" in holds.read_text(encoding="utf-8"), 120,
              "press-7 safety hold persisted")
        before_versions = {(v["version"], v["status"]) for v in _get(base, "/api/schedule/versions")}
        active_before = next(v for (v, s) in before_versions if s == "active")
        t_before = datetime.fromisoformat(_get(base, "/api/health")["factory_time"])

        # the watchdog sees a dead probe and restarts `app`; its compose stand-in sends the SIGTERM
        fake = tmp_path / "compose"
        fake.write_text(f"""#!/bin/sh
echo "$@" >> {tmp_path}/calls
[ -e {tmp_path}/sched/resume.request ] && echo present >> {tmp_path}/calls
kill -TERM {p1.pid}
""")
        fake.chmod(0o755)
        env = {**os.environ, "APP_URL": f"http://127.0.0.1:{_port()}/api/health", "LLM_URL": base + "/api/health",
               "QDRANT_URL": base + "/api/health", "FAIL_LIMIT": "1", "STATE_DIR": str(tmp_path / "wd"),
               "COMPOSE": str(fake), "LOG": str(tmp_path / "wd.log"), "LLM_GRACE_S": "0",
               "APP_SCHEDULE_DIR": str(tmp_path / "sched")}
        subprocess.run(["bash", str(WATCHDOG)], env=env, timeout=60, check=False)
        assert (tmp_path / "calls").read_text().split() == ["restart", "app", "present"]
        p1.wait(30)
        assert not (tmp_path / "sched" / "running.json").exists()      # clean stop really happened
    finally:
        _stop(p1)

    p2 = _serve(tmp_path, port, "serve2.log")
    try:
        _wait(lambda: _get(base, "/api/health")["ok"] is not None, 60, "server back up")
        after = {(v["version"], v["status"]) for v in _get(base, "/api/schedule/versions")}
        assert (1, "active") not in after and (active_before, "active") in after, after
        assert {v for v, _ in before_versions} <= {v for v, _ in after}
        assert "CURE-07" in holds.read_text(encoding="utf-8")
        press7 = _get(base, "/api/machines/CURE-07")
        assert press7["state"]["status"] != "running", press7["state"]          # still held, not back in service
        t_after = datetime.fromisoformat(_get(base, "/api/health")["factory_time"])
        assert t_after >= t_before                                       # clock continued, no 02:14 replay
        assert not (tmp_path / "sched" / "resume.request").exists()      # consumed
    finally:
        _stop(p2)
