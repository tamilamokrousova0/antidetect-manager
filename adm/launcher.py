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
import threading
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
    # No OS re-authentication (Touch ID / Windows Hello / macOS keychain prompt
    # "wants to access the system password storage") when a password is filled
    # or about:logins is opened. Firefox 146+ keeps the switch in a LOCKED pref
    # that a first-run migration derives -- inverted -- from the two legacy
    # prefs below, so all four are set: new names for migrated profiles, legacy
    # names for profiles whose first run is this launch.
    "signon.management.page.os-auth.locked.enabled": False,
    "extensions.formautofill.creditCards.os-auth.locked.enabled": False,
    "extensions.formautofill.reauth.enabled": True,
    "signon.management.page.os-auth.enabled": True,
    # no "import passwords from another browser" rows / nags in the autocomplete
    "signon.showAutoCompleteImport": "",
    "signon.suggestImportCount": 0,
    # no breach / vulnerable-password lookups (remote checks, extra prompts)
    "signon.management.page.breach-alerts.enabled": False,
    "signon.management.page.vulnerable-passwords.enabled": False,
    # Native title bar above the tabs, so the window title (which the badge
    # addon prefixes with the profile name) is actually visible on macOS.
    "browser.tabs.inTitlebar": 0,
    # Geolocation comes from the proxy geo (Camoufox answers inside the network
    # provider). The OS providers are off: with them Firefox asks macOS /
    # Windows for Location Services and, when the app has no permission,
    # opens System Settings on every site request.
    "geo.provider.use_corelocation": False,
    "geo.provider.ms-windows-location": False,
    "geo.provider.use_geoclue": False,
    "geo.prompt.open_system_prefs": False,
}


# Toolbar layout. Camoufox 156 parked the new-tab "+" and the all-tabs menu at
# the far right of the navigation bar and left two flexible springs there, so
# the right side of the address bar was a row of gaps. Firefox 158 keeps the
# stock layout for a fresh profile ("+" next to the tabs), so new profiles get
# no override at all; profiles with a saved state are rewritten once:
# "+" and all-tabs back into the tab strip, springs / Firefox-account button /
# bookmarks-items out of the nav-bar.
_NAVBAR_DROP = {"new-tab-button", "alltabs-button", "smartwindow-group-tabs-button", "ai-window-toggle",
                "customizableui-special-spring2", "customizableui-special-spring3",
                "fxa-toolbar-menu-button", "personal-bookmarks", "reset-pbm-toolbar-button"}
_TABSTRIP_EXTRAS = ("smartwindow-group-tabs-button", "ai-window-toggle")


def _toolbar_layout(state: dict) -> dict:
    pl = state.setdefault("placements", {})
    nav = pl.get("nav-bar", [])
    extras = [w for w in nav if w in _TABSTRIP_EXTRAS]
    pl["nav-bar"] = [w for w in nav if w not in _NAVBAR_DROP]
    tabs = [w for w in pl.get("TabsToolbar", []) if w not in ("new-tab-button", "alltabs-button") and w not in extras]
    if "tabbrowser-tabs" not in tabs:
        tabs.insert(0, "tabbrowser-tabs")
    i = tabs.index("tabbrowser-tabs") + 1
    pl["TabsToolbar"] = tabs[:i] + ["new-tab-button", "alltabs-button"] + tabs[i:] + extras
    for area in list(pl):
        if area not in ("nav-bar", "TabsToolbar"):
            pl[area] = [w for w in pl[area] if w not in ("new-tab-button", "alltabs-button")]
    return state


def new_tab_button_pref(p: dict) -> dict:
    """Once per profile (marker .toolbar-v2): rewrite the saved toolbar state in
    prefs.js (profile closed). Nothing for a profile without a saved state:
    the Firefox 158 default already has "+" in the tab strip. The marker keeps
    later user customisation (toolbar "Customize") untouched."""
    import re
    d = store.profile_dir(p)
    marker = d / ".toolbar-v2"
    prefs = d / "prefs.js"
    key = "browser.uiCustomization.state"
    if marker.exists():
        return {}
    if prefs.exists():
        text = prefs.read_text()
        m = re.search(r'user_pref\("%s", (".*?")\);' % re.escape(key), text)
        if m:
            try:
                state = _toolbar_layout(json.loads(json.loads(m.group(1))))
                text = text[:m.start(1)] + json.dumps(json.dumps(state)) + text[m.end(1):]
                prefs.write_text(text)
            except (ValueError, KeyError):
                pass
    marker.touch()
    (d / ".newtab-fixed").unlink(missing_ok=True)
    return {}


