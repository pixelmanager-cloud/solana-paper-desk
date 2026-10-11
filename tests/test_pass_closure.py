"""SYNTHETIC_TEST_ONLY: generic FAILED_CHARGED / ABANDONED_CHARGED pass closure (T22).

Real run_once / gate / ledger / monitoring-budget code against the existing cycle fixtures; fixtures only, no
network, no credentials. Process death is simulated with a forked child that calls os._exit(137) at a chosen
stage (no Python cleanup, the kernel releases its flocks exactly as for SIGKILL).
"""
import copy
from contextlib import closing
from dataclasses import replace
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from desk import monitoring_budget as monitoring, paper_cycle as cycle, paper_pass_closure as closure
from desk import paper_read_sources as transport, paper_terminal_reconciliation as terminal, quote_execution as qe
from tests import lock_sources
from desk.evidence import EvidenceStore
from desk.model import digest
from tests.test_paper_cycle import PaperCycleTests

KILLED = 137


class Base(unittest.TestCase):
    def setUp(self):
        self.h = PaperCycleTests('test_empty_cycle_and_restart_preserve_new_experiment')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.store = self.h.f.progress.store
        self.progress = self.h.f.progress
        self.scan = self.h.target.scan_id
        self.h.http_calls = []
        self.h.sell_output = 10_000_000
        # T22G: owner evidence is injected ("nothing holds any lock": every pass/process under test is dead or is
        # this process), so lock-ownership logic behaves identically on Linux and macOS. Tests that need a live
        # holder inject their own table (tests/test_pass_closure_allowlist.py).
        lock_sources.free(self)

    # -- helpers ---------------------------------------------------------
    def rows(self):
        with closing(self.store.connect()) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE name='paper_pass_closures'").fetchone():
                return []
            return c.execute('SELECT pass_id,intent_hash,closure_hash,status,retired_scan FROM paper_pass_closures ORDER BY rowid').fetchall()

    def passes(self):
        with closing(self.store.connect()) as c:
            return c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes ORDER BY rowid').fetchall()

    def null_passes(self):
        return [p for p in self.passes() if p[2] is None]

    def record(self, index=0):
        return self.store.load(self.rows()[index][2])

    def gate(self, scans=()):
        return terminal.gate(self.store, self.h.f.jobs.path, scans, ledger_locked=str(self.h.path))

    def fail_entry(self, error=None):
        """A charged entry read that fails through the REAL transport, so the failed attempt original is retained.

        T22G: SOURCE_REQUEST_FAILED only says that a read failed. The closure classifies it from the retained
        attempt original, so the fixture's injected failure (no original) is replaced by a transport-level error.
        Default: a connection reset (transient). Pass another exception to exercise the latching classes.
        """
        def failure(*args, **kwargs):
            raise error or ConnectionResetError('SYNTHETIC_TEST_ONLY')
        with patch.object(transport, 'build_opener', side_effect=failure), \
                patch.object(transport.os.environ, 'get', return_value='SYNTHETIC_TEST_ONLY'), \
                patch.object(transport.time, 'time', return_value=self.h.f.at):
            first = self.h.run_cycle(source_factory=transport.PaperReadSources)
        self.h.f.calls.clear()
        return first

    def in_child(self, work):
        """Run `work` in a forked child that must die via os._exit(KILLED); returns its exit status."""
        pid = os.fork()
        if pid == 0:
            try:
                work()
                code = 0
            except BaseException:
                import traceback
                traceback.print_exc()
                code = 99
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        return os.WEXITSTATUS(status)


