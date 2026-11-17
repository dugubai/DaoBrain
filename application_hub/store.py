"""Single-host SQLite persistence; no hidden remote DB or network calls.

Transactions combine object versions and metadata-only audit rows. A configured
path must already have a writable parent; deployment never replaces an old DB.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from fastapi import HTTPException

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,
 password TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY, user_id TEXT NOT NULL,
 expires INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
 owner TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
 payload TEXT NOT NULL, created INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS object_kind ON objects(kind,created);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, actor TEXT NOT NULL,
 action TEXT NOT NULL, object_id TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payment_events(id TEXT PRIMARY KEY, digest TEXT NOT NULL);
"""


@contextmanager
def transaction():
    value = os.environ.get("APP_DB_PATH", "")
    path = Path(value)
    if not value or not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
        raise HTTPException(503, "业务存储未配置")
    new_database = not path.exists()
    db = sqlite3.connect(str(path), timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        if new_database:
            db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        # Never run schema mutations against an existing or incompatible DB.
        has_version = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
        if not has_version or [r[0] for r in db.execute("SELECT version FROM schema_version")] != [1]:
            raise HTTPException(503, "数据库版本不匹配")
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def audit(db, actor, action, object_id):
    # Never store clinical prose, credentials or prompts in audit records.
    db.execute("INSERT INTO events VALUES(?,?,?,?,?)",
               (uuid4().hex, actor, action, object_id, int(time.time())))


def decode(row):
    value = dict(row)
    value["payload"] = json.loads(value["payload"])
    return value


def get(db, kind, object_id):
    row = db.execute("SELECT * FROM objects WHERE id=? AND kind=?", (object_id, kind)).fetchone()
    if row is None:
        raise HTTPException(404, "记录不存在")
    return decode(row)


def create(db, kind, owner, payload, status="draft"):
    # A bounded prototype, not an unbounded production ingestion service.
    if db.execute("SELECT count(*) FROM objects").fetchone()[0] >= 2000:
        raise HTTPException(409, "原型记录容量已满")
    object_id = uuid4().hex
    db.execute("INSERT INTO objects VALUES(?,?,?,?,?,?,?)",
               (object_id, kind, owner, 1, status,
                json.dumps(payload, ensure_ascii=False, allow_nan=False), int(time.time())))
    audit(db, owner, kind + ".create", object_id)
    return get(db, kind, object_id)


def update(db, item, expected_version, payload, status, actor, action):
    if item["version"] != expected_version:
        raise HTTPException(409, "记录版本冲突")
    if expected_version >= 10000:
        raise HTTPException(409, "原型版本上限")
    result = db.execute("UPDATE objects SET version=version+1,payload=?,status=? WHERE id=? AND version=?",
                        (json.dumps(payload, ensure_ascii=False, allow_nan=False), status,
                         item["id"], expected_version))
    if result.rowcount != 1:
        raise HTTPException(409, "记录版本冲突")
    audit(db, actor, action, item["id"])
    return get(db, item["kind"], item["id"])


def listing(db, kind, owner=None, offset=0, limit=20, status=None):
    clauses, values = ["kind=?"], [kind]
    if owner is not None:
        clauses.append("owner=?")
        values.append(owner)
    if status is not None:
        clauses.append("status=?")
        values.append(status)
    where = " AND ".join(clauses)
    rows = db.execute("SELECT * FROM objects WHERE " + where + " ORDER BY created,id LIMIT ? OFFSET ?",
                      (*values, limit, offset)).fetchall()
    return [decode(row) for row in rows]
