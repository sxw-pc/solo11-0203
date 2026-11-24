"""Command-line entry point for the offline evidence desk."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .audit import audit
from .evidence import minute
from .replay import GROUPS, TABLES
from .store import database, history, ingest, recompute, transaction, view

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def clock(text):
    try:
        value = int(text)
    except ValueError:
        value = None
    if not minute(value):
        raise argparse.ArgumentTypeError("expected a nonnegative service minute")
    return value


def parser():
    p = argparse.ArgumentParser(prog="python -m bike_rebalance")
    actions = p.add_subparsers(dest="command", required=True)
    for name in ("init", "ingest", "fleet", "supply", "cage", "status", "history", "demo", "rebuild", "audit"):
        command = actions.add_parser(name)
        command.add_argument("--db", required=True, help="SQLite database path")
        if name in GROUPS:
            command.add_argument("--input", help=f"ingest a JSON file containing only {name} events")
        if name == "ingest":
            command.add_argument("input")
        if name in (*GROUPS, "ingest", "rebuild"):
            command.add_argument("--at", type=clock, help="effective service minute for the current projection")
        if name == "history":
            command.add_argument("entity", choices=TABLES)
            command.add_argument("key")
    return p


def demo(db):
    def summary():
        groups = view(db, TABLES)["entities"]
        return {
            "shared_cage_used": groups["cages"]["C-shared"]["used"],
            "allocation": {key: groups["manifest_lines"][key]["assigned_type"] for key in ("M-cargo/1", "M-flex/1")},
            "movement": {key: groups["manifests"][key]["state"] for key in
                         ("M-cargo", "M-flex", "M-full", "M-check", "M-reuse")},
            "scans": {key: groups["scans"][key]["result"] for key in ("G-cargo-receive", "G-cargo-public")},
            "R-check_vehicle": groups["reservations"]["R-check"]["vehicle"],
            "R-mid": groups["reservations"]["R-mid"]["admission"],
            "R-flex": groups["reservations"]["R-flex"]["admission"],
            "R-dock-bridge": groups["reservations"]["R-dock-bridge"]["admission"],
        }

    if db.execute("SELECT EXISTS (SELECT 1 FROM events)").fetchone()[0]:
        raise ValueError("demo needs a new or empty database")
    ingest(db, FIXTURES / "shift.json", now=40)
    initial = summary()
    ingest(db, FIXTURES / "late.json", now=40)
    return {"status": "DEMO_OK", "before_late_evidence": initial, "after_late_evidence": summary()}


def run(args, db):
    command = args.command
    incoming = getattr(args, "input", None)
    if command == "init":
        return {"status": "INITIALIZED"}
    if command == "demo":
        return demo(db)
    if incoming:
        return ingest(db, incoming, args.at, subsystem=command if command in GROUPS else None)
    if command == "rebuild":
        with transaction(db, write=True):
            return recompute(db, args.at)
    with transaction(db):
        if command == "history":
            return history(db, args.entity, args.key)
        if command == "audit":
            return audit(db)
        return view(db, GROUPS.get(command, TABLES))


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command in GROUPS and args.at is not None and args.input is None:
        p.error("use --input with --at, or use rebuild --at")
    readonly = args.command in ("status", "history", "audit") or (
        args.command in GROUPS and args.input is None)
    try:
        with database(args.db, readonly=readonly) as db:
            result = run(args, db)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=True, sort_keys=True))
    return 1 if args.command == "audit" and result["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
