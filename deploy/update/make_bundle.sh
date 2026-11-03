#!/usr/bin/env bash
# Build an offline update bundle on an ONLINE machine; carry it to the Spark on USB or push over the LAN
# (page 10: 離線更新 USB／區網更新，一鍵回滾).
#   deploy/update/make_bundle.sh [version] [out_dir]
# Bundle = warroom-<ver>.tar containing:
#   src.tar.gz         git archive of the repo at HEAD (code, deploy/, bench/, web sources)
#   images.tar         `docker save` of warroom-app:<ver> + IMAGES (default: MQTT_IMAGE from deploy/spark/images.lock,
#                      the small broker compose needs, digest-pinned; add the VLLM_IMAGE / QDRANT_IMAGE refs from
#                      images.lock for a first offline install)
#   MANIFEST           version, git sha, created, image list, app_image + app_image_id (the built image's ID: the
#                      app has no registry digest, so this is its pin; offline_update.sh checks it after docker load)
#   SHA256SUMS         checked by offline_update.sh before anything is touched
# Env: SKIP_IMAGES=1 (code-only bundle, tests), IMAGES="extra images" (default images.lock's MQTT_IMAGE; IMAGES= for none),
#      DOCKER=docker,
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
  tar -C "$SRC" --exclude=.git --exclude=.claude --exclude=node_modules --exclude=.venv --exclude=data/runtime \
      --exclude=deploy/releases --exclude=bench/results -czf "$work/src.tar.gz" .
else
  git -C "$SRC" archive --format=tar.gz -o "$work/src.tar.gz" HEAD
fi
images=()
app_image_id=""
lock_mqtt="$(sed -n 's/^MQTT_IMAGE=//p' "$SRC/deploy/spark/images.lock" | tail -1)"
if [[ "${SKIP_IMAGES:-0}" != 1 ]]; then
  $DOCKER build -f "$SRC/deploy/Dockerfile" -t "warroom-app:$VER" "$SRC"
  app_image_id="$($DOCKER image inspect --format '{{.Id}}' "warroom-app:$VER")"
  extra=(${IMAGES-$lock_mqtt})
  for img in ${extra[@]+"${extra[@]}"}; do
    $DOCKER image inspect "$img" >/dev/null 2>&1 || $DOCKER pull "$img"
  done
  images=("warroom-app:$VER" ${extra[@]+"${extra[@]}"})
  $DOCKER save -o "$work/images.tar" "${images[@]}"
fi
{
  echo "version=$VER"
  echo "git_sha=$(git -C "$SRC" rev-parse HEAD)"
  echo "created=$(date '+%Y-%m-%dT%H:%M:%S%z')"
  echo "images=${images[*]:-}"
  echo "app_image=warroom-app:$VER"
  echo "app_image_id=$app_image_id"
} >"$work/MANIFEST"
( cd "$work" && sha256 $(ls | grep -v SHA256SUMS) >SHA256SUMS )
tar -C "$work" -cf "$OUT/warroom-$VER.tar" .
echo "$OUT/warroom-$VER.tar"
