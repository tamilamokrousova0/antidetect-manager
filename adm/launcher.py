"""Launch one profile in Camoufox with its pinned fingerprint and proxy.

Blocks until the browser window is closed. The Electron UI will run this as a
subprocess per profile (`adm launch <id>`), so one crash can't take down others.
"""
import asyncio
import json
import os
import signal
import subprocess
import sys
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
    # Password manager, as in stock Firefox: Camoufox (and Playwright) turn it
    # off for scraping. Per profile: Firefox offers to save a login on the
    # first sign-in and autofills it next time; encrypted in logins.json +
    # key4.db inside the profile folder; about:logins lists them.
    "signon.rememberSignons": True,
    "signon.autofillForms": True,
    "signon.formlessCapture.enabled": True,
    "signon.generation.enabled": False,  # no "use a generated password" offers
    "browser.formfill.enable": True,  # form history (usernames, search fields)
    # Native title bar above the tabs, so the window title (which the badge
    # addon prefixes with the profile name) is actually visible on macOS.
    "browser.tabs.inTitlebar": 0,
}


# Camoufox's toolbar default puts the new-tab "+" and the all-tabs menu at the
# far right of the navigation bar, where nobody looks for them ("there is no +
# for a new tab"). The tab strip itself cannot take them: this build bounces
# any widget placed in TabsToolbar (or the bookmarks bar) straight back into
# the nav-bar, so the best visible spot is right after the address bar.
_DEFAULT_UI_STATE = {
    "placements": {
        "widget-overflow-fixed-list": [], "unified-extensions-area": [],
        "nav-bar": ["back-button", "forward-button", "stop-reload-button", "customizableui-special-spring1",
                    "vertical-spacer", "urlbar-container", "customizableui-special-spring2", "downloads-button",
                    "fxa-toolbar-menu-button", "reset-pbm-toolbar-button", "unified-extensions-button",
                    "new-tab-button", "customizableui-special-spring3", "alltabs-button", "personal-bookmarks"],
        "TabsToolbar": ["tabbrowser-tabs"], "vertical-tabs": [], "PersonalToolbar": [],
    },
    "seen": ["reset-pbm-toolbar-button", "developer-button"],
    "dirtyAreaCache": ["nav-bar", "TabsToolbar", "vertical-tabs", "PersonalToolbar"],
    "currentVersion": 26, "newElementCount": 3,
}


def _tabs_with_new_tab_button(state: dict) -> dict:
    """new-tab-button (+) and alltabs-button right after the address bar."""
    pl = state.setdefault("placements", {})
    nav = [w for w in pl.get("nav-bar", []) if w not in ("new-tab-button", "alltabs-button")]
    i = nav.index("urlbar-container") + 1 if "urlbar-container" in nav else len(nav)
    pl["nav-bar"] = nav[:i] + ["new-tab-button", "alltabs-button"] + nav[i:]
    for area in list(pl):
        if area != "nav-bar":
            pl[area] = [w for w in pl[area] if w not in ("new-tab-button", "alltabs-button")]
    return state


def new_tab_button_pref(pid: str) -> dict:
    """Once per profile: move the buttons in the saved toolbar state (prefs.js,
    profile closed). A profile without a saved state gets the corrected default
    as a launch pref until Firefox saves one. A marker keeps later user
    customisation (toolbar "Customize") untouched."""
    import re
    d = store.profile_dir(pid)
    marker = d / ".newtab-fixed"
    prefs = d / "prefs.js"
    key = "browser.uiCustomization.state"
    if prefs.exists():
        text = prefs.read_text()
        m = re.search(r'user_pref\("%s", (".*?")\);' % re.escape(key), text)
        if m:
            if not marker.exists():
                try:
                    state = _tabs_with_new_tab_button(json.loads(json.loads(m.group(1))))
                    text = text[:m.start(1)] + json.dumps(json.dumps(state)) + text[m.end(1):]
                    prefs.write_text(text)
                except (ValueError, KeyError):
                    pass
                marker.touch()
            return {}
    if marker.exists():
        return {}
    return {key: json.dumps(_tabs_with_new_tab_button(json.loads(json.dumps(_DEFAULT_UI_STATE))))}


def write_badge_addon(p: dict) -> str:
    """Tiny per-profile WebExtension that prefixes every window title with the
    profile name ("FB Anna — Facebook — Camoufox"), so open profiles are told
    apart at a glance. windows.update(titlePreface) is browser chrome only:
    document.title and anything a page can read stay untouched. userChrome.css
    is not an option: Camoufox does not load legacy profile customizations."""
    d = store.profile_dir(p["id"]) / "badge-addon"
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 2,
        "name": f"Профиль: {p['name']}",
        "description": "Имя профиля в заголовке окна (antidetect-manager)",
        "version": "1.0",
        "browser_specific_settings": {"gecko": {"id": f"badge-{p['id']}@antidetect-manager"}},
        "background": {"scripts": ["background.js"]},
    }
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    (d / "background.js").write_text(
        f"const PREFACE = {json.dumps(p['name'] + ' — ', ensure_ascii=False)};\n"
        "const apply = (id) => browser.windows.update(id, { titlePreface: PREFACE }).catch(() => {});\n"
        "browser.windows.getAll().then((ws) => ws.forEach((w) => apply(w.id)));\n"
        "browser.windows.onCreated.addListener((w) => apply(w.id));\n")
    return str(d)


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


