"""Unplug test (page 9: 拔網路線 → 2 秒內告警、對外連線 0 次).

Steps
 1. Block outbound traffic (--block): Linux applies deploy/network/nftables-warroom.nft (or --block-cmd,
    e.g. "ip link set eth1 down"); macOS loads deploy/network/pf-warroom.conf into a pf anchor. Needs root.
    Without root the test still runs and says the block was NOT applied.
 2. While a socket watcher samples every 100 ms, trigger an anomaly and time the first alert card:
      --app-url http://127.0.0.1:8000   real stack: POST /api/sim/control scenario, wait on /ws for
                                        `anomaly` then `alert_card`; also checks /api/health and /phone (LAN path)
      (default)                         direct: alert-card call against the local LLM endpoint
 3. Count outbound connections from the watched processes (model server, qdrant, app, + --pid): any remote
    address that is not loopback / RFC1918 / link-local / --lan-cidr counts. Target 0.
 4. Unblock (always, in finally).

  uv run python -m bench.unplug_test                         # Mac: direct, socket check only
  sudo -E uv run python -m bench.unplug_test --block --app-url http://127.0.0.1:8000     # Spark
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import platform
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, run, save_result, stream_chat
from bench.memory_check import role_of
from bench.prompts import ANOMALIES, alert_card_messages, card_schema
from warroom.factory import ALERT_CARD_MAX_TOKENS, LATENCY_FIRST_ALERT_S

ROOT = Path(__file__).resolve().parent.parent
NFT = ROOT / "deploy" / "network" / "nftables-warroom.nft"
PF = ROOT / "deploy" / "network" / "pf-warroom.conf"


def is_outbound(ip: str, lan: list[ipaddress._BaseNetwork] | None = None) -> bool:
    """True when a remote address leaves the factory: not loopback/private/link-local/multicast/LAN."""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_private or a.is_link_local or a.is_multicast or a.is_unspecified:
        return False
    return not any(a in n for n in (lan or []))


class SocketWatcher:
    """Samples inet connections of the watched processes; records every outbound remote seen."""

    def __init__(self, pids: set[int] | None = None, lan: list | None = None, interval_s: float = 0.1,
                 match_roles: bool = True):
        self.pids = set(pids or ())
        self.lan = lan or []
        self.interval_s = interval_s
        self.match_roles = match_roles
        self.outbound: dict[tuple[int, str, int], str] = {}
        self.samples = 0
        self.watched: set[int] = set()
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def targets(self) -> set[int]:
        pids = set(self.pids)
        if self.match_roles:
            for p in psutil.process_iter(["pid", "cmdline", "name"]):
                try:
                    if role_of(p.info["name"], p.info["cmdline"]):
                        pids.add(p.info["pid"])
                except psutil.Error:
                    continue
        return pids

    def sample(self) -> None:
        for pid in self.targets():
            try:
                conns = psutil.Process(pid).net_connections(kind="inet")
            except psutil.AccessDenied:
                if f"access denied pid {pid}" not in self.errors:
                    self.errors.append(f"access denied pid {pid}")
                continue
            except psutil.Error:
                continue
            self.watched.add(pid)
            for c in conns:
                if c.raddr and is_outbound(c.raddr.ip, self.lan):
                    self.outbound[(pid, c.raddr.ip, c.raddr.port)] = c.status
        self.samples += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.interval_s)

    def __enter__(self):
        self.sample()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=2)

    def report(self) -> dict[str, Any]:
        return {"samples": self.samples, "watched_pids": sorted(self.watched),
                "outbound_connections": len(self.outbound),
                "outbound": [{"pid": k[0], "ip": k[1], "port": k[2], "status": v} for k, v in self.outbound.items()],
                "errors": self.errors}


def block_commands(custom_block: str | None, custom_unblock: str | None) -> tuple[list[str], list[str]]:
    if custom_block:
        return ["sh", "-c", custom_block], ["sh", "-c", custom_unblock or "true"]
    if platform.system() == "Linux":
        return ["nft", "-f", str(NFT)], ["nft", "delete", "table", "inet", "warroom_offline"]
    if platform.system() == "Darwin":
        # stock /etc/pf.conf does not reference our anchor, so load main ruleset + anchor together
        load = (f"(cat /etc/pf.conf; echo 'anchor \"warroom_offline\"'; "
                f"echo 'load anchor \"warroom_offline\" from \"{PF}\"') | pfctl -E -f -")
        return ["sh", "-c", load], ["sh", "-c", "pfctl -a warroom_offline -F all; pfctl -f /etc/pf.conf"]
    return [], []


def apply(cmd: list[str]) -> tuple[bool, str]:
    if not cmd:
        return False, "no block command for this platform"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stderr or r.stdout).strip()[:500]
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def internet_reachable(timeout_s: float = 2.0) -> bool:
    """Probe: does a TCP connect to a public address succeed? (after blocking it must not)"""
    import socket
    for host in ("1.1.1.1", "8.8.8.8"):
        try:
            with socket.create_connection((host, 443), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


async def direct_alert(ep: Endpoint, runs: int) -> list[float | None]:
    out = []
    cli = ep.client()
    for i in range(runs):
        r = await stream_chat(ep, alert_card_messages(ANOMALIES[i % 2]), max_tokens=ALERT_CARD_MAX_TOKENS,
                              response_format=json_schema_format("AlertCard", card_schema()), client=cli)
        out.append(round(r.total_s, 4) if r.ok else None)
    await cli.close()
    return out


async def app_alert(app_url: str, scenario: str, timeout_s: float, speed: float) -> dict[str, Any]:
    """Real stack: trigger scenario, time anomaly -> alert_card on /ws, check health + phone page."""
    import websockets
    res: dict[str, Any] = {}
    async with httpx.AsyncClient(base_url=app_url, timeout=5) as c:
        h = await c.get("/api/health")
        res["health"] = h.json() if h.status_code == 200 else h.status_code
        ph = await c.get("/phone")
        res["phone_page_status"] = ph.status_code
        ws_url = re.sub(r"^http", "ws", app_url) + "/ws"
        async with websockets.connect(ws_url, max_size=None) as ws:
            await c.post("/api/sim/control", json={"action": "scenario", "scenario": scenario})
            await c.post("/api/sim/control", json={"action": "speed", "speed": speed})
            await c.post("/api/sim/control", json={"action": "play"})
            t_anom = None
            deadline = time.perf_counter() + timeout_s
            while time.perf_counter() < deadline:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.perf_counter()))
                except asyncio.TimeoutError:
                    break
                if msg.get("type") == "anomaly" and t_anom is None:
                    t_anom = time.perf_counter()
                    res["anomaly"] = msg["data"].get("message")
                if msg.get("type") == "alert_card" and t_anom is not None:
                    res["alert_after_anomaly_s"] = round(time.perf_counter() - t_anom, 4)
                    res["card"] = msg["data"].get("card")
                    break
    return res


async def main_async(args) -> dict[str, Any]:
    lan = [ipaddress.ip_network(c) for c in (args.lan_cidr or [])]
    ep = Endpoint.from_env(args.base_url, args.model)
    out: dict[str, Any] = {"platform": platform.platform(), "is_root": os.geteuid() == 0}
    blk, unblk = block_commands(args.block_cmd, args.unblock_cmd)
    out["block"] = {"requested": args.block, "applied": False}
    if args.block:
        ok, msg = apply(blk)
        out["block"].update(applied=ok, cmd=" ".join(blk), msg=msg)
    try:
        out["internet_reachable_during_test"] = internet_reachable()
        await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)   # model resident
        with SocketWatcher(set(args.pid or []), lan) as w:
            if args.app_url:
                out["endpoint"] = await endpoint_info(ep)
                out["app"] = await app_alert(args.app_url, args.scenario, args.timeout, args.speed)
                alert_s = out["app"].get("alert_after_anomaly_s")
            else:
                out["endpoint"] = await endpoint_info(ep)
                out["direct_alert_s"] = await direct_alert(ep, args.runs)
                vals = [v for v in out["direct_alert_s"] if v is not None]
                alert_s = max(vals) if vals else None
            time.sleep(0.3)
        out["sockets"] = w.report()
    finally:
        if args.block and out["block"]["applied"]:
            ok, msg = apply(unblk)
            out["block"]["unblocked"] = ok
    outbound = out["sockets"]["outbound_connections"]
    out["verdicts"] = [
        Verdict("alert_offline", f"≤ {LATENCY_FIRST_ALERT_S} s", alert_s,
                None if alert_s is None else alert_s <= LATENCY_FIRST_ALERT_S,
                note=("via app /ws (anomaly -> alert_card)" if args.app_url else "direct alert-card call")
                + ("" if alert_s is not None else ": no alert_card seen before timeout")).__dict__,
        Verdict("outbound_connections", "0", outbound, outbound == 0,
                note="" if out["block"]["applied"] else "network NOT blocked (needs root); socket count only").__dict__,
    ]
    if args.block and out["block"]["applied"]:
        out["verdicts"].append(Verdict("internet_blocked", "unreachable", out["internet_reachable_during_test"],
                                       not out["internet_reachable_during_test"]).__dict__)
    out["saved_to"] = str(save_result("unplug", ep, out, args.out_dir))
    return out


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--block", action="store_true")
    p.add_argument("--block-cmd")
    p.add_argument("--unblock-cmd")
    p.add_argument("--app-url")
    p.add_argument("--scenario", default="press7_temp_drop")
    p.add_argument("--speed", type=float, default=60.0)
    p.add_argument("--timeout", type=float, default=180.0)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--pid", type=int, action="append")
    p.add_argument("--lan-cidr", action="append")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    for v in out["verdicts"]:
        print(f"{v['name']:<22} target {v['target']:<8} measured {v['measured']}  pass={v['passed']}  {v['note']}")
    print("block:", out["block"], " watched pids:", out["sockets"]["watched_pids"])
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
