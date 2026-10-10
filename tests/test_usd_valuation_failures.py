"""SYNTHETIC_TEST_ONLY: SOL/USD failures end terminal only when they are TRANSIENT (T37G).

T37F made every failed SOL/USD attempt terminal, including TLS, unclassified, 4xx and malformed answers: a laundering path (a
provider/path fault retired as an ordinary no-entry or FAILED_CHARGED). The probe table here runs the REAL paper cycle (Kraken
lifecycle harness, synthetic wire only; no network, no credentials) for every cause x {entry, held} x {Kraken up, Kraken down}
and pins the closure: transient -> terminal (NO_ENTRY / FAILED_CHARGED), anything else -> INTEGRITY_HOLD, except a Jupiter 401/403
whose pass was carried by a valid Kraken observation."""
import io
import json
import ssl
import sqlite3
import unittest
import urllib.error
from decimal import Decimal as D

from unittest.mock import patch

from desk import paper_cycle_no_entry as no_entry, regime_producer as producer, usd_valuation as uv
from desk import kraken_usd_observation as kraken
from desk.model import canonical
from desk.sol_usd_observation import SOL_MINT
from tests import test_usd_valuation_cycle as cyc
from tests import test_usd_valuation_regime as regime_fixtures


def http(code):
    class Rejected(urllib.error.HTTPError):
        def __init__(self, message):
            super().__init__('http://fixture', code, message, {}, io.BytesIO(b''))
    Rejected.__name__ = 'HTTP%d' % code
    return Rejected


TRANSIENT = {'transport': ConnectionResetError, '408': http(408), '503': http(503)}
FAULTS = {'tls': ssl.SSLCertVerificationError, 'unclassified': OSError, '401': http(401), '403': http(403), '404': http(404)}
EXTRA_MINTS = canonical({SOL_MINT: {'usdPrice': 100, 'blockId': 100, 'decimals': 9}, 'EXTRA': {'usdPrice': 1, 'blockId': 1, 'decimals': 6}}).encode()
MALFORMED = {'html': b'<html>', 'extra mints': EXTRA_MINTS,
             'no block id': canonical({SOL_MINT: {'usdPrice': 100, 'decimals': 9}}).encode(),
             'wrong decimals': canonical({SOL_MINT: {'usdPrice': 100, 'blockId': 100, 'decimals': 6}}).encode()}


class Probe(cyc.V2Base):
    def runTest(self):
        pass

    def enter(self):
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        return self.allowance()

    def end_state(self, mode, *, jupiter=None, kraken=None, wire=None, blocked_exit=False):
        """The pass's terminal state after the named failures: COMPLETE | NO_ENTRY | FAILED_CHARGED | INTEGRITY_HOLD | LATCH."""
        kw = {}
        if mode == 'held':
            self.enter()
            self.h.sell_output = 40_000 if blocked_exit else 30_000_000      # dust quote: UNRESOLVED_QUOTE_DEMAND, after the USD reads
            kw = dict(positions=(self.held_item(),), candidates=(), monitoring=True)
        if jupiter is not None:
            self.h.jupiter_fail, self.h.jupiter_exc = True, jupiter
        if wire is not None:
            self.h.jupiter_wire = wire
        if kraken is not None:
            self.h.kraken_fail, self.h.kraken_exc = True, kraken
        result = self.run_cycle(**kw)
        store = self.h.f.progress.store
        with store.connect() as c:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            no_entry_rows = c.execute('SELECT count(*) FROM paper_cycle_no_entry').fetchone()[0] if 'paper_cycle_no_entry' in names else 0
            closures = [r[0] for r in c.execute('SELECT status FROM paper_pass_closures')] if 'paper_pass_closures' in names else []
            pending = c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0]
        self.last = result
        if result['status'] == 'COMPLETE':
            return 'COMPLETE'
        if no_entry_rows and mode == 'entry':
            return 'NO_ENTRY'
        if closures:
            return closures[-1]
        return 'LATCH' if pending else 'OTHER'


