"""Unplug test (page 9: 拔網路線 → 2 秒內告警、對外連線 0 次).

Two kinds of block, and only one of them is the page-9 test:
  --unplug IFACE   (page-9 verdict) the uplink really goes down: `ip link set dev IFACE down` (macOS `ifconfig
                   IFACE down`), repeatable for every uplink. The LAN resolver, proxies and NTP become
                   unreachable, so resolver timeouts and dead upstreams are exercised; `dns_unreachable` checks
                   that no configured resolver still answers. The boot firewall stays loaded and still logs.
  --block          (secondary check) firewall only: the boot nft table (or pf anchor) drops non-LAN
                   destinations, but the LAN, its DNS resolver and any proxy stay up. `internet_blocked`
                   passes by construction here; what this run proves is that the stack made no attempt, as
                   seen by the drop log and the DNS log. The `page9_unplug` verdict stays None.

Steps
 1. Block outbound traffic: --unplug and/or --block (above), or --block-cmd. Needs root. On Linux, when
    warroom-firewall.service already loaded the table, --block reuses it as-is (nothing is applied or removed),
    which is why --block alone is not an unplug. Without root the test still runs, says the block was NOT
    applied, and the egress verdict is None.
 2. Zero the firewall's drop counter (nft `dropped_egress`), then watch for a fixed window (--watch, default
    600 s = 10 min of normal running) plus one event:
      --app-url http://127.0.0.1:8000   real stack: POST /api/sim/control scenario, wait on /ws for `anomaly`,
                                        the instant detector card and the COMMANDER card (source="commander");
                                        the page-9 ≤ 2 s target is judged on the commander card. The commander's
                                        TTFT/total come from the event's `llm_call` audit entry. Also checks
                                        /api/health and /phone (LAN path).
      (default)                         direct: alert-card call against the local LLM endpoint
 3. Egress evidence, strongest first:
      - nft drop log (Linux, block applied): kernel log lines with prefix "warroom-egress-drop host|fwd ",
        read from `journalctl -k` for the window and counted as ATTEMPTS (distinct proto/src/sport/dst/dport;
        a blocked TCP connect is 2+ packets). `fwd` = a bridged container (vLLM, qdrant, mqtt): counted.
        `host` = a host-network process, logged with its UID: counted when the UID is the app's (APP_UID,
        the app runs with network_mode: host) or belongs to a watched stack process in the host network
        namespace; otherwise it is DGX OS housekeeping (timesyncd, snapd, motd-news) and is reported as
        host_other_attempts, not counted. DNS counts too: every new query to port 53 is logged
        "warroom-dns host|fwd " before any accept (a lookup to a LAN resolver is data leaving through that
        resolver), attributed the same way and added to the stack's attempts. Log unreadable -> the per-chain
        packet counters (egress_containers counted, egress_host reported); old table without them -> the
        box-wide dropped_egress packet counter, flagged as such.
      - pf block-rule packet counter (macOS, block applied)
      - conntrack snapshots every second (Linux, root, `conntrack` installed): flows to non-LAN addresses
      - socket samples of the watched processes every --interval s. On Linux the sockets are read from
        /proc/<pid>/net/* (the process's OWN network namespace, so Docker containers are seen); psutil
        reads /proc/net/* of the caller's namespace and would miss every container socket.
    Watched processes: --pid, plus model server / qdrant / app processes that listen on a watched port
    (model port from --base-url, app port from --app-url, 6333/6334, --watch-port) and their children.
    Same-role processes on other ports (a user's own Ollama, another app's qdrant) are listed as ignored.
 4. Internet probe (must fail while blocked), then unblock (always, in finally).

 --sandbox (macOS, no root): re-run this test inside `sandbox-exec` with SANDBOX_PROFILE, which denies every
    outbound connection except to localhost. The block is real but process-level: it covers this process and
    everything it starts (with --spawn-app, the whole war-room app), not the model server (Ollama), which the
    socket watcher still checks. The drop counter is the kernel's own Sandbox log: every
    `deny(1) network-outbound` line for a process in our tree is one blocked attempt to leave. `log` refuses to
    run inside a sandbox, so the sandboxed child records its pids and window and the unsandboxed parent reads
    the log after the child exits, then re-judges and re-saves the result. Name lookups are denied too: the
    profile blocks the mDNSResponder socket, so a lookup is a logged deny (counted, listed as dns_by_pid)
    instead of a query that leaves the Mac through the system resolver. `localhost` still resolves from
    /etc/hosts; the endpoint URLs are rewritten to 127.0.0.1 so the bench's own calls make no lookup.
 --spawn-app: start `python -m warroom.api serve` (LLM_MODE=llm against --base-url/--model) on a free
    localhost port as a child, run the --app-url path against it, watch it, stop it afterwards.

  uv run python -m bench.unplug_test --watch 0               # Mac: direct, socket check only
  uv run python -m bench.unplug_test --sandbox --spawn-app --watch 120   # Mac: app really offline, no root
  sudo -E bench/py.sh -m bench.unplug_test --unplug enP7s7 --app-url http://127.0.0.1:8000   # Spark, page 9
  sudo -E bench/py.sh -m bench.unplug_test --block --app-url http://127.0.0.1:8000             # secondary
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import psutil

from bench.llm import Endpoint, Verdict, endpoint_info, json_schema_format, run, save_result, stream_chat
from bench.memory_check import role_of
from bench.prompts import ANOMALIES, COMMANDER_MAX_TOKENS, commander_format, commander_messages, live_anomaly
from warroom.factory import ALERT_CARD_MAX_TOKENS, LATENCY_FIRST_ALERT_S, TTFT_TARGET_S

ROOT = Path(__file__).resolve().parent.parent
NFT = ROOT / "deploy" / "network" / "nftables-warroom.nft"
PF = ROOT / "deploy" / "network" / "pf-warroom.conf"
NFT_COUNTER = ["inet", "warroom_offline", "dropped_egress"]   # box-wide total (packets), kept for old tables
# Split counters in nftables-warroom.nft: output hook = host processes, forward hook = the containers.
NFT_SPLIT = {"host": "egress_host", "containers": "egress_containers"}
DROP_LOG_PREFIX = "warroom-egress-drop"
DNS_LOG_PREFIX = "warroom-dns"           # nftables-warroom.nft logs every new :53 query before any accept
NFT_DNS = {"host": "dns_host", "containers": "dns_containers"}
DNS_PROBE_ZONE = "example.com"    # probe asks for a fresh random name under it: no cache can answer that
APP_UID = 10001          # deploy/Dockerfile `useradd --uid 10001`; the app container is host-networked
QDRANT_PORTS = (6333, 6334)
PROBE_HOSTS = ("1.1.1.1", "8.8.8.8")
SANDBOX_ENV = "WARROOM_UNPLUG_SANDBOXED"
RESULT_PATH_ENV = "WARROOM_UNPLUG_RESULT_PATH"      # child writes its saved-result path here for the parent
MDNS_SOCKET = "/private/var/run/mDNSResponder"
# Seatbelt profile: everything allowed except outbound network to anything but localhost. Local IPC over unix
# sockets stays allowed, EXCEPT the mDNSResponder socket: a lookup through it is a DNS query that leaves the
# Mac (the onnxruntime telemetry host was resolved that way before its connect was denied). Denied, it shows
# in the Sandbox log as `deny(1) network-outbound /private/var/run/mDNSResponder` and is counted. Later rules
# win in SBPL, so the deny comes last. `localhost` still resolves (from /etc/hosts).
SANDBOX_PROFILE = f"""(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
(deny network-outbound (remote unix-socket (path-literal "{MDNS_SOCKET}")))
"""


def sandbox_argv(argv: list[str]) -> list[str]:
    """Command that re-runs this module under sandbox-exec with the offline profile."""
    return ["sandbox-exec", "-p", SANDBOX_PROFILE, sys.executable, "-m", "bench.unplug_test", *argv]


def in_sandbox() -> bool:
    return os.environ.get(SANDBOX_ENV) == "1"


_DENY_RE = re.compile(r"Sandbox: \S+\((\d+)\) deny\(1\) network-outbound(?:\s+(\S+))?")


def sandbox_deny_detail(text: str, pids: set[int], before: str | None = None) -> dict[str, Any]:
    """`deny(1) network-outbound` reports in `log show --style compact` output for `pids`, per pid.

    Only a report that names a remote (`remote:*:443`, `1.1.1.1:443`) is a blocked attempt to leave: the
    kernel also logs remote-less network-outbound denies around ordinary localhost sockets (seen with a bare
    connect to 127.0.0.1:11434 that succeeded), and those are counted apart as `no_remote`. A deny naming the
    mDNSResponder socket is a name lookup: counted with the rest and also listed in `dns_by_pid`. 'N duplicate
    report(s) for' adds N; the kernel repeats the remote on those lines. `before` ('YYYY-MM-DD HH:MM:SS.mmm')
    drops lines at or after it."""
    by: dict[int, int] = {}
    dns: dict[int, int] = {}
    no_remote: dict[int, int] = {}
    remotes: set[str] = set()
    for ln in text.splitlines():
        if before and ln[:23] >= before:
            continue
        m = _DENY_RE.search(ln)
        if not m or int(m.group(1)) not in pids:
            continue
        d = re.search(r"(\d+) duplicate reports? for", ln)
        k = int(d.group(1)) if d else 1
        pid = int(m.group(1))
        if m.group(2):
            by[pid] = by.get(pid, 0) + k
            remotes.add(m.group(2))
            if m.group(2) == MDNS_SOCKET:              # a name lookup: counted, and listed apart
                dns[pid] = dns.get(pid, 0) + k
        else:
            no_remote[pid] = no_remote.get(pid, 0) + k
    return {"by_pid": dict(sorted(by.items())), "remotes": sorted(remotes),
            "dns_by_pid": dict(sorted(dns.items())), "no_remote_by_pid": dict(sorted(no_remote.items()))}


def parse_sandbox_denies(text: str, pids: set[int], before: str | None = None) -> int:
    """Blocked attempts to leave by `pids`: the remote-naming denies of sandbox_deny_detail."""
    return sum(sandbox_deny_detail(text, pids, before)["by_pid"].values())


def now_ms() -> str:
    """Local wall time with milliseconds, in `log show --style compact` timestamp format."""
    t = time.time()
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) + f".{int(t % 1 * 1000):03d}"


def process_tree() -> set[int]:
    me = psutil.Process()
    return {me.pid} | {c.pid for c in me.children(recursive=True)}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def spawn_app(ep: Endpoint, timeout_s: float = 120.0) -> tuple[subprocess.Popen, str, Path]:
    """Start the real war-room app on localhost with the LLM commander pointed at `ep`."""
    port = free_port()
    run_dir = Path(tempfile.mkdtemp(prefix="warroom-unplug-"))
    env = os.environ | {"LLM_MODE": "llm", "BASE_URL": ep.base_url, "MODEL": ep.model,
                        "AUDIT_PATH": str(run_dir / "audit.jsonl"), "SCHEDULE_DIR": str(run_dir / "schedules"),
                        "QDRANT_PATH": str(run_dir / "qdrant"), "HF_HUB_OFFLINE": "1"}
    log = open(run_dir / "app.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "warroom.api", "serve", "--host", "127.0.0.1",
                             "--port", str(port), "--scenario", "normal", "--llm-mode", "llm"],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"app exited with {proc.returncode}; log {run_dir / 'app.log'}")
        try:
            if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                return proc, url, run_dir
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError(f"app not healthy after {timeout_s}s; log {run_dir / 'app.log'}")


def is_outbound(ip: str, lan: list[ipaddress._BaseNetwork] | None = None) -> bool:
    """True when a remote address leaves the factory: not loopback/private/link-local/multicast/LAN."""
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    elif isinstance(a, ipaddress.IPv6Address) and a.packed[:12] == bytes(12) and int(a) > 1:
        a = ipaddress.IPv4Address(a.packed[12:])          # IPv4-compatible "::127.0.0.1" (macOS reports it)
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


def parse_pf_dns_packets(text: str) -> int | None:
    """Sum `Packets:` of the `label "warroom-dns"` pass rules (box-wide DNS to the LAN resolver)."""
    total, in_dns, found = 0, False, False
    for ln in text.splitlines():
        if not ln.startswith((" ", "\t", "[")):
            in_dns = ln.strip().startswith("pass") and 'label "warroom-dns"' in ln
            continue
        m = re.search(r"Packets:\s*(\d+)", ln)
        if m and in_dns:
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


_DROP_RE = re.compile(DROP_LOG_PREFIX + r" (?:(host|fwd) )?(.*)")
_DNS_RE = re.compile(DNS_LOG_PREFIX + r" (?:(host|fwd) )?(.*)")


def parse_dns_log(text: str) -> list[dict[str, Any]]:
    """New DNS queries (port 53) in kernel log text, one dict per (chain, proto, src, sport, dst, dport), same
    shape as parse_drop_log plus kind="dns". Logged before any accept, so a query the LAN resolver answered
    is here too: it is data leaving the factory through that resolver."""
    out = parse_drop_log(text, _DNS_RE)
    for a in out:
        a["kind"] = "dns"
    return out


def parse_drop_log(text: str, pattern: re.Pattern = _DROP_RE) -> list[dict[str, Any]]:
    """Blocked egress ATTEMPTS in kernel log text: one dict per distinct (chain, proto, src, sport, dst, dport).

    nf_log line: `warroom-egress-drop host IN= OUT=eth0 SRC=.. DST=1.1.1.1 .. PROTO=TCP SPT=40000 DPT=443 ..
    UID=0 GID=0`. A TCP SYN and its retransmits share the source port, so they are one attempt (`packets`
    counts them). chain: host (output hook), containers (forward hook), box (old table, no chain tag)."""
    seen: dict[tuple, dict[str, Any]] = {}
    for ln in text.splitlines():
        m = pattern.search(ln)
        if not m:
            continue
        f = dict(kv.split("=", 1) for kv in m.group(2).split() if "=" in kv)
        if not f.get("DST"):
            continue
        chain = {"host": "host", "fwd": "containers"}.get(m.group(1) or "", "box")
        sport, dport = int(f.get("SPT") or 0), int(f.get("DPT") or 0)
        key = (chain, f.get("PROTO", "?").lower(), f.get("SRC", ""), sport, f["DST"], dport)
        if key in seen:
            seen[key]["packets"] += 1
            continue
        uid = f.get("UID")
        seen[key] = {"chain": chain, "proto": key[1], "src": key[2], "sport": sport, "dst": f["DST"],
                     "dport": dport, "uid": int(uid) if uid and uid.isdigit() else None, "packets": 1}
    return list(seen.values())


def host_ns_uids(pids: set[int], proc: Path = Path("/proc")) -> set[int]:
    """Effective UIDs of the `pids` that share this process's network namespace. Their sockets leave through
    the output hook, so a `host` drop with one of these UIDs may be the stack's; container processes (own
    namespace, forward hook) are excluded so their root UID does not claim the OS's root daemons' drops."""
    try:
        mine = os.readlink(proc / "self" / "ns" / "net")
    except OSError:
        return set()
    out: set[int] = set()
    for pid in pids:
        try:
            if os.readlink(proc / str(pid) / "ns" / "net") == mine:
                out.add(psutil.Process(pid).uids().effective)
        except (OSError, psutil.Error):
            continue
    return out


def attribute_drops(attempts: list[dict[str, Any]], stack_uids: set[int]) -> tuple[list, list]:
    """(stack, other). Container and untagged (old table) drops are the stack's; host drops are the stack's
    only when their UID is a stack UID. A host drop with no UID (no owning socket) is the OS's."""
    stack, other = [], []
    for a in attempts:
        mine = a["chain"] != "host" or (a.get("uid") is not None and a["uid"] in stack_uids)
        (stack if mine else other).append(a)
    return stack, other


