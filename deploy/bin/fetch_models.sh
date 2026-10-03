#!/usr/bin/env bash
# Download every weight the Spark needs into HF_CACHE while the box is still online (run once, or on the
# online build machine and carry ${HF_CACHE} over on USB). After this the stack runs with HF_HUB_OFFLINE=1.
# install_spark.sh calls this, then `--check`, and fails the install if any weight is missing.
#   deploy/bin/fetch_models.sh                  # NVFP4 (~21.6 GB) + DSpark draft (~1.4 GB), production
#   WITH_CONTROL=1 deploy/bin/fetch_models.sh   # + BF16 checkpoint for the FP8 control group (~66 GB);
#                                               # install_spark.sh passes WITH_CONTROL=1 by default
#   deploy/bin/fetch_models.sh --check          # offline: verify the cache only, download nothing
# hf CLI: $HF_CLI if set, else `hf` on PATH, else `uvx --from huggingface_hub[cli] hf`, else a private venv
# under ${HF_CACHE}/.hfcli built with python3 -m venv + pip (DGX OS ships python3, not uv).
set -euo pipefail
HF_CACHE="${HF_CACHE:-/opt/warroom/hf}"
MODE="${1:-fetch}"
models=(nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4 nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4-DSpark)
[[ "${WITH_CONTROL:-0}" == "1" ]] && models+=(nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16)

# A model is present when the HF cache has a snapshot with config.json, every shard its safetensors index
# names (or at least one .safetensors when unsharded), and no half-downloaded blob. Pure filesystem: no
# network and no hf CLI, so it also runs on the firewalled box.
check_model() {
  local id="$1" dir snap idx f ok
  dir="$HF_CACHE/hub/models--${id//\//--}"
  [[ -d "$dir/snapshots" ]] || { echo "weights MISSING: $id (no $dir/snapshots)" >&2; return 1; }
  if compgen -G "$dir/blobs/*.incomplete" >/dev/null; then
    echo "weights INCOMPLETE: $id (partial download in $dir/blobs)" >&2; return 1
  fi
  for snap in "$dir"/snapshots/*/; do
    [[ -f "$snap/config.json" ]] || continue
    ok=0
    idx="$snap/model.safetensors.index.json"
    if [[ -f "$idx" ]]; then
      ok=1
      while read -r f; do
        [[ -e "$snap/$f" ]] || { echo "weights MISSING: $id shard $f" >&2; ok=0; }
      done < <(grep -o '"[^"]*\.safetensors"' "$idx" | tr -d '"' | sort -u)
    elif compgen -G "$snap/*.safetensors" >/dev/null; then
      ok=1
    fi
    (( ok == 1 )) && { echo "weights ok: $id"; return 0; }
  done
  echo "weights MISSING: $id (no complete snapshot under $dir/snapshots)" >&2
  return 1
}

if [[ "$MODE" == "--check" ]]; then
  missing=0
  for m in "${models[@]}"; do check_model "$m" || missing=1; done
  (( missing == 0 )) || { echo "fetch_models --check: weights missing in $HF_CACHE; vLLM cannot start offline" >&2; exit 1; }
  exit 0
fi

hf_cli() {
  if [[ -n "${HF_CLI:-}" ]]; then $HF_CLI "$@"
  elif command -v hf >/dev/null; then hf "$@"
  elif command -v uvx >/dev/null; then uvx --from 'huggingface_hub[cli]' hf "$@"
  else
    local venv="$HF_CACHE/.hfcli"
    if [[ ! -x "$venv/bin/hf" ]]; then
      echo "no hf or uvx on PATH: building $venv with python3 -m venv + pip" >&2
      python3 -m venv "$venv"
      "$venv/bin/pip" install -q 'huggingface_hub[cli]'
    fi
    "$venv/bin/hf" "$@"
  fi
}

mkdir -p "$HF_CACHE"
for m in "${models[@]}"; do
  echo "== $m"
  HF_HOME="$HF_CACHE" HF_HUB_OFFLINE=0 hf_cli download "$m"
done
du -sh "$HF_CACHE"
HF_CACHE="$HF_CACHE" WITH_CONTROL="${WITH_CONTROL:-0}" bash "$0" --check
