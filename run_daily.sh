#!/bin/bash
# Daily 22:00 job: summarize the day's voice entries, then notify.
# Wired up via com.sticks3.voice-daily.plist (a user LaunchAgent).
set -euo pipefail
cd "$(dirname "$0")"
PY="./.venv/bin/python"
"$PY" summarize.py
"$PY" notify.py
