import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from bike_rebalance.replay import rebuild

ROOT = Path(__file__).resolve().parents[1]


def e(kind, key, minute, **payload):
    return {"kind": kind, "id": key, "at": minute, **payload}


def returned(key, at, vehicle, station="P-NORTH"):
    return e("return", key, at, vehicle=vehicle, station=station, outcome="docked")


def checked(key, at, vehicle, kind="city", cleared=True):
    return e("check", key, at, vehicle=vehicle, type=kind, cleared=cleared)


def load_scan(key, at, manifest, vehicle, gate="G-NORTH"):
    return e("scan", key, at, manifest=manifest, vehicle=vehicle,
             stage="load", channel="gate", site=gate)


def unload_scan(key, at, manifest, vehicle, site):
    return e("scan", key, at, manifest=manifest, vehicle=vehicle,
             stage="unload", channel="gate", site=site)


def redirect(key, at, manifest, vehicles, sender, target):
    return e("redirect", key, at, manifest=manifest, vehicles=vehicles,
             from_depot=sender, to_depot=target)


def two_bike_shift():
    """Two eligible city bikes, both planned and loaded onto one 2-slot cage."""
    return [
        e("space", "S0", 0, station="P-NORTH", free=5),
        e("cage", "C2", 0, slots=2),
        returned("T1", 1, "V-1"), checked("Q1", 1, "V-1"),
        returned("T2", 1, "V-2"), checked("Q2", 1, "V-2"),
        e("manifest", "M2", 4, cage="C2", origin="P-NORTH", gate="G-NORTH",
          depot="D-EAST", depart=10, due=25, items=[["city"], ["city"]]),
        load_scan("L1", 10, "M2", "V-1"),
        load_scan("L2", 10, "M2", "V-2"),
    ]


