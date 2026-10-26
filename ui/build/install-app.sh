#!/bin/sh
# Rebuild and reinstall «Antidetect Manager.app» into /Applications (arm64, self-contained).
set -e
cd "$(dirname "$0")/.."
osascript -e 'quit app "Antidetect Manager"' 2>/dev/null || true
[ -x bundle/adm ] || build/make-bundle.sh
npm run dist
APP="dist/mac-arm64/Antidetect Manager.app"
rm -rf "/Applications/Antidetect Manager.app"
ditto "$APP" "/Applications/Antidetect Manager.app"
echo "installed: /Applications/Antidetect Manager.app"
