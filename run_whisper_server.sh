#!/bin/bash
# Resident whisper.cpp inference server. Keeps the model in memory so
# transcribe.py (with WHISPER_SERVER_URL set) doesn't reload it per clip.
# Defaults to large-v3; override with WHISPER_MODEL.
set -euo pipefail
cd "$(dirname "$0")"
MODEL="${WHISPER_MODEL:-./models/ggml-large-v3.bin}"
HOST="${WHISPER_HOST:-127.0.0.1}"
PORT="${WHISPER_PORT:-8080}"
exec whisper-server -m "$MODEL" --host "$HOST" --port "$PORT" -l auto -t 4
