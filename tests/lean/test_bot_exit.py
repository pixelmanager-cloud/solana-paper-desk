"""SYNTHETIC_TEST_ONLY: L23 bot-exit detection (shadow first). Real lean modules end to end; only HTTP is faked (tests/lean/fakeworld.py)
plus a synthetic pool transaction stream (``Flow``) behind ``getSignaturesForAddress`` / ``getTransaction``. No network, no keys.

Scenarios (ages in seconds after T0, the entry):
  bot_pump   a bot trades the pool at 5 tx/s (70 % buys) and the price climbs a staircase; at 900 the bot stops (0.5 tx/s, 90 % sells,
             price sags); at 1000 the pool gap-dumps to 0.2 and the stop can only fill after the gap.
  noise      a normal pool: 2 tx/s, 50/50 buys and sells of small sizes by many distinct wallets.
  quiet      0.2 tx/s (no "bot rate" to lose) with one creator sell / one whale sell.
"""
import bisect
import hashlib
import json
import os
import random
import socket
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from desk.security import base58
from lean import bot_exit as B, providers, report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, SOL, T0, Token, World
from tests.lean.test_e2e_real import KEYS, ROOT, steps

EXAMPLE = ROOT / 'config' / 'lean' / 'lean.example.json'
BASELINE = ROOT / 'tests' / 'lean' / 'lean.baseline.json'
LAMPORTS = 10 ** 9
TICK = 10


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def wallet(i):
    return base58(hashlib.sha256(b'wallet-%d' % i).digest())


BOTS = [wallet(900 + i) for i in range(3)]


