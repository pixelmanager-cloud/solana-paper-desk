"""SYNTHETIC_TEST_ONLY: opt-in portfolio-level risk gates (paper_portfolio_risk_version 1).

Real engine and ledger, config/paper.json plus the four explicit portfolio parameters, synthetic events. No provider,
network or store outside a temp directory. With the flag absent the engine must behave exactly as before.
"""
import ast
import copy
import tempfile
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from desk import engine
from desk.engine import initial_state, portfolio_blockers, portfolio_open_risk, transition
from desk.ledger import Ledger
from desk.model import digest
from tests.helpers import T, config, control, event

KEY = 'paper_portfolio_risk_version'
ALL = ('portfolio_max_open_risk_fraction', 'portfolio_loss_streak_cooldown_after',
       'portfolio_cooldown_minutes', 'portfolio_max_entries_per_10m')
# Digest of scenario() on the engine BEFORE this task's change (r1 + T16 + T23 base), flag absent.
BASE_DIGEST = '765bd69994735e4d53c875509153dd4afae696821986596ae7b61b20c505f494'


def cfg_on(**changes):
    values = {KEY: 1, 'portfolio_max_open_risk_fraction': '0.5', 'portfolio_loss_streak_cooldown_after': 100,
              'portfolio_cooldown_minutes': 1, 'portfolio_max_entries_per_10m': 100}
    values.update(changes)
    return {**config(), **values}


def scenario(cfg):
    """Fixed multi-position sequence: entries, throttle reject, stop and danger exits, controls, a clock expiry."""
    state, log = initial_state(cfg), []

    def go(e):
        nonlocal state
        state, out = transition(state, e, cfg)
        log.append(copy.deepcopy(out))
    go(event(T, mint='A'))
    go(event(T + 59, mint='A'))
    go(event(T + 60, mint='B'))
    go(event(T + 61, mint='A'))
    go(event(T + 61, mint='B'))
    go(event(T + 61, mint='C'))
    go(event(T + 119, mint='A'))
    go(event(T + 119, mint='B'))
    go(event(T + 120, mint='C'))
    go(event(T + 130, mint='A', reserve_sol='40'))
    go(event(T + 135, mint='B', danger=True))
    go(control(T + 140, 'PAUSE_ENTRY'))
    go(control(T + 150, 'RESUME'))
    go(event(T + 399, mint='C'))
    go(event(T + 400, mint='D'))
    go({'schema_version': 1, 'event_id': 'clock:1', 'ts': T + 500, 'kind': 'clock', 'actor': 'paper_monitor'})
    return state, log


class Run:
    """Drives the real transition on one state. `held` marks are refreshed one second before each entry."""

    def __init__(self, cfg):
        self.cfg, self.state, self.held = cfg, initial_state(cfg), []

    def go(self, e):
        self.state, out = transition(self.state, e, self.cfg)
        return out

    def enter(self, mint, at):
        for h in list(self.state['positions']):
            self.go(event(at - 1, mint=h))
        out = self.go(event(at, mint=mint))
        return out[0]

    def lose(self, mint, at):
        return self.go(event(at, mint=mint, reserve_sol='40'))[0]            # STOP

    def lose_danger(self, mint, at):
        return self.go(event(at, mint=mint, danger=True))[0]                 # DANGER exit at the flat price: fees -> loss

    def win(self, mint, at):
        return self.go(event(at, mint=mint, danger=True, reserve_sol='160'))[0]


def position(**changes):
    p = {'qty': '1', 'initial_qty': '1', 'cost_left': '0.1', 'initial_cost': '0.1', 'trade_pnl': '0', 'opened_at': T,
         'exit_blocked': None, 'mark_status': 'MODEL_ESTIMATE', 'stage': 0, 'stop_ratio': '0.82', 'peak_ratio': '1',
         'touched_15': False, 'mark_value': '0.1', 'mark_at': T, 'pool': 'P', 'entry_scores': {},
         'provenance': 'SYNTHETIC_TEST_ONLY', 'taker': None}
    p.update(changes)
    return p


