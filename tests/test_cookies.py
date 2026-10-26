"""Cookie format parsing/serialization (no browser)."""
import json, time
from adm.cookies import parse, to_json, to_netscape, CookieFormatError, SESSION_TTL

F = int(time.time()) + 86400
src = [
    {"domain": ".example.com", "hostOnly": False, "name": "a", "value": "1", "path": "/", "sameSite": "unspecified", "secure": False, "expirationDate": F + 0.5},
    {"domain": ".example.com", "hostOnly": True, "name": "h", "value": "2", "path": "/", "sameSite": "lax", "secure": True, "session": True},
    {"domain": "x.com", "name": "ms", "value": "3", "expirationDate": F * 1000, "sameSite": "no_restriction", "secure": True},
    {"domain": "x.com", "name": "old", "value": "4", "expirationDate": 1600000000},
    {"domain": "x.com", "name": "nosecure", "value": "5", "sameSite": "no_restriction", "secure": False, "expirationDate": F},
    {"name": "nodomain", "value": "6"},
]
c, skipped = parse("﻿" + json.dumps(src))
by = {x["name"]: x for x in c}
assert set(by) == {"a", "h", "ms", "nosecure"}, by.keys()
assert by["h"]["domain"] == "example.com" and by["h"]["expires"] > time.time() + SESSION_TTL - 60  # session -> persistent
assert by["ms"]["expires"] == F                     # ms -> s
assert by["a"]["sameSite"] == "None" and by["nosecure"]["sameSite"] == "Lax"
assert len(skipped) == 2
assert parse(json.dumps({"cookies": src[:1]}))[0][0]["name"] == "a"   # wrapper object

txt = to_netscape(c)
c2, _ = parse(txt)
assert {(x["name"], x["domain"], x["secure"]) for x in c2} == {(x["name"], x["domain"], x["secure"]) for x in c}
back = json.loads(to_json(c))
assert {x["name"]: x["hostOnly"] for x in back}["h"] is True

for bad in ("", "hello", "[1,2", "{\"a\": 1"):
    try:
        parse(bad); raise AssertionError(bad)
    except CookieFormatError:
        pass
print("cookies OK")
