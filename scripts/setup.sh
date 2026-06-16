#!/usr/bin/env bash
# First-run setup helper for TaskHub for StickS3.
#
# Defaults are intentionally conservative:
# - install/repair the macOS Host
# - create/sync firmware/task_monitor/secrets.h
# - do not install Arduino dependencies unless --deps is passed
# - do not compile or upload firmware unless --compile or --upload is passed
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/StickS3TaskHub"
TOKEN_FILE="$APP_DIR/token"
SECRETS="$ROOT/firmware/task_monitor/secrets.h"
SECRETS_EXAMPLE="$ROOT/firmware/task_monitor/secrets.h.example"

DO_HOST=1
DO_FIRMWARE=1
DO_DEPS=0
DO_COMPILE=0
DO_UPLOAD=0
DO_PROVISION=0
NON_INTERACTIVE=0

WIFI_SSID="${TASKHUB_WIFI_SSID:-}"
WIFI_PASSWORD="${TASKHUB_WIFI_PASSWORD:-}"
DEVICE_TOKEN="${TASKHUB_DEVICE_TOKEN:-}"
DEVICE_ID="${TASKHUB_DEVICE_ID:-sticks3-task-01}"
TASKHUB_LANG="${TASKHUB_LANG:-en}"
TASKHUB_VOICE_SEND="${TASKHUB_VOICE_SEND:-1}"

usage() {
  cat <<'EOF'
TaskHub for StickS3 setup

Usage:
  ./scripts/setup.sh [options]

Common:
  ./scripts/setup.sh
  ./scripts/setup.sh --deps --compile
  ./scripts/setup.sh --deps --upload
  TASKHUB_WIFI_SSID="My WiFi" TASKHUB_WIFI_PASSWORD="secret" ./scripts/setup.sh

Options:
  --host-only              Install/repair only the macOS Host
  --firmware-only          Configure/build only the firmware
  --skip-host              Do not install the macOS Host
  --skip-firmware          Do not create or edit firmware secrets
  --deps                   Install Arduino ESP32 core and required libraries
  --compile                Compile the StickS3 firmware after setup
  --upload                 Compile and upload the StickS3 firmware
  --provision              Configure an already-flashed StickS3 over USB
  --wifi-ssid VALUE        Set WIFI_SSID in firmware secrets
  --wifi-password VALUE    Set WIFI_PASSWORD in firmware secrets
  --token VALUE            Set the shared Host/firmware device token
  --device-id VALUE        Set DEVICE_ID in firmware secrets
  --lang VALUE             Set device UI language: en or zh, default: en
  --voice-send VALUE       Send transcript automatically: on/off, default: on
  --non-interactive        Do not prompt for missing Wi-Fi values
  -h, --help               Show this help

Environment variables:
  TASKHUB_WIFI_SSID
  TASKHUB_WIFI_PASSWORD
  TASKHUB_DEVICE_TOKEN
  TASKHUB_DEVICE_ID
  TASKHUB_LANG
  TASKHUB_VOICE_SEND
EOF
}

log() {
  printf '[setup] %s\n' "$*"
}

warn() {
  printf '[setup] warning: %s\n' "$*" >&2
}

fail() {
  printf '[setup] error: %s\n' "$*" >&2
  exit 1
}

have() {
  command -v "$1" >/dev/null 2>&1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --host-only)
      DO_HOST=1
      DO_FIRMWARE=0
      ;;
    --firmware-only)
      DO_HOST=0
      DO_FIRMWARE=1
      ;;
    --skip-host)
      DO_HOST=0
      ;;
    --skip-firmware)
      DO_FIRMWARE=0
      ;;
    --deps)
      DO_DEPS=1
      ;;
    --compile)
      DO_COMPILE=1
      ;;
    --upload)
      DO_UPLOAD=1
      DO_COMPILE=1
      ;;
    --provision)
      DO_PROVISION=1
      ;;
    --wifi-ssid)
      [ "$#" -ge 2 ] || fail "--wifi-ssid requires a value"
      WIFI_SSID="$2"
      shift
      ;;
    --wifi-password)
      [ "$#" -ge 2 ] || fail "--wifi-password requires a value"
      WIFI_PASSWORD="$2"
      shift
      ;;
    --token)
      [ "$#" -ge 2 ] || fail "--token requires a value"
      DEVICE_TOKEN="$2"
      shift
      ;;
    --device-id)
      [ "$#" -ge 2 ] || fail "--device-id requires a value"
      DEVICE_ID="$2"
      shift
      ;;
    --lang)
      [ "$#" -ge 2 ] || fail "--lang requires a value"
      TASKHUB_LANG="$2"
      shift
      ;;
    --voice-send)
      [ "$#" -ge 2 ] || fail "--voice-send requires a value"
      TASKHUB_VOICE_SEND="$2"
      shift
      ;;
    --non-interactive)
      NON_INTERACTIVE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
  shift