# ------------------------------------------------------------------------------------------------------- the synthetic pool stream
class Flow:
    """Pool transactions of one fake token: ``events`` = [(t, side, sol_lamports, trader)]. Reserves follow the token's price path."""

    def __init__(self, token, events, failed_every=0):
        self.token = token
        self.events = sorted(events, key=lambda e: e[0])
        self.times = [e[0] for e in self.events]
        self.sigs = [base58(hashlib.sha512(b'%d-%d' % (token.tag, i)).digest()) for i in range(len(self.events))]
        self.index = {s: i for i, s in enumerate(self.sigs)}
        self.failed_every = failed_every
        self.requests = []

    def signatures(self, params, t):
        address, opts = params
        if address != self.token.pool:
            return []
        self.requests.append(('sigs', t, dict(opts)))
        hi = bisect.bisect_right(self.times, t)
        lo = self.index[opts['until']] + 1 if opts.get('until') else 0
        out = []
        for i in range(hi - 1, lo - 1, -1):
            if len(out) >= opts['limit']:
                break
            failed = bool(self.failed_every) and i % self.failed_every == 0
            out.append({'signature': self.sigs[i], 'slot': 1000 + i, 'blockTime': int(self.events[i][0]),
                        'err': {'InstructionError': [0, 'x']} if failed else None, 'memo': None, 'confirmationStatus': 'confirmed'})
        return out

    def transaction(self, params, t):
        i = self.index.get(params[0])
        if i is None:
            return None
        self.requests.append(('tx', t, i))
        at, side, sol, trader = self.events[i]
        base, quote = self.token.reserves(at)
        tokens = max(1, sol * base // max(1, quote))
        pool, mint = self.token.pool, self.token.mint
        if side == 'buy':
            pool_pre, pool_post, trader_pre, trader_post = (base, quote), (base - tokens, quote + sol), 0, tokens
        else:
            pool_pre, pool_post, trader_pre, trader_post = (base, quote), (base + tokens, quote - sol), tokens, 0

        def balances(pool_amounts, trader_amount):
            return [{'accountIndex': 1, 'mint': mint, 'owner': pool, 'uiTokenAmount': {'amount': str(pool_amounts[0])}},
                    {'accountIndex': 2, 'mint': SOL, 'owner': pool, 'uiTokenAmount': {'amount': str(pool_amounts[1])}},
                    {'accountIndex': 3, 'mint': mint, 'owner': trader, 'uiTokenAmount': {'amount': str(trader_amount)}}]
        return {'slot': 1000 + i, 'blockTime': int(at),
                'transaction': {'message': {'accountKeys': [{'pubkey': trader, 'signer': True}], 'instructions': []},
                                'signatures': [self.sigs[i]]},
                'meta': {'err': None, 'preTokenBalances': balances(pool_pre, trader_pre),
                         'postTokenBalances': balances(pool_post, trader_post), 'innerInstructions': []}}

    def install(self, world, flows=None):
        flows = flows or [self]
        world.extra_rpc['getSignaturesForAddress'] = lambda p, t: [x for f in flows for x in f.signatures(p, t)]
        world.extra_rpc['getTransaction'] = lambda p, t: next((f.transaction(p, t) for f in flows if p[0] in f.index), None)


def bot_pump_events(stop_at=900, dump_at=1000, end=1600, seed=11):
    rng = random.Random(seed)
    events = []
    k = 0
    t = T0 - 600.0
    while t < T0 + stop_at:                                        # the bot: 5 tx/s, 70 % buys, the rest bot sells
        side = 'buy' if rng.random() < 0.7 else 'sell'
        events.append((t, side, 300_000_000, BOTS[rng.randrange(3)]))
        k += 1
        t += 0.2
    k = 0
    t = T0 + stop_at
    while t < T0 + dump_at:                                        # the bot stopped: 0.5 tx/s, 90 % sells by holders
        events.append((t, 'buy' if k % 10 == 0 else 'sell', 50_000_000 if k % 10 == 0 else 200_000_000, wallet(k)))
        k += 1
        t += 2.0
    while t < T0 + end:                                            # after the gap: a trickle
        events.append((t, 'sell' if k % 2 else 'buy', 20_000_000, wallet(k)))
        k += 1
        t += 3.0
    return events


BOT_PATH = steps((0, 1.0), (300, 1.1), (600, 1.25), (900, 1.15), (1000, 0.2))


def noise_events(rate=2.0, end=1600, seed=7):
    rng = random.Random(seed)
    events, t = [], T0 - 600.0
    while t < T0 + end:
        events.append((t, rng.choice(('buy', 'sell')), rng.randint(50, 500) * 1_000_000, wallet(rng.randint(0, 5000))))
        t += 1.0 / rate * (0.5 + rng.random())
    return events


def quiet_events(extra=(), end=1600):
    events = [(T0 - 600.0 + 10.0 * k, 'buy' if k % 2 else 'sell', 100_000_000, wallet(k)) for k in range(int((end + 600) / 10))]
    return events + list(extra)


# --------------------------------------------------------------------------------------------------------------- the harness
class Scenario(unittest.TestCase):
    maxDiff = None

    def build(self, token, events, *, bot_exit=None, execution=None, failed_every=0):
        """``bot_exit``: None = the key absent (today); a dict = the example's block with these overrides."""
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        self.token = token
        self.world = World(self.root, [token], self.clock)
        self.world.add_frames([T0])
        self.flow = Flow(token, events, failed_every=failed_every)
        self.flow.install(self.world)
        self.cfg = load_config(BASELINE)
        if bot_exit is not None:
            shipped = json.loads(EXAMPLE.read_text())['bot_exit']
            self.cfg['bot_exit'] = {**shipped, **bot_exit}
        if execution is not None:
            self.cfg['execution'] = execution
        return self.runner()

    def runner(self):
        r = build_runner(self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS, code_version='l23-test',
                         clock=self.clock.time,
                         transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                           'sleep': self.clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        return r

    def run_until(self, r, end, *, start=None, check=None):
        """Every TICK: the position pass, the candidate pass, then (the monitor's own thread in production) one bot-exit pass."""
        tick = self.clock.time() if start is None else start
        while tick <= T0 + end:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass()
            r.candidate_pass()
            if r.bot_exit is not None:
                r.bot_exit.poll_pass(r)
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            if check:
                check(r, tick - T0)
            tick += TICK
        return r

    def signals(self, r, kind=B.SHADOW_KIND):
        return r.store.rows('decisions', kind=kind, limit=10000)

    def fills(self, r):
        return [(f['ts'], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in r.store.rows('fills', limit=10000)]

    def closed(self, r):
        return r.store.closed_positions()


# ----------------------------------------------------------------------------------------------------------------- shadow
class Shadow(Scenario):
    def test_a_bot_stop_is_signalled_before_the_gap_dump_and_fills_are_identical_to_today(self):
        today = self.build(Token(31, path=BOT_PATH), bot_pump_events())
        self.assertIsNone(today.bot_exit)
        self.run_until(today, 1300)
        baseline_fills = self.fills(today)
        (closed,) = self.closed(today)
        self.assertEqual(closed['state']['reason'], 'STOP')                       # today: the stop fills after the gap

        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={})
        self.assertEqual(r.bot_exit.cfg.mode, 'shadow')
        self.run_until(r, 1300)
        self.assertEqual(self.fills(r), baseline_fills)                            # IDENTICAL fills: shadow never trades
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'STOP')
        (signal,) = self.signals(r)                                                # one signal per lifecycle
        f = json.loads(signal['features'])
        self.assertIn('BOT_STOPPED', json.loads(signal['reasons']))
        self.assertGreater(signal['ts'], T0 + 900)                                 # after the bot stopped...
        self.assertLess(signal['ts'], T0 + 1000)                                   # ...BEFORE the dump
        self.assertEqual(f['mode'], 'shadow')
        self.assertEqual(f['open_fill_id'], closed['open_fill_id'])
        self.assertLess(Decimal(f['metrics']['tx_rate_ratio']), Decimal('0.3'))
        self.assertGreater(Decimal(f['metrics']['sell_share_30s']), Decimal('0.6'))
        self.assertEqual(f['value_source'], 'jupiter_low')                         # one low-lane Jupiter quote (no D1 mark exists)
        sell = [x for x in self.fills(r) if x[1] == 'sell'][0]
        self.assertGreater(f['value_lamports'], 3 * (sell[3] - sell[4]))           # the signal would have fetched far more
        self.assertEqual(self.signals(r, B.LIVE_KIND), [])
        self.assertEqual(r.store.rows('decisions', kind='exit', limit=100)[-1]['reasons'], '["STOP"]')

    def test_the_report_says_the_signal_would_have_saved_money(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={})
        self.run_until(r, 1300)
        summary = report.build(str(self.state / 'lean.sqlite'))
        d = summary['bot_exit']['data']
        self.assertEqual((d['signals'], d['scored'], d['saved_count'], d['cost_count'], d['open'], d['unvalued']), (1, 1, 1, 0, 0, 0))
        self.assertEqual(d['false_positive_rate'], '0.0000')
        self.assertGreater(Decimal(d['saved_sum_sol']), 0)
        self.assertEqual(Decimal(d['saved_sum_sol']), Decimal(d['shadow_sum_sol']) - Decimal(d['actual_sum_sol']))
        self.assertEqual((d['closed_trades'], d['closed_without_signal'], d['big_losses'], d['big_losses_signalled']), (1, 0, 1, 1))
        self.assertEqual(d['rows'][0]['status'], 'SAVED')
        html = report.render_html(summary)
        self.assertIn('Bot-exit signals', html)

    def test_a_normal_noisy_pool_gives_no_signal(self):
        r = self.build(Token(32, path=steps((0, 1.0), (700, 0.75))), noise_events(), bot_exit={})
        self.run_until(r, 1300)
        self.assertEqual(self.signals(r), [])
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'STOP')
        w = r.bot_exit.health()
        self.assertGreater(w['polls'], 50)
        self.assertGreater(w['swaps_parsed'], 50)                                  # it really looked
        d = report.build(str(self.state / 'lean.sqlite'))['bot_exit']['data']
        self.assertEqual((d['signals'], d['closed_trades'], d['closed_without_signal']), (0, 1, 1))
        self.assertNotIn('Bot-exit signals', report.render_html(report.build(str(self.state / 'lean.sqlite'))))

    def test_a_false_positive_is_counted_when_holding_paid_more(self):
        creator = 77
        sell = (T0 + 503.0, 'sell', 100_000_000, str(_pk(creator)))
        path = steps((0, 1.0), (600, 1.5), (900, 0.95))
        r = self.build(Token(33, path=path, coin_creator_tag=creator), quiet_events([sell]), bot_exit={})
        self.run_until(r, 1300)
        (signal,) = self.signals(r)
        self.assertEqual(json.loads(signal['reasons']), ['CREATOR_SELL'])
        self.assertTrue(T0 + 503 < signal['ts'] <= T0 + 520)
        d = report.build(str(self.state / 'lean.sqlite'))['bot_exit']['data']
        self.assertEqual((d['scored'], d['saved_count'], d['cost_count'], d['false_positive_rate']), (1, 0, 1, '1.0000'))
        self.assertEqual(d['rows'][0]['status'], 'COST')

    def test_a_whale_sell_signals(self):
        sell = (T0 + 401.0, 'sell', 20 * LAMPORTS, wallet(4242))
        r = self.build(Token(34, path=steps((0, 1.0))), quiet_events([sell]), bot_exit={})
        self.run_until(r, 450)
        (signal,) = self.signals(r)
        self.assertEqual(json.loads(signal['reasons']), ['WHALE_SELL'])
        self.assertGreater(Decimal(json.loads(signal['features'])['metrics']['max_sell_reserve_frac']), Decimal('0.15'))
        self.assertIsNone(json.loads(signal['features'])['metrics']['tx_rate_ratio'])   # a quiet pool has no bot rate to lose

    def test_the_shadow_value_is_a_paper_sell_of_the_whole_quantity_at_the_signal(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={})
        self.run_until(r, 990)
        (signal,) = self.signals(r)
        f = json.loads(signal['features'])
        (quote_at,) = [c[2] for c in self.world.calls if c[0] == 'jupiter' and signal['ts'] <= c[2] < signal['ts'] + TICK]
        out, _fee = self.token.quote(quote_at, self.token.mint, f['qty_raw'])
        self.assertEqual(f['qty_raw'], r.store.positions()[self.token.mint].qty_raw)
        self.assertEqual(f['value_lamports'], out * (10000 - r.pcfg.slippage_bps) // 10000 - r.pcfg.fee_lamports)

    def test_pumpers_are_only_the_early_buyers(self):
        r = self.build(Token(32, path=steps((0, 1.0))), noise_events(), bot_exit={})
        sizes = {}

        def check(runner, age):
            watch = next(iter(runner.bot_exit.watches.values()), None)
            if watch is not None:
                sizes[age] = len(watch.pumpers)
        self.run_until(r, 400, check=check)
        self.assertGreater(sizes[130], 3)
        self.assertEqual(sizes[400], sizes[130])                                    # nobody joins after pumper_window_s
        watch = next(iter(r.bot_exit.watches.values()))
        self.assertTrue(watch.pumpers <= {s['trader'] for s in self.flow_swaps(watch.entered_at, watch.entered_at + 120)})

    def flow_swaps(self, lo, hi):
        return [{'trader': e[3]} for e in self.flow.events if lo <= e[0] <= hi + 1 and e[1] == 'buy']

    def test_rules_are_data_driven(self):
        rules = [{'name': 'ANY_SELLS', 'all': [{'metric': 'sell_share_30s', 'op': '>', 'value': '0.2'}]}]
        r = self.build(Token(35, path=steps((0, 1.0))), noise_events(), bot_exit={'rules': rules})
        self.run_until(r, 100)
        (signal,) = self.signals(r)
        self.assertEqual(json.loads(signal['reasons']), ['ANY_SELLS'])


def _pk(tag):
    from solders.pubkey import Pubkey
    return Pubkey.from_bytes(bytes([tag]) * 32)


# ------------------------------------------------------------------------------------------------------------------- live
EXECUTION = {'exec_delay_s': 3, 'priority_fee_sol': '0.0001', 'jito_tip_sol': '0.0001', 'ata_rent_sol': '0.00203928'}


class Live(Scenario):
    def test_live_mode_exits_before_the_gap_at_a_better_price_than_the_stop(self):
        stop_run = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={}, execution=EXECUTION)
        self.run_until(stop_run, 1300)
        stop_sell = [x for x in self.fills(stop_run) if x[1] == 'sell']
        self.assertEqual(self.closed(stop_run)[0]['state']['reason'], 'STOP')

        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'mode': 'live'}, execution=EXECUTION)
        self.run_until(r, 1300)
        (closed,) = self.closed(r)
        self.assertEqual(closed['state']['reason'], 'BOT_EXIT')
        (signal,) = self.signals(r, B.LIVE_KIND)
        self.assertEqual(self.signals(r, B.SHADOW_KIND), [])
        sells = [x for x in self.fills(r) if x[1] == 'sell']
        self.assertEqual(len(sells), 1)
        self.assertLess(sells[0][0], T0 + 1000)                                    # out BEFORE the gap
        self.assertGreater(sells[0][0], signal['ts'])                              # on the position pass after the signal
        self.assertLessEqual(sells[0][0] - signal['ts'], TICK + 5)
        self.assertGreater(sells[0][3], 3 * stop_sell[0][3])                       # far better than the stop after the gap
        exits = [d for d in r.store.rows('decisions', kind='exit', limit=100) if d['action'] == 'SELL']
        self.assertEqual(json.loads(exits[-1]['reasons']), ['BOT_EXIT'])
        state = [json.loads(s['state']) for s in r.store.rows('position_state', limit=100) if s['event'] == 'closed'][0]
        self.assertEqual(state['execution']['fill'], 'q1')                         # through the L10 two-phase path (q0, delay, q1)
        self.assertIsNotNone(state['execution']['q0_ref'])
        self.assertEqual(r.bot_exit.trigger(closed['mint'], closed['open_fill_id']), None)   # closed: forgotten

    def test_a_live_signal_survives_a_restart(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'mode': 'live'})
        open_id = {}

        def check(runner, age):
            if self.signals(runner, B.LIVE_KIND) and not open_id:
                open_id['id'] = runner.store.position_states()[self.token.mint]['open_fill_id']
                raise _Crash()
        with self.assertRaises(_Crash):
            # the process dies right after the signal pass, before any position pass saw it
            self.run_until(r, 1300, check=check)
        r.store.close()
        r2 = self.runner()
        self.assertTrue(r2.bot_exit.trigger(self.token.mint, open_id['id']).startswith('BOT_EXIT:'))
        r2.position_pass()
        (closed,) = self.closed(r2)
        self.assertEqual(closed['state']['reason'], 'BOT_EXIT')
        self.assertLess(closed['ts'], T0 + 1000)
        self.assertEqual(len(self.signals(r2, B.LIVE_KIND)), 1)                    # not signalled twice

    def test_a_shadow_signal_never_becomes_an_exit_after_switching_to_live(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={})
        self.run_until(r, 950)
        (signal,) = self.signals(r)
        key = (self.token.mint, json.loads(signal['features'])['open_fill_id'])
        self.assertEqual(r.bot_exit.health()['live_triggers'], 0)                  # a shadow signal arms nothing
        with r.bot_exit._lock:
            r.bot_exit._live[key] = 'BOT_EXIT:FORGED'
        self.assertIsNone(r.bot_exit.trigger(*key))                                 # and shadow mode never answers a trigger
        r.store.close()
        self.cfg['bot_exit'] = {**self.cfg['bot_exit'], 'mode': 'live'}
        r2 = self.runner()
        self.assertIsNone(r2.bot_exit.trigger(self.token.mint, json.loads(signal['features'])['open_fill_id']))
        self.run_until(r2, 1300, start=self.clock.time() + TICK)
        self.assertEqual(self.closed(r2)[0]['state']['reason'], 'STOP')           # the lifecycle already signalled: not again


