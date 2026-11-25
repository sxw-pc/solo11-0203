"""Public dock capacity and reservation commitments.

Stations declared in ``inspection`` (a station -> checker mapping, see
bike_rebalance.readers.checker) keep a space snapshot usable only while the
reader inspection window since that snapshot still releases it; stations not
declared there follow the plain snapshot-age rule.
"""
from collections import defaultdict

SNAPSHOT_MAX_AGE = 10


class Supply:
    def __init__(self, events, inspection=None):
        self.snapshots = {}
        self.latest = {}
        self.reservations = {}
        self.actions = {}
        self.movements = defaultdict(list)
        self.dock_redemptions = set()
        self.snapshot_counts = defaultdict(set)
        self.inspection = inspection or {}
        for e in events:
            if e["kind"] == "space":
                self.snapshot_counts[e["station"], e["at"]].add(e["free"])

    def observe_space(self, e):
        counts = self.snapshot_counts[e["station"], e["at"]]
        result = "SNAPSHOT" if len(counts) == 1 else "SPACE_CONFLICT"
        self.snapshots[e["id"]] = {**e, "result": result}
        self.latest[e["station"]] = self.snapshots[e["id"]]

    def adjust(self, station, at, proof, delta):
        self.movements[station].append((at, proof, delta))

    def free(self, station, now):
        snapshot = self.latest.get(station)
        if snapshot is None or now - snapshot["at"] > SNAPSHOT_MAX_AGE:
            return None, "SPACE_STALE", None
        if snapshot["result"] == "SPACE_CONFLICT":
            return None, "SPACE_CONFLICT", snapshot["id"]
        check = self.inspection.get(station)
        if check is not None and now > snapshot["at"]:
            released, _ = check(snapshot["at"], now)
            if not released:
                return None, "INSPECTION_MISSING", snapshot["id"]
        total = snapshot["free"] + sum(delta for at, _, delta in self.movements[station]
                                       if at >= snapshot["at"])
        return max(0, total), "SNAPSHOT", snapshot["id"]

    def holds(self, now):
        return {r["vehicle"] for r in self.reservations.values()
                if r["mode"] == "bike" and self.live(r, now)}

    @staticmethod
    def live(r, now):
        return r["admission"] == "ACCEPTED" and r["action"] is None and now < r["end"]

    def dock_peak(self, station, begin, end, now):
        edges = defaultdict(int)
        for r in self.reservations.values():
            if r["station"] != station or r["mode"] != "dock" or not self.live(r, now):
                continue
            start, stop = max(begin, r["begin"]), min(end, r["end"])
            if start < stop:
                edges[start] += 1
                edges[stop] -= 1
        current = peak = 0
        for tick in sorted(edges):
            current += edges[tick]
            peak = max(peak, current)
        return peak

    def book(self, e, fleet, cage):
        row = {**e, "vehicle": None, "action": None, "space_proof": None}
        if e["mode"] == "bike":
            options = fleet.available(e["station"], self.holds(e["at"]) | cage.holds())
            row["vehicle"] = next((key for key, kind in options.items() if kind == e["type"]), None)
            row["admission"] = "ACCEPTED" if row["vehicle"] else "NO_BIKE"
        else:
            free, result, row["space_proof"] = self.free(e["station"], e["at"])
            if free is None:
                row["admission"] = result
            else:
                peak = self.dock_peak(e["station"], e["begin"], e["end"], e["at"])
                row["admission"] = "ACCEPTED" if free > peak else "NO_DOCK"
        self.reservations[e["id"]] = row

    def action(self, e, fleet):
        r = self.reservations.get(e["reservation"])
        if not r or not self.live(r, e["at"]):
            result = "NOT_OPEN"
        elif e["kind"] == "cancel":
            result = "CANCELLED"
        elif e["at"] < r["begin"]:
            result = "OUTSIDE_WINDOW"
        elif r["mode"] == "bike":
            result = self.redeem_bike(e, r, fleet)
        else:
            result = self.redeem_dock(e, r, fleet)
        if result in ("CANCELLED", "REDEEMED"):
            r["action"] = e["id"]
        self.actions[e["id"]] = {**e, "result": result}

    def redeem_bike(self, e, r, fleet):
        if e["proof"] != r["vehicle"] or not fleet.eligible(r["vehicle"], r["station"], r["type"]):
            return "PICKUP_UNVERIFIED"
        fleet.place(r["vehicle"], "ride", None, e["id"])
        self.adjust(r["station"], e["at"], e["id"], 1)
        return "REDEEMED"

    def redeem_dock(self, e, r, fleet):
        proof = fleet.returns.get(e["proof"])
        if not (proof and proof["consumes_dock"] and proof["station"] == r["station"]
                and r["begin"] <= proof["at"] <= e["at"] and e["proof"] not in self.dock_redemptions):
            return "DOCK_UNVERIFIED"
        self.dock_redemptions.add(e["proof"])
        return "REDEEMED"

    def tables(self, now, fleet, cage):
        reservation_rows = {}
        for key, r in self.reservations.items():
            state = r["admission"]
            if state == "ACCEPTED":
                if r["action"]:
                    state = self.actions[r["action"]]["result"]
                elif now >= r["end"]:
                    state = "EXPIRED"
                elif r["mode"] == "bike" and not fleet.eligible(r["vehicle"], r["station"], r["type"]):
                    state = "AT_RISK"
                else:
                    state = "HELD"
            reservation_rows[key] = {**r, "state": state}
        stations = self.latest.keys() | self.movements.keys() | {r["station"] for r in reservation_rows.values()}
        offerings = {}
        for station in sorted(stations):
            free, _, proof = self.free(station, now)
            peak = self.dock_peak(station, now, now + 1, now)
            offerings[station] = {
                "free": free, "space_proof": proof, "reservable_docks_now": None if free is None else max(0, free - peak),
                "reservable_bikes": fleet.available(station, self.holds(now) | cage.holds()),
            }
        return {"snapshots": self.snapshots, "reservations": reservation_rows,
                "reservation_actions": self.actions, "offerings": offerings}
