"""Browser engine = Camoufox build + GeoIP database + default addons, all in
~/Library/Caches/camoufox (shared by the packaged app and the dev checkout).
The packaged .app carries only the Python side; the engine (~1.3 GB) is
fetched here on first run."""
import importlib.metadata as md
import json
import sys


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