class DiversionReceipts(unittest.TestCase):
    def test_one_bike_diverted_other_still_received_by_original_depot(self):
        events = two_bike_shift() + [
            # Backup depot scans before any diversion receipt: cannot end custody.
            unload_scan("early-backup", 11, "M2", "V-1", "D-WEST"),
            redirect("RD1", 12, "M2", ["V-1"], "D-EAST", "D-WEST"),
            unload_scan("backup-ok", 13, "M2", "V-1", "D-WEST"),
            unload_scan("original-ok", 14, "M2", "V-2", "D-EAST"),
            # The original depot can no longer sign for the diverted bike.
            unload_scan("original-late-try", 15, "M2", "V-1", "D-EAST"),
        ]
        view = rebuild(events, 16)

        self.assertEqual(view["scans"]["early-backup"]["result"], "WRONG_DESTINATION_GATE")
        self.assertEqual(view["scans"]["early-backup"]["responsible_depot"], "D-EAST")
        self.assertEqual(view["redirects"]["RD1"]["result"], "REDIRECTED")
        self.assertEqual(view["redirects"]["RD1"]["transferred"], ["V-1"])
        self.assertEqual(view["scans"]["backup-ok"]["result"], "UNLOADED")
        self.assertEqual(view["scans"]["backup-ok"]["responsible_depot"], "D-WEST")
        self.assertEqual(view["scans"]["original-ok"]["result"], "UNLOADED")
        self.assertEqual(view["scans"]["original-ok"]["responsible_depot"], "D-EAST")
        self.assertEqual(view["scans"]["original-late-try"]["result"], "GATE_NOT_RESPONSIBLE")
        self.assertEqual(view["scans"]["original-late-try"]["responsible_depot"], "D-WEST")

        line1, line2 = view["manifest_lines"]["M2/1"], view["manifest_lines"]["M2/2"]
        self.assertEqual(line1["vehicle"], "V-1")
        self.assertEqual(line1["receiver"], "D-WEST")
        self.assertEqual(line1["destination_proof"], "backup-ok")
        self.assertEqual(line1["redirects"], [
            {"receipt": "RD1", "at": 12, "from_depot": "D-EAST", "to_depot": "D-WEST"}])
        self.assertEqual(line2["receiver"], "D-EAST")
        self.assertEqual(line2["destination_proof"], "original-ok")
        self.assertEqual(line2["redirects"], [])

        self.assertEqual(view["vehicles"]["V-1"]["location"], "warehouse")
        self.assertEqual(view["vehicles"]["V-1"]["site"], "D-WEST")
        self.assertEqual(view["vehicles"]["V-2"]["site"], "D-EAST")
        self.assertEqual(view["manifests"]["M2"]["state"], "COMPLETE_ON_TIME")

    def test_invalid_pending_or_unsigned_scans_never_release_cage_slots(self):
        events = two_bike_shift() + [
            unload_scan("early-backup", 11, "M2", "V-1", "D-WEST"),
            redirect("RD1", 12, "M2", ["V-1"], "D-EAST", "D-WEST"),
        ]
        # Before the valid backup sign-off the diverted bike still holds its slot.
        waiting = rebuild(events, 12)
        self.assertEqual(waiting["cages"]["C2"]["used"], 2)
        self.assertEqual(waiting["vehicles"]["V-1"]["location"], "transit")

        events.append(unload_scan("backup-ok", 13, "M2", "V-1", "D-WEST"))
        view = rebuild(events, 13)
        # Only V-1's slot is released; V-2 remains physically caged.
        self.assertEqual(view["cages"]["C2"]["used"], 1)
        self.assertEqual(view["vehicles"]["V-1"]["location"], "warehouse")
        self.assertEqual(view["vehicles"]["V-2"]["location"], "transit")

        # A non-causal or duplicate sign-off cannot release anything either.
        more = events + [
            unload_scan("same-minute", 10, "M2", "V-2", "D-EAST"),
            unload_scan("duplicate", 14, "M2", "V-1", "D-WEST"),
        ]
        result = rebuild(more, 14)
        self.assertEqual(result["scans"]["same-minute"]["result"], "NONCAUSAL_UNLOAD")
        self.assertEqual(result["scans"]["duplicate"]["result"], "DUPLICATE_UNLOAD")
        self.assertEqual(result["cages"]["C2"]["used"], 1)

    def _three_bike_shift(self):
        events = [
            e("space", "S0", 0, station="P-NORTH", free=6),
            e("cage", "C3", 0, slots=3),
            *[returned(f"T{i}", 1, f"V-{i}") for i in (3, 4, 5)],
            *[checked(f"Q{i}", 1, f"V-{i}") for i in (3, 4, 5)],
            e("manifest", "M3", 4, cage="C3", origin="P-NORTH", gate="G-NORTH",
              depot="D-EAST", depart=8, due=25,
              items=[["city"], ["city"], ["city"]]),
            load_scan("L3", 8, "M3", "V-3"),
            load_scan("L4", 8, "M3", "V-4"),
            load_scan("L5", 8, "M3", "V-5"),
        ]
        return events

    def test_overlapping_same_minute_instructions_keep_one_authoritative_depot(self):
        events = self._three_bike_shift() + [
            # Two same-minute receipts from the same responsible depot, targets
            # overlap on V-4, each also names a distinct other vehicle.
            redirect("RD-A", 10, "M3", ["V-3", "V-4"], "D-EAST", "D-WEST"),
            redirect("RD-B", 10, "M3", ["V-4", "V-5"], "D-EAST", "D-NORTH"),
            # Immediate scans at both candidate depots.
            unload_scan("G-west", 10, "M3", "V-4", "D-WEST"),
            unload_scan("G-north", 10, "M3", "V-4", "D-NORTH"),
            unload_scan("G-west-v3", 10, "M3", "V-3", "D-WEST"),
            unload_scan("G-north-v5", 10, "M3", "V-5", "D-NORTH"),
        ]
        view = rebuild(events, 10)

        # Both receipts are authorized against the identical pre-minute state
        # (D-EAST responsible for all three). Event id order breaks the overlap:
        # RD-A claims V-4 first and wins; RD-B fails atomically, touching neither.
        self.assertEqual(view["redirects"]["RD-A"]["result"], "REDIRECTED")
        self.assertEqual(set(view["redirects"]["RD-A"]["transferred"]), {"V-3", "V-4"})
        self.assertEqual(view["redirects"]["RD-B"]["result"], "REDIRECT_CONFLICT")
        self.assertEqual(view["redirects"]["RD-B"]["failures"], [
            {"vehicle": "V-4", "reason": "REDIRECT_CONFLICT", "winning_receipt": "RD-A"}])

        receivers = {view["manifest_lines"][f"M3/{i}"]["vehicle"]:
                     view["manifest_lines"][f"M3/{i}"]["receiver"] for i in (1, 2, 3)}
        self.assertEqual(receivers, {"V-3": "D-WEST", "V-4": "D-WEST", "V-5": "D-EAST"})

        self.assertEqual(view["scans"]["G-west"]["result"], "UNLOADED")
        self.assertEqual(view["scans"]["G-north"]["result"], "GATE_NOT_RESPONSIBLE")
        self.assertEqual(view["scans"]["G-west-v3"]["result"], "UNLOADED")
        # V-5 was never diverted: its line carries no receipt chain, so a foreign
        # gate keeps the legacy wrong-site verdict while the field still names the
        # single depot authorized to sign.
        self.assertEqual(view["scans"]["G-north-v5"]["result"], "WRONG_DESTINATION_GATE")
        self.assertEqual(view["scans"]["G-north-v5"]["responsible_depot"], "D-EAST")
        # Exactly one depot could sign for each vehicle; two slots released.
        self.assertEqual(view["cages"]["C3"]["used"], 1)
        self.assertEqual(view["vehicles"]["V-4"]["site"], "D-WEST")
        self.assertEqual(view["vehicles"]["V-5"]["location"], "transit")

    def test_late_earlier_effective_evidence_re_adjudicates_on_replay(self):
        # Independent timeline: all three bikes are already in custody by minute
        # 8, so a receipt effective at minute 9 can legitimately move V-4.
        events = [
            e("space", "S0", 0, station="P-NORTH", free=6),
            e("cage", "C3", 0, slots=3),
            *[returned(f"T{i}", 1, f"V-{i}") for i in (3, 4, 5)],
            *[checked(f"Q{i}", 1, f"V-{i}") for i in (3, 4, 5)],
            e("manifest", "M3", 4, cage="C3", origin="P-NORTH", gate="G-NORTH",
              depot="D-EAST", depart=8, due=25, items=[["city"], ["city"], ["city"]]),
            load_scan("L3", 8, "M3", "V-3"),
            load_scan("L4", 8, "M3", "V-4"),
            load_scan("L5", 8, "M3", "V-5"),
            redirect("RD-A", 10, "M3", ["V-3", "V-4"], "D-EAST", "D-WEST"),
            redirect("RD-B", 10, "M3", ["V-4", "V-5"], "D-EAST", "D-NORTH"),
            unload_scan("G-west", 10, "M3", "V-4", "D-WEST"),
            unload_scan("G-north", 10, "M3", "V-4", "D-NORTH"),
        ]
        before = rebuild(events, 10)
        self.assertEqual(before["redirects"]["RD-A"]["result"], "REDIRECTED")
        self.assertEqual(before["redirects"]["RD-B"]["result"], "REDIRECT_CONFLICT")
        self.assertEqual(before["scans"]["G-west"]["result"], "UNLOADED")
        self.assertEqual(before["scans"]["G-north"]["result"], "GATE_NOT_RESPONSIBLE")

        # A late-arriving receipt whose effective minute (9) predates the contest:
        # V-4 already belonged to D-NORTH before minute 10, so neither same-minute
        # instruction (both issued by D-EAST) still holds authority over it; each
        # batch fails atomically and the same scans flip to the opposite verdicts.
        late = redirect("RD-EARLY", 9, "M3", ["V-4"], "D-EAST", "D-NORTH")
        after = rebuild(events + [late], 10)
        self.assertEqual(after["redirects"]["RD-EARLY"]["result"], "REDIRECTED")
        self.assertEqual(after["redirects"]["RD-A"]["result"], "REDIRECT_REJECTED")
        self.assertEqual(after["redirects"]["RD-A"]["failures"], [
            {"vehicle": "V-4", "reason": "NOT_RESPONSIBLE_ISSUER"}])
        self.assertEqual(after["redirects"]["RD-B"]["result"], "REDIRECT_REJECTED")
        self.assertEqual(after["redirects"]["RD-B"]["failures"], [
            {"vehicle": "V-4", "reason": "NOT_RESPONSIBLE_ISSUER"}])
        line4 = next(line for line in after["manifest_lines"].values()
                     if line["vehicle"] == "V-4")
        self.assertEqual(line4["receiver"], "D-NORTH")
        self.assertEqual([r["receipt"] for r in line4["redirects"]], ["RD-EARLY"])
        self.assertEqual(after["scans"]["G-west"]["result"], "GATE_NOT_RESPONSIBLE")
        self.assertEqual(after["scans"]["G-north"]["result"], "UNLOADED")
        self.assertEqual(after["vehicles"]["V-4"]["site"], "D-NORTH")
        self.assertEqual(after["vehicles"]["V-3"]["location"], "transit")
        self.assertEqual(after["vehicles"]["V-5"]["location"], "transit")
        # Replay order independence still holds with the late evidence present.
        self.assertEqual(rebuild(events + [late], 10),
                         rebuild(reversed(events + [late]), 10))

    def test_batch_receipt_is_atomic_and_issuer_must_hold_responsibility(self):
        unloaded_first = two_bike_shift() + [
            unload_scan("u1", 11, "M2", "V-1", "D-EAST"),
            # Batch names one already-received bike and one caged bike: nothing moves.
            redirect("RD-bad", 12, "M2", ["V-1", "V-2"], "D-EAST", "D-WEST"),
        ]
        view = rebuild(unloaded_first, 12)
        self.assertEqual(view["redirects"]["RD-bad"]["result"], "REDIRECT_REJECTED")
        self.assertEqual(view["redirects"]["RD-bad"]["failures"], [
            {"vehicle": "V-1", "reason": "ALREADY_RECEIVED"}])
        self.assertEqual(view["manifest_lines"]["M2/2"]["receiver"], "D-EAST")

        # The backup depot cannot issue a receipt before it holds responsibility.
        foreign = two_bike_shift() + [
            redirect("RD-foreign", 12, "M2", ["V-1"], "D-WEST", "D-NORTH")]
        foreign_view = rebuild(foreign, 12)
        self.assertEqual(foreign_view["redirects"]["RD-foreign"]["result"], "REDIRECT_REJECTED")
        self.assertEqual(foreign_view["redirects"]["RD-foreign"]["failures"], [
            {"vehicle": "V-1", "reason": "NOT_RESPONSIBLE_ISSUER"}])

        # Chained diversion: only the current receiver can move the bike again.
        chained = two_bike_shift() + [
            redirect("RD-1", 12, "M2", ["V-1"], "D-EAST", "D-WEST"),
            redirect("RD-2-stale", 13, "M2", ["V-1"], "D-EAST", "D-NORTH"),
            redirect("RD-2", 13, "M2", ["V-1"], "D-WEST", "D-NORTH"),
        ]
        chained_view = rebuild(chained, 13)
        self.assertEqual(chained_view["redirects"]["RD-1"]["result"], "REDIRECTED")
        self.assertEqual(chained_view["redirects"]["RD-2-stale"]["result"], "REDIRECT_REJECTED")
        self.assertEqual(chained_view["redirects"]["RD-2"]["result"], "REDIRECTED")
        self.assertEqual(chained_view["manifest_lines"]["M2/1"]["receiver"], "D-NORTH")

    def test_same_minute_load_cannot_be_diverted_without_prior_custody(self):
        events = [
            e("space", "S0", 0, station="P-NORTH", free=5),
            e("cage", "C2", 0, slots=2),
            returned("T1", 1, "V-1"), checked("Q1", 1, "V-1"),
            returned("T2", 1, "V-2"), checked("Q2", 1, "V-2"),
            e("manifest", "M2", 4, cage="C2", origin="P-NORTH", gate="G-NORTH",
              depot="D-EAST", depart=10, due=25, items=[["city"], ["city"]]),
            load_scan("L1", 10, "M2", "V-1"),
            # V-2 is still PLANNED at minute 10 adjudication.
            redirect("RD-same", 10, "M2", ["V-1", "V-2"], "D-EAST", "D-WEST"),
            load_scan("L2", 11, "M2", "V-2"),
        ]
        view = rebuild(events, 11)
        self.assertEqual(view["redirects"]["RD-same"]["result"], "REDIRECT_REJECTED")
        self.assertEqual({f["vehicle"]: f["reason"]
                          for f in view["redirects"]["RD-same"]["failures"]},
                         {"V-2": "NOT_LOADED"})
        self.assertEqual(view["manifest_lines"]["M2/1"]["receiver"], "D-EAST")

    def test_legacy_inputs_without_redirect_keep_original_scan_verdicts(self):
        # Original fixtures contain no redirect receipts: every verdict and the
        # late-arrival interpretation must remain byte-for-byte identical.
        shift = json.loads((ROOT / "fixtures" / "shift.json").read_text(encoding="utf-8"))
        late = json.loads((ROOT / "fixtures" / "late.json").read_text(encoding="utf-8"))
        view = rebuild(shift + late, 40)
        self.assertEqual(view["redirects"], {})
        self.assertEqual(view["scans"]["G-cargo-receive"]["result"], "UNLOADED")
        self.assertEqual(view["scans"]["G-cargo-receive"]["responsible_depot"], "D-EAST")
        self.assertEqual(view["scans"]["G-flex-wrong"]["result"], "WRONG_DESTINATION_GATE")
        self.assertEqual(view["scans"]["G-flex-receive"]["result"], "WAREHOUSE_LATE")
        self.assertEqual(view["manifests"]["M-flex"]["state"], "COMPLETE_LATE")
        self.assertEqual(view["manifest_lines"]["M-cargo/1"]["receiver"], "D-EAST")
        self.assertEqual(view["manifest_lines"]["M-cargo/1"]["redirects"], [])


