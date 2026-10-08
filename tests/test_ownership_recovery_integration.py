"""Linux process-death recovery across acquisition, bank and shared budget.

Only I/O replies and explicit process-death hooks are fixtures. Production
admission, fenced Jobs recovery, history replay, source sealing and bank capture
run together against real SQLite files. No live RPC or capture/pool journal.
"""
import copy
import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import JobPersistence, BIRTH_ACQUISITION_V1
from desk.model import digest
from desk.ownership_acquisition import acquire, _Setup
from desk.ownership_worker import advance, saved_progress
from tests.test_ownership_integration import synthetic_launch


def fixture_rpc(evidence, ledger, uid, *, crash_clock=False, account_success=False):
    mint, _, account, raw, values = synthetic_launch()

    def rpc(method, params):
        prior = read_ledger(ledger)
        admission = HistoryProgress(EvidenceStore(evidence)).admission(uid)
        # Check the real committed reservation from another SQLite connection.
        assert admission['requests_used'] == len(prior) + 1
        with open(ledger, 'a') as f:
            f.write(json.dumps({'method': method, 'params': params,
                                'reserved': admission['requests_used']}) + '\n')
            f.flush()
            os.fsync(f.fileno())
        if method == 'getAccountInfo':
            return {'value': values[0]}
        if method == 'getSlot':
            # A later getSlot would refresh cutoff and must never occur.
            return 20 if not any(p['method'] == method for p in prior) else 900
        if method == 'getMultipleAccounts':
            assert params == [[mint, account], {'encoding': 'base64', 'commitment': 'finalized'}]
            # Likewise the second bank request would return a different bank.
            return {'context': {'slot': 20 if not any(p['method'] == method for p in prior) else 901},
                    'value': values}
        if method == 'getBlockTime':
            assert params == [20]
            if crash_clock:
                os._exit(73)
            return 105
        assert method == 'getTransactionsForAddress'
        assert params[1]['filters']['slot'] == {'gte': 0, 'lt': 21}
        assert 'blockTime' not in params[1]['filters']
        if params[0] == account:
            if not account_success:
                raise OSError('synthetic account-history outage')
            return {'data': [copy.deepcopy(raw)]}
        assert params[0] == mint
        if params[1].get('paginationToken') == 'next':
            return {'data': []}
        return {'data': [copy.deepcopy(raw)], 'paginationToken': 'next'}
    return rpc


