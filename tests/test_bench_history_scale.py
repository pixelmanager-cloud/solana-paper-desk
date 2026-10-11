"""SYNTHETIC_TEST_ONLY: the history-scale benchmark harness (T24R) at a tiny scale: shape, determinism of the structure, and safety."""
import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
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

    def flat(self, **over):
        row = {'scale': 1, 'gate': 'OK', 'warm_gate': 'OK', 'grown_gate': 'OK', 'snapshot': 'OK', 'warm_gate_loads': 42, 'warm_replays': 8,
               'grown_gate_loads': 50, 'snapshot_loads': 2, 'next_read_loads': 8, 'warm_gate_seconds': 0.2, 'snapshot_seconds': 0.06}
        return {**row, **over}

    def test_check_flat_accepts_constant_loads_and_bounded_seconds(self):
        rows = [self.flat(scale=1), self.flat(scale=10, warm_gate_seconds=0.3, grown_gate_loads=51), self.flat(scale=30, warm_gate_seconds=0.45)]
        self.assertEqual(bench.check_flat(rows), [])

    def test_check_flat_names_every_kind_of_growth(self):
        for key, value in (('warm_gate_loads', 43), ('warm_replays', 9), ('snapshot_loads', 3), ('next_read_loads', 9), ('grown_gate_loads', 53)):
            with self.subTest(key=key):
                problems = bench.check_flat([self.flat(scale=1), self.flat(scale=10, **{key: value})])
                self.assertEqual(len(problems), 1)
                self.assertIn(key, problems[0])
        problems = bench.check_flat([self.flat(scale=1), self.flat(scale=10, warm_gate_seconds=0.6)])
        self.assertIn('warm_gate_seconds', problems[0])
        problems = bench.check_flat([self.flat(scale=1), self.flat(scale=10, snapshot_seconds=0.19)])
        self.assertIn('snapshot_seconds', problems[0])

    def test_check_flat_refuses_one_scale_and_reports_a_failed_scale(self):
        self.assertTrue(bench.check_flat([self.flat()]))
        self.assertTrue(bench.check_flat([self.flat(scale=1), {'scale': 10, 'ERROR': 'child exit 1'}]))
        self.assertTrue(bench.check_flat([self.flat(scale=1), self.flat(scale=10, warm_gate='ERROR:ValueError:x')]))

    def test_passes_and_receipts_grow_and_each_scale_runs_in_a_fresh_interpreter(self):
        rows = bench.run([1, 2], 5, 2, passes_per_scale=12, receipts_per_scale=9, fresh=True)
        self.assertEqual([r['scale'] for r in rows], [1, 2])
        for row in rows:
            self.assertNotIn('ERROR', row, row)
            self.assertEqual((row['gate'], row['warm_gate'], row['grown_gate'], row['snapshot']), ('OK',) * 4, row)
        self.assertEqual((rows[0]['receipts'], rows[1]['receipts']), (10, 19))               # the real one plus the clones
        self.assertGreaterEqual(rows[1]['passes'] - rows[0]['passes'], 12)

    def test_the_command_line_refuses_what_would_meet_the_ceilings(self):
        for argv in (['--scales', '30', '--receipts-per-scale', '18'],                          # 540 receipts > 512
                     ['--scales', '30', '--passes-per-scale', '301'],                            # 9030 passes: the 10,000 ceiling is near
                     ['--passes-per-scale', '-1']):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                bench.main(argv)

    def test_check_exit_code_follows_flatness(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()), unittest.mock.patch.object(
                bench, 'run', return_value=[self.flat(scale=1), self.flat(scale=10, warm_gate_loads=99)]):
            self.assertEqual(bench.main(['--scales', '1,10', '--check']), 1)
        with contextlib.redirect_stdout(out), unittest.mock.patch.object(bench, 'run', return_value=[self.flat(scale=1), self.flat(scale=10)]):
            self.assertEqual(bench.main(['--scales', '1,10', '--check']), 0)

    def test_a_failing_measurement_is_reported_not_hidden(self):
        seconds, value = bench.timed(lambda: 1 / 0)
        self.assertTrue(value.startswith('ERROR:ZeroDivisionError'))
        self.assertGreaterEqual(seconds, 0)


if __name__ == '__main__':
    unittest.main()
