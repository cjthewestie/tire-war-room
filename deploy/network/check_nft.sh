#!/bin/sh
# Load nftables-warroom.nft into a real Linux kernel (a throwaway container's network namespace) and check,
# per case, which counter moves. Runs on a Mac or the Spark; needs only docker:
#   deploy/network/check_nft.sh
# Expected: the app uid (10001) is dropped for a LAN resolver, a LAN proxy and the internet, passes for an
# allowed peer and loopback; every DNS query shows in dns_host; root (DGX OS stand-in) still reaches the LAN.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
# tools baked once into a cached image: apk at test time is slow, and nothing may need the network once
# the table is loaded
printf 'FROM alpine:3.20\nRUN apk add --no-cache nftables bind-tools python3 && adduser -D -u 10001 app\n' |
  docker build -q -t warroom-nftcheck - >/dev/null
exec docker run --rm --cap-add NET_ADMIN -v "$here":/n:ro warroom-nftcheck sh -euc '
mkdir -p /etc/warroom/peers.d
echo "add element inet warroom_offline stack_peers4 { 192.168.10.5 }" > /etc/warroom/peers.d/plant.nft
nft -f /n/nftables-warroom.nft
nft list set inet warroom_offline stack_peers4 | grep -q 192.168.10.5 || { echo "FAIL peers include"; exit 1; }
conn() { python3 -c "import socket,sys;s=socket.socket();s.settimeout(1);s.connect_ex((sys.argv[1],int(sys.argv[2])))" "$1" "$2"; }
as_app() { su app -s /bin/sh -c "$*"; }
ctr() { nft list counter inet warroom_offline "$1" | sed -n "s/.*packets \([0-9]*\).*/\1/p"; }
fails=0
check() {  # name, expect_drop(0/1), expect_dns(0/1)
  sleep 1; d=$(ctr egress_host); q=$(ctr dns_host)
  got_d=$([ "$d" -gt 0 ] && echo 1 || echo 0); got_q=$([ "$q" -gt 0 ] && echo 1 || echo 0)
  ok=PASS; [ "$got_d" = "$2" ] && [ "$got_q" = "$3" ] || { ok=FAIL; fails=$((fails+1)); }
  printf "%-4s %-34s egress_host=%-3s dns_host=%s\n" "$ok" "$1" "$d" "$q"
  nft reset counters table inet warroom_offline >/dev/null
}
nft reset counters table inet warroom_offline >/dev/null
as_app "dig +time=1 +tries=1 @10.0.0.53 example.com >/dev/null 2>&1 || true";       check "app: DNS udp -> LAN resolver" 1 1
as_app "dig +tcp +time=1 +tries=1 @10.0.0.53 example.com >/dev/null 2>&1 || true"; check "app: DNS tcp -> LAN resolver" 1 1
as_app "$(command -v python3) -c \"import socket;s=socket.socket();s.settimeout(1);s.connect_ex((\\\"192.168.1.9\\\",3128))\""; check "app: CONNECT -> LAN proxy" 1 0
as_app "$(command -v python3) -c \"import socket;s=socket.socket();s.settimeout(1);s.connect_ex((\\\"1.1.1.1\\\",443))\"";     check "app: TCP -> internet" 1 0
as_app "$(command -v python3) -c \"import socket;s=socket.socket();s.settimeout(1);s.connect_ex((\\\"192.168.10.5\\\",1883))\""; check "app: TCP -> allowed peer" 0 0
as_app "$(command -v python3) -c \"import socket;s=socket.socket();s.settimeout(1);s.connect_ex((\\\"127.0.0.1\\\",9))\"";     check "app: TCP -> loopback" 0 0
conn 192.168.1.9 3128;                                                              check "root: TCP -> LAN" 0 0
dig +time=1 +tries=1 @10.0.0.53 example.com >/dev/null 2>&1 || true;                check "root: DNS -> LAN resolver" 0 1
conn 1.1.1.1 443;                                                                   check "root: TCP -> internet" 1 0
echo "failures: $fails"; [ "$fails" -eq 0 ]
'
