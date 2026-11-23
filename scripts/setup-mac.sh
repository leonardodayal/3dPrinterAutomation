#!/bin/bash
# One-shot setup for a Mac that will run printloop next to the printer (e.g. a Mac mini).
# Safe to re-run: every step checks whether it's already done.
#
#   1. Copy this project folder to the Mac (include .env and profiles/; skip .venv, runs/, tools/, printer-backup/)
#   2. bash scripts/setup-mac.sh
#
# Installs (Homebrew): python@3.12, colima, docker, openscad@snapshot, orcaslicer
#   without Homebrew (e.g. Intel Macs): the same tools into ~/.local/bin and ~/Applications, see install_without_brew
# Builds:  .venv, the Obico ML detector image, and starts the detector container
# Checks:  .env, Claude Code login, printer / camera reachability, sleep settings
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)

green() { printf '\033[32m✔ %s\033[0m\n' "$*"; }
warn()  { printf '\033[33m! %s\033[0m\n' "$*"; }
fail()  { printf '\033[31m✘ %s\033[0m\n' "$*"; exit 1; }
step()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }

case "$ROOT" in
  "$HOME/Documents"*|"$HOME/Desktop"*)
    fail "This folder may be iCloud-synced (Desktop & Documents). Unzip/move it outside, e.g. ~/3dPrinterAutomation";;
esac

# Without Homebrew (Intel Macs: Homebrew's installer refuses them; or a non-admin account), everything goes
# into the home folder from official release downloads, no admin needed:
#   ~/.local/bin: uv (+ Python 3.12), colima, limactl, docker   ~/.docker/cli-plugins: buildx
#   ~/Applications: OpenSCAD (latest snapshot), OrcaSlicer
# Versions are pinned to what was tested; bump them here.
COLIMA_VER=v0.10.3 LIMA_VER=2.3.0 DOCKER_VER=29.10.0 BUILDX_VER=v0.39.1 ORCA_VER=v2.5.2

