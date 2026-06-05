#!/bin/sh
# Install the resident whisper.cpp server as a LaunchAgent so TaskHub voice mode
# always has a warm, in-memory model to transcribe against. Idempotent.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/StickS3TaskHub"
PLIST="$HOME/Library/LaunchAgents/com.sticks3.whisper.plist"
LABEL="com.sticks3.whisper"

WHISPER_BIN="${WHISPER_BIN:-$(command -v whisper-server 2>/dev/null || echo /opt/homebrew/bin/whisper-server)}"
MODEL="${WHISPER_MODEL:-$ROOT/host/models/ggml-large-v3-turbo.bin}"
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
  echo "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin" >&2
  exit 1
fi

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$WHISPER_BIN</string>
    <string>-m</string>
    <string>$MODEL</string>
    <string>--host</string>
    <string>$HOST</string>
    <string>--port</string>
    <string>$PORT</string>
    <string>-l</string>
    <string>auto</string>
    <string>-t</string>
    <string>$THREADS</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
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

echo "whisper-server LaunchAgent installed (model: $MODEL, $HOST:$PORT)."
echo "First start loads the model into memory (~5-15s)."
