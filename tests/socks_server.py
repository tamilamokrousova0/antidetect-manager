"""Minimal SOCKS5 server with user/pass auth, for local testing only.
Usage: python tests/socks_server.py PORT USER PASS   (binds 127.0.0.1)"""
import asyncio, sys
from adm.socks_forward import _read_addr

PORT, USER, PASS = int(sys.argv[1]), sys.argv[2].encode(), sys.argv[3].encode()

async def pipe(a, b):
    try:
        while d := await a.read(65536):
            b.write(d); await b.drain()
    except Exception:
        pass
    finally:
        b.close()

async def handle(r, w):
    try:
        _, n = await r.readexactly(2); await r.readexactly(n)
        w.write(b"\x05\x02"); await w.drain()
        await r.readexactly(1)
        u = await r.readexactly((await r.readexactly(1))[0]); p = await r.readexactly((await r.readexactly(1))[0])
        if (u, p) != (USER, PASS):
            w.write(b"\x01\x01"); await w.drain(); w.close(); return
        w.write(b"\x01\x00"); await w.drain()
        _, _, _, atyp = await r.readexactly(4); a = await _read_addr(r, atyp)
        host = a[1:-2].decode() if atyp == 3 else ".".join(map(str, a[:4]))
        tr, tw = await asyncio.open_connection(host, int.from_bytes(a[-2:], "big"))
        w.write(b"\x05\x00\x00\x01" + bytes(6)); await w.drain()
        await asyncio.gather(pipe(r, tw), pipe(tr, w))
    except Exception:
        w.close()

async def main():
    srv = await asyncio.start_server(handle, "127.0.0.1", PORT)
    print(f"socks5 on 127.0.0.1:{PORT}", flush=True)
    await srv.serve_forever()

asyncio.run(main())
