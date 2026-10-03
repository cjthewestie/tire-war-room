#!/usr/bin/env bash
# One-click rollback of the software release (page 10: 一鍵回滾). The schedule has its own one-click
# rollback in the app (POST /api/schedule/rollback); this one swaps the code/images.
#   sudo deploy/update/rollback.sh            # back to the release before the current one
#   sudo deploy/update/rollback.sh <ver>      # to a named release under /opt/warroom/releases
# Env: WARROOM_ROOT, HEALTH_URL, HEALTH_TIMEOUT_S, RESTART_CMD (same as offline_update.sh)
set -euo pipefail
ROOT="${WARROOM_ROOT:-/opt/warroom}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"
HEALTH_TIMEOUT_S="${HEALTH_TIMEOUT_S:-180}"
RESTART_CMD="${RESTART_CMD:-systemctl restart warroom-stack warroom-preload}"

CUR="$(readlink "$ROOT/current" 2>/dev/null || true)"
if [[ -n "${1:-}" ]]; then TARGET="$ROOT/releases/$1"
elif [[ -f "$ROOT/releases/.previous" ]]; then TARGET="$(cat "$ROOT/releases/.previous")"
else
  # newest release that is not current
  TARGET="$(ls -1dt "$ROOT"/releases/*/ 2>/dev/null | sed 's:/$::' | grep -vx "$CUR" | head -1 || true)"
fi
[[ -n "$TARGET" && -d "$TARGET" ]] || { echo "rollback: no release to roll back to" >&2; exit 2; }
[[ "$TARGET" == "$CUR" ]] && { echo "rollback: $TARGET is already current"; exit 0; }

ln -sfn "$TARGET" "$ROOT/current"
[[ -n "$CUR" ]] && echo "$CUR" >"$ROOT/releases/.previous"
echo "rollback: current -> $TARGET (was ${CUR:-none}); restarting"
sh -c "$RESTART_CMD"
deadline=$(( $(date +%s) + HEALTH_TIMEOUT_S ))
until curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; do
  (( $(date +%s) > deadline )) && { echo "rollback: still unhealthy after ${HEALTH_TIMEOUT_S}s" >&2; exit 5; }
  sleep 2
done
echo "rollback: healthy on $(basename "$TARGET")"