class DefaultOffTests(unittest.TestCase):
    def test_flag_absent_is_byte_identical_to_the_previous_engine(self):
        state, log = scenario(config())
        self.assertEqual(digest([state, log]), BASE_DIGEST)
        self.assertFalse({'portfolio_entries', 'portfolio_cooldown_until'} & set(state))
        self.assertEqual(sorted(state['positions']), ['C', 'D'])
        self.assertEqual(state['loss_streak'], 2)

    def test_portfolio_helpers_are_inert_without_the_flag(self):
        self.assertEqual(portfolio_blockers({'positions': {}}, config(), T), [])
        engine_state = initial_state(config())
        self.assertIsNone(engine._portfolio_risk(config()))
        engine._portfolio_after_exit(dict(engine_state, loss_streak=99), config(), T)

    def test_permissive_parameters_change_no_decision_only_add_spacing_state(self):
        base_state, base_log = scenario(config())
        on_state, on_log = scenario(cfg_on())
        self.assertEqual(on_log, base_log)
        extra = set(on_state) - set(base_state)
        self.assertEqual(extra, {'portfolio_entries'})
        self.assertEqual({k: v for k, v in on_state.items() if k != 'portfolio_entries'}, base_state)


class ConfigValidationTests(unittest.TestCase):
    def test_parameters_without_the_flag_fail_closed(self):
        for key in ALL:
            with self.subTest(key), self.assertRaises(ValueError):
                transition(initial_state(config()), event(T), {**config(), key: 1 if key != ALL[0] else '0.1'})

    def test_bad_flag_values_and_incomplete_parameter_sets_fail_closed(self):
        bad = [cfg_on(**{KEY: 2}), cfg_on(**{KEY: '1'}), cfg_on(**{KEY: True}), cfg_on(**{KEY: 0}),
               cfg_on(portfolio_max_open_risk_fraction='0'), cfg_on(portfolio_max_open_risk_fraction='1'),
               cfg_on(portfolio_max_open_risk_fraction='-0.1'), cfg_on(portfolio_max_open_risk_fraction='x'),
               cfg_on(portfolio_max_open_risk_fraction=None), cfg_on(portfolio_loss_streak_cooldown_after=0),
               cfg_on(portfolio_loss_streak_cooldown_after=2.5), cfg_on(portfolio_loss_streak_cooldown_after=True),
               cfg_on(portfolio_loss_streak_cooldown_after=101), cfg_on(portfolio_cooldown_minutes=0),
               cfg_on(portfolio_cooldown_minutes=1441), cfg_on(portfolio_cooldown_minutes='30'),
               cfg_on(portfolio_max_entries_per_10m=0), cfg_on(portfolio_max_entries_per_10m=101),
               {**cfg_on(), 'mode': 'live'}]
        for cfg in bad:
            with self.subTest(cfg={k: cfg.get(k) for k in (KEY,) + ALL + ('mode',)}), self.assertRaises(ValueError):
                transition(initial_state(config()), event(T), cfg)
        for key in ALL:
            missing = cfg_on()
            del missing[key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                transition(initial_state(config()), event(T), missing)

    def test_valid_configuration_parses_exactly(self):
        pr = engine._portfolio_risk(cfg_on(portfolio_max_open_risk_fraction='0.03', portfolio_loss_streak_cooldown_after=3,
                                          portfolio_cooldown_minutes=30, portfolio_max_entries_per_10m=2))
        self.assertEqual(pr, {'fraction': D('0.03'), 'after': 3, 'seconds': 1800, 'entries': 2})

    def test_invalid_configuration_is_caught_even_by_a_control_event(self):
        with self.assertRaises(ValueError):
            transition(initial_state(config()), control(T, 'PAUSE_ENTRY'), cfg_on(portfolio_cooldown_minutes=0))


class AggregateOpenRiskTests(unittest.TestCase):
    def test_open_risk_is_cost_at_risk_times_distance_to_each_positions_own_stop(self):
        state = {'positions': {'A': position(), 'B': position(cost_left='0.05'), 'C': position(stop_ratio='1'),
                               'D': position(stop_ratio='1.4'), 'E': position(stop_ratio='0.9')}}
        # 0.1*0.18 + 0.05*0.18 + 0 + 0 + 0.1*0.10
        self.assertEqual(portfolio_open_risk(state), D('0.018') + D('0.009') + D('0.010'))
        self.assertEqual(portfolio_open_risk({'positions': {}}), D(0))

    def test_entries_are_clamped_to_the_remaining_risk_budget_then_refused(self):
        run = Run(cfg_on(portfolio_max_open_risk_fraction='0.005'))
        first = run.enter('A', T)
        self.assertEqual((first['type'], first['amount_sol']), ('fill', '0.083333333'))      # unclamped: risk 0.015 < 0.025
        fee, stop = D('0.00005'), D('0.18')
        for held in list(run.state['positions']):
            run.go(event(T + 59, mint=held))                          # fresh marks, as the entry path does before deciding
        # the limit is a fraction of the equity AT THE ENTRY DECISION (deterministic, before the new fill's costs)
        limit = D('0.005') * engine.equity(run.state)
        room = limit - portfolio_open_risk(run.state)
        expected = (room / stop - fee).quantize(D('.000000001'), rounding='ROUND_DOWN')
        second = run.go(event(T + 60, mint='B'))[0]
        self.assertEqual((second['type'], second['amount_sol']), ('fill', str(expected)))    # clamped, not refused
        self.assertLess(expected, D('0.083333333'))
        self.assertLessEqual(portfolio_open_risk(run.state), limit)
        self.assertGreater(portfolio_open_risk(run.state), limit - D('0.0000001'))             # budget used up (to rounding)
        third = run.enter('C', T + 120)
        self.assertEqual((third['type'], third['reason']), ('reject', 'PORTFOLIO_RISK_CAP'))
        self.assertEqual(set(third), {'type', 'reason', 'mint', 'size_sol', 'open_risk', 'limit'})
        self.assertEqual(sorted(run.state['positions']), ['A', 'B'])

    def test_room_between_zero_and_the_minimum_order_is_refused_as_a_risk_cap_not_a_size_error(self):
        run = Run(cfg_on(portfolio_max_open_risk_fraction='0.00311'))                          # room after A ~ 0.0005 SOL of risk
        self.assertEqual(run.enter('A', T)['type'], 'fill')
        for held in list(run.state['positions']):
            run.go(event(T + 59, mint=held))
        cap = engine._portfolio_amount_cap(run.state, run.cfg, engine._portfolio_risk(run.cfg))
        self.assertTrue(D(0) < cap < D(run.cfg['min_order_sol']), cap)
        reject = run.go(event(T + 60, mint='B'))[0]
        self.assertEqual((reject['type'], reject['reason']), ('reject', 'PORTFOLIO_RISK_CAP'))   # not BELOW_MINIMUM

    def test_risk_budget_returns_when_a_position_closes_and_its_risk_leaves_the_book(self):
        run = Run(cfg_on(portfolio_max_open_risk_fraction='0.005'))
        run.enter('A', T)
        run.enter('B', T + 60)
        run.win('A', T + 130)
        self.assertEqual(run.enter('C', T + 200)['type'], 'fill')

    def test_risk_stops_counting_once_a_rung_moves_the_stop_to_breakeven(self):
        run = Run(cfg_on(portfolio_max_open_risk_fraction='0.005'))
        run.enter('A', T)
        before = portfolio_open_risk(run.state)
        run.go(event(T + 30, mint='A', reserve_sol='160'))                                    # first take-profit rung
        position_a = run.state['positions']['A']
        self.assertEqual((position_a['stage'], position_a['stop_ratio']), (1, '1'))
        self.assertLess(portfolio_open_risk(run.state), before)
        self.assertEqual(portfolio_open_risk(run.state), D(0))

    def test_interaction_with_max_positions_precedence_and_exposure(self):
        run = Run(cfg_on(portfolio_max_entries_per_10m=1, max_positions=1))
        run.enter('A', T)
        reject = run.enter('B', T + 60)
        self.assertEqual(reject['reasons'][:1], ['MAX_POSITIONS'])
        self.assertIn('PORTFOLIO_ENTRY_SPACING', reject['reasons'])
        self.assertEqual(reject['reason'], 'MAX_POSITIONS')                                    # existing gates keep precedence
        # exposure limit (8% of equity) is applied by size() first; the risk clamp only ever lowers the amount
        free = Run(cfg_on())
        capped = Run(cfg_on(portfolio_max_open_risk_fraction='0.9'))
        self.assertEqual(free.enter('A', T)['amount_sol'], capped.enter('A', T)['amount_sol'])


class LossStreakCooldownTests(unittest.TestCase):
    def cfg(self, **changes):
        return cfg_on(portfolio_loss_streak_cooldown_after=2, portfolio_cooldown_minutes=30, **changes)

    def two_losses(self, run):
        run.enter('A', T)
        run.enter('B', T + 60)
        self.assertEqual(run.lose('A', T + 130)['reason'], 'STOP')
        self.assertEqual(run.lose_danger('B', T + 135)['reason'], 'DANGER')
        self.assertEqual(run.state['loss_streak'], 2)

    def test_cooldown_starts_at_the_streak_and_ends_exactly_after_m_minutes(self):
        run = Run(self.cfg())
        self.two_losses(run)
        until = T + 135 + 1800
        self.assertEqual(run.state['portfolio_cooldown_until'], until)
        for at in (T + 200, until - 1):
            reject = run.enter('C', at)
            self.assertEqual((reject['type'], reject['reason']), ('reject', 'PORTFOLIO_LOSS_COOLDOWN'), at)
        self.assertEqual(run.enter('C', until)['type'], 'fill')

    def test_one_loss_below_n_never_cools_down_and_a_win_resets_the_streak(self):
        run = Run(self.cfg())
        run.enter('A', T)
        run.enter('B', T + 60)
        run.lose('A', T + 130)
        self.assertNotIn('portfolio_cooldown_until', run.state)
        run.win('B', T + 135)
        self.assertEqual(run.state['loss_streak'], 0)
        self.assertNotIn('portfolio_cooldown_until', run.state)
        self.assertEqual(run.enter('C', T + 200)['type'], 'fill')

    def test_each_further_loss_at_or_above_n_renews_the_cooldown_from_that_exit(self):
        run = Run(self.cfg())
        self.two_losses(run)
        first_until = run.state['portfolio_cooldown_until']
        run.enter('C', first_until)
        later = first_until + 100
        run.go(event(later - 1, mint='C'))
        run.lose_danger('C', later)
        self.assertEqual(run.state['loss_streak'], 3)
        self.assertEqual(run.state['portfolio_cooldown_until'], later + 1800)

    def test_partial_exits_do_not_count_and_do_not_change_the_streak(self):
        run = Run(self.cfg())
        run.enter('A', T)
        run.go(event(T + 30, mint='A', reserve_sol='160'))                                    # partial take-profit
        self.assertIn('A', run.state['positions'])
        self.assertEqual(run.state['loss_streak'], 0)
        self.assertNotIn('portfolio_cooldown_until', run.state)

    def test_cooldown_does_not_block_exits_or_marks(self):
        run = Run(self.cfg())
        run.enter('A', T)
        run.enter('B', T + 60)
        run.lose('A', T + 130)
        run.lose_danger('B', T + 135)
        run.state['positions']['X'] = position(opened_at=T + 140, mark_at=T + 140)
        out = run.go(event(T + 150, mint='X', danger=True))
        self.assertEqual(out[0]['type'], 'fill')                                              # exits still work during cooldown

    def test_both_loss_streak_update_sites_call_the_portfolio_hook(self):
        """The quote-mode exit path needs heavy fixtures; assert structurally that every streak update is followed by the hook."""
        tree = ast.parse(Path(engine.__file__).read_text())
        updates = hooks = 0
        for node in ast.walk(tree):
            body = getattr(node, 'body', None)
            if not isinstance(body, list):
                continue
            for index, stmt in enumerate(body):
                if (isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Subscript)
                        and ast.unparse(stmt.targets[0]).replace("'", '"') == 'state["loss_streak"]'
                        and 'loss_streak' in ast.unparse(stmt.value)):
                    updates += 1
                    nxt = body[index + 1]
                    if isinstance(nxt, ast.Expr) and ast.unparse(nxt.value).startswith('_portfolio_after_exit(state, cfg, e'):
                        hooks += 1
        self.assertEqual((updates, hooks), (2, 2))