class FailedEntryClosureTests(Base):
    def test_failed_entry_pass_is_closed_with_a_replay_verifiable_record_and_retired(self):
        first = self.fail_entry()
        self.assertEqual(first['blockers'], ['SOURCE_REQUEST_FAILED'])
        self.assertEqual(self.null_passes(), [])
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        rec = self.record()
        self.assertEqual((rec['status'], rec['cause'], rec['role'], rec['retired_scan']),
                         ('FAILED_CHARGED', 'SOURCE_REQUEST_FAILED', 'ENTRY', self.scan))
        self.assertEqual(rec['scans'], {self.scan: {'before': 0, 'after': 1}})
        self.assertEqual(rec['result_hash'], first['evidence_hash'])
        self.assertEqual((rec['entry_authorized'], rec['live_readiness'], rec['execution_status']),
                         (False, False, 'EXECUTION_UNVERIFIED'))
        # the pass row is bound two ways: pass.outcome_hash == row.closure_hash == digest(record)
        self.assertEqual(self.passes()[0][2], rows[0][2])
        self.assertEqual(digest(rec), rows[0][2])
        closure._verify(self.store, self.progress, rec, row=rows[0])
        self.assertEqual(self.gate((self.scan,)), 'REJECTED_SCAN_RETIRED')
        self.assertIsNone(self.gate(('some-other-scan',)))            # per scan: other candidates proceed
        self.assertIsNone(self.gate(()))
        self.assertEqual(self.progress.admission(self.scan)['requests_used'], 1)   # charged, never refunded

    def test_retired_scan_is_refused_without_reads_and_other_state_unchanged(self):
        self.fail_entry()
        before = self.progress.admission(self.scan)
        second = self.h.run_cycle()
        self.assertEqual((second['status'], second['blockers']), ('RECOVERY_REQUIRED', ['REJECTED_SCAN_RETIRED']))
        self.assertEqual(self.h.f.calls, [])
        self.assertEqual(self.progress.admission(self.scan), before)
        self.assertEqual(len(self.rows()), 1)

    def test_closure_rows_are_immutable_unique_and_schema_guarded(self):
        self.fail_entry()
        pass_id, intent, key = self.rows()[0][:3]
        with closing(sqlite3.connect(self.store.path)) as c:
            for sql, args in (("UPDATE paper_pass_closures SET closure_hash=?", ('0' * 64,)),
                              ("UPDATE paper_pass_closures SET status='FAILED_CHARGED'", ()),
                              ("DELETE FROM paper_pass_closures", ()),
                              ("INSERT INTO paper_pass_closures VALUES(?,?,?,?,NULL)", (pass_id, intent, key, 'FAILED_CHARGED'))):
                with self.subTest(sql=sql), self.assertRaisesRegex(sqlite3.DatabaseError, 'immutable'):
                    c.execute(sql, args)
            c.execute('DROP TRIGGER paper_pass_closures_update')
            c.commit()
        with self.assertRaisesRegex(ValueError, 'schema'):
            self.gate()

    def test_forged_or_stale_records_do_not_verify(self):
        self.fail_entry()
        rec, row = self.record(), self.rows()[0]
        other_scan = 'f' * 32

        def mutate(path, value):
            clone = copy.deepcopy(rec)
            target = clone
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = value
            return clone
        forged = {
            'status': mutate(['status'], 'COMPLETE'),
            'integrity cause': mutate(['cause'], 'LEDGER_INTEGRITY_FAILURE'),
            'wrong intent': mutate(['intent_hash'], '0' * 64),
            'other pass': mutate(['pass_id'], 'a' * 32),
            'refund below baseline': mutate(['scans', self.scan, 'after'], -1),
            'charge above ceiling': mutate(['scans', self.scan, 'after'], 19),
            'charge beyond live counter': mutate(['scans', self.scan, 'after'], 2),
            'baseline moved': mutate(['scans', self.scan, 'before'], 1),
            'role flipped': mutate(['role'], 'HELD'),
            'extra scan': mutate(['scans'], {self.scan: rec['scans'][self.scan], other_scan: {'before': 0, 'after': 0}}),
            'unrelated attempt ref': mutate(['attempt_refs'], [rec['intent_hash']]),
            'ledger anchor': mutate(['ledger', 'events_hash'], '0' * 64),
            'retired elsewhere': mutate(['retired_scan'], other_scan),
            'flags': mutate(['entry_authorized'], True),
            'extra field': {**rec, 'surprise': 1},
        }
        for name, bad in forged.items():
            # without the row binding, so that the semantic replay checks (not merely the digest) are what refuse it
            with self.subTest(name), self.assertRaises(ValueError):
                closure._verify(self.store, self.progress, bad)
            with self.subTest(name + ' (with row)'), self.assertRaises(ValueError):
                closure._verify(self.store, self.progress, bad, row=row)
        # a record that is right but bound to the wrong table row
        with self.assertRaises(ValueError):
            closure._verify(self.store, self.progress, rec, row=(row[0], row[1], '0' * 64, row[3], row[4]))
        closure._verify(self.store, self.progress, rec, row=row)

    def test_close_refuses_resolved_unknown_integrity_duplicate_and_foreign_passes(self):
        self.fail_entry()
        pass_id = self.rows()[0][0]
        for kwargs in (dict(pass_id=pass_id, status='FAILED_CHARGED', cause='SOURCE_REQUEST_FAILED'),   # already closed
                       dict(pass_id='b' * 32, status='FAILED_CHARGED', cause='X'),                          # no such pass
                       ):
            with self.subTest(kwargs=kwargs), self.assertRaises(closure.ClosureRefused):
                closure.close(self.store, self.progress, **kwargs)
        # a COMPLETE pass can never be re-closed
        ok = self.h.run_cycle(candidates=())
        self.assertEqual(ok['status'], 'COMPLETE')
        done = [p for p in self.passes() if p[2] is not None and p[0] != pass_id]
        self.assertTrue(done)
        with self.assertRaises(closure.ClosureRefused):
            closure.close(self.store, self.progress, pass_id=done[0][0], status='FAILED_CHARGED', cause='ANY')
        # unresolved pass + integrity cause / unknown status are refused
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('c' * 32, self.passes()[0][1]))
        for kwargs in (dict(status='FAILED_CHARGED', cause='LEDGER_INTEGRITY_FAILURE'),
                       dict(status='FAILED_CHARGED', cause='USD_ORIGINAL_BINDING_INVALID'),
                       dict(status='RESOLVED', cause='X')):
            with self.subTest(kwargs=kwargs), self.assertRaises(closure.ClosureRefused):
                closure.close(self.store, self.progress, pass_id='c' * 32, **kwargs)

    def test_unsupported_or_malformed_intents_are_never_closed(self):
        self.h.run_cycle(candidates=())
        bad = self.store.save({'kind': 'something_else_v1'})
        worse = self.store.save({'kind': 'paper_cycle_intent_v1', 'closure_v1': True, 'admissions': {}, 'ledger': str(self.h.path)})
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('d' * 32, bad))
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('e' * 32, worse))
        for pass_id in ('d' * 32, 'e' * 32):
            with self.subTest(pass_id=pass_id), self.assertRaises(closure.ClosureRefused):
                closure.close(self.store, self.progress, pass_id=pass_id, status='ABANDONED_CHARGED', cause=closure.ABANDONED_CAUSE)
        result = closure.recover_abandoned(self.store, self.progress, ledger_db=self.h.path, cfg=self.h.cfg, clock=lambda: self.h.f.at,
                                         research_db=self.h.f.jobs.path)
        self.assertEqual(result['closed'], [])
        self.assertEqual(len(result['refused']), 1)          # the pre-closure intent is never even attempted
        self.assertEqual(self.gate(), 'OBSERVATION_RECOVERY_REQUIRED')   # still latched: unknown shapes fail closed


