"""SYNTHETIC_TEST_ONLY: the price-path recorder (L07). Fake Helius, real Store, injected clocks. No network."""
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from desk.programs import unbase58
from desk.security import TOKEN_PROGRAM, base58
from lean import paths
from lean.paths import NON_HAZARD_REASONS, PathRecorder, PathTarget, ShareBucket, SOL_MINT, compute_mark, hazard_free, target_from_screen
from lean.providers import ProviderError
from lean.store import Store

CODE, STRATEGY = 'abc1234', 'default-v1'


def pk(n):
    """A valid, distinct pubkey (32 bytes)."""
    return base58(bytes([n % 256, (n // 256) % 256]) + bytes(30))


def features(**over):
    f = {'token_program': TOKEN_PROGRAM, 'decimals': 6, 'virtual_quote_reserves_raw': 30_000_000_000, 'quote_gross_raw': 10_000_000_000,
         'quote_spendable_raw': 9_900_000_000, 'base_reserve_raw': 800_000_000_000_000}
    f.update(over)
    return f


class Screen:
    def __init__(self, passed=True, reasons=(), feats=None):
        self.passed, self.reasons, self.features = passed, list(reasons), features() if feats is None else feats


class Cand:
    def __init__(self, n):
        self.mint, self.pool = pk(1000 + n), pk(2000 + n)


def token_account(mint, owner, amount, program=TOKEN_PROGRAM):
    data = unbase58(mint) + unbase58(owner) + amount.to_bytes(8, 'little') + bytes(100)
    return {'owner': program, 'lamports': 2039280, 'data': data, 'executable': False}


class FakeHelius:
    """Answers getMultipleAccounts for the targets it knows: {vault pubkey: account | None}."""

    def __init__(self):
        self.accounts, self.calls, self.fail, self.slot = {}, [], None, 100

    def add_pool(self, target, base_raw, quote_raw):
        self.accounts[target.base_vault] = token_account(target.mint, target.pool, base_raw, target.token_program)
        self.accounts[target.quote_vault] = token_account(SOL_MINT, target.pool, quote_raw)

    def get_multiple_accounts(self, keys):
        self.calls.append(list(keys))
        if self.fail is not None:
            raise self.fail
        self.slot += 1
        return {'slot': self.slot, 'accounts': [self.accounts.get(k) for k in keys]}, b'{}', {}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.store = Store(Path(self.tmp.name) / 'lean.sqlite', initial_cash_sol='5', code_version=CODE, strategy_version=STRATEGY, clock=self.clock)
        self.helius = FakeHelius()
        self.bucket_clock = Clock(0.0)
        self.bucket = ShareBucket(4.0, 4, clock=self.bucket_clock)
        self.rec = self.make()

    def make(self, **kw):
        kw.setdefault('bucket', self.bucket)
        return PathRecorder(store=self.store, helius=self.helius, code_version=CODE, strategy_version=STRATEGY, clock=self.clock, **kw)

    def track(self, n, **kw):
        c, s = Cand(n), kw.pop('screen', None) or Screen()
        self.assertTrue(self.rec.start(c, s, entered=kw.pop('entered', False), **kw))
        t = target_from_screen(c, s)
        self.helius.add_pool(t, 800_000_000_000_000, 10_000_000_000)
        return c, t

    def kinds(self, kind):
        return self.store.rows('observations', where=f"WHERE kind='{kind}'")

    def advance(self, seconds):
        self.clock.t += seconds
        self.bucket_clock.t += seconds


class TestMarkMath(unittest.TestCase):
    def test_known_answer_matches_the_screen_definition(self):
        t = PathTarget('m', 'p', 'b', 'q', TOKEN_PROGRAM, 6, 30_000_000_000, 100_000_000)
        price, liq = compute_mark(t, 800_000_000_000_000, 10_000_000_000)
        # (10 + 30) SOL / 800,000,000 tokens
        self.assertEqual(Decimal(price), Decimal('0.00000005'))
        # 2 * (10 SOL - 0.1 SOL fees)
        self.assertEqual(Decimal(liq), Decimal('19.8'))

    def test_empty_pool_has_no_price(self):
        t = PathTarget('m', 'p', 'b', 'q', TOKEN_PROGRAM, 6, 0, 0)
        self.assertIsNone(compute_mark(t, 0, 5))
        self.assertIsNone(compute_mark(t, 5, 0))

    def test_fees_never_make_liquidity_negative(self):
        t = PathTarget('m', 'p', 'b', 'q', TOKEN_PROGRAM, 6, 0, 10_000)
        self.assertEqual(Decimal(compute_mark(t, 1000, 5_000)[1]), 0)


class TestHazardFilter(unittest.TestCase):
    def test_passed_and_market_only_failures_are_tracked(self):
        self.assertTrue(hazard_free(Screen()))
        for reason in sorted(NON_HAZARD_REASONS):
            self.assertTrue(hazard_free(Screen(False, [reason])))
        self.assertTrue(hazard_free(Screen(False, sorted(NON_HAZARD_REASONS))))

    def test_any_hazard_reason_blocks_tracking(self):
        for reasons in (['MINT_AUTHORITY_PRESENT'], ['MARKET_CAP_BELOW_MIN', 'FREEZE_AUTHORITY_PRESENT'], ['PROVIDER_ERROR'], []):
            self.assertFalse(hazard_free(Screen(False, reasons)), reasons)

    def test_missing_or_garbled_features_are_not_tracked(self):
        c = Cand(1)
        for bad in ({}, features(token_program='nope'), features(decimals=-1), features(quote_gross_raw='x'), features(quote_spendable_raw=10 ** 12),
                    features(virtual_quote_reserves_raw=-1)):
            self.assertIsNone(target_from_screen(c, Screen(True, (), bad)), bad)
        self.assertIsNotNone(target_from_screen(c, Screen()))


class TestRecorder(Base):
    def test_start_writes_features_entry_flag_and_reason(self):
        c = Cand(1)
        s = Screen(False, ['LIQUIDITY_BELOW_MIN'])
        self.assertTrue(self.rec.start(c, s, entered=False, reason='COST_CAP'))
        row = self.kinds('path_start')[0]
        meta, raw = json.loads(row['meta']), json.loads(row['raw'])
        self.assertEqual((row['mint'], meta['entered'], meta['reason']), (c.mint, False, 'COST_CAP'))
        self.assertEqual(raw['features'], s.features)
        self.assertEqual(raw['screen_reasons'], ['LIQUIDITY_BELOW_MIN'])
        self.assertEqual((row['code_version'], row['strategy_version']), (CODE, STRATEGY))

    def test_reason_defaults_to_the_screen_reasons(self):
        self.rec.start(Cand(1), Screen(False, ['MARKET_CAP_ABOVE_MAX']), entered=False)
        self.assertEqual(json.loads(self.kinds('path_start')[0]['meta'])['reason'], 'MARKET_CAP_ABOVE_MAX')

    def test_hazard_candidates_are_not_started_or_written(self):
        self.assertFalse(self.rec.start(Cand(1), Screen(False, ['MINT_AUTHORITY_PRESENT']), entered=False))
        self.assertEqual(self.kinds('path_start'), [])

    def test_duplicate_start_is_ignored(self):
        c = Cand(1)
        self.assertTrue(self.rec.start(c, Screen(), entered=False))
        self.assertFalse(self.rec.start(c, Screen(), entered=True))
        self.assertEqual(len(self.kinds('path_start')), 1)

    def test_marks_every_interval_for_path_hours_then_ends(self):
        self.rec = self.make(path_hours=0.01, interval_s=15)       # 36 s
        c, t = self.track(1)
        marks = []
        for _ in range(4):
            marks.append(self.rec.poll())
            self.advance(15)
        self.assertEqual(marks[:3], [1, 1, 1])                    # t=0, 15, 30
        self.assertEqual(marks[3], 0)                              # t=45 > 36: over
        rows = self.kinds('path_mark')
        self.assertEqual([json.loads(r['meta'])['n'] for r in rows], [1, 2, 3])
        end = self.kinds('path_end')
        self.assertEqual(len(end), 1)
        self.assertEqual(json.loads(end[0]['meta'])['marks'], 3)
        self.assertEqual(json.loads(end[0]['meta'])['why'], 'COMPLETE')
        self.assertEqual(self.rec.health()['active_paths'], 0)

    def test_not_due_before_the_interval(self):
        self.track(1)
        self.assertEqual(self.rec.poll(), 1)
        self.advance(14)
        self.assertEqual(self.rec.poll(), 0)
        self.advance(1)
        self.assertEqual(self.rec.poll(), 1)

    def test_mark_content(self):
        c, t = self.track(1)
        self.rec.poll()
        m = json.loads(self.kinds('path_mark')[0]['meta'])
        self.assertEqual(m['status'], 'OK')
        self.assertEqual(Decimal(m['price_sol']), Decimal('0.00000005'))
        self.assertEqual((m['base_raw'], m['quote_raw'], m['slot']), (800_000_000_000_000, 10_000_000_000, 101))
        self.assertEqual(json.loads(self.kinds('path_mark')[0]['raw']), m)
        self.assertEqual(self.kinds('path_mark')[0]['mint'], c.mint)

    def test_250_pools_use_batches_of_at_most_100_accounts(self):
        self.bucket = ShareBucket(1000, 1000, clock=self.bucket_clock)
        self.rec = self.make(bucket=self.bucket)
        for n in range(250):
            self.track(n)
        self.assertEqual(self.rec.poll(), 250)
        self.assertEqual(sorted(len(c) for c in self.helius.calls), [100] * 5)                          # 250 pools = 500 accounts
        self.assertEqual(len({k for c in self.helius.calls for k in c}), 500)    # every vault asked exactly once
        self.assertEqual(sum(len(c) for c in self.helius.calls), 500)

    def test_partial_last_batch(self):
        self.bucket = ShareBucket(1000, 1000, clock=self.bucket_clock)
        self.rec = self.make(bucket=self.bucket)
        for n in range(75):
            self.track(n)
        self.rec.poll()
        self.assertEqual([len(c) for c in self.helius.calls], [100, 50])

    def test_each_pool_gets_its_own_reserves(self):
        a, ta = self.track(1)
        b, tb = self.track(2)
        self.helius.add_pool(tb, 400_000_000_000_000, 10_000_000_000)
        self.rec.poll()
        by_mint = {r['mint']: json.loads(r['meta']) for r in self.kinds('path_mark')}
        self.assertEqual(Decimal(by_mint[a.mint]['price_sol']) * 2, Decimal(by_mint[b.mint]['price_sol']))

    def test_closed_vault_is_recorded_as_a_status_not_dropped(self):
        c, t = self.track(1)
        self.helius.accounts[t.quote_vault] = None
        self.rec.poll()
        m = json.loads(self.kinds('path_mark')[0]['meta'])
        self.assertEqual((m['status'], m['price_sol']), ('ACCOUNT_MISSING', None))

    def test_wrong_owner_mint_or_program_is_malformed(self):
        c, t = self.track(1)
        self.helius.accounts[t.base_vault] = token_account(pk(5), t.pool, 10)                        # another mint
        self.rec.poll()
        self.advance(15)
        self.helius.add_pool(t, 1, 1)
        self.helius.accounts[t.quote_vault] = token_account(SOL_MINT, pk(6), 10)                     # another owner
        self.rec.poll()
        self.advance(15)
        self.helius.add_pool(t, 1, 1)
        self.helius.accounts[t.quote_vault] = dict(token_account(SOL_MINT, t.pool, 10), owner=pk(7))   # another program
        self.rec.poll()
        self.advance(15)
        self.helius.accounts[t.quote_vault] = {'owner': TOKEN_PROGRAM, 'lamports': 1, 'data': b'short', 'executable': False}
        self.rec.poll()
        self.assertEqual([json.loads(r['meta'])['status'] for r in self.kinds('path_mark')], ['MALFORMED'] * 4)

    def test_empty_pool_status(self):
        c, t = self.track(1)
        self.helius.add_pool(t, 0, 10)
        self.rec.poll()
        self.assertEqual(json.loads(self.kinds('path_mark')[0]['meta'])['status'], 'EMPTY_POOL')

    def test_response_shape_mismatch_is_a_gap(self):
        self.track(1)
        self.helius.get_multiple_accounts = lambda keys: ({'slot': 1, 'accounts': []}, b'', {})
        self.assertEqual(self.rec.poll(), 0)
        self.assertEqual(self.kinds('path_mark'), [])
        self.assertEqual(self.rec.health()['gaps'], 1)

    def test_capacity_limit(self):
        self.rec = self.make(max_paths=2)
        self.track(1)
        self.track(2)
        self.assertFalse(self.rec.start(Cand(3), Screen(), entered=False))
        self.assertEqual(self.rec.health()['dropped_capacity'], 1)


class TestShedding(Base):
    def test_429_pauses_then_backs_off_exponentially_and_recovers(self):
        self.track(1)
        self.helius.fail = ProviderError('HTTP_429', True)
        self.assertEqual(self.rec.poll(), 0)                      # calls=1, pause 30 s
        self.assertEqual(len(self.helius.calls), 1)
        self.advance(15)
        self.rec.poll()
        self.assertEqual(len(self.helius.calls), 1)                # shed: no request during the pause
        self.assertEqual(self.rec.health()['shed'], 1)
        self.advance(16)
        self.rec.poll()                                            # t=31: asks again, fails again, pause 60 s
        self.assertEqual(len(self.helius.calls), 2)
        self.advance(59)
        self.rec.poll()
        self.assertEqual(len(self.helius.calls), 2)
        self.helius.fail = None
        self.advance(2)
        self.assertEqual(self.rec.poll(), 1)                       # recovered
        self.advance(15)
        self.helius.fail = ProviderError('HTTP_429', True)
        self.rec.poll()
        self.assertAlmostEqual(self.rec.pause_until - self.clock.t, 30)    # level reset after a success

    def test_pause_is_capped(self):
        self.rec = self.make(shed_base_s=30, shed_max_s=100)
        self.track(1)
        self.helius.fail = ProviderError('HTTP_429', True)
        for _ in range(8):
            self.advance(1000)
            self.rec.poll()
        self.assertLessEqual(self.rec.pause_until - self.clock.t, 100)

    def test_non_transient_error_backs_off_briefly(self):
        self.track(1)
        self.helius.fail = ProviderError('RESPONSE_INVALID', False)
        self.rec.poll()
        self.assertAlmostEqual(self.rec.pause_until - self.clock.t, 5)

    def test_gaps_are_counted_and_recorded_in_path_end(self):
        self.rec = self.make(path_hours=0.01)
        self.track(1)
        self.helius.fail = ProviderError('HTTP_429', True)
        self.rec.poll()
        self.advance(40)
        self.rec.poll()
        end = json.loads(self.kinds('path_end')[0]['meta'])
        self.assertGreaterEqual(end['gaps'], 1)

    def test_own_bucket_limits_calls_and_never_sleeps(self):
        self.bucket = ShareBucket(1.0, 1, clock=self.bucket_clock)      # 1 call/s
        self.rec = self.make(bucket=self.bucket)
        for n in range(150):
            self.track(n)                                                 # 3 chunks of 50 pools
        self.rec.poll()
        self.assertEqual(len(self.helius.calls), 1)                       # the other chunks skipped, not queued
        self.assertEqual(self.rec.health()['bucket_skips'], 2)
        self.assertEqual(self.rec.health()['gaps'], 100)

    def test_bucket_refills_with_time_only(self):
        b = ShareBucket(2.0, 2, clock=self.bucket_clock)
        self.assertEqual([b.take() for _ in range(3)], [True, True, False])
        self.bucket_clock.t += 0.5
        self.assertTrue(b.take())
        self.assertFalse(b.take())

    def test_default_share_is_forty_percent_of_ten_per_second(self):
        r = PathRecorder(store=self.store, helius=self.helius, code_version=CODE, strategy_version=STRATEGY)
        self.assertEqual((r.bucket.rate, r.interval_s, r.path_s), (4.0, 15.0, 6 * 3600.0))


class TestIsolation(Base):
    def test_store_failure_on_start_never_raises(self):
        self.store.add_observation = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk'))
        self.assertFalse(self.rec.start(Cand(1), Screen(), entered=False))
        self.assertEqual(self.rec.health()['errors'], 1)
        self.assertEqual(self.rec.health()['active_paths'], 0)

    def test_store_failure_on_mark_is_a_gap_and_the_path_continues(self):
        self.track(1)
        real = self.store.add_observation
        self.store.add_observation = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk'))
        self.assertEqual(self.rec.poll(), 0)
        self.store.add_observation = real
        self.advance(15)
        self.assertEqual(self.rec.poll(), 1)
        self.assertEqual(json.loads(self.kinds('path_mark')[0]['meta'])['n'], 1)    # numbering has no hole
        self.assertEqual(self.rec.health()['gaps'], 1)

    def test_garbage_input_never_raises(self):
        for c, s in ((None, None), (Cand(1), None), (Cand(1), 'x'), (object(), Screen())):
            self.assertFalse(self.rec.start(c, s, entered=False))

    def test_unexpected_exception_in_poll_is_swallowed(self):
        self.track(1)
        self.helius.get_multiple_accounts = lambda keys: (_ for _ in ()).throw(KeyboardInterrupt) if False else (_ for _ in ()).throw(ValueError('x'))
        self.assertEqual(self.rec.poll(), 0)

    def test_rows_are_append_only(self):
        self.track(1)
        self.rec.poll()
        for sql in ("UPDATE observations SET kind='x'", 'DELETE FROM observations'):
            with self.assertRaises(Exception):
                self.store.db.execute(sql)
        self.assertEqual(len(self.kinds('path_mark')), 1)


class TestResume(Base):
    def test_restart_continues_open_paths_without_a_second_start(self):
        c, t = self.track(1)
        self.rec.poll()
        self.advance(100)
        again = self.make()
        self.assertEqual(again.resume(), 1)
        self.assertEqual(again.poll(), 1)
        self.assertEqual(len(self.kinds('path_start')), 1)
        self.assertFalse(again.start(c, Screen(), entered=False))      # still tracked: no duplicate start

    def test_ended_and_expired_paths_are_not_resumed(self):
        self.rec = self.make(path_hours=0.01)
        self.track(1)
        self.advance(40)
        self.rec.poll()                                                 # ends
        self.track(2)
        self.advance(40)                                                # expired without a poll
        again = self.make(path_hours=0.01)
        self.assertEqual(again.resume(), 0)

    def test_a_path_closed_early_is_not_resumed(self):
        c, t = self.track(1)
        self.rec._end(c.mint, self.clock.t, 'MANUAL')                    # ended but its window has not expired
        again = self.make()
        self.assertEqual(again.resume(), 0)

    def test_marks_after_restart_keep_counting_from_the_stored_marks(self):
        self.track(1)
        self.rec.poll()
        self.advance(15)
        again = self.make()
        again.resume()
        again.poll()
        self.assertEqual([json.loads(r['meta'])['n'] for r in self.kinds('path_mark')], [1, 2])

    def test_run_loop_stops_on_event(self):
        stop = threading.Event()
        stop.set()
        self.rec.run(stop, tick=0.01)


class TestRunnerHook(unittest.TestCase):
    def test_recorder_exception_cannot_break_an_entry(self):
        from lean.runner import Runner

        class Boom:
            def start(self, *a, **k):
                raise RuntimeError('recorder bug')
        r = Runner.__new__(Runner)
        r.path_recorder = Boom()
        r._track(Cand(1), Screen(), True)                               # must not raise
        r.path_recorder = None
        r._track(Cand(1), Screen(), True)

    def test_runner_hands_every_screened_candidate_to_the_recorder(self):
        from tests.lean.test_runner import Harness, candidates

        class Spy:
            def __init__(self):
                self.starts = []

            def start(self, candidate, screen, *, entered, reason=None, candidate_id=None):
                self.starts.append((candidate['mint'], entered, reason))

        class Case(Harness):
            def runTest(self):
                pass
        case = Case()
        self.addCleanup(case.doCleanups)
        r = case.build(items=candidates(8), behaviour={'M01': 'low_liquidity', 'M02': 'mint_authority'}, cfg={'entry_probe_sol': 0.02, 'max_positions': 4})
        spy = r.path_recorder = Spy()
        case.jup.fail_mints = {'M03'}
        r.candidate_pass()
        by = {m: (e, why) for m, e, why in spy.starts}
        self.assertEqual(len(spy.starts), len(by))                      # once per candidate
        self.assertTrue(by['M00'][0])                                   # entered
        self.assertEqual(by['M03'], (False, 'QUOTE_FAILED'))
        self.assertEqual(by['M01'][0], False)                           # screen failures are handed over too (the recorder filters hazards)
        self.assertEqual(by['M07'], (False, 'FULL'))                    # cap reached: not entered, still handed over
        self.assertEqual(sorted(m for m, (e, w) in by.items() if e), ['M00', 'M04', 'M05', 'M06'])
        self.assertEqual(len(by), 8)

    def test_the_recorder_runs_in_its_own_thread_and_stops_with_the_runner(self):
        from tests.lean.test_runner import Harness, candidates

        class Case(Harness):
            def runTest(self):
                pass
        case = Case()
        self.addCleanup(case.doCleanups)
        r = case.build(items=[])

        class Rec:
            ran = threading.Event()

            def run(self, stop, tick=1.0):
                self.ran.set()
                stop.wait()
        r.path_recorder = Rec()
        t = threading.Thread(target=lambda: r.run(candidate_interval=0.01, position_interval=0.01, install_signals=False), daemon=True)
        t.start()
        try:
            started = Rec.ran.wait(5)
        finally:
            r.stop.set()
            t.join(5)
        self.assertTrue(started)
        self.assertFalse(t.is_alive())


if __name__ == '__main__':
    unittest.main()
