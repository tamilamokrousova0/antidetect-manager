"""Прогон профиля через проверочные сайты со скриншотами.
Usage: scripts/live_check.py <profile> [out_dir]   (окно браузера откроется)"""
import asyncio, json, os, sys
from adm.launcher import launch

SITES = [
    ("bl-ip", "https://browserleaks.com/ip", 6),
    ("bl-webrtc", "https://browserleaks.com/webrtc", 8),
    ("bl-dns", "https://browserleaks.com/dns", 15),
    ("creepjs", "https://abrahamjuliot.github.io/creepjs/", 25),
    ("pixelscan", "https://pixelscan.net/fingerprint-check", 30),
    ("sannysoft", "https://bot.sannysoft.com/", 8),
]

OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/adm-live-check"
os.makedirs(OUT, exist_ok=True)

async def main():
    async def ready(ctx):
        page = ctx.pages[0]
        for name, url, wait in SITES:
            try:
                await page.goto(url, timeout=60000, wait_until="domcontentloaded")
                await asyncio.sleep(wait)
                await page.screenshot(path=f"{OUT}/{name}.png", full_page=True)
                text = await page.inner_text("body")
                open(f"{OUT}/{name}.txt", "w").write(text)
                print(name, "ok", len(text), flush=True)
            except Exception as e:
                print(name, "ERR", str(e).splitlines()[0], flush=True)
        await ctx.close()
    await launch(sys.argv[1], headless=False, on_ready=ready)

asyncio.run(main())
