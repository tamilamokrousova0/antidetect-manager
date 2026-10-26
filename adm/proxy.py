"""Proxy string parsing and checking (exit IP + geo) without extra deps."""
import asyncio
import json
import time
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit, unquote

from .socks_forward import Upstream, upstream_connect, encode_target

GEO_HOST = "ip-api.com"
GEO_PATH = "/json/?fields=status,message,query,country,countryCode,regionName,city,lat,lon,timezone,isp,proxy,hosting"


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
        return Upstream(self.host, self.port, self.username, self.password)


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


async def _http_get_via_socks(p: Proxy, host: str, path: str) -> bytes:
    atyp, addr = encode_target(host, 80)
    r, w, _ = await upstream_connect(p.upstream(), atyp, addr)
    try:
        w.write(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        return await asyncio.wait_for(r.read(-1), 20)
    finally:
        w.close()


async def _http_get_via_http(p: Proxy, host: str, path: str) -> bytes:
    import base64
    r, w = await asyncio.wait_for(asyncio.open_connection(p.host, p.port), 20)
    try:
        auth = ""
        if p.username:
            tok = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
            auth = f"Proxy-Authorization: Basic {tok}\r\n"
        w.write(f"GET http://{host}{path} HTTP/1.1\r\nHost: {host}\r\n{auth}Connection: close\r\n\r\n".encode())
        await w.drain()
        return await asyncio.wait_for(r.read(-1), 20)
    finally:
        w.close()


async def check_proxy(p: Proxy) -> dict:
    """Return {ok, ip, country, city, timezone, lat, lon, latency_ms, error}."""
    t0 = time.monotonic()
    try:
        fn = _http_get_via_socks if p.scheme == "socks5" else _http_get_via_http
        raw = await asyncio.wait_for(fn(p, GEO_HOST, GEO_PATH), 30)
        head, _, body = raw.partition(b"\r\n\r\n")
        if b" 200 " not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError(head.split(b"\r\n", 1)[0].decode(errors="replace"))
        if b"transfer-encoding: chunked" in head.lower():
            body = _dechunk(body)
        data = json.loads(body)
        if data.get("status") != "success":
            raise RuntimeError(data.get("message", "geo lookup failed"))
        return {
            "ok": True,
            "ip": data["query"],
            "country": data.get("countryCode"),
            "country_name": data.get("country"),
            "region": data.get("regionName"),
            "city": data.get("city"),
            "timezone": data.get("timezone"),
            "lat": data.get("lat"),
            "lon": data.get("lon"),
            "isp": data.get("isp"),
            "flag_proxy": data.get("proxy"),
            "flag_hosting": data.get("hosting"),
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "checked_at": int(time.time()),
        }
    except Exception as e:
        return {"ok": False, "error": _humanize(e), "raw_error": f"{type(e).__name__}: {e}",
                "checked_at": int(time.time())}


def _humanize(e: Exception) -> str:
    s = str(e)
    if isinstance(e, ConnectionRefusedError):
        return "порт закрыт (прокси не принимает соединения)"
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return "таймаут: прокси не ответил за 30 с"
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
