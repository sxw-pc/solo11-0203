import json
from pathlib import Path
import unittest

from bike_rebalance.packing import pack
from bike_rebalance.replay import rebuild

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def e(kind, key, minute, **payload):
    return {"kind": kind, "id": key, "at": minute, **payload}


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def space(key, at, free):
    return e("space", key, at, station="P-NORTH", free=free)


def reservation(key, at, begin, end, bike=None):
    return e("reservation", key, at, station="P-NORTH", mode="bike" if bike else "dock",
             type=bike, begin=begin, end=end)


def returned(key, at, vehicle, station="P-NORTH", outcome="docked"):
    return e("return", key, at, vehicle=vehicle, station=station, outcome=outcome)


def checked(key, at, vehicle, kind="city", cleared=True):
    return e("check", key, at, vehicle=vehicle, type=kind, cleared=cleared)


class ReplayBusiness(unittest.TestCase):
    def setUp(self):
        self.shift = fixture("shift")
        self.late = fixture("late")

    def test_public_receipts_and_check_conflicts_fail_closed(self):
        events = [returned("a", 1, "V"), returned("b", 1, "V", "OTHER"),
                  checked("c", 1, "V"), checked("d", 1, "V"), checked("e", 1, "V", "cargo"),
                  reservation("r", 2, 2, 4, "city"), returned("f", 3, "V"), checked("g", 3, "V")]
        view = rebuild(events, 4)
        self.assertEqual({view["returns"][k]["result"] for k in ("a", "b")}, {"DOCK_CONFLICT"})
        self.assertEqual({view["checks"][k]["result"] for k in ("c", "d", "e")}, {"CHECK_CONFLICT"})
        self.assertEqual(view["reservations"]["r"]["admission"], "NO_BIKE")
        self.assertEqual(view["offerings"]["P-NORTH"]["reservable_bikes"], {"V": "city"})
        self.assertEqual(rebuild(events[:1], 1)["returns"]["a"]["result"], "DOCKED")
        self.assertEqual(rebuild(events[:1], 1)["offerings"]["P-NORTH"]["reservable_bikes"], {})

    def test_peak_overlap_and_touching_windows(self):
        view = rebuild(self.shift, 16)
        self.assertEqual(view["reservations"]["R-dock-early"]["state"], "EXPIRED")
        self.assertEqual(view["reservations"]["R-dock-late"]["admission"], "ACCEPTED")
        self.assertEqual(view["reservations"]["R-dock-bridge"]["admission"], "ACCEPTED")
        self.assertEqual(view["reservations"]["R-dock-denied"]["admission"], "NO_DOCK")

    def test_stale_snapshot_and_late_conflicting_capacity(self):
        events = [space("one", 0, 1), reservation("fresh", 10, 15, 18), reservation("stale", 11, 18, 20)]
        original = rebuild(events, 11)
        self.assertEqual(original["reservations"]["fresh"]["admission"], "ACCEPTED")
        self.assertEqual(original["reservations"]["stale"]["admission"], "SPACE_STALE")
        changed = rebuild(events + [space("conflicts", 0, 2)], 11)
        self.assertEqual(changed["reservations"]["fresh"]["admission"], "SPACE_CONFLICT")
        self.assertIsNone(changed["offerings"]["P-NORTH"]["reservable_docks_now"])

    def test_pickup_protection_redeem_cancel_and_expiry(self):
        events = [returned("dock-v", 1, "V"), checked("check-v", 1, "V"), space("free", 1, 2),
                  reservation("first", 2, 4, 8, "city"), reservation("repeat", 3, 8, 10, "city"),
                  e("redeem", "early", 3, reservation="first", proof="V"),
                  e("redeem", "wrong", 4, reservation="first", proof="OTHER"),
                  e("cancel", "released", 5, reservation="first"),
                  reservation("second", 6, 6, 10, "city"), checked("blocked", 7, "V", cleared=False),
                  e("redeem", "unsafe", 7, reservation="second", proof="V"), checked("again", 8, "V"),
                  e("redeem", "collected", 8, reservation="second", proof="V"),
                  reservation("third", 9, 9, 10, "city"), reservation("only-dock", 9, 10, 11),
                  e("cancel", "expired-release", 11, reservation="only-dock")]
        result = rebuild(events, 11)
        self.assertEqual(result["reservations"]["repeat"]["admission"], "NO_BIKE")
        self.assertEqual(result["reservation_actions"]["early"]["result"], "OUTSIDE_WINDOW")
        self.assertEqual(result["reservation_actions"]["wrong"]["result"], "PICKUP_UNVERIFIED")
        self.assertEqual(result["reservation_actions"]["unsafe"]["result"], "PICKUP_UNVERIFIED")
        self.assertEqual(rebuild(events, 7)["reservations"]["second"]["state"], "AT_RISK")
        self.assertEqual(result["reservations"]["first"]["state"], "CANCELLED")
        self.assertEqual(result["reservations"]["second"]["state"], "REDEEMED")
        self.assertEqual(result["reservations"]["third"]["admission"], "NO_BIKE")
        self.assertEqual(result["reservations"]["only-dock"]["state"], "EXPIRED")
        self.assertEqual(result["reservation_actions"]["expired-release"]["result"], "NOT_OPEN")
        self.assertEqual(result["vehicles"]["V"]["location"], "ride")
        self.assertEqual(result["offerings"]["P-NORTH"]["free"], 2)

    def test_dropoff_requires_new_public_return_and_cannot_double_redeem(self):
        events = [space("spaces", 0, 2), reservation("r1", 1, 3, 8), reservation("r2", 2, 3, 8),
                  returned("uncertain", 3, "V", outcome="uncertain"),
                  e("redeem", "bad", 4, reservation="r1", proof="uncertain"), returned("good", 5, "V"),
                  e("redeem", "once", 6, reservation="r1", proof="good"),
                  e("redeem", "twice", 7, reservation="r2", proof="good")]
        result = rebuild(events, 7)
        self.assertEqual(result["reservation_actions"]["bad"]["result"], "DOCK_UNVERIFIED")
        self.assertEqual(result["reservations"]["r1"]["state"], "REDEEMED")
        self.assertEqual(result["reservation_actions"]["twice"]["result"], "DOCK_UNVERIFIED")
        self.assertEqual(result["offerings"]["P-NORTH"]["free"], 1)
        self.assertEqual(result["offerings"]["P-NORTH"]["reservable_docks_now"], 0)

    def test_reconfirmed_public_position_does_not_consume_a_second_dock(self):
        events = [space("space", 0, 3), returned("initial", 1, "V"),
                  reservation("dock", 2, 2, 8), returned("unclear", 3, "V", outcome="uncertain"),
                  returned("confirmed-again", 4, "V"), returned("duplicate", 5, "V"),
                  e("redeem", "no-new-dock", 6, reservation="dock", proof="confirmed-again")]
        result = rebuild(events, 6)
        self.assertEqual(result["returns"]["confirmed-again"]["result"], "DOCKED")
        self.assertFalse(result["returns"]["confirmed-again"]["consumes_dock"])
        self.assertEqual(result["returns"]["duplicate"]["result"], "ALREADY_PARKED")
        self.assertEqual(result["offerings"]["P-NORTH"]["free"], 2)
        self.assertEqual(result["reservation_actions"]["no-new-dock"]["result"], "DOCK_UNVERIFIED")

    def test_loading_rechecks_type_and_rejects_wrong_origin_evidence(self):
        def extra_scan(key, at, site):
            return e("scan", key, at, manifest="M-cargo", vehicle="V-B", stage="load", channel="gate", site=site)

        events = self.shift + [extra_scan("early", 9, "G-NORTH"), extra_scan("wrong", 10, "P-NORTH"),
                               checked("cargo-changed", 10, "V-B", "electric"), extra_scan("changed", 11, "G-NORTH")]
        result = rebuild(events, 11)
        self.assertEqual(result["scans"]["early"]["result"], "WRONG_ORIGIN_GATE_OR_TIME")
        self.assertEqual(result["scans"]["wrong"]["result"], "WRONG_ORIGIN_GATE_OR_TIME")
        self.assertEqual(result["scans"]["changed"]["result"], "LOAD_INELIGIBLE")
        self.assertEqual(result["vehicles"]["V-B"]["location"], "public")
        self.assertEqual(result["manifest_lines"]["M-cargo/1"]["state"], "PLANNED")

    def test_shared_capacity_and_complete_packing(self):
        early = rebuild(self.shift, 9)
        self.assertEqual(early["cages"]["C-shared"]["used"], 3)
        self.assertEqual(early["manifest_lines"]["M-cargo/1"]["vehicle"], "V-B")
        line = early["manifest_lines"]["M-flex/1"]
        self.assertEqual((line["requested_type"], line["assigned_type"], line["slots"]), ("cargo", "electric", 1))
        self.assertEqual(early["manifests"]["M-full"]["state"], "NO_CAGE_SPACE")
        self.assertFalse(early["manifests"]["M-full"]["lines"])
        self.assertEqual(early["reservations"]["R-flex"]["admission"], "NO_BIKE")
        without_flex = rebuild((event for event in self.shift if event["id"] != "M-flex"), 9)
        self.assertEqual(without_flex["reservations"]["R-flex"]["vehicle"], "V-C")
        available = {"B": "cargo", "C": "electric"}
        self.assertEqual(pack([["cargo", "electric"], ["cargo"]], available, 3), ("C", "B"))
        self.assertIsNone(pack([["cargo", "electric"], ["cargo"]], available, 2))

    def test_qualification_reallocates_reservation_and_denies_manifest(self):
        before = rebuild(self.shift, 9)
        after = rebuild(self.shift + self.late, 9)
        self.assertEqual(before["reservations"]["R-check"]["vehicle"], "V-D")
        self.assertEqual(after["reservations"]["R-check"]["vehicle"], "V-E")
        self.assertEqual(before["manifest_lines"]["M-check/1"]["vehicle"], "V-E")
        self.assertEqual(after["manifests"]["M-check"]["state"], "NO_FLEET")

    def test_causal_scans_reopen_cage_without_counting_warehouse_as_dock(self):
        before = rebuild(self.shift, 24)
        after = rebuild(self.shift + self.late, 24)
        self.assertEqual(before["scans"]["G-cargo-receive"]["result"], "MISSING_ORIGIN")
        self.assertEqual(after["scans"]["G-cargo-load"]["result"], "ORIGIN_LATE")
        self.assertEqual(after["scans"]["G-cargo-public"]["result"], "PUBLIC_IS_NOT_GATE")
        self.assertEqual(after["scans"]["G-flex-wrong"]["result"], "WRONG_DESTINATION_GATE")
        self.assertEqual(after["returns"]["T-cargo-public"]["result"], "IN_CUSTODY")
        self.assertEqual(after["manifests"]["M-cargo"]["state"], "COMPLETE_ON_TIME")
        self.assertEqual(after["manifests"]["M-flex"]["state"], "COMPLETE_LATE")
        self.assertEqual(after["manifest_lines"]["M-cargo/1"]["destination_proof"], "G-cargo-receive")
        self.assertEqual(after["vehicles"]["V-B"]["location"], "warehouse")
        self.assertNotIn("D-EAST", after["offerings"])
        self.assertEqual(before["manifests"]["M-reuse"]["state"], "CAGE_BUSY_OTHER_TRIP")
        self.assertEqual(after["manifests"]["M-reuse"]["state"], "ALLOCATED")
        self.assertEqual(before["reservations"]["R-mid"]["state"], "HELD")
        self.assertEqual(after["reservations"]["R-mid"]["state"], "NO_BIKE")
        self.assertEqual(rebuild(self.shift + self.late, 40)["manifests"]["M-reuse"]["state"], "VOID")

    def test_void_is_blocked_after_load_and_noncausal_arrival_cannot_release(self):
        extra = [e("void", "void", 12, manifest="M-flex"),
                 e("scan", "same-minute", 10, manifest="M-flex", vehicle="V-C", stage="unload",
                   channel="gate", site="D-EAST")]
        result = rebuild(self.shift + extra, 21)
        self.assertEqual(result["voids"]["void"]["result"], "CUSTODY_STARTED")
        self.assertEqual(result["scans"]["same-minute"]["result"], "NONCAUSAL_UNLOAD")
        self.assertEqual(result["manifest_lines"]["M-flex/1"]["state"], "LOADED")
        self.assertEqual(result["cages"]["C-shared"]["used"], 3)

    def test_unordered_intake_has_same_current_projection(self):
        all_events = self.shift + self.late
        self.assertEqual(rebuild(all_events, 40), rebuild(reversed(all_events), 40))