def write_badge_addon(p: dict, reopen: list[str] | None = None) -> str:
    """Tiny per-profile WebExtension: prefixes every window title with the
    profile name ("FB Anna — Facebook — Camoufox"), so open profiles are told
    apart at a glance (windows.update(titlePreface) is browser chrome only:
    document.title and anything a page can read stay untouched; userChrome.css
    is not an option, Camoufox does not load it). It also reopens the tabs
    remembered when the user closed the last window (reopen.json): tabs.create
    makes real tabs in the one window, whereas Playwright's new_page() opens a
    new WINDOW per tab in Firefox -- five remembered tabs came back as five
    windows, and session restore then kept five windows forever."""
    d = store.profile_dir(p) / "badge-addon"
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 2,
        "name": f"Профиль: {p['name']}",
        "description": "Имя профиля в заголовке окна, восстановление вкладок (antidetect-manager)",
        "version": "1.1",
        "browser_specific_settings": {"gecko": {"id": f"badge-{p['id']}@antidetect-manager"}},
        "permissions": ["tabs"],
        "background": {"scripts": ["background.js"]},
    }
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    # merge_once: a profile that already accumulated several windows from the
    # old reopen path (one window per remembered tab) gets them merged into one
    # on its next launch, a single time, so deliberate multi-window use later
    # is left alone.
    merged = store.profile_dir(p) / ".windows-merged"
    (d / "reopen.json").write_text(json.dumps({"urls": reopen or [], "merge_once": not merged.exists()}))
    merged.touch()
    (d / "background.js").write_text(
        f"const PREFACE = {json.dumps(p['name'] + ' — ', ensure_ascii=False)};\n"
        "const apply = (id) => browser.windows.update(id, { titlePreface: PREFACE }).catch(() => {});\n"
        "const all = () => browser.windows.getAll().then((ws) => ws.forEach((w) => apply(w.id)));\n"
        "all();\n"
        "browser.windows.onCreated.addListener((w) => apply(w.id));\n"
        "// the background may start before the first window exists, and a title\n"
        "// change from the page re-renders without the preface on some builds\n"
        "browser.tabs.onUpdated.addListener((_id, info, tab) => { if (info.title || info.status === 'complete') apply(tab.windowId); });\n"
        "setTimeout(all, 1500); setTimeout(all, 5000);\n"
        "// Reopen remembered tabs as TABS of the first window, only if session restore\n"
        "// brought nothing back (every tab still blank). 3 s: after the launcher has\n"
        "// trimmed the extra blank pages Playwright opens on top of the session.\n"
        "const BLANK = (u) => !u || u === 'about:blank' || u === 'about:newtab' || u === 'about:home';\n"
        "setTimeout(async () => {\n"
        "  let plan = { urls: [], merge_once: false };\n"
        "  try { plan = await (await fetch(browser.runtime.getURL('reopen.json'))).json(); } catch (e) { return; }\n"
        "  try {\n"
        "    if (plan.merge_once) {\n"
        "      const wins = await browser.windows.getAll({ populate: true, windowTypes: ['normal'] });\n"
        "      if (wins.length > 1) {\n"
        "        // tabs.move across windows swaps docshells, which Playwright's Firefox\n"
        "        // does not survive (the moved tabs died); recreate them instead\n"
        "        const target = wins[0];\n"
        "        for (const w of wins.slice(1)) {\n"
        "          for (const t of w.tabs) if (!BLANK(t.url)) await browser.tabs.create({ url: t.url, windowId: target.id, active: false });\n"
        "          await browser.windows.remove(w.id);\n"
        "        }\n"
        "      }\n"
        "    }\n"
        "  } catch (e) {}\n"
        "  try {\n"
        "    const urls = plan.urls || [];\n"
        "    if (!urls.length) return;\n"
        "    const tabs = await browser.tabs.query({});\n"
        "    if (!tabs.length || !tabs.every((t) => BLANK(t.url))) return;\n"
        "    const first = tabs[0];\n"
        "    await browser.tabs.update(first.id, { url: urls[0] });\n"
        "    for (const u of urls.slice(1)) await browser.tabs.create({ url: u, windowId: first.windowId, active: false });\n"
        "  } catch (e) {}\n"
        "}, 3000);\n")
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


