#!/usr/bin/env bash
# One-time move of the audit HMAC key from beside the log (APP_DATA/audit.jsonl.key, the app's fallback) to
# AUDIT_KEY_FILE outside the data volume (G1), WITHOUT breaking the existing chain.
#   sudo systemctl stop warroom-stack        # the app must not be running
#   sudo deploy/bin/migrate-audit-key.sh
#   sudo systemctl start warroom-stack
# Why a script: the new compose sets WARROOM_AUDIT_REQUIRE_KEY=1 + WARROOM_AUDIT_KEY_FILE + WARROOM_AUDIT_ANCHOR.
# Started with a NEW key, or with the anchor mirror missing beside a non-empty log, the app archives the whole
# log as audit.jsonl.broken-N and starts a new chain. This script, in order:
#   1. moves the old key byte-for-byte to AUDIT_KEY_FILE (uid APP_UID, 0400, 0700 dir) and deletes it beside the log;
#   2. seeds AUDIT_ANCHOR_DIR/audit.jsonl.head from the log's own signed <log>.head (same key, same seq);
#   3. runs init-audit-key.sh --check.
# The app reads the key file's bytes STRIPPED of surrounding whitespace. The old key is 32 raw random bytes; if
# its first or last byte is ASCII whitespace the moved key would not match and the chain would break. Then the
# script stops and changes nothing, unless ROTATE=1: the old log, head, sidecars, broken-N archives and the old
# key are moved together into <key dir>/retired-<time>/ (still verifiable offline with that key) and a new key
# and a new chain start.
# Env: WARROOM_ROOT=/opt/warroom, ENV_FILE, AUDIT_KEY_FILE, AUDIT_ANCHOR_DIR, APP_DATA, APP_UID=10001 (as in
#      init-audit-key.sh), ROTATE=1 (see above), DOCKER=docker (used only to check the app is stopped).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="${WARROOM_ROOT:-/opt/warroom}"
ENV_FILE="${ENV_FILE:-$ROOT/current/deploy/spark/.env}"
from_env() { [[ -f "$ENV_FILE" ]] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 || true; }
KEY="${AUDIT_KEY_FILE:-$(from_env AUDIT_KEY_FILE)}"; KEY="${KEY:-$ROOT/secrets/audit.key}"
ANCHOR_DIR="${AUDIT_ANCHOR_DIR:-$(from_env AUDIT_ANCHOR_DIR)}"; ANCHOR_DIR="${ANCHOR_DIR:-$ROOT/audit-anchor}"
DATA="${APP_DATA:-$(from_env APP_DATA)}"; DATA="${DATA:-$ROOT/data/runtime}"
APP_UID="${APP_UID:-10001}"
DOCKER="${DOCKER:-docker}"
LOG="$DATA/audit.jsonl"
OLD="$LOG.key"
KEY_DIR="$(dirname "$KEY")"
ANCHOR="$ANCHOR_DIR/audit.jsonl.head"
export WARROOM_ROOT="$ROOT" ENV_FILE AUDIT_KEY_FILE="$KEY" AUDIT_ANCHOR_DIR="$ANCHOR_DIR" APP_DATA="$DATA" APP_UID

die() { echo "migrate: $*" >&2; echo "migrate: nothing more was changed" >&2; exit 1; }
is_ws() { case "$1" in 9|10|11|12|13|32) return 0 ;; esac; return 1; }
byte_at() { if [[ "$1" == first ]]; then head -c1 "$2"; else tail -c1 "$2"; fi | od -An -tu1 | tr -d ' \n'; }

if command -v "$DOCKER" >/dev/null 2>&1; then
  running="$("$DOCKER" ps -q --filter label=com.docker.compose.project=warroom \
             --filter label=com.docker.compose.service=app 2>/dev/null || true)"
  [[ -z "$running" ]] || die "the warroom app container is running; sudo systemctl stop warroom-stack first"
fi

mkdir -p "$KEY_DIR"
chown "$APP_UID" "$KEY_DIR"
chmod 700 "$KEY_DIR"

if [[ -e "$OLD" ]]; then
  if [[ -e "$KEY" ]]; then
    cmp -s "$OLD" "$KEY" || die "both $OLD and $KEY exist and differ; decide which key signs $LOG before migrating"
    rm -f "$OLD"
    echo "migrate: $KEY already holds the same key; removed the copy beside the log"
  else
    [[ -s "$OLD" ]] || die "$OLD is empty"
    first="$(byte_at first "$OLD")"; last="$(byte_at last "$OLD")"
    if is_ws "$first" || is_ws "$last"; then
      if [[ "${ROTATE:-0}" != 1 ]]; then
        die "the old key starts or ends with a whitespace byte, which the app strips from a key FILE, so moving it" \
            "would break the chain. Rerun with ROTATE=1 to retire the old log + key together and start a new chain"
      fi
      retired="$KEY_DIR/retired-$(date +%Y%m%d%H%M%S)"
      mkdir -p "$retired"; chmod 700 "$retired"
      for f in "$LOG" "$LOG.head" "$LOG.corrupt" "$LOG".broken-* "$OLD"; do
        if [[ -e "$f" ]]; then mv "$f" "$retired/"; fi
      done
      echo "migrate: ROTATE=1: old log, head, sidecars and key moved to $retired (verify offline with that key)"
      bash "$HERE/init-audit-key.sh"
      exit 0
    fi
    tmp="$KEY.tmp.$$"
    ( umask 077; cp "$OLD" "$tmp" )
    chown "$APP_UID" "$tmp"
    chmod 400 "$tmp"
    mv "$tmp" "$KEY"
    cmp -s "$OLD" "$KEY" || die "copy of $OLD to $KEY does not match byte-for-byte"
    rm -f "$OLD"
    echo "migrate: moved the audit key $OLD -> $KEY (uid $APP_UID, 0400)"
  fi
elif [[ ! -e "$KEY" ]]; then
  if [[ -s "$LOG" ]]; then
    die "$LOG has entries but there is no key to move ($OLD and $KEY are both missing); it was signed with" \
        "WARROOM_AUDIT_KEY or another key file: put that key at $KEY (uid $APP_UID, 0400) and rerun"
  fi
  echo "migrate: no existing key and no log: first install"
  bash "$HERE/init-audit-key.sh"
  exit 0
else
  echo "migrate: key already at $KEY; nothing beside the log"
fi

mkdir -p "$ANCHOR_DIR"
chown "$APP_UID" "$ANCHOR_DIR"
chmod 700 "$ANCHOR_DIR"
if [[ -f "$LOG.head" ]]; then
  if [[ ! -e "$ANCHOR" ]]; then
    tmp="$ANCHOR.tmp.$$"
    cp "$LOG.head" "$tmp"
    chown "$APP_UID" "$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$ANCHOR"
    echo "migrate: seeded the anchor mirror $ANCHOR from $LOG.head"
  elif ! cmp -s "$LOG.head" "$ANCHOR"; then
    echo "migrate: WARNING $ANCHOR differs from $LOG.head; left untouched (the app accepts an anchor one entry" \
         "behind and archives the log on anything else)" >&2
  fi
elif [[ -s "$LOG" ]]; then
  echo "migrate: WARNING $LOG has entries but no $LOG.head: the app will archive it as broken-N on start" >&2
fi

bash "$HERE/init-audit-key.sh" --check
