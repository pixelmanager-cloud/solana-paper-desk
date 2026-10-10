"""SYNTHETIC_TEST_ONLY: the history-scale benchmark harness (T24R) at a tiny scale: shape, determinism of the structure, and safety."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tools.research import bench_history_scale as bench


class BenchHarnessTests(unittest.TestCase):
    def test_tiny_run_reports_every_metric_and_no_error(self):
        rows = bench.run([1, 2], 5, 2)
        self.assertEqual([r['scale'] for r in rows], [1, 2])
        for row in rows:
            for key in ('pages', 'gate_seconds', 'gate_loads', 'gate', 'reads', 'snapshot_seconds', 'snapshot_loads',
                        'snapshot', 'next_read_seconds', 'next_read_loads'):
                self.assertIn(key, row)
            self.assertEqual((row['gate'], row['snapshot']), ('OK', 'OK'), row)
        self.assertEqual((rows[0]['reads'], rows[1]['reads']), (2, 4))
        self.assertEqual(rows[1]['pages'] - rows[0]['pages'], 5)

    def test_cli_prints_json_lines_and_writes_the_file_exclusively(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            path = str(Path(tmp) / 'bench.json')
            self.assertEqual(bench.main(['--scales', '1', '--pages-per-scale', '3', '--reads-per-scale', '1', '--json', path]), 0)
            self.assertEqual(json.loads(Path(path).read_text())[0]['scale'], 1)
            with self.assertRaises(FileExistsError):
                bench.main(['--scales', '1', '--pages-per-scale', '3', '--reads-per-scale', '1', '--json', path])
        self.assertEqual(json.loads(out.getvalue().splitlines()[0])['scale'], 1)

    def test_bad_arguments_are_refused(self):
        for argv in (['--scales', '0'], ['--scales', ''], ['--pages-per-scale', '-1']):
            with self.assertRaises((SystemExit, ValueError)):
                bench.main(argv)

    def test_a_failing_measurement_is_reported_not_hidden(self):
        seconds, value = bench.timed(lambda: 1 / 0)
        self.assertTrue(value.startswith('ERROR:ZeroDivisionError'))
        self.assertGreaterEqual(seconds, 0)


if __name__ == '__main__':
    unittest.main()
