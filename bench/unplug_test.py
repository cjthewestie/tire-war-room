"""Unplug test (page 9: 拔網路線 → 2 秒內告警、對外連線 0 次).

Steps
 1. Block outbound traffic (--block): Linux applies deploy/network/nftables-warroom.nft (or --block-cmd,
    e.g. "ip link set eth1 down"); macOS loads deploy/network/pf-warroom.conf into a pf anchor. Needs root.
    Without root the test still runs, says the block was NOT applied, and the egress verdict is None.
 2. Zero the firewall's drop counter (nft `dropped_egress`), then watch for a fixed window (--watch, default
    600 s = 10 min of normal running) plus one event:
      --app-url http://127.0.0.1:8000   real stack: POST /api/sim/control scenario, wait on /ws for `anomaly`,
                                        the instant detector card and the COMMANDER card (source="commander");
                                        the page-9 ≤ 2 s target is judged on the commander card. The commander's
                                        TTFT/total come from the event's `llm_call` audit entry. Also checks
                                        /api/health and /phone (LAN path).
      (default)                         direct: alert-card call against the local LLM endpoint
 3. Egress evidence, strongest first:
      - nft `dropped_egress` counter (Linux, block applied): packets the firewall dropped = attempts to leave
      - pf block-rule packet counter (macOS, block applied)
      - conntrack snapshots every second (Linux, root, `conntrack` installed): flows to non-LAN addresses
      - socket samples of the watched processes every --interval s. On Linux the sockets are read from
        /proc/<pid>/net/* (the process's OWN network namespace, so Docker containers are seen); psutil
        reads /proc/net/* of the caller's namespace and would miss every container socket.
    Watched processes: --pid, plus model server / qdrant / app processes that listen on a watched port
    (model port from --base-url, app port from --app-url, 6333/6334, --watch-port) and their children.
    Same-role processes on other ports (a user's own Ollama, another app's qdrant) are listed as ignored.
 4. Internet probe (must fail while blocked), then unblock (always, in finally).

  uv run python -m bench.unplug_test --watch 0               # Mac: direct, socket check only
  sudo -E uv run python -m bench.unplug_test --block --app-url http://127.0.0.1:8000     # Spark, 10 min
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import psutil

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, run, save_result, stream_chat
from bench.memory_check import role_of
from bench.prompts import ANOMALIES, alert_card_messages, card_schema
from warroom.factory import ALERT_CARD_MAX_TOKENS, LATENCY_FIRST_ALERT_S, TTFT_TARGET_S

ROOT = Path(__file__).resolve().parent.parent
NFT = ROOT / "deploy" / "network" / "nftables-warroom.nft"
PF = ROOT / "deploy" / "network" / "pf-warroom.conf"
NFT_COUNTER = ["inet", "warroom_offline", "dropped_egress"]
QDRANT_PORTS = (6333, 6334)
PROBE_HOSTS = ("1.1.1.1", "8.8.8.8")


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


# ----------------------------------------------------------------------------- Linux /proc, per netns
TCP_STATES = {"01": "ESTABLISHED", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2",
              "06": "TIME_WAIT", "07": "CLOSE", "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN",
              "0B": "CLOSING"}


def _hex_addr(h: str) -> tuple[str, int]:
    """Decode a /proc/net/tcp 'ADDR:PORT' (address words are host-endian = little-endian on x86/arm64)."""
    addr, port = h.split(":")
    raw = bytes.fromhex(addr)
    if len(raw) == 4:
        ip = ipaddress.IPv4Address(raw[::-1])
    else:
        ip = ipaddress.IPv6Address(b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
    return str(ip), int(port, 16)


def linux_inet_conns(pid: int, proc: Path = Path("/proc")) -> list[dict[str, Any]]:
    """Inet sockets owned by `pid`, read from /proc/<pid>/net/* (its own network namespace).

    psutil parses /proc/net/* = the CALLER's namespace, so a process in a Docker network namespace shows
    zero connections there. Raises PermissionError / FileNotFoundError like reading /proc would."""
    inodes: set[str] = set()
    fd_dir = proc / str(pid) / "fd"
    for fd in os.listdir(fd_dir):
        try:
            target = os.readlink(fd_dir / fd)
        except OSError:
            continue
        m = re.match(r"socket:\[(\d+)\]", target)
        if m:
            inodes.add(m.group(1))
    out: list[dict[str, Any]] = []
    for proto in ("tcp", "tcp6", "udp", "udp6"):
        f = proc / str(pid) / "net" / proto
        try:
            lines = f.read_text().splitlines()[1:]
        except OSError:
            continue
        for ln in lines:
            parts = ln.split()
            if len(parts) < 10 or parts[9] not in inodes:
                continue
            lip, lport = _hex_addr(parts[1])
            rip, rport = _hex_addr(parts[2])
            status = TCP_STATES.get(parts[3].upper(), parts[3]) if proto.startswith("tcp") else "NONE"
            out.append({"proto": proto, "lip": lip, "lport": lport,
                        "rip": None if rport == 0 else rip, "rport": rport, "status": status})
    return out


def process_conns(pid: int) -> list[dict[str, Any]]:
    if platform.system() == "Linux":
        try:
            return linux_inet_conns(pid)
        except PermissionError as e:
            raise psutil.AccessDenied(pid) from e
        except FileNotFoundError as e:
            raise psutil.NoSuchProcess(pid) from e
    out = []
    for c in psutil.Process(pid).net_connections(kind="inet"):
        out.append({"proto": "tcp" if c.type == socket.SOCK_STREAM else "udp",
                    "lip": c.laddr.ip if c.laddr else None, "lport": c.laddr.port if c.laddr else 0,
                    "rip": c.raddr.ip if c.raddr else None, "rport": c.raddr.port if c.raddr else 0,
                    "status": c.status})
    return out


def listen_ports(conns: list[dict[str, Any]]) -> set[int]:
    return {c["lport"] for c in conns if c["status"] == "LISTEN"}


class SocketWatcher:
    """Samples inet sockets of the watched processes; records every outbound remote seen.

    Socket sampling misses connections shorter than `interval_s`; the firewall counter and conntrack in
    EgressEvidence are the stronger evidence. This is the per-process attribution."""

    def __init__(self, pids: set[int] | None = None, lan: list | None = None, interval_s: float = 0.1,
                 match_roles: bool = True, ports: set[int] | None = None):
        self.pids = set(pids or ())
        self.lan = lan or []
        self.interval_s = interval_s
        self.match_roles = match_roles
        self.ports = set(ports or ())
        self.outbound: dict[tuple[int, str, int], str] = {}
        self.samples = 0
        self.watched: set[int] = set()
        self.ignored: dict[int, str] = {}
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._targets_at = 0.0
        self._targets: set[int] = set()

    def _role_targets(self) -> set[int]:
        """Role-matched processes that listen on a watched port, plus their children (vLLM engine core,
        ollama runner). With no ports given, every role match is kept."""
        cands: dict[int, str] = {}
        for p in psutil.process_iter(["pid", "cmdline", "name"]):
            try:
                role = role_of(p.info["name"], p.info["cmdline"])
            except psutil.Error:
                continue
            if role:
                cands[p.info["pid"]] = role
        if not self.ports:
            return set(cands)
        keep: set[int] = set()
        for pid in cands:
            try:
                if listen_ports(process_conns(pid)) & self.ports:
                    keep.add(pid)
            except psutil.Error:
                continue
        for pid, role in cands.items():
            if pid in keep:
                continue
            try:
                if any(par.pid in keep for par in psutil.Process(pid).parents()):
                    keep.add(pid)
                    continue
            except psutil.Error:
                pass
            self.ignored[pid] = role
        for pid in keep:
            self.ignored.pop(pid, None)
        return keep

    def targets(self) -> set[int]:
        now = time.monotonic()
        if self.match_roles and now - self._targets_at > 2.0:        # re-scan roles every 2 s, not every tick
            self._targets = self._role_targets()
            self._targets_at = now
        return set(self.pids) | (self._targets if self.match_roles else set())

    def sample(self) -> None:
        for pid in self.targets():
            try:
                conns = process_conns(pid)
            except psutil.AccessDenied:
                if f"access denied pid {pid}" not in self.errors:
                    self.errors.append(f"access denied pid {pid}")
                continue
            except psutil.Error:
                continue
            self.watched.add(pid)
            for c in conns:
                if c["rip"] and is_outbound(c["rip"], self.lan):
                    self.outbound[(pid, c["rip"], c["rport"])] = c["status"]
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
        self._t.join(timeout=5)

    def report(self) -> dict[str, Any]:
        return {"samples": self.samples, "interval_s": self.interval_s, "watched_pids": sorted(self.watched),
                "ignored_pids": {str(k): v for k, v in sorted(self.ignored.items())},
                "outbound_connections": len(self.outbound),
                "outbound": [{"pid": k[0], "ip": k[1], "port": k[2], "status": v} for k, v in self.outbound.items()],
                "errors": self.errors}


# ----------------------------------------------------------------------------- firewall + conntrack
def parse_nft_counter(text: str) -> dict[str, int] | None:
    m = re.search(r"packets\s+(\d+)\s+bytes\s+(\d+)", text)
    return {"packets": int(m.group(1)), "bytes": int(m.group(2))} if m else None


def parse_pf_block_packets(text: str) -> int | None:
    """Sum `Packets:` of the block rules in `pfctl -a warroom_offline -vsr` output."""
    total, in_block, found = 0, False, False
    for ln in text.splitlines():
        if not ln.startswith(" ") and not ln.startswith("\t") and not ln.startswith("["):
            in_block = ln.strip().startswith("block")
            continue
        m = re.search(r"Packets:\s*(\d+)", ln)
        if m and in_block:
            total += int(m.group(1))
            found = True
    return total if found else None


def parse_conntrack(text: str, lan: list | None = None) -> set[tuple[str, str, int]]:
    """Flows whose ORIGINAL-direction destination leaves the factory: {(proto, dst, dport)}."""
    out = set()
    for ln in text.splitlines():
        parts = ln.split()
        proto = next((p for p in parts if p in ("tcp", "udp", "icmp", "icmpv6", "sctp")), "?")
        dst = re.search(r"\bdst=(\S+)", ln)
        if not dst or not is_outbound(dst.group(1), lan):
            continue
        dport = re.search(r"\bdport=(\d+)", ln)
        out.add((proto, dst.group(1), int(dport.group(1)) if dport else 0))
    return out


def _run(cmd: list[str], timeout: float = 10) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


class EgressEvidence:
    """Firewall drop counter (zeroed at start) + conntrack snapshots once a second for the whole window."""

    def __init__(self, block_applied: bool, lan: list | None = None, conntrack_interval_s: float = 1.0):
        self.system = platform.system()
        self.block_applied = block_applied
        self.lan = lan or []
        self.interval_s = conntrack_interval_s
        self.conntrack_ok = self.system == "Linux" and os.geteuid() == 0 and bool(shutil.which("conntrack"))
        self.flows: set[tuple[str, str, int]] = set()
        self.conntrack_snapshots = 0
        self.notes: list[str] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def counter(self) -> int | None:
        if not self.block_applied:
            return None
        if self.system == "Linux":
            ok, txt = _run(["nft", "list", "counter", *NFT_COUNTER])
            c = parse_nft_counter(txt) if ok else None
            if c is None:
                self.notes.append(f"nft counter unreadable: {txt[:200]}")
            return None if c is None else c["packets"]
        if self.system == "Darwin":
            ok, txt = _run(["pfctl", "-a", "warroom_offline", "-vsr"])
            n = parse_pf_block_packets(txt) if ok else None
            if n is None:
                self.notes.append(f"pf counters unreadable: {txt[:200]}")
            return n
        return None

    def reset_counter(self) -> None:
        if self.block_applied and self.system == "Linux":
            ok, txt = _run(["nft", "reset", "counter", *NFT_COUNTER])
            if not ok:
                self.notes.append(f"nft reset failed: {txt[:200]}")
        # pf: the anchor was just loaded, so its rule counters start at 0

    def snapshot(self) -> None:
        if not self.conntrack_ok:
            return
        ok, txt = _run(["conntrack", "-L"], timeout=5)
        if ok:
            self.flows |= parse_conntrack(txt, self.lan)
            self.conntrack_snapshots += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.snapshot()
            self._stop.wait(self.interval_s)

    def __enter__(self):
        self.reset_counter()
        self.start_count = self.counter()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=10)
        self.snapshot()
        self.end_count = self.counter()

    def report(self) -> dict[str, Any]:
        dropped = None
        if self.start_count is not None and self.end_count is not None:
            dropped = self.end_count - self.start_count
        return {"firewall_dropped_packets": dropped, "conntrack_available": self.conntrack_ok,
                "conntrack_snapshots": self.conntrack_snapshots,
                "conntrack_outbound_flows": [{"proto": p, "dst": d, "dport": n} for p, d, n in sorted(self.flows)],
                "notes": self.notes}


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
    for host in PROBE_HOSTS:
        try:
            with socket.create_connection((host, 443), timeout=timeout_s):
                return True
        except OSError:
            continue
    return False


def watch_ports(base_url: str | None, app_url: str | None, extra: list[int] | None) -> set[int]:
    ports = set(QDRANT_PORTS) | set(extra or [])
    for u in (base_url, app_url):
        if u:
            p = urlparse(u)
            ports.add(p.port or (443 if p.scheme == "https" else 80))
    return ports


async def direct_alert(ep: Endpoint, runs: int) -> list[float | None]:
    out = []
    cli = ep.client()
    for i in range(runs):
        r = await stream_chat(ep, alert_card_messages(ANOMALIES[i % 2]), max_tokens=ALERT_CARD_MAX_TOKENS,
                              response_format=json_schema_format("AlertCard", card_schema()), client=cli)
        out.append(round(r.total_s, 4) if r.ok else None)
    await cli.close()
    return out


def card_source(msg_data: dict[str, Any], seen_before: int) -> str:
    """'detector' (instant no-LLM placeholder) or 'commander' (LLM-written). An app without the source flag
    publishes the placeholder first, so the first card of an event is the detector's."""
    src = msg_data.get("source")
    if src in ("detector", "commander"):
        return src
    return "detector" if seen_before == 0 else "commander"


async def commander_call(c: httpx.AsyncClient, event_id: str) -> dict[str, Any] | None:
    """The event's first `llm_call` audit entry: ttft_ms, total_ms, tokens, mode."""
    try:
        r = await c.get("/api/audit", params={"event_id": event_id, "kind": "llm_call", "limit": 50})
        items = r.json() if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return None
    for e in items:
        p = e.get("payload") or {}
        if "error" in p:
            return {"error": p["error"]}
        return {k: p.get(k) for k in ("mode", "model", "ttft_ms", "total_ms", "prompt_tokens",
                                      "completion_tokens", "valid_json", "fell_back_to_mock")}
    return None


async def app_alert(app_url: str, scenario: str, timeout_s: float, speed: float) -> dict[str, Any]:
    """Real stack: trigger scenario; time anomaly -> first card (detector placeholder) and anomaly -> first
    COMMANDER card on /ws; read the commander's TTFT from the audit log; check health + phone page."""
    import websockets
    res: dict[str, Any] = {}
    async with httpx.AsyncClient(base_url=app_url, timeout=5) as c:
        h = await c.get("/api/health")
        res["health"] = h.json() if h.status_code == 200 else h.status_code
        ph = await c.get("/phone")
        res["phone_page_status"] = ph.status_code
        ws_url = re.sub(r"^http", "ws", app_url) + "/ws"
        cards: dict[str, int] = {}
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
                    d = msg["data"]
                    eid = d.get("event_id")
                    if "event_id" in res and eid != res["event_id"]:
                        continue                                     # only the first event is timed
                    src = card_source(d, cards.get(eid, 0))
                    cards[eid] = cards.get(eid, 0) + 1
                    dt = round(time.perf_counter() - t_anom, 4)
                    res.setdefault("event_id", eid)
                    res["source_flag_present"] = "source" in d
                    if src == "detector" and "placeholder_after_anomaly_s" not in res:
                        res["placeholder_after_anomaly_s"] = dt
                        res["placeholder_card"] = d.get("card")
                    if src == "commander":
                        res["alert_after_anomaly_s"] = dt
                        res["card"] = d.get("card")
                        break
        if res.get("event_id"):
            for _ in range(20):                                     # audit entry lands right after the plan
                res["commander_call"] = await commander_call(c, res["event_id"])
                if res["commander_call"]:
                    break
                await asyncio.sleep(0.5)
    return res


async def main_async(args) -> dict[str, Any]:
    lan = [ipaddress.ip_network(c) for c in (args.lan_cidr or [])]
    ep = Endpoint.from_env(args.base_url, args.model)
    out: dict[str, Any] = {"platform": platform.platform(), "is_root": os.geteuid() == 0, "watch_s": args.watch}
    blk, unblk = block_commands(args.block_cmd, args.unblock_cmd)
    out["block"] = {"requested": args.block, "applied": False}
    if args.block:
        ok, msg = apply(blk)
        out["block"].update(applied=ok, cmd=" ".join(blk), msg=msg)
    applied = out["block"]["applied"]
    alert_s = None
    try:
        await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)   # model resident
        ports = watch_ports(ep.base_url, args.app_url, args.watch_port)
        t0 = time.monotonic()
        with EgressEvidence(applied, lan) as egress, \
                SocketWatcher(set(args.pid or []), lan, interval_s=args.interval, ports=ports) as w:
            out["endpoint"] = await endpoint_info(ep)
            if args.app_url:
                out["app"] = await app_alert(args.app_url, args.scenario, args.timeout, args.speed)
                alert_s = out["app"].get("alert_after_anomaly_s")
            else:
                out["direct_alert_s"] = await direct_alert(ep, args.runs)
                vals = [v for v in out["direct_alert_s"] if v is not None]
                alert_s = max(vals) if vals else None
            while time.monotonic() - t0 < args.watch:                  # keep watching normal running
                await asyncio.sleep(min(1.0, args.watch - (time.monotonic() - t0)))
            await asyncio.sleep(0.3)
        out["watched_for_s"] = round(time.monotonic() - t0, 1)
        out["watch_ports"] = sorted(ports)
        out["sockets"] = w.report()
        out["egress"] = egress.report()
        # probe AFTER the window so its own packets are not counted as the stack's egress
        out["internet_reachable_during_test"] = internet_reachable()
    finally:
        if args.block and applied:
            ok, msg = apply(unblk)
            out["block"]["unblocked"] = ok
    out["verdicts"] = verdicts(out, alert_s, bool(args.app_url))
    out["saved_to"] = str(save_result("unplug", ep, out, args.out_dir))
    return out


def verdicts(out: dict[str, Any], alert_s: float | None, via_app: bool) -> list[dict[str, Any]]:
    applied = out["block"]["applied"]
    vs = []
    if via_app:
        app = out.get("app", {})
        note = "anomaly -> first COMMANDER (LLM) card on /ws"
        if not app.get("source_flag_present", True):
            note += " (app has no source flag: 2nd card of the event taken as the commander's)"
        if alert_s is None:
            note += ": no commander card before timeout"
        vs.append(Verdict("alert_offline", f"≤ {LATENCY_FIRST_ALERT_S} s", alert_s,
                          None if alert_s is None else alert_s <= LATENCY_FIRST_ALERT_S, note=note,
                          extra={"placeholder_after_anomaly_s": app.get("placeholder_after_anomaly_s")}).__dict__)
        cc = app.get("commander_call") or {}
        ttft = cc.get("ttft_ms")
        ttft_s = None if ttft is None else round(ttft / 1000, 3)
        vs.append(Verdict("commander_ttft", f"≤ {TTFT_TARGET_S} s", ttft_s,
                          None if ttft_s is None else ttft_s <= TTFT_TARGET_S,
                          note=("from the event's llm_call audit entry" if cc else "no llm_call audit entry")
                          + (f"; mode={cc.get('mode')}" if cc.get("mode") else ""), extra=cc).__dict__)
    else:
        vs.append(Verdict("alert_offline", f"≤ {LATENCY_FIRST_ALERT_S} s", alert_s,
                          None if alert_s is None else alert_s <= LATENCY_FIRST_ALERT_S,
                          note="direct alert-card call" + ("" if alert_s is not None else ": no card")).__dict__)
    socks = out["sockets"]["outbound_connections"]
    eg = out["egress"]
    dropped = eg["firewall_dropped_packets"]
    flows = len(eg["conntrack_outbound_flows"]) if eg["conntrack_available"] else None
    evidence = {"socket_samples": socks, "firewall_dropped_packets": dropped, "conntrack_outbound_flows": flows}
    if not applied:
        passed, note = None, ("network NOT blocked (needs root); socket samples only, which miss short "
                              "connections — not evidence of 0 outbound")
    elif dropped is None:
        passed, note = None, "block applied but firewall counter unreadable; socket samples only"
    else:
        passed = socks == 0 and dropped == 0 and (flows in (None, 0))
        note = "firewall drop counter + socket samples" + (" + conntrack" if flows is not None else "")
    measured = socks + (dropped or 0) + (flows or 0)
    vs.append(Verdict("outbound_connections", "0", measured, passed, note=note, extra=evidence).__dict__)
    if applied:
        vs.append(Verdict("internet_blocked", "unreachable", out["internet_reachable_during_test"],
                          not out["internet_reachable_during_test"]).__dict__)
    return vs


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
    p.add_argument("--watch", type=float, default=600.0,
                   help="seconds to watch egress in total (normal running + the event); default 600")
    p.add_argument("--interval", type=float, default=0.1, help="socket sample interval, s")
    p.add_argument("--pid", type=int, action="append")
    p.add_argument("--watch-port", type=int, action="append", help="extra listen port marking a stack process")
    p.add_argument("--lan-cidr", action="append")
    p.add_argument("--out-dir", type=Path)
    out = run(main_async(p.parse_args(argv)))
    for v in out["verdicts"]:
        print(f"{v['name']:<22} target {v['target']:<8} measured {v['measured']}  pass={v['passed']}  {v['note']}")
    print("block:", out["block"], " watched pids:", out["sockets"]["watched_pids"],
          " ignored:", out["sockets"]["ignored_pids"], " egress:", out["egress"])
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
