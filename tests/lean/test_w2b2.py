"""SYNTHETIC_TEST_ONLY: W2b2, the fixes of the W2b pre-deploy review (learning-data integrity). No network. One fail-first test per fix:
  #1 a prefetched row is a different feature (version / window_mode; the learner keeps it in its own ``name@p`` columns, never pooled)
  #2 holder / dev snapshots are READ by the job and stamped (``snapshot_at``); the learner refuses one older than ``max_snapshot_age_s``
  #3 no flow prefetch for a probable soft reject; the prefetch spends its own flow budget
  #4 hazard paths are not in the coverage figures of the learner; the example samples 10 %
  #5 health: probe fp rate, low-lane counters, held-risk probes shed by reason
  #6 a job running at the decision ends cancelled, never done; ``take`` is single-use
  #7 discovery never holds the lock across a store read
  #8 a prefetch's raw bodies carry the candidate id
"""
import json
import socket
import threading
import unittest
from pathlib import Path
from decimal import Decimal
from unittest import mock

from lean import learn as LN, paths as L, prefetch as PF, strategy as S
from lean.candidates import Candidate, Screen
from tests.lean import w2bworld as W
from tests.lean.test_e2e_w2b import Base, PLAN, rows_of
from tests.lean.fakeworld import T0

ROOT = Path(__file__).resolve().parents[2]
STRATEGY = S.StrategyConfig.load(str(ROOT / 'config' / 'lean' / 'strategy-default.json'))
KINDS = ('features', 'flow', 'wallet_signals', 'clusters', 'risk_external')


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def dataset(sim):
    sim.runner.write_health()
    return LN.build_dataset(sim.runner.store.path, sim.runner.paths.db.path, STRATEGY, pool_fee_bps=25)


