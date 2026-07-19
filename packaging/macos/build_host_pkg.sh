#!/usr/bin/env bash
# Build the macOS Host installer from an explicit, secret-free file allowlist.
set -euo pipefail

export COPYFILE_DISABLE=1
export COPY_EXTENDED_ATTRIBUTES_DISABLE=1

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(awk -F'"' '/^TASK_HUB_VERSION = / { print $2; exit }' "$ROOT/host/taskhub_config.py")"
OUT_DIR="$ROOT/dist/macos"
TMP_BASE="${TMPDIR:-/tmp}"
WORK_DIR="$(mktemp -d "$TMP_BASE/taskhub-host-pkg.XXXXXX")"
PAYLOAD="$WORK_DIR/payload"
SCRIPTS="$WORK_DIR/scripts"
INSTALL_ROOT="$PAYLOAD/Library/Application Support/TaskHubHost"
APP_ROOT="$PAYLOAD/Applications/TaskHub Host.app/Contents"
PKG="$OUT_DIR/TaskHub-Host-${VERSION}.pkg"
STAGED_PKG="$WORK_DIR/TaskHub-Host-${VERSION}.pkg"
trap 'rm -rf "$WORK_DIR"' EXIT

[ "$(uname -s)" = "Darwin" ] || {
  echo "The macOS package must be built on macOS." >&2
  exit 1
}
command -v pkgbuild >/dev/null 2>&1 || {
  echo "pkgbuild is required (install Xcode Command Line Tools)." >&2
  exit 1
}
[ -n "$VERSION" ] || {
  echo "Could not read TASK_HUB_VERSION." >&2
  exit 1
}

mkdir -p "$INSTALL_ROOT/host" "$INSTALL_ROOT/scripts" "$APP_ROOT/MacOS" "$SCRIPTS" "$OUT_DIR"

# Deliberate allowlist: never copy secrets.h, models, logs, caches, or the repo.
cp "$ROOT/host/task_hub.py" "$INSTALL_ROOT/host/task_hub.py"
cp "$ROOT/host/taskhub_config.py" "$INSTALL_ROOT/host/taskhub_config.py"
cp "$ROOT/host/taskhub_voice.py" "$INSTALL_ROOT/host/taskhub_voice.py"
cp "$ROOT/scripts/provision_sticks3.sh" "$INSTALL_ROOT/scripts/provision_sticks3.sh"
cp "$ROOT/packaging/macos/resources/run_task_hub.sh" "$INSTALL_ROOT/run_task_hub.sh"
cp "$ROOT/packaging/macos/resources/taskhub-provision" "$PAYLOAD/usr-local-taskhub-provision"
cp "$ROOT/packaging/macos/resources/Info.plist" "$APP_ROOT/Info.plist"
cp "$ROOT/packaging/macos/resources/taskhub-host-app" "$APP_ROOT/MacOS/TaskHub Host"
cp "$ROOT/packaging/macos/scripts/postinstall" "$SCRIPTS/postinstall"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$APP_ROOT/Info.plist"

mkdir -p "$PAYLOAD/usr/local/bin"
mv "$PAYLOAD/usr-local-taskhub-provision" "$PAYLOAD/usr/local/bin/taskhub-provision"
chmod +x \
  "$INSTALL_ROOT/run_task_hub.sh" \
  "$INSTALL_ROOT/scripts/provision_sticks3.sh" \
  "$PAYLOAD/usr/local/bin/taskhub-provision" \
  "$APP_ROOT/MacOS/TaskHub Host" \
  "$SCRIPTS/postinstall"

# ExFAT-style volumes can materialize resource forks as AppleDouble files.
xattr -cr "$PAYLOAD" "$SCRIPTS" 2>/dev/null || true
find "$PAYLOAD" "$SCRIPTS" -type f \( -name '._*' -o -name '.DS_Store' \) -delete

args=(
  --root "$PAYLOAD"
  --scripts "$SCRIPTS"
  --identifier "com.taskhub.sticks3.host"
  --version "$VERSION"
  --install-location "/"
  --ownership recommended
  --filter '(^|/)\._.*$'
  --filter '(^|/)\.DS_Store$'
)
if [ -n "${TASKHUB_INSTALLER_IDENTITY:-}" ]; then
  args+=(--sign "$TASKHUB_INSTALLER_IDENTITY")
fi

pkgbuild "${args[@]}" "$STAGED_PKG"
cp "$STAGED_PKG" "$PKG"

if [ -n "${TASKHUB_NOTARY_PROFILE:-}" ]; then
  xcrun notarytool submit "$PKG" --keychain-profile "$TASKHUB_NOTARY_PROFILE" --wait
  xcrun stapler staple "$PKG"
fi

echo "Built: $PKG"
if [ -z "${TASKHUB_INSTALLER_IDENTITY:-}" ]; then
  echo "Unsigned development build. Set TASKHUB_INSTALLER_IDENTITY for a public release."
fi
