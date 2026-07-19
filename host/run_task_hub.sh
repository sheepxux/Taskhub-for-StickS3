#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN="${TASK_HUB_TOKEN:-}"

if [ -z "$TOKEN" ] && [ -s "$HOME/Library/Application Support/StickS3TaskHub/token" ]; then
  TOKEN="$(cat "$HOME/Library/Application Support/StickS3TaskHub/token")"
elif [ -z "$TOKEN" ] && [ -f "$ROOT/firmware/task_monitor/secrets.h" ]; then
  TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/task_monitor/secrets.h" || true)"
elif [ -z "$TOKEN" ] && [ -f "$ROOT/firmware/voice_recorder/secrets.h" ]; then
  TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/voice_recorder/secrets.h" || true)"
fi
[ -n "$TOKEN" ] || {
  echo "Task Hub token is missing. Run ./scripts/setup.sh first." >&2
  exit 1
}
[ "$TOKEN" != "dev-token" ] || {
  echo "Refusing to start with dev-token. Connect the StickS3 and run ./scripts/setup.sh --rotate-token --provision." >&2
  exit 1
}

export TASK_HUB_TOKEN="$TOKEN"
export PATH="$HOME/.local/node/bin:$HOME/.local/node-v22.22.1-darwin-arm64/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -z "${TASK_HUB_WHISPER_MODEL:-}" ] && [ -f "$ROOT/host/models/ggml-large-v3-turbo-q5_0.bin" ]; then
  export TASK_HUB_WHISPER_MODEL="$ROOT/host/models/ggml-large-v3-turbo-q5_0.bin"
fi
exec python3 -u "$ROOT/host/task_hub.py" \
  --bind "${TASK_HUB_BIND:-0.0.0.0}" \
  --port "${TASK_HUB_PORT:-5577}"
