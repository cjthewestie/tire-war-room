#!/usr/bin/env bash
# Mac dev stack (Apple Silicon): a project-owned Ollama server + the war-room app, no Docker needed.
# Runs a SEPARATE `ollama serve` on 127.0.0.1:11435 so the user's own Ollama (11434, its own context size)
# is left alone; both share ~/.ollama/models, so nothing is downloaded twice.
#
#   deploy/mac/dev.sh ollama     start the project Ollama (32k+ context, 3 parallel slots, q8_0 KV ~ FP8 KV)
#   deploy/mac/dev.sh pull       pull the Nemotron models used on the Mac
#   deploy/mac/dev.sh app        run the app against it (LLM_MODE=llm); `app --mock` = no model at all
#   deploy/mac/dev.sh bench      run the benchmark suite, results into bench/results/
#   deploy/mac/dev.sh env        print the env block for running anything else against it
#   deploy/mac/dev.sh stop       stop the project Ollama
#
# Model choice (verified with `ollama show` 2026-10-03): nemotron-3.5-lightning:30b-a3b-nvfp4 is the same
# 30B-A3B Nemotron 3.5 Lightning in NVFP4 (MLX backend on Apple Silicon, 23 GB, tools + thinking).
# The :30b-a3b-mxfp8 tag (34 GB) is the 8-bit comparison for the NVFP4-vs-FP8 accuracy runner.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${WARROOM_OLLAMA_PORT:-11435}"
MODEL="${MODEL:-nemotron-3.5-lightning:30b-a3b-nvfp4}"
MODEL_FP8="${MODEL_FP8:-nemotron-3.5-lightning:30b-a3b-mxfp8}"
RUN_DIR="${RUN_DIR:-$REPO/data/runtime/ollama}"
export OLLAMA_HOST="127.0.0.1:$PORT"

up() { curl -fsS "http://127.0.0.1:$PORT/api/version" >/dev/null 2>&1; }

cmd_ollama() {
  if up; then echo "project ollama already on :$PORT"; return; fi
  mkdir -p "$RUN_DIR"
  OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-40960}" OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-3}" \
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE="${OLLAMA_KV_CACHE_TYPE:-q8_0}" OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-24h}" \
  OLLAMA_MAX_LOADED_MODELS=1 nohup ollama serve >"$RUN_DIR/ollama.log" 2>&1 &
  echo $! >"$RUN_DIR/ollama.pid"
  for _ in $(seq 1 50); do up && break; sleep 0.2; done
  up && echo "project ollama on :$PORT (pid $(cat "$RUN_DIR/ollama.pid"), log $RUN_DIR/ollama.log)" \
     || { echo "ollama failed to start, see $RUN_DIR/ollama.log" >&2; exit 1; }
}

cmd_pull() { cmd_ollama; ollama pull "$MODEL"; [[ "${WITH_FP8:-1}" == 1 ]] && ollama pull "$MODEL_FP8" || true; }

cmd_env() {
  echo "export LLM_MODE=llm BASE_URL=http://127.0.0.1:$PORT/v1 MODEL=$MODEL"
}

cmd_app() {
  cd "$REPO"
  if [[ "${1:-}" == "--mock" ]]; then LLM_MODE=mock exec uv run python -m warroom.api; fi
  cmd_ollama
  LLM_MODE=llm BASE_URL="http://127.0.0.1:$PORT/v1" MODEL="$MODEL" exec uv run python -m warroom.api
}

cmd_bench() {
  cmd_ollama
  cd "$REPO"
  export BASE_URL="http://127.0.0.1:$PORT/v1" MODEL="$MODEL"
  uv run python -m bench.ttft --runs "${RUNS:-5}"
  uv run python -m bench.concurrent --runs 3
  uv run python -m bench.full_event --runs 3
  uv run python -m bench.memory_check
  uv run python -m bench.accuracy --compare "$BASE_URL|$MODEL" "$BASE_URL|$MODEL_FP8"
  uv run python -m bench.unplug_test
  uv run python -m bench.tokens_per_watt --duration 60 ${WATTS:+--watts $WATTS} ${IDLE_WATTS:+--idle-watts $IDLE_WATTS}
  uv run python -m bench.report
}

cmd_stop() {
  if [[ -f "$RUN_DIR/ollama.pid" ]]; then kill "$(cat "$RUN_DIR/ollama.pid")" 2>/dev/null || true; rm -f "$RUN_DIR/ollama.pid"; fi
  echo "stopped project ollama"
}

case "${1:-}" in
  ollama) cmd_ollama ;; pull) cmd_pull ;; app) shift; cmd_app "$@" ;; bench) cmd_bench ;;
  env) cmd_env ;; stop) cmd_stop ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
