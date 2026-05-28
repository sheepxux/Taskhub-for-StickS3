#!/bin/bash
# Resident transcription worker: drains the upload queue, runs whisper, writes
# markdown into OpenClaw memory, and reindexes. Pairs with the whisper-server
# LaunchAgent. Wired up via com.sticks3.transcribe.plist.
set -euo pipefail
cd "$(dirname "$0")"

# launchd gives a minimal PATH; add node (for the openclaw reindex call) and
# common tool locations so ffmpeg/whisper/openclaw all resolve.
export PATH="$HOME/.local/node/bin:$HOME/.local/npm/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"

export VOICE_DIR="${VOICE_DIR:-$HOME/.openclaw/workspace/memory/voice}"
export WHISPER_SERVER_URL="${WHISPER_SERVER_URL:-http://127.0.0.1:8080}"
export OPENCLAW_BIN="${OPENCLAW_BIN:-$HOME/.local/npm/bin/openclaw}"
exec ./.venv/bin/python transcribe.py
