"""Verify raw intake and derive current and historical state without writing."""
from hashlib import sha256

from .evidence import decode, stable
from .replay import TABLES, rebuild
from .store import event_rows, projection_rows


def audit(db):
    issues = []
    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        issues.append("sqlite quick_check failed")
    if db.execute("PRAGMA foreign_key_check").fetchall():
        issues.append("sqlite foreign key check failed")
    raw_batches, canonical = read_batches(db, issues)
    if event_rows(db) != canonical:
        issues.append("event index differs from raw batches")

    seen, previous, revisions, clock = {}, {}, 0, None
    for run, clock, batch in db.execute("SELECT id, clock, batch FROM runs ORDER BY id"):
        if batch is not None and batch in raw_batches:
            seen.update(raw_batches[batch])
        after = flatten(rebuild(seen.values(), clock))
        expected = {(entity, key): (previous.get((entity, key)), after.get((entity, key)))
                    for entity, key in previous.keys() | after.keys()
                    if previous.get((entity, key)) != after.get((entity, key))}
        recorded = {(entity, key): (old, new) for entity, key, old, new in db.execute(
            "SELECT entity, key, before, after FROM history WHERE run = ?", (run,))}
        if expected != recorded:
            issues.append(f"history mismatch in run {run}")
        previous = after
        revisions += len(recorded)
    if seen.keys() != canonical.keys():
        issues.append("raw events missing from run lineage")
    current = {(table, key): data for table in TABLES for key, data in projection_rows(db, table).items()}
    if current != previous:
        issues.append("current projection differs from raw replay")
    return {"status": "AUDIT_OK" if not issues else "AUDIT_FAILED", "as_of": clock,
            "events": len(canonical), "batches": len(raw_batches), "entities": len(current),
            "revisions": revisions, "issues": issues}


def read_batches(db, issues):
    raw_batches, canonical = {}, {}
    for digest, raw in db.execute("SELECT digest, raw FROM batches ORDER BY digest"):
        if sha256(raw).hexdigest() != digest:
            issues.append(f"raw hash mismatch for {digest}")
        try:
            events = {e["id"]: e for e in decode(raw)}
        except ValueError as error:
            issues.append(f"invalid raw batch {digest}: {error}")
            continue
        raw_batches[digest] = events
        indexed = {row[0] for row in db.execute("SELECT event_id FROM batch_events WHERE digest = ?", (digest,))}
        if indexed != events.keys():
            issues.append(f"batch event membership mismatch for {digest}")
        for key, event in events.items():
            normalized = stable(event)
            if canonical.setdefault(key, normalized) != normalized:
                issues.append(f"conflicting raw event {key}")
    return raw_batches, canonical


def flatten(tables):
    return {(table, key): stable(data) for table, rows in tables.items() for key, data in rows.items()}
