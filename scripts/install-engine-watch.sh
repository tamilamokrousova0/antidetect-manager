#!/bin/sh
# Еженедельная (пн 11:00) проверка новой стабильной версии Camoufox с уведомлением в Telegram (claw-send).
# Копия сторожа кладётся в ~/.local/var: launchd не может запускать файлы из ~/Documents (TCC).
set -e
D="$HOME/.local/var/antidetect-manager"
PLIST="$HOME/Library/LaunchAgents/ai.antidetect-manager.engine-watch.plist"
mkdir -p "$D"
cp "$(dirname "$0")/engine_watch.py" "$D/engine_watch.py"
"$(dirname "$0")/../update-engine.sh" --check >/dev/null   # пишет state.json
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ai.antidetect-manager.engine-watch</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>$D/engine_watch.py</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$D/engine-watch.log</string>
  <key>StandardErrorPath</key><string>$D/engine-watch.log</string>
</dict>
</plist>
PL
launchctl bootout "gui/$(id -u)/ai.antidetect-manager.engine-watch" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart "gui/$(id -u)/ai.antidetect-manager.engine-watch"
sleep 5
tail -2 "$D/engine-watch.log"
echo "установлено: $PLIST (удалить: launchctl bootout gui/\$(id -u)/ai.antidetect-manager.engine-watch && rm \"$PLIST\")"
