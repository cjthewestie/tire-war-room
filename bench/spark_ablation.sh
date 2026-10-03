#!/usr/bin/env bash
# Page-10 control group on the DGX Spark: "以 FP8、無快取、無推測解碼再跑一次，列出各項最佳化貢獻".
# Restarts the vllm service once per config, waits for health, runs the latency benches, tags results.
#   deploy/spark/.env must exist; run from the repo root on the Spark:  bench/spark_ablation.sh
# Configs (QUANT SPEC PREFIX_CACHE KV_DTYPE):
#   full        nvfp4 dspark 1 fp8    production
#   no_spec     nvfp4 off    1 fp8    -> contribution of speculative decoding (decode tok/s, bench.decode)
#   no_cache    nvfp4 dspark 0 fp8    -> contribution of prefix caching
#   mtp         nvfp4 mtp    1 fp8    -> built-in MTP instead of the DSpark draft
#   control     fp8   off    0 auto   -> page-10 control group
# Then the accuracy gap: full (NVFP4) vs control (FP8) on the factory quiz.
# Weights: the stack is offline (HF_HUB_OFFLINE=1, boot firewall), so before vLLM is touched the run checks
# that every checkpoint the requested configs need is in HF_CACHE -- `control` needs the ~66 GB BF16 one
# (fetched by install_spark.sh unless WITH_CONTROL=0). Exits non-zero if any requested config did not come up
# or the NVFP4-vs-FP8 gap could not be paired, so a missing page-10 row is never a silent success.
# Python: bench/py.sh -- on the Spark that is /opt/warroom/venv (built by install_spark.sh while still online;
# the host has no uv and the boot firewall blocks PyPI), on a Mac the uv-managed .venv.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-bench/py.sh}"
# Fail before touching vLLM if the bench interpreter cannot import every bench this run needs.
"$PY" -c "import bench.ttft, bench.decode, bench.concurrent, bench.full_event, bench.memory_check, bench.accuracy, bench.tokens_per_watt, ortools, openai" \
  || { echo "bench interpreter not usable ($PY); on the Spark re-run deploy/bin/install_spark.sh while online" >&2; exit 1; }
[[ "${PREFLIGHT_ONLY:-0}" == 1 ]] && { echo "preflight ok: $("$PY" -c 'import sys; print(sys.executable)')"; exit 0; }
COMPOSE="${COMPOSE:-docker compose --project-directory deploy/spark}"
URL="${URL:-http://127.0.0.1:8001}"
CONFIGS="${CONFIGS:-full no_spec no_cache mtp control}"
WAIT_TRIES="${WAIT_TRIES:-240}" WAIT_SLEEP="${WAIT_SLEEP:-5}"

cfg() {   # QUANT SPEC PREFIX_CACHE KV_DTYPE per config (a case, not declare -A: macOS bash 3.2 runs the tests)
  case "$1" in
    full) echo "nvfp4 dspark 1 fp8" ;; no_spec) echo "nvfp4 off 1 fp8" ;; no_cache) echo "nvfp4 dspark 0 fp8" ;;
    mtp) echo "nvfp4 mtp 1 fp8" ;; control) echo "fp8 off 0 auto" ;; *) return 1 ;;
  esac
}
need_control=0
for c in $CONFIGS; do
  cfg "$c" >/dev/null || { echo "unknown config '$c' (known: full no_spec no_cache mtp control)" >&2; exit 2; }
  [[ "$(cfg "$c")" == fp8* ]] && need_control=1
done
# Weights, before touching vLLM: the same dir compose mounts (deploy/spark/.env), checked offline.
if [[ -z "${HF_CACHE:-}" && -f deploy/spark/.env ]]; then HF_CACHE="$(sed -n 's/^HF_CACHE=//p' deploy/spark/.env | tail -1)"; fi
HF_CACHE="${HF_CACHE:-/opt/warroom/hf}"
if ! HF_CACHE="$HF_CACHE" WITH_CONTROL="$need_control" bash deploy/bin/fetch_models.sh --check; then
  if (( need_control )); then
    echo "ablation aborted: the FP8 control group needs the BF16 checkpoint (~66 GB) in $HF_CACHE." >&2
    echo "  while online:  sudo HF_CACHE=$HF_CACHE WITH_CONTROL=1 deploy/bin/fetch_models.sh" >&2
    echo "  or skip it:    CONFIGS='full no_spec no_cache mtp' bench/spark_ablation.sh (no page-10 FP8 row)" >&2
  else
    echo "ablation aborted: production weights missing in $HF_CACHE; re-run deploy/bin/install_spark.sh while online" >&2
  fi
  exit 1
