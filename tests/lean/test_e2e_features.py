"""SYNTHETIC_TEST_ONLY: L12F end to end with the REAL modules (runner wiring, screen, strategy, paper, store, providers' shared low
lane, features, report). Only HTTP is faked (tests/lean/fakeworld.py; the pool's signatures and the dev wallet's token accounts are
served through its ``extra_rpc`` hook, the holder list by the world itself). No network, no credentials.

Scenario (12 launches, ticks of 60 s; after each tick the recorder's queue is drained, as its thread would):
  ok_1, ok_2     passed and entered: 2 extra calls (dev balance, pool history), holders reused from the screen
  cost           passed, rejected after the quote (COST_BUDGET)
  no_route       passed, the entry quote has no route: ABORTED_BY_ERROR
  mcap_high      SOFT reject: the third call is the holder list
  liquidity_low  SOFT reject
  mint_authority HAZARD reject: the cheap row only, nothing spent
  concentrated   HAZARD reject (it reached the holder stage, so it still has the screen's holder figures)
  truncated      the pool has 1000+ signatures: HISTORY_TRUNCATED, no paging
  no_creator     the pool's coin_creator is the system address: no dev call
  outage         Helius answers 503 during its screen: a retried candidate gets ONE row, from its final attempt
and the trading result is IDENTICAL with the recorder switched off.
"""
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from lean import features as F, providers, report
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, SUPPLY, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}
SCENARIO = [('ok_1', dict(coin_creator_tag=71), 0), ('cost', dict(route_fee_bps=600, coin_creator_tag=72), 50),
            ('mint_authority', dict(hazard='mint_authority', coin_creator_tag=73), 100), ('mcap_high', dict(quote_sol=3000, coin_creator_tag=74), 150),
            ('no_route', dict(no_route=True, coin_creator_tag=75), 200), ('ok_2', dict(coin_creator_tag=76), 250),
            ('concentrated', dict(hazard='concentrated', coin_creator_tag=77), 300),
            ('liquidity_low', dict(quote_sol=20, base_raw=2 * 10 ** 13, coin_creator_tag=78), 400),
            ('truncated', dict(coin_creator_tag=79), 450), ('no_creator', dict(), 500), ('outage', dict(coin_creator_tag=80), 600)]
OUTAGE = (T0 + 590, T0 + 650)
END = T0 + 2400
BLOCK_BEFORE_RECEIPT = 25                      # the graduation block time is 25 s before the discovery receive time
DEV_UNITS = 2 * 10 ** 13                       # every dev wallet holds 2 % of the supply


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def grad_slot(world):
    import sqlite3
    with sqlite3.connect(world.discovery_db) as c:
        payload = json.loads(c.execute('SELECT payload FROM raw_events ORDER BY seq LIMIT 1').fetchone()[0])
    return payload['params']['result']['slot']


