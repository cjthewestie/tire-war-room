#!/usr/bin/env bash
# Write-access smoke test against the RUNNING compose stack, run on the Spark host itself (or in any
# --network host container). Proves the two page-9 approval seats work through the deployed app:
#   touchscreen = 127.0.0.1, no PIN        -> passes the guard
#   phone       = the box's LAN IP, no PIN -> 401;  with X-Supervisor-Pin -> passes the guard
# Docker port publishing used to rewrite every source to the bridge gateway, so the touchscreen got 403.
#   WARROOM_SUPERVISOR_PIN=... deploy/spark/smoke_access.sh            # guard checks only (safe on a live line)
#   APPROVE=1 WARROOM_SUPERVISOR_PIN=... deploy/spark/smoke_access.sh  # also approve one real pending request
#                                                                      # from the touchscreen (demo stacks only)
# Env: PORT=8000, LAN_IP (default: first `hostname -I`), WAIT_S=120 (APPROVE: time to wait for a request).
set -euo pipefail
PORT="${PORT:-8000}"
PIN="${WARROOM_SUPERVISOR_PIN:?set WARROOM_SUPERVISOR_PIN (the value in deploy/spark/.env)}"
LAN_IP="${LAN_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
[[ -n "$LAN_IP" ]] || { echo "smoke: no LAN IP (set LAN_IP)" >&2; exit 2; }
fail=0

post() {  # post <host> <path> <json> [pin] -> prints the HTTP status
  local args=(-s -o /dev/null -w '%{http_code}' -m 10 -X POST "http://$1:$PORT$2" -H 'content-type: application/json' -d "$3")
  [[ -n "${4:-}" ]] && args+=(-H "X-Supervisor-Pin: $4")
  curl "${args[@]}" || true
}
expect() {  # expect <label> <got> <want...>
  local label="$1" got="$2"; shift 2
  for w in "$@"; do [[ "$got" == "$w" ]] && { echo "ok   $label: $got"; return; }; done
  echo "FAIL $label: got $got, want $*" >&2; fail=1
}

for i in $(seq 1 60); do curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 1; done
probe='{"request_id":"smoke-none","decision":"approve","via":"touchscreen"}'
# 404 = the guard let the write through and the (made-up) request does not exist
expect "touchscreen 127.0.0.1, no PIN" "$(post 127.0.0.1 /api/approvals/smoke-none/decision "$probe")" 404
expect "phone $LAN_IP, no PIN" "$(post "$LAN_IP" /api/approvals/smoke-none/decision "${probe/touchscreen/phone}")" 401
expect "phone $LAN_IP, PIN" "$(post "$LAN_IP" /api/approvals/smoke-none/decision "${probe/touchscreen/phone}" "$PIN")" 404

if [[ "${APPROVE:-0}" == 1 ]]; then
  deadline=$(( $(date +%s) + ${WAIT_S:-120} )); rid=""
  while [[ -z "$rid" ]] && (( $(date +%s) < deadline )); do
    rid="$(curl -fsS -m 5 "http://127.0.0.1:$PORT/api/approvals?status=pending" 2>/dev/null \
      | python3 -c 'import json,sys; p=[a for a in json.load(sys.stdin) if a.get("status")=="pending"]; print(p[0]["id"] if p else "")' \
      2>/dev/null || true)"
    [[ -n "$rid" ]] || { post 127.0.0.1 /api/sim/step '{"seconds":60}' >/dev/null; sleep 1; }
  done
  if [[ -z "$rid" ]]; then echo "FAIL no pending approval within ${WAIT_S:-120}s (load a fault scenario)" >&2; fail=1
  else
    expect "touchscreen approves $rid" \
      "$(post 127.0.0.1 "/api/approvals/$rid/decision" "{\"request_id\":\"$rid\",\"decision\":\"approve\",\"via\":\"touchscreen\"}")" 200
  fi
fi
(( fail == 0 )) && echo "smoke_access: all checks passed" || { echo "smoke_access: FAILED" >&2; exit 1; }
