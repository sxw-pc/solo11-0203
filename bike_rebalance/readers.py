"""Read-head commissioning and sample coverage for fixed-site inspections."""

import json
from pathlib import Path
from sqlite3 import connect

from .evidence import minute, require, token, unique_pairs

SCHEMA = """
CREATE TABLE IF NOT EXISTS reader(reader TEXT PRIMARY KEY, station TEXT NOT NULL, period INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sample(id TEXT PRIMARY KEY, reader TEXT NOT NULL REFERENCES reader,
                                 tick INTEGER NOT NULL, signal INTEGER NOT NULL, UNIQUE(reader,tick));
"""


def open_reader_log(path):
    db = connect(path)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    return db


def register(db, reader, station, period):
    require(token(reader) and token(station) and minute(period) and period > 0, 'invalid reader identity or period')
    with db:
        db.execute('INSERT INTO reader VALUES (?,?,?)', (reader, station, period))
    return {'reader': reader, 'station': station, 'period': period}


def accept(db, path):
    rows = json.loads(Path(path).read_bytes(), object_pairs_hook=unique_pairs)
    require(isinstance(rows, list) and bool(rows), 'expected a nonempty reader sample list')
    with db:
        for row in rows:
            require(isinstance(row, dict) and set(row) == {'id','reader','tick','signal'}, 'invalid sample fields')
            id, reader, tick, signal = (row[key] for key in ('id','reader','tick','signal'))
            require(token(id) and token(reader) and minute(tick) and type(signal) is int and 0 <= signal <= 100,
                    'invalid reader sample')
            found = db.execute('SELECT reader,tick,signal FROM sample WHERE id=?', (id,)).fetchone()
            require(found is None or found == (reader,tick,signal), 'sample receipt changed')
            if found is None:
                db.execute('INSERT INTO sample VALUES (?,?,?,?)', (id,reader,tick,signal))
    return {'samples': len(rows)}


def coverage(db, station, start, end):
    require(token(station) and minute(start) and minute(end) and 0 < end-start <= 1440, 'invalid inspection span')
    report = {}
    for reader, period in db.execute('SELECT reader,period FROM reader WHERE station=? ORDER BY reader', (station,)):
        rows = dict(db.execute('SELECT tick,id FROM sample WHERE reader=? AND tick>=? AND tick<?', (reader, start, end)))
        expected = range(start,end,period)
        missing = [tick for tick in expected if tick not in rows]
        report[reader] = {'expected': len(expected), 'missing': missing, 'proof': [rows[t] for t in expected if t in rows]}
    require(bool(report), 'station has no registered readers')
    return {'station':station,'start':start,'end':end,'readers':report,
            'state':'COMPLETE' if all(not row['missing'] for row in report.values()) else 'MISSING'}
