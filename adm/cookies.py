"""Cookie import/export for a stopped profile.

Formats:
  json      — Cookie-Editor / EditThisCookie array (what Octo, Dolphin, AdsPower
              and most extensions read and write). Also accepts {"cookies": [...]}
              and Playwright-style objects.
  netscape  — cookies.txt (curl / yt-dlp / "Get cookies.txt").

The profile is opened headless with its own fingerprint and a black-hole proxy:
we only touch the cookie store, so no request may leave the machine — neither
via the real IP nor via the profile's proxy.
"""
import asyncio
import json
import time
import warnings

from . import store

warnings.filterwarnings("ignore", message="Passing your own fingerprint is not recommended")
# Offline session through a black-hole proxy: geo spoofing is irrelevant here.
warnings.filterwarnings("ignore", message="When using a proxy, it is heavily recommended")

# Session cookies die when the browser closes, and import runs in its own
# headless session — they would be gone before the user opens the profile.
SESSION_TTL = 30 * 86400

BLACKHOLE_PROXY = {"server": "socks5://127.0.0.1:9"}  # discard port, nothing listens

_SAMESITE_IN = {
    "no_restriction": "None", "none": "None", "unspecified": "None",  # Firefox treats unset as None
    "lax": "Lax", "strict": "Strict",
}
_SAMESITE_OUT = {"None": "no_restriction", "Lax": "lax", "Strict": "strict"}


