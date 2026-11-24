"""Inventory and independent reader receipts at a battery exchange cabinet."""

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS battery (
       id TEXT PRIMARY KEY, fitting TEXT NOT NULL, charge INTEGER NOT NULL CHECK(charge BETWEEN 0 AND 100),
       bin TEXT UNIQUE NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS battery_read (
       id TEXT PRIMARY KEY, bin TEXT NOT NULL, battery TEXT NOT NULL REFERENCES battery,
       operation TEXT NOT NULL CHECK(operation IN ('take','return')), minute INTEGER NOT NULL CHECK(minute >= 0))''',
    '''CREATE TABLE IF NOT EXISTS exchange (
       id TEXT PRIMARY KEY, bin TEXT NOT NULL, take TEXT UNIQUE NOT NULL REFERENCES battery_read,
       put_back TEXT UNIQUE NOT NULL REFERENCES battery_read, minute INTEGER NOT NULL CHECK(minute >= 0))''',
)


def ensure_schema(db):
    for statement in SCHEMA:
        db.execute(statement)


def register(db, id, fitting, charge, bin):
    db.execute('INSERT INTO battery VALUES (?,?,?,?)', (id, fitting, charge, bin))
    return {'battery': id, 'bin': bin}


def scan(db, id, bin, battery, operation, minute):
    row = (id, bin, battery, operation, minute)
    prior = db.execute('SELECT * FROM battery_read WHERE id=?', (id,)).fetchone()
    if prior is not None and prior != row:
        raise ValueError(f'conflicting reader receipt {id}')
    if prior is None:
        db.execute('INSERT INTO battery_read VALUES (?,?,?,?,?)', row)
    return {'receipt': id, 'duplicate': prior is not None}


def read(db, id):
    row = db.execute('SELECT bin,battery,operation,minute FROM battery_read WHERE id=?', (id,)).fetchone()
    if row is None:
        raise ValueError(f'no reader receipt {id}')
    return row
