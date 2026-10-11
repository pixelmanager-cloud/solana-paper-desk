"""SYNTHETIC_TEST_ONLY: T22J - the coordinator's probe table through the REAL run_once and the REAL history_first prepare.

T22J made the strict history classification opt-in (only the paper cycle's `_history`, which history_first also runs).
These probes pin that the strict safety still holds on both paths after that change:

* TLS, UNCLASSIFIED, bare OSError, "History page digest conflict", HISTORY_BINDING_INVALID, "Cached request mismatch"
  => INTEGRITY_HOLD (the pass stays NULL, the store gate stays latched);
* timeout, connection reset, HTTP 429, HTTP 502, PACING_DATABASE_BUSY => FAILED_CHARGED with the scan retired.

Transport-level failures are raised by the synthetic opener (the real PaperReadSources transport classifies them and
retains the failed attempt original). History-source-level failures are raised from inside the real
PaperHistorySource call, after HistoryProgress.advance has durably charged the reservation. Fixtures only; no network.
"""
import copy
import inspect
import json
import ssl
import textwrap
import time
import unittest
from contextlib import closing
from unittest.mock import patch
from urllib.error import HTTPError

from desk import paper_history_source, paper_read_sources as transport, provider_pacing
from desk.paper_read_sources import PaperReadError
from desk.model import canonical
from desk.providers import SOL
from tests import lock_sources
from tests import test_history_first_paper_entry as history_first_fixture
from tests import test_paper_cycle as cycle_fixtures
from tests.test_history_laundering import Probe
from tests.test_paper_read_sources import Response


def http(status):
    return HTTPError('https://rpc.invalid/', status, 'SYNTHETIC', {}, None)


REAL_CALL = paper_history_source.PaperHistorySource.__call__


def source_failure(kind):
    """A failure raised from inside the real PaperHistorySource call (the reservation is already charged)."""
    def call(self, method, params):
        if kind == 'HISTORY_BINDING_INVALID':
            return REAL_CALL(self, method, ['SYNTHETIC_TEST_ONLY', params[1]])     # the real binding check refuses it
        if kind == 'UNCLASSIFIED_ERROR':
            raise PaperReadError('UNCLASSIFIED_ERROR')
        raise ValueError(kind)
    return call


HOLD_TRANSPORT = {
    'TLS': ssl.SSLCertVerificationError('SYNTHETIC_TEST_ONLY'),
    'UNCLASSIFIED (transport)': ValueError('SYNTHETIC_TEST_ONLY'),
    'bare OSError': OSError('SYNTHETIC_TEST_ONLY'),
}
HOLD_SOURCE = ('History page digest conflict', 'HISTORY_BINDING_INVALID', 'Cached request mismatch', 'UNCLASSIFIED_ERROR')
CLOSE_TRANSPORT = {
    'timeout': TimeoutError('SYNTHETIC_TEST_ONLY'),
    'connection reset': ConnectionResetError('SYNTHETIC_TEST_ONLY'),
    'HTTP 429': http(429),
    'HTTP 502': http(502),
    'PACING_DATABASE_BUSY': provider_pacing.PacingError('PACING_DATABASE_BUSY'),
}


class RunOnceProbeTable(Probe):
    def test_integrity_causes_hold(self):
        for name, failure in HOLD_TRANSPORT.items():
            with self.subTest(name):
                super().setUp()
                self.assertHeld(self.run_expecting(failure))
        for kind in HOLD_SOURCE:
            with self.subTest(kind):
                super().setUp()
                with patch.object(paper_history_source.PaperHistorySource, '__call__', source_failure(kind)):
                    outcome = self.run_expecting()
                self.assertHeld(outcome)

    def test_transient_causes_close_failed_charged_and_retire_the_scan(self):
        for name, failure in CLOSE_TRANSPORT.items():
            with self.subTest(name):
                super().setUp()
                outcome = self.run_expecting(failure)
                self.assertClosed(outcome)
                self.assertTrue(self.record()['attempt_refs'], 'the closure binds the failed page original')


class HistoryFirstProbeTable(unittest.TestCase):
    def setUp(self):
        self.h = history_first_fixture.HistoryFirstTests('test_dry_run_no_credentials_no_history_or_provider_spend')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.row['provenance'] = 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'
        self.h.save()
        lock_sources.free(self)

    def invoke(self, failure=None):
        """history_first through the real prepare -> _history -> PaperHistorySource -> transport; the opener fails the page."""
        f = self.h.f
        f.f.at = int(time.time()); f.http_calls = []; f.sell_output = 10_000_000
        code = textwrap.dedent(inspect.getsource(f.actual_cycle)); a = code.index('    class Opener:'); b = code.index('    with ExitStack()')
        ns = {**vars(cycle_fixtures), 'outer': f}; exec(textwrap.dedent(code[a:b]), ns); delegate = ns['Opener']()
        f.history_failure = failure

        class HTTP:
            def open(self, request, *, timeout):
                if '/price/v3?' in request.full_url:
                    return Response(canonical({SOL: {'usdPrice': 100, 'blockId': 100, 'decimals': 9}}).encode())
                return delegate.open(request, timeout=timeout)
        with patch.object(history_first_fixture.tool.cli, '_credentials'), \
                patch.dict('os.environ', {'HELIUS_API_KEY': 'SYNTHETIC_TEST_ONLY', 'JUPITER_API_KEY': 'SYNTHETIC_TEST_ONLY'}), \
                patch.object(transport, 'build_opener', return_value=HTTP()):
            try:
                return self.h.invoke(live=True, systemd_credentials=True)
            except Exception as error:
                return error

    def state(self):
        store = self.h.f.f.progress.store
        with closing(store.connect()) as c:
            names = c.execute("SELECT name FROM sqlite_master WHERE name='paper_pass_closures'").fetchall()
            rows = c.execute('SELECT status,retired_scan FROM paper_pass_closures').fetchall() if names else []
            null = c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0]
        return rows, null

    def charged(self):
        return self.h.f.f.progress.admission(self.h.f.target.scan_id)['requests_used']

    def test_integrity_causes_hold(self):
        cases = [(name, failure, None) for name, failure in HOLD_TRANSPORT.items()] + [(k, None, k) for k in HOLD_SOURCE]
        for name, failure, kind in cases:
            with self.subTest(name):
                self.setUp()
                before = self.charged()
                if kind is None:
                    outcome = self.invoke(failure)
                else:
                    with patch.object(paper_history_source.PaperHistorySource, '__call__', source_failure(kind)):
                        outcome = self.invoke()
                self.assertEqual(self.state(), ([('INTEGRITY_HOLD', None)], 1), outcome)
                self.assertEqual(self.charged(), before + 1)                       # the charge stays charged

    def test_transient_causes_close_failed_charged_and_retire_the_scan(self):
        for name, failure in CLOSE_TRANSPORT.items():
            with self.subTest(name):
                self.setUp()
                outcome = self.invoke(failure)
                self.assertEqual(self.state(), ([('FAILED_CHARGED', self.h.f.target.scan_id)], 0), outcome)


if __name__ == '__main__':
    unittest.main()
