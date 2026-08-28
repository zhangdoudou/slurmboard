#!/bin/bash
#
# Build a double-clickable Slurmboard.app bundle from the SwiftPM executable.
#
# Usage:
#   ./build_app.sh            # release build -> ./Slurmboard.app
#   ./build_app.sh --debug    # debug build
#
set -euo pipefail

cd "$(dirname "$0")"

CONFIG="release"
if [[ "${1:-}" == "--debug" ]]; then
    CONFIG="debug"
fi
BUILD_SCRATCH_PATH="${TMPDIR:-/tmp/}slurmboard-swiftpm-build"

echo "==> Building ($CONFIG)..."
swift build -c "$CONFIG" --scratch-path "$BUILD_SCRATCH_PATH"

BIN_DIR="$(swift build -c "$CONFIG" --scratch-path "$BUILD_SCRATCH_PATH" --show-bin-path)"
APP="Slurmboard.app"
CONTENTS="$APP/Contents"

echo "==> Assembling $APP..."
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
cp "$BIN_DIR/SlurmboardApp" "$CONTENTS/MacOS/SlurmboardApp"
cp Info.plist "$CONTENTS/Info.plist"
cp slurmboard.py "$CONTENTS/Resources/slurmboard.py"

# Ad-hoc code signature so Gatekeeper lets a locally-built app run.
echo "==> Ad-hoc signing..."
codesign --force --deep --sign - "$APP"

echo "==> Done: $PWD/$APP"
echo "    Double-click it in Finder, or run: open $APP"
