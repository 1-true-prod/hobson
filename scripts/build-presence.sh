#!/bin/bash
# build-presence.sh — build build/HobsonPresence.app from presence/.
#
# The camera permission belongs to the app's code identity. An ad-hoc
# signature's identity is its hash, which every rebuild changes, so the
# designated requirement is set to the bundle identifier alone: a rebuild
# keeps the grant. And nothing is rebuilt unless the source changed.
#
#   scripts/build-presence.sh           build if the source changed
#   scripts/build-presence.sh --force   build anyway
#   scripts/build-presence.sh --check   exit 0 if built and current, else 1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/presence"
APP="$ROOT/build/HobsonPresence.app"
BUNDLE_ID="local.hobson.presence"
STAMP_FILE="$APP/Contents/Resources/source.sha256"

stamp="$(cat "$SRC/main.swift" "$SRC/Info.plist" | shasum -a 256 | cut -d' ' -f1)"
current=""
[ -f "$STAMP_FILE" ] && current="$(cat "$STAMP_FILE")"

if [ "${1:-}" = "--check" ]; then
    [ "$current" = "$stamp" ] && [ -x "$APP/Contents/MacOS/HobsonPresence" ]
    exit $?
fi
if [ "${1:-}" != "--force" ] && [ "$current" = "$stamp" ]; then
    echo "HobsonPresence is up to date"
    exit 0
fi

if ! command -v swiftc >/dev/null 2>&1; then
    echo "swiftc not found: install the Xcode Command Line Tools (xcode-select --install)" >&2
    exit 2
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
bundle="$tmp/HobsonPresence.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources"
swiftc -O -swift-version 5 \
    -framework AppKit -framework AVFoundation -framework CoreAudio -framework Vision \
    -o "$bundle/Contents/MacOS/HobsonPresence" "$SRC/main.swift"
cp "$SRC/Info.plist" "$bundle/Contents/Info.plist"
echo "$stamp" > "$bundle/Contents/Resources/source.sha256"
codesign --force --sign - --identifier "$BUNDLE_ID" \
    --requirements "=designated => identifier \"$BUNDLE_ID\"" "$bundle"

mkdir -p "$ROOT/build"
rm -rf "$APP"
mv "$bundle" "$APP"
echo "Built $APP"
