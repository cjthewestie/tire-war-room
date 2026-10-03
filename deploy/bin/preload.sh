#!/usr/bin/env bash
# Boot self-start, step 2 (page 10: 開機自啟, 模型預先載入): wait for the model server, then send the
# commander's real prompts once so weights, CUDA graphs and the prefix cache are hot before the first
# anomaly. Called by warroom-preload.service after warroom-stack.service.
#   LLM_URL=http://127.0.0.1:8001/v1 MODEL=nemotron deploy/bin/preload.sh
set -euo pipefail
LLM_URL="${LLM_URL:-http://127.0.0.1:8001/v1}"
MODEL="${MODEL:-nemotron}"
WAIT_S="${WAIT_S:-1200}"
COMPOSE_DIR="${COMPOSE_DIR:-/opt/warroom/current/deploy/spark}"

deadline=$(( $(date +%s) + WAIT_S ))
until curl -fsS "${LLM_URL}/models" >/dev/null 2>&1; do
  if (( $(date +%s) > deadline )); then echo "preload: model server not up after ${WAIT_S}s" >&2; exit 1; fi
  sleep 5
done
echo "preload: model server up"
# Preferred: the bench prompts from inside the app container (same system prefix as the commander).
if command -v docker >/dev/null && docker compose --project-directory "$COMPOSE_DIR" exec -T \
     -e BASE_URL=http://vllm:8000/v1 -e MODEL="$MODEL" app python -m bench.preload 2>/dev/null; then
  exit 0
fi
# Fallback: one tiny request so at least the weights and graphs are resident.
curl -fsS "${LLM_URL}/chat/completions" -H 'Content-Type: application/json' \
  -d "{\"model\":\"${MODEL}\",\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}],\"max_tokens\":1}" >/dev/null
echo "preload: warmed with fallback request"
