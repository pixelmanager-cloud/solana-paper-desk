"""SYNTHETIC_TEST_ONLY: lean runner against interface fakes (L01-L04 not required). Fixtures only, no network."""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass

from lean import runner as R
from lean.runner import AccountingHalt, ProviderError, Runner, reserve_mark

SOL = R.SOL_MINT
INITIAL = 5.0


@dataclass
class Fill:
    side: str
    mint: str
    qty_raw: int
    sol: float
    fee: float = 0.00005


@dataclass
class Pos:
    mint: str
    qty_raw: int
    cost: float
    vault_base: str = 'vb'
    vault_quote: str = 'vq'


class FakeStore:
    """Append-only; positions and cash derived from fills; invariants like L01's."""
    def __init__(self, fail_after=None):
        self.rows, self.lock, self.fail_after, self.writes = [], threading.Lock(), fail_after, 0
        self.corrupt = False

    def record(self, kind, payload, *, code_version, strategy_version):
        with self.lock:
            self.writes += 1
            if self.fail_after is not None and self.writes > self.fail_after:
                raise OSError('disk full')
            assert code_version and strategy_version
            self.rows.append((kind, payload, code_version, strategy_version))
            return len(self.rows)

    def fills(self):
        return [p for k, p, *_ in self.rows if k == 'fill']

    def positions(self):
        held = {}
        for f in self.fills():
            p = held.setdefault(f['mint'], [0, 0.0])
            if f['side'] == 'buy':
                p[0] += f['qty_raw']; p[1] += f['sol']
            else:
                share = f['qty_raw'] / p[0] if p[0] else 1
                p[1] -= p[1] * share; p[0] -= f['qty_raw']
        return [Pos(m, q, c) for m, (q, c) in held.items() if q > 0]

    def cash(self):
        c = INITIAL
        for f in self.fills():
            c += (-f['sol'] if f['side'] == 'buy' else f['sol']) - f['fee']
        return c

    def check_invariants(self):
        held = {}
        for f in self.fills():
            held[f['mint']] = held.get(f['mint'], 0) + (f['qty_raw'] if f['side'] == 'buy' else -f['qty_raw'])
            if held[f['mint']] < 0:
                raise AccountingHalt('negative quantity')
        if self.corrupt or self.cash() < 0:
            raise AccountingHalt('cash does not reconcile')


class FakeCandidates:
    def __init__(self, items, behaviour):
        self.items, self.behaviour = items, behaviour

    def iter_new_candidates(self, db, cursor):
        return [c for c in self.items if cursor is None or c['cursor'] > cursor]

    def screen(self, candidate, providers, cfg):
        what = self.behaviour.get(candidate['mint'], 'pass')
        if what == 'crash':
            raise ValueError('malformed response')
        if what == 'provider':
            raise ProviderError('HTTP_503', True)
        passed = what == 'pass'
        return type('Screen', (), {'passed': passed, 'reasons': [] if passed else [what], 'features': {'mint': candidate['mint']}})()


class FakeStrategy:
    """Exit by mark ratio: <= 0.82 stop, >= 1.4 take-profit; entry unless the portfolio is full."""
    def entry_decision(self, features, quote, portfolio, cfg):
        full = len(portfolio['positions']) >= cfg.get('max_positions', 4)
        return type('D', (), {'enter': not full, 'size_sol': 0.02, 'reason': 'FULL' if full else 'OK'})()

    def exit_decision(self, position, mark, quote, now, cfg):
        ratio = mark / position.cost
        if ratio <= 0.82:
            return type('D', (), {'exit': True, 'fraction': 1.0, 'reason': 'STOP'})()
        if ratio >= 1.4:
            return type('D', (), {'exit': True, 'fraction': 1.0, 'reason': 'TAKE_PROFIT'})()
        return type('D', (), {'exit': False, 'fraction': 0.0, 'reason': 'HOLD'})()


class FakePaper:
    def buy(self, quote, size_sol, cfg):
        return Fill('buy', quote['mint'], quote['out_raw'], size_sol)

    def sell(self, position, quote, fraction, cfg):
        return Fill('sell', position.mint, int(position.qty_raw * fraction), quote['sol_out'])


class FakeJupiter:
    def __init__(self):
        self.calls, self.fail_mints, self.sell_value = [], set(), {}

    def quote(self, i, o, amount, taker):
        self.calls.append((i, o, amount))
        mint = o if i == SOL else i
        if mint in self.fail_mints:
            raise ProviderError('TIMEOUT', True)
        if i == SOL:
            return {'mint': mint, 'out_raw': 1000}, b'{"q":1}', {}
        return {'mint': mint, 'sol_out': self.sell_value.get(mint, 0.02)}, b'{"q":2}', {}


class FakeHelius:
    def __init__(self):
        self.calls = []

    def get_multiple_accounts(self, pubkeys):
        self.calls.append(list(pubkeys))
        return [None] * len(pubkeys), b'raw', {}


