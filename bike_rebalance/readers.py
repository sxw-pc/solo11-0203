"""Read-head commissioning and calibrated sample coverage for fixed-site inspections.

A read head restarts its reported minute whenever its power supply is swapped, so
raw ticks cannot be matched against shift minutes directly.  Each sample names its
source segment (one uninterrupted run of the reader clock); samples without a
segment keep the legacy fixed-start reading (correction 0).  Independent
calibration anchors pin a segment tick to a shift minute.  Within one source
segment the shared clock correction must be consistent, and every correction the
recorded anchors allow stays a legal calibration: an inspection window is
COMPLETE only when every legal calibration covers it, one sample covers at most
one required minute, and samples never fill gaps across readers or segments.
When the receipt bound to a required minute still varies between legal
calibrations, the station freezes one coherent candidate (minute/receipt
bindings that hold under a single legal calibration) before its space snapshot
keeps backing reservations.
"""

import json
from itertools import product
from pathlib import Path
from sqlite3 import connect

from .evidence import minute, require, token, unique_pairs

SCHEMA = """
CREATE TABLE IF NOT EXISTS reader(reader TEXT PRIMARY KEY, station TEXT NOT NULL, period INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sample(id TEXT PRIMARY KEY, reader TEXT NOT NULL REFERENCES reader,
                                  segment TEXT NOT NULL, tick INTEGER NOT NULL, signal INTEGER NOT NULL,
                                  UNIQUE(reader,segment,tick));
CREATE TABLE IF NOT EXISTS anchor(id TEXT PRIMARY KEY, reader TEXT NOT NULL REFERENCES reader,
                                  segment TEXT NOT NULL, tick INTEGER NOT NULL, minute INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS candidate(id TEXT PRIMARY KEY, station TEXT NOT NULL, start INTEGER NOT NULL,
                                     finish INTEGER NOT NULL, bindings TEXT NOT NULL CHECK(json_valid(bindings)));
"""
MAX_CALIBRATIONS = 1024


def open_reader_log(path):
    db = connect(path)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute('PRAGMA table_info(sample)')}
    if 'segment' not in columns:
        with db:
            db.execute('ALTER TABLE sample RENAME TO sample_legacy')
            db.execute('CREATE TABLE sample(id TEXT PRIMARY KEY, reader TEXT NOT NULL REFERENCES reader,'
                       ' segment TEXT NOT NULL, tick INTEGER NOT NULL, signal INTEGER NOT NULL,'
                       ' UNIQUE(reader,segment,tick))')
            db.execute("INSERT INTO sample SELECT id, reader, '', tick, signal FROM sample_legacy")
            db.execute('DROP TABLE sample_legacy')
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
            require(isinstance(row, dict) and set(row) in ({'id', 'reader', 'tick', 'signal'},
                                                           {'id', 'reader', 'tick', 'signal', 'segment'}),
                    'invalid sample fields')
            segment = row.get('segment', '')
            id, reader, tick, signal = (row[key] for key in ('id', 'reader', 'tick', 'signal'))
            require(token(id) and token(reader) and (segment == '' or token(segment))
                    and minute(tick) and type(signal) is int and 0 <= signal <= 100,
                    'invalid reader sample')
            found = db.execute('SELECT reader,segment,tick,signal FROM sample WHERE id=?', (id,)).fetchone()
            require(found is None or found == (reader, segment, tick, signal), 'sample receipt changed')
            if found is None:
                db.execute('INSERT INTO sample VALUES (?,?,?,?,?)', (id, reader, segment, tick, signal))
    return {'samples': len(rows)}


def calibrate(db, path):
    rows = json.loads(Path(path).read_bytes(), object_pairs_hook=unique_pairs)
    require(isinstance(rows, list) and bool(rows), 'expected a nonempty calibration anchor list')
    with db:
        for row in rows:
            require(isinstance(row, dict) and set(row) == {'id', 'reader', 'segment', 'tick', 'minute'},
                    'invalid anchor fields')
            id, reader, segment, tick, at = (row[key] for key in ('id', 'reader', 'segment', 'tick', 'minute'))
            require(token(id) and token(reader) and token(segment) and minute(tick) and minute(at),
                    'invalid calibration anchor')
            found = db.execute('SELECT reader,segment,tick,minute FROM anchor WHERE id=?', (id,)).fetchone()
            require(found is None or found == (reader, segment, tick, at), 'anchor receipt changed')
            if found is None:
                db.execute('INSERT INTO anchor VALUES (?,?,?,?,?)', (id, reader, segment, tick, at))
    return {'anchors': len(rows)}


