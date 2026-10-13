"""SYNTHETIC_TEST_ONLY: W2b end to end on the realistic-latency fake world (tests/lean/w2bworld.py) with the REAL runner, screen, strategy,
paper, store, providers (the shared low lane), flow, wallet signals, features, RugCheck recorder, path recorder, selection learner and
the prefetcher. Only HTTP is faked; no network, no credentials.

#8  features READY at the decision
      * before / after: with the prefetch OFF no collector value was observed before the screen decision (the collectors start after it);
        with it ON every hazard-free candidate has them, in a stressed world too (faults, a 429 shed window, launch bursts);
      * the MAIN ARM IS BYTE-IDENTICAL: fills, position states, decisions and cash with the prefetch on and off (the auxiliary thread's
        time never moves the trader's);
      * NO LOOKAHEAD: every committed row is checked against an oracle built from the swap tape (what had happened by its window end);
        a prefetch that FINISHES after the decision is refused and the collector runs as before; a stale or late row is excluded by the
        learner;
      * hazards cost the probe only; the probe agrees with the real screen.
#9  a deterministic random sample of the hazard rejects gets a price path
      * labelled ``hazard_reject: <reason>``, never entered (no quote, no fill, no entry decision), counted in health;
      * the learner reports them in their own section and its real-population rows are identical with the sample on and off.
"""
import json
import math
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lean import learn as LN, paths as L, prefetch as PF, providers, report, strategy as S
from lean import labels as LB
from tests.lean import w2bworld as W
from tests.lean.fakeworld import T0

ROOT = Path(__file__).resolve().parents[2]


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def rows_of(r, kind):
    return [json.loads(o['raw']) for o in r.store.rows('observations', kind=kind, limit=100000)]


