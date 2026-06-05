#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/StickS3TaskHub"
PLIST="$HOME/Library/LaunchAgents/com.sticks3.taskhub.plist"
TOKEN_FILE="$APP_DIR/token"

export PATH="$HOME/.local/node/bin:$HOME/.local/node-v22.22.1-darwin-arm64/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents"

cp "$ROOT/host/task_hub.py" "$APP_DIR/task_hub.py"
chmod +x "$APP_DIR/task_hub.py"
for helper in "$ROOT"/host/taskhub_*.py; do
  [ -f "$helper" ] || continue
  cp "$helper" "$APP_DIR/$(basename "$helper")"
done

if [ ! -s "$TOKEN_FILE" ]; then
  TOKEN=""
  if [ -f "$ROOT/firmware/task_monitor/secrets.h" ]; then
    TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/task_monitor/secrets.h" || true)"
  elif [ -f "$ROOT/firmware/voice_recorder/secrets.h" ]; then
    TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/voice_recorder/secrets.h" || true)"
  fi
  if [ -z "$TOKEN" ]; then
    TOKEN="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24 || true)"
  fi
  if [ -z "$TOKEN" ]; then
    TOKEN="dev-token"
  fi
  umask 077
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
fi

cat > "$APP_DIR/run_task_hub.sh" <<'SH'
#!/bin/sh
set -eu

ROOT="$HOME/Library/Application Support/StickS3TaskHub"
TOKEN="$(cat "$ROOT/token" 2>/dev/null || true)"

export TASK_HUB_TOKEN="${TOKEN:-dev-token}"
export PATH="$HOME/.local/node/bin:$HOME/.local/node-v22.22.1-darwin-arm64/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec /usr/bin/python3 -u "$ROOT/task_hub.py" --bind 0.0.0.0 --port 5577
SH
chmod +x "$APP_DIR/run_task_hub.sh"

if command -v npm >/dev/null 2>&1; then
  (
    cd "$APP_DIR"
    if [ ! -f package.json ]; then
      npm init -y >/dev/null 2>&1 || true
    fi
    npm install classic-level >/dev/null 2>&1 || true
  )
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.sticks3.taskhub</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$APP_DIR/run_task_hub.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$APP_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$APP_DIR/task_hub.log</string>
  <key>StandardErrorPath</key>
  <string>$APP_DIR/task_hub.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/com.sticks3.taskhub"

echo "Task Hub installed and started."
echo "Health: http://127.0.0.1:5577/health"
