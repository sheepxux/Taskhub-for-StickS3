#!/bin/bash
# Daily 22:00 job: summarize the day's voice entries, then notify.
# Wired up via com.sticks3.voice-daily.plist (a user LaunchAgent).
set -euo pipefail
cd "$(dirname "$0")"
PY="./.venv/bin/python"

# Write transcripts/summaries into OpenClaw's indexed memory so `openclaw memory
# search` (and the agent) can find them. Override VOICE_DIR to relocate.
export VOICE_DIR="${VOICE_DIR:-$HOME/.openclaw/workspace/memory/voice}"
OPENCLAW_BIN="${OPENCLAW_BIN:-$HOME/.local/npm/bin/openclaw}"

"$PY" summarize.py
"$PY" notify.py

# Make the day's summary searchable in OpenClaw immediately. Best-effort.
[ -x "$OPENCLAW_BIN" ] && "$OPENCLAW_BIN" memory index || true
