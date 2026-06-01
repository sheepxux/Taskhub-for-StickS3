#!/bin/sh
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOKEN=""

if [ -f "$ROOT/firmware/task_monitor/secrets.h" ]; then
  TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/task_monitor/secrets.h" || true)"
elif [ -f "$ROOT/firmware/voice_recorder/secrets.h" ]; then
  TOKEN="$(awk '/#define[[:space:]]+DEVICE_TOKEN[[:space:]]+"/ { sub(/^.*"/, ""); sub(/".*$/, ""); print; exit }' "$ROOT/firmware/voice_recorder/secrets.h" || true)"
fi

export TASK_HUB_TOKEN="${TOKEN:-dev-token}"
export PATH="$HOME/.local/node/bin:$HOME/.local/node-v22.22.1-darwin-arm64/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
exec python3 -u "$ROOT/host/task_hub.py" \
  --bind "${TASK_HUB_BIND:-0.0.0.0}" \
  --port "${TASK_HUB_PORT:-5577}"
