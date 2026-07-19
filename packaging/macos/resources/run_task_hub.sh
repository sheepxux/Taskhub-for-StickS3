#!/bin/sh
set -eu

USER_ROOT="$HOME/Library/Application Support/StickS3TaskHub"
INSTALL_ROOT="/Library/Application Support/TaskHubHost"
TOKEN="$(cat "$USER_ROOT/token" 2>/dev/null || true)"
[ -n "$TOKEN" ] || {
  echo "Task Hub token is missing: $USER_ROOT/token" >&2
  exit 1
}

export TASK_HUB_TOKEN="$TOKEN"
export PATH="$HOME/.local/node/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [ -z "${TASK_HUB_WHISPER_MODEL:-}" ] && [ -f "$USER_ROOT/models/ggml-large-v3-turbo-q5_0.bin" ]; then
  export TASK_HUB_WHISPER_MODEL="$USER_ROOT/models/ggml-large-v3-turbo-q5_0.bin"
fi

exec /usr/bin/python3 -u "$INSTALL_ROOT/host/task_hub.py" --bind 0.0.0.0 --port 5577