class DiversionPersistence(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.filename = self.folder / "diversion.sqlite"

    def write_batch(self, name, data):
        target = self.folder / name
        target.write_text(json.dumps(data), encoding="utf-8")
        return target

    def cli(self, action, *arguments):
        return subprocess.run([sys.executable, "-B", "-m", "bike_rebalance", action,
                               "--db", str(self.filename), *arguments], cwd=ROOT,
                              capture_output=True, text=True, check=False)

    def test_late_evidence_flips_projection_and_recompute_audit_agree(self):
        from bike_rebalance.audit import audit
        from bike_rebalance.store import database, ingest, recompute, transaction, view
        from bike_rebalance.replay import TABLES

        base = [
            e("space", "S0", 0, station="P-NORTH", free=6),
            e("cage", "C3", 0, slots=3),
            *[returned(f"T{i}", 1, f"V-{i}") for i in (3, 4, 5)],
            *[checked(f"Q{i}", 1, f"V-{i}") for i in (3, 4, 5)],
            e("manifest", "M3", 4, cage="C3", origin="P-NORTH", gate="G-NORTH",
              depot="D-EAST", depart=8, due=25, items=[["city"], ["city"], ["city"]]),
            load_scan("L3", 8, "M3", "V-3"),
            load_scan("L4", 8, "M3", "V-4"),
            load_scan("L5", 8, "M3", "V-5"),
            redirect("RD-A", 10, "M3", ["V-3", "V-4"], "D-EAST", "D-WEST"),
            redirect("RD-B", 10, "M3", ["V-4", "V-5"], "D-EAST", "D-NORTH"),
            unload_scan("G-west", 10, "M3", "V-4", "D-WEST"),
            unload_scan("G-north", 10, "M3", "V-4", "D-NORTH"),
        ]
        late = [redirect("RD-EARLY", 9, "M3", ["V-4"], "D-EAST", "D-NORTH")]
        with database(self.filename) as db:
            ingest(db, self.write_batch("base.json", base), now=10)
            first = view(db, TABLES)["entities"]
            self.assertEqual(first["redirects"]["RD-B"]["result"], "REDIRECT_CONFLICT")
            self.assertEqual(first["scans"]["G-west"]["result"], "UNLOADED")
            ingest(db, self.write_batch("late.json", late), now=10)
            second = view(db, TABLES)["entities"]
            self.assertEqual(second["redirects"]["RD-A"]["result"], "REDIRECT_REJECTED")
            self.assertEqual(second["redirects"]["RD-B"]["result"], "REDIRECT_REJECTED")
            self.assertEqual(second["scans"]["G-north"]["result"], "UNLOADED")
            self.assertEqual(second["scans"]["G-west"]["result"], "GATE_NOT_RESPONSIBLE")
            with transaction(db, write=True):
                recompute(db, 9)
            early = view(db, TABLES)["entities"]
            self.assertEqual(early["redirects"]["RD-EARLY"]["result"], "REDIRECTED")
            self.assertNotIn("RD-A", early["redirects"])
            with transaction(db, write=True):
                recompute(db, 10)
            self.assertEqual(view(db, TABLES)["entities"], second)
            self.assertEqual(audit(db)["status"], "AUDIT_OK")

    def test_invalid_redirect_batches_are_rejected(self):
        bad_cases = {
            "empty.json": [redirect("R0", 5, "M", [], "D-EAST", "D-WEST")],
            "same.json": [redirect("R1", 5, "M", ["V-1", "V-1"], "D-EAST", "D-WEST")],
            "depot.json": [redirect("R2", 5, "M", ["V-1"], "D-EAST", "D-EAST")],
        }
        for name, data in bad_cases.items():
            failed = self.cli("ingest", str(self.write_batch(name, data)))
            with self.subTest(name=name):
                self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertEqual(json.loads(self.cli("audit").stdout)["batches"], 0)