class ProbeTableTests(Probe):
    """Every cause x {entry, held} x {Kraken up, Kraken down}. The names are the injected cause."""

    def run_row(self, mode, expected, **kw):
        self.setUp() if getattr(self, '_used', False) else None
        self._used = True
        state = self.end_state(mode, **kw)
        self.assertEqual(state, expected, (mode, {k: getattr(v, '__name__', v) for k, v in kw.items() if k != 'wire'}, self.last.get('blockers')))

    def test_a_transient_jupiter_failure_with_kraken_down_ends_terminal(self):
        for name, exc in TRANSIENT.items():
            for mode, terminal in (('entry', 'NO_ENTRY'), ('held', 'FAILED_CHARGED')):
                with self.subTest(cause=name, mode=mode):
                    self.run_row(mode, terminal, jupiter=exc, kraken=ConnectionResetError)

    def test_a_non_transient_jupiter_failure_with_kraken_down_is_held_never_retired(self):
        for name, exc in FAULTS.items():
            for mode in ('entry', 'held'):
                with self.subTest(cause=name, mode=mode):
                    self.run_row(mode, 'INTEGRITY_HOLD', jupiter=exc, kraken=ConnectionResetError)

    def test_a_malformed_jupiter_answer_with_kraken_down_is_held(self):
        for name, wire in MALFORMED.items():
            for mode in ('entry', 'held'):
                with self.subTest(cause=name, mode=mode):
                    self.run_row(mode, 'INTEGRITY_HOLD', wire=wire, kraken=ConnectionResetError)

    def test_a_transient_kraken_failure_with_jupiter_transient_down_ends_terminal(self):
        for name, exc in TRANSIENT.items():
            for mode, terminal in (('entry', 'NO_ENTRY'), ('held', 'FAILED_CHARGED')):
                with self.subTest(cause=name, mode=mode):
                    self.run_row(mode, terminal, jupiter=ConnectionResetError, kraken=exc)

    def test_a_non_transient_kraken_failure_is_held_never_retired(self):
        for name, exc in FAULTS.items():
            for mode in ('entry', 'held'):
                with self.subTest(cause=name, mode=mode):
                    self.run_row(mode, 'INTEGRITY_HOLD', jupiter=ConnectionResetError, kraken=exc)

    def test_with_kraken_up_the_fallback_carries_the_pass_whatever_the_primary_failure_was(self):
        for name, exc in {**TRANSIENT, **FAULTS}.items():
            for mode in ('entry', 'held'):
                with self.subTest(cause=name, mode=mode):
                    self.run_row(mode, 'COMPLETE', jupiter=exc)
        for name, wire in MALFORMED.items():
            with self.subTest(cause=name, mode='entry'):
                self.run_row('entry', 'COMPLETE', wire=wire)


class AuthExceptionTests(Probe):
    """A Jupiter 401/403 may end a pass terminal ONLY when a valid Kraken observation carried it."""

    def test_401_and_403_with_kraken_measured_end_a_blocked_exit_terminal(self):
        for name in ('401', '403'):
            with self.subTest(cause=name):
                self.setUp() if getattr(self, '_used', False) else None
                self._used = True
                state = self.end_state('held', jupiter=FAULTS[name], blocked_exit=True)
                self.assertEqual(state, 'FAILED_CHARGED', self.last)
                self.assertIn('UNRESOLVED_QUOTE_DEMAND', self.last['blockers'])
                self.assertEqual(len(self.last['usd_evidence_refs']), 2)         # the failed primary and the measured fallback are both cited

    def test_any_other_primary_failure_with_kraken_measured_still_holds_a_blocked_exit(self):
        for name in ('tls', 'unclassified', '404'):
            with self.subTest(cause=name):
                self.setUp() if getattr(self, '_used', False) else None
                self._used = True
                self.assertEqual(self.end_state('held', jupiter=FAULTS[name], blocked_exit=True), 'INTEGRITY_HOLD', self.last)

    def test_a_malformed_primary_with_kraken_measured_still_holds_a_blocked_exit(self):
        self.assertEqual(self.end_state('held', wire=MALFORMED['extra mints'], blocked_exit=True), 'INTEGRITY_HOLD', self.last)

    def test_401_without_a_measured_kraken_never_ends_terminal(self):
        self.assertEqual(self.end_state('held', jupiter=FAULTS['401'], kraken=ConnectionResetError), 'INTEGRITY_HOLD', self.last)


