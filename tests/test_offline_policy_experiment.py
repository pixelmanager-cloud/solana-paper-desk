"""Synthetic persisted-engine experiments; no transport or acceptance mocks."""
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout, closing
from pathlib import Path
from unittest.mock import patch

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import canonical, digest
from desk.paper_checkpoint import RecoveryRequired
from research.policy_experiment import evaluate, main, InvalidExperiment
from tests.helpers import T, config, event, control


class OfflinePolicyExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.training = self.make('train', T)
        self.holdout = self.make('hold', T + 100)
        self.trials = [{'min_liquidity_usd': '9000'}, {'min_age_seconds': 400}]

    def make(self, name, at, *, close=True, reject=False, cfg=None):
        path = self.root / (name + '.sqlite')
        with closing(Ledger(path)) as ledger:
            ledger.apply(event(at, mint='SYNTHETIC_' + name, danger=reject),
                         cfg or config(), transition, initial_state)
            if close and not reject:
                ledger.apply(event(at + 10, mint='SYNTHETIC_' + name, danger=True),
                             cfg or config(), transition, initial_state)
        return path

    def run_eval(self, **kwargs):
        with patch('socket.socket', side_effect=AssertionError('No network')):
            return evaluate(kwargs.get('training', [self.training]),
                            kwargs.get('holdout', [self.holdout]),
                            kwargs.get('trials', self.trials), now=kwargs.get('now', T + 200))

    def dump(self, path):
        with sqlite3.connect(path) as c: return list(c.iterdump())

    def test_actual_closed_lifecycle_reports_net_costs_without_proposal_pnl(self):
        before = self.dump(self.training)
        r = self.run_eval()
        self.assertEqual(r['status'], 'INSUFFICIENT_DATA')
        self.assertEqual(r['proposal_trials_count'], 2)
        self.assertEqual(r['optimization_trials_performed'], 0)
        self.assertEqual(r['partitions']['training']['closed_trade_count'], 1)
        self.assertGreater(float(r['partitions']['training']['recorded_fees_sol']), 0)
        self.assertEqual(r['partitions']['training']['marks'], [[]])
        self.assertEqual(r['execution_status'], 'EXECUTION_UNVERIFIED')
        self.assertEqual(r['fee_slippage_policy']['adverse_slippage_bps'], config()['adverse_slippage_bps'])
        for p in r['proposals']:
            self.assertIsNone(p['counterfactual_net_pnl_sol'])
            self.assertFalse(p['promotion_authorized'])
        self.assertFalse(r['entry_authorized'])
        self.assertEqual(before, self.dump(self.training))

    def test_rejected_unobserved_candidates_have_no_invented_returns(self):
        a = self.make('rejecttrain', T, reject=True)
        b = self.make('rejecthold', T + 100, reject=True)
        r = self.run_eval(training=[a], holdout=[b])
        self.assertEqual(r['status'], 'INSUFFICIENT_DATA')
        for part in r['partitions'].values():
            self.assertEqual(part['closed_trade_count'], 0)
            self.assertEqual(part['observed_closed_net_pnl_sol'], '0')
        self.assertTrue(all(p['counterfactual_net_pnl_sol'] is None for p in r['proposals']))

    def test_marks_and_partial_realized_are_separate_not_closed_success(self):
        a = self.make('open', T, close=False)
        with closing(Ledger(a, must_exist=True)) as ledger:
            ledger.apply(event(T + 5, mint='SYNTHETIC_open', reserve_sol='160'), config(), transition, initial_state)
        r = self.run_eval(training=[a])
        p = r['partitions']['training']
        self.assertEqual(p['closed_trade_count'], 0)
        self.assertEqual(len(p['marks'][0]), 1)
        self.assertIn('TRAINING_OPEN_LIFECYCLE', r['blockers'])
        self.assertIsNone(p['marks'][0][0]['model_unrealized_pnl_sol'])

    def test_duplicate_copied_db_and_overlapping_or_equal_holdout_reject(self):
        import shutil
        copy = self.root / 'copy.sqlite'; shutil.copyfile(self.training, copy)
        overlap = self.make('overlap', T + 10)
        for train, hold in (([self.training], [copy]), ([self.training], [overlap]),
                            ([self.holdout], [self.training]),
                            ([self.holdout, self.training], [self.make('later', T + 300)])):
            with self.subTest(train=train, hold=hold), self.assertRaises(InvalidExperiment):
                self.run_eval(training=train, holdout=hold)

    def test_feature_timestamp_leak_and_future_observation_reject(self):
        a = self.root / 'oldfeature.sqlite'
        with closing(Ledger(a)) as ledger:
            ledger.apply(event(T + 100, mint='SYNTHETIC_oldfeature', holder_at=T + 5),
                         config(), transition, initial_state)
        with self.assertRaisesRegex(InvalidExperiment, 'HOLDOUT_NOT_STRICTLY_LATER'):
            self.run_eval(holdout=[a])
        with self.assertRaises(InvalidExperiment): self.run_eval(now=T + 105)

    def test_frozen_controls_and_hardcoded_score_momentum_not_tunable(self):
        for key in ('mode', 'max_exposure_fraction', 'fixed_fee_sol', 'adverse_slippage_bps',
                    'price_ttl_seconds', 'paper_signal_policy_version', 'entry_score',
                    'momentum', 'token_program', 'request_budget', 'max_positions'):
            with self.subTest(key=key), self.assertRaises(InvalidExperiment):
                self.run_eval(trials=[{key: '1'}])

    def test_invalid_thresholds_trials_and_ranges_reject(self):
        for trial in ({'min_age_seconds': True}, {'min_age_seconds': -1},
                      {'min_age_seconds': 30000}, {'min_liquidity_usd': 'NaN'},
                      {'min_liquidity_usd': 'Infinity'}, {'min_liquidity_usd': '1e999'},
                      {'min_liquidity_usd': '-1'}, {'min_liquidity_usd': 9000}, {}):
            with self.subTest(trial=trial), self.assertRaises(InvalidExperiment): self.run_eval(trials=[trial])
        for trials in ([], self.trials * 17, [self.trials[0]] * 2, [{'min_age_seconds': 300}]):
            with self.assertRaises(InvalidExperiment): self.run_eval(trials=trials)

    def test_mixed_configs_and_corrupt_journal_reject_without_repair(self):
        cfg = config(); cfg['max_positions'] = 5
        changed = self.make('changed', T + 100, cfg=cfg)
        with self.assertRaises(InvalidExperiment): self.run_eval(holdout=[changed])
        with sqlite3.connect(self.holdout) as c:
            c.execute("UPDATE metadata SET value='bad' WHERE key='config_hash'")
        before = self.dump(self.holdout)
        with self.assertRaises(RecoveryRequired): self.run_eval()
        self.assertEqual(before, self.dump(self.holdout))

    def test_real_cli_redacts_paths_and_rejects_missing_without_initialization(self):
        trials = self.root / 'trials.json'; trials.write_text(json.dumps(self.trials))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['--training', str(self.training), '--holdout', str(self.holdout),
                         '--trials', str(trials), '--now', str(T + 200)])
        self.assertEqual(code, 0); self.assertNotIn(str(self.root), out.getvalue())
        absent = self.root / 'credential-secret.sqlite'
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(['--training', str(absent), '--holdout', str(self.holdout),
                         '--trials', str(trials), '--now', str(T + 200)])
        self.assertEqual(code, 2); self.assertFalse(absent.exists())
        self.assertNotIn('credential-secret', out.getvalue())

    def test_original_quote_assumptions_risk_and_net_costs_survive(self):
        from tests.test_quote_execution import QuoteExecutionTests
        from tests.test_paper_experimental_scoring import experimental
        cases = []
        for at in (T, T + 400):
            c = QuoteExecutionTests(); c.setUp(); self.addCleanup(c.doCleanups)
            c.cfg['experimental_policy_version'] = 1
            buy = c.quote('buy', 10_000_000, 1_000_000, at=at)
            reverse = c.quote('sell', c.raw, 10_000_000, at=at)
            c.apply(experimental(ts=at, mint=c.mint, pool=c.pool, taker=c.wallet), (buy, reverse))
            c.apply(experimental(ts=at + 1, mint=c.mint, pool=c.pool, taker=c.wallet, danger=True),
                    (c.quote('sell', c.raw, 10_000_000, at=at + 1),))
            cases.append(c)
        r = self.run_eval(training=[cases[0].path], holdout=[cases[1].path], now=T + 500)
        for part in r['partitions'].values():
            self.assertEqual(part['closed_trade_count'], 1)
            self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY', part['risk_flags'])
            self.assertEqual(len(part['execution_assumptions']), 2)
        original = json.loads(cases[0].ledger.db.execute("SELECT payload FROM outcomes LIMIT 1").fetchone()[0])
        self.assertEqual(r['partitions']['training']['execution_assumptions'][0], original['quote_execution']['assumptions'])

    def test_minimum_counts_never_enable_counterfactual_or_profitability_ranking(self):
        paths = []
        for name, at, count in (('manytrain', T, 20), ('manyhold', T + 2000, 10)):
            path = self.root / (name + '.sqlite')
            with closing(Ledger(path)) as ledger:
                for i in range(count):
                    ts = at + 60 * i; mint = 'SYNTHETIC_' + name + str(i)
                    ledger.apply(event(ts, mint=mint), config(), transition, initial_state)
                    ledger.apply(event(ts + 10, mint=mint, danger=True, reserve_sol='160'), config(), transition, initial_state)
            paths.append(path)
        r = self.run_eval(training=[paths[0]], holdout=[paths[1]], now=T + 4000)
        self.assertEqual(r['partitions']['training']['closed_trade_count'], 20)
        self.assertEqual(r['partitions']['holdout']['closed_trade_count'], 10)
        self.assertEqual(r['status'], 'BLOCKED_COUNTERFACTUAL')
        self.assertEqual(r['profitability_verdict'], 'NOT_ASSESSED')
        self.assertTrue(all(p['counterfactual_net_pnl_sol'] is None for p in r['proposals']))

    def test_zero_trade_checkpoint_and_missing_holdout_are_insufficient(self):
        path = self.root / 'zero.sqlite'
        with closing(Ledger(path)) as ledger:
            ledger.apply(control(T, 'RESUME'), config(), transition, initial_state)
        r = self.run_eval(training=[path], holdout=[])
        self.assertEqual(r['status'], 'INSUFFICIENT_DATA')
        self.assertEqual(r['partitions']['training']['closed_trade_count'], 0)
        self.assertIsNone(r['partitions']['holdout']['first_at'])
        self.assertEqual(r['partitions']['holdout']['sample_count'], 0)
        self.assertIn('HOLDOUT_CLOSED_TRADE_MINIMUM_NOT_MET', r['blockers'])

    def test_cli_duplicate_trial_keys_reject_before_evaluation(self):
        trials = self.root / 'duplicate.json'
        trials.write_text('[{"min_age_seconds":400,"min_age_seconds":500}]')
        out = io.StringIO()
        with redirect_stdout(out), patch('research.policy_experiment.evaluate', side_effect=AssertionError('Must not evaluate')):
            code = main(['--training', str(self.training), '--trials', str(trials), '--now', str(T + 200)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['reason'], 'DUPLICATE_TRIAL_JSON_KEY')

    def test_real_cli_malformed_profile_and_window_shapes_reject_read_only(self):
        from desk.experiment_report import experiment_report
        trials = self.root / 'shape-trials.json'; trials.write_text(json.dumps(self.trials))
        shapes = [(None, 'SIGNAL_PROFILE_SHAPE_INVALID'),
                  ([], 'SIGNAL_PROFILE_SHAPE_INVALID'),
                  ('user text', 'SIGNAL_PROFILE_SHAPE_INVALID')]
        shapes += [({'window': value}, 'SIGNAL_WINDOW_SHAPE_INVALID')
                   for value in (None, [], 'user text', {},
                                 {'start_inclusive': True, 'end_inclusive': T},
                                 {'start_inclusive': T + 1, 'end_inclusive': T})]
        shapes.append(({}, 'SIGNAL_WINDOW_SHAPE_INVALID'))
        for index, (profile, reason) in enumerate(shapes):
            with self.subTest(profile=profile):
                path = self.root / ('shape-' + str(index) + '.sqlite')
                with closing(Ledger(path)) as ledger:
                    ledger.apply(event(paper_signal_profile=profile), config(), transition, initial_state)
                # Existing report validator accepts this unrelated V1 metadata;
                # the offline window reader must reject explicitly, not crash.
                experiment_report(path, now=T + 200)
                before = self.dump(path)
                with sqlite3.connect(path) as c:
                    hashes = list(c.execute('SELECT event_id,payload_hash FROM events'))
                out = io.StringIO()
                with redirect_stdout(out), patch('socket.socket', side_effect=AssertionError('No network')):
                    code = main(['--training', str(path), '--holdout', str(self.holdout),
                                 '--trials', str(trials), '--now', str(T + 200)])
                self.assertEqual(code, 2)
                result = json.loads(out.getvalue())
                self.assertEqual(result['status'], 'INVALID_INPUT')
                self.assertEqual(result['reason'], reason)
                self.assertFalse(result['entry_authorized'])
                self.assertFalse(result['promotion_authorized'])
                self.assertNotIn(str(path), out.getvalue())
                self.assertEqual(before, self.dump(path))
                with sqlite3.connect(path) as c:
                    self.assertEqual(hashes, list(c.execute('SELECT event_id,payload_hash FROM events')))
