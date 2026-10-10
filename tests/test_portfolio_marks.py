"""SYNTHETIC_TEST_ONLY: batched reserve-implied portfolio marks (paper_portfolio_mark_source_version=1)."""
import base64
import copy
import json
import unittest
from decimal import Decimal

from desk import engine, portfolio_marks as pm
from desk.model import digest
from desk.engine import initial_state, transition
from desk.providers import PUMP, PUMPSWAP, SOL
from desk.pools import ATA
from tests.helpers import T, config
from tests.test_paper_concurrency import position as base_position

b64 = lambda raw: base64.b64encode(raw).decode()
ZERO = bytes(32)


def cfg_marks(**changes):
    return {**config(), 'mode': 'paper', 'paper_quote_execution_version': 1, 'paper_concurrent_entries_version': 1,
            pm.KEY: 1, pm.FEE_KEY: '25', **changes}


class Chain:
    """Synthetic pools: real PDAs/ATAs, parseable pool accounts, token-account bytes."""
    def __init__(self, seed, decimals=6, program=pm.TOKEN_PROGRAM, mint_extra=b''):
        from solders.pubkey import Pubkey
        self.Pubkey = Pubkey
        self.mint = Pubkey.from_bytes(bytes([seed]) * 32)
        creator = Pubkey.find_program_address([b'pool-authority', bytes(self.mint)], Pubkey.from_string(PUMP))[0]
        self.pool, bump = Pubkey.find_program_address(
            [b'pool', bytes(2), bytes(creator), bytes(self.mint), bytes(Pubkey.from_string(SOL))], Pubkey.from_string(PUMPSWAP))
        lp = Pubkey.find_program_address([b'pool_lp_mint', bytes(self.pool)], Pubkey.from_string(PUMPSWAP))[0]
        self.program, self.decimals = program, decimals
        self.vaults = [Pubkey.find_program_address([bytes(self.pool), bytes(Pubkey.from_string(p)), bytes(m)],
                                                   Pubkey.from_string(ATA))[0]
                       for p, m in ((program, self.mint), (pm.TOKEN_PROGRAM, Pubkey.from_string(SOL)))]
        self.pool_raw = (bytes([241, 154, 109, 4, 17, 177, 109, 188]) + bytes([bump]) + bytes(2)
                         + b''.join(bytes(x) for x in [creator, self.mint, Pubkey.from_string(SOL), lp, *self.vaults])
                         + (1000).to_bytes(8, 'little') + bytes(32))
        mint = bytearray(82)
        mint[44], mint[45] = decimals, 1
        self.mint_account = {'owner': program, 'executable': False, 'data': [b64(bytes(mint) + mint_extra), 'base64']}

    def token(self, mint, amount, owner=None, program=None):
        d = bytearray(165)
        d[:32] = bytes(mint)
        d[32:64] = bytes(owner if owner is not None else self.pool)
        d[64:72] = amount.to_bytes(8, 'little')
        d[108] = 1
        return {'owner': program or pm.TOKEN_PROGRAM, 'executable': False, 'data': [b64(bytes(d)), 'base64']}

    def accounts(self, base, quote, **kw):
        sol = self.Pubkey.from_string(SOL)
        return [{'owner': PUMPSWAP, 'executable': False, 'data': [b64(self.pool_raw), 'base64']},
                self.token(self.mint, base, program=self.program), self.token(sol, quote)]

    def position(self, **changes):
        fields = dict(pool=str(self.pool), qty='1000', initial_qty='1000', cost_left='0.1', initial_cost='0.1',
                      quote_execution={'mint_decimals': self.decimals,
                                       'original_mint_json': json.dumps({'account': self.mint_account})})
        return base_position(**{**fields, **changes})


