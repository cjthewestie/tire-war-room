#!/usr/bin/env bash
# Apply an offline update bundle on the Spark, with automatic rollback if the new release is unhealthy.
#   sudo deploy/update/offline_update.sh /media/usb/warroom-<ver>.tar
# Steps: verify SHA256SUMS -> unpack to releases/<ver> -> docker load images -> carry .env over and pin
# APP_IMAGE=warroom-app:<ver> -> switch `current` symlink -> restart stack -> health check.
# Unhealthy within HEALTH_TIMEOUT_S -> deploy/update/rollback.sh to the previous release.
# Data (/opt/warroom/data: audit log, schedules, qdrant) is outside releases and is never touched.
# Env: WARROOM_ROOT=/opt/warroom  HEALTH_URL=http://127.0.0.1:8000/api/health  HEALTH_TIMEOUT_S=180
#      RESTART_CMD="systemctl restart warroom-stack warroom-preload"  DOCKER=docker
set -euo pipefail
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }
BUNDLE="${1:?usage: offline_update.sh <warroom-ver.tar>}"
ROOT="${WARROOM_ROOT:-/opt/warroom}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-180}"
RESTART_CMD="${RESTART_CMD:-systemctl restart warroom-stack warroom-preload}"
DOCKER="${DOCKER:-docker}"
HERE="$(cd "$(dirname "$0")" && pwd)"

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
tar -C "$work" -xf "$BUNDLE"
( cd "$work" && sha256 -c SHA256SUMS >/dev/null ) || { echo "update: checksum mismatch, nothing changed" >&2; exit 3; }
VER="$(sed -n 's/^version=//p' "$work/MANIFEST")"
[[ -n "$VER" ]] || { echo "update: MANIFEST has no version" >&2; exit 3; }
REL="$ROOT/releases/$VER"
PREV="$(readlink "$ROOT/current" 2>/dev/null || true)"
if [[ "$PREV" == "$REL" ]]; then echo "update: $VER already current"; exit 0; fi

rm -rf "$REL.tmp"; mkdir -p "$REL.tmp"
tar -C "$REL.tmp" -xzf "$work/src.tar.gz"
if [[ -f "$work/images.tar" ]]; then $DOCKER load -i "$work/images.tar"; fi
if [[ -n "$PREV" && -f "$PREV/deploy/spark/.env" ]]; then cp "$PREV/deploy/spark/.env" "$REL.tmp/deploy/spark/.env"
elif [[ -f "$REL.tmp/deploy/spark/.env.example" ]]; then cp "$REL.tmp/deploy/spark/.env.example" "$REL.tmp/deploy/spark/.env"
else mkdir -p "$REL.tmp/deploy/spark"; : >"$REL.tmp/deploy/spark/.env"; fi
if grep -q '^APP_IMAGE=' "$REL.tmp/deploy/spark/.env"; then
  sed -i.bak "s|^APP_IMAGE=.*|APP_IMAGE=warroom-app:$VER|" "$REL.tmp/deploy/spark/.env" && rm -f "$REL.tmp/deploy/spark/.env.bak"
else echo "APP_IMAGE=warroom-app:$VER" >>"$REL.tmp/deploy/spark/.env"; fi
cp "$work/MANIFEST" "$REL.tmp/MANIFEST"
rm -rf "$REL"; mv "$REL.tmp" "$REL"

[[ -n "$PREV" ]] && echo "$PREV" >"$ROOT/releases/.previous"
ln -sfn "$REL" "$ROOT/current"
echo "update: current -> $REL (previous: ${PREV:-none}); restarting"
sh -c "$RESTART_CMD"

deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
until curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; do
  if (( $(date +%s) > deadline )); then
    echo "update: $VER unhealthy after ${HEALTH_TIMEOUT_S}s -> rolling back" >&2
    [[ -n "$PREV" ]] && "$HERE/rollback.sh" "$(basename "$PREV")"
    exit 4
  fi
  sleep 2
done
echo "update: $VER healthy"
