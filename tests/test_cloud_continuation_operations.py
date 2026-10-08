"""Offline operational probes for PR24 raw histories plus PR25 consumer.

Only fixture transport supplies data; all replay/consumer functions execute.
MEASUREMENTS records observations for an external runner, never CI deadlines.
"""
import copy
import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk.dashboard import handler
from desk.decision_runner import assess, consume, recent_decisions
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.model import canonical, digest
from desk.ownership_worker import advance, saved_progress

MEASUREMENTS = []
OBSERVED_AT = 120


@contextmanager
def offline_only():
    with ExitStack() as stack:
        for target in ('socket.socket', 'desk.providers.fetch_json', 'desk.providers.api_key'):
            stack.enter_context(patch(target, side_effect=AssertionError('Offline fixture boundary: ' + target)))
        yield


class MemoryHTTPConnection:
    """Real BaseHTTPRequestHandler parsing without an OS socket or listener."""
    def __init__(self, path):
        self.input = io.BytesIO(('GET ' + path + ' HTTP/1.0\r\nHost: 127.0.0.1:8765\r\n\r\n').encode())
        self.output = io.BytesIO()

    def makefile(self, *args):
        return self.input

    def sendall(self, body):
        self.output.write(body)


class CloudContinuationOperationsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads((Path(__file__).resolve().parents[1] /
            'fixtures/cloud_history_integration/multi_account.json').read_text())
        cls.baseline_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.baseline_tmp.cleanup)
        cls.baseline = Path(cls.baseline_tmp.name)
        source, evidence = cls.baseline / 'research.sqlite', cls.baseline / 'evidence.sqlite'
        store = EvidenceStore(evidence)
        cls.fixture_calls = []

        def transport(method, params):
            cls.fixture_calls.append(method)
            f = cls.fixture
            if method == 'getMultipleAccounts':
                if params != f['snapshot']['params']:
                    raise AssertionError('Unexpected fixture snapshot request')
                return copy.deepcopy(f['snapshot']['result'])
            if method == 'getBlockTime':
                if params != f['block_time']['params']:
                    raise AssertionError('Unexpected fixture clock request')
                return f['block_time']['result']
            if method != 'getTransactionsForAddress':
                raise AssertionError('Unexpected fixture method')
            address, options = params
            if options['commitment'] != 'finalized' or options['filters']['tokenAccounts'] != 'none':
                raise AssertionError('Unbound fixture query')
            if 'slot' in options['filters'] and options['filters']['slot'] != {'gte': 0, 'lt': 21}:
                raise AssertionError('Wrong cutoff')
            rows = f['records'] if address == f['mint'] else [r for r in f['records'] if any(
                r['transaction']['message']['accountKeys'][b['accountIndex']]['pubkey'] == address
                for side in ('preTokenBalances', 'postTokenBalances') for b in r['meta'][side])]
            split = max(1, len(rows) // 2)
            if 'paginationToken' not in options:
                return {'data': copy.deepcopy(rows[:split]), 'paginationToken': 'second:' + address}
            if options['paginationToken'] != 'second:' + address:
                raise AssertionError('Wrong fixture cursor')
            return {'data': copy.deepcopy(rows[split:])}

        with offline_only():
            _, initial = collect_history(cls.fixture['mint'], 90, 120, transport, max_pages=2,
                                         capture=store.save, token_accounts='none')
            mint_hash = store.save({'method': 'getAccountInfo',
                'params': [cls.fixture['mint'], {'encoding': 'base64', 'commitment': 'confirmed'}],
                'result': {'value': cls.fixture['snapshot']['result']['value'][0]}})
            report = {'mint': cls.fixture['mint'], 'observed_at': OBSERVED_AT, 'calls': 3,
                      'findings': [], 'unknowns': [], 'mint_evidence_hash': mint_hash,
                      'history_queries': [initial]}
            report['report_hash'] = digest(report)
            with sqlite3.connect(source) as c:
                c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
                c.executemany('INSERT INTO scans VALUES(?,?,?,?,?)', [
                    (f'scan-{i:03}', cls.fixture['mint'], OBSERVED_AT, 'COMPLETE', canonical(report))
                    for i in range(51)])
            # Independent source-bound budgets/heads/jobs for 51 investigations.
            # Identical synthetic raw pages intentionally deduplicate in storage.
            for i in range(51):
                for _ in range(4):
                    result = advance(source, evidence, f'scan-{i:03}', transport, max_calls=3)
                if not result['snapshot']['reconciled'] or result['requests_used'] != 13:
                    raise AssertionError('PR24 raw continuation fixture did not reconcile')
                if len(result['history']['inventory']['accounts']) != 3:
                    raise AssertionError('Wrong account frontier')
            cls.fixture_calls = tuple(cls.fixture_calls)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.evidence = self.root / 'research.sqlite', self.root / 'evidence.sqlite'
        self.journal = self.root / 'paper-decisions.sqlite'
        # SQLite backup avoids aliasing or touching the baseline across tests.
        for name in ('research.sqlite', 'evidence.sqlite'):
            with sqlite3.connect((self.baseline / name).resolve().as_uri() + '?mode=ro', uri=True) as src:
                with sqlite3.connect(self.root / name) as dst:
                    src.backup(dst)
        self.guard = offline_only()
        self.guard.__enter__()
        self.addCleanup(self.guard.__exit__, None, None, None)

    def scans(self):
        with sqlite3.connect(self.source) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM scans ORDER BY id')]

    def journal_rows(self):
        with sqlite3.connect(self.journal) as c:
            return {table: c.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                    for table in ('decisions', 'decision_evaluations')}

    def database_state(self, path):
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as c:
            return tuple(c.iterdump())

    def measure(self, label, *, limit=20, now=1000, instrument=False):
        counters = {'assessed_ids': [], 'loads': 0, 'raw_bytes_loaded': 0, 'decodes': 0}
        with ExitStack() as stack:
            if instrument:
                with sqlite3.connect(self.evidence) as c:
                    sizes = dict(c.execute('SELECT hash,raw_bytes FROM pages'))
                original_load = EvidenceStore.load
                def load(store, key):
                    counters['loads'] += 1
                    counters['raw_bytes_loaded'] += sizes.get(key, 0)
                    return original_load(store, key)
                def evaluate(scan, *args, **kwargs):
                    counters['assessed_ids'].append(scan['id'])
                    return assess(scan, *args, **kwargs)
                from desk.history import decode as original_decode
                def decode(raw):
                    counters['decodes'] += 1
                    return original_decode(raw)
                stack.enter_context(patch.object(EvidenceStore, 'load', new=load))
                stack.enter_context(patch('desk.decision_runner.assess', new=evaluate))
                stack.enter_context(patch('desk.history.decode', new=decode))
            started, cpu = time.perf_counter(), time.process_time()
            result = consume(self.source, self.journal, now=now, limit=limit, evidence_db=self.evidence)
            elapsed, cpu_elapsed = time.perf_counter()-started, time.process_time()-cpu
        MEASUREMENTS.append({'label': label, 'limit': limit, 'now': now, 'instrumented': instrument,
                             'elapsed_seconds': elapsed, 'cpu_seconds': cpu_elapsed,
                             'consumed': result['consumed'], **counters})
        return result, counters

    def assert_historical_rejection(self, decision):
        self.assertEqual(decision['observed_at'], OBSERVED_AT)
        self.assertEqual(decision['decision'], 'REJECT')
        self.assertFalse(decision['eligible_for_trading'])
        evidence = decision['entry_evidence']
        gate = evidence['gates']['history_snapshot']
        self.assertEqual(gate['status'], 'VERIFIED_COMPONENT')
        self.assertEqual(gate['scope'], 'Historical finalized cutoff; not current entry freshness')
        self.assertEqual(evidence['gates']['transfer_history']['status'], 'BLOCKED')
        for reason in ('FUNDING_SERVICE_CLASSIFICATION_NOT_VERIFIED_FOR_ENTRY',
                       'CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED',
                       'EXACT_ENTRY_EXIT_ROUTE_POLICY_NOT_VERIFIED',
                       'LIVE_FLOW_MOMENTUM_AND_COST_INPUTS_UNVERIFIED'):
            self.assertIn(reason, decision['reasons'])
        return gate

    def test_raw_three_account_eight_page_provenance_and_no_budget_mutation(self):
        scan = self.scans()[0]
        before_source, before_evidence = self.database_state(self.source), self.database_state(self.evidence)
        store = EvidenceStore(self.evidence, read_only=True)
        progress = saved_progress(store, scan)
        self.assertEqual(len(progress['history_queries']), 4)
        self.assertEqual(sum(len(q['pages']) for q in progress['history_queries']), 8)
        self.assertEqual(progress['requests_used'], 13)
        decision = assess(scan, 1000, store)
        gate = self.assert_historical_rejection(decision)
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', decision['reasons'])
        history = decision['entry_evidence']['continued_ownership_history']
        self.assertEqual(history['observed_transaction_count'], 4)
        self.assertEqual(history['account_queries']['verified'], 3)
        self.assertEqual({r['account']: r['amount_raw'] for r in history['account_continuity']['end_states']},
                         self.fixture['expected_balances'])
        for query in progress['history_queries']:
            for page in query['pages']:
                self.assertIn(page['request_evidence_hash'], gate['evidence_hashes'])
                self.assertIn(page['payload_hash'], gate['evidence_hashes'])
        self.assertEqual(self.database_state(self.source), before_source)
        self.assertEqual(self.database_state(self.evidence), before_evidence)

    def test_fifty_completed_repeated_consumption_is_idempotent_but_replays(self):
        with sqlite3.connect(self.source) as c:
            c.execute("DELETE FROM scans WHERE id='scan-050'")
        source_before, evidence_before = self.database_state(self.source), self.database_state(self.evidence)
        for batch, expected in enumerate((20, 20, 10)):
            result, _ = self.measure('50-initial-' + str(batch), instrument=True)
            self.assertEqual(result['consumed'], expected)
            for decision in result['decisions']:
                self.assert_historical_rejection(decision)
        before = self.journal_rows()
        self.assertEqual(len(before['decisions']), 50)
        self.assertEqual(len(before['decision_evaluations']), 50)
        for repeat in range(3):
            result, _ = self.measure('50-warm-unchanged-' + str(repeat))
            self.assertEqual(result['consumed'], 0)
            self.assertEqual(result['decisions'], [])
        result, counters = self.measure('50-unchanged-counted', instrument=True)
        self.assertEqual(result['consumed'], 0)
        self.assertEqual(set(counters['assessed_ids']), {f'scan-{i:03}' for i in range(50)})
        self.assertEqual(len(counters['assessed_ids']), 50)
        self.assertGreater(counters['loads'], 0)
        self.assertGreater(counters['decodes'], 0)
        self.assertEqual(self.journal_rows(), before)
        self.assertEqual(self.database_state(self.source), source_before)
        self.assertEqual(self.database_state(self.evidence), evidence_before)

    def test_recent_window_is_fifty_and_limit_one_does_not_cap_replay_work(self):
        first, _ = self.measure('51-initial', limit=50)
        self.assertEqual(first['consumed'], 50)
        second, _ = self.measure('51-finish', limit=50)
        self.assertEqual(second['consumed'], 1)
        before = self.journal_rows()
        result, counters = self.measure('51-recent-50-limit-1', limit=1, instrument=True)
        self.assertEqual(result['consumed'], 0)
        self.assertEqual(set(counters['assessed_ids']), {f'scan-{i:03}' for i in range(1, 51)})
        self.assertEqual(len(counters['assessed_ids']), 50)
        self.assertNotIn('scan-000', counters['assessed_ids'])
        self.assertEqual(self.journal_rows(), before)

    def test_original_age_not_bank_clock_or_later_evaluation_controls_freshness(self):
        scan, store = self.scans()[0], EvidenceStore(self.evidence, read_only=True)
        for now, stale in ((119, True), (120, False), (130, False), (131, True), (1000, True)):
            with self.subTest(now=now):
                decision = assess(scan, now, store)
                self.assert_historical_rejection(decision)
                self.assertEqual(decision['evaluated_at'], now)
                self.assertEqual('INVESTIGATION_NOT_FRESH_FOR_ENTRY' in decision['reasons'], stale)
                self.assertEqual(decision['entry_evidence']['continuation_evidence']['replay']['block_time'], 110)

    def test_http_dashboard_keeps_historical_component_and_reject_after_aging(self):
        with sqlite3.connect(self.source) as c:
            c.execute("DELETE FROM scans WHERE id!='scan-000'")
        result, _ = self.measure('1-fresh-journal', now=120)
        self.assertEqual(result['consumed'], 1)
        original = self.journal_rows()
        result, _ = self.measure('1-aged-unchanged', now=1000, limit=1)
        self.assertEqual(result['consumed'], 0)
        connection = MemoryHTTPConnection('/api/decisions')
        handler(SimpleNamespace(db=str(self.source)), 8765)(connection, ('127.0.0.1', 1), SimpleNamespace())
        headers, body = connection.output.getvalue().split(b'\r\n\r\n', 1)
        self.assertIn(b'200 OK', headers)
        self.assertIn(b'Cache-Control: no-store', headers)
        projected = json.loads(body)
        self.assertEqual(projected['status'], 'EVIDENCE_GATES_CONNECTED')
        self.assertFalse(projected['automatic_entry_enabled'])
        self.assertEqual(len(projected['decisions']), 1)
        decision = projected['decisions'][0]
        self.assert_historical_rejection(decision)
        self.assertEqual(decision['evaluated_at'], 120)
        # A historical row is not refreshed merely because it was replayed later.
        self.assertNotIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', decision['reasons'])
        self.assertEqual(self.journal_rows(), original)

    def test_raw_page_loss_is_rechecked_but_old_same_revision_remains_historical(self):
        with sqlite3.connect(self.source) as c:
            c.execute("DELETE FROM scans WHERE id!='scan-000'")
        self.measure('1-before-page-loss')
        before = self.journal_rows()
        store = EvidenceStore(self.evidence, read_only=True)
        progress = saved_progress(store, self.scans()[0])
        lost = progress['history_queries'][1]['pages'][1]['payload_hash']
        with sqlite3.connect(self.evidence) as c:
            c.execute('DELETE FROM pages WHERE hash=?', (lost,))
        recomputed = []
        def evaluate(*args, **kwargs):
            result = assess(*args, **kwargs)
            recomputed.append(result)
            return result
        with patch('desk.decision_runner.assess', new=evaluate):
            result, _ = self.measure('1-after-page-loss')
        self.assertEqual(result['consumed'], 0)
        self.assertEqual(len(recomputed), 1)
        self.assertEqual(recomputed[0]['entry_evidence']['gates']['history_snapshot']['status'], 'BLOCKED')
        self.assertEqual(recomputed[0]['decision'], 'REJECT')
        self.assertIn('CONTINUATION_RAW_EVIDENCE_UNVERIFIED', recomputed[0]['reasons'])
        self.assertEqual(self.journal_rows(), before)
        historical = recent_decisions(self.journal)['decisions'][0]
        self.assert_historical_rejection(historical)
        self.assertEqual(historical['evaluated_at'], 1000)


if __name__ == '__main__':
    unittest.main()