class EntrySpacingTests(unittest.TestCase):
    def test_at_most_k_entries_per_rolling_ten_minutes(self):
        run = Run(cfg_on(portfolio_max_entries_per_10m=2))
        self.assertEqual(run.enter('A', T)['type'], 'fill')
        self.assertEqual(run.enter('B', T + 60)['type'], 'fill')
        reject = run.enter('C', T + 120)
        self.assertEqual((reject['type'], reject['reason']), ('reject', 'PORTFOLIO_ENTRY_SPACING'))
        self.assertEqual(run.state['portfolio_entries'], [T, T + 60])                           # a refusal consumes nothing
        # A's entry is exactly 600 s old at T+600: outside the window, so only B counts
        self.assertEqual(run.enter('C', T + 599)['reason'], 'PORTFOLIO_ENTRY_SPACING')
        self.assertEqual(run.enter('C', T + 600)['type'], 'fill')
        self.assertEqual(run.state['portfolio_entries'], [T + 60, T + 600])

    def test_k_one_blocks_a_burst_of_correlated_entries(self):
        run = Run(cfg_on(portfolio_max_entries_per_10m=1))
        run.enter('A', T)
        for at in (T + 60, T + 200, T + 599):
            self.assertEqual(run.enter('B', at)['reason'], 'PORTFOLIO_ENTRY_SPACING')
        self.assertEqual(run.enter('B', T + 600)['type'], 'fill')

    def test_spacing_state_is_bounded_and_ignores_expired_entries(self):
        run = Run(cfg_on(portfolio_max_entries_per_10m=3, max_positions=8))
        for i in range(3):
            run.enter(f'M{i}', T + 60 * i)
        self.assertLessEqual(len(run.state['portfolio_entries']), 3)
        later = T + 5000
        for h in list(run.state['positions']):
            run.go(event(later - 1, mint=h))
        self.assertEqual(run.enter('N', later)['type'], 'fill')
        self.assertEqual(run.state['portfolio_entries'], [later])

    def test_rejected_and_non_entry_events_do_not_touch_portfolio_state(self):
        run = Run(cfg_on(portfolio_max_entries_per_10m=1))
        run.enter('A', T)
        before = copy.deepcopy(run.state['portfolio_entries'])
        run.enter('B', T + 60)
        run.go(control(T + 70, 'PAUSE_ENTRY'))
        run.go(control(T + 71, 'RESUME'))
        self.assertEqual(run.state['portfolio_entries'], before)


