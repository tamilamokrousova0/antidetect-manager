#!/bin/sh
# Установка Antidetect Manager на Mac (Apple Silicon).
# Ставит: Python-окружение + Camoufox (браузер ~1.3 ГБ), Electron UI, приложение в /Applications и Dock.
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

[ "$(uname -m)" = "arm64" ] || { echo "Нужен Mac на Apple Silicon (arm64)"; exit 1; }
command -v brew >/dev/null || { echo "Нужен Homebrew: https://brew.sh"; exit 1; }
for pkg in uv node; do command -v $pkg >/dev/null || brew install $pkg; done

echo "== Python + Camoufox"
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt

echo "== Браузер Camoufox (многопоточная загрузка)"
PYTHONPATH="$ROOT" .venv/bin/python scripts/fetch_browser.py

echo "== Проверка ядра"
PYTHONPATH="$ROOT" .venv/bin/python tests/test_forwarder.py
PYTHONPATH="$ROOT" .venv/bin/python tests/test_cookies.py
PYTHONPATH="$ROOT" .venv/bin/python tests/test_proxy_geo.py
(cd tests && PYTHONPATH="$ROOT" ../.venv/bin/python test_forwarder_state.py)
./adm.sh list >/dev/null

echo "== Приложение"
(cd ui && npm install --no-audit --no-fund && npm run bundle)
ui/build/install-app.sh

if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Antidetect%20Manager.app"; then
  defaults write com.apple.dock persistent-apps -array-add '<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file:///Applications/Antidetect%20Manager.app/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>'
  killall Dock
fi
echo
echo "Готово: Antidetect Manager в /Applications и в Dock (самодостаточный .app, папку проекта можно переносить)."