class AttemptUnitTests(unittest.TestCase):
    def jupiter(self, **kw):
        return regime_fixtures.jupiter('100', 1000, **kw)

    def kraken(self, **kw):
        return {**regime_fixtures.kraken_attempt('100.00000', 1000), **kw}

    def test_attempt_fault_vocabulary(self):
        failed = lambda code, status=None, rec=None: {**(rec or self.jupiter()), 'failure_code': code, 'http_status': status, 'response_bytes_base64': None}
        self.assertIsNone(uv.attempt_fault(self.jupiter()))
        for code in ('TRANSPORT_ERROR', 'DEADLINE_EXCEEDED', 'RPC_ERROR', 'KRAKEN_PROVIDER_ERROR', 'PACING_DEADLINE_EXCEEDED'):
            self.assertIsNone(uv.attempt_fault(failed(code)), code)
        for status in (408, 429, 500, 503):
            self.assertIsNone(uv.attempt_fault(failed('HTTP_REJECTED', status)), status)
        for code in ('TLS_ERROR', 'UNCLASSIFIED_ERROR', 'WHATEVER_NEW'):
            self.assertIsNotNone(uv.attempt_fault(failed(code)), code)
        for status in (400, 401, 403, 404):
            self.assertIsNotNone(uv.attempt_fault(failed('HTTP_REJECTED', status)), status)
        for status in (401, 403):                                           # the exception: Jupiter only, Kraken measured only
            rejected = failed('HTTP_REJECTED', status)
            self.assertIsNone(uv.attempt_fault(rejected, kraken_measured=True))
            self.assertIsNotNone(uv.attempt_fault(rejected, kraken_measured=False))
            self.assertIsNotNone(uv.attempt_fault(failed('HTTP_REJECTED', status, self.kraken()), kraken_measured=True))
        self.assertIsNotNone(uv.attempt_fault(failed('HTTP_REJECTED', 404), kraken_measured=True))
        self.assertIsNotNone(uv.attempt_fault(failed('TLS_ERROR'), kraken_measured=True))

    def test_a_malformed_answer_is_a_fault_a_merely_stale_one_is_not(self):
        self.assertIsNotNone(uv.attempt_fault(self.jupiter(response_bytes_base64='PGh0bWw+')))          # '<html>'
        self.assertIsNotNone(uv.attempt_fault(self.jupiter(http_status=500)))                          # a body with a non-200 status
        broken = self.kraken(response_bytes_base64='e30=')                                              # '{}'
        self.assertIsNotNone(uv.attempt_fault(broken))
        quiet = self.kraken()                                                                           # the last trade is 40 s old
        import base64
        body = ('{"error":[],"result":{"SOLUSD":[["100.00000","1.00000",%d.25,"s","l","",123]],"last":"123000000000"}}' % (1000 - 40)).encode()
        quiet['response_bytes_base64'] = base64.b64encode(body).decode()
        self.assertIsNone(uv.attempt_fault(quiet))

    def test_verdicts_are_per_scan_so_one_scans_kraken_never_excuses_anothers_rejection(self):
        records = {
            'j-a': {**self.jupiter(scan_id='a'), 'failure_code': 'HTTP_REJECTED', 'http_status': 401, 'response_bytes_base64': None},
            'k-a': self.kraken(scan_id='a'),
            'j-b': {**self.jupiter(scan_id='b'), 'failure_code': 'HTTP_REJECTED', 'http_status': 401, 'response_bytes_base64': None},
            'k-b': {**self.kraken(scan_id='b'), 'failure_code': 'TRANSPORT_ERROR', 'response_bytes_base64': None}}
        self.assertEqual(uv.faulted_scans(records.__getitem__, list(records)), ['b'])
        self.assertEqual(uv.faulted_scans(records.__getitem__, ['j-a', 'k-a']), [])

    def test_unreadable_and_foreign_refs_are_skipped_not_trusted(self):
        records = {'x': {'kind': 'paper_read_attempt_v1', 'scan_id': 'a', 'source_id': 'helius-mainnet-paper-confirmed-v1', 'method': 'getSlot'}}
        def load(ref):
            if ref == 'gone':
                raise ValueError('unreadable')
            return records[ref]
        self.assertEqual(uv.faulted_scans(load, ['gone', 'x']), [])

    def test_the_cited_failure_check_is_transient_only_and_takes_the_measured_flag(self):
        failed = {**self.jupiter(), 'failure_code': 'TLS_ERROR', 'http_status': None, 'response_bytes_base64': None}
        result = {'usd_evidence_refs': ['k']}
        self.assertFalse(no_entry._cited_usd_failure('k', failed, result, 's'))
        self.assertFalse(no_entry._cited_usd_failure('k', failed, result, 's', kraken_measured=True))
        transient = {**failed, 'failure_code': 'TRANSPORT_ERROR'}
        self.assertTrue(no_entry._cited_usd_failure('k', transient, result, 's'))
        rejected = {**failed, 'failure_code': 'HTTP_REJECTED', 'http_status': 403}
        self.assertFalse(no_entry._cited_usd_failure('k', rejected, result, 's'))
        self.assertTrue(no_entry._cited_usd_failure('k', rejected, result, 's', kraken_measured=True))

    def test_the_fault_blocker_is_never_a_normal_rejection_even_if_declared(self):
        self.assertFalse(no_entry.producer_blocker_is_normal('SOL_USD_SOURCE_FAULT'))
        self.assertFalse(no_entry.producer_blocker_is_normal('SOL_USD_SOURCE_FAULT', ('SOL_USD_SOURCE_FAULT',)))
        self.assertTrue(no_entry.producer_blocker_is_normal('SOL_USD_UNAVAILABLE'))
        self.assertTrue(no_entry.producer_blocker_is_normal('USD_SOURCE_DIVERGENCE'))
        self.assertNotIn('SOL_USD_SOURCE_FAULT', uv.NORMAL_BLOCKERS)


