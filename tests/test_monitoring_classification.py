"""T25: held-read failure classification and orphaned-reservation resolution. Fixture only.

Real MonitoringBudget/PaperReadSources/EvidenceStore on the synthetic quote ledger of
tests.test_monitoring_budget; the transport is a fake opener, nothing touches a network.

Classification rule under test: an availability failure carries no evidence about the
position, so it is charged and retained but must not latch the shared allowance
(blocked='SOURCE_FAILURE'). A rejection, an integrity failure, a TLS failure or an
unknown exception (a bug, or a man-in-the-middle symptom) must latch.
"""
import contextlib
import errno
import http.client
import io
import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from email.message import Message

from desk import coordinator_rpc as strict
from desk import monitoring_budget as mb
from desk import provider_pacing
from desk.model import canonical, digest
from desk.monitoring_budget import MonitoringBudget, MonitoringBlocked
from desk import paper_read_sources as reads
from desk.paper_read_sources import PaperReadError, PaperReadSources, RPC_ID
from tests import test_monitoring_budget as base
from tests.helpers import T
from tests.test_paper_read_sources import KEY, Response


class _Fixture(base.MonitoringBudgetTests):
    def runTest(self):
        pass


for _name in dir(base.MonitoringBudgetTests):      # reuse setUp/helpers only
    if _name.startswith('test_'):
        setattr(_Fixture, _name, None)

LINUX_LOCKS = os.path.exists('/proc/locks')
linux_only = unittest.skipUnless(LINUX_LOCKS, 'needs the real Linux /proc/locks and /proc/<pid>/fd; the injected-table '
                                 'tests below cover the same logic on every platform')

OK_BODY = canonical({'jsonrpc': '2.0', 'id': RPC_ID, 'result': 100}).encode()


class FakePacer:
    def __init__(self, code):
        self.code = code

    def acquire(self, provider, timeout_seconds):
        raise provider_pacing.PacingError(self.code)

    def finish(self, *a, **k):
        pass

    def throttle(self, *a, **k):
        pass


def http_error(status):
    return HTTPError('https://rpc.invalid/', status, 'synthetic', Message(), None)


class Classification(_Fixture):
    def attempt(self, *, raises=None, body=None, pacing=None):
        """One charged held read; returns (retained attempt record, blocked column)."""
        source = PaperReadSources(self.progress, self.scan, monitoring_budget=self.budget)

        class Opener:
            def open(_, request, *, timeout):
                if raises is not None:
                    raise raises
                return Response(OK_BODY if body is None else body)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('desk.paper_read_sources.os.environ.get', return_value=KEY))
            stack.enter_context(patch('desk.paper_read_sources.build_opener', return_value=Opener()))
            if pacing is not None:
                stack.enter_context(patch('desk.paper_read_sources.provider_pacing.configured',
                                          return_value=FakePacer(pacing)))
            with self.assertRaises(PaperReadError) as caught:
                source.rpc_with_evidence('getSlot', [{'commitment': 'finalized'}], timeout_seconds=3)
        return self.store.load(caught.exception.evidence_hash), self.accounting()[2]