class E2E(unittest.TestCase):
    maxDiff = None

    def build_world(self, *, features):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        clock = FakeTime(T0)
        tokens = [Token(21 + 2 * i, **kw) for i, (_, kw, _) in enumerate(SCENARIO)]
        role = {t.mint: name for t, (name, _, _) in zip(tokens, SCENARIO)}
        world = World(root, tokens, clock)
        offsets = [off for _, _, off in SCENARIO]
        world.add_frames([T0 + off for off in offsets])
        world.outages['helius'] = OUTAGE
        slot = grad_slot(world)
        pools = {t.pool: (t, T0 + off - 300 - BLOCK_BEFORE_RECEIPT) for t, off in zip(tokens, offsets)}
        calls = []

        def signatures(params, t):
            pool, options = params
            calls.append(('getSignaturesForAddress', pool))
            token, born = pools[pool]
            born = int(born)
            if role[token.mint] == 'truncated':
                return [{'signature': 's%d' % i, 'slot': slot + i, 'blockTime': born + i, 'err': None} for i in range(options['limit'])]
            sigs = [{'signature': 'create', 'slot': slot, 'blockTime': born, 'err': None}]
            sigs += [{'signature': 't%d' % i, 'slot': slot + 1 + i, 'blockTime': born + off, 'err': None} for i, off in enumerate((3, 40, 599))]
            sigs += [{'signature': 'bad', 'slot': slot + 9, 'blockTime': born + 5, 'err': {'InstructionError': [0, 'x']}},
                     {'signature': 'late', 'slot': slot + 99, 'blockTime': born + 700, 'err': None}]
            return list(reversed(sigs))

        def owner_accounts(params, t):
            calls.append(('getTokenAccountsByOwner', params[0]))
            return {'context': {'slot': 1}, 'value': [{'account': {'data': {'parsed': {'info': {'tokenAmount': {'amount': str(DEV_UNITS)}}}}}}]}
        world.extra_rpc = {'getSignaturesForAddress': signatures, 'getTokenAccountsByOwner': owner_accounts}
        cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        from lean import paths
        cfg['paths'] = paths.config({'enabled': False})                                          # isolate the feature recorder
        cfg['lanes'] = {'shares': providers.validate_lane_shares({'main': 0.5, 'exit': 0.2, 'low': 0.3}), 'low_shed_s': 1}
        cfg['features'] = F.config({'enabled': True}) if features else F.config(None)
        r = build_runner(cfg, state_dir=root / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='e2e-features',
                         clock=clock.time, transport_kwargs={'opener': world.opener, 'clock': clock.time, 'monotonic': clock.monotonic,
                                                             'sleep': clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        return r, role, world, calls, clock

    def drive(self, r, clock, *, spacing=2.0):
        tick = T0
        while tick <= END:
            if clock.time() < tick:
                clock.t = tick
            r.position_pass()
            r.candidate_pass()
            if r.features is not None and hasattr(r.features, 'process_one'):
                while True:
                    clock.t += spacing                                  # real calls take time: the low lane refills between candidates
                    if not r.features.process_one():
                        break
            self.assertIsNone(r.halted, r.halted)
            tick += 60

    def rows(self, r, role):
        out = {}
        for obs in r.store.rows('observations', kind='features', limit=100000):
            payload = json.loads(obs['raw'])
            out[role[payload['mint']]] = payload
        return out

    def test_one_row_per_screened_candidate_entered_or_not(self):
        r, role, world, calls, clock = self.build_world(features=True)
        self.drive(r, clock)
        screened = {role[d['mint']] for d in r.store.rows('decisions', kind='screen', limit=100000) if d['action'] in ('PASS', 'REJECT')}
        observations = r.store.rows('observations', kind='features', limit=100000)
        self.assertEqual(len(observations), len({o['mint'] for o in observations}))                  # exactly one per candidate
        rows = self.rows(r, role)
        self.assertEqual(set(rows), screened)
        self.assertEqual(set(rows), {name for name, _, _ in SCENARIO})
        entered = {role[f['mint']] for f in r.store.rows('fills', limit=100000) if f['side'] == 'buy'}
        for name, row in rows.items():
            self.assertEqual(row['entered'], name in entered, name)
            self.assertEqual(set(row['fields']), set(F.FIELDS))
            self.assertTrue(all((row['fields'][n] is None) == (n in row['missing']) for n in F.FIELDS), name)
            self.assertLessEqual(row['calls'], F.MAX_EXTRA_CALLS)
            self.assertLessEqual(row['screen_at'], row['collected_at'])
        self.assertEqual(rows['ok_1']['not_entered_reason'], None)
        self.assertEqual(rows['cost']['not_entered_reason'], 'COST_BUDGET')
        self.assertEqual(rows['no_route']['not_entered_reason'], 'ABORTED_BY_ERROR')
        self.assertEqual(rows['mcap_high']['not_entered_reason'], 'MARKET_CAP_ABOVE_MAX')
        self.assertIn('ACTIVE_MINT_AUTHORITY', rows['mint_authority']['not_entered_reason'])
        self.assertTrue(all(o['code_version'] == 'e2e-features' for o in observations))

    def test_the_features_are_the_real_values(self):
        r, role, world, calls, clock = self.build_world(features=True)
        self.drive(r, clock)
        rows = self.rows(r, role)
        by_mint = {role[t.mint]: t for t in world.order}
        ok = rows['ok_1']
        f = ok['fields']
        # dev wallet: the pool's coin_creator, the dev call asked for exactly that wallet and mint
        self.assertEqual(f['creator_wallet'], by_mint['ok_1'].coin_creator)
        self.assertIn(('getTokenAccountsByOwner', by_mint['ok_1'].coin_creator), calls)
        self.assertEqual(f['dev_holding_pct'], '2.0000')
        # graduation: the BLOCK time, not the discovery receive time (25 s later)
        self.assertEqual(f['graduation_block_time'], int(T0 - 300 - BLOCK_BEFORE_RECEIPT))
        self.assertEqual(f['seconds_graduation_to_screen'], float(f['age_seconds_at_screen']) + BLOCK_BEFORE_RECEIPT)
        self.assertEqual(f['tx_count_first_10m'], 3)                                       # 3, 40, 599; the failed, the late and the creation tx are out
        # holders: the ten 1 % holders; the pool vault (half the supply) is excluded, and so would the burn accounts be
        self.assertEqual((f['top1_pct'], f['top10_pct']), ('1.0000', '10.0000'))
        self.assertEqual(ok['calls'], 2)
        self.assertEqual((f['mint_authority_active'], f['token_program']), (False, F.TOKEN_PROGRAM))

    def test_soft_rejects_spend_the_third_call_on_holders_and_hazards_spend_nothing(self):
        r, role, world, calls, clock = self.build_world(features=True)
        self.drive(r, clock)
        rows = self.rows(r, role)
        by_mint = {role[t.mint]: t for t in world.order}
        for name in ('mcap_high', 'liquidity_low'):
            self.assertEqual(rows[name]['calls'], 3, name)
            self.assertIsNotNone(rows[name]['fields']['top10_pct'], name)
            self.assertIsNotNone(rows[name]['fields']['dev_holding_pct'], name)
        self.assertEqual(rows['mcap_high']['fields']['top1_pct'], '1.0000')
        for name in ('mint_authority', 'concentrated'):
            self.assertEqual(rows[name]['calls'], 0, name)
            self.assertEqual(rows[name]['missing']['dev_holding_pct'], 'NOT_ENRICHED_HAZARD_REJECT', name)
            self.assertNotIn(('getTokenAccountsByOwner', by_mint[name].coin_creator), calls)
        # the concentrated hazard reached the holder stage: its cheap row still has the screen's holder figures (70 % + ten 1 %)
        self.assertEqual(rows['concentrated']['fields']['top1_pct'], '70.0000')
        self.assertEqual(rows['concentrated']['fields']['top10_pct'], '79.0000')

    def test_truncated_history_and_unknown_creator_are_reasons_not_paging_or_spend(self):
        r, role, world, calls, clock = self.build_world(features=True)
        self.drive(r, clock)
        rows = self.rows(r, role)
        by_mint = {role[t.mint]: t for t in world.order}
        t = rows['truncated']
        self.assertEqual(t['missing']['graduation_block_time'], 'HISTORY_TRUNCATED')
        self.assertEqual(calls.count(('getSignaturesForAddress', by_mint['truncated'].pool)), 1)             # one page, never paged
        self.assertEqual(t['fields']['dev_holding_pct'], '2.0000')                                   # the other groups are unaffected
        n = rows['no_creator']
        self.assertEqual((n['missing']['creator_wallet'], n['missing']['dev_holding_pct']), ('CREATOR_UNKNOWN', 'CREATOR_UNKNOWN'))
        self.assertEqual(n['calls'], 1)

    def test_a_retried_candidate_has_one_row_from_its_final_attempt(self):
        r, role, world, calls, clock = self.build_world(features=True)
        self.drive(r, clock)
        self.assertIn(('helius', 'OUTAGE'), {(p, m) for p, m, _ in world.calls})
        self.assertGreater(r.counts['retries'], 0)
        outage_screens = [d for d in r.store.rows('decisions', kind='screen', limit=100000) if role[d['mint']] == 'outage']
        self.assertEqual([d['action'] for d in outage_screens], ['FAILED', 'PASS'])
        self.assertEqual(sum(1 for o in r.store.rows('observations', kind='features', limit=100000) if role[o['mint']] == 'outage'), 1)

    def test_every_extra_call_went_through_the_low_lane(self):
        r, role, world, calls, clock = self.build_world(features=True)
        before = providers.low_lane_stats('helius', clock=clock.monotonic, sleep=clock.sleep)['granted']
        self.drive(r, clock)
        rows = self.rows(r, role)
        granted = providers.low_lane_stats('helius', clock=clock.monotonic, sleep=clock.sleep)['granted'] - before
        holder_calls = sum(1 for n in ('mcap_high', 'liquidity_low') if rows[n]['calls'] == 3)
        self.assertEqual(granted, len(calls) + holder_calls + r.features.stats['errors'] * 0)
        self.assertEqual(granted, sum(row['calls'] for row in rows.values()))

    def test_the_low_lane_sheds_without_slowing_anything_when_it_has_no_token(self):
        r, role, world, calls, clock = self.build_world(features=True)
        r.features.helius.transport.limiter.shed(10 ** 6)                                     # the lane is shed for the whole run
        self.drive(r, clock)
        rows = self.rows(r, role)
        self.assertEqual(calls, [])                                                           # not one request was sent
        self.assertEqual({row['missing']['tx_count_first_10m'] for n, row in rows.items() if n not in ('mint_authority', 'concentrated')}, {providers.LANE_SHED})
        self.assertIsNotNone(rows['ok_1']['fields']['market_cap_usd'])                          # the free fields survive
        self.assertEqual(rows['ok_1']['fields']['creator_wallet'] is not None, True)
        self.assertEqual({o['kind'] for o in r.store.rows('observations', limit=100000)} >= {'features'}, True)
        entered = {role[f['mint']] for f in r.store.rows('fills', limit=100000) if f['side'] == 'buy'}
        self.assertIn('ok_1', entered)                                                         # trading was not affected

    def test_the_recorder_never_changes_what_the_desk_does(self):
        with_features, role_a, *_ = self.build_world(features=True)
        self.drive(with_features, _[-1])
        without, role_b, *rest = self.build_world(features=False)
        self.drive(without, rest[-1])

        def result(r, role):
            decisions = sorted((d['kind'], role[d['mint']], d['action'], tuple(json.loads(d['reasons']))) for d in r.store.rows('decisions', limit=100000)
                               if d['mint'] in role)
            fills = [(role[f['mint']], f['side'], f['qty_raw'], f['sol_lamports'], f['fee_lamports']) for f in r.store.rows('fills', limit=100000)]
            return decisions, fills, r.store.cash()
        a, b = result(with_features, role_a), result(without, role_b)
        self.assertTrue(a[1])
        self.assertEqual(a, b)
        self.assertIsNone(without.features)
        without.write_health()
        self.assertEqual(without.store.rows('observations', kind='features'), [])
        self.assertEqual(json.loads((Path(without.state_dir) / 'health.json').read_text())['features'], {'enabled': False})

    def test_health_and_the_report_show_the_features(self):
        r, role, world, calls, clock = self.build_world(features=True)
        self.drive(r, clock)
        r.write_health()
        health = json.loads((Path(r.state_dir) / 'health.json').read_text())['features']
        self.assertEqual((health['enabled'], health['rows'], health['queued']), (True, len(SCENARIO), 0))
        summary = report.build(r.store.path, now=clock.time())
        data = summary['features']['data']
        self.assertEqual((data['rows'], data['entered_rows']), (len(SCENARIO), data['entered_rows']))
        self.assertEqual(data['rows'], len(SCENARIO))
        self.assertGreater(data['entered_rows'], 0)
        self.assertIn('creator_wallet:CREATOR_UNKNOWN', data['missing'])
        self.assertIn('features vs outcome', report.render_html(summary))

    def test_run_starts_the_recorder_thread_and_stops_it_with_the_runner(self):
        r, role, world, calls, clock = self.build_world(features=True)
        ran = threading.Event()

        class Rec:
            def on_candidate(self, *a, **k):
                pass

            def health(self):
                return {'enabled': True}

            def run(self, stop, tick=0.5):
                ran.set()
                stop.wait()
        r.features = Rec()
        thread = threading.Thread(target=lambda: r.run(candidate_interval=0.01, position_interval=0.01, install_signals=False), daemon=True)
        thread.start()
        try:
            started = ran.wait(5)
        finally:
            r.stop.set()
            thread.join(10)
        self.assertTrue(started)
        self.assertFalse(thread.is_alive())

    def test_a_broken_recorder_changes_no_entry(self):
        r, role, world, calls, clock = self.build_world(features=True)

        class Boom:
            def on_candidate(self, *a, **k):
                raise RuntimeError('recorder bug')

            def health(self):
                return {'enabled': True}
        r.features = Boom()
        self.drive(r, clock)
        self.assertTrue(r.store.rows('fills', limit=10))
        self.assertNotIn('CANDIDATE_FAILED', r.errors_by_code)


if __name__ == '__main__':
    unittest.main()
