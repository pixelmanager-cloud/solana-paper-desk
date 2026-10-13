"""M1: data correctness. #7 effective-reserves marks with the Pump fee tiers, #10 outages / feed staleness / config hashes,
#12 raw_policy, #13 per-pass row spam. Unit tests on real captured mainnet bytes, end to end on the fake world (only HTTP is
faked: tests/lean/fakeworld.py).

Real evidence used (no network):
  fixtures/mainnet-sell-event.json         a PumpSwap SellEvent (pool reserves, virtual reserves, every fee) - exact to the lamport
  fixtures/mainnet-dynamic-fees.json       the Pump fee program's FeeConfig account (the market-cap fee tiers)
  fixtures/lean/m1-mark-calibration.json   8 live (Jupiter q0, vault accounts, pool account) triples from the paper run
"""
import base64
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import adapters as A, marks as M, paper, report
from lean.__main__ import CONFIG_KEYS, ConfigError, build_runner, lean_config_hash, load_config
from lean.store import Store, raw_droppable
from tests.lean.fakeworld import FakeTime, T0, Token, World, mainnet_fee_schedule
from tests.lean.test_e2e_real import KEYS, ROOT, steps

FIX = ROOT / 'fixtures'


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def schedule():
    return M.decode_schedule(json.loads((FIX / 'mainnet-dynamic-fees.json').read_text())['response']['value'])


def model(cfg=None, *, pool_fee_bps=120, with_schedule=True, at=0.0):
    m = M.MarkModel(cfg if cfg is not None else {'model': 'effective'}, pool_fee_bps=pool_fee_bps)
    if with_schedule:
        m.observe_call([json.loads((FIX / 'mainnet-dynamic-fees.json').read_text())['response']['value']], at)
    return m


