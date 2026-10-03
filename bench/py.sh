#!/usr/bin/env bash
# Run the benches with the right Python, from the repo root, on the Mac and on the DGX Spark alike:
#   bench/py.sh -m bench.ttft --runs 10
#   sudo -E bench/py.sh -m bench.unplug_test --block --app-url http://127.0.0.1:8000
# Interpreter, first match wins:
#   1. $WARROOM_PY                                  explicit override
#   2. ${WARROOM_ROOT:-/opt/warroom}/venv/bin/python   the Spark: built by deploy/bin/install_spark.sh while the box
#      was still online, so it works after warroom-firewall.service has cut PyPI. Host-native on purpose:
#      memory_check reads host processes, docker stats and nvidia-smi; unplug_test drives nft as root.
#   3. uv run python                                 dev Mac (the .venv from `uv sync`)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="${WARROOM_ROOT:-/opt/warroom}"
if [[ -n "${WARROOM_PY:-}" ]]; then
  exec "$WARROOM_PY" "$@"
elif [[ -x "$ROOT/venv/bin/python" ]]; then
  # offline: the embedding model was pre-fetched by install_spark.sh; never reach for the Hub
  [[ -d "$ROOT/fastembed" ]] && export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$ROOT/fastembed}"
  export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}" TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}" DO_NOT_TRACK=1
  exec "$ROOT/venv/bin/python" "$@"
elif command -v uv >/dev/null 2>&1; then
  exec uv run python "$@"
fi
echo "bench/py.sh: no interpreter. On the Spark run deploy/bin/install_spark.sh (builds $ROOT/venv);" \
     "on a dev machine install uv and run 'uv sync'; or set WARROOM_PY." >&2
exit 127
