"""GET contract fixtures: temporary SQLite only; never submit or run scans."""
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from desk.dashboard import Jobs, handler
from desk.decision_runner import consume
from desk.engine import initial_state, transition
from desk.entry_evidence import POLICY
from desk.evidence import EvidenceStore
from desk.ledger import Ledger
from desk.model import canonical, digest
from tests.helpers import T, config, control, event


class CloudDashboardEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scanner = Mock(side_effect=AssertionError('GET must not investigate'))
        self.jobs = Jobs(self.root / 'research.sqlite', scanner=self.scanner)
        self.cfg = config()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.jobs, 0))
        self.server.RequestHandlerClass = handler(self.jobs, self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.addCleanup(self.scanner.assert_not_called)
        self.submit_guard = patch.object(self.jobs, 'submit', side_effect=AssertionError('No submitted scans'))
        self.submit_guard.start()
        self.addCleanup(self.submit_guard.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        try:
            connection.request('GET', path, headers=headers or {})
            response = connection.getresponse()
            body = json.loads(response.read())
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertEqual(response.getheader('X-Content-Type-Options'), 'nosniff')
            self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))
            return response.status, body
        finally:
            connection.close()

    def ok(self, path):
        status, body = self.get(path)
        self.assertEqual(status, 200)
        return body

    def scan(self, key='scan', report=None, created=T):
        if report is None:
            report = {'mint': 'fixture-mint', 'observed_at': T, 'eligible_for_trading': True,
                      'findings': [], 'unknowns': ['FIXTURE_OWNERSHIP_UNRESOLVED']}
        with sqlite3.connect(self.jobs.db) as c:
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',
                      (key, report['mint'], created, 'COMPLETE', canonical(report)))
        return report

    def apply(self, value):
        ledger = Ledger(self.root / 'active-paper.sqlite')
        try:
            return ledger.apply(value, self.cfg, transition, initial_state)
        finally:
            ledger.close()

    def paper(self, now=T):
        with patch('desk.paper_view.time.time', return_value=now):
            return self.ok('/api/paper')

    def snapshot(self):
        # Logical snapshots also cover WAL-backed databases and original records.
        result = {}
        for path in self.root.glob('*.sqlite'):
            with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as c:
                result[path.name] = list(c.iterdump())
        return result

    def test_missing_databases_are_not_zero_balances_or_created(self):
        before = set(self.root.iterdir())
        paper = self.paper()
        self.assertEqual(paper['status'], 'NOT_CONFIGURED')
        for key in ('cash_sol', 'realized_pnl_sol', 'estimated_equity_sol'):
            self.assertIsNone(paper[key])
        self.assertEqual(self.ok('/api/decisions'), {'status': 'NOT_CONFIGURED', 'decisions': []})
        self.assertEqual(self.ok('/api/launches')['launches'], [])
        self.assertEqual(before, set(self.root.iterdir()))

    def test_status_preserves_research_mode_and_request_ceiling(self):
        self.assertEqual(self.ok('/api/status'), {'mode': 'RESEARCH_ONLY', 'live_trading': False,
                         'max_scans_per_day': 10, 'max_rpc_calls_per_scan': 18,
                         'automatic_entry_enabled': False})

    def test_foreign_host_and_origin_are_rejected_on_every_read_endpoint(self):
        for path in ('/api/status', '/api/scans', '/api/decisions', '/api/paper', '/api/launches'):
            for headers in ({'Host': 'foreign.example'}, {'Origin': 'https://foreign.example'},
                            {'Host': '127.0.0.1'}, {'Origin': 'null'}):
                with self.subTest(path=path, headers=headers):
                    status, body = self.get(path, headers)
                    self.assertEqual(status, 403)
                    self.assertEqual(body, {'error': 'Local access only'})
        status, _ = self.get('/api/status', {'Host': f'localhost:{self.server.server_port}',
                         'Origin': f'http://localhost:{self.server.server_port}'})
        self.assertEqual(status, 200)

    def test_scan_claims_do_not_become_decision_approval(self):
        self.scan()
        consume(self.jobs.db, self.root / 'paper-decisions.sqlite', now=T)
        self.assertTrue(self.ok('/api/scans')[0]['result']['eligible_for_trading'])
        decision = self.ok('/api/decisions')['decisions'][0]
        self.assertEqual(decision['decision'], 'REJECT')
        self.assertFalse(decision['eligible_for_trading'])
        source = self.ok('/api/scans')[0]
        source['result'] = canonical(source['result'])
        self.assertEqual(decision['source_hash'], digest(source))
        self.assertIn('FIXTURE_OWNERSHIP_UNRESOLVED', decision['reasons'])
        self.assertIn('CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED', decision['reasons'])
        self.assertEqual(decision['entry_evidence']['gates']['token_controls']['status'], 'BLOCKED')

    def test_old_and_future_observations_remain_unfresh_at_evaluation(self):
        for key, observed in (('old', T-11), ('future', T+1)):
            self.scan(key, {'mint': 'fixture-mint', 'observed_at': observed})
        consume(self.jobs.db, self.root / 'paper-decisions.sqlite', now=T)
        for decision in self.ok('/api/decisions')['decisions']:
            self.assertEqual(decision['evaluated_at'], T)
            self.assertNotEqual(decision['observed_at'], T)
            self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', decision['reasons'])

    def test_new_evaluation_does_not_refresh_original_observation(self):
        self.scan()
        consume(self.jobs.db, self.root / 'paper-decisions.sqlite', now=T)
        path = self.root / 'paper-decisions.sqlite'
        with sqlite3.connect(path) as c:
            original = c.execute('SELECT * FROM decisions').fetchall()
            value = json.loads(original[0][3])
            value['evaluated_at'] = T+100
            value['entry_evidence']['ownership_progress_hash'] = 'a'*64
            value['entry_evidence']['ownership_requests_used'] = 18
            c.execute('INSERT INTO decision_evaluations VALUES(?,?,?,?,?)',
                      ('scan', POLICY+':'+ 'a'*64, value['source_hash'], original[0][2], canonical(value)))
        latest = self.ok('/api/decisions')['decisions'][0]
        self.assertEqual(latest['observed_at'], T)
        self.assertEqual(latest['evaluated_at'], T+100)
        self.assertEqual(latest['entry_evidence']['ownership_requests_used'], 18)
        self.assertFalse(latest['eligible_for_trading'])
        with sqlite3.connect(path) as c:
            self.assertEqual(c.execute('SELECT * FROM decisions').fetchall(), original)

    def test_hash_bound_fixture_component_keeps_gross_supply_scope(self):
        capture = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/mainnet-holder-snapshot.json').read_text())
        enumeration, response = capture['enumeration'], capture['rpc']['response']
        store = EvidenceStore(self.root / 'evidence.sqlite')
        holder_hash = store.save({'method': 'getMultipleAccounts', 'params':
            [[enumeration['mint'], *[r['address'] for r in enumeration['accounts']]],
             {'encoding': 'base64', 'commitment': 'confirmed', 'minContextSlot': enumeration['indexed_slot_max']}],
            'result': response})
        mint_hash = store.save({'method': 'getAccountInfo', 'params':
            [enumeration['mint'], {'encoding': 'base64', 'commitment': 'confirmed'}],
            'result': {'value': response['value'][0]}})
        report = {'mint': enumeration['mint'], 'observed_at': T, 'mint_evidence_hash': mint_hash,
                  'holder_snapshot': {'evidence_hash': holder_hash}, 'verified_pools': []}
        report['report_hash'] = digest(report)
        self.scan(report=report)
        consume(self.jobs.db, self.root / 'paper-decisions.sqlite', now=T, evidence_db=store.path)
        decision = self.ok('/api/decisions')['decisions'][0]
        gate = decision['entry_evidence']['gates']['holder_snapshot']
        self.assertEqual(gate['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(gate['evidence_hashes'], [holder_hash])
        self.assertIn('Gross supply', gate['scope'])
        self.assertIn('gross_top10_supply_pct', decision['entry_evidence']['metrics'])
        self.assertEqual(decision['decision'], 'REJECT')
        self.assertNotIn('bundle_pct', decision['entry_evidence']['metrics'])

    def test_decision_and_scan_projection_limits_are_not_coverage_claims(self):
        for i in range(55):
            self.scan(str(i), created=T+i)
        consume(self.jobs.db, self.root / 'paper-decisions.sqlite', now=T, limit=50)
        self.assertEqual(len(self.ok('/api/scans')), 50)
        decisions = self.ok('/api/decisions')['decisions']
        self.assertEqual(len(decisions), 20)
        self.assertTrue(all(d['decision'] == 'REJECT' for d in decisions))
        self.assertEqual(self.ok('/api/launches')['coverage'], 'Periodic bounded sample; not all launches')

    def test_obsolete_policy_rows_cannot_replace_current_policy_view(self):
        self.scan()
        consume(self.jobs.db, self.root / 'paper-decisions.sqlite', now=T)
        with sqlite3.connect(self.root / 'paper-decisions.sqlite') as c:
            c.execute('INSERT INTO decision_evaluations VALUES(?,?,?,?,?)',
                      ('scan', 'obsolete-policy', '0'*64, '{}',
                       canonical({'decision': 'FORGED_OLD_APPROVAL'})))
        decisions = self.ok('/api/decisions')['decisions']
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]['decision'], 'REJECT')
        self.assertEqual(decisions[0]['policy'], POLICY)

    def test_launch_projection_keeps_observation_identity_and_skips_bad_json(self):
        capture = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/mainnet-launch.json').read_text())
        from desk.decode import decode
        observation = decode(capture['payload'])
        launch = next(ix for ix in observation['program_observations']
                      if ix.get('kind') == 'LAUNCH' and ix.get('status') == 'IDENTIFIED')
        ledger = Ledger(self.root / 'launches.sqlite')
        try:
            ledger.record_raw('public-launch-fixture', T, observation['slot'], capture['payload'])
        finally:
            ledger.close()
        with sqlite3.connect(self.root / 'launches.sqlite') as c:
            c.execute('INSERT INTO raw_events(source_id,received_at,slot,payload) VALUES(?,?,?,?)',
                      ('malformed', T+1, 0, 'not-json'))
        body = self.ok('/api/launches')
        self.assertEqual(body['launches'], [{'mint': launch['mint'], 'wallet': launch['wallet'],
                         'slot': observation['slot'], 'signature': observation['signature'], 'received_at': T}])
        self.assertEqual(body['coverage'], 'Periodic bounded sample; not all launches')
        self.assertNotIn('eligible_for_trading', body['launches'][0])

    def test_stale_future_and_exact_ttl_marks_are_presented_without_fabrication(self):
        self.apply(event())
        for now in (T, T+self.cfg['price_ttl_seconds']):
            self.assertIsNotNone(self.paper(now)['estimated_equity_sol'])
        for now in (T-1, T+self.cfg['price_ttl_seconds']+1):
            paper = self.paper(now)
            self.assertEqual(len(paper['positions']), 1)
            self.assertIsNone(paper['estimated_equity_sol'])
            self.assertIsNone(paper['positions'][0]['unrealized_pnl_sol'])
            self.assertEqual(paper['positions'][0]['mark_status'], 'STALE')
        position = self.paper()['positions'][0]
        self.assertEqual(position['provenance'], 'SYNTHETIC_TEST_ONLY')
        self.assertFalse(position['valuation_verified'])

    def test_pause_is_durable_and_does_not_claim_connected_runner(self):
        self.apply(event())
        self.apply(control(T+1, 'PAUSE_ENTRY'))
        paper = self.paper(T+1)
        self.assertEqual(paper['strategy_mode'], 'ENTRY_PAUSED')
        self.assertEqual(paper['runner_status'], 'NOT_CONNECTED')
        self.assertFalse(paper['automatic_entry_enabled'])
        self.assertEqual(len(paper['positions']), 1)
        self.assertEqual(self.paper(T+1), paper)

    def test_unverified_exit_survives_resume_and_reopen_without_sell_fill(self):
        self.apply(event())
        self.apply(event(T+1, danger=True, provenance='MAINNET_OBSERVATION'))
        self.apply(control(T+2, 'RESUME'))
        paper = self.paper(T+2)
        self.assertEqual(paper['strategy_mode'], 'EXIT_ONLY')
        self.assertEqual(paper['positions'][0]['exit_blocked'], 'EXIT_SELLABILITY_UNVERIFIED')
        self.assertEqual(paper['positions'][0]['mark_status'], 'UNVERIFIED_EXIT')
        self.assertIsNone(paper['estimated_equity_sol'])
        self.assertEqual(paper['realized_pnl_sol'], '0')
        outcomes = [r['outcome'] for r in paper['recent_outcomes']]
        self.assertTrue(any(o['type'] == 'blocked_exit' and o.get('reasons') for o in outcomes))
        self.assertFalse(any(o.get('side') == 'sell' for o in outcomes))
        self.assertEqual(self.paper(T+2), paper)

    def test_outcomes_expose_net_accounting_and_decimal_strings(self):
        self.apply(event())
        self.apply(event(T+1, danger=True))
        paper = self.paper(T+1)
        outcomes = [r['outcome'] for r in paper['recent_outcomes']]
        buy = next(o for o in outcomes if o.get('side') == 'buy')
        sell = next(o for o in outcomes if o.get('side') == 'sell')
        self.assertEqual(paper['positions'], [])
        self.assertEqual(paper['realized_pnl_sol'], sell['realized_pnl_sol'])
        self.assertIsInstance(paper['cash_sol'], str)
        self.assertEqual(buy['fee_sol'], self.cfg['fixed_fee_sol'])
        self.assertEqual(sell['fee_sol'], self.cfg['fixed_fee_sol'])
        self.assertIn('proceeds_sol', sell)
        self.assertIn('not evidence of profitability', paper['notice'])

    def test_corrupt_projections_are_unavailable_and_redacted(self):
        (self.root / 'active-paper.sqlite').write_text('SENSITIVE_FIXTURE_SENTINEL')
        with sqlite3.connect(self.root / 'paper-decisions.sqlite') as c:
            c.execute('CREATE TABLE decision_evaluations(scan_id TEXT, policy TEXT, decision TEXT)')
            c.execute('INSERT INTO decision_evaluations VALUES(?,?,?)', ('bad', POLICY, 'SENSITIVE_FIXTURE_SENTINEL'))
        paper, decisions = self.paper(), self.ok('/api/decisions')
        self.assertEqual(paper['status'], 'LEDGER_UNAVAILABLE')
        self.assertEqual(decisions, {'status': 'UNAVAILABLE', 'decisions': []})
        self.assertNotIn('SENSITIVE_FIXTURE_SENTINEL', json.dumps([paper, decisions]))

    def test_gets_preserve_original_records_and_never_create_evidence(self):
        self.scan()
        consume(self.jobs.db, self.root / 'paper-decisions.sqlite', now=T)
        self.apply(event())
        before = self.snapshot()
        for path in ('/api/status', '/api/scans', '/api/decisions', '/api/paper', '/api/launches'):
            self.ok(path)
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.root / 'evidence.sqlite').exists())


if __name__ == '__main__':
    unittest.main()
