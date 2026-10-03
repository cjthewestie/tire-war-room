#!/usr/bin/env bash
# First install on the DGX Spark (run as root from an unpacked release or a git checkout).
# Layout:  /opt/warroom/releases/<ver>/   code, one dir per release (offline_update.sh adds more)
#          /opt/warroom/current -> releases/<ver>
#          /opt/warroom/data/{runtime,qdrant}   audit log, schedules, vector store (survive updates/rollbacks)
#          /opt/warroom/hf                      model weights (deploy/bin/fetch_models.sh)
#   sudo deploy/bin/install_spark.sh [version]
# Images: warroom-firewall.service drops all non-LAN egress (dockerd included) from the next boot on, so
# every image the stack needs is pulled/built HERE, while the box is still online, and the install fails
# if one is missing. Offline install: `docker load -i images.tar` first, then SKIP_PULL=1.
# Env: DOCKER=docker, SKIP_PULL=1 (verify only), NO_IMAGES=1 (skip the image step: tests, no docker).
set -euo pipefail
ROOT="${WARROOM_ROOT:-/opt/warroom}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
VER="${1:-$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
DOCKER="${DOCKER:-docker}"

mkdir -p "$ROOT/releases" "$ROOT/data/runtime" "$ROOT/data/qdrant" "$ROOT/hf"
if [[ ! -d "$ROOT/releases/$VER" ]]; then
  mkdir -p "$ROOT/releases/$VER"
  tar -C "$SRC" --exclude=.git --exclude=node_modules --exclude=.venv --exclude='data/runtime' -cf - . \
    | tar -C "$ROOT/releases/$VER" -xf -
fi
[[ -f "$ROOT/releases/$VER/deploy/spark/.env" ]] || cp "$ROOT/releases/$VER/deploy/spark/.env.example" \
  "$ROOT/releases/$VER/deploy/spark/.env"
ln -sfn "$ROOT/releases/$VER" "$ROOT/current"
chown -R 10001 "$ROOT/data/runtime" 2>/dev/null || true   # app container user

if [[ "${NO_IMAGES:-0}" != 1 ]]; then
  compose=("$DOCKER" compose --project-directory "$ROOT/current/deploy/spark")
  if [[ "${SKIP_PULL:-0}" != 1 ]]; then
    echo "pulling vllm + qdrant and building app while still online (the boot firewall blocks registries)"
    "${compose[@]}" pull vllm qdrant
    "${compose[@]}" build app
  fi
  images=$("${compose[@]}" config --images)
  [[ -n "$images" ]] || { echo "install: compose lists no images" >&2; exit 1; }
  missing=0
  while read -r img; do
    [[ -z "$img" ]] && continue
    if "$DOCKER" image inspect "$img" >/dev/null 2>&1; then echo "image ok: $img"
    else echo "install: image MISSING: $img" >&2; missing=1; fi
  done <<<"$images"
  (( missing == 0 )) || { echo "install aborted: the stack cannot start offline without these images" >&2; exit 1; }
fi

for u in "$ROOT"/current/deploy/systemd/*.service; do ln -sfn "$u" "$SYSTEMD_DIR/$(basename "$u")"; done
if command -v systemctl >/dev/null && [[ "${NO_SYSTEMCTL:-0}" != 1 ]]; then
  systemctl daemon-reload
  systemctl enable warroom-firewall.service warroom-stack.service warroom-preload.service warroom-watchdog.service
  echo "installed $VER (images present). Start it now with:"
  echo "  systemctl start warroom-firewall warroom-stack warroom-preload warroom-watchdog"
fi
echo "current -> $(readlink "$ROOT/current")"
