#!/usr/bin/env bash
# Run the war-room app inside an OpenShell sandbox on the DGX Spark (page 5 sandbox box).
# Needs: the OpenShell CLI + gateway (https://docs.nvidia.com/openshell/latest), the warroom-app image
# (docker build -f deploy/Dockerfile -t warroom-app:latest .), and vLLM / Qdrant / MQTT from
# deploy/spark/docker-compose.yml already up on the host.
#   SPARK_HOST=192.168.10.20 deploy/openshell/run_sandboxed.sh          # create + forward :8000 to the LAN
#   deploy/openshell/run_sandboxed.sh --dry-run                          # print the policy and commands only
set -euo pipefail
cd "$(dirname "$0")/../.."
SPARK_HOST="${SPARK_HOST:-127.0.0.1}"
NAME="${SANDBOX_NAME:-warroom}"
IMAGE="${APP_IMAGE:-warroom-app:latest}"
out="data/runtime/openshell"
mkdir -p "$out"
sed "s/\${SPARK_HOST}/${SPARK_HOST}/g" deploy/openshell/warroom-policy.yaml > "$out/warroom-policy.yaml"
if grep -q '\${' "$out/warroom-policy.yaml"; then echo "unfilled placeholder in policy" >&2; exit 1; fi

create=(openshell sandbox create --name "$NAME" --from "$IMAGE" --policy "$out/warroom-policy.yaml"
  --env LLM_MODE=llm --env "BASE_URL=http://${SPARK_HOST}:8001/v1" --env MODEL=nemotron
  --env LLM_FALLBACK_TO_MOCK=true --env "QDRANT_URL=http://${SPARK_HOST}:6333"
  --env WARROOM_INGEST=mqtt --env "WARROOM_MQTT_URL=mqtt://${SPARK_HOST}:1883"
  --env "WARROOM_SUPERVISOR_PIN=${WARROOM_SUPERVISOR_PIN:-}" --env HOST=0.0.0.0 --env PORT=8000
  -- /opt/venv/bin/python -m warroom.api)
forward=(openshell forward start 8000 "$NAME")

if [[ "${1:-}" == "--dry-run" ]]; then
  cat "$out/warroom-policy.yaml"; printf '%q ' "${create[@]}"; echo; printf '%q ' "${forward[@]}"; echo
  exit 0
fi
command -v openshell >/dev/null || { echo "openshell CLI not installed" >&2; exit 1; }
"${create[@]}"
"${forward[@]}"
