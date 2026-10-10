"""SYNTHETIC_TEST_ONLY: T20F item 2. Candidate supply must be a bounded walk over the newest discovery rows, never a
scan of the (unindexed, ever-growing) table. Fixture tables only; no provider, no discovery listener."""
import math
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from tools import paper_entry_dispatcher as tool

COLUMNS = 'seq,source_id,received_at,slot,payload_hash,typeof(payload),length(CAST(payload AS BLOB))'
NOW = 10_000_000.0
PAYLOAD = 'x' * 300


class WindowWalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'discovery.sqlite'
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('CREATE TABLE raw_events(seq INTEGER PRIMARY KEY AUTOINCREMENT,source_id TEXT,received_at REAL,'
                      'slot INTEGER,payload TEXT,payload_hash TEXT)')

    def insert(self, times):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('PRAGMA synchronous=OFF')
            c.executemany('INSERT INTO raw_events(source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?)',
                          ((f's{i}', t, i, PAYLOAD, 'h') for i, t in enumerate(times)))
            c.commit()

    def walk(self, oldest, youngest=300, statements=None):
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as d:
            if statements is not None:
                d.set_trace_callback(statements.append)
            return list(tool._window_rows(d, NOW, oldest, youngest, COLUMNS))

    def test_yields_exactly_the_window_newest_first_and_never_truncates_a_six_hour_window(self):
        # 5000 rows/hour for 8 hours; the old LIMIT 20000 would have cut the 6 h window (30000 rows) short.
        step = 3600 / 5000
        times = [NOW - 8 * 3600 + i * step for i in range(8 * 5000)]
        self.insert(times)
        rows = self.walk(oldest=20700)
        expected = [t for t in times if 300 <= NOW - t <= 20700]
        self.assertGreater(len(expected), 20000)
        self.assertEqual(len(rows), len(expected))
        seqs = [r[0] for r in rows]
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        self.assertTrue(all(300 <= NOW - r[2] <= 20700 for r in rows))

    def test_the_walk_stops_at_the_window_edge_and_reads_only_the_newest_pages(self):
        old = [NOW - 100_000 + i * 0.1 for i in range(200_000)]              # long before the window
        recent = [NOW - 3000 + i * 3 for i in range(1000)]                    # 1000 rows inside a 6300 s window
        self.insert(old + recent)
        statements = []
        rows = self.walk(oldest=6300, statements=statements)
        self.assertEqual(len(rows), len([t for t in recent if NOW - t >= 300]))
        pages = [s for s in statements if s.startswith('SELECT') and 'raw_events' in s]
        # 1000 window rows + grace rows + the first old page: a handful of 500-row pages, never ~400 for the table
        self.assertLessEqual(len(pages), 6, len(pages))

    def test_receipt_time_skew_inside_the_grace_is_walked_through_and_beyond_it_ends_the_walk(self):
        base = NOW - 5000
        times = [base + i for i in range(100)]
        times[60] = NOW - 6800         # 500 s past the window edge (6300): inside the 600 s grace, not yielded, walk goes on
        self.insert(times)
        got = {round(NOW - r[2]) for r in self.walk(oldest=6300)}
        self.assertNotIn(round(NOW - 6800), got)
        self.assertIn(round(NOW - (base + 10)), got)                          # rows older than the skewed one are reached
        self.assertEqual(len(got), 99)
        # Beyond the grace (>= 900 s past the edge) the walk ends: documented assumption of receipt-ordered rows.
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DELETE FROM raw_events'); c.commit()
        times[60] = NOW - 7300
        self.insert(times)
        got = {round(NOW - r[2]) for r in self.walk(oldest=6300)}
        self.assertNotIn(round(NOW - (base + 10)), got)
        self.assertIn(round(NOW - (base + 90)), got)

    def test_malformed_receipt_time_is_yielded_so_the_integrity_check_still_rejects_it(self):
        self.insert([NOW - 1000, NOW - 900])
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("INSERT INTO raw_events(source_id,received_at,slot,payload,payload_hash) VALUES('bad','not-a-time',9,'x','h')")
            c.commit()
        rows = self.walk(oldest=6300)
        self.assertEqual([type(r[2]).__name__ for r in rows][0], 'str')

    def test_five_hundred_thousand_rows_cost_milliseconds_not_a_table_scan(self):
        old = [NOW - 200_000 + i * 0.2 for i in range(500_000)]
        recent = [NOW - 3000 + i for i in range(2000)]
        started = time.perf_counter()
        self.insert(old + recent)
        build = time.perf_counter() - started
        self.assertGreater(os.path.getsize(self.path), 100_000_000)           # a real >100 MB table, not a toy
        started = time.perf_counter()
        rows = self.walk(oldest=6300)
        walk = time.perf_counter() - started
        self.assertEqual(len(rows), len([t for t in recent if NOW - t >= 300]))
        self.assertLess(walk, 1.0, f'walk took {walk:.2f}s over 502k rows (built in {build:.1f}s)')
        # The query this replaces reads every page of the table (no index on received_at).
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as d:
            started = time.perf_counter()
            d.execute(f'SELECT {COLUMNS} FROM raw_events WHERE received_at BETWEEN ? AND ? ORDER BY seq DESC LIMIT 20000',
                      (NOW - 6300, NOW - 300)).fetchall()
            scan = time.perf_counter() - started
        self.assertLess(walk, scan, f'walk {walk:.3f}s should beat the table scan {scan:.3f}s')


if __name__ == '__main__':
    unittest.main()
