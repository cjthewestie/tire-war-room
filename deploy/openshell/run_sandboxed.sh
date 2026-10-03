#!/usr/bin/env bash
# Run the war-room app inside an OpenShell sandbox on the DGX Spark (page 5 sandbox box).
# Needs: the OpenShell CLI + gateway (https://docs.nvidia.com/openshell/latest), the warroom-app image
# (docker build -f deploy/Dockerfile -t warroom-app:latest .), nft + sudo, and vLLM / Qdrant / MQTT from
# deploy/spark/docker-compose.yml up and bound to the Spark's LAN IP:
#   cd deploy/spark && VLLM_BIND=192.168.10.20 QDRANT_BIND=192.168.10.20 MQTT_BIND=192.168.10.20 \
#     docker compose up -d vllm qdrant mqtt
#   SPARK_HOST=192.168.10.20 WARROOM_SUPERVISOR_PIN=... deploy/openshell/run_sandboxed.sh
#   SPARK_HOST=192.168.10.20 deploy/openshell/run_sandboxed.sh --dry-run   # print policy, nft table, commands
#
# Why each step exists:
# - The sandbox has its own network namespace: 127.0.0.1 there is the sandbox, not the Spark, so the
#   services must listen on SPARK_HOST (a loopback SPARK_HOST is refused). nft-sandbox-services.nft then
#   drops every source but SANDBOX_SRC on :8001/:6333, so the LAN still cannot reach the model or the KB.
# - The compose `app` already owns :8000, so it is stopped first; two war rooms would also mean two
#   state machines and two audit logs.
# - `openshell forward` delivers every client to the app as 127.0.0.1. With loopback counted as the
#   on-site seat that would make every LAN phone PIN-free, so the sandboxed app runs with
#   WARROOM_LOOPBACK_LOCAL=0 and a supervisor PIN is required: every approval on this path needs the PIN.
# - LLM_FALLBACK_TO_MOCK=true keeps the line safe if the model dies later, but it would also hide a
#   sandbox that never reached vLLM. So the run ends by reading /api/health through the forward and
#   fails unless llm_reachable and qdrant_ok are both true.
set -euo pipefail
cd "$(dirname "$0")/../.."
DRY=0; [[ "${1:-}" == "--dry-run" ]] && DRY=1
SPARK_HOST="${SPARK_HOST:-}"
NAME="${SANDBOX_NAME:-warroom}"
IMAGE="${APP_IMAGE:-warroom-app:latest}"
SANDBOX_SRC="${SANDBOX_SRC:-172.16.0.0/12}"     # OpenShell/Docker bridge range the sandbox egresses from
HEALTH_WAIT_S="${HEALTH_WAIT_S:-120}"
die() { echo "run_sandboxed: $*" >&2; exit 1; }

[[ -n "$SPARK_HOST" ]] || die "set SPARK_HOST to the Spark's LAN IP (the address vLLM/Qdrant/MQTT are bound to)"
case "$SPARK_HOST" in
  127.*|localhost|::1|0.0.0.0) die "SPARK_HOST=$SPARK_HOST is loopback/any: inside the sandbox that is the sandbox itself. Use the Spark's LAN IP." ;;
