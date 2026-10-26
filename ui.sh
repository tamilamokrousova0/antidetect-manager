#!/bin/sh
# Запуск интерфейса менеджера профилей
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR/ui" || exit 1
[ -x node_modules/.bin/electron ] || npm install
exec ./node_modules/.bin/electron .