class RegimeSourceTests(unittest.TestCase):
    """T37G item 2: when the valuation used the Kraken fallback the regime reads THAT series."""
    setUp = regime_fixtures.RegimeSeriesTests.setUp
    put_series = regime_fixtures.RegimeSeriesTests.put_series

    def test_the_kraken_fallback_valuation_reads_the_kraken_series_not_a_stale_jupiter_price(self):
        self.put_series(regime_fixtures.jupiter, [(2600, '100'), (700, '90')])           # Jupiter: 700 s old, inside the 900 s TTL, span 1900 s
        self.put_series(regime_fixtures.kraken_attempt, [(2000, '100.00000'), (6, '102.00000')])
        self.assertEqual(producer.sol_usd_change_pct(self.store.path, regime_fixtures.TS, regime_fixtures.TTL, 2), ('-10.0000', regime_fixtures.TS - 700))
        self.assertEqual(producer.sol_usd_change_pct(self.store.path, regime_fixtures.TS, regime_fixtures.TTL, 2, 'KRAKEN'), ('2.0000', regime_fixtures.TS - 6))
        evidence = producer.evidence(self.research, self.store.path, regime_fixtures.TS, regime_fixtures.TTL, valuation_version=2, source='KRAKEN')
        self.assertEqual((evidence['sol_usd_change_pct'], evidence['as_of']), ('2.0000', regime_fixtures.TS - 6))

    def test_a_kraken_fallback_without_enough_kraken_history_gives_no_regime_never_the_jupiter_series(self):
        self.put_series(regime_fixtures.jupiter, [(2600, '100'), (700, '90')])
        self.put_series(regime_fixtures.kraken_attempt, [(6, '102.00000')])               # no span of its own
        self.assertEqual(producer.sol_usd_change_pct(self.store.path, regime_fixtures.TS, regime_fixtures.TTL, 2, 'KRAKEN'), (None, None))
        self.assertIsNone(producer.evidence(self.research, self.store.path, regime_fixtures.TS, regime_fixtures.TTL, valuation_version=2, source='KRAKEN'))

    def test_the_default_source_and_version_one_are_unchanged(self):
        self.put_series(regime_fixtures.jupiter, [(2400, '100'), (5, '98')])
        self.assertEqual(producer.sol_usd_change_pct(self.store.path, regime_fixtures.TS, regime_fixtures.TTL, 2, 'JUPITER'), ('-2.0000', regime_fixtures.TS - 5))
        self.assertEqual(producer.sol_usd_change_pct(self.store.path, regime_fixtures.TS, regime_fixtures.TTL, 2), ('-2.0000', regime_fixtures.TS - 5))
        self.assertEqual(producer.sol_usd_change_pct(self.store.path, regime_fixtures.TS, regime_fixtures.TTL, 1), (None, None))


class RegimeCycleTests(Probe):
    """The cycle hands the regime producer the source the valuation actually used."""
    extra = {'paper_regime_version': 1}

    def entry_sources(self, **failures):
        seen = []

        def fake(research, evidence, ts, ttl, **kw):
            seen.append(kw)
            return {'version': 1, 'as_of': ts, 'graduations_per_hour': '60', 'sol_usd_change_pct': '1'}
        for name, value in failures.items():
            setattr(self.h, name, value)
        with patch.object(producer, 'evidence', fake):
            result = self.run_cycle()
        return seen, result

    def test_a_primary_valuation_asks_for_the_jupiter_series(self):
        seen, result = self.entry_sources()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertTrue(seen)
        self.assertEqual({tuple(sorted(kw.items())) for kw in seen}, {(('source', 'JUPITER'), ('valuation_version', 2))})

    def test_a_kraken_fallback_valuation_asks_for_the_kraken_series(self):
        seen, result = self.entry_sources(jupiter_fail=True)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertTrue(seen)
        self.assertEqual({tuple(sorted(kw.items())) for kw in seen}, {(('source', 'KRAKEN'), ('valuation_version', 2))})


if __name__ == '__main__':
    unittest.main()
