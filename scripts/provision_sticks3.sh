#!/usr/bin/env bash
# Provision a M5Burner/public TaskHub firmware over USB serial.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Library/Application Support/StickS3TaskHub"
TOKEN_FILE="$APP_DIR/token"

SERIAL_PORT="${TASKHUB_SERIAL_PORT:-}"
WIFI_SSID="${TASKHUB_WIFI_SSID:-}"
WIFI_PASSWORD="${TASKHUB_WIFI_PASSWORD:-}"
DEVICE_ID="${TASKHUB_DEVICE_ID:-sticks3-task-01}"
DEVICE_TOKEN="${TASKHUB_DEVICE_TOKEN:-}"
HOST="${TASKHUB_HOST:-}"
HOST_PORT="${TASKHUB_PORT:-5577}"
SKIP_HOST=0
RESET_ONLY=0
NON_INTERACTIVE=0

usage() {
  cat <<'EOF'
TaskHub StickS3 USB provisioning

Usage:
  ./scripts/provision_sticks3.sh [options]

Common:
  ./scripts/provision_sticks3.sh
  TASKHUB_WIFI_SSID="My WiFi" TASKHUB_WIFI_PASSWORD="secret" ./scripts/provision_sticks3.sh
  ./scripts/provision_sticks3.sh --reset

Options:
  --serial-port VALUE    StickS3 USB serial port, default: first /dev/cu.usbmodem*
  --wifi-ssid VALUE      Wi-Fi SSID to store on the StickS3
  --wifi-password VALUE  Wi-Fi password to store on the StickS3
  --host VALUE           Mac Host LAN IP fallback, default: auto-detect
  --port VALUE           Mac Host HTTP port, default: 5577
  --token VALUE          Shared Host/device token, default: installed Host token
  --device-id VALUE      Device id stored on the StickS3
  --skip-host            Do not install/repair the Mac Host first
  --reset                Clear runtime config on the StickS3 and restart it
  --non-interactive      Do not prompt for missing values
  -h, --help             Show this help

Environment variables:
  TASKHUB_SERIAL_PORT
  TASKHUB_WIFI_SSID
  TASKHUB_WIFI_PASSWORD
  TASKHUB_HOST
  TASKHUB_PORT
  TASKHUB_DEVICE_TOKEN
  TASKHUB_DEVICE_ID
EOF
}

log() {
  printf '[provision] %s\n' "$*"
}

fail() {
  printf '[provision] error: %s\n' "$*" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --serial-port)
      [ "$#" -ge 2 ] || fail "--serial-port requires a value"
      SERIAL_PORT="$2"
      shift
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
    --host)
      [ "$#" -ge 2 ] || fail "--host requires a value"
      HOST="$2"
      shift
      ;;
    --port)
      [ "$#" -ge 2 ] || fail "--port requires a value"
      HOST_PORT="$2"
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
    --skip-host)
      SKIP_HOST=1
      ;;
    --reset)
      RESET_ONLY=1
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

detect_serial_port() {
  local p
  p="$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)"
  [ -n "$p" ] && printf '%s\n' "$p"
}

detect_host_ip() {
  local ip
  for iface in en0 en1 en2; do
    ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
    if [ -n "$ip" ]; then
      printf '%s\n' "$ip"
      return 0
    fi
  done
  python3 - <<'PY' 2>/dev/null || true
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
finally:
    s.close()
PY
}

detect_wifi_ssid() {
  local line
  for iface in en0 en1 en2; do
    line="$(networksetup -getairportnetwork "$iface" 2>/dev/null || true)"
    case "$line" in
      *": "*) printf '%s\n' "${line#*: }"; return 0 ;;
    esac
  done
}

if [ "$SKIP_HOST" -eq 0 ]; then
  "$ROOT/host/install_task_hub.sh"
fi

if [ -z "$SERIAL_PORT" ]; then
  SERIAL_PORT="$(detect_serial_port || true)"
fi
[ -n "$SERIAL_PORT" ] || fail "no StickS3 serial port found; plug it in and retry"
[ -e "$SERIAL_PORT" ] || fail "serial port does not exist: $SERIAL_PORT"

if [ -z "$DEVICE_TOKEN" ] && [ -s "$TOKEN_FILE" ]; then
  DEVICE_TOKEN="$(cat "$TOKEN_FILE")"
fi
[ -n "$DEVICE_TOKEN" ] || fail "missing token; install the Host or pass --token"

if [ -z "$HOST" ]; then
  HOST="$(detect_host_ip | head -1 || true)"
fi
[ -n "$HOST" ] || fail "could not auto-detect Mac LAN IP; pass --host"

if [ "$RESET_ONLY" -eq 0 ]; then
  if [ -z "$WIFI_SSID" ]; then
    WIFI_SSID="$(detect_wifi_ssid | head -1 || true)"
  fi
  if [ -z "$WIFI_SSID" ] && [ "$NON_INTERACTIVE" -eq 0 ]; then
    printf 'Wi-Fi SSID: '
    IFS= read -r WIFI_SSID
  fi
  [ -n "$WIFI_SSID" ] || fail "missing Wi-Fi SSID"

  if [ -z "$WIFI_PASSWORD" ] && [ "$NON_INTERACTIVE" -eq 0 ]; then
    printf 'Wi-Fi password (leave empty for open Wi-Fi): '
    stty -echo 2>/dev/null || true
    IFS= read -r WIFI_PASSWORD
    stty echo 2>/dev/null || true
    printf '\n'
  fi
fi

stty -f "$SERIAL_PORT" 115200 raw -echo -echoe -echok -ixon -ixoff 2>/dev/null || true

export SERIAL_PORT WIFI_SSID WIFI_PASSWORD HOST HOST_PORT DEVICE_ID DEVICE_TOKEN RESET_ONLY
python3 - <<'PY'
import json
import os
import select
import time

port = os.environ["SERIAL_PORT"]
reset_only = os.environ.get("RESET_ONLY") == "1"
if reset_only:
    payload = {"cmd": "taskhub.reset"}
    success_type = "taskhub.reset"
else:
    payload = {
        "cmd": "taskhub.configure",
        "ssid": os.environ["WIFI_SSID"],
        "password": os.environ.get("WIFI_PASSWORD", ""),
        "host": os.environ["HOST"],
        "port": int(os.environ.get("HOST_PORT") or "5577"),
        "device_id": os.environ.get("DEVICE_ID") or "sticks3-task-01",
        "token": os.environ["DEVICE_TOKEN"],
    }
    success_type = "taskhub.configured"

line = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
buf = b""
deadline = time.time() + 20
next_send = 0.0
try:
    while time.time() < deadline:
        now = time.time()
        if now >= next_send:
            os.write(fd, line)
            next_send = now + 0.5
        ready, _, _ = select.select([fd], [], [], 0.25)
        if not ready:
            continue
        try:
            chunk = os.read(fd, 1024)
        except BlockingIOError:
            continue
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            print(text)
            try:
                resp = json.loads(text)
            except json.JSONDecodeError:
                continue
            if resp.get("type") == success_type and resp.get("ok") is True:
                raise SystemExit(0)
            if resp.get("type") == "taskhub.error":
                raise SystemExit(f"device error: {resp.get('message')}")
    raise SystemExit("timed out waiting for StickS3 provisioning response")
finally:
    os.close(fd)
PY

if [ "$RESET_ONLY" -eq 1 ]; then
  log "runtime config reset on $SERIAL_PORT"
else
  log "configured $SERIAL_PORT for Wi-Fi '$WIFI_SSID' and Host $HOST:$HOST_PORT"
  log "the StickS3 will restart and connect to TaskHub"
fi
