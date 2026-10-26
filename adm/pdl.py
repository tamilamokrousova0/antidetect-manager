"""Parallel ranged download. GitHub release assets come at ~40 KB/s per
connection from this network (1.3 GB browser = hours); 16-24 ranges in
parallel get ~8 MB/s."""
import concurrent.futures as cf
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.request


def _resolve(url: str) -> str:
    # signed CDN redirect URLs expire, so this is re-run per attempt
    return urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30).geturl()


def download(url: str, dest_fileobj, parts: int = 16, progress=None) -> int:
    final = _resolve(url)
    size = int(urllib.request.urlopen(urllib.request.Request(final, method="HEAD"), timeout=30)
               .headers["Content-Length"])
    step = size // parts + 1
    tmp = tempfile.mkdtemp(prefix="adm-pdl-")
    done = [0] * parts

    def get(i):
        start, end = i * step, min(size, (i + 1) * step) - 1
        part = os.path.join(tmp, f"p{i}")
        need = end - start + 1
        for _ in range(60):
            have = os.path.getsize(part) if os.path.exists(part) else 0
            if have >= need:
                done[i] = need
                return
            with open(part, "ab") as f:
                subprocess.run(["curl", "-sSL", "--speed-limit", "2000", "--speed-time", "45",
                                "-r", f"{start + have}-{end}", _resolve(url)],
                               stdout=f, stderr=subprocess.DEVNULL)
            done[i] = os.path.getsize(part)
            if progress:
                progress(sum(done), size)
        raise RuntimeError(f"part {i} failed after retries")

    # curl writes each part as it goes; report the real byte count every second
    # instead of only when a 65 MB part completes (the UI shows this).
    stop = threading.Event()

    def monitor():
        while not stop.wait(1):
            have = sum(os.path.getsize(f) for f in (os.path.join(tmp, f"p{i}") for i in range(parts)) if os.path.exists(f))
            if progress:
                progress(min(have, size), size)

    mon = threading.Thread(target=monitor, daemon=True)
    try:
        if progress:
            mon.start()
        with cf.ThreadPoolExecutor(parts) as ex:
            list(ex.map(get, range(parts)))
        stop.set()
        for i in range(parts):
            with open(os.path.join(tmp, f"p{i}"), "rb") as f:
                shutil.copyfileobj(f, dest_fileobj)
        return size
    finally:
        stop.set()
        shutil.rmtree(tmp, ignore_errors=True)
