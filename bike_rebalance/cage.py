"""Shared cage allocation, loading, and two-ended warehouse custody."""
from .evidence import TYPES
from .packing import pack


class Cage:
    def __init__(self):
        self.cages = {}
        self.manifests = {}
        self.lines = {}
        self.scans = {}
        self.voids = {}
        self.redirects = {}

    def holds(self):
        return {line["vehicle"] for line in self.lines.values() if line["state"] in ("PLANNED", "LOADED")}

    def active(self, cage):
        return [line for line in self.lines.values() if line["cage"] == cage
                and line["state"] in ("PLANNED", "LOADED")]

    @staticmethod
    def trip(manifest):
        return tuple(manifest[k] for k in ("origin", "gate", "depot", "depart"))

    def line_for(self, manifest_id, vehicle):
        manifest = self.manifests.get(manifest_id)
        if manifest is None:
            return None, None
        line = next((self.lines[key] for key in manifest["lines"]
                     if self.lines[key]["vehicle"] == vehicle), None)
        return manifest, line

    def register(self, e):
        old = self.cages.get(e["id"])
        if old is None:
            self.cages[e["id"]] = {"slots": e["slots"], "registered_at": e["at"]}

    def plan(self, e, fleet, supply):
        cage = self.cages.get(e["cage"])
        active = self.active(e["cage"])
        row = {**e, "plan": "ALLOCATED", "lines": [], "void_proof": None,
               "used_before": sum(line["slots"] for line in active)}
        if cage is None:
            row["plan"] = "UNKNOWN_CAGE"
        elif any(self.trip(self.manifests[line["manifest"]]) != self.trip(e) for line in active):
            row["plan"] = "CAGE_BUSY_OTHER_TRIP"
        else:
            available = fleet.available(e["origin"], supply.holds(e["at"]) | self.holds())
            remaining = cage["slots"] - row["used_before"]
            chosen = pack(e["items"], available, remaining)
            if chosen is None:
                can_fill = pack(e["items"], available, 24)
                row["plan"] = "NO_CAGE_SPACE" if can_fill else "NO_FLEET"
            else:
                self.assign(row, chosen, available)
        self.manifests[e["id"]] = row

    def assign(self, row, chosen, available):
        for index, vehicle in enumerate(chosen):
            kind = available[vehicle]
            key = f'{row["id"]}/{index + 1}'
            self.lines[key] = {"manifest": row["id"], "cage": row["cage"], "vehicle": vehicle,
                               "requested_type": row["items"][index][0], "assigned_type": kind,
                               "slots": TYPES[kind], "state": "PLANNED", "origin_proof": None,
                               "destination_proof": None, "loaded_at": None, "unloaded_at": None,
                               "receiver": row["depot"], "redirects": []}
            row["lines"].append(key)

    def scan(self, e, fleet, supply):
        manifest = self.manifests.get(e["manifest"])
        if e["channel"] != "gate":
            result, responsible = "PUBLIC_IS_NOT_GATE", None
        elif manifest is None or manifest["plan"] != "ALLOCATED" or manifest["void_proof"]:
            result, responsible = "NO_ACTIVE_MANIFEST", None
        else:
            line = next((self.lines[key] for key in manifest["lines"]
                         if self.lines[key]["vehicle"] == e["vehicle"]), None)
            if line is None:
                result, responsible = "UNLISTED_VEHICLE", None
            elif e["stage"] == "load":
                result, responsible = self.load(e, manifest, line, fleet, supply), None
            else:
                result, responsible = self.unload(e, manifest, line, fleet)
        self.scans[e["id"]] = {**e, "result": result, "responsible_depot": responsible}

    @staticmethod
    def load(e, manifest, line, fleet, supply):
        if e["site"] != manifest["gate"] or e["at"] < manifest["depart"]:
            return "WRONG_ORIGIN_GATE_OR_TIME"
        if line["state"] != "PLANNED":
            return "DUPLICATE_LOAD"
        vehicle = line["vehicle"]
        if not fleet.eligible(vehicle, manifest["origin"], line["assigned_type"]):
            return "LOAD_INELIGIBLE"
        line.update(state="LOADED", origin_proof=e["id"], loaded_at=e["at"])
        fleet.place(vehicle, "transit", manifest["gate"], e["id"])
        supply.adjust(manifest["origin"], e["at"], e["id"], 1)
        return "ORIGIN_LATE" if e["at"] > manifest["depart"] else "LOADED"

    def unload(self, e, manifest, line, fleet):
        responsible = line["receiver"]
        if e["site"] != responsible:
            # The gate scanning must be the depot traceably responsible for the
            # vehicle at this minute; any other gate (including the original
            # depot after a diversion) cannot end custody.
            return ("GATE_NOT_RESPONSIBLE" if line["redirects"] else "WRONG_DESTINATION_GATE",
                    responsible)
        if line["state"] == "PLANNED":
            return "MISSING_ORIGIN", responsible
        if line["state"] == "UNLOADED":
            return "DUPLICATE_UNLOAD", responsible
        if e["at"] <= line["loaded_at"]:
            return "NONCAUSAL_UNLOAD", responsible
        if not fleet.begin_receipt(line["vehicle"], responsible, e["id"]):
            return "CUSTODY_NOT_TRACEABLE", responsible
        line.update(state="UNLOADED", destination_proof=e["id"], unloaded_at=e["at"])
        fleet.place(line["vehicle"], "warehouse", responsible, e["id"])
        return ("WAREHOUSE_LATE" if e["at"] > manifest["due"] else "UNLOADED"), responsible

    def redirect_batch(self, events):
        """Adjudicate one effective minute of diversion receipts.

        Every instruction is authorized against the same snapshot of receiving
        responsibility taken before this minute's receipts take effect, so two
        instructions issued in the same minute see identical authority even when
        their vehicle sets overlap. Among otherwise-valid instructions claiming
        the same vehicle, the event with the smallest id wins (the same replayable
        tie-break used everywhere). An instruction whose whole vehicle set cannot
        transfer fails atomically: none of its vehicles changes responsibility.
        """
        snapshot = {}
        for key, line in self.lines.items():
            if line["state"] == "LOADED":
                snapshot[(line["manifest"], line["vehicle"])] = line["receiver"]

        prepared = []
        for e in events:
            rejected, vehicles = [], []
            manifest = self.manifests.get(e["manifest"])
            if manifest is None or manifest["plan"] != "ALLOCATED" or manifest["void_proof"]:
                rejected.append(("*", "NO_ACTIVE_MANIFEST"))
            else:
                for vehicle in e["vehicles"]:
                    _, line = self.line_for(e["manifest"], vehicle)
                    if line is None:
                        rejected.append((vehicle, "UNLISTED_VEHICLE"))
                    elif line["state"] == "PLANNED":
                        rejected.append((vehicle, "NOT_LOADED"))
                    elif line["state"] == "UNLOADED":
                        rejected.append((vehicle, "ALREADY_RECEIVED"))
                    elif snapshot.get((e["manifest"], vehicle)) is None:
                        # Loaded within this same minute: no pre-minute custody
                        # state existed, so the receipt cannot yet be authorized.
                        rejected.append((vehicle, "NOT_LOADED"))
                    elif snapshot[(e["manifest"], vehicle)] != e["from_depot"]:
                        rejected.append((vehicle, "NOT_RESPONSIBLE_ISSUER"))
                    else:
                        vehicles.append(vehicle)
            prepared.append((e, rejected, vehicles))

        # Conflict resolution over the shared pre-minute authority state: valid
        # instructions are examined in event-id order and the first to claim a
        # vehicle keeps it; a later overlapping instruction loses as a whole.
        winners = {}
        conflicts = {}
        for e, rejected, vehicles in prepared:
            if rejected:
                continue
            blocked = next((v for v in vehicles if v in winners), None)
            if blocked is not None:
                conflicts[e["id"]] = (blocked, winners[blocked])
            else:
                for v in vehicles:
                    winners[v] = e["id"]

        for e, rejected, vehicles in prepared:
            if rejected:
                self.redirects[e["id"]] = {
                    **e, "result": "REDIRECT_REJECTED",
                    "failures": [{"vehicle": v, "reason": reason} for v, reason in rejected]}
            elif e["id"] in conflicts:
                blocked, winner = conflicts[e["id"]]
                self.redirects[e["id"]] = {
                    **e, "result": "REDIRECT_CONFLICT",
                    "failures": [{"vehicle": blocked, "reason": "REDIRECT_CONFLICT",
                                  "winning_receipt": winner}]}
            else:
                transferred = []
                for vehicle in vehicles:
                    _, line = self.line_for(e["manifest"], vehicle)
                    line["receiver"] = e["to_depot"]
                    line["redirects"].append({"receipt": e["id"], "at": e["at"],
                                              "from_depot": e["from_depot"],
                                              "to_depot": e["to_depot"]})
                    transferred.append(vehicle)
                self.redirects[e["id"]] = {**e, "result": "REDIRECTED", "transferred": transferred}

    def void(self, e):
        m = self.manifests.get(e["manifest"])
        if not m or m["plan"] != "ALLOCATED" or m["void_proof"]:
            result = "NO_ACTIVE_MANIFEST"
        elif any(self.lines[key]["origin_proof"] for key in m["lines"]):
            result = "CUSTODY_STARTED"
        else:
            m["void_proof"] = e["id"]
            for key in m["lines"]:
                self.lines[key]["state"] = "VOID"
            result = "VOIDED"
        self.voids[e["id"]] = {**e, "result": result}

    def tables(self, now):
        manifests = {}
        for key, m in self.manifests.items():
            lines = [self.lines[line] for line in m["lines"]]
            state = m["plan"]
            if m["void_proof"]:
                state = "VOID"
            elif lines and all(line["state"] == "UNLOADED" for line in lines):
                late = any(line["unloaded_at"] > m["due"] for line in lines)
                state = "COMPLETE_LATE" if late else "COMPLETE_ON_TIME"
            elif lines and now > m["due"]:
                state = "OVERDUE"
            elif any(line["origin_proof"] for line in lines):
                state = "IN_PROGRESS"
            manifests[key] = {**m, "state": state}
        cages = {key: {**row, "used": sum(line["slots"] for line in self.active(key))}
                 for key, row in self.cages.items()}
        return {"cages": cages, "manifests": manifests, "manifest_lines": self.lines,
                "scans": self.scans, "voids": self.voids, "redirects": self.redirects}
