"""L07R/L07R2: lean.paths (price-path recorder) on its OWN paths.sqlite and a real low-lane Helius client; only HTTP is
faked. The recorder never sees the trader's store: these tests do not even create one."""
import base64
import hashlib
import json
import logging
import socket
import sqlite3
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from desk.security import TOKEN_PROGRAM, base58
from lean import adapters as A, paper, paths as L, providers as P
from lean.candidates import Screen

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
    """Fake Helius getMultipleAccounts over HTTP: vault amounts by pubkey; ``status`` forces an HTTP error and
    ``rpc_error`` a JSON-RPC error object."""

    def __init__(self):
        self.amounts, self.requests, self.status, self.rpc_error = {}, [], 200, None

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        keys = body['params'][0]
        self.requests.append(keys)
        if self.status != 200:
            return Resp(b'{"error":"slow down"}', self.status)
        if self.rpc_error is not None:
            return Resp(json.dumps({'jsonrpc': '2.0', 'id': 1, 'error': self.rpc_error}).encode())
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
        self.db = self.open_db()

    def open_db(self):
        db = L.PathStore(self.dir / 'paths.sqlite', clock=self.clock.now)
        self.addCleanup(db.close)
        return db

    def transport(self, lane):
        c = self.clock
        return P.Transport('helius', opener=self.rpc, clock=c.now, monotonic=c.now, sleep=c.sleep, rng=lambda: 0.5, lane=lane)

    def recorder(self, db=None, **kw):
        kw.setdefault('path_hours', 6.0)
        kw.setdefault('min_free_gb', 0)
        return L.PathRecorder(db=db or self.db, helius=P.Helius(KEY, self.transport('low')), pcfg=PCFG,
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
            self.clock.t += 0.001                                      # distinct start times: a stable oldest-first order

    def q(self, sql, db=None, *args):
        return (db or self.db).db.execute(sql, args).fetchall()


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
        self.assertEqual({r[0] for r in self.q('SELECT mint FROM path_marks')}, set(mints))

    def test_small_batches_and_no_paths_no_calls(self):
        rec = self.recorder()
        self.assertEqual(rec.poll(), 0)
        self.assertEqual(self.rpc.requests, [])
        self.start(rec, self.add_pools(50))
        rec.poll()
        self.assertEqual([len(k) for k in self.rpc.requests], [100])


class StorageTests(Base):
    def test_plain_indexed_tables_in_their_own_file(self):
        names = {r[0] for r in self.q("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        self.assertTrue({'paths', 'path_marks', 'path_gaps', 'path_ends', 'path_marks_mint_ts', 'path_gaps_mint_ts'} <= names)
        rec = self.recorder()
        mint = self.add_pools(1)[0]
        self.start(rec, [mint])
        rec.poll()
        plan = ' '.join(str(r) for r in self.q("EXPLAIN QUERY PLAN SELECT * FROM path_marks WHERE mint=? AND ts>=?", None, mint, 0))
        self.assertIn('path_marks_mint_ts', plan)
        row = dict(zip(L.PATH_COLUMNS, self.q('SELECT %s FROM paths' % ','.join(L.PATH_COLUMNS))[0]))
        self.assertEqual((row['mint'], row['entered'], row['stage'], json.loads(row['reason'])),
                         (mint, 0, 'screen', ['MARKET_CAP_ABOVE_MAX']))
        self.assertEqual(json.loads(row['features'])['market_cap_usd'], '90000')

    def test_rollover_past_max_db_gb_keeps_the_live_paths(self):
        rec = self.recorder(max_db_gb=0.0001)                           # ~107 kB
        mints = self.add_pools(40)
        self.start(rec, mints)
        with self.assertLogs('lean.paths', 'WARNING') as logs:
            for _ in range(12):
                rec.poll()
                self.clock.t += 15
                if rec.health()['rollovers']:
                    break
        self.assertEqual(rec.health()['rollovers'], 1)
        archives = sorted(p.name for p in self.dir.glob('paths-*.sqlite'))
        self.assertEqual(archives, ['paths-20270115.sqlite'])
        self.assertIn('rolled over to paths-20270115.sqlite', '\n'.join(logs.output))
        old = sqlite3.connect(self.dir / archives[0])
        self.addCleanup(old.close)
        self.assertGreater(old.execute('SELECT COUNT(*) FROM path_marks').fetchone()[0], 0)
        live = self.q('SELECT mint, carried_from FROM paths')                # the fresh file carries every live path
        self.assertEqual({m for m, _ in live}, set(mints))
        self.assertEqual({c for _, c in live}, {archives[0]})
        self.assertEqual(self.q('SELECT COUNT(*) FROM path_marks')[0][0], 0)
        self.assertEqual(rec.poll(), 40)                                  # and marking goes on in the new file
        self.assertEqual(self.q('SELECT COUNT(*) FROM path_marks')[0][0], 40)

    def test_low_disk_writes_nothing(self):
        rec = self.recorder(min_free_gb=1)
        self.start(rec, self.add_pools(3))
        with mock.patch.object(self.db, 'free_bytes', return_value=10):
            self.assertEqual(rec.poll(), 0)
        self.assertEqual((self.rpc.requests, rec.health()['disk_low']), ([], 1))
        self.assertEqual(self.q('SELECT COUNT(*) FROM path_marks')[0][0], 0)


class MarkTests(Base):
    def test_mark_is_adapters_mark_of_the_reference_quantity(self):
        rec = self.recorder(ref_size_sol='0.2')
        soft, held = self.add_pools(2)
        self.start(rec, [soft])
        fill = mock.Mock(qty_raw=777_000_000, sol_lamports=122_000_000, fee_lamports=1_000_000)
        self.assertTrue(rec.start(held, 5, features(held), entered=True, stage='entry', reasons=[], fill=fill))
        self.rpc.amounts[features(held)['pool_quote_token_account']] = GROSS0 * 2      # the held pool doubled
        rec.poll()
        marks = {r[0]: r for r in self.q('SELECT mint,ts,slot,base_raw,quote_raw,net_sol,liquidity_sol,status FROM path_marks')}
        targets = {m: json.loads(t) for m, t in self.q('SELECT mint,target FROM paths')}
        spent = 200_000_000 * (10_000 - 25) // 10_000
        ref_qty = BASE0 * spent // (GROSS0 + spent)
        self.assertEqual((targets[soft]['qty_raw'], targets[soft]['basis']), (ref_qty, 'ref_size'))
        self.assertEqual((targets[held]['qty_raw'], targets[held]['cost_lamports'], targets[held]['basis']),
                         (777_000_000, 123_000_000, 'fill'))
        self.assertEqual(Decimal(marks[soft][5]), A.mark(ref_qty, BASE0, GROSS0, pool_fee_bps=25, pcfg=PCFG))
        self.assertEqual(Decimal(marks[held][5]), A.mark(777_000_000, BASE0, 2 * GROSS0, pool_fee_bps=25, pcfg=PCFG))
        self.assertEqual(marks[held][2:5] + marks[held][7:], (7, BASE0, 2 * GROSS0, 'OK'))
        self.assertEqual(Decimal(marks[soft][6]), Decimal(2 * SPENDABLE0) / 10 ** 9)

    def test_closed_vault_and_empty_pool_are_recorded_as_statuses(self):
        rec = self.recorder()
        gone, empty = self.add_pools(2)
        self.start(rec, [gone, empty])
        del self.rpc.amounts[features(gone)['pool_base_token_account']]
        self.rpc.amounts[features(empty)['pool_quote_token_account']] = 0
        rec.poll()
        marks = dict((m, (s, n)) for m, s, n in self.q('SELECT mint,status,net_sol FROM path_marks'))
        self.assertEqual(marks, {gone: ('ACCOUNT_MISSING', None), empty: ('EMPTY_POOL', None)})


class SheddingAndGapTests(Base):
    def test_429_sheds_the_recorder_and_writes_gap_rows(self):
        rec = self.recorder()
        mints = self.add_pools(120)                                      # 3 calls per poll
        self.start(rec, mints)
        self.rpc.status = 429
        self.assertEqual(rec.poll(), 0)
        self.assertEqual(len(self.rpc.requests), 1)                       # the 429; the rest of the poll is not sent
        h = rec.health()
        self.assertEqual((h['gaps'], h['marks'], h['errors_by_code'].get('HTTP_429')), (120, 0, 1))
        self.assertEqual(self.q('SELECT cause, COUNT(*) FROM path_gaps GROUP BY cause'), [('HTTP_429', 120)])
        self.assertEqual(self.clock.sleeps, [])                           # a shed window is never waited
        self.rpc.status = 200
        self.clock.t += 15
        rec.poll()                                                        # still inside the 30 s window: shed, unsent
        self.assertEqual(len(self.rpc.requests), 1)
        self.assertEqual(dict(self.q('SELECT cause, COUNT(*) FROM path_gaps GROUP BY cause')), {'HTTP_429': 120, 'shed': 120})
        self.clock.t += 16
        self.assertEqual(rec.poll(), 120)
        self.assertEqual(len(self.rpc.requests), 4)

    def test_rpc_throttle_code_sheds_the_low_lane(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(60))                              # 2 calls
        self.rpc.rpc_error = {'code': -32005, 'message': 'rate limited'}
        rec.poll()
        self.assertEqual(len(self.rpc.requests), 1)                       # -32005 shed the lane: the 2nd call is not sent
        self.assertEqual(dict(self.q('SELECT cause, COUNT(*) FROM path_gaps GROUP BY cause')), {'RPC_ERROR': 50, 'shed': 10})

    def test_rpc_throttle_on_the_main_lane_sheds_the_recorder_too(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(3))
        self.rpc.rpc_error = {'code': -32005, 'message': 'rate limited'}
        with self.assertRaises(P.ProviderError):
            P.Helius(KEY, self.transport('main')).get_multiple_accounts([key('x')])
        self.rpc.rpc_error, before = None, len(self.rpc.requests)
        self.assertEqual(rec.poll(), 0)
        self.assertEqual(len(self.rpc.requests), before)

    def test_a_429_on_the_main_lane_sheds_the_recorder(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(3))
        self.rpc.status = 429
        main = self.transport('main')
        main.max_attempts = 1
        with self.assertRaises(P.ProviderError):
            main.call('rpc:getMultipleAccounts', lambda: P.Request('https://x.invalid/', data=b'{"params":[[]]}'))
        self.rpc.status, before = 200, len(self.rpc.requests)
        self.assertEqual(rec.poll(), 0)
        self.assertEqual(len(self.rpc.requests), before)                  # the recorder sent nothing
        self.assertEqual(rec.health()['shed'], 1)

    def test_rotation_never_always_drops_the_newest_paths(self):
        """The time budget allows 2 calls per poll (no pacing room): 150 pools = 3 calls. Without rotation the newest 50
        would never be marked; with it every path is marked within two polls and the gaps say 'budget'."""
        rec = self.recorder(interval_s=0.5)                              # 0.4 s budget < one 0.5 s pace
        mints = self.add_pools(150)
        self.start(rec, mints)
        rec.poll()
        first = {r[0] for r in self.q('SELECT DISTINCT mint FROM path_marks')}
        self.assertEqual(len(first), 100)
        self.assertEqual(self.q('SELECT DISTINCT cause FROM path_gaps'), [('budget',)])
        newest = set(mints[100:])
        self.assertFalse(newest & first)
        self.clock.t += 15
        rec.poll()
        self.assertEqual({r[0] for r in self.q('SELECT DISTINCT mint FROM path_marks')}, set(mints))

    def test_stop_is_checked_between_chunks(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(150))
        stop = threading.Event()
        real = rec.helius.get_multiple_accounts

        def first_then_stop(keys):
            stop.set()
            return real(keys)
        rec.helius.get_multiple_accounts = first_then_stop
        self.assertEqual(rec.poll(stop), 50)
        self.assertEqual(len(self.rpc.requests), 1)
        self.assertEqual(self.q('SELECT cause, COUNT(*) FROM path_gaps GROUP BY cause'), [('stopped', 100)])


class ResumeTests(Base):
    def test_restart_resumes_the_live_paths_from_the_file(self):
        rec = self.recorder(path_hours=1.0)
        old = self.add_pools(1, prefix='old')
        self.start(rec, old)
        self.clock.t += 1800
        live = self.add_pools(3, prefix='live')
        self.start(rec, live)
        rec.poll()
        before = rec.active()
        self.db.close()                                                   # the process dies
        self.clock.t += 2400                                              # 'old' ended while we were down
        db = self.open_db()
        rec2 = self.recorder(db=db, path_hours=1.0)
        self.assertEqual(rec2.resume(), 3)
        self.assertEqual(rec2.active(), {m: before[m] for m in live})
        self.assertEqual(self.q('SELECT mint, why FROM path_ends', db), [(old[0], 'EXPIRED_WHILE_DOWN')])
        self.assertEqual(rec2.poll(), 3)
        self.assertEqual(rec2.health()['resumed'], 3)

    def test_resume_of_an_empty_or_broken_file_is_harmless(self):
        rec = self.recorder()
        self.assertEqual(rec.resume(), 0)
        self.db.db.execute("INSERT INTO paths(mint,start_ts,ends_ts,entered,reason,screen_reasons,holders_checked,features,"
                           "target,code_version,strategy_version) VALUES('x',0,9e12,0,'[]','[]',0,'{}','not json','t','s')")
        self.assertEqual(rec.resume(), 0)
        self.assertEqual(rec.health()['errors_by_code'], {'RESUME_FAILED': 1})


class TimingAndAppendOnlyTests(Base):
    def test_path_starts_at_start_and_stops_after_path_hours(self):
        rec = self.recorder(path_hours=1.0, interval_s=15.0)
        mint = self.add_pools(1)[0]
        t0 = self.clock.t
        rec.start(mint, None, features(mint), entered=False, stage='screen', reasons=['X'])
        while self.clock.t < t0 + 3600 + 60:
            rec.poll()
            self.clock.t += 15
        marks = self.q('SELECT ts FROM path_marks')
        self.assertEqual(len(marks), 240)                                 # every 15 s in [t0, t0 + 1 h)
        self.assertTrue(all(t0 <= ts < t0 + 3600 for (ts,) in marks))
        self.assertEqual(self.q('SELECT why, ts, marks, gaps FROM path_ends'), [('COMPLETE', t0 + 3600, 240, 0)])
        self.assertEqual(len(self.rpc.requests), 240)                     # nothing polled after the end
        self.assertEqual((rec.active(), self.db.live_paths()), ({}, []))

    def test_rows_are_append_only(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(3))
        statements = []
        self.db.db.set_trace_callback(statements.append)
        rec.poll()
        tables = ('paths', 'path_marks', 'path_gaps', 'path_ends')
        first = {t: self.q('SELECT * FROM %s' % t) for t in tables}
        self.clock.t += 15
        rec.poll()
        for t in tables:
            self.assertEqual(self.q('SELECT * FROM %s' % t)[:len(first[t])], first[t])   # earlier rows never change
        self.assertEqual(self.q('SELECT COUNT(*) FROM path_marks')[0][0], 6)
        writes = [s.split()[0].upper() for s in statements if s.split()[0].upper() in ('INSERT', 'UPDATE', 'DELETE', 'REPLACE')]
        self.assertEqual(set(writes), {'INSERT'})

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

    def note(self, mint, screen, **facts):
        return {'mint': mint, 'cid': None, 'screen': screen, 'entry_reasons': None, 'fill': None, 'error': None, **facts}

    def test_only_hazard_free_screens_are_recorded(self):
        rec = self.recorder()
        m = self.add_pools(5)
        cases = [(['MARKET_CAP_ABOVE_MAX'], None, True), (['LIQUIDITY_BELOW_MIN', 'MARKET_CAP_BELOW_MIN'], None, True),
                 (['ACTIVE_MINT_AUTHORITY'], None, False), (['MARKET_CAP_ABOVE_MAX', 'LP_FREEZE_AUTHORITY'], None, False),
                 (['HOLDERS_UNAVAILABLE'], {'code': 'HOLDERS_UNAVAILABLE', 'transient': True}, False)]
        for mint, (reasons, error, expected) in zip(m, cases):
            self.assertEqual(rec.on_candidate(self.note(mint, self.screen(mint, False, reasons, error))), expected, reasons)
        self.assertEqual(rec.health()['hazard_skipped'], 3)
        self.assertEqual({r[0] for r in self.q('SELECT mint FROM paths')}, {m[0], m[1]})

    def test_outcome_is_pure_runner_memory(self):
        s = Screen(True, (), {})
        self.assertEqual(L.outcome(Screen(False, ('MARKET_CAP_ABOVE_MAX',), {})), ('screen', ['MARKET_CAP_ABOVE_MAX'], False))
        self.assertEqual(L.outcome(s, entry_reasons=['ENTRY_THROTTLE']), ('entry', ['ENTRY_THROTTLE'], False))
        self.assertEqual(L.outcome(s, entry_reasons=['ENTRY'], fill=object()), ('entry', [], True))
        self.assertEqual(L.outcome(s, entry_reasons=['ENTRY']), ('entry', ['NOT_FILLED'], False))
        self.assertEqual(L.outcome(s, error='HTTP_400'), ('quote', ['ENTRY_FAILED:HTTP_400'], False))
        self.assertEqual(L.outcome(s), ('entry', ['ENTRIES_STOPPED'], False))
        rec = self.recorder()
        mint = self.add_pools(1)[0]
        self.assertTrue(rec.on_candidate(self.note(mint, self.screen(mint, True), entry_reasons=['COST_BUDGET'])))
        self.assertEqual(self.q('SELECT stage, reason FROM paths'), [('entry', '["COST_BUDGET"]')])


class FailureIsolationTests(Base):
    def test_failures_are_counted_never_raised(self):
        rec = self.recorder()
        self.start(rec, self.add_pools(2))
        with mock.patch.object(self.db, 'add', side_effect=sqlite3.OperationalError('disk I/O error')):
            self.assertEqual(rec.poll(), 0)
        rec.helius = mock.Mock(get_multiple_accounts=mock.Mock(side_effect=RuntimeError('boom')))
        self.assertEqual(rec.poll(), 0)
        h = rec.health()
        self.assertEqual((h['write_failures'], h['gaps'], h['marks']), (1, 4, 0))
        self.assertEqual(h['errors_by_code'], {'WRITE_FAILED': 1, 'POLL_CALL_FAILED': 1})
        self.assertFalse(rec.start('not-a-key', None, {}, entered=False, stage='screen', reasons=[]))
        m = self.add_pools(1, prefix='x')[0]
        with mock.patch.object(self.db, 'insert_path', side_effect=sqlite3.OperationalError('locked')):
            self.assertFalse(rec.start(m, None, features(m), entered=False, stage='screen', reasons=[]))
        self.assertNotIn(m, rec.active())                                 # a failed start leaves nothing behind
        self.assertFalse(rec.on_candidate({'screen': None}))
        with mock.patch.object(self.db, 'rollover', side_effect=OSError('rename')):
            rec.max_db_bytes = 1
            rec.helius = P.Helius(KEY, self.transport('low'))
            self.clock.t += 60
            rec.poll()
        self.assertEqual(rec.health()['errors_by_code']['ROLLOVER_FAILED'], 1)


class ConfigTests(unittest.TestCase):
    def test_paths_config(self):
        self.assertFalse(L.config(None)['enabled'])
        cfg = L.config({'enabled': True, 'path_hours': 6, 'interval_s': 15})
        self.assertEqual((cfg['enabled'], cfg['db'], cfg['max_db_gb'], cfg['ref_size_sol']), (True, None, 2.0, '0.2'))
        self.assertEqual(L.db_path(cfg, '/state'), Path('/state/paths.sqlite'))
        self.assertEqual(L.db_path(dict(cfg, db='/data/p.sqlite'), '/state'), Path('/data/p.sqlite'))
        for bad in ({'enabled': 'yes'}, {'path_hour': 6}, {'interval_s': 0}, {'path_hours': 100}, {'ref_size_sol': 'x'},
                    {'max_paths': 0}, {'db': ''}, {'max_db_gb': 0}, {'min_free_gb': -1}, []):
            with self.assertRaises(ValueError, msg=repr(bad)):
                L.config(bad)


if __name__ == '__main__':
    logging.basicConfig()
    unittest.main()
