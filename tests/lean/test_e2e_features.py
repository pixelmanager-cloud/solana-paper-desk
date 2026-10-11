"""SYNTHETIC_TEST_ONLY: L12 end to end. The real lean runner on the fake world (only HTTP faked); the feature recorder gets a scripted
Helius for the three extra calls (the fake world does not serve them). No network, no credentials."""
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lean import features as F
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_features import ScriptedHelius

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
SCENARIO = [('ok_1', dict(), 0), ('cost', dict(route_fee_bps=600), 50), ('mint_authority', dict(hazard='mint_authority'), 100),
            ('quote_retry', dict(), 150), ('mcap_high', dict(quote_sol=3000), 200), ('no_route', dict(no_route=True), 250),
            ('ok_2', dict(), 300), ('concentrated', dict(hazard='concentrated'), 400), ('liquidity_low', dict(quote_sol=20, base_raw=2 * 10 ** 13), 500),
            ('outage', dict(), 600), ('ok_3', dict(), 700), ('ok_4', dict(), 800)]
JUPITER_OUTAGE = (T0 + 140, T0 + 200)
OUTAGE = (T0 + 590, T0 + 650)
END = T0 + 2400


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class Boom:
    def submit(self, *a, **k):
        raise RuntimeError('recorder bug')


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.clock = FakeTime(T0)
        self.tokens = [Token(21 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCENARIO)]
        self.role = {t.mint: role for t, (role, _, _) in zip(self.tokens, SCENARIO)}
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames([T0 + off for _, _, off in SCENARIO])
        self.world.outages['helius'] = OUTAGE
        self.world.outages['jupiter'] = JUPITER_OUTAGE
        self.cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')

    def runner(self, recorder_factory):
        r = build_runner(self.cfg, state_dir=self.root / 'state', discovery_db=self.world.discovery_db, keys=KEYS, code_version='e2e-test',
                         clock=self.clock.time, transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time,
                                                                  'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep, 'rng': lambda: 0.5})
        r.features_recorder = recorder_factory(r.store)
        return r

    def drive(self, r, work=None):
        tick = T0
        while tick <= END:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass()
            r.candidate_pass()
            if work:
                work()
            self.assertIsNone(r.halted, r.halted)
            tick += 60

    def test_one_row_per_screened_candidate_entered_or_not(self):
        helius = ScriptedHelius(creation=T0 - 900)
        rec = None

        def factory(store):
            nonlocal rec
            rec = F.FeatureRecorder(store=store, helius=helius, code_version='e2e-test', strategy_version='x', clock=self.clock.time,
                                    bucket=F.LowPriorityBucket(100.0, 300, clock=self.clock.monotonic))
            return rec
        r = self.runner(factory)
        self.drive(r, work=lambda: [None for _ in iter(lambda: rec.process_one(), False)])
        screens = r.store.rows('decisions', kind='screen', limit=100000)
        screened = {d['mint'] for d in screens if d['action'] in ('PASS', 'REJECT')}
        rows = [json.loads(o['raw']) for o in r.store.rows('observations', kind='features', limit=100000)]
        self.assertEqual(len(rows), len({x['mint'] for x in rows}))                          # one per candidate
        self.assertEqual({x['mint'] for x in rows}, screened)
        by_role = {self.role[x['mint']]: x for x in rows}
        entered = {self.role[f['mint']] for f in r.store.rows('fills', limit=100000) if f['side'] == 'buy'}
        self.assertEqual(entered, {'ok_1', 'quote_retry', 'ok_2', 'outage'})
        for role, x in by_role.items():
            self.assertEqual(x['entered'], role in entered, role)
            self.assertEqual(set(x['fields']), set(F.FIELDS))
            self.assertTrue(all((x['fields'][n] is None) == (n in x['missing']) for n in F.FIELDS))
        self.assertEqual(by_role['ok_1']['fields']['dev_holding_pct'], '1.0000')
        self.assertEqual(by_role['ok_1']['fields']['seconds_creation_to_graduation'] > 0, True)
        self.assertEqual(by_role['mint_authority']['fields']['mint_authority_active'], True)
        self.assertEqual(by_role['mint_authority']['missing']['creator_wallet'], 'NOT_ENRICHED_HAZARD_REJECT')
        self.assertEqual(by_role['concentrated']['missing']['creator_wallet'], 'NOT_ENRICHED_HAZARD_REJECT')
        self.assertEqual(by_role['cost']['not_entered_reason'], 'COST_BUDGET')                 # after the quote
        self.assertEqual(by_role['ok_3']['not_entered_reason'], 'MAX_POSITIONS')               # the pre-quote gate
        self.assertEqual(by_role['no_route']['not_entered_reason'], 'ABORTED_BY_ERROR')        # a permanent provider error mid-entry
        self.assertTrue(by_role['quote_retry']['entered'])                                      # a transient quote failure was retried: its one row says entered
        self.assertEqual(by_role['quote_retry']['not_entered_reason'], None)
        self.assertEqual(by_role['mcap_high']['not_entered_reason'], 'MARKET_CAP_ABOVE_MAX')
        self.assertIsNotNone(by_role['mcap_high']['fields']['creator_wallet'])             # soft reject: enriched
        self.assertIn('LIQUIDITY_BELOW_MIN', by_role['liquidity_low']['not_entered_reason'])
        self.assertTrue(all(o['code_version'] == 'e2e-test' for o in r.store.rows('observations', kind='features', limit=100000)))
        self.assertLessEqual(len(helius.calls), 3 * len(rows))                               # at most 3 extra calls per candidate
        self.assertIn(('helius', 'OUTAGE'), {(p, m) for p, m, _ in self.world.calls})        # a transient screen failure happened and was retried
        self.assertGreater(r.counts['retries'], 0)

    def test_a_broken_recorder_changes_no_entry(self):
        base = self.runner(lambda store: None)
        self.drive(base)
        fills_without = [(f['mint'], f['qty_raw'], f['sol_lamports']) for f in base.store.rows('fills', limit=100000)]
        base.store.close()
        self.tmp.cleanup()
        self.setUp()
        broken = self.runner(lambda store: Boom())
        self.drive(broken)
        fills_with = [(f['mint'], f['qty_raw'], f['sol_lamports']) for f in broken.store.rows('fills', limit=100000)]
        self.assertTrue(fills_without)
        self.assertEqual(len(fills_with), len(fills_without))
        self.assertEqual(broken.store.rows('observations', kind='features'), [])
        self.assertNotIn('CANDIDATE_FAILED', broken.errors_by_code)                            # the recorder bug was swallowed, not turned into an error

    def test_run_starts_the_recorder_thread_and_stops_it_with_the_runner(self):
        import threading
        ran = threading.Event()

        class Rec:
            def submit(self, *a, **k):
                pass

            def run(self, stop, tick=0.5):
                ran.set()
                stop.wait()
        r = self.runner(lambda store: Rec())
        t = threading.Thread(target=lambda: r.run(candidate_interval=0.01, position_interval=0.01, install_signals=False), daemon=True)
        t.start()
        try:
            started = ran.wait(5)
        finally:
            r.stop.set()
            t.join(10)
        self.assertTrue(started)
        self.assertFalse(t.is_alive())

    def test_feature_outcome_table_over_the_run(self):
        helius = ScriptedHelius(creation=T0 - 900)
        rec = None

        def factory(store):
            nonlocal rec
            rec = F.FeatureRecorder(store=store, helius=helius, code_version='e2e-test', strategy_version='x', clock=self.clock.time,
                                    bucket=F.LowPriorityBucket(100.0, 300, clock=self.clock.monotonic))
            return rec
        r = self.runner(factory)
        self.drive(r, work=lambda: [None for _ in iter(lambda: rec.process_one(), False)])
        rows = F.feature_rows(r.store)
        realized = F.outcomes(r.store)
        table = F.feature_outcome_table(rows, realized, min_total=1, min_bucket=1)
        n_entered_closed = sum(1 for x in rows if x['entered'] and x['mint'] in realized)
        if n_entered_closed:
            self.assertEqual(table['market_cap_usd']['n'], n_entered_closed)
        self.assertTrue(all(t['insufficient'] is False for t in table.values()))


if __name__ == '__main__':
    unittest.main()
