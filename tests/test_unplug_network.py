"""Unplug test can see LAN-side egress: DNS queries are logged and counted, the war-room UID gets no blanket
LAN pass, --unplug really takes the uplink down and is the only run that yields the page-9 verdict."""
from __future__ import annotations

import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from bench import unplug_test as u

NFT = Path(__file__).resolve().parent.parent / "deploy" / "network" / "nftables-warroom.nft"
PF = NFT.with_name("pf-warroom.conf")


def _chain(text: str, name: str) -> list[str]:
    body = re.search(r"chain " + name + r" \{(.*?)\n    \}", text, re.DOTALL).group(1)
    return [ln.strip() for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith(("#", "type filter hook"))]


def test_nft_logs_dns_before_any_accept_and_narrows_the_app_uid():
    text = NFT.read_text()
    out = _chain(text, "output")
    dns = [i for i, r in enumerate(out) if "dport 53" in r]
    lan = next(i for i, r in enumerate(out) if r.startswith("ip daddr @lan4 accept"))
    app_drop = next(i for i, r in enumerate(out) if r.startswith("meta skuid 10001 counter") and r.endswith("drop"))
    assert len(dns) == 2 and all('log prefix "warroom-dns host "' in out[i] and "skuid" in out[i] for i in dns)
    # only loopback comes before the DNS log; the app UID is dropped before the RFC1918 pass
    assert out[0] == 'oifname "lo" accept' and max(dns) < app_drop < lan
    assert not any("accept" in r for r in out[1:min(dns)])
    assert f"meta skuid {u.APP_UID} ct state established,related accept" in out
    fwd = _chain(text, "forward")
    fdns = [i for i, r in enumerate(fwd) if "dport 53" in r]
    flan = next(i for i, r in enumerate(fwd) if r.startswith("ip daddr @lan4 accept"))
    assert len(fdns) == 4 and max(fdns) < flan
    assert any(r.startswith('iifname "br-*" counter') and r.endswith("drop") for r in fwd[:flan])
    # stack peers start empty; the include is a glob, so a box without peers files still loads
    assert re.search(r"set stack_peers4 \{\s*type ipv4_addr; flags interval;\s*\}", text)
    assert 'include "/etc/warroom/peers.d/*.nft"' in text
    for name in ("dns_host", "dns_containers", "egress_host", "egress_containers", "dropped_egress"):
        assert f"counter {name} {{ }}" in text


def test_pf_anchor_labels_dns():
    text = PF.read_text()
    rules = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    dns = next(i for i, r in enumerate(rules) if 'label "warroom-dns"' in r)
    assert dns < next(i for i, r in enumerate(rules) if r.startswith("pass out quick to <lan>"))
    pf = ('pass out log quick proto udp from any to <lan> port = 53 keep state label "warroom-dns"\n'
          "  [ Evaluations: 10  Packets: 4  Bytes: 300  States: 0 ]\n"
          'pass out log quick proto tcp from any to <lan> port = 53 flags S/SA keep state label "warroom-dns"\n'
          "  [ Evaluations: 10  Packets: 2  Bytes: 120  States: 0 ]\n"
          "pass out quick from any to <lan> flags S/SA keep state\n"
          "  [ Evaluations: 10  Packets: 99  Bytes: 1  States: 0 ]\n")
    assert u.parse_pf_dns_packets(pf) == 6 and u.parse_pf_dns_packets("") is None


_LOG = """\
2026-10-03T02:14:01+0800 spark kernel: warroom-dns host IN= OUT=enP7s7 SRC=192.168.1.20 DST=192.168.1.1 LEN=60 PROTO=UDP SPT=41000 DPT=53 LEN=40 UID=10001 GID=999
2026-10-03T02:14:01+0800 spark kernel: warroom-egress-drop host IN= OUT=enP7s7 SRC=192.168.1.20 DST=192.168.1.1 LEN=60 PROTO=UDP SPT=41000 DPT=53 LEN=40 UID=10001 GID=999
2026-10-03T02:14:02+0800 spark kernel: warroom-dns host IN= OUT=enP7s7 SRC=192.168.1.20 DST=192.168.1.1 LEN=60 PROTO=UDP SPT=42000 DPT=53 LEN=40 UID=101 GID=101
2026-10-03T02:14:03+0800 spark kernel: warroom-dns fwd IN=br-edge OUT=enP7s7 SRC=172.19.0.4 DST=192.168.1.1 LEN=60 PROTO=UDP SPT=43000 DPT=53 LEN=40
2026-10-03T02:14:04+0800 spark kernel: warroom-egress-drop fwd IN=br-edge OUT=enP7s7 SRC=172.19.0.4 DST=192.168.1.1 LEN=60 PROTO=UDP SPT=43000 DPT=53 LEN=40
"""


