"""Browser engine = Camoufox build + GeoIP database + default addons, all in
~/Library/Caches/camoufox (shared by the packaged app and the dev checkout).
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
    return {"installed": browser is not None, "browser": browser, "geoip": geoip,
            "package": md.version("camoufox"), "cache_dir": str(INSTALL_DIR)}


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
    apply_patches()
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
    # camoufox_path() is the versioned install dir holding Camoufox.app
    for cand in (app / "Camoufox.app", app, app.parent):
        p = cand / "Contents/Resources/distribution/policies.json"
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
