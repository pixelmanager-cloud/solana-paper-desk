"""Render real temporary-ledger projections; no providers or submitted scans."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.decision_runner import assess
from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.paper_view import paper_status
from tests.helpers import T, config, event
from tests.test_cloud_decision_renderer import render


class CloudPaperRendererTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'active-paper.sqlite'
        self.cfg = config()
        guard = patch('socket.socket', side_effect=AssertionError('Fixture-only renderer'))
        guard.start()
        self.addCleanup(guard.stop)

    def checkpoint(self, events=(), now=T+1):
        ledger = Ledger(self.path)
        try:
            for value in events:
                ledger.apply(value, self.cfg, transition, initial_state)
        finally:
            ledger.close()
        return paper_status(self.path, now=now)

    def dom(self, data):
        return render(paper_data=data, now_ms=(T+20)*1000)['paper_html']

    def test_research_completion_and_candidate_rejection_are_not_positions(self):
        report = {'mint': 'SYNTHETIC_RESEARCH', 'observed_at': T,
                  'eligible_for_trading': True, 'findings': [], 'unknowns': ['OWNERSHIP_UNVERIFIED']}
        scan = {'id': 'fixture-research', 'mint': report['mint'], 'created': T-2,
                'status': 'COMPLETE', 'result': json.dumps(report)}
        decision = assess(scan, T+3)
        data = {'status': 'EVIDENCE_GATES_CONNECTED', 'automatic_entry_enabled': False,
                'decisions': [decision]}
        result = render(data, paper_data=paper_status(self.path, now=T),
                        scans_data=[{**scan, 'result': report}], now_ms=(T+20)*1000)
        self.assertIn('Research-only evidence · not a paper entry', result['scans_html'])
        self.assertIn('Original investigation observation:', result['scans_html'])
        self.assertIn('Observation age at display: 20 seconds', result['scans_html'])
        self.assertIn('REJECT', result['html'])
        self.assertIn('Rejected candidate; this evaluation does not create a simulated entry.', result['html'])
        self.assertIn('Paper positions are shown separately.', result['html'])
        self.assertIn('No active simulated paper ledger configured.', result['paper_html'])
        self.assertNotIn('Simulated open position ·', result['paper_html'])
        self.assertEqual(result['controls'], {'submit_listeners': 1, 'refresh_listeners': 1})
        self.assertTrue(all(call['method'] == 'GET' for call in result['calls']))

    def test_open_fixture_position_remains_model_only_with_exact_amounts(self):
        data = self.checkpoint([event()])
        html = self.dom(data)
        position = data['positions'][0]
        self.assertIn('Simulated open position · SYNTHETIC_A', html)
        self.assertIn(position['quantity']+' tokens', html)
        self.assertIn('remaining allocated cost: '+position['cost_left_sol']+' SOL', html)
        self.assertIn('Provenance: SYNTHETIC_TEST_ONLY', html)
        self.assertIn('Model-only unrealized PnL: '+position['unrealized_pnl_sol']+' SOL; valuation is not verified.', html)
        self.assertIn('Automatic paper entries: disabled.', html)
        self.assertIn('runner reported: not connected', html)
        self.assertIn('This checkpoint is not continuous monitoring status.', html)
        self.assertIn('paper results are not evidence of profitability', html)

    def test_full_simulated_sell_shows_no_open_position_without_inventing_close_records(self):
        data = self.checkpoint([event(), event(T+1, danger=True)])
        html = self.dom(data)
        sell = next(item['outcome'] for item in data['recent_outcomes']
                    if item['outcome'].get('side') == 'sell')
        self.assertIn('No simulated positions open at this checkpoint', html)
        self.assertIn('Simulated sell fill · SYNTHETIC_A', html)
        self.assertIn('No simulated position for this mint is open at the saved checkpoint. Full closed-position records are not provided.', html)
        self.assertIn('Recorded net proceeds: '+sell['proceeds_sol']+' SOL', html)
        self.assertIn('allocated simulated realized PnL: '+sell['realized_pnl_sol']+' SOL', html)
        self.assertIn('Bounded recent outcome window (up to 50 records)', html)
        self.assertNotIn('Closed positions: 1', html)

    def test_partial_simulated_sell_does_not_label_remaining_position_closed(self):
        data = self.checkpoint([event(), event(T+1, reserve_sol='200')])
        self.assertTrue(any(item['outcome'].get('side') == 'sell' for item in data['recent_outcomes']))
        html = self.dom(data)
        self.assertIn('Simulated open position · SYNTHETIC_A', html)
        self.assertIn('Simulated sell fill · SYNTHETIC_A', html)
        self.assertIn('An open simulated position for this mint remains at the saved checkpoint; this sell record does not certify full closure.', html)
        self.assertNotIn('No simulated position for this mint is open', html)

    def test_stale_projection_withholds_pnl_even_if_response_claims_a_value(self):
        data = self.checkpoint([event()], now=T+self.cfg['price_ttl_seconds']+1)
        # Contradictory response fields must not make stale values look current.
        data['positions'][0]['unrealized_pnl_sol'] = '999999.12345'
        data['estimated_equity_sol'] = '999999.12345'
        html = self.dom(data)
        self.assertIn('Stale valuation: current model PnL is withheld.', html)
        self.assertIn('Simulated equity withheld:', html)
        self.assertIn('Last saved model estimate (unverified):', html)
        self.assertIn('Simulated open position · SYNTHETIC_A', html)
        self.assertNotIn('999999.12345', html)

    def test_unverified_exit_remains_open_and_is_never_presented_as_sell_fill(self):
        data = self.checkpoint([event(), event(T+1, danger=True, provenance='MAINNET_OBSERVATION')])
        html = self.dom(data)
        self.assertIn('Saved strategy mode: exit only', html)
        self.assertIn('Simulated open position · SYNTHETIC_A', html)
        self.assertIn('Unresolved simulated exit: EXIT_SELLABILITY_UNVERIFIED. Position remains open', html)
        self.assertIn('Unresolved simulated exit outcome · SYNTHETIC_A', html)
        self.assertIn('No simulated sell fill is recorded by this outcome', html)
        self.assertIn('Model unrealized PnL withheld', html)
        self.assertNotIn('Simulated sell fill ·', html)

    def test_absent_empty_and_corrupt_ledgers_do_not_become_zero_or_running(self):
        absent = paper_status(self.path, now=T)
        empty = self.checkpoint()
        self.path.write_text('corrupt fixture only')
        unavailable = paper_status(self.path, now=T)
        for data, label in ((absent, 'No active simulated paper ledger configured.'),
                            (empty, 'Simulated ledger has no saved checkpoint.'),
                            (unavailable, 'Simulated ledger unavailable.')):
            with self.subTest(status=data['status']):
                html = self.dom(data)
                self.assertIn(label, html)
                self.assertIn('No balances or positions can be confirmed.', html)
                self.assertNotIn('Research collection is running', html)
                self.assertNotIn('Simulated cash: 0', html)
                self.assertNotIn('No simulated positions open', html)

    def test_paper_outcome_reason_is_literal_text_not_executable_markup(self):
        data = self.checkpoint([event(), event(T+1, danger=True, provenance='MAINNET_OBSERVATION')])
        attack = '<img src=x onerror="globalThis.executed=true">'
        item = next(item for item in data['recent_outcomes'] if item['outcome']['type'] == 'blocked_exit')
        item['outcome']['reason'] = attack
        html = self.dom(data)
        self.assertIn('Recorded reason: &lt;img', html)
        self.assertNotIn('<img', html)
        self.assertIn('No simulated sell fill is recorded by this outcome', html)


if __name__ == '__main__':
    unittest.main()
