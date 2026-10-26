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


def profile_dir(pid: str) -> Path:
    return PROFILES_DIR / pid


STATUSES = ("", "warmup", "active", "checkpoint", "banned")


def create(db, name: str, os_name: str, fingerprint: dict, proxy: str | None = None,
           tags: list[str] | None = None, notes: str = "", status: str = "") -> dict:
    pid = uuid.uuid4().hex[:12]
    db.execute(
        "INSERT INTO profiles (id,name,tags,notes,os,proxy,fingerprint,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, name, json.dumps(tags or []), notes, os_name, proxy, json.dumps(fingerprint), status, int(time.time())),
    )
    db.commit()
    profile_dir(pid).mkdir(parents=True, exist_ok=True)
    return get(db, pid)


def get(db, ref: str) -> dict | None:
    """Look up by id, id prefix or exact name."""
    r = db.execute("SELECT * FROM profiles WHERE id=? OR name=?", (ref, ref)).fetchone()
    if r is None:
        rows = db.execute("SELECT * FROM profiles WHERE id LIKE ?", (ref + "%",)).fetchall()
        r = rows[0] if len(rows) == 1 else None
    return _row(r)


def list_all(db, tag: str | None = None) -> list[dict]:
    rows = [_row(r) for r in db.execute("SELECT * FROM profiles ORDER BY created_at")]
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
    db.execute("DELETE FROM profiles WHERE id=?", (pid,))
    db.commit()
    shutil.rmtree(profile_dir(pid), ignore_errors=True)
