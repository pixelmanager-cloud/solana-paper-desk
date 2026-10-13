"""SYNTHETIC_TEST_ONLY: W2b unit tests (#8 prefetch, #9 hazard-reject path sample). No network. The end-to-end proofs are in
tests/lean/test_e2e_w2b.py; this module pins the pieces they rest on: the deterministic sample, the configs (strict at load, the production
config unchanged), the prefetch's use rules (a result is usable only if it finished AND is as-of before the decision) and its readiness
counters."""
import copy
import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lean import learn as LN, paths as L, prefetch as PF
from lean.__main__ import CONFIG_KEYS, ConfigError, lean_config_hash, load_config
from lean.candidates import Candidate, Screen

ROOT = Path(__file__).resolve().parents[2]
PROD = ROOT / 'tests' / 'lean' / 'lean.r1-prod.json'


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


# ============================================================================================================ #9 the sample
class HazardSampleTests(unittest.TestCase):
    MINTS = ['%s%042d' % (c, i) for i in range(1000) for c in 'M']

    def test_it_is_a_pure_function_of_the_mint_and_the_fraction(self):
        for mint in self.MINTS[:50]:
            first = L.hazard_selected(mint, 0.2)
            self.assertEqual([L.hazard_selected(mint, 0.2) for _ in range(5)], [first] * 5)

    def test_golden_values_pin_the_hash_so_a_restart_a_replay_and_another_machine_agree(self):
        digest = hashlib.sha256(b'lean.paths.hazard_sample:' + b'GoldenMint1111111111111111111111111111111').digest()
        expected = int.from_bytes(digest[:8], 'big') / 2.0 ** 64
        self.assertAlmostEqual(expected, 0.431049398, places=8)         # (a literal: the salt and the byte order are part of the contract)
        self.assertTrue(L.hazard_selected('GoldenMint1111111111111111111111111111111', 0.44))
        self.assertFalse(L.hazard_selected('GoldenMint1111111111111111111111111111111', 0.43))
        # three more, by their literal position in [0, 1): 0.2481 is in a 25 % sample, 0.6827 and 0.7617 are not in a 50 % one
        self.assertTrue(L.hazard_selected('Mint2', 0.25))
        self.assertFalse(L.hazard_selected('Mint4', 0.5))
        self.assertFalse(L.hazard_selected('Mint0', 0.5))

    def test_zero_is_never_one_is_always_and_a_larger_sample_contains_the_smaller(self):
        self.assertFalse(any(L.hazard_selected(m, 0) for m in self.MINTS))
        self.assertTrue(all(L.hazard_selected(m, 1) for m in self.MINTS))
        small = {m for m in self.MINTS if L.hazard_selected(m, 0.2)}
        large = {m for m in self.MINTS if L.hazard_selected(m, 0.5)}
        self.assertLess(small, large)

    def test_the_share_is_about_the_fraction(self):
        for fraction in (0.1, 0.2, 0.5):
            share = sum(1 for m in self.MINTS if L.hazard_selected(m, fraction)) / len(self.MINTS)
            self.assertAlmostEqual(share, fraction, delta=0.05)

    def test_hazard_reject_means_a_reject_for_a_hazard_not_a_soft_band_or_a_failed_screen(self):
        def screen(reasons, passed=False, error=None):
            return Screen(passed, tuple(reasons), {}, (), (), error)
        self.assertTrue(L.is_hazard_reject(screen(['MAYHEM_POOL'])))
        self.assertTrue(L.is_hazard_reject(screen(['MARKET_CAP_ABOVE_MAX', 'HOLDER_REWARD_POOL'])))
        self.assertFalse(L.is_hazard_reject(screen(['MARKET_CAP_ABOVE_MAX', 'LIQUIDITY_BELOW_MIN'])))
        self.assertFalse(L.is_hazard_reject(screen([], passed=True)))
        self.assertFalse(L.is_hazard_reject(screen(['PROVIDER_ERROR:HTTP_503'], error={'code': 'HTTP_503', 'transient': True})))
        self.assertFalse(L.is_hazard_reject(None))

    def test_the_label_names_every_screen_reason(self):
        s = Screen(False, ('MAYHEM_POOL', 'HOLDER_REWARD_POOL'), {}, (), ())
        self.assertEqual(L.hazard_reasons(s), ['hazard_reject: MAYHEM_POOL', 'hazard_reject: HOLDER_REWARD_POOL'])
        self.assertEqual(L.HAZARD_STAGE, 'hazard_reject')