fi
OUT="bench/results/spark-ablation-$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"

# Pause the watchdog's restarts for the run, or it would kill vllm mid-load after 3 failed probes.
PAUSE_FILE="${PAUSE_FILE:-/run/warroom-watchdog/pause}"
pause_wd() { touch "$PAUSE_FILE" 2>/dev/null || sudo -n touch "$PAUSE_FILE" 2>/dev/null; }
unpause_wd() { rm -f "$PAUSE_FILE" 2>/dev/null || sudo -n rm -f "$PAUSE_FILE" 2>/dev/null || true; }
if [[ -d "$(dirname "$PAUSE_FILE")" ]]; then
  pause_wd || { echo "cannot create $PAUSE_FILE (run with sudo, or stop warroom-watchdog first)" >&2; exit 1; }
  trap unpause_wd EXIT
fi

wait_up() {
  for _ in $(seq 1 "$WAIT_TRIES"); do curl -fsS "$URL/health" >/dev/null 2>&1 && return 0; sleep "$WAIT_SLEEP"; done
  return 1
}

failed=()
for c in $CONFIGS; do
  read -r q s p k <<<"$(cfg "$c")"
  [[ -e "$PAUSE_FILE" ]] && { pause_wd || true; }   # refresh: the watchdog ignores a pause older than 4 h
  echo "=== $c: QUANT=$q SPEC=$s PREFIX_CACHE=$p KV_DTYPE=$k"
  QUANT=$q SPEC=$s PREFIX_CACHE=$p KV_DTYPE=$k GPU_MEM_UTIL=$([[ $q == fp8 ]] && echo 0.45 || echo 0.23) \
    $COMPOSE up -d --force-recreate vllm
  wait_up || { echo "vllm did not come up for $c" >&2; failed+=("$c"); continue; }
  export BASE_URL="$URL/v1" MODEL=nemotron
  "$PY" -m bench.ttft --runs 10 --out-dir "$OUT/$c"
  "$PY" -m bench.decode --runs 10 --config "$c" --out-dir "$OUT/$c"   # page 10: 推測解碼 95–108 tok/s, batch 1
  "$PY" -m bench.concurrent --runs 5 --out-dir "$OUT/$c"
  "$PY" -m bench.full_event --runs 3 --out-dir "$OUT/$c"
  "$PY" -m bench.memory_check --out-dir "$OUT/$c"
  "$PY" -m bench.accuracy --quant "$q" --out-dir "$OUT/$c"   # open book (headline), kb + generated quiz
  # tokens/s and, with a plug meter (TPW_ARGS="--watts <avg> --idle-watts <idle>" or "--meter-csv f.csv"), tokens/W
  # shellcheck disable=SC2086
  "$PY" -m bench.tokens_per_watt --duration "${TPW_DURATION:-60}" ${TPW_ARGS:-} --out-dir "$OUT/$c"
done
# restore production (the watchdog stays paused until it is healthy again)
$COMPOSE up -d --force-recreate vllm
wait_up || echo "production vllm not healthy yet; the watchdog resumes now" >&2
# Page-10 accuracy gap: NVFP4 (full) vs the real FP8 control, paired per item with a 95% CI.
fa=$(ls "$OUT"/full/accuracy-*.json 2>/dev/null | head -1 || true)
fc=$(ls "$OUT"/control/accuracy-*.json 2>/dev/null | head -1 || true)
paired=0
if [[ -n "$fa" && -n "$fc" ]]; then
  "$PY" -m bench.accuracy --pair "$fa" "$fc" --out-dir "$OUT" && paired=1
else
  echo "accuracy gap not paired: need both full and control runs" >&2
fi
# Spec-decode contribution in tok/s: full and mtp vs no_spec, plus tokens/s per config.
"$PY" -m bench.decode --ablation "$OUT" --out-dir "$OUT" || echo "decode ablation summary failed" >&2
echo "results in $OUT (accuracy per config; paired NVFP4 vs FP8 gap in $OUT/accuracy-compare-*-paired-*.json)"
rc=0
if (( ${#failed[@]} )); then echo "FAILED: vllm never came up for: ${failed[*]}" >&2; rc=1; fi
if [[ " $CONFIGS " == *" full "* && " $CONFIGS " == *" control "* ]] && (( paired == 0 )); then
  echo "FAILED: page-10 NVFP4 vs FP8 accuracy gap has no Spark number" >&2; rc=1
fi
exit "$rc"