class _Crash(Exception):
    pass


# --------------------------------------------------------------------------------------------------- budget, shed, isolation
class Budget(Scenario):
    def test_calls_stay_within_the_pass_budget_on_the_low_lane(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'max_calls_per_pass': 3, 'sample_txs': 2})
        self.assertEqual(r.bot_exit.low.helius.transport.lane, 'low')
        self.assertEqual(r.bot_exit.low.jupiter.transport.lane, 'low')
        per_pass = []
        orig = r.bot_exit.poll_pass

        def counted(runner):
            before = len([c for c in self.world.calls if c[0] == 'helius'])
            made = orig(runner)
            per_pass.append((made, len([c for c in self.world.calls if c[0] == 'helius']) - before))
            return made
        r.bot_exit.poll_pass = counted
        self.run_until(r, 300)
        self.assertTrue(per_pass)
        self.assertTrue(all(made == sent <= 3 for made, sent in per_pass), per_pass)
        self.assertEqual(r.bot_exit.health()['calls'], sum(m for m, _s in per_pass))

    def test_a_short_budget_samples_the_falling_rate_first(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        bot, calm = Token(31, path=BOT_PATH), Token(30, path=steps((0, 1.0)))    # the calm pool sorts FIRST by key
        self.token = bot
        self.world = World(self.root, [bot, calm], self.clock)
        self.world.add_frames([T0, T0 + 70])                                        # one entry per minute
        flows = [Flow(bot, bot_pump_events()), Flow(calm, noise_events(rate=5.0))]
        flows[0].install(self.world, flows)
        self.cfg = load_config(BASELINE)
        self.cfg['bot_exit'] = {**json.loads(EXAMPLE.read_text())['bot_exit'], 'max_calls_per_pass': 4, 'holders_every': 0}
        r = self.runner()
        self.run_until(r, 990)
        self.assertEqual(len(r.store.positions()), 2)
        (signal,) = self.signals(r)
        self.assertEqual((signal['mint'], json.loads(signal['reasons'])), (bot.mint, ['BOT_STOPPED']))
        self.assertLess(signal['ts'], T0 + 1000)

    def test_a_shed_lane_makes_no_call_and_no_delay(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={})
        self.run_until(r, 0)                                                        # entered
        providers.shed_low('helius', 3600, clock=self.clock.monotonic, sleep=self.clock.sleep)
        before, at = len(self.world.calls), self.clock.time()
        self.assertEqual(r.bot_exit.poll_pass(r), 0)
        self.assertEqual((len(self.world.calls), self.clock.time()), (before, at))     # nothing sent, no time spent
        self.assertEqual(r.bot_exit.health()['sheds'], 1)

    def test_a_lane_shed_answer_ends_the_pass_without_a_reserve_too(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'reserve_tokens': 0})
        self.run_until(r, 0)
        providers.shed_low('helius', 3600, clock=self.clock.monotonic, sleep=self.clock.sleep)
        before, at = len(self.world.calls), self.clock.time()
        self.assertEqual(r.bot_exit.poll_pass(r), 0)
        self.assertEqual((len(self.world.calls), self.clock.time()), (before, at))
        self.assertEqual((r.bot_exit.health()['sheds'], r.bot_exit.health()['yields']), (1, 0))   # the LANE_SHED answer itself

    def test_a_permanently_shed_lane_changes_nothing_about_the_exits(self):
        today = self.build(Token(31, path=BOT_PATH), bot_pump_events())
        self.run_until(today, 1300)
        baseline = self.fills(today)
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'mode': 'live'})
        self.run_until(r, 0)
        providers.shed_low('helius', 3600 * 5, clock=self.clock.monotonic, sleep=self.clock.sleep)
        self.run_until(r, 1300, start=T0 + TICK)
        self.assertEqual(self.fills(r), baseline)                                   # same STOP, same time, same price
        self.assertEqual(self.signals(r, B.LIVE_KIND), [])
        self.assertGreater(r.bot_exit.health()['sheds'], 90)

    def test_a_yield_leaves_the_reserve_for_the_other_low_lane_users(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'pace_s': 0})
        self.run_until(r, 0)
        self.clock.advance(5)                                                       # the bucket refills (burst 2 = 1 call + 1 reserve)
        limiter = r.bot_exit.low.helius.transport.limiter
        made = r.bot_exit.poll_pass(r)
        self.assertGreaterEqual(made, 1)
        self.assertGreaterEqual(limiter.available(), r.bot_exit.cfg.reserve_tokens - 1e-9)   # never drained below the reserve
        self.assertGreater(r.bot_exit.health()['yields'], 0)

    def test_a_provider_failure_is_isolated_and_counted(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={})
        self.run_until(r, 0)
        self.world.extra_rpc['getSignaturesForAddress'] = lambda p, t: {'not': 'a list'}
        self.run_until(r, 60, start=T0 + TICK)
        self.assertIsNone(r.halted)
        errors = [e for e in r.store.rows('errors', limit=1000) if e['scope'] == 'bot_exit']
        self.assertTrue(errors and errors[0]['code'] == 'SIGNATURES_MALFORMED')
        self.assertIn(self.token.mint, r.store.positions())

    def test_failed_transactions_are_not_counted_as_bot_activity(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={}, failed_every=4)
        self.run_until(r, 600)
        watch = next(iter(r.bot_exit.watches.values()))
        m = r.bot_exit.metrics(watch, watch.polled_at)
        self.assertAlmostEqual(float(m['failed_share_30s']), 0.25, delta=0.01)
        self.assertAlmostEqual(float(m['tx_rate_30s']), 3.75, delta=0.15)          # 5/s less the failed quarter

    def test_the_trigger_hook_is_a_lookup_only(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'mode': 'live'})
        self.run_until(r, 0)
        with mock.patch.object(self.world, 'opener', side_effect=AssertionError('I/O in the hook')):
            self.assertIsNone(r.bot_exit.trigger(self.token.mint, 1))
            self.assertIsNone(r.bot_exit.trigger(self.token.mint, 'x'))