def test_dns_log_is_parsed_attributed_and_deduped_with_its_drop():
    dns = u.parse_dns_log(_LOG)
    assert len(dns) == 3 and all(a["kind"] == "dns" for a in dns)
    assert len(u.parse_drop_log(_LOG)) == 2                    # the drop parser ignores the DNS lines
    stack, other = u.attribute_drops(dns, {u.APP_UID})
    assert {a["sport"] for a in stack} == {41000, 43000} and [a["uid"] for a in other] == [101]
    drops, _ = u.attribute_drops(u.parse_drop_log(_LOG), {u.APP_UID})
    out = {"block": {"applied": True, "kind": "firewall"}, "internet_reachable_during_test": False, "app": {},
           "sockets": {"outbound_connections": 0, "outbound": []},
           "egress": {"firewall_dropped_packets": 2, "stack_drop_attempts": drops, "stack_dns_queries": stack,
                      "host_other_attempts": [], "host_other_dns_queries": other,
                      "conntrack_available": False, "conntrack_outbound_flows": []}}
    v = {x["name"]: x for x in u.verdicts(out, 0.5, False)}
    # app query (DNS-logged + dropped) = 1, container query (DNS-logged + dropped) = 1; resolved's own = 0
    assert v["outbound_connections"]["measured"] == 2 and v["outbound_connections"]["passed"] is False
    assert v["outbound_connections"]["extra"]["stack_dns_queries"] == 2
    assert "secondary check" in v["outbound_connections"]["note"]


def test_a_dns_query_the_lan_resolver_answered_still_counts():
    # the query went out (accepted by the LAN pass for a non-stack path, e.g. a container on an old table)
    dns = u.parse_dns_log(_LOG.splitlines()[3])
    out = {"block": {"applied": True, "kind": "firewall"}, "internet_reachable_during_test": False, "app": {},
           "sockets": {"outbound_connections": 0, "outbound": []},
           "egress": {"firewall_dropped_packets": 0, "stack_drop_attempts": [], "stack_dns_queries": dns,
                      "conntrack_available": False, "conntrack_outbound_flows": []}}
    v = {x["name"]: x for x in u.verdicts(out, 0.5, False)}["outbound_connections"]
    assert v["measured"] == 1 and v["passed"] is False
    # log unreadable: the container DNS packet counter is counted with the container drops
    out["egress"] = {"firewall_dropped_packets": 0, "container_dropped_packets": 0, "dns_container_packets": 3,
                     "conntrack_available": False, "conntrack_outbound_flows": []}
    assert {x["name"]: x for x in u.verdicts(out, 0.5, False)}["outbound_connections"]["measured"] == 3


def _base(kind="firewall", unplug=None, reach=False, dns=None):
    blk = {"applied": True, "kind": kind}
    if unplug:
        blk["unplug"] = unplug
    return {"block": blk, "internet_reachable_during_test": reach, "app": {}, "dns_probe": dns,
            "sockets": {"outbound_connections": 0, "outbound": []},
            "egress": {"firewall_dropped_packets": 0, "stack_drop_attempts": [], "stack_dns_queries": [],
                       "host_other_attempts": [], "conntrack_available": False, "conntrack_outbound_flows": []}}


def test_firewall_only_run_never_yields_the_page9_verdict():
    dns_up = {"servers": {"192.168.1.1": {"ok": True, "rcode": 3, "answered": True}}, "resolved": True}
    v = {x["name"]: x for x in u.verdicts(_base(dns=dns_up), 0.5, False)}
    assert v["outbound_connections"]["passed"] is True
    assert v["page9_unplug"]["passed"] is None and "not exercised" in v["page9_unplug"]["note"]
    assert v["dns_unreachable"]["passed"] is None                    # LAN resolver up by design: info only
    assert "by construction" in v["internet_blocked"]["note"]


def test_unplugged_run_is_the_page9_verdict():
    down = {"ifaces": ["enP7s7"], "applied": True, "link_state_after_down": {"enP7s7": "down"}}
    dead = {"servers": {"127.0.0.53": {"ok": True, "rcode": 2, "answered": False},
                        "192.168.1.1": {"ok": False, "error": "OSError: [Errno 101] Network is unreachable"}},
            "resolved": False}
    v = {x["name"]: x for x in u.verdicts(_base("unplug", down, dns=dead), 0.5, False)}
    assert v["page9_unplug"]["passed"] is True and v["page9_unplug"]["measured"] == ["enP7s7"]
    assert v["dns_unreachable"]["passed"] is True and "uplink down" in v["outbound_connections"]["note"]
    # a resolver still answering means the cable was not the only way out: page 9 fails
    alive = {"servers": {"10.0.0.53": {"ok": True, "rcode": 3, "answered": True}}, "resolved": True}
    v = {x["name"]: x for x in u.verdicts(_base("unplug", down, dns=alive), 0.5, False)}
    assert v["dns_unreachable"]["passed"] is False and v["page9_unplug"]["passed"] is False
    # the link did not go down: no page-9 claim either way
    up = {**down, "link_state_after_down": {"enP7s7": "up"}}
    v = {x["name"]: x for x in u.verdicts(_base("unplug", up, dns=dead), 0.5, False)}
    assert v["page9_unplug"]["passed"] is None and "did not go down" in v["page9_unplug"]["note"]