class PathsConfigTests(unittest.TestCase):
    def test_absent_is_zero_and_the_example_uses_point_two(self):
        self.assertEqual(L.config(None)['hazard_path_sample'], 0.0)
        self.assertEqual(L.config({'enabled': True})['hazard_path_sample'], 0.0)
        example = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        self.assertEqual(L.config(example['paths'])['hazard_path_sample'], 0.2)

    def test_it_is_a_number_in_zero_one(self):
        for bad in (-0.1, 1.5, True, '0.2', None, float('nan')):
            with self.assertRaises(ValueError, msg=repr(bad)):
                L.config({'hazard_path_sample': bad})
        self.assertEqual(L.config({'hazard_path_sample': 1})['hazard_path_sample'], 1.0)


class HazardRecorderTests(unittest.TestCase):
    """The recorder's side of #9 on its own: queue, first read, shed, expiry, unrecordable. The vault reads are a fake low-lane Helius."""

    def setUp(self):
        from tests.lean import test_paths as TPATH
        self.T = TPATH
        TPATH.P.configure_lanes()
        self.addCleanup(TPATH.P.configure_lanes)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = TPATH.Clock()
        self.rpc = TPATH.Rpc()
        self.db = L.PathStore(Path(self.tmp.name) / 'paths.sqlite', clock=self.clock.now)
        self.addCleanup(self.db.close)
        c = self.clock
        transport = TPATH.P.Transport('helius', opener=self.rpc, clock=c.now, monotonic=c.now, sleep=c.sleep, rng=lambda: 0.5, lane='low')
        self.rec = L.PathRecorder(db=self.db, helius=TPATH.P.Helius(TPATH.KEY, transport), pcfg=TPATH.PCFG, code_version='t', strategy_version='s',
                                  pool_fee_bps=25, clock=c.now, sleep=c.sleep, min_free_gb=0, hazard_path_sample=1.0)

    def hazard(self, i=0, role='mayhem'):
        from tests.lean import w2bworld as W
        from desk.providers import PUMPSWAP
        token = W.make_token(role, i)
        body = json.dumps({'result': {'value': [token.mint_account(), token.account(token.pool_raw, PUMPSWAP)]}}).encode()
        self.rpc.amounts[token.base_vault], self.rpc.amounts[token.quote_vault] = 2 * 10 ** 14, 80 * 10 ** 9
        screen = Screen(False, ('MAYHEM_POOL',), {'age_seconds': 62.0}, (), (('accounts_mint_pool', body),))
        return token, {'mint': token.mint, 'cid': i + 1, 'screen': screen, 'entry_reasons': None, 'fill': None, 'error': None}

    def paths(self):
        return self.db.db.execute('SELECT mint, stage, reason, entered FROM paths').fetchall()

    def test_a_queued_hazard_is_started_by_the_next_poll_with_one_read_of_its_two_vaults(self):
        token, note = self.hazard()
        self.assertTrue(self.rec.on_candidate(note))
        self.assertEqual(self.paths(), [])                                           # nothing is written on the trader's thread
        self.assertEqual(self.rpc.requests, [])                                      # and nothing is requested there
        self.rec.poll()
        self.assertEqual(self.rpc.requests[0], [token.base_vault, token.quote_vault])      # ONE read, the two vaults, then the mark
        self.assertEqual(self.paths(), [(token.mint, 'hazard_reject', '["hazard_reject: MAYHEM_POOL"]', 0)])
        health = self.rec.health()
        self.assertEqual((health['hazard_seen'], health['hazard_sampled'], health['hazard_started'], health['hazard_queued']), (1, 1, 1, 0))
        self.assertEqual(health['hazard_by_reason'], {'MAYHEM_POOL': 1})

    def test_a_pool_without_a_retained_account_body_cannot_be_recorded(self):
        _token, note = self.hazard()
        note['screen'] = Screen(False, ('MAYHEM_POOL',), {}, (), ())
        self.rec.on_candidate(note)
        self.rec.poll()
        self.assertEqual(self.paths(), [])
        self.assertEqual(self.rec.health()['hazard_unrecordable'], 1)

    def test_a_shed_lane_keeps_the_sample_for_the_next_poll_and_an_old_one_is_dropped(self):
        token, note = self.hazard()
        self.rec.on_candidate(note)
        self.rec.helius.transport.limiter.shed(60)                                   # the low lane is shed: nothing is sent
        self.rec.poll()
        self.assertEqual((self.paths(), self.rpc.requests), ([], []))
        self.assertEqual(self.rec.health()['hazard_queued'], 1)                      # still waiting
        self.clock.t += 61
        self.rec.poll()
        self.assertEqual(len(self.paths()), 1)                                       # started once the lane is back
        old_token, old = self.hazard(1)
        self.rec.on_candidate(old)
        self.clock.t += L.HAZARD_MAX_WAIT_S + 1
        self.rec.poll()
        self.assertEqual(len(self.paths()), 1)
        self.assertEqual(self.rec.health()['hazard_expired'], 1)

    def test_the_queue_is_bounded_and_an_unsampled_hazard_is_only_counted(self):
        _t, note = self.hazard()
        self.rec.hazard_path_sample = 0.0
        self.rec.on_candidate(note)
        self.assertEqual(self.rec.health()['hazard_queued'], 0)
        self.assertEqual((self.rec.health()['hazard_seen'], self.rec.health()['hazard_skipped'], self.rec.health()['hazard_sampled']), (1, 1, 0))
        self.rec.hazard_path_sample = 1.0
        for i in range(L.HAZARD_QUEUE_MAX):
            self.rec._hazard_queue.append({'mint': 'm%d' % i, 'cid': None, 'screen': note['screen'], 'queued_at': self.clock.now()})
        _t2, another = self.hazard(2)
        self.assertFalse(self.rec.on_candidate(another))                              # full: counted as an unrecorded hazard
        self.assertEqual(self.rec.health()['hazard_queue_full'], 1)

    def test_a_failed_screen_and_a_soft_reject_are_not_hazards(self):
        _t, note = self.hazard()
        for screen in (Screen(False, ('HOLDERS_UNAVAILABLE',), {}, (), (), {'code': 'HTTP_503', 'transient': True, 'stage': 'holders'}),
                       Screen(False, ('MARKET_CAP_ABOVE_MAX',), {'mint': 'x'}, (), ())):
            self.rec.on_candidate(dict(note, screen=screen))
        self.assertEqual(self.rec.health()['hazard_seen'], 0)


