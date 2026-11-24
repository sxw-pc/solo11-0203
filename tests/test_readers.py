from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from bike_rebalance import readers


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
