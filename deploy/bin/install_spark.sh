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
# Supervisor PIN: the only way the phone approves over the LAN (the touchscreen on 127.0.0.1 needs none; the
# app runs on the host network). Without it no remote client can approve, so refuse to finish until it is set.
# Pass it as WARROOM_SUPERVISOR_PIN=... to write it into .env, or edit .env and rerun.
pin_envf="$ROOT/releases/$VER/deploy/spark/.env"
pin_in_env() { sed -n 's/^WARROOM_SUPERVISOR_PIN=//p' "$pin_envf" | tail -1; }
if [[ -n "${WARROOM_SUPERVISOR_PIN:-}" && "$WARROOM_SUPERVISOR_PIN" != "$(pin_in_env)" ]]; then
  { grep -v '^WARROOM_SUPERVISOR_PIN=' "$pin_envf" || true; echo "WARROOM_SUPERVISOR_PIN=$WARROOM_SUPERVISOR_PIN"; } \
    >"$pin_envf.tmp" && mv "$pin_envf.tmp" "$pin_envf"
fi
chmod 600 "$pin_envf" 2>/dev/null || true
pin="$(pin_in_env)"
if (( ${#pin} < 8 )) || [[ "$pin" =~ [[:space:]\"\'\$\#] ]]; then
  echo "install aborted: WARROOM_SUPERVISOR_PIN in $pin_envf is empty, under 8 characters, or has spaces/quotes/\$/#" >&2
  echo "  the supervisor phone could not approve anything. Rerun with: sudo WARROOM_SUPERVISOR_PIN=\$(openssl rand -hex 6) $0 $VER" >&2
  exit 1
fi
ln -sfn "$ROOT/releases/$VER" "$ROOT/current"
chown -R 10001 "$ROOT/data/runtime" 2>/dev/null || true   # app container user

if [[ "${NO_IMAGES:-0}" != 1 ]]; then
  compose=("$DOCKER" compose --project-directory "$ROOT/current/deploy/spark")
  if [[ "${SKIP_PULL:-0}" != 1 ]]; then
    echo "pulling every non-buildable compose image (vllm, qdrant, mqtt, ...) and building app while still online"
    echo "(the boot firewall blocks registries)"
    "${compose[@]}" pull --ignore-buildable
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

# Bench venv: $ROOT/venv, the interpreter bench/py.sh (and so bench/spark_ablation.sh) uses on this host.
# Host-native, not the app container: memory_check reads host processes, docker stats and nvidia-smi, and
# unplug_test drives nft as root. Built HERE while online: the host has no uv, and the boot firewall blocks
# PyPI. uv comes from PATH, $UV, or is copied out of the official uv image. SKIP_PULL=1 syncs --offline from
# the uv cache ($ROOT/.uv-cache). The SOP embedding model is pre-fetched to $ROOT/fastembed for full_event.
# Env: NO_BENCH_VENV=1 (skip: tests), UV=/path/to/uv, UV_IMAGE (default ghcr.io/astral-sh/uv:0.9).
if [[ "${NO_BENCH_VENV:-0}" != 1 ]]; then
  UV="${UV:-$(command -v uv 2>/dev/null || true)}"
  if [[ -z "$UV" ]]; then
    UV="$ROOT/bin/uv"
    if [[ ! -x "$UV" ]]; then
      mkdir -p "$ROOT/bin"
      rc=0; cid="$("$DOCKER" create "${UV_IMAGE:-ghcr.io/astral-sh/uv:0.9}")" || rc=1
      if (( rc == 0 )); then
        "$DOCKER" cp "$cid:/uv" "$UV" || rc=1
        "$DOCKER" rm "$cid" >/dev/null 2>&1 || true
      fi
      (( rc == 0 )) && [[ -x "$UV" ]] ||{ echo "install aborted: no uv on PATH and cannot copy it from the uv image" >&2; exit 1; }
    fi
  fi
  offline=(); [[ "${SKIP_PULL:-0}" == 1 ]] && offline=(--offline)
  echo "building the bench venv $ROOT/venv with $UV (frozen uv.lock, no dev deps)"
  (cd "$ROOT/current" && UV_PROJECT_ENVIRONMENT="$ROOT/venv" UV_PYTHON_INSTALL_DIR="$ROOT/python" \
    UV_CACHE_DIR="$ROOT/.uv-cache" "$UV" sync --frozen --no-dev --no-install-project ${offline[@]+"${offline[@]}"}) \
    || { echo "install aborted: bench venv sync failed (benches could not run once the firewall is up)" >&2; exit 1; }
  if [[ "${SKIP_PULL:-0}" != 1 ]]; then
    (cd "$ROOT/current" && "$ROOT/venv/bin/python" -c "from fastembed import TextEmbedding; \
from warroom.config import get_settings; TextEmbedding(model_name=get_settings().embed_model, cache_dir='$ROOT/fastembed')") \
      || { echo "install aborted: could not pre-fetch the SOP embedding model" >&2; exit 1; }
    chmod -R a+rX "$ROOT/fastembed" 2>/dev/null || true
  fi
  (cd "$ROOT/current" && WARROOM_ROOT="$ROOT" bench/py.sh -c "import bench.ttft, bench.concurrent, bench.full_event, \
bench.memory_check, bench.accuracy, bench.tokens_per_watt, bench.unplug_test, ortools, openai, psutil") \
    || { echo "install aborted: the bench venv cannot import the benches" >&2; exit 1; }
  echo "bench venv ok: run benches with bench/py.sh -m bench.<name> (bench/spark_ablation.sh does)"
fi

# Quiet DGX OS's own internet calls, so the unplug test's 0 means the box made no attempt at all. The nft
# rules drop and log them anyway (host hook, with UID) and bench/unplug_test.py does not count them against
# the stack, but each one is still a packet aimed out of the factory (p.9 對外連線 0 次).
if [[ "${QUIET_OS:-1}" == 1 && ( $EUID -eq 0 || -n "${ETC_DIR:-}" ) ]]; then
  ETC="${ETC_DIR:-/etc}"
  mkdir -p "$ETC/systemd/timesyncd.conf.d"
  if [[ -n "${LAN_NTP:-}" ]]; then
    printf '[Time]\nNTP=%s\nFallbackNTP=\n' "$LAN_NTP" > "$ETC/systemd/timesyncd.conf.d/warroom.conf"
    echo "timesyncd -> LAN NTP $LAN_NTP (no public fallback)"
  else
    printf '[Time]\nNTP=\nFallbackNTP=\n' > "$ETC/systemd/timesyncd.conf.d/warroom.conf"
    echo "WARNING: LAN_NTP not set; timesyncd public pools (ntp.ubuntu.com) disabled, clock will drift until" \
         "LAN_NTP=<factory NTP> is set and install re-run" >&2
  fi
  if [[ -f "$ETC/default/motd-news" ]]; then
    sed 's/^ENABLED=.*/ENABLED=0/' "$ETC/default/motd-news" > "$ETC/default/motd-news.tmp" \
      && mv "$ETC/default/motd-news.tmp" "$ETC/default/motd-news"
  fi
  if command -v systemctl >/dev/null && [[ "${NO_SYSTEMCTL:-0}" != 1 ]]; then
    systemctl disable --now motd-news.timer apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
    systemctl restart systemd-timesyncd 2>/dev/null || true
    if command -v snap >/dev/null; then snap refresh --hold 2>/dev/null || true; fi   # no store refresh calls
  fi
fi

for u in "$ROOT"/current/deploy/systemd/*.service; do ln -sfn "$u" "$SYSTEMD_DIR/$(basename "$u")"; done
if command -v systemctl >/dev/null && [[ "${NO_SYSTEMCTL:-0}" != 1 ]]; then
  systemctl daemon-reload
  systemctl enable warroom-firewall.service warroom-stack.service warroom-preload.service warroom-watchdog.service
  echo "installed $VER (images present). Start it now with:"
  echo "  systemctl start warroom-firewall warroom-stack warroom-preload warroom-watchdog"
fi
echo "current -> $(readlink "$ROOT/current")"
