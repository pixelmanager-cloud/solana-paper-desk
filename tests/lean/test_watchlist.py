"""SYNTHETIC_TEST_ONLY: lean.watchlist - the soft/hazard allow-list, config, persistence across a restart, interval,
expiry and per-pass bound. Decisions are written to a real store the way the runner writes them. No network."""
import os
import tempfile
import unittest
from pathlib import Path

from lean import watchlist as W
from lean.store import Store

MINT = 'M' * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(os.path.realpath(self.tmp.name)) / 'lean.sqlite'
        self.now = [1000.0]
        self.open()

    def open(self, **cfg):
        self.store = Store(self.path, initial_cash_sol=10, code_version='c', strategy_version='s', clock=lambda: self.now[0])
        self.addCleanup(self.store.close)
        self.watch = self.make(**cfg)

    def make(self, **cfg):
        base = dict(enabled=True, watch_interval_s=300, watch_hours=6, watch_max_per_pass=5)
        return W.Watchlist(self.store, W.WatchConfig(**dict(base, **cfg)), clock=lambda: self.now[0], code_version='c', strategy_version='s')

    def candidate(self, mint=MINT, source=None):
        meta = {'payload_hash': 'h'}
        if source:
            meta['source'] = source
        self.store.add_candidate(mint, pool='pool', signature='sig', slot=7, migrated_at=500.0, hint_seq=3, meta=meta)
        return self.watch.candidate(mint)

    def decide(self, kind, action, reasons, mint=MINT):
        cid = self.store.candidate_id(mint)
        self.store.add_decision(kind, action, mint=mint, candidate_id=cid, reasons=reasons)

    def events(self, kind):
        return self.store.rows('events', kind=kind, limit=1000)


class ClassifyTests(unittest.TestCase):
    def test_soft_screen_reasons(self):
        for reasons in (['TOO_YOUNG'], ['MARKET_CAP_BELOW_MIN'], ['MARKET_CAP_ABOVE_MAX'], ['LIQUIDITY_BELOW_MIN'],
                        ['LIQUIDITY_BELOW_MIN', 'MARKET_CAP_BELOW_MIN']):
            self.assertEqual(W.classify('screen', 'REJECT', reasons), 'SOFT', reasons)

    def test_soft_entry_reasons(self):
        for reason in ('MARKET_CAP', 'LIQUIDITY', 'COST_BUDGET', 'MAX_POSITIONS', 'COOLDOWN', 'ENTRY_THROTTLE',
                       'LOSS_STREAK_PAUSE', 'DAILY_LOSS_STOP', 'BELOW_MINIMUM'):
            self.assertEqual(W.classify('entry', 'SKIP', [reason]), 'SOFT', reason)

    def test_one_hazard_reason_makes_the_whole_rejection_a_hazard(self):
        for kind, soft in (('screen', 'LIQUIDITY_BELOW_MIN'), ('entry', 'MAX_POSITIONS')):
            for hazard in ('ACTIVE_MINT_AUTHORITY', 'ACTIVE_FREEZE_AUTHORITY', 'OUTSTANDING_WITHDRAWABLE_LP_SUPPLY',
                           'HOLDER_CONCENTRATION_ABOVE_MAX', 'TOO_OLD', 'ALREADY_HELD', 'MARKET_CAP_UNKNOWN',
                           'LIQUIDITY_UNKNOWN', 'POOL_BINDING_INVALID', 'PROVIDER_ERROR:HTTP_503', 'SOMETHING_NEW', ''):
                self.assertEqual(W.classify(kind, 'REJECT' if kind == 'screen' else 'SKIP', [soft, hazard]), 'HAZARD', hazard)
                self.assertEqual(W.classify(kind, 'REJECT' if kind == 'screen' else 'SKIP', [hazard]), 'HAZARD', hazard)

    def test_screen_and_entry_reason_sets_do_not_cross(self):
        self.assertEqual(W.classify('screen', 'REJECT', ['MAX_POSITIONS']), 'HAZARD')
        self.assertEqual(W.classify('entry', 'SKIP', ['TOO_YOUNG']), 'HAZARD')

    def test_empty_or_missing_reasons_are_a_hazard(self):
        for reasons in ([], None, ()):
            self.assertEqual(W.classify('screen', 'REJECT', reasons), 'HAZARD')

    def test_pass_buy_failed_are_not_rejections(self):
        self.assertIsNone(W.classify('screen', 'PASS', []))
        self.assertIsNone(W.classify('screen', 'FAILED', ['PROVIDER_ERROR:HTTP_503']))
        self.assertIsNone(W.classify('entry', 'BUY', ['ENTRY']))
        self.assertIsNone(W.classify('exit', 'SELL', ['STOP']))


