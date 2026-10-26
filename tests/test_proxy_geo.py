"""Geo check: provider fallback and failure classification, no network."""
import asyncio
from adm import proxy as px

P = px.parse_proxy("socks5://u:p@h:1080")

def run(fetch_impl, providers=None):
    px._fetch, orig = fetch_impl, px._fetch
    try:
        if providers is not None:
            import os; os.environ["ADM_GEO_PROVIDERS"] = providers
        return asyncio.run(px.check_proxy(P))
    finally:
        px._fetch = orig
        import os; os.environ.pop("ADM_GEO_PROVIDERS", None)

def body(obj):
    import json; return b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + json.dumps(obj).encode()

IPAPI = {"status": "success", "query": "1.2.3.4", "countryCode": "DE", "country": "Germany", "city": "Berlin", "timezone": "Europe/Berlin", "lat": 1, "lon": 2, "isp": "X", "proxy": False, "hosting": False}
IPWHO = {"success": True, "ip": "1.2.3.4", "country_code": "DE", "country": "Germany", "city": "Berlin", "timezone": {"id": "Europe/Berlin"}, "latitude": 1, "longitude": 2, "connection": {"isp": "X"}}
IPINFO = {"ip": "1.2.3.4", "country": "DE", "city": "Berlin", "region": "BE", "loc": "1,2", "org": "X", "timezone": "Europe/Berlin"}

# 1. first provider ok
async def f1(p, host, scheme, path, state):
    state["connected"] = True; return body(IPAPI)
r = run(f1); assert r["ok"] and r["provider"] == "ip-api.com" and r["timezone"] == "Europe/Berlin", r

# 2. ip-api 429 -> ipwho.is answers
async def f2(p, host, scheme, path, state):
    state["connected"] = True
    if host == "ip-api.com": return b"HTTP/1.1 429 Too Many Requests\r\n\r\n{}"
    return body(IPWHO)
r = run(f2); assert r["ok"] and r["provider"] == "ipwho.is" and r["country"] == "DE", r

# 3. ip-api garbage, ipwho.is failure JSON, ipinfo ok
async def f3(p, host, scheme, path, state):
    state["connected"] = True
    return {"ip-api.com": b"HTTP/1.1 200 OK\r\n\r\n<html>", "ipwho.is": body({"success": False, "message": "quota"}), "ipinfo.io": body(IPINFO)}[host]
r = run(f3); assert r["ok"] and r["provider"] == "ipinfo.io" and r["lat"] == 1.0, r

# 4. proxy dead -> stop at once, proxy_alive False
calls = []
async def f4(p, host, scheme, path, state):
    calls.append(host); raise px.ProxyDown("неверный логин или пароль")
r = run(f4); assert not r["ok"] and r["proxy_alive"] is False and calls == ["ip-api.com"], (r, calls)

# 5. proxy fine, every geo service down -> proxy_alive True
async def f5(p, host, scheme, path, state):
    state["connected"] = True; raise px.GeoUnavailable("503")
r = run(f5); assert not r["ok"] and r["proxy_alive"] is True and "гео-сервисы недоступны" in r["error"], r

# 6. provider order override
seen = []
async def f6(p, host, scheme, path, state):
    seen.append(host); state["connected"] = True; return body(IPINFO)
r = run(f6, providers="ipinfo.io"); assert seen == ["ipinfo.io"] and r["provider"] == "ipinfo.io", (seen, r)

# 7. silent proxy: first timeout without a connection = proxy down
async def f7(p, host, scheme, path, state):
    await asyncio.sleep(999)
px.PROVIDER_TIMEOUT, saved = 0, px.PROVIDER_TIMEOUT
try:
    r = run(f7); assert not r["ok"] and r["proxy_alive"] is False and "таймаут" in r["error"], r
finally:
    px.PROVIDER_TIMEOUT = saved
print("proxy geo OK")
