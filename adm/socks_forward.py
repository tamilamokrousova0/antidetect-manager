"""Local SOCKS5 forwarder: 127.0.0.1:<port> (no auth) -> upstream SOCKS5 (user/pass).

Firefox/Playwright can't authenticate to SOCKS5, so every profile with an
authenticated socks5 proxy gets its own local listener bound to loopback.
Hostnames are passed through to the upstream unresolved (ATYP=domain),
so DNS happens on the proxy side.
"""
import asyncio
import ipaddress
import socket
import struct
from dataclasses import dataclass


@dataclass
class Upstream:
    host: str
    port: int
    username: str | None = None
    password: str | None = None


class SocksError(Exception):
    pass


async def _read_addr(reader: asyncio.StreamReader, atyp: int) -> bytes:
    """Read DST.ADDR+DST.PORT as raw bytes (re-sent verbatim upstream)."""
    if atyp == 1:
        return await reader.readexactly(4 + 2)
    if atyp == 4:
        return await reader.readexactly(16 + 2)
    if atyp == 3:
        ln = await reader.readexactly(1)
        return ln + await reader.readexactly(ln[0] + 2)
    raise SocksError(f"bad atyp {atyp}")


async def upstream_connect(up: Upstream, atyp: int, addr: bytes):
    """Open a tunnel through the upstream proxy. Returns (reader, writer, reply)."""
    r, w = await asyncio.wait_for(asyncio.open_connection(up.host, up.port), 20)
    try:
        methods = b"\x02" if up.username else b"\x00"
        w.write(b"\x05\x01" + methods)
        await w.drain()
        ver, method = await r.readexactly(2)
        if ver != 5 or method == 0xFF:
            raise SocksError("upstream rejected auth methods")
        if method == 2:
            u = (up.username or "").encode()
            p = (up.password or "").encode()
            w.write(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            await w.drain()
            _, status = await r.readexactly(2)
            if status != 0:
                raise SocksError("upstream auth failed")
        w.write(b"\x05\x01\x00" + bytes([atyp]) + addr)
        await w.drain()
        head = await r.readexactly(4)
        bnd = await _read_addr(r, head[3])
        if head[1] != 0:
            raise SocksError(f"upstream connect failed, rep={head[1]}")
        return r, w, head + bnd
    except BaseException:
        w.close()
        raise


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter):
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


class Forwarder:
    def __init__(self, upstream: Upstream, port: int = 0):
        self.upstream = upstream
        self.port = port
        self._server: asyncio.base_events.Server | None = None

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter):
        try:
            ver, n = await r.readexactly(2)
            await r.readexactly(n)
            if ver != 5:
                raise SocksError("not socks5")
            w.write(b"\x05\x00")
            await w.drain()
            ver, cmd, _, atyp = await r.readexactly(4)
            addr = await _read_addr(r, atyp)
            if cmd != 1:  # CONNECT only; UDP is not tunnelled (WebRTC is handled in-browser)
                w.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                await w.drain()
                w.close()
                return
            try:
                ur, uw, reply = await upstream_connect(self.upstream, atyp, addr)
            except Exception:
                w.write(b"\x05\x01\x00\x01" + b"\x00" * 6)
                await w.drain()
                w.close()
                return
            w.write(reply)
            await w.drain()
            await asyncio.gather(_pipe(r, uw), _pipe(ur, w))
        except (asyncio.IncompleteReadError, ConnectionError, SocksError):
            w.close()

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()


def encode_target(host: str, port: int) -> tuple[int, bytes]:
    try:
        ip = ipaddress.ip_address(host)
        atyp = 1 if ip.version == 4 else 4
        return atyp, ip.packed + struct.pack(">H", port)
    except ValueError:
        h = host.encode()
        return 3, bytes([len(h)]) + h + struct.pack(">H", port)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