def _run(cmd: list[str], timeout: float = 10) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout if r.returncode == 0 else (r.stderr or r.stdout)).strip()
    except Exception as e:  # noqa: BLE001
        return False, str(e)


class EgressEvidence:
    """Firewall drop log + counters (zeroed at start) + conntrack snapshots once a second for the window."""

    def __init__(self, block_applied: bool, lan: list | None = None, conntrack_interval_s: float = 1.0):
        self.system = platform.system()
        self.block_applied = block_applied
        self.lan = lan or []
        self.interval_s = conntrack_interval_s
        self.conntrack_ok = self.system == "Linux" and os.geteuid() == 0 and bool(shutil.which("conntrack"))
        self.flows: set[tuple[str, str, int]] = set()
        self.conntrack_snapshots = 0
        self.notes: list[str] = []
        self.sandbox = in_sandbox()
        self.sandbox_pids: set[int] = set()
        self._since = time.strftime("%Y-%m-%d %H:%M:%S")
        self._t0 = self._t1 = time.time()
        self.split_start: dict[str, int | None] = {}
        self.split_end: dict[str, int | None] = {}
        self.drop_log: str | None = None
        self.pf_dns_packets: int | None = None
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def counter(self) -> int | None:
        if not self.block_applied:
            return None
        if self.sandbox:            # `log` cannot run sandboxed: record pids, the parent counts afterwards
            self.sandbox_pids |= process_tree()
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
            self.pf_dns_packets = parse_pf_dns_packets(txt) if ok else None
            if n is None:
                self.notes.append(f"pf counters unreadable: {txt[:200]}")
            return n
        return None

    def split_counts(self) -> dict[str, int | None]:
        """Per-chain packet counters (host / containers); None each when the loaded table predates them."""
        if not (self.block_applied and self.system == "Linux") or self.sandbox:
            return {}
        out: dict[str, int | None] = {}
        for k, name in (*NFT_SPLIT.items(), *((f"dns_{k}", v) for k, v in NFT_DNS.items())):
            ok, txt = _run(["nft", "list", "counter", *NFT_COUNTER[:2], name])
            c = parse_nft_counter(txt) if ok else None
            out[k] = None if c is None else c["packets"]
        return out

    def read_drop_log(self) -> str | None:
        """Kernel log lines with the drop prefix inside the window, or None when journalctl cannot be read."""
        ok, txt = _run(["journalctl", "-k", "-o", "short-iso", "--no-pager", "--since", f"@{int(self._t0)}",
                        "--until", f"@{int(self._t1) + 1}"], timeout=30)
        if not ok:
            self.notes.append(f"kernel log unreadable (journalctl -k): {txt[:200]}")
            return None
        return "\n".join(ln for ln in txt.splitlines() if DROP_LOG_PREFIX in ln or DNS_LOG_PREFIX in ln)

    def reset_counter(self) -> None:
        if self.block_applied and self.system == "Linux":
            for name in (NFT_COUNTER[2], *NFT_SPLIT.values(), *NFT_DNS.values()):
                ok, txt = _run(["nft", "reset", "counter", *NFT_COUNTER[:2], name])
                if not ok:
                    self.notes.append(f"nft reset {name} failed: {txt[:200]}")
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
        self._t0 = time.time()
        self.start_count = self.counter()
        self.split_start = self.split_counts()
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=10)
        self.snapshot()
        self.end_count = self.counter()
        self.split_end = self.split_counts()
        self._t1 = time.time()
        self._until = now_ms()
        if self.block_applied and self.system == "Linux" and not self.sandbox:
            self.drop_log = self.read_drop_log()

    def report(self, stack_pids: set[int] | None = None) -> dict[str, Any]:
        """`stack_pids`: the watched stack processes; host-hook drops count only for their UIDs."""
        dropped = None
        if self.start_count is not None and self.end_count is not None:
            dropped = self.end_count - self.start_count
        extra: dict[str, Any] = ({"sandbox_pids": sorted(self.sandbox_pids), "bench_pid": os.getpid(),
                                  "window": [self._since, self._until]}
                                 if self.sandbox else {})
        keys = (*NFT_SPLIT, *(f"dns_{k}" for k in NFT_DNS))
        split = {k: (None if self.split_start.get(k) is None or self.split_end.get(k) is None
                     else self.split_end[k] - self.split_start[k]) for k in keys}
        if self.split_end:
            extra["host_dropped_packets"] = split["host"]
            extra["container_dropped_packets"] = split["containers"]
            extra["dns_host_packets"] = split["dns_host"]              # None: table predates the DNS log
            extra["dns_container_packets"] = split["dns_containers"]
        if self.system == "Darwin" and self.pf_dns_packets is not None:
            extra["dns_packets_box_wide"] = self.pf_dns_packets     # mDNSResponder: not attributable
        if self.drop_log is not None:
            uids = host_ns_uids(set(stack_pids or ())) | {APP_UID}
            stack, other = attribute_drops(parse_drop_log(self.drop_log), uids)
            dns_stack, dns_other = attribute_drops(parse_dns_log(self.drop_log), uids)
            extra.update(stack_drop_attempts=stack, host_other_attempts=other, stack_host_uids=sorted(uids),
                         stack_dns_queries=dns_stack, host_other_dns_queries=dns_other,
                         counter_source="kernel log 'warroom-egress-drop' + 'warroom-dns': attempts and DNS "
                                        "queries, attributed by chain + UID")
        elif split.get("containers") is not None:
            extra["counter_source"] = ("nft egress_containers + dns_containers packet counters (kernel log "
                                       "unreadable; packets, not attempts; egress_host / dns_host reported, "
                                       "not counted)")
        elif dropped is not None and self.system == "Linux":
            extra["counter_source"] = ("nft dropped_egress packet counter, BOX-WIDE (old table without split "
                                       "counters): includes DGX OS traffic, not attributed to the stack")
        return {**extra, "firewall_dropped_packets": dropped, "conntrack_available": self.conntrack_ok,
                "conntrack_snapshots": self.conntrack_snapshots,
                "conntrack_outbound_flows": [{"proto": p, "dst": d, "dport": n} for p, d, n in sorted(self.flows)],
                "notes": self.notes}


