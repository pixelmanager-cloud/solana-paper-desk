"""Held-position watcher: fake websocket feed, fake RPC, fake clock. No network, no provider keys, no systemd."""
import ast
import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing, redirect_stdout
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

from desk.ledger import Ledger
from desk.model import load_config
from desk.providers import PUMPSWAP, SOL
from desk.security import TOKEN_2022, TOKEN_PROGRAM
from tools.ops import held_watcher as hw

REPO = Path(__file__).resolve().parents[1]
CONFIG = REPO / 'config' / 'experiments' / 'paper-kraken-fresh.example.json'
HAVE_SOLDERS = importlib.util.find_spec('solders') is not None
UNIT = 'desk-paper-held-cycle.service'


def pubkey(n):
    from solders.pubkey import Pubkey
    return Pubkey.from_bytes(bytes([n]) * 32)


def token_account(mint, amount, owner=TOKEN_PROGRAM, initialized=1, size=165):
    from solders.pubkey import Pubkey
    data = bytearray(size)
    data[:32] = bytes(Pubkey.from_string(mint))
    if size >= 72:
        data[64:72] = amount.to_bytes(8, 'little')
    if size > 108:
        data[108] = initialized
    return {'owner': owner, 'executable': False, 'lamports': 1, 'data': [base64.b64encode(bytes(data)).decode(), 'base64']}


def position(**over):
    p = {'qty': '10000000', 'cost_left': '0.1', 'pool': 'POOL', 'stop_ratio': '0.82', 'peak_ratio': '1', 'stage': 0,
         'opened_at': 1_000, 'touched_15': False, 'quote_execution': {'mint_decimals': 6}}
    p.update(over)
    return p


class Pool:
    """A real PumpSwap pool identity (PDA, LP mint, ATA vaults) built the way tests/test_pools.py does."""

    def __init__(self):
        from solders.pubkey import Pubkey
        from desk.pools import ATA
        from desk.providers import PUMP
        self.mint = pubkey(7)
        self.creator = Pubkey.find_program_address([b'pool-authority', bytes(self.mint)], Pubkey.from_string(PUMP))[0]
        self.pool, self.bump = Pubkey.find_program_address(
            [b'pool', bytes(2), bytes(self.creator), bytes(self.mint), bytes(Pubkey.from_string(SOL))], Pubkey.from_string(PUMPSWAP))
        lp = Pubkey.find_program_address([b'pool_lp_mint', bytes(self.pool)], Pubkey.from_string(PUMPSWAP))[0]
        self.vaults = [Pubkey.find_program_address([bytes(self.pool), bytes(Pubkey.from_string(TOKEN_PROGRAM)), bytes(m)],
                                                   Pubkey.from_string(ATA))[0] for m in (self.mint, Pubkey.from_string(SOL))]
        self.raw = bytes([241, 154, 109, 4, 17, 177, 109, 188]) + bytes([self.bump]) + bytes(2) + b''.join(
            bytes(x) for x in [self.creator, self.mint, Pubkey.from_string(SOL), lp, *self.vaults]) \
            + (1000).to_bytes(8, 'little') + bytes(32)          # lp_supply + coin_creator, as tests/test_pools.py
        self.mint_s, self.pool_s = str(self.mint), str(self.pool)
        self.base_vault, self.quote_vault = str(self.vaults[0]), str(self.vaults[1])

    def account(self, raw=None, owner=PUMPSWAP):
        return {'owner': owner, 'executable': False, 'data': [base64.b64encode(raw or self.raw).decode(), 'base64']}


class FakeFeed:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.calls = []

    async def updates(self, accounts):
        self.calls.append(list(accounts))
        while True:
            item = await self.queue.get()
            if isinstance(item, Exception):
                raise item
            yield item


class MathTests(unittest.TestCase):
    cfg = load_config(CONFIG)

    def test_implied_ratio_known_answer_and_parity_with_strategy_swap_quote(self):
        ratio = hw.implied_ratio(position(), 10 ** 15, 10 * 10 ** 9, self.cfg, '25')
        self.assertEqual(str(ratio), '0.9822099680685165474392930518')
        from desk.strategy import swap_quote
        e = {'route_available': True, 'reserve_sol': '10', 'reserve_tokens': '1000000000', 'pool_fee_bps': '25'}
        mark = swap_quote(e, D('10000000'), 'sell', self.cfg) - D(self.cfg['fixed_fee_sol'])
        self.assertEqual(ratio, mark / D('0.1'))

    def test_drained_pool_is_the_worst_case_not_an_error(self):
        self.assertEqual(hw.implied_ratio(position(), 10 ** 15, 0, self.cfg, '25'), D(0))

    def test_unpriceable_positions_fail_closed(self):
        for over in ({'cost_left': '0'}, {'qty': '0'}):
            with self.assertRaises(hw.WatcherError):
                hw.implied_ratio(position(**over), 10 ** 15, 10 ** 10, self.cfg, '25')

    def reasons(self, ratio, now=2_000, mode='RUNNING', peak=D(0), margin=D(0), **over):
        return hw.exit_reasons(position(**over), None if ratio is None else D(ratio), now, self.cfg, mode, peak, margin)

    def test_stop_boundary_and_margin(self):
        self.assertEqual(self.reasons('0.82'), ['STOP'])
        self.assertEqual(self.reasons('0.8201'), [])
        self.assertEqual(self.reasons('0.8201', margin=D('0.02')), ['STOP'])
        self.assertEqual(self.reasons('0.85', margin=D('0.02')), [])

    def test_trailing_only_from_stage_three_and_follows_the_engines_recorded_peak(self):
        self.assertEqual(self.reasons('1.5', stage=2, peak_ratio='3'), [])
        self.assertEqual(self.reasons('2.0', stage=3, peak_ratio='3', stop_ratio='1.4'), ['TRAILING_STOP'])   # 2.0 <= 3*0.7=2.1
        self.assertEqual(self.reasons('2.2', stage=3, peak_ratio='3', stop_ratio='1.4'), [])
        # The watcher saw a higher peak than the ledger recorded. The ENGINE trails its recorded peak, so the
        # watcher's peak may move the line only by one margin (T28F item 9); beyond that it would trigger passes
        # the engine will not act on.
        self.assertEqual(self.reasons('2.2', stage=3, peak_ratio='3', stop_ratio='1.4', peak=D('4')), [])
        self.assertEqual(self.reasons('2.2', stage=3, peak_ratio='3', stop_ratio='1.4', peak=D('4'), margin=D('0.02')), [])
        # inside the margin the hint counts: recorded 3, watcher 3.01 moves the line 2.12 -> 2.127 (with the margin)
        self.assertEqual(self.reasons('2.125', stage=3, peak_ratio='3', stop_ratio='1.4', margin=D('0.02')), [])
        self.assertEqual(self.reasons('2.125', stage=3, peak_ratio='3', stop_ratio='1.4', peak=D('3.01'), margin=D('0.02')),
                         ['TRAILING_STOP'])
        self.assertEqual(self.reasons('2.12', stage=3, peak_ratio='3', stop_ratio='1.4', peak=D('9'), margin=D('0.02')),
                         ['TRAILING_STOP'])                       # capped at recorded+margin = 3.02: line 2.134
        self.assertEqual(self.reasons('2.14', stage=3, peak_ratio='3', stop_ratio='1.4', peak=D('9'), margin=D('0.02')), [])

    def test_take_profit_rungs_follow_the_stage(self):
        self.assertEqual(self.reasons('1.4', stage=0), ['TAKE_PROFIT'])
        self.assertEqual(self.reasons('1.39', stage=0), [])
        self.assertEqual(self.reasons('1.39', stage=0, margin=D('0.02')), ['TAKE_PROFIT'])   # nudged slightly early
        self.assertEqual(self.reasons('1.37', stage=0, margin=D('0.02')), [])
        self.assertEqual(self.reasons('1.4', stage=1, stop_ratio='1'), [])
        self.assertEqual(self.reasons('2', stage=1, stop_ratio='1'), ['TAKE_PROFIT'])
        self.assertEqual(self.reasons('3', stage=2, stop_ratio='1.4'), ['TAKE_PROFIT'])
        self.assertEqual(self.reasons('9', stage=3, peak_ratio='9', stop_ratio='1.4'), [])

    def test_time_based_reasons_need_no_reserves(self):
        self.assertEqual(self.reasons(None, now=1_000 + 2_699), [])
        self.assertEqual(self.reasons(None, now=1_000 + 2_700), ['TIME_STOP'])
        self.assertEqual(self.reasons(None, now=1_000 + 2_700, touched_15=True), [])
        self.assertEqual(self.reasons(None, now=1_000 + 21_600, touched_15=True), ['MAX_HOLD'])
        self.assertEqual(self.reasons(None, now=1_000 + 21_600), ['MAX_HOLD', 'TIME_STOP'])

    def test_liquidating_mode_and_priority_order(self):
        self.assertEqual(self.reasons(None, mode='LIQUIDATING'), ['LIQUIDATE'])
        self.assertEqual(self.reasons('0.5', now=1_000 + 21_600, mode='LIQUIDATING'),
                         ['LIQUIDATE', 'STOP', 'MAX_HOLD', 'TIME_STOP'])

    def test_thresholds_match_the_engine_source(self):
        """The ladder, touched and trailing-stage constants are duplicated from desk/engine.py; fail if they drift."""
        tree = ast.parse((REPO / 'desk' / 'engine.py').read_text())
        manage = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'manage_position')
        ladder = next(n for n in ast.walk(manage) if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == 'ladder')
        triggers = tuple(D(ast.literal_eval(t.elts[0].args[0])) for t in ladder.value.elts)
        self.assertEqual(triggers, hw.LADDER)
        source = ast.get_source_segment((REPO / 'desk' / 'engine.py').read_text(), manage)
        self.assertIn('ratio >= D("1.15")', source)
        self.assertEqual(hw.TOUCHED, D('1.15'))
        self.assertIn('p["stage"] >= 3 and ratio <= dec(p["peak_ratio"]) * (ONE - dec(cfg["trailing_fraction"]))', source)
        self.assertEqual(hw.TRAILING_STAGE, 3)
        self.assertIn('ratio <= dec(p["stop_ratio"])', source)
        self.assertIn('e["ts"] - p["opened_at"] >= cfg["max_hold_seconds"]', source)
        self.assertIn('not p["touched_15"] and e["ts"] - p["opened_at"] >= cfg["time_stop_seconds"]', source)


@unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
class AccountTests(unittest.TestCase):
    def setUp(self):
        self.pool = Pool()

    def test_decode_accepts_both_token_programs(self):
        for owner in (TOKEN_PROGRAM, TOKEN_2022):
            self.assertEqual(hw.decode_token_account(token_account(self.pool.mint_s, 123, owner), self.pool.mint_s), 123)

    def test_decode_fails_closed(self):
        good = token_account(self.pool.mint_s, 5)
        bad = {
            'wrong owner': dict(good, owner=PUMPSWAP),
            'not a dict': None,
            'short': token_account(self.pool.mint_s, 5, size=100),
            'uninitialized': token_account(self.pool.mint_s, 5, initialized=0),
            'wrong mint': token_account(SOL, 5),
            'bad base64': dict(good, data=['***', 'base64']),
            'wrong encoding': dict(good, data=[good['data'][0], 'base58']),
            'no data': {'owner': TOKEN_PROGRAM},
        }
        for label, account in bad.items():
            with self.subTest(label), self.assertRaises(hw.WatcherError):
                hw.decode_token_account(account, self.pool.mint_s)

    def resolve(self, account):
        return hw.resolve_vaults(position(pool=self.pool.pool_s), self.pool.mint_s, lambda _pool: account)

    def test_vaults_resolved_from_a_verified_pool(self):
        self.assertEqual(self.resolve(self.pool.account()), (self.pool.base_vault, self.pool.quote_vault))

    def test_pool_identity_failures(self):
        with self.assertRaises(hw.WatcherError):
            hw.resolve_vaults(position(pool=self.pool.pool_s), str(pubkey(9)), lambda _p: self.pool.account())   # mint mismatch
        with self.assertRaises(ValueError):
            self.resolve(self.pool.account(owner=TOKEN_PROGRAM))                                                # wrong program
        tampered = bytearray(self.pool.raw)
        tampered[8] ^= 0x01                                                                                      # bump byte
        with self.assertRaises(hw.WatcherError):
            self.resolve(self.pool.account(bytes(tampered)))
        other = Pool()
        with self.assertRaises(hw.WatcherError):
            hw.resolve_vaults(position(pool=str(pubkey(11))), other.mint_s, lambda _p: other.account())        # PDA mismatch


class LedgerOpenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'ledger.sqlite'
        ledger = Ledger(str(self.path))
        ledger.close()
        self.uris = []

    def connect(self, fail_plain):
        def fake(target, **kwargs):
            self.uris.append(target.split('?', 1)[1])
            if fail_plain and target.endswith('?mode=ro'):
                raise sqlite3.OperationalError('unable to open database file')
            return sqlite3.connect(target, **kwargs)
        return fake

    def test_plain_read_only_is_preferred(self):
        with closing(hw.open_ledger_ro(self.path, self.connect(False))) as db:
            db.execute('SELECT 1')
        self.assertEqual(self.uris, ['mode=ro'])

    def test_immutable_fallback_only_for_a_wal_database_without_a_wal_sidecar(self):
        with closing(hw.open_ledger_ro(self.path, self.connect(True))) as db:
            self.assertIsNotNone(db.execute("SELECT count(*) FROM sqlite_master").fetchone())
        self.assertEqual(self.uris, ['mode=ro', 'mode=ro&immutable=1'])
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ['ledger.sqlite'])   # immutable: no sidecars
        self.uris.clear()
        self.path.with_name(self.path.name + '-wal').write_bytes(b'x')
        with self.assertRaises(sqlite3.OperationalError):
            hw.open_ledger_ro(self.path, self.connect(True))
        self.assertEqual(self.uris, ['mode=ro'])

    def test_no_immutable_fallback_for_a_non_wal_database(self):
        plain = Path(self.tmp.name) / 'plain.sqlite'
        with closing(sqlite3.connect(plain)) as db:
            db.execute('CREATE TABLE t(x)')
        with self.assertRaises(sqlite3.OperationalError):
            hw.open_ledger_ro(plain, self.connect(True))
        self.assertEqual(self.uris, ['mode=ro'])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = hw.WatcherStore(Path(self.tmp.name) / 'w' / 'watcher.sqlite')
        self.addCleanup(self.store.close)

    def test_append_only_tables_reject_update_and_delete(self):
        rid = self.store.charge('x', 100.0)
        self.store.finish(rid, False, 'boom')
        self.store.record_trigger(100.0, 'M', ['STOP'], D('0.5'), 7, 99.0, 'stream', 'path', True, 'ok')
        self.store.health(100.0, 'CODE', 'x')
        self.store.save_vault('P', 'M', 'B', 'Q', 1.0)
        for table in ('requests', 'request_results', 'triggers', 'health', 'vaults'):
            for sql in ('UPDATE %s SET ts=0' % table if table != 'request_results' else 'UPDATE request_results SET ok=1',
                        'DELETE FROM %s' % table):
                with self.subTest(sql), self.assertRaises(sqlite3.DatabaseError):
                    self.store.db.execute(sql)

    def test_vault_cache_is_first_write_wins(self):
        self.store.save_vault('P', 'M', 'B1', 'Q1', 1.0)
        self.store.save_vault('P', 'M', 'B2', 'Q2', 2.0)
        self.assertEqual(self.store.vault('P'), ('M', 'B1', 'Q1', None))      # the 4th column holds optional pool/mint facts

    def test_allowance_is_a_rolling_window_and_failures_are_charged(self):
        for t in (10.0, 20.0, 4000.0):
            rid = self.store.charge('k', t)
            self.store.finish(rid, False, 'RpcError')
        self.assertEqual(self.store.used(4001.0), 1)           # only the 4000.0 charge is inside the last hour
        self.assertEqual(self.store.used(3611.0), 2)           # 10.0 has just left the window, 20.0 has not

    def test_store_and_directory_are_private(self):
        self.assertEqual(os.stat(Path(self.tmp.name) / 'w').st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(Path(self.tmp.name) / 'w' / 'watcher.sqlite').st_mode & 0o777, 0o600)


class WatcherBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.t = 5_000.0
        self.pool = Pool() if HAVE_SOLDERS else None
        self.mint = self.pool.mint_s if self.pool else 'MINT'
        self.ledger = self.root / 'paper-ledger.sqlite'
        self.cfg = load_config(CONFIG)
        self.rpc_calls = []
        self.rpc_failure = None
        self.block_times = {}
        self.reserves = (10 ** 15, 10 * 10 ** 9)          # base raw, quote raw (ratio ~0.982)
        self.slot = 100
        self.sleeps = []

    # --- fixtures
    def write_state(self, positions, mode='RUNNING'):
        ledger = Ledger(str(self.ledger))
        ledger.db.execute('DELETE FROM state')
        ledger.db.execute('INSERT INTO state(id, payload) VALUES(1, ?)', (json.dumps({'positions': positions, 'mode': mode}),))
        ledger.close()

    def pos(self, **over):
        over.setdefault('pool', self.pool.pool_s)
        over.setdefault('opened_at', int(self.t) - 100)          # young position: no time-based exit unless a test asks
        return position(**over)

    def fake_rpc(self, method, params):
        self.rpc_calls.append((method, params))
        if self.rpc_failure is not None:
            raise self.rpc_failure
        if method == 'getAccountInfo':
            if params[0] == self.mint:
                return {'context': {'slot': self.slot}, 'value': self.mint_account()}
            return {'context': {'slot': self.slot}, 'value': self.pool.account()}
        if method == 'getBlockTime':
            return self.block_times.get(params[0], 1_700_000_000)
        if method == 'getMultipleAccounts':
            values = []
            for acct in params[0]:
                if acct == self.pool.base_vault:
                    values.append(token_account(self.pool.mint_s, self.reserves[0]))
                else:
                    values.append(token_account(SOL, self.reserves[1]))
            return {'context': {'slot': self.slot}, 'value': values}
        raise AssertionError(method)

    def mint_account(self, owner=TOKEN_PROGRAM, decimals=6, extensions=b''):
        data = bytearray(82)
        data[36:44] = (10 ** 15).to_bytes(8, 'little')
        data[44], data[45] = decimals, 1
        raw = bytes(data) + (bytes(83) + b'\x01' + extensions if extensions else b'')
        return {'owner': owner, 'executable': False, 'lamports': 1, 'data': [base64.b64encode(raw).decode(), 'base64']}

    def vault_values(self, base=None, quote=None):
        return (token_account(self.pool.mint_s, self.reserves[0] if base is None else base),
                token_account(SOL, self.reserves[1] if quote is None else quote))

    async def fake_sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)

    def make(self, feed=None, method='path', runner=None, **kwargs):
        self.store = hw.WatcherStore(self.root / 'state' / 'watcher.sqlite')
        self.addCleanup(self.store.close)
        self.trigger = hw.Trigger(method, UNIT, self.root / 'state' / 'trigger' / 'request', runner, lambda: self.t)
        params = dict(ledger=self.ledger, cfg=self.cfg, store=self.store, trigger=self.trigger, rpc=self.fake_rpc, feed=feed,
                      clock=lambda: self.t, sleep=self.fake_sleep, rng=lambda: 1.0, health_path=self.root / 'state' / 'health.json',
                      min_request_interval=0.0, poll_seconds=2.0, reconcile_seconds=30.0, refresh_seconds=5.0,
                      min_trigger_gap=0.0)
        params.update(kwargs)
        self.w = hw.Watcher(**params)
        return self.w

    async def settle(self, n=8):
        for _ in range(n):
            await asyncio.sleep(0)

    def triggers(self):
        return self.store.db.execute('SELECT mint, reason, ratio, slot, source, method, ok FROM triggers ORDER BY id').fetchall()

    def request_file(self):
        path = self.root / 'state' / 'trigger' / 'request'
        return json.loads(path.read_text()) if path.exists() else None


@unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
class PollingTests(WatcherBase):
    async def test_polling_fallback_resolves_vaults_then_triggers_on_a_stop_crossing(self):
        self.write_state({self.mint: self.pos()})
        w = self.make()
        await w.tick()                                                      # refresh, resolve vaults (getAccountInfo), poll
        # pool account, mint account (decimals / transfer fee), then ONE same-slot poll of both vaults
        self.assertEqual([c[0] for c in self.rpc_calls], ['getAccountInfo', 'getAccountInfo', 'getMultipleAccounts'])
        self.assertEqual(w.status, 'POLLING')
        self.assertEqual(self.triggers(), [])
        self.reserves = (10 ** 15, 5 * 10 ** 9)                             # liquidity pulled: ratio ~0.49
        self.t += 2.5
        self.slot += 1
        await w.tick()
        rows = self.triggers()
        self.assertEqual([(r[1], r[4], r[5], r[6]) for r in rows], [('STOP', 'poll', 'path', 1)])
        self.assertLess(D(rows[0][2]), D('0.82'))
        request = self.request_file()
        self.assertEqual((request['mint'], request['reasons']), (self.mint, ['STOP']))

    async def test_requests_are_charged_before_the_call_and_failures_too(self):
        self.write_state({self.mint: self.pos()})
        w = self.make(allowance_per_hour=100)
        await w.tick()
        self.rpc_failure = hw.RpcError('HTTP_429')
        self.t += 2.5
        await w.tick()
        self.t += 1.0                                                       # still inside the 429 hold (2 s): nothing is sent
        await w.tick()
        kinds = [r[0] for r in self.store.db.execute('SELECT kind FROM requests ORDER BY id')]
        self.assertEqual(kinds, ['getAccountInfo', 'getAccountInfo', 'getMultipleAccounts', 'getMultipleAccounts'])
        results = [r[0] for r in self.store.db.execute('SELECT ok FROM request_results ORDER BY request_id')]
        self.assertEqual(results, [1, 1, 1, 0])
        self.assertIn('POLL_FAILED', w.warnings)
        self.assertEqual(w.status, 'DEGRADED')

    async def test_allowance_is_a_hard_ceiling(self):
        self.write_state({self.mint: self.pos()})
        w = self.make(allowance_per_hour=3)
        for _ in range(10):
            await w.tick()
            self.t += 2.5
        self.assertEqual(self.store.used(self.t), 3)
        self.assertEqual(len(self.rpc_calls), 3)
        self.assertIn('ALLOWANCE_EXHAUSTED', w.warnings)
        self.assertEqual(w.status, 'DEGRADED')
        self.t += 3600
        await w.tick()
        self.assertNotIn('ALLOWANCE_EXHAUSTED', w.warnings)                # a fresh window restores it

    async def test_minimum_request_interval_is_enforced_across_requests(self):
        self.write_state({self.mint: self.pos()})
        w = self.make(min_request_interval=1.0)
        await w.tick()
        # pool account, mint account, first poll: three requests, two enforced gaps
        self.assertTrue(self.sleeps and abs(sum(self.sleeps) - 2.0) < 1e-6)

    async def test_stale_reserves_never_trigger_a_price_exit_but_time_exits_still_fire(self):
        self.write_state({self.mint: self.pos()})
        w = self.make()
        await w.tick()
        self.rpc_failure = hw.RpcError('TIMEOUT')
        self.reserves = (10 ** 15, 10 ** 9)                                 # would be a STOP, but we cannot see it
        self.t += 60                                                        # snapshot older than max_reserve_age
        await w.tick()
        self.assertEqual(self.triggers(), [])
        self.assertIn('RESERVES_STALE', w.warnings)
        self.t = (5_000 - 100) + 21_600 + 1                                 # max hold reached: a ledger-only fact
        await w.tick()
        self.assertEqual([r[1] for r in self.triggers()], ['MAX_HOLD'])

    async def test_out_of_order_and_foreign_updates_are_ignored(self):
        self.write_state({self.mint: self.pos()})
        w = self.make()
        await w.tick()
        low, quote = self.vault_values(quote=10 ** 9)
        w.handle_update(self.pool.quote_vault, quote, self.slot - 5, 'stream')           # older slot than the snapshot
        self.assertEqual(self.triggers(), [])
        w.handle_update('SomeOtherAccount1111111111111111111111111111', quote, self.slot + 1, 'stream')
        self.assertEqual(self.triggers(), [])
        w.handle_update(self.pool.quote_vault, token_account(self.pool.mint_s, 1), self.slot + 2, 'stream')   # wrong mint
        self.assertIn('VAULT_DECODE_FAILED', w.warnings)
        self.assertEqual(self.triggers(), [])

    async def test_closed_position_is_dropped_and_ledger_errors_do_not_crash(self):
        self.write_state({self.mint: self.pos()})
        w = self.make()
        await w.tick()
        self.assertEqual(len(w.positions), 1)
        self.write_state({})
        self.t += 6
        await w.tick()
        self.assertEqual((w.positions, w.accounts, w.status), ({}, {}, 'IDLE'))
        self.ledger.write_bytes(b'not a database')
        self.t += 6
        await w.tick()
        self.assertIn('LEDGER_UNREADABLE', w.warnings)

    async def test_vault_resolution_failure_is_retried_later_and_not_cached(self):
        self.write_state({self.mint: self.pos()})
        w = self.make(vault_retry_seconds=60)
        original = self.pool.raw
        self.pool.raw = original[:70]                                       # malformed pool account
        await w.tick()
        self.assertIn('VAULT_RESOLUTION_FAILED', w.warnings)
        self.assertEqual(w.vaults, {})
        self.assertIsNone(self.store.vault(self.pool.pool_s))
        calls = len(self.rpc_calls)
        self.t += 6
        await w.tick()
        self.assertEqual(len(self.rpc_calls), calls)                        # not retried before vault_retry_seconds
        self.pool.raw = original
        self.t += 60
        await w.tick()
        self.assertEqual(len(w.vaults), 1)
        self.assertIsNotNone(self.store.vault(self.pool.pool_s))

    async def test_positions_missing_fields_are_reported_not_watched(self):
        self.write_state({self.mint: {'qty': '1'}})
        w = self.make()
        await w.tick()
        self.assertEqual(w.positions, {})
        self.assertIn('POSITION_FIELDS_MISSING', w.warnings)


@unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
class TriggerTests(WatcherBase):
    async def crossing(self, **kwargs):
        self.write_state({self.mint: self.pos()})
        w = self.make(**kwargs)
        await w.tick()
        return w

    async def push(self, w, quote, slot=None):
        """A stream notification for the quote vault; the base vault is re-confirmed at the same slot so the whole
        snapshot is fresh (the real poller does the same)."""
        self.slot = slot or self.slot + 1
        w.apply_account(self.pool.base_vault, token_account(self.pool.mint_s, self.reserves[0]), self.slot, 'stream')
        w.handle_update(self.pool.quote_vault, token_account(SOL, quote), self.slot, 'stream')

    async def test_debounce_then_retrigger_and_per_reason_keys(self):
        w = await self.crossing(debounce_seconds=30)
        await self.push(w, 4 * 10 ** 9)
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual([r[1] for r in self.triggers()], ['STOP'])
        self.assertEqual(w.suppressed, 1)
        self.t += 31
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual([r[1] for r in self.triggers()], ['STOP', 'STOP'])

    async def test_restart_honours_the_persisted_debounce(self):
        w = await self.crossing(debounce_seconds=30)
        await self.push(w, 4 * 10 ** 9)
        self.assertEqual(len(self.triggers()), 1)
        self.store.close()
        w2 = self.make(debounce_seconds=30)
        await w2.tick()
        self.t += 3
        await self.push(w2, 4 * 10 ** 9)
        self.assertEqual(len(self.triggers()), 1)

    async def test_per_position_and_global_hourly_caps(self):
        w = await self.crossing(debounce_seconds=0, max_triggers_per_position_hour=2)
        for _ in range(5):
            self.t += 1
            await self.push(w, 3 * 10 ** 9)
        self.assertEqual(len(self.triggers()), 2)
        self.assertIn('TRIGGER_RATE_LIMITED', w.warnings)
        self.t += 3601
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual(len(self.triggers()), 3)

    async def test_any_two_requests_are_spaced_by_the_global_gap_without_being_remembered(self):
        w = await self.crossing(debounce_seconds=0, min_trigger_gap=5.0)
        await self.push(w, 3 * 10 ** 9)
        self.t += 1
        await self.push(w, 3 * 10 ** 9)                                      # redundant: one held pass covers every position
        self.assertEqual(len(self.triggers()), 1)
        self.assertEqual(w.suppressed, 1)
        self.t += 5
        await self.push(w, 3 * 10 ** 9)                                      # still crossing after the gap -> fires
        self.assertEqual(len(self.triggers()), 2)

    async def test_global_cap(self):
        w = await self.crossing(debounce_seconds=0, max_triggers_hour=1)
        await self.push(w, 3 * 10 ** 9)
        self.t += 1
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual(len(self.triggers()), 1)

    async def test_take_profit_and_trailing_crossings(self):
        self.write_state({self.mint: self.pos()})
        w = self.make(margin=0)
        await w.tick()
        await self.push(w, 150 * 10 ** 9)                                   # price up: ratio far above 1.4
        self.assertEqual([r[1] for r in self.triggers()], ['TAKE_PROFIT'])
        self.write_state({self.mint: self.pos(stage=3, peak_ratio='3', stop_ratio='1.4')})
        self.reserves = (10 ** 15, 25 * 10 ** 9)                            # ratio ~2.46: above the 2.1 trailing line
        self.t += 60
        self.store.close()
        w2 = self.make(margin=0)
        await w2.tick()
        before = len(self.triggers())
        await self.push(w2, 20 * 10 ** 9)                                   # ratio ~1.96 <= 3 * 0.7, still above the 1.4 stop
        self.assertEqual([r[1] for r in self.triggers()][before:], ['TRAILING_STOP'])

    async def test_systemctl_method_uses_no_block_and_failures_are_recorded_and_retried_soon(self):
        calls = []
        outcomes = [SimpleNamespace(returncode=1, stdout='', stderr='denied'), SimpleNamespace(returncode=0, stdout='', stderr='')]

        def runner(argv):
            calls.append(argv)
            return outcomes.pop(0)
        w = await self.crossing(method='systemctl', runner=runner, debounce_seconds=300)
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual(calls[0], ['systemctl', 'start', '--no-block', UNIT])
        self.assertEqual([r[6] for r in self.triggers()], [0])
        self.assertIn('TRIGGER_FAILED', w.warnings)
        await self.push(w, 3 * 10 ** 9)                                     # immediately: still inside the 5 s retry guard
        self.assertEqual(len(calls), 1)
        self.t += 6
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual([r[6] for r in self.triggers()], [0, 1])
        self.assertNotIn('TRIGGER_FAILED', w.warnings)

    async def test_runner_exception_is_contained(self):
        def runner(argv):
            raise OSError('no systemctl')
        w = await self.crossing(method='systemctl', runner=runner)
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual([r[6] for r in self.triggers()], [0])

    async def test_dry_run_records_but_does_not_fire(self):
        w = await self.crossing(method='none')
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual([(r[5], r[6]) for r in self.triggers()], [('none', 1)])
        self.assertIsNone(self.request_file())

    async def test_path_trigger_is_atomic_private_and_changes_on_every_fire(self):
        w = await self.crossing(debounce_seconds=0)
        await self.push(w, 3 * 10 ** 9)
        first = (self.root / 'state' / 'trigger' / 'request')
        self.assertEqual(os.stat(first).st_mode & 0o777, 0o600)
        content1 = first.read_text()
        self.t += 1
        await self.push(w, 3 * 10 ** 9)
        self.assertNotEqual(first.read_text(), content1)
        self.assertEqual(list(first.parent.glob('.*.tmp')), [])

    async def test_unwritable_trigger_path_is_a_recorded_failure(self):
        w = await self.crossing()
        w.trigger.request_path = self.root / 'state' / 'trigger' / 'request'
        (self.root / 'state' / 'trigger').mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / 'state' / 'trigger', 0o500)
        self.addCleanup(os.chmod, self.root / 'state' / 'trigger', 0o700)
        if os.geteuid() == 0:
            self.skipTest('root ignores directory permissions')
        await self.push(w, 3 * 10 ** 9)
        self.assertEqual([r[6] for r in self.triggers()], [0])


@unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
class StreamTests(WatcherBase):
    async def start(self, feed, **kwargs):
        self.write_state({self.mint: self.pos()})
        w = self.make(feed=feed, **kwargs)
        await w.tick()
        await self.settle()
        return w

    async def test_connected_stream_seeds_once_and_reacts_to_notifications(self):
        feed = FakeFeed()
        w = await self.start(feed)
        self.assertEqual(feed.calls, [sorted([self.pool.base_vault, self.pool.quote_vault])])
        self.assertFalse(w.stream_up)
        feed.queue.put_nowait((hw.CONNECTED, None, 0))
        await self.settle()
        self.assertTrue(w.stream_up)
        await w.tick()
        self.assertEqual(w.status, 'STREAM')
        polls = [c for c in self.rpc_calls if c[0] == 'getMultipleAccounts']
        self.assertEqual(len(polls), 1 + 1)                                 # initial polling tick + the post-connect seed
        feed.queue.put_nowait((self.pool.quote_vault, token_account(SOL, 3 * 10 ** 9), self.slot + 1))
        await self.settle()
        self.assertEqual(self.triggers(), [])                               # one vault alone is never priced (T28F item 2)
        feed.queue.put_nowait((self.pool.base_vault, token_account(self.pool.mint_s, self.reserves[0]), self.slot + 1))
        await self.settle()
        self.assertEqual([(r[1], r[4]) for r in self.triggers()], [('STOP', 'stream')])   # the pair of ONE slot completes
        # quiet pool: no notifications, reserves stay valid while the stream is up (reconcile keeps them fresh)
        polls_before = len([c for c in self.rpc_calls if c[0] == 'getMultipleAccounts'])
        self.t += 10
        await w.tick()
        self.assertEqual(len([c for c in self.rpc_calls if c[0] == 'getMultipleAccounts']), polls_before)  # not polling at 2 s while streaming

    async def test_reconcile_poll_catches_a_missed_notification(self):
        feed = FakeFeed()
        w = await self.start(feed, reconcile_seconds=30)
        feed.queue.put_nowait((hw.CONNECTED, None, 0))
        await self.settle()
        await w.tick()
        self.assertEqual(self.triggers(), [])
        self.reserves = (10 ** 15, 3 * 10 ** 9)                             # moved with no websocket message
        self.slot += 5
        self.t += 31
        await w.tick()
        self.assertEqual([(r[1], r[4]) for r in self.triggers()], [('STOP', 'reconcile')])

    async def test_lost_stream_falls_back_to_polling_with_backoff_and_a_health_warning(self):
        feed = FakeFeed()
        w = await self.start(feed, backoff_cap=60.0)
        feed.queue.put_nowait((hw.CONNECTED, None, 0))
        await self.settle()
        feed.queue.put_nowait(hw.StreamLost('ConnectionResetError'))
        await self.settle()
        self.assertFalse(w.stream_up)
        self.assertEqual(w.warnings.get('STREAM_DOWN'), 'ConnectionResetError')
        self.assertEqual(self.sleeps[:1], [2.0])                            # 2**1 * (0.5 + 1.0/2)
        self.t += 3
        await w.tick()
        self.assertEqual(w.status, 'POLLING')
        # more failures: exponential, capped
        for _ in range(8):
            feed.queue.put_nowait(hw.StreamLost('TimeoutError'))
            await self.settle()
        self.assertEqual(max(self.sleeps), 60.0)
        self.assertEqual(self.sleeps[:4], [2.0, 4.0, 8.0, 16.0])
        # recovery clears the warning and resets the backoff
        feed.queue.put_nowait((hw.CONNECTED, None, 0))
        await self.settle()
        self.assertTrue(w.stream_up)
        self.assertNotIn('STREAM_DOWN', w.warnings)

    async def test_account_set_change_restarts_the_stream(self):
        feed = FakeFeed()
        w = await self.start(feed)
        self.assertEqual(len(feed.calls), 1)
        self.write_state({})
        self.t += 6
        await w.tick()
        await self.settle()
        self.assertEqual(w.stream_accounts, frozenset())
        self.write_state({self.mint: self.pos()})
        self.t += 6
        await w.tick()
        await self.settle()
        self.assertEqual(len(feed.calls), 2)

    async def test_run_loop_stops_cleanly_and_writes_health(self):
        feed = FakeFeed()
        self.write_state({self.mint: self.pos()})
        w = self.make(feed=feed, tick_seconds=0.01)
        stop = asyncio.Event()
        task = asyncio.create_task(w.run(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, 2)
        health = json.loads((self.root / 'state' / 'health.json').read_text())
        self.assertEqual(health['positions_watched'], 1)
        self.assertEqual(health['allowance_limit'], 1800)
        self.assertEqual(os.stat(self.root / 'state' / 'health.json').st_mode & 0o777, 0o600)
        self.assertTrue(w.stream_task is None or w.stream_task.done())


@unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
class LedgerSafetyTests(WatcherBase):
    def digest(self):
        """The ledger file itself. A plain ``mode=ro`` WAL reader may create empty -shm/-wal sidecars; that is SQLite,
        not a ledger write, and the immutable fallback (read-only mounts) creates none."""
        path = self.ledger
        return (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns, path.stat().st_size)

    async def test_the_ledger_is_never_written(self):
        self.write_state({self.mint: self.pos()})
        before = self.digest()
        w = self.make(debounce_seconds=0)
        await w.tick()
        self.t += 3
        self.reserves = (10 ** 15, 3 * 10 ** 9)
        self.slot += 1
        await w.tick()
        self.assertTrue(self.triggers())
        self.assertEqual(self.digest(), before)
        with closing(sqlite3.connect(self.ledger.as_uri() + '?mode=ro', uri=True)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("UPDATE state SET payload='x'")

    async def test_symlinked_ledger_is_refused(self):
        self.write_state({self.mint: self.pos()})
        link = self.root / 'link.sqlite'
        link.symlink_to(self.ledger)
        with self.assertRaises(hw.WatcherError):
            hw.read_ledger_state(link)

    async def test_watcher_state_lives_outside_the_ledger_directory_files(self):
        self.write_state({self.mint: self.pos()})
        w = self.make()
        await w.tick()
        names = {p.name for p in self.root.glob('paper-ledger.sqlite*')}
        self.assertLessEqual(names, {'paper-ledger.sqlite', 'paper-ledger.sqlite-shm', 'paper-ledger.sqlite-wal'})
        wal = self.root / 'paper-ledger.sqlite-wal'
        self.assertTrue(not wal.exists() or wal.stat().st_size == 0)         # a reader never appends frames
        self.assertEqual({p.name for p in (self.root / 'state').glob('*.sqlite')}, {'watcher.sqlite'})


class WebsocketFeedTests(unittest.IsolatedAsyncioTestCase):
    class FakeWs:
        def __init__(self, script):
            self.sent, self.script = [], script

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send(self, text):
            self.sent.append(json.loads(text))

        def __aiter__(self):
            return self._iter()

        async def _iter(self):
            for item in self.script(self.sent):
                if isinstance(item, Exception):
                    raise item
                yield json.dumps(item)

    def feed(self, script, secret='SECRETKEY'):
        ws = self.FakeWs(script)
        self.ws = ws
        return hw.WebsocketFeed('wss://x/?api-key=' + secret, connect=lambda *a, **k: ws)

    async def collect(self, feed, accounts, limit=10):
        out = []
        try:
            async for item in feed.updates(accounts):
                out.append(item)
                if len(out) >= limit:
                    break
        except hw.StreamLost as exc:
            out.append(('LOST', str(exc)))
        return out

    async def test_subscribes_signals_connected_then_maps_notifications_by_subscription(self):
        def script(sent):
            yield {'jsonrpc': '2.0', 'id': 1, 'result': 11}
            yield {'jsonrpc': '2.0', 'id': 2, 'result': 22}
            yield {'method': 'accountNotification', 'params': {'subscription': 22, 'result': {'context': {'slot': 9}, 'value': 'V2'}}}
            yield {'method': 'accountNotification', 'params': {'subscription': 999, 'result': {'context': {'slot': 9}, 'value': 'X'}}}
            yield {'method': 'accountNotification', 'params': {'subscription': 11, 'result': {'context': {'slot': 10}, 'value': 'V1'}}}
            yield {'method': 'other'}
        got = await self.collect(self.feed(script), ['A', 'B'])
        self.assertEqual(got[:3], [(hw.CONNECTED, None, 0), ('B', 'V2', 9), ('A', 'V1', 10)])
        self.assertEqual(got[3], ('LOST', 'STREAM_CLOSED'))
        self.assertEqual([m['method'] for m in self.ws.sent], ['accountSubscribe'] * 2)
        self.assertEqual(self.ws.sent[0]['params'], ['A', {'encoding': 'base64', 'commitment': 'confirmed'}])

    async def test_rejected_subscription_and_transport_errors_are_classes_only(self):
        got = await self.collect(self.feed(lambda sent: [{'id': 1, 'error': {'message': 'key SECRETKEY invalid'}}]), ['A'])
        self.assertEqual(got, [('LOST', 'SUBSCRIPTION_REJECTED')])
        got = await self.collect(self.feed(lambda sent: [ConnectionResetError('wss://x/?api-key=SECRETKEY reset')]), ['A'])
        self.assertEqual(got, [('LOST', 'ConnectionResetError')])
        for _, text in got:
            self.assertNotIn('SECRETKEY', text)

    async def test_no_accounts_is_not_a_connection(self):
        got = await self.collect(self.feed(lambda sent: []), [])
        self.assertEqual(got, [('LOST', 'NO_ACCOUNTS')])


class HeliusRpcTests(unittest.TestCase):
    class Resp:
        def __init__(self, status, body):
            self.status, self.body = status, body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return self.body.encode()

    def rpc(self, behaviour):
        self.seen = []

        def opener(request, timeout):
            self.seen.append((request.full_url, request.data, timeout))
            return behaviour()
        return hw.HeliusRpc('SECRETKEY', opener=opener)

    def test_success_and_request_shape(self):
        rpc = self.rpc(lambda: self.Resp(200, json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'v': 1}})))
        self.assertEqual(rpc('getMultipleAccounts', [['a']]), {'v': 1})
        url, body, timeout = self.seen[0]
        self.assertTrue(url.startswith('https://mainnet.helius-rpc.com/'))
        self.assertEqual(json.loads(body)['method'], 'getMultipleAccounts')
        self.assertEqual(timeout, 5.0)

    def test_errors_are_classes_and_never_contain_the_key_or_url(self):
        def http429():
            raise urllib.error.HTTPError('https://mainnet.helius-rpc.com/?api-key=SECRETKEY', 429, 'Too Many', {}, io.BytesIO(b''))
        cases = {
            'HTTP_429': http429,
            'TIMEOUT': lambda: (_ for _ in ()).throw(TimeoutError('t')),
            'NETWORK': lambda: (_ for _ in ()).throw(urllib.error.URLError('https://x/?api-key=SECRETKEY')),
            'BAD_RESPONSE': lambda: self.Resp(200, 'not json'),
            'RPC_ERROR': lambda: self.Resp(200, json.dumps({'error': {'message': 'SECRETKEY'}})),
            'HTTP_500': lambda: self.Resp(500, '{}'),
        }
        for expected, behaviour in cases.items():
            with self.subTest(expected), self.assertRaises(hw.RpcError) as ctx:
                self.rpc(behaviour)('getSlot', [])
            self.assertEqual(str(ctx.exception), expected)

    def test_redirects_are_not_followed(self):
        handler = hw._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, '', {}, 'http://elsewhere'))


