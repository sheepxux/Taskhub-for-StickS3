#!/usr/bin/env bash
# Build a public TaskHub firmware binary set without compiling local secrets.h.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FQBN="esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=8M,PartitionScheme=custom,PSRAM=opi"
SKETCH="$ROOT/firmware/task_monitor"
OUT_DIR="$ROOT/dist/m5burner/task_monitor_public"
PATH_MAP_FLAGS="-ffile-prefix-map=$HOME=/build -fmacro-prefix-map=$HOME=/build -fdebug-prefix-map=$HOME=/build"

mkdir -p "$OUT_DIR"

arduino-cli compile \
  -b "$FQBN" \
  --clean \
  --export-binaries \
  --output-dir "$OUT_DIR" \
  --build-property "compiler.cpp.extra_flags=-DTASKHUB_PUBLIC_BUILD=1 $PATH_MAP_FLAGS" \
  --build-property "compiler.c.extra_flags=$PATH_MAP_FLAGS" \
  "$SKETCH"

echo
echo "Public M5Burner firmware artifacts:"
find "$OUT_DIR" -maxdepth 1 -type f \( -name '*.bin' -o -name '*.elf' \) -print | sort
echo
echo "This build ignores firmware/task_monitor/secrets.h. Users must run:"
echo "  ./scripts/provision_sticks3.sh"
