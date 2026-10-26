"""E2E: real Camoufox via authenticated SOCKS5 (local test upstream) -> in-page checks."""
import asyncio, json, os, sys, tempfile
os.environ.setdefault("ADM_DATA_DIR", tempfile.mkdtemp(prefix="adm-e2e-"))
from adm import store
from adm.launcher import launch
from adm.socks_forward import _read_addr

hits = []

async def upstream(r, w):
    try:
        _, n = await r.readexactly(2); await r.readexactly(n)
        w.write(b"\x05\x02"); await w.drain()
        await r.readexactly(1); u = await r.readexactly((await r.readexactly(1))[0]); p = await r.readexactly((await r.readexactly(1))[0])
        if (u, p) != (b"e2e", b"s3cr@t"): w.write(b"\x01\x01"); await w.drain(); w.close(); return
        w.write(b"\x01\x00"); await w.drain()
        _, _, _, atyp = await r.readexactly(4); a = await _read_addr(r, atyp)
        host = a[1:-2].decode() if atyp == 3 else ".".join(map(str, a[:4]))
        hits.append((atyp, host))
        tr, tw = await asyncio.open_connection(host, int.from_bytes(a[-2:], "big"))
        w.write(b"\x05\x00\x00\x01" + bytes(6)); await w.drain()
        async def pipe(x, y):
            try:
                while d := await x.read(65536): y.write(d); await y.drain()
            except Exception: pass
            finally: y.close()
        await asyncio.gather(pipe(r, tw), pipe(tr, w))
    except Exception:
        w.close()

CHECKS = """async () => {
  const out = {};
  const safe = async (k, f) => { try { out[k] = await f(); } catch (e) { out[k] = "ERR " + String(e); } };
  await safe('ip', async () => (await (await fetch('https://api.ipify.org?format=json')).json()).ip);
  out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  out.intlLocale = Intl.DateTimeFormat().resolvedOptions().locale;
  out.languages = navigator.languages;
  out.webdriver = navigator.webdriver;
  out.ua = navigator.userAgent;
  out.platform = navigator.platform;
  out.screen = [screen.width, screen.height, devicePixelRatio];
  out.cores = navigator.hardwareConcurrency;
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  out.gpu = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
  await safe('rtc', () => new Promise(res => {
    const ips = new Set();
    try {
      const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
      pc.createDataChannel('x');
      pc.onicecandidate = e => { if (!e.candidate) { res([...ips]); return; }
        ips.add(e.candidate.candidate.split(" ").slice(4, 8).join(" ")); };
      pc.createOffer().then(o => pc.setLocalDescription(o));
      setTimeout(() => res([...ips]), 6000);
    } catch (e) { res('ERR ' + e); }
  }));
  await safe('canvas', () => { const c = document.createElement('canvas'); const x = c.getContext('2d');
    x.font = '16px Arial'; x.fillText('adm fp test', 2, 20); const s = c.toDataURL();
    let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h.toString(16); });
  return JSON.stringify(out);
}"""

async def main():
    srv = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    db = store.connect()
    from adm.cli import new_fingerprint
    ref = sys.argv[1] if len(sys.argv) > 1 else None
    p = store.get(db, ref) if ref else None
    if not p:
        p = store.create(db, "e2e", "macos", new_fingerprint("macos"), proxy=f"socks5://e2e:s3cr%40t@127.0.0.1:{port}")
    else:
        store.update(db, p["id"], proxy=f"socks5://e2e:s3cr%40t@127.0.0.1:{port}")
    result = {}
    async def on_ready(ctx):
        page = ctx.pages[0]
        await page.goto("https://example.com")
        result.update(json.loads(await page.evaluate(CHECKS)))
        result["stored_locale"] = store.get(db, p["id"])["locale"]
        result["proxy_check"] = {k: store.get(db, p["id"])["proxy_check"].get(k) for k in ("ip", "country", "timezone")}
        await ctx.close()
    await launch(p["id"], headless=True, on_ready=on_ready)
    result["upstream_hosts_unresolved"] = all(a == 3 for a, _ in hits if not _.replace(".", "").isdigit())
    result["profile"] = p["id"]; result["data_dir"] = os.environ["ADM_DATA_DIR"]
    print(json.dumps(result, ensure_ascii=False, indent=2))

asyncio.run(main())
