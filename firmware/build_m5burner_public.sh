#!/usr/bin/env bash
# Build the public TaskHub firmware for M5Burner: no secrets.h compiled in.
# A device burned with this boots into the first-run flow (Wi-Fi captive
# portal -> pairing code with the Mac Host). See packaging/m5burner/README.md
# for the publishing steps.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FQBN="esp32:esp32:esp32s3:USBMode=hwcdc,CDCOnBoot=cdc,FlashSize=8M,PartitionScheme=custom,PSRAM=opi,UploadMode=cdc"
SKETCH="$ROOT/firmware/task_monitor"
BUILD_DIR="$ROOT/dist/m5burner/task_monitor_public"
OUT_DIR="$ROOT/dist/m5burner"
VERSION="$(awk -F'"' '/^#define TASKHUB_FW_VERSION/ { print $2; exit }' "$SKETCH/task_monitor.ino")"
[ -n "$VERSION" ] || VERSION="dev"
PATH_MAP_FLAGS="-ffile-prefix-map=$HOME=/build -fmacro-prefix-map=$HOME=/build -fdebug-prefix-map=$HOME=/build"

mkdir -p "$BUILD_DIR" "$OUT_DIR"

arduino-cli compile \
  -b "$FQBN" \
  --clean \
  --export-binaries \
  --output-dir "$BUILD_DIR" \
  --build-property "compiler.cpp.extra_flags=-DTASKHUB_PUBLIC_BUILD=1 $PATH_MAP_FLAGS" \
  --build-property "compiler.c.extra_flags=$PATH_MAP_FLAGS" \
  "$SKETCH"

# M5Burner burns a single image at 0x0: the merged bootloader+partitions+app.
MERGED="$BUILD_DIR/task_monitor.ino.merged.bin"
[ -f "$MERGED" ] || { echo "merged binary missing: $MERGED" >&2; exit 1; }
RELEASE_BIN="$OUT_DIR/TaskHub-StickS3-v$VERSION.bin"
cp "$MERGED" "$RELEASE_BIN"
# Refuse to publish anything that still contains local Wi-Fi/token strings.
if [ -f "$SKETCH/secrets.h" ]; then
  while IFS= read -r secret; do
    [ -n "$secret" ] || continue
    if grep -aqF -- "$secret" "$RELEASE_BIN"; then
      echo "Refusing to publish: local secret from secrets.h found in the binary." >&2
      exit 1
    fi
  done < <(awk -F'"' '/#define[[:space:]]+(WIFI_SSID|WIFI_PASSWORD|DEVICE_TOKEN)[[:space:]]+"/ { if (length($2) > 3) print $2 }' "$SKETCH/secrets.h")
fi
shasum -a 256 "$RELEASE_BIN" | tee "$RELEASE_BIN.sha256"

echo
echo "M5Burner firmware: $RELEASE_BIN"
echo "Publish: M5Burner > USER CUSTOM > Publish, device StickS3, upload that .bin"
echo "         (listing text: packaging/m5burner/README.md)"
echo "First boot: Wi-Fi portal 'TaskHub-XXXX' -> pairing code -> Mac Host asks for it."
