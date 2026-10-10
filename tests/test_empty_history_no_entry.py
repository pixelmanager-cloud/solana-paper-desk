"""Synthetic original bytes; real history/receipt/gate validators, no HTTP."""
import copy
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from desk import history_preparation_rejection as rejection,paper_terminal_reconciliation as terminal
from desk import paper_empty_history_reconciliation as recovery
from tools import history_first_paper_entry as entry


class ProspectiveEmptyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_history_preparation_phase import PreparationTests
        self.p=PreparationTests();self.p.setUp();self.addCleanup(self.p.doCleanups)

    def test_verified_empty_window_publishes_no_entry_and_allows_next_scan(self):
        p=self.p
        with self.assertRaisesRegex(entry.PreparationRejected,'HISTORY_FEATURE_EMPTY_WINDOW'):
            p.advance({'data':[]})
        before=p.progress.admission(p.target.scan_id)
        result=p.publish('HISTORY_FEATURE_EMPTY_WINDOW')
        self.assertEqual(result['attempt_refs'].__len__(),1)
        self.assertEqual(before['requests_used'],7)
        self.assertEqual(result['status'],'NO_ENTRY');self.assertFalse(result['entry_authorized'])
        self.assertEqual(rejection.verify(p.store,p.progress,result)['kind'],'history_first_paper_preparation_v4')
        self.assertEqual(p.publish('HISTORY_FEATURE_EMPTY_WINDOW'),result)
        self.assertEqual(terminal.gate(p.store,p.context['research_db'],(p.target.scan_id,)),'REJECTED_SCAN_RETIRED')
        self.assertIsNone(terminal.gate(p.store,p.context['research_db'],('b'*32,)))
        self.assertEqual(p.progress.admission(p.target.scan_id),before)

    def test_incomplete_empty_and_nonempty_windows_do_not_claim_no_entry(self):
        p=self.p
        p.advance({'data':[],'paginationToken':'next'})
        with self.assertRaises(ValueError):p.publish('HISTORY_FEATURE_EMPTY_WINDOW')
        self.assertIsNone(p.marker())

    def test_missing_captured_attempt_and_changed_ledger_stay_blocked(self):
        p=self.p
        with self.assertRaises(entry.PreparationRejected):p.advance({'data':[]})
        state=p.progress.snapshot(p.budget.history_id)
        keys=rejection._attempts(p.store,p.target.scan_id,6,7)
        with p.store.connect() as c:c.execute('DELETE FROM pages WHERE hash=?',(keys[0][0],))
        with self.assertRaises(ValueError):p.publish('HISTORY_FEATURE_EMPTY_WINDOW')
        self.assertIsNone(p.marker())

    def test_empty_receipt_requires_explicit_policy_and_strict_shapes(self):
        for malformed in ({},None,[],{'association':recovery.ASSOCIATION}):
            self.assertFalse(recovery.shape(malformed))
            with self.assertRaises(ValueError):recovery.approved(malformed)

    def test_replay_proven_missing_measurement_matrix(self):
        from tests.test_live_strategy_features import transaction
        from desk.security import base58
        for mode in ('sell_only','latest_slot_tie','stale_trade'):
            with self.subTest(mode=mode):
                from tests.test_history_preparation_phase import PreparationTests
                p=PreparationTests();p.setUp()
                try:
                    rows=[transaction(base58((i+1).to_bytes(64,'big')),
                          100 if mode=='latest_slot_tie' else 100+i,
                          p.as_of-60 if mode=='stale_trade' else p.as_of-1,
                          side='sell' if mode=='sell_only' else 'buy',
                          wallet=base58(bytes([i+1])*32)) for i in range(40)]
                    with self.assertRaisesRegex(entry.PreparationRejected,'HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE'):
                        p.advance({'data':rows})
                    before=p.progress.admission(p.target.scan_id)
                    result=p.publish('HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE')
                    self.assertTrue(result['bounds']['missing_measurements'])
                    rejection.verify(p.store,p.progress,result)
                    self.assertIsNone(terminal.gate(p.store,p.context['research_db'],('b'*32,)))
                    self.assertEqual(p.progress.admission(p.target.scan_id),before)
                finally:p.doCleanups()