# ================================================================================================================= #1
class PrefetchIsADifferentFeature(Base):
    def test_every_prefetched_row_says_so_in_its_version_and_its_mode_and_a_fallback_row_does_not(self):
        on = self.run_sim(PLAN, prefetch=True)
        off = self.run_sim(PLAN, prefetch=False)
        expected = {'features': ('3p', 'features_version'), 'flow': ('flow_v1p', 'version'), 'wallet_signals': ('wallet_signals_v2p', 'version'),
                    'clusters': ('clusters_v1p', 'version'), 'risk_external': ('risk_external_v1p', 'version')}
        for kind, (version, key) in expected.items():
            rows = [r for r in rows_of(on.runner, kind) if 'prefetch' in r]
            self.assertGreater(len(rows), 8, kind)
            for row in rows:
                self.assertEqual((row[key], row['window_mode']), (version, 'prefetch'), kind)
            for row in rows_of(off.runner, kind) + [r for r in rows_of(on.runner, kind) if 'prefetch' not in r]:
                self.assertNotIn('window_mode', row, kind)                           # a decision-time row is byte-for-byte what it was
                self.assertEqual(row[key], 3 if kind == 'features' else version[:-1], kind)

    def test_the_learner_puts_the_prefetch_in_its_own_columns_and_never_pools_it(self):
        on = self.run_sim(PLAN, prefetch=True)
        ds = dataset(on)
        self.assertTrue(ds.rows)
        self.assertEqual(set(ds.stats['feature_names']), set(LN.FEATURE_NAMES))
        self.assertIn('window_modes', ds.stats)
        saw = {'p': 0, 'base': 0}
        for row in ds.rows:
            f = row.features
            for name in LN.FLOW_FEATURES + LN.WALLET_FEATURES + LN.RISK_FEATURES:
                self.assertFalse(f[name] is not None and f[name + LN.PREFETCH_SUFFIX] is not None, name)     # never both: never pooled
            if f['buy_sell_ratio@p'] is not None:
                saw['p'] += 1
                self.assertIsNone(f['buy_sell_ratio'])
            if f['market_cap_usd'] is not None:
                saw['base'] += 1                                                       # the screen's own figures stay in their usual column
            self.assertNotIn('market_cap_usd@p', f)
        self.assertGreater(saw['p'], 3)
        self.assertGreater(saw['base'], 3)
        self.assertGreater(ds.stats['window_modes']['flow']['prefetch'], 3)

    def test_a_world_without_a_prefetch_fills_only_the_decision_columns(self):
        off = self.run_sim(PLAN, prefetch=False)
        ds = dataset(off)
        self.assertTrue(ds.rows)
        self.assertNotIn('window_modes', ds.stats)
        for row in ds.rows:
            self.assertEqual({n for n in LN.PREFETCH_FEATURE_NAMES if row.features[n] is not None}, set())
        self.assertTrue(any(row.features['buy_sell_ratio'] is not None for row in ds.rows))

    def test_a_loaded_world_mixes_prefetched_and_fallback_rows_and_they_stay_apart(self):
        sim = self.run_sim(PLAN, prefetch=True, latency=2.2)
        modes = {kind: {('prefetch' if 'prefetch' in r else 'decision') for r in rows_of(sim.runner, kind)} for kind in ('flow', 'wallet_signals')}
        self.assertTrue(any(len(m) == 2 for m in modes.values()), modes)          # both kinds of row exist in one config hash
        ds = dataset(sim)
        for row in ds.rows:
            for name in LN.FLOW_FEATURES + LN.WALLET_FEATURES:
                self.assertFalse(row.features[name] is not None and row.features[name + LN.PREFETCH_SUFFIX] is not None, name)
        both = {name for name in LN.FLOW_FEATURES if any(r.features[name] is not None for r in ds.rows)
                and any(r.features[name + LN.PREFETCH_SUFFIX] is not None for r in ds.rows)}
        self.assertTrue(both)                                                          # and each column has its own population

    def test_the_guards_refuse_a_version_that_disagrees_with_the_mode(self):
        policy = LN.LookaheadPolicy()
        flow = {'version': 'flow_v1p', 'window_mode': 'prefetch', 'window_after_decision_s': 0.0, 'buy_sell_ratio': 1.5}
        self.assertEqual(LN.flow_values(flow, 100.0, policy, LN._Counts(), ('buy_sell_ratio',))['buy_sell_ratio'], 1.5)
        for bad in ({'version': 'flow_v1p'}, {'window_mode': 'prefetch', 'version': 'flow_v1'}, {'version': 'flow_v1', 'window_mode': 'prefetch'},
                    {'version': 'flow_v1p', 'window_mode': 'decision'}):
            counts = LN._Counts()
            row = {**flow, **bad}
            if 'window_mode' not in bad and bad.get('version') == 'flow_v1p':
                del row['window_mode']
            self.assertIsNone(LN.flow_values(row, 100.0, policy, counts, ('buy_sell_ratio',))['buy_sell_ratio'], bad)
            self.assertEqual(counts.skipped, {'FLOW_OLD_VERSION': 1}, bad)
        features = {'features_version': '3p', 'window_mode': 'prefetch', 'fields': {'dev_holding_pct': '2.0'}, 'flags': {}, 'late_s': -5.0}
        self.assertIsNotNone(LN.feature_values(features, 100.0, policy, LN._Counts(), ('dev_holding_pct',))['dev_holding_pct'])
        counts = LN._Counts()
        self.assertIsNone(LN.feature_values({**features, 'features_version': 3}, 100.0, policy, counts, ('dev_holding_pct',))['dev_holding_pct'])
        self.assertEqual(counts.skipped, {'FEATURES_BAD_PREFETCH_VERSION': 1})

    def test_only_the_fields_the_prefetch_measured_leave_the_features_row(self):
        sim = self.run_sim(PLAN, prefetch=True)
        rows = [r for r in rows_of(sim.runner, 'features') if 'prefetch' in r]
        self.assertTrue(rows)
        for row in rows:
            measured = set(row['prefetch_fields'])
            self.assertTrue(measured <= {'dev_holding_pct', 'tx_count_first_10m', 'top1_pct', 'top10_pct'})
            self.assertNotIn('market_cap_usd', measured)
            self.assertNotIn('seconds_graduation_to_screen', measured)
            if sim.scene.role[row['mint']] == 'ok':
                self.assertNotIn('top10_pct', measured)                                # a PASS keeps the screen's own holder shares
        ds = dataset(sim)
        self.assertTrue(any(r.features['tx_count_first_10m@p'] is not None and r.features['market_cap_usd'] is not None for r in ds.rows))

    def test_a_rule_on_a_prefetch_column_needs_the_same_wait_as_its_decision_column(self):
        rule = LN.Rule((LN.Condition('buy_sell_ratio@p', '>', LN.Decimal('2')),))
        self.assertIn('wait_for_flow:buy_sell_ratio', LN.runner_changes([rule]))


