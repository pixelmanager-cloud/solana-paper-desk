"""Agent 05 preliminary adversarial review; public captures and local SQLite only."""
import base64
import copy
import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from desk.decision_runner import assess, consume
from desk.entry_evidence import evaluate
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.model import canonical, digest
from desk.decode import decode


class CloudEntryGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = EvidenceStore(self.root / 'evidence.sqlite')
        fixtures = Path(__file__).resolve().parents[1] / 'fixtures'
        fixture = json.loads((fixtures / 'mainnet-holder-snapshot.json').read_text())
        e, r = fixture['enumeration'], fixture['rpc']['response']
        self.holder = {'method': 'getMultipleAccounts', 'params': [[e['mint'], *[x['address'] for x in e['accounts']]],
                       {'encoding': 'base64', 'commitment': 'confirmed', 'minContextSlot': e['indexed_slot_max']}], 'result': r}
        self.mint = {'method': 'getAccountInfo', 'params': [e['mint'], {'encoding': 'base64', 'commitment': 'confirmed'}],
                     'result': {'value': copy.deepcopy(r['value'][0])}}
        self.report = {'mint': e['mint'], 'observed_at': 100, 'findings': [], 'unknowns': [],
                       'mint_evidence_hash': self.store.save(self.mint),
                       'holder_snapshot': {'evidence_hash': self.store.save(self.holder)}, 'verified_pools': []}
        payload = json.loads((fixtures / 'mainnet-launch.json').read_text())['payload']
        event = next(x for x in decode(payload)['program_observations'] if x.get('name') == 'CreateEvent')
        self.launch_mint, self.launch_at = event['fields']['mint'], event['fields']['timestamp']
        f = payload['params']['result']
        self.raw = {**f['transaction'], 'slot': f['slot'], 'blockTime': self.launch_at, 'signature': f['signature']}

    def signed(self, report=None):
        report = copy.deepcopy(self.report if report is None else report)
        report.pop('report_hash', None)
        report['report_hash'] = digest(report)
        return report

    def decision(self, report=None, now=100, progress=None):
        report = self.signed(report)
        return assess({'id': 'scan', 'mint': self.report['mint'], 'created': 99,
                       'status': 'COMPLETE', 'result': canonical(report)}, now, self.store, progress)

    def blocked(self, result):
        self.assertFalse(result['eligible_for_trading'])
        self.assertIn('CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED', result['reasons'])
        self.assertIn('EXACT_ENTRY_EXIT_ROUTE_POLICY_NOT_VERIFIED', result['reasons'])

    def history_report(self, pages=None):
        pages = pages or [{'data': [self.raw]}]
        responses = iter(pages)
        _, query = collect_history(self.launch_mint, self.launch_at - 1, self.launch_at + 1,
                                   lambda *a: next(responses), max_pages=len(pages),
                                   capture=self.store.save, token_accounts='none')
        return {'mint': self.launch_mint, 'history_queries': [query]}, query

    def test_forged_summary_approvals_do_not_authorize_entry(self):
        report = {**self.report, 'eligible_for_trading': True, 'bundle_verified': True,
                  'transfer_history_complete': True, 'transaction_policy_ok': True,
                  'simulation_passed': True, 'strategy_inputs_verified': True}
        d = self.decision(report)
        self.blocked(d)
        self.assertEqual(d['decision'], 'REJECT')

    def test_missing_and_invalid_evidence_references_fail_closed(self):
        for key in (None, '', '0' * 64, 7):
            with self.subTest(key=key):
                report = {**self.report, 'mint_evidence_hash': key, 'holder_snapshot': {'evidence_hash': key}}
                r = evaluate(self.signed(report), self.store)
                self.blocked(r)
                self.assertIn('TOKEN_RAW_EVIDENCE_UNAVAILABLE', r['reasons'])
                self.assertNotIn('gross_top10_supply_pct', r['metrics'])

    def test_tampered_content_under_original_hash_is_rejected(self):
        key = self.report['mint_evidence_hash']
        changed = copy.deepcopy(self.mint)
        changed['params'][0] = self.launch_mint
        raw = canonical(changed).encode()
        with self.store.connect() as db:
            db.execute('UPDATE pages SET payload=?,raw_bytes=? WHERE hash=?', (zlib.compress(raw), len(raw), key))
        r = evaluate(self.signed(), self.store)
        self.assertIn('TOKEN_RAW_EVIDENCE_UNAVAILABLE', r['reasons'])
        self.blocked(r)

    def test_cross_mint_requests_and_scan_binding_are_rejected(self):
        report = {**self.report, 'mint': self.launch_mint}
        d = self.decision(report)
        self.assertIn('INVESTIGATION_MINT_UNVERIFIED', d['reasons'])
        self.assertIn('TOKEN_RAW_EVIDENCE_UNAVAILABLE', d['reasons'])
        self.assertIn('HOLDER_RAW_EVIDENCE_UNAVAILABLE', d['reasons'])
        self.blocked(d)

    def test_cross_mint_holder_bytes_are_rejected_even_with_new_hash(self):
        holder = copy.deepcopy(self.holder)
        raw = bytearray(base64.b64decode(holder['result']['value'][1]['data'][0]))
        raw[:32] = bytes(32)
        holder['result']['value'][1]['data'][0] = base64.b64encode(raw).decode()
        report = {**self.report, 'holder_snapshot': {'evidence_hash': self.store.save(holder)}}
        r = evaluate(self.signed(report), self.store)
        self.assertIn('SNAPSHOT_HOLDER_IDENTITY_CHANGED', r['reasons'])
        self.blocked(r)

    def test_holder_slot_drift_and_duplicate_accounts_are_rejected(self):
        for mode in ('drift', 'duplicate'):
            with self.subTest(mode=mode):
                holder = copy.deepcopy(self.holder)
                if mode == 'drift':
                    holder['result']['context']['slot'] = holder['params'][1]['minContextSlot'] + 33
                else:
                    holder['params'][0].append(holder['params'][0][1])
                    holder['result']['value'].append(holder['result']['value'][1])
                report = {**self.report, 'holder_snapshot': {'evidence_hash': self.store.save(holder)}}
                r = evaluate(self.signed(report), self.store)
                self.assertEqual(r['gates']['holder_snapshot']['status'], 'BLOCKED')
                self.blocked(r)

    def test_inconsistent_separate_mint_and_snapshot_controls_block_entry(self):
        mint = copy.deepcopy(self.mint)
        raw = bytearray(base64.b64decode(mint['result']['value']['data'][0]))
        raw[:4] = (1).to_bytes(4, 'little')
        raw[4:36] = bytes([1]) * 32
        mint['result']['value']['data'][0] = base64.b64encode(raw).decode()
        r = evaluate(self.signed({**self.report, 'mint_evidence_hash': self.store.save(mint)}), self.store)
        self.assertEqual(r['gates']['token_controls']['status'], 'BLOCKED')
        self.assertEqual(r['gates']['holder_snapshot']['status'], 'VERIFIED_COMPONENT')
        self.blocked(r)

    def test_freshness_boundary_missing_future_bool_and_string(self):
        for observed in (None, True, '100', 89, 101):
            with self.subTest(observed=observed):
                d = self.decision({**self.report, 'observed_at': observed})
                self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', d['reasons'])
                self.blocked(d)
        self.assertNotIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', self.decision({**self.report, 'observed_at': 90})['reasons'])

    def test_rehashing_timestamp_refreshes_freshness_label_but_never_entry(self):
        stale = self.decision(now=1000)
        refreshed = self.decision({**self.report, 'observed_at': 1000}, now=1000)
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', stale['reasons'])
        self.assertNotIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', refreshed['reasons'])
        self.assertEqual(stale['entry_evidence']['gates'], refreshed['entry_evidence']['gates'])
        self.blocked(refreshed)

    def test_old_public_snapshot_is_verified_component_without_age_binding(self):
        r = evaluate(self.signed(), self.store)
        self.assertEqual(r['gates']['holder_snapshot']['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(r['gates']['token_controls']['status'], 'VERIFIED_COMPONENT')
        self.blocked(r)

    def test_new_checksum_does_not_authenticate_synthetic_provider_bytes(self):
        holder = copy.deepcopy(self.holder)
        raw = bytearray(base64.b64decode(holder['result']['value'][1]['data'][0]))
        raw[32:64] = bytes([2]) * 32  # Forged wallet, same balances and valid layout.
        holder['result']['value'][1]['data'][0] = base64.b64encode(raw).decode()
        r = evaluate(self.signed({**self.report, 'holder_snapshot': {'evidence_hash': self.store.save(holder)}}), self.store)
        self.assertEqual(r['gates']['holder_snapshot']['status'], 'VERIFIED_COMPONENT')
        self.blocked(r)

    def test_history_summary_forgery_range_rebinding_and_missing_manifest(self):
        for mode in ('complete', 'range', 'manifest'):
            with self.subTest(mode=mode):
                report, q = self.history_report([{'data': [self.raw], 'paginationToken': 'next'}])
                if mode == 'complete': q['query_coverage_verified'] = True
                elif mode == 'range': q['start'] -= 1
                else: del q['pages'][0]['request_evidence_hash']
                q.pop('evidence_hash', None)
                q['evidence_hash'] = digest(q)
                r = evaluate(self.signed(report), self.store)
                self.assertIn('REQUEST_BOUND_HISTORY_UNAVAILABLE', r['reasons'])
                self.blocked(r)

    def test_cross_mint_history_and_nineteen_query_budget_fail_closed(self):
        report, q = self.history_report()
        for attack in ({**report, 'mint': self.report['mint']}, {**report, 'history_queries': [q] * 19}):
            r = evaluate(self.signed(attack), self.store)
            self.assertEqual(r['gates']['mint_history_coverage']['status'], 'BLOCKED')
            self.blocked(r)

    def test_history_missing_page_and_reordered_cursor_chain_fail_closed(self):
        report, q = self.history_report([{'data': [self.raw], 'paginationToken': 'next'}, {'data': []}])
        q['pages'].reverse()
        q.pop('evidence_hash', None)
        q['evidence_hash'] = digest(q)
        self.assertIn('REQUEST_BOUND_HISTORY_UNAVAILABLE', evaluate(self.signed(report), self.store)['reasons'])
        report, q = self.history_report()
        with self.store.connect() as db:
            db.execute('DELETE FROM pages WHERE hash=?', (q['pages'][0]['payload_hash'],))
        self.assertIn('REQUEST_BOUND_HISTORY_UNAVAILABLE', evaluate(self.signed(report), self.store)['reasons'])

    def test_continuation_does_not_refresh_original_time_or_replace_gates(self):
        report, q = self.history_report()
        original = {**report, 'observed_at': 1}
        scan = {'id': 'scan', 'mint': self.launch_mint, 'created': 1, 'status': 'COMPLETE', 'result': canonical(self.signed(original))}
        progress = {'history_queries': [q], 'evidence_hash': digest(q), 'requests_used': 18, 'observed_at': 1000}
        d = assess(scan, 1000, self.store, progress)
        self.assertEqual(d['observed_at'], 1)
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', d['reasons'])
        self.assertEqual(d['entry_evidence']['gates'], evaluate(self.signed(original), self.store)['gates'])
        self.blocked(d)

    def test_consumer_preserves_original_after_source_timestamp_rewrite(self):
        source, dest = self.root / 'research.sqlite', self.root / 'decisions.sqlite'
        with sqlite3.connect(source) as db:
            db.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            db.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('scan', self.report['mint'], 99, 'COMPLETE', canonical(self.signed())))
        with patch('socket.socket', side_effect=AssertionError('No network permitted')):
            first = consume(source, dest, now=1000, evidence_db=self.store.path)
            with sqlite3.connect(source) as db:
                db.execute('UPDATE scans SET result=?', (canonical(self.signed({**self.report, 'observed_at': 1000})),))
            second = consume(source, dest, now=1000, evidence_db=self.store.path)
        self.assertEqual(first['consumed'], 1)
        self.assertEqual(second['consumed'], 0)
        with sqlite3.connect(dest) as db:
            payload, decision = db.execute('SELECT source_payload,decision FROM decisions').fetchone()
            self.assertEqual(json.loads(json.loads(payload)['result'])['observed_at'], 100)
            self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', json.loads(decision)['reasons'])
            self.assertEqual(db.execute('SELECT count(*) FROM decision_evaluations').fetchone()[0], 1)
