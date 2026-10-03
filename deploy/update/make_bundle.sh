#!/usr/bin/env bash
# Build an offline update bundle on an ONLINE machine; carry it to the Spark on USB or push over the LAN
# (page 10: 離線更新 USB／區網更新，一鍵回滾).
#   deploy/update/make_bundle.sh [version] [out_dir]
# Bundle = warroom-<ver>.tar containing:
#   src.tar.gz         git archive of the repo at HEAD (code, deploy/, bench/, web sources)
#   images.tar         `docker save` of warroom-app:<ver> (+ IMAGES="vllm/vllm-openai:... qdrant/..." if changed)
#   MANIFEST           version, git sha, created, image list
#   SHA256SUMS         checked by offline_update.sh before anything is touched
# Env: SKIP_IMAGES=1 (code-only bundle, tests), IMAGES="extra images to include", DOCKER=docker,
#      FROM_WORKTREE=1 (bundle the working tree instead of git HEAD)
set -euo pipefail
sha256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
VER="${1:-$(git -C "$SRC" rev-parse --short HEAD)}"
OUT="${2:-$SRC/deploy/releases}"
DOCKER="${DOCKER:-docker}"
work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
mkdir -p "$OUT"

if [[ "${FROM_WORKTREE:-0}" == 1 ]]; then   # hot-fix bundle from uncommitted tree (also used by tests)
  tar -C "$SRC" --exclude=.git --exclude=node_modules --exclude=.venv --exclude=data/runtime \
      --exclude=deploy/releases --exclude=bench/results -czf "$work/src.tar.gz" .
else
  git -C "$SRC" archive --format=tar.gz -o "$work/src.tar.gz" HEAD
fi
images=()
if [[ "${SKIP_IMAGES:-0}" != 1 ]]; then
  $DOCKER build -f "$SRC/deploy/Dockerfile" -t "warroom-app:$VER" "$SRC"
  images=("warroom-app:$VER" ${IMAGES:-})
  $DOCKER save -o "$work/images.tar" "${images[@]}"
fi
{
  echo "version=$VER"
  echo "git_sha=$(git -C "$SRC" rev-parse HEAD)"
  echo "created=$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "images=${images[*]:-}"
} >"$work/MANIFEST"
( cd "$work" && sha256 $(ls | grep -v SHA256SUMS) >SHA256SUMS )
tar -C "$work" -cf "$OUT/warroom-$VER.tar" .
echo "$OUT/warroom-$VER.tar"
