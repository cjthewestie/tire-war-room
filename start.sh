#!/usr/bin/env bash
# One-command start on a Mac (Apple Silicon).
#   ./start.sh            mock mode: no model at all, the whole demo runs on the rule-based commander
#   ./start.sh ollama     Ollama mode: Nemotron 3.5 Lightning NVFP4 on a project Ollama (deploy/mac/dev.sh)
# Then open http://localhost:8000 (dashboard), http://localhost:8000/phone (supervisor phone) and http://localhost:8000/factory (3D factory).
# Env passes through: PORT, SCENARIO (normal|press7_temp_drop|open_mill_overheat|bad_data|concurrent_3), SIM_SPEED.
set -euo pipefail
cd "$(dirname "$0")"
command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
command -v pnpm >/dev/null || { echo "Node.js 20+ and pnpm are required (corepack enable pnpm)" >&2; exit 1; }
uv sync --quiet
if [[ ! -f web/dist/index.html ]]; then
  (cd web && { [[ -d node_modules ]] || pnpm install --frozen-lockfile; } && pnpm build)
fi
case "${1:-mock}" in
  mock)   LLM_MODE=mock exec uv run python -m warroom.api serve ;;
  ollama) exec deploy/mac/dev.sh app ;;
  *) sed -n '2,6p' "$0"; exit 2 ;;
esac
