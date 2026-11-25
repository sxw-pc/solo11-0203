"""CLI for the stand-alone station reader commissioning log."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from . import readers
from .evidence import require, unique_pairs


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    sub = parser.add_subparsers(dest='operation',required=True)
    add = sub.add_parser('register')
    add.add_argument('reader')
    add.add_argument('station')
    add.add_argument('period',type=int)
    receive = sub.add_parser('accept')
    receive.add_argument('file')
    anchor = sub.add_parser('calibrate')
    anchor.add_argument('file')
    freeze = sub.add_parser('nominate')
    freeze.add_argument('file')
    view = sub.add_parser('coverage')
    view.add_argument('station')
    view.add_argument('start',type=int)
    view.add_argument('end',type=int)
    gate = sub.add_parser('release')
    gate.add_argument('station')
    gate.add_argument('start',type=int)
    gate.add_argument('end',type=int)
    args = parser.parse_args(argv)
    try:
        with closing(readers.open_reader_log(args.db)) as db:
            if args.operation == 'register':
                result = readers.register(db, args.reader, args.station, args.period)
            elif args.operation == 'accept':
                result = readers.accept(db, args.file)
            elif args.operation == 'calibrate':
                result = readers.calibrate(db, args.file)
            elif args.operation == 'nominate':
                doc = json.loads(Path(args.file).read_bytes(), object_pairs_hook=unique_pairs)
                require(isinstance(doc, dict) and set(doc) == {'id', 'station', 'start', 'end', 'bindings'},
                        'invalid candidate file')
                result = readers.nominate(db, doc['id'], doc['station'], doc['start'], doc['end'], doc['bindings'])
            elif args.operation == 'coverage':
                result = readers.coverage(db, args.station, args.start, args.end)
            else:
                ok, detail = readers.released(db, args.station, args.start, args.end)
                result = {'released': ok, **detail}
            print(json.dumps(result, sort_keys=True))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2,f'inspection: {exc}\n')


if __name__ == '__main__':
    main()
