#!/bin/bash
# build-setup.sh — build build/HobsonSetup.app, the setup wizard's window.
#
# Only a window: the wizard itself is setup/ui/, served by setup_wizard.py.
# It needs no permissions, so unlike HobsonPresence an ad-hoc signature is all
# it gets. Nothing is rebuilt unless the source changed.
#
#   scripts/build-setup.sh           build if the source changed
#   scripts/build-setup.sh --force   build anyway
#   scripts/build-setup.sh --check   exit 0 if built and current, else 1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/setup"
APP="$ROOT/build/HobsonSetup.app"
STAMP_FILE="$APP/Contents/Resources/source.sha256"

stamp="$(cat "$SRC/main.swift" "$SRC/Info.plist" | shasum -a 256 | cut -d' ' -f1)"
current=""
[ -f "$STAMP_FILE" ] && current="$(cat "$STAMP_FILE")"

if [ "${1:-}" = "--check" ]; then
    [ "$current" = "$stamp" ] && [ -x "$APP/Contents/MacOS/HobsonSetup" ]
    exit $?
fi
if [ "${1:-}" != "--force" ] && [ "$current" = "$stamp" ]; then
    echo "HobsonSetup is up to date"
    exit 0
fi

if ! command -v swiftc >/dev/null 2>&1; then
    echo "swiftc not found: install the Xcode Command Line Tools (xcode-select --install)" >&2
    exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
bundle="$tmp/HobsonSetup.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
swiftc -O -swift-version 5 -framework AppKit -framework WebKit \
    -o "$bundle/Contents/MacOS/HobsonSetup" "$SRC/main.swift"
cp "$SRC/Info.plist" "$bundle/Contents/Info.plist"
echo "$stamp" > "$bundle/Contents/Resources/source.sha256"
codesign --force --sign - "$bundle"

mkdir -p "$ROOT/build"
rm -rf "$APP"
mv "$bundle" "$APP"
echo "Built $APP"
