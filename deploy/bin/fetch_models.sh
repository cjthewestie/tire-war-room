#!/usr/bin/env bash
# Download every weight the Spark needs into HF_CACHE while the box is still online (run once, or on the
# online build machine and carry ${HF_CACHE} over on USB). After this the stack runs with HF_HUB_OFFLINE=1.
#   deploy/bin/fetch_models.sh            # NVFP4 + DSpark draft (production)
#   WITH_CONTROL=1 deploy/bin/fetch_models.sh   # + BF16 checkpoint for the FP8 control group (~60 GB)
set -euo pipefail
HF_CACHE="${HF_CACHE:-/opt/warroom/hf}"
mkdir -p "$HF_CACHE"
models=(nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark)
[[ "${WITH_CONTROL:-0}" == "1" ]] && models+=(nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16)
for m in "${models[@]}"; do
  echo "== $m"
  HF_HOME="$HF_CACHE" HF_HUB_OFFLINE=0 uvx --from 'huggingface_hub[cli]' hf download "$m"
done
du -sh "$HF_CACHE"
