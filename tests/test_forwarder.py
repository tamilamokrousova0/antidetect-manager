"""Forwarder end-to-end: client -> Forwarder(no auth) -> fake upstream SOCKS5 (user/pass) -> HTTP server."""
import asyncio
from adm.socks_forward import Forwarder, Upstream, _read_addr, encode_target, upstream_connect
from adm.proxy import parse_proxy


async def fake_upstream(r, w, seen):
    ver, n = await r.readexactly(2); methods = await r.readexactly(n)
    assert 2 in methods; w.write(b"\x05\x02"); await w.drain()
    await r.readexactly(1); u = await r.readexactly((await r.readexactly(1))[0])
    p = await r.readexactly((await r.readexactly(1))[0])
    ok = (u, p) == (b"user", b"p@ss")
    w.write(b"\x01" + (b"\x00" if ok else b"\x01")); await w.drain()
    if not ok: w.close(); return
    _, _, _, atyp = await r.readexactly(4); addr = await _read_addr(r, atyp)
    seen.append((atyp, addr))
    host = "127.0.0.1"; port = int.from_bytes(addr[-2:], "big")
    tr, tw = await asyncio.open_connection(host, port)
    w.write(b"\x05\x00\x00\x01" + bytes(4) + b"\x00\x00"); await w.drain()
    async def pipe(a, b):
        while d := await a.read(65536): b.write(d); await b.drain()
        b.close()
    await asyncio.gather(pipe(r, tw), pipe(tr, w))


async def http_handler(r, w):
    await r.readuntil(b"\r\n\r\n")
    w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"); await w.drain(); w.close()


async def main():
    seen = []
    http = await asyncio.start_server(http_handler, "127.0.0.1", 0)
    hport = http.sockets[0].getsockname()[1]
    up = await asyncio.start_server(lambda r, w: fake_upstream(r, w, seen), "127.0.0.1", 0)
    uport = up.sockets[0].getsockname()[1]

    fw = Forwarder(Upstream("127.0.0.1", uport, "user", "p@ss"))
    lport = await fw.start()
    # client speaks plain no-auth socks5 to forwarder, asks for a DOMAIN target
    atyp, addr = encode_target("localhost", hport)
    r, w, _ = await upstream_connect(Upstream("127.0.0.1", lport), atyp, addr)
    w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"); await w.drain()
    body = await r.read(-1)
    assert body.endswith(b"hello"), body
    assert seen[0][0] == 3, "hostname must reach upstream unresolved (remote DNS)"

    # wrong password -> forwarder returns failure to client
    fw2 = Forwarder(Upstream("127.0.0.1", uport, "user", "bad")); p2 = await fw2.start()
    try:
        await upstream_connect(Upstream("127.0.0.1", p2), atyp, addr); raise AssertionError("should fail")
    except Exception as e:
        assert "connect failed" in str(e), e

    for s in ("1.2.3.4:1080:user:p@ss", "socks5://user:p%40ss@1.2.3.4:1080", "user:p@ss@1.2.3.4:1080"):
        px = parse_proxy(s)
        if px: assert (px.host, px.port, px.username, px.password) == ("1.2.3.4", 1080, "user", "p@ss"), (s, px)
    print("forwarder OK")

asyncio.run(main())