def nft_table_loaded() -> bool:
    """Is the boot egress firewall (table inet warroom_offline) already in the ruleset?"""
    ok, _ = _run(["nft", "list", "table", *NFT_COUNTER[:2]])
    return ok


def unplug_commands(ifaces: list[str], system: str | None = None) -> tuple[list[str], list[str]]:
    """Take the uplink(s) really down for the window, and back up afterwards (page 9: 拔網路線)."""
    system = system or platform.system()
    for i in ifaces:
        if not re.fullmatch(r"[A-Za-z0-9_.:@-]{1,32}", i):
            raise ValueError(f"bad interface name {i!r}")
    if system == "Linux":
        down = " && ".join(f"ip link set dev {i} down" for i in ifaces)
        up = "; ".join(f"ip link set dev {i} up" for i in ifaces)
    elif system == "Darwin":
        down = " && ".join(f"ifconfig {i} down" for i in ifaces)
        up = "; ".join(f"ifconfig {i} up" for i in ifaces)
    else:
        return [], []
    return ["sh", "-c", down], ["sh", "-c", up]


def saved_routes(ifaces: list[str]) -> list[str]:
    """IPv4 + IPv6 routes through `ifaces` (Linux). `ip link set down` flushes them; networkd/NetworkManager
    usually re-adds them on link up, a static or container setup does not, so the test puts them back."""
    if platform.system() != "Linux":
        return []
    out: list[str] = []
    for fam in ("-4", "-6"):
        for i in ifaces:
            ok, txt = _run(["ip", fam, "route", "show", "dev", i])
            if ok:
                out += [f"{fam} {ln.strip()}" + ("" if " dev " in f" {ln} " else f" dev {i}")
                        for ln in txt.splitlines()
                        if ln.strip() and not ln.startswith(("fe80::", "multicast", "ff00::"))]
    return out