class ClassificationTable(unittest.TestCase):
    def check(self, cases):
        for label, kwargs, code, latched in cases:
            with self.subTest(case=label):
                case = Classification()
                case.setUp()
                self.addCleanup(case.doCleanups)
                record, blocked = case.attempt(**kwargs)
                self.assertEqual(record['failure_code'], code)
                self.assertEqual(blocked == 'SOURCE_FAILURE', latched, record)
                self.assertEqual(case.accounting()[1], 1)           # charged either way
                with case.store.connect() as c:                       # and retained either way
                    self.assertEqual(c.execute('SELECT count(*) FROM paper_monitoring_outcomes').fetchone()[0], 1)

    def test_known_network_exceptions_are_transient(self):
        self.check([
            ('timeout', {'raises': TimeoutError('t')}, 'TRANSPORT_ERROR', False),
            ('socket.timeout', {'raises': socket.timeout('t')}, 'TRANSPORT_ERROR', False),
            ('reset', {'raises': ConnectionResetError()}, 'TRANSPORT_ERROR', False),
            ('refused', {'raises': ConnectionRefusedError()}, 'TRANSPORT_ERROR', False),
            ('broken pipe', {'raises': BrokenPipeError()}, 'TRANSPORT_ERROR', False),
            ('remote disconnected', {'raises': http.client.RemoteDisconnected('x')}, 'TRANSPORT_ERROR', False),
            ('dns', {'raises': socket.gaierror(-2, 'Name or service not known')}, 'TRANSPORT_ERROR', False),
            ('network unreachable', {'raises': OSError(errno.ENETUNREACH, 'unreachable')}, 'TRANSPORT_ERROR', False),
            ('host unreachable', {'raises': OSError(errno.EHOSTUNREACH, 'unreachable')}, 'TRANSPORT_ERROR', False),
            ('urlerror(refused)', {'raises': URLError(ConnectionRefusedError())}, 'TRANSPORT_ERROR', False),
            ('urlerror(dns)', {'raises': URLError(socket.gaierror(-2, 'x'))}, 'TRANSPORT_ERROR', False),
            ('urlerror(timeout)', {'raises': URLError(TimeoutError('t'))}, 'TRANSPORT_ERROR', False),
            ('tls eof (peer closed)', {'raises': ssl.SSLEOFError()}, 'TRANSPORT_ERROR', False),
        ])

    def test_tls_failures_latch(self):
        self.check([
            ('cert verification', {'raises': ssl.SSLCertVerificationError('bad cert')}, 'TLS_ERROR', True),
            ('urlerror(cert)', {'raises': URLError(ssl.SSLCertVerificationError('bad cert'))}, 'TLS_ERROR', True),
            ('generic ssl error', {'raises': ssl.SSLError('handshake failure')}, 'TLS_ERROR', True),
        ])

    def test_programming_errors_and_unknown_exceptions_latch(self):
        self.check([
            ('ValueError', {'raises': ValueError('bug')}, 'UNCLASSIFIED_ERROR', True),
            ('AttributeError', {'raises': AttributeError('bug')}, 'UNCLASSIFIED_ERROR', True),
            ('TypeError', {'raises': TypeError('bug')}, 'UNCLASSIFIED_ERROR', True),
            ('KeyError', {'raises': KeyError('bug')}, 'UNCLASSIFIED_ERROR', True),
            ('RuntimeError', {'raises': RuntimeError('bug')}, 'UNCLASSIFIED_ERROR', True),
            ('plain OSError without errno', {'raises': OSError('opaque')}, 'UNCLASSIFIED_ERROR', True),
            ('PermissionError', {'raises': PermissionError(errno.EACCES, 'denied')}, 'UNCLASSIFIED_ERROR', True),
            ('urlerror(str reason)', {'raises': URLError('unknown url type')}, 'UNCLASSIFIED_ERROR', True),
            ('bad status line', {'raises': http.client.BadStatusLine('x')}, 'UNCLASSIFIED_ERROR', True),
        ])

    def test_unclassified_record_never_carries_the_exception_text(self):
        case = Classification()
        case.setUp()
        self.addCleanup(case.doCleanups)
        record, _ = case.attempt(raises=ValueError('SYNTHETIC_SECRET_SENTINEL'))
        self.assertNotIn('SYNTHETIC_SECRET_SENTINEL', canonical(record))

    def test_json_rpc_errors_are_transient(self):
        rpc_error = lambda code, message: canonical(
            {'jsonrpc': '2.0', 'id': RPC_ID, 'error': {'code': code, 'message': message}}).encode()
        self.check([
            ('block not available', {'body': rpc_error(-32004, 'Block not available for slot 1')}, 'RPC_ERROR', False),
            ('internal error', {'body': rpc_error(-32603, 'Internal error')}, 'RPC_ERROR', False),
        ])

    def test_integrity_codes_still_latch(self):
        self.check([
            ('invalid envelope', {'body': b'{"unexpected":"envelope"}'}, 'RESPONSE_INVALID', True),
            ('not json', {'body': b'not json'}, 'RESPONSE_INVALID', True),
            ('truncated read', {'body': http.client.IncompleteRead(b'{"jsonrpc"')}, 'RESPONSE_TRUNCATED', True),
        ])

    def test_http_statuses(self):
        self.check([(str(s), {'raises': http_error(s)}, 'HTTP_REJECTED', latched)
                    for s, latched in ((408, False), (429, False), (500, False), (503, False), (504, False),
                                       (400, True), (401, True), (403, True), (404, True))])

    def test_coordinator_rpc_error_without_status_is_transient(self):
        self.check([('coordinator', {'raises': strict.CoordinatorRPCError('static safe message')},
                     'HTTP_REJECTED', False)])

    def test_http_error_with_a_malformed_status_latches_and_none_is_only_the_coordinator(self):
        self.check([
            ('non-int HTTPError code', {'raises': HTTPError('https://rpc.invalid/', '503', 'x', Message(), None)},
             'HTTP_REJECTED', True),
            ('coordinator error (status None)', {'raises': strict.CoordinatorRPCError('safe')}, 'HTTP_REJECTED', False),
        ])
        case = Classification()
        case.setUp()
        self.addCleanup(case.doCleanups)
        record, _ = case.attempt(raises=HTTPError('https://rpc.invalid/', '503', 'x', Message(), None))
        self.assertEqual(record['http_status'], -1)

    def _fresh(self):
        case = Classification()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_chunked_body_catch_all_classifies_instead_of_assuming_transport(self):
        def chunked_response(failure):
            data = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n'
            class Sock:
                def makefile(self, *a, **k):
                    return io.BytesIO(data)
            response = http.client.HTTPResponse(Sock())
            response.begin()
            def boom(amount):
                raise failure
            response._safe_read = boom
            return response
        for failure, code in ((ConnectionResetError(), 'TRANSPORT_ERROR'), (TimeoutError('t'), 'TRANSPORT_ERROR'),
                              (ValueError('bug'), 'UNCLASSIFIED_ERROR'), (AttributeError('bug'), 'UNCLASSIFIED_ERROR'),
                              (ssl.SSLCertVerificationError('bad'), 'TLS_ERROR'), (RuntimeError('bug'), 'UNCLASSIFIED_ERROR')):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(reads._ChunkReadError) as caught:
                    reads._read_chunked(chunked_response(failure), 1000, lambda: None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(mb._latching_failure({'failure_code': code}), code != 'TRANSPORT_ERROR')

    def test_pacing_contention_is_transient_and_integrity_latches(self):
        self.check([(code, {'pacing': code}, code, latched) for code, latched in (
            ('PACING_DEADLINE_EXCEEDED', False), ('PACING_DATABASE_BUSY', False), ('PACING_QUEUE_FULL', False),
            ('PACING_DATABASE_INVALID', True), ('PACING_DATABASE_CHANGED', True),
            ('PACING_GRANT_MISMATCH', True), ('PACING_POLICY_INVALID', True),
            ('PACING_CLOCK_INVALID', True), ('PACING_REQUEST_INVALID', True))])


class LatchPredicate(unittest.TestCase):
    """Pure predicate, for codes that need a provider-specific transport to produce."""

    def test_table(self):
        cases = {
            'KRAKEN_PROVIDER_ERROR': False, 'RPC_ERROR': False, 'TRANSPORT_ERROR': False,
            'DEADLINE_EXCEEDED': False, 'ABANDONED_CHARGED': False,
            'PACING_DEADLINE_EXCEEDED': False, 'PACING_DATABASE_BUSY': False, 'PACING_QUEUE_FULL': False,
            'KRAKEN_PRICE_INVALID': True, 'KRAKEN_PACING_NOT_CONFIGURED': True, 'CREDENTIAL_UNAVAILABLE': True,
            'RESPONSE_INVALID': True, 'RESPONSE_TRUNCATED': True, 'RESPONSE_OVERSIZED': True,
            'RESPONSE_HEADERS_INVALID': True, 'CLOCK_INVALID': True, 'TLS_ERROR': True,
            'UNCLASSIFIED_ERROR': True, 'PACING_DATABASE_INVALID': True, 'SOMETHING_NEW': True,
        }
        for code, latched in cases.items():
            with self.subTest(code=code):
                self.assertEqual(mb._latching_failure({'failure_code': code}), latched)
        self.assertFalse(mb._latching_failure({'failure_code': None}))

    def test_http_rejected_status_rules(self):
        for status, latched in ((None, False), (408, False), (429, False), (500, False), (599, False),
                                (400, True), (401, True), (403, True), (404, True), (302, True), ('503', True)):
            with self.subTest(status=status):
                self.assertEqual(mb._latching_failure({'failure_code': 'HTTP_REJECTED', 'http_status': status}),
                                 latched)


class Abandonment(_Fixture):
    """A reservation committed before the HTTP call, then the process died (T09 audit F5)."""

    def setUp(self):
        super().setUp()
        if not LINUX_LOCKS:          # no /proc here: inject empty sources ("nothing holds anything")
            self.use_lock_sources(table='')

    def use_lock_sources(self, *, table, proc=None):
        """Point the owner evidence at fixture files (portable): a /proc/locks-format table and a /proc-like tree."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        locks = os.path.join(tmp.name, 'locks')
        with open(locks, 'w') as stream:
            stream.write(table)
        root = os.path.join(tmp.name, 'proc')
        os.mkdir(root)
        for pid, target in (proc or {}).items():
            os.makedirs(f'{root}/{pid}/fd')
            os.symlink(target, f'{root}/{pid}/fd/7')
        for patcher in (patch.object(mb, 'LOCK_TABLE', locks), patch.object(mb, 'PROC_ROOT', root)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_locks(self):
        """run_once creates both lock files (open 'a') before any reservation: so must the fixture."""
        for path in (self.lock_path(), str(self.f.path) + '.paper-cycle.lock'):
            with open(path, 'a'):
                pass

    def kill_after_reservation(self):
        self.create_locks()
        with patch('desk.paper_read_sources.os.environ.get', return_value=KEY), \
                patch('desk.paper_read_sources.build_opener') as opener:
            opener.return_value.open.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                self.read_unmocked()

    def restart(self, at):
        self.budget = MonitoringBudget(self.store, self.f.path, self.f.cfg, clock=lambda: at)
        return self.budget

    def rows(self):
        with self.store.connect() as c:
            return (c.execute('SELECT * FROM paper_monitoring_reservations ORDER BY id').fetchall(),
                    c.execute('SELECT * FROM paper_monitoring_outcomes ORDER BY reservation_id').fetchall(),
                    c.execute('SELECT * FROM paper_monitoring_budget').fetchone())

    def lock_path(self):
        return str(self.store.path) + '.ownership-invocation.lock'

    def start_holder(self, path):
        holder = subprocess.Popen([sys.executable, '-c', textwrap.dedent('''
            import fcntl, sys, time
            f = open(sys.argv[1], "a"); fcntl.flock(f, fcntl.LOCK_EX); print("ready", flush=True); time.sleep(600)
        '''), path], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.poll() is None and holder.kill(), holder.wait()))
        self.assertEqual(holder.stdout.readline().strip(), 'ready')
        return holder

    # -- deadline -------------------------------------------------------
    def test_pending_within_the_deadline_still_fails_closed(self):
        self.kill_after_reservation()
        self.restart(T + mb.ABANDON_AFTER_SECONDS - 1)
        before = self.rows()
        with patch('desk.paper_read_sources.os.environ.get') as credentials:
            with self.assertRaises(PaperReadError) as caught:
                self.read_unmocked()
        credentials.assert_not_called()
        self.assertEqual(caught.exception.code, 'MONITORING_OUTCOME_PENDING')
        self.assertEqual(self.rows(), before)
        self.assertIn('MONITORING_OUTCOME_PENDING', self.budget.snapshot()['blockers'])

    def test_orphan_older_than_deadline_is_resolved_abandoned_charged_and_reads_resume(self):
        self.kill_after_reservation()
        (reservation,), (), budget_before = self.rows()
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        _, key = self.read()                                    # the held pass is no longer wedged
        reservations, outcomes, budget = self.rows()
        self.assertEqual(reservations[0], reservation)           # original reservation row untouched
        self.assertEqual([o[0] for o in outcomes], [1, 2])
        self.assertEqual((budget[8], budget[9]), (2, None))     # total charged 1 -> 2, never refunded, no latch
        record = self.store.load(outcomes[0][1])
        self.assertEqual(record['failure_code'], 'ABANDONED_CHARGED')
        self.assertEqual((record['kind'], record['charged'], record['refunded']),
                         (mb.ABANDONED_KIND, True, False))
        self.assertEqual((record['reservation_id'], record['reserved_at'], record['resolved_at']),
                         (1, T, T + mb.ABANDON_AFTER_SECONDS + 1))
        self.assertEqual(self.store.load(key)['monitoring_reservation']['total_used'], 2)
        self.assertEqual(self.budget.snapshot()['status'], 'AVAILABLE')

    def test_snapshot_does_not_report_a_deadline_expired_orphan_as_a_blocker(self):
        """tests/test_audit_first_cycle_b_monitoring.py::test_process_killed_after_reservation_must_not_block_forever"""
        self.kill_after_reservation()
        self.budget.clock = lambda: T + 7 * 86400
        self.assertEqual(self.budget.snapshot()['blockers'], [])

    def test_snapshot_is_read_only(self):
        self.kill_after_reservation()
        self.budget.clock = lambda: T + 7 * 86400
        before = self.rows()
        self.budget.snapshot()
        self.assertEqual(self.rows(), before)                    # resolution is appended by the next read only

    def test_resolution_is_idempotent_and_never_rewrites_history(self):
        self.kill_after_reservation()
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        self.read()
        after_first = self.rows()
        self.read()
        reservations, outcomes, _ = self.rows()
        self.assertEqual(reservations[:2], after_first[0])
        self.assertEqual(outcomes[:2], after_first[1])           # the abandoned outcome is unchanged
        self.assertEqual(sum(1 for o in outcomes if o[0] == 1), 1)
        with self.store.connect() as c:
            with self.assertRaises(sqlite3_errors()):
                c.execute('DELETE FROM paper_monitoring_outcomes WHERE reservation_id=1')
            with self.assertRaises(sqlite3_errors()):
                c.execute("UPDATE paper_monitoring_outcomes SET evidence_hash='x' WHERE reservation_id=1")

    def test_abandoned_outcome_counts_against_the_rolling_window(self):
        self.kill_after_reservation()
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        self.read()
        self.assertEqual(self.budget.snapshot()['window_used'], 2)
        self.assertEqual(self.budget.snapshot()['remaining'], mb.CAP - 2)

    # -- owner evidence ---------------------------------------------------
    @linux_only
    def test_live_owner_lock_holder_prevents_abandonment(self):
        self.kill_after_reservation()
        self.start_holder(self.lock_path())
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        before = self.rows()
        with self.assertRaises(PaperReadError) as caught:
            self.read_unmocked()
        self.assertEqual(caught.exception.code, 'MONITORING_OUTCOME_PENDING')
        self.assertEqual(self.rows(), before)
        self.assertIn('MONITORING_OUTCOME_PENDING', self.budget.snapshot()['blockers'])

    @linux_only
    def test_owner_killed_with_sigkill_releases_the_lease_and_the_orphan_resolves(self):
        self.kill_after_reservation()
        holder = self.start_holder(self.lock_path())
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        with self.assertRaises(PaperReadError):
            self.read_unmocked()
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        self.read()
        self.assertEqual(self.store.load(self.rows()[1][0][1])['failure_code'], 'ABANDONED_CHARGED')

    @linux_only
    def test_the_cycle_lock_of_the_ledger_also_counts_as_a_live_owner(self):
        self.kill_after_reservation()
        self.start_holder(str(self.f.path) + '.paper-cycle.lock')
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        with self.assertRaises(PaperReadError) as caught:
            self.read_unmocked()
        self.assertEqual(caught.exception.code, 'MONITORING_OUTCOME_PENDING')

    def test_locks_held_by_this_very_process_are_the_new_owner_not_a_blocker(self):
        """run_once holds both locks while it reads: its own PID must not block resolving a predecessor's orphan."""
        import fcntl
        self.kill_after_reservation()
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        with open(self.lock_path(), 'a') as a, open(str(self.f.path) + '.paper-cycle.lock', 'a') as b:
            fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.read()
        self.assertEqual(self.accounting()[1:], (2, None))

    @linux_only
    # -- portable owner evidence (injected lock table / process tree) --------
    def table_line(self, path, pid):
        return f'1: FLOCK  ADVISORY  WRITE {pid} 08:01:{os.stat(path).st_ino} 0 EOF\n'

    def blocked_read(self):
        before = self.rows()
        with self.assertRaises(PaperReadError) as caught:
            self.read_unmocked()
        self.assertEqual(caught.exception.code, 'MONITORING_OUTCOME_PENDING')
        self.assertEqual(self.rows(), before)

    def test_injected_table_with_a_foreign_holder_blocks_and_without_resolves(self):
        self.kill_after_reservation()
        self.use_lock_sources(table=self.table_line(self.lock_path(), os.getpid() + 1))
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.blocked_read()
        self.use_lock_sources(table=self.table_line(self.lock_path(), os.getpid()))     # only ourselves: the new owner
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        self.read()
        self.assertEqual(self.store.load(self.rows()[1][0][1])['failure_code'], 'ABANDONED_CHARGED')

    def test_missing_lock_file_means_owner_unknown_not_owner_gone(self):
        self.kill_after_reservation()
        os.unlink(self.lock_path())
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.assertIsNone(mb._lock_holders(self.lock_path()))
        self.blocked_read()
        self.assertIn('MONITORING_OUTCOME_PENDING', self.budget.snapshot()['blockers'])
        os.unlink(str(self.f.path) + '.paper-cycle.lock')               # the other lock missing is the same
        with open(self.lock_path(), 'a'):
            pass
        self.blocked_read()

    def test_recreated_lock_file_hides_no_live_owner(self):
        self.kill_after_reservation()
        old = self.lock_path()
        # A live process (pid 4242) still holds the ORIGINAL, now unlinked inode; the path names a new, unlocked file.
        self.use_lock_sources(table='', proc={4242: old + ' (deleted)'})
        os.unlink(old)
        with open(old, 'a'):
            pass
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.assertEqual(mb._lock_holders(old), set())                   # the new file looks free ...
        self.assertEqual(mb._deleted_lock_holders(old), {4242})          # ... but a live holder is on the old inode
        self.blocked_read()

    def test_unreadable_process_table_or_same_user_permission_failure_fails_closed(self):
        self.kill_after_reservation()
        self.use_lock_sources(table='')
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        with patch.object(mb, 'PROC_ROOT', '/nonexistent-proc-root'):
            self.assertIsNone(mb._deleted_lock_holders(self.lock_path()))
            self.blocked_read()
        real_listdir = os.listdir
        def deny(path):
            if str(path).endswith('/fd'):
                raise PermissionError('denied')
            return real_listdir(path)
        self.use_lock_sources(table='', proc={4243: 'x'})
        with patch.object(mb.os, 'listdir', deny):
            self.assertIsNone(mb._deleted_lock_holders(self.lock_path()))   # same-uid process we cannot inspect

    def test_handoff_context_orphan_is_critical_with_operator_guidance(self):
        self.kill_after_reservation()
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        with patch.object(self.budget, '_accounting', return_value=(3,) + self.accounting_row()[1:]):
            snapshot = self.budget.snapshot()
        self.assertEqual(snapshot['health'], 'CRITICAL')
        self.assertEqual(snapshot['operator_guidance'], mb.HANDOFF_PENDING_GUIDANCE)
        self.assertIn('MONITORING_OUTCOME_PENDING', snapshot['blockers'])
        normal = self.budget.snapshot()                                     # same orphan, native context: resolvable
        self.assertNotIn('health', normal)

    def accounting_row(self):
        with self.store.connect() as c:
            return self.budget._accounting(c)

    def test_unreadable_lock_table_fails_closed(self):
        self.kill_after_reservation()
        self.start_holder(self.lock_path())
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        real_open = open

        def deny(path, *a, **k):
            if str(path) == str(mb.LOCK_TABLE):
                raise PermissionError('denied')
            return real_open(path, *a, **k)
        with patch('builtins.open', deny), self.assertRaises(PaperReadError) as caught:
            self.read_unmocked()
        self.assertEqual(caught.exception.code, 'MONITORING_OUTCOME_PENDING')

    # -- adversarial -------------------------------------------------------
    def forge(self, **changes):
        """Append an 'abandoned' outcome for reservation 1 with one field changed, bypassing the API."""
        self.kill_after_reservation()
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        self.read()                                              # produces the genuine record for id 1
        genuine = self.store.load(self.rows()[1][0][1])
        self.replace_outcome({**genuine, **changes})

    def replace_outcome(self, record):
        """Swap the retained outcome of reservation 1 for `record`, bypassing the API and its triggers."""
        key = self.store.save(record)                           # own transaction: before the write lock below
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND "
                                     "tbl_name='paper_monitoring_outcomes'").fetchall():
                c.execute(f'DROP TRIGGER {name}')
            c.execute('UPDATE paper_monitoring_outcomes SET evidence_hash=? WHERE reservation_id=1', (key,))
            c.execute('COMMIT')

    def assertAccountingRejects(self):
        with self.assertRaises(MonitoringBlocked) as caught:
            self.budget.snapshot()
        self.assertEqual(caught.exception.code, 'MONITORING_ACCOUNTING_INVALID')

    def test_forged_params_binding_is_rejected(self):
        self.forge(params_hash='0' * 64)
        self.assertAccountingRejects()

    def test_forged_reservation_receipt_is_rejected(self):
        self.kill_after_reservation()
        self.restart(T + mb.ABANDON_AFTER_SECONDS + 1)
        self.now = T + mb.ABANDON_AFTER_SECONDS + 1
        self.read()
        receipt = self.store.load(self.rows()[1][0][1])['monitoring_reservation']
        for field, value in (('id', 2), ('total_used', 2), ('reserved_at', T + 1), ('mint', 'OTHER'),
                             ('checkpoint_hash', '0' * 64), ('cap', 1)):
            with self.subTest(field=field):
                self.forge_with_receipt({**receipt, field: value})
                self.assertAccountingRejects()

    def forge_with_receipt(self, receipt):
        self.replace_outcome({**self.store.load(self.rows()[1][0][1]), 'monitoring_reservation': receipt})

    def test_forged_abandonment_records_are_rejected(self):
        """Resolution before the deadline, a refund, an uncharged request or a reshaped record never verifies."""
        forgeries = {
            'resolved before the deadline': {'resolved_at': T + 1},
            'resolved at exactly the deadline': {'resolved_at': T + mb.ABANDON_AFTER_SECONDS},
            'refunded': {'refunded': True},
            'not charged': {'charged': False},
            'other deadline': {'abandon_after_seconds': 1},
            'other failure code': {'failure_code': 'TRANSPORT_ERROR'},
            'other reservation id': {'reservation_id': 2},
            'other reserved_at': {'reserved_at': T + 5},
            'extra field': {'note': 'x'},
        }
        for label, changes in forgeries.items():
            with self.subTest(forgery=label):
                case = Abandonment()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.forge(**changes)
                case.assertAccountingRejects()

    def test_abandonment_record_cannot_bind_a_different_scan_or_method(self):
        for field, value in (('scan_id', 'other-scan'), ('method', 'getBlockTime')):
            with self.subTest(field=field):
                case = Abandonment()
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.forge(**{field: value})
                case.assertAccountingRejects()


def sqlite3_errors():
    import sqlite3
    return sqlite3.DatabaseError


if __name__ == '__main__':
    unittest.main()