# ================================================================================================================= #2
class SnapshotsAreStampedAndAged(Base):
    def test_a_soft_reject_reads_its_holder_list_in_the_job_not_from_the_probe(self):
        sim = self.run_sim(PLAN, prefetch=True)
        soft = [r for r in rows_of(sim.runner, 'features') if sim.scene.role[r['mint']] == 'mcap_high' and 'prefetch' in r]
        self.assertTrue(soft)
        for row in soft:
            entry = sim.runner.prefetch.entries.get(row['mint'])
            at = row['snapshot_at']
            self.assertEqual(set(at), {'dev_holding_pct', 'top1_pct', 'top10_pct'})
            self.assertEqual(at['top10_pct'], at['top1_pct'])
            self.assertGreater(at['top10_pct'], row['prefetch']['asof'])           # read by the job (after its as-of), ...
            self.assertLessEqual(at['top10_pct'], row['collected_at'])                # ... before the row was complete
            self.assertGreater(row['decision_ts'] - at['top10_pct'], 0)
            if entry is not None and entry.probe_at:
                self.assertGreater(at['top10_pct'] - entry.probe_at, 5.0)             # nowhere near the probe's ~+2 s snapshot
            self.assertEqual(row['flags']['top10_pct'], 'AT_COLLECTION')

    def test_a_clusters_row_stamps_the_time_its_holder_list_was_read(self):
        sim = self.run_sim(PLAN, prefetch=True)
        rows = [r for r in rows_of(sim.runner, 'clusters') if 'prefetch' in r]
        self.assertGreater(len(rows), 8)
        for row in rows:
            self.assertEqual(row['holders_source'], 'prefetch')
            self.assertNotIn('AT_COLLECTION', row['flags'].values())          # read before the decision, so the arms may use it
            at = row['snapshot_at']['holders']
            self.assertGreater(at, row['prefetch']['asof'] - 5)
            self.assertLessEqual(at, row['collected_at'])

    def test_the_learner_refuses_a_snapshot_older_than_the_limit_and_keeps_a_fresh_one(self):
        policy = LN.LookaheadPolicy()
        self.assertEqual(policy.max_snapshot_age_s, LN.DEFAULT_MAX_SNAPSHOT_AGE_S)
        row = {'features_version': '3p', 'window_mode': 'prefetch', 'late_s': -20.0, 'fields': {'top10_pct': '31.0', 'dev_holding_pct': '2.0'},
               'flags': {'top10_pct': 'AT_COLLECTION', 'dev_holding_pct': 'AT_COLLECTION'}, 'snapshot_at': {'top10_pct': 960.0, 'dev_holding_pct': 1030.0}}
        counts = LN._Counts()
        got = LN.feature_values(row, 1062.0, policy, counts, ('top10_pct', 'dev_holding_pct'))
        self.assertIsNone(got['top10_pct'])                                            # 102 s old
        self.assertIsNotNone(got['dev_holding_pct'])                                   # 32 s old
        self.assertEqual(counts.excluded, {'top10_pct': {'SNAPSHOT_TOO_OLD': 1}})
        relaxed = LN.feature_values(row, 1062.0, LN.LookaheadPolicy(max_snapshot_age_s=120), LN._Counts(), ('top10_pct',))
        self.assertIsNotNone(relaxed['top10_pct'])
        cluster = {'version': 'clusters_v1p', 'window_mode': 'prefetch', 'holders_source': 'prefetch', 'supply_source': 'prefetch',
                   'late_s': -20.0, 'window_after_decision_s': 0.0, 'largest_cluster_supply_pct': 12.0, 'jito_bundled_buys': 2,
                   'flags': {}, 'snapshot_at': {'holders': 980.0}}
        old = LN.cluster_values(cluster, 1062.0, policy, LN._Counts(), ('largest_cluster_supply_pct', 'jito_bundled_buys'))
        self.assertIsNone(old['largest_cluster_supply_pct'])                           # 82 s old holder list
        self.assertEqual(old['jito_bundled_buys'], 2)                                  # the pool-window figure is not a snapshot
        fresh = LN.cluster_values({**cluster, 'snapshot_at': {'holders': 1040.0}}, 1062.0, policy, LN._Counts(), ('largest_cluster_supply_pct',))
        self.assertEqual(fresh['largest_cluster_supply_pct'], 12)
        for bad in (-1, True, float('nan')):
            with self.assertRaises(LN.LearnError):
                LN.LookaheadPolicy(max_snapshot_age_s=bad)

    def test_a_fallback_row_has_no_stamp_and_so_no_age_limit(self):
        row = {'features_version': 3, 'late_s': 4.0, 'fields': {'dev_holding_pct': '2.0'}, 'flags': {'dev_holding_pct': 'AT_COLLECTION'}}
        self.assertIsNotNone(LN.feature_values(row, 1062.0, LN.LookaheadPolicy(), LN._Counts(), ('dev_holding_pct',))['dev_holding_pct'])