def _offsets(db, reader):
    """Legal clock corrections per source segment of one reader.

    The legacy fixed-start segment '' keeps correction 0.  Any other segment is
    usable only through its anchors; every correction the anchors allow stays
    legal, and a segment without anchors has no verifiable correction at all.
    """
    anchored = {}
    for segment, tick, at in db.execute('SELECT segment,tick,minute FROM anchor WHERE reader=?', (reader,)):
        anchored.setdefault(segment, set()).add(at - tick)
    result = {}
    for (segment,) in db.execute('SELECT DISTINCT segment FROM sample WHERE reader=?', (reader,)):
        result[segment] = [0] if segment == '' else sorted(anchored.get(segment, ()))
    return result


def _reader_coverage(db, reader, period, start, end):
    expected = list(range(start, end, period))
    samples = db.execute('SELECT segment,tick,id FROM sample WHERE reader=? ORDER BY id', (reader,)).fetchall()
    offsets = _offsets(db, reader)
    usable = {segment: choices for segment, choices in offsets.items() if choices}
    segments = sorted(usable)
    total = 1
    for segment in segments:
        total *= len(usable[segment])
    require(total <= MAX_CALIBRATIONS, 'calibration evidence too ambiguous')
    combos = product(*(usable[segment] for segment in segments)) if segments else ()
    receipts = {at: set() for at in expected}
    standing, witness, first = None, {}, True
    for combo in combos:
        calibration = dict(zip(segments, combo))
        covered = {}
        for segment, tick, sid in samples:
            if segment in calibration:
                covered.setdefault(tick + calibration[segment], []).append(sid)
        hit = {at for at in expected if at in covered}
        standing = hit if standing is None else standing & hit
        for at in hit:
            receipts[at].update(covered[at])
        if first:
            witness, first = covered, False
    standing = standing or set()
    return {'expected': len(expected),
            'missing': [at for at in expected if at not in standing],
            'proof': [min(witness[at]) for at in expected if at in witness],
            'ambiguous': any(len(ids) > 1 for ids in receipts.values()),
            'calibrations': total if segments else 0,
            'receipts': {at: sorted(ids) for at, ids in receipts.items() if ids},
            'segments': {segment: {'offsets': offsets[segment],
                                   'samples': sum(1 for seg, _, _ in samples if seg == segment)}
                         for segment in sorted({seg for seg, _, _ in samples})}}


def coverage(db, station, start, end):
    require(token(station) and minute(start) and minute(end) and 0 < end-start <= 1440, 'invalid inspection span')
    report = {}
    for reader, period in db.execute('SELECT reader,period FROM reader WHERE station=? ORDER BY reader', (station,)):
        report[reader] = _reader_coverage(db, reader, period, start, end)
    require(bool(report), 'station has no registered readers')
    return {'station': station, 'start': start, 'end': end, 'readers': report,
            'state': 'COMPLETE' if all(not row['missing'] for row in report.values()) else 'MISSING'}


def _normalize_bindings(bindings):
    require(isinstance(bindings, dict) and bool(bindings), 'invalid candidate bindings')
    normalized = {}
    for reader, bound in bindings.items():
        require(token(reader) and isinstance(bound, dict) and bool(bound), 'invalid candidate bindings')
        row = {}
        for key, receipt in bound.items():
            at = key
            if isinstance(at, str):
                require(at.isdigit(), 'invalid candidate minute')
                at = int(at)
            require(minute(at) and token(receipt), 'invalid candidate binding')
            row[at] = receipt
        normalized[reader] = row
    return normalized


