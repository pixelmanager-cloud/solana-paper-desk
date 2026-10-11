"""SYNTHETIC_TEST_ONLY: L16 end to end with the REAL lean modules; only HTTP is faked (tests/lean/fakeworld.py).

Scenario A  a routable token is bought and stopped out: entry and exit both recorded ROUTABLE with the route.
Scenario B  an exit whose route disappears (a rug): the trade's exit check is ONE NOT_ROUTABLE row with the provider's own
            error code (the first attempt), however many passes retry it; the position stays open; the later successful sell
            adds nothing; the report shows 0 % for that one exit.
Scenario C  the build endpoint demands a funded taker: ROUTE_CHECK_UNSUPPORTED is recorded exactly once, the check disables
            itself, a restart does not re-probe, and normal screening still works.
Nothing is signed or sent in any scenario (every row says so and lean/ has no such primitive: test_route_check).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lean import report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World, Resp
from tests.lean.test_e2e_real import KEYS, LADDER, ROOT, STOP, steps

NO_ROUTE_BODY = b'{"error":"Could not find any route","errorCode":"COULD_NOT_FIND_ANY_ROUTE"}'
FUNDS_BODY = b'{"error":"taker has insufficient balance to build","errorCode":"INSUFFICIENT_FUNDS"}'


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class ScriptedWorld(World):
    """Jupiter answers can be overridden per call: hook(input_mint, output_mint, t) -> Resp | None."""
    hook = None

    def _jupiter(self, path, q, t):
        if self.hook:
            override = self.hook(q['inputMint'], q['outputMint'], t)
            if override is not None:
                self.calls.append(('jupiter', 'override', t))
                return override
        return super()._jupiter(path, q, t)


class RouteCheckEndToEnd(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name)); self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        example = load_config(ROOT / 'tests' / 'lean' / 'lean.baseline.json')
        self.assertEqual(example['route_check'], {'enabled': False})              # the shipped example is off
        self.cfg = {**example, 'route_check': {'enabled': True}}

    def make(self, tokens, frames):
        self.tokens = tokens
        self.world = ScriptedWorld(self.root, tokens, self.clock)
        self.world.add_frames([T0 + f for f in frames])

    def runner(self, cfg=None):
        return build_runner(cfg or self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS,
                            code_version='e2e-route', clock=self.clock.time,
                            transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                              'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def drive(self, r, until, step=60):
        tick = self.clock.time()
        while tick <= T0 + until:
            self.clock.t = max(self.clock.t, tick)
            r.position_pass(); r.candidate_pass(); r.write_health()
            self.assertIsNone(r.halted, r.halted)
            tick += step

    def checks(self, r):
        return [(d['action'], json.loads(d['features']), json.loads(d['reasons'])) for d in r.store.rows('decisions', kind='route_check', limit=1000)]

    def test_a_entry_and_exit_are_recorded_routable(self):
        self.make([Token(21, path=STOP)], [0])
        r = self.runner(); self.drive(r, 2400)
        self.assertEqual(r.store.positions(), {})
        checks = self.checks(r)
        self.assertEqual([(a, f['side']) for a, f, _ in checks], [('ROUTABLE', 'entry'), ('ROUTABLE', 'exit')])
        for _, f, _ in checks:
            self.assertEqual((f['route'], f['signed'], f['sent']), (['Pump.fun Amm'], False, False))
            self.assertIsNotNone(f['quote_ref'])
        data = report.build(r.store.path if hasattr(r.store, 'path') else self.state / 'lean.sqlite', now=self.clock.time())['route_check']['data']
        self.assertEqual((data['entry']['pct'], data['exit']['pct']), (100.0, 100.0))
        html = report.render_html(report.build(self.state / 'lean.sqlite', now=self.clock.time()))
        self.assertIn('Live-route check', html)

    def test_b_a_vanished_exit_route_is_one_not_routable_exit_however_often_it_is_retried(self):
        token = Token(21, path=STOP)
        self.make([token], [0])
        gone = (T0 + 500, T0 + 1500)      # entry at ~T0, the STOP triggers at ~T0+600
        self.world.hook = lambda i, o, t: Resp(NO_ROUTE_BODY, 400) if i == token.mint and gone[0] <= t < gone[1] else None
        r = self.runner(); self.drive(r, 3000)
        failed_quotes = [e for e in r.store.rows('errors', limit=1000) if e['scope'] == 'quote:exit']
        self.assertGreaterEqual(len(failed_quotes), 2)                          # retried every pass while the route was gone (in `errors`)
        checks = self.checks(r)
        self.assertEqual([(a, f['side']) for a, f, _ in checks], [('ROUTABLE', 'entry'), ('NOT_ROUTABLE', 'exit')])
        self.assertEqual(checks[1][2], ['COULD_NOT_FIND_ANY_ROUTE'])
        self.assertEqual(checks[1][1]['trade'], r.store.closed_positions()[0]['open_fill_id'])
        self.assertEqual(r.store.positions(), {})                               # it did sell once the route came back
        data = report.build(self.state / 'lean.sqlite', now=self.clock.time())['route_check']['data']
        self.assertEqual((data['entry']['pct'], data['exit']['checked'], data['exit']['pct']), (100.0, 1, 0.0))
        self.assertEqual(data['not_routable_reasons'], {'COULD_NOT_FIND_ANY_ROUTE': 1})

    def test_f_a_provider_outage_is_inconclusive_not_a_missing_route(self):
        token = Token(21, path=STOP)
        self.make([token], [0])
        down = (T0 + 500, T0 + 900)
        self.world.hook = lambda i, o, t: Resp(b'{"error":"overloaded"}', 503) if i == token.mint and down[0] <= t < down[1] else None
        r = self.runner(); self.drive(r, 3000)
        checks = self.checks(r)
        self.assertEqual([(a, f['side']) for a, f, _ in checks], [('ROUTABLE', 'entry'), ('INCONCLUSIVE', 'exit'), ('ROUTABLE', 'exit')])
        data = report.build(self.state / 'lean.sqlite', now=self.clock.time())['route_check']['data']
        self.assertEqual((data['exit']['checked'], data['exit']['pct'], data['inconclusive'], data['not_routable_reasons']), (1, 100.0, 1, {}))

    def test_h_a_retried_candidate_has_one_inconclusive_entry_row_then_one_check(self):
        token = Token(21)
        self.make([token], [0])
        down = (T0, T0 + 90)                                                    # the first two attempts (ticks 0 and 60) fail with 503
        self.world.hook = lambda i, o, t: Resp(b'{"error":"overloaded"}', 503) if o == token.mint and down[0] <= t < down[1] else None
        r = self.runner(); self.drive(r, 600)
        self.assertEqual([(a, f['side']) for a, f, _ in self.checks(r)], [('INCONCLUSIVE', 'entry'), ('ROUTABLE', 'entry')])
        self.assertEqual(len(r.store.positions()), 1)

    def test_g_a_buy_quote_without_a_route_is_one_entry_check_per_candidate(self):
        token = Token(21)
        self.make([token], [0])
        self.world.hook = lambda i, o, t: Resp(NO_ROUTE_BODY, 400) if o == token.mint else None
        r = self.runner(); self.drive(r, 900)
        self.assertEqual([(a, f['side']) for a, f, _ in self.checks(r)], [('NOT_ROUTABLE', 'entry')])
        self.assertEqual(r.store.positions(), {})

    def test_c_funded_taker_requirement_disables_the_check_once_and_survives_restart(self):
        self.make([Token(21), Token(23)], [0, 300])
        self.world.hook = lambda i, o, t: Resp(FUNDS_BODY, 400)
        r = self.runner(); self.drive(r, 900)
        rows = self.checks(r)
        self.assertEqual([a for a, _, _ in rows], ['UNSUPPORTED'])
        self.assertEqual(rows[0][2], ['ROUTE_CHECK_UNSUPPORTED'])
        self.assertFalse(r.route_check.enabled)
        self.assertGreater(r.store.counts()['candidates'], 0)                  # screening kept working (quotes failed as errors)
        r.store.close()
        r2 = self.runner(); self.drive(r2, 1500)
        self.assertEqual([a for a, _, _ in self.checks(r2)], ['UNSUPPORTED'])   # no re-probe after the restart
        self.assertFalse(r2.route_check.enabled)

    def test_e_a_failed_partial_take_profit_quote_is_not_a_full_exit_check(self):
        token = Token(21, path=LADDER)
        self.make([token], [0])
        gone = (T0 + 250, T0 + 500)                                            # covers the first take-profit rung (+30%)
        self.world.hook = lambda i, o, t: Resp(NO_ROUTE_BODY, 400) if i == token.mint and gone[0] <= t < gone[1] else None
        r = self.runner(); self.drive(r, 4500)
        failed_quotes = [e for e in r.store.rows('errors', limit=1000) if e['scope'] == 'quote:exit']
        self.assertGreaterEqual(len(failed_quotes), 1)                          # the partial exit quote did fail...
        self.assertEqual([a for a, _, _ in self.checks(r) if a != 'ROUTABLE'], [])   # ...but only FULL exits are route-checked
        sides = [f['side'] for _, f, _ in self.checks(r)]
        self.assertEqual((sides[0], sides.count('exit')), ('entry', 1))         # one ROUTABLE record, for the closing sell

    def test_d_disabled_by_config_records_nothing(self):
        self.make([Token(21, path=STOP)], [0])
        r = self.runner({**self.cfg, 'route_check': {}}); self.drive(r, 2400)
        self.assertEqual(self.checks(r), [])
        self.assertTrue(r.store.closed_positions())


if __name__ == '__main__':
    unittest.main()
