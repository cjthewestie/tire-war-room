#!/usr/bin/env bash
# vLLM launcher for Nemotron 3.5 Lightning 30B-A3B on one DGX Spark (GB10, 128 GB unified, 240 W).
# Runs inside the vllm/vllm-openai container (mounted by deploy/spark/docker-compose.yml).
#
# Knobs (env), so the same script serves the production config AND the page-10 control group:
#   QUANT=nvfp4 | fp8          nvfp4: NVFP4 checkpoint (production). fp8: BF16 checkpoint quantised online to FP8
#                              (control group; NVIDIA publishes no Lightning FP8 checkpoint as of 2026-10-03)
#   SPEC=dspark | mtp | off    speculative decoding. dspark = NVIDIA's 2 GB draft (= the "草稿模型 2 GB" on p.9)
#   PREFIX_CACHE=1 | 0         automatic prefix caching (TTFT target assumes it)
#   KV_DTYPE=fp8 | auto        KV cache dtype
#   MAX_MODEL_LEN=32768        p.9/p.10: 32k context incl. supervisor dialogue
#   MAX_NUM_SEQS=4             3 concurrent anomalies + 1 spare
#   GPU_MEM_UTIL=0.23          fraction of the 128 GB pool vLLM pre-allocates (0.23 x 128 = ~29.4 GB). Page-9 budget
#                              for the model server is 19+2+4+5 = 30 GB; with Qdrant 3 + app 6 + DGX OS 10 the stack
#                              lands at ~48-49 GB, inside the page-9 36-50 GB band (0.28 gave ~55 GB, outside it).
#                              The model card's 0.85 would take ~109 GB.
#                              Real weight sizes (HF API ?blobs=true, 2026-10-03): NVFP4 checkpoint 21.6 GB (page 9
#                              says 19), DSpark draft 1.3 GB (page 9 says 2) -> ~22.9 GB, leaving ~6.5 GB of the
#                              fraction for KV + activations + CUDA graphs. KV for 4 x 32k is small (hybrid Mamba:
#                              only a few attention layers carry KV, fp8), but this is NOT yet measured on a Spark:
#                              run `bench.memory_check` there with nvidia-smi at MAX_MODEL_LEN=32768 before quoting
#                              the band. If vLLM reports "not enough KV cache", raise to 0.25 (~32 GB, stack ~51 GB).
#
# Sources (read 2026-10-03):
#   model card + DGX Spark command: https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4
#     (--moe-backend marlin --kv-cache-dtype fp8 --enable-prefix-caching --mamba-backend flashinfer
#      --mamba-cache-mode align --reasoning-parser nemotron_v3 --tool-call-parser qwen3_coder
#      --enable-auto-tool-choice --speculative_config.model <DSpark> --speculative_config.num_speculative_tokens 3;
#      vLLM >= 0.27.1; MTP is built into the checkpoint)
#   vLLM recipe: https://recipes.vllm.ai/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16 (image v0.28.0)
#   structured outputs: https://docs.vllm.ai/en/latest/features/structured_outputs.html
#     (--structured-outputs-config.backend xgrammar; clients use response_format json_schema;
#      the old guided_json field was removed in vLLM 0.12)
set -euo pipefail

QUANT="${QUANT:-nvfp4}"
SPEC="${SPEC:-dspark}"
PREFIX_CACHE="${PREFIX_CACHE:-1}"
KV_DTYPE="${KV_DTYPE:-fp8}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-32768}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-4}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.23}"
PORT="${PORT:-8000}"

NVFP4_CKPT="${NVFP4_CKPT:-nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4}"
BF16_CKPT="${BF16_CKPT:-nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16}"
DSPARK_CKPT="${DSPARK_CKPT:-nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark}"

args=()
case "$QUANT" in
  nvfp4) MODEL="$NVFP4_CKPT"; args+=(--moe-backend marlin) ;;
  fp8)   MODEL="$BF16_CKPT";  args+=(--quantization fp8) ;;
  *) echo "QUANT must be nvfp4|fp8" >&2; exit 2 ;;
esac

args+=(
  --served-model-name "$MODEL" nemotron
  --host 0.0.0.0 --port "$PORT"
  --max-model-len "$MAX_MODEL_LEN"
  --max-num-seqs "$MAX_NUM_SEQS"
  --gpu-memory-utilization "$GPU_MEM_UTIL"
  --kv-cache-dtype "$KV_DTYPE"
  --mamba-backend flashinfer
  --mamba-cache-mode align
  --reasoning-parser nemotron_v3
  --tool-call-parser qwen3_coder
  --enable-auto-tool-choice
  --structured-outputs-config.backend xgrammar
)

if [[ "$PREFIX_CACHE" == "1" ]]; then args+=(--enable-prefix-caching); else args+=(--no-enable-prefix-caching); fi

case "$SPEC" in
  dspark) args+=(--speculative_config.model "$DSPARK_CKPT" --speculative_config.num_speculative_tokens 3) ;;
  mtp)    args+=(--speculative-config '{"method":"mtp","num_speculative_tokens":3}') ;;
  off)    ;;
  *) echo "SPEC must be dspark|mtp|off" >&2; exit 2 ;;
esac

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  printf '%q ' vllm serve "$MODEL" "${args[@]}"; echo
  exit 0
fi
exec vllm serve "$MODEL" "${args[@]}"
