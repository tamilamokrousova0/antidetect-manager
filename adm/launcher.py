"""Launch one profile in Camoufox with its pinned fingerprint and proxy.

Blocks until the browser window is closed. The Electron UI will run this as a
subprocess per profile (`adm launch <id>`), so one crash can't take down others.
"""
import asyncio
import signal
import time
import warnings

from camoufox.async_api import AsyncCamoufox

from . import store
from .proxy import parse_proxy, check_proxy
from .socks_forward import Forwarder

# Pinning our own stored fingerprint is the whole point; silence that one warning.
warnings.filterwarnings("ignore", message="Passing your own fingerprint is not recommended")


# Restore previous tabs on start, like a normal browser with "open previous
# windows and tabs". Camoufox sets privacy_level=2 / resume_from_crash=false;
# stock Firefox uses 0 / true, which is what lets session cookies (logins
# without "remember me") survive a restart.
SESSION_PREFS = {
    "browser.startup.page": 3,
    "browser.sessionstore.privacy_level": 0,
    "browser.sessionstore.resume_from_crash": True,
}


def main_locale(country: str) -> str:
    from camoufox.locales import SELECTOR, normalize_locale
    terr = SELECTOR.root.find(f"territory[@type='{country}']")
    pops = terr.findall("languagePopulation") if terr is not None else []
    if not pops:
        return "en-US"

    # Prefer official languages: second-language English often outnumbers each
    # official one (BE would otherwise come out as en-BE).
    def share(el):
        return float(el.get("populationPercent") or 0)

    official = [el for el in pops if (el.get("officialStatus") or "").startswith(("official", "de_facto"))]
    best = max(official or pops, key=share)
    return normalize_locale(f"{best.get('type').replace('_', '-')}-{country}").as_string


async def _drop_extra_blank_tabs(ctx):
    """Playwright opens about:blank on top of the restored session; without
    this every launch adds another blank tab that is then restored forever."""
    await asyncio.sleep(1.5)  # let session restore open its tabs
    blanks = [p for p in ctx.pages if p.url == "about:blank"]
    if len(blanks) < len(ctx.pages):
        for p in blanks:
            await p.close()
    else:
        for p in blanks[1:]:
            await p.close()


async def launch(ref: str, url: str | None = None, headless: bool = False, on_ready=None):
    db = store.connect()
    p = store.get(db, ref)
    if not p:
        raise SystemExit(f"profile not found: {ref}")

    forwarder = None
    proxy_opt = None
    geoip = None
    if p["proxy"]:
        px = parse_proxy(p["proxy"])
        # Re-check every launch: exit IP may have rotated, and timezone/geo
        # must match the IP the sites will actually see.
        chk = await check_proxy(px)
        store.update(db, p["id"], proxy_check=chk)
        if not chk["ok"]:
            raise SystemExit(f"прокси не прошёл проверку, запуск отменён (иначе сайт увидел бы реальный IP): {chk['error']}")
        geoip = chk["ip"]
        locale = p["locale"]
        if not p["locale_manual"] and (not locale or not locale.endswith("-" + (chk["country"] or ""))):
            # Left alone, Camoufox re-draws the language on every launch from
            # speaker stats that include minorities (fr-DE, it-DE...). Pin the
            # country's main language instead; the user can override it.
            locale = main_locale(chk["country"])
            store.update(db, p["id"], locale=locale)
        if px.scheme == "socks5" and px.username:
            forwarder = Forwarder(px.upstream())
            port = await forwarder.start()
            proxy_opt = {"server": f"socks5://127.0.0.1:{port}"}
        elif px.scheme == "socks5":
            proxy_opt = {"server": f"socks5://{px.host}:{px.port}"}
        else:
            proxy_opt = {"server": f"http://{px.host}:{px.port}"}
            if px.username:
                proxy_opt |= {"username": px.username, "password": px.password or ""}

    opts = dict(
        os=p["os"],
        fingerprint=p["fingerprint"],
        persistent_context=True,
        user_data_dir=str(store.profile_dir(p["id"])),
        headless=headless,
        humanize=False,
    )
    # Restore previous tabs on start, like a normal browser with "open previous
    # windows and tabs"; this also keeps session cookies (logins without
    # "remember me") across restarts instead of dropping them on close.
    opts["firefox_user_prefs"] = dict(SESSION_PREFS)
    if proxy_opt:
        opts |= dict(proxy=proxy_opt, geoip=geoip, locale=locale)
        # Without this Firefox resolves DNS locally for SOCKS (DNS leak).
        opts["firefox_user_prefs"]["network.proxy.socks_remote_dns"] = True

    store.update(db, p["id"], last_used_at=int(time.time()))
    try:
        async with AsyncCamoufox(**opts) as ctx:
            # Subscribe before anything can close the context, or wait() hangs forever.
            closed = asyncio.Event()
            ctx.on("close", lambda *_: closed.set())
            # UI "Stop" sends SIGTERM: close the browser cleanly so cookies are
            # flushed and no orphaned Firefox is left behind.
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, closed.set)
            await _drop_extra_blank_tabs(ctx)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            if url:
                await page.goto(url)
            if on_ready:
                await on_ready(ctx)
            await closed.wait()
    finally:
        if forwarder:
            await forwarder.stop()
