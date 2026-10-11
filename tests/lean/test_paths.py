"""L07R: lean.paths (price-path recorder) on a real Store and a real low-lane Helius client; only HTTP is faked."""
import base64
import hashlib
import json
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from desk.security import TOKEN_PROGRAM, base58
from lean import adapters as A, paper, paths as L, providers as P
from lean.candidates import Screen
from lean.store import Store

KEY = 'TEST-HELIUS-KEY-0000'
PCFG = paper.PaperConfig(fee_lamports=50_000, slippage_bps=50)
BASE0, GROSS0, SPENDABLE0 = 2 * 10 ** 14, 80 * 10 ** 9, 79 * 10 ** 9


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def key(tag):
    return base58(hashlib.sha256(tag.encode()).digest())


def vault(amount):
    data = bytearray(165)
    data[64:72] = int(amount).to_bytes(8, 'little')
    return {'data': [base64.b64encode(bytes(data)).decode(), 'base64'], 'owner': TOKEN_PROGRAM, 'lamports': 2_039_280,
            'executable': False}


def features(mint, **extra):
    return {'mint': mint, 'pool': key('pool' + mint), 'pool_base_token_account': key('base' + mint),
            'pool_quote_token_account': key('quote' + mint), 'decimals': 6, 'base_reserve_raw': str(BASE0),
            'quote_gross_raw': str(GROSS0), 'quote_spendable_raw': str(SPENDABLE0), 'holder_check': 'OK',
            'market_cap_usd': '90000', **extra}


class Clock:
    def __init__(self, start=1_800_000_000.0):
        self.t, self.sleeps = start, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class Resp:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}

    def read(self, n):
        return self.body[:n]

    def close(self):
        pass


class Rpc:
    """Fake Helius getMultipleAccounts over HTTP: vault amounts by pubkey; ``status`` forces an HTTP error."""

    def __init__(self):
        self.amounts, self.requests, self.status = {}, [], 200

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        assert body['method'] == 'getMultipleAccounts'
        keys = body['params'][0]
        self.requests.append(keys)
        if self.status != 200:
            return Resp(b'{"error":"slow down"}', self.status)
        value = [vault(self.amounts[k]) if k in self.amounts else None for k in keys]
        return Resp(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'context': {'slot': 7}, 'value': value}}).encode())


class Base(unittest.TestCase):
    def setUp(self):
        P.configure_lanes()
        self.addCleanup(P.configure_lanes)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.clock = Clock()
        self.rpc = Rpc()
        self.store = self.open_store()

    def open_store(self):
        store = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='test', strategy_version='lean-1',
                      clock=self.clock.now)
        self.addCleanup(store.close)
        return store

    def transport(self, lane):
        c = self.clock
        return P.Transport('helius', opener=self.rpc, clock=c.now, monotonic=c.now, sleep=c.sleep, rng=lambda: 0.5, lane=lane)

    def recorder(self, store=None, **kw):
        kw.setdefault('path_hours', 6.0)
        return L.PathRecorder(store=store or self.store, helius=P.Helius(KEY, self.transport('low')), pcfg=PCFG,
                              code_version='test', strategy_version='lean-1', pool_fee_bps=25, clock=self.clock.now,
                              sleep=self.clock.sleep, **kw)

    def add_pools(self, n, *, quote=GROSS0, base=BASE0, prefix='m'):
        mints = [key('%s%d' % (prefix, i)) for i in range(n)]
        for m in mints:
            f = features(m)
            self.rpc.amounts[f['pool_base_token_account']] = base
            self.rpc.amounts[f['pool_quote_token_account']] = quote
        return mints

    def start(self, rec, mints, **kw):
        for m in mints:
            self.assertTrue(rec.start(m, None, features(m), entered=False, stage='screen', reasons=['MARKET_CAP_ABOVE_MAX'], **kw))

    def rows(self, kind, store=None):
        return [dict(r, meta=json.loads(r['meta'])) for r in (store or self.store).rows('observations', kind=kind, limit=100000)]


