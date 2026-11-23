#!/bin/bash
# Run `printloop serve` automatically at login and keep it running (macOS LaunchAgent).
#   bash scripts/install-launchd.sh            install / update and start
#   bash scripts/install-launchd.sh uninstall  stop and remove
#   launchctl kickstart -k gui/$(id -u)/com.printloop.serve   restart (e.g. after a code change)
#
# The service runs under `caffeinate -i`, so the Mac doesn't sleep while it runs (the display still can).
# Logs: runs/serve.log and runs/serve.err.log. Also makes Colima (Docker) start at login so the
# Obico detector container (restart: unless-stopped) comes back after a reboot.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
LABEL=com.printloop.serve
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

case "$ROOT" in
  "$HOME/Documents"*|"$HOME/Desktop"*)
    echo "This folder may be synced by iCloud (Desktop & Documents). Move it outside, e.g. ~/3dPrinterAutomation." >&2
    exit 1;;
esac

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  # The Colima agent (Macs without Homebrew) stays: Docker/Obico are useful without the service too.
  echo "Removed $LABEL"
  exit 0
fi

[ -x .venv/bin/python ] || { echo "Run scripts/setup-mac.sh first" >&2; exit 1; }
mkdir -p runs "$HOME/Library/LaunchAgents"
CLAUDE_DIR=$(dirname "$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")")

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$ROOT/.venv/bin/python</string><string>-u</string><string>-m</string><string>printloop</string><string>serve</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$CLAUDE_DIR:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$ROOT/runs/serve.log</string>
  <key>StandardErrorPath</key><string>$ROOT/runs/serve.err.log</string>
</dict>
</plist>
EOF
plutil -lint "$PLIST" >/dev/null

if command -v brew >/dev/null; then
  brew services start colima >/dev/null 2>&1 || echo "! couldn't enable colima at login: run 'brew services start colima'"
elif command -v colima >/dev/null; then
  # No Homebrew (e.g. an Intel Mac, where Homebrew no longer installs): our own LaunchAgent for Colima,
  # doing what `brew services start colima` does. colima/limactl/docker live in ~/.local/bin.
  # Reloading this agent stops `colima start --foreground` and with it the Docker VM and the detector, so
  # only reload when the plist changed or the agent isn't loaded (re-running this script mid-print is safe).
  CPLIST="$HOME/Library/LaunchAgents/com.printloop.colima.plist"
  CNEW=$(mktemp)
  cat > "$CNEW" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.printloop.colima</string>
  <key>ProgramArguments</key>
  <array><string>$(command -v colima)</string><string>start</string><string>--foreground</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$(dirname "$(command -v colima)"):/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- restart only if it crashed: "already running" and a deliberate "colima stop" exit 0 -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$ROOT/runs/colima.log</string>
  <key>StandardErrorPath</key><string>$ROOT/runs/colima.log</string>
</dict>
</plist>
EOF
  plutil -lint "$CNEW" >/dev/null
  if cmp -s "$CNEW" "$CPLIST" && launchctl print "$DOMAIN/com.printloop.colima" >/dev/null 2>&1; then
    rm -f "$CNEW"                                     # unchanged and loaded: leave Colima running
  else
    mv "$CNEW" "$CPLIST"
    if launchctl print "$DOMAIN/com.printloop.colima" >/dev/null 2>&1; then
      launchctl bootout "$DOMAIN/com.printloop.colima" 2>/dev/null || true
      for _ in $(seq 60); do launchctl print "$DOMAIN/com.printloop.colima" >/dev/null 2>&1 || break; sleep 1; done
    fi
    launchctl bootstrap "$DOMAIN" "$CPLIST" || echo "! couldn't enable colima at login"
  fi
else
  echo "! colima not found: the Obico detector won't come back after a reboot"
fi
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 3
launchctl print "$DOMAIN/$LABEL" | grep -E "state =|pid =" || true
tail -n 3 runs/serve.log 2>/dev/null || true
echo "Installed $LABEL. Restart after code changes: launchctl kickstart -k $DOMAIN/$LABEL"
