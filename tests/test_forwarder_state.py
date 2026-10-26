"""Forwarder: HTTP CONNECT upstream and the down/up state callback (no browser)."""
import asyncio, base64
from adm.socks_forward import Forwarder, Upstream, _read_addr, decode_target
from test_forwarder import fake_upstream  # authenticated fake SOCKS5 upstream -> local target

async def http_proxy(r, w, seen):
    """Minimal HTTP CONNECT proxy requiring user:p@ss."""
    head = await r.readuntil(b"\r\n\r\n")
    line = head.split(b"\r\n")[0].decode(); target = line.split()[1]
    seen.append(target)
    want = "Basic " + base64.b64encode(b"user:p@ss").decode()
    if want.encode() not in head:
        w.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"); await w.drain(); w.close(); return
    host, port = target.rsplit(":", 1)
    tr, tw = await asyncio.open_connection("127.0.0.1", int(port))
    w.write(b"HTTP/1.1 200 Connection established\r\n\r\n"); await w.drain()
    async def pipe(a, b):
        try:
            while d := await a.read(65536): b.write(d); await b.drain()
        except Exception: pass
        finally: b.close()
    await asyncio.gather(pipe(r, tw), pipe(tr, w))

async def http_server(r, w):
    await r.readuntil(b"\r\n\r\n")
    w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"); await w.drain(); w.close()

async def socks_get(port, host, hport):
    """Client side: SOCKS5 no-auth CONNECT via the forwarder, then GET /."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(b"\x05\x01\x00"); await w.drain(); await r.readexactly(2)
    h = host.encode(); w.write(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + hport.to_bytes(2, "big")); await w.drain()
    head = await r.readexactly(4); await _read_addr(r, head[3])
    if head[1] != 0: w.close(); return None
    w.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"); await w.drain()
    body = await r.read(-1); w.close(); return body

async def main():
    assert decode_target(3, b"\x0bexample.com\x01\xbb") == ("example.com", 443)
    assert decode_target(1, b"\x7f\x00\x00\x01\x00\x50") == ("127.0.0.1", 80)
    srv = await asyncio.start_server(http_server, "127.0.0.1", 0); sport = srv.sockets[0].getsockname()[1]
    seen = []
    hp = await asyncio.start_server(lambda r, w: http_proxy(r, w, seen), "127.0.0.1", 0); hpport = hp.sockets[0].getsockname()[1]
    states = []
    # 1. HTTP CONNECT upstream with auth
    fw = Forwarder(Upstream("127.0.0.1", hpport, "user", "p@ss", "http"), on_state=states.append)
    port = await fw.start()
    body = await socks_get(port, "127.0.0.1", sport)
    assert body and body.endswith(b"hello"), body
    assert seen == [f"127.0.0.1:{sport}"], seen
    # 2. wrong password -> 407 -> connect refused to the client, counts as a failure
    fw.upstream = Upstream("127.0.0.1", hpport, "user", "wrong", "http")
    assert await socks_get(port, "127.0.0.1", sport) is None
    assert await socks_get(port, "127.0.0.1", sport) is None
    assert states == ["down"], states
    # 3. upstream back (fake SOCKS5 with auth) -> "up"
    seen2 = []
    up = await asyncio.start_server(lambda r, w: fake_upstream(r, w, seen2), "127.0.0.1", 0); upport = up.sockets[0].getsockname()[1]
    fw.upstream = Upstream("127.0.0.1", upport, "user", "p@ss")
    body = await socks_get(port, "127.0.0.1", sport)
    assert body and body.endswith(b"hello"), body
    assert states == ["down", "up"], states
    # 4. dead upstream (closed port) -> down again after 2 failures
    fw.upstream = Upstream("127.0.0.1", 1)
    await socks_get(port, "127.0.0.1", sport); await socks_get(port, "127.0.0.1", sport)
    assert states == ["down", "up", "down"], states
    await fw.stop(); srv.close(); hp.close(); up.close()
    print("forwarder state OK")

asyncio.run(main())
