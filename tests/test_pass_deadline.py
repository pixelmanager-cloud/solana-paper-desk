"""T24/F9: whole-pass deadline is a versioned config value; an overrun closes, never latches.

Fixtures only: synthetic wire bytes, a modelled monotonic clock, no network or keys.
"""
import unittest
from pathlib import Path

from desk import paper_cycle as cycle
from tests.test_audit_first_cycle_c_held import _Base
from tests.test_kraken_lifecycle import actual_cycle


class Deadline(unittest.TestCase):
    def test_absent_keys_keep_the_strict_ten_seconds(self):
        self.assertEqual(cycle.pass_deadline({'price_ttl_seconds': 10}), 10)

    def test_valid_opt_in(self):
        for seconds in (10, 20, 30):
            self.assertEqual(cycle.pass_deadline({'paper_pass_deadline_version': 1, 'paper_pass_deadline_seconds': seconds}), seconds)

    def test_invalid_or_half_configured_values_raise(self):
        K, V = 'paper_pass_deadline_seconds', 'paper_pass_deadline_version'
        for bad in ({K: 20}, {V: 1}, {V: 2, K: 20}, {V: True, K: 20}, {V: 1, K: True}, {V: 1, K: 20.0},
                    {V: 1, K: '20'}, {V: 1, K: 9}, {V: 1, K: 31}, {V: 1, K: 0}, {V: 1, K: -10}, {V: None, K: 20}, {V: 1, K: None}):
            with self.assertRaises(ValueError, msg=bad):
                cycle.pass_deadline(bad)


class SlowProvider(_Base):
    def slow(self, per_request, **extra):
        outer = self.h

        class Slow(list):
            def append(self_, item):
                outer.f.tick += per_request
                super().append(item)
        outer.http_calls = Slow()
        outer.f.tick = 1
        if extra:
            outer.cfg = outer.cfg | extra
            outer.path = Path(outer.f.tmp.name) / 'deadline-experiment.sqlite'
            cycle.initialize(outer.path, outer.cfg)

    def test_default_deadline_overrun_blocks_but_does_not_latch(self):
        self.slow(1.6)
        first = actual_cycle(self.h)
        self.assertEqual(first['status'], 'BLOCKED', first)
        self.assertIn('CYCLE_DEADLINE_UNAVAILABLE', first['blockers'])
        self.assertEqual(self._null_passes(), 0)          # closed (T22), not a NULL latch
        self.h.http_calls = []
        self.h.f.tick = 1
        second = actual_cycle(self.h)
        self.assertNotIn('OBSERVATION_RECOVERY_REQUIRED', second['blockers'], second)

    def test_longer_deadline_is_still_enforced(self):
        self.slow(5.0, paper_pass_deadline_version=1, paper_pass_deadline_seconds=30)
        result = actual_cycle(self.h)
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertIn('CYCLE_DEADLINE_UNAVAILABLE', result['blockers'])
        self.assertEqual(self._null_passes(), 0)

    def test_invalid_deadline_config_refuses_before_any_request(self):
        self.slow(0.1, paper_pass_deadline_version=1, paper_pass_deadline_seconds=99)
        with self.assertRaises(ValueError):
            actual_cycle(self.h)
        self.assertEqual(len(self.h.http_calls), 0)


if __name__ == '__main__':
    unittest.main()
