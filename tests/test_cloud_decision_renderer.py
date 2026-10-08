"""Fixture DOM tests execute shipped static JS in Node; no backend or providers."""
import json
import os
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    return {'status': 'EVIDENCE_GATES_CONNECTED', 'automatic_entry_enabled': False,
            'decisions': [{'decision': 'REJECT', 'mint': 'SYNTHETIC_UI_FIXTURE',
                           'observed_at': 120, 'evaluated_at': 990,
                           'eligible_for_trading': False,
                           'reasons': ['CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED'],
                           'entry_evidence': {'gates': {
                               'history_snapshot': {'status': 'VERIFIED_COMPONENT', 'reasons': [],
                                   'scope': 'Historical finalized cutoff; not current entry freshness'},
                               'bundle_exposure': {'status': 'BLOCKED',
                                   'reasons': ['CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED']}},
                               'ownership_requests_used': 13,
                               'continued_ownership_history': {'observed_transaction_count': 4,
                                   'account_queries': {'verified': 3, 'required': 3}}}}]}


def render(data=None, **options):
    command = ['node', str(ROOT / 'tests/cloud_decision_renderer_harness.js'),
               str(ROOT / 'desk/static/app.js')]
    result = subprocess.run(command, input=json.dumps({'data': fixture() if data is None else data, **options}),
                            text=True, capture_output=True, check=True, timeout=10,
                            env={**os.environ, 'TZ': 'UTC'})
    return json.loads(result.stdout)


class CloudDecisionRendererTests(unittest.TestCase):
    def test_aged_observation_and_recent_evaluation_remain_distinct(self):
        result = render(now_ms=1000000)
        text = '\n'.join(result['paragraphs'])
        self.assertEqual(result['summaries'], ['REJECT · SYNTHETIC_UI_FIXTURE · historical evaluation'])
        self.assertIn('Original observation time:', text)
        self.assertIn('Observation age at display: 880 seconds', text)
        self.assertIn('Historical evaluation time:', text)
        self.assertIn('VERIFIED_COMPONENT (historical component only) · Scope: Historical finalized cutoff; not current entry freshness', text)
        self.assertIn('Immutable historical evaluations.', text)
        self.assertIn('Current evidence health is not continuously certified', text)
        self.assertIn('Historical entry blockers: current holder bundle exposure unverified', text)
        self.assertIn('13/18 investigation RPC requests used. Historical evidence only.', text)
        self.assertEqual(result['controls'], {'submit_listeners': 1, 'refresh_listeners': 1})
        self.assertTrue(all(call['method'] == 'GET' for call in result['calls']))
        self.assertEqual(set(call['url'] for call in result['calls']),
                         {'/api/scans', '/api/launches', '/api/paper', '/api/decisions'})

    def test_age_advances_for_unchanged_journal_without_rewriting_evaluation(self):
        result = render(now_ms=1000000, next_now_ms=1015000)
        self.assertIn('Observation age at display: 880 seconds', result['first_html'])
        self.assertIn('Observation age at display: 895 seconds', result['html'])
        first_time = result['first_html'].split('Historical evaluation time:')[1].split('</p>')[0]
        next_time = result['html'].split('Historical evaluation time:')[1].split('</p>')[0]
        self.assertEqual(first_time, next_time)
        self.assertIn('REJECT', result['html'])

    def test_old_reasons_do_not_classify_current_freshness(self):
        data = fixture()
        data['decisions'][0]['observed_at'] = 1000
        data['decisions'][0]['reasons'].append('INVESTIGATION_NOT_FRESH_FOR_ENTRY')
        result = render(data, now_ms=1000000)
        text = '\n'.join(result['paragraphs'])
        self.assertIn('Observation age at display: 0 seconds', text)
        self.assertIn('Historical entry blockers:', text)
        self.assertIn('investigation not fresh for entry', text)
        self.assertNotIn('Current status: STALE', text)
        self.assertNotIn('Current status: APPROVED', text)
        self.assertIn('do not grant current entry approval', text)

    def test_missing_or_invalid_observation_never_coerces_to_zero_age(self):
        for value in (None, '120', True, False, -1, 120.5, {}, []):
            with self.subTest(value=value):
                data = fixture()
                data['decisions'][0]['observed_at'] = value
                result = render(data, now_ms=1000000)
                text = '\n'.join(result['paragraphs'])
                self.assertIn('Original observation time: Unavailable (missing or invalid timestamp)', text)
                self.assertIn('Observation age at display: Unavailable (missing or invalid observation time)', text)
                self.assertNotIn('Invalid Date', text)
                self.assertNotIn('NaN', text)
        data = fixture()
        del data['decisions'][0]['observed_at']
        self.assertIn('Observation age at display: Unavailable', render(data)['html'])
        for special in ('nan', 'infinity', 'unsafe', 'out_of_date_range'):
            with self.subTest(special=special):
                self.assertIn('Observation age at display: Unavailable', render(special_observed=special)['html'])

    def test_future_observation_and_bad_browser_clock_withhold_age(self):
        data = fixture()
        data['decisions'][0]['observed_at'] = 1001
        text = '\n'.join(render(data, now_ms=1000000)['paragraphs'])
        self.assertIn('observation time is in the future relative to this browser clock', text)
        self.assertNotIn('-1 seconds', text)
        for special in ('nan', 'negative'):
            self.assertIn('Unavailable (invalid browser clock)', render(special_clock=special)['html'])

    def test_missing_scope_and_invalid_evaluation_are_explicit(self):
        for scope in (None, '', '   ', 123):
            with self.subTest(scope=scope):
                data = fixture()
                data['decisions'][0]['entry_evidence']['gates']['history_snapshot']['scope'] = scope
                data['decisions'][0]['evaluated_at'] = '990'
                text = '\n'.join(render(data)['paragraphs'])
                self.assertIn('VERIFIED_COMPONENT (historical component only) · Scope: No scope recorded; no broader verification is implied.', text)
                self.assertIn('Historical evaluation time: Unavailable (missing or invalid timestamp)', text)

    def test_untrusted_mint_scope_and_reasons_are_literal_dom_text(self):
        attack = '<img src=x onerror="globalThis.executed=true">'
        data = fixture()
        data['decisions'][0]['mint'] = attack
        data['decisions'][0]['entry_evidence']['gates']['history_snapshot']['scope'] = attack
        data['decisions'][0]['reasons'] = [attack]
        result = render(data)
        self.assertIn(attack, result['summaries'][0])
        self.assertIn('Scope: '+attack, '\n'.join(result['paragraphs']))
        self.assertNotIn('<img', result['html'])
        self.assertIn('&lt;img', result['html'])

    def test_unavailable_projection_or_failed_fetch_never_leaves_verified_view(self):
        for status in ('NOT_CONFIGURED', 'UNAVAILABLE', 'unexpected-status'):
            with self.subTest(status=status):
                data = fixture()
                data['status'] = status
                result = render(data)
                self.assertEqual(result['summaries'], [])
                self.assertNotIn('VERIFIED_COMPONENT', result['html'])
                self.assertIn('Current evidence health is not continuously certified', result['html'])
        result = render(next_fetch_ok=False)
        self.assertIn('VERIFIED_COMPONENT', result['first_html'])
        self.assertIn('Historical decisions unavailable.', result['html'])
        self.assertNotIn('VERIFIED_COMPONENT', result['html'])
        data = fixture()
        data['decisions'] = None
        self.assertIn('Historical decisions unavailable.', render(data)['html'])


if __name__ == '__main__':
    unittest.main()
