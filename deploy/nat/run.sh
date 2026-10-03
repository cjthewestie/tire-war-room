#!/usr/bin/env bash
# Run the war-room event through NeMo Agent Toolkit, with NAT layered over the project env.
#   deploy/nat/run.sh run                 # one page-11 event via `nat run` (mock commander)
#   deploy/nat/run.sh eval                # `nat eval` + profiler -> data/runtime/nat/
#   LLM=1 BASE_URL=http://localhost:8000/v1 MODEL=nemotron deploy/nat/run.sh eval   # real commander (Spark vLLM)
set -euo pipefail
cd "$(dirname "$0")/../.."
mode="${1:-run}"
over=()
if [[ "${LLM:-0}" == "1" ]]; then
  over+=(--override workflow.llm_mode llm --override workflow.base_url "${BASE_URL:?}" --override workflow.model "${MODEL:?}")
fi
nat=(uv run --with-requirements deploy/nat/requirements.txt nat)
case "$mode" in
  run)  "${nat[@]}" run --config_file deploy/nat/workflow.yml --input "${SCENARIO:-press7_temp_drop}" ${over[@]+"${over[@]}"} ;;
  eval) "${nat[@]}" eval --config_file deploy/nat/eval.yml ${over[@]+"${over[@]}"} ;;
  *) echo "usage: $0 run|eval" >&2; exit 2 ;;
esac
