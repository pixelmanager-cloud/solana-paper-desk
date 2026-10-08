"""Unmocked raw multi-account history; only fixture transport replaces RPC."""
import base64
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from desk.decode import decode
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.model import digest
from desk.ordering import collect_ordering
from desk.ownership_snapshot import replay_snapshot
from desk.ownership_worker import advance, saved_progress
from desk.replay_history import reconstruct_launch_history, replay_history


class OwnershipMultiHistoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.f = json.loads((Path(__file__).resolve().parents[1] /
                             'fixtures/cloud_history_integration/multi_account.json').read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'evidence.sqlite'
        self.store = EvidenceStore(self.path)
        self.records = self.f['records']
        self.calls = []

    def rows_for(self, address):
        if address == self.f['mint']:
            return self.records
        # Provider account histories overlap, as actual transfer queries do.
        return [r for r in self.records if any(
            r['transaction']['message']['accountKeys'][b['accountIndex']]['pubkey'] == address
            for side in ('preTokenBalances', 'postTokenBalances') for b in r['meta'][side])]

    def transport(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'getMultipleAccounts':
            self.assertEqual(params, self.f['snapshot']['params'])
            return copy.deepcopy(self.f['snapshot']['result'])
        if method == 'getBlockTime':
            self.assertEqual(params, self.f['block_time']['params'])
            return self.f['block_time']['result']
        self.assertEqual(method, 'getTransactionsForAddress')
        address, options = params
        self.assertEqual(options['commitment'], 'finalized')
        self.assertEqual(options['filters']['tokenAccounts'], 'none')
        if 'slot' in options['filters']:
            self.assertEqual(options['filters']['slot'], {'gte': 0, 'lt': 21})
            self.assertNotIn('blockTime', options['filters'])
        rows = self.rows_for(address)
        split = max(1, len(rows) // 2)
        if 'paginationToken' not in options:
            return {'data': copy.deepcopy(rows[:split]), 'paginationToken': 'second:' + address}
        self.assertEqual(options['paginationToken'], 'second:' + address)
        return {'data': copy.deepcopy(rows[split:])}

    def capture_report(self):
        queries = []
        for address in [self.f['mint']] + self.f['accounts']:
            _, coverage = collect_history(address, 90, 120, self.transport, max_pages=2,
                                          capture=self.store.save, token_accounts='none',
                                          slot_range={'gte': 0, 'lt': 21})
            self.assertEqual(len(coverage['pages']), 2)
            self.assertTrue(coverage['query_coverage_verified'])
            queries.append(coverage)
        return {'mint': self.f['mint'], 'history_queries': queries}

    def replay(self, report):
        # Reopen storage before reconstructing; no fixture summary is trusted.
        store = EvidenceStore(self.path, read_only=True)
        snapshot_hash = self.store.save(self.f['snapshot'])
        time_hash = self.store.save(self.f['block_time'])
        before = len(self.calls)
        history = reconstruct_launch_history(report, store)
        snapshot = replay_snapshot(report, store, snapshot_hash, time_hash)
        self.assertEqual(len(self.calls), before)
        return history, snapshot

    def assert_matching(self, history, snapshot, count):
        self.assertTrue(history['launch_verified'])
        self.assertTrue(history['inventory']['initialization_inventory_verified'])
        self.assertEqual(history['inventory']['account_count'], 3)
        self.assertEqual(history['account_queries']['verified'], 3)
        self.assertEqual(history['account_queries']['reasons'], [])
        self.assertEqual(history['observed_transaction_count'], count)
        self.assertTrue(history['observed_movements']['passed'])
        self.assertTrue(history['account_continuity']['passed'])
        self.assertTrue(snapshot['reconciled'], snapshot['reasons'])
        self.assertEqual(snapshot['observed_supply_raw'], '100')
        self.assertEqual(snapshot['supply_raw'], '100')
        self.assertFalse(snapshot['common_control_verified'])
        self.assertFalse(snapshot['eligible_for_trading'])
        self.assertFalse(history['transfer_history_complete'])

    def test_three_accounts_eight_pages_four_transactions_replay_exact_balances(self):
        report = self.capture_report()
        history, snapshot = self.replay(report)
        self.assert_matching(history, snapshot, 4)
        self.assertEqual(len(self.calls), 8)
        self.assertEqual({e['account']: e['amount_raw'] for e in history['account_continuity']['end_states']},
                         self.f['expected_balances'])
        self.assertEqual(snapshot['holder_totals_raw'],
                         {self.f['owners'][a]: n for a, n in self.f['expected_balances'].items()})

    def test_worker_continues_all_pages_across_restart_without_summary_patch(self):
        _, initial = collect_history(self.f['mint'], 90, 120, self.transport, max_pages=2,
                                     capture=self.store.save, token_accounts='none')
        mint_hash = self.store.save({'method': 'getAccountInfo',
                                    'params': [self.f['mint'], {'encoding': 'base64', 'commitment': 'confirmed'}],
                                    'result': {'value': self.f['snapshot']['result']['value'][0]}})
        report = {'mint': self.f['mint'], 'observed_at': 120, 'calls': 3,
                  'findings': [], 'unknowns': [], 'mint_evidence_hash': mint_hash,
                  'history_queries': [initial]}
        report['report_hash'] = digest(report)
        original = json.dumps(report)
        db = self.root / 'research.sqlite'
        with sqlite3.connect(db) as connection:
            connection.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            connection.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('multi', self.f['mint'], 120, 'COMPLETE', original))
        self.calls.clear()
        snapshots = []
        for _ in range(4):
            result = advance(db, self.path, 'multi', self.transport, max_calls=3)
            self.assertLessEqual(result['provider_calls'], 3)
            self.assertLessEqual(result['requests_used'], 18)
            if 'snapshot_evidence' in result:
                snapshots.append(result['snapshot_evidence']['snapshot_hash'])
        self.assert_matching(result['history'], result['snapshot'], 4)
        self.assertEqual(result['requests_used'], 13)
        self.assertEqual(len(self.calls), 10)  # Bank, clock, eight history pages.
        self.assertEqual(len(set(snapshots)), 1)
        self.assertEqual(sum(m == 'getMultipleAccounts' for m, _ in self.calls), 1)
        self.assertEqual(advance(db, self.path, 'multi', self.transport)['requests_used'], 13)
        self.assertEqual(len(self.calls), 10)
        with sqlite3.connect(db) as connection:
            connection.row_factory = sqlite3.Row
            scan = connection.execute('SELECT * FROM scans').fetchone()
            self.assertEqual(scan['result'], original)
        persisted = saved_progress(EvidenceStore(self.path, read_only=True), scan)
        self.assertEqual(persisted['snapshot'], result['snapshot'])

    def test_omitted_account_page_blocks_replay_despite_matching_mint_history(self):
        report = self.capture_report()
        page = report['history_queries'][1]['pages'][1]
        with self.store.connect() as connection:
            connection.execute('DELETE FROM pages WHERE hash=?', (page['payload_hash'],))
        history, snapshot = self.replay(report)
        self.assertTrue(history['launch_verified'])
        self.assertIn('ACCOUNT_HISTORY_RAW_REPLAY_FAILED', history['account_queries']['reasons'])
        self.assertFalse(snapshot['reconciled'])
        self.assertIn('ACCOUNT_QUERIES_INCOMPLETE', snapshot['reasons'])

    def test_conflicting_event_across_queries_is_quarantined(self):
        report = self.capture_report()
        address = self.f['accounts'][1]
        original_records = self.records
        self.records = copy.deepcopy(self.records)
        self.records[2]['meta']['preTokenBalances'][0]['uiTokenAmount']['amount'] = '41'
        _, replacement = collect_history(address, 90, 120, self.transport, max_pages=2,
                                         capture=self.store.save, token_accounts='none',
                                         slot_range={'gte': 0, 'lt': 21})
        self.records = original_records
        report['history_queries'][2] = replacement
        history, snapshot = self.replay(report)
        self.assertIn('ACCOUNT_HISTORY_CONFLICTING_TRANSACTION', history['account_queries']['reasons'])
        self.assertEqual(history['observed_transaction_count'], 3)
        self.assertFalse(snapshot['reconciled'])

    def test_duplicate_transfer_page_does_not_authorize_complete_account_query(self):
        self.records.append(copy.deepcopy(self.records[-1]))
        # Capture explicitly permits observing blocked coverage in this attack.
        queries = []
        for address in [self.f['mint']] + self.f['accounts']:
            _, q = collect_history(address, 90, 120, self.transport, max_pages=2,
                                   capture=self.store.save, token_accounts='none',
                                   slot_range={'gte': 0, 'lt': 21})
            queries.append(q)
        history, snapshot = self.replay({'mint': self.f['mint'], 'history_queries': queries})
        self.assertIn('HISTORY_DUPLICATE_RECORD', history['query_reasons'])
        self.assertFalse(snapshot['reconciled'])

    def test_close_recreate_with_witnesses_replays_new_lifetime(self):
        self.records += self.f['lifetime_records']
        self.f['snapshot'] = self.f['lifetime_snapshot']
        history, snapshot = self.replay(self.capture_report())
        self.assert_matching(history, snapshot, 8)
        self.assertEqual({e['account']: e['amount_raw'] for e in history['account_continuity']['end_states']},
                         dict(zip(self.f['accounts'], ['70', '25', '5'])))

    def test_recreate_without_initialization_witness_fails_closed(self):
        self.records += self.f['lifetime_records']
        self.records[-2]['transaction']['message']['instructions'] = []
        self.f['snapshot'] = self.f['lifetime_snapshot']
        history, snapshot = self.replay(self.capture_report())
        self.assertIn('HISTORY_ACCOUNT_REOPEN_UNWITNESSED', history['account_continuity']['reasons'])
        self.assertFalse(snapshot['reconciled'])

    def test_closed_account_absent_from_bank_remains_in_inventory(self):
        self.records += self.f['lifetime_records'][:2]
        self.f['snapshot'] = copy.deepcopy(self.f['lifetime_snapshot'])
        # Drain C to A: 75 + 25; the snapshot still requests C, returning null.
        value = self.f['snapshot']['result']['value'][1]
        data = bytearray(base64.b64decode(value['data'][0]))
        data[64:72] = (75).to_bytes(8, 'little')
        value['data'][0] = base64.b64encode(data).decode()
        self.f['snapshot']['result']['value'][3] = None
        history, snapshot = self.replay(self.capture_report())
        self.assert_matching(history, snapshot, 6)
        ending = next(e for e in history['account_continuity']['end_states']
                      if e['account'] == self.f['accounts'][2])
        self.assertTrue(ending['closed'])
        self.assertIsNone(ending['amount_raw'])

    def test_close_without_closure_witness_cannot_become_missing_account(self):
        self.records += self.f['lifetime_records']
        self.records[-3]['transaction']['message']['instructions'] = []
        self.f['snapshot'] = self.f['lifetime_snapshot']
        history, snapshot = self.replay(self.capture_report())
        self.assertFalse(history['observed_movements']['passed'])
        self.assertFalse(history['account_continuity']['passed'])
        self.assertFalse(snapshot['reconciled'])

    def test_changed_owner_between_transactions_is_not_valid_continuity(self):
        account = self.f['accounts'][0]
        for side in ('preTokenBalances', 'postTokenBalances'):
            for row in self.records[-1]['meta'][side]:
                if row['accountIndex'] == 1:
                    row['owner'] = self.f['owners'][self.f['accounts'][1]]
        history, snapshot = self.replay(self.capture_report())
        self.assertTrue(history['observed_movements']['passed'])
        self.assertIn('HISTORY_ACCOUNT_CONTROL_CHANGED', history['account_continuity']['reasons'])
        self.assertFalse(snapshot['reconciled'])
        self.assertEqual(history['inventory']['accounts'][0]['address'], account)

    def test_same_slot_transfer_without_persisted_block_order_is_unknown(self):
        self.records[-1]['slot'] = self.records[-2]['slot']
        self.records[-1]['blockTime'] = self.records[-2]['blockTime']
        history, snapshot = self.replay(self.capture_report())
        self.assertIn('BLOCK_ORDERING_UNVERIFIED', history['block_ordering']['reasons'])
        self.assertIn('HISTORY_SAME_SLOT_ORDER_UNKNOWN', history['account_continuity']['reasons'])
        self.assertFalse(snapshot['reconciled'])

    def test_persisted_raw_block_order_resolves_same_slot_without_lexical_sort(self):
        self.records[-1]['slot'] = 12
        self.records[-1]['blockTime'] = 102
        # Enumerate provider rows in the opposite within-slot order. Accounting
        # must use the saved finalized block rather than that page order.
        self.records[-2:] = list(reversed(self.records[-2:]))
        report = self.capture_report()
        from desk.security import base58
        block = {'blockhash': base58(bytes([30]) * 32),
                 'previousBlockhash': base58(bytes([29]) * 32),
                 'parentSlot': 11, 'blockTime': 102,
                 'signatures': [self.records[-1]['signature'], self.records[-2]['signature']]}
        observations, _ = replay_history(report['history_queries'][0], self.store)
        def block_transport(method, params):
            self.assertEqual(method, 'getBlock')
            self.assertEqual(params, [12, {'commitment': 'finalized', 'transactionDetails': 'signatures',
                                         'rewards': False, 'maxSupportedTransactionVersion': 1}])
            return copy.deepcopy(block)
        ordering = collect_ordering(self.f['mint'], observations, block_transport,
                                    capture=self.store.save)
        self.assertTrue(ordering['verified'])
        report['account_history'] = {'block_ordering': ordering}
        history, snapshot = self.replay(report)
        self.assert_matching(history, snapshot, 4)
        self.assertEqual(history['block_ordering']['required_slots'], [12])
        self.assertEqual(history['block_ordering']['proofs'][0]['positions'],
                         {signature: i for i, signature in enumerate(block['signatures'])})

    def test_unsupported_authority_normalization_blocks_history(self):
        # Inventory the unsupported operation without claiming lifecycle support.
        raw = copy.deepcopy(self.records[-1])
        raw['transaction']['message']['instructions'].append({
            'programId': raw['transaction']['message']['instructions'][0]['programId'],
            'parsed': {'type': 'setAuthority', 'info': {
                'account': self.f['accounts'][0], 'authorityType': 'CloseAccount',
                'authority': self.f['owners'][self.f['accounts'][0]],
                'newAuthority': self.f['owners'][self.f['accounts'][1]]}}})
        observation = decode(raw)
        self.assertIn('UNDECODED_TOKEN_INSTRUCTION', observation['limitations'])
        self.assertIn('UNSUPPORTED_TOKEN_CONTROL_OPERATION', observation['limitations'])
        self.assertEqual(observation['token_control_operations'][0]['type'], 'setAuthority')
        self.assertNotIn('token_authority_changes', observation)
        # Transfer witnesses survive, but a matching final bank cannot approve
        # ignored authority lifecycle changes.
        self.assertEqual(observation['transfers'], decode(self.records[-1])['transfers'])

        self.records[-1] = raw
        history, snapshot = self.replay(self.capture_report())
        self.assertFalse(history['inventory']['initialization_inventory_verified'])
        self.assertFalse(history['observed_movements']['passed'])
        self.assertFalse(history['account_continuity']['passed'])
        self.assertFalse(snapshot['reconciled'])
