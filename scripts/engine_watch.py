#!/usr/bin/python3
"""Weekly Camoufox update watcher (launchd). Stdlib only, system python.

Installed copy lives in ~/.local/var/antidetect-manager/ — launchd jobs can't
exec anything under ~/Documents (TCC), so this never touches the project; it
reads state.json written by update-engine.sh.

Notifies once per new stable camoufox release: claw-send (Telegram) if present,
otherwise a macOS notification.
  engine_watch.py            normal run
  engine_watch.py --dry-run  print the message instead of sending
"""
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

HOME = Path.home()
STATE = HOME / ".local/var/antidetect-manager/state.json"
CLAW = HOME / ".local/bin/claw-send"


def get_json(url):
    return json.load(urllib.request.urlopen(url, timeout=30))


def vtuple(v):
    return tuple(int(x) if x.isdigit() else 0 for x in v.replace("b", ".").split("."))


def main():
    dry = "--dry-run" in sys.argv
    state = json.loads(STATE.read_text())
    installed = state.get("camoufox", "0")
    latest = get_json("https://pypi.org/pypi/camoufox/json")["info"]["version"]
    firefox = get_json("https://product-details.mozilla.org/1.0/firefox_versions.json")["LATEST_FIREFOX_VERSION"]
    browser = (state.get("browser") or "").rsplit("/", 1)[-1].split("-", 1)[0]  # 156.0.1-beta.36-xxxx -> 156.0.1

    print(f"camoufox installed {installed}, latest stable {latest}; engine Firefox {browser}, stock Firefox {firefox}")
    if vtuple(latest) <= vtuple(installed) or state.get("notified") == latest:
        return

    msg = (f"Antidetect Manager: вышла новая стабильная версия Camoufox {latest} (стоит {installed}, "
           f"движок Firefox {browser}, обычный Firefox уже {firefox}).\n"
           f"Закрой профили и запусти: {state.get('project', '~/…/antidetect-manager')}/update-engine.sh")
    if dry:
        print("WOULD SEND:", msg)
        return
    if CLAW.exists():
        subprocess.run([str(CLAW), msg], check=True, timeout=180)
    else:  # another Mac without the Telegram bridge: plain macOS notification
        subprocess.run(["osascript", "-e", f"display notification {json.dumps(msg)} with title \"Antidetect Manager\""], check=True)
    state["notified"] = latest
    STATE.write_text(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
