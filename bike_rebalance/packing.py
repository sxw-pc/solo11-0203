"""Find a complete manifest assignment against inventory and physical slot budget."""
from functools import lru_cache

from .evidence import TYPES


def pack(items, available, slots):
    kinds = tuple(TYPES)
    bikes = tuple(tuple(key for key, actual in available.items() if actual == kind) for kind in kinds)

    @lru_cache(maxsize=None)
    def choose(used):
        index = sum(used)
        if index == len(items):
            return (0, 0, 0, ())
        remaining = slots - sum(count * TYPES[kind] for kind, count in zip(kinds, used))
        best = None
        for rank, kind in enumerate(items[index]):
            pos = kinds.index(kind)
            if used[pos] >= len(bikes[pos]) or TYPES[kind] > remaining:
                continue
            updated = (*used[:pos], used[pos] + 1, *used[pos + 1:])
            tail = choose(updated)
            if tail is None:
                continue
            candidate = (tail[0] + (rank > 0), tail[1] + rank, tail[2] + TYPES[kind],
                         (bikes[pos][used[pos]], *tail[3]))
            if best is None or candidate < best:
                best = candidate
        return best

    best = choose((0,) * len(kinds))
    return None if best is None else best[3]