class ConfigTests(unittest.TestCase):
    def test_defaults_are_off_with_spec_values(self):
        c = W.WatchConfig.from_dict(None)
        self.assertEqual((c.enabled, c.watch_interval_s, c.watch_hours, c.watch_max_per_pass), (False, 300.0, 6.0, 5))
        self.assertFalse(W.WatchConfig.from_dict({}).enabled)

    def test_strict(self):
        for bad in ({'enabled': 1}, {'enabled': 'yes'}, {'watch_interval_s': 5}, {'watch_interval_s': True},
                    {'watch_hours': -1}, {'watch_hours': 1000}, {'watch_max_per_pass': 0}, {'watch_max_per_pass': 2.5},
                    {'typo': 1}, [], 'x'):
            with self.assertRaises(W.WatchConfigError, msg=str(bad)):
                W.WatchConfig.from_dict(bad)

    def test_disabled_watchlist_does_nothing(self):
        # Base.open builds an enabled one; build a disabled one on its own store
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(os.path.realpath(tmp)) / 'l.sqlite', initial_cash_sol=10, code_version='c', strategy_version='s')
            self.addCleanup(store.close)
            watch = W.Watchlist(store, W.WatchConfig(), clock=lambda: 1.0, code_version='c', strategy_version='s')
            store.add_candidate(MINT, pool='p', signature='s', slot=1, migrated_at=1.0, hint_seq=1, meta={})
            store.add_decision('screen', 'REJECT', mint=MINT, candidate_id=1, reasons=['TOO_YOUNG'])
            watch.after_handle(watch.candidate(MINT))
            self.assertEqual(watch.entries, {})
            self.assertEqual(watch.due(), [])