def restore_routes(routes: list[str], wait_s: float = 5.0) -> dict[str, Any]:
    """After link up: wait for the network manager to re-add the routes, then `ip route replace` any that are
    still missing. Returns what was re-added and what failed."""
    def missing() -> list[str]:
        have = {fam: _run(["ip", fam, "route", "show"])[1] for fam in ("-4", "-6")}
        return [r for r in routes if r.split(" ", 1)[1].split(" dev ")[0].split()[0] not in have[r[:2]]]
    deadline = time.monotonic() + wait_s
    while missing() and time.monotonic() < deadline:
        time.sleep(0.5)
    readded, failed = [], []
    for r in missing():
        fam, rest = r.split(" ", 1)
        ok, msg = _run(["ip", fam, "route", "replace", *rest.split()])
        (readded if ok else failed).append(r if ok else f"{r}: {msg[:80]}")
    return {"readded": readded, "failed": failed}


def link_states(ifaces: list[str], sys_net: Path = Path("/sys/class/net")) -> dict[str, str]:
    """Operational state per interface: Linux /sys/class/net/<if>/operstate, macOS `ifconfig` flags."""
    out: dict[str, str] = {}
    for i in ifaces:
        if platform.system() == "Linux" or (sys_net / i).exists():
            try:
                out[i] = (sys_net / i / "operstate").read_text().strip()
            except OSError as e:
                out[i] = f"unknown ({e.__class__.__name__})"
        else:
            ok, txt = _run(["ifconfig", i])
            flags = re.search(r"flags=\w+<([^>]*)>", txt) if ok else None
            out[i] = "unknown" if not flags else ("up" if "UP" in flags.group(1).split(",") else "down")
    return out


