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
# Copy whisper models onto the internal disk instead of symlinking into the
# repo: a launchd-run agent cannot follow a symlink into an external volume
# (possibly unmounted at login) or a TCC-protected folder such as ~/Desktop.
if [ -L "$APP_DIR/models" ]; then
  rm -f "$APP_DIR/models"
fi
mkdir -p "$APP_DIR/models"
# Only the model the Host actually uses (default, or TASK_HUB_WHISPER_MODEL):
# whisper models are 0.5-1.6GB each, so copying every *.bin wastes disk.
WHISPER_MODEL_NAME="$(basename "${TASK_HUB_WHISPER_MODEL:-ggml-large-v3-turbo-q5_0.bin}")"
model="$ROOT/host/models/$WHISPER_MODEL_NAME"
if [ -f "$model" ]; then
  target="$APP_DIR/models/$WHISPER_MODEL_NAME"
  if [ ! -f "$target" ] || [ "$(stat -f %z "$model")" != "$(stat -f %z "$target")" ]; then
    echo "Copying model $WHISPER_MODEL_NAME to $APP_DIR/models ..."
    cp "$model" "$target.partial" && mv "$target.partial" "$target"
  fi
fi

if [ ! -s "$TOKEN_FILE" ]; then
  TOKEN=""
  if [ -f "$ROOT/firmware/task_monitor/secrets.h" ]; then
    TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/task_monitor/secrets.h" || true)"
  elif [ -f "$ROOT/firmware/voice_recorder/secrets.h" ]; then
    TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/voice_recorder/secrets.h" || true)"
  fi
  if [ "$TOKEN" = "dev-token" ]; then
    TOKEN=""
  fi
  if [ -z "$TOKEN" ]; then
    TOKEN="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24 || true)"
  fi
  [ -n "$TOKEN" ] || {
    echo "Task Hub install failed: could not generate a secure device token." >&2
    exit 1
  }
  umask 077
  printf '%s' "$TOKEN" > "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
if [ "$(cat "$TOKEN_FILE")" = "dev-token" ]; then
  echo "Warning: the existing installation uses dev-token. Connect the StickS3 and run:" >&2
  echo "  ./scripts/setup.sh --rotate-token --provision" >&2
fi

cat > "$APP_DIR/run_task_hub.sh" <<'SH'
#!/bin/sh
set -eu

ROOT="$HOME/Library/Application Support/StickS3TaskHub"

# launchd appends forever; cap the log at ~10MB, keeping the recent tail.
LOG="$ROOT/task_hub.log"
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG" 2>/dev/null || echo 0)" -gt 10485760 ]; then
  tail -c 1048576 "$LOG" > "$LOG.prev" 2>/dev/null || true
  : > "$LOG"
fi

TOKEN="$(cat "$ROOT/token" 2>/dev/null || true)"
[ -n "$TOKEN" ] || {
  echo "Task Hub token is missing: $ROOT/token" >&2
  exit 1
}

export TASK_HUB_TOKEN="$TOKEN"
export PATH="$HOME/.local/node/bin:$HOME/.local/node-v22.22.1-darwin-arm64/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -z "${TASK_HUB_WHISPER_MODEL:-}" ] && [ -f "$ROOT/models/ggml-large-v3-turbo-q5_0.bin" ]; then
  export TASK_HUB_WHISPER_MODEL="$ROOT/models/ggml-large-v3-turbo-q5_0.bin"
fi
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
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>
  <key>ProcessType</key>
  <string>Interactive</string>
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

# Post-install check: the launchd-run Host has no Terminal/IDE permissions of
# its own, so adapters that read other apps' data (or data folders that live on
# an external volume) silently get EPERM until the Python binary launchd runs
# is granted Full Disk Access. Surface that here instead of on the Stick.
i=0
while [ $i -lt 20 ]; do
  if curl -fsS -m 2 http://127.0.0.1:5577/health >/dev/null 2>&1; then break; fi
  i=$((i + 1)); sleep 0.5
done
if [ $i -ge 20 ]; then
  echo "Warning: Host did not answer /health within 10s; check $APP_DIR/task_hub.log" >&2
  exit 0
fi
DIAG="$(curl -fsS -m 60 -H "X-Device-Token: $(cat "$TOKEN_FILE")" http://127.0.0.1:5577/diagnostics.json 2>/dev/null || true)"
BLOCKED="$(printf '%s' "$DIAG" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for row in d.get("adapters", []):
    err = str(row.get("error") or "")
    if not row.get("ok") and err:
        print("  - %s: %s" % (row.get("source"), err[:160]))
' 2>/dev/null || true)"
if [ -n "$BLOCKED" ]; then
  HOST_PID="$(launchctl print "gui/$(id -u)/com.sticks3.taskhub" 2>/dev/null | awk '/^[[:space:]]*pid = /{print $3; exit}')"
  HOST_BIN="$( [ -n "$HOST_PID" ] && ps -o comm= -p "$HOST_PID" 2>/dev/null || true)"
  echo ""
  echo "Some adapters could not read their data:"
  echo "$BLOCKED"
  if printf '%s' "$DIAG" | grep -q "Full Disk Access"; then
    echo ""
    echo "Fix: System Settings > Privacy & Security > Full Disk Access > '+', press"
    echo "Cmd+Shift+G and add this binary (the one launchd actually runs):"
    echo "  ${HOST_BIN:-/usr/bin/python3}"
    case "$HOST_BIN" in
      *Python.app/Contents/MacOS/Python)
        echo "  (or its bundle: ${HOST_BIN%/Contents/MacOS/Python})" ;;
    esac
    echo "then restart the Host:  launchctl kickstart -k gui/$(id -u)/com.sticks3.taskhub"
    echo "Open the pane:  open 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles'"
  fi
fi
