"""`camoufox fetch` with a parallel downloader for the ~1.3 GB browser archive
(single GitHub connections can crawl at ~40 KB/s). sha256 check and versioned
install stay Camoufox's own. Used by setup.sh and update_engine.py."""
import sys

import camoufox.pkgman as pm

from adm.pdl import download

_orig = pm.webdl


def webdl(url, desc=None, buffer=None, bar=True, progress_callback=None):
    if url.endswith(".zip") and "/releases/download/" in url and "camoufox-" in url:
        print("parallel download:", url.rsplit("/", 1)[-1], flush=True)
        download(url, buffer, parts=20,
                 progress=lambda d, t: print(f"\r  {d >> 20} / {t >> 20} MB", end="", flush=True))
        print(flush=True)
        return buffer
    return _orig(url, desc=desc, buffer=buffer, bar=bar, progress_callback=progress_callback)


pm.webdl = webdl
sys.argv = ["camoufox", "fetch"]
from camoufox.__main__ import cli  # noqa: E402

cli()
