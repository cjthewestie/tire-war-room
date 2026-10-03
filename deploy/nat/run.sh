#!/usr/bin/env bash
# Run the war-room event through NeMo Agent Toolkit (page 10: targets recorded with the NAT profiler).
#   deploy/nat/run.sh run                 # one page-11 event via `nat run` (mock commander)
#   deploy/nat/run.sh eval                # `nat eval` + profiler -> data/runtime/nat/
#   LLM=1 BASE_URL=http://127.0.0.1:8001/v1 MODEL=nemotron deploy/nat/run.sh eval   # real commander (Spark vLLM)
# On the Spark vLLM publishes :8001 (deploy/spark/docker-compose.yml, served name "nemotron"); :8000 is the app.
# NAT executable, first match wins (same idea as bench/py.sh):
#   1. $WARROOM_NAT                                   explicit override (path to a `nat` executable)
#   2. ${WARROOM_ROOT:-/opt/warroom}/nat-venv/bin/nat   the Spark: built by deploy/nat/build_venv.sh from
#      install_spark.sh while the box was still online, so it runs after warroom-firewall.service cuts PyPI
#   3. uv run --with-requirements deploy/nat/requirements.txt nat   dev Mac (NAT layered over the .venv)
set -euo pipefail
cd "$(dirname "$0")/../.."
mode="${1:-run}"
ROOT="${WARROOM_ROOT:-/opt/warroom}"
over=()
if [[ "${LLM:-0}" == "1" ]]; then
  over+=(--override workflow.llm_mode llm --override workflow.base_url "${BASE_URL:?}" --override workflow.model "${MODEL:?}")
fi
if [[ -n "${WARROOM_NAT:-}" ]]; then
  nat=("$WARROOM_NAT")
elif [[ -x "$ROOT/nat-venv/bin/nat" ]]; then
  # the release's own code first (nat-venv only carries the entry point); offline caches, never the Hub
  export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
  [[ -d "$ROOT/fastembed" ]] && export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$ROOT/fastembed}"
  export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}" DO_NOT_TRACK=1
  nat=("$ROOT/nat-venv/bin/nat")
elif command -v uv >/dev/null 2>&1; then
  nat=(uv run --with-requirements deploy/nat/requirements.txt nat)
else
  echo "deploy/nat/run.sh: no NAT. On the Spark run deploy/bin/install_spark.sh (builds $ROOT/nat-venv);" \
       "on a dev machine install uv; or set WARROOM_NAT." >&2
  exit 127
fi
case "$mode" in
  run)  "${nat[@]}" run --config_file deploy/nat/workflow.yml --input "${SCENARIO:-press7_temp_drop}" ${over[@]+"${over[@]}"} ;;
  eval) "${nat[@]}" eval --config_file deploy/nat/eval.yml ${over[@]+"${over[@]}"} ;;
  *) echo "usage: $0 run|eval" >&2; exit 2 ;;
esac
