"""SQLite storage for profiles. Browser data lives in DATA_DIR/profiles/<id>/."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from platformdirs import user_data_dir  # camoufox dependency; macOS: ~/Library/Application Support, Windows: %LOCALAPPDATA%

DATA_DIR = Path(os.environ.get("ADM_DATA_DIR") or user_data_dir("antidetect-manager", appauthor=False, roaming=False))
PROFILES_DIR = DATA_DIR / "profiles"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  tags          TEXT NOT NULL DEFAULT '[]',
  notes         TEXT NOT NULL DEFAULT '',
  os            TEXT NOT NULL,
  proxy         TEXT,               -- proxy URL incl. credentials
  proxy_check   TEXT,               -- JSON of last check_proxy()
  fingerprint   TEXT NOT NULL,      -- JSON, fixed at creation
  locale        TEXT,               -- e.g. de-DE; auto = main language of the proxy country
  locale_manual INTEGER NOT NULL DEFAULT 0,  -- 1 = set by user, never auto-replaced
  status        TEXT NOT NULL DEFAULT '',   -- '' | warmup | active | checkpoint | banned
  dir           TEXT,               -- folder name under profiles/ (= sanitized profile name; the id for old rows)
  self_check    TEXT,               -- JSON: in-browser check after launch + passport
  created_at    INTEGER NOT NULL,
  last_used_at  INTEGER
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "profiles.db", timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(profiles)")}
    if "locale" not in cols:
        db.execute("ALTER TABLE profiles ADD COLUMN locale TEXT")
    if "locale_manual" not in cols:
        db.execute("ALTER TABLE profiles ADD COLUMN locale_manual INTEGER NOT NULL DEFAULT 0")
    if "status" not in cols:
        db.execute("ALTER TABLE profiles ADD COLUMN status TEXT NOT NULL DEFAULT ''")
    if "self_check" not in cols:
        db.execute("ALTER TABLE profiles ADD COLUMN self_check TEXT")
    if "dir" not in cols:
        db.execute("ALTER TABLE profiles ADD COLUMN dir TEXT")
        db.execute("UPDATE profiles SET dir = id")  # existing folders are named by id; renamed on next list/launch
        db.commit()
    return db


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    d["tags"] = json.loads(d["tags"])
    d["fingerprint"] = json.loads(d["fingerprint"])
    d["proxy_check"] = json.loads(d["proxy_check"]) if d["proxy_check"] else None
    d["self_check"] = json.loads(d["self_check"]) if d.get("self_check") else None
    return d


_BAD = set('<>:"/\\|?*') | {chr(i) for i in range(32)}


def safe_dirname(name: str) -> str:
    """Profile name as a folder name: readable in Finder/Explorer, safe on
    every filesystem (no path separators, Windows-reserved chars/names,
    trailing dots or spaces), at most 60 chars."""
    s = "".join("_" if c in _BAD else c for c in (name or "")).strip(" .")
    s = " ".join(s.split())[:60].rstrip(" .")
    if s.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}:
        s = f"_{s}"
    return s


def profile_dir(p) -> Path:
    """Folder of a profile: pass the profile dict (preferred) or its id."""
    if isinstance(p, dict):
        return PROFILES_DIR / (p.get("dir") or p["id"])
    db = connect()
    row = db.execute("SELECT dir FROM profiles WHERE id=?", (p,)).fetchone()
    return PROFILES_DIR / ((row[0] if row and row[0] else None) or p)


def _locked(d: Path) -> bool:
    """Firefox holds .parentlock (macOS/Linux) or parent.lock (Windows) while running."""
    return (d / ".parentlock").exists() or (d / "parent.lock").exists() or (d / "lock").exists()


def desired_dir(db, p: dict) -> str:
    base = safe_dirname(p["name"]) or p["id"]
    taken = {r[0] for r in db.execute("SELECT dir FROM profiles WHERE id<>? AND dir IS NOT NULL", (p["id"],))}
    if base in taken:
        base = f"{base}-{p['id'][:6]}"
    return base


def sync_dir(db, p: dict) -> dict:
    """Make the folder name follow the profile name. Skipped while the browser
    holds the folder (renamed on the next launch instead)."""
    want = desired_dir(db, p)
    cur = p.get("dir") or p["id"]
    if want == cur:
        return p
    old = PROFILES_DIR / cur
    new = PROFILES_DIR / want
    if old.exists():
        if _locked(old) or new.exists():
            return p
        old.rename(new)
    update(db, p["id"], dir=want)
    p["dir"] = want
    return p


STATUSES = ("", "warmup", "active", "checkpoint", "banned")


def create(db, name: str, os_name: str, fingerprint: dict, proxy: str | None = None,
           tags: list[str] | None = None, notes: str = "", status: str = "") -> dict:
    pid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO profiles (id,name,tags,notes,os,proxy,fingerprint,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, name, json.dumps(tags or []), notes, os_name, proxy, json.dumps(fingerprint), status, int(time.time())),
    )
    db.commit()
    p = get(db, pid)
    d = desired_dir(db, p)
    update(db, pid, dir=d)
    (PROFILES_DIR / d).mkdir(parents=True, exist_ok=True)
    return get(db, pid)


def get(db, ref: str) -> dict | None:
    """Look up by id, id prefix or exact name."""
    r = db.execute("SELECT * FROM profiles WHERE id=? OR name=?", (ref, ref)).fetchone()
    if r is None:
        rows = db.execute("SELECT * FROM profiles WHERE id LIKE ?", (ref + "%",)).fetchall()
        r = rows[0] if len(rows) == 1 else None
    return _row(r)


def list_all(db, tag: str | None = None) -> list[dict]:
    rows = [sync_dir(db, _row(r)) for r in db.execute("SELECT * FROM profiles ORDER BY created_at")]
    if tag:
        rows = [r for r in rows if tag in r["tags"]]
    return rows


def update(db, pid: str, **fields):
    for k in ("tags", "fingerprint", "proxy_check", "self_check"):
        if k in fields and fields[k] is not None and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k])
    sets = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE profiles SET {sets} WHERE id=?", (*fields.values(), pid))
    db.commit()


def delete(db, pid: str):
    import shutil
    p = get(db, pid)
    db.execute("DELETE FROM profiles WHERE id=?", (pid,))
    db.commit()
    if p:
        shutil.rmtree(profile_dir(p), ignore_errors=True)
