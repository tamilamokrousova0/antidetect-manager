"""Proxy string parsing and checking (exit IP + geo via several providers)."""
import asyncio
import json
import re
import time
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit, unquote

from .socks_forward import Upstream, upstream_connect, encode_target

@dataclass
class Proxy:
    scheme: str  # socks5 | http
    host: str
    port: int
    username: str | None = None
    password: str | None = None

    def to_url(self, hide_password: bool = False) -> str:
        auth = ""
        if self.username:
            pw = "***" if hide_password else (self.password or "")
            auth = f"{self.username}:{pw}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def upstream(self) -> Upstream:
        return Upstream(self.host, self.port, self.username, self.password, self.scheme)


def parse_proxy(s: str) -> Proxy:
    """Accepts socks5://u:p@h:port, http://..., h:port, h:port:u:p, u:p@h:port."""
    s = s.strip()
    if "://" not in s:
        parts = s.split(":", 3)
        if len(parts) == 4 and parts[1].isdigit():
            h, port, u, p = parts
            return Proxy("socks5", h, int(port), u, p)
        if "@" in s:
            # user:pass@host:port — split on the LAST @ so passwords may contain @
            cred, _, hp = s.rpartition("@")
            u, _, p = cred.partition(":")
            h, _, port = hp.rpartition(":")
            return Proxy("socks5", h, int(port), u, p)
        s = "socks5://" + s
    u = urlsplit(s)
    scheme = {"socks5h": "socks5", "socks": "socks5", "https": "http"}.get(u.scheme, u.scheme)
    if scheme not in ("socks5", "http"):
        raise ValueError(f"unsupported proxy scheme: {u.scheme}")
    if not u.hostname or not u.port:
        raise ValueError(f"bad proxy: {s}")
    return Proxy(scheme, u.hostname, u.port,
                 unquote(u.username) if u.username else None,
                 unquote(u.password) if u.password else None)


async def _read_response(r: asyncio.StreamReader) -> bytes:
    """Read exactly one HTTP response. Some proxies ignore `Connection: close`
    and keep the tunnel open, so reading to EOF would hang until the timeout
    and the (working) proxy would be reported as dead."""
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 20)
    lower = head.lower()
    m = re.search(rb"content-length:\s*(\d+)", lower)
    if m:
        body = await asyncio.wait_for(r.readexactly(int(m.group(1))), 20)
    elif b"transfer-encoding: chunked" in lower:
        body = b""  # raw chunk framing is kept; check_proxy() runs _dechunk()
        while True:
            line = await asyncio.wait_for(r.readuntil(b"\r\n"), 20)
            size = int(line.split(b";")[0], 16)
            body += line + await asyncio.wait_for(r.readexactly(size + 2), 20)
            if size == 0:
                break
    else:
        body = await asyncio.wait_for(r.read(-1), 20)
    return head + body


class ProxyDown(Exception):
    """The proxy itself: unreachable, wrong credentials, closed port."""


class GeoUnavailable(Exception):
    """The proxy works; the geo service behind it does not (down, 429, bad JSON)."""


# ---- geo providers, tried in order. ip-api is HTTP-only and limited to 45
# requests/min per source IP; the others are HTTPS fallbacks so that a geo
# outage never blocks launching every profile. ADM_GEO_PROVIDERS="ipwho.is,..."
# restricts/reorders them (tests).
def _parse_ipapi(d: dict) -> dict:
    if d.get("status") != "success":
        raise GeoUnavailable(d.get("message") or "ip-api: lookup failed")
    return {"ip": d["query"], "country": d.get("countryCode"), "country_name": d.get("country"),
            "region": d.get("regionName"), "city": d.get("city"), "timezone": d.get("timezone"),
            "lat": d.get("lat"), "lon": d.get("lon"), "isp": d.get("isp"),
            "flag_proxy": d.get("proxy"), "flag_hosting": d.get("hosting")}