# Font families this Mac really has = what stock Firefox here enumerates.
# For a native (macOS-on-macOS) identity Camoufox claims only the OS base, so
# real fonts outside that base (Herculanum, SignPainter, ...) are hidden and the
# profile measures differently from stock Firefox on the same machine. Pixelscan's
# mac font check failed on exactly that (plus Rockwell/Kefa, see README).
_HOST_FONTS_JXA = ('ObjC.import("AppKit"); '
                   'JSON.stringify(ObjC.deepUnwrap($.NSFontManager.sharedFontManager.availableFontFamilies))')


def host_font_families() -> list[str]:
    if sys.platform != "darwin":
        return []
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", _HOST_FONTS_JXA],
                             capture_output=True, text=True, timeout=15).stdout
        return [f for f in json.loads(out) if isinstance(f, str)]
    except Exception:
        return []


def native_mac_fonts() -> list[str]:
    """Allow-list for Camoufox: every family the host has, plus Camoufox's own
    macOS catalogue (fonts resolved by name that the family list omits). The
    list only unhides real fonts; a name that is not installed renders nothing."""
    fams = host_font_families()
    if not fams:
        return []
    import camoufox
    with open(os.path.join(os.path.dirname(camoufox.__file__), "fonts.json")) as f:
        catalog = json.load(f).get("mac", [])
    return sorted(set(fams) | set(catalog))


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


def _close_with_last_window(ctx, closed: asyncio.Event, last_tabs):
    """Closing the last window (red button / Cmd-W) leaves Firefox running
    without windows on macOS, so the profile would stay "running" forever and
    the next click on the Dock icon opens an empty window. Treat it as closing
    the profile: when no page is left for a moment, finish the launch.
    Firefox then has no window to restore next time, so the tab URLs are
    remembered in last_tabs and reopened on the next launch."""
    loop = asyncio.get_running_loop()
    urls = {}  # page -> last main-frame url

    def on_page_close(page):
        async def check():
            await asyncio.sleep(1.0)  # Cmd-N first opens a page, then closes the old one
            if not ctx.pages:
                tabs = [u for u in urls.values() if u and u != "about:blank"]
                last_tabs.write_text(json.dumps(tabs)) if tabs else None
                closed.set()
        loop.create_task(check())

    def watch(page):
        urls[page] = page.url

        def nav(frame):
            if frame == page.main_frame:
                urls[page] = frame.url
        page.on("framenavigated", nav)
        page.on("close", on_page_close)

    for p in ctx.pages:
        watch(p)
    ctx.on("page", watch)


async def _reopen_last_tabs(ctx, last_tabs):
    """Session restore gives nothing back after the user closed the last window
    (see _close_with_last_window): reopen the remembered tabs instead."""
    if not last_tabs.exists():
        return
    try:
        tabs = json.loads(last_tabs.read_text())
    finally:
        last_tabs.unlink(missing_ok=True)
    if not tabs or any(p.url != "about:blank" for p in ctx.pages):
        return
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    for i, url in enumerate(tabs):
        p = page if i == 0 else await ctx.new_page()
        try:
            await p.goto(url, wait_until="commit", timeout=30000)
        except Exception:
            pass


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
    opts["firefox_user_prefs"].update(new_tab_button_pref(p["id"]))
    if p["os"] == "macos" and sys.platform == "darwin":
        fonts = native_mac_fonts()
        if fonts:
            opts["fonts"] = fonts
    if proxy_opt:
        opts |= dict(proxy=proxy_opt, geoip=geoip, locale=locale)
        # Without this Firefox resolves DNS locally for SOCKS (DNS leak).
        opts["firefox_user_prefs"]["network.proxy.socks_remote_dns"] = True

    store.update(db, p["id"], last_used_at=int(time.time()))
    opts["addons"] = [write_badge_addon(p)]
    from .engine import apply_patches
    apply_patches()  # engine may have been (re)installed by hand; keeps logins enabled
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
            last_tabs = store.profile_dir(p["id"]) / "last_tabs.json"
            await _reopen_last_tabs(ctx, last_tabs)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _close_with_last_window(ctx, closed, last_tabs)
            if url:
                await page.goto(url)
            if on_ready:
                await on_ready(ctx)
            await closed.wait()
    finally:
        if forwarder:
            await forwarder.stop()
