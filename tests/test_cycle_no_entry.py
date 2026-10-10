"""Generic terminal NO_ENTRY for normal cycle rejections (T01).

Fixtures only: synthetic Helius/Jupiter/Kraken bytes from the retained-empty
fixture (see tests/test_empty_history_no_entry.py); no provider credentials.
"""
import contextlib
import copy
import sqlite3
import time
from tests import legacy_null_pass
import unittest
from contextlib import closing
from dataclasses import replace
from unittest.mock import patch

from desk import paper_cycle as cycle, paper_cycle_no_entry as no_entry, paper_terminal_reconciliation as terminal
from desk import paper_read_sources as transport
from desk.model import digest
from tests.test_empty_history_no_entry import RetainedEmptyTests


def fixture(legacy=False):
    """Empty-window MARKET_PRODUCER_BLOCKED pass on a fresh store set."""
    t = RetainedEmptyTests('test_missing_capture_stays_pending')
    if not legacy:
        t.legacy_null_pass = contextlib.nullcontext
    t.setUp()
    return t


class CycleNoEntryTests(unittest.TestCase):
    def setUp(self):
        legacy_null_pass.install(self)    # T22: these tests certify retained pre-T22 NULL/unresolved states
        self.t = fixture(); self.addCleanup(self.t.doCleanups)
        t = self.t
        self.store, self.progress, self.scan = t.store, t.progress, t.h.f.target.scan_id
        self.research = t.ctx['research_db']

    def pass_row(self, store=None):
        with closing((store or self.store).connect()) as c:
            return c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes WHERE outcome_hash IS NOT NULL ORDER BY rowid DESC').fetchall()

    def cycle_pass(self):
        rows = [r for r in self.pass_row() if terminal._load(self.store, r[2]).get('kind') == no_entry.KIND]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_fresh_store_rejection_is_terminal_and_next_candidate_enters(self):
        t = self.t; f = t.h.f
        self.assertEqual(t.result['blockers'], ['MARKET_PRODUCER_BLOCKED'])
        identity, intent, outcome = self.cycle_pass()
        rec = terminal._load(self.store, outcome)
        self.assertEqual((rec['status'], rec['entry_authorized'], rec['execution_status']), ('NO_ENTRY', False, 'EXECUTION_UNVERIFIED'))
        self.assertEqual(rec['charged'], 5)
        self.assertEqual(rec['result_hash'], t.result['evidence_hash'])
        self.assertEqual(terminal.gate(self.store, self.research, ()), None)
        self.assertEqual(terminal.gate(self.store, self.research, (self.scan,)), 'REJECTED_SCAN_RETIRED')
        with closing(self.store.connect()) as c:  # no reconciliation receipt of any kind
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({n for n in names if n.startswith('paper_terminal_')}, set())
        # Charges stay charged and the retired scan is not retried.
        self.assertEqual(self.progress.admission(self.scan)['requests_used'], rec['admission_after']['requests_used'])
        # Next candidate on the same store set proceeds to a simulated BUY.
        retired = self.progress.admission(self.scan)
        self.next_candidate_buys()
        self.assertEqual(self.progress.admission(self.scan), retired)
        self.assertIsNone(terminal.gate(self.store, self.research, ()))

    def next_candidate_buys(self):
        from desk.programs import unbase58
        from desk.security import base58
        from desk.ownership_acquisition import _Setup
        t = self.t; f = t.h.f
        f.f.at = int(time.time()); f.target = f.f.target()
        _Setup(self.store, f.f.jobs.descriptor(f.target.scan_id), self.progress.admission(f.target.scan_id))
        t.empty_history = False
        manifest = self.store.load(f.item.graduation_refs[0]); response = copy.deepcopy(self.store.load(manifest['response_hash']))
        raw = response['data'][0]; raw['blockTime'] = f.f.at-600
        ix = raw['meta']['innerInstructions'][0]['instructions'][0]
        data = bytearray(unbase58(ix['data'])); data[136:144] = raw['blockTime'].to_bytes(8, 'little', signed=True); ix['data'] = base58(data)
        key = self.store.save(response); ref = self.store.save({**manifest, 'response_hash': key})
        item = replace(f.item, target=f.target, provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',
                       graduated_at=f.f.at-600, graduation_refs=(ref,), history_as_of=None)
        with patch.dict('os.environ', {'HELIUS_API_KEY': 'SYNTHETIC_TEST_ONLY', 'JUPITER_API_KEY': 'SYNTHETIC_TEST_ONLY'}), \
                patch.object(transport, 'build_opener', return_value=t.http):
            result = cycle.run_once(f.f.jobs.path, self.store.path, f.path, f.cfg, candidates=(item,), dependency_blockers=())
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertTrue(any(x.get('side') == 'buy' for x in result['outcomes']), result)

    # ---- adversarial: every tamper must make the gate fail closed -------------------
    def drop_guards(self, table):
        with closing(self.store.connect()) as c:
            for name, in list(c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))):
                c.execute(f'DROP TRIGGER {name}')

    def tamper(self, *statements):
        """Apply raw SQL as a hostile writer; guards are recreated so only content is judged."""
        self.drop_guards(no_entry.TABLE)
        with closing(self.store.connect()) as c:
            for sql, args in statements:
                c.execute(sql, args)
            for sql in no_entry.GUARDS.values():
                c.execute(sql)

    def assert_latched(self):
        with self.assertRaises(ValueError):
            terminal.gate(self.store, self.research, ())

    def test_forged_receipt_row_for_other_pass(self):
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('f'*32, 'e'*64))
            c.execute(f'INSERT INTO {no_entry.TABLE} VALUES(?,?,?,?)', ('f'*32, 'forged-scan', 'e'*64, 'd'*64))
        self.assert_latched()

    def test_mismatched_intent_hash_in_receipt(self):
        self.tamper((f'UPDATE {no_entry.TABLE} SET intent_hash=?', ('c'*64,)))
        self.assert_latched()

    def test_pass_intent_changed_after_publication(self):
        identity, intent, outcome = self.cycle_pass()
        other = self.store.save({'kind': 'paper_cycle_intent_v1', 'other': True})
        with closing(self.store.connect()) as c:
            c.execute('UPDATE paper_observation_passes SET intent_hash=? WHERE id=?', (other, identity))
        self.assert_latched()

    def test_category_b_blocker_cannot_be_presented_as_normal(self):
        legacy = fixture(legacy=True); self.addCleanup(legacy.doCleanups)
        store = legacy.store
        with closing(store.connect()) as c:
            pending = c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
        ledger = no_entry.ledger_snapshot(legacy.ctx['ledger_db'])
        for code in ('USD_ORIGINAL_BINDING_INVALID', 'SOURCE_REQUEST_FAILED', 'SOURCE_CONTENT_REJECTED',
                     'HISTORY_RECOVERY_REQUIRED', 'LEDGER_INTEGRITY_FAILURE', 'OBSERVATIONS_INCOMPLETE'):
            self.assertNotIn(code, no_entry.NORMAL)
            forged = {**legacy.result, 'blockers': [code]}; forged.pop('evidence_hash')
            with self.assertRaises(ValueError):
                no_entry.publish(store, legacy.progress, pass_id=pending[0], intent_hash=pending[1], result=forged, ledger=ledger)
        # A normal code whose necessary charged-original conditions do not hold is refused.
        for code in ('CYCLE_REQUEST_BUDGET_EXHAUSTED', 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 'FEATURE_HISTORY_PAGE_LIMIT'):
            forged = {k: v for k, v in legacy.result.items() if k != 'evidence_hash'}
            forged['blockers'] = [code]
            with self.subTest(code=code), self.assertRaises(ValueError):
                no_entry.publish(store, legacy.progress, pass_id=pending[0], intent_hash=pending[1], result=forged, ledger=ledger)
        with closing(store.connect()) as c:
            self.assertIsNone(c.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?', (pending[0],)).fetchone()[0])
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (no_entry.TABLE,)).fetchone())
        self.assertEqual(terminal.gate(store, legacy.ctx['research_db'], ()), 'OBSERVATION_RECOVERY_REQUIRED')

    def test_duplicate_receipt_and_replay_are_refused(self):
        identity, intent, outcome = self.cycle_pass()
        with closing(self.store.connect()) as c:
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute(f'INSERT INTO {no_entry.TABLE} VALUES(?,?,?,?)', (identity, 'another-scan', intent, outcome))
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute(f'INSERT INTO {no_entry.TABLE} VALUES(?,?,?,?)', ('9'*32, self.scan, intent, outcome))
        rec = terminal._load(self.store, outcome)
        with self.assertRaises(ValueError):
            no_entry.publish(self.store, self.progress, pass_id=identity, intent_hash=intent, result={k: v for k, v in self.t.result.items() if k != 'evidence_hash'}, ledger=no_entry.ledger_snapshot(self.t.ctx['ledger_db']))
        self.assertEqual(self.cycle_pass(), (identity, intent, outcome))
        self.assertEqual(rec['pass_id'], identity)

    def test_receipt_rows_are_immutable(self):
        for sql in (f'UPDATE {no_entry.TABLE} SET scan_id=scan_id', f'DELETE FROM {no_entry.TABLE}'):
            with closing(self.store.connect()) as c, self.assertRaises(sqlite3.DatabaseError):
                c.execute(sql)

    def test_deleted_or_updated_original_rows_latch(self):
        identity, intent, outcome = self.cycle_pass()
        with closing(self.store.connect()) as c:
            c.execute('UPDATE paper_observation_passes SET outcome_hash=NULL WHERE id=?', (identity,))
        self.assert_latched()
        with closing(self.store.connect()) as c:
            c.execute('DELETE FROM paper_observation_passes WHERE id=?', (identity,))
        self.assert_latched()

    def test_dropped_receipt_table_cannot_erase_the_retirement(self):
        self.drop_guards(no_entry.TABLE)
        with closing(self.store.connect()) as c:
            c.execute(f'DROP TABLE {no_entry.TABLE}')
        self.assert_latched()

    def test_missing_attempt_original_and_changed_charge_latch(self):
        identity, intent, outcome = self.cycle_pass()
        key = terminal._load(self.store, outcome)['attempt_refs'][0]
        with closing(self.store.connect()) as c:
            c.execute('DELETE FROM pages WHERE hash=?', (key,))
        self.assert_latched()

    def test_extra_charge_after_publication_latches(self):
        self.assertTrue(self.progress.reserve(self.scan))
        self.assert_latched()

    def test_ledger_prefix_rewrite_latches_but_new_events_do_not(self):
        from desk import engine
        from desk.ledger import Ledger
        ledger_db = self.t.ctx['ledger_db']
        with closing(Ledger(ledger_db, must_exist=True)) as ledger:
            ledger.apply({**cycle.INIT, 'event_id': 'later-valid-clock', 'ts': int(time.time())},
                         self.t.h.f.cfg, engine.transition, engine.initial_state)
        self.assertIsNone(terminal.gate(self.store, self.research, ()))
        with closing(sqlite3.connect(ledger_db)) as c:
            for name, in list(c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='events'")):
                c.execute(f'DROP TRIGGER {name}')
            c.execute("UPDATE events SET payload=payload||' ' WHERE seq=1"); c.commit()
        self.assert_latched()

    def test_tampered_receipt_content_latches(self):
        identity, intent, outcome = self.cycle_pass()
        rec = terminal._load(self.store, outcome)
        for change in ({'blocker': 'CYCLE_REQUEST_BUDGET_EXHAUSTED'}, {'entry_authorized': True}, {'charged': 4},
                       {'attempt_refs': rec['attempt_refs'][:-1]}, {'result_hash': '0'*64}, {'mode': 'position'}):
            forged = self.store.save({**rec, **change})
            self.tamper((f'UPDATE {no_entry.TABLE} SET outcome_hash=?', (forged,)),
                        ('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=?', (forged, identity)))
            with self.subTest(change=change):
                self.assert_latched()
            self.tamper((f'UPDATE {no_entry.TABLE} SET outcome_hash=?', (outcome,)),
                        ('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=?', (outcome, identity)))
            self.assertIsNone(terminal.gate(self.store, self.research, ()))

    def test_code_consistency_proofs(self):
        history = [{'method': 'getTransactionsForAddress'}]*8
        no_entry._consistent('FEATURE_HISTORY_PAGE_LIMIT', 0, 8, history)
        no_entry._consistent('CYCLE_REQUEST_BUDGET_EXHAUSTED', 0, 18, [])
        no_entry._consistent('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 4, 18, [])
        no_entry._consistent('MARKET_PRODUCER_BLOCKED', 0, 5, [])
        for args in (('FEATURE_HISTORY_PAGE_LIMIT', 0, 8, history[:7]), ('CYCLE_REQUEST_BUDGET_EXHAUSTED', 0, 17, []),
                     ('CYCLE_REQUEST_BUDGET_EXHAUSTED', 1, 18, []), ('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED', 0, 17, [])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                no_entry._consistent(*args)

    def test_allow_list_is_only_deterministic_outcomes(self):
        integrity = {'USD_ORIGINAL_BINDING_INVALID', 'PRICE_ORIGINAL_BINDING_INVALID', 'KRAKEN_ORIGINAL_BINDING_INVALID',
                     'SAVED_CONFIG_MISMATCH', 'SAVED_IMPLEMENTATION_MISMATCH', 'SHARED_BUDGET_CHARGE_OR_IDENTITY_MISMATCH',
                     'GRADUATION_SOURCE_BINDING_INVALID', 'GRADUATION_TIMESTAMP_CONFLICT', 'SOURCE_RESPONSE_STALE',
                     'SOURCE_REQUEST_FAILED', 'SOURCE_CONTENT_REJECTED', 'HISTORY_RECOVERY_REQUIRED', 'WALL_CLOCK_UNAVAILABLE',
                     'CYCLE_DEADLINE_UNAVAILABLE', 'MINT_POOL_BANK_MISMATCH', 'MINT_POOL_TOKEN_PROGRAM_MISMATCH',
                     'UNRESOLVED_QUOTE_DEMAND', 'QUOTE_DEMAND_INVALID', 'OBSERVATIONS_INCOMPLETE', 'UNRESOLVED_POSITION_EXIT',
                     'LEDGER_INTEGRITY_FAILURE', 'DEADLINE_EXCEEDED', 'RPC_ERROR', 'HELD_OBSERVATION_CONTENT_REJECTED',
                     'EVENT_READER_SIZE_LIMIT', 'QUOTE_DEMAND_LIMIT'}
        self.assertFalse(integrity & no_entry.NORMAL)
        import inspect
        source = inspect.getsource(cycle)
        for code in no_entry.NORMAL:
            self.assertIn(f"'{code}'", source)


if __name__ == '__main__':
    unittest.main()
