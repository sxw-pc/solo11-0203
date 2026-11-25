from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from bike_rebalance import readers
from bike_rebalance.supply import Supply


class ReaderCoverage(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(readers.open_reader_log(':memory:')))
        readers.register(self.db,'H-A','NORTH',10)

    def test_out_of_order_samples_cover_expected_ticks(self):
        data = Path(__file__).resolve().parents[1] / 'fixtures' / 'reader-samples.json'
        self.assertEqual(readers.accept(self.db,data), readers.accept(self.db,data))
        result = readers.coverage(self.db,'NORTH',0,30)
        self.assertEqual(result['state'],'COMPLETE')
        self.assertEqual(result['readers']['H-A']['proof'],['R0','R10','R20'])

    def test_missing_inspection_samples_are_visible(self):
        result = readers.coverage(self.db,'NORTH',0,21)
        self.assertEqual(result['readers']['H-A']['missing'],[0,10,20])
        self.assertEqual(result['state'],'MISSING')

    def test_refused_identity_rolls_back_entire_file(self):
        data = Path(__file__).resolve().parents[1] / 'fixtures' / 'reader-samples.json'
        readers.accept(self.db,data)
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / 'bad.json'
            bad.write_text('[{"id":"new","reader":"H-A","tick":30,"signal":90},{"id":"R0","reader":"H-A","tick":0,"signal":0}]', encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'receipt changed'):
                readers.accept(self.db,bad)
        self.assertEqual(readers.coverage(self.db,'NORTH',0,40)['readers']['H-A']['missing'],[30])