def _parse_ipwho(d: dict) -> dict:
    if d.get("success") is False:
        raise GeoUnavailable(d.get("message") or "ipwho.is: lookup failed")
    return {"ip": d["ip"], "country": d.get("country_code"), "country_name": d.get("country"),
            "region": d.get("region"), "city": d.get("city"), "timezone": (d.get("timezone") or {}).get("id"),
            "lat": d.get("latitude"), "lon": d.get("longitude"), "isp": (d.get("connection") or {}).get("isp"),
            "flag_proxy": None, "flag_hosting": None}


def _parse_ipinfo(d: dict) -> dict:
    if "error" in d or "ip" not in d:
        raise GeoUnavailable(str(d.get("error") or "ipinfo: lookup failed")[:120])
    lat, _, lon = (d.get("loc") or ",").partition(",")
    return {"ip": d["ip"], "country": d.get("country"), "country_name": None,
            "region": d.get("region"), "city": d.get("city"), "timezone": d.get("timezone"),
            "lat": float(lat) if lat else None, "lon": float(lon) if lon else None, "isp": d.get("org"),
            "flag_proxy": None, "flag_hosting": None}


PROVIDERS = [
    ("ip-api.com", "http",
     "/json/?fields=status,message,query,country,countryCode,regionName,city,lat,lon,timezone,isp,proxy,hosting",
     _parse_ipapi),
    ("ipwho.is", "https", "/", _parse_ipwho),
    ("ipinfo.io", "https", "/json", _parse_ipinfo),
]
PROVIDER_TIMEOUT = 15


def providers() -> list:
    import os
    order = os.environ.get("ADM_GEO_PROVIDERS")
    if not order:
        return PROVIDERS
    by_host = {h: (h, sch, path, parse) for h, sch, path, parse in PROVIDERS}
    out = []
    for h in [x.strip() for x in order.split(",") if x.strip()]:
        # unknown host = a deliberately dead provider (tests): same path/parser as ip-api
        out.append(by_host.get(h, (h, "http", PROVIDERS[0][2], _parse_ipapi)))
    return out


def _classify_socks(e: Exception) -> Exception:
    s = str(e)
    if "connect failed" in s:
        return GeoUnavailable("прокси не смог открыть соединение к сервису")
    return ProxyDown(_humanize(e))


async def _http_get_via_socks(p: Proxy, host: str, path: str, state: dict) -> bytes:
    atyp, addr = encode_target(host, 80)
    try:
        r, w, _ = await asyncio.wait_for(upstream_connect(p.upstream(), atyp, addr), PROVIDER_TIMEOUT)
    except Exception as e:
        raise _classify_socks(e) from e
    state["connected"] = True
    try:
        w.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        return await _read_response(r)
    except Exception as e:
        raise GeoUnavailable(_humanize(e)) from e
    finally:
        w.close()