class IntegrityHoldTests(Base):
    def history_failure(self):
        # corrupt retained evidence surfacing mid-pass: an integrity cause, not a provider failure
        def corrupt(progress, scan):
            raise ValueError('Original evidence missing/corrupt')
        with self.assertRaises(ValueError):
            self.h.run_cycle(source_factory=corrupt)
        return {'blockers': ['HISTORY_RECOVERY_REQUIRED']}

    def test_integrity_cause_holds_the_latch_and_recovery_cannot_close_it(self):
        first = self.history_failure()
        self.assertEqual(len(self.null_passes()), 1)                      # deliberately left NULL
        hold = self.rows()
        self.assertEqual([r[3] for r in hold], ['INTEGRITY_HOLD'])
        self.assertIsNone(hold[0][4])
        self.assertEqual(self.gate(), 'OBSERVATION_RECOVERY_REQUIRED')
        # a later run performs the bounded recovery step first and must still be refused
        calls = len(self.h.f.calls)
        again = self.h.run_cycle()
        self.assertEqual((again['status'], again['blockers']), ('RECOVERY_REQUIRED', ['OBSERVATION_RECOVERY_REQUIRED']))
        self.assertEqual(len(self.h.f.calls), calls)
        recovered = closure.recover_abandoned(self.store, self.progress, ledger_db=self.h.path, cfg=self.h.cfg, clock=lambda: self.h.f.at,
                                         research_db=self.h.f.jobs.path)
        self.assertEqual((recovered['closed'], recovered['refused']), ([], []))
        with self.assertRaises(closure.ClosureRefused):
            closure.close(self.store, self.progress, pass_id=self.null_passes()[0][0], status='ABANDONED_CHARGED', cause=closure.ABANDONED_CAUSE)
        self.assertEqual([r[3] for r in self.rows()], ['INTEGRITY_HOLD'])

    def test_recovery_closes_an_abandoned_pass_but_not_a_held_one_in_the_same_store(self):
        self.history_failure()
        held = self.null_passes()[0][0]
        with closing(self.store.connect()) as c:                          # a second, truly abandoned pass
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('a1' * 16, self.passes()[0][1] if False else self.store.save(
                {'kind': 'paper_cycle_intent_v1', 'closure_v1': True, 'config_hash': digest(self.h.cfg), 'ledger': str(self.h.path), 'targets': [],
                 'admissions': {self.scan: self.progress.admission(self.scan)}})))
        result = closure.recover_abandoned(self.store, self.progress, ledger_db=self.h.path, cfg=self.h.cfg, clock=lambda: self.h.f.at,
                                         research_db=self.h.f.jobs.path)
        self.assertEqual(result['closed'], ['a1' * 16])
        self.assertEqual(sorted(r[3] for r in self.rows()), ['ABANDONED_CHARGED', 'INTEGRITY_HOLD'])
        self.assertEqual([p[0] for p in self.null_passes()], [held])


