#!/bin/bash
# Resident whisper.cpp inference server for TaskHub voice mode. Keeps the model
# in memory so each clip transcribes fast (no per-request model reload).
# Defaults to ggml-large-v3-turbo-q5_0 (good Mandarin + English, fast on Apple
# Silicon). Override the model/host/port via env vars.
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${WHISPER_MODEL:-./models/ggml-large-v3-turbo-q5_0.bin}"
HOST="${WHISPER_HOST:-127.0.0.1}"
PORT="${WHISPER_PORT:-8080}"
THREADS="${WHISPER_THREADS:-4}"

if [ ! -f "$MODEL" ]; then
  echo "whisper model not found: $MODEL" >&2
  echo "Download one, e.g.:" >&2
  echo "  curl -L -o ./models/ggml-large-v3-turbo-q5_0.bin \\" >&2
  echo "    https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin" >&2
  exit 1
fi

exec whisper-server -m "$MODEL" --host "$HOST" --port "$PORT" -l auto -t "$THREADS"