def resolvers(resolv_conf: Path = Path("/etc/resolv.conf"), upstream_conf: Path =
              Path("/run/systemd/resolve/resolv.conf")) -> list[str]:
    """Nameservers the box would use: resolv.conf, plus systemd-resolved's real upstreams (the stub 127.0.0.53
    alone would only show that the local stub answers)."""
    out: list[str] = []
    for f in (resolv_conf, upstream_conf):
        try:
            for ln in f.read_text().splitlines():
                parts = ln.split()
                if len(parts) >= 2 and parts[0] == "nameserver" and parts[1] not in out:
                    out.append(parts[1])
        except OSError:
            continue
    return out


def dns_query_packet(name: str, qid: int = 0x5752) -> bytes:
    """A minimal RFC 1035 query: one A question, recursion desired."""
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0"
    return qid.to_bytes(2, "big") + b"\x01\x00" + b"\x00\x01\x00\x00\x00\x00\x00\x00" + q + b"\x00\x01\x00\x01"


def parse_dns_reply(data: bytes, qid: int = 0x5752) -> dict[str, Any]:
    """rcode and answer count of a reply to dns_query_packet. `answered`: the resolver reached an
    authoritative answer (NOERROR or NXDOMAIN) = its upstream path out of the factory works. SERVFAIL /
    REFUSED from a local stub whose upstream is gone is not an answer."""
    if len(data) < 12 or int.from_bytes(data[:2], "big") != qid:
        return {"ok": False, "error": "short or foreign reply"}
    rcode = data[3] & 0x0F
    answers = int.from_bytes(data[6:8], "big")
    return {"ok": True, "rcode": rcode, "answers": answers, "answered": rcode in (0, 3)}


def dns_probe(servers: list[str] | None = None, name: str | None = None, timeout_s: float = 2.0,
              port: int = 53) -> dict[str, Any]:
    """Ask every configured resolver for a fresh random name (never cached, so a stub's cache cannot fake an
    answer). With the uplink down none may answer (page 9: DNS gone too). SERVFAIL from the local stub = not
    answered; a timeout = unreachable."""
    servers = resolvers() if servers is None else servers
    name = name or f"warroom-unplug-{os.urandom(6).hex()}.{DNS_PROBE_ZONE}"
    per: dict[str, Any] = {}
    for srv in servers:
        fam = socket.AF_INET6 if ":" in srv else socket.AF_INET
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s:
                s.settimeout(timeout_s)
                s.sendto(dns_query_packet(name), (srv.split("%")[0], port))
                per[srv] = parse_dns_reply(s.recvfrom(4096)[0])
        except TimeoutError:
            per[srv] = {"ok": False, "error": "timeout"}
        except OSError as e:
            per[srv] = {"ok": False, "error": f"{e.__class__.__name__}: {e}"[:120]}
    return {"name": name, "servers": per, "resolved": any(r.get("answered") for r in per.values())}


