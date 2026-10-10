"""SYNTHETIC_TEST_ONLY: market-regime entry gate (paper_regime_version=1)."""
import copy
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

from desk import engine, regime
from tests.helpers import T, config, event


def record(grad='8', sol='0.5', ts=T, **extra):
    return {'version': 1, 'as_of': ts, 'graduations_per_hour': grad, 'sol_usd_change_pct': sol, **extra}


def for_score(value, ts=T):
    """Evidence whose score is exactly `value` under the default policy (grad_rate_normal=6)."""
    return record(grad=str(Decimal(value) * 6), sol='0', ts=ts)


class PolicyAndScoreTests(unittest.TestCase):
    def setUp(self):
        self.pol = regime.policy({})

    def test_defaults_validate_and_overrides_are_checked(self):
        self.assertEqual(regime.policy({'paper_regime_policy': {'ttl_seconds': 60}})['ttl_seconds'], 60)
        for bad in ({'nope': 1}, {'ttl_seconds': 0}, {'ttl_seconds': True}, {'off_exit': '0.2'},
                    {'caution_exit': '0.5'}, {'caution_size_multiplier': '1'}, {'caution_size_multiplier': '0'},
                    {'grad_rate_normal': '0'}, {'off_enter': 'NaN'}, 'x'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                regime.policy({'paper_regime_policy': bad})

    def test_flag_values(self):
        self.assertFalse(regime.enabled({}))
        self.assertTrue(regime.enabled({'paper_regime_version': 1}))
        for bad in (0, 2, True, '1', 1.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                regime.enabled({'paper_regime_version': bad})

    def test_score_is_the_minimum_component(self):
        self.assertEqual(regime.score(record('6', '1'), self.pol), 1)
        self.assertEqual(regime.score(record('3', '0'), self.pol), Decimal('0.5'))             # slow graduations
        self.assertEqual(regime.score(record('60', '-2.5'), self.pol), Decimal('0.5'))         # SOL falling
        self.assertEqual(regime.score(record('60', '-9'), self.pol), 0)
        self.assertEqual(regime.score(record('60', '0', median_forward_return='-0.1'), self.pol), Decimal('0.5'))
        self.assertEqual(regime.score(record('60', '0', median_forward_return='0.3'), self.pol), 1)
        self.assertEqual(regime.score(record('3', '-2.5', median_forward_return='-0.15'), self.pol), Decimal('0.25'))

    def test_hysteresis_transitions(self):
        p, d = self.pol, Decimal
        steps = [('NORMAL', '0.55', 'NORMAL'), ('NORMAL', '0.45', 'CAUTION'), ('CAUTION', '0.55', 'CAUTION'),
                 ('CAUTION', '0.6', 'NORMAL'), ('CAUTION', '0.2', 'OFF'), ('NORMAL', '0.2', 'OFF'),
                 ('OFF', '0.3', 'OFF'), ('OFF', '0.34', 'OFF'), ('OFF', '0.35', 'CAUTION'),
                 ('OFF', '0.59', 'CAUTION'), ('OFF', '0.6', 'NORMAL'), ('NORMAL', '0.25', 'CAUTION')]
        for before, value, after in steps:
            with self.subTest(before=before, value=value):
                self.assertEqual(regime.next_state(before, d(value), p), after)
        with self.assertRaises(ValueError):
            regime.next_state('BOGUS', d('1'), p)

    def test_malformed_evidence_is_refused(self):
        pol = self.pol
        bad = [None, [], {}, record(extra=1) if False else {**record(), 'extra': 1}, {**record(), 'version': 2},
               {**record(), 'version': True}, {**record(), 'as_of': T + 1}, {**record(), 'as_of': -1},
               {**record(), 'as_of': 1.5}, {**record(), 'as_of': True}, record(grad='-1'), record(grad=8),
               record(grad='NaN'), record(grad='Infinity'), record(sol='x'), record(sol=1),
               record(median_forward_return='-2'), record(median_forward_return=0.1), record(grad='1' * 40)]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                regime.validate_record(item, T, pol)
        with self.assertRaisesRegex(ValueError, 'STALE'):
            regime.validate_record(record(ts=T - 901), T, pol)
        regime.validate_record(record(ts=T - 900), T, pol)


class EngineGateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config() | {'paper_regime_version': 1}

    def enter(self, evidence, *, ts=T, state=None, cfg=None, **changes):
        cfg = cfg or self.cfg
        state = state if state is not None else engine.initial_state(cfg)
        if evidence is not None:
            changes['regime'] = evidence
        return engine.transition(copy.deepcopy(state), event(ts, **changes), cfg)

    def test_normal_enters_and_records_the_regime(self):
        state, out = self.enter(record())
        (fill,) = [o for o in out if o['type'] == 'fill']
        self.assertEqual(fill['regime']['state'], 'NORMAL')
        self.assertEqual(state['regime_state'], 'NORMAL')

    def test_caution_halves_the_size_and_off_blocks(self):
        _, base = engine.transition(engine.initial_state(config()), event(), config())
        normal = Decimal([o for o in base if o['type'] == 'fill'][0]['amount_sol'])
        state, out = self.enter(for_score('0.45'))
        fill = [o for o in out if o['type'] == 'fill'][0]
        self.assertEqual(fill['regime']['state'], 'CAUTION')
        self.assertEqual(Decimal(fill['amount_sol']), normal / 2)
        state, out = self.enter(for_score('0.1'))
        (reject,) = [o for o in out if o['type'] == 'reject']
        self.assertEqual((reject['reason'], reject['regime']['state']), ('REGIME_OFF', 'OFF'))
        self.assertEqual(state['positions'], {})
        self.assertEqual(state['regime_state'], 'OFF')

    def test_missing_stale_future_and_malformed_evidence_fail_closed_and_keep_state(self):
        for label, evidence, reason in (('missing', None, 'REGIME_EVIDENCE_REQUIRED'),
                                        ('stale', record(ts=T - 901), 'REGIME_STALE'),
                                        ('future', record(ts=T + 5), 'REGIME_EVIDENCE_INVALID'),
                                        ('garbage', {'version': 1}, 'REGIME_EVIDENCE_INVALID')):
            with self.subTest(label):
                carry = engine.initial_state(self.cfg) | {'regime_state': 'CAUTION'}
                state, out = self.enter(evidence, state=carry)
                (reject,) = [o for o in out if o['type'] == 'reject']
                self.assertEqual(reject['reason'], reason)
                self.assertEqual((state['positions'], state['regime_state']), ({}, 'CAUTION'))

    def test_hysteresis_through_the_engine(self):
        carry, seen = 'NORMAL', []
        for value in ('0.45', '0.55', '0.2', '0.3', '0.4', '0.7'):
            state = engine.initial_state(self.cfg) | {'regime_state': carry}
            carry = self.enter(for_score(value), state=state)[0]['regime_state']
            seen.append(carry)
        self.assertEqual(seen, ['CAUTION', 'CAUTION', 'OFF', 'OFF', 'CAUTION', 'NORMAL'])

    def test_exits_are_never_gated(self):
        state, _ = self.enter(record())
        state['regime_state'] = 'OFF'
        state, out = engine.transition(state, event(T + 1, danger=True), self.cfg)    # no regime evidence at all
        self.assertTrue(any(o['type'] == 'fill' and o['side'] == 'sell' for o in out), out)
        self.assertFalse(any(str(o.get('reason', '')).startswith('REGIME') for o in out))

    def test_absent_flag_is_byte_identical_and_ignores_evidence(self):
        cfg = config()
        plain = engine.transition(engine.initial_state(cfg), event(), cfg)
        with_evidence = engine.transition(engine.initial_state(cfg), event(regime=for_score('0.0')), cfg)
        self.assertEqual(json.dumps(plain, sort_keys=True), json.dumps(with_evidence, sort_keys=True))
        self.assertNotIn('regime_state', plain[0])

    def test_replay_is_deterministic_and_clock_free(self):
        def run():
            state = engine.initial_state(self.cfg)
            outs = []
            for i, value in enumerate(('0.45', '0.2', '0.4', '0.9')):
                state = json.loads(json.dumps(state))                       # restart between events
                state['regime_state'] = state.get('regime_state', 'NORMAL')
                state, out = self.enter(for_score(value, ts=T + 61 * i), ts=T + 61 * i, state=state, mint=f'M{i}')
                outs.append(out)
            return json.dumps([state, outs], sort_keys=True)
        with patch('time.time', side_effect=AssertionError('wall clock')), \
                patch('time.monotonic', side_effect=AssertionError('wall clock')):
            first = run()
        self.assertEqual(first, run())

    def test_invalid_flag_or_policy_is_refused(self):
        for cfg in (config() | {'paper_regime_version': 2}, self.cfg | {'paper_regime_policy': {'off_exit': '0.1'}}):
            with self.subTest(cfg=cfg.get('paper_regime_policy')), self.assertRaises(ValueError):
                self.enter(record(), cfg=cfg)


if __name__ == '__main__':
    unittest.main()
