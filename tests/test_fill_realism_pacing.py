"""The lowest pacing priority: measurement never delays a trading quote (T14G item 3)."""
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
import sqlite3

from desk import provider_pacing as pace


class Clock:
    def __init__(self):
        self.wall = 1000.0

    def time(self):
        return self.wall

    def sleep(self, seconds):
        self.wall += seconds


class ResearchPriorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'pacing.sqlite'
        pace.initialize(self.path)
        self.clock = Clock()

    def pacer(self, priority):
        return pace.Pacer(self.path, priority=priority, clock=self.clock.time, monotonic=self.clock.time, sleep=self.clock.sleep)

    def waiter(self, ticket, priority, created):
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('INSERT INTO waiters VALUES(?,?,?,?,?)', (ticket, 'jupiter', priority, created, created + 4))

    def test_priority_is_validated_and_research_is_accepted(self):
        self.pacer('research')
        for bad in ('RESEARCH', 'low', '', None):
            with self.assertRaises(pace.PacingError):
                self.pacer(bad)

    def test_a_trading_quote_goes_ahead_of_an_earlier_waiting_research_request(self):
        self.waiter('a' * 32, 'research', self.clock.wall - 1)        # research has been waiting longer
        ticket = self.pacer('investigation').acquire('jupiter', timeout_seconds=5)
        self.assertEqual(len(ticket), 32)                              # the investigation request is granted first
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM waiters WHERE priority='research'").fetchone()[0], 1)

    def test_research_waits_behind_an_investigation_waiter_and_behind_held(self):
        self.waiter('b' * 32, 'investigation', self.clock.wall - 1)
        with self.assertRaises(pace.PacingError) as raised:
            self.pacer('research').acquire('jupiter', timeout_seconds=1)
        self.assertEqual(raised.exception.code, 'PACING_DEADLINE_EXCEEDED')
        with closing(sqlite3.connect(self.path)) as c, c:
            c.execute('DELETE FROM waiters')
        self.waiter('c' * 32, 'held', self.clock.wall - 1)
        with self.assertRaises(pace.PacingError):
            self.pacer('research').acquire('jupiter', timeout_seconds=1)

    def test_research_alone_is_granted_and_the_database_still_validates(self):
        ticket = self.pacer('research').acquire('jupiter', timeout_seconds=5)
        self.waiter('d' * 32, 'research', self.clock.wall)
        self.pacer('held')                                             # _validate accepts a research waiter row


if __name__ == '__main__':
    unittest.main()
