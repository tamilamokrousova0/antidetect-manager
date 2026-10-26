# Antidetect Manager

Менеджер браузерных профилей для macOS (Apple Silicon) поверх [Camoufox](https://github.com/daijro/camoufox) — Firefox с подменой отпечатка на уровне C++.
У каждого профиля свой закреплённый отпечаток, свой прокси (SOCKS5 с логином или HTTP), свои куки и вкладки.

## Установка на Mac

Нужны Apple Silicon и [Homebrew](https://brew.sh).

```sh
git clone <этот репозиторий> ~/Documents/Claude/antidetect-manager
cd ~/Documents/Claude/antidetect-manager
./setup.sh
```

`setup.sh` ставит `uv` и `node` (если их нет), Python-окружение с Camoufox, сам браузер (~1.3 ГБ, многопоточная загрузка), собирает приложение **Antidetect Manager** в `/Applications` и добавляет его в Dock.
Папку проекта после установки не переносить: путь к ней зашит в приложение (после переноса — `ui/build/install-app.sh`).
При первом запуске macOS может спросить доступ к папке «Документы» — разрешить.

Данные профилей: `~/Library/Application Support/antidetect-manager/` (`ADM_DATA_DIR` переопределяет).

## Использование

Приложение: список профилей, поиск/фильтр по тегам, создание пачкой, проверка прокси, запуск/закрытие, карточка профиля (прокси, теги, заметки, язык, cookies).
Горячие клавиши: ⌘N — новый профиль, ⌘F — поиск, Esc — закрыть карточку.

То же из терминала (`--json` у всех команд):
```sh
./adm.sh create acc --count 20 --proxy 'host:port:user:pass' --tags fb,de
./adm.sh list [--tag fb]
./adm.sh check all                       # перепроверить прокси
./adm.sh check-proxy 'socks5://u:p@h:port'
./adm.sh set acc-001 --proxy '...' --locale en-US   # --proxy '' убрать, --locale '' авто
./adm.sh launch acc-001 [--url https://...]
./adm.sh cookies-import acc-001 cookies.json [--replace]   # JSON или cookies.txt, '-' = stdin
./adm.sh cookies-export acc-001 [--format netscape] [-o file]
./adm.sh delete acc-001
```
Форматы прокси: `host:port:user:pass`, `socks5://user:pass@host:port`, `user:pass@host:port`, `http://user:pass@host:port`.

## Обновление движка

```sh
./update-engine.sh --check     # есть ли новая стабильная версия
./update-engine.sh             # обновить (профили закрыть)
./update-engine.sh --to 0.5.8  # поставить конкретную версию / откатиться
```
Берутся только стабильные релизы с PyPI; пакет обновляется вместе с «парной» сборкой браузера, после — пробный запуск, при сбое — автооткат. Старые сборки остаются в `~/Library/Caches/camoufox/browsers/`.

Еженедельная проверка с уведомлением (пн 11:00; Telegram через `claw-send`, если он есть, иначе уведомление macOS):
```sh
scripts/install-engine-watch.sh
```

## Как устроено

| | |
|---|---|
| `adm/store.py` | SQLite + папка на профиль |
| `adm/proxy.py` | парсинг прокси, проверка exit IP / гео (ip-api.com) |
| `adm/socks_forward.py` | локальный форвардер: Firefox не умеет SOCKS5 с логином → 127.0.0.1 без авторизации → прокси с авторизацией; DNS на стороне прокси |
| `adm/launcher.py` | запуск: проверка прокси (без неё не стартует), geoip → таймзона/геолокация/WebRTC-IP, язык, восстановление сессии |
| `adm/cookies.py` | импорт/экспорт cookies в фоновой сессии без сети |
| `adm/pdl.py` | многопоточная загрузка больших файлов с GitHub |
| `adm/cli.py` | CLI |
| `ui/` | Electron 45: `main.js` вызывает `adm.sh --json`, каждый открытый профиль — отдельный процесс |
| `scripts/` | обновление движка, загрузка браузера, сторож обновлений, `live_check.py` |
| `tests/` | `test_forwarder.py`, `test_cookies.py` (без браузера), `e2e_launch.py`, `socks_server.py` (тестовый SOCKS5 с логином) |

## Что закреплено за профилем

- **Отпечаток** генерируется один раз (статистика реальных устройств) и хранится в профиле: платформа, экран, GPU, шрифты, медиа-устройства, голоса, аудио — одинаковые от запуска к запуску. Версия Firefox в UA всегда равна версии установленного движка.
- **Язык** — основной официальный язык страны прокси (DE→de-DE, BE→nl-BE), меняется только при смене страны; можно задать вручную.
- **По IP прокси при каждом запуске**: таймзона, геолокация, IP в WebRTC.
- **Сессия**: вкладки и сессионные cookies восстанавливаются между запусками, как в обычном браузере.

## Решения и ловушки

- Canvas меняется от сессии к сессии — это штатная защита Firefox (`privacy.baselineFingerprintingProtection`). Не выключать: без неё у всех профилей на одном Mac одинаковый canvas железа.
- На macOS-хосте выбирать отпечаток macOS — тогда шрифты настоящие системные.
- GPU у mac-профилей «Apple M1, or similar» — так настоящий Firefox показывает любой Apple Silicon.
- Импорт/экспорт cookies — только у закрытого профиля. Сессионные cookies при импорте получают срок 30 дней. Фоновая сессия ждёт ~3 с восстановления сессии: если закрыть раньше, Firefox сохранит пустую сессию.
- Прокси не прошёл проверку → профиль не запускается (иначе сайт увидел бы реальный IP).
- Сетевой отпечаток выхода (TCP/IP: ОС, MTU) — свойство прокси, браузером не исправляется.
- launchd не может запускать файлы из `~/Documents` (TCC) — поэтому сторож обновлений копируется в `~/.local/var/`.

## Проверка профиля на сайтах

```sh
PYTHONPATH=. .venv/bin/python scripts/live_check.py <профиль> [папка]
```
BrowserLeaks (IP / WebRTC / DNS), CreepJS, Pixelscan, SannyBot — скриншоты и текст в папку (по умолчанию `/tmp/adm-live-check`). Откроется окно браузера.
