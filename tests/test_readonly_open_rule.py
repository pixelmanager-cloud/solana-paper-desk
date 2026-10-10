"""SYNTHETIC_TEST_ONLY: T42 item 6 - the shared read-only open rule in healthcheck, forward_eval and fill_realism_report.

Rule (tools.research.funnel_report.open_mode, the T02F rule): `mode=ro`, falling back to `immutable=1` ONLY for a quiet WAL
file (header says WAL, no `-wal` sidecar). Why it matters under `ReadOnlyPaths=`: a plain read-only open of a quiet WAL store
creates `-shm`/`-wal` beside it (root-owned when a root-run tool does it, and impossible under a read-only mount), so the
section would be an ERROR instead of data. The opposite failure is just as bad: `immutable=1` on a store with a live writer
would miss rows that are only in the WAL, so a store with a non-empty `-wal` must stay on `mode=ro`.
"""
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from tests import helpers
from tools.ops import healthcheck
from tools.research import fill_realism_report, forward_eval

T = helpers.T


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.ledger = self.dir / 'ledger.sqlite'
        ledger = Ledger(self.ledger)
        for event in (helpers.event(ts=T, mint='A'), helpers.event(ts=T + 60, mint='B')):
            ledger.apply(event, helpers.config(), transition, initial_state)
        ledger.close()

    def names(self):
        return sorted(os.listdir(self.dir))

    def events(self, connection):
        return connection.execute('SELECT count(*) FROM events').fetchone()[0]


class QuietWalTests(Fixture):
    def test_the_fixture_is_a_quiet_wal_store(self):
        self.assertEqual(self.names(), ['ledger.sqlite'])
        self.assertEqual(self.ledger.read_bytes()[18:20], b'\x02\x02')

    def test_healthcheck_ro_leaves_no_sidecars(self):
        with closing(healthcheck.ro(self.ledger)) as c:
            self.assertEqual(self.events(c), 2)
        self.assertEqual(self.names(), ['ledger.sqlite'])

    def test_forward_eval_connect_leaves_no_sidecars(self):
        with closing(forward_eval._connect(self.ledger)) as c:
            self.assertEqual(self.events(c), 2)
        self.assertEqual(self.names(), ['ledger.sqlite'])

    def test_fill_realism_report_leaves_no_sidecars(self):
        fill_realism_report.report(self.ledger)
        self.assertEqual(self.names(), ['ledger.sqlite'])


class LiveWriterTests(Fixture):
    """With a live writer the WAL holds rows the main file does not: a reader must use `mode=ro`, never `immutable=1`."""

    def setUp(self):
        super().setUp()
        self.writer = sqlite3.connect(self.ledger, isolation_level=None)
        self.addCleanup(self.writer.close)
        self.writer.execute('PRAGMA wal_autocheckpoint=0')
        self.writer.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('live',%d,'{}','h')" % (T + 120))
        self.assertGreater(os.path.getsize(str(self.ledger) + '-wal'), 0)

    def test_healthcheck_ro_sees_the_wal_rows(self):
        with closing(healthcheck.ro(self.ledger)) as c:
            self.assertEqual(self.events(c), 3)

    def test_forward_eval_connect_sees_the_wal_rows(self):
        with closing(forward_eval._connect(self.ledger)) as c:
            self.assertEqual(self.events(c), 3)

    def test_the_rule_choice_is_ro_while_a_wal_sidecar_exists(self):
        from tools.research import funnel_report as fr
        self.assertEqual(fr.open_mode(self.ledger), 'ro')


class StillRefusesUnsafePathsTests(Fixture):
    def test_healthcheck_ro_still_refuses_a_symlink_or_a_missing_store(self):
        link = self.dir / 'link.sqlite'
        link.symlink_to(self.ledger)
        for path in (link, self.dir / 'absent.sqlite'):
            with self.subTest(path=path.name), self.assertRaises(FileNotFoundError):
                healthcheck.ro(path)

    def test_forward_eval_and_fill_realism_still_refuse_a_symlink(self):
        link = self.dir / 'link.sqlite'
        link.symlink_to(self.ledger)
        with self.assertRaises(Exception):
            fill_realism_report.report(link)


if __name__ == '__main__':
    unittest.main()
