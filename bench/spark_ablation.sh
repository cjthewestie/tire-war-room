#!/usr/bin/env bash
# Page-10 control group on the DGX Spark: "以 FP8、無快取、無推測解碼再跑一次，列出各項最佳化貢獻".
# Restarts the vllm service once per config, waits for health, runs the latency benches, tags results.
#   deploy/spark/.env must exist; run from the repo root on the Spark:  bench/spark_ablation.sh
# Configs (QUANT SPEC PREFIX_CACHE KV_DTYPE):
#   full        nvfp4 dspark 1 fp8    production
#   no_spec     nvfp4 off    1 fp8    -> contribution of speculative decoding
#   no_cache    nvfp4 dspark 0 fp8    -> contribution of prefix caching
#   mtp         nvfp4 mtp    1 fp8    -> built-in MTP instead of the DSpark draft
#   control     fp8   off    0 auto   -> page-10 control group
# Then the accuracy gap: full (NVFP4) vs control (FP8) on the factory quiz.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose --project-directory deploy/spark"
URL="${URL:-http://127.0.0.1:8001}"
CONFIGS="${CONFIGS:-full no_spec no_cache mtp control}"
OUT="bench/results/spark-ablation-$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"

declare -A CFG=(
  [full]="nvfp4 dspark 1 fp8" [no_spec]="nvfp4 off 1 fp8" [no_cache]="nvfp4 dspark 0 fp8"
  [mtp]="nvfp4 mtp 1 fp8" [control]="fp8 off 0 auto"
)
wait_up() { for _ in $(seq 1 240); do curl -fsS "$URL/health" >/dev/null 2>&1 && return 0; sleep 5; done; return 1; }

for c in $CONFIGS; do
  read -r q s p k <<<"${CFG[$c]}"
  echo "=== $c: QUANT=$q SPEC=$s PREFIX_CACHE=$p KV_DTYPE=$k"
  QUANT=$q SPEC=$s PREFIX_CACHE=$p KV_DTYPE=$k GPU_MEM_UTIL=$([[ $q == fp8 ]] && echo 0.45 || echo 0.28) \
    $COMPOSE up -d --force-recreate vllm
  wait_up || { echo "vllm did not come up for $c" >&2; continue; }
  export BASE_URL="$URL/v1" MODEL=nemotron
  uv run python -m bench.ttft --runs 10 --out-dir "$OUT/$c"
  uv run python -m bench.concurrent --runs 5 --out-dir "$OUT/$c"
  uv run python -m bench.full_event --runs 3 --out-dir "$OUT/$c"
  uv run python -m bench.memory_check --out-dir "$OUT/$c"
  uv run python -m bench.accuracy --out-dir "$OUT/$c"
done
# restore production
$COMPOSE up -d --force-recreate vllm
echo "results in $OUT (accuracy per config; gap = full vs control score_points)"
