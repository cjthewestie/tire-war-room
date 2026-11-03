#!/usr/bin/env bash
# Audit-log HMAC key for the Spark stack (G1), kept OUTSIDE the data volume so someone who can edit
# data/runtime/audit.jsonl cannot also re-sign it.
#   sudo deploy/bin/init-audit-key.sh           first install: create the key + anchor dir if missing, then check
#   sudo deploy/bin/init-audit-key.sh --check   check only (warroom-stack.service runs this before `compose up`)
# The key is 32 random bytes written as 64 hex characters (the app uses the file's bytes as the key, stripped of
# surrounding whitespace, so text is the safe format), owned by the app uid, mode 0400, in a 0700 directory.
# Only the compose `app` service mounts it (read-only); vllm, qdrant and mqtt never see it.
# Refuses (exit 1) when: the key is missing (in --check), empty, a symlink, not a regular file, not owned by the
# app uid, readable by group/others, its directory is open to group/others, it sits inside APP_DATA, or an old
# <log>.key still sits beside the log (run deploy/bin/migrate-audit-key.sh: creating a new key instead would make
# the app archive the whole existing log as audit.jsonl.broken-N). The anchor dir must exist, be owned by the
# app uid and sit outside APP_DATA; an existing log without a mirrored anchor is reported (migrate seeds it).
# Env: WARROOM_ROOT=/opt/warroom, ENV_FILE (default $ROOT/current/deploy/spark/.env: AUDIT_KEY_FILE,
#      AUDIT_ANCHOR_DIR, APP_DATA are read from it when not set), APP_UID=10001 (the Dockerfile's `warroom` user).
set -euo pipefail
MODE=init
case "${1:-}" in
  "") ;;
  --check) MODE=check ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac
ROOT="${WARROOM_ROOT:-/opt/warroom}"
ENV_FILE="${ENV_FILE:-$ROOT/current/deploy/spark/.env}"
from_env() { [[ -f "$ENV_FILE" ]] && sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 || true; }
KEY="${AUDIT_KEY_FILE:-$(from_env AUDIT_KEY_FILE)}"; KEY="${KEY:-$ROOT/secrets/audit.key}"
ANCHOR_DIR="${AUDIT_ANCHOR_DIR:-$(from_env AUDIT_ANCHOR_DIR)}"; ANCHOR_DIR="${ANCHOR_DIR:-$ROOT/audit-anchor}"
DATA="${APP_DATA:-$(from_env APP_DATA)}"; DATA="${DATA:-$ROOT/data/runtime}"
APP_UID="${APP_UID:-10001}"
LOG="$DATA/audit.jsonl"
KEY_DIR="$(dirname "$KEY")"

die() { echo "audit key: $*" >&2; echo "audit key: refusing to start the stack" >&2; exit 1; }
abspath() { (cd "$(dirname "$1")" 2>/dev/null && echo "$(pwd -P)/$(basename "$1")") || echo "$1"; }
inside() { local p d; p="$(abspath "$1")"; d="$(abspath "$2")"; [[ "$p" == "$d" || "$p" == "$d"/* ]]; }
owner() { stat -c %u "$1" 2>/dev/null || stat -f %u "$1"; }
perms() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
open_to_others() { (( (8#$(perms "$1")) & 8#077 )); }

if [[ "$MODE" == init ]]; then
  if [[ -e "$LOG.key" && ! -e "$KEY" ]]; then
    die "$LOG.key (the old key beside the log) exists. Run deploy/bin/migrate-audit-key.sh to move it to $KEY;" \
        "a new key would make the app archive the existing log as audit.jsonl.broken-N"
  fi
  inside "$KEY" "$DATA" && die "AUDIT_KEY_FILE=$KEY is inside APP_DATA=$DATA; put it outside the data volume"
  if [[ ! -e "$KEY" ]]; then
    mkdir -p "$KEY_DIR"
    chown "$APP_UID" "$KEY_DIR"
    chmod 700 "$KEY_DIR"
    tmp="$KEY.tmp.$$"
    ( umask 077; od -An -tx1 -N32 /dev/urandom | tr -d ' \n' >"$tmp"; echo >>"$tmp" )
    [[ "$(tr -d '\n' <"$tmp" | wc -c | tr -d ' ')" == 64 ]] || { rm -f "$tmp"; die "could not read 32 bytes from /dev/urandom"; }
    chown "$APP_UID" "$tmp"
    chmod 400 "$tmp"
    mv "$tmp" "$KEY"
    echo "audit key: created $KEY (uid $APP_UID, 0400)"
  fi
  if [[ ! -d "$ANCHOR_DIR" ]]; then
    mkdir -p "$ANCHOR_DIR"
    chown "$APP_UID" "$ANCHOR_DIR"
    chmod 700 "$ANCHOR_DIR"
    echo "audit key: created anchor dir $ANCHOR_DIR (uid $APP_UID, 0700)"
  fi
fi

# ---- checks (both modes)
[[ -e "$KEY" || -L "$KEY" ]] || die "$KEY missing. First install: sudo deploy/bin/init-audit-key.sh"
[[ -L "$KEY" ]] && die "$KEY is a symlink; use a regular file"
[[ -f "$KEY" ]] || die "$KEY is not a regular file (a directory here usually means Docker created it: remove it and rerun init)"
[[ -s "$KEY" ]] || die "$KEY is empty"
inside "$KEY" "$DATA" && die "$KEY is inside APP_DATA=$DATA; the key must not sit on the data volume"
[[ "$(owner "$KEY")" == "$APP_UID" ]] || die "$KEY is owned by uid $(owner "$KEY"), not the app uid $APP_UID"
open_to_others "$KEY" && die "$KEY has mode $(perms "$KEY"); group/others must not have access (chmod 400)"
open_to_others "$KEY_DIR" && die "$KEY_DIR has mode $(perms "$KEY_DIR"); group/others must not have access (chmod 700)"
[[ -e "$LOG.key" ]] && die "$LOG.key still sits beside the log; run deploy/bin/migrate-audit-key.sh (it moves or retires it)"
[[ -d "$ANCHOR_DIR" ]] || die "anchor dir $ANCHOR_DIR missing. First install: sudo deploy/bin/init-audit-key.sh"
inside "$ANCHOR_DIR" "$DATA" && die "anchor dir $ANCHOR_DIR is inside APP_DATA=$DATA; put it outside (ideally another disk)"
[[ "$(owner "$ANCHOR_DIR")" == "$APP_UID" ]] || die "anchor dir $ANCHOR_DIR is owned by uid $(owner "$ANCHOR_DIR"), not $APP_UID"
if [[ -s "$LOG" && ! -e "$ANCHOR_DIR/audit.jsonl.head" ]]; then
  echo "audit key: WARNING $LOG has entries but $ANCHOR_DIR/audit.jsonl.head does not exist: the app will archive the" \
       "log as broken-N on start. If this is the first start with the mirrored anchor, run deploy/bin/migrate-audit-key.sh" >&2
fi
echo "audit key ok: $KEY (uid $APP_UID, mode $(perms "$KEY")), anchor dir $ANCHOR_DIR"