done

read_define() {
  local key="$1"
  local file="$2"
  [ -f "$file" ] || return 0
  awk -v key="$key" '
    $1 == "#define" && $2 == key {
      value = $0
      sub("^[^\"]*\"", "", value)
      sub("\".*$", "", value)
      print value
      exit
    }
  ' "$file"
}

set_string_define() {
  local key="$1"
  local value="$2"
  python3 - "$SECRETS" "$key" "$value" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

text = path.read_text()
escaped = value.replace("\\", "\\\\").replace('"', '\\"')
line = f'#define {key:<15} "{escaped}"'
pattern = re.compile(rf'^[ \t]*#define[ \t]+{re.escape(key)}[ \t]+".*"[ \t]*$', re.M)
if pattern.search(text):
    text = pattern.sub(line, text)
else:
    if not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
path.write_text(text)
PY
}

set_raw_define() {
  local key="$1"
  local value="$2"
  python3 - "$SECRETS" "$key" "$value" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]

text = path.read_text()
line = f'#define {key:<15} {value}'
pattern = re.compile(rf'^[ \t]*#define[ \t]+{re.escape(key)}[ \t]+.*$', re.M)
if pattern.search(text):
    text = pattern.sub(line, text)
else:
    if not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
path.write_text(text)
PY
}

normalize_bool_flag() {
  local value
  value="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1|true|yes|on) printf '1\n' ;;
    0|false|no|off) printf '0\n' ;;
    *) return 1 ;;
  esac
}

random_token() {
  LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 24 || true
}

ensure_python() {
  have python3 || fail "python3 is required"
}

ensure_token() {
  if [ -n "$DEVICE_TOKEN" ]; then
    return
  fi

  if [ -s "$TOKEN_FILE" ]; then
    DEVICE_TOKEN="$(cat "$TOKEN_FILE")"
    return
  fi

  if [ -f "$SECRETS" ]; then
    DEVICE_TOKEN="$(read_define DEVICE_TOKEN "$SECRETS" || true)"
  fi

  if [ -z "$DEVICE_TOKEN" ] || [ "$DEVICE_TOKEN" = "dev-token" ]; then
    DEVICE_TOKEN="$(random_token)"
  fi

  if [ -z "$DEVICE_TOKEN" ]; then
    DEVICE_TOKEN="dev-token"
  fi
}

configure_firmware() {
  ensure_python
  [ -f "$SECRETS_EXAMPLE" ] || fail "missing $SECRETS_EXAMPLE"

  if [ ! -f "$SECRETS" ]; then
    cp "$SECRETS_EXAMPLE" "$SECRETS"
    log "created firmware/task_monitor/secrets.h"
  fi

  local current_ssid current_password
  current_ssid="$(read_define WIFI_SSID "$SECRETS" || true)"
  current_password="$(read_define WIFI_PASSWORD "$SECRETS" || true)"

  if [ -z "$WIFI_SSID" ] && [ "$NON_INTERACTIVE" -eq 0 ] && {
    [ -z "$current_ssid" ] || [ "$current_ssid" = "your-wifi-ssid" ]; }; then
    printf 'Wi-Fi SSID: '
    IFS= read -r WIFI_SSID
  fi

  if [ -z "$WIFI_PASSWORD" ] && [ "$NON_INTERACTIVE" -eq 0 ] && {
    [ -z "$current_password" ] || [ "$current_password" = "your-wifi-password" ]; }; then
    printf 'Wi-Fi password: '
    stty -echo 2>/dev/null || true
    IFS= read -r WIFI_PASSWORD
    stty echo 2>/dev/null || true
    printf '\n'
  fi

  [ -n "$DEVICE_TOKEN" ] && set_string_define DEVICE_TOKEN "$DEVICE_TOKEN"
  [ -n "$DEVICE_ID" ] && set_string_define DEVICE_ID "$DEVICE_ID"
  [ -n "$TASKHUB_LANG" ] && set_string_define TASKHUB_LANG "$TASKHUB_LANG"
  [ -n "$TASKHUB_VOICE_SEND" ] && set_raw_define VOICE_AUTO_SEND "$TASKHUB_VOICE_SEND"
  [ -n "$WIFI_SSID" ] && set_string_define WIFI_SSID "$WIFI_SSID"
  [ -n "$WIFI_PASSWORD" ] && set_string_define WIFI_PASSWORD "$WIFI_PASSWORD"

  current_ssid="$(read_define WIFI_SSID "$SECRETS" || true)"
  current_password="$(read_define WIFI_PASSWORD "$SECRETS" || true)"
  if [ -z "$current_ssid" ] || [ "$current_ssid" = "your-wifi-ssid" ]; then
    warn "WIFI_SSID is still a placeholder in firmware/task_monitor/secrets.h"
  fi
  if [ -z "$current_password" ] || [ "$current_password" = "your-wifi-password" ]; then
    warn "WIFI_PASSWORD is still a placeholder in firmware/task_monitor/secrets.h"
  fi

  log "firmware secrets configured"
}