def block_commands(custom_block: str | None, custom_unblock: str | None,
                   table_loaded: bool | None = None) -> tuple[list[str], list[str]]:
    if custom_block:
        return ["sh", "-c", custom_block], ["sh", "-c", custom_unblock or "true"]
    if platform.system() == "Linux":
        if table_loaded is None:
            table_loaded = nft_table_loaded()
        if table_loaded:
            # warroom-firewall.service already loaded it at boot: re-loading would duplicate every rule and
            # deleting it afterwards would leave the Spark with no egress allow-list until reboot (p.10).
            # Leave it alone; EgressEvidence resets and reads its counter.
            return ["true"], ["true"]
        return ["nft", "-f", str(NFT)], ["nft", "delete", "table", *NFT_COUNTER[:2]]
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
    """Probe: does a TCP connect to a public address succeed? (after blocking it must not)

    Inside the sandbox the probe runs in its own short-lived child (still sandboxed), so its deliberate,
    blocked connects carry a pid that is not in the recorded sandbox_pids and are never counted as egress."""
    if in_sandbox():
        code = f"from bench.unplug_test import _probe; raise SystemExit(0 if _probe({timeout_s!r}) else 1)"
        try:
            r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, timeout=timeout_s * len(PROBE_HOSTS) + 30)
            return r.returncode == 0
        except subprocess.TimeoutExpired:
            return False
    return _probe(timeout_s)


def _probe(timeout_s: float) -> bool:
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
        # the commander's real call; the card is the alert_card field leading its plan stream
        r = await stream_chat(ep, commander_messages(live_anomaly(ANOMALIES[i % 2])), max_tokens=COMMANDER_MAX_TOKENS,
                              response_format=commander_format(), client=cli, watch_alert_card=True,
                              stop_on_alert_card=True)
        out.append(round(r.alert_card_s, 4) if r.ok and r.alert_card_s is not None else None)
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
    async with httpx.AsyncClient(base_url=app_url, timeout=30) as c:     # scenario load takes ~5 s
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
    if in_sandbox():
        # `localhost` would make the bench (and the spawned app) look the name up; under the sandbox that is a
        # denied mDNSResponder lookup, counted as egress. 127.0.0.1 needs no lookup.
        ep = dataclasses.replace(ep, base_url=re.sub(r"//localhost(?=[:/]|$)", "//127.0.0.1", ep.base_url))
        if args.app_url:
            args.app_url = re.sub(r"//localhost(?=[:/]|$)", "//127.0.0.1", args.app_url)
    out: dict[str, Any] = {"platform": platform.platform(), "is_root": os.geteuid() == 0, "watch_s": args.watch}
    unplug = list(args.unplug or [])
    blk, unblk = block_commands(args.block_cmd, args.unblock_cmd) if (args.block or args.block_cmd) else ([], [])
    out["block"] = {"requested": bool(args.block or args.sandbox or unplug or args.block_cmd), "applied": False,
                    "kind": "sandbox" if (in_sandbox() or args.sandbox) else "unplug" if unplug
                    else "custom" if args.block_cmd else "firewall" if args.block else "none"}
    unplug_ok = False
    if unplug and not in_sandbox():
        down, _up = unplug_commands(unplug)
        routes = saved_routes(unplug)
        ok, msg = apply(down)
        states = link_states(unplug)
        unplug_ok = ok and all(v == "down" for v in states.values())
        out["block"]["unplug"] = {"ifaces": unplug, "cmd": " ".join(down), "applied": ok, "msg": msg,
                                  "link_state_after_down": states, "saved_routes": routes}
    if in_sandbox():
        out["block"].update(applied=True, method="sandbox-exec", scope=(
            "process-level: bench" + (" + spawned war-room app" if args.spawn_app else "")
            + "; outbound allowed to localhost only; model server is outside the sandbox (socket-watched)"),
            profile=SANDBOX_PROFILE)
    elif args.block or args.block_cmd:
        ok, msg = apply(blk)
        out["block"].update(applied=ok, cmd=" ".join(blk), msg=msg)
        if blk == ["true"] and not args.block_cmd:
            out["block"]["msg"] = ("boot firewall table inet warroom_offline already loaded; reused, not re-applied "
                                   "or removed. Firewall only: LAN, its DNS resolver and proxies stay up")
    if unplug and not in_sandbox():
        # an unplug alone is a block; the firewall (boot table) keeps logging what the stack still tries
        out["block"]["applied"] = unplug_ok or out["block"]["applied"]
        if not out["block"].get("cmd") and platform.system() == "Linux" and nft_table_loaded():
            out["block"]["firewall"] = "boot table inet warroom_offline loaded: drop + DNS log active"
            out["block"]["applied_firewall"] = True
    applied = out["block"]["applied"]
    firewall_logs = bool(out["block"].get("cmd") or out["block"].get("applied_firewall"))
    alert_s = None
    app_proc = None
    try:
        await stream_chat(ep, [{"role": "user", "content": "hi"}], max_tokens=1)   # model resident
        if args.spawn_app:
            app_proc, args.app_url, run_dir = spawn_app(ep)
            out["spawned_app"] = {"pid": app_proc.pid, "url": args.app_url, "run_dir": str(run_dir)}
            args.pid = (args.pid or []) + [app_proc.pid]
        ports = watch_ports(ep.base_url, args.app_url, args.watch_port)
        t0 = time.monotonic()
        with EgressEvidence(applied and (firewall_logs or in_sandbox() or platform.system() != "Linux"), lan) \
                as egress, \
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
        out["egress"] = egress.report(stack_pids=w.watched | set(args.pid or []))
        # probe AFTER the window so its own packets are not counted as the stack's egress
        out["internet_reachable_during_test"] = internet_reachable()
        # DNS: with the uplink down no resolver may still answer for a public name. Skipped in the sandbox,
        # where the probe's own denied packets would be counted against the bench's pid.
        out["dns_probe"] = None if in_sandbox() else dns_probe()
    finally:
        if app_proc is not None:
            app_proc.terminate()
            try:
                app_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                app_proc.kill()
        if (args.block or args.block_cmd) and out["block"].get("cmd") and not in_sandbox():
            ok, msg = apply(unblk)
            out["block"]["unblocked"] = ok
        if unplug and not in_sandbox():
            ok, msg = apply(unplug_commands(unplug)[1])
            out["block"]["unplug"]["restored"] = ok
            out["block"]["unplug"]["link_state_after_up"] = link_states(unplug)
            if routes:
                out["block"]["unplug"]["routes"] = restore_routes(routes)
    out["verdicts"] = verdicts(out, alert_s, bool(args.app_url))
    out["saved_to"] = str(save_result("unplug", ep, out, args.out_dir))
    if in_sandbox() and os.environ.get(RESULT_PATH_ENV):
        Path(os.environ[RESULT_PATH_ENV]).write_text(out["saved_to"], encoding="utf-8")
    return out