# ================================================================================================================= #3
class FlowOnlyWhereItCanBeUsed(Base):
    def test_no_flow_is_prefetched_for_a_probable_soft_reject_and_the_gate_is_counted(self):
        sim = self.run_sim(PLAN, prefetch=True, prefetch_cfg={'ttl_s': 86400})
        p = sim.runner.prefetch
        soft = [e for m, e in p.entries.items() if sim.scene.role[m] in ('mcap_high', 'liquidity_low')]
        self.assertTrue(soft)
        for entry in soft:
            self.assertTrue(entry.soft)
            self.assertEqual((entry.jobs['flow'].state, entry.jobs['flow'].why), ('skipped', 'SOFT_REJECT'))
            self.assertEqual(entry.jobs['features'].payload['need_holders'], True)         # ... and its holder list is read by the job instead
        for m, entry in p.entries.items():
            if sim.scene.role[m] == 'ok':
                self.assertIn(entry.soft, (False, None))                                   # (None: before the runner has cached a SOL/USD rate)
                self.assertEqual(entry.jobs['flow'].state, 'done')
                self.assertEqual(entry.jobs['features'].payload['need_holders'], False)
        gate = p.health()['flow_gate']
        self.assertEqual(gate['skipped_soft'], len(soft))
        self.assertEqual((gate['skipped_soft_but_passed'], gate['ran_but_not_passed']), (0, 0))
        self.assertFalse([1 for row in rows_of(sim.runner, 'flow') if sim.scene.role[row['mint']] in ('mcap_high', 'liquidity_low')])

    def test_without_a_sol_usd_rate_nothing_is_skipped_on_the_bands(self):
        sim = self.run_sim(PLAN[:10], prefetch=True, until=100.0)
        p = sim.runner.prefetch
        probe = Screen(True, (), {'market_cap_usd': '1000', 'liquidity_usd': '100'}, (), ())
        p.sol_usd = None
        self.assertIsNone(p._soft(probe))
        p.sol_usd = lambda: None
        self.assertIsNone(p._soft(probe))
        p.sol_usd = lambda: 150
        cfg = p.screen_cfg
        mid = (Decimal(str(cfg['min_market_cap_usd'])) + Decimal(str(cfg['max_market_cap_usd']))) / 2
        liq = Decimal(str(cfg['min_liquidity_usd'])) * 3
        inside = Screen(True, (), {'market_cap_usd': str(mid / 150), 'liquidity_usd': str(liq / 150)}, (), ())
        self.assertFalse(p._soft(inside))                                                 # the probe's SOL-denominated figures x 150 are inside the bands
        for caps, liqs in ((mid * 100, liq), (mid / 1000, liq), (mid, liq / 100)):        # above the max / below the min / liquidity below its min
            outside = Screen(True, (), {'market_cap_usd': str(caps / 150), 'liquidity_usd': str(liqs / 150)}, (), ())
            self.assertTrue(p._soft(outside))
        self.assertIsNone(p._soft(Screen(True, (), {}, (), ())))                          # no market figures: unknown

    def test_the_prefetch_spends_its_own_flow_budget_and_the_post_screen_cap_is_not_touched(self):
        sim = self.run_sim(PLAN, prefetch=True)
        flow = sim.runner.flow
        self.assertIsNotNone(flow.prefetch_budget)
        self.assertIsNot(flow.prefetch_budget, flow.budget)
        self.assertGreater(flow.prefetch_budget.used(), 50)                               # the prefetch's calls ...
        self.assertEqual(flow.budget.used(), flow.health()['calls_last_hour'])
        self.assertLess(flow.budget.used(), flow.prefetch_budget.used())                  # ... did not go through the post-screen budget
        self.assertEqual(flow.health()['prefetch_calls_last_hour'], flow.prefetch_budget.used())

    def test_an_exhausted_prefetch_budget_leaves_the_fallback_whole(self):
        sim = self.run_sim(PLAN, prefetch=True, prefetch_cfg={'flow_calls_per_hour': 1})
        flow = sim.runner.flow
        self.assertLessEqual(flow.prefetch_budget.used(), 1)
        rows = rows_of(sim.runner, 'flow')
        passed = {d['mint'] for d in sim.runner.store.rows('decisions', kind='screen', limit=100000) if d['action'] == 'PASS'}
        self.assertEqual({r['mint'] for r in rows}, passed)                                # every PASS still has its flow row (the fallback ran)
        self.assertLessEqual(sum(1 for r in rows if 'prefetch' in r), 1)                  # (the one call the prefetch's budget allowed: a partial row)
        self.assertGreater(flow.budget.used(), 50)