class ReviewRegressionTests(Base):
    def test_receipt_certified_and_pre_closure_passes_are_never_recovered(self):
        self.h.run_cycle(candidates=())
        intent = self.store.save({'kind': 'paper_cycle_intent_v1', 'closure_v1': True, 'config_hash': digest(self.h.cfg),
                                  'ledger': str(self.h.path), 'targets': [], 'admissions': {self.scan: self.progress.admission(self.scan)}})
        legacy = self.store.save({'kind': 'paper_cycle_intent_v1', 'config_hash': digest(self.h.cfg), 'ledger': str(self.h.path),
                                  'targets': [], 'admissions': {self.scan: self.progress.admission(self.scan)}})
        with closing(self.store.connect()) as c:
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('c1' * 16, intent))
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', ('c2' * 16, legacy))
        with patch.object(closure, 'certified_ids', return_value={'c1' * 16}):
            result = closure.recover_abandoned(self.store, self.progress, ledger_db=self.h.path, cfg=self.h.cfg, clock=lambda: self.h.f.at,
                                         research_db=self.h.f.jobs.path)
        self.assertEqual((result['closed'], result['refused']), ([], []))
        self.assertEqual(sorted(p[0] for p in self.null_passes()), ['c1' * 16, 'c2' * 16])   # byte-for-byte untouched
        with patch.object(closure, 'certified_ids', return_value={'c1' * 16}), self.assertRaises(closure.ClosureRefused):
            closure.close(self.store, self.progress, pass_id='c1' * 16, status='ABANDONED_CHARGED', cause=closure.ABANDONED_CAUSE)

    def test_corrupt_evidence_and_unclassified_defects_hold_while_transport_errors_close(self):
        cases = {'corrupt': (ValueError('Evidence checksum mismatch'), True), 'defect': (RuntimeError('boom'), True),
                 'json': (json.JSONDecodeError('x', 'y', 0), True), 'sqlite': (sqlite3.DatabaseError('malformed'), True),
                 'timeout': (TimeoutError('timeout'), False), 'interrupt': (KeyboardInterrupt(), False),
                 'reset': (ConnectionResetError('x'), False), 'bare OSError': (OSError('x'), None)}
        for name, (error, held) in cases.items():
            with self.subTest(name):
                cause, transient = closure.classify(error)
                if held is None:                        # T22G: unclassified is neither: it simply never closes
                    self.assertFalse(transient, cause)
                else:
                    self.assertEqual((closure.is_integrity(cause), transient), (held, not held), cause)

    def test_zero_charge_failure_does_not_retire_the_scan(self):
        def defect(progress, scan):
            raise TimeoutError('SYNTHETIC source factory failure before any request')    # transient (a bare OSError latches)
        with self.assertRaises(TimeoutError):
            self.h.run_cycle(source_factory=defect)
        self.assertEqual(self.progress.admission(self.scan)['requests_used'], 0)
        self.assertEqual([r[3] for r in self.rows()], ['FAILED_CHARGED'])
        self.assertIsNone(self.rows()[0][4])
        self.assertIsNone(self.gate((self.scan,)))


