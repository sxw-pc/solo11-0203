import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from bike_rebalance.audit import audit
from bike_rebalance.replay import TABLES
from bike_rebalance.store import database, history, ingest, recompute, transaction, view

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


class Persistence(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.filename = self.folder / "verify.sqlite"

    def write_batch(self, name, data):
        target = self.folder / name
        target.write_text(json.dumps(data), encoding="utf-8")
        return target

    def cli(self, action, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "bike_rebalance", action,
                               "--db", str(self.filename), *arguments], cwd=ROOT,
                              capture_output=True, text=True, check=False)

    def test_replay_retains_revised_history_and_unchanged_rerun(self):
        with database(self.filename) as db:
            ingest(db, FIXTURES / "shift.json", now=40)
            ingest(db, FIXTURES / "late.json", now=40)
            original = view(db, TABLES)
            records = history(db, "manifests", "M-cargo")
            self.assertEqual([item["after"]["state"] for item in records], ["OVERDUE", "COMPLETE_ON_TIME"])
            self.assertEqual(ingest(db, FIXTURES / "late.json")["changed"], 0)
            with transaction(db, write=True):
                recompute(db, 6)
            self.assertNotIn("M-reuse", view(db, TABLES)["entities"]["manifests"])
            with transaction(db, write=True):
                recompute(db, 40)
            self.assertEqual(view(db, TABLES), original)
            self.assertEqual(audit(db)["status"], "AUDIT_OK")
            self.assertEqual(audit(db)["batches"], 2)

    def test_id_conflict_rolls_back_raw_rows_and_canonical_projection(self):
        extra = [{"id": "new", "kind": "space", "at": 1, "station": "P-NORTH", "free": 1},
                 {"id": "S-opening", "kind": "space", "at": 0, "station": "P-NORTH", "free": 9}]
        with database(self.filename) as db:
            ingest(db, FIXTURES / "shift.json", now=40)
            before = audit(db)
            with self.assertRaisesRegex(ValueError, "conflicting immutable event id"):
                ingest(db, self.write_batch("collision.json", extra))
            self.assertEqual(audit(db), before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)

    def test_invalid_input_has_no_committed_batch(self):
        bad = self.folder / "bad.json"
        bad.write_text('[{"id":"S","id":"T","at":0,"kind":"space","station":"P","free":1}]', encoding="utf-8")
        failed = self.cli("ingest", str(bad))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("duplicate JSON key", failed.stderr)
        self.assertEqual(json.loads(self.cli("audit").stdout)["batches"], 0)

    def test_audit_reports_projection_and_historical_drift(self):
        self.assertEqual(self.cli("demo").returncode, 0)
        with database(self.filename) as db:
            db.execute("UPDATE manifests SET data = '{}' WHERE key = 'M-cargo'")
            db.execute("DELETE FROM history WHERE entity = 'scans' AND key = 'G-cargo-receive'")
        result = self.cli("audit")
        self.assertEqual(result.returncode, 1)
        issues = json.loads(result.stdout)["issues"]
        self.assertIn("current projection differs from raw replay", issues)
        self.assertTrue(any(issue.startswith("history mismatch") for issue in issues))

    def test_audit_compares_hash_index_and_raw_file_membership(self):
        with database(self.filename) as db:
            ingest(db, FIXTURES / "late.json")
            db.execute("DELETE FROM batch_events WHERE event_id = 'Q-D-stop'")
            payload = db.execute("SELECT raw FROM batches").fetchone()[0]
            db.execute("UPDATE batches SET raw = ?", (payload + b' ',))
            issues = audit(db)["issues"]
            self.assertTrue(any(issue.startswith("batch event membership mismatch") for issue in issues))
            self.assertTrue(any(issue.startswith("raw hash mismatch") for issue in issues))

    def test_subsystems_accept_own_evidence_and_refuse_foreign_input(self):
        fleet = self.write_batch("fleet.json", [{"kind": "return", "id": "t1", "at": 1,
                                                 "vehicle": "V", "station": "P", "outcome": "docked"}])
        supply = self.write_batch("supply.json", [{"kind": "space", "id": "s1", "at": 2,
                                                     "station": "P", "free": 1}])
        cage = self.write_batch("cage.json", [{"kind": "cage", "id": "c1", "at": 1, "slots": 2}])
        rejected = self.cli("fleet", "--input", str(supply))
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("belongs to supply", rejected.stderr)
        for group, file, table, key in (("fleet", fleet, "returns", "t1"), ("supply", supply, "snapshots", "s1"),
                                        ("cage", cage, "cages", "c1")):
            with self.subTest(group=group):
                result = self.cli(group, "--input", str(file))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(key, json.loads(self.cli(group).stdout)["entities"][table])
        self.assertEqual(json.loads(self.cli("audit").stdout)["status"], "AUDIT_OK")
