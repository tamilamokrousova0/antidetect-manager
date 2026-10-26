"""adm — antidetect profile manager CLI. All commands support --json for the UI."""
import argparse
import asyncio
import json
import sys

from . import store
from .proxy import parse_proxy, check_proxy

OS_CHOICES = ("macos", "windows", "linux")


def new_fingerprint(os_name: str) -> dict:
    from camoufox.fingerprints import generate_fingerprint
    fp = generate_fingerprint(os=os_name)
    return json.loads(json.dumps(fp, default=str))


def _public(p: dict, full: bool = False) -> dict:
    d = {k: v for k, v in p.items() if k != "fingerprint"}
    if d.get("proxy"):
        d["proxy"] = parse_proxy(d["proxy"]).to_url(hide_password=True)
    nav = p["fingerprint"].get("navigator", {})
    # Only fields that reach the page unchanged. UA version, core count and
    # screen size are adjusted at launch (engine version / host cores / display).
    d["summary"] = {
        "platform": nav.get("platform"),
        "gpu": (p["fingerprint"].get("gpu") or {}).get("renderer"),
    }
    if full:
        d["fingerprint"] = p["fingerprint"]
    return d


def _tags(args) -> list[str] | None:
    if getattr(args, "tags", None) is not None:
        return [s.strip() for s in args.tags.split(",") if s.strip()]
    return args.tag


def _out(args, data, text: str):
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else text)


def cmd_create(args, db):
    proxy = None
    if args.proxy:
        proxy = parse_proxy(args.proxy).to_url()
    created = []
    for i in range(args.count):
        name = args.name if args.count == 1 else f"{args.name}-{i + 1:03d}"
        p = store.create(db, name, args.os, new_fingerprint(args.os), proxy=proxy,
                         tags=_tags(args) or [], notes=args.notes or "")
        created.append(_public(p))
    _out(args, created, "\n".join(f"{c['id']}  {c['name']}  {c['summary']}" for c in created))


def cmd_list(args, db):
    rows = [_public(p) for p in store.list_all(db, args.tag)]
    lines = []
    for r in rows:
        chk = r.get("proxy_check") or {}
        geo = f"{chk.get('ip')} {chk.get('country')}/{chk.get('city')}" if chk.get("ok") else ("proxy ERR" if chk else "-")
        lines.append(f"{r['id']}  {r['name']:<24} {r['os']:<8} {geo:<36} {','.join(r['tags'])}")
    _out(args, rows, "\n".join(lines) or "(no profiles)")


def cmd_show(args, db):
    p = store.get(db, args.profile) or sys.exit("not found")
    _out(args, _public(p, full=True), json.dumps(_public(p, full=args.full), ensure_ascii=False, indent=2))


def cmd_set(args, db):
    p = store.get(db, args.profile) or sys.exit("not found")
    fields = {}
    if args.proxy is not None:
        fields["proxy"] = parse_proxy(args.proxy).to_url() if args.proxy else None
        fields["proxy_check"] = None
    if args.name:
        fields["name"] = args.name
    if _tags(args) is not None:
        fields["tags"] = _tags(args)
    if args.notes is not None:
        fields["notes"] = args.notes
    if args.locale is not None:
        # '' returns the profile to automatic (main language of the proxy country)
        fields["locale"] = args.locale or None
        fields["locale_manual"] = 1 if args.locale else 0
    if fields:
        store.update(db, p["id"], **fields)
    _out(args, _public(store.get(db, p["id"])), "ok")


def cmd_check(args, db):
    targets = store.list_all(db) if args.profile == "all" else [store.get(db, args.profile)]
    results = []

    async def run():
        for p in targets:
            if not p or not p["proxy"]:
                continue
            chk = await check_proxy(parse_proxy(p["proxy"]))
            store.update(db, p["id"], proxy_check=chk)
            results.append({"id": p["id"], "name": p["name"], **chk})

    asyncio.run(run())
    lines = [f"{r['name']:<24} " + (f"OK  {r['ip']}  {r['country']}/{r['city']}  {r['timezone']}  {r['latency_ms']}ms"
                                    + ("  [hosting/proxy flag]" if r.get("flag_hosting") or r.get("flag_proxy") else "")
                                    if r["ok"] else f"ERR {r['error']}") for r in results]
    _out(args, results, "\n".join(lines) or "(nothing to check)")


def cmd_check_raw(args, db):
    chk = asyncio.run(check_proxy(parse_proxy(args.proxy)))
    _out(args, chk, json.dumps(chk, ensure_ascii=False, indent=2))


def cmd_launch(args, db):
    from .launcher import launch

    async def ready(ctx):
        # UI waits for this line to flip the profile from "starting" to "running"
        print(json.dumps({"event": "ready"}) if args.json else "browser ready", flush=True)

    asyncio.run(launch(args.profile, url=args.url, headless=args.headless, on_ready=ready))