class DailyCapsAndBlockersTests(unittest.TestCase):
    def test_daily_pause_and_liquidation_behave_identically_with_the_flag_on(self):
        def losing(cfg):
            run = Run(cfg)
            run.enter('A', T)
            run.lose('A', T + 130)
            run.state['day_gross_losses'] = '0.5'
            run.go(event(T + 140, mint='Z', reserve_sol='40'))
            run.go(event(T + 200, mint='Z2'))
            return run.state['mode'], run.state['day_gross_losses'], run.state['loss_streak']
        self.assertEqual(losing(config()), losing(cfg_on()))

    def test_portfolio_blockers_is_pure_and_matches_the_engine(self):
        cfg = cfg_on(portfolio_max_entries_per_10m=1, portfolio_loss_streak_cooldown_after=1, portfolio_cooldown_minutes=10,
                     portfolio_max_open_risk_fraction='0.005')
        run = Run(cfg)
        self.assertEqual(portfolio_blockers(run.state, cfg, T), [])
        run.enter('A', T)
        snapshot = copy.deepcopy(run.state)
        self.assertEqual(portfolio_blockers(run.state, cfg, T + 60), ['PORTFOLIO_ENTRY_SPACING'])
        self.assertEqual(run.state, snapshot)                                                   # no mutation
        run.lose('A', T + 130)
        self.assertEqual(portfolio_blockers(run.state, cfg, T + 140), ['PORTFOLIO_LOSS_COOLDOWN', 'PORTFOLIO_ENTRY_SPACING'])
        self.assertEqual(portfolio_blockers(run.state, cfg, T + 130 + 600), [])
        state = {'positions': {'A': position(cost_left='0.2')}, 'cash': '4.8'}
        self.assertEqual(portfolio_blockers(state, cfg, T), ['PORTFOLIO_RISK_CAP'])
        self.assertEqual(portfolio_blockers(state, config(), T), [])


