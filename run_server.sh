#!/bin/bash
# Resident OpenClaw skill HTTP server (upload + entries/notifications API).
# Binds 0.0.0.0:5577 so the StickS3 can reach it over the LAN.
# Wired up via com.sticks3.server.plist (a user LaunchAgent).
set -euo pipefail
cd "$(dirname "$0")"
export VR_HOST="${VR_HOST:-0.0.0.0}"
export VR_PORT="${VR_PORT:-5577}"
exec ./.venv/bin/python server.py
