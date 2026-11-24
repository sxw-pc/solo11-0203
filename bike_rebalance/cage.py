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

    def holds(self):
        return {line["vehicle"] for line in self.lines.values() if line["state"] in ("PLANNED", "LOADED")}

    def active(self, cage):
        return [line for line in self.lines.values() if line["cage"] == cage
                and line["state"] in ("PLANNED", "LOADED")]

    @staticmethod
    def trip(manifest):
        return tuple(manifest[k] for k in ("origin", "gate", "depot", "depart"))

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
                               "destination_proof": None, "loaded_at": None, "unloaded_at": None}
            row["lines"].append(key)

    def scan(self, e, fleet, supply):
        manifest = self.manifests.get(e["manifest"])
        if e["channel"] != "gate":
            result = "PUBLIC_IS_NOT_GATE"
        elif manifest is None or manifest["plan"] != "ALLOCATED" or manifest["void_proof"]:
            result = "NO_ACTIVE_MANIFEST"
        else:
            line = next((self.lines[key] for key in manifest["lines"]
                         if self.lines[key]["vehicle"] == e["vehicle"]), None)
            if line is None:
                result = "UNLISTED_VEHICLE"
            elif e["stage"] == "load":
                result = self.load(e, manifest, line, fleet, supply)
            else:
                result = self.unload(e, manifest, line, fleet)
        self.scans[e["id"]] = {**e, "result": result}

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

    @staticmethod
    def unload(e, manifest, line, fleet):
        if e["site"] != manifest["depot"]:
            return "WRONG_DESTINATION_GATE"
        if line["state"] == "PLANNED":
            return "MISSING_ORIGIN"
        if line["state"] == "UNLOADED":
            return "DUPLICATE_UNLOAD"
        if e["at"] <= line["loaded_at"]:
            return "NONCAUSAL_UNLOAD"
        line.update(state="UNLOADED", destination_proof=e["id"], unloaded_at=e["at"])
        fleet.place(line["vehicle"], "warehouse", manifest["depot"], e["id"])
        return "WAREHOUSE_LATE" if e["at"] > manifest["due"] else "UNLOADED"

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
                "scans": self.scans, "voids": self.voids}