install_host() {
  mkdir -p "$APP_DIR"
  if [ -n "$DEVICE_TOKEN" ]; then
    umask 077
    printf '%s' "$DEVICE_TOKEN" > "$TOKEN_FILE"
  fi
  "$ROOT/host/install_task_hub.sh"
}

install_arduino_deps() {
  if ! have arduino-cli; then
    if have brew; then
      log "installing arduino-cli with Homebrew"
      brew install arduino-cli
    else
      fail "arduino-cli is missing. Install it first, or install Homebrew and rerun with --deps."
    fi
  fi

  log "installing ESP32 Arduino core and required libraries"
  arduino-cli config init >/dev/null 2>&1 || true
  arduino-cli core update-index
  arduino-cli core install esp32:esp32
  arduino-cli lib install M5Unified ArduinoJson
}

check_firmware_tools() {
  have arduino-cli || fail "arduino-cli is required for --compile/--upload. Rerun with --deps or install it manually."
}

print_summary() {
  cat <<EOF

TaskHub setup complete.

Useful checks:
  curl http://127.0.0.1:5577/health
  open http://127.0.0.1:5577/peers

Firmware config:
  firmware/task_monitor/secrets.h

Flash later:
  ./firmware/flash_task_monitor.sh all

EOF
}

if [ "$(uname -s)" != "Darwin" ]; then
  warn "the Host is currently designed for macOS; continuing anyway"
fi

ensure_python
ensure_token
TASKHUB_LANG="$(printf '%s' "$TASKHUB_LANG" | tr '[:upper:]' '[:lower:]')"
case "$TASKHUB_LANG" in
  en|zh|zh-*) ;;
  *) fail "--lang must be en or zh" ;;
esac
TASKHUB_VOICE_SEND="$(normalize_bool_flag "$TASKHUB_VOICE_SEND")" || fail "--voice-send must be on/off or 1/0"

if [ "$DO_FIRMWARE" -eq 1 ]; then
  configure_firmware
fi

if [ "$DO_HOST" -eq 1 ]; then
  install_host
fi

if [ "$DO_DEPS" -eq 1 ]; then
  install_arduino_deps
fi

if [ "$DO_COMPILE" -eq 1 ]; then
  check_firmware_tools
  if [ "$DO_UPLOAD" -eq 1 ]; then
    "$ROOT/firmware/flash_task_monitor.sh" all
  else
    "$ROOT/firmware/flash_task_monitor.sh" compile
  fi
fi

if [ "$DO_PROVISION" -eq 1 ]; then
  args=(--skip-host --device-id "$DEVICE_ID")
  [ "$NON_INTERACTIVE" -eq 1 ] && args+=(--non-interactive)
  [ -n "$WIFI_SSID" ] && args+=(--wifi-ssid "$WIFI_SSID")
  [ -n "$WIFI_PASSWORD" ] && args+=(--wifi-password "$WIFI_PASSWORD")
  [ -n "$DEVICE_TOKEN" ] && args+=(--token "$DEVICE_TOKEN")
  [ -n "$TASKHUB_LANG" ] && args+=(--lang "$TASKHUB_LANG")
  [ -n "$TASKHUB_VOICE_SEND" ] && args+=(--voice-send "$TASKHUB_VOICE_SEND")
  "$ROOT/scripts/provision_sticks3.sh" "${args[@]}"
fi

print_summary
