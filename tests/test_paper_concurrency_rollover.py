"""SYNTHETIC_TEST_ONLY: T16H item 2/3. Day rollover with 3 open positions and NO relaxed portfolio TTL (10 s price TTL).

With T35's batched portfolio marks every position's valuation mark is fresh at the start of a held pass (or checkpoint),
so the day rolls over there, in every mode, whether or not a candidate exists, and without
`paper_portfolio_mark_ttl_seconds`.
"""
import copy
import unittest

from desk import engine, paper_concurrency as pc, portfolio_marks as pm
from desk.engine import initial_state, transition
from tests.helpers import T, config
from tests.test_portfolio_marks import Chain, cfg_marks

OLD_DAY = '2000-01-01'


class RolloverWithoutTtl(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_marks(max_positions=3)
        self.assertNotIn(engine.PORTFOLIO_TTL_KEY, self.cfg)
        self.assertEqual(engine.portfolio_ttl(self.cfg), self.cfg['price_ttl_seconds'])
        self.state = initial_state(self.cfg)
        self.state.update(last_ts=T, day=OLD_DAY, day_gross_losses='0.4')
        chains = [Chain(seed) for seed in (7, 8, 9)]
        for i, chain in enumerate(chains):
            self.state['positions'][str(chain.mint)] = chain.position(mark_at=T - 600 + 60 * i, mark_value='0.1')
        self.mints = sorted(self.state['positions'])

    def marks(self, ts, mints=None, observed=None, value='0.09'):
        marks = {m: {'value_sol': value, 'pool': self.state['positions'][m]['pool'], 'base_raw': '1', 'quote_raw': '1'}
                 for m in (mints or self.mints)}
        return pm.build_event(ts, ts - 2 if observed is None else observed, 42, 'd' * 64, marks)

    def roll(self, **changes):
        state = copy.deepcopy(self.state)
        state.update(changes)
        return transition(state, self.marks(T + 20), self.cfg)

    def test_the_flag_validates_without_the_ttl_key(self):
        self.assertEqual(pc.selected(self.cfg), 1)
        self.assertEqual(pc.selected({**self.cfg, 'paper_rollover_after_mark_version': 1}), 1)

    def test_book_full_with_no_candidate(self):
        self.assertEqual(len(self.state['positions']), self.cfg['max_positions'])      # no room for a new entry
        state, out = self.roll()
        self.assertNotEqual(state['day'], OLD_DAY)
        self.assertEqual(state['day_gross_losses'], '0')
        self.assertEqual(state['day_start_equity'], str(engine.equity(state)))
        self.assertFalse(any(o.get('reason') == 'DAY_ROLLOVER_DEFERRED_UNVERIFIED_MARKS' for o in out))

    def test_exit_only_mode(self):
        state, _ = self.roll(mode='EXIT_ONLY')
        self.assertNotEqual(state['day'], OLD_DAY)
        self.assertEqual(state['mode'], 'EXIT_ONLY')

    def test_ledger_with_room_and_running_mode(self):
        self.cfg['max_positions'] = 4
        state, _ = transition(copy.deepcopy(self.state), self.marks(T + 20), self.cfg)
        self.assertNotEqual(state['day'], OLD_DAY)

    def test_a_missing_position_defers_the_rollover_and_a_stale_observation_is_refused(self):
        state, out = transition(copy.deepcopy(self.state), self.marks(T + 20, self.mints[:2]), self.cfg)
        self.assertEqual((state['day'], state['day_gross_losses']), (OLD_DAY, '0.4'))
        self.assertTrue(any(o.get('reason') == 'DAY_ROLLOVER_DEFERRED_UNVERIFIED_MARKS' for o in out))
        with self.assertRaises(ValueError):                       # 12 s old: the event itself is refused, nothing moves
            transition(copy.deepcopy(self.state), self.marks(T + 20, observed=T + 8), self.cfg)

    def test_a_blocked_exit_defers_the_rollover(self):
        self.state['positions'][self.mints[0]].update(exit_blocked='EXACT_FRESH_SELL_QUOTE_REQUIRED', mark_status='UNVERIFIED_EXIT')
        state, _ = self.roll()
        self.assertEqual(state['day'], OLD_DAY)

    def test_three_sequential_legs_8s_apart_cannot_satisfy_the_10s_rule_but_the_marks_event_does(self):
        # Why the rule rests on the batched marks: the first leg's mark is 16 s old when the third leg completes.
        self.assertGreater(2 * 8, self.cfg['price_ttl_seconds'])
        state, _ = self.roll()
        self.assertNotEqual(state['day'], OLD_DAY)


if __name__ == '__main__':
    unittest.main()
