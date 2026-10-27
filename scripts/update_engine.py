"""Update Camoufox (python package + its paired browser build) with rollback.

  ./update-engine.sh            check and update if a newer STABLE release exists
  ./update-engine.sh --check    only report
  ./update-engine.sh --beta     follow pre-releases (also with --check)
  ./update-engine.sh --to 0.5.8 install an exact version (manual rollback)

Each camoufox package release pins the browser build it was tested with
(browser-pin.json), so the package is upgraded first and then its paired
build is fetched. Old browser builds stay installed for rollback.
Profiles must be closed: the running browser binary is being switched.
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv/bin/python"


def pkg_version() -> str:
    return subprocess.check_output([PY, "-c", "import importlib.metadata as m; print(m.version('camoufox'))"],
                                   text=True).strip()


def latest_stable(beta: bool = False) -> str:
    data = json.load(urllib.request.urlopen("https://pypi.org/pypi/camoufox/json", timeout=30))
    if beta:
        from adm.engine import _vkey
        return max((v for v, f in data["releases"].items() if f and not all(x.get("yanked") for x in f)), key=_vkey)
    return data["info"]["version"]  # PyPI "version" is the latest non-prerelease


def active_browser() -> str | None:
    cfg = Path.home() / "Library/Caches/camoufox/config.json"
    return json.loads(cfg.read_text()).get("active_version") if cfg.exists() else None


def set_active_browser(rel: str):
    cfg = Path.home() / "Library/Caches/camoufox/config.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data["active_version"] = rel
    cfg.write_text(json.dumps(data, indent=2))


STATE = Path.home() / ".local/var/antidetect-manager/state.json"


def write_state():
    """Read by the launchd watcher, which can't exec anything in ~/Documents (TCC)."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(STATE.read_text()) if STATE.exists() else {}
    data.update({"camoufox": pkg_version(), "browser": active_browser(), "project": str(ROOT)})
    STATE.write_text(json.dumps(data, indent=2))


def profiles_running() -> bool:
    out = subprocess.run(["pgrep", "-f", "Camoufox.app/Contents/MacOS"], capture_output=True, text=True).stdout
    return bool(out.strip())


def pip_install(spec: str):
    subprocess.check_call(["uv", "pip", "install", "--python", str(PY), "--prerelease=allow", spec])


SMOKE = r'''
import asyncio, warnings
warnings.filterwarnings("ignore")
from camoufox.async_api import AsyncCamoufox
async def main():
    async with AsyncCamoufox(os="macos", headless=True) as b:
        p = await b.new_page()
        print("UA:", await p.evaluate("navigator.userAgent"))
asyncio.run(main())
'''


def run_py(code: str):
    subprocess.check_call([PY, "-c", code], cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--beta", action="store_true", help="follow pre-releases (Camoufox ships detection fixes there first)")
    ap.add_argument("--to", help="install this exact package version (also for manual rollback)")
    args = ap.parse_args()

    cur, new, browser = pkg_version(), args.to or latest_stable(args.beta), active_browser()
    write_state()
    print(f"installed: camoufox {cur}, browser {browser}")
    print(f"{'target' if args.to else 'latest stable'}: camoufox {new}")
    if cur == new:
        print("up to date")
        return
    if args.check:
        print("update available — run without --check")
        return
    if profiles_running():
        sys.exit("close all profiles first (a Camoufox browser is running)")

    print(f"\n== upgrading package {cur} -> {new}")
    try:
        pip_install(f"camoufox[geoip]=={new}")
        print("\n== fetching paired browser build")
        subprocess.check_call([PY, str(ROOT / "scripts/fetch_browser.py")], cwd=ROOT,
                              env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})
        print("\n== smoke test")
        run_py(SMOKE)
    except Exception as e:
        print(f"\n!! update failed ({e}); rolling back to camoufox {cur}, browser {browser}")
        pip_install(f"camoufox[geoip]=={cur}")
        if browser:
            set_active_browser(browser)
        run_py(SMOKE)
        sys.exit("rolled back")
    write_state()
    (ROOT / "requirements.txt").write_text(f"camoufox[geoip]=={pkg_version()}\n")  # what setup.sh installs
    print(f"\nupdated: camoufox {pkg_version()}, browser {active_browser()}")
    print(f"previous browser kept for rollback: {browser}")


if __name__ == "__main__":
    main()