class BatchingTests(Base):
    def test_250_pools_are_5_calls_of_at_most_100_accounts(self):
        """getMultipleAccounts takes at most 100 ACCOUNTS and a pool needs two vaults: 250 pools = 5 calls (50 pools each).
        The low lane (2/s, burst 2) grants 2 at once; this recorder thread paces the other 3 (0.5 s each) inside its
        12 s budget, so every pool is marked in this poll and nothing bursts past the lane."""
        rec = self.recorder()
        mints = self.add_pools(250)
        self.start(rec, mints)
        self.assertEqual(rec.poll(), 250)
        self.assertEqual(len(self.rpc.requests), 5)
        self.assertTrue(all(len(keys) <= P.MAX_MULTIPLE_ACCOUNTS for keys in self.rpc.requests))
        sent = [k for keys in self.rpc.requests for k in keys]
        self.assertEqual(len(sent), len(set(sent)))
        self.assertEqual(set(sent), {features(m)[k] for m in mints for k in ('pool_base_token_account', 'pool_quote_token_account')})
        self.assertEqual(self.clock.sleeps, [0.5, 0.5, 0.5])
        self.assertEqual(rec.health()['paced'], 3)
        self.assertEqual({r['mint'] for r in self.rows('path_mark')}, set(mints))

    def test_small_batches_and_no_paths_no_calls(self):
        rec = self.recorder()
        self.assertEqual(rec.poll(), 0)
        self.assertEqual(self.rpc.requests, [])
        mints = self.add_pools(50)
        self.start(rec, mints)
        rec.poll()
        self.assertEqual([len(k) for k in self.rpc.requests], [100])


class MarkTests(Base):
    def test_mark_is_adapters_mark_of_the_reference_quantity(self):
        rec = self.recorder(ref_size_sol='0.2')
        soft, held = self.add_pools(2)
        self.start(rec, [soft])
        position = mock.Mock(qty_raw=777_000_000, cost_lamports=123_000_000)
        self.assertTrue(rec.start(held, 5, features(held), entered=True, stage='entry', reasons=[], position=position))
        self.rpc.amounts[features(held)['pool_quote_token_account']] = GROSS0 * 2      # the held pool doubled
        rec.poll()
        marks = {r['mint']: r['meta'] for r in self.rows('path_mark')}
        spent = 200_000_000 * (10_000 - 25) // 10_000
        ref_qty = BASE0 * spent // (GROSS0 + spent)
        starts = {r['mint']: r['meta'] for r in self.rows('path_start')}
        self.assertEqual(starts[soft]['target']['qty_raw'], ref_qty)
        self.assertEqual(starts[soft]['target']['basis'], 'ref_size')
        self.assertEqual((starts[held]['target']['qty_raw'], starts[held]['target']['basis'], starts[held]['entered']),
                         (777_000_000, 'fill', True))
        self.assertEqual(Decimal(marks[soft]['net_sol']), A.mark(ref_qty, BASE0, GROSS0, pool_fee_bps=25, pcfg=PCFG))
        self.assertEqual(Decimal(marks[held]['net_sol']), A.mark(777_000_000, BASE0, 2 * GROSS0, pool_fee_bps=25, pcfg=PCFG))
        self.assertEqual((marks[held]['base_raw'], marks[held]['quote_raw'], marks[held]['status'], marks[held]['slot']),
                         (BASE0, 2 * GROSS0, 'OK', 7))
        self.assertEqual(Decimal(marks[soft]['liquidity_sol']), Decimal(2 * SPENDABLE0) / 10 ** 9)
        raw = json.loads(self.rows('path_mark')[0]['raw'])
        self.assertEqual(set(raw), {'base_raw', 'quote_raw', 'slot'})

    def test_closed_vault_and_empty_pool_are_recorded_as_statuses(self):
        rec = self.recorder()
        gone, empty = self.add_pools(2)
        self.start(rec, [gone, empty])
        del self.rpc.amounts[features(gone)['pool_base_token_account']]
        self.rpc.amounts[features(empty)['pool_quote_token_account']] = 0
        rec.poll()
        marks = {r['mint']: r['meta'] for r in self.rows('path_mark')}
        self.assertEqual((marks[gone]['status'], marks[gone]['net_sol']), ('ACCOUNT_MISSING', None))
        self.assertEqual((marks[empty]['status'], marks[empty]['net_sol']), ('EMPTY_POOL', None))


class SheddingTests(Base):
    def test_429_sheds_the_recorder_and_the_rest_of_the_poll_is_gaps(self):
        rec = self.recorder()
        mints = self.add_pools(120)                                      # 3 calls per poll
        self.start(rec, mints)
        self.rpc.status = 429
        self.assertEqual(rec.poll(), 0)
        self.assertEqual(len(self.rpc.requests), 1)                       # the 429; the 2 other chunks shed, unsent
        h = rec.health()
        self.assertEqual((h['shed'], h['gaps'], h['marks'], h['errors_by_code'].get('HTTP_429')), (1, 120, 0, 1))
        self.assertEqual(self.clock.sleeps, [])                           # a shed window is never waited
        self.rpc.status = 200
        self.clock.t += 15
        rec.poll()                                                        # still inside the 30 s window
        self.assertEqual((len(self.rpc.requests), rec.health()['shed']), (1, 2))
        self.clock.t += 16
        self.assertEqual(rec.poll(), 120)
        self.assertEqual(len(self.rpc.requests), 4)

    def test_a_429_on_the_main_lane_sheds_the_recorder(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(3))
        self.rpc.status = 429
        main = self.transport('main')
        main.max_attempts = 1
        with self.assertRaises(P.ProviderError):
            main.call('rpc:getAccountInfo', lambda: P.Request('https://x.invalid/', data=b'{"method":"getMultipleAccounts","params":[[]]}'))
        self.rpc.status, before = 200, len(self.rpc.requests)
        self.assertEqual(rec.poll(), 0)
        self.assertEqual(len(self.rpc.requests), before)                  # the recorder sent nothing
        self.assertEqual(rec.health()['shed'], 1)


