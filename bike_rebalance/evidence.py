"""Input contract and stable ordering for offline evidence."""
import json

TYPES = {"city": 1, "electric": 1, "cargo": 2}
FIELDS = {
    "return": ("vehicle station outcome", "fleet"),
    "check": ("vehicle type cleared", "fleet"),
    "space": ("station free", "supply"),
    "reservation": ("station mode begin end type", "supply"),
    "cancel": ("reservation", "supply"),
    "redeem": ("reservation proof", "supply"),
    "cage": ("slots", "cage"),
    "manifest": ("cage origin gate depot depart due items", "cage"),
    "scan": ("manifest vehicle stage channel site", "cage"),
    "redirect": ("manifest vehicles from_depot to_depot", "cage"),
    "void": ("manifest", "cage"),
}
PRIORITY = {
    kind: index for index, kind in enumerate(
        ("cage", "space", "check", "return", "load", "redirect", "unload",
         "reservation", "cancel", "redeem", "void", "manifest")
    )
}


def stable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def order(event):
    return event["at"], PRIORITY[event.get("stage", event["kind"])], event["id"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def token(value):
    return isinstance(value, str) and 0 < len(value) <= 80 and not any(c.isspace() for c in value)


def minute(value):
    return type(value) is int and 0 <= value <= 2_147_483_647


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key {key}")
        result[key] = value
    return result


def decode(raw, subsystem=None):
    events = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    require(isinstance(events, list) and events, "expected a nonempty JSON array")
    for event in events:
        validate(event, subsystem)
    require(len({e['id'] for e in events}) == len(events), "duplicate event id within file")
    return events


def validate(e, subsystem):
    require(isinstance(e, dict) and token(e.get("kind")) and e["kind"] in FIELDS, "unknown event kind")
    kind = e["kind"]
    fields, owner = FIELDS[kind]
    label = f"{kind}/{e.get('id')}"
    require(subsystem is None or subsystem == owner, f"{label}: belongs to {owner}")
    require(set(e) == {"id", "at", "kind", *fields.split()}, f"{label}: wrong fields")
    require(token(e["id"]) and minute(e["at"]), f"{label}: invalid event identity or minute")
    for key in set(e) & {"vehicle", "station", "reservation", "proof", "cage", "origin",
                         "gate", "depot", "manifest", "site", "from_depot", "to_depot"}:
        require(token(e[key]), f"{label}: invalid {key}")
    if kind == "return":
        require(e["outcome"] in ("docked", "uncertain"), f"{label}: invalid dock result")
    if kind == "check":
        require(token(e["type"]) and e["type"] in TYPES and type(e["cleared"]) is bool,
                f"{label}: invalid type or clearance")
    if kind == "space":
        require(minute(e["free"]), f"{label}: invalid dock count")
    if kind == "cage":
        require(type(e["slots"]) is int and 1 <= e["slots"] <= 24, f"{label}: invalid cage capacity")
    if kind == "reservation":
        require(minute(e["begin"]) and minute(e["end"]) and e["at"] <= e["begin"] < e["end"],
                f"{label}: invalid reservation interval")
        valid = (e["mode"] == "bike" and token(e["type"]) and e["type"] in TYPES
                 or e["mode"] == "dock" and e["type"] is None)
        require(valid, f"{label}: expected bike type or untyped dock mode")
    if kind == "manifest":
        require(minute(e["depart"]) and minute(e["due"]) and e["at"] < e["depart"] < e["due"],
                f"{label}: invalid movement interval")
        require(len({e["origin"], e["gate"], e["depot"]}) == 3, f"{label}: movement sites must differ")
        require(isinstance(e["items"], list) and 1 <= len(e["items"]) <= 12, f"{label}: invalid item count")
        for item in e["items"]:
            require(isinstance(item, list) and 1 <= len(item) <= len(TYPES)
                    and all(token(t) and t in TYPES for t in item) and len(set(item)) == len(item),
                    f"{label}: invalid type alternatives")
    if kind == "scan":
        require(e["stage"] in ("load", "unload") and e["channel"] in ("gate", "public"),
                f"{label}: invalid channel or stage")
    if kind == "redirect":
        require(isinstance(e["vehicles"], list) and 1 <= len(e["vehicles"]) <= 12,
                f"{label}: invalid vehicle count")
        require(all(token(v) for v in e["vehicles"]), f"{label}: invalid vehicle")
        require(len(e["vehicles"]) == len(set(e["vehicles"])), f"{label}: duplicate vehicle")
        require(e["from_depot"] != e["to_depot"], f"{label}: redirect depots must differ")
