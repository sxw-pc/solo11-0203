"""Collect directed physical approaches to the public bicycle access desk."""

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS access_path (
       id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
       length INTEGER NOT NULL CHECK(length>0), open INTEGER NOT NULL CHECK(open IN (0,1)))''',
    '''CREATE TABLE IF NOT EXISTS access_arrow (
       node TEXT PRIMARY KEY, path TEXT NOT NULL REFERENCES access_path, proof TEXT NOT NULL)''',
)


def setup(db):
    for ddl in SCHEMA:
        db.execute(ddl)


def survey(db, id, source, target, length, open=True):
    row = (id,source,target,length,open)
    prior = db.execute('SELECT * FROM access_path WHERE id=?',(id,)).fetchone()
    if source == target or (prior is not None and prior != row):
        raise ValueError('invalid or conflicting path survey')
    if prior is None:
        db.execute('INSERT INTO access_path VALUES (?,?,?,?,?)',row)
    return {'path':id,'duplicate':prior is not None}


def outgoing(db, node):
    return list(db.execute('SELECT id,target,length FROM access_path WHERE source=? AND open=1 ORDER BY id',(node,)))