class ResumeTests(Base):
    def test_restart_resumes_the_live_paths_from_the_store(self):
        rec = self.recorder(path_hours=1.0)
        old = self.add_pools(1, prefix='old')
        self.start(rec, old)
        self.clock.t += 1800
        live = self.add_pools(3, prefix='live')
        self.start(rec, live)
        rec.poll()
        before = rec.active()
        self.store.close()                                                # the process dies
        self.clock.t += 2400                                              # 'old' ended while we were down
        store = self.open_store()
        rec2 = self.recorder(store=store, path_hours=1.0)
        self.assertEqual(rec2.resume(), 3)
        self.assertEqual(rec2.active(), {m: before[m] for m in live})
        ends = self.rows('path_end', store)
        self.assertEqual([(r['mint'], r['meta']['why']) for r in ends], [(old[0], 'EXPIRED_WHILE_DOWN')])
        self.assertEqual(rec2.poll(), 3)
        self.assertEqual(json.loads(json.dumps(store.latest_event('paths_active')[1]['active'])), sorted(before[m] for m in live))
        self.assertEqual(rec2.health()['resumed'], 3)

    def test_resume_without_checkpoint_or_with_a_bad_one_is_harmless(self):
        rec = self.recorder()
        self.assertEqual(rec.resume(), 0)
        self.store.record('paths_active', {'active': [999999]}, code_version='test', strategy_version='lean-1')
        self.assertEqual(rec.resume(), 0)
        self.assertEqual(rec.health()['errors_by_code'], {'RESUME_ROW_INVALID': 1})