# ============================================================================================================ #7 unit
class MarkMath(unittest.TestCase):
    def test_the_captured_sell_event_is_reproduced_to_the_lamport(self):
        """vault + virtual prices the output, the tier at the pool's market cap sets lp/protocol/creator, each rounded up."""
        f = json.loads((FIX / 'mainnet-sell-event.json').read_text())['decoded']['fields']
        sched = schedule()
        quote_eff = M.effective_quote(f['pool_quote_token_reserves'], f['virtual_quote_reserves'])
        fees, cap = M.tier_fees(sched, quote_eff, f['pool_base_token_reserves'], f['base_supply'])
        self.assertEqual(fees, {'lp_fee_bps': f['lp_fee_basis_points'], 'protocol_fee_bps': f['protocol_fee_basis_points'],
                                'creator_fee_bps': f['coin_creator_fee_basis_points']})            # 2 / 93 / 30 below 420 SOL
        self.assertLess(cap, 420 * 10 ** 9)
        out = M.sell_output(f['base_amount_in'], f['pool_base_token_reserves'], quote_eff, fees)
        self.assertEqual(out, f['user_quote_amount_out'])
        self.assertEqual(f['pool_quote_token_reserves'] + f['virtual_quote_reserves'],
                         quote_eff)
        # and through the model, with the pool fields as the pool account would give them
        m = model()
        net, info = m.value(f['base_amount_in'], f['pool_base_token_reserves'], f['pool_quote_token_reserves'],
                            pool={'virtual_quote_reserves_raw': f['virtual_quote_reserves'], 'protocol_fees_raw': 0,
                                  'creator_fees_raw': f['creator_fee_unclaimed'], 'coin_creator': f['coin_creator'], 'creator_fee_bps': 0},
                            pool_info={'supply_raw': f['base_supply']}, now=1.0, fee_lamports=0)
        self.assertEqual(net, f['user_quote_amount_out'])
        self.assertEqual((info['fee_bps'], info['fee_source'], info['fee_fallback'], info['pool_source']), (125, 'dynamic', False, 'live'))
        # the desk's own pinned SDK replay agrees (desk.sell_fees, used read-only)
        from desk.sell_fees import standard_sell_amounts
        self.assertEqual(int(standard_sell_amounts(f['base_amount_in'], f['pool_base_token_reserves'], quote_eff, fees,
                                                   has_creator=True)['user_output_raw']), out)

    def test_live_jupiter_quotes_are_matched_by_effective_and_missed_by_vault(self):
        """8 real paper exits: Jupiter's q0 vs the mark from the vaults the runner read <= 6 s from it."""
        data = json.loads((FIX / 'lean' / 'm1-mark-calibration.json').read_text())
        self.assertIn('LEAN_PAPER_RUN', data['provenance'])
        eff, vault = model(), M.MarkModel(None, pool_fee_bps=25)
        r_eff, r_vault = [], []
        for pair in data['pairs']:
            accounts = [pair['base_vault'], pair['quote_vault'], pair['pool']]
            net, info = eff.value_accounts(pair['qty_raw'], accounts, pool_info={'supply_raw': pair['supply_raw']}, now=1.0, fee_lamports=0)
            self.assertEqual((info['fee_source'], info['pool_source']), ('dynamic', 'live'))
            self.assertGreater(info['virtual_raw'], 10 ** 10)                       # every graduated pool is boosted (~17.6 SOL)
            vnet, _ = vault.value_accounts(pair['qty_raw'], accounts[:2], now=1.0, fee_lamports=0)
            r_eff.append(pair['jupiter_out_lamports'] / net)
            r_vault.append(pair['jupiter_out_lamports'] / vnet)
        r_eff.sort(), r_vault.sort()
        median = lambda xs: (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2      # noqa: E731
        # (the tails are price moves inside the <= 6 s between the vault read and the quote, and fees accrued since the screen)
        self.assertLess(abs(median(r_eff) - 1), 0.003, r_eff)
        self.assertTrue(all(abs(r - 1) < 0.015 for r in r_eff[2:6]), r_eff)        # the middle half within 1.5 %
        self.assertGreater(median(r_vault), 1.04, r_vault)                       # the vault mark under-values them
        self.assertLess(sum(abs(r - 1) for r in r_eff), sum(abs(r - 1) for r in r_vault) / 3)

    def test_the_vault_model_is_exactly_todays_mark(self):
        pcfg = paper.PaperConfig(fee_lamports=250_000, slippage_bps=50)
        m = M.MarkModel(None, pool_fee_bps=25)
        for qty, base, quote in ((10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9), (3, 7, 11), (5 * 10 ** 12, 10 ** 14, 10 ** 9)):
            net, info = m.value(qty, base, quote, now=0, fee_lamports=pcfg.fee_lamports)
            self.assertEqual(A.sol(net), A.mark(qty, base, quote, pool_fee_bps=25, pcfg=pcfg))
            self.assertEqual(info['model'], 'vault')
        self.assertEqual((m.keys_per_position, m.call_keys()), (2, []))

    def test_fallbacks_are_flagged(self):
        m = model(with_schedule=False, pool_fee_bps=120)
        net, info = m.value(10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9, pool={'virtual_quote_reserves_raw': 18 * 10 ** 9, 'protocol_fees_raw': 0,
                                                                     'creator_fees_raw': 0, 'coin_creator': 'x', 'creator_fee_bps': 0},
                            pool_info={'supply_raw': 10 ** 15}, now=5.0, fee_lamports=0)
        self.assertEqual((info['fee_source'], info['fee_fallback'], info['fee_bps']), ('config', True, 120))
        # a stale schedule is not used
        m = model(at=0.0)
        _, info = m.value(10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9, pool_info={'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 15},
                          now=3601.0, fee_lamports=0)
        self.assertEqual((info['fee_source'], info['pool_source']), ('config', 'screen'))
        _, info = m.value(10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9, pool_info={'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 15},
                          now=3599.0, fee_lamports=0)
        self.assertEqual((info['fee_source'], info['fee_bps']), ('dynamic', 120))             # 98 SOL x 5 = 490 SOL mcap: tier 1
        # an undecodable schedule account is not used
        m = M.MarkModel({'model': 'effective'}, pool_fee_bps=77)
        m.observe_call([{'owner': 'x', 'data': ['AAAA', 'base64'], 'executable': False}], 1.0)
        self.assertIsNone(m.schedule(1.0))
        self.assertEqual(m.health()['schedule_undecodable'], 1)
        # no pool fields at all: no mark
        with self.assertRaises(M.MarkError):
            m.value(1, 2, 3, now=0, fee_lamports=0)

    def test_creator_fee_override_and_a_coin_without_creator(self):
        m = model()
        pool = {'virtual_quote_reserves_raw': 18 * 10 ** 9, 'protocol_fees_raw': 0, 'creator_fees_raw': 0, 'coin_creator': 'C' * 32,
                'creator_fee_bps': 0}
        args = (10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9)
        _, plain = m.value(*args, pool=pool, pool_info={'supply_raw': 10 ** 15}, now=1, fee_lamports=0)
        _, override = m.value(*args, pool=dict(pool, creator_fee_bps=50), pool_info={'supply_raw': 10 ** 15}, now=1, fee_lamports=0)
        _, none = m.value(*args, pool=dict(pool, coin_creator=M.DEFAULT_PUBKEY), pool_info={'supply_raw': 10 ** 15}, now=1, fee_lamports=0)
        self.assertEqual(plain['fees_bps'], {'lp_fee_bps': 20, 'protocol_fee_bps': 5, 'creator_fee_bps': 95})
        self.assertEqual(override['fees_bps']['creator_fee_bps'], 50)
        self.assertEqual((override['fee_bps'], none['fee_bps']), (75, 25))
        net_none, _ = m.value(*args, pool=dict(pool, coin_creator=M.DEFAULT_PUBKEY), pool_info={'supply_raw': 10 ** 15}, now=1,
                              fee_lamports=0)
        gross = 98 * 10 ** 9 * 10 ** 9 // (2 * 10 ** 14 + 10 ** 9)
        self.assertEqual(net_none, gross - (gross * 20 + 9999) // 10000 - (gross * 5 + 9999) // 10000)

    def test_the_vault_model_reads_two_accounts_and_effective_three(self):
        self.assertEqual(M.MarkModel(None).position_keys('b', 'q', 'p'), ['b', 'q'])
        self.assertEqual(model().position_keys('b', 'q', 'p'), ['b', 'q', 'p'])
        self.assertEqual(model({'model': 'effective', 'fee_source': 'config'}).call_keys(), [])

    def test_buy_output_is_the_programs_buy(self):
        """The recorder's reference quantity: what ref_size buys (fees off the input), as the fake world's program prices it."""
        token = boosted()

        class Sched:
            fee_schedule = schedule()
        token.world = Sched
        from tests.lean.fakeworld import SOL
        base, quote = token.reserves(T0)
        out, _fee = token.quote(T0, SOL, 2 * 10 ** 8)
        self.assertEqual(M.buy_output(2 * 10 ** 8, base, quote + token.virtual_lamports, 120), out)
        self.assertLess(out, base * 2 * 10 ** 8 // (quote + token.virtual_lamports + 2 * 10 ** 8))

    def test_accrued_fees_are_recorded_not_subtracted(self):
        m = model()
        pool = {'virtual_quote_reserves_raw': 18 * 10 ** 9, 'protocol_fees_raw': 10 ** 9, 'creator_fees_raw': 2 * 10 ** 9,
                'coin_creator': 'C' * 32, 'creator_fee_bps': 0}
        _, info = m.value(10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9, pool=pool, pool_info={'supply_raw': 10 ** 15}, now=1, fee_lamports=0)
        self.assertEqual((info['quote_effective_raw'], info['accrued_fees_raw']), (98 * 10 ** 9, 3 * 10 ** 9))

    def test_config_is_strict(self):
        self.assertEqual(M.config(None)['model'], 'vault')
        for bad in ({'model': 'magic'}, {'fee_source': 'guess'}, {'typo': 1}, {'schedule_ttl_s': 0}, []):
            with self.assertRaises(M.MarkConfigError):
                M.config(bad)
        self.assertEqual(M.fee_config_address(), json.loads((FIX / 'mainnet-dynamic-fees.json').read_text())['address'])


# ============================================================================================================ e2e helpers
class World1(unittest.TestCase):
    TICK = 10

    def build(self, *tokens, cfg_over=None, fee_account=True, base='lean.baseline.json', ready=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        self.tokens = list(tokens)
        self.world = World(self.root, self.tokens, self.clock)
        address, account, fields = mainnet_fee_schedule()
        self.world.fee_schedule = fields
        if fee_account:
            self.world.fee_account = {address: account}
        self.world.add_frames(ready or [T0] * len(tokens))
        self.cfg = load_config(ROOT / 'tests' / 'lean' / base)
        for key, value in (cfg_over or {}).items():
            self.cfg[key] = value
        return self.runner()

    def runner(self):
        return build_runner(self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS,
                            code_version='m1-test', clock=self.clock.time,
                            transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time,
                                              'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def run_until(self, r, end, *, tick=None, check=None, candidates=True):
        tick_s = tick or self.TICK
        t = self.clock.time()
        while t <= T0 + end:
            if self.clock.time() < t:
                self.clock.t = t
            r.position_pass()
            if candidates:
                r.candidate_pass()
            self.assertIsNone(r.halted, r.halted)
            if check:
                check(r, t - T0)
            t += tick_s
        return r

    def decisions(self, r, mint, kind='exit'):
        return r.store.rows('decisions', mint=mint, kind=kind, limit=100000)

    def closed(self, r, mint):
        return [c for c in r.store.closed_positions() if c['mint'] == mint]


EFFECTIVE = {'marks': {'model': 'effective', 'fee_source': 'dynamic', 'schedule_ttl_s': 3600.0}, 'pool_fee_bps': 120}


def boosted(tag=31, **kw):
    return Token(tag, virtual_sol=18, coin_creator_tag=9, accrued_fees_lamports=5 * 10 ** 8, **kw)


# ============================================================================================================ #7 e2e
class EffectiveMarksE2E(World1):
    def first_ratio(self, r, mint):
        position = r.store.positions()[mint]
        held = A.strategy_position(position, r.store.position_states()[mint]['state'])
        return r.marks[mint][0] / held.cost_left

    def test_a_fresh_position_marks_about_one_and_holds(self):
        token = boosted()
        r = self.build(token, cfg_over=EFFECTIVE)
        self.run_until(r, 0)
        self.assertIn(token.mint, r.store.positions())
        self.clock.t = T0 + 10
        r.position_pass()
        ratio = self.first_ratio(r, token.mint)
        self.assertTrue(Decimal('0.95') < ratio < Decimal('1'), ratio)             # round-trip fees only
        info = r.mark_info[token.mint]
        self.assertEqual((info['model'], info['fee_source'], info['pool_source'], info['fee_bps']), ('effective', 'dynamic', 'live', 120))
        self.assertEqual(info['virtual_raw'], token.virtual_lamports)
        self.assertEqual(info['accrued_fees_raw'], token.accrued_fees)
        self.assertEqual(self.closed(r, token.mint), [])
        # the open state keeps the screen's pool fields; the marks observation keeps every input (reproducible without the body)
        pool_info = r.store.position_states()[token.mint]['state']['pool_info']
        self.assertEqual((pool_info['virtual_quote_reserves_raw'], pool_info['supply_raw']), (token.virtual_lamports, 10 ** 15))
        obs = r.store.rows('observations', kind='marks', limit=10)[-1]
        meta = json.loads(obs['meta'])
        self.assertEqual((meta['layout'], meta['model'], meta['call_keys']), ('base,quote,pool', 'effective', 1))
        stored = meta['marks'][token.mint]
        position = r.store.positions()[token.mint]
        fees = {'lp_fee_bps': 20, 'protocol_fee_bps': 5, 'creator_fee_bps': 95}
        again = M.sell_output(position.qty_raw, stored['base_raw'], stored['quote_raw'] + stored['virtual_raw'], fees) - r.pcfg.fee_lamports
        self.assertEqual(again, stored['net_lamports'])
        self.assertEqual(A.sol(stored['net_lamports']), r.marks[token.mint][0])

    def test_the_vault_mark_stops_the_same_fresh_position_at_once(self):
        """The bug: with today's marks the boosted pool reads ~0.80 of cost at the first mark, under the 0.82 stop."""
        token = boosted()
        r = self.build(token)
        self.run_until(r, 0)
        self.clock.t = T0 + 10
        r.position_pass()
        (closed,) = self.closed(r, token.mint)
        self.assertEqual(closed['state']['reason'], 'STOP')
        ratio = Decimal(json.loads(self.decisions(r, token.mint)[-1]['features'])['ratio'])
        self.assertLess(ratio, Decimal('0.85'))
        # the price did not move: the sale got ~0.97 of cost, the stop fired on a mark ~17 % too low
        sell = r.store.rows('fills', mint=token.mint, limit=10)[-1]
        buy = r.store.rows('fills', mint=token.mint, limit=10)[0]
        self.assertGreater(sell['sol_lamports'] / (buy['sol_lamports'] + buy['fee_lamports']), 0.95)

    def test_stop_take_profit_and_time_stop_fire_at_the_intended_ratios(self):
        """The mark ratio that triggers an exit is the ratio the sale gets (within the per-tick move), for all three rules."""
        cases = {'stop': (steps((300, 0.80)), 'STOP'), 'tp': (steps((300, 1.55)), 'TAKE_PROFIT'), 'time': (None, 'TIME_STOP')}
        for name, (path, reason) in cases.items():
            with self.subTest(name):
                token = boosted(path=path)
                r = self.build(token, cfg_over=EFFECTIVE)
                self.run_until(r, 0)
                self.run_until(r, 2800, tick=20, candidates=False)
                exits = [d for d in self.decisions(r, token.mint) if d['action'] == 'SELL']
                self.assertTrue(exits, name)
                first = exits[0]
                self.assertEqual(json.loads(first['reasons']), [reason])
                ratio = Decimal(json.loads(first['features'])['ratio'])
                fills = r.store.rows('fills', mint=token.mint, limit=10)
                buy, sell = fills[0], fills[1]
                sold = Decimal(sell['qty_raw']) / Decimal(buy['qty_raw'])
                got = Decimal(sell['sol_lamports'] - sell['fee_lamports']) / (Decimal(buy['sol_lamports'] + buy['fee_lamports']) * sold)
                # the fill carries the 50 bps paper slippage the mark leaves out
                self.assertLess(abs(got / ratio - Decimal('0.995')), Decimal('0.006'), (name, got, ratio))
                if reason == 'STOP':
                    self.assertTrue(Decimal('0.75') < ratio <= Decimal('0.82'), ratio)
                if reason == 'TAKE_PROFIT':
                    self.assertTrue(ratio >= Decimal('1.4'), ratio)
                if reason == 'TIME_STOP':
                    self.assertGreaterEqual(int(sell['ts']) - int(buy['ts']), 2700)
                    self.assertTrue(Decimal('0.95') < ratio < Decimal('1'), ratio)

    def test_fee_fallback_without_a_schedule_account(self):
        token = boosted()
        r = self.build(token, cfg_over=EFFECTIVE, fee_account=False)
        self.run_until(r, 10)
        info = r.mark_info[token.mint]
        self.assertEqual((info['fee_source'], info['fee_fallback'], info['fee_bps']), ('config', True, 120))
        health = r.health()['marks_model']
        self.assertGreater(health['fee_config'], 0)
        self.assertEqual(health['fee_dynamic'], 0)

    def test_a_missing_pool_account_falls_back_to_the_screen_fields(self):
        token = boosted()
        r = self.build(token, cfg_over=EFFECTIVE)
        self.run_until(r, 0)
        real = token.accounts

        def without_pool(t):
            out = real(t)
            out.pop(token.pool, None)
            return out
        token.accounts = without_pool
        self.clock.t = T0 + 10
        r.position_pass()
        self.assertEqual(r.mark_info[token.mint]['pool_source'], 'screen')
        self.assertEqual(r.mark_info[token.mint]['virtual_raw'], token.virtual_lamports)

    def test_paths_record_effective_marks_and_labels_reproduce_them(self):
        token = boosted(path=steps((120, 1.1), (240, 0.9)))
        r = self.build(token, cfg_over=EFFECTIVE)
        r.l07r_start_paths()
        for i in range(30):
            self.clock.t = T0 + 15 * i
            r.position_pass()
            r.candidate_pass()
            r.paths.poll()
        db = sqlite3.connect(r.paths.db.path)
        rows = db.execute('SELECT ts, base_raw, quote_raw, net_sol, model, virtual_raw, accrued_fees_raw, fee_bps, fee_source '
                          'FROM path_marks WHERE mint=? AND status="OK"', (token.mint,)).fetchall()
        self.assertGreater(len(rows), 10)
        self.assertEqual({(r_[4], r_[5], r_[7], r_[8]) for r_ in rows}, {('effective', token.virtual_lamports, 120, 'dynamic')})
        from lean import labels as LB
        paths, _ = LB.load_paths([db])
        (path,) = [p for p in paths if p.mint == token.mint]
        self.assertTrue(all(p.effective for p in path.points))
        ref = LB.reference(path, r.pcfg)
        for point, row in zip(path.points, rows):
            self.assertLessEqual(abs(LB.mark_lamports(point, ref.qty_raw, 25, r.pcfg) - int(Decimal(row[3]) * 10 ** 9)), 2)
        target = json.loads(db.execute('SELECT target FROM paths WHERE mint=?', (token.mint,)).fetchone()[0])
        self.assertEqual(target['pool_info']['virtual_quote_reserves_raw'], token.virtual_lamports)

    def test_an_older_paths_file_gets_the_new_columns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'paths.sqlite'
        db = sqlite3.connect(path)
        from lean import paths as P
        for sql in P.SCHEMA:
            if 'path_marks(' in sql and 'CREATE TABLE' in sql:
                sql = ('CREATE TABLE IF NOT EXISTS path_marks(mint TEXT NOT NULL, ts REAL NOT NULL, slot INTEGER, base_raw INTEGER, '
                       'quote_raw INTEGER, net_sol TEXT, liquidity_sol TEXT, status TEXT NOT NULL)')
            db.execute(sql)
        db.execute("INSERT INTO path_marks VALUES('m', 1.0, 1, 2, 3, '0.1', '0.2', 'OK')")
        db.commit()
        db.close()
        store = P.PathStore(path)
        cols = [r[1] for r in store.db.execute('PRAGMA table_info(path_marks)')]
        self.assertEqual(cols[-5:], ['model', 'virtual_raw', 'accrued_fees_raw', 'fee_bps', 'fee_source'])
        self.assertEqual(store.db.execute('SELECT model, net_sol FROM path_marks').fetchall(), [(None, '0.1')])
        store.close()
        P.PathStore(path).close()                                                 # idempotent

    def test_production_config_keeps_the_vault_marks(self):
        token = boosted()
        r = self.build(token, base='lean.r1-prod.json')
        self.assertEqual((r.mark_model.model, r.store.raw_policy, r.pool_fee_bps), ('vault', 'all', 25))
        self.run_until(r, 0)
        r.position_pass()
        meta = json.loads(r.store.rows('observations', kind='marks', limit=10)[-1]['meta'])
        self.assertEqual(meta['layout'], 'base,quote')


class ProductionConfig(unittest.TestCase):
    def test_the_live_config_loads_unchanged_and_the_new_keys_default_off(self):
        cfg = load_config(ROOT / 'tests' / 'lean' / 'lean.r1-prod.json')
        self.assertEqual(cfg['marks'], M.config(None))
        self.assertEqual((cfg['raw_policy'], cfg['pool_fee_bps'], cfg['initial_cash_sol']), ('all', 25, '100'))
        self.assertEqual((cfg['screen']['min_age_seconds'], cfg['wallet_signals']['window_s'], cfg['ops']['credit_budget_month']),
                         (60, 60, 9000000))
        self.assertEqual(cfg['sell_evidence']['unverified_sell_policy'], 'flag_only')
        self.assertEqual((cfg['lanes']['helius_rps'], cfg['bot_exit']['max_calls_per_pass']), (20, 18))
        for key in ('blocked_log_s', 'exit_backoff_max_s', 'peak_persist_s', 'discovery_stale_s'):
            self.assertEqual(cfg[key], CONFIG_KEYS[key])
        # on the VPS strategy_config is an absolute path
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        raw = json.loads((ROOT / 'tests' / 'lean' / 'lean.r1-prod.json').read_text())
        path = Path(tmp.name) / 'lean.json'
        path.write_text(json.dumps({**raw, 'strategy_config': str(ROOT / 'config' / 'lean' / 'strategy-default.json')}))
        self.assertEqual(Path(load_config(path)['strategy_config']).resolve(), Path(cfg['strategy_config']).resolve())

    def test_the_example_lists_every_new_key(self):
        raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        for key in ('marks', 'raw_policy', 'blocked_log_s', 'exit_backoff_max_s', 'peak_persist_s', 'discovery_stale_s'):
            self.assertIn(key, raw)
            self.assertIn(key, CONFIG_KEYS)
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.assertEqual((cfg['marks']['model'], cfg['raw_policy'], cfg['pool_fee_bps']), ('effective', 'errors_only', 120))

    def test_bad_values_are_refused(self):
        raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for key, value in (('raw_policy', 'some'), ('blocked_log_s', -1), ('marks', {'model': 'x'}), ('discovery_stale_s', 10),
                           ('peak_persist_s', True)):
            path = Path(tmp.name) / 'lean.json'
            path.write_text(json.dumps({**raw, key: value, 'strategy_config': str(ROOT / 'config' / 'lean' / 'strategy-default.json')}))
            with self.assertRaises(ConfigError, msg=key):
                load_config(path)


# ============================================================================================================ #10
class OutagesAndHashes(World1):
    def test_a_restart_writes_the_outage_and_its_duration(self):
        token = boosted()
        r = self.build(token)
        self.assertEqual(r.store.rows('events', kind='outage'), [])               # a new store: nothing was down
        self.run_until(r, 30)
        r.write_health()
        last_health = json.loads((self.state / 'health.json').read_text())['at']
        r.store.close()
        self.clock.t += 3600
        r2 = self.runner()
        (row,) = r2.store.rows('events', kind='outage')
        outage = json.loads(row['payload'])
        self.assertEqual(outage['previous_health_at'], last_health)
        self.assertAlmostEqual(outage['duration_s'], self.clock.t - outage['down_since'], places=3)
        self.assertGreaterEqual(outage['duration_s'], 3600)
        self.assertEqual(r2.health()['last_outage'], outage)

    def test_discovery_silence_and_recovery_are_events(self):
        token = boosted()
        r = self.build(token, cfg_over={'discovery_stale_s': 600})
        self.run_until(r, 1200, tick=60)                                        # the only frame was received at T0 - 300
        stale = r.store.rows('events', kind='discovery_stale')
        self.assertEqual(len(stale), 1)
        self.assertGreater(json.loads(stale[0]['payload'])['silent_s'], 600)
        self.assertTrue(r.health()['discovery_feed']['stale'])
        with sqlite3.connect(self.world.discovery_db) as c:
            c.execute('INSERT INTO raw_events(seq,source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?,?)',
                      (99, 'confirmed:99', self.clock.t, 1, '{}', 'x'))
        self.run_until(r, 1300, tick=60)
        (rec,) = r.store.rows('events', kind='discovery_recovered')
        self.assertGreater(json.loads(rec['payload'])['duration_s'], 600)
        self.assertEqual(len(r.store.rows('events', kind='discovery_stale')), 1)       # transitions only

    def test_the_start_event_and_health_carry_both_hashes_and_a_comment_is_not_a_new_run(self):
        token = boosted()
        r = self.build(token)
        start = json.loads(r.store.rows('events', kind='start')[-1]['payload'])
        self.assertEqual(len(start['lean_config_hash']), 64)
        self.assertEqual(start['strategy_config_hash'], r.cfg.config_hash)
        self.assertEqual(r.health()['lean_config_hash'], start['lean_config_hash'])
        self.assertEqual(lean_config_hash(self.cfg), start['lean_config_hash'])
        self.assertEqual(lean_config_hash({**self.cfg, '_comment': 'edited'}), start['lean_config_hash'])
        self.assertNotEqual(lean_config_hash({**self.cfg, 'stale_mark_s': 31}), start['lean_config_hash'])
        self.assertNotEqual(lean_config_hash({**self.cfg, 'lanes': {**self.cfg['lanes'], 'low_shed_s': 31}}), start['lean_config_hash'])

    def test_report_and_learner_filter_by_the_hash(self):
        a, b = boosted(41), boosted(42)
        r = self.build(a, b, ready=[T0, T0 + 4000])
        r.l07r_start_paths()
        self.run_until(r, 3900, tick=60)                                        # run 1: trades a (b is not old enough)
        r.paths.poll()
        hash1 = json.loads(r.store.rows('events', kind='start')[-1]['payload'])['lean_config_hash']
        r.store.close()
        self.cfg['stale_mark_s'] = 31                                           # a different lean.json: a new run
        r = self.runner()
        r.l07r_start_paths()
        self.run_until(r, 8000, tick=60)
        r.paths.poll()
        hash2 = json.loads(r.store.rows('events', kind='start')[-1]['payload'])['lean_config_hash']
        self.assertNotEqual(hash1, hash2)
        db = self.state / 'lean.sqlite'
        everything = report.build(db, now=T0 + 9000)
        one = report.build(db, now=T0 + 9000, config_hash=hash1[:12])
        two = report.build(db, now=T0 + 9000, config_hash=hash2)
        mints = lambda s: {row['mint'] for row in s['trades']['data']['rows']}         # noqa: E731
        self.assertEqual(mints(everything), {a.mint, b.mint})
        self.assertEqual((mints(one), mints(two)), ({a.mint}, {b.mint}))
        self.assertEqual(one['config_hash_filter']['runs'], 1)
        with self.assertRaises(report.ReportError):
            report.build(db, now=T0, config_hash='abc')
        from lean import learn, strategy as S
        strategy = S.StrategyConfig.load(self.cfg['strategy_config'])
        paths_db = self.state / 'paths.sqlite'
        none = learn.build_dataset(db, paths_db, strategy, config_hash='f' * 16)
        self.assertEqual(none.stats['skipped'].get('OTHER_CONFIG_HASH'), none.stats['paths'])
        first = learn.build_dataset(db, paths_db, strategy, config_hash=hash1)
        self.assertGreaterEqual(first.stats['skipped'].get('OTHER_CONFIG_HASH', 0), 1)
        self.assertLess(first.stats['skipped'].get('OTHER_CONFIG_HASH', 0), first.stats['paths'])
        self.assertEqual(first.stats['config_hash_filter'], hash1)


# ============================================================================================================ #12
class RawPolicy(World1):
    def test_errors_only_keeps_failures_and_downstream_inputs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / 'lean.sqlite', initial_cash_sol='1', code_version='c', strategy_version='s', raw_policy='errors_only')
        body = b'{"outAmount":"1"}' * 10
        ids = {kind: store.add_observation(kind, body, meta={'k': 1}) for kind in
               ('quote:exit', 'marks', 'sol_usd', 'screen:accounts_vaults_lp', 'risk_external:rugcheck',
                'error:NO_ROUTE', 'screen:accounts_mint_pool', 'screen:holders', 'features', 'wallet_signals')}
        dropped = {'quote:exit', 'marks', 'sol_usd', 'screen:accounts_vaults_lp', 'risk_external:rugcheck'}
        self.assertEqual({k for k in ids if raw_droppable(k)}, dropped)
        for kind, oid in ids.items():
            got = store.observation(oid)                                          # still verifies its hashes
            self.assertEqual(got['full_bytes'], len(body))
            if kind in dropped:
                self.assertEqual((got['raw'], got['raw_truncated'], got['meta']['raw_omitted']), (b'', True, 'errors_only'), kind)
                import hashlib
                self.assertEqual(got['full_sha256'], hashlib.sha256(body).hexdigest())
            else:
                self.assertEqual((got['raw'], got['raw_truncated']), (body, False), kind)
        store.close()
        # the same file opens under the other policy (no schema change, no migration)
        Store(Path(tmp.name) / 'lean.sqlite', code_version='c', strategy_version='s').close()
        with self.assertRaises(Exception):
            Store(Path(tmp.name) / 'x.sqlite', initial_cash_sol='1', raw_policy='some')

    def scenario(self, policy):
        token = boosted(path=steps((300, 0.7)))
        r = self.build(token, cfg_over={**EFFECTIVE, 'raw_policy': policy, 'route_check': {'enabled': True}})
        self.run_until(r, 600)
        return r, token

    def test_errors_only_end_to_end(self):
        full, token = self.scenario('all')
        slim, token2 = self.scenario('errors_only')
        size = lambda r: r.store.db.execute('SELECT SUM(length(raw)) FROM observations').fetchone()[0]     # noqa: E731
        self.assertLess(size(slim), size(full) / 3)
        # the same trading
        trade = lambda r, t: [(f['side'], f['qty_raw'], f['sol_lamports']) for f in r.store.rows('fills', mint=t.mint, limit=10)]  # noqa: E731
        self.assertEqual(trade(full, token), trade(slim, token2))
        # route_check read the bodies from memory: real verdicts, never RAW_NOT_RETAINED
        actions = sorted(d['action'] for d in slim.store.rows('decisions', kind='route_check', limit=100))
        self.assertEqual(actions, sorted(d['action'] for d in full.store.rows('decisions', kind='route_check', limit=100)))
        self.assertIn('ROUTABLE', actions)
        # a fill quote's parsed amounts are in its meta
        sell = slim.store.rows('fills', mint=token2.mint, limit=10)[-1]
        meta = json.loads(slim.store.rows('observations', id=int(sell['quote_ref']))[0]['meta'])
        self.assertEqual(meta['raw_omitted'], 'errors_only')
        self.assertEqual(sell['sol_lamports'], meta['out_amount'] * (10000 - 50) // 10000)     # the paper fill = quote less slippage

    def test_route_check_without_the_body_is_inconclusive(self):
        from lean.route_check import RouteChecker
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / 'lean.sqlite', initial_cash_sol='1', code_version='c', strategy_version='s', raw_policy='errors_only')
        ref = store.add_observation('quote:exit', b'{"routePlan":[]}')
        checker = RouteChecker(store, {'enabled': True})
        checker.on_fill('exit', 'mint', ref, trade=1)
        (row,) = store.rows('decisions', kind='route_check')
        self.assertEqual((row['action'], json.loads(row['reasons'])), ('INCONCLUSIVE', ['RAW_NOT_RETAINED']))

    def test_notify_reads_the_marks_from_the_meta(self):
        token = boosted()
        r = self.build(token, cfg_over={**EFFECTIVE, 'raw_policy': 'errors_only'})
        self.run_until(r, 20)
        from lean.notify import StoreView
        view = StoreView(r.store.db)
        positions, states = r.store.positions(), r.store.position_states()
        got = view.latest_marks(positions, states, pool_fee_bps=25)
        self.assertEqual(got[token.mint][0], int(r.marks[token.mint][0] * 10 ** 9))


# ============================================================================================================ #13
class RowSpam(World1):
    def stuck(self, **over):
        """A pool that collapses to dust: the stop fires, every exit quote is worth less than the fee (STUCK_POSITION)."""
        token = Token(51, path=steps((60, 0.000001)))
        r = self.build(token, cfg_over={'unexitable_after_s': 7200, **over})
        self.run_until(r, 0)
        self.run_until(r, 7800, candidates=False)
        return r, token

    def test_a_stuck_position_logs_and_quotes_sparsely_and_is_still_written_off_on_time(self):
        old, t_old = self.stuck(blocked_log_s=0, exit_backoff_max_s=0)
        new, t_new = self.stuck()
        blocked = lambda r, t: [d for d in r.store.rows('decisions', mint=t.mint, kind='exit', limit=100000) if d['action'] == 'BLOCKED']  # noqa: E731
        quotes = lambda r, t: len(r.store.rows('observations', mint=t.mint, kind='quote:exit', limit=100000))                 # noqa: E731
        self.assertGreater(len(blocked(old, t_old)), 600)                     # one per 10 s pass for 2 h: the spam
        self.assertGreater(quotes(old, t_old), 600)
        self.assertLessEqual(len(blocked(new, t_new)), 7200 / 300 + 3)
        self.assertLessEqual(quotes(new, t_new), 7200 / 60 + 8)
        rows = blocked(new, t_new)
        self.assertEqual(json.loads(rows[0]['reasons'])[0], 'STUCK_POSITION')
        self.assertGreater(sum(json.loads(r_['features'])['suppressed_since_last'] for r_ in rows), 0)
        # D2: the same write-off, at the same time (the failure clock is persisted, not the quote cadence)
        (c_old,), (c_new,) = self.closed(old, t_old.mint), self.closed(new, t_new.mint)
        self.assertEqual((c_old['state']['reason'], c_new['state']['reason']), ('UNEXITABLE', 'UNEXITABLE'))
        self.assertEqual(c_old['state']['exit_failing_since'], c_new['state']['exit_failing_since'])
        self.assertLessEqual(abs(c_old['ts'] - c_new['ts']), 10)
        self.assertGreater(new.health()['log_throttle']['exit_backoff_skips'], 0)

    def test_the_two_phase_exits_of_the_live_config_back_off_too(self):
        """L10 execution on (as in production): phase 1 / phase 2 of a stuck exit, the same sparse rows and the same write-off."""
        execution = {'exec_delay_s': 3, 'priority_fee_sol': '0.0001', 'jito_tip_sol': '0.0001', 'ata_rent_sol': '0.00203928'}
        old, t_old = self.stuck(execution=execution, blocked_log_s=0, exit_backoff_max_s=0)
        new, t_new = self.stuck(execution=execution)
        count = lambda r, t, kind: len(r.store.rows('observations', mint=t.mint, kind=kind, limit=100000))      # noqa: E731
        self.assertGreater(count(old, t_old, 'quote:exit'), 1200)                     # q0 + q1 every 10 s pass
        self.assertLessEqual(count(new, t_new, 'quote:exit'), 2 * (7200 / 60 + 8))      # q0 + q1 per backed-off attempt
        blocked = [d for d in new.store.rows('decisions', mint=t_new.mint, kind='exit', limit=100000) if d['action'] == 'BLOCKED']
        self.assertLessEqual(len(blocked), 7200 / 300 + 3)
        (c_old,), (c_new,) = self.closed(old, t_old.mint), self.closed(new, t_new.mint)
        self.assertEqual((c_old['state']['reason'], c_new['state']['reason']), ('UNEXITABLE', 'UNEXITABLE'))
        self.assertLessEqual(abs(c_old['ts'] - c_new['ts']), 10)

    def test_the_write_off_is_due_on_the_first_pass_whatever_the_back_off_phase(self):
        for after in (7200, 7210, 7230, 7250):
            with self.subTest(after):
                r, token = self.stuck(unexitable_after_s=after)
                (closed,) = self.closed(r, token.mint)
                due = int(closed['state']['exit_failing_since']) + after
                self.assertTrue(0 <= closed['ts'] - due < 10, (closed['ts'], due))

    def test_the_backoff_is_exponential_and_capped(self):
        new, token = self.stuck()
        times = [o['ts'] for o in new.store.rows('observations', mint=token.mint, kind='quote:exit', limit=100000)]
        gaps = [round(b - a) for a, b in zip(times, times[1:])]
        self.assertEqual(gaps[:4], [10, 20, 40, 60])
        self.assertLessEqual(max(gaps), 60 + 10)

    def test_a_route_that_comes_back_resets_the_backoff_and_exits(self):
        token = Token(52, path=steps((60, 0.5)), no_route_from=100, route_back_at=400)
        r = self.build(token)
        self.run_until(r, 0)
        self.run_until(r, 600, candidates=False)
        (closed,) = self.closed(r, token.mint)
        self.assertEqual(closed['state']['reason'], 'STOP')
        sell = r.store.rows('fills', mint=token.mint, limit=10)[-1]
        self.assertLessEqual(sell['ts'] - (T0 + 400), 60 + 10)                # at most one capped back-off late
        self.assertEqual(r._exit_backoff, {})

    def test_a_held_exit_resets_the_back_off(self):
        """No route while the stop is wanted (back-off grows), the price recovers for one pass (HOLD: reset), drops again with the
        route back: the exit is quoted at once, not after the old back-off."""
        token = Token(56, path=steps((60, 0.5), (255, 1.0), (265, 0.5)), no_route_from=50, route_back_at=265)
        r = self.build(token)
        self.run_until(r, 0)
        self.run_until(r, 400, candidates=False)
        (closed,) = self.closed(r, token.mint)
        sell = r.store.rows('fills', mint=token.mint, limit=10)[-1]
        self.assertEqual(sell['ts'], T0 + 270)

    def test_the_in_memory_peak_survives_a_dip_inside_the_window(self):
        path = steps((15, 1.10), (25, 1.105), (35, 1.102))
        peaks = {}
        for name, persist, tag in (('old', 0, 57), ('new', 60, 58)):
            token = Token(tag, path=path)
            r = self.build(token, cfg_over={'peak_persist_s': persist})
            self.run_until(r, 0)
            self.run_until(r, 50, candidates=False)
            r.flush_pending_states()
            peaks[name] = r.store.position_states()[token.mint]['state']['peak_ratio']
        self.assertEqual(peaks['new'], peaks['old'])

    def test_peak_only_updates_are_throttled_but_trading_is_identical(self):
        def climb(age):
            return Decimal(1) + Decimal(int(age) // 10) / 2000                 # a slow, steady rise: a new peak every pass
        runs = {}
        for name, persist in (('old', 0), ('new', 60)):
            token = Token(53 if name == 'old' else 54, path=climb)
            r = self.build(token, cfg_over={'peak_persist_s': persist})
            self.run_until(r, 0)
            self.run_until(r, 1800, candidates=False)
            updates = [s for s in r.store.rows('position_state', mint=token.mint, limit=100000) if s['event'] == 'update']
            runs[name] = (r, token, updates)
        old, new = runs['old'][2], runs['new'][2]
        self.assertGreater(len(old), 120)
        self.assertLess(len(new), len(old) / 3)
        # in memory the peak is current; on stop it is flushed
        r, token, _ = runs['new']
        live_peak = Decimal(r._pending_state[token.mint][1]['peak_ratio']) if token.mint in r._pending_state else None
        r.flush_pending_states()
        persisted = Decimal(r.store.position_states()[token.mint]['state']['peak_ratio'])
        if live_peak is not None:
            self.assertEqual(persisted, live_peak)
        old_peak = Decimal(runs['old'][0].store.position_states()[runs['old'][1].mint]['state']['peak_ratio'])
        self.assertEqual(persisted, old_peak)

    def test_a_restart_loses_at_most_the_throttle_window(self):
        token = Token(55, path=lambda age: Decimal(1) + Decimal(int(age) // 10) / 2000)
        r = self.build(token)
        self.run_until(r, 0)
        self.run_until(r, 1200, candidates=False)
        live = Decimal((r._pending_state.get(token.mint) or (None, r.store.position_states()[token.mint]['state']))[1]['peak_ratio'])
        stored = Decimal(r.store.position_states()[token.mint]['state']['peak_ratio'])
        self.assertLessEqual(stored, live)
        self.assertGreaterEqual(stored * Decimal('1.01'), live)                # never more than 1 % behind
        r.store.close()                                                          # a crash: nothing flushed
        r2 = self.runner()
        self.assertEqual(Decimal(r2.store.position_states()[token.mint]['state']['peak_ratio']), stored)


if __name__ == '__main__':
    unittest.main()
