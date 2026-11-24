"""CLI for the stand-alone station reader commissioning log."""

import argparse
from contextlib import closing
import json
import sqlite3

from . import readers


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
    view = sub.add_parser('coverage')
    view.add_argument('station')
    view.add_argument('start',type=int)
    view.add_argument('end',type=int)
    args = parser.parse_args(argv)
    path = args.db
    parameters = vars(args).copy()
    del parameters['db']
    del parameters['operation']
    try:
        function = {'register':readers.register,'accept':readers.accept,'coverage':readers.coverage}[args.operation]
        if args.operation == 'accept':
            parameters = {'path':args.file}
        with closing(readers.open_reader_log(path)) as db:
            print(json.dumps(function(db, **parameters),sort_keys=True))
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(2,f'inspection: {exc}\n')


if __name__ == '__main__':
    main()