def _https_get_sync(p: Proxy, host: str, path: str, state: dict) -> bytes:
    """HTTPS through the proxy with PySocks (a camoufox dependency): SOCKS5 or
    HTTP CONNECT, remote DNS, Basic auth."""
    import http.client
    import ssl
    import socks
    s = socks.socksocket()
    s.set_proxy(socks.SOCKS5 if p.scheme == "socks5" else socks.HTTP, p.host, p.port, True, p.username, p.password)
    s.settimeout(PROVIDER_TIMEOUT)
    try:
        s.connect((host, 443))
    except socks.ProxyConnectionError as e:
        raise ProxyDown(_humanize(e)) from e
    except (socks.SOCKS5AuthError, socks.SOCKS4Error) as e:
        raise ProxyDown("неверный логин или пароль") from e
    except socks.HTTPError as e:
        raise (ProxyDown("неверный логин или пароль") if "407" in str(e) else GeoUnavailable(str(e))) from e
    except socks.SOCKS5Error as e:
        raise GeoUnavailable("прокси не смог открыть соединение к сервису") from e
    except socks.ProxyError as e:
        raise ProxyDown(_humanize(e)) from e
    except OSError as e:
        raise ProxyDown(_humanize(e)) from e
    state["connected"] = True
    try:
        ss = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        conn = http.client.HTTPSConnection(host, timeout=PROVIDER_TIMEOUT)
        conn.sock = ss
        conn.request("GET", path, headers={"User-Agent": "curl/8.7.1", "Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        head = f"HTTP/1.1 {resp.status} {resp.reason}\r\n\r\n".encode()
        return head + body
    except Exception as e:
        raise GeoUnavailable(_humanize(e)) from e
    finally:
        s.close()


async def _fetch(p: Proxy, host: str, scheme: str, path: str, state: dict) -> bytes:
    if scheme == "https":
        return await asyncio.to_thread(_https_get_sync, p, host, path, state)
    return await _http_get_via_socks(p, host, path, state)  # upstream_connect speaks both socks5 and http


def _json_body(raw: bytes) -> dict:
    head, _, body = raw.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 200 " not in status:
        raise GeoUnavailable(status.strip() or "пустой ответ")
    if b"transfer-encoding: chunked" in head.lower():
        body = _dechunk(body)
    try:
        return json.loads(body)
    except ValueError as e:
        raise GeoUnavailable("ответ не JSON") from e


async def check_proxy(p: Proxy) -> dict:
    """{ok, ip, country, city, timezone, ..., provider, latency_ms, checked_at}
    or {ok: False, error, proxy_alive}. proxy_alive=True means the tunnel
    works and only the geo services failed -- the launcher may then fall back
    to the last good check instead of refusing to start."""
    t0 = time.monotonic()
    state = {"connected": False}
    errors = []
    for host, scheme, path, parse in providers():
        try:
            raw = await asyncio.wait_for(_fetch(p, host, scheme, path, state), PROVIDER_TIMEOUT + 5)
            geo = parse(_json_body(raw))
            return {"ok": True, **geo, "provider": host,
                    "latency_ms": int((time.monotonic() - t0) * 1000), "checked_at": int(time.time())}
        except ProxyDown as e:
            return {"ok": False, "proxy_alive": False, "error": str(e),
                    "raw_error": f"{type(e.__cause__ or e).__name__}: {e.__cause__ or e}", "checked_at": int(time.time())}
        except asyncio.TimeoutError:
            if not state["connected"]:
                return {"ok": False, "proxy_alive": False, "error": f"таймаут: прокси не ответил за {PROVIDER_TIMEOUT} с",
                        "raw_error": "TimeoutError", "checked_at": int(time.time())}
            errors.append(f"{host}: таймаут")
        except (GeoUnavailable, KeyError, TypeError, ValueError) as e:
            errors.append(f"{host}: {e}")
    return {"ok": False, "proxy_alive": state["connected"],
            "error": ("гео-сервисы недоступны, прокси отвечает" if state["connected"] else "прокси не отвечает")
                     + " (" + "; ".join(errors)[:300] + ")",
            "raw_error": "; ".join(errors), "checked_at": int(time.time())}


def _humanize(e: Exception) -> str:
    s = str(e)
    if isinstance(e, ConnectionRefusedError):
        return "порт закрыт (прокси не принимает соединения)"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)) or "timed out" in s:
        return f"таймаут: нет ответа за {PROVIDER_TIMEOUT} с"
    if isinstance(e, OSError) and ("nodename" in s or "Name or service" in s):
        return "хост прокси не найден (DNS)"
    if "auth failed" in s:
        return "неверный логин или пароль"
    if "rejected auth methods" in s:
        return "прокси не принял способ авторизации"
    if "connect failed" in s:
        return "прокси не смог открыть соединение к сайту"
    if isinstance(e, asyncio.IncompleteReadError) or isinstance(e, ConnectionResetError):
        return "прокси оборвал соединение"
    return f"{type(e).__name__}: {s}"


def _dechunk(b: bytes) -> bytes:
    out = b""
    while b:
        size_line, _, b = b.partition(b"\r\n")
        size = int(size_line.split(b";")[0], 16)
        if size == 0:
            break
        out += b[:size]
        b = b[size + 2:]
    return out


def proxy_dict(p: Proxy) -> dict:
    d = asdict(p)
    return d
