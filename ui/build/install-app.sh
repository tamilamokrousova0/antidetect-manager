#!/bin/sh
# Пересобрать и переустановить «Antidetect Manager.app» в /Applications (arm64).
set -e
cd "$(dirname "$0")/.."
osascript -e 'quit app "Antidetect Manager"' 2>/dev/null || true
npm run dist
APP="dist/mac-arm64/Antidetect Manager.app"
codesign --force --deep --sign - "$APP"   # ad-hoc: Apple Silicon refuses unsigned modified bundles
rm -rf "/Applications/Antidetect Manager.app"
ditto "$APP" "/Applications/Antidetect Manager.app"
echo "installed: /Applications/Antidetect Manager.app"
