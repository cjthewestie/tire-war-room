#!/usr/bin/env bash
# Linux dev stack (WSL2 on a Windows laptop, or any Linux host without Docker): the war-room app through uv,
# plus a project-owned Ollama on 127.0.0.1:11435. It runs a SEPARATE `ollama serve` with a 32k context so the
# commander prompt (~5.5k tokens) is never silently truncated; a system Ollama on 11434 is left alone.
# Fresh machine: run deploy/linux/bootstrap_wsl.sh once (installs node 22 and uv, then runs setup).
#
#   deploy/linux/dev.sh setup        check node 22.12+ and uv, then uv sync, npm ci + build, SOP index, check
#   deploy/linux/dev.sh app --mock   run the app with the rule-based commander, no model at all
#   deploy/linux/dev.sh test         full pytest run in the background, log in .scratch/baseline/
#   deploy/linux/dev.sh ollama       start the project Ollama (32k context, 1 slot for an 8 GB GPU)
#   deploy/linux/dev.sh pull         pull $MODEL into it
#   deploy/linux/dev.sh app          run the app against it (LLM_MODE=llm)
#   deploy/linux/dev.sh env          print the env block for running anything else against it
#   deploy/linux/dev.sh stop         stop the project Ollama
#   deploy/linux/dev.sh watch        follow data/runtime/audit.jsonl as one Chinese line per entry
#
# MODEL defaults to nemotron-3-nano:4b, a stand-in that fits an 8 GB GPU; the event model runs on the Spark.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${WARROOM_OLLAMA_PORT:-11435}"
MODEL="${MODEL:-nemotron-3-nano:4b}"
RUN_DIR="${RUN_DIR:-$REPO/data/runtime/ollama}"
export OLLAMA_HOST="127.0.0.1:$PORT"

up() { curl -fsS "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1; }

need_node() {
  local v major minor
  v="$(node -v 2>/dev/null | sed 's/^v//')"
  major="${v%%.*}"; minor="$(echo "$v" | cut -d. -f2)"
  if [[ -z "$v" ]] || (( major < 22 || (major == 22 && minor < 12) )); then
    echo "node 22.12+ is required (found ${v:-none}); web/ builds with Vite 8" >&2; exit 1
  fi
}

cmd_setup() {
  cd "$REPO"
  command -v uv >/dev/null || { echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }
  need_node
  uv sync
  (cd web && npm ci --no-audit --no-fund && npm run build)
  uv run python -m warroom.kb ingest      # first run downloads the SOP embedding model; prints mode=vector|lexical
  uv run python -m warroom.api check
}

cmd_test() {
  cd "$REPO"
  mkdir -p .scratch/baseline
  local log=".scratch/baseline/pytest-$(date +%Y%m%d-%H%M%S).log"
  nohup uv run pytest -q >"$log" 2>&1 &
  echo "pytest running in the background (pid $!), log $log"
  echo "follow it with: tail -f $log"
}

cmd_ollama() {
  if up; then echo "project ollama already on :$PORT"; return; fi
  mkdir -p "$RUN_DIR"
  OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}" OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}" \
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}" OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-24h}" \
  OLLAMA_MAX_LOADED_MODELS=1 nohup ollama serve >"$RUN_DIR/ollama.log" 2>&1 &
  echo $! >"$RUN_DIR/ollama.pid"
  for _ in $(seq 1 50); do up && break; sleep 0.2; done
  up && echo "project ollama on :$PORT (pid $(cat "$RUN_DIR/ollama.pid"), log $RUN_DIR/ollama.log)" \
     || { echo "ollama failed to start, see $RUN_DIR/ollama.log" >&2; exit 1; }
}

cmd_pull() { cmd_ollama; ollama pull "$MODEL"; }

cmd_env() {
  echo "export LLM_MODE=llm BASE_URL=http://127.0.0.1:$PORT/v1 MODEL=$MODEL"
}

cmd_app() {
  cd "$REPO"
  if [[ "${1:-}" == "--mock" ]]; then LLM_MODE=mock exec uv run python -m warroom.api serve; fi
  cmd_ollama
  LLM_MODE=llm BASE_URL="http://127.0.0.1:$PORT/v1" MODEL="$MODEL" exec uv run python -m warroom.api serve
}

cmd_stop() {
  if [[ -f "$RUN_DIR/ollama.pid" ]]; then kill "$(cat "$RUN_DIR/ollama.pid")" 2>/dev/null || true; rm -f "$RUN_DIR/ollama.pid"; fi
  echo "stopped project ollama"
}

cmd_watch() { exec python3 "$REPO/deploy/linux/watch_audit.py" -f "$REPO/data/runtime/audit.jsonl"; }

case "${1:-}" in
  setup) cmd_setup ;; test) cmd_test ;; ollama) cmd_ollama ;; pull) cmd_pull ;; app) shift; cmd_app "$@" ;;
  env) cmd_env ;; stop) cmd_stop ;; watch) cmd_watch ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