class CookieFormatError(ValueError):
    pass


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Return (playwright_cookies, skipped_reasons)."""
    text = text.strip().lstrip("\ufeff")
    if not text:
        raise CookieFormatError("пусто")
    if text[0] in "[{":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CookieFormatError(f"битый JSON: {e}") from None
        if isinstance(data, dict):
            data = data.get("cookies", [data])
        if not isinstance(data, list):
            raise CookieFormatError("ожидался массив cookies")
        return _from_json(data)
    if "\t" not in text:
        raise CookieFormatError("формат не распознан: нужен JSON-массив cookies или cookies.txt")
    return _from_netscape(text)


def _from_json(items: list) -> tuple[list[dict], list[str]]:
    out, skipped = [], []
    now = time.time()
    for i, c in enumerate(items):
        if not isinstance(c, dict) or not c.get("name") or "value" not in c:
            skipped.append(f"#{i}: нет name/value")
            continue
        domain = c.get("domain") or ""
        if not domain:
            skipped.append(f"{c['name']}: нет domain")
            continue
        host_only = c.get("hostOnly")
        if host_only is True:
            domain = domain.lstrip(".")
        elif host_only is False and not domain.startswith("."):
            domain = "." + domain
        expires = c.get("expirationDate", c.get("expires", c.get("expiry")))
        if c.get("session") or expires in (None, "", -1, 0):
            expires = now + SESSION_TTL
        else:
            expires = float(expires)
            if expires > 1e11:  # milliseconds
                expires /= 1000
            if expires < now:
                skipped.append(f"{c['name']} ({domain}): истёк")
                continue
        same = _SAMESITE_IN.get(str(c.get("sameSite") or "unspecified").lower(), "None")
        secure = bool(c.get("secure"))
        if same == "None" and not secure and str(c.get("sameSite") or "").lower() in ("no_restriction", "none"):
            same = "Lax"  # SameSite=None without Secure is rejected by the browser
        out.append({
            "name": str(c["name"]),
            "value": str(c["value"]),
            "domain": domain,
            "path": c.get("path") or "/",
            "expires": expires,
            "httpOnly": bool(c.get("httpOnly")),
            "secure": secure,
            "sameSite": same,
        })
    return out, skipped


def _from_netscape(text: str) -> tuple[list[dict], list[str]]:
    out, skipped = [], []
    now = time.time()
    for n, line in enumerate(text.splitlines(), 1):
        http_only = False
        if line.startswith("#HttpOnly_"):
            line, http_only = line[len("#HttpOnly_"):], True
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            raise CookieFormatError(f"строка {n}: не cookies.txt (ожидалось 7 полей через TAB)")
        domain, sub, path, secure, expiry, name, value = parts
        domain = ("." + domain.lstrip(".")) if sub.upper() == "TRUE" else domain.lstrip(".")
        exp = float(expiry or 0)
        if exp and exp < now:
            skipped.append(f"{name} ({domain}): истёк")
            continue
        out.append({
            "name": name, "value": value, "domain": domain, "path": path or "/",
            "expires": exp if exp else now + SESSION_TTL, "httpOnly": http_only,
            "secure": secure.upper() == "TRUE", "sameSite": "None" if secure.upper() == "TRUE" else "Lax",
        })
    return out, skipped


def to_json(cookies: list[dict]) -> str:
    rows = []
    for i, c in enumerate(cookies, 1):
        session = c.get("expires", -1) in (-1, None)
        row = {
            "domain": c["domain"],
            "hostOnly": not c["domain"].startswith("."),
            "httpOnly": c.get("httpOnly", False),
            "name": c["name"],
            "path": c.get("path", "/"),
            "sameSite": _SAMESITE_OUT.get(c.get("sameSite"), "unspecified"),
            "secure": c.get("secure", False),
            "session": session,
            "storeId": "0",
            "value": c["value"],
            "id": i,
        }
        if not session:
            row["expirationDate"] = c["expires"]
        rows.append(row)
    return json.dumps(rows, ensure_ascii=False, indent=2)


def to_netscape(cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        d = c["domain"]
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        exp = c.get("expires", -1)
        lines.append("\t".join([
            prefix + d, "TRUE" if d.startswith(".") else "FALSE", c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE", str(int(exp)) if exp and exp > 0 else "0",
            c["name"], c["value"],
        ]))
    return "\n".join(lines) + "\n"


def _is_running(pid: str) -> bool:
    # Firefox holds this lock while the profile is open.
    lock = store.profile_dir(pid) / ".parentlock"
    try:
        import fcntl
        with open(lock, "a") as f:
            fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.lockf(f, fcntl.LOCK_UN)
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return True


async def _with_context(ref: str, fn):
    from camoufox.async_api import AsyncCamoufox
    db = store.connect()
    p = store.get(db, ref)
    if not p:
        raise SystemExit(f"профиль не найден: {ref}")
    if _is_running(p["id"]):
        raise SystemExit("профиль открыт — закройте браузер и повторите")
    from .launcher import SESSION_PREFS
    # Same session prefs as a real launch so session cookies are restored (and
    # saved back), but tabs stay unloaded: nothing navigates, saved tabs survive.
    prefs = {**SESSION_PREFS, "browser.sessionstore.restore_on_demand": True,
             "browser.sessionstore.restore_tabs_lazily": True}
    async with AsyncCamoufox(
        firefox_user_prefs=prefs,
        os=p["os"], fingerprint=p["fingerprint"], persistent_context=True,
        user_data_dir=str(store.profile_dir(p["id"])), headless=True,
        proxy=BLACKHOLE_PROXY, locale=p["locale"] or "en-US",
        i_know_what_im_doing=True,  # no geoip on purpose: offline cookie-store session
    ) as ctx:
        # Session restore runs async after startup. Closing before it finishes
        # makes Firefox save an EMPTY session: tabs and session cookies lost.
        await asyncio.sleep(3)
        return await fn(ctx)


async def export_cookies(ref: str) -> list[dict]:
    return await _with_context(ref, lambda ctx: ctx.cookies())


async def import_cookies(ref: str, cookies: list[dict], replace: bool = False) -> dict:
    async def op(ctx):
        if replace:
            await ctx.clear_cookies()
        # add one by one so a single bad cookie doesn't abort the batch
        ok, failed = 0, []
        for c in cookies:
            try:
                await ctx.add_cookies([c])
                ok += 1
            except Exception as e:
                failed.append(f"{c['name']} ({c['domain']}): {str(e).splitlines()[0][:120]}")
        total = len(await ctx.cookies())
        return {"imported": ok, "failed": failed, "total_in_profile": total}
    return await _with_context(ref, op)