class MonitoringAbandonTests(Base):
    def setUp(self):
        super().setUp()
        self.item, self.allowance = self.h.monitoring_fixture()

    def reserve(self, n=1):
        for i in range(n):
            self.allowance.reserve_read(self.progress, self.scan, 'getBlockTime', [i + 1])

    def age(self):
        # T25F: an orphan is abandoned only when older than ABANDON_AFTER_SECONDS and the owner is provably gone,
        # which needs both lock files to exist (this process, the only possible holder, is allowed).
        for lock in (str(self.store.path) + '.ownership-invocation.lock', str(self.h.path) + '.paper-cycle.lock'):
            open(lock, 'a').close()
        self.h.f.at += monitoring.ABANDON_AFTER_SECONDS + 30

    def test_young_or_owned_orphan_is_not_abandoned(self):
        # T22F: the T25F preconditions hold for the pass-closure entry point too (nothing is written when unmet).
        self.reserve(1)
        self.age()
        young = self.h.f.at - (monitoring.ABANDON_AFTER_SECONDS + 30) + 10
        self.h.f.at = young
        with self.assertRaisesRegex(monitoring.MonitoringBlocked, 'OUTCOME_PENDING'):
            self.allowance.abandon_pending()
        self.h.f.at += monitoring.ABANDON_AFTER_SECONDS + 30
        os.unlink(str(self.h.path) + '.paper-cycle.lock')                # a missing lock file proves nothing
        with self.assertRaisesRegex(monitoring.MonitoringBlocked, 'OUTCOME_PENDING'):
            self.allowance.abandon_pending()
        with closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_outcomes').fetchone()[0], 0)

    def test_dangling_reservation_gets_an_explicit_abandoned_outcome_and_stays_charged(self):
        self.reserve(1)
        self.assertIn('MONITORING_OUTCOME_PENDING', self.allowance.snapshot()['blockers'])
        with self.assertRaisesRegex(monitoring.MonitoringBlocked, 'OUTCOME_PENDING'):
            self.reserve(1)                                             # no new read until the dead one is resolved
        total = self.allowance.snapshot()['total_used']
        self.age()
        self.assertEqual(self.allowance.abandon_pending(), [total])
        snap = self.allowance.snapshot()
        self.assertEqual((snap['blockers'], snap['total_used'], snap['window_used']), ([], total, total))
        with closing(self.store.connect()) as c:
            key = c.execute('SELECT evidence_hash FROM paper_monitoring_outcomes WHERE reservation_id=?', (total,)).fetchone()[0]
        self.assertEqual(self.store.load(key)['kind'], 'paper_monitoring_abandoned_v1')
        self.assertEqual(self.allowance.abandon_pending(), [])            # idempotent
        # a normal pass still works afterwards and is charged exactly once more
        self.h.sell_output = 10_000_000
        done = self.h.actual_cycle(positions=(self.item,), candidates=(), monitoring=True)
        self.assertEqual(done['status'], 'COMPLETE', done)
        self.assertEqual(self.allowance.snapshot()['total_used'], total + done['monitoring_attempted_requests'])

    def test_forged_abandoned_outcomes_fail_the_accounting_replay_and_roll_back(self):
        self.reserve(1)
        pending = self.allowance.snapshot()['total_used']
        real = monitoring.MonitoringBudget._save_page
        self.age()

        def forging(change):
            # T22F: abandonment now writes inside its own transaction (_save_page), not through EvidenceStore.save.
            def save(budget, c, payload):
                if type(payload) is dict and payload.get('kind') == 'paper_monitoring_abandoned_v1':
                    payload = change(copy.deepcopy(payload))
                return real(budget, c, payload)
            return save

        def top(field, value):
            return lambda r: {**r, field: value}

        def receipt(field, value):
            return lambda r: {**r, 'monitoring_reservation': {**r['monitoring_reservation'], field: value}}
        variants = {
            'params hash': top('params_hash', '0' * 64), 'completed read': top('request_completed', True),
            'provider failure code': top('failure_code', 'HTTP_REJECTED'), 'other scan': top('scan_id', 'x' * 32),
            'other method': top('method', 'getSlot'), 'receipt id': receipt('id', pending + 1),
            'receipt time': receipt('reserved_at', 1.0), 'receipt mint': receipt('mint', 'M' * 32),
            'receipt checkpoint': receipt('checkpoint_hash', '0' * 64), 'receipt cap': receipt('cap', 1),
        }
        for name, change in variants.items():
            with self.subTest(name), patch.object(monitoring.MonitoringBudget, '_save_page', forging(change)):
                with self.assertRaises(monitoring.MonitoringBlocked):
                    self.allowance.abandon_pending()
                with closing(self.store.connect()) as c:                  # rolled back: still dangling, nothing written
                    self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_outcomes WHERE reservation_id=?', (pending,)).fetchone()[0], 0)
        self.assertEqual(self.allowance.abandon_pending(), [pending])      # the genuine record is accepted
        before = self.allowance.snapshot()
        self.assertEqual(self.allowance.abandon_pending(), [])             # completed reads are never touched
        self.assertEqual(self.allowance.snapshot(), before)

    def test_more_than_the_bound_refuses_and_writes_nothing(self):
        self.reserve(1)
        with closing(self.store.connect()) as c:                          # reserve_read allows one; tamper-grade state
            template = c.execute('SELECT at,scan_id,mint,checkpoint_hash,method,params_hash FROM paper_monitoring_reservations ORDER BY id DESC LIMIT 1').fetchone()
            first = c.execute('SELECT max(id) FROM paper_monitoring_reservations').fetchone()[0]
            for i in range(monitoring.MAX_ABANDONED):
                c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)', (first + 1 + i, *template))
            c.execute('UPDATE paper_monitoring_budget SET total=?', (first + monitoring.MAX_ABANDONED,))
            c.commit()
        with self.assertRaises(monitoring.MonitoringBlocked):
            self.allowance.abandon_pending()
        with closing(self.store.connect()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_outcomes WHERE reservation_id>=?', (first,)).fetchone()[0], 0)


class KilledProcessTests(Base):
    """A forked child dies (os._exit) at a stage; the next start must recover deterministically."""

    def entry_in_child(self, patches):
        def work():
            with patches():
                self.h.actual_cycle()
            os._exit(0)           # a child that was NOT killed is a test failure
        return self.in_child(work)

    def recover(self):
        return closure.recover_abandoned(self.store, self.progress, ledger_db=self.h.path, cfg=self.h.cfg, clock=lambda: self.h.f.at,
                                         research_db=self.h.f.jobs.path)

    def assert_recovered_once(self, expect_charge_delta=None):
        admission = self.progress.admission(self.scan)
        events = self.events()
        result = self.recover()
        self.assertEqual(result['refused'], [], result)
        self.assertEqual(len(result['closed']), 1)
        self.assertEqual(self.null_passes(), [])
        rec = self.record()
        self.assertEqual((rec['status'], rec['cause'], rec['result_hash'], rec['retired_scan']),
                         ('ABANDONED_CHARGED', 'LEASE_GONE', None, None))
        self.assertEqual(self.progress.admission(self.scan), admission)            # recovery never charges or refunds
        self.assertEqual(self.events(), events)
        again = self.recover()                                                      # deterministic and idempotent
        self.assertEqual((again['closed'], again['refused']), ([], []))
        self.assertIsNone(self.gate())
        return rec

    def events(self):
        with closing(sqlite3.connect(self.h.path)) as c:
            return c.execute('SELECT count(*) FROM events').fetchone()[0], c.execute('SELECT count(*) FROM outcomes').fetchone()[0]

    def test_kill_before_the_first_charge(self):
        self.assertEqual(self.entry_in_child(lambda: patch.object(cycle, 'Ledger', side_effect=lambda *a, **k: os._exit(KILLED))), KILLED)
        self.assertEqual(len(self.null_passes()), 1)
        self.assertEqual(self.progress.admission(self.scan)['requests_used'], 0)
        self.assertEqual(self.gate(), 'OBSERVATION_RECOVERY_REQUIRED')              # latched until the next start recovers it
        rec = self.assert_recovered_once()
        self.assertEqual(rec['scans'], {self.scan: {'before': 0, 'after': 0}})
        # nothing was charged, so the very same candidate may run again from a clean store
        ok = self.h.actual_cycle()
        self.assertEqual(ok['status'], 'COMPLETE', ok)

    def test_kill_after_a_charge_before_its_original_is_recorded(self):
        def patches():
            real = cycle.PaperReadSources.rpc

            def killed(source, *a, **k):
                self.progress.reserve(source.scan_id)
                os._exit(KILLED)
            return patch.object(cycle.PaperReadSources, 'rpc', killed)
        # the actual-transport harness builds PaperReadSources; kill on its first rpc after the charge
        self.assertEqual(self.entry_in_child(patches), KILLED)
        self.assertEqual(self.progress.admission(self.scan)['requests_used'], 1)
        self.assertEqual(len(self.null_passes()), 1)
        rec = self.assert_recovered_once()
        self.assertEqual(rec['scans'], {self.scan: {'before': 0, 'after': 1}})
        self.assertEqual(rec['attempt_refs'], [])                                   # the charge has no original: visible, not hidden
        # the charge stays: a restart continues from 1/18, no double charge, and a fresh pass is not latched
        self.assertEqual(self.progress.admission(self.scan)['requests_used'], 1)
        self.assertEqual(self.gate((self.scan,)), None)                              # ABANDONED never retires a scan

    def test_kill_after_the_result_is_written_but_before_the_outcome_is_attached(self):
        real = EvidenceStore.save

        def patches():
            def saving(store, payload):
                key = real(store, payload)
                if type(payload) is dict and payload.get('kind') == 'paper_cycle_v1':
                    os._exit(KILLED)
                return key
            return patch.object(EvidenceStore, 'save', saving)
        self.assertEqual(self.entry_in_child(patches), KILLED)
        self.assertEqual(len(self.null_passes()), 1)
        state = cycle._state(self.h.path, self.h.cfg)
        self.assertEqual(len(state['positions']), 1)                                 # the BUY was already recorded
        rec = self.assert_recovered_once()
        self.assertEqual(rec['role'], 'ENTRY')
        self.assertEqual(len(cycle._state(self.h.path, self.h.cfg)['positions']), 1)  # not duplicated, not undone
        # the open position can now be monitored: no store-wide latch is left
        self.h.sell_output = 10_000_000
        from tests.test_paper_cycle import PaperCycleTests  # noqa: F401  (same harness)
        position = cycle._state(self.h.path, self.h.cfg)['positions'][self.h.target.mint]
        item = replace(self.h.item, target=replace(self.h.target, amount_raw=qe.raw_quantity(position['qty'], 6)), graduation_refs=())
        cycle.MonitoringBudget(self.store, self.h.path, self.h.cfg, clock=lambda: self.h.f.at).provision()
        held = self.h.actual_cycle(positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(held['status'], 'COMPLETE', held)

    def test_kill_in_a_held_pass_after_its_monitoring_reservation(self):
        item, allowance = self.h.monitoring_fixture()
        before = self.progress.admission(self.scan)

        def work():
            class Dies(cycle.PaperReadSources):
                def rpc(source, method, params, *, timeout_seconds):
                    source.monitoring_budget.reserve_read(source.progress, source.scan_id, method, params)
                    os._exit(KILLED)
            self.h.run_cycle(position_targets=(item,), candidates=(), monitoring=True, source_factory=Dies)
            os._exit(0)
        self.assertEqual(self.in_child(work), KILLED)
        self.assertIn('MONITORING_OUTCOME_PENDING', allowance.snapshot()['blockers'])
        self.assertEqual(len(self.null_passes()), 1)
        used = allowance.snapshot()['total_used']
        # T25F: the orphan reservation is abandoned only once it is older than ABANDON_AFTER_SECONDS and no process
        # holds the cycle locks (the dead child's flocks are gone), so recovery runs after that age.
        self.h.f.at += monitoring.ABANDON_AFTER_SECONDS + 30
        rec = self.assert_recovered_once()
        self.assertEqual((rec['role'], rec['monitoring']), ('HELD', {'first': used, 'last': used}))
        self.assertEqual(self.progress.admission(self.scan), before)
        snap = allowance.snapshot()
        self.assertEqual((snap['blockers'], snap['total_used']), ([], used))        # charged once, resolved, not refunded
        self.h.sell_output = 500_000
        done = self.h.actual_cycle(positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(done['status'], 'COMPLETE', done)
        self.assertEqual(cycle._state(self.h.path, self.h.cfg)['positions'], {})    # the position exited after the kill

    def test_run_once_recovers_by_itself_and_never_while_the_owner_is_alive(self):
        self.assertEqual(self.entry_in_child(lambda: patch.object(cycle, 'Ledger', side_effect=lambda *a, **k: os._exit(KILLED))), KILLED)
        # while a live owner holds the ledger lock, recovery by another invocation is refused outright
        with cycle._lock(str(self.h.path) + '.paper-cycle.lock') as acquired:
            self.assertTrue(acquired)
            busy = self.h.run_cycle()
        self.assertEqual(busy['blockers'], ['PAPER_CYCLE_BUSY'])
        self.assertEqual(len(self.null_passes()), 1)                                 # untouched
        ok = self.h.actual_cycle()                                                   # the next start recovers, then runs
        self.assertEqual(ok['status'], 'COMPLETE', ok)
        self.assertEqual([r[3] for r in self.rows()], ['ABANDONED_CHARGED'])


class DispatcherClosureTests(unittest.TestCase):
    def setUp(self):
        from tests import test_paper_entry_dispatcher as fixture
        from tools import paper_entry_dispatcher as tool
        self.tool = tool
        self.h = fixture.DispatcherTests('test_dry_run_no_admission_credentials_or_io_and_context_activation')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        lock_sources.free(self)       # T22H M1: owner evidence is injected, so the kill/abandon tests also pass on macOS (no /proc/locks)

    def results(self):
        with sqlite3.connect(self.h.journal) as c:
            return [json.loads(r[0]) for r in c.execute('SELECT payload FROM results ORDER BY rowid')]

    def kill_after_intent(self):
        def work():
            with patch.object(self.tool.cli, '_credentials'), \
                    patch('desk.providers.helius_rpc', side_effect=lambda *a, **k: os._exit(KILLED)):
                self.h.invoke(execute=True, systemd_credentials=True)
            os._exit(0)
        pid = os.fork()
        if pid == 0:
            try:
                work()
            finally:
                os._exit(99)
        return os.WEXITSTATUS(os.waitpid(pid, 0)[1])

    def test_killed_dispatch_stays_refused_inside_its_deadline_then_is_abandoned(self):
        self.assertEqual(self.kill_after_intent(), KILLED)
        self.assertEqual((self.h.count('intents'), self.h.count('results')), (1, 0))
        with self.assertRaisesRegex(ValueError, 'Unresolved dispatch'):
            self.h.invoke()                                                           # read-only path never recovers
        with patch.object(self.tool.cli, '_credentials'), self.assertRaisesRegex(ValueError, 'Unresolved dispatch'):
            self.h.invoke(execute=True, systemd_credentials=True)                     # still inside the deadline
        self.assertEqual(self.h.count('results'), 0)
        later = self.tool._now() + self.tool.ABANDON_AFTER_SECONDS + 1
        with patch.object(self.tool, '_now', return_value=later), patch.object(self.tool.cli, '_credentials'), \
                patch('desk.providers.helius_rpc', side_effect=AssertionError('no provider call during recovery')):
            outcome = self.h.invoke(execute=True, systemd_credentials=True)
        self.assertEqual(outcome['status'], 'NO_CANDIDATE')                           # the same candidate is never retried
        result = self.results()[0]['result']
        self.assertEqual((result['kind'], result['status'], result['cause'], result['entry_authorized']),
                         ('dispatcher_failed_charged_v1', 'ABANDONED_CHARGED', 'LEASE_GONE', False))
        with self.h.f.jobs.connect() as c:
            scan = c.execute('SELECT id FROM scans').fetchone()[0]
        self.assertEqual(self.h.f.progress.admission(scan)['requests_used'], 1)        # the in-flight request stays charged, never refunded
        with patch.object(self.tool, '_now', return_value=later):
            self.assertEqual(self.h.invoke()['status'], 'NO_CANDIDATE')               # strict replay of the new result kind passes

    def test_failure_dispositions_must_replay(self):
        self.assertEqual(self.kill_after_intent(), KILLED)
        later = self.tool._now() + self.tool.ABANDON_AFTER_SECONDS + 1
        with patch.object(self.tool, '_now', return_value=later), patch.object(self.tool.cli, '_credentials'):
            self.h.invoke(execute=True, systemd_credentials=True)
        identity, payload = sqlite3.connect(self.h.journal).execute('SELECT id,payload FROM results').fetchone()
        good = json.loads(payload)
        journal = self.h.journal

        def replace_result(value):
            with sqlite3.connect(journal) as c:
                c.execute('PRAGMA recursive_triggers=OFF')
                guards = c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='results'").fetchall()
                for name, _ in guards:
                    c.execute(f'DROP TRIGGER {name}')
                from desk.model import canonical
                c.execute('UPDATE results SET payload=?,hash=? WHERE id=?', (canonical(value), digest(value), identity))
                for _, sql in guards:
                    c.execute(sql)
        bad_cases = {
            'abandoned too early': {**good, 'at': good['at'] - self.tool.ABANDON_AFTER_SECONDS},
            'integrity cause': {**good, 'result': {**good['result'], 'status': 'FAILED_CHARGED', 'cause': 'LEDGER_INTEGRITY_FAILURE'}},
            # T22G allow-list: a FAILED_CHARGED disposition must name an allow-listed transient cause
            'cause off the allow-list': {**good, 'result': {**good['result'], 'status': 'FAILED_CHARGED', 'cause': 'MINT_BINDING'}},
            'latching provider class': {**good, 'result': {**good['result'], 'status': 'FAILED_CHARGED', 'cause': 'TLS_ERROR'}},
            'wrong abandon cause': {**good, 'result': {**good['result'], 'cause': 'SOMETHING_ELSE'}},
            'entry authorized': {**good, 'result': {**good['result'], 'entry_authorized': True}},
            'extra field': {**good, 'result': {**good['result'], 'surprise': 1}},
            'other intent': {**good, 'intent_hash': '0' * 64},
        }
        for name, bad in bad_cases.items():
            with self.subTest(name):
                replace_result(bad)
                with patch.object(self.tool, '_now', return_value=later), self.assertRaises(ValueError):
                    self.h.invoke()
        replace_result(good)
        with patch.object(self.tool, '_now', return_value=later):
            self.assertEqual(self.h.invoke()['status'], 'NO_CANDIDATE')

    def test_in_process_failure_closes_failed_charged_and_a_new_candidate_proceeds(self):
        # T22G: the acquisition's typed PROVIDER_RETRY_REQUIRED status is raised by the dispatcher as the allow-listed
        # ACQUISITION_INCOMPLETE. (A raw provider OSError makes the acquisition return the ambiguous
        # ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED, which stays unresolved: see the next tests.)
        with patch.object(self.tool.cli, '_credentials'), \
                patch.object(self.tool.acquisition, 'acquire', return_value={'status': 'PROVIDER_RETRY_REQUIRED', 'scan_id': None}):
            with self.assertRaises(ValueError):
                self.h.invoke(execute=True, systemd_credentials=True)
        result = self.results()[0]['result']
        self.assertEqual((result['kind'], result['status'], result['cause']), ('dispatcher_failed_charged_v1', 'FAILED_CHARGED', 'ACQUISITION_INCOMPLETE'))
        self.assertEqual(self.h.invoke()['status'], 'NO_CANDIDATE')
        self.h.append_distinct_migration()
        fresh = self.h.invoke()
        self.assertEqual(fresh['status'], 'DRY_RUN')                                  # another candidate is selectable
        self.assertNotEqual(fresh['hint']['mint'], self.h.mint)


    def test_a_raw_provider_error_leaves_the_ambiguous_status_unresolved(self):
        with patch.object(self.tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=OSError('timeout')):
            with self.assertRaises((ValueError, OSError)):
                self.h.invoke(execute=True, systemd_credentials=True)
        self.assertEqual((self.h.count('intents'), self.h.count('results')), (1, 0))

    def test_a_later_successful_provider_call_clears_the_transient_attribution(self):
        # T22G: the ambiguous status is attributed to the LAST provider call only. An earlier transient error that
        # was followed by a successful call cannot explain it, so the intent stays unresolved.
        def acquire(research, evidence, rpc, **kwargs):
            try:
                rpc('getSlot', [])
            except TimeoutError:
                pass
            rpc('getSlot', [])
            return {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None}
        with patch.object(self.tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=[TimeoutError('x'), 1]), \
                patch.object(self.tool.acquisition, 'acquire', side_effect=acquire):
            with self.assertRaises(ValueError):
                self.h.invoke(execute=True, systemd_credentials=True)
        self.assertEqual((self.h.count('intents'), self.h.count('results')), (1, 0))

    def test_the_last_provider_call_failing_transiently_explains_the_ambiguous_status(self):
        def acquire(research, evidence, rpc, **kwargs):
            rpc('getSlot', [])
            try:
                rpc('getSlot', [])
            except TimeoutError:
                pass
            return {'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None}
        with patch.object(self.tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=[1, TimeoutError('x')]), \
                patch.object(self.tool.acquisition, 'acquire', side_effect=acquire):
            with self.assertRaises(ValueError):
                self.h.invoke(execute=True, systemd_credentials=True)
        self.assertEqual((self.h.count('intents'), self.h.count('results')), (1, 1))
        self.assertEqual(self.results()[0]['result']['cause'], 'TRANSPORT_ERROR')

    def test_blocked_evidence_status_after_the_intent_stays_unresolved(self):
        # T22G allow-list: only the acquisition's ordinary retry statuses close; a status that may mean blocked
        # evidence leaves the intent unresolved and the dispatcher latched.
        with patch.object(self.tool.cli, '_credentials'), \
                patch.object(self.tool.acquisition, 'acquire', return_value={'status': 'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED', 'scan_id': None}):
            with self.assertRaises(ValueError):
                self.h.invoke(execute=True, systemd_credentials=True)
        self.assertEqual((self.h.count('intents'), self.h.count('results')), (1, 0))
        with self.assertRaisesRegex(ValueError, 'Unresolved dispatch'):
            self.h.invoke()


class GateCostTests(Base):
    def test_gate_loads_no_closure_unless_a_requested_scan_is_retired(self):
        self.fail_entry()
        loads = []
        real = terminal._load

        def counting(*args, **kwargs):
            loads.append(args[1])
            return real(*args, **kwargs)
        research = self.h.f.jobs.path
        with patch.object(terminal, '_load', side_effect=counting):
            self.assertIsNone(closure.gate(self.store, research, ('other-scan',), ledger_locked=str(self.h.path)))
        base = len(loads)
        # many more (synthetic, correctly bound) closed passes must not add proof loads
        with closing(self.store.connect()) as c:
            for i in range(200):
                pass_id, intent, key = '%032x' % (i + 1), '%064x' % (i + 1), '%064x' % (i + 1001)
                c.execute('INSERT INTO paper_observation_passes VALUES(?,?,?)', (pass_id, intent, key))
                c.execute('INSERT INTO paper_pass_closures VALUES(?,?,?,?,NULL)', (pass_id, intent, key, 'FAILED_CHARGED'))
        loads.clear()
        with patch.object(terminal, '_load', side_effect=counting):
            self.assertIsNone(closure.gate(self.store, research, ('other-scan',), ledger_locked=str(self.h.path)))
        self.assertEqual(len(loads), base)
        self.assertEqual(base, 0)                  # the cheap SQL binding inventory loads no evidence page at all


if __name__ == '__main__':
    unittest.main()
