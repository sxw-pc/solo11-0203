"""Public dock returns, vehicle type/clearance, and observed location."""
from collections import defaultdict


class Fleet:
    def __init__(self, events):
        self.vehicles = {}
        self.returns = {}
        self.checks = {}
        self.return_groups = defaultdict(set)
        self.check_groups = defaultdict(set)
        for event in events:
            if event["kind"] == "check":
                self.check_groups[event["vehicle"], event["at"]].add((event["type"], event["cleared"]))
            if event["kind"] == "return":
                self.return_groups[event["vehicle"], event["at"]].add(
                    (event["station"], event["outcome"]))

    def vehicle(self, key):
        return self.vehicles.setdefault(key, {"location": "unknown", "site": None, "position_proof": None,
                                              "type": None, "cleared": False, "check_proof": None,
                                              "last_public_dock": None})

    def observe_check(self, e):
        vehicle = self.vehicle(e["vehicle"])
        group = self.check_groups[e["vehicle"], e["at"]]
        conflict = len(group) > 1
        if conflict:
            verdict = "CHECK_CONFLICT"
        elif vehicle.get("check_at") == e["at"]:
            verdict = "CORROBORATED"
        else:
            verdict = "CLEARED" if e["cleared"] else "QUARANTINED"
        if vehicle.get("check_at") != e["at"]:
            vehicle.update(type=None if conflict else e["type"], cleared=e["cleared"] and not conflict,
                           check_at=e["at"], check_proof=e["id"], check_conflict=conflict)
        self.checks[e["id"]] = {**e, "result": verdict}

    def observe_return(self, e):
        vehicle = self.vehicle(e["vehicle"])
        group = self.return_groups[e["vehicle"], e["at"]]
        consumes_dock = False
        if vehicle["location"] == "transit":
            verdict = "IN_CUSTODY"
        elif len(group) > 1:
            self.place(e["vehicle"], "unconfirmed", None, e["id"])
            verdict = "DOCK_CONFLICT"
        elif e["outcome"] == "uncertain":
            self.place(e["vehicle"], "unconfirmed", e["station"], e["id"])
            verdict = "DOCK_UNCERTAIN"
        elif vehicle["location"] == "public" and vehicle["site"] == e["station"]:
            verdict = "ALREADY_PARKED"
        else:
            consumes_dock = vehicle["last_public_dock"] != e["station"]
            self.place(e["vehicle"], "public", e["station"], e["id"])
            verdict = "DOCKED"
        self.returns[e["id"]] = {**e, "result": verdict, "consumes_dock": consumes_dock}

    def place(self, key, location, site, proof):
        vehicle = self.vehicle(key)
        vehicle.update(location=location, site=site, position_proof=proof)
        if location != "unconfirmed":
            vehicle["last_public_dock"] = site if location == "public" else None

    def begin_receipt(self, key, depot, proof):
        """Authorize a warehouse sign-off only against traceable transit custody.

        A gate may end the rebalance custody of a vehicle only while the vehicle
        is actually in transit under a valid load proof; the caller then places
        it at the warehouse. Returns False for anything not in custody, so no
        un-traceable sign-off can release a cage slot.
        """
        vehicle = self.vehicles.get(key)
        if vehicle is None or vehicle["location"] != "transit" or vehicle["position_proof"] is None:
            return False
        vehicle["receiving_depot"] = depot
        vehicle["receipt_proof"] = proof
        return True

    def eligible(self, key, station, vehicle_type):
        v = self.vehicles[key]
        return v["location"] == "public" and v["site"] == station and v["cleared"] and v["type"] == vehicle_type

    def available(self, station, excluded):
        return {key: v["type"] for key, v in sorted(self.vehicles.items())
                if key not in excluded and self.eligible(key, station, v["type"])}

    def tables(self):
        return {"vehicles": self.vehicles, "returns": self.returns, "checks": self.checks}