# ============================================================================================================ configs / production
class PrefetchConfigTests(unittest.TestCase):
    def test_absent_is_off_and_every_default_validates(self):
        self.assertFalse(PF.config(None)['enabled'])
        self.assertFalse(PF.config({})['enabled'])
        on = PF.config({'enabled': True})
        self.assertEqual(on['kinds'], list(PF.KINDS))
        self.assertEqual(on['lead_s'], PF.DEFAULTS['lead_s'])

    def test_strict(self):
        bad = [{'typo': 1}, {'enabled': 1}, {'kinds': []}, {'kinds': ['flow', 'flow']}, {'kinds': ['nope']}, {'lead_s': {'nope': 1}},
               {'lead_s': {'flow': -1}}, {'lead_s': {'flow': True}}, {'settle_s': -1}, {'max_stale_s': 0}, {'max_attempts': 0},
               {'max_jobs_per_pass': True}, {'skip_hazards': 'yes'}, {'probe': False, 'skip_hazards': True}, {'probe': False, 'skip_hazards': False},
               [], 'on']
        for raw in bad:
            with self.assertRaises(PF.ConfigError, msg=repr(raw)):
                PF.config(raw)
        PF.config({'probe': False, 'skip_hazards': False, 'kinds': ['flow', 'risk_external', 'wallet_signals']})

    def test_a_partial_lead_override_keeps_the_other_defaults(self):
        cfg = PF.config({'lead_s': {'flow': 15}})
        self.assertEqual(cfg['lead_s']['flow'], 15.0)
        self.assertEqual(cfg['lead_s']['wallet_signals'], PF.DEFAULTS['lead_s']['wallet_signals'])


