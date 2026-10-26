# Antidetect Manager

Менеджер браузерных профилей для macOS (Apple Silicon) поверх [Camoufox](https://github.com/daijro/camoufox) — Firefox с подменой отпечатка на уровне C++.
У каждого профиля свой закреплённый отпечаток, свой прокси (SOCKS5 с логином или HTTP), свои куки и вкладки.

## Установка на Mac

Нужен Mac на Apple Silicon (M1 и новее).

1. Скачать `Antidetect-Manager-<версия>-arm64.dmg` из [Releases](https://github.com/tamilamokrousova0/antidetect-manager/releases), открыть и перетащить приложение в «Программы».
2. Снять карантин (приложение не подписано Apple Developer ID, иначе macOS скажет «повреждено» или «не удаётся проверить разработчика»):
   ```sh
   xattr -dr com.apple.quarantine "/Applications/Antidetect Manager.app"
   ```
   Альтернатива без терминала: запустить, в «Системные настройки → Конфиденциальность и безопасность» нажать «Открыть всё равно».
3. При первом запуске приложение предложит скачать движок браузера (Camoufox, ~1.3 ГБ) в `~/Library/Caches/camoufox` — один раз.

Приложение самодостаточно: внутри свой Python и все зависимости, папка проекта, Homebrew и uv не нужны.
Данные профилей: `~/Library/Application Support/antidetect-manager/` (`ADM_DATA_DIR` переопределяет).

### Из исходников (для разработки)

```sh
git clone https://github.com/tamilamokrousova0/antidetect-manager ~/Documents/Claude/antidetect-manager
cd ~/Documents/Claude/antidetect-manager
./setup.sh
```
`setup.sh` ставит `uv` и `node` (если их нет), Python-окружение с Camoufox, сам браузер, собирает приложение в `/Applications` и добавляет его в Dock.

Сборка дистрибутива: `cd ui && npm run bundle && npm run dist:dmg` → `ui/dist/*.dmg` и `*.zip` (бандл Python + пакеты собирает `ui/build/make-bundle.sh`, подпись ad-hoc в `ui/build/afterpack.js`).

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
| `ui/` | Electron 45: `main.js` вызывает `adm --json` (в .app — `Contents/Resources/adm/run` со встроенным Python, в dev — `adm.sh`), каждый открытый профиль — отдельный процесс; экран первого запуска качает движок (`adm engine status/fetch`) |
| `scripts/` | обновление движка, загрузка браузера, сторож обновлений, `live_check.py` |
| `tests/` | `test_forwarder.py`, `test_cookies.py` (без браузера), `e2e_launch.py`, `socks_server.py` (тестовый SOCKS5 с логином) |

## Что закреплено за профилем

- **Отпечаток** генерируется один раз (статистика реальных устройств) и хранится в профиле: платформа, экран, GPU, шрифты, медиа-устройства, голоса, аудио — одинаковые от запуска к запуску. Версия Firefox в UA всегда равна версии установленного движка.
- **Язык** — основной официальный язык страны прокси (DE→de-DE, BE→nl-BE), меняется только при смене страны; можно задать вручную.
- **По IP прокси при каждом запуске**: таймзона, геолокация, IP в WebRTC.
- **Сессия**: вкладки и сессионные cookies восстанавливаются между запусками, как в обычном браузере. Закрытие последнего окна браузера (⌘W / красная кнопка) = закрытие профиля: без этого Firefox на macOS остаётся жить без окон, профиль висит «запущен», а клик по Dock открывает пустое окно. Так как закрытому окну нечего восстанавливать, адреса вкладок запоминаются в `last_tabs.json` профиля и открываются при следующем запуске.
- **Кнопка «+» (новая вкладка)**: Camoufox прячет её в самый правый угол панели навигации. Вернуть в полосу вкладок нельзя — эта сборка выбрасывает любой виджет из TabsToolbar обратно в nav-bar (проверено через CustomizableUI в chrome-контексте: и `new-tab-button`, и `downloads-button`, и закладочная панель), поэтому лаунчер один раз на профиль ставит «+» и список вкладок сразу после адресной строки (`browser.uiCustomization.state` в prefs.js, маркер `.newtab-fixed`; дальнейшая ручная настройка панели не трогается). ⌘T работает как обычно.
- **Имя профиля в окне**: заголовок окна начинается с имени профиля («FB Anna — Facebook»). Делает крошечное расширение на профиль (`badge-addon/` в папке профиля, `windows.update({titlePreface})`), страница этого не видит; заголовок показывается нативной полосой над вкладками (`browser.tabs.inTitlebar = 0`). userChrome.css Camoufox не грузит.
- **Пароли**: штатный менеджер паролей Firefox включён — при первом входе (Facebook, Google, …) браузер предлагает сохранить логин, в следующий раз подставляет его в форму; хранится зашифрованно в папке профиля (`logins.db` + `key4.db`), список — `about:logins`. Автоматического нажатия «Войти» нет намеренно: автологин скриптом на FB/Google — верный способ словить чекпоинт.

## Решения и ловушки

- Canvas меняется от сессии к сессии — это штатная защита Firefox (`privacy.baselineFingerprintingProtection`). Не выключать: без неё у всех профилей на одном Mac одинаковый canvas железа.
- На macOS-хосте выбирать отпечаток macOS — тогда шрифты настоящие системные. Лаунчер передаёт Camoufox allow-list из реальных семейств хоста (`NSFontManager`) + каталог Camoufox: без этого Camoufox показывает только «базу» macOS и прячет настоящие шрифты (Herculanum, SignPainter, Kefa III, Marion), отличаясь от обычного Firefox на той же машине.
- Pixelscan «Masking detected» на macOS 27: их эталонный список шрифтов Mac требует Rockwell, который macOS 27 резолвит по имени, но не перечисляет (скрыт из enumeration, копия в `~/Library/Fonts` не помогает). Обычный Firefox 158 на том же Mac получает тот же вердикт; остальные 11 признаков их проверки (UA/заголовки, WebGL, платформа, локаль, железо, canvas) у профиля чистые. С прокси карточки «Timezone spoofed» / «Proxy detected» зелёные.
- Проверка прокси читает ровно один HTTP-ответ (по Content-Length / chunked), а не до EOF: часть прокси игнорирует `Connection: close` и держит туннель, из-за чего рабочий прокси раньше считался мёртвым (таймаут 30 с).
- GPU у mac-профилей «Apple M1, or similar» — так настоящий Firefox показывает любой Apple Silicon.
- Импорт/экспорт cookies — только у закрытого профиля. Сессионные cookies при импорте получают срок 30 дней. Фоновая сессия ждёт ~3 с восстановления сессии: если закрыть раньше, Firefox сохранит пустую сессию.
- Прокси не прошёл проверку → профиль не запускается (иначе сайт увидел бы реальный IP).
- Сетевой отпечаток выхода (TCP/IP: ОС, MTU) — свойство прокси, браузером не исправляется.
- launchd не может запускать файлы из `~/Documents` (TCC) — поэтому сторож обновлений копируется в `~/.local/var/`.
- Camoufox выключает менеджер паролей не только prefs, но и корпоративной политикой `PasswordManagerEnabled: false` в `Camoufox.app/Contents/Resources/distribution/policies.json` (пref становится locked). `adm engine patch` убирает `PasswordManagerEnabled` / `OfferToSaveLogins` / `DisableFormHistory` (вызывается после `engine fetch` и перед каждым запуском; оригинал — `policies.json.orig`). Бандл Camoufox только linker-signed, без печати ресурсов, поэтому правка не ломает запуск.
- .app без Developer ID: подпись ad-hoc (`codesign --sign -`), иначе Apple Silicon не запускает изменённый бандл; Gatekeeper у скачавшего всё равно требует снять карантин. Python внутри — python-build-standalone из uv (переносимый), пакеты через `pip --target`, без venv (venv не переносится).

## Проверка профиля на сайтах

```sh
PYTHONPATH=. .venv/bin/python scripts/live_check.py <профиль> [папка]
```
BrowserLeaks (IP / WebRTC / DNS), CreepJS, Pixelscan, SannyBot — скриншоты и текст в папку (по умолчанию `/tmp/adm-live-check`). Откроется окно браузера.
