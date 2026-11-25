"""Rebuild all business projections in effective-time order."""
from .cage import Cage
from .evidence import order
from .fleet import Fleet
from .supply import Supply

GROUPS = {
    "fleet": ("vehicles", "returns", "checks"),
    "supply": ("snapshots", "reservations", "reservation_actions", "offerings"),
    "cage": ("cages", "manifests", "manifest_lines", "scans", "redirects", "voids"),
}
TABLES = tuple(table for tables in GROUPS.values() for table in tables)


def rebuild(events, now):
    events = sorted((e for e in events if e["at"] <= now), key=order)
    fleet, supply, cage = Fleet(events), Supply(events), Cage()

    def public_return(e):
        fleet.observe_return(e)
        if fleet.returns[e["id"]]["consumes_dock"]:
            supply.adjust(e["station"], e["at"], e["id"], -1)

    handlers = {
        "return": public_return,
        "check": fleet.observe_check,
        "space": supply.observe_space,
        "reservation": lambda e: supply.book(e, fleet, cage),
        "cancel": lambda e: supply.action(e, fleet),
        "redeem": lambda e: supply.action(e, fleet),
        "cage": cage.register,
        "manifest": lambda e: cage.plan(e, fleet, supply),
        "scan": lambda e: cage.scan(e, fleet, supply),
        "void": cage.void,
    }
    index = 0
    while index < len(events):
        event = events[index]
        if event["kind"] != "redirect":
            handlers[event["kind"]](event)
            index += 1
            continue
        # All redirects sharing an effective minute are contiguous under the
        # global order and adjudicated together against one pre-minute state.
        minute = event["at"]
        batch = []
        while index < len(events) and events[index]["kind"] == "redirect" and events[index]["at"] == minute:
            batch.append(events[index])
            index += 1
        cage.redirect_batch(batch)
    return fleet.tables() | supply.tables(now, fleet, cage) | cage.tables(now)
