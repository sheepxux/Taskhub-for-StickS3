#!/bin/bash
# TaskHub for StickS3 - one-line Mac installer.
#
#   curl -fsSL https://raw.githubusercontent.com/sheepxux/Taskhub-for-StickS3/main/scripts/install.sh | bash
#
# Installs the Mac Host (LaunchAgent on 127.0.0.1:5577 + LAN), then waits for a
# StickS3 running the TaskHub firmware (burned with M5Burner) to ask for
# pairing, and asks you for the 4-digit code shown on its screen.
#
# Environment overrides:
#   TASKHUB_REPO=owner/name   (default sheepxux/Taskhub-for-StickS3)
#   TASKHUB_REF=main          (branch or tag)
#   TASKHUB_NO_PAIR=1         (install only, skip the pairing prompt)
#   TASKHUB_LANG=zh           (prompt language for pairing)
set -euo pipefail

REPO="${TASKHUB_REPO:-sheepxux/Taskhub-for-StickS3}"
REF="${TASKHUB_REF:-main}"
APP_DIR="$HOME/Library/Application Support/StickS3TaskHub"

say() { printf '%s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "TaskHub Host runs on macOS."

# /usr/bin/python3 is a stub until the Command Line Tools exist; calling it
# would pop a GUI installer and hang this script. Detect that first.
if ! xcode-select -p >/dev/null 2>&1; then
  say "Python 3 (Apple Command Line Tools) is not installed yet."
  say "macOS will now open an installer window. Finish it, then run this command again."
  xcode-select --install >/dev/null 2>&1 || true
  exit 1
fi
/usr/bin/python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null \
  || die "Python 3.8+ is required (found: $(/usr/bin/python3 --version 2>&1))."

# Fetch the repo (git is optional: a tarball works on a fresh Mac).
if [ -n "${TASKHUB_SRC:-}" ] && [ -f "$TASKHUB_SRC/host/install_task_hub.sh" ]; then
  SRC="$TASKHUB_SRC"
else
  TMP="$(mktemp -d "${TMPDIR:-/tmp}/taskhub-install.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT
  say "Downloading TaskHub ($REPO@$REF)..."
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$REF.tar.gz" -o "$TMP/src.tgz" 2>/dev/null \
    || curl -fsSL "https://github.com/$REPO/archive/refs/tags/$REF.tar.gz" -o "$TMP/src.tgz" \
    || die "could not download https://github.com/$REPO ($REF)."
  tar -xzf "$TMP/src.tgz" -C "$TMP"
  SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
  [ -f "$SRC/host/install_task_hub.sh" ] || die "unexpected archive layout."
fi

say "Installing the Mac Host..."
sh "$SRC/host/install_task_hub.sh"
[ -f "$APP_DIR/taskhub_pair.py" ] || die "Host install did not finish (missing $APP_DIR/taskhub_pair.py)."

if [ "${TASKHUB_NO_PAIR:-0}" != "1" ]; then
  say ""
  say "Host is running. Now pair your StickS3:"
  say "  1. Burn 'TaskHub for StickS3' with M5Burner and power the Stick on."
  say "  2. On your phone, join the Wi-Fi 'TaskHub-XXXX' it shows and pick your home Wi-Fi."
  say "  3. When the Stick shows a 4-digit code, type it here."
  say ""
  /usr/bin/python3 "$APP_DIR/taskhub_pair.py" --timeout 900 --lang "${TASKHUB_LANG:-en}" || {
    say ""
    say "Not paired yet. Run this any time to pair:"
    say "  python3 \"$APP_DIR/taskhub_pair.py\""
    exit 0
  }
fi

say ""
say "Done. Health: http://127.0.0.1:5577/health"
say "Pair another StickS3 later:  python3 \"$APP_DIR/taskhub_pair.py\""
