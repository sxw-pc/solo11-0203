"""Rebuild all business projections in effective-time order."""
from .cage import Cage
from .evidence import order
from .fleet import Fleet
from .supply import Supply

GROUPS = {
    "fleet": ("vehicles", "returns", "checks"),
    "supply": ("snapshots", "reservations", "reservation_actions", "offerings"),
    "cage": ("cages", "manifests", "manifest_lines", "scans", "voids"),
}
TABLES = tuple(table for tables in GROUPS.values() for table in tables)


def rebuild(events, now, inspection=None):
    events = sorted((e for e in events if e["at"] <= now), key=order)
    fleet, supply, cage = Fleet(events), Supply(events, inspection), Cage()

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
    for event in events:
        handlers[event["kind"]](event)
    return fleet.tables() | supply.tables(now, fleet, cage) | cage.tables(now)
