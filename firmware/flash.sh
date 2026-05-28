#!/bin/bash
# Build + flash the StickS3 Step 4 firmware skeleton.
#
# Usage:  ./firmware/flash.sh [compile|upload|monitor]   (default: compile+upload)
#
# Toolchain (already installed in this repo's setup):
#   brew install arduino-cli
#   arduino-cli core install esp32:esp32@3.3.8
#   arduino-cli lib install M5Unified
#
# IMPORTANT — re-flashing after the app is running:
#   The ESP32-S3 native USB-CDC is held by the running app, so esptool's
#   auto-reset into download mode often fails ("No serial data received").
#   To re-flash: hold BtnA (GPIO0/BOOT) while pressing the power/RESET button
#   to enter download mode, THEN run this script.
set -euo pipefail
cd "$(dirname "$0")/.."

# PartitionScheme=custom → esp32 core uses firmware/voice_recorder/partitions.csv
# (single app + enlarged data partition; see that file for rationale).
FQBN="esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=8M,PartitionScheme=custom,PSRAM=opi"
SKETCH="firmware/voice_recorder"
# Data (spiffs/LittleFS) partition for read-back: esptool read_flash 0x390000 0x450000
DATA_OFFSET="0x390000"
DATA_SIZE="0x450000"
PORT="$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)"

case "${1:-all}" in
  compile) arduino-cli compile -b "$FQBN" "$SKETCH" ;;
  upload)  arduino-cli upload  -b "$FQBN" -p "$PORT" "$SKETCH" ;;
  monitor) arduino-cli monitor -p "$PORT" -c baudrate=115200 ;;
  all)
    arduino-cli compile -b "$FQBN" "$SKETCH"
    [ -z "$PORT" ] && { echo "no /dev/cu.usbmodem* port found"; exit 1; }
    echo "flashing to $PORT ..."
    arduino-cli upload -b "$FQBN" -p "$PORT" "$SKETCH"
    ;;
  *) echo "usage: $0 [compile|upload|monitor|all]"; exit 1 ;;
esac