def cmd_cookies_export(args, db):
    from . import cookies as ck
    data = asyncio.run(ck.export_cookies(args.profile))
    text = ck.to_netscape(data) if args.format == "netscape" else ck.to_json(data)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
        _out(args, {"exported": len(data), "file": args.output}, f"exported {len(data)} cookies -> {args.output}")
    elif args.json:
        print(json.dumps({"exported": len(data), "text": text}, ensure_ascii=False))
    else:
        sys.stdout.write(text)


def cmd_cookies_import(args, db):
    from . import cookies as ck
    raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8-sig").read()
    try:
        parsed, skipped = ck.parse(raw)
    except ck.CookieFormatError as e:
        sys.exit(f"не удалось разобрать cookies: {e}")
    if not parsed:
        sys.exit("в файле нет действующих cookies" + (f" (пропущено: {len(skipped)})" if skipped else ""))
    res = asyncio.run(ck.import_cookies(args.profile, parsed, replace=args.replace))
    res["skipped"] = skipped
    _out(args, res, f"imported {res['imported']}, failed {len(res['failed'])}, skipped {len(skipped)}, "
                    f"now in profile: {res['total_in_profile']}"
                    + "".join(f"\n  ! {x}" for x in res["failed"] + skipped))


def cmd_engine(args, db):
    from . import engine
    if args.action == "status":
        st = engine.status()
        _out(args, st, f"camoufox {st['package']}, browser: {st['browser'] or 'NOT INSTALLED'}, "
                       f"geoip: {'ok' if st['geoip'] else 'missing'}  ({st['cache_dir']})")
    else:
        engine.fetch(engine.print_progress(args.json))


def cmd_delete(args, db):
    p = store.get(db, args.profile) or sys.exit("not found")
    store.delete(db, p["id"])
    _out(args, {"deleted": p["id"]}, f"deleted {p['id']} {p['name']}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="adm")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="create profile(s) with a fresh pinned fingerprint")
    c.add_argument("name")
    c.add_argument("--os", choices=OS_CHOICES, default="macos")
    c.add_argument("--proxy")
    c.add_argument("--tag", action="append")
    c.add_argument("--tags", help="comma-separated")
    c.add_argument("--notes")
    c.add_argument("--count", type=int, default=1)
    c.set_defaults(fn=cmd_create)

    c = sub.add_parser("list")
    c.add_argument("--tag")
    c.set_defaults(fn=cmd_list)

    c = sub.add_parser("show")
    c.add_argument("profile")
    c.add_argument("--full", action="store_true", help="include full fingerprint")
    c.set_defaults(fn=cmd_show)

    c = sub.add_parser("set", help="edit profile fields (--proxy '' to remove)")
    c.add_argument("profile")
    c.add_argument("--proxy")
    c.add_argument("--name")
    c.add_argument("--tag", action="append")
    c.add_argument("--tags", help="comma-separated, '' clears")
    c.add_argument("--locale", help="e.g. en-US; '' = auto by proxy country")
    c.add_argument("--notes")
    c.set_defaults(fn=cmd_set)

    c = sub.add_parser("engine", help="browser engine in ~/Library/Caches/camoufox")
    c.add_argument("action", choices=("status", "fetch"))
    c.set_defaults(fn=cmd_engine)

    c = sub.add_parser("check", help="check proxy of a profile, or 'all'")
    c.add_argument("profile")
    c.set_defaults(fn=cmd_check)

    c = sub.add_parser("check-proxy", help="check a proxy string without a profile")
    c.add_argument("proxy")
    c.set_defaults(fn=cmd_check_raw)

    c = sub.add_parser("launch")
    c.add_argument("profile")
    c.add_argument("--url")
    c.add_argument("--headless", action="store_true")
    c.set_defaults(fn=cmd_launch)

    c = sub.add_parser("cookies-export", help="export cookies of a stopped profile")
    c.add_argument("profile")
    c.add_argument("--format", choices=("json", "netscape"), default="json")
    c.add_argument("-o", "--output")
    c.set_defaults(fn=cmd_cookies_export)

    c = sub.add_parser("cookies-import", help="import cookies (JSON or cookies.txt, auto-detected); file or -")
    c.add_argument("profile")
    c.add_argument("file")
    c.add_argument("--replace", action="store_true", help="delete existing cookies first")
    c.set_defaults(fn=cmd_cookies_import)

    c = sub.add_parser("delete")
    c.add_argument("profile")
    c.set_defaults(fn=cmd_delete)

    args = ap.parse_args(argv)
    args.fn(args, store.connect())


if __name__ == "__main__":
    main()