def _candidate_check(db, station, start, end, bindings):
    """Verify that bindings freeze exactly the required minutes and hold under
    one single legal calibration: no reused receipt, no cross-reader filling,
    and one shared correction per source segment that the anchors allow."""
    registered = dict(db.execute('SELECT reader,period FROM reader WHERE station=?', (station,)).fetchall())
    if not registered:
        return False, 'station has no registered readers'
    if set(bindings) != set(registered):
        return False, 'candidate readers differ from registered readers'
    for reader, period in sorted(registered.items()):
        bound = bindings[reader]
        if set(bound) != set(range(start, end, period)):
            return False, f'candidate minutes differ from required minutes for {reader}'
        receipts = list(bound.values())
        if len(set(receipts)) != len(receipts):
            return False, f'candidate reuses a sample receipt for {reader}'
        placed = {}
        for sid in set(receipts):
            row = db.execute('SELECT reader,segment,tick FROM sample WHERE id=?', (sid,)).fetchone()
            if row is None:
                return False, f'unknown sample receipt {sid}'
            if row[0] != reader:
                return False, f'sample receipt {sid} belongs to another reader'
            placed[sid] = (row[1], row[2])
        offsets = _offsets(db, reader)
        needed = {}
        for at, sid in bound.items():
            segment, tick = placed[sid]
            if not offsets.get(segment):
                return False, f'sample receipt {sid} has no verifiable clock correction'
            needed.setdefault(segment, set()).add(at - tick)
        for segment, corrections in needed.items():
            if len(corrections) != 1:
                return False, f'candidate mixes clock corrections within segment {segment}'
            if not corrections <= set(offsets[segment]):
                return False, f'candidate uses an illegal clock correction for segment {segment}'
    return True, None


def nominate(db, candidate, station, start, end, bindings):
    """Freeze one coherent candidate evidence set for a station window.

    A candidate that cannot hold under a single legal calibration is rejected
    and never stored; retransmitting a frozen candidate with the same content
    keeps the decision unchanged.
    """
    require(token(candidate) and token(station) and minute(start) and minute(end) and 0 < end-start <= 1440,
            'invalid candidate identity or span')
    bindings = _normalize_bindings(bindings)
    ok, reason = _candidate_check(db, station, start, end, bindings)
    if not ok:
        return {'candidate': candidate, 'state': 'REJECTED', 'reason': reason}
    payload = json.dumps(bindings, sort_keys=True)
    with db:
        found = db.execute('SELECT station,start,finish,bindings FROM candidate WHERE id=?', (candidate,)).fetchone()
        require(found is None or found == (station, start, end, payload), 'candidate receipt changed')
        if found is None:
            db.execute('INSERT INTO candidate VALUES (?,?,?,?,?)', (candidate, station, start, end, payload))
    return {'candidate': candidate, 'state': 'FROZEN'}


def released(db, station, start, end):
    """Decide whether a station window may keep backing space-snapshot reservations.

    The window must be COMPLETE under every legal calibration.  If each required
    minute already has one fixed receipt, the coverage report itself is the
    frozen evidence; otherwise one stored candidate that still holds under a
    single legal calibration is required.  The returned detail names the
    affected inspection span and the minutes that lost coverage.
    """
    require(token(station) and minute(start) and minute(end) and 0 < end-start <= 1440, 'invalid inspection span')
    detail = {'station': station, 'start': start, 'end': end}
    if not db.execute('SELECT EXISTS(SELECT 1 FROM reader WHERE station=?)', (station,)).fetchone()[0]:
        return False, {**detail, 'state': 'NO_READERS'}
    report = coverage(db, station, start, end)
    if report['state'] != 'COMPLETE':
        return False, {**detail, 'state': 'MISSING', 'coverage': report}
    if not any(row['ambiguous'] for row in report['readers'].values()):
        return True, {**detail, 'state': 'RELEASED', 'coverage': report}
    for cid, payload in db.execute('SELECT id,bindings FROM candidate WHERE station=? AND start=? AND finish=?'
                                   ' ORDER BY id', (station, start, end)):
        ok, _ = _candidate_check(db, station, start, end, _normalize_bindings(json.loads(payload)))
        if ok:
            return True, {**detail, 'state': 'RELEASED', 'candidate': cid, 'coverage': report}
    return False, {**detail, 'state': 'CANDIDATE_REQUIRED', 'coverage': report}


def checker(db, *stations):
    """Declare the stations whose space snapshots depend on reader inspection.

    Returns the station -> checker mapping expected by supply.Supply; stations
    not declared here keep the plain snapshot-age behaviour.
    """
    require(all(token(station) for station in stations), 'invalid station')
    return {station: (lambda start, end, station=station: released(db, station, start, end))
            for station in stations}