def test_unplug_commands_per_platform():
    down, up = u.unplug_commands(["enP7s7", "wlan0"], system="Linux")
    assert down == ["sh", "-c", "ip link set dev enP7s7 down && ip link set dev wlan0 down"]
    assert up == ["sh", "-c", "ip link set dev enP7s7 up; ip link set dev wlan0 up"]
    down, up = u.unplug_commands(["en0"], system="Darwin")
    assert down[-1] == "ifconfig en0 down" and up[-1] == "ifconfig en0 up"
    with pytest.raises(ValueError):
        u.unplug_commands(["eth0; rm -rf /"], system="Linux")


def test_link_states_reads_operstate(tmp_path):
    (tmp_path / "eth9").mkdir()
    (tmp_path / "eth9" / "operstate").write_text("down\n")
    assert u.link_states(["eth9"], sys_net=tmp_path) == {"eth9": "down"}


def test_resolvers_include_systemd_upstreams(tmp_path):
    (tmp_path / "stub").write_text("nameserver 127.0.0.53\noptions edns0\n")
    (tmp_path / "up").write_text("# managed\nnameserver 192.168.1.1\nnameserver 127.0.0.53\n")
    assert u.resolvers(tmp_path / "stub", tmp_path / "up") == ["127.0.0.53", "192.168.1.1"]
    assert u.resolvers(tmp_path / "missing", tmp_path / "missing2") == []


def test_dns_probe_against_a_local_fake_resolver():
    """NOERROR/NXDOMAIN = the resolver reached an answer; SERVFAIL (stub whose upstream is gone) = not."""
    def serve(rcode: int):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))

        def run():
            data, addr = s.recvfrom(512)
            assert data[13:].startswith(b"warroom-unplug-")
            s.sendto(data[:2] + bytes([0x81, 0x80 | rcode]) + data[4:], addr)
            s.close()
        threading.Thread(target=run, daemon=True).start()
        return s.getsockname()[1]

    for rcode, answered in ((3, True), (0, True), (2, False)):
        r = u.dns_probe(["127.0.0.1"], timeout_s=2, port=serve(rcode))
        assert r["servers"]["127.0.0.1"]["answered"] is answered and r["resolved"] is answered
        assert r["name"].endswith(".example.com")
    assert u.dns_probe([], timeout_s=0.1)["resolved"] is False
    assert u.parse_dns_reply(b"\x00") == {"ok": False, "error": "short or foreign reply"}


def test_sandbox_deny_of_mdnsresponder_is_a_counted_lookup():
    log = ("2026 E kernel[0:1] (Sandbox) Sandbox: Python(86447) deny(1) network-outbound "
           "/private/var/run/mDNSResponder\n"
           "2026 E kernel[0:1] (Sandbox) Sandbox: Python(86447) deny(1) network-outbound remote:*:443\n")
    d = u.sandbox_deny_detail(log, {86447})
    assert d["dns_by_pid"] == {86447: 1} and d["by_pid"] == {86447: 2}
    assert u.parse_sandbox_denies(log, {86447}) == 2
    assert u.SANDBOX_PROFILE.rstrip().endswith(
        '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))')


@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("sandbox-exec"), reason="macOS only")
def test_sandbox_profile_blocks_name_lookups_but_not_localhost():
    code = ("import socket\n"
            "print('LOCAL', socket.getaddrinfo('localhost', 80)[0][4][0])\n"
            "try:\n    socket.getaddrinfo('example.com', 443); print('RESOLVED')\n"
            "except socket.gaierror as e:\n    print('DENIED', e.errno)\n")
    r = subprocess.run(["sandbox-exec", "-p", u.SANDBOX_PROFILE, sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0].startswith("LOCAL") and "DENIED" in r.stdout, r.stdout


def test_saved_routes_formats_lines_once(monkeypatch):
    monkeypatch.setattr(u.platform, "system", lambda: "Linux")
    outs = {("-4", "eth0"): "default via 10.0.0.1 proto dhcp\n10.0.0.0/24 proto kernel scope link src 10.0.0.5",
            ("-6", "eth0"): "fe80::/64 proto kernel metric 256\n2001:db8::/64 dev eth0 proto ra"}
    monkeypatch.setattr(u, "_run", lambda cmd, timeout=10: (True, outs[(cmd[1], cmd[-1])]))
    assert u.saved_routes(["eth0"]) == ["-4 default via 10.0.0.1 proto dhcp dev eth0",
                                        "-4 10.0.0.0/24 proto kernel scope link src 10.0.0.5 dev eth0",
                                        "-6 2001:db8::/64 dev eth0 proto ra"]
