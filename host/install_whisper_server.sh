#!/bin/sh
# Install the resident whisper.cpp server as a LaunchAgent so TaskHub voice mode
# always has a warm, in-memory model to transcribe against. Idempotent.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/StickS3TaskHub"
PLIST="$HOME/Library/LaunchAgents/com.sticks3.whisper.plist"
LABEL="com.sticks3.whisper"

WHISPER_BIN="${WHISPER_BIN:-$(command -v whisper-server 2>/dev/null || echo /opt/homebrew/bin/whisper-server)}"
MODEL="${WHISPER_MODEL:-$ROOT/host/models/ggml-large-v3-turbo-q5_0.bin}"
HOST="${WHISPER_HOST:-127.0.0.1}"
PORT="${WHISPER_PORT:-8080}"
THREADS="${WHISPER_THREADS:-4}"

mkdir -p "$APP_DIR" "$HOME/Library/LaunchAgents"

if [ ! -x "$WHISPER_BIN" ] && ! command -v whisper-server >/dev/null 2>&1; then
  echo "whisper-server not found. Install whisper.cpp (e.g. 'brew install whisper-cpp')." >&2
  exit 1
fi
if [ ! -f "$MODEL" ]; then
  echo "Whisper model not found: $MODEL" >&2
  echo "Download one, e.g.:" >&2
  echo "  mkdir -p '$ROOT/host/models' && curl -L -o '$MODEL' \\" >&2
  echo "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin" >&2
  exit 1
fi

# Keep the model on the internal disk. Referencing the repo copy breaks when
# the repo lives on an external volume (unmounted at login) or inside a TCC-
# protected folder such as ~/Desktop, which a launchd agent cannot read —
# whisper-server then crash-loops and floods its log.
if [ -L "$APP_DIR/models" ]; then
  rm -f "$APP_DIR/models"
fi
mkdir -p "$APP_DIR/models"
INSTALLED_MODEL="$APP_DIR/models/$(basename "$MODEL")"
if [ "$MODEL" != "$INSTALLED_MODEL" ]; then
  if [ ! -f "$INSTALLED_MODEL" ] || [ "$(stat -f %z "$MODEL")" != "$(stat -f %z "$INSTALLED_MODEL")" ]; then
    echo "Copying model $(basename "$MODEL") to $APP_DIR/models ..."
    cp "$MODEL" "$INSTALLED_MODEL.partial" && mv "$INSTALLED_MODEL.partial" "$INSTALLED_MODEL"
  fi
fi

cat > "$APP_DIR/run_whisper_server_agent.sh" <<SH
#!/bin/sh
set -eu

# launchd appends forever; cap the log at ~10MB, keeping the recent tail.
LOG="$APP_DIR/whisper-server.log"
if [ -f "\$LOG" ] && [ "\$(stat -f %z "\$LOG" 2>/dev/null || echo 0)" -gt 10485760 ]; then
  tail -c 1048576 "\$LOG" > "\$LOG.prev" 2>/dev/null || true
  : > "\$LOG"
fi

MODEL="$INSTALLED_MODEL"
if [ ! -f "\$MODEL" ]; then
  echo "whisper model missing: \$MODEL (rerun host/install_whisper_server.sh)" >&2
  exit 1
fi
exec "$WHISPER_BIN" -m "\$MODEL" --host "$HOST" --port "$PORT" -l auto -t "$THREADS"
SH
chmod +x "$APP_DIR/run_whisper_server_agent.sh"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$APP_DIR/run_whisper_server_agent.sh</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>$APP_DIR/whisper-server.log</string>
  <key>StandardErrorPath</key>
  <string>$APP_DIR/whisper-server.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "whisper-server LaunchAgent installed (model: $INSTALLED_MODEL, $HOST:$PORT)."
echo "First start loads the model into memory (~5-15s)."
