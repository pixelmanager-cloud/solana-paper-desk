"""SYNTHETIC_TEST_ONLY: T35F. Guarded risk equity (source version 2), one rollover rule, closed vaults, flag-absent identity."""
import copy
import json
import re
import unittest
from decimal import Decimal
from pathlib import Path

from desk import engine, portfolio_marks as pm
from desk.engine import initial_state, transition
from tests.golden import off_identity_scenario
from tests.helpers import T, config
from tests.test_portfolio_marks import Chain, cfg_marks, reply

GOLDEN = Path(__file__).parent / 'golden' / 'portfolio_marks_off_identity.json'


def book(cfg, executable='1.0', cash='3', day='2026-10-07'):
    state = initial_state(cfg)
    state.update(last_ts=T, day=day, cash=cash, peak_equity='5', day_start_equity='5')
    for seed in (7, 8):
        chain = Chain(seed)
        state['positions'][str(chain.mint)] = chain.position(mark_at=T, mark_value=executable, cost_left='1.0', initial_cost='1.0')
    return state


def marks_event(state, value, ts=T + 5, observed=None, **extra):
    marks = {m: {'value_sol': value, 'pool': p['pool'], 'base_raw': '1', 'quote_raw': '1', **extra}
             for m, p in state['positions'].items()}
    return pm.build_event(ts, ts - 2 if observed is None else observed, 9, 'e' * 64, marks)


