"""SYNTHETIC_TEST_ONLY: versioned EXIT_ONLY recovery (paper_exit_only_recovery_version=1).

Pure engine tests on hand-built state; the end-to-end entry -> stale watchdog -> exit path is
tests.test_audit_first_cycle_c_held.ExitOnlyStickinessTests.
"""
import copy
import json
import unittest

from desk import engine
from tests.helpers import config

CLOCK = {'schema_version': 1, 'kind': 'clock', 'actor': 'paper_monitor'}


def control(command, ts, n):
    return {'schema_version': 1, 'kind': 'control', 'event_id': f'ctl{n}', 'ts': ts, 'actor': 'operator', 'command': command}


class ExitOnlyRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config() | {'paper_exit_only_recovery_version': 1}
        self.state = engine.initial_state(self.cfg)
        self.state['positions']['M'] = {'mark_at': 100, 'mark_value': '0.01', 'mark_status': 'MODEL_ESTIMATE',
                                        'exit_blocked': None}
        self.late = 100 + self.cfg['price_ttl_seconds'] + 5

    def daily_pause_drawdown(self, state):
        # equity 5.01 -> drawdown strictly between the pause and the liquidate fractions
        pause, liquidate = float(self.cfg['daily_pause_fraction']), float(self.cfg['daily_liquidate_fraction'])
        self.assertLess(pause, liquidate)
        state['day_start_equity'] = str(5.01 / (1 - (pause + liquidate) / 2))

    def block(self, cfg=None):
        cfg = cfg or self.cfg
        state, out = engine.transition(copy.deepcopy(self.state), dict(CLOCK, event_id='clock1', ts=self.late), cfg)
        self.assertEqual(state['mode'], 'EXIT_ONLY')
        return state

    def clear(self, state):
        state['positions']['M']['exit_blocked'] = None
        state['positions']['M']['mark_status'] = 'MODEL_ESTIMATE'
        out = []
        engine.risk(state, self.cfg, out)
        return out

    def test_unresolved_exit_pause_lifts_when_nothing_is_blocked(self):
        state = self.block()
        self.assertEqual(state['exit_only_cause'], 'UNRESOLVED_EXIT')
        out = self.clear(state)
        self.assertEqual(state['mode'], 'RUNNING')
        self.assertNotIn('exit_only_cause', state)
        self.assertEqual(out, [{'type': 'control', 'reason': 'UNRESOLVED_EXIT_CLEARED'}])

    def test_still_blocked_position_keeps_exit_only(self):
        state = self.block()
        out = []
        engine.risk(state, self.cfg, out)
        self.assertEqual((state['mode'], state['exit_only_cause']), ('EXIT_ONLY', 'UNRESOLVED_EXIT'))
        self.assertEqual(out, [], 'no spurious CLEARED control while an exit is still blocked')
        state['positions']['N'] = dict(state['positions']['M'])
        state['positions']['M']['exit_blocked'] = None
        engine.risk(state, self.cfg, out)
        self.assertEqual(state['mode'], 'EXIT_ONLY', 'second position is still blocked')
        self.assertEqual(out, [], 'no spurious CLEARED control while an exit is still blocked')

    def test_default_off_is_byte_identical_and_sticky(self):
        cfg = config()
        state = copy.deepcopy(self.state)
        state, _ = engine.transition(state, dict(CLOCK, event_id='clock1', ts=self.late), cfg)
        self.assertEqual(state['mode'], 'EXIT_ONLY')
        self.assertNotIn('exit_only_cause', state)
        state['positions']['M']['exit_blocked'] = None
        engine.risk(state, cfg, [])
        self.assertEqual(state['mode'], 'EXIT_ONLY')
        self.assertNotIn('exit_only_cause', state)

    def test_default_off_risk_path_never_records_a_cause(self):
        cfg = config()
        state = copy.deepcopy(self.state)
        state['positions']['M']['exit_blocked'] = 'STALE_OR_UNAVAILABLE_EXIT'
        out = []
        engine.risk(state, cfg, out)
        self.assertEqual((state['mode'], out), ('EXIT_ONLY', [{'type': 'control', 'reason': 'UNRESOLVED_EXIT_BLOCKS_ENTRY'}]))
        self.assertNotIn('exit_only_cause', state)

    def test_operator_exit_only_is_never_lifted(self):
        state, _ = engine.transition(copy.deepcopy(self.state), control('EXIT_ONLY', 200, 1), self.cfg)
        self.assertNotIn('exit_only_cause', state)
        engine.risk(state, self.cfg, [])
        self.assertEqual(state['mode'], 'EXIT_ONLY')
        # operator command issued while the engine pause is active replaces its provenance
        state = self.block()
        state, _ = engine.transition(state, control('EXIT_ONLY', self.late + 1, 2), self.cfg)
        self.assertNotIn('exit_only_cause', state)
        self.clear(state)
        self.assertEqual(state['mode'], 'EXIT_ONLY')

    def test_daily_pause_and_loss_streak_still_hold_after_the_exit_is_cleared(self):
        for label, mutate in (('loss_streak', lambda s: s.__setitem__('loss_streak', 6)),
                              ('daily_drawdown', self.daily_pause_drawdown)):
            with self.subTest(label):
                state = self.block()
                mutate(state)
                out = self.clear(state)
                self.assertEqual(state['mode'], 'EXIT_ONLY')
                self.assertNotIn('exit_only_cause', state)
                self.assertIn({'type': 'control', 'reason': 'RISK_EXIT_ONLY'}, out)

    def test_liquidation_is_not_lifted_and_cause_is_dropped(self):
        state = self.block()
        state['mode'] = 'LIQUIDATING'
        engine.risk(state, self.cfg, [])
        self.assertEqual(state['mode'], 'LIQUIDATING')
        self.assertNotIn('exit_only_cause', state)

    def test_resume_command_cannot_bypass_a_still_blocked_exit(self):
        state = self.block()
        state, _ = engine.transition(state, control('RESUME', self.late + 1, 3), self.cfg)
        self.assertEqual((state['mode'], state['exit_only_cause']), ('EXIT_ONLY', 'UNRESOLVED_EXIT'))
        state['positions']['M']['exit_blocked'] = None
        state, _ = engine.transition(state, control('RESUME', self.late + 2, 4), self.cfg)
        self.assertEqual(state['mode'], 'RUNNING')
        self.assertNotIn('exit_only_cause', state)

    def test_cause_survives_a_json_round_trip_restart(self):
        state = json.loads(json.dumps(self.block()))
        self.clear(state)
        self.assertEqual(state['mode'], 'RUNNING')

    def test_invalid_version_values_are_rejected(self):
        for bad in (0, 2, True, '1', 1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    engine.transition(copy.deepcopy(self.state), dict(CLOCK, event_id='x', ts=self.late),
                                      config() | {'paper_exit_only_recovery_version': bad})


if __name__ == '__main__':
    unittest.main()
