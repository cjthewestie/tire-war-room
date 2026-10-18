#!/usr/bin/env bash
# One-time setup of a fresh WSL2 Ubuntu 22.04 (or another Debian/Ubuntu host) for the Linux dev stack, then
# `deploy/linux/dev.sh setup`. Needs internet and root (WSL's default user is root). Safe to re-run.
#   - node 22 from NodeSource: web/ builds with Vite 8 (node 22.12+). Ubuntu's own nodejs 12 is removed first,
#     because its split packages (libnode72, nodejs-doc) collide with NodeSource's single nodejs package.
#   - uv from astral.sh when missing.
# Ollama is not installed here; `deploy/linux/dev.sh ollama` expects the `ollama` binary (ollama.com/download).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
export PATH="$HOME/.local/bin:$PATH"

node_ok() {
  local v major minor
  v="$(node -v 2>/dev/null | sed 's/^v//')"
  major="${v%%.*}"; minor="$(echo "$v" | cut -d. -f2)"
  [[ -n "$v" ]] && (( major > 22 || (major == 22 && minor >= 12) ))
}

if ! node_ok; then
  if dpkg -s libnode72 >/dev/null 2>&1; then $SUDO apt-get remove -y nodejs nodejs-doc libnode72; fi
  curl -fsSL https://deb.nodesource.com/setup_22.x | $SUDO bash -
  $SUDO apt-get install -y nodejs
fi
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
echo "node $(node -v) · npm $(npm -v) · $(uv --version)"
exec bash "$REPO/deploy/linux/dev.sh" setup