def candidates(n):
    return [{'mint': 'M%02d' % i, 'cursor': i + 1} for i in range(n)]


class Harness(unittest.TestCase):
    def build(self, items=None, behaviour=None, store=None, cfg=None, default_marks=False):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.now = [1000.0]
        self.store = store or FakeStore()
        self.jup, self.hel = FakeJupiter(), FakeHelius()
        self.cands = FakeCandidates(items if items is not None else candidates(20), behaviour or {})
        self.kill = os.path.join(tmp.name, 'KILL')
        self.marks = {}
        self.runner = Runner(store=self.store, helius=self.hel, jupiter=self.jup, kraken=None, candidates=self.cands,
                             strategy=FakeStrategy(), paper=FakePaper(), cfg=cfg or {'entry_probe_sol': 0.02, 'max_positions': 4},
                             discovery_db='/nonexistent/discovery.sqlite', health_path=os.path.join(tmp.name, 'health.json'),
                             code_version='abc123', strategy_version='s1', kill_switch=self.kill, clock=lambda: self.now[0],
                             mark_fn=None if default_marks else self.fake_marks)
        return self.runner

    def fake_marks(self, positions):
        return {p.mint: self.marks[p.mint] for p in positions if p.mint in self.marks}

    def health(self):
        with open(self.runner.health_path) as stream:
            return json.load(stream)


class EndToEnd(Harness):
    def test_twenty_candidates_mixed_outcomes_outage_and_invariants_hold(self):
        behaviour = {'M01': 'low_liquidity', 'M03': 'mint_authority', 'M05': 'crash', 'M07': 'provider',
                     'M09': 'holders', 'M11': 'low_liquidity', 'M13': 'crash', 'M15': 'freeze_authority'}
        self.build(behaviour=behaviour)
        self.jup.fail_mints = {'M02'}                      # entry quote times out for this one
        r, s = self.runner, self.store
        r.candidate_pass()
        self.assertEqual(len([p for k, p, *_ in s.rows if k == 'screen' and not p['passed']]), 5)
        self.assertEqual(r.errors_by_code['CANDIDATE_FAILED'], 2)       # M05, M13: malformed -> recorded, loop moved on
        self.assertEqual(r.errors_by_code['PROVIDER_HTTP_503'], 1)      # M07
        self.assertEqual(r.errors_by_code['PROVIDER_TIMEOUT'], 1)       # M02 quote
        self.assertEqual(len(s.positions()), 4)                         # portfolio cap from config
        self.assertEqual((r.counts['candidates'], r.cursor), (20, 20))
        r.candidate_pass(); self.assertEqual(r.counts['candidates'], 20)   # cursor: nothing re-processed
        held = [p.mint for p in s.positions()]
        self.marks = {held[0]: 0.5 * 0.02, held[1]: 1.6 * 0.02, held[2]: 0.02, held[3]: 0.02}
        self.jup.sell_value = {held[0]: 0.009, held[1]: 0.031}
        good = r.mark_fn
        r.mark_fn = lambda positions: (_ for _ in ()).throw(ProviderError('TIMEOUT', True))
        self.assertEqual(r.position_pass(), 0)                          # provider outage mid-run: positions untouched
        self.assertEqual(len(s.positions()), 4); self.assertIsNone(r.halted)
        r.mark_fn = good
        self.assertEqual(r.position_pass(), 2)
        self.assertEqual(sorted(f['reason'] for f in s.fills() if f['side'] == 'sell'), ['STOP', 'TAKE_PROFIT'])
        self.assertEqual(len(s.positions()), 2)
        s.check_invariants()
        self.assertTrue(all(f['execution_status'] == 'EXECUTION_UNVERIFIED' for f in s.fills()))
        self.assertTrue(all(row[2:] == ('abc123', 's1') for row in s.rows))   # code_version + strategy_version on every row

    def test_exit_quote_outage_leaves_position_open_and_retries(self):
        self.build(items=candidates(1))
        r = self.runner; r.candidate_pass()
        mint = self.store.positions()[0].mint
        self.marks = {mint: 0.001}
        self.jup.fail_mints = {mint}
        self.assertEqual(r.position_pass(), 0)
        self.assertEqual(len(self.store.positions()), 1)
        self.assertEqual(r.errors_by_code.get('PROVIDER_TIMEOUT'), 1)   # typed provider failure, not an unexpected one
        self.assertNotIn('POSITION_CHECK_FAILED', r.errors_by_code)
        self.jup.fail_mints = set()
        self.assertEqual(r.position_pass(), 1)
        self.assertEqual(self.store.positions(), [])

    def test_already_held_mint_is_not_bought_twice(self):
        self.build(items=[{'mint': 'M00', 'cursor': 1}, {'mint': 'M00', 'cursor': 2}])
        self.runner.candidate_pass()
        self.assertEqual(len(self.store.fills()), 1)