esac
[[ "$SPARK_HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "SPARK_HOST must be an IPv4 address (nft set + policy)"
if [[ $DRY -eq 0 && -z "${WARROOM_SUPERVISOR_PIN:-}" ]]; then
  die "set WARROOM_SUPERVISOR_PIN (8+ random chars): behind the forward every client needs it, the touch screen too"
fi

out="data/runtime/openshell"
mkdir -p "$out"
sed "s/\${SPARK_HOST}/${SPARK_HOST}/g" deploy/openshell/warroom-policy.yaml > "$out/warroom-policy.yaml"
sed -e "s/\${SPARK_HOST}/${SPARK_HOST}/g" -e "s#\${SANDBOX_SRC}#${SANDBOX_SRC}, ${SPARK_HOST}#g" \
  deploy/openshell/nft-sandbox-services.nft > "$out/nft-sandbox-services.nft"
for f in "$out/warroom-policy.yaml" "$out/nft-sandbox-services.nft"; do
  if grep -v '^#' "$f" | grep -q '\${'; then die "unfilled placeholder in $f"; fi
done

nft_load=(sudo nft -f "$out/nft-sandbox-services.nft")
stop_app=(docker compose -f deploy/spark/docker-compose.yml stop app)
create=(openshell sandbox create --name "$NAME" --from "$IMAGE" --policy "$out/warroom-policy.yaml"
  --env LLM_MODE=llm --env "BASE_URL=http://${SPARK_HOST}:8001/v1" --env MODEL=nemotron
  --env LLM_FALLBACK_TO_MOCK=true --env "QDRANT_URL=http://${SPARK_HOST}:6333"
  --env WARROOM_INGEST=mqtt --env "WARROOM_MQTT_URL=mqtt://${SPARK_HOST}:1883"
  --env "WARROOM_SUPERVISOR_PIN=${WARROOM_SUPERVISOR_PIN:-}" --env WARROOM_LOOPBACK_LOCAL=0
  --env HOST=0.0.0.0 --env PORT=8000
  -- /opt/venv/bin/python -m warroom.api)
forward=(openshell forward start 8000 "$NAME")

if [[ $DRY -eq 1 ]]; then
  cat "$out/warroom-policy.yaml"; echo "---"; cat "$out/nft-sandbox-services.nft"; echo "---"
  printf '%q ' "${nft_load[@]}"; echo
  printf '%q ' "${stop_app[@]}"; echo
  printf '%q ' "${create[@]}" | sed -E 's/WARROOM_SUPERVISOR_PIN=[^ ]+/WARROOM_SUPERVISOR_PIN=<set>/'; echo
  printf '%q ' "${forward[@]}"; echo
  exit 0
fi
command -v openshell >/dev/null || die "openshell CLI not installed"
command -v nft >/dev/null || die "nft not installed (needed to keep :8001/:6333 off the LAN)"

# 1. The services must answer on SPARK_HOST, not just on 127.0.0.1.
curl -fsS -m 5 "http://${SPARK_HOST}:8001/health" >/dev/null \
  || die "vLLM not answering on ${SPARK_HOST}:8001; start compose with VLLM_BIND=${SPARK_HOST}"
curl -fsS -m 5 "http://${SPARK_HOST}:6333/readyz" >/dev/null \
  || die "Qdrant not answering on ${SPARK_HOST}:6333; start compose with QDRANT_BIND=${SPARK_HOST}"
(exec 3<>"/dev/tcp/${SPARK_HOST}/1883") 2>/dev/null \
  || die "MQTT not answering on ${SPARK_HOST}:1883; start compose with MQTT_BIND=${SPARK_HOST}"

# 2. Only the sandbox (and the box itself) may reach :8001/:6333.
sudo nft delete table inet warroom_sandbox_svc 2>/dev/null || true
"${nft_load[@]}"

# 3. One war room on :8000.
"${stop_app[@]}" || true
if (exec 3<>/dev/tcp/127.0.0.1/8000) 2>/dev/null; then die ":8000 still in use after stopping the compose app"; fi

# 4. Sandbox + forward.
"${create[@]}"
"${forward[@]}"

# 5. Prove the sandboxed app reached vLLM and Qdrant (fallback-to-mock would otherwise hide it).
deadline=$((SECONDS + HEALTH_WAIT_S)); health=""
while (( SECONDS < deadline )); do
  health="$(curl -fsS -m 5 http://127.0.0.1:8000/api/health 2>/dev/null || true)"
  if [[ "$health" == *'"llm_reachable":true'* && "$health" == *'"qdrant_ok":true'* ]]; then
    echo "sandboxed war room up: $health"; exit 0
  fi
  sleep 3
done
die "sandboxed app not healthy within ${HEALTH_WAIT_S}s (need llm_reachable:true and qdrant_ok:true): ${health:-no answer}"
