"""SYNTHETIC_TEST_ONLY: regime hook inside paper_cycle.run_once (opt-in paper_regime_version=1)."""
import unittest
from unittest.mock import patch

from desk import engine, paper_cycle as cycle, regime_producer
from tests import test_paper_cycle as base




class RegimeCycleTests(base.PaperCycleTests):
    def enable(self):
        self.cfg = {**self.cfg, 'paper_regime_version': 1}
        self.path = self.path.with_name('cycle-regime.sqlite')
        cycle.initialize(self.path, self.cfg)

    def entry(self):
        self.http_calls = []; self.sell_output = 10_000_000
        self.seen_events = []
        real = engine.transition
        def spy(state, event, *a, **k):
            if isinstance(event, dict) and event.get('kind') == 'market':
                self.seen_events.append(event)
            return real(state, event, *a, **k)
        with patch.object(engine, 'transition', spy):
            return self.actual_cycle()

    def fills(self, result):
        return [x for x in result['outcomes'] if x.get('side') == 'buy']

    def test_flag_off_still_enters_and_event_carries_no_regime(self):
        result = self.entry()
        self.assertEqual(len(self.fills(result)), 1, result)
        self.assertTrue(all('regime' not in e for e in self.seen_events))

    def test_producer_without_evidence_is_terminal_no_entry(self):
        self.enable()
        result = self.entry()
        self.assertEqual(self.fills(result), [], result)
        self.assertTrue(any('REGIME_EVIDENCE_REQUIRED' in str(x) for x in result['outcomes']), result['outcomes'])
        self.assertEqual(cycle._state(self.path, self.cfg)['positions'], {})

    def test_valid_evidence_enters_with_regime_bound_into_event_id(self):
        self.enable()
        seen = []
        def fake(research, evidence, ts, ttl):
            seen.append(ts)
            return {'version': 1, 'as_of': ts, 'graduations_per_hour': '60', 'sol_usd_change_pct': '1'}
        with patch.object(regime_producer, 'evidence', fake):
            result = self.entry()
        self.assertEqual(len(self.fills(result)), 1, result)
        self.assertTrue(seen)
        self.assertTrue(self.seen_events)
        from desk.model import digest
        for e in self.seen_events:
            self.assertEqual(e['regime']['version'], 1)
            self.assertEqual(e['event_id'], 'paper-market:' + digest({k: v for k, v in e.items() if k != 'event_id'}))

    def test_off_regime_blocks_entry(self):
        self.enable()
        bad = lambda r, e, ts, ttl: {'version': 1, 'as_of': ts, 'graduations_per_hour': '0', 'sol_usd_change_pct': '0'}
        with patch.object(regime_producer, 'evidence', bad):
            result = self.entry()
        self.assertEqual(self.fills(result), [], result)

    def test_invalid_regime_config_refused_at_load(self):
        for extra in ({'paper_regime_version': 2}, {'paper_regime_version': 1, 'paper_regime_policy': {'bogus': 1}}):
            with self.assertRaises(ValueError):
                cycle._config({**self.cfg, **extra})


for _name in dir(base.PaperCycleTests):
    if _name.startswith('test_') and _name not in vars(RegimeCycleTests):
        setattr(RegimeCycleTests, _name, None)  # run only this module's tests, not the inherited suite

if __name__ == '__main__':
    unittest.main()
