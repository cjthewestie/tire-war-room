#!/usr/bin/env bash
# Watchdog (page 10: watchdog 異常即重啟). Probes each service; after FAIL_LIMIT consecutive failures it
# restarts that service only. Runs every 15 s from warroom-watchdog.timer, or `--loop` as a daemon.
# The safety core does not depend on any of this: it lives in the PLC and in the app's red path.
#
# Env (all overridable, tests use fakes):
#   APP_URL=http://127.0.0.1:8000/api/health  LLM_URL=http://127.0.0.1:8001/health
#   QDRANT_URL=http://127.0.0.1:6333/readyz   FAIL_LIMIT=3   STATE_DIR=/run/warroom-watchdog
#   COMPOSE="docker compose --project-directory /opt/warroom/current/deploy/spark"
#   LLM_GRACE_S=900   (no vLLM restarts this long after boot OR after the last vLLM restart: a cold
#                      load of the weights + CUDA graph capture takes minutes; compose start_period is 600 s)
#   DOCKER=docker     (vLLM is also left alone while `docker inspect` reports health=starting)
#   PAUSE_FILE=$STATE_DIR/pause  (exists -> probe and log, but restart nothing; bench/spark_ablation.sh
#                      creates it while it recreates vllm. Ignored once older than PAUSE_MAX_S=14400.)
#   LOG=/dev/stderr   (journald picks it up under systemd)
set -uo pipefail
APP_URL="${APP_URL:-http://127.0.0.1:8000/api/health}"
LLM_URL="${LLM_URL:-http://127.0.0.1:8001/health}"
QDRANT_URL="${QDRANT_URL:-http://127.0.0.1:6333/readyz}"
FAIL_LIMIT="${FAIL_LIMIT:-3}"
STATE_DIR="${STATE_DIR:-/run/warroom-watchdog}"
COMPOSE="${COMPOSE:-docker compose --project-directory /opt/warroom/current/deploy/spark}"
LLM_GRACE_S="${LLM_GRACE_S:-900}"
INTERVAL_S="${INTERVAL_S:-15}"
LOG="${LOG:-/dev/stderr}"
DOCKER="${DOCKER:-docker}"
PAUSE_FILE="${PAUSE_FILE:-$STATE_DIR/pause}"
PAUSE_MAX_S="${PAUSE_MAX_S:-14400}"
mkdir -p "$STATE_DIR"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') watchdog: $*" >>"$LOG"; }

uptime_s() {
  if [[ -r /proc/uptime ]]; then cut -d. -f1 /proc/uptime
  else echo $(( $(date +%s) - $(sysctl -n kern.boottime | sed -E 's/^\{ sec = ([0-9]+).*/\1/') )); fi
}

mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }

paused() {   # a pause file left behind by a crashed bench run must not disable the watchdog forever
  [[ -e "$PAUSE_FILE" ]] || return 1
  (( $(date +%s) - $(mtime "$PAUSE_FILE") < PAUSE_MAX_S )) && return 0
  log "pause file $PAUSE_FILE older than ${PAUSE_MAX_S}s, ignoring it"; return 1
}

since_restart_s() {   # seconds since this watchdog last restarted <name>; huge if never
  local t; t=$(cat "$STATE_DIR/$1.restarted" 2>/dev/null || echo 0)
  echo $(( $(date +%s) - t ))
}

container_health() {   # health status of a compose service's container ("" when unknown)
  local cid; cid=$($COMPOSE ps -q "$1" 2>/dev/null | head -n1) || return 0
  [[ -n "$cid" ]] || return 0
  "$DOCKER" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cid" 2>/dev/null || true
}

check() {   # check <name> <url> <compose-service>
  local name=$1 url=$2 svc=$3 f="$STATE_DIR/$1.fails" n
  if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
    [[ -s "$f" && "$(cat "$f")" != 0 ]] && log "$name recovered"
    echo 0 >"$f"; return 0
  fi
  n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$f"
  log "$name probe failed ($n/$FAIL_LIMIT) $url"
  if (( n >= FAIL_LIMIT )); then
    if paused; then log "paused by $PAUSE_FILE, not restarting $svc"; return 1; fi
    if [[ "$name" == llm ]]; then
      if (( $(uptime_s) < LLM_GRACE_S )); then
        log "llm still inside boot grace (${LLM_GRACE_S}s), not restarting"; return 1
      fi
      if (( $(since_restart_s llm) < LLM_GRACE_S )); then
        log "llm still inside restart grace (${LLM_GRACE_S}s since last restart), not restarting"; return 1
      fi
      if [[ "$(container_health "$svc")" == starting ]]; then
        log "llm container health=starting (still loading), not restarting"; return 1
      fi
    fi
    log "restarting $svc"
    $COMPOSE restart "$svc" >>"$LOG" 2>&1 || log "restart $svc FAILED"
    echo 0 >"$f"
    date +%s >"$STATE_DIR/$name.restarted"
    echo "$(date +%s) $svc" >>"$STATE_DIR/restarts.log"
  fi
  return 1
}

once() {
  local rc=0
  check app "$APP_URL" app || rc=1
  check llm "$LLM_URL" vllm || rc=1
  check qdrant "$QDRANT_URL" qdrant || rc=1
  return $rc
}

if [[ "${1:-}" == "--loop" ]]; then
  [[ -n "${NOTIFY_SOCKET:-}" ]] && systemd-notify --ready
  while true; do once; [[ -n "${NOTIFY_SOCKET:-}" ]] && systemd-notify WATCHDOG=1; sleep "$INTERVAL_S"; done
else
  once
fi