class ProductionConfigUnchanged(unittest.TestCase):
    def test_the_live_config_loads_with_both_features_off(self):
        cfg = load_config(PROD)
        self.assertFalse(cfg['prefetch']['enabled'])
        self.assertEqual(cfg['paths']['hazard_path_sample'], 0.0)
        self.assertEqual((cfg['screen']['min_age_seconds'], cfg['pool_fee_bps'], cfg['initial_cash_sol']), (60, 25, '100'))
        for key in ('prefetch',):
            self.assertIn(key, CONFIG_KEYS)

    def test_the_config_hash_of_the_live_config_is_what_it_was_before_the_new_keys_existed(self):
        """The learner / report ``--config-hash`` windows must not split at this deploy: with both new features off the hash covers exactly
        the keys the previous code loaded (the formula is M1's: canonical JSON of the loaded config, ``_comment`` keys left out)."""
        cfg = load_config(PROD)

        def strip(value):
            if isinstance(value, dict):
                return {k: strip(v) for k, v in value.items() if k != '_comment'}
            if isinstance(value, list):
                return [strip(v) for v in value]
            return value
        before = strip(cfg)
        del before['prefetch']
        del before['paths']['hazard_path_sample']
        text = json.dumps(before, sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)
        self.assertEqual(lean_config_hash(cfg), hashlib.sha256(text.encode()).hexdigest())

    def test_turning_either_on_is_a_new_run(self):
        raw = json.loads(PROD.read_text())
        raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lean.json'

            def hash_of(**patch):
                path.write_text(json.dumps({**raw, **patch}))
                return lean_config_hash(load_config(path))
            base = hash_of()
            for patch in ({'prefetch': {'enabled': True}}, {'paths': {**raw['paths'], 'hazard_path_sample': 0.2}}):
                self.assertNotEqual(hash_of(**patch), base, patch)
            # an explicit prefetch block that is OFF, and an explicit zero sample, are the same run as none
            self.assertEqual(hash_of(prefetch={'enabled': False}, paths={**raw['paths'], 'hazard_path_sample': 0}), base)

    def test_bad_values_are_refused_at_load(self):
        raw = json.loads(PROD.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            for patch in ({'prefetch': {'enabled': True, 'typo': 1}}, {'prefetch': {'kinds': ['x']}}, {'paths': {**raw['paths'], 'hazard_path_sample': 2}},
                          {'prefetch': 'on'}):
                path = Path(tmp) / 'lean.json'
                path.write_text(json.dumps({**raw, **patch, 'strategy_config': str(ROOT / 'config' / 'lean' / 'strategy-default.json')}))
                with self.assertRaises((ConfigError, ValueError), msg=patch):
                    load_config(path)

    def test_the_example_lists_both_and_turns_them_on(self):
        example = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        self.assertTrue(example['prefetch']['enabled'])
        self.assertEqual(example['paths']['hazard_path_sample'], 0.2)
        PF.config(example['prefetch'])


# ============================================================================================================ #8 the use rules
class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def candidate(mint='MintA', received=1000.0):
    return Candidate(seq=1, mint=mint, pool='Pool' + mint, signature='sig', slot=7, migrated_at=received, payload_hash='h')


class PrefetchRules(unittest.TestCase):
    def build(self, **cfg):
        self.clock = Clock()
        p = PF.Prefetch(cfg=PF.config({'enabled': True, **cfg}), store=mock.Mock(), helius=mock.Mock(transport=None), discovery_db='x',
                        screen_cfg={'min_age_seconds': 60}, min_age_s=60, clock=self.clock, sleep=lambda s: None)
        return p

    @staticmethod
    def done(entry, kind, *, asof, finished, payload=None):
        job = entry.jobs[kind]
        job.state, job.asof, job.started_at, job.finished_at, job.payload = 'done', asof, asof + 3, finished, payload or {'p': kind}
        return job

    def entry(self, p, mint='MintA', received=1000.0):
        e = PF.Entry(candidate(mint, received), received)
        p.entries[mint] = e
        return e

    def test_a_result_is_usable_only_if_done_finished_and_asof_before_the_decision(self):
        p = self.build()
        e = self.entry(p)
        self.done(e, 'flow', asof=1030.0, finished=1034.0)
        got = p.take('flow', 'MintA', 1062.0)
        self.assertEqual((got['asof'], got['finished_at'], got['payload']), (1030.0, 1034.0, {'p': 'flow'}))
        self.assertIsNone(p.take('flow', 'MintA', 1033.99))                      # finished AFTER this decision: never
        self.assertIsNone(p.take('flow', 'MintA', 1029.0))
        self.assertEqual(p.misses['flow'], {'FINISHED_AFTER_DECISION': 2})
        odd = self.done(e, 'wallet_signals', asof=1030.0, finished=1010.0)             # (cannot happen: a job finishes after it starts; still refused)
        self.assertIsNone(p.take('wallet_signals', 'MintA', 1020.0))
        self.assertEqual(p.misses['wallet_signals'], {'ASOF_AFTER_DECISION': 1})
        self.assertFalse(odd.used)
        self.assertIsNone(p.take('flow', 'Nobody', 1062.0))
        self.assertIsNone(p.take('features', 'MintA', 1062.0))                    # not done
        self.assertIn('NOT_DONE:' + 'pending', p.misses['features'])

    def test_the_boundary_is_inclusive_only_for_finished_at_equal_to_the_decision(self):
        p = self.build()
        e = self.entry(p)
        self.done(e, 'flow', asof=1030.0, finished=1040.0)
        self.assertIsNotNone(p.take('flow', 'MintA', 1040.0))
        self.assertIsNone(p.take('flow', 'MintA', 1039.999999))

    def test_a_result_older_than_max_stale_at_the_decision_is_not_used(self):
        p = self.build(max_stale_s=30)
        e = self.entry(p)
        self.done(e, 'flow', asof=1000.0, finished=1004.0)
        self.assertIsNotNone(p.take('flow', 'MintA', 1030.0))
        self.assertIsNone(p.take('flow', 'MintA', 1030.5))
        self.assertEqual(p.misses['flow'], {'STALE': 1})

    def test_features_need_the_probes_holders_for_a_soft_reject_else_the_recorder_reads_them(self):
        p = self.build()
        e = self.entry(p)
        self.done(e, 'features', asof=1020.0, finished=1024.0, payload={'holders': None})
        self.assertIsNone(p.take_features('MintA', 1062.0, True))
        self.assertIsNotNone(p.take_features('MintA', 1062.0, False))
        self.done(e, 'features', asof=1020.0, finished=1024.0, payload={'holders': ['1.0000', '10.0000']})
        self.assertIsNotNone(p.take_features('MintA', 1062.0, True))

    def test_when_a_job_starts_is_lead_seconds_before_the_earliest_screen(self):
        p = self.build()
        e = self.entry(p, received=1000.0)
        self.assertEqual(p.due(e, 'probe'), 1002.0)
        self.assertEqual(p.due(e, 'flow'), 1000.0 + 60 - 22)
        self.assertEqual(p.due(e, 'risk_external'), 1000.0 + max(2.0, 60 - 40))
        p300 = PF.Prefetch(cfg=PF.config({'enabled': True}), store=None, helius=None, discovery_db='x', screen_cfg={}, min_age_s=300, clock=Clock())
        e300 = PF.Entry(candidate(), 1000.0)
        self.assertEqual(p300.due(e300, 'flow'), 1000.0 + 300 - 22)               # scales with the screen's min age
        pshort = PF.Prefetch(cfg=PF.config({'enabled': True}), store=None, helius=None, discovery_db='x', screen_cfg={}, min_age_s=20, clock=Clock())
        self.assertEqual(pshort.due(e300, 'flow'), 1000.0 + 2.0)                  # never earlier than min_start_s

    def test_readiness_counts_only_what_finished_before_the_decision_and_only_for_hazard_free_candidates(self):
        p = self.build()
        free, hazard, soft_one, late = self.entry(p, 'Free'), self.entry(p, 'Haz'), self.entry(p, 'Soft'), self.entry(p, 'Late')
        for e in (free, soft_one, late):
            for kind in PF.KINDS:
                self.done(e, kind, asof=1020.0, finished=1030.0)
        self.done(late, 'flow', asof=1020.0, finished=1070.0)                    # finished after the decision at 1062
        self.done(late, 'risk_external', asof=1020.0, finished=1063.0)
        for e in (free, soft_one, late):
            e.gate = 'FREE'
        hazard.gate = 'HAZARD'
        ok = Screen(True, (), {}, (), ())
        bad = Screen(False, ('MAYHEM_POOL',), {}, (), ())
        soft = Screen(False, ('MARKET_CAP_ABOVE_MAX',), {}, (), ())
        p.on_decision('Free', 1062.0, ok)
        p.on_decision('Haz', 1062.0, bad)
        p.on_decision('Soft', 1062.0, soft)                                       # hazard-free, not a PASS: no flow expected
        p.on_decision('Late', 1062.0, ok)                                         # a PASS whose flow and RugCheck jobs were still running
        h = p.health()['ready_at_decision']
        self.assertEqual((h['decisions'], h['hazard_free'], h['passed']), (4, 3, 2))
        self.assertEqual(h['ready'], {'features': 3, 'risk_external': 2, 'wallet_signals': 3, 'clusters': 3, 'flow': 1})
        self.assertEqual(h['rate'], {'features': 1.0, 'risk_external': round(2 / 3, 4), 'wallet_signals': 1.0, 'clusters': 1.0, 'flow': 0.5})
        self.assertEqual(p.health()['probe_vs_screen'], {'tp': 1, 'fp': 0, 'fn': 0, 'tn': 3, 'unknown': 0})
        # pending jobs are cancelled at the decision; a second decision of the same mint counts nothing
        self.assertTrue(all(j.state != 'pending' for j in free.jobs.values()))
        p.on_decision('Free', 1100.0, ok)
        self.assertEqual(p.health()['ready_at_decision']['decisions'], 4)

    def test_a_failed_screen_is_not_a_decision(self):
        p = self.build()
        e = self.entry(p)
        failed = Screen(False, ('HOLDERS_UNAVAILABLE',), {}, (), (), {'code': 'HTTP_503', 'transient': True, 'stage': 'holders'})
        p.on_decision('MintA', 1062.0, failed)
        self.assertIsNone(e.decided_at)
        self.assertEqual(p.health()['ready_at_decision']['decisions'], 0)

    def test_the_probes_verdict_is_compared_with_the_screens(self):
        p = self.build()
        a, b, c, d = (self.entry(p, m) for m in 'ABCD')
        a.gate, b.gate, c.gate = 'HAZARD', 'HAZARD', 'FREE'
        ok, bad = Screen(True, (), {}, (), ()), Screen(False, ('MAYHEM_POOL',), {}, (), ())
        p.on_decision('A', 1062.0, bad)
        p.on_decision('B', 1062.0, ok)                                             # the probe said hazard, the screen passed: a false positive
        p.on_decision('C', 1062.0, bad)                                            # the probe said free, the screen said hazard: a false negative
        p.on_decision('D', 1062.0, ok)                                             # no probe verdict
        self.assertEqual(p.health()['probe_vs_screen'], {'tp': 1, 'fp': 1, 'fn': 1, 'tn': 0, 'unknown': 1})

    def test_pending_jobs_wait_for_the_verdict_then_skip_a_hazard_and_never_start_after_the_earliest_screen(self):
        p = self.build()
        p.flow = mock.Mock(prefetch=None)
        e = self.entry(p)
        self.clock.t = 1045.0
        flow = e.jobs['flow']
        self.assertEqual(p._ready(e, flow, 1045.0), 'wait')                       # the probe has not answered yet
        e.jobs['probe'].state, e.gate = 'done', 'HAZARD'
        self.assertEqual(p._ready(e, flow, 1045.0), 'skip')
        self.assertEqual((flow.state, flow.why), ('skipped', 'HAZARD'))
        e.gate, flow.state = 'FREE', 'pending'
        self.assertEqual(p._ready(e, flow, 1045.0), 'run')
        self.assertEqual(p._ready(e, flow, 1020.0), 'wait')                       # before its start time
        self.assertEqual(p._ready(e, flow, 1091.0), 'skip')                       # 31 s after the earliest screen: the decision is made
        self.assertEqual(flow.why, 'TOO_LATE')

    def test_a_busy_low_lane_postpones_a_job_without_sending_anything(self):
        p = self.build()
        p.flow = mock.Mock(prefetch=None)
        limiter = mock.Mock()
        limiter.available.return_value = 2.0
        p.helius = mock.Mock(transport=mock.Mock(limiter=limiter))
        e = self.entry(p)
        e.jobs['probe'].state, e.gate = 'done', 'FREE'
        self.assertEqual(p._ready(e, e.jobs['flow'], 1045.0), 'wait')            # 2 tokens < 2 needed + 1 reserved for the others
        self.assertEqual(p.counts['lane_waits'], 1)
        limiter.available.return_value = 3.0
        self.assertEqual(p._ready(e, e.jobs['flow'], 1045.0), 'run')

    def test_the_gate_pauses_everything_and_a_pass_never_raises(self):
        p = self.build()
        p.gate = lambda: False
        p._discover = mock.Mock(side_effect=AssertionError('discovery must not run while gated'))
        self.assertEqual(p.prefetch_pass(), 0)
        p.gate = lambda: True
        p._discover = mock.Mock(side_effect=RuntimeError('boom'))
        self.assertEqual(p.prefetch_pass(), 0)
        self.assertEqual((p.counts['errors'], p.last_error), (1, 'RuntimeError'))

    def test_the_thread_loop_runs_and_stops_with_the_runners_event(self):
        import threading
        p = self.build(pass_interval_s=0.1)
        p._discover = mock.Mock()
        stop = threading.Event()
        p.stop = stop
        thread = threading.Thread(target=p.run_loop, daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if p._discover.called:
                    break
                threading.Event().wait(0.05)
            self.assertTrue(p._discover.called)
        finally:
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_the_stamp_names_the_real_times(self):
        pre = {'asof': 1030.0, 'started_at': 1033.0, 'finished_at': 1036.5}
        stamp = PF.Prefetch._stamp(pre, 1062.0)
        self.assertEqual((stamp['asof'], stamp['started_at'], stamp['finished_at'], stamp['staleness_s']), (1030.0, 1033.0, 1036.5, 32.0))


class PrefetchJobs(unittest.TestCase):
    """What a job does with what its collector returned (stubbed collectors: no HTTP)."""

    def setUp(self):
        self.clock = Clock(1040.0)
        self.p = PF.Prefetch(cfg=PF.config({'enabled': True}), store=mock.Mock(), helius=mock.Mock(transport=None), discovery_db='x',
                             screen_cfg={'min_age_seconds': 60}, min_age_s=60, clock=self.clock, sleep=lambda s: None)
        self.entry = PF.Entry(candidate(), 1000.0)
        self.p.entries['MintA'] = self.entry

    def run_job(self, kind):
        job = self.entry.jobs[kind]
        self.p._run(self.entry, job)
        return job

    @staticmethod
    def payload(rc_score=None, why=None):
        return {'fields': {'rc_score': rc_score}, 'missing': {} if why is None else {'rc_score': why}, 'calls': 1}

    def test_a_rugcheck_report_that_could_not_be_read_for_a_transient_reason_is_asked_for_again(self):
        for why in ('RC_HTTP_429', 'RC_BACKOFF', 'RC_TIMEOUT', 'RC_HTTP_503'):
            self.entry.jobs['risk_external'] = PF.Job('risk_external')
            self.p.risk_external = mock.Mock(collect=mock.Mock(return_value=self.payload(None, why)))
            job = self.run_job('risk_external')
            self.assertEqual((job.state, job.why), ('pending', why), why)
            self.assertGreater(job.next_at, self.clock())                          # not at once

    def test_unknown_mint_is_an_answer_and_a_report_is_a_result(self):
        for payload in (self.payload(None, 'RC_UNKNOWN_MINT'), self.payload(120)):
            self.entry.jobs['risk_external'] = PF.Job('risk_external')
            self.p.risk_external = mock.Mock(collect=mock.Mock(return_value=payload))
            self.assertEqual(self.run_job('risk_external').state, 'done')

    def test_a_job_gives_up_after_max_attempts_and_the_collector_then_runs_as_before(self):
        self.p.risk_external = mock.Mock(collect=mock.Mock(return_value=self.payload(None, 'RC_BACKOFF')))
        for _ in range(3):
            self.clock.t += 5.0
            job = self.run_job('risk_external')
        self.assertEqual((job.state, job.attempts), ('failed', 3))
        self.assertIsNone(self.p.take('risk_external', 'MintA', 1062.0))

    def test_features_on_a_shed_lane_with_nothing_sent_are_asked_for_again_but_a_partial_result_is_kept(self):
        shed = {'extra': {}, 'missing': {'dev_holding_pct': 'LANE_SHED', 'graduation_block_time': 'LANE_SHED'}, 'flags': {}, 'calls': 0,
                'creator': 'C', 'holders': None}
        partial = {**shed, 'calls': 1, 'extra': {'dev_holding_pct': '2.0000'}, 'missing': {'graduation_block_time': 'LANE_SHED'}}
        self.entry.probe = Screen(True, (), {}, (), ())
        for payload, state in ((shed, 'pending'), (partial, 'done')):
            self.entry.jobs['features'] = PF.Job('features')
            self.p.features = mock.Mock(prefetch_run=mock.Mock(return_value=payload))
            self.assertEqual(self.run_job('features').state, state)

    def test_a_collector_bug_is_a_failed_job_never_an_exception(self):
        self.p.risk_external = mock.Mock(collect=mock.Mock(side_effect=RuntimeError('boom')))
        job = self.run_job('risk_external')
        self.assertEqual((job.state, job.why), ('failed', 'ERROR:RuntimeError'))
        self.assertEqual((self.p.counts['errors'], self.p.last_error), (1, 'RuntimeError'))

    def test_a_flow_job_whose_collector_wants_a_retry_is_retried_and_one_that_learned_nothing_leaves_no_result(self):
        from lean import flow as FL

        def collect(*args, sink=None, **kw):
            raise FL._Retry('LANE_SHED')
        self.p.flow = mock.Mock(collect=collect)
        job = self.run_job('flow')
        self.assertEqual((job.state, job.why), ('pending', 'LANE_SHED'))
        self.assertIsNone(self.p.take('flow', 'MintA', 1062.0))


class LearnerAcceptsPrefetchedRows(unittest.TestCase):
    """The learner's lookahead guards read ``late_s``, ``window_after_decision_s`` and ``supply_source`` of a row: a prefetched row stays
    inside them, and a row that is not (a late one, or one whose supply was read after the decision) is excluded exactly as before."""
    POLICY = LN.LookaheadPolicy()

    def wallet(self, **kw):
        row = {'version': LN.WALLET_VERSION, 'window_after_decision_s': 0.0, 'bundle_score': 0.5, 'bundled_supply_pct': 3.0,
               'supply_source': 'rpc_prefetch', 'late_s': -20.0, 'decision_ts': 1062.0, 'smart_asof_ts': 1020.0, 'smart_buyers_count': 1}
        row.update(kw)
        return LN.wallet_values(row, 1062.0, self.POLICY, LN._Counts())

    def test_a_supply_read_before_the_decision_by_a_prefetch_counts(self):
        self.assertIsNotNone(self.wallet()['bundled_supply_pct'])
        self.assertIsNotNone(self.wallet()['smart_buyers_count'])

    def test_a_prefetch_supply_in_a_row_collected_after_the_decision_does_not(self):
        self.assertIsNone(self.wallet(late_s=4.0)['bundled_supply_pct'])
        self.assertIsNone(self.wallet(late_s=None)['bundled_supply_pct'])
        self.assertIsNone(self.wallet(supply_source='rpc_after_decision')['bundled_supply_pct'])
        self.assertIsNotNone(self.wallet(late_s=4.0)['bundle_score'])                 # (the window figure is cut at the decision either way)

    def test_a_cluster_row_from_a_prefetch_counts_only_if_it_was_observed_before_the_decision(self):
        def cluster(**kw):
            row = {'version': LN.CLUSTERS_VERSION, 'holders_source': 'prefetch', 'supply_source': 'prefetch', 'late_s': -20.0,
                   'window_after_decision_s': 0.0, 'cluster_count': 2, 'largest_cluster_supply_pct': 12.5, 'jito_bundled_buys': 1, 'flags': {}}
            row.update(kw)
            return LN.cluster_values(row, 1062.0, self.POLICY, LN._Counts())
        timely = cluster()
        self.assertEqual((timely['cluster_count'], timely['largest_cluster_supply_pct'], timely['jito_bundled_buys']), (2, 12.5, 1))
        for late in (4.0, None):
            late_row = cluster(late_s=late)
            self.assertIsNone(late_row['largest_cluster_supply_pct'])                    # the holder snapshot / supply were read after the decision
        self.assertIsNone(cluster(holders_source='rpc_after')['largest_cluster_supply_pct'])
        self.assertIsNone(cluster(window_after_decision_s=3.0)['jito_bundled_buys'])

    def test_a_smart_table_newer_than_the_decision_is_still_refused(self):
        self.assertIsNone(self.wallet(smart_asof_ts=1063.0)['smart_buyers_count'])

    def test_snapshots_with_a_negative_late_s_pass_and_a_late_one_is_excluded(self):
        counts = LN._Counts()
        row = {'features_version': 3, 'late_s': -12.0, 'fields': {'dev_holding_pct': '2.0000'}, 'flags': {'dev_holding_pct': 'AT_COLLECTION'}}
        self.assertEqual(LN.feature_values(row, 1062.0, self.POLICY, counts, ('dev_holding_pct',))['dev_holding_pct'], 2)
        row['late_s'] = 16.0
        self.assertIsNone(LN.feature_values(row, 1062.0, self.POLICY, counts, ('dev_holding_pct',))['dev_holding_pct'])
        risk = {'version': LN.RISK_VERSION, 'late_s': -30.0, 'fields': {'rc_score': 120}, 'flags': {'rc_score': 'AT_COLLECTION'}}
        self.assertEqual(LN.risk_values(risk, 1062.0, self.POLICY, counts, ('rc_score',))['rc_score'], 120)
        risk['late_s'] = 40.0
        self.assertIsNone(LN.risk_values(risk, 1062.0, self.POLICY, counts, ('rc_score',))['rc_score'])


if __name__ == '__main__':
    unittest.main()