# ================================================================================================================= #4
class HazardPathsAreNotCoverage(Base):
    HAZARDS = {'mayhem', 'concentrated', 'mint_authority', 'holder_reward'}

    def test_the_coverage_figures_are_the_same_with_and_without_the_sample(self):
        off = self.run_sim(PLAN, prefetch=False, hazard_sample=0.0)
        on = self.run_sim(PLAN, prefetch=False, hazard_sample=1.0)
        a, b = dataset(off), dataset(on)
        self.assertGreater(b.stats['hazard_paths'], 5)
        self.assertEqual(b.stats['paths'], a.stats['paths'])                          # the real population's path count: hazards not in it
        self.assertEqual(b.stats['skipped'], a.stats['skipped'])
        self.assertEqual(b.stats['labels'], a.stats['labels'])
        self.assertEqual(b.stats['with_features'], a.stats['with_features'])
        self.assertIn('hazard_skipped', b.stats)                                       # their own skips are counted apart

    def test_a_hazard_path_that_cannot_be_decided_is_a_hazard_skip_not_a_coverage_skip(self):
        on = self.run_sim(PLAN, prefetch=False, hazard_sample=1.0)
        hazards = {t.mint for t in on.scene.tokens if on.scene.role[t.mint] in self.HAZARDS}
        real = LN.decision_time

        def blind_to_hazards(decisions, candidate_id, start_ts):
            return None if candidate_id in {on.runner.store.candidate_id(m) for m in hazards} else real(decisions, candidate_id, start_ts)
        with mock.patch.object(LN, 'decision_time', blind_to_hazards):
            ds = dataset(on)
        self.assertGreater(ds.stats['hazard_skipped'].get('DECISION_UNKNOWN', 0), 5)
        self.assertNotIn('DECISION_UNKNOWN', ds.stats['skipped'])                         # the real population's coverage is untouched
        self.assertEqual(ds.hazard, [])

    def test_the_example_samples_ten_percent(self):
        example = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        self.assertEqual(example['paths']['hazard_path_sample'], 0.1)


