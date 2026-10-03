#!/usr/bin/env bash
# First install on the DGX Spark (run as root from an unpacked release or a git checkout).
# Layout:  /opt/warroom/releases/<ver>/   code, one dir per release (offline_update.sh adds more)
#          /opt/warroom/current -> releases/<ver>
#          /opt/warroom/data/{runtime,qdrant}   audit log, schedules, vector store (survive updates/rollbacks)
#          /opt/warroom/hf                      model weights (deploy/bin/fetch_models.sh)
#   sudo deploy/bin/install_spark.sh [version]
set -euo pipefail
ROOT="${WARROOM_ROOT:-/opt/warroom}"
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
VER="${1:-$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"

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

for u in "$ROOT"/current/deploy/systemd/*.service; do ln -sfn "$u" "$SYSTEMD_DIR/$(basename "$u")"; done
if command -v systemctl >/dev/null && [[ "${NO_SYSTEMCTL:-0}" != 1 ]]; then
  systemctl daemon-reload
  systemctl enable warroom-firewall.service warroom-stack.service warroom-preload.service warroom-watchdog.service
  echo "installed $VER. Build the app image (docker compose build app) or load it from a bundle, then:"
  echo "  systemctl start warroom-firewall warroom-stack warroom-preload warroom-watchdog"
fi
echo "current -> $(readlink "$ROOT/current")"
