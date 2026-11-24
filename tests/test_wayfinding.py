from contextlib import closing
import sqlite3
from unittest import TestCase

from bike_rebalance import access_map, wayfinding


class WayfindingTests(TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(sqlite3.connect(':memory:')))
        self.db.execute('PRAGMA foreign_keys=ON')
        access_map.setup(self.db)
        for id,x,y,n in [('AB','A','B',2),('BA','B','A',2),('AT','A','T',8),('BT','B','T',3)]:
            access_map.survey(self.db,id,x,y,n)

    def test_shortest_and_installed_paths_can_differ(self):
        wayfinding.point(self.db,'A','AT','photo')
        self.assertEqual(wayfinding.suggest(self.db,'A','T')['paths'],['AB','BT'])
        self.assertEqual(wayfinding.follow(self.db,'A','T')['paths'],['AT'])

    def test_independent_changes_can_form_a_loop(self):
        wayfinding.point(self.db,'A','AB','a')
        wayfinding.point(self.db,'B','BT','b')
        self.assertEqual(wayfinding.follow(self.db,'A','T')['state'],'PATH')
        wayfinding.point(self.db,'B','BA','b2')
        self.assertEqual(wayfinding.follow(self.db,'A','T')['state'],'LOOP')