class RestartAndReplayTests(unittest.TestCase):
    def test_cold_restart_preserves_streak_cooldown_and_spacing(self):
        cfg = cfg_on(portfolio_loss_streak_cooldown_after=2, portfolio_cooldown_minutes=30, portfolio_max_entries_per_10m=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'l.sqlite'
            ledger = Ledger(path)

            def apply(e):
                return ledger.apply(e, cfg, transition, initial_state)
            apply(event(T, mint='A'))
            apply(event(T + 59, mint='A'))
            apply(event(T + 60, mint='B'))
            apply(event(T + 130, mint='A', reserve_sol='40'))
            apply(event(T + 135, mint='B', danger=True))
            ledger.close()
            ledger = Ledger(path)                                                                # cold restart: state read back
            out = apply(event(T + 200, mint='C'))
            self.assertEqual((out[0]['type'], out[0]['reason']), ('reject', 'PORTFOLIO_LOSS_COOLDOWN'))
            self.assertIn('PORTFOLIO_ENTRY_SPACING', out[0]['reasons'])                          # A (T) and B (T+60) still inside 600 s
            ledger.close()

    def test_restart_after_cooldown_and_window_expiry_allows_the_entry(self):
        cfg = cfg_on(portfolio_loss_streak_cooldown_after=2, portfolio_cooldown_minutes=30, portfolio_max_entries_per_10m=2)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'l.sqlite'
            ledger = Ledger(path)
            for e in (event(T, mint='A'), event(T + 59, mint='A'), event(T + 60, mint='B'),
                      event(T + 130, mint='A', reserve_sol='40'), event(T + 135, mint='B', danger=True)):
                ledger.apply(e, cfg, transition, initial_state)
            ledger.close()
            ledger = Ledger(path)
            out = ledger.apply(event(T + 135 + 1800, mint='C'), cfg, transition, initial_state)
            self.assertEqual(out[0]['type'], 'fill')                                             # spacing window long expired
            ledger.close()

    def test_replay_is_deterministic_and_never_reads_the_wall_clock(self):
        def boom(*a, **k):
            raise AssertionError('wall clock read inside the engine')
        cfg = cfg_on(portfolio_loss_streak_cooldown_after=2, portfolio_cooldown_minutes=30, portfolio_max_entries_per_10m=2,
                     portfolio_max_open_risk_fraction='0.005')
        with patch.object(time, 'time', boom), patch.object(time, 'monotonic', boom):
            first = scenario(cfg)
            second = scenario(cfg)
        self.assertEqual(digest(list(first)), digest(list(second)))
        self.assertNotEqual(digest(list(first)), BASE_DIGEST)                                    # the gates really changed decisions

    def test_a_restarted_state_equals_the_uninterrupted_state(self):
        cfg = cfg_on(portfolio_max_entries_per_10m=2)
        events = [event(T, mint='A'), event(T + 59, mint='A'), event(T + 60, mint='B')]
        direct = Run(cfg)
        for e in events:
            direct.go(e)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'l.sqlite'
            for e in events:
                ledger = Ledger(path)                                                            # a "restart" before every event
                ledger.apply(e, cfg, transition, initial_state)
                ledger.close()
            import json
            ledger = Ledger(path)
            stored = json.loads(ledger.db.execute('SELECT payload FROM state').fetchone()[0])
            ledger.close()
        self.assertEqual(stored['portfolio_entries'], direct.state['portfolio_entries'])
        self.assertEqual(sorted(stored['positions']), sorted(direct.state['positions']))


if __name__ == '__main__':
    unittest.main()
