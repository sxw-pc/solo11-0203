"""Confirm the removed and returned packs as one physical exchange."""
from .battery_scan import read


def status(db, id):
    row = db.execute('SELECT bin,take,put_back,minute FROM exchange WHERE id=?', (id,)).fetchone()
    if row is None:
        raise ValueError(f'no exchange {id}')
    return dict(zip(('bin','take','put_back','minute'), row))


def exchange(db, id, bin, take, put_back, minute):
    taken_bin, new, taken_op, taken_at = read(db, take)
    returned_bin, old, returned_op, returned_at = read(db, put_back)
    if ((taken_bin, taken_op, returned_bin, returned_op) != (bin, 'take', bin, 'return')
            or not taken_at <= returned_at <= minute or new == old):
        raise ValueError('invalid exchange evidence')
    outbound, = db.execute('SELECT charge FROM battery WHERE id=? AND bin=?', (new, bin)).fetchone() or (-1,)
    new_location = f'issued:{id}'
    fittings = db.execute('SELECT DISTINCT fitting FROM battery WHERE id IN (?,?)', (new, old)).fetchall()
    if outbound < 60 or len(fittings) != 1:
        raise ValueError('battery unavailable or incompatible')
    old_location, = db.execute('SELECT bin FROM battery WHERE id=?', (old,)).fetchone()
    if not old_location.startswith(('bike:', 'issued:')):
        raise ValueError('returned battery must be out of the cabinet')
    db.execute('INSERT INTO exchange VALUES (?,?,?,?,?)', (id, bin, take, put_back, minute))
    db.execute('UPDATE battery SET bin=? WHERE id=?', (new_location, new))
    db.execute('UPDATE battery SET bin=? WHERE id=?', (bin, old))
    return status(db, id)
