"""Suggest a shortest access path and validate what the arrows actually show."""
from heapq import heappop, heappush

from .access_map import outgoing


def suggest(db, source, target):
    queue = [(0, (), source)]
    seen = set()
    while queue:
        distance, path, node = heappop(queue)
        if node in seen:
            continue
        if node == target:
            return {'state':'PATH', 'paths':list(path), 'length':distance}
        seen.add(node)
        for arc, next_node, length in outgoing(db, node):
            if next_node not in seen:
                heappush(queue,(distance+length,(*path,arc),next_node))
    return {'state':'UNREACHABLE'}


def point(db, node, path, proof):
    if path not in {edge[0] for edge in outgoing(db,node)}:
        raise ValueError('arrow requires a surveyed outgoing path')
    db.execute('''INSERT INTO access_arrow VALUES (?,?,?) ON CONFLICT(node)
                  DO UPDATE SET path=excluded.path,proof=excluded.proof''',(node,path,proof))
    return {'node':node,'path':path,'proof':proof}


def follow(db, source, target):
    visited = set()
    route = []
    node = source
    while node != target:
        if node in visited:
            return {'state':'LOOP','paths':route}
        visited.add(node)
        edge = db.execute('''SELECT p.id,p.target FROM access_arrow a JOIN access_path p ON p.id=a.path
                             WHERE a.node=? AND p.open=1''',(node,)).fetchone()
        if edge is None:
            return {'state':'UNMARKED','paths':route}
        route.append(edge[0])
        node = edge[1]
    return {'state':'PATH','paths':route}