class WatchTests(Base):
    def test_soft_screen_reject_is_added_with_its_source(self):
        c = self.candidate(source='pump_b')
        self.decide('screen', 'REJECT', ['LIQUIDITY_BELOW_MIN'])
        self.watch.after_handle(c)
        self.assertEqual(self.watch.entries[MINT]['source'], 'pump_b')
        self.assertEqual(len(self.events('watch_add')), 1)

    def test_soft_entry_skip_after_a_screen_pass_is_added(self):
        c = self.candidate()
        self.decide('screen', 'PASS', [])
        self.watch.after_handle(c)
        self.assertEqual(self.watch.entries, {})              # the entry decision has not landed: undecided
        self.decide('entry', 'SKIP', ['MAX_POSITIONS'])
        self.watch.after_handle(c)
        self.assertIn(MINT, self.watch.entries)

    def test_hazard_reject_is_never_added(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['ACTIVE_MINT_AUTHORITY'])
        self.watch.after_handle(c)
        self.assertEqual((self.watch.entries, self.events('watch_add')), ({}, []))
        self.now[0] += 10 ** 6
        self.assertEqual(self.watch.due(), [])

    def test_mixed_soft_and_hazard_is_never_added(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['LIQUIDITY_BELOW_MIN', 'ACTIVE_FREEZE_AUTHORITY'])
        self.watch.after_handle(c)
        self.assertEqual(self.watch.entries, {})

    def test_provider_failure_neither_adds_nor_removes(self):
        c = self.candidate()
        self.decide('screen', 'FAILED', ['PROVIDER_ERROR:HTTP_503'])
        self.watch.after_handle(c)
        self.assertEqual(self.watch.entries, {})
        self.decide('screen', 'REJECT', ['TOO_YOUNG'])
        self.watch.after_handle(c)
        self.decide('screen', 'FAILED', ['PROVIDER_ERROR:HTTP_503'])
        self.watch.after_handle(c)
        self.assertIn(MINT, self.watch.entries)
        self.assertEqual(self.events('watch_end'), [])

    def test_rescreen_is_due_only_after_the_interval(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['TOO_YOUNG'])
        self.watch.after_handle(c)
        self.now[0] += 299
        self.assertEqual(self.watch.due(), [])
        self.now[0] += 1
        self.assertEqual(self.watch.due(), [MINT])

    def test_rescreen_hands_back_the_stored_candidate_and_schedules_the_next(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['TOO_YOUNG'])
        self.watch.after_handle(c)
        self.now[0] += 300
        got = []
        self.assertEqual(self.watch.rescreen(lambda cand, source: got.append((cand, source))), 1)
        self.assertEqual(got[0][0], c)
        self.assertEqual((got[0][0].pool, got[0][0].slot, got[0][0].migrated_at, got[0][0].seq, got[0][0].payload_hash),
                         ('pool', 7, 500.0, 3, 'h'))
        self.assertEqual(self.watch.rescreen(lambda *a: got.append(a)), 0)    # not again before the interval
        self.assertEqual(len(self.events('watch_rescreen')), 1)

    def test_a_failing_handler_does_not_hot_loop(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['TOO_YOUNG'])
        self.watch.after_handle(c)
        self.now[0] += 300
        with self.assertRaises(RuntimeError):
            self.watch.rescreen(lambda *a: (_ for _ in ()).throw(RuntimeError('x')))
        self.assertEqual(self.watch.due(), [])

    def test_expires_after_watch_hours(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['TOO_YOUNG'])
        self.watch.after_handle(c)
        self.now[0] += 6 * 3600 + 1
        self.assertEqual(self.watch.due(), [])
        self.assertEqual(self.watch.entries, {})
        self.assertEqual(len(self.events('watch_end')), 1)
        # an expired mint can never be watched again, even if it is soft-rejected again later
        self.decide('screen', 'REJECT', ['TOO_YOUNG'])
        self.watch.after_handle(c)
        self.assertEqual(self.watch.entries, {})

    def test_not_expired_just_inside_the_window(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['TOO_YOUNG'])
        self.watch.after_handle(c)
        self.now[0] += 6 * 3600
        self.assertEqual(self.watch.due(), [MINT])

    def test_a_later_hazard_ends_the_watch(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['LIQUIDITY_BELOW_MIN'])
        self.watch.after_handle(c)
        self.decide('screen', 'REJECT', ['ACTIVE_MINT_AUTHORITY'])
        self.watch.after_handle(c)
        self.assertEqual(self.watch.entries, {})
        self.assertIn('HAZARD', self.events('watch_end')[0]['payload'])
        self.now[0] += 10 ** 6
        self.assertEqual(self.watch.due(), [])

    def test_entering_ends_the_watch(self):
        c = self.candidate()
        self.decide('screen', 'REJECT', ['LIQUIDITY_BELOW_MIN'])
        self.watch.after_handle(c)
        self.decide('screen', 'PASS', [])
        self.decide('entry', 'BUY', ['ENTRY'])
        self.watch.after_handle(c)
        self.assertEqual(self.watch.entries, {})
        self.assertIn('ENTERED', self.events('watch_end')[0]['payload'])

    def test_per_pass_bound_oldest_first(self):
        mints = ['%s' % chr(65 + i) * 32 for i in range(4)]
        self.watch = self.make(watch_max_per_pass=2)
        for i, m in enumerate(mints):
            self.now[0] = 1000.0 + i
            c = self.candidate(m)
            self.decide('screen', 'REJECT', ['TOO_YOUNG'], mint=m)
            self.watch.after_handle(c)
        self.now[0] = 5000.0
        self.assertEqual(self.watch.due(), mints[:2])

    def test_state_survives_a_restart_and_ended_mints_stay_ended(self):
        a, b = 'A' * 32, 'B' * 32
        for m in (a, b):
            c = self.candidate(m)
            self.decide('screen', 'REJECT', ['TOO_YOUNG'], mint=m)
            self.watch.after_handle(c)
        self.decide('screen', 'REJECT', ['ACTIVE_MINT_AUTHORITY'], mint=b)
        self.watch.after_handle(self.watch.candidate(b))
        self.store.close()
        self.now[0] += 100
        self.open()
        self.assertEqual(sorted(self.watch.entries), [a])
        self.assertIn(b, self.watch.ended)
        self.assertEqual(self.watch.entries[a]['first_at'], 1000.0)          # the 6h window is NOT restarted
        self.assertEqual(self.watch.due(), [a])                              # re-screened soon after a restart
        self.decide('screen', 'REJECT', ['TOO_YOUNG'], mint=b)
        self.watch.after_handle(self.watch.candidate(b))
        self.assertNotIn(b, self.watch.entries)

    def test_missing_candidate_row_ends_cleanly(self):
        self.watch.entries['Q' * 32] = {'first_at': 1000.0, 'next_at': 0.0, 'source': 'x'}
        self.assertEqual(self.watch.rescreen(lambda *a: self.fail('must not run')), 0)
        self.assertEqual(self.watch.entries, {})


if __name__ == '__main__':
    unittest.main()