def reply(chains, amounts, slot=500):
    values = []
    for chain in chains:
        values += chain.accounts(*amounts[str(chain.mint)])
    return {'context': {'slot': slot}, 'value': values}


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_marks()
        self.chain = Chain(7)
        self.position = self.chain.position()

    def test_parity_with_the_held_watcher_model(self):
        from tools.ops.held_watcher import implied_ratio
        for base, quote, fee, creator, transfer, virtual in [(5 * 10 ** 9, 4 * 10 ** 11, 25, 0, 0, 0),
                (10 ** 9, 10 ** 10, 25, 30, 0, 0), (10 ** 9, 10 ** 10, 25, 30, 500, 10 ** 9), (1, 10, 25, 0, 0, 0)]:
            ours = pm.implied_ratio(self.position, base, quote, self.cfg, fee, creator_fee_bps=creator,
                                    transfer_fee_bps=transfer, virtual_quote_raw=virtual)
            theirs = implied_ratio(self.position, base, quote, self.cfg, fee, creator, transfer, virtual)
            # same model; ours runs at a fixed 60-digit context, the watcher's at the ambient 28 digits
            self.assertAlmostEqual(ours, theirs, delta=Decimal('1e-20'), msg=str((base, quote, fee, creator, transfer, virtual)))

    def test_orientation_decimals_and_fees(self):
        value = lambda b, q, fee, **k: pm.implied_value(self.position, b, q, self.cfg, fee, **k)
        deep = value(10 ** 12, 10 ** 12, 25)          # 1e6 tokens vs 1000 SOL
        self.assertLess(deep, Decimal('1.0'))                       # 1000 tokens of 1e6: well under 1 SOL
        # more SOL against the same tokens is worth more (quote is the numerator), more tokens is worth less
        self.assertGreater(value(10 ** 12, 2 * 10 ** 12, 25), deep)
        self.assertLess(value(2 * 10 ** 12, 10 ** 12, 25), deep)
        # decimals: the same raw base reserve at 9 decimals is 1000x FEWER tokens, so a fixed 1000-token position owns far more of it
        other = Chain(7, decimals=9).position()
        self.assertGreater(pm.implied_value(other, 10 ** 12, 10 ** 12, self.cfg, 25), deep * 100)
        # each fee lowers the value; virtual quote reserves raise it
        self.assertLess(value(10 ** 12, 10 ** 12, 25, creator_fee_bps=30), deep)
        self.assertLess(value(10 ** 12, 10 ** 12, 25, transfer_fee_bps=500), deep)
        self.assertGreater(value(10 ** 12, 10 ** 12, 25, virtual_quote_raw=10 ** 12), deep)
        self.assertEqual(value(10 ** 12, 0, 25), 0)                  # drained pool
        with self.assertRaises(pm.MarkError):
            value(-1, 10, 25)
        with self.assertRaises(pm.MarkError):
            value(10, 2 ** 64, 25)

    def test_cross_slot_pair_is_never_priced(self):
        with self.assertRaises(pm.MarkError):
            pm.pair_value(self.position, (10 ** 12, 100), (10 ** 12, 101), self.cfg, 25)
        self.assertGreater(pm.pair_value(self.position, (10 ** 12, 100), (10 ** 12, 100), self.cfg, 25), 0)
        with self.assertRaises(pm.MarkError):
            pm.pair_value(self.position, (10 ** 12, None), (10 ** 12, None), self.cfg, 25)