class Thread(Scenario):
    def test_the_monitor_runs_on_its_own_thread_and_reports_alive(self):
        r = self.build(Token(31, path=BOT_PATH), bot_pump_events(), bot_exit={'poll_s': 1})
        self.run_until(r, 0)
        stop = threading.Event()
        r.bot_exit.stop = stop
        passes = threading.Event()
        orig = r.bot_exit.poll_pass

        def one(runner):
            out = orig(runner)
            passes.set()
            return out
        r.bot_exit.poll_pass = one
        thread = r._aux_threads['bot_exit'] = threading.Thread(target=r.bot_exit.run_loop, args=(r,), daemon=True)
        thread.start()
        self.assertTrue(passes.wait(5))
        health = r.health()['bot_exit']
        self.assertTrue(health['alive'])
        self.assertEqual((health['enabled'], health['mode']), (True, 'shadow'))
        for key in ('signals', 'calls', 'sheds', 'polls'):
            self.assertIn(key, health)
        stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(r.health()['bot_exit']['alive'])


# --------------------------------------------------------------------------------------------------------------- config
class Config(unittest.TestCase):
    def test_absent_or_disabled_is_off(self):
        self.assertFalse(B.Config.from_dict(None).enabled)
        self.assertFalse(B.Config.from_dict({}).enabled)
        self.assertIsNone(B.build(None, store=None, keys=KEYS, code_version='c', strategy_version='s'))
        self.assertIsNone(B.build({'enabled': False, 'mode': 'live'}, store=None, keys=KEYS, code_version='c', strategy_version='s'))
        self.assertIsNone(load_config(BASELINE)['bot_exit'])
        self.assertEqual(B.Config.from_dict({}).mode, 'shadow')

    def test_strict(self):
        bad = [{'enable': True}, {'enabled': 'yes'}, {'mode': 'LIVE'}, {'poll_s': 0}, {'poll_s': 1.5}, {'pace_s': -1},
               {'rules': []}, {'rules': [{'name': 'X', 'all': [{'metric': 'nope', 'op': '<', 'value': '1'}]}]},
               {'rules': [{'name': 'X', 'all': [{'metric': 'tx_rate_ratio', 'op': '=', 'value': '1'}]}]},
               {'rules': [{'name': 'X', 'all': [{'metric': 'tx_rate_ratio', 'op': '<', 'value': 0.3}]}]},
               {'rules': [{'name': 'X', 'all': [{'metric': 'tx_rate_ratio', 'op': '<', 'value': 'NaN'}]}]},
               {'rules': [{'name': 'X', 'all': []}]},
               {'rules': [{'name': 'X', 'all': [{'metric': 'creator_sold', 'op': '>=', 'value': '1'}]},
                          {'name': 'X', 'all': [{'metric': 'creator_sold', 'op': '>=', 'value': '1'}]}]},
               {'sample_txs': 9, 'max_calls_per_pass': 4}, 'shadow']
        for value in bad:
            with self.assertRaises(B.BotExitConfigError, msg=value):
                B.Config.from_dict(value)
        with tempfile.TemporaryDirectory() as d:
            raw = json.loads(EXAMPLE.read_text())
            raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
            raw['bot_exit'] = {'enabled': True, 'mode': 'paper'}
            path = Path(d) / 'lean.json'
            path.write_text(json.dumps(raw))
            from lean.__main__ import ConfigError
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_the_example_turns_on_shadow_only(self):
        raw = json.loads(EXAMPLE.read_text())
        cfg = B.Config.from_dict(load_config(EXAMPLE)['bot_exit'])
        self.assertEqual((cfg.enabled, cfg.mode), (True, 'shadow'))
        self.assertEqual(cfg.rules, B.Config.from_dict({}).rules)                  # the example spells out the defaults
        self.assertEqual({k for k in raw['bot_exit'] if k != '_comment'}, set(B.DEFAULTS))

    def test_the_production_config_loads_unchanged_with_bot_exit_off(self):
        from tests.lean.test_lint2 import production_r1
        raw = production_r1()
        raw.pop('bot_exit')                                                         # the live r1 config has no bot_exit key
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'lean.json'
            path.write_text(json.dumps(raw))
            cfg = load_config(path)
            self.assertIsNone(cfg['bot_exit'])
            world = World(Path(os.path.realpath(d)), [Token(41)], FakeTime(T0))
            r = build_runner(cfg, state_dir=Path(d) / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='l23',
                             transport_kwargs={'opener': world.opener})
            self.addCleanup(r.store.close)
            self.assertIsNone(r.bot_exit)
            self.assertEqual(r.health()['bot_exit'], {'enabled': False, 'alive': False})
            raw['bot_exit'] = json.loads(EXAMPLE.read_text())['bot_exit']           # adding exactly the example block: shadow on
            path.write_text(json.dumps(raw))
            cfg = load_config(path)
            r2 = build_runner(cfg, state_dir=Path(d) / 'state2', discovery_db=world.discovery_db, keys=KEYS, code_version='l23',
                              transport_kwargs={'opener': world.opener})
            self.addCleanup(r2.store.close)
            self.assertEqual(r2.bot_exit.cfg.mode, 'shadow')

    def test_python_m_lean_once_builds_it_and_never_starts_it(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from lean import __main__ as entry
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            world = World(root, [Token(41)], FakeTime(T0))
            world.add_frames([T0])
            keys = root / 'provider-keys.json'
            keys.write_text(json.dumps({'HELIUS_API_KEY': KEYS['helius'], 'JUPITER_API_KEY': KEYS['jupiter']}))
            keys.chmod(0o400)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch('lean.providers.default_opener', world.opener), mock.patch('logging.basicConfig'), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = entry.main(['--config', str(EXAMPLE), '--state-dir', str(root / 'state'), '--discovery-db',
                                 str(world.discovery_db), '--keys-file', str(keys), '--once'])
            self.assertEqual(rc, 0, err.getvalue())
            health = json.loads((root / 'state' / 'health.json').read_text())
            self.assertEqual((health['bot_exit']['enabled'], health['bot_exit']['mode'], health['bot_exit']['calls']), (True, 'shadow', 0))
            self.assertFalse(health['bot_exit']['alive'])
            self.assertNotIn('TEST-HELIUS-KEY', out.getvalue() + err.getvalue() + json.dumps(health))


# ------------------------------------------------------------------------------------------------------------------ pure
class Pure(unittest.TestCase):
    MINT, POOL = wallet(1), wallet(2)

    def tx(self, pool_pre, pool_post, trader=(0, 0), err=None):
        def bal(p, tr):
            return [{'mint': self.MINT, 'owner': self.POOL, 'uiTokenAmount': {'amount': str(p[0])}},
                    {'mint': SOL, 'owner': self.POOL, 'uiTokenAmount': {'amount': str(p[1])}},
                    {'mint': self.MINT, 'owner': wallet(3), 'uiTokenAmount': {'amount': str(tr)}}]
        return {'blockTime': 100, 'meta': {'err': err, 'preTokenBalances': bal(pool_pre, trader[0]), 'postTokenBalances': bal(pool_post, trader[1])},
                'transaction': {'message': {'accountKeys': [{'pubkey': wallet(9), 'signer': True}]}}}

    def test_parse_swap(self):
        buy = B.parse_swap(self.tx((1000, 80), (900, 90), (0, 100)), self.MINT, self.POOL)
        self.assertEqual((buy['side'], buy['sol'], buy['trader'], buy['reserve_pre'], buy['tokens']), ('buy', 10, wallet(3), 80, 100))
        sell = B.parse_swap(self.tx((1000, 80), (1100, 70), (100, 0)), self.MINT, self.POOL)
        self.assertEqual((sell['side'], sell['sol'], sell['reserve_post']), ('sell', 10, 70))
        self.assertIsNone(B.parse_swap(self.tx((1000, 80), (1000, 80)), self.MINT, self.POOL))          # not a swap of this pool
        self.assertIsNone(B.parse_swap(self.tx((1000, 80), (900, 70)), self.MINT, self.POOL))           # tokens and SOL both out
        self.assertIsNone(B.parse_swap(self.tx((1000, 80), (900, 90), err={'x': 1}), self.MINT, self.POOL))
        self.assertIsNone(B.parse_swap(None, self.MINT, self.POOL))
        no_trader = self.tx((1000, 80), (900, 90))
        self.assertEqual(B.parse_swap(no_trader, self.MINT, self.POOL)['trader'], wallet(9))           # the signer
        for bad in ({}, {'meta': 1}, {'meta': {'preTokenBalances': 'x'}}, {'blockTime': '1', 'meta': {}},
                    {'meta': {'preTokenBalances': [{'mint': self.MINT, 'owner': 1, 'uiTokenAmount': {'amount': '1'}}]}}):
            with self.assertRaises(B.BotExitError):
                B.parse_swap(bad, self.MINT, self.POOL)

    def test_parse_signatures(self):
        sig = base58(bytes(range(64)))
        self.assertEqual(B.parse_signatures([{'signature': sig, 'blockTime': 5, 'err': None}]), [(sig, 5, False)])
        for bad in ({}, [1], [{'signature': 'x'}], [{'signature': sig, 'blockTime': '5'}]):
            with self.assertRaises(B.BotExitError):
                B.parse_signatures(bad)

    def test_evaluate_never_fires_on_unknown_metrics(self):
        rules = B.Config.from_dict({}).rules
        self.assertEqual(B.evaluate(rules, dict.fromkeys(B.METRICS)), [])
        m = dict.fromkeys(B.METRICS)
        m.update(tx_rate_ratio=Decimal('0.29'), sell_share_30s=Decimal('0.61'))
        self.assertEqual(B.evaluate(rules, m), ['BOT_STOPPED'])
        m.update(tx_rate_ratio=Decimal('0.3'))
        self.assertEqual(B.evaluate(rules, m), [])                                  # strict <
        m.update(pumper_net_sol_30s=Decimal('-0.1'), pumper_sellers_30s=1)
        self.assertEqual(B.evaluate(rules, m), [])
        m.update(pumper_sellers_30s=2)
        self.assertEqual(B.evaluate(rules, m), ['PUMPERS_OUT'])

    def test_holders_outflow(self):
        self.assertIsNone(B.holders_outflow(None, {'a': 1}))
        self.assertEqual(B.holders_outflow({'a': 100, 'b': 100}, {'a': 50, 'b': 100}), Decimal('0.25'))
        self.assertEqual(B.holders_outflow({'a': 100, 'b': 100}, {'b': 100, 'c': 80}), Decimal('0.1'))   # a dropped out: <= 80 left
        self.assertEqual(B.holders_outflow({'a': 100}, {'a': 150}), Decimal(0))
        self.assertEqual(B.parse_largest({'value': [{'address': 'v', 'amount': '9'}, {'address': 'h', 'amount': '3'}]}, {'v'}), {'h': 3})
        with self.assertRaises(B.BotExitError):
            B.parse_largest({'value': [{'address': 'h', 'amount': 3}]}, set())

    def test_spread_keeps_the_newest(self):
        self.assertEqual(B._spread(list('abcdefghij'), 2), ['a', 'j'])
        self.assertEqual(B._spread(list('abcdefghij'), 3), ['a', 'e', 'j'])
        self.assertEqual(B._spread(list('ab'), 5), ['a', 'b'])
        self.assertEqual(B._spread(list('ab'), 0), [])
        self.assertEqual(B._spread(list('abc'), 1), ['a'])

    def test_rates_are_none_over_a_gap_or_before_coverage(self):
        cfg = B.Config.from_dict({'enabled': True})
        monitor = B.BotExit.__new__(B.BotExit)
        monitor.cfg = cfg
        watch = B.Watch('m', 1, 'p', 'v', 0.0, None)
        for k in range(2401):                                                       # 10 tx/s for 240 s
            watch.sigs.append((k / 10, False))
        watch.covered_since = 0.0
        m = monitor.metrics(watch, 240.0)
        self.assertEqual((m['tx_rate_30s'], m['tx_rate_baseline'], m['tx_rate_ratio']), (Decimal('10'), Decimal('10'), Decimal('1')))
        watch.covered_since = 100.0                                                 # the baseline starts before the record
        self.assertIsNone(monitor.metrics(watch, 240.0)['tx_rate_ratio'])
        watch.covered_since = 0.0
        watch.gaps = [(215.0, 225.0)]                                               # a full page inside the recent window
        m = monitor.metrics(watch, 240.0)
        self.assertIsNone(m['tx_rate_30s'])
        self.assertIsNone(m['tx_rate_ratio'])
        monitor.cfg = B.Config.from_dict({'min_baseline_tx': 5000})                 # a quiet baseline: no rate rule
        watch.gaps = []
        self.assertIsNone(monitor.metrics(watch, 240.0)['tx_rate_ratio'])

    def test_sell_share_needs_min_sample_swaps(self):
        monitor = B.BotExit.__new__(B.BotExit)
        monitor.cfg = B.Config.from_dict({'enabled': True})
        watch = B.Watch('m', 1, 'p', 'v', 0.0, None)
        watch.covered_since = float('-inf')
        for k in range(3):
            watch.swaps.append({'t': 230.0 + k, 'side': 'sell', 'sol': 1, 'trader': wallet(k), 'reserve_pre': 100, 'reserve_post': 99, 'sig': str(k)})
        self.assertIsNone(monitor.metrics(watch, 240.0)['sell_share_30s'])        # 3 < min_sample 4
        watch.swaps.append({'t': 235.5, 'side': 'buy', 'sol': 1, 'trader': wallet(9), 'reserve_pre': 99, 'reserve_post': 100, 'sig': '9'})
        watch.swaps = B.deque(sorted(watch.swaps, key=lambda x: x['t']))
        m = monitor.metrics(watch, 240.0)
        self.assertEqual((m['sell_share_30s'], m['swaps_sampled_30s'], m['net_sol_flow_30s']), (Decimal('0.75'), 4, Decimal('0')))

    def test_a_full_signature_page_records_a_gap(self):
        class Lane:
            pass
        cfg = B.Config.from_dict({'enabled': True, 'sig_limit': 10, 'reserve_tokens': 0, 'pace_s': 0})
        monitor = B.BotExit.__new__(B.BotExit)
        monitor.cfg, monitor.stop, monitor._lock, monitor.sleep = cfg, None, threading.Lock(), lambda s: None
        monitor.clock = iter([100.0, 200.0]).__next__
        monitor.counts = dict.fromkeys(('calls', 'sheds', 'yields', 'polls', 'errors'), 0)
        pages = [[{'signature': base58(bytes([i]) * 64), 'blockTime': 100 - i, 'err': None} for i in range(10)],
                 [{'signature': base58(bytes([50 + i]) * 64), 'blockTime': 200 - i, 'err': None} for i in range(10)]]
        lane = Lane()
        lane.helius = mock.Mock()
        lane.helius.rpc.side_effect = [(p, b'', {}) for p in pages]
        lane.helius.transport = None
        monitor.low = lane
        runner = mock.Mock()
        runner.stop.is_set.return_value = False
        watch = B.Watch('m', 1, wallet(5), 'v', 0.0, None)
        monitor._signatures(runner, watch, [5], 100.0)
        self.assertEqual(watch.covered_since, 91.0)                                 # full first page: complete from its oldest only
        monitor._signatures(runner, watch, [5], 200.0)
        self.assertEqual(watch.gaps, [(100.0, 191.0)])
        self.assertEqual(lane.helius.rpc.call_args_list[1][0][1][1]['until'], pages[0][0]['signature'])


if __name__ == '__main__':
    unittest.main()
