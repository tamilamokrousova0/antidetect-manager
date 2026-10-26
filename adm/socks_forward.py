"""Local SOCKS5 forwarder: 127.0.0.1:<port> (no auth) -> upstream SOCKS5 or
HTTP CONNECT proxy (user/pass).

Every profile with a proxy gets its own local listener bound to loopback: the
browser only ever talks SOCKS5 without auth to 127.0.0.1, the forwarder does
the upstream protocol and credentials. Hostnames are passed through to the
upstream unresolved (ATYP=domain / CONNECT host:port), so DNS happens on the
proxy side. Because every connection passes here, the forwarder also knows
when the proxy stops answering (on_state callback: "down" / "up").
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
    scheme: str = "socks5"  # socks5 | http


def decode_target(atyp: int, addr: bytes) -> tuple[str, int]:
    port = struct.unpack(">H", addr[-2:])[0]
    if atyp == 1:
        return str(ipaddress.IPv4Address(addr[:4])), port
    if atyp == 4:
        return str(ipaddress.IPv6Address(addr[:16])), port
    return addr[1:-2].decode("idna"), port


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


async def _http_connect(up: Upstream, atyp: int, addr: bytes):
    """CONNECT through an HTTP proxy (Basic auth). Same error vocabulary as SOCKS."""
    import base64
    host, port = decode_target(atyp, addr)
    r, w = await asyncio.wait_for(asyncio.open_connection(up.host, up.port), 20)
    try:
        auth = ""
        if up.username:
            tok = base64.b64encode(f"{up.username}:{up.password or ''}".encode()).decode()
            auth = f"Proxy-Authorization: Basic {tok}\r\n"
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        w.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n{auth}\r\n".encode())
        await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 20)
        status = head.split(b"\r\n", 1)[0].split(b" ")
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
        if code == 407:
            raise SocksError("upstream auth failed")
        if code != 200:
            raise SocksError(f"upstream connect failed, rep={code}")
        return r, w, b"\x05\x00\x00\x01" + b"\x00" * 6
    except BaseException:
        w.close()
        raise


async def upstream_connect(up: Upstream, atyp: int, addr: bytes):
    """Open a tunnel through the upstream proxy. Returns (reader, writer, reply)."""
    if up.scheme == "http":
        return await _http_connect(up, atyp, addr)
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
    # consecutive upstream failures before the proxy is reported "down"
    DOWN_AFTER = 2

    def __init__(self, upstream: Upstream, port: int = 0, on_state=None):
        self.upstream = upstream
        self.port = port
        self.on_state = on_state  # callable(state: "down" | "up")
        self.failures = 0
        self.down = False
        self._server: asyncio.base_events.Server | None = None

    def _note(self, ok: bool):
        if ok:
            self.failures = 0
            if self.down:
                self.down = False
                if self.on_state:
                    self.on_state("up")
        else:
            self.failures += 1
            if self.failures >= self.DOWN_AFTER and not self.down:
                self.down = True
                if self.on_state:
                    self.on_state("down")

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
            except SocksError as e:
                # the proxy answered: a refused target is the site's problem, not the proxy's
                self._note("connect failed" in str(e))
                w.write(b"\x05\x01\x00\x01" + b"\x00" * 6)
                await w.drain()
                w.close()
                return
            except Exception:
                self._note(False)
                w.write(b"\x05\x01\x00\x01" + b"\x00" * 6)
                await w.drain()
                w.close()
                return
            self._note(True)
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
