"""Atomic evidence intake, entity projections and decision history in SQLite."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from .evidence import decode, stable
from .replay import TABLES, rebuild

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    digest TEXT PRIMARY KEY, filename TEXT NOT NULL, raw BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, payload TEXT NOT NULL CHECK(json_valid(payload))
);
CREATE TABLE IF NOT EXISTS batch_events (
    digest TEXT REFERENCES batches(digest), event_id TEXT REFERENCES events(id),
    PRIMARY KEY (digest, event_id)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, clock INTEGER NOT NULL, batch TEXT REFERENCES batches(digest)
);
CREATE TABLE IF NOT EXISTS history (
    run INTEGER REFERENCES runs(id), entity TEXT NOT NULL, key TEXT NOT NULL,
    before TEXT, after TEXT, PRIMARY KEY (run, entity, key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS history_entity ON history(entity, key, run);
"""


@contextmanager
def database(filename, readonly=False):
    target = Path(filename).resolve().as_uri() + "?mode=ro" if readonly else filename
    db = sqlite3.connect(target, uri=readonly, timeout=10, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and not readonly:
            projection = "\n".join(f"""CREATE TABLE IF NOT EXISTS {table} (
                key TEXT PRIMARY KEY, data TEXT NOT NULL CHECK(json_valid(data))
            );""" for table in TABLES)
            db.executescript(f"BEGIN IMMEDIATE;\n{SCHEMA}\n{projection}\nPRAGMA user_version=1;\nCOMMIT;")
        elif version != 1:
            raise ValueError(f"unsupported schema version {version}")
        yield db
    finally:
        db.close()


@contextmanager
def transaction(db, write=False):
    db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def event_rows(db):
    return dict(db.execute("SELECT id, payload FROM events"))


def current_clock(db):
    row = db.execute("SELECT clock FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else row[0]


def projection_rows(db, table):
    return dict(db.execute(f"SELECT key, data FROM {table} ORDER BY key"))


def view(db, tables):
    return {"as_of": current_clock(db), "entities": {
        table: {key: json.loads(data) for key, data in projection_rows(db, table).items()}
        for table in tables}}


def ingest(db, filename, now=None, subsystem=None):
    raw = Path(filename).read_bytes()
    incoming = decode(raw, subsystem)
    digest = sha256(raw).hexdigest()
    with transaction(db, write=True):
        db.execute("INSERT OR IGNORE INTO batches VALUES (?, ?, ?)", (digest, Path(filename).name, raw))
        for event in incoming:
            key, payload = event["id"], stable(event)
            row = db.execute("SELECT payload FROM events WHERE id = ?", (key,)).fetchone()
            if row is not None and row[0] != payload:
                raise ValueError(f"conflicting immutable event id {key}")
            db.execute("INSERT OR IGNORE INTO events VALUES (?, ?)", (key, payload))
            db.execute("INSERT OR IGNORE INTO batch_events VALUES (?, ?)", (digest, key))
        return recompute(db, now, digest)


def recompute(db, now, batch=None):
    events = [json.loads(payload) for payload in event_rows(db).values()]
    previous = current_clock(db)
    if now is None:
        now = max(previous or 0, max((event["at"] for event in events), default=0))
    result = rebuild(events, now)
    run = db.execute("INSERT INTO runs(clock, batch) VALUES (?, ?)", (now, batch)).lastrowid
    changed = 0
    for table in TABLES:
        before = projection_rows(db, table)
        after = {key: stable(data) for key, data in result[table].items()}
        for key in sorted(before.keys() | after.keys()):
            old, new = before.get(key), after.get(key)
            if old == new:
                continue
            changed += 1
            if new is None:
                db.execute(f"DELETE FROM {table} WHERE key = ?", (key,))
            else:
                db.execute(f"INSERT OR REPLACE INTO {table}(key, data) VALUES (?, ?)", (key, new))
            db.execute("INSERT INTO history VALUES (?, ?, ?, ?, ?)", (run, table, key, old, new))
    return {"run": run, "as_of": now, "changed": changed, "batch": batch}


def history(db, table, key):
    rows = db.execute("""SELECT run, clock, before, after FROM history
                       JOIN runs ON runs.id = history.run WHERE entity = ? AND key = ? ORDER BY run""",
                      (table, key))
    return [{"run": run, "as_of": clock, "before": json.loads(old) if old is not None else None,
             "after": json.loads(new) if new is not None else None} for run, clock, old, new in rows]