class RestartedClockCoverage(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(readers.open_reader_log(':memory:')))
        self.folder = self.enterContext(tempfile.TemporaryDirectory())

    def feed(self, rows, name='samples.json'):
        path = Path(self.folder) / name
        path.write_text(json.dumps(rows), encoding='utf-8')
        return readers.accept(self.db, path)

    def calibrate(self, rows, name='anchors.json'):
        path = Path(self.folder) / name
        path.write_text(json.dumps(rows), encoding='utf-8')
        return readers.calibrate(self.db, path)

    @staticmethod
    def dock_reservation(key, station, at, begin, end):
        return {'kind': 'reservation', 'id': key, 'at': at, 'station': station,
                'mode': 'dock', 'type': None, 'begin': begin, 'end': end}

    def test_restart_segments_and_boundary_occupancy(self):
        readers.register(self.db, 'H-B', 'EAST', 10)
        self.feed([
            {'id': 'B0', 'reader': 'H-B', 'segment': 'S1', 'tick': 0, 'signal': 90},
            {'id': 'B10', 'reader': 'H-B', 'segment': 'S1', 'tick': 10, 'signal': 91},
            {'id': 'B20', 'reader': 'H-B', 'segment': 'S2', 'tick': 0, 'signal': 93},
            {'id': 'B30', 'reader': 'H-B', 'segment': 'S2', 'tick': 10, 'signal': 94},
        ])
        # unverifiable restarted clocks must not fill shift minutes with raw ticks
        result = readers.coverage(self.db, 'EAST', 0, 30)
        self.assertEqual(result['state'], 'MISSING')
        self.assertEqual(result['readers']['H-B']['missing'], [0, 10, 20])
        self.assertEqual(result['readers']['H-B']['segments']['S2']['offsets'], [])
        # anchoring only the first segment leaves the restarted half unusable
        self.calibrate([{'id': 'K1', 'reader': 'H-B', 'segment': 'S1', 'tick': 0, 'minute': 0}])
        result = readers.coverage(self.db, 'EAST', 0, 30)
        self.assertEqual(result['readers']['H-B']['missing'], [20])
        self.assertEqual(result['state'], 'MISSING')
        # the second segment's own anchor places its samples on the shift clock
        self.calibrate([{'id': 'K2', 'reader': 'H-B', 'segment': 'S2', 'tick': 0, 'minute': 20}],
                       name='anchors2.json')
        result = readers.coverage(self.db, 'EAST', 0, 30)
        self.assertEqual(result['state'], 'COMPLETE')
        self.assertEqual(result['readers']['H-B']['proof'], ['B0', 'B10', 'B20'])
        self.assertEqual(result['readers']['H-B']['segments']['S2']['offsets'], [20])
        # a corroborating anchor sharing the same correction changes nothing
        self.calibrate([{'id': 'K3', 'reader': 'H-B', 'segment': 'S1', 'tick': 10, 'minute': 10}],
                       name='anchors3.json')
        self.assertEqual(readers.coverage(self.db, 'EAST', 0, 30)['state'], 'COMPLETE')
        # the boundary minute 20 is occupied by both segments but counted once
        self.feed([{'id': 'B20old', 'reader': 'H-B', 'segment': 'S1', 'tick': 20, 'signal': 92}],
                  name='boundary.json')
        result = readers.coverage(self.db, 'EAST', 0, 30)
        self.assertEqual(result['state'], 'COMPLETE')
        self.assertEqual(result['readers']['H-B']['proof'], ['B0', 'B10', 'B20'])
        self.assertEqual(result['readers']['H-B']['receipts'][20], ['B20', 'B20old'])
        # window ends are exclusive: minute 20 closes the first window, opens the second
        first = readers.coverage(self.db, 'EAST', 0, 20)
        self.assertEqual(first['readers']['H-B']['proof'], ['B0', 'B10'])
        self.assertEqual(first['state'], 'COMPLETE')
        second = readers.coverage(self.db, 'EAST', 20, 40)
        self.assertEqual(second['readers']['H-B']['proof'], ['B20', 'B30'])
        self.assertEqual(second['state'], 'COMPLETE')

    def test_ambiguous_window_requires_one_coherent_candidate(self):
        readers.register(self.db, 'H-C', 'WEST', 1)
        self.feed([
            {'id': 'P', 'reader': 'H-C', 'segment': 'S1', 'tick': 4, 'signal': 80},
            {'id': 'Q', 'reader': 'H-C', 'segment': 'S1', 'tick': 5, 'signal': 81},
            {'id': 'R', 'reader': 'H-C', 'segment': 'S1', 'tick': 6, 'signal': 82},
        ])
        # two clock corrections are both legal; each covers minutes 10 and 11
        # with different samples (P/Q under correction 6, Q/R under correction 5)
        self.calibrate([
            {'id': 'K1', 'reader': 'H-C', 'segment': 'S1', 'tick': 4, 'minute': 10},
            {'id': 'K2', 'reader': 'H-C', 'segment': 'S1', 'tick': 4, 'minute': 9},
        ])
        report = readers.coverage(self.db, 'WEST', 10, 12)
        row = report['readers']['H-C']
        self.assertEqual(report['state'], 'COMPLETE')
        self.assertEqual(row['calibrations'], 2)
        self.assertTrue(row['ambiguous'])
        self.assertEqual(row['receipts'][10], ['P', 'Q'])
        self.assertEqual(row['receipts'][11], ['Q', 'R'])
        ok, detail = readers.released(self.db, 'WEST', 10, 12)
        self.assertFalse(ok)
        self.assertEqual(detail['state'], 'CANDIDATE_REQUIRED')
        # a candidate that can only reuse Q for both minutes mixes calibrations
        bad = readers.nominate(self.db, 'C-bad', 'WEST', 10, 12, {'H-C': {10: 'Q', 11: 'Q'}})
        self.assertEqual(bad['state'], 'REJECTED')
        self.assertIn('reuses', bad['reason'])
        # stitching receipts that each need a different correction is also refused
        mixed = readers.nominate(self.db, 'C-mixed', 'WEST', 10, 12, {'H-C': {10: 'P', 11: 'R'}})
        self.assertEqual(mixed['state'], 'REJECTED')
        self.assertIn('mixes', mixed['reason'])
        self.assertFalse(readers.released(self.db, 'WEST', 10, 12)[0])
        # the snapshot must not accept bookings while no coherent candidate is frozen
        space = {'kind': 'space', 'id': 'S-west', 'at': 10, 'station': 'WEST', 'free': 1}
        supply = Supply([space], inspection=readers.checker(self.db, 'WEST'))
        supply.observe_space(space)
        self.assertEqual(supply.free('WEST', 10), (1, 'SNAPSHOT', 'S-west'))
        self.assertEqual(supply.free('WEST', 12), (None, 'INSPECTION_MISSING', 'S-west'))
        supply.book(self.dock_reservation('r-w1', 'WEST', 12, 12, 13), None, None)
        self.assertEqual(supply.reservations['r-w1']['admission'], 'INSPECTION_MISSING')
        # freezing one coherent candidate releases the same snapshot for a real booking
        frozen = readers.nominate(self.db, 'C-ok', 'WEST', 10, 12, {'H-C': {10: 'P', 11: 'Q'}})
        self.assertEqual(frozen, {'candidate': 'C-ok', 'state': 'FROZEN'})
        self.assertEqual(readers.nominate(self.db, 'C-ok', 'WEST', 10, 12, {'H-C': {10: 'P', 11: 'Q'}}),
                         frozen)
        with self.assertRaisesRegex(ValueError, 'candidate receipt changed'):
            readers.nominate(self.db, 'C-ok', 'WEST', 10, 12, {'H-C': {10: 'Q', 11: 'R'}})
        ok, detail = readers.released(self.db, 'WEST', 10, 12)
        self.assertTrue(ok)
        self.assertEqual(detail['candidate'], 'C-ok')
        self.assertEqual(supply.free('WEST', 12), (1, 'SNAPSHOT', 'S-west'))
        supply.book(self.dock_reservation('r-w2', 'WEST', 12, 12, 13), None, None)
        self.assertEqual(supply.reservations['r-w2']['admission'], 'ACCEPTED')

    def test_late_anchor_reopens_released_window(self):
        readers.register(self.db, 'H-D', 'SOUTH', 10)
        samples = [
            {'id': 'D0', 'reader': 'H-D', 'segment': 'S1', 'tick': 0, 'signal': 70},
            {'id': 'D10', 'reader': 'H-D', 'segment': 'S1', 'tick': 10, 'signal': 71},
            {'id': 'D20', 'reader': 'H-D', 'segment': 'S1', 'tick': 20, 'signal': 72},
        ]
        self.feed(samples)
        self.calibrate([{'id': 'K1', 'reader': 'H-D', 'segment': 'S1', 'tick': 0, 'minute': 0}])
        self.assertEqual(readers.coverage(self.db, 'SOUTH', 0, 30)['state'], 'COMPLETE')
        self.assertTrue(readers.released(self.db, 'SOUTH', 0, 30)[0])
        space = {'kind': 'space', 'id': 'S-south', 'at': 0, 'station': 'SOUTH', 'free': 1}
        supply = Supply([space], inspection=readers.checker(self.db, 'SOUTH'))
        supply.observe_space(space)
        supply.book(self.dock_reservation('r-s1', 'SOUTH', 10, 10, 15), None, None)
        self.assertEqual(supply.reservations['r-s1']['admission'], 'ACCEPTED')
        # a late independent anchor allows a second correction that leaves the
        # window uncovered, so the space availability is re-adjudicated
        self.calibrate([{'id': 'K2', 'reader': 'H-D', 'segment': 'S1', 'tick': 0, 'minute': 1}],
                       name='anchors2.json')
        report = readers.coverage(self.db, 'SOUTH', 0, 30)
        self.assertEqual(report['state'], 'MISSING')
        self.assertEqual(report['readers']['H-D']['missing'], [0, 10, 20])
        ok, detail = readers.released(self.db, 'SOUTH', 0, 10)
        self.assertFalse(ok)
        self.assertEqual(detail['state'], 'MISSING')
        self.assertEqual((detail['start'], detail['end']), (0, 10))
        self.assertEqual(detail['coverage']['readers']['H-D']['missing'], [0])
        self.assertEqual(supply.free('SOUTH', 10), (None, 'INSPECTION_MISSING', 'S-south'))
        supply.book(self.dock_reservation('r-s2', 'SOUTH', 10, 10, 15), None, None)
        self.assertEqual(supply.reservations['r-s2']['admission'], 'INSPECTION_MISSING')
        self.assertEqual(supply.reservations['r-s1']['admission'], 'ACCEPTED')
        # retransmitting the same receipts does not change the decision
        self.calibrate([{'id': 'K2', 'reader': 'H-D', 'segment': 'S1', 'tick': 0, 'minute': 1}],
                       name='anchors3.json')
        self.feed(samples, name='resend.json')
        self.assertEqual(supply.free('SOUTH', 10), (None, 'INSPECTION_MISSING', 'S-south'))
        with self.assertRaisesRegex(ValueError, 'anchor receipt changed'):
            self.calibrate([{'id': 'K2', 'reader': 'H-D', 'segment': 'S1', 'tick': 0, 'minute': 2}],
                           name='anchors4.json')

    def test_undeclared_station_keeps_plain_snapshot_rule(self):
        space = {'kind': 'space', 'id': 'S-open', 'at': 0, 'station': 'OPEN', 'free': 2}
        supply = Supply([space])
        supply.observe_space(space)
        self.assertEqual(supply.free('OPEN', 5), (2, 'SNAPSHOT', 'S-open'))
        supply.book(self.dock_reservation('r-o1', 'OPEN', 5, 5, 6), None, None)
        self.assertEqual(supply.reservations['r-o1']['admission'], 'ACCEPTED')