class Base(unittest.TestCase):
    maxDiff = None

    def run_sim(self, plan, *, until=None, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sim = W.Sim(Path(os.path.realpath(tmp.name)), plan, **kw)
        self.addCleanup(sim.close)
        self.addCleanup(providers.configure_lanes)
        sim.run(plan[-1][1] + 300.0 + 240.0 if until is None else until)
        return sim


PLAN = W.scenario(30, seed=7)
STRESS = W.scenario(40, seed=11, gap=(6.0, 30.0))


class Readiness(Base):
    def test_before_and_after_in_the_realistic_latency_world(self):
        off = self.run_sim(PLAN, prefetch=False, fault_p=0.02)
        on = self.run_sim(PLAN, prefetch=True, fault_p=0.02)
        a, b = PF.readiness(off.runner.store.path), PF.readiness(on.runner.store.path)
        self.assertEqual(a['population'], b['population'])                       # the same launches, screened the same way
        self.assertGreater(b['population']['free'], 10)
        self.assertGreater(b['population']['screened'] - b['population']['free'], 10)      # and a lot of hazard rejects
        for name, one in a['features'].items():
            two = b['features'][name]
            self.assertEqual(one['ready'], 0, name)                               # BEFORE: nothing was observed before the decision
            self.assertGreaterEqual(two['ready_rate'], 0.9, name)                 # AFTER
            self.assertGreaterEqual(two['usable'], one['usable'] - 1, name)       # and nothing the learner could use was lost
        # the runner's own KPI says the same
        health = on.runner.health()['prefetch']['ready_at_decision']
        kinds = {'flow': 'flow.buy_sell_ratio', 'features': 'features.dev_holding_pct', 'risk_external': 'risk.rc_score',
                 'wallet_signals': 'wallet.bundle_score', 'clusters': 'clusters.cluster_count'}
        for kind, name in kinds.items():
            self.assertEqual(health['ready'][kind], b['features'][name]['ready'], kind)
        self.assertEqual((health['hazard_free'], health['passed']), (b['population']['free'], b['population']['passed']))

    def test_the_report_and_the_health_show_the_readiness(self):
        sim = self.run_sim(PLAN, prefetch=True)
        summary = report.build(sim.runner.store.path, now=sim.clock.time())
        data = summary['readiness']['data']
        self.assertEqual(summary['readiness']['status'], 'OK')
        raw = PF.readiness(sim.runner.store.path)                                  # the learner-based one: same ready counts as the meta-based
        for name, row in data['features'].items():
            self.assertEqual((row['n'], row['row'], row['ready']), (raw['features'][name]['n'], raw['features'][name]['row'],
                                                                    raw['features'][name]['ready']), name)
            self.assertIsNone(row['usable'])                                           # (the report never reads an observation blob)
        self.assertGreaterEqual(data['features']['flow.buy_sell_ratio']['ready_rate'], 0.9)
        html = report.render_html(summary)
        self.assertIn('Features ready at the decision', html)
        self.assertIn('flow.buy_sell_ratio', html)
        off = self.run_sim(PLAN[:10], prefetch=False)
        self.assertEqual({r['ready'] for r in report.build(off.runner.store.path, now=off.clock.time())['readiness']['data']['features'].values()}, {0})
        self.assertIn('prefetch', sim.runner.health())
        self.assertIn(sim.runner.health()['prefetch']['alive'], (True, False))

    def test_the_runner_starts_the_prefetch_thread_with_the_other_collectors(self):
        sim = self.run_sim(PLAN[:3], prefetch=True, until=100.0)
        names = [name for name, _target, _args in sim.runner._aux_specs()]
        self.assertIn('prefetch', names)
        self.assertIs(sim.runner.prefetch.stop, sim.runner.stop)                  # one shutdown for all of them
        off = self.run_sim(PLAN[:3], prefetch=False, until=100.0)
        self.assertNotIn('prefetch', [name for name, _t, _a in off.runner._aux_specs()])

    def test_a_halted_trader_or_the_kill_switch_pauses_the_prefetch(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sim = W.Sim(Path(os.path.realpath(tmp.name)), PLAN[:6], prefetch=True)
        self.addCleanup(sim.close)
        self.addCleanup(providers.configure_lanes)
        Path(sim.runner.kill_switch).write_text('stop')
        sim.run(PLAN[2][1] + 150.0)                                                # the first three launches arrive while the trader takes no entries
        self.assertEqual(sim.runner.prefetch.counts['discovered'], 0)
        self.assertEqual(sum(1 for (actor, _m), _n in sim.scene.net.calls.items() if actor == 'prefetch'), 0)
        os.unlink(sim.runner.kill_switch)
        sim.run(PLAN[5][1] + 300.0)
        self.assertGreater(sim.runner.prefetch.counts['discovered'], 0)           # and it resumes, for the launches that arrive after
        self.assertFalse({t.mint for t in sim.scene.tokens[:3]} & set(sim.runner.prefetch.entries))

    def test_a_soft_reject_gets_its_holder_shares_from_the_probe_flagged_as_a_snapshot(self):
        sim = self.run_sim(PLAN, prefetch=True)
        soft = [r for r in rows_of(sim.runner, 'features') if sim.scene.role[r['mint']] == 'mcap_high']
        self.assertTrue(soft)
        for row in soft:
            self.assertIn('prefetch', row)
            self.assertIsNotNone(row['fields']['top10_pct'])
            self.assertEqual((row['flags']['top10_pct'], row['flags']['top1_pct']), ('AT_COLLECTION', 'AT_COLLECTION'))
            self.assertLessEqual(row['late_s'], 0)                                       # read before the decision

    def test_a_stressed_world_still_has_nearly_everything_ready(self):
        sim = self.run_sim(STRESS, prefetch=True, fault_p=0.05, latency=1.5)
        sim_shed = self.run_sim(STRESS, prefetch=True, fault_p=0.05, latency=1.5, shed_windows=[(T0 + 300.0, T0 + 330.0)])
        for world, floor in ((sim, 0.85), (sim_shed, 0.8)):
            ready = PF.readiness(world.runner.store.path)
            for name, row in ready['features'].items():
                self.assertGreaterEqual(row['ready_rate'], floor, name)

    def test_hazards_cost_the_probe_only_and_the_probe_agrees_with_the_screen(self):
        sim = self.run_sim(PLAN, prefetch=True, prefetch_cfg={'ttl_s': 86400})
        p = sim.runner.prefetch
        hazard_roles = {'mayhem', 'concentrated', 'mint_authority', 'holder_reward'}
        for token in sim.scene.tokens:
            entry = p.entries.get(token.mint)
            self.assertIsNotNone(entry, token.tag)
            role = sim.scene.role[token.mint]
            self.assertEqual(entry.gate, 'HAZARD' if role in hazard_roles else 'FREE', role)
            self.assertEqual(entry.real, entry.gate)
            jobs = {k: j.state for k, j in entry.jobs.items()}
            if role in hazard_roles:
                self.assertEqual({k: v for k, v in jobs.items() if k != 'probe'}, {k: 'skipped' for k in PF.KINDS}, role)
            else:
                self.assertEqual(set(jobs.values()), {'done'}, (role, jobs))
        h = p.health()
        self.assertEqual((h['probe_vs_screen']['fp'], h['probe_vs_screen']['fn']), (0, 0))
        self.assertEqual(h['probes'], len(PLAN))
        self.assertGreater(h['hazard_skipped'], 0)
        # a hazard never reached a collector's own request: the prefetch skipped it, the probe made <= 3 calls
        per = sim.scene.net.calls
        self.assertEqual(sum(n for (actor, m), n in per.items() if actor == 'prefetch' and m == 'rugcheck'),
                         sum(1 for t in sim.scene.tokens if sim.scene.role[t.mint] not in hazard_roles))

    def test_the_cost_of_the_prefetch_is_the_probe_plus_what_the_collectors_would_have_called_anyway(self):
        off = self.run_sim(PLAN, prefetch=False)
        on = self.run_sim(PLAN, prefetch=True)

        def aux_calls(sim):
            return sum(n for (actor, m), n in sim.scene.net.calls.items() if actor in ('flow', 'wallet', 'features', 'risk', 'prefetch', 'clusters_collector') and m != 'rugcheck')
        n = len(PLAN)
        extra = aux_calls(on) - aux_calls(off)
        self.assertLess(extra / n, 6.0)                                           # a few calls per launch (the probe + flow/features for soft rejects)
        # no lane other than the low one was touched by the prefetch
        for (actor, method), count in on.scene.net.calls.items():
            if actor == 'prefetch':
                self.assertIn(method, ('getMultipleAccounts', 'getTokenLargestAccounts', 'getSignaturesForAddress', 'getTransaction',
                                       'getTokenAccountsByOwner', 'getTokenSupply', 'rugcheck'))
        granted = providers.low_lane_stats('helius', clock=on.clock.monotonic, sleep=on.clock.sleep)['granted']
        sent = sum(count for (actor, m), count in on.scene.net.calls.items() if actor not in W.MAIN_ACTORS and m not in ('rugcheck', 'jupiter', 'kraken'))
        self.assertEqual(granted, sent)                                          # every auxiliary Helius call went through the shared low lane


class MainArm(Base):
    @staticmethod
    def scrub(value):
        """A state / decision JSON without the ``*_ref`` keys (the store's autoincrement ids of the quote observations)."""
        if isinstance(value, dict):
            return {k: MainArm.scrub(v) for k, v in value.items() if not k.endswith('ref')}
        if isinstance(value, list):
            return [MainArm.scrub(v) for v in value]
        return value

    @staticmethod
    def main_view(sim):
        s = sim.runner.store
        decisions = [(d['ts'], d['kind'], d['mint'], d['action'], d['reasons']) for d in s.rows('decisions', limit=100000)]
        # every column of every fill (ts, quantities, fees, balances after ...) except ``quote_ref``: the store's autoincrement id of the quote's
        # raw observation, which the auxiliary rows written in between necessarily shift (it names a row, it is not a number the trade used)
        fills = [tuple(sorted((k, v) for k, v in f.items() if k != 'quote_ref')) for f in s.rows('fills', limit=100000)]
        states = [(p['ts'], p['mint'], p['event'], json.dumps(MainArm.scrub(json.loads(p['state'])), sort_keys=True))
                  for p in s.rows('position_state', limit=100000)]
        return decisions, fills, states, s.cash(), sorted(s.positions())

    def test_fills_decisions_and_states_are_byte_identical_with_the_prefetch_on_and_off(self):
        off = self.run_sim(PLAN, prefetch=False, fault_p=0.02)
        on = self.run_sim(PLAN, prefetch=True, fault_p=0.02)
        a, b = self.main_view(off), self.main_view(on)
        self.assertGreater(len(a[1]), 4)                                         # there were trades
        self.assertEqual(a, b)
        # ... and the decision times are exactly what they were (62.6 s in production; here 61-66 s after the frame)
        decided = [d[0] - off.scene.arrival[d[2]] for d in a[0] if d[1] == 'screen']
        self.assertTrue(all(60.0 <= x <= 70.0 for x in decided), decided)

    def test_it_holds_for_a_stressed_world_too(self):
        off = self.run_sim(STRESS, prefetch=False, fault_p=0.05, latency=1.5)
        on = self.run_sim(STRESS, prefetch=True, fault_p=0.05, latency=1.5, shed_windows=[(T0 + 300.0, T0 + 330.0)])
        self.assertEqual(self.main_view(off)[1], self.main_view(on)[1])


class NoLookahead(Base):
    """Every committed row against an oracle built from the swap tape: what had happened by the row's own window end. The same oracles
    run over the busy world and over a quiet one whose swaps are timed to fall in the gap between a prefetch's as-of and its first call
    (a CANARY: a collector that did not cut its window at the as-of would count it)."""

    @staticmethod
    def trades(sim, mint):
        token = next(t for t in sim.scene.tokens if t.mint == mint)
        return sim.scene.tape.pools[token.pool]['trades']

    def check_flow(self, sim):
        prefetched = 0
        for row in rows_of(sim.runner, 'flow'):
            decision = row['decision_ts']
            self.assertLessEqual(row['window_end'], math.floor(decision))          # the window ends at or before the decision
            self.assertEqual(row['window_after_decision_s'], 0.0)
            if 'prefetch' in row:
                prefetched += 1
                stamp = row['prefetch']
                self.assertLessEqual(row['collected_at'], decision)                # observed BEFORE the decision
                self.assertLessEqual(row['late_s'], 0)
                self.assertLess(stamp['asof'], decision)
                self.assertEqual(row['collected_at'], stamp['finished_at'])
                self.assertLessEqual(row['window_end'], math.floor(stamp['asof']))
            covered, end = row['covered_from'], row['window_end']
            expected = sum(1 for t, _w, _s, _a, failed in self.trades(sim, row['mint']) if not failed and covered <= int(t) <= end - 1)
            self.assertEqual(row['window_txs'], expected, row['mint'])           # not one transaction after the window end
        return prefetched

    def check_wallet(self, sim):
        prefetched = 0
        for row in rows_of(sim.runner, 'wallet_signals'):
            mint, decision = row['mint'], row['decision_ts']
            cut = row['prefetch']['asof'] if 'prefetch' in row else decision
            if 'prefetch' in row:
                prefetched += 1
                self.assertLessEqual(row['collected_at'], decision)
                self.assertLess(row['late_s'], 0)
                self.assertEqual(row['supply_source'], 'rpc_prefetch')
                self.assertLessEqual(row['smart_asof_ts'], decision)
            self.assertEqual(row['window_after_decision_s'], 0.0)
            grad = W.slot_at(sim.scene.born[mint])
            window = [(t, signer, side) for t, signer, side, _a, failed in self.trades(sim, mint)
                      if not failed and grad <= W.slot_at(t) <= grad + 150 and int(t) <= cut]
            buyers = []
            for _t, signer, side in window[:6]:
                if side == 'buy' and signer not in buyers:
                    buyers.append(signer)
            if row['tx_skipped'] == 0:                                                           # (a failed getTransaction is a skipped one)
                self.assertEqual(row['buyer_wallets'], buyers, (mint, cut))
            else:
                self.assertTrue(set(row['buyer_wallets']) <= set(buyers), (mint, cut))
            later = {signer for t, signer, side, _a, failed in self.trades(sim, mint) if not failed and side == 'buy' and int(t) > cut}
            self.assertFalse((later - set(buyers)) & set(row['buyer_wallets']))                 # nobody who first bought after the cut
        return prefetched

    def check_features(self, sim):
        prefetched = 0
        for row in rows_of(sim.runner, 'features'):
            fields = row['fields']
            if fields['tx_count_first_10m'] is None:
                continue
            born = fields['graduation_block_time']
            end = born + row['flags']['tx_count_window_s']
            self.assertLessEqual(end, row['decision_ts'] + 1e-6)
            if 'prefetch' in row:
                prefetched += 1
                self.assertLessEqual(row['collected_at'], row['decision_ts'])
                self.assertLessEqual(row['late_s'], 0)
                self.assertLess(row['prefetch']['asof'], row['decision_ts'])
                self.assertAlmostEqual(end, row['prefetch']['asof'], places=2)
            trades = sorted(self.trades(sim, row['mint']), key=lambda x: x[0])
            expected = sum(1 for t, _w, _s, _a, failed in trades[1:] if not failed and born <= int(t) <= end)
            self.assertEqual(fields['tx_count_first_10m'], expected, row['mint'])
            self.assertEqual(row['flags']['tx_count_first_10m'], 'CUT_AT_DECISION')
        return prefetched

    def check_clusters(self, sim):
        prefetched = 0
        for row in rows_of(sim.runner, 'clusters'):
            mint, decision = row['mint'], row['decision_ts']
            cut = row['prefetch']['asof'] if 'prefetch' in row else decision
            if 'prefetch' in row:
                prefetched += 1
                self.assertLessEqual(row['collected_at'], decision)
                self.assertLess(row['late_s'], 0)
                self.assertIn(row['holders_source'], ('prefetch',))
                self.assertLess(row['prefetch']['asof'], decision)
            if row.get('window_txs') is None:
                continue
            self.assertEqual(row['window_after_decision_s'], 0.0)
            grad = W.slot_at(sim.scene.born[mint])
            expected = sum(1 for t, _w, _s, _a, failed in self.trades(sim, mint) if not failed and grad <= W.slot_at(t) <= grad + 300 and int(t) <= cut)
            self.assertEqual(row['window_txs'], expected, (mint, cut))
        return prefetched

    def test_every_committed_row_holds_exactly_what_had_happened_by_its_cut(self):
        sim = self.run_sim(PLAN, prefetch=True, fault_p=0.02)
        for check in (self.check_flow, self.check_wallet, self.check_features, self.check_clusters):
            self.assertGreater(check(sim), 8, check.__name__)

    def test_a_swap_between_the_cut_and_the_first_call_is_in_no_prefetched_row(self):
        plan = [('ok', 5.0 + 70 * i) for i in range(10)]
        sim = self.run_sim(plan, prefetch=True, sparse=True, until=5.0 + 70 * 10 + 400)
        for check in (self.check_flow, self.check_wallet, self.check_features, self.check_clusters):
            self.assertGreater(check(sim), 6, check.__name__)
        # the canaries really were visible to the prefetch's calls and really came after its as-of: the test could have failed
        shown = 0
        for row in rows_of(sim.runner, 'wallet_signals'):
            if 'prefetch' not in row:
                continue
            mint = row['mint']
            index = [t.mint for t in sim.scene.tokens].index(mint) + 1
            received = sim.scene.arrival[mint]
            after = [k for k, off in enumerate(W.CANARY_OFFSETS) if received + off > row['prefetch']['asof']]
            visible = [k for k in after if received + W.CANARY_OFFSETS[k] <= row['prefetch']['started_at']]
            self.assertTrue(visible, 'no canary between the as-of and the call: the test proves nothing')
            self.assertFalse({W.canary_wallet(index, k) for k in after} & set(row['buyer_wallets']))
            shown += 1
        self.assertGreater(shown, 6)
        # and the same world WITHOUT a prefetch counts them (the collectors cut at the decision, which is after them)
        plain = self.run_sim(plan, prefetch=False, sparse=True, until=5.0 + 70 * 10 + 400)
        counted = sum(1 for row in rows_of(plain.runner, 'wallet_signals')
                      if {W.canary_wallet(i + 1, 0) for i in range(10)} & set(row['buyer_wallets']))
        self.assertGreater(counted, 6)

    def test_rugcheck_rows_never_count_a_token_created_after_the_cut(self):
        plan = [('ok', 5.0 + 70 * i) for i in range(8)]
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sim = W.Sim(Path(os.path.realpath(tmp.name)), plan, prefetch=True)
        self.addCleanup(sim.close)
        self.addCleanup(providers.configure_lanes)
        from tests.lean.test_risk_external import iso, report as rc_report
        for token, (_role, at) in zip(sim.scene.tokens, plan):
            prior = [{'mint': 'P%s' % token.mint[:30], 'marketCap': 3000, 'createdAt': iso(T0 + at - 86400)},
                     {'mint': 'Q%s' % token.mint[:30], 'marketCap': 3000, 'createdAt': iso(T0 + at + 200)}]       # AFTER every decision
            sim.scene.net.reports[token.mint] = rc_report(token.mint, score=100, name='n' + token.mint[:4], symbol='S', creator_tokens=prior)
        sim.run(plan[-1][1] + 540)
        rows = rows_of(sim.runner, 'risk_external')
        self.assertEqual(len(rows), len(plan))
        for row in rows:
            self.assertIn('prefetch', row)
            self.assertEqual(row['fields']['creator_tokens_30d'], 1, row['mint'])           # the one created a day before, never the later one
            self.assertEqual(row['fields']['creator_tokens_7d'], 1)
            self.assertLessEqual(row['collected_at'], row['decision_ts'])
            self.assertLess(row['late_s'], 0)
            self.assertLess(row['prefetch']['asof'], row['decision_ts'])

    def test_a_prefetch_that_finishes_after_the_decision_is_refused_and_the_collector_runs_as_before(self):
        # a slow world (6x the latencies): the jobs start 8 s before the earliest screen and are still running, or not yet started, at the decision
        slow = self.run_sim(PLAN[:12], prefetch=True, latency=6.0, prefetch_cfg={'lead_s': {'flow': 8.0, 'wallet_signals': 8.0, 'features': 8.0,
                                                                                            'clusters': 8.0}})
        p = slow.runner.prefetch
        late = sum(m.get('FINISHED_AFTER_DECISION', 0) for m in p.misses.values())
        self.assertGreater(late, 0)                                                           # a job finished after its decision: refused
        for kind in ('flow', 'wallet_signals', 'features', 'clusters', 'risk_external'):
            for row in rows_of(slow.runner, kind):
                stamp = row.get('prefetch')
                if stamp is not None:                                                         # whatever WAS committed was finished in time
                    self.assertLessEqual(stamp['finished_at'], row['decision_ts'], kind)
                    self.assertLessEqual(row['late_s'], 0, kind)
                    self.assertLessEqual(row['collected_at'], row['decision_ts'], kind)
        for kind in ('flow', 'wallet_signals', 'clusters'):
            rows = rows_of(slow.runner, kind)
            self.assertTrue(rows, kind)
            self.assertTrue(any('prefetch' not in r for r in rows), kind)                    # the post-screen fallback ran, as before
            self.assertTrue(all(r['late_s'] >= 0 for r in rows if 'prefetch' not in r), kind)  # and says it was not collected before the decision

    def test_the_learner_excludes_a_prefetched_row_if_it_were_late_and_keeps_a_timely_one(self):
        sim = self.run_sim(PLAN[:14], prefetch=True)
        rows = rows_of(sim.runner, 'risk_external')
        row = next(r for r in rows if 'prefetch' in r and r['fields']['rc_score'] is not None)
        policy = LN.LookaheadPolicy()
        timely = LN.risk_values(row, row['decision_ts'], policy, LN._Counts(), ('rc_score',))
        self.assertIsNotNone(timely['rc_score'])
        late = dict(row, late_s=60.0)                                          # the same row if it had been observed a minute after the decision
        excluded = LN._Counts()
        self.assertIsNone(LN.risk_values(late, row['decision_ts'], policy, excluded, ('rc_score',))['rc_score'])
        self.assertEqual(excluded.excluded, {'rc_score': {'AT_COLLECTION_LATE': 1}})


class HazardPaths(Base):
    HAZARDS = {'mayhem': 'MAYHEM_POOL', 'concentrated': 'HOLDER_CONCENTRATION_ABOVE_MAX', 'mint_authority': 'ACTIVE_MINT_AUTHORITY',
               'holder_reward': 'HOLDER_REWARD_POOL'}

    @staticmethod
    def path_rows(sim):
        db = sim.runner.paths.db.db
        return db.execute('SELECT mint, stage, reason, screen_reasons, entered, holders_checked, features, target FROM paths').fetchall()

    def test_the_sample_is_recorded_labelled_never_traded_and_counted(self):
        sim = self.run_sim(PLAN, prefetch=False, hazard_sample=0.5)
        r = sim.runner
        role = sim.scene.role
        by_mint = {row[0]: row for row in self.path_rows(sim)}
        hazards = [t.mint for t in sim.scene.tokens if role[t.mint] in self.HAZARDS]
        sampled = {m for m in hazards if L.hazard_selected(m, 0.5)}
        self.assertTrue(sampled and sampled != set(hazards))                        # a real sample: some in, some out
        got = {m for m, row in by_mint.items() if row[1] == L.HAZARD_STAGE}
        self.assertEqual(got, sampled)                                              # exactly the deterministic sample
        for mint in sampled:
            _m, stage, reason, screen_reasons, entered, _holders, features, target = by_mint[mint]
            expected = self.HAZARDS[role[mint]]
            self.assertEqual(json.loads(reason), ['hazard_reject: ' + expected])
            self.assertIn(expected, json.loads(screen_reasons))
            self.assertEqual(entered, 0)
            self.assertEqual(json.loads(target)['basis'], 'ref_size')
        # NEVER TRADED: no quote, no fill, no entry decision for any hazard (sampled or not)
        traded = {f['mint'] for f in r.store.rows('fills', limit=100000)}
        entry_decisions = {d['mint'] for d in r.store.rows('decisions', kind='entry', limit=100000)}
        self.assertFalse(traded & set(hazards))
        self.assertFalse(entry_decisions & set(hazards))
        self.assertFalse({q for q in r.store.rows('observations', kind='quote:buy', limit=100000) if q['mint'] in hazards})
        # the unsampled hazards, and nothing else without a plain path, have no path
        self.assertFalse((set(hazards) - sampled) & set(by_mint))
        for mint, row in by_mint.items():
            if role[mint] not in self.HAZARDS:
                self.assertNotEqual(row[1], L.HAZARD_STAGE)
        # marks were recorded and are real
        db = r.paths.db.db
        for mint in sampled:
            marks = db.execute("SELECT status, net_sol FROM path_marks WHERE mint=? ORDER BY ts", (mint,)).fetchall()
            self.assertGreater(len(marks), 20, role[mint])
            self.assertEqual({s for s, _ in marks}, {'OK'})
            self.assertTrue(all(float(n) > 0 for _, n in marks))
        # health counts them
        health = r.health()['paths']
        self.assertEqual(health['hazard_path_sample'], 0.5)
        self.assertEqual(health['hazard_seen'], len(hazards))
        self.assertEqual(health['hazard_sampled'], len(sampled))
        self.assertEqual(health['hazard_started'], len(sampled))
        self.assertEqual((health['hazard_unrecordable'], health['hazard_expired'], health['hazard_queued']), (0, 0, 0))
        self.assertEqual(sum(health['hazard_by_reason'].values()), len(sampled))
        self.assertEqual(health['hazard_skipped'], len(hazards) - len(sampled))
        self.assertGreaterEqual(health['hazard_probe_calls'], 1)

    def test_with_the_sample_absent_nothing_changes(self):
        sim = self.run_sim(PLAN, prefetch=False, hazard_sample=0.0)
        self.assertFalse([1 for row in self.path_rows(sim) if row[1] == L.HAZARD_STAGE])
        health = sim.runner.health()['paths']
        self.assertEqual((health['hazard_sampled'], health['hazard_started'], health['hazard_probe_calls']), (0, 0, 0))
        hazards = sum(1 for t in sim.scene.tokens if sim.scene.role[t.mint] in self.HAZARDS)
        self.assertEqual(health['hazard_skipped'], hazards)                                 # (exactly the L07R2 behaviour)

    def test_every_hazard_the_sample_takes_when_it_is_one(self):
        sim = self.run_sim(PLAN, prefetch=False, hazard_sample=1.0)
        hazards = [t.mint for t in sim.scene.tokens if sim.scene.role[t.mint] in self.HAZARDS]
        got = {row[0] for row in self.path_rows(sim) if row[1] == L.HAZARD_STAGE}
        self.assertEqual(got, set(hazards))
        self.assertEqual(sim.runner.health()['paths']['hazard_skipped'], 0)

    def test_a_hazard_that_reached_the_market_stage_starts_from_the_screens_own_figures(self):
        sim = self.run_sim(PLAN, prefetch=False, hazard_sample=1.0)
        by_mint = {row[0]: row for row in self.path_rows(sim)}
        for token in sim.scene.tokens:
            if sim.scene.role[token.mint] == 'concentrated':
                features = json.loads(by_mint[token.mint][6])
                self.assertEqual(features['holder_check'], 'OK')                       # it got past the holder stage
                self.assertNotIn('hazard_probe_at', features)                          # no extra read: the screen had everything
            if sim.scene.role[token.mint] == 'mayhem':
                self.assertIn('hazard_probe_at', json.loads(by_mint[token.mint][6]))    # stopped at the pool stage: read at start

    def test_the_trading_is_unchanged_by_the_sample(self):
        off = self.run_sim(PLAN, prefetch=False, hazard_sample=0.0)
        on = self.run_sim(PLAN, prefetch=False, hazard_sample=1.0)
        a, b = MainArm.main_view(off), MainArm.main_view(on)
        self.assertGreater(len(a[1]), 4)
        self.assertEqual(a, b)

    def test_the_learner_reports_them_separately_and_its_real_rows_are_identical_without_them(self):
        off = self.run_sim(PLAN, prefetch=False, hazard_sample=0.0)
        on = self.run_sim(PLAN, prefetch=False, hazard_sample=1.0)
        strategy = S.StrategyConfig.load(str(ROOT / 'config' / 'lean' / 'strategy-default.json'))

        def dataset(sim):
            sim.runner.write_health()
            return LN.build_dataset(sim.runner.store.path, sim.runner.paths.db.path, strategy, pool_fee_bps=25)
        a, b = dataset(off), dataset(on)
        hazards = {t.mint for t in on.scene.tokens if on.scene.role[t.mint] in self.HAZARDS}
        self.assertEqual(a.hazard, [])
        self.assertTrue(b.hazard)
        self.assertTrue({r.mint for r in b.hazard} <= hazards)
        self.assertFalse({r.mint for r in b.rows} & hazards)                       # never in the real rows
        self.assertEqual([(r.mint, r.label.label, r.enterable) for r in a.rows], [(r.mint, r.label.label, r.enterable) for r in b.rows])
        self.assertEqual(b.stats['labels'], a.stats['labels'])                      # the label counts of the real population
        self.assertEqual(b.stats['hazard_paths'], len(hazards))
        # the learner's own section
        result = LN.learn(b.rows, strategy=strategy, dataset_stats=b.stats, hazard_rows=b.hazard, min_bucket=1)
        section = result['hazard_rejects']
        self.assertEqual(section['by_reason']['ALL']['n'], len(b.hazard))
        for row in b.hazard:
            for reason in row.reasons:
                self.assertIn(reason, section['by_reason'])
        self.assertEqual(result['counts']['candidates'], len(b.rows))               # the real count excludes them
        self.assertIn('Hazard-reject sample', LN.render_html(result))
        # the winning mayhem / rugging concentrated launches of this world show up with the labels the price paths imply
        labels = {on.scene.role[r.mint]: r.label.label for r in b.hazard}
        if 'mayhem' in labels:
            self.assertEqual(labels['mayhem'], 'WIN')
        if 'concentrated' in labels:
            self.assertEqual(labels['concentrated'], 'RUG')
        # and a run without them reports no such section
        self.assertNotIn('hazard_rejects', LN.learn(a.rows, strategy=strategy, dataset_stats=a.stats, min_bucket=1))

    def test_a_hazard_path_is_not_an_outcome_for_the_smart_wallet_table(self):
        from lean import wallet_signals as WS
        sim = self.run_sim(PLAN, prefetch=False, hazard_sample=1.0)
        db_path = sim.runner.paths.db.path
        hazard = next(t.mint for t in sim.scene.tokens if sim.scene.role[t.mint] == 'mayhem')
        self.assertEqual(WS.read_path(db_path, hazard), ([], False))
        normal = next(t.mint for t in sim.scene.tokens if sim.scene.role[t.mint] == 'ok')
        self.assertTrue(WS.read_path(db_path, normal)[0])


class ProductionShape(Base):
    def test_the_production_config_unchanged_runs_with_both_features_absent(self):
        sim = self.run_sim(PLAN[:12], prefetch=False, hazard_sample=0.0)
        r = sim.runner
        self.assertIsNone(r.prefetch)
        health = r.health()
        self.assertEqual(health['prefetch'], {'enabled': False, 'alive': False})
        self.assertEqual(health['paths']['hazard_path_sample'], 0.0)
        for kind in ('flow', 'wallet_signals', 'features'):
            for row in rows_of(r, kind):
                self.assertNotIn('prefetch', row)
        self.assertTrue(r.store.rows('fills', limit=10))


if __name__ == '__main__':
    unittest.main()