# ================================================================================================================= #5
class RollbackSignals(Base):
    def test_health_has_the_probe_false_positive_rate(self):
        p = PF.Prefetch(cfg=PF.config({'enabled': True}), store=mock.Mock(), helius=mock.Mock(transport=None), discovery_db='x',
                        screen_cfg={'min_age_seconds': 60}, min_age_s=60, clock=lambda: 1000.0)
        self.assertIsNone(p.health()['probe_fp_rate'])
        self.assertEqual(PF.Prefetch._fp_rate({'tp': 5, 'fp': 1, 'fn': 0, 'tn': 9, 'unknown': 0}), 0.1)
        self.assertEqual(PF.Prefetch._fp_rate({'tp': 5, 'fp': 3, 'fn': 0, 'tn': 6, 'unknown': 0}), 0.3333)
        self.assertEqual(PF.Prefetch._fp_rate({'tp': 5, 'fp': 0, 'fn': 2, 'tn': 0, 'unknown': 0}), None)

    def test_the_runner_exposes_the_low_lane_and_the_held_risk_sheds_apart(self):
        sim = self.run_sim(PLAN[:10], prefetch=True, patch={'held_risk': {'enabled': True}})
        health = sim.runner.health()
        lane = health['low_lane']
        self.assertEqual(set(lane['lane']), {'granted', 'shed_no_token', 'shed_backoff', 'sheds'})
        self.assertGreater(lane['lane']['granted'], 10)
        self.assertIsNotNone(sim.runner.held_risk)
        self.assertIn('held_risk_shed', lane)
        probe = health['prefetch']['probe_vs_screen']
        self.assertEqual(set(probe), {'tp', 'fp', 'fn', 'tn', 'unknown'})
        self.assertIn('probe_fp_rate', health['prefetch'])

    def test_a_shed_held_risk_probe_is_counted_by_why(self):
        from lean import held_risk as H, providers
        counts = {'shed': 0}
        fake = mock.Mock(counts=counts)
        for why in ('no_token', 'backoff', None):
            H.HeldRisk._count_shed(fake, providers.ProviderError(providers.LANE_SHED, True, meta={'why': why} if why else {}))
        self.assertEqual(counts, {'shed': 3, 'shed_no_token': 1, 'shed_backoff': 1})


