"""SYNTHETIC_TEST_ONLY: the regime log (lean.regime). A real lean Store, no network, a fake clock."""
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import unittest

from lean import regime
from lean.providers import ProviderError
from lean.store import Store

T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


class ChangeTests(unittest.TestCase):
    H = [(T0 - 3600, Decimal(100)), (T0 - 7200, Decimal(50)), (T0 - 300, Decimal(140))]

    def test_closest_sample_within_tolerance(self):
        self.assertEqual(regime.change(self.H, T0, 3600, Decimal(110), tolerance=360), Decimal('0.1'))
        self.assertEqual(regime.change(self.H, T0, 7200, Decimal(100), tolerance=360), Decimal(1))

    def test_no_sample_near_the_wanted_time_is_unknown_not_extrapolated(self):
        self.assertIsNone(regime.change(self.H, T0, 86400, Decimal(110), tolerance=600))
        self.assertEqual(regime.change(self.H, T0, 3600, Decimal(110), tolerance=0), Decimal('0.1'))   # exact sample, zero tolerance
        self.assertIsNone(regime.change(self.H, T0 + 1, 3600, Decimal(110), tolerance=0))               # one second off: unknown
        self.assertIsNone(regime.change([], T0, 3600, Decimal(110), tolerance=360))

    def test_reference_must_be_a_positive_price(self):
        self.assertIsNone(regime.change([(T0 - 3600, Decimal(0))], T0, 3600, Decimal(1), tolerance=60))

    def test_nearest_wins_over_a_farther_sample(self):
        history = [(T0 - 3600 - 200, Decimal(10)), (T0 - 3600 + 20, Decimal(100))]
        self.assertEqual(regime.change(history, T0, 3600, Decimal(150), tolerance=360), Decimal('0.5'))


class RegimeLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(os.path.realpath(self.tmp.name)) / 'lean.sqlite'
        self.clock = Clock()
        self.store = Store(self.path, initial_cash_sol='10', code_version='t', strategy_version='s', clock=self.clock)
        self.addCleanup(self.store.close)

    def log(self, **kw):
        return regime.RegimeLog(self.store, clock=self.clock, **kw)

    def rows(self):
        return self.store.rows('observations', kind='regime')

    def test_row_shape_and_storage(self):
        log = self.log()
        record = log.sample(lambda: Decimal('150.5'))
        self.assertEqual(record['kind'], 'lean_regime_v1')
        self.assertEqual(record['sol_usd'], '150.5')
        self.assertEqual((record['sol_usd_change_1h'], record['sol_usd_change_24h']), (None, None))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]['raw']), record)
        self.assertEqual(json.loads(rows[0]['meta']), {'error': None, 'sol_usd': '150.5'})

    def test_changes_against_own_earlier_samples(self):
        log = self.log()
        for i, price in enumerate(['100', '110', '120', '130', '140', '150', '160', '170', '180', '190', '200', '210', '220']):
            self.clock.t = T0 + i * 300
            record = log.sample(lambda price=price: Decimal(price))
        # 13th sample is exactly one hour after the first: 220 vs 100
        self.assertEqual(record['sol_usd_change_1h'], '1.200000')
        self.assertIsNone(record['sol_usd_change_24h'])
        self.clock.t = T0 + 86400 + 100
        late = log.sample(lambda: Decimal(500))
        self.assertIsNone(late['sol_usd_change_1h'])          # nothing near t-1h: unknown, never extrapolated
        self.assertEqual(late['sol_usd_change_24h'], '4.000000')   # the first sample is 100 s from the 24 h mark, inside the tolerance
        self.clock.t = T0 + 86400 + 8000
        too_late = log.sample(lambda: Decimal(500))
        self.assertIsNone(too_late['sol_usd_change_24h'])     # 4400 s from the nearest sample, beyond the 4320 s tolerance: unknown

    def test_24h_change_when_a_reference_exists(self):
        log = self.log()
        log.sample(lambda: Decimal(100))
        self.clock.t = T0 + 86400
        record = log.sample(lambda: Decimal(90))
        self.assertEqual(record['sol_usd_change_24h'], '-0.100000')

    def test_history_survives_a_restart(self):
        log = self.log()
        log.sample(lambda: Decimal(100))
        self.clock.t = T0 + 3600
        again = self.log()                                        # a new process on the same store
        self.assertEqual(len(again.history), 1)
        self.assertEqual(again.last_at, T0)
        self.assertEqual(again.sample(lambda: Decimal(103))['sol_usd_change_1h'], '0.030000')

    def test_provider_failure_is_recorded_not_raised(self):
        log = self.log()
        def failing():
            raise ProviderError('TIMEOUT', True)
        record = log.sample(failing)
        self.assertEqual((record['sol_usd'], record['error']), (None, 'TIMEOUT'))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(log.history), 0)                     # a failed sample is not a price
        for bad in (lambda: Decimal(0), lambda: Decimal('NaN'), lambda: Decimal(-5), lambda: 'garbage'):
            self.assertIsNone(log.sample(bad)['sol_usd'])

    def test_candidates_last_hour_and_rate_window(self):
        for i in range(5):
            self.store.add_candidate('mint%d' % i, ts=T0 - 100 * i)
        self.store.add_candidate('old', ts=T0 - 4000)
        log = self.log()
        short = log.sample(lambda: Decimal(1))
        self.assertEqual((short['candidates_1h'], short['candidates_per_hour']), (5, None))      # < 10 min of observation
        self.clock.t = T0 + 1800
        for i in range(5):
            self.store.add_candidate('late%d' % i, ts=T0 + 1700 + i)
        longer = log.sample(lambda: Decimal(1))
        self.assertEqual(longer['window_s'], 1800.0)
        self.assertEqual(longer['candidates_1h'], 5 + 5 - 0)       # first five are < 1 h old at T0+1800 too
        self.assertEqual(longer['candidates_per_hour'], round(longer['candidates_1h'] * 2, 3))

    def test_due_cadence_and_interval_bounds(self):
        log = self.log(interval_s=300)
        self.assertTrue(log.due())
        log.sample(lambda: Decimal(1))
        self.assertFalse(log.due())
        self.clock.t += 299
        self.assertFalse(log.due())
        self.clock.t += 1
        self.assertTrue(log.due())
        for bad in (29, 3601, True, '300', None):
            with self.assertRaises(ValueError):
                self.log(interval_s=bad)

    def test_regime_rows_are_append_only_and_never_read_by_the_trader(self):
        log = self.log()
        log.sample(lambda: Decimal(1))
        import sqlite3
        with self.assertRaises(sqlite3.Error):
            self.store.db.execute("UPDATE observations SET kind='x' WHERE kind='regime'")
        runner_source = (Path(__file__).resolve().parents[2] / 'lean' / 'runner.py').read_text()
        strategy_source = (Path(__file__).resolve().parents[2] / 'lean' / 'strategy.py').read_text()
        for source in (runner_source, strategy_source):
            self.assertNotIn("'regime'", source)
            self.assertNotIn('"regime"', source)
            self.assertNotIn('lean.regime', source)


if __name__ == '__main__':
    unittest.main()
