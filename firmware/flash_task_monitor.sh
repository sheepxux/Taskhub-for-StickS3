#!/bin/bash
# Build + flash the StickS3 AI task monitor firmware.
#
# Usage: ./firmware/flash_task_monitor.sh [compile|upload|monitor|all]
set -euo pipefail
cd "$(dirname "$0")/.."

FQBN="esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=8M,PartitionScheme=custom,PSRAM=opi"
SKETCH="firmware/task_monitor"
PORT="$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)"

case "${1:-all}" in
  compile)
    arduino-cli compile -b "$FQBN" "$SKETCH"
    ;;
  upload)
    [ -z "$PORT" ] && { echo "no /dev/cu.usbmodem* port found"; exit 1; }
    arduino-cli upload -b "$FQBN" -p "$PORT" "$SKETCH"
    ;;
  monitor)
    [ -z "$PORT" ] && { echo "no /dev/cu.usbmodem* port found"; exit 1; }
    arduino-cli monitor -p "$PORT" -c baudrate=115200
    ;;
  all)
    arduino-cli compile -b "$FQBN" "$SKETCH"
    [ -z "$PORT" ] && { echo "no /dev/cu.usbmodem* port found"; exit 1; }
    echo "flashing to $PORT ..."
    arduino-cli upload -b "$FQBN" -p "$PORT" "$SKETCH"
    ;;
  *)
    echo "usage: $0 [compile|upload|monitor|all]"
    exit 1
    ;;
esac