class TimingAndAppendOnlyTests(Base):
    def test_path_starts_at_start_and_stops_after_path_hours(self):
        rec = self.recorder(path_hours=1.0, interval_s=15.0)
        mint = self.add_pools(1)[0]
        t0 = self.clock.t
        self.start(rec, [mint])
        polls = 0
        while self.clock.t < t0 + 3600 + 60:
            rec.poll()
            polls += 1
            self.clock.t += 15
        marks = self.rows('path_mark')
        self.assertEqual(len(marks), 240)                                 # every 15 s in [t0, t0 + 1 h)
        self.assertTrue(all(t0 <= r['ts'] < t0 + 3600 for r in marks))
        ends = self.rows('path_end')
        self.assertEqual([(r['meta']['why'], r['ts'], r['meta']['marks_this_process']) for r in ends], [('COMPLETE', t0 + 3600, 240)])
        self.assertEqual(len(self.rpc.requests), 240)                     # nothing polled after the end
        self.assertEqual(rec.active(), {})
        self.assertEqual(self.store.latest_event('paths_active')[1]['active'], [])

    def test_rows_are_append_only(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(3))
        rec.poll()
        first = self.store.rows('observations', limit=100000)
        self.clock.t += 15
        rec.poll()
        after = self.store.rows('observations', limit=100000)
        self.assertEqual(after[:len(first)], first)                       # earlier rows never change
        self.assertEqual(len(after), len(first) + 3)
        for sql in ("UPDATE observations SET meta='{}' WHERE kind='path_mark'", "DELETE FROM observations WHERE kind='path_mark'",
                    "UPDATE events SET payload='{}' WHERE kind='paths_active'"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.db.execute(sql)

    def test_duplicates_and_capacity(self):
        rec = self.recorder(max_paths=2)
        a, b, c = self.add_pools(3)
        self.start(rec, [a, b])
        self.assertFalse(rec.start(a, None, features(a), entered=False, stage='screen', reasons=[]))
        self.assertFalse(rec.start(c, None, features(c), entered=False, stage='screen', reasons=[]))
        h = rec.health()
        self.assertEqual((h['duplicates'], h['capacity_dropped'], h['active_paths']), (1, 1, 2))


class EligibilityAndOutcomeTests(Base):
    def screen(self, mint, passed, reasons=(), error=None):
        return Screen(passed, tuple(reasons), features(mint), (), (), error)

    def test_only_hazard_free_screens_are_recorded(self):
        rec = self.recorder()
        m = self.add_pools(6)
        self.assertTrue(rec.on_candidate(m[0], None, self.screen(m[0], False, ['MARKET_CAP_ABOVE_MAX'])))
        self.assertTrue(rec.on_candidate(m[1], None, self.screen(m[1], False, ['LIQUIDITY_BELOW_MIN', 'MARKET_CAP_BELOW_MIN'])))
        self.assertFalse(rec.on_candidate(m[2], None, self.screen(m[2], False, ['ACTIVE_MINT_AUTHORITY'])))
        self.assertFalse(rec.on_candidate(m[3], None, self.screen(m[3], False, ['MARKET_CAP_ABOVE_MAX', 'LP_FREEZE_AUTHORITY'])))
        self.assertFalse(rec.on_candidate(m[4], None, self.screen(m[4], False, ['HOLDERS_UNAVAILABLE'],
                                                                   error={'code': 'HOLDERS_UNAVAILABLE', 'transient': True})))
        self.assertEqual(rec.health()['hazard_skipped'], 3)
        starts = {r['mint']: r['meta'] for r in self.rows('path_start')}
        self.assertEqual(set(starts), {m[0], m[1]})
        self.assertEqual(starts[m[0]]['not_entered'], {'stage': 'screen', 'reasons': ['MARKET_CAP_ABOVE_MAX']})

    def test_entry_outcome_comes_from_this_attempts_decisions(self):
        rec = self.recorder()
        skip, failed = self.add_pools(2)
        for mint in (skip, failed):
            self.store.add_decision('screen', 'PASS', mint=mint, reasons=[])
        self.store.add_decision('entry', 'SKIP', mint=skip, reasons=['ENTRY_THROTTLE'])
        self.store.add_error('HTTP_400', transient=False, mint=failed)
        self.assertTrue(rec.on_candidate(skip, 1, self.screen(skip, True)))
        self.assertTrue(rec.on_candidate(failed, 2, self.screen(failed, True)))
        starts = {r['mint']: r['meta'] for r in self.rows('path_start')}
        self.assertEqual(starts[skip]['not_entered'], {'stage': 'entry', 'reasons': ['ENTRY_THROTTLE']})
        self.assertEqual(starts[failed]['not_entered'], {'stage': 'quote', 'reasons': ['ENTRY_FAILED:HTTP_400']})
        self.assertEqual(starts[skip]['candidate_id'], 1)


class FailureIsolationTests(Base):
    def test_store_and_provider_failures_are_counted_never_raised(self):
        rec = self.recorder()
        mints = self.add_pools(2)
        self.start(rec, mints)
        with mock.patch.object(self.store, 'add_observations', side_effect=sqlite3.OperationalError('disk I/O error')):
            self.assertEqual(rec.poll(), 0)
        rec.helius = mock.Mock(get_multiple_accounts=mock.Mock(side_effect=RuntimeError('boom')))
        self.assertEqual(rec.poll(), 0)
        h = rec.health()
        self.assertEqual((h['write_failures'], h['gaps'], h['marks']), (1, 4, 0))
        self.assertEqual(h['errors_by_code'], {'WRITE_FAILED': 1, 'POLL_CALL_FAILED': 1})
        self.assertFalse(rec.start('not-a-key', None, {}, entered=False, stage='screen', reasons=[]))
        with mock.patch.object(self.store, 'add_observation', side_effect=sqlite3.OperationalError('locked')):
            m = self.add_pools(1, prefix='x')[0]
            self.assertFalse(rec.start(m, None, features(m), entered=False, stage='screen', reasons=[]))
        self.assertNotIn(m, rec.active())                                 # a failed start leaves nothing behind
        with mock.patch.object(self.store, 'rows', side_effect=sqlite3.OperationalError('locked')):
            self.assertFalse(rec.on_candidate(m, None, Screen(True, (), features(m))))


class ConfigTests(unittest.TestCase):
    def test_paths_config(self):
        self.assertFalse(L.config(None)['enabled'])
        self.assertFalse(L.config({})['enabled'])
        cfg = L.config({'enabled': True, 'path_hours': 6, 'interval_s': 15})
        self.assertEqual((cfg['enabled'], cfg['path_hours'], cfg['interval_s'], cfg['ref_size_sol']), (True, 6.0, 15.0, '0.2'))
        for bad in ({'enabled': 'yes'}, {'path_hour': 6}, {'interval_s': 0}, {'path_hours': 100}, {'ref_size_sol': 'x'},
                    {'max_paths': 0}, []):
            with self.assertRaises(Exception, msg=repr(bad)):
                L.config(bad)


if __name__ == '__main__':
    unittest.main()
