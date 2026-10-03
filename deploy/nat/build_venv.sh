#!/usr/bin/env bash
# Build the NeMo Agent Toolkit venv the page-10 profiler runs from on the DGX Spark: ${ROOT}/nat-venv.
# Called by deploy/bin/install_spark.sh right after it builds the bench venv ${ROOT}/venv, while the box is
# still online (warroom-firewall.service cuts PyPI from the next boot on, and the host has no uv on PATH).
#   deploy/nat/build_venv.sh                    # ROOT=/opt/warroom, uv from $UV or PATH or ${ROOT}/bin/uv
#   SKIP_PULL=1 deploy/nat/build_venv.sh        # offline: install from the uv cache only
# Layout mirrors `uv run --with-requirements deploy/nat/requirements.txt` on the dev Mac: nat-venv holds only
# NAT and its own resolution (nvidia-nat-profiler pins pandas<3 / rich<15, so it cannot join the locked base
# env), and a .pth appends ${ROOT}/venv's site-packages after its own, so ortools/qdrant/fastembed come from
# the frozen base env and NAT's pins win where they overlap. The base venv is synced --no-install-project,
# so the war-room's "nat.components" entry point (pyproject.toml) is written here as a tiny dist-info; the
# code itself is imported from the release dir (deploy/nat/run.sh puts it first on PYTHONPATH), so an
# offline update/rollback that swaps ${ROOT}/current needs no rebuild unless requirements.txt changed.
# Env: WARROOM_ROOT (default /opt/warroom), UV, SKIP_PULL=1, UV_CACHE_DIR (default ${ROOT}/.uv-cache).
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
ROOT="${WARROOM_ROOT:-/opt/warroom}"
BASE="$ROOT/venv"
NV="$ROOT/nat-venv"
UV="${UV:-$(command -v uv 2>/dev/null || echo "$ROOT/bin/uv")}"
[[ -x "$UV" ]] || { echo "build_venv: no uv (set UV=, or run install_spark.sh which fetches $ROOT/bin/uv)" >&2; exit 1; }
[[ -x "$BASE/bin/python" ]] || { echo "build_venv: base venv $BASE missing (install_spark.sh builds it first)" >&2; exit 1; }
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.uv-cache}" UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$ROOT/python}"
offline=(); [[ "${SKIP_PULL:-0}" == 1 ]] && offline=(--offline)

echo "building the NAT profiler venv $NV over $BASE"
# same interpreter as the base venv, so the base site-packages (compiled wheels) load in it
base_exe="$("$BASE/bin/python" -c 'import sys; print(sys._base_executable)')"
"$UV" venv --clear --quiet --python "$base_exe" "$NV" ${offline[@]+"${offline[@]}"}
"$UV" pip install --quiet --python "$NV/bin/python" -r "$SRC/deploy/nat/requirements.txt" ${offline[@]+"${offline[@]}"}

site="$("$NV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
base_site="$("$BASE/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
echo "$base_site" > "$site/zz_warroom_base_venv.pth"     # appended after nat-venv's own packages
# entry point(s) straight from pyproject.toml, so this never drifts from the dev install
"$NV/bin/python" - "$SRC/pyproject.toml" "$site" <<'PY'
import pathlib, sys, tomllib
py = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
eps = py["project"]["entry-points"]["nat.components"]
d = pathlib.Path(sys.argv[2]) / "warroom_nat_entrypoint-0.dist-info"
d.mkdir(exist_ok=True)
(d / "METADATA").write_text("Metadata-Version: 2.1\nName: warroom-nat-entrypoint\nVersion: 0\n")
(d / "entry_points.txt").write_text("[nat.components]\n" + "".join(f"{k} = {v}\n" for k, v in eps.items()))
(d / "RECORD").write_text("")
(d / "INSTALLER").write_text("warroom-build_venv\n")
PY
"$UV" pip freeze --python "$NV/bin/python" > "$NV/requirements.freeze"   # what the profiler records ran on

# prove it: NAT imports, the war-room plugin entry point is visible, and the plugin module loads from SRC
(cd "$SRC" && PYTHONPATH="$SRC" HF_HUB_OFFLINE=1 DO_NOT_TRACK=1 "$NV/bin/python" -c "
import importlib.metadata as m, nat.cli.main, ortools, qdrant_client, warroom.natkit.register
eps = {e.name: e.value for e in m.entry_points(group='nat.components')}
assert eps.get('warroom') == 'warroom.natkit.register', eps
print('nat venv ok:', m.version('nvidia-nat'), '| pandas', m.version('pandas'))") \
  || { echo "build_venv: $NV cannot load NAT with the war-room plugin" >&2; exit 1; }
[[ -x "$NV/bin/nat" ]] || { echo "build_venv: $NV/bin/nat missing" >&2; exit 1; }
chmod -R a+rX "$NV" 2>/dev/null || true