class RequestAndResponseTests(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_marks()
        self.chains = [Chain(seed) for seed in (7, 8, 9, 10)]
        self.positions = {str(c.mint): c.position() for c in self.chains}
        self.amounts = {str(c.mint): (10 ** 12, 10 ** 12) for c in self.chains}

    def keys(self):
        return pm.request_keys(self.positions)

    def test_one_request_covers_every_position_deterministically(self):
        keys = self.keys()
        self.assertEqual(len(keys), 12)
        self.assertEqual(keys, pm.request_keys(dict(reversed(list(self.positions.items())))))   # order independent
        self.assertEqual(pm.request_params(self.positions), [keys, {'encoding': 'base64', 'commitment': 'confirmed', 'minContextSlot': 0}])
        for i, mint in enumerate(sorted(self.positions)):
            self.assertEqual(keys[3 * i], self.positions[mint]['pool'])
        with self.assertRaises(pm.MarkError):
            pm.request_keys({})

    def parse(self, chains=None, **kw):
        chains = sorted(chains or self.chains, key=lambda c: str(c.mint))
        return pm.marks_from_result(self.positions, self.keys(), reply(chains, self.amounts, **kw), self.cfg, pool_fee_bps=pm.pool_fee_bps(self.cfg))

    def test_all_four_marks_in_one_same_slot_response(self):
        marks, slot, errors = self.parse()
        self.assertEqual((sorted(marks), slot, errors), (sorted(self.positions), 500, {}))
        self.assertTrue(all(Decimal(m['value_sol']) > 0 for m in marks.values()))

    def test_bad_context_and_counts_reject_everything(self):
        keys, good = self.keys(), reply(sorted(self.chains, key=lambda c: str(c.mint)), self.amounts)
        for bad in (None, {}, {'context': {}, 'value': good['value']}, {'context': {'slot': True}, 'value': good['value']},
                    {'context': {'slot': -1}, 'value': good['value']}, {'context': {'slot': 5}, 'value': good['value'][:-1]},
                    {'context': {'slot': 5}, 'value': 'x'}):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(pm.MarkError):
                pm.marks_from_result(self.positions, keys, bad, self.cfg, pool_fee_bps=Decimal(25))

    def test_a_broken_position_is_dropped_while_the_others_are_refreshed(self):
        chains = sorted(self.chains, key=lambda c: str(c.mint))
        response = reply(chains, self.amounts)
        victim = str(chains[1].mint)
        vault = bytearray(base64.b64decode(response['value'][4]['data'][0]))
        vault[32:64] = ZERO                                               # vault no longer owned by the pool
        response['value'][4]['data'][0] = b64(bytes(vault))
        marks, _, errors = pm.marks_from_result(self.positions, self.keys(), response, self.cfg, pool_fee_bps=Decimal(25))
        self.assertEqual(sorted(marks), sorted(set(self.positions) - {victim}))
        self.assertEqual(errors, {victim: 'VAULT_OWNER_MISMATCH'})

    def test_substituted_or_mismatched_accounts_are_rejected(self):
        chains = sorted(self.chains, key=lambda c: str(c.mint))
        cases = {}
        swapped = reply(chains, self.amounts)
        swapped['value'][1], swapped['value'][2] = swapped['value'][2], swapped['value'][1]       # base/quote swapped
        cases['swapped'] = swapped
        wrong_mint = reply(chains, self.amounts)
        wrong_mint['value'][1] = chains[0].token(chains[1].mint, 10 ** 12, program=pm.TOKEN_PROGRAM)
        cases['mint'] = wrong_mint
        other_pool = reply(chains, self.amounts)
        other_pool['value'][0] = reply([chains[1]], self.amounts)['value'][0]
        cases['pool'] = other_pool
        short = reply(chains, self.amounts)
        short['value'][2]['data'][0] = b64(bytes(100))
        cases['short'] = short
        for name, response in cases.items():
            marks, _, errors = pm.marks_from_result(self.positions, self.keys(), response, self.cfg, pool_fee_bps=Decimal(25))
            self.assertIn(str(chains[0].mint), errors, name)
            self.assertNotIn(str(chains[0].mint), marks, name)

    def test_pool_account_naming_other_vaults_than_the_derived_ones_is_rejected(self):
        chains = sorted(self.chains, key=lambda c: str(c.mint))
        for offset, name in ((11 + 32 * 4, 'base vault'), (11 + 32 * 5, 'quote vault')):
            response = reply(chains, self.amounts)
            raw = bytearray(base64.b64decode(response['value'][0]['data'][0]))
            raw[offset] ^= 1                                                    # pool fields no longer match the derived ATAs
            response['value'][0]['data'][0] = b64(bytes(raw))
            marks, _, errors = pm.marks_from_result(self.positions, self.keys(), response, self.cfg, pool_fee_bps=Decimal(25))
            self.assertEqual(errors.get(str(chains[0].mint)), 'POOL_BINDING_MISMATCH', name)
            self.assertNotIn(str(chains[0].mint), marks, name)

    def test_wrong_decimals_in_retained_mint_evidence_rejects_that_position(self):
        chains = sorted(self.chains, key=lambda c: str(c.mint))
        self.positions[str(chains[0].mint)]['quote_execution']['mint_decimals'] = 9
        marks, _, errors = self.parse()
        self.assertEqual(errors[str(chains[0].mint)], 'DECIMALS_MISMATCH')

    def test_token_2022_transfer_fee_is_priced_where_known(self):
        ext = bytearray(1 + 4 + 108)          # account type byte, then one TransferFeeConfig extension
        ext[0] = 1
        ext[1:3] = (1).to_bytes(2, 'little')
        ext[3:5] = (108).to_bytes(2, 'little')
        ext[5 + 106:5 + 108] = (500).to_bytes(2, 'little')
        plain, taxed = Chain(11), Chain(11, program=pm.TOKEN_2022, mint_extra=bytes(165 - 82) + bytes(ext))
        self.assertEqual(pm.mint_facts(taxed.mint_account), (6, 500))
        self.assertEqual(pm.mint_facts(plain.mint_account), (6, 0))
        marks = {}
        for name, chain in (('plain', plain), ('taxed', taxed)):
            positions = {str(chain.mint): chain.position()}
            got, _, errors = pm.marks_from_result(positions, pm.request_keys(positions), reply([chain], {str(chain.mint): (10 ** 12, 10 ** 12)}),
                                                  self.cfg, pool_fee_bps=Decimal(25))
            self.assertEqual(errors, {}, name)
            marks[name] = Decimal(got[str(chain.mint)]['value_sol'])
        self.assertLess(marks['taxed'], marks['plain'])

    def test_event_binding_and_staleness(self):
        marks, slot, _ = self.parse()
        event = pm.build_event(T, T - 3, slot, 'a' * 64, marks)
        pm.validate_event(event)
        for mutate in (lambda e: e.update(ts=T + 11), lambda e: e.update(observed_at=T + 1), lambda e: e.update(slot=True),
                       lambda e: e.update(source='x'), lambda e: e.update(extra=1), lambda e: e.update(evidence_hash='z'),
                       lambda e: e['marks'][sorted(e['marks'])[0]].update(value_sol='NaN'),
                       lambda e: e['marks'][sorted(e['marks'])[0]].update(value_sol='1E+99'),
                       lambda e: e['marks'][sorted(e['marks'])[0]].update(value_sol='-1'),
                       lambda e: e.update(marks={})):
            bad = copy.deepcopy(event)
            mutate(bad)
            if 'extra' not in bad:       # a forger recomputes the content hash; the shape/range/age checks must still refuse
                bad['event_id'] = 'paper-marks:' + digest({k: v for k, v in bad.items() if k != 'event_id'})
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                pm.validate_event(bad)
        tampered = copy.deepcopy(event)
        tampered['marks'][sorted(tampered['marks'])[0]]['value_sol'] = '99'
        with self.assertRaisesRegex(ValueError, 'hash'):
            pm.validate_event(tampered)

    def test_flag_validation_fails_closed(self):
        self.assertEqual(pm.selected(config()), 0)
        self.assertEqual(pm.selected(self.cfg), 1)
        for bad in ({pm.KEY: 2}, {pm.KEY: True}, {pm.KEY: '1'}, {pm.FEE_KEY: 25}, {pm.FEE_KEY: 'NaN'}, {pm.FEE_KEY: '-1'},
                    {pm.FEE_KEY: '10000'}, {'paper_concurrent_entries_version': None}, {'paper_quote_execution_version': 0},
                    {'mode': 'live'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                pm.selected({**self.cfg, **bad})
        no_fee = dict(self.cfg)
        del no_fee[pm.FEE_KEY]
        with self.assertRaises(ValueError):
            pm.selected(no_fee)


class EngineTests(unittest.TestCase):
    """The marks event is VALUATION ONLY."""
    def setUp(self):
        self.cfg = cfg_marks()
        self.state = initial_state(self.cfg)
        self.state['last_ts'] = T
        self.state['day'] = '2026-10-07'
        self.chains = {m: Chain(s) for m, s in (('A', 7), ('B', 8))}
        for name, chain in self.chains.items():
            self.state['positions'][str(chain.mint)] = chain.position(mark_at=T, mark_value='0.1')
        self.mints = sorted(self.state['positions'])

    def event(self, ts=T + 20, observed=None, value='0.07', mints=None):
        marks = {m: {'value_sol': value, 'pool': self.state['positions'][m]['pool'], 'base_raw': '1', 'quote_raw': '1'}
                 for m in (mints or self.mints)}
        return pm.build_event(ts, ts - 2 if observed is None else observed, 777, 'b' * 64, marks)

    def apply(self, event, cfg=None, state=None):
        return transition(copy.deepcopy(state or self.state), event, cfg or self.cfg)

    def test_valuation_only_fields_change(self):
        state, out = self.apply(self.event())
        for mint in self.mints:
            before, after = self.state['positions'][mint], state['positions'][mint]
            self.assertEqual({k: v for k, v in after.items() if not k.startswith('portfolio_mark')}, before)
            self.assertEqual((after['portfolio_mark_value'], after['portfolio_mark_at'], after['portfolio_mark_source']),
                             ('0.07', T + 18, pm.SOURCE))
        self.assertEqual(out[-1], {'type': 'portfolio_marks', 'source': pm.SOURCE, 'slot': 777, 'applied': self.mints})
        self.assertEqual(state['cash'], self.state['cash'])
        self.assertFalse([o for o in out if o['type'] in ('fill', 'blocked_exit')])

    def test_marks_satisfy_portfolio_valuation_but_never_an_exit_side_check(self):
        state, _ = self.apply(self.event())
        self.assertEqual(engine.equity(state), Decimal(state['cash']) + Decimal('0.14'))
        for p in state['positions'].values():
            self.assertEqual(engine.valuation(p), (Decimal('0.07'), T + 18))
            self.assertEqual(p['mark_at'], T)                      # the executable mark and its age are untouched
        # the stale-mark watchdog (desk.monitor / engine clock branch) reads mark_at, which is still the old executable mark
        ttl = self.cfg['price_ttl_seconds']
        self.assertTrue(all(not 0 <= T + 21 - p['mark_at'] <= ttl for p in state['positions'].values()))

    def test_a_blocked_exit_is_not_cured_and_an_older_observation_never_replaces_a_newer(self):
        blocked = copy.deepcopy(self.state)
        blocked['positions'][self.mints[0]].update(exit_blocked='EXACT_FRESH_SELL_QUOTE_REQUIRED', mark_status='UNVERIFIED_EXIT')
        state, _ = self.apply(self.event(), state=blocked)
        self.assertEqual(state['positions'][self.mints[0]]['exit_blocked'], 'EXACT_FRESH_SELL_QUOTE_REQUIRED')
        self.assertFalse(engine._marks_current(state, self.cfg, T + 20))      # still not current: no rollover/entry waiver
        newer, _ = self.apply(self.event(ts=T + 30, value='0.05'))
        stale_replay, _ = transition(copy.deepcopy(newer), self.event(ts=T + 31, observed=T + 25, value='0.01'), self.cfg)
        self.assertEqual(stale_replay['positions'][self.mints[0]]['portfolio_mark_value'], '0.05')

    def test_flag_absent_rejects_the_event_kind_exactly_as_before(self):
        plain = {k: v for k, v in self.cfg.items() if k not in (pm.KEY, pm.FEE_KEY)}
        with self.assertRaisesRegex(ValueError, 'unknown event kind'):
            self.apply(self.event(), cfg=plain)

    def test_out_of_order_and_unknown_position_and_tamper(self):
        _, out = self.apply(self.event(ts=T - 1, observed=T - 2))
        self.assertEqual(out, [{'type': 'reject', 'reason': 'OUT_OF_ORDER'}])
        event = self.event(mints=self.mints[:1])
        foreign = pm.build_event(T + 20, T + 18, 5, 'c' * 64, {'unknown': {'value_sol': '1', 'pool': 'p', 'base_raw': '1', 'quote_raw': '1'}})
        state, out = self.apply(foreign)
        self.assertEqual(out[0]['reason'], 'PORTFOLIO_MARK_POSITION_MISMATCH')
        self.assertNotIn('portfolio_mark_at', state['positions'][self.mints[0]])
        bad = copy.deepcopy(event)
        bad['slot'] += 1
        with self.assertRaises(ValueError):
            self.apply(bad)

    def test_entry_gate_uses_fresh_portfolio_marks_and_still_rejects_stale_ones(self):
        from tests.helpers import event as market_event
        market = lambda ts: market_event(ts, mint='SYNTHETIC_NEW', graduated_at=ts - 600)
        # Without marks the 20 s old executable marks fail the 10 s rule; with fresh portfolio marks they do not.
        stale = copy.deepcopy(self.state)
        refused = engine.valuation(stale['positions'][self.mints[0]])[1]
        self.assertGreater(T + 20 - refused, self.cfg['price_ttl_seconds'])
        fresh, _ = self.apply(self.event())
        for p in fresh['positions'].values():
            self.assertLessEqual(T + 21 - engine.valuation(p)[1], self.cfg['price_ttl_seconds'])
        aged, _ = self.apply(self.event(ts=T + 20, observed=T + 12))
        for p in aged['positions'].values():
            self.assertGreater(T + 25 - engine.valuation(p)[1], self.cfg['price_ttl_seconds'])    # 13 s old: stale again

    def test_rollover_requires_every_valuation_mark_current(self):
        state = copy.deepcopy(self.state)
        state['day'] = '2000-01-01'
        done, out = self.apply(self.event(), state=state)
        self.assertNotEqual(done['day'], '2000-01-01')
        self.assertEqual(done['day_gross_losses'], '0')
        partial, out = self.apply(self.event(mints=self.mints[:1]), state=state)
        self.assertEqual(partial['day'], '2000-01-01')
        self.assertTrue(any(o.get('reason') == 'DAY_ROLLOVER_DEFERRED_UNVERIFIED_MARKS' for o in out))

    def test_every_event_is_replay_deterministic(self):
        first, _ = self.apply(self.event())
        second, _ = self.apply(self.event())
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))




from tests import test_paper_concurrency as _tpc


class MonitorServiceMarksTests(_tpc.MonitorLegTests):
    """The held pass starts with ONE positions-less marks cycle, then the exit legs run exactly as before."""

    def test_refresh_runs_first_with_no_positions_then_every_leg(self):
        code, seen, _ = self.run_service(cfg_marks(), wall=None)
        self.assertEqual([len(t['position_targets']) for t in seen], [0, 1, 1, 1])
        self.assertEqual(code, 0)

    def test_a_failed_refresh_never_stops_the_exit_legs(self):
        code, seen, _ = self.run_service(cfg_marks(), wall=None, codes=[2])
        self.assertEqual([len(t['position_targets']) for t in seen], [0, 1, 1, 1])
        self.assertEqual(code, 0)           # the refresh code is not an exit-leg code

    def test_flag_absent_and_single_position_runs_are_unchanged(self):
        plain = {k: v for k, v in cfg_marks().items() if k not in (pm.KEY, pm.FEE_KEY)}
        plain['paper_portfolio_mark_ttl_seconds'] = 120
        _, seen, _ = self.run_service(plain, wall=None)
        self.assertEqual([len(t['position_targets']) for t in seen], [1, 1, 1])


for _name in dir(_tpc.MonitorLegTests):
    if _name.startswith('test_') and _name not in vars(MonitorServiceMarksTests):
        setattr(MonitorServiceMarksTests, _name, None)

if __name__ == '__main__':
    unittest.main()
