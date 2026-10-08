"""Offline boundary tests, not QA-1 acceptance or forward-observation evidence.

All data originates from tests.helpers or inline synthetic responses; each test
owns temporary SQLite files. Network connections are forbidden in this module.
"""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.decision_runner import consume
from desk.engine import initial_state, transition
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.history_progress import HistoryProgress
from desk.ledger import Ledger
from desk.model import D, digest
from desk.monitor import tick
from desk.replay_history import replay_history
from desk.security import base58
from desk.storage import snapshot, verify
from tests.helpers import T, config, control, event


class CloudReadinessInvariants(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cfg = config()
        self.path = self.root / 'paper.sqlite'
        self.ledger = Ledger(self.path)
        self.addCleanup(lambda: self.ledger.close())
        guard = patch('socket.socket.connect', side_effect=AssertionError('Offline fixtures only'))
        guard.start()
        self.addCleanup(guard.stop)

    def apply(self, observation):
        return self.ledger.apply(observation, self.cfg, transition, initial_state)

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.path)

    def test_raw_conflict_and_real_data_assertions_cannot_create_entry(self):
        raw = {'signature': 'synthetic-signature', 'slot': 1}
        self.assertTrue(self.ledger.record_raw('synthetic-source', T, 1, raw))
        with self.assertRaises(ValueError):
            self.ledger.record_raw('synthetic-source', T + 1, 1, {**raw, 'slot': 2})
        outcome = self.apply(event(provenance='MAINNET_OBSERVATION'))
        self.assertIn('LIVE_FEATURE_ADAPTER_NOT_READY', outcome[0]['reasons'])
        report = self.ledger.report()
        self.assertEqual(report['raw_event_count'], 1)
        self.assertEqual(json.loads(self.ledger.db.execute('SELECT payload FROM raw_events').fetchone()[0]), raw)
        self.assertEqual(report['state']['positions'], {})
        self.assertFalse(any(o['type'] == 'fill' for o in report['outcomes']))

    def test_rehashed_history_assertions_cannot_rebind_saved_request(self):
        store = EvidenceStore(self.root / 'evidence.sqlite')
        mint = base58(bytes([7]) * 32)
        _, coverage = collect_history(mint, 10, 20, lambda *args: {'data': []},
                                     max_pages=1, capture=store.save, token_accounts='none')
        self.assertEqual(replay_history(coverage, store)[1], coverage)
        original = copy.deepcopy(coverage)
        coverage['start'] = 9
        coverage.pop('evidence_hash')
        coverage['evidence_hash'] = digest(coverage)
        with self.assertRaises(ValueError):
            replay_history(coverage, store)
        self.assertEqual(replay_history(original, store)[1], original)

    def test_interrupted_attempt_survives_restore_and_exhausts_shared_budget(self):
        evidence = self.root / 'evidence.sqlite'
        progress = HistoryProgress(EvidenceStore(evidence))
        identity = digest({'synthetic_scan': 1})
        progress.budget('scan', identity, 17)
        first = progress.create('scan', base58(bytes([7]) * 32), 10, 20)
        second = progress.create('scan', base58(bytes([8]) * 32), 10, 20)
        def interrupt(*args):
            raise KeyboardInterrupt('synthetic interruption after reservation')
        with self.assertRaises(KeyboardInterrupt):
            progress.advance(first, interrupt)
        restored = self.root / 'restored-evidence.sqlite'
        manifest = snapshot(evidence, restored)
        self.assertTrue(verify(restored, manifest['sha256']))
        resumed = HistoryProgress(EvidenceStore(restored))
        resumed.budget('scan', identity, 0)
        result = resumed.advance(second, lambda *args: self.fail('Nineteenth request attempted'))
        self.assertEqual(result['requests_used'], 18)
        self.assertEqual(result['blocked'], 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
        self.assertIsNone(resumed.snapshot(first)['coverage'])
        self.assertEqual(resumed.snapshot(first)['attempts'], 1)

    def test_reject_journal_restore_preserves_original_source_and_no_ledger(self):
        source = self.root / 'research.sqlite'
        journal = self.root / 'decisions.sqlite'
        forged = {'mint': 'synthetic-mint', 'observed_at': T, 'eligible_for_trading': True,
                  'findings': [], 'unknowns': []}
        with sqlite3.connect(source) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', ('scan', forged['mint'], T, 'COMPLETE', json.dumps(forged)))
        result = consume(source, journal, now=T)
        self.assertFalse(result['automatic_entry_enabled'])
        self.assertEqual(result['decisions'][0]['decision'], 'REJECT')
        with sqlite3.connect(journal) as c:
            before = c.execute('SELECT * FROM decisions').fetchall()
        restored = self.root / 'restored-decisions.sqlite'
        snapshot(journal, restored)
        self.assertEqual(consume(source, restored, now=T + 100)['consumed'], 0)
        with sqlite3.connect(restored) as c:
            self.assertEqual(c.execute('SELECT * FROM decisions').fetchall(), before)
        with sqlite3.connect(source) as c:
            self.assertEqual(json.loads(c.execute('SELECT result FROM scans').fetchone()[0]), forged)
        self.assertIsNone(self.ledger.report()['state'])
        self.assertEqual(self.ledger.report()['outcomes'], [])

    def test_failure_after_transition_before_checkpoint_rolls_back_every_write(self):
        self.ledger.db.execute("CREATE TRIGGER synthetic_abort BEFORE INSERT ON outcomes BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
        before = self.ledger.report()
        with self.assertRaises(sqlite3.IntegrityError):
            self.apply(event())
        self.assertEqual(self.ledger.report(), before)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)
        self.assertEqual(self.ledger.db.execute('SELECT COUNT(*) FROM metadata').fetchone()[0], 0)
        self.ledger.db.execute('DROP TRIGGER synthetic_abort')
        self.reopen()
        self.assertEqual(self.apply(event())[0]['side'], 'buy')
        committed = self.ledger.report()
        self.assertEqual(self.apply(event()), [])
        self.assertEqual(self.ledger.report(), committed)

    def test_outage_restore_and_resume_cannot_hide_unverified_inventory(self):
        self.apply(event())
        before = self.ledger.report()['state']
        self.assertEqual(tick(self.path, self.cfg, now=T + 11)['status'], 'MARKS_EXPIRED')
        restored = self.root / 'restored-paper.sqlite'
        manifest = snapshot(self.path, restored)
        self.assertTrue(verify(restored, manifest['sha256']))
        self.ledger.close()
        self.path = restored
        self.ledger = Ledger(restored)
        self.apply(control(T + 12, 'RESUME'))
        self.apply(event(T + 13, danger=True, provenance='MAINNET_OBSERVATION'))
        after = self.ledger.report()['state']
        self.assertEqual(after['mode'], 'EXIT_ONLY')
        for field in ('cash', 'realized_pnl'):
            self.assertEqual(after[field], before[field])
        for field in ('qty', 'cost_left', 'mark_at', 'mark_value'):
            self.assertEqual(after['positions']['SYNTHETIC_A'][field], before['positions']['SYNTHETIC_A'][field])
        self.assertFalse(any(o.get('side') == 'sell' for o in self.ledger.report()['outcomes']))
        self.apply(event(T + 14))
        self.assertEqual(self.ledger.report()['state']['mode'], 'EXIT_ONLY')

    def test_partial_exit_restart_final_exit_conserves_costs_and_net_pnl(self):
        self.apply(event())
        self.apply(event(T + 5, reserve_sol='160'))
        partial = self.ledger.report()
        self.assertGreater(D(partial['state']['positions']['SYNTHETIC_A']['cost_left']), 0)
        self.reopen()
        self.assertEqual(self.apply(event(T + 5, reserve_sol='160')), [])
        self.assertEqual(self.ledger.report(), partial)
        self.apply(event(T + 6, reserve_sol='160', danger=True))
        report = self.ledger.report()
        fills = [o for o in report['outcomes'] if o['type'] == 'fill']
        buy = fills[0]
        sells = fills[1:]
        self.assertEqual([o['side'] for o in fills], ['buy', 'sell', 'sell'])
        self.assertEqual(report['state']['positions'], {})
        self.assertAlmostEqual(sum(D(o['quantity']) for o in sells), D(buy['quantity']))
        cost = D(buy['amount_sol']) + D(buy['fee_sol'])
        net = sum(D(o['proceeds_sol']) for o in sells) - cost
        self.assertAlmostEqual(D(report['state']['realized_pnl']), net)
        self.assertAlmostEqual(D(report['state']['cash']) - D(self.cfg['initial_equity_sol']), net)
        self.assertTrue(all(D(o['fee_sol']) > 0 for o in fills))


if __name__ == '__main__':
    unittest.main()