class GuardConfigTests(unittest.TestCase):
    def test_versions_and_divergence_validation(self):
        self.assertEqual(pm.selected(cfg_marks()), 1)
        v2 = cfg_marks(**{pm.KEY: 2})
        self.assertEqual(pm.selected(v2), 2)
        self.assertEqual(pm.max_divergence(v2), Decimal('0.03'))
        self.assertEqual(pm.max_divergence(cfg_marks(**{pm.KEY: 2, pm.DIVERGENCE_KEY: '0.1'})), Decimal('0.1'))
        self.assertIsNone(pm.max_divergence(cfg_marks()))
        for bad in ('0', '1', '-0.1', 'NaN', '0.03' * 10, 0.03, 3, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pm.selected(cfg_marks(**{pm.KEY: 2, pm.DIVERGENCE_KEY: bad}))
        with self.assertRaises(ValueError):                      # the bound is meaningless (and refused) for version 1
            pm.selected(cfg_marks(**{pm.DIVERGENCE_KEY: '0.03'}))
        with self.assertRaises(ValueError):
            pm.selected({**{k: v for k, v in cfg_marks().items() if k != pm.KEY}, pm.DIVERGENCE_KEY: '0.03'})


class GuardedValuationTests(unittest.TestCase):
    def test_model_counts_only_within_the_divergence_of_the_executable_mark(self):
        pos = lambda model: {'mark_value': '1.0', 'mark_at': T, 'portfolio_mark_value': model, 'portfolio_mark_at': T + 5}
        guard = Decimal('0.03')
        self.assertEqual(engine.valuation(pos('1.03'), guard), (Decimal('1.03'), T + 5))      # exactly at the bound: model
        self.assertEqual(engine.valuation(pos('0.97'), guard), (Decimal('0.97'), T + 5))
        self.assertEqual(engine.valuation(pos('1.0301'), guard), (Decimal('1.0'), T + 5))     # beyond: executable value,
        self.assertEqual(engine.valuation(pos('0.9699'), guard), (Decimal('1.0'), T + 5))     # but the fresh timestamp stays
        self.assertEqual(engine.valuation(pos('0.5'), None), (Decimal('0.5'), T + 5))         # unguarded (version 1 / sizing)
        zero = {**pos('0.1'), 'mark_value': '0'}
        self.assertEqual(engine.valuation(zero, guard)[0], 0)                                   # no executable basis: executable
        old = {**pos('0.5'), 'portfolio_mark_at': T - 1}
        self.assertEqual(engine.valuation(old, guard), (Decimal('1.0'), T))                    # older than the executable mark


class GuardedRiskTests(unittest.TestCase):
    def apply(self, cfg, state, value):
        return transition(copy.deepcopy(state), marks_event(state, value), cfg)

    def test_a_divergent_low_model_mark_cannot_liquidate_under_version_2_but_can_under_version_1(self):
        v1, v2 = cfg_marks(), cfg_marks(**{pm.KEY: 2})
        state = book(v1)
        liquidated, out = self.apply(v1, state, '0')
        self.assertEqual(liquidated['mode'], 'LIQUIDATING')
        self.assertTrue(any(o.get('reason') == 'DAILY_LIQUIDATE' for o in out))
        guarded, out = self.apply(v2, state, '0')
        self.assertEqual(guarded['mode'], 'RUNNING')
        self.assertFalse(any(o.get('reason') == 'DAILY_LIQUIDATE' for o in out))

    def test_a_fresh_model_mark_still_satisfies_the_freshness_gates_but_not_the_risk_equity(self):
        v2 = cfg_marks(**{pm.KEY: 2})
        state, _ = self.apply(v2, book(v2), '0')
        for p in state['positions'].values():
            self.assertEqual(p['portfolio_mark_at'], T + 3)
        self.assertTrue(engine._marks_current(state, v2, T + 5))
        self.assertEqual(engine.equity(state), Decimal('3'))                                    # model-fed (limits/sizing)
        self.assertEqual(engine.equity(state, guard=engine.mark_guard(v2)), Decimal('5'))      # risk equity: executable

    def test_peak_equity_is_not_inflated_by_a_divergent_high_model_mark(self):
        v1, v2 = cfg_marks(), cfg_marks(**{pm.KEY: 2})
        high1, _ = self.apply(v1, book(v1), '3.0')
        high2, _ = self.apply(v2, book(v2), '3.0')
        self.assertEqual(Decimal(high1['peak_equity']), Decimal('9'))
        self.assertEqual(Decimal(high2['peak_equity']), Decimal('5'))
        within, _ = self.apply(v2, book(v2), '1.02')
        self.assertEqual(Decimal(within['peak_equity']), Decimal('5.04'))                      # within 3 %: the model counts

    def test_day_start_equity_after_rollover_uses_the_guarded_value(self):
        v2 = cfg_marks(**{pm.KEY: 2, 'paper_rollover_after_mark_version': 1})
        state = book(v2, day='2000-01-01')
        rolled, _ = self.apply(v2, state, '3.0')
        self.assertNotEqual(rolled['day'], '2000-01-01')
        self.assertEqual(Decimal(rolled['day_start_equity']), Decimal('5'))                     # not 9
        v1 = cfg_marks(paper_rollover_after_mark_version=1)
        self.assertEqual(Decimal(self.apply(v1, book(v1, day='2000-01-01'), '3.0')[0]['day_start_equity']), Decimal('9'))

    def test_executable_decline_still_liquidates_under_version_2(self):
        v2 = cfg_marks(**{pm.KEY: 2})
        state = book(v2, executable='0.2')                       # the executable marks fell: equity 3.4 vs day start 5
        state['peak_equity'] = state['day_start_equity'] = '5'
        out_state, out = self.apply(v2, state, '0.2')
        self.assertEqual(out_state['mode'], 'LIQUIDATING')


class OneRolloverRuleTests(unittest.TestCase):
    def test_engine_has_exactly_one_place_that_rolls_the_day(self):
        text = (Path(__file__).resolve().parents[1] / 'desk' / 'engine.py').read_text()
        self.assertEqual(len(re.findall(r'state\["day"\] = day', text)), 1)
        self.assertEqual(len(re.findall(r'_roll_day\(', text)), 4)                              # def + three call sites

    def test_marks_event_rolls_only_under_the_single_versioned_flag(self):
        plain, flagged = cfg_marks(), cfg_marks(paper_rollover_after_mark_version=1)
        state = book(plain, day='2000-01-01')
        off, out = transition(copy.deepcopy(state), marks_event(state, '1.0'), plain)
        self.assertEqual(off['day'], '2000-01-01')
        self.assertFalse(any('ROLLOVER' in str(o.get('reason')) for o in out))
        on, _ = transition(copy.deepcopy(state), marks_event(state, '1.0'), flagged)
        self.assertNotEqual(on['day'], '2000-01-01')


class ClosedVaultTests(unittest.TestCase):
    """A null pool/vault account is one OBSERVATION; the position is valued at zero (reason *_CLOSED) only after two
    consecutive distinct observations (T16I: a transient null must not zero a healthy position)."""
    def setUp(self):
        self.cfg = cfg_marks()
        self.chains = sorted([Chain(7), Chain(8)], key=lambda c: str(c.mint))
        self.positions = {str(c.mint): c.position() for c in self.chains}
        self.amounts = {str(c.mint): (10 ** 12, 10 ** 12) for c in self.chains}

    def parse(self, response):
        return pm.marks_from_result(self.positions, pm.request_keys(self.positions), response, self.cfg, pool_fee_bps=Decimal(25))

    def test_a_null_account_is_reported_as_a_null_observation_with_a_reason(self):
        for index, reason in ((1, 'VAULT_NULL'), (2, 'VAULT_NULL'), (0, 'POOL_NULL')):
            response = reply(self.chains, self.amounts)
            response['value'][index] = None
            marks, slot, errors = self.parse(response)
            victim = str(self.chains[0].mint)
            self.assertEqual(errors, {}, reason)
            self.assertEqual(marks[victim], {'value_sol': '0', 'pool': self.positions[victim]['pool'], 'base_raw': '0',
                                             'quote_raw': '0', 'reason': reason})
            self.assertGreater(Decimal(marks[str(self.chains[1].mint)]['value_sol']), 0)       # the other position is unaffected
            pm.validate_event(pm.build_event(T, T - 1, slot, 'f' * 64, marks))

    def test_the_event_cannot_claim_closure_itself_and_a_reason_needs_a_zero_value(self):
        response = reply(self.chains, self.amounts)
        response['value'][1] = None
        marks, slot, _ = self.parse(response)
        victim = str(self.chains[0].mint)
        for change in ({'value_sol': '0.5'}, {'reason': 'WHATEVER'}, {'reason': 'VAULT_CLOSED'}, {'reason': 'POOL_CLOSED'}):
            bad = copy.deepcopy(marks)
            bad[victim].update(change)
            with self.assertRaises(ValueError, msg=str(change)):
                pm.validate_event(pm.build_event(T, T - 1, slot, 'f' * 64, bad))

    def observation(self, state, ts, reason='VAULT_NULL', only=0):
        mints = sorted(state['positions'])
        marks = {m: {'value_sol': '0.9', 'pool': state['positions'][m]['pool'], 'base_raw': '1', 'quote_raw': '1'} for m in mints}
        marks[mints[only]] = {'value_sol': '0', 'pool': state['positions'][mints[only]]['pool'], 'base_raw': '0', 'quote_raw': '0',
                              'reason': reason}
        return pm.build_event(ts + 2, ts, 9, 'e' * 64, marks)

    def test_closure_needs_two_consecutive_distinct_observations(self):
        cfg = cfg_marks()
        state = book(cfg)
        state['positions'] = {m: {**p, 'pool': self.positions[m]['pool']} for m, p in zip(sorted(self.positions), state['positions'].values())}
        victim = sorted(state['positions'])[0]
        first, out = transition(copy.deepcopy(state), self.observation(state, T + 3), cfg)
        p = first['positions'][victim]
        self.assertEqual((p['portfolio_mark_nulls'], 'portfolio_mark_value' in p), (1, False))     # unconfirmed: not valued at zero
        self.assertEqual(out[-1]['unconfirmed_null'], [victim])
        replay, _ = transition(copy.deepcopy(first), self.observation(state, T + 3), cfg)           # the SAME observation again
        self.assertEqual(replay['positions'][victim]['portfolio_mark_nulls'], 1)
        second, out = transition(copy.deepcopy(first), self.observation(state, T + 6), cfg)
        closed = second['positions'][victim]
        self.assertEqual((closed['portfolio_mark_value'], closed['portfolio_mark_reason']), ('0', 'VAULT_CLOSED'))
        self.assertEqual(closed['mark_value'], '1.0')                      # the executable mark is untouched
        self.assertEqual(engine.valuation(closed), (Decimal('0'), T + 6))
        pool_case, _ = transition(copy.deepcopy(first), self.observation(state, T + 6, 'POOL_NULL'), cfg)
        self.assertEqual(pool_case['positions'][victim]['portfolio_mark_reason'], 'POOL_CLOSED')

    def test_an_ordinary_answer_in_between_resets_the_count(self):
        cfg = cfg_marks()
        state = book(cfg)
        state['positions'] = {m: {**p, 'pool': self.positions[m]['pool']} for m, p in zip(sorted(self.positions), state['positions'].values())}
        victim = sorted(state['positions'])[0]
        first, _ = transition(copy.deepcopy(state), self.observation(state, T + 3), cfg)
        healthy = pm.build_event(T + 8, T + 6, 9, 'e' * 64, {m: {'value_sol': '0.9', 'pool': state['positions'][m]['pool'],
                                 'base_raw': '1', 'quote_raw': '1'} for m in state['positions']})
        reset, _ = transition(copy.deepcopy(first), healthy, cfg)
        self.assertNotIn('portfolio_mark_nulls', reset['positions'][victim])
        again, _ = transition(copy.deepcopy(reset), self.observation(state, T + 10), cfg)
        self.assertEqual(again['positions'][victim]['portfolio_mark_nulls'], 1)                   # starts over
        self.assertNotIn('portfolio_mark_reason', again['positions'][victim])


class FlagAbsentIdentityTests(unittest.TestCase):
    def test_flag_absent_engine_output_is_byte_identical_to_integration_r1(self):
        """The golden was produced by the same scenario run on origin/integration/r1 (worktree), before this branch."""
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual(off_identity_scenario.run(), golden)
        self.assertEqual(golden['extra_position_keys'], [])

    def test_golden_has_the_expected_shape(self):
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual(sorted(golden), ['cash', 'day', 'day_start_equity', 'extra_position_keys', 'outcome_count',
                                          'outcomes_digest', 'positions', 'state_digest'])


if __name__ == '__main__':
    unittest.main()


class MarksReadDeadlineTests(unittest.TestCase):
    """T35F item 3: the marks read lives INSIDE the 10 s cycle budget; it never extends it."""
    def setUp(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        self.SimpleNamespace, self.MagicMock = SimpleNamespace, MagicMock
        self.cfg = cfg_marks()
        self.chain = Chain(7)
        self.positions = {str(self.chain.mint): self.chain.position()}

    def run_refresh(self, remaining, read_ok=True):
        from unittest.mock import patch
        from desk import paper_cycle as cycle
        budget = self.SimpleNamespace(start=100.0, monitoring_attempted=0, remaining=lambda: remaining,
                                      now=lambda: T + 5, calls=[])
        source = self.MagicMock()
        keys = pm.request_keys(self.positions)
        reply_value = reply([self.chain], {str(self.chain.mint): (10 ** 12, 10 ** 12)})
        source.rpc_with_evidence.side_effect = lambda m, p, timeout_seconds: (reply_value, 'a' * 64)
        class Allowance:
            used = 0
            def __init__(self, *a, **k):
                pass
            def snapshot(self):
                return {'blockers': [], 'total_used': Allowance.used}
        store = self.MagicMock()
        store.load.return_value = {'observed_at': T + 3}
        delivered, result = [], {'diagnostics': []}

        def charge(*a, **k):
            Allowance.used += 1
            return reply_value, 'a' * 64
        source.rpc_with_evidence.side_effect = charge
        with patch.object(cycle, '_state', return_value={'positions': self.positions}), \
                patch.object(cycle, 'MonitoringBudget', Allowance), patch.object(cycle, '_entry_scan_id', return_value='s'):
            cycle._refresh_portfolio_marks("p", self.cfg, store, self.MagicMock(), lambda *a, **k: source, budget,
                                           delivered.append, lambda: T + 5, result)
        return budget, source, delivered, result, keys

    def test_too_little_time_left_skips_the_read_without_charging_or_touching_the_deadline(self):
        budget, source, delivered, result, _ = self.run_refresh(remaining=2.0)
        self.assertEqual(result['diagnostics'], [{'portfolio_marks': 'UNAVAILABLE', 'code': 'PORTFOLIO_MARKS_DEADLINE'}])
        source.rpc_with_evidence.assert_not_called()
        self.assertEqual((budget.start, budget.monitoring_attempted, delivered), (100.0, 0, []))

    def test_the_read_is_bounded_by_the_remaining_budget_and_never_extends_it(self):
        for remaining, expected in ((9.0, 5.0), (4.0, 4.0), (3.0, 3.0)):
            budget, source, delivered, result, _ = self.run_refresh(remaining=remaining)
            self.assertEqual(source.rpc_with_evidence.call_args.kwargs['timeout_seconds'], expected, remaining)
            self.assertEqual(budget.start, 100.0, 'the 10 s cycle deadline is not moved by the read')
            self.assertEqual((budget.monitoring_attempted, len(delivered)), (1, 1))
            self.assertEqual(result['diagnostics'][-1]['portfolio_marks'], 'APPLIED')