install_without_brew() {
  local bin="$HOME/.local/bin" dl arch darch bxarch mp app
  arch=$(uname -m)                                    # x86_64 | arm64 (colima/lima naming)
  [ "$arch" = arm64 ] && darch=aarch64 bxarch=arm64 || darch=x86_64 bxarch=amd64
  dl=$(mktemp -d)
  mkdir -p "$bin" "$HOME/Applications" "$HOME/.docker/cli-plugins"
  export PATH="$bin:$PATH"

  if ! command -v uv >/dev/null; then
    echo "installing uv"; curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  fi
  uv python find 3.12 >/dev/null 2>&1 || uv python install 3.12
  green "python $(uv python find 3.12)"

  if ! command -v colima >/dev/null; then
    echo "installing colima $COLIMA_VER"
    (cd "$dl" && curl -fsSLO "https://github.com/abiosoft/colima/releases/download/$COLIMA_VER/colima-Darwin-$arch" \
      && curl -fsSLO "https://github.com/abiosoft/colima/releases/download/$COLIMA_VER/colima-Darwin-$arch.sha256sum" \
      && shasum -a 256 -c "colima-Darwin-$arch.sha256sum" >/dev/null) || fail "colima download failed"
    install -m 755 "$dl/colima-Darwin-$arch" "$bin/colima"
  fi
  green "colima"
  if ! command -v limactl >/dev/null; then
    echo "installing lima $LIMA_VER"
    local t="lima-$LIMA_VER-Darwin-$arch.tar.gz"
    (cd "$dl" && curl -fsSLO "https://github.com/lima-vm/lima/releases/download/v$LIMA_VER/$t" \
      && curl -fsSL "https://github.com/lima-vm/lima/releases/download/v$LIMA_VER/SHA256SUMS" -o lima.sums \
      && grep " $t\$" lima.sums | shasum -a 256 -c >/dev/null) || fail "lima download failed"
    tar -xzf "$dl/$t" -C "$HOME/.local"               # bin/limactl + share/lima
  fi
  green "lima"
  if ! command -v docker >/dev/null; then
    echo "installing docker CLI $DOCKER_VER"
    curl -fsSL "https://download.docker.com/mac/static/stable/$darch/docker-$DOCKER_VER.tgz" | tar -xz -C "$dl"
    install -m 755 "$dl/docker/docker" "$bin/docker"
  fi
  green "docker CLI"
  if [ ! -x "$HOME/.docker/cli-plugins/docker-buildx" ]; then
    curl -fsSL "https://github.com/docker/buildx/releases/download/$BUILDX_VER/buildx-$BUILDX_VER.darwin-$bxarch" \
      -o "$HOME/.docker/cli-plugins/docker-buildx"
    chmod +x "$HOME/.docker/cli-plugins/docker-buildx"
  fi
  green "docker buildx"

  # Apps: mount the official dmg, copy the .app into ~/Applications.
  install_dmg() {  # url app-name
    [ -d "/Applications/$2.app" ] || [ -d "$HOME/Applications/$2.app" ] && { green "$2"; return; }
    echo "installing $2"
    curl -fsSL "$1" -o "$dl/app.dmg"
    mp=$(hdiutil attach -nobrowse -readonly "$dl/app.dmg" | tail -1 | cut -f3-)
    app=$(ls -d "$mp"/*.app | head -1)
    ditto "$app" "$HOME/Applications/$2.app"
    hdiutil detach -quiet "$mp"; rm -f "$dl/app.dmg"
    green "$2"
  }
  local scad
  scad=$(curl -fsSL https://files.openscad.org/snapshots/ | grep -oE 'OpenSCAD-[0-9.]+\.dmg' | sort -uV | tail -1)
  [ -n "$scad" ] || fail "couldn't find an OpenSCAD snapshot at files.openscad.org/snapshots"
  install_dmg "https://files.openscad.org/snapshots/$scad" OpenSCAD
  install_dmg "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/$ORCA_VER/OrcaSlicer_Mac_universal_${ORCA_VER#v}.dmg" OrcaSlicer
  rm -rf "$dl"

  grep -q '.local/bin' "$HOME/.bash_profile" "$HOME/.zprofile" "$HOME/.zshrc" 2>/dev/null \
    || warn "add ~/.local/bin to your PATH (e.g. in ~/.zprofile) to use colima/docker in Terminal"
  PY="$(uv python find 3.12)"
}

# Find an installed .app in /Applications or ~/Applications; prints the path to its binary.
app_bin() { for d in /Applications "$HOME/Applications"; do [ -x "$d/$1.app/Contents/MacOS/$2" ] && { echo "$d/$1.app/Contents/MacOS/$2"; return; }; done; }

step "Tools"
if command -v brew >/dev/null; then
  green "brew $(brew --version | head -1 | cut -d' ' -f2)"
  for f in python@3.12 colima docker; do
    brew list --formula "$f" >/dev/null 2>&1 && green "$f" || { echo "installing $f"; brew install "$f"; }
  done
  for c in openscad@snapshot orcaslicer; do
    brew list --cask "$c" >/dev/null 2>&1 && green "$c" || { echo "installing $c"; brew install --cask "$c"; }
  done
  PY="$(brew --prefix python@3.12)/bin/python3.12"
else
  warn "no Homebrew: installing into your home folder instead (~/.local/bin, ~/Applications)"
  install_without_brew
fi

step "Python environment"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
green "venv ready ($(.venv/bin/python --version))"

step "Configuration"
[ -f .env ] || fail ".env missing: copy it from the old machine (it holds tokens and printer settings)"
for f in profiles/k1c_machine.json profiles/k1c_process.json profiles/k1c_filament_pla.json; do
  [ -f "$f" ] || fail "$f missing: copy profiles/ from the old machine"
done
green ".env and slicer profiles present"
# Point .env at this Mac's OrcaSlicer and OpenSCAD, wherever .env said they were before.
ORCA=$(app_bin OrcaSlicer OrcaSlicer); [ -n "$ORCA" ] || fail "OrcaSlicer.app not found in /Applications or ~/Applications"
sed -i '' "s#^SLICER_BIN=.*#SLICER_BIN=$ORCA#" .env
if command -v openscad >/dev/null; then SCAD=openscad; else SCAD=$(app_bin OpenSCAD OpenSCAD); fi
[ -n "$SCAD" ] || fail "OpenSCAD not found (no openscad command, no OpenSCAD.app)"
sed -i '' "s#^OPENSCAD_BIN=.*#OPENSCAD_BIN=$SCAD#" .env
green "slicer: $ORCA | openscad: $SCAD"

step "Docker (Colima) + Obico failure detector"
# vz = Apple's Virtualization.framework (no QEMU needed; Homebrew's colima pulls in QEMU, the no-brew install doesn't)
colima status >/dev/null 2>&1 || colima start --vm-type vz --cpu 4 --memory 8
green "colima running"
if ! docker image inspect printloop/obico-ml-api:local >/dev/null 2>&1; then
  # tarball, not git clone: without the Command Line Tools, macOS's git is only an installer stub
  if [ ! -d tools/obico-server/ml_api ]; then
    mkdir -p tools/obico-server
    curl -fsSL https://github.com/TheSpaghettiDetective/obico-server/archive/HEAD.tar.gz \
      | tar -xz -C tools/obico-server --strip-components 1
  fi
  echo "building the detector image (~6 GB, about 5-10 min)…"
  docker build -q -t printloop/obico-ml-api:local tools/obico-server/ml_api
fi
scripts/obico-ml.sh >/dev/null
for i in $(seq 30); do curl -sf http://127.0.0.1:3333/hc/ >/dev/null && break; sleep 2; done
curl -sf http://127.0.0.1:3333/hc/ >/dev/null && green "detector healthy on 127.0.0.1:3333" || warn "detector not healthy yet: docker logs obico-ml"

step "Claude Code (your claude.ai subscription)"
if ! command -v claude >/dev/null; then
  warn "Claude Code not installed. Install it (https://claude.com/claude-code), then run: claude auth login"
elif env -u ANTHROPIC_API_KEY claude auth status 2>/dev/null | grep -q '"authMethod": "claude.ai"'; then
  green "logged in with claude.ai"
else
  warn "Not logged in with a claude.ai account. Run: claude auth login"
fi

step "Printer and camera"
.venv/bin/python - <<'EOF' || warn "printer checks failed: is this Mac on the printer's network?"
from printloop.config import Config
from printloop.moonraker import Moonraker
import requests
c = Config.load()
m = Moonraker(c.moonraker_url, c.moonraker_api_key, web_url=c.moonraker_web_url)
print("  printer:", m.printer_info().get("state"), "| busy:", m.busy_reason() or "no")
r = requests.get(c.camera_snapshot_url, timeout=10); r.raise_for_status()
print("  camera:", len(r.content), "bytes from", c.camera_snapshot_url)
EOF

step "Sleep"
if pmset -g | grep -E '^\s*sleep\s+0' >/dev/null; then
  green "system sleep is off"
else
  warn "This Mac may sleep and stop monitoring. System Settings > Energy: turn on 'Prevent automatic sleeping'"
fi

step "Final check"
.venv/bin/python -m pytest -q 2>&1 | tail -1
.venv/bin/python -m printloop preflight || warn "preflight reported problems (see above)"
echo
green "Done. Start the Slack request service with:  .venv/bin/python -m printloop serve"
