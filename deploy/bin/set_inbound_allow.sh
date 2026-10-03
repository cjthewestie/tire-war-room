#!/usr/bin/env bash
# Site values for the inbound allow-list (deploy/network/nftables-warroom-inbound.nft) and reload it.
#   sudo MQTT_GATEWAYS=192.168.10.5 APP_CLIENTS=192.168.10.0/24 deploy/bin/set_inbound_allow.sh
#   MQTT_GATEWAYS  comma-separated IPv4 addresses/CIDRs allowed to reach the broker on :1883 (the plant
#                  Modbus/OPC UA -> MQTT data gateway). Empty/unset = nobody from the LAN.
#   APP_CLIENTS    comma-separated IPv4 CIDRs allowed to reach the app on :8000 (supervisor phones,
#                  plant PCs). Unset = keep the RFC1918 default.
# Writes ${INBOUND_DIR:-/etc/warroom/inbound.d}/allow.nft, then reloads the table unless NO_RELOAD=1.
set -euo pipefail
DIR="${INBOUND_DIR:-/etc/warroom/inbound.d}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)/deploy/network/nftables-warroom-inbound.nft"
die() { echo "set_inbound_allow: $*" >&2; exit 1; }

check_list() {   # $1 name, $2 value: IPv4 or IPv4/CIDR, comma-separated
  local item
  IFS=',' read -ra items <<<"$2"
  for item in ${items[@]+"${items[@]}"}; do
    item="${item// /}"
    [[ -z "$item" ]] && continue
    [[ "$item" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}(/([0-9]|[12][0-9]|3[0-2]))?$ ]] || die "$1: '$item' is not an IPv4 address or CIDR"
    if [[ "$item" == 0.0.0.0/0 ]]; then die "$1: 0.0.0.0/0 would open the port to every source"; fi
  done
}
norm() { local v="${1// /}"; v="${v#,}"; echo "${v%,}" | sed 's/,,*/,/g; s/,/, /g'; }

gw="$(norm "${MQTT_GATEWAYS:-}")"
check_list MQTT_GATEWAYS "$gw"
mkdir -p "$DIR"
{
  echo "# written by deploy/bin/set_inbound_allow.sh; loaded by the include in nftables-warroom-inbound.nft"
  echo "flush set inet warroom_inbound mqtt_gateways"
  if [[ -n "$gw" ]]; then echo "add element inet warroom_inbound mqtt_gateways { $gw }"; fi
  if [[ -n "${APP_CLIENTS+x}" ]]; then
    ac="$(norm "$APP_CLIENTS")"
    [[ -n "$ac" ]] || die "APP_CLIENTS is set but empty: the supervisor phone could not reach the app (unset it to keep the default)"
    check_list APP_CLIENTS "$ac"
    echo "flush set inet warroom_inbound app_clients"
    echo "add element inet warroom_inbound app_clients { $ac }"
  fi
} >"$DIR/allow.nft.tmp"
mv "$DIR/allow.nft.tmp" "$DIR/allow.nft"
echo "wrote $DIR/allow.nft"; cat "$DIR/allow.nft"
if [[ "${NO_RELOAD:-0}" != 1 ]]; then
  nft -c -f "$SRC" || die "the table does not load with these values; nothing changed in the kernel"
  nft delete table inet warroom_inbound 2>/dev/null || true
  nft -f "$SRC"
  nft list set inet warroom_inbound mqtt_gateways
fi
