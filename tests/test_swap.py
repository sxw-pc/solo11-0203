import sqlite3
import unittest

from bike_rebalance import battery_scan, swap
from bike_rebalance.store import transaction


class SwapTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:', isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        battery_scan.ensure_schema(self.db)
        battery_scan.register(self.db, 'N', 'city', 80, 'D1')
        battery_scan.register(self.db, 'O', 'city', 20, 'bike:V')
        battery_scan.scan(self.db, 'T', 'D1', 'N', 'take', 1)
        battery_scan.scan(self.db, 'R', 'D1', 'O', 'return', 2)

    def test_exchange_moves_two_physical_packs_together(self):
        with transaction(self.db, write=True):
            swap.exchange(self.db, 'X', 'D1', 'T', 'R', 2)
        self.assertEqual(dict(self.db.execute('SELECT id,bin FROM battery')), {'N':'issued:X','O':'D1'})
        self.assertEqual(swap.status(self.db, 'X')['put_back'], 'R')

    def test_raw_duplicate_and_conflicting_receipts(self):
        self.assertTrue(battery_scan.scan(self.db, 'T', 'D1', 'N', 'take', 1)['duplicate'])
        with self.assertRaises(ValueError):
            battery_scan.scan(self.db, 'T', 'D1', 'N', 'take', 2)