class CliTests(WatcherBase):
    def args(self, **over):
        values = dict(ledger=str(self.ledger), config=str(CONFIG), state_dir=str(self.root / 'state'), credentials_dir=None,
                      trigger='path', unit=UNIT, pool_fee_bps='25', poll_seconds=2.0, reconcile_seconds=30.0,
                      refresh_seconds=5.0, allowance_per_hour=1800, min_request_interval=0.0, debounce_seconds=30.0,
                      max_triggers_per_position_hour=12, max_triggers_hour=60, margin='0.02', no_stream=False, once=True,
                      dry_run=False, min_trigger_gap_seconds=5.0, creator_fee_bps='0', http_backoff_cap=300.0,
                      settle_seconds=1.0, refire_seconds=5.0, max_refires=3, stall_seconds=60.0, stable_seconds=60.0,
                      price_guarantee_seconds=60.0, max_time_triggers_per_position_hour=6, max_time_triggers_hour=30)
        values.update(over)
        return SimpleNamespace(**values)

    @unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
    async def test_once_cycle_with_injected_rpc_needs_no_credentials_and_can_fire(self):
        self.write_state({self.mint: self.pos()})
        self.reserves = (10 ** 15, 3 * 10 ** 9)
        result = await hw.run_command(self.args(), rpc=self.fake_rpc, clock=lambda: self.t)
        self.assertEqual(result['positions'], [self.mint])
        self.assertEqual(result['triggers'], 1)
        self.assertTrue((self.root / 'state' / 'health.json').exists())
        self.assertTrue((self.root / 'state' / 'trigger' / 'request').exists())

    @unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
    async def test_dry_run_flag_never_writes_a_request(self):
        self.write_state({self.mint: self.pos()})
        self.reserves = (10 ** 15, 3 * 10 ** 9)
        result = await hw.run_command(self.args(dry_run=True), rpc=self.fake_rpc, clock=lambda: self.t)
        self.assertEqual(result['triggers'], 1)
        self.assertFalse((self.root / 'state' / 'trigger' / 'request').exists())

    async def test_argument_validation_fails_closed(self):
        for over in ({'poll_seconds': 0.1}, {'poll_seconds': 600}, {'allowance_per_hour': 0}, {'margin': '0.9'},
                     {'debounce_seconds': -1}, {'max_triggers_hour': 0}, {'refresh_seconds': 0.1},
                     {'min_trigger_gap_seconds': -1}, {'min_trigger_gap_seconds': 601}, {'http_backoff_cap': 0},
                     {'refire_seconds': 0}, {'max_refires': 11}, {'price_guarantee_seconds': 0}, {'creator_fee_bps': 'x'},
                     {'max_time_triggers_hour': 0}, {'stall_seconds': -1}):
            with self.subTest(over), self.assertRaises(hw.WatcherError):
                await hw.run_command(self.args(**over), rpc=self.fake_rpc)

    def test_credentials_must_be_private_and_contain_the_helius_key(self):
        cred = self.root / 'cred'
        cred.mkdir()
        path = cred / 'provider-keys.json'
        path.write_text(json.dumps({'HELIUS_API_KEY': 'abc123'}))
        os.chmod(path, 0o644)
        with self.assertRaises(hw.WatcherError):
            hw.load_helius_key(str(cred))
        os.chmod(path, 0o400)
        self.assertEqual(hw.load_helius_key(str(cred)), 'abc123')
        for value in ({}, {'HELIUS_API_KEY': ''}, {'HELIUS_API_KEY': 'a&b=c'}, {'HELIUS_API_KEY': 5}, []):
            os.chmod(path, 0o600)
            path.write_text(json.dumps(value))
            os.chmod(path, 0o400)
            with self.subTest(value), self.assertRaises(hw.WatcherError):
                hw.load_helius_key(str(cred))
        with self.assertRaises(hw.WatcherError):
            hw.load_helius_key(None)

    def test_main_reports_blocked_without_a_traceback(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = hw.main(['run', '--ledger', str(self.ledger), '--config', str(CONFIG), '--state-dir', str(self.root / 's'),
                            '--poll-seconds', '0.1'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'BLOCKED')

    def test_main_without_credentials_is_blocked_before_any_io(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = hw.main(['run', '--ledger', str(self.ledger), '--config', str(CONFIG), '--state-dir', str(self.root / 's'),
                            '--credentials-dir', str(self.root / 'nowhere')])
        self.assertEqual(code, 2)


@unittest.skipUnless(HAVE_SOLDERS, 'optional solders dependency')
class ReviewFixTests(WatcherBase):
    """T28F: one test (or more) per coordinator finding."""

    async def crossing(self, **kwargs):
        self.write_state({self.mint: self.pos()})
        w = self.make(**kwargs)
        await w.tick()
        return w

    def pair(self, w, base, quote, slot):
        """A complete same-slot pair, the way a swap produces it."""
        w.apply_account(self.pool.base_vault, token_account(self.pool.mint_s, base), slot, 'stream')
        w.handle_update(self.pool.quote_vault, token_account(SOL, quote), slot, 'stream')

    def add_event(self, mint, ts, kind='quote_exit'):
        ledger = Ledger(str(self.ledger))
        ledger.db.execute('INSERT INTO events(event_id, ts, payload, payload_hash) VALUES(?,?,?,?)',
                          ('ev-%s-%s' % (ts, kind), ts, json.dumps({'kind': kind, 'mint': mint, 'ts': ts}), 'h'))
        ledger.close()

    # ---- item 2: only complete same-slot pairs are ever priced
    async def test_a_lone_vault_update_cannot_fake_a_price_spike(self):
        w = await self.crossing()
        self.slot += 1
        # Liquidity doubles: both reserves double, price unchanged. The base notification arrives first.
        w.handle_update(self.pool.base_vault, token_account(self.pool.mint_s, 2 * 10 ** 15), self.slot, 'stream')
        self.assertEqual(self.triggers(), [])                       # old base+new... pairing would read ratio ~0.49 -> fake STOP
        w.handle_update(self.pool.quote_vault, token_account(SOL, 20 * 10 ** 9), self.slot, 'stream')
        self.assertEqual(self.triggers(), [])
        self.assertEqual(w.snapshots[self.mint]['slot'], self.slot)

    async def test_vaults_from_different_slots_are_never_combined(self):
        w = await self.crossing()
        base_slot = self.slot + 1
        w.handle_update(self.pool.base_vault, token_account(self.pool.mint_s, self.reserves[0]), base_slot, 'stream')
        w.handle_update(self.pool.quote_vault, token_account(SOL, 3 * 10 ** 9), base_slot + 1, 'stream')   # a LATER slot
        self.assertEqual(self.triggers(), [])
        self.assertLess(w.snapshots[self.mint]['slot'], base_slot)   # still the old complete pair
        w.handle_update(self.pool.base_vault, token_account(self.pool.mint_s, self.reserves[0]), base_slot + 1, 'stream')
        self.assertEqual([r[1] for r in self.triggers()], ['STOP'])  # the slot completed: now it is priced

    async def test_a_lone_update_is_completed_by_one_same_slot_poll(self):
        w = await self.crossing(settle_seconds=1.0)
        self.slot += 1
        w.handle_update(self.pool.quote_vault, token_account(SOL, 3 * 10 ** 9), self.slot, 'stream')   # one-sided
        self.assertIn(self.mint, w.partial_since)
        self.reserves = (10 ** 15, 3 * 10 ** 9)
        self.t += 0.5
        polls = len([c for c in self.rpc_calls if c[0] == 'getMultipleAccounts'])
        await w.tick()
        self.assertEqual(len([c for c in self.rpc_calls if c[0] == 'getMultipleAccounts']), polls)      # not yet: it may be a swap arriving
        self.t += 1.0
        self.slot += 1
        await w.tick()
        self.assertEqual(len([c for c in self.rpc_calls if c[0] == 'getMultipleAccounts']), polls + 1)  # settled by ONE poll
        self.assertEqual([r[1] for r in self.triggers()], ['STOP'])
        self.assertNotIn(self.mint, w.partial_since)

    async def test_polls_never_split_a_pools_two_vaults_across_calls(self):
        w = self.make()
        w.accounts = {}
        for n in range(75):                                            # 150 accounts: needs two calls
            w.accounts['B%03d' % n] = ('M%03d' % n, 'base')
            w.accounts['Q%03d' % n] = ('M%03d' % n, 'quote')
        chunks = list(w.poll_chunks())
        self.assertEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), hw.MAX_ACCOUNTS_PER_CALL)
            mints = [w.accounts[a][0] for a in chunk]
            self.assertTrue(all(mints.count(m) == 2 for m in mints))   # both vaults of a mint are always together

    # ---- item 1: price exits have their own budget
    async def test_unresolved_time_reasons_cannot_exhaust_the_price_budget(self):
        self.write_state({self.mint: self.pos(opened_at=int(self.t) - 21_601)})          # MAX_HOLD holds from the start
        w = self.make(debounce_seconds=0, min_trigger_gap=0, max_time_triggers_per_position_hour=3,
                      max_triggers_per_position_hour=2, price_guarantee_seconds=3600)
        for _ in range(8):
            await w.tick()
            self.t += 1
        self.assertEqual([r[1] for r in self.triggers()], ['MAX_HOLD'] * 3)
        self.assertIn('TRIGGER_RATE_LIMITED', w.warnings)
        self.reserves = (10 ** 15, 3 * 10 ** 9)                                           # now a real crash
        self.slot += 1
        await w.tick()
        rows = self.store.db.execute('SELECT reason, klass, ok FROM triggers ORDER BY id').fetchall()
        self.assertEqual(rows[-1], ('STOP', 'price', 1))
        self.assertEqual(self.store.triggers_since(self.t - 3600, None, 'time'), 3)          # the time budget stayed exhausted
        self.assertGreaterEqual(self.store.triggers_since(self.t - 3600, None, 'price'), 1)  # while the price exit was never blocked

    async def test_a_time_reason_the_held_pass_already_handled_is_backed_off_exponentially(self):
        self.write_state({self.mint: self.pos(opened_at=int(self.t) - 21_601)})
        w = self.make(debounce_seconds=30, min_trigger_gap=0)
        await w.tick()
        first = self.t
        self.assertEqual(len(self.triggers()), 1)
        self.add_event(self.mint, int(first) + 2)                    # the held pass looked at it; the position is still open
        self.t = first + 31
        await w.tick()
        self.assertEqual(len(self.triggers()), 1)                    # a plain 30 s debounce would have fired here
        self.t = first + 61
        await w.tick()
        self.assertEqual(len(self.triggers()), 2)
        self.add_event(self.mint, int(self.t) + 2)
        self.t += 61
        await w.tick()
        self.assertEqual(len(self.triggers()), 2)                    # now 2 x 2 = 120 s
        self.t += 60
        await w.tick()
        self.assertEqual(len(self.triggers()), 3)

    async def test_a_price_exit_fires_once_per_guarantee_window_whatever_the_caps_say(self):
        w = await self.crossing(debounce_seconds=0, min_trigger_gap=0, max_triggers_per_position_hour=1, price_guarantee_seconds=60)
        self.pair(w, 10 ** 15, 3 * 10 ** 9, self.slot + 1)
        self.t += 10
        self.pair(w, 10 ** 15, 3 * 10 ** 9, self.slot + 2)
        self.assertEqual(len(self.triggers()), 1)                    # capped inside the window
        self.t += 55
        self.pair(w, 10 ** 15, 3 * 10 ** 9, self.slot + 3)
        self.assertEqual([r[1] for r in self.triggers()], ['STOP', 'STOP'])   # the guarantee overrides the cap

    # ---- item 3: provider backoff and the 0.5 req/s ceiling
    async def test_provider_failures_put_requests_on_hold_with_exponential_jittered_backoff(self):
        w = self.make(http_backoff_cap=100.0, rng=lambda: 1.0)
        for code, hold in (('HTTP_429', 2.0), ('HTTP_503', 4.0), ('TIMEOUT', 8.0)):
            self.rpc_failure = hw.RpcError(code)
            with self.assertRaises(hw.RpcError):
                await w.request('probe', 'getSlot', [])
            self.assertAlmostEqual(w.backoff_until - self.t, hold)
            used = self.store.used(self.t)
            with self.assertRaises(hw.ProviderBackoff):
                await w.request('probe', 'getSlot', [])
            self.assertEqual(self.store.used(self.t), used)          # held requests are not sent and not charged
            self.t = w.backoff_until + 0.01
        self.assertIn('PROVIDER_BACKOFF', w.warnings)
        self.rpc_failure = None
        self.reserves = (10 ** 15, 10 ** 10)
        await w.request('probe', 'getBlockTime', [1])
        self.assertEqual((w.http_failures, 'PROVIDER_BACKOFF' in w.warnings), (0, False))   # success resets the ladder

    async def test_backoff_is_capped_jittered_and_not_applied_to_other_errors(self):
        w = self.make(http_backoff_cap=5.0, rng=lambda: 0.0)
        for _ in range(6):
            self.rpc_failure = hw.RpcError('HTTP_429')
            with self.assertRaises(hw.RpcError):
                await w.request('probe', 'getSlot', [])
            self.assertLessEqual(w.backoff_until - self.t, 2.5 + 1e-9)    # cap 5 x jitter 0.5
            self.t = w.backoff_until + 0.01
        self.rpc_failure = hw.RpcError('HTTP_401')
        before = w.backoff_until
        with self.assertRaises(hw.RpcError):
            await w.request('probe', 'getSlot', [])
        self.assertEqual(w.backoff_until, before)                         # an auth error is not a "retry later"

    async def test_default_request_rate_is_at_most_half_a_request_per_second(self):
        self.write_state({self.mint: self.pos()})
        store = hw.WatcherStore(self.root / 'rate' / 'watcher.sqlite')
        self.addCleanup(store.close)
        trigger = hw.Trigger('none', UNIT, None, None, lambda: self.t)
        w = hw.Watcher(ledger=self.ledger, cfg=self.cfg, store=store, trigger=trigger, rpc=self.fake_rpc, clock=lambda: self.t,
                       sleep=self.fake_sleep, rng=lambda: 1.0)
        self.assertEqual((w.min_interval, w.allowance), (2.0, 1800))
        start = self.t
        while self.t - start < 600:
            await w.tick()
            self.t += 0.25
        used = store.used(self.t)
        self.assertLessEqual(used / (self.t - start), 0.5 + 0.01)
        parsed = hw.build_parser().parse_args(['run', '--ledger', 'l', '--config', 'c', '--state-dir', 's'])
        self.assertEqual((parsed.min_request_interval, parsed.allowance_per_hour), (2.0, 1800))

    # ---- item 4: a lost request is asked again
    async def test_a_request_with_no_following_held_pass_is_refired_then_reported(self):
        w = await self.crossing(debounce_seconds=300, min_trigger_gap=0)
        self.reserves = (10 ** 15, 3 * 10 ** 9)                       # the crash persists: polls keep seeing it
        self.pair(w, 10 ** 15, 3 * 10 ** 9, self.slot + 1)
        self.assertEqual(len(self.triggers()), 1)
        for step, expected in ((5.1, 2), (10.1, 3), (20.1, 4)):
            self.t += step
            self.slot += 1
            await w.tick()
            self.assertEqual(len(self.triggers()), expected)
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT source FROM triggers ORDER BY id')][1:], ['refire'] * 3)
        self.t += 41
        await w.tick()
        self.assertEqual(len(self.triggers()), 4)                     # three refires at most
        self.assertIn('TRIGGER_NOT_ACKNOWLEDGED', w.warnings)
        self.assertEqual(w.stats['refires'], 3)

    async def test_no_refire_when_the_ledger_shows_a_held_pass_after_the_request(self):
        w = await self.crossing(debounce_seconds=300, min_trigger_gap=0)
        self.reserves = (10 ** 15, 3 * 10 ** 9)
        self.pair(w, 10 ** 15, 3 * 10 ** 9, self.slot + 1)
        self.add_event(self.mint, int(self.t) + 1)                   # the held pass ran after the request
        self.t += 6
        self.slot += 1
        await w.tick()
        self.assertEqual(len(self.triggers()), 1)
        self.assertNotIn(self.mint, w.pending_refire)

    async def test_refire_stops_when_the_reason_disappears(self):
        w = await self.crossing(debounce_seconds=300, min_trigger_gap=0)
        self.pair(w, 10 ** 15, 3 * 10 ** 9, self.slot + 1)
        self.reserves = (10 ** 15, 10 * 10 ** 9)                      # price recovered
        self.t += 6
        self.slot += 1
        await w.tick()
        self.assertEqual(len(self.triggers()), 1)
        self.assertNotIn(self.mint, w.pending_refire)

    # ---- item 5: slot -> block time
    async def test_block_time_of_the_observed_slot_is_recorded_after_a_trigger(self):
        w = await self.crossing(debounce_seconds=300, min_trigger_gap=0)
        slot = self.slot + 1
        self.block_times[slot] = 1_700_000_123
        self.pair(w, 10 ** 15, 3 * 10 ** 9, slot)
        self.t += 2.5
        await w.tick()
        rows = self.store.db.execute('SELECT slot, block_time, ok FROM trigger_slot_times').fetchall()
        self.assertEqual(rows, [(slot, 1_700_000_123, 1)])

    async def test_block_time_failures_are_recorded_and_retried_a_bounded_number_of_times(self):
        w = await self.crossing(debounce_seconds=300, min_trigger_gap=0, max_refires=0)
        self.pair(w, 10 ** 15, 3 * 10 ** 9, self.slot + 1)
        self.rpc_failure = hw.RpcError('RPC_ERROR')
        for _ in range(8):
            self.t += 3
            await w.tick()
        rows = self.store.db.execute('SELECT ok FROM trigger_slot_times').fetchall()
        self.assertEqual(rows, [(0,)] * 3)                            # first try + two retries, then it stops

    # ---- item 6: stream health
    async def test_a_silent_but_connected_stream_is_verified_and_a_stall_falls_back_to_polling(self):
        feed = FakeFeed()
        self.write_state({self.mint: self.pos()})
        w = self.make(feed=feed, stall_seconds=60, reconcile_seconds=0)
        await w.tick()
        await self.settle()
        feed.queue.put_nowait((hw.CONNECTED, None, 0))
        await self.settle()
        await w.tick()
        self.assertEqual(w.status, 'STREAM')
        self.t += 61                                                   # a quiet pool: nothing moved
        await w.tick()
        self.assertNotIn('STREAM_STALLED', w.warnings)
        self.assertEqual(w.stats['stalls'], 0)
        self.t += 61
        self.reserves = (10 ** 15, 3 * 10 ** 9)                        # moved, but no notification arrived
        self.slot += 1
        await w.tick()
        await self.settle()
        self.assertEqual(w.stats['stalls'], 1)
        self.assertIn('STREAM_STALLED', w.warnings)
        self.assertEqual([r[1] for r in self.triggers()], ['STOP'])    # the poll still caught the crossing
        self.assertFalse(w.stream_up)                                  # polling again until the new connection is acknowledged
        await w.tick()
        await self.settle()
        self.assertEqual(len(feed.calls), 2)                           # and a fresh stream was started

    async def test_reconnect_backoff_is_kept_across_flapping_connections_and_reset_only_when_stable(self):
        feed = FakeFeed()
        w = await self.start_stream(feed, stable_seconds=60)
        for _ in range(3):                                             # connect, lose it within a second
            feed.queue.put_nowait((hw.CONNECTED, None, 0))
            await self.settle()
            feed.queue.put_nowait(hw.StreamLost('FLAP'))
            await self.settle(20)
        self.assertEqual([x for x in self.sleeps if x >= 1], [2.0, 4.0, 8.0])     # not 2, 2, 2
        feed.queue.put_nowait((hw.CONNECTED, None, 0))
        await self.settle()
        self.t += 61                                                   # a stable connection
        feed.queue.put_nowait(hw.StreamLost('LATE'))
        await self.settle(20)
        self.assertEqual([x for x in self.sleeps if x >= 1][-1], 2.0)             # the ladder starts over

    async def start_stream(self, feed, **kwargs):
        self.write_state({self.mint: self.pos()})
        w = self.make(feed=feed, **kwargs)
        await w.tick()
        await self.settle()
        return w

    # ---- item 8: mark accuracy and visibility
    def test_creator_and_transfer_fees_lower_the_mark_and_the_known_answers_hold(self):
        cfg = load_config(CONFIG)
        base = hw.implied_ratio(position(), 10 ** 15, 10 * 10 ** 9, cfg, '25')
        creator = hw.implied_ratio(position(), 10 ** 15, 10 * 10 ** 9, cfg, '25', creator_fee_bps=5)
        from desk.strategy import swap_quote
        e = {'route_available': True, 'reserve_sol': '10', 'reserve_tokens': '1000000000', 'pool_fee_bps': '30'}
        self.assertEqual(creator, (swap_quote(e, D('10000000'), 'sell', cfg) - D(cfg['fixed_fee_sol'])) / D('0.1'))   # 25 + 5 bps
        self.assertLess(creator, base)
        transfer = hw.implied_ratio(position(), 10 ** 15, 10 * 10 ** 9, cfg, '25', transfer_fee_bps=100)
        effective = D('10000000') * (1 - D('0.01')) * (1 - D('0.0025'))
        out = D(10) * effective / (D(10 ** 9) + effective) * (1 - D(cfg['adverse_slippage_bps']) / 10000)
        self.assertEqual(transfer, (out - D(cfg['fixed_fee_sol'])) / D('0.1'))
        virtual = hw.implied_ratio(position(), 10 ** 15, 10 * 10 ** 9, cfg, '25', virtual_quote_raw=10 * 10 ** 9)
        self.assertGreater(virtual, base)                              # a boosted pool prices on gross + virtual reserves

    def test_mint_facts_decode_decimals_and_the_transfer_fee_extension(self):
        plain = self.mint_account()
        self.assertEqual(hw.mint_facts(plain), (6, 0))
        body = bytes(106) + (250).to_bytes(2, 'little')                       # newer schedule: 250 bps
        tlv = (1).to_bytes(2, 'little') + (108).to_bytes(2, 'little') + body
        self.assertEqual(hw.mint_facts(self.mint_account(owner=TOKEN_2022, extensions=tlv)), (6, 250))
        for bad in (self.mint_account(owner=TOKEN_2022, extensions=(1).to_bytes(2, 'little') + (108).to_bytes(2, 'little') + bytes(10)),
                    self.mint_account(owner=TOKEN_2022, extensions=(1).to_bytes(2, 'little') + (7).to_bytes(2, 'little') + bytes(7)),
                    {**plain, 'owner': PUMPSWAP}, {**plain, 'data': ['!!', 'base64']}, None):
            with self.assertRaises(hw.WatcherError):
                hw.mint_facts(bad)

    async def test_a_token_2022_transfer_fee_is_read_once_and_moves_the_stop(self):
        tlv = (1).to_bytes(2, 'little') + (108).to_bytes(2, 'little') + bytes(106) + (250).to_bytes(2, 'little')
        original = self.mint_account
        self.mint_account = lambda **kw: original(owner=TOKEN_2022, extensions=tlv)
        self.write_state({self.mint: self.pos()})
        w = self.make()
        self.reserves = (10 ** 15, int(8.6 * 10 ** 9))
        plain = hw.implied_ratio(position(), self.reserves[0], self.reserves[1], self.cfg, '25')
        fee = hw.implied_ratio(position(), self.reserves[0], self.reserves[1], self.cfg, '25', transfer_fee_bps=250)
        self.assertGreater(plain, D('0.82') + D('0.02'))              # without the fee this is still above stop + margin
        self.assertLessEqual(fee, D('0.82') + D('0.02'))              # with the 2.5% fee it is not
        await w.tick()
        self.assertEqual(w.facts[self.mint]['transfer_fee_bps'], 250)
        self.assertEqual(json.loads(self.store.vault(self.pool.pool_s)[3])['transfer_fee_bps'], 250)   # cached with the vaults
        self.assertEqual([r[1] for r in self.triggers()], ['STOP'])
        reads = [c for c in self.rpc_calls if c[0] == 'getAccountInfo']
        self.assertEqual(len(reads), 2)                               # pool + mint, once
        w2 = self.make()                                              # a restart reuses the cached facts: no new reads
        await w2.tick()
        self.assertEqual(len([c for c in self.rpc_calls if c[0] == 'getAccountInfo']), 2)
        self.assertEqual(w2.facts[self.mint]['transfer_fee_bps'], 250)

    async def test_position_without_quote_execution_warns_and_still_prices_from_the_mint(self):
        position_without = self.pos()
        del position_without['quote_execution']
        self.write_state({self.mint: position_without})
        self.reserves = (10 ** 15, 3 * 10 ** 9)
        w = self.make()
        await w.tick()
        self.assertIn('POSITION_NO_QUOTE_EXECUTION', w.warnings)       # visible, never silent
        self.assertEqual([r[1] for r in self.triggers()], ['STOP'])    # decimals came from the mint account
        self.assertNotIn('POSITION_NO_DECIMALS', w.warnings)

    async def test_unknown_decimals_are_a_warning_not_a_silent_drop(self):
        position_without = self.pos()
        del position_without['quote_execution']
        self.write_state({self.mint: position_without})
        original = self.fake_rpc
        def rpc(method, params):
            if method == 'getAccountInfo' and params[0] == self.mint:
                raise hw.RpcError('HTTP_404')
            return original(method, params)
        w = self.make()
        w.rpc = rpc
        self.reserves = (10 ** 15, 3 * 10 ** 9)
        await w.tick()
        self.assertIn('POSITION_NO_DECIMALS', w.warnings)
        self.assertIn('POSITION_NO_QUOTE_EXECUTION', w.warnings)
        self.assertEqual(self.triggers(), [])

class LatencyReportTests(WatcherBase):
    def build(self, passes, trigger_rows, slot_times=()):
        """passes: (event_id, ts, mint, kind, fill_reason-or-None); trigger_rows: record_trigger positional args."""
        ledger = Ledger(str(self.ledger))
        for event_id, ts, mint, kind, fill in passes:
            ledger.db.execute('INSERT INTO events(event_id, ts, payload, payload_hash) VALUES(?,?,?,?)',
                              (event_id, ts, json.dumps({'kind': kind, 'mint': mint, 'ts': ts}), 'h'))
            if fill is not None:
                side, reason = fill
                ledger.db.execute('INSERT INTO outcomes(event_id, payload) VALUES(?,?)',
                                  (event_id, json.dumps({'type': 'fill', 'side': side, 'mint': mint, 'reason': reason})))
        ledger.close()
        store = hw.WatcherStore(self.root / 'state' / 'watcher.sqlite')
        ids = [store.record_trigger(*row) for row in trigger_rows]
        for index, slot, block_time in slot_times:
            store.record_slot_time(ids[index], slot, 0.0, block_time, True)
        store.close()
        return hw.latency_report(self.ledger, self.root / 'state' / 'watcher.sqlite')

    def test_known_answer(self):
        report = self.build(
            [('e1', 5_000, 'M1', 'market', ('buy', 'ENTRY')), ('e2', 5_105, 'M1', 'quote_exit', ('sell', 'STOP')),
             ('e3', 6_030, 'M2', 'quote_exit', ('sell', 'TRAILING_STOP'))],
            [(5_100.0, 'M1', ['STOP'], D('0.5'), 77, 5_099.5, 'stream', 'path', True, 'ok', 'price'),
             (6_000.0, 'M2', ['TRAILING_STOP'], D('2'), 78, 5_999.0, 'poll', 'path', True, 'ok', 'price'),
             (7_000.0, 'M3', ['STOP'], None, None, None, 'tick', 'path', True, 'ok', 'price')],
            slot_times=[(0, 77, 5_098)])
        self.assertEqual((report['summary']['triggers'], report['summary']['with_fill'], report['summary']['with_pass']), (3, 2, 2))
        self.assertEqual(report['summary']['trigger_to_fill_median_seconds'], 17.5)       # (5 and 30)
        first, second, third = report['triggers']
        self.assertEqual((first['trigger_to_fill_seconds'], first['fill_reason'], first['watch_to_trigger_seconds']), (5.0, 'STOP', 0.5))
        self.assertEqual((first['trigger_to_pass_seconds'], first['slot_block_time'], first['slot_to_trigger_seconds']), (5.0, 5_098, 2.0))
        self.assertEqual(first['slot_wall_time'], 'BLOCK_TIME')
        self.assertEqual((second['trigger_to_fill_seconds'], second['fill_reason'], second['slot_wall_time']), (30.0, 'TRAILING_STOP', 'NOT_RECORDED'))
        self.assertIsNone(third['trigger_to_fill_seconds'])
        self.assertEqual(report['summary']['slot_to_trigger_median_seconds'], 2.0)

    def test_pairing_is_strictly_after_the_trigger_and_inside_the_window(self):
        report = self.build(
            [('before', 5_099, 'M1', 'quote_exit', ('sell', 'STOP')),       # a fill BEFORE the trigger is never its fill
             ('same', 5_100, 'M1', 'quote_exit', ('sell', 'STOP')),         # same whole second: not provably after
             ('far', 5_100 + 301, 'M1', 'quote_exit', ('sell', 'STOP'))],   # beyond the bounded window
            [(5_100.0, 'M1', ['STOP'], None, 1, 5_099.0, 'stream', 'path', True, 'ok', 'price')])
        entry = report['triggers'][0]
        self.assertEqual((entry['trigger_to_pass_seconds'], entry['trigger_to_fill_seconds']), (None, None))
        self.assertEqual(report['summary']['with_fill'], 0)

    def test_buy_fills_other_mints_and_unfired_triggers_are_not_matched(self):
        report = self.build(
            [('b', 5_110, 'M1', 'market', ('buy', 'ENTRY')), ('o', 5_120, 'M2', 'quote_exit', ('sell', 'STOP'))],
            [(5_100.0, 'M1', ['STOP'], None, None, None, 'poll', 'path', True, 'ok', 'price'),
             (5_101.0, 'M2', ['STOP'], None, None, None, 'poll', 'path', False, 'RUNNER_OSError', 'price')])
        self.assertEqual(report['summary']['with_fill'], 0)
        first, second = report['triggers']
        self.assertEqual(first['trigger_to_pass_seconds'], 10.0)             # a pass is attributed ...
        self.assertIsNone(first['trigger_to_fill_seconds'])                  # ... but a BUY fill is not an exit fill
        self.assertFalse(second['fired']); self.assertIsNone(second['trigger_to_pass_seconds'])

    def test_several_requests_for_one_pass_attribute_the_latency_once_to_the_last(self):
        report = self.build(
            [('p', 5_130, 'M1', 'quote_exit', ('sell', 'STOP'))],
            [(5_100.0, 'M1', ['STOP'], None, 1, 5_099.0, 'stream', 'path', True, 'ok', 'price'),
             (5_105.0, 'M1', ['STOP'], None, 1, 5_099.0, 'refire', 'path', True, 'ok', 'price'),
             (5_110.0, 'M1', ['STOP'], None, 1, 5_099.0, 'refire', 'path', True, 'ok', 'price')])
        flags = [(t['superseded'], t['trigger_to_fill_seconds']) for t in report['triggers']]
        self.assertEqual(flags, [(True, None), (True, None), (False, 20.0)])
        self.assertEqual((report['summary']['with_fill'], report['summary']['superseded']), (1, 2))   # the fill is counted once

    def test_a_later_unrelated_pass_is_not_attributed_to_a_lost_request(self):
        report = self.build(
            [('late', 5_100 + 4_000, 'M1', 'quote_exit', ('sell', 'MAX_HOLD'))],
            [(5_100.0, 'M1', ['STOP'], None, 1, 5_099.0, 'stream', 'path', True, 'ok', 'price')])
        self.assertIsNone(report['triggers'][0]['trigger_to_pass_seconds'])

    def test_unreadable_inputs_fail_closed(self):
        with self.assertRaises(hw.WatcherError):
            hw.latency_report(self.root / 'missing.sqlite', self.root / 'state' / 'watcher.sqlite')


class DocsTests(unittest.TestCase):
    def test_the_shared_pacing_rationale_and_the_rate_ceiling_are_documented(self):
        text = (REPO / 'docs' / 'ops' / 'HELD_WATCHER.md').read_text()
        for needle in ('T09 F6', '0.5 requests/second', 'PROVIDER_BACKOFF', 'Same-slot reserves', 'price-guarantee',
                       'TRIGGER_NOT_ACKNOWLEDGED', 'STREAM_STALLED', 'POSITION_NO_QUOTE_EXECUTION', 'superseded',
                       'ReadWritePaths='):
            self.assertIn(needle, text)


class UnitTemplateTests(unittest.TestCase):
    FRESH = REPO / 'deploy' / 'fresh'
    MAP = {'FRESH_ROOT': '/var/lib/solana-desk-fresh/v1', 'RELEASE_DIR': '/opt/solana-desk-releases/abc1234',
           'CONFIG': '/etc/solana-paper/fresh.json', 'STATE_DIR': '/var/lib/solana-desk-health'}

    def render(self, name):
        text = (self.FRESH / name).read_text()
        used = set(re.findall(r'<([A-Z_]+)>', text))
        self.assertLessEqual(used, set(self.MAP), name)
        for key, value in self.MAP.items():
            text = text.replace('<%s>' % key, value)
        return text

    def test_service_is_hardened_read_only_on_the_trading_stores_and_loopback_free(self):
        text = self.render('desk-held-watcher.service')
        for needle in ('ProtectSystem=strict', 'NoNewPrivileges=true', 'ProtectHome=true', 'PrivateTmp=true', 'UMask=0077',
                       'MemoryMax=', 'User=solana-desk', 'Restart=on-failure', 'LoadCredential=provider-keys.json:/etc/solana-desk/provider-keys.json',
                       'ReadOnlyPaths=/var/lib/solana-desk-fresh/v1'):
            self.assertIn(needle, text)
        # T28F item 7: the ONLY writable path is the watcher's own directory, not the shared health/state directory.
        writable = [l.split('=', 1)[1] for l in text.splitlines() if l.startswith('ReadWritePaths=')]
        self.assertEqual(writable, ['/var/lib/solana-desk-health/held-watcher'])
        (exec_line,) = [l for l in text.splitlines() if l.startswith('ExecStart=')]
        self.assertIn('--state-dir /var/lib/solana-desk-health/held-watcher ', exec_line + ' ')
        (line,) = [l for l in text.splitlines() if l.startswith('ExecStart=')]
        self.assertIn('-m tools.ops.held_watcher run', line)
        self.assertIn('--ledger /var/lib/solana-desk-fresh/v1/paper-ledger.sqlite', line)
        self.assertNotIn('ReadWritePaths=/var/lib/solana-desk-fresh', text)
        self.assertNotRegex(line, r'0\.0\.0\.0|--(host|bind|listen)')
        self.assertNotIn('DESK_PROVIDER_PACING_DB', re.sub(r'(?m)^#.*$', '', text))   # own pacing; never the shared store
        self.assertRegex(text, r'WorkingDirectory=/opt/solana-desk-releases/abc1234')

    def test_path_unit_starts_only_the_held_cycle_without_privileges(self):
        text = self.render('desk-paper-held-cycle.path')
        self.assertIn('PathChanged=/var/lib/solana-desk-health/held-watcher/trigger/request', text)
        self.assertIn('Unit=desk-paper-held-cycle.service', text)
        self.assertIn('WantedBy=multi-user.target', text)
        self.assertNotIn('ExecStart', text)


if __name__ == '__main__':
    unittest.main()
