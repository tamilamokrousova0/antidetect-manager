#!/bin/sh
# Обновление движка Camoufox (пакет + парная сборка браузера) с откатом
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/scripts/update_engine.py" "$@"