# ================================================================================================================= #6 / #7
class Jobs(unittest.TestCase):
    def setUp(self):
        self.t = [1000.0]
        self.p = PF.Prefetch(cfg=PF.config({'enabled': True}), store=mock.Mock(candidate_id=mock.Mock(return_value=None)), helius=mock.Mock(transport=None),
                             discovery_db='x', screen_cfg={'min_age_seconds': 60}, min_age_s=60, clock=lambda: self.t[0], sleep=lambda s: None)
        self.entry = PF.Entry(Candidate(1, 'MintA', 'PoolA', 'sig', 7, 1000.0, 'h'), 1000.0)
        self.p.entries['MintA'] = self.entry

    def test_a_job_running_at_the_decision_ends_cancelled_and_is_never_counted_done(self):
        job = self.entry.jobs['flow']

        def collect(*args, sink=None, **kw):
            self.p.on_decision('MintA', 1062.0, Screen(True, (), {}, (), ()))        # the decision comes while the job runs
            sink.update(body={'x': 1}, finished_at=1063.0)
        self.p.flow = mock.Mock(collect=collect)
        self.entry.jobs['probe'].state, self.entry.gate = 'done', 'FREE'
        self.t[0] = 1060.0
        self.p._run(self.entry, job)
        self.assertEqual((job.state, job.why), ('cancelled', 'DECIDED_WHILE_RUNNING'))
        self.assertIsNone(self.p.take('flow', 'MintA', 1070.0))
        self.assertEqual(self.p.counts['jobs_done'], 0)
        self.assertEqual(self.p.health()['job_states'].get('flow:done'), None)
        self.assertEqual(self.p.counts['cancelled'], len([1 for j in self.entry.jobs.values() if j.state == 'cancelled' or j.cancelled]))

    def test_a_job_that_asks_for_a_retry_after_the_decision_is_cancelled_too(self):
        from lean import flow as FL
        job = self.entry.jobs['flow']

        def collect(*args, sink=None, **kw):
            self.p.on_decision('MintA', 1062.0, Screen(True, (), {}, (), ()))
            raise FL._Retry('LANE_SHED')
        self.p.flow = mock.Mock(collect=collect)
        self.p._run(self.entry, job)
        self.assertEqual(job.state, 'cancelled')
        self.assertEqual(self.p.counts['retries'], 0)

    def test_take_is_single_use(self):
        job = self.entry.jobs['risk_external']
        job.state, job.asof, job.started_at, job.finished_at, job.payload = 'done', 1020.0, 1021.0, 1025.0, {'p': 1}
        self.assertIsNotNone(self.p.take('risk_external', 'MintA', 1062.0))
        self.assertIsNone(self.p.take('risk_external', 'MintA', 1062.0))               # the same decision again
        self.assertIsNone(self.p.take('risk_external', 'MintA', 1100.0))               # a later re-screen of the mint
        self.assertEqual(self.p.misses['risk_external'], {'ALREADY_USED': 2})
        self.assertEqual(self.p.used['risk_external'], 1)

    def test_discovery_reads_the_store_outside_the_lock(self):
        started, release = threading.Event(), threading.Event()

        def slow(mint):
            started.set()
            release.wait(10)
            return None
        self.p.store.candidate_id = slow
        self.p._cursor = 0
        scan = mock.Mock(candidates=[Candidate(2, 'MintB', 'PoolB', 'sig', 7, 1000.0, 'h')], next_cursor=2)
        with mock.patch('lean.prefetch.C.scan_new', return_value=scan):
            thread = threading.Thread(target=self.p._discover, args=(1000.0,), daemon=True)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                done = threading.Event()

                def decide():
                    self.p.on_decision('MintA', 1062.0, Screen(True, (), {}, (), ()))
                    done.set()
                threading.Thread(target=decide, daemon=True).start()
                self.assertTrue(done.wait(2), 'on_decision waited behind the discovery thread\'s store read')
            finally:
                release.set()
                thread.join(5)
        self.assertIn('MintB', self.p.entries)

    def test_an_already_handled_mint_is_not_an_entry(self):
        self.p.store.candidate_id = lambda mint: 7 if mint == 'MintC' else None
        self.p._cursor = 0
        scan = mock.Mock(candidates=[Candidate(3, 'MintC', 'PoolC', 'sig', 7, 1000.0, 'h'), Candidate(4, 'MintD', 'PoolD', 'sig', 7, 1000.0, 'h')],
                         next_cursor=4)
        with mock.patch('lean.prefetch.C.scan_new', return_value=scan):
            self.p._discover(1000.0)
        self.assertNotIn('MintC', self.p.entries)
        self.assertIn('MintD', self.p.entries)
        self.assertEqual(self.p.counts['already_handled'], 1)


# ================================================================================================================= #8
class RawBodiesCarryTheCandidate(Base):
    def test_a_prefetchs_raw_wallet_and_rugcheck_bodies_are_written_with_the_candidate_id(self):
        sim = self.run_sim(PLAN[:12], prefetch=True, wallet_retain_raw='signatures')
        raws = sim.runner.store.rows('observations', limit=100000)
        kinds = {}
        for o in raws:
            if o['kind'].startswith('wallet_signals:raw:') or o['kind'] == 'risk_external:rugcheck':
                kinds.setdefault(o['kind'].split(':')[0], []).append(o)
        self.assertTrue(kinds.get('wallet_signals'), 'no raw wallet body kept')
        self.assertTrue(kinds.get('risk_external'), 'no raw RugCheck body kept')
        for name, rows in kinds.items():
            self.assertTrue(all(o['candidate_id'] is not None for o in rows), name)
        prefetched = {r['mint'] for r in rows_of(sim.runner, 'wallet_signals') if 'prefetch' in r}
        self.assertTrue(prefetched & {o['mint'] for o in kinds['wallet_signals']})            # ... including those of the prefetched rows


if __name__ == '__main__':
    unittest.main()