def count_sandbox_denies(pids: set[int], since: str, until: str,
                         detail: dict[str, Any] | None = None) -> tuple[int | None, str]:
    """Blocked outbound attempts by `pids` from `since` up to (not incl.) `until` ('...HH:MM:SS.mmm', the moment
    the internet probe starts), from the kernel Sandbox log. Must run unsandboxed. `detail`, if given, is
    filled with sandbox_deny_detail (attempts per pid, remotes) so the report names only the pids that tried."""
    end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.mktime(time.strptime(until[:19], "%Y-%m-%d %H:%M:%S")) + 1))
    ok, txt = _run(["/usr/bin/log", "show", "--style", "compact", "--start", since, "--end", end,
                    "--predicate", 'eventMessage CONTAINS "network-outbound"'], timeout=120)
    if ok and detail is not None:
        detail.update(sandbox_deny_detail(txt, pids, before=until))
    return (parse_sandbox_denies(txt, pids, before=until), "") if ok else (None, txt[:200])


def finalize_sandboxed(path: Path) -> dict[str, Any]:
    """Parent side of --sandbox: fill the deny count into the child's result, re-judge, re-save in place."""
    out = json.loads(path.read_text(encoding="utf-8"))
    eg = out["egress"]
    detail: dict[str, Any] = {}
    n, err = count_sandbox_denies(set(eg.get("sandbox_pids", [])), *eg["window"], detail=detail)
    eg["firewall_dropped_packets"] = n
    if detail:
        eg["denies_by_pid"] = {str(k): v for k, v in detail["by_pid"].items()}
        eg["deny_remotes"] = detail["remotes"]
        eg["dns_by_pid"] = {str(k): v for k, v in detail.get("dns_by_pid", {}).items()}
        eg["no_remote_denies_by_pid"] = {str(k): v for k, v in detail["no_remote_by_pid"].items()}
    roles = {str(p): "sandboxed child" for p in eg.get("sandbox_pids", [])}
    if eg.get("bench_pid"):
        roles[str(eg["bench_pid"])] = "bench"
    if (out.get("spawned_app") or {}).get("pid"):
        roles[str(out["spawned_app"]["pid"])] = "war-room app"
    eg["pid_roles"] = roles
    eg["counter_source"] = "kernel Sandbox log: deny(1) network-outbound for the sandboxed pids"
    if err:
        eg.setdefault("notes", []).append(f"sandbox log unreadable: {err}")
    out["saved_to"] = str(path)
    alert = next((v for v in out["verdicts"] if v["name"] == "alert_offline"), {})
    out["verdicts"] = verdicts(out, alert.get("measured"), "app" in out)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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
        # Info only: the page-10 TTFT target is defined at 32k context (judged by bench/ttft.py); this is
        # the app's own commander call, whose prompt is ~5-6k tokens, so it is not compared with that target.
        ptoks = cc.get("prompt_tokens")
        vs.append(Verdict("commander_ttft_app_prompt", f"info (≤ {TTFT_TARGET_S} s target is at 32k; see ttft bench)",
                          ttft_s, None,
                          note=("from the event's llm_call audit entry" if cc else "no llm_call audit entry")
                          + (f"; prompt_tokens={ptoks}" if ptoks else "")
                          + (f"; mode={cc.get('mode')}" if cc.get("mode") else ""), extra=cc).__dict__)
    else:
        vs.append(Verdict("alert_offline", f"≤ {LATENCY_FIRST_ALERT_S} s", alert_s,
                          None if alert_s is None else alert_s <= LATENCY_FIRST_ALERT_S,
                          note="direct alert-card call" + ("" if alert_s is not None else ": no card")).__dict__)
    sk = out["sockets"]
    eg = out["egress"]
    flows = len(eg["conntrack_outbound_flows"]) if eg["conntrack_available"] else None
    stack_attempts = eg.get("stack_drop_attempts")          # Linux kernel drop log, attributed
    stack_dns = eg.get("stack_dns_queries") or []
    if stack_attempts is not None:
        # Attempts, not packets; one blocked connect seen by several sources is one attempt. A DNS query from
        # the app is both DNS-logged and drop-logged: one attempt.
        def key(a: dict[str, Any]) -> tuple:
            return (a.get("proto"), a.get("src"), a.get("sport"), a.get("dst"), a.get("dport"))
        att = {key(a): a for a in stack_attempts} | {key(a): a for a in stack_dns}
        sock_att = {("sock", o.get("pid"), o.get("ip"), o.get("port")) for o in sk.get("outbound", [])}
        drop_dst = {(a["dst"], a["dport"]) for a in att.values()}
        sock_att = {t for t in sock_att if (t[2], t[3]) not in drop_dst}   # SYN_SENT of a dropped connect
        flow_new = [f for f in eg["conntrack_outbound_flows"] if (f.get("dst"), f.get("dport")) not in drop_dst]
        dropped = len(att)
        measured = dropped + len(sock_att) + len(flow_new)
        source = (f"kernel drop + DNS log: {len(stack_attempts)} stack attempt(s), {len(stack_dns)} stack DNS "
                  f"quer(y/ies) (containers + stack-UID host); {len(eg.get('host_other_attempts') or [])} DGX OS "
                  f"host attempt(s) and {len(eg.get('host_other_dns_queries') or [])} OS DNS quer(y/ies) not counted")
    else:
        dropped = eg.get("container_dropped_packets", eg["firewall_dropped_packets"])
        if dropped is not None and eg.get("dns_container_packets"):
            dropped += eg["dns_container_packets"]
        measured = sk["outbound_connections"] + (dropped or 0) + (flows or 0)
        source = ("sandbox-exec deny log (process-level; model server socket-watched only)"
                  if out["block"].get("method") == "sandbox-exec"
                  else eg.get("counter_source") or "firewall drop counter")
    blk = out["block"]
    kind = blk.get("kind") or ("sandbox" if blk.get("method") == "sandbox-exec" else "firewall")
    unp = blk.get("unplug") or {}
    unplugged = bool(unp.get("applied")) and all(v == "down" for v in (unp.get("link_state_after_down") or {"x": ""}).values())
    dns = out.get("dns_probe")
    tier = ("page-9 run: uplink down" if unplugged
            else "secondary check: firewall only, LAN + its DNS resolver stay up" if kind in ("firewall", "custom")
            else "secondary check: process-level sandbox" if kind == "sandbox" else "")
    evidence = {"socket_samples": sk["outbound_connections"], "stack_dropped": dropped,
                "stack_dns_queries": len(stack_dns), "tier": tier,
                "firewall_dropped_packets_box_wide": eg["firewall_dropped_packets"],
                "host_other_attempts": len(eg.get("host_other_attempts") or []),
                "conntrack_outbound_flows": flows}
    if not applied:
        passed, note = None, ("network NOT blocked (needs root, or the block command failed: see block.msg / "
                              "block.unplug.msg); socket samples only, which miss short connections — not "
                              "evidence of 0 outbound")
    elif dropped is None:
        passed, note = None, "block applied but firewall counter unreadable; socket samples only"
    else:
        passed = measured == 0
        note = source + " + socket samples" + (" + conntrack" if flows is not None else "")
        if tier:
            note += f" [{tier}]"
    vs.append(Verdict("outbound_connections", "0", measured, passed, note=note, extra=evidence).__dict__)
    if applied:
        vs.append(Verdict("internet_blocked", "unreachable", out["internet_reachable_during_test"],
                          not out["internet_reachable_during_test"],
                          note="" if unplugged else "passes by construction under a firewall/sandbox block; "
                          "meaningful only with --unplug").__dict__)
    if dns is not None and applied:
        vs.append(Verdict("dns_unreachable", "no resolver answers", dns.get("resolved"),
                          (not dns.get("resolved")) if unplugged else None,
                          note=("resolvers: " + ", ".join(f"{k}={v.get('error') or ('answered' if v.get('answered') else 'rcode ' + str(v.get('rcode')))}"
                                                         for k, v in (dns.get("servers") or {}).items())
                                + ("" if unplugged else " (info: the LAN resolver is meant to stay up without --unplug)"))
                          ).__dict__)
    if unplugged:
        p9 = None if passed is None else (passed and not out["internet_reachable_during_test"]
                                          and not (dns or {}).get("resolved"))
        vs.append(Verdict("page9_unplug", "link down, 0 outbound, no DNS", unp.get("ifaces"), p9,
                          note="uplink(s) down for the whole window: " + ", ".join(unp.get("ifaces") or [])
                          + ("" if p9 is not None else "; not judged: no firewall drop/DNS log evidence of 0 "
                             "outbound (load the boot nft table, or add --block)")).__dict__)
    else:
        why = ("uplink did not go down: " + json.dumps(unp.get("link_state_after_down"))) if unp else \
            "not exercised: no --unplug, so the cable was never pulled (LAN, DNS and proxies stayed up)"
        vs.append(Verdict("page9_unplug", "link down, 0 outbound, no DNS", None, None,
                          note=why + "; re-run with --unplug <uplink> as root").__dict__)
    return vs