class Halts(Harness):
    def test_accounting_violation_stops_entries_but_not_position_checks_and_writes_reason(self):
        self.build(items=candidates(3))
        r = self.runner
        r.candidate_pass()
        self.assertEqual(r.counts['entered'], 3)
        self.store.corrupt = True
        self.cands.items += [{'mint': 'N%d' % i, 'cursor': 100 + i} for i in range(3)]
        r.candidate_pass()                                              # the first new buy trips the invariant
        self.assertIn('ACCOUNTING_INVARIANT', r.halted)
        entered = r.counts['entered']
        self.cands.items += [{'mint': 'P%d' % i, 'cursor': 200 + i} for i in range(3)]   # fresh candidates after the halt
        self.assertEqual(r.candidate_pass(), 0)
        self.assertEqual(r.counts['entered'], entered)                  # halted: no new entries
        r.write_health(); self.assertIn('ACCOUNTING_INVARIANT', self.health()['halted'])
        self.marks = {p.mint: 0.02 for p in self.store.positions()}
        self.assertEqual(r.position_pass(), 0)                          # held monitoring still runs

    def test_store_write_failure_halts_entries(self):
        self.build(items=candidates(5), store=FakeStore(fail_after=3))
        r = self.runner
        r.candidate_pass()
        self.assertEqual(r.halted, 'STORE_WRITE_FAILED')
        self.assertLess(r.counts['entered'], 5)

    def test_kill_switch_file_blocks_entries_and_removal_resumes(self):
        self.build(items=candidates(2))
        r = self.runner
        open(self.kill, 'w').close()
        self.assertEqual(r.candidate_pass(), 0)
        self.assertEqual(self.store.positions(), [])
        r.write_health(); self.assertTrue(self.health()['kill_switch'])
        os.remove(self.kill)
        self.assertEqual(r.candidate_pass(), 2)
        self.assertEqual(len(self.store.positions()), 2)


class Lifecycle(Harness):
    def run_in_thread(self):
        t = threading.Thread(target=self.runner.run, kwargs=dict(candidate_interval=0.01, position_interval=0.01, install_signals=False))
        t.start()
        return t

    def test_stop_joins_both_loops_and_writes_final_health(self):
        self.build(items=candidates(6))
        t = self.run_in_thread()
        deadline = time.time() + 5
        while self.runner.counts['entered'] < 4 and time.time() < deadline:
            time.sleep(0.01)
        self.runner.stop.set(); t.join(5)
        self.assertFalse(t.is_alive())
        h = self.health()
        self.assertEqual((h['kind'], h['code_version'], h['strategy_version']), ('lean_health_v1', 'abc123', 's1'))
        self.assertEqual(len(h['open_positions']), 4)
        self.assertIsNotNone(h['last_loop']['candidate_loop']); self.assertIsNotNone(h['last_loop']['position_loop'])
        self.assertFalse(h['live_readiness'])
        self.store.check_invariants()

    def test_loop_body_bug_in_one_loop_does_not_kill_the_other(self):
        self.build(items=candidates(2))
        self.runner.candidate_pass = lambda: (_ for _ in ()).throw(RuntimeError('bug'))
        t = self.run_in_thread(); time.sleep(0.2); self.runner.stop.set(); t.join(5)
        self.assertFalse(t.is_alive())
        self.assertGreater(self.runner.errors_by_code.get('LOOP_FAILED', 0), 0)
        self.assertIsNotNone(self.runner.last['position_loop'])


class Marks(Harness):
    @staticmethod
    def account(amount):
        data = bytearray(165); data[64:72] = amount.to_bytes(8, 'little')
        return {'data': [base64.b64encode(bytes(data)).decode(), 'base64']}

    def test_reserve_mark_is_constant_product_spot_value(self):
        mark = reserve_mark(Pos('M', 2_000_000, 0.01), (self.account(1_000_000_000), self.account(50 * 10 ** 9)))
        self.assertAlmostEqual(mark, 2_000_000 * 50 * 10 ** 9 / 1_000_000_000 / 10 ** 9)

    def test_missing_or_empty_pool_is_a_typed_failure(self):
        for accounts in ((None, self.account(1)), (self.account(0), self.account(1))):
            with self.assertRaises(ProviderError):
                reserve_mark(Pos('M', 1, 0.01), accounts)

    def test_default_marks_make_one_batched_read_for_all_positions(self):
        self.build(items=candidates(3), default_marks=True)
        self.runner.candidate_pass()
        self.hel.calls.clear()
        self.runner.position_pass()
        self.assertEqual(len(self.hel.calls), 1)                        # N positions, ONE getMultipleAccounts call
        self.assertEqual(len(self.hel.calls[0]), 6)


if __name__ == '__main__':
    unittest.main()