# Font families the host really has = what stock Firefox here enumerates.
# For a native identity (macOS profile on a Mac, Windows profile on Windows)
# Camoufox claims only the OS base, so real fonts outside that base are hidden
# and the profile measures differently from stock Firefox on the same machine.
# Pixelscan's mac font check failed on exactly that (plus Rockwell, see README).
_HOST_FONTS_JXA = ('ObjC.import("AppKit"); '
                   'JSON.stringify(ObjC.deepUnwrap($.NSFontManager.sharedFontManager.availableFontFamilies))')
_HOST_FONTS_PS = ('Add-Type -AssemblyName System.Drawing; '
                  '(New-Object System.Drawing.Text.InstalledFontCollection).Families | ForEach-Object { $_.Name }')
_HOST_OS_KEY = {"darwin": "mac", "win32": "win"}  # sys.platform -> Camoufox fonts.json key
_PROFILE_OS_KEY = {"macos": "mac", "windows": "win", "linux": "lin"}


def host_font_families() -> list[str]:
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["osascript", "-l", "JavaScript", "-e", _HOST_FONTS_JXA],
                                 capture_output=True, text=True, timeout=15).stdout
            return [f for f in json.loads(out) if isinstance(f, str)]
        if sys.platform == "win32":
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _HOST_FONTS_PS],
                                 capture_output=True, text=True, timeout=30,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            return [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        pass
    return []


def native_host_fonts(profile_os: str) -> list[str]:
    """Allow-list for Camoufox when the profile's OS is the host's OS: every
    family the host has, plus Camoufox's catalogue for that OS (fonts resolved
    by name that the family list omits). The list only unhides real fonts; a
    name that is not installed renders nothing. [] = leave Camoufox alone."""
    key = _HOST_OS_KEY.get(sys.platform)
    if not key or _PROFILE_OS_KEY.get(profile_os) != key:
        return []
    fams = host_font_families()
    if not fams:
        return []
    import camoufox
    with open(os.path.join(os.path.dirname(camoufox.__file__), "fonts.json"), encoding="utf-8") as f:
        catalog = json.load(f).get(key, [])
    return sorted(set(fams) | set(catalog))


def _stop_requests(closed: asyncio.Event) -> None:
    """How the UI closes a profile. SIGTERM where the event loop supports it
    (not on Windows); and, when launched by the UI (ADM_UI=1, stdin is a
    pipe), the line "stop" on stdin or stdin closing (the UI died) -- which
    works everywhere and leaves no orphaned browser behind."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, closed.set)
        except (NotImplementedError, RuntimeError, ValueError):
            pass
    if os.environ.get("ADM_UI") != "1" or sys.stdin is None or sys.stdin.closed:
        return

    def reader():
        try:
            for line in sys.stdin:
                if line.strip().lower() == "stop":
                    break
        except Exception:
            pass
        loop.call_soon_threadsafe(closed.set)

    threading.Thread(target=reader, daemon=True, name="adm-stdin").start()


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


def _event(d: dict) -> None:
    """One JSON line on stdout for the UI (it also reads {"event": "ready"})."""
    print(json.dumps(d, ensure_ascii=False), flush=True)


_SELF_CHECK_JS = r"""
async () => {
  const out = {
    tz: Intl.DateTimeFormat().resolvedOptions().timeZone, langs: navigator.languages,
    ua: navigator.userAgent, platform: navigator.platform,
    screen: [screen.width, screen.height], dpr: window.devicePixelRatio,
    cores: navigator.hardwareConcurrency, webrtc: [],
    gpu: (() => { try { const g = document.createElement('canvas').getContext('webgl');
      const d = g.getExtension('WEBGL_debug_renderer_info'); return g.getParameter(d.UNMASKED_RENDERER_WEBGL); } catch (e) { return null; } })(),
  };
  // IPv6-only endpoint: a proxied profile must look like an IPv4-only network
  // (Camoufox < 0.5.9 let IPv6 literals and AAAA-only hosts through, #849).
  try {
    const ctl = new AbortController(); setTimeout(() => ctl.abort(), 6000);
    const r = await fetch('https://api6.ipify.org/?format=text', { cache: 'no-store', signal: ctl.signal });
    out.ipv6 = (await r.text()).trim();
  } catch (e) { out.ipv6 = null; out.ipv6_error = String(e && e.name || e); }
  try {
    const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
    pc.createDataChannel('x');
    pc.onicecandidate = (e) => { if (e.candidate) out.webrtc.push(e.candidate.candidate); };
    await pc.setLocalDescription(await pc.createOffer());
    await new Promise((r) => setTimeout(r, 3000));
    pc.close();
  } catch (e) { out.webrtc_error = String(e); }
  return out;
}
"""

def _host_ipv6() -> str | None:
    """This machine's public IPv6 (direct, no proxy) -- the address that must
    never show up inside a proxied profile."""
    import urllib.request
    try:
        with urllib.request.urlopen("https://api6.ipify.org/?format=text", timeout=5) as r:
            return r.read().decode().strip() or None
    except Exception:
        return None


async def _self_check(ctx, db, pid: str, chk: dict, locale: str | None) -> None:
    """What the site will see, checked from inside the running browser:
    WebRTC candidates must carry the proxy IP only, an IPv6-only host must not
    answer with this machine's IPv6, the language must match the profile
    locale, the timezone should match the proxy geo. Stored as
    profile.self_check (with the "passport") and announced to the UI."""
    import ipaddress
    await asyncio.sleep(1.5)
    try:
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        host6_task = asyncio.get_running_loop().run_in_executor(None, _host_ipv6)
        d = await page.evaluate(_SELF_CHECK_JS)
        host6 = await host6_task
    except Exception as e:
        _event({"event": "selfcheck", "ok": False, "level": "warn", "error": f"{type(e).__name__}: {e}"})
        return
    ips = set()
    for cand in d.get("webrtc") or []:
        # "candidate:0 1 UDP 2122 <address> <port> typ host ..." -- the address
        # is an IP or an mDNS name (xxxx.local); only real public IPs matter
        parts = cand.split()
        try:
            ip = ipaddress.ip_address(parts[4])
        except (IndexError, ValueError):
            continue
        if ip.is_global:
            ips.add(str(ip))
    leaks = sorted(ip for ip in ips if ip != chk.get("ip"))
    ipv6 = d.get("ipv6")
    ipv6_leak = bool(ipv6 and host6 and ipv6 == host6)
    if ipv6 and host6 is None:
        # cannot tell whose address it is; through a SOCKS tunnel it is the proxy's
        ipv6_leak = False
    want_lang = (locale or "").split(",")[0].split("-")[0].lower()
    got_lang = ((d.get("langs") or [""])[0] or "").split("-")[0].lower()
    lang_ok = not want_lang or want_lang == got_lang
    tz_ok = not chk.get("timezone") or d.get("tz") == chk.get("timezone")
    level = "bad" if (leaks or ipv6_leak or not lang_ok) else ("warn" if not tz_ok else "ok")
    problems = []
    if leaks:
        problems.append("WebRTC отдаёт не IP прокси: " + ", ".join(leaks))
    if ipv6_leak:
        problems.append(f"IPv6-only сайт видит адрес этого компьютера: {ipv6}")
    if not lang_ok:
        problems.append(f"язык браузера {got_lang or '?'} вместо {want_lang}")
    if not tz_ok:
        problems.append(f"таймзона браузера {d.get('tz')} ≠ {chk.get('timezone')} по гео прокси")
    result = {
        "ok": level != "bad", "level": level, "checked_at": int(time.time()), "problems": problems,
        "webrtc_ips": sorted(ips), "proxy_ip": chk.get("ip"),
        "ipv6": ipv6, "host_ipv6": host6,  # ipv6=None: IPv6-only host unreachable, as on an IPv4-only network
        "passport": {k: d.get(k) for k in ("ua", "platform", "screen", "dpr", "cores", "gpu", "langs", "tz")},
    }
    try:
        store.update(db, pid, self_check=result)
    except Exception:
        pass
    _event({"event": "selfcheck", **{k: result[k] for k in ("ok", "level", "problems")}})


async def launch(ref: str, url: str | None = None, headless: bool = False, on_ready=None):
    db = store.connect()
    p = store.get(db, ref)
    if not p:
        raise SystemExit(f"profile not found: {ref}")
    p = store.sync_dir(db, p)  # folder follows the profile name (closed now, so it can be renamed)

    forwarder = None
    proxy_opt = None
    geoip = None
    if p["proxy"]:
        px = parse_proxy(p["proxy"])
        # Re-check every launch: exit IP may have rotated, and timezone/geo
        # must match the IP the sites will actually see.
        chk = await check_proxy(px)
        if not chk["ok"] and chk.get("proxy_alive") and (p.get("proxy_check") or {}).get("ok"):
            # The tunnel works, only the geo services are down: start with the
            # last good geo instead of blocking every profile on a third party.
            prev = p["proxy_check"]
            chk = {**prev, "stale": True, "stale_reason": chk["error"], "stale_at": int(time.time())}
            print(f"warning: гео-сервисы недоступны, запуск с прошлой проверкой "
                  f"({prev.get('ip')} {prev.get('country')} {prev.get('timezone')}): {chk['stale_reason']}",
                  file=sys.stderr, flush=True)
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
        # Every proxy goes through the local forwarder (SOCKS5 or HTTP CONNECT
        # upstream): the browser never sees credentials, DNS is remote, and the
        # forwarder notices when the proxy stops answering mid-session.
        forwarder = Forwarder(px.upstream(), on_state=lambda st: _event({"event": "proxy", "state": st}))
        port = await forwarder.start()
        proxy_opt = {"server": f"socks5://127.0.0.1:{port}"}
        # Real users of a non-English locale almost always carry English as a
        # secondary language (de-DE,de;q=0.8,en-US;q=0.5,en;q=0.3).
        if locale and "," not in locale and not locale.lower().startswith("en"):
            lang = locale.split("-")[0]
            locale = f"{locale},{lang},en-US,en"  # navigator.languages / Accept-Language as stock Firefox builds them

    opts = dict(
        os=p["os"],
        fingerprint=p["fingerprint"],
        persistent_context=True,
        user_data_dir=str(store.profile_dir(p)),
        headless=headless,
        humanize=False,
        allow_addon_new_tab=True,  # the badge addon reopens remembered tabs with tabs.create
    )
    # Restore previous tabs on start, like a normal browser with "open previous
    # windows and tabs"; this also keeps session cookies (logins without
    # "remember me") across restarts instead of dropping them on close.
    opts["firefox_user_prefs"] = dict(SESSION_PREFS)
    opts["firefox_user_prefs"].update(new_tab_button_pref(p))
    fonts = native_host_fonts(p["os"])
    if fonts:
        opts["fonts"] = fonts
    if proxy_opt:
        opts |= dict(proxy=proxy_opt, geoip=geoip, locale=locale)
        # Without this Firefox resolves DNS locally for SOCKS (DNS leak).
        opts["firefox_user_prefs"]["network.proxy.socks_remote_dns"] = True

    store.update(db, p["id"], last_used_at=int(time.time()))
    last_tabs = store.profile_dir(p) / "last_tabs.json"
    reopen = []
    if last_tabs.exists():
        try:
            reopen = json.loads(last_tabs.read_text())
        except ValueError:
            pass
        last_tabs.unlink(missing_ok=True)
    opts["addons"] = [write_badge_addon(p, reopen)]
    from .engine import apply_patches
    apply_patches()  # engine may have been (re)installed by hand; keeps logins enabled
    try:
        async with AsyncCamoufox(**opts) as ctx:
            # Subscribe before anything can close the context, or wait() hangs forever.
            closed = asyncio.Event()
            ctx.on("close", lambda *_: closed.set())
            # UI "Stop": close the browser cleanly so cookies are flushed and no
            # orphaned Firefox is left behind.
            _stop_requests(closed)
            await _drop_extra_blank_tabs(ctx)
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _close_with_last_window(ctx, closed, last_tabs)
            if url:
                await page.goto(url)
            if on_ready:
                await on_ready(ctx)
            if p["proxy"] and chk.get("ok"):
                asyncio.get_running_loop().create_task(_self_check(ctx, db, p["id"], chk, locale))
            await closed.wait()
    finally:
        if forwarder:
            await forwarder.stop()