def main(argv: list[str] | None = None) -> dict[str, Any]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url")
    p.add_argument("--model")
    p.add_argument("--block", action="store_true",
                   help="firewall only (secondary check): boot nft table / pf anchor; LAN and DNS stay up")
    p.add_argument("--unplug", action="append", metavar="IFACE",
                   help="page-9 run: take this uplink down for the window (repeat for each uplink); root")
    p.add_argument("--sandbox", action="store_true", help="macOS, no root: run inside sandbox-exec, offline")
    p.add_argument("--spawn-app", action="store_true", help="start the war-room app as a child and test it")
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
    args = p.parse_args(argv)
    if args.sandbox and not in_sandbox():
        if platform.system() != "Darwin" or not shutil.which("sandbox-exec"):
            p.error("--sandbox needs macOS sandbox-exec; on Linux use --block")
        with tempfile.NamedTemporaryFile("r", suffix=".path") as f:
            env = os.environ | {SANDBOX_ENV: "1", RESULT_PATH_ENV: f.name}
            r = subprocess.run(sandbox_argv(sys.argv[1:] if argv is None else list(argv)), env=env)
            saved = Path(f.name).read_text(encoding="utf-8").strip()
        if r.returncode != 0 or not saved:
            raise SystemExit(f"sandboxed run failed (exit {r.returncode})")
        out = finalize_sandboxed(Path(saved))
        print("-- after reading the Sandbox deny log outside the sandbox:")
    else:
        out = run(main_async(args))
    for v in out["verdicts"]:
        print(f"{v['name']:<22} target {v['target']:<8} measured {v['measured']}  pass={v['passed']}  {v['note']}")
    print("block:", out["block"], " watched pids:", out["sockets"]["watched_pids"],
          " ignored:", out["sockets"]["ignored_pids"], " egress:", out["egress"])
    print("saved", out["saved_to"])
    return out


if __name__ == "__main__":
    main()
