"""Browser engine = Camoufox build + GeoIP database + default addons, all in
the user cache dir (~/Library/Caches/camoufox on macOS, %LOCALAPPDATA%\\camoufox on
Windows), shared by the packaged app and the dev checkout.
The packaged .app carries only the Python side; the engine (~1.3 GB) is
fetched here on first run."""
import importlib.metadata as md
import json
import os
import sys
from pathlib import Path


def status() -> dict:
    from camoufox.pkgman import INSTALL_DIR, installed_verstr
    browser = None
    try:
        browser = installed_verstr()
    except Exception:
        pass
    geoip = False
    try:
        from camoufox.geolocation import get_mmdb_path
        geoip = get_mmdb_path().exists()
    except Exception:
        pass
    from . import store
    return {"installed": browser is not None, "browser": browser, "geoip": geoip,
            "package": md.version("camoufox"), "cache_dir": str(INSTALL_DIR),
            "data_dir": str(store.DATA_DIR)}


def check(channel: str = "stable") -> dict:
    """Update check: the camoufox package on PyPI (newer package = new app
    release, the package is frozen inside the .app) and whether the installed
    browser build is the one this package pairs with (after an app update the
    paired build may still need fetching). channel="beta" also counts
    pre-releases: Camoufox ships detection fixes there first (twice-monthly
    train; e.g. the hCaptcha Juggler tell was fixed in 0.5.9b1)."""
    import urllib.request
    st = status()
    st["channel"] = channel
    try:
        from camoufox.browser_pin import effective_pin
        from camoufox.multiversion import load_config
        pin = effective_pin(load_config())
        st["pin"] = pin.spec if pin else None
    except Exception:
        st["pin"] = None
    st["browser_paired"] = bool(st["browser"]) and (st["pin"] is None or st["browser"] == st["pin"])
    try:
        data = json.load(urllib.request.urlopen("https://pypi.org/pypi/camoufox/json", timeout=20))
        if channel == "beta":
            versions = [v for v, files in data["releases"].items() if files and not all(f.get("yanked") for f in files)]
            st["pypi_latest"] = max(versions, key=_vkey)
        else:
            st["pypi_latest"] = data["info"]["version"]  # latest non-prerelease
        st["package_outdated"] = _vkey(st["pypi_latest"]) > _vkey(st["package"])
    except Exception as e:
        st["pypi_latest"] = None
        st["package_outdated"] = None
        st["error"] = f"PyPI недоступен: {type(e).__name__}"
    return st


def _vkey(v: str) -> tuple:
    """Sortable version: 0.5.9 > 0.5.9b5 > 0.5.9b1 > 0.5.8."""
    import re
    m = re.match(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?", v)
    if not m:
        return (0,)
    pre = {"a": 0, "b": 1, "rc": 2}.get(m.group(4), 3)  # final release sorts last
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), pre, int(m.group(5) or 0))


def fetch(progress=None) -> None:
    """`camoufox fetch` (browser + GeoIP + addons + fpgen model) with the
    parallel downloader for the browser archive: single GitHub connections
    crawl at ~40 KB/s. progress(dict) gets {"stage": ..., "done", "total"}."""
    import camoufox.pkgman as pm
    from .pdl import download

    emit = progress or (lambda d: None)
    orig = pm.webdl

    def webdl(url, desc=None, buffer=None, bar=True, progress_callback=None):
        if url.endswith(".zip") and "/releases/download/" in url and "camoufox-" in url:
            name = url.rsplit("/", 1)[-1]
            emit({"stage": "browser", "file": name})
            download(url, buffer, parts=20,
                     progress=lambda d, t: emit({"stage": "browser", "file": name, "done": d, "total": t}))
            return buffer
        emit({"stage": "other", "file": desc or url.rsplit("/", 1)[-1]})
        return orig(url, desc=desc, buffer=buffer, bar=bar, progress_callback=progress_callback)

    pm.webdl = webdl
    from camoufox.__main__ import cli
    emit({"stage": "start"})
    # click: standalone_mode=False -> exceptions propagate instead of sys.exit
    cli.main(args=["fetch"], standalone_mode=False)
    try:
        apply_patches()
    except Exception as e:  # a failed patch must not fail the download; launch retries it
        print(f"warning: policies patch skipped: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    emit({"stage": "done", **status()})


def print_progress(as_json: bool):
    last = [None]

    def p(d):
        if as_json:
            print(json.dumps(d, ensure_ascii=False), flush=True)
            return
        if d.get("stage") == "browser" and "done" in d:
            line = f"\r  {d['file']}: {d['done'] >> 20} / {d['total'] >> 20} MB"
            if line != last[0]:
                sys.stdout.write(line); sys.stdout.flush(); last[0] = line
            if d["done"] >= d["total"]:
                print(flush=True)
        elif d.get("stage") in ("other", "browser"):
            print(f"  {d['file']}", flush=True)
        elif d.get("stage") == "done":
            print(f"engine ready: {d.get('browser')}", flush=True)
    return p


# Camoufox ships enterprise policies that lock the password manager off
# (signon.rememberSignons becomes a locked pref, so user prefs cannot turn it
# back on). A profile browser must remember logins like stock Firefox does.
# The bundle is linker-signed only (no resource seal), so editing is safe.
_POLICY_DROP = ("PasswordManagerEnabled", "OfferToSaveLogins", "DisableFormHistory")


def policies_path() -> Path | None:
    try:
        from camoufox.pkgman import camoufox_path
        app = camoufox_path(download_if_missing=False)
    except Exception:
        return None
    # camoufox_path() is the versioned install dir: Camoufox.app on macOS,
    # camoufox.exe + distribution/ at the top level on Windows and Linux.
    for p in (app / "Camoufox.app/Contents/Resources/distribution/policies.json",
              app / "distribution/policies.json"):
        if p.exists():
            return p
    return None


def apply_patches() -> dict:
    """Idempotent; called after fetch and before every launch."""
    p = policies_path()
    if not p:
        return {"patched": False, "reason": "policies.json not found"}
    data = json.loads(p.read_text())
    pol = data.get("policies", {})
    dropped = [k for k in _POLICY_DROP if k in pol]
    if not dropped:
        return {"patched": False, "reason": "already clean"}
    orig = p.with_suffix(".json.orig")
    if not orig.exists():
        orig.write_text(p.read_text())
    for k in dropped:
        del pol[k]
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return {"patched": True, "dropped": dropped, "file": str(p)}