def read_ledger(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


def acquisition_death(research, evidence, ledger, uid, boundary):
    rpc = fixture_rpc(evidence, ledger, uid)
    if boundary == 'cutoff':
        original = _Setup.save
        def save(self, name, record):
            original(self, name, record)
            if name == 'cutoff_hash':
                os._exit(71)
        with patch.object(_Setup, 'save', save):
            acquire(research, evidence, rpc, scan_id=uid)
    else:
        original = HistoryProgress.advance
        def checkpoint(self, key, provider):
            original(self, key, provider)
            os._exit(72)
        with patch.object(HistoryProgress, 'advance', checkpoint):
            acquire(research, evidence, rpc, scan_id=uid)
    raise AssertionError('Process-death boundary not reached')


def bank_clock_death(research, evidence, ledger, uid):
    advance(research, evidence, uid, fixture_rpc(evidence, ledger, uid, crash_clock=True), max_calls=4)
    raise AssertionError('Bank-time process-death boundary not reached')


class OwnershipRecoveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.research = self.root / 'research.sqlite'
        self.evidence = self.root / 'evidence.sqlite'
        self.ledger = self.root / 'fixture-attempts.jsonl'
        self.mint = synthetic_launch()[0]
        with patch('desk.job_persistence.time.time', return_value=100000):
            self.uid = JobPersistence(self.research).admit(
                self.mint, kind=BIRTH_ACQUISITION_V1, evidence_db=self.evidence)

    def progress(self):
        return HistoryProgress(EvidenceStore(self.evidence))

    def die(self, target, exitcode, *extra):
        child = multiprocessing.get_context('fork').Process(
            target=target, args=(self.research, self.evidence, self.ledger, self.uid, *extra))
        child.start()
        try:
            child.join(10)
            self.assertFalse(child.is_alive(), 'Fixture child did not exit')
            self.assertEqual(child.exitcode, exitcode)
        finally:
            if child.is_alive():
                child.terminate()
                child.join(5)

    def preserved(self):
        with EvidenceStore(self.evidence).connect() as c:
            return (c.execute('SELECT * FROM ownership_acquisition_setup').fetchall(),
                    c.execute('SELECT * FROM ownership_acquisition_history').fetchall(),
                    c.execute('SELECT * FROM ownership_banks').fetchall(),
                    JobPersistence(self.research).source(self.uid))

    def establish_interrupted_chain(self):
        self.die(acquisition_death, 71, 'cutoff')
        self.assertEqual(self.progress().admission(self.uid)['requests_used'], 2)
        jobs = JobPersistence(self.research)
        self.assertEqual(jobs.source(self.uid)['status'], 'RUNNING')
        descriptor = jobs.descriptor(self.uid)
        with EvidenceStore(self.evidence).connect() as c:
            setup = c.execute('SELECT * FROM ownership_acquisition_setup').fetchone()
        self.assertEqual(EvidenceStore(self.evidence).load(setup[3])['result'], 20)

        self.die(acquisition_death, 72, 'history')
        self.assertEqual(self.progress().admission(self.uid)['requests_used'], 3)
        with EvidenceStore(self.evidence).connect() as c:
            self.assertEqual(c.execute('SELECT * FROM ownership_acquisition_setup').fetchone(), setup)
            history = c.execute('SELECT id,query,coverage,status,attempts FROM ownership_history').fetchone()
            self.assertEqual(history[4], 1)
        self.assertEqual(json.loads(history[2])['next_cursor'], 'next')
        # Reopen through public acquire: Jobs recovers RUNNING, fences a new
        # generation, replays the persisted page, and requests only its cursor.
        result = acquire(self.research, self.evidence,
                         fixture_rpc(self.evidence, self.ledger, self.uid), scan_id=self.uid)
        self.assertEqual(result['status'], 'SEED_HISTORY_ACQUIRED')
        self.assertEqual((result['provider_calls'], result['requests_used']), (1, 4))
        self.assertEqual(result['report']['acquisition']['cutoff'], 20)
        self.assertEqual(result['report']['calls'], 4)
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(jobs.descriptor(self.uid), descriptor)
        self.assertEqual(self.progress().admission(self.uid)['state'], 'SEALED')
        source = jobs.source(self.uid)
        with EvidenceStore(self.evidence).connect() as c:
            archive = c.execute('SELECT source_hash,budget,query,coverage,attempts FROM ownership_acquisition_history').fetchone()
            self.assertEqual(archive, (digest(source), self.uid, history[1],
                                     json.dumps(result['report']['history_queries'][0], sort_keys=True, separators=(',', ':')), 2))

        self.die(bank_clock_death, 73)
        admission = self.progress().admission(self.uid)
        self.assertEqual(admission['requests_used'], 6)
        self.assertEqual(admission['prepared_requests_used'], 4)
        bank = self.progress().bank(self.uid)
        self.assertIsNone(bank['block_time_hash'])
        self.assertEqual(EvidenceStore(self.evidence).load(bank['snapshot_hash'])['result']['context']['slot'], 20)
        resumed = advance(self.research, self.evidence, self.uid,
                          fixture_rpc(self.evidence, self.ledger, self.uid), max_calls=4)
        self.assertEqual((resumed['provider_calls'], resumed['requests_used']), (2, 8))
        self.assertEqual(resumed['status'], 'PROVIDER_RETRY_REQUIRED')
        self.assertFalse(resumed['snapshot']['reconciled'])
        self.assertFalse(resumed['eligible_for_trading'])
        self.assertEqual(resumed['snapshot_evidence']['snapshot_hash'], bank['snapshot_hash'])
        return self.preserved()

    def finish_budget(self, success):
        preserved = self.establish_interrupted_chain()
        for used in range(9, 18):
            result = advance(self.research, self.evidence, self.uid,
                             fixture_rpc(self.evidence, self.ledger, self.uid), max_calls=4)
            self.assertEqual((result['provider_calls'], result['requests_used']), (1, used))
            self.assertEqual(result['status'], 'PROVIDER_RETRY_REQUIRED')
            self.assertFalse(result['eligible_for_trading'])
            self.assertEqual(self.preserved(), preserved)
        result = advance(self.research, self.evidence, self.uid,
                         fixture_rpc(self.evidence, self.ledger, self.uid, account_success=success), max_calls=4)
        self.assertEqual(result['requests_used'], 18)
        self.assertEqual(result['snapshot']['reconciled'], success)
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(self.preserved(), preserved)
        attempts = read_ledger(self.ledger)
        self.assertEqual([a['reserved'] for a in attempts], list(range(1, 19)))
        self.assertEqual(sum(a['method'] == 'getSlot' for a in attempts), 1)
        self.assertEqual(sum(a['method'] == 'getMultipleAccounts' for a in attempts), 1)
        self.assertEqual(attempts[3]['params'][1]['paginationToken'], 'next')
        # Reopen both APIs repeatedly at the exhausted shared ceiling. Even a
        # now-healthy provider cannot replace the bank/cutoff or hide spending.
        for _ in range(2):
            def forbidden(*args):
                self.fail('I/O after shared 18-attempt ceiling')
            acquired = acquire(self.research, self.evidence, forbidden, scan_id=self.uid)
            self.assertEqual((acquired['status'], acquired['requests_used']), ('ALREADY_COMPLETE', 18))
            continued = advance(self.research, self.evidence, self.uid, forbidden, max_calls=4)
            self.assertEqual((continued['provider_calls'], continued['requests_used']), (0, 18))
            if not success:
                self.assertEqual(continued['status'], 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
                self.assertFalse(continued['snapshot']['reconciled'])
            self.assertFalse(continued['eligible_for_trading'])
            self.assertEqual(self.preserved(), preserved)
            source = JobPersistence(self.research).source(self.uid)
            head = saved_progress(EvidenceStore(self.evidence), source)
            self.assertEqual(head['requests_used'], 18)
        self.assertEqual(read_ledger(self.ledger), attempts)

    def test_recovery_succeeds_on_eighteenth_attempt_without_refreshing_cutoff(self):
        self.finish_budget(True)

    def test_missing_account_coverage_stays_blocked_after_restart_at_ceiling(self):
        self.finish_budget(False)
