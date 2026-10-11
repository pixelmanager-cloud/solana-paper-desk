"""SYNTHETIC_TEST_ONLY: T37F items 4 and 5 - the configured divergence limit in event validation, and the health
WARNING for persistent Jupiter PriceV3 authorization failures. Fixtures only; no network, no credentials."""
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from decimal import Decimal as D

from desk import engine, model, quote_execution as qe, usd_valuation as uv
from desk.evidence import EvidenceStore
from tests import test_ops_healthcheck as ops, test_usd_valuation as unit, test_usd_valuation_cycle as cyc
from tools.ops import healthcheck


class UsdAuthHealthTests(ops.Fixture):
    def evidence(self):
        return EvidenceStore(self.root / 'evidence.sqlite')

    def report(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = healthcheck.main(['--root', str(self.root), '--discovery-db', str(self.discovery), '--pacing-db', str(self.pacing),
                                     '--no-systemd'], clock=lambda: ops.NOW)
        return code, json.loads(out.getvalue())

    def usd_auth(self):
        return [c for c in self.report()[1]['checks'] if c['check'] == 'usd_auth']

    def reject(self, store, n, status=401):
        for i in range(n):
            store.save(unit.jupiter_record(None, status=status, failure='HTTP_REJECTED', observed_at=ops.NOW - 100 + i, requests_used=3 + i))

    def test_no_attempts_is_ok(self):
        (finding,) = self.usd_auth()
        self.assertEqual(finding['severity'], 'OK')

    def test_three_consecutive_rejections_raise_a_warning_naming_the_status(self):
        self.reject(self.evidence(), 3, 403)
        (finding,) = self.usd_auth()
        self.assertEqual((finding['severity'], finding['consecutive'], finding['last_status']), ('WARN', 3, 403))
        self.assertIn('falling back to Kraken', finding['detail'])
        self.assertNotIn('CRITICAL', {c['severity'] for c in self.report()[1]['checks'] if c['check'] == 'usd_auth'})

    def test_a_warning_never_makes_the_report_critical_by_itself(self):
        before = self.report()[1]['status']
        self.reject(self.evidence(), 5)
        self.assertEqual(self.report()[1]['status'], before if before != 'OK' else 'WARN')

    def test_recovery_clears_the_warning_and_two_rejections_are_not_persistent(self):
        store = self.evidence()
        self.reject(store, 2)
        self.assertEqual(self.usd_auth()[0]['severity'], 'OK')
        self.reject(store, 3, 401)
        self.assertEqual(self.usd_auth()[0]['severity'], 'WARN')
        store.save(unit.jupiter_record(unit.jupiter_body(), observed_at=ops.NOW - 1, requests_used=9))
        self.assertEqual(self.usd_auth()[0]['severity'], 'OK')

    def test_a_missing_evidence_store_is_the_existing_critical_probe_not_a_silent_pass(self):
        (self.root / 'evidence.sqlite').unlink()
        code, rep = self.report()
        self.assertEqual(code, 2)
        self.assertTrue(any(c['check'] == 'evidence' and c['severity'] == 'CRITICAL' for c in rep['checks']))


class ConfiguredLimitTests(cyc.V2Base):
    def exit_event(self):
        from desk.monitoring_budget import MonitoringBudget
        self.run_cycle()
        allowance = MonitoringBudget(self.h.f.progress.store, self.h.path, self.h.cfg, clock=lambda: self.h.f.at); allowance.provision()
        self.h.sell_output = 30_000_000
        self.assertEqual(self.run_cycle(positions=(self.held_item(),), candidates=(), monitoring=True)['status'], 'COMPLETE')
        return [e for e in self.events() if e['kind'] == 'quote_exit'][-1]

    def test_event_validation_rebuilds_with_the_configured_limit_and_refuses_a_different_stored_one(self):
        event = self.exit_event()
        self.assertEqual(event['paper_usd_valuation']['divergence_max_fraction'], '0.01')
        profile = qe.selected(self.h.cfg)
        model.validate_event(event, token_profile_version=profile)                              # no configured limit: as before
        model.validate_event(event, token_profile_version=profile, usd_divergence=D('0.01'))    # configured == stored
        for configured in (D('0.05'), D('0.001'), D('0.25')):
            with self.subTest(configured), self.assertRaises(ValueError):
                model.validate_event(event, token_profile_version=profile, usd_divergence=configured)
        tampered = json.loads(json.dumps(event))
        tampered['paper_usd_valuation']['divergence_max_fraction'] = '0.20'                     # a self-declared looser limit
        with self.assertRaises(ValueError):
            model.validate_event(tampered, token_profile_version=profile, usd_divergence=D('0.01'))

    def test_engine_binds_the_configured_limit_only_for_valuation_2(self):
        self.assertEqual(engine._usd_limit({'paper_usd_valuation_version': 2, uv.KEY_DIVERGENCE: '0.05'}), {'usd_divergence': D('0.05')})
        self.assertEqual(engine._usd_limit({'paper_usd_valuation_version': 2}), {'usd_divergence': D('0.01')})
        self.assertEqual(engine._usd_limit({'paper_usd_valuation_version': 1}), {})
        self.assertEqual(engine._usd_limit({}), {})

    def test_engine_transition_refuses_an_exit_whose_stored_limit_differs_from_the_configured_one(self):
        event = self.exit_event()
        state = cyc.cycle._state(self.h.path, self.h.cfg)
        looser = {**self.h.cfg, uv.KEY_DIVERGENCE: '0.05'}
        with self.assertRaises(ValueError):
            engine.transition(state, event, looser)


if __name__ == '__main__':
    unittest.main()
