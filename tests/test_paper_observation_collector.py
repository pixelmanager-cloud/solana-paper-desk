"""Injected synthetic protocol fixture transport only; no provider calls."""
import copy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import JobPersistence, BIRTH_ACQUISITION_V1
from desk.model import canonical, digest
from desk.paper_observation_collector import (ObservationTarget, BoundedSource,
                                              collect_observations)
from desk.providers import SOL


class PaperObservationCollectorTests(unittest.TestCase):
    def setUp(self):
        from tests.test_pools import PoolTests
        self.protocol = PoolTests(); self.protocol.setUp()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.jobs = JobPersistence(root/'research.sqlite')
        self.progress = HistoryProgress(EvidenceStore(root/'evidence.sqlite'))
        self.at = 100000; self.tick = 0.; self.calls = []; self.sources = {}
        self.fail = None; self.charge = True; self.delay = 0; self.bad_quote = False
        self.taker = 'FzULv8pR9Rd7cyVKjVkzmJ1eqEmgwDnzjYyNUcEJtoG9'

    def target(self, quantity=10000000, *, protocol=None):
        protocol = protocol or self.protocol
        # Real existing persisted interfaces, not a fabricated boolean admission.
        mint = str(protocol.mint)
        with patch('desk.job_persistence.time.time', return_value=self.at):
            scan = self.jobs.admit(mint, kind=BIRTH_ACQUISITION_V1, evidence_db=self.progress.store.path)
        descriptor = self.jobs.descriptor(scan)
        self.progress.admit(scan, {'kind': 'ownership_admission_v1', 'scan_id': scan,
                                  'mint': mint, 'created': descriptor['admitted_at']})
        def rpc(method, params, *, timeout_seconds):
            self.before_io(scan, method, params, timeout_seconds)
            if method == 'getAccountInfo' and params[0] == mint:
                return {
                    'context': {'slot': 100}, 'value': copy.deepcopy(protocol.rpc('getMultipleAccounts', [])['value'][6]),
                    'unknown_rpc_field': {'retained': True}}
            return copy.deepcopy(protocol.rpc(method, params))
        def quote(input_mint, output_mint, amount, taker, *, timeout_seconds):
            self.before_io(scan, 'quote', [input_mint, output_mint, amount, taker], timeout_seconds)
            output = 2000000 if input_mint == SOL else 9000000
            response = {'inputMint': input_mint, 'outputMint': output_mint, 'inAmount': str(amount),
                        'outAmount': str(output), 'otherAmountThreshold': str(output-1),
                        'swapMode': 'ExactIn', 'slippageBps': 100,
                        'routePlan': [{'percent': 100, 'swapInfo': {'ammKey': str(protocol.pool),
                            'inputMint': input_mint, 'outputMint': output_mint,
                            'inAmount': str(amount), 'outAmount': str(output)}}],
                        'unknown_quote_field': ['unchanged', None]}
            if self.bad_quote: response['inAmount'] = '1'
            return {'kind': 'unsigned_route_probe', 'observed_at': self.at, 'response': response,
                    'request': {'inputMint': input_mint, 'outputMint': output_mint, 'amount': str(amount),
                                'taker': taker, 'slippageBps': '100'}}
        self.sources[scan] = BoundedSource('fixture-protocol-rpc', 'fixture-protocol-quote', rpc, quote)
        return ObservationTarget(scan, mint, str(protocol.pool), self.taker, quantity)

    def before_io(self, scan, method, params, timeout):
        self.assertGreater(timeout, 0)
        if self.charge: self.assertTrue(self.progress.reserve(scan))
        self.calls.append((scan, method, copy.deepcopy(params), timeout))
        self.tick += self.delay
        if method == self.fail: raise OSError('SECRET_KEY=https://secret.invalid/?api-key=do-not-leak')

    def collect(self, **kwargs):
        return collect_observations(jobs=self.jobs, progress=self.progress, sources=self.sources,
            wall_clock=lambda: self.at, monotonic=lambda: self.tick, **kwargs)

    def second_target(self, first, amount=10000000):
        # A complete persisted job permits another daily admission for its mint.
        source = self.jobs.source(first.scan_id)
        report = {'mint': first.mint, 'eligible_for_trading': False, 'calls': 0}
        report['report_hash'] = digest(report)
        source.update(status='COMPLETE', result=canonical(report))
        admission = self.progress.admission(first.scan_id)
        self.progress.prepare_source(first.scan_id, admission['descriptor_hash'], source)
        self.progress.seal_source(first.scan_id, admission['descriptor_hash'], digest(source))
        with self.jobs.connect() as c:
            c.execute("UPDATE scans SET status='COMPLETE',result=? WHERE id=?", (source['result'], first.scan_id))
        return self.target(amount)

    def test_usable_open_position_before_candidate_exact_units_and_provenance(self):
        position = self.target(1234567); candidate = self.second_target(position)
        result = self.collect(open_positions=(position,), candidates=(candidate,))
        self.assertEqual(result.attempted_requests, 8); self.assertIsNone(result.stopped_reason)
        self.assertEqual([c[0] for c in self.calls], [position.scan_id]*4+[candidate.scan_id]*4)
        sell, buy = result.observations
        self.assertIsNone(sell.failure); self.assertIsNone(buy.failure)
        self.assertEqual(sell.quote.input_raw, 1234567)
        self.assertEqual(sell.quote.input_units, Decimal('1.234567'))
        self.assertEqual(buy.quote.input_raw, 10000000)
        self.assertEqual(buy.pool.reserve_sol, Decimal('0.001'))
        self.assertEqual(buy.quote.source.source_id, self.sources[candidate.scan_id].quote_source_id)
        self.assertIsNone(buy.quote.executable_fill_proof)
        self.assertEqual(result.missing_sources, ('SOL_USD_PRICE_SOURCE_MISSING',))
        originals = [self.progress.store.load(k) for k in buy.evidence_refs]
        self.assertTrue(originals[0]['result']['unknown_rpc_field']['retained'])
        self.assertEqual(originals[-1]['result']['response']['unknown_quote_field'], ['unchanged', None])
        self.assertEqual(originals[-1]['acquired_at'], self.at)
        self.assertEqual(self.progress.admission(candidate.scan_id)['requests_used'], 4)

    def test_no_candidate_io_without_accepted_persisted_admission(self):
        target = self.target()
        with self.progress.store.connect() as c: c.execute('DELETE FROM ownership_admissions')
        result = self.collect(candidates=(target,))
        self.assertEqual(result.attempted_requests, 0); self.assertEqual(self.calls, [])
        self.assertEqual(result.observations[0].failure, 'PERSISTED_ADMISSION_REQUIRED')

    def test_wrong_store_mint_and_prepared_admission_reject_before_io(self):
        from dataclasses import replace
        target = self.target()
        wrong = replace(target, mint=SOL)
        self.assertEqual(self.collect(candidates=(wrong,)).attempted_requests, 0)
        with self.progress.store.connect() as c:
            c.execute("UPDATE ownership_admissions SET state='PREPARED' WHERE id=?", (target.scan_id,))
        self.assertEqual(self.collect(candidates=(target,)).attempted_requests, 0)
        self.assertEqual(self.calls, [])

    def test_shared_ceiling_survives_repeated_passes_and_cannot_reset(self):
        target = self.target()
        for _ in range(17): self.assertTrue(self.progress.reserve(target.scan_id))
        first = self.collect(candidates=(target,))
        self.assertEqual(first.attempted_requests, 1)
        self.assertEqual(first.observations[0].failure, 'SHARED_REQUEST_BUDGET_EXHAUSTED')
        second = self.collect(candidates=(target,))
        self.assertEqual(second.attempted_requests, 0)
        self.assertEqual(self.progress.admission(target.scan_id)['requests_used'], 18)
        self.assertEqual(len(self.calls), 1)

    def test_per_pass_ceiling_includes_failed_attempts(self):
        target = self.target()
        result = self.collect(candidates=(target,), request_ceiling=2)
        self.assertEqual(result.attempted_requests, 2)
        self.assertEqual(result.stopped_reason, 'PASS_REQUEST_CEILING')
        self.assertEqual(len(self.calls), 2)

    def test_ambiguous_failure_never_retries_or_reads_new_candidate_or_leaks(self):
        position = self.target(1000000); candidate = self.second_target(position)
        self.fail = 'getMultipleAccounts'
        result = self.collect(open_positions=(position,), candidates=(candidate,))
        self.assertEqual(result.attempted_requests, 3)
        self.assertEqual(result.stopped_reason, 'SOURCE_REQUEST_FAILED')
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(self.progress.admission(position.scan_id)['requests_used'], 3)
        self.assertEqual(self.progress.admission(candidate.scan_id)['requests_used'], 0)
        self.assertNotIn('SECRET', repr(result)); self.assertNotIn('secret.invalid', repr(result))
        for key in result.observations[0].evidence_refs:
            self.assertNotIn('SECRET', canonical(self.progress.store.load(key)))

    def test_successful_raw_response_persisted_even_when_deadline_exceeded(self):
        target = self.target(); self.delay = 11
        result = self.collect(candidates=(target,))
        self.assertEqual(result.stopped_reason, 'PASS_DEADLINE_EXCEEDED')
        self.assertEqual(len(self.calls), 1)
        refs = result.observations[0].evidence_refs
        self.assertEqual(len(refs), 1)
        self.assertIn('unknown_rpc_field', self.progress.store.load(refs[0])['result'])

    def test_uncharged_wrapper_and_corrupt_quote_stop_without_retry(self):
        target = self.target(); self.charge = False
        result = self.collect(candidates=(target,))
        self.assertEqual(result.stopped_reason, 'SHARED_BUDGET_CHARGE_MISMATCH')
        self.assertEqual(len(self.calls), 1)
        self.charge = True; self.bad_quote = True; self.calls.clear()
        result = self.collect(candidates=(target,))
        self.assertEqual(result.stopped_reason, 'SOURCE_CONTENT_REJECTED')
        self.assertEqual(len(self.calls), 4)
        self.assertIsNone(result.observations[0].quote)
        self.assertEqual(len(result.observations[0].evidence_refs), 5)

    def test_originals_saved_before_normalization_and_stale_quote_rejected(self):
        target = self.target(); source = self.sources[target.scan_id]
        def stale(*args, **kwargs):
            value = source.quote(*args, **kwargs); value['observed_at'] -= 11; return value
        self.sources[target.scan_id] = BoundedSource(source.rpc_source_id, source.quote_source_id, source.rpc, stale)
        result = self.collect(candidates=(target,))
        self.assertEqual(result.stopped_reason, 'SOURCE_CONTENT_REJECTED')
        self.assertEqual(self.progress.store.load(result.observations[0].evidence_refs[-1])['result']['observed_at'], self.at-11)
        self.assertIsNone(result.observations[0].quote)

    def test_monotonic_regression_and_source_identity_reject(self):
        target = self.target(); values = iter([10, 9])
        result = collect_observations(jobs=self.jobs, progress=self.progress, sources=self.sources,
            candidates=(target,), monotonic=lambda: next(values), wall_clock=lambda: self.at)
        self.assertEqual(result.stopped_reason, 'MONOTONIC_CLOCK_INVALID')
        self.assertEqual(self.calls, [])
        source = self.sources[target.scan_id]
        self.sources[target.scan_id] = BoundedSource('https://secret.invalid/?api-key=x', source.quote_source_id, source.rpc, source.quote)
        result = self.collect(candidates=(target,))
        self.assertEqual(result.attempted_requests, 0)
        self.assertEqual(result.observations[0].failure, 'TRUSTED_SOURCE_REQUIRED')

    def test_original_request_survives_mutation_and_invalid_completion_clock(self):
        target = self.target(); source = self.sources[target.scan_id]
        def mutated(method, params, **kwargs):
            result = source.rpc(method, params, **kwargs)
            params[1]['commitment'] = 'processed'
            return result
        self.sources[target.scan_id] = BoundedSource(source.rpc_source_id, source.quote_source_id, mutated, source.quote)
        result = self.collect(candidates=(target,))
        self.assertEqual(result.stopped_reason, 'SOURCE_REQUEST_MUTATED')
        raw = self.progress.store.load(result.observations[0].evidence_refs[0])
        self.assertEqual(raw['params'][1]['commitment'], 'confirmed')
        self.sources[target.scan_id] = source
        times = iter([self.at, float('nan')])
        result = collect_observations(jobs=self.jobs, progress=self.progress, sources=self.sources,
            candidates=(target,), wall_clock=lambda: next(times), monotonic=lambda: self.tick)
        self.assertEqual(result.stopped_reason, 'ACQUISITION_CLOCK_INVALID')
        raw = self.progress.store.load(result.observations[0].evidence_refs[0])
        self.assertIsNone(raw['acquired_at'])
        self.assertIn('value', raw['result'])

    def test_input_and_policy_bounds_and_inconsistent_completed_source(self):
        target = self.target()
        for kwargs in ({'request_ceiling': 19}, {'deadline_seconds': 0},
                       {'candidates': (target,)*19}, {'max_age_seconds': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError): self.collect(**kwargs)
        with self.jobs.connect() as c:
            c.execute("UPDATE scans SET status='COMPLETE' WHERE id=?", (target.scan_id,))
        result = self.collect(candidates=(target,))
        self.assertEqual(result.attempted_requests, 0)
        self.assertEqual(result.observations[0].failure, 'PERSISTED_ADMISSION_REQUIRED')
        self.assertEqual(self.calls, [])

    def test_deadline_rechecked_after_persisted_admission_before_io(self):
        import desk.paper_observation_collector as module
        target = self.target(); original = module._admission; checks = []
        def slow_admission(*args):
            result = original(*args); checks.append(True)
            if len(checks) == 2: self.tick = 11
            return result
        with patch.object(module, '_admission', side_effect=slow_admission):
            result = self.collect(candidates=(target,))
        self.assertEqual(result.stopped_reason, 'PASS_DEADLINE_EXCEEDED')
        self.assertEqual(result.attempted_requests, 0)
        self.assertEqual(self.calls, [])

    def other_target(self, seed=8):
        # Distinct canonical fixture mint/pool: both jobs can remain ADMITTED
        # through the ordinary daily-queue interface, without forged statuses.
        from tests.test_pools import PoolTests
        from desk.providers import PUMP, PUMPSWAP
        from desk.pools import ATA
        from desk.security import TOKEN_PROGRAM
        protocol = PoolTests(); protocol.setUp(); pubkey = protocol.Pubkey
        protocol.mint = pubkey.from_bytes(bytes([seed])*32)
        protocol.creator = pubkey.find_program_address([b'pool-authority', bytes(protocol.mint)], pubkey.from_string(PUMP))[0]
        protocol.pool, protocol.bump = pubkey.find_program_address([b'pool', bytes(2), bytes(protocol.creator),
            bytes(protocol.mint), bytes(pubkey.from_string(SOL))], pubkey.from_string(PUMPSWAP))
        protocol.lp = pubkey.find_program_address([b'pool_lp_mint', bytes(protocol.pool)], pubkey.from_string(PUMPSWAP))[0]
        protocol.vaults = [pubkey.find_program_address([bytes(protocol.pool), bytes(pubkey.from_string(TOKEN_PROGRAM)),
            bytes(mint)], pubkey.from_string(ATA))[0] for mint in (protocol.mint, pubkey.from_string(SOL))]
        protocol.raw = protocol.raw[:8] + bytes([protocol.bump]) + bytes(2) + b''.join(bytes(key) for key in
            (protocol.creator, protocol.mint, pubkey.from_string(SOL), protocol.lp, *protocol.vaults)) + protocol.raw[203:]
        return self.target(protocol=protocol)

    def prepare_finalizer(self, target):
        source = self.jobs.source(target.scan_id)
        admission = self.progress.admission(target.scan_id)
        report = {'mint': target.mint, 'eligible_for_trading': False, 'calls': admission['requests_used']}
        report['report_hash'] = digest(report)
        source.update(status='COMPLETE', result=canonical(report))
        self.progress.prepare_source(target.scan_id, admission['descriptor_hash'], source)

    def test_completion_clock_exception_keeps_exact_returned_response_and_stops(self):
        position = self.target(1234567); candidate = self.other_target()
        source = self.sources[position.scan_id]; returned = []
        def rpc(*args, **kwargs):
            response = source.rpc(*args, **kwargs); returned.append(copy.deepcopy(response)); return response
        self.sources[position.scan_id] = BoundedSource(source.rpc_source_id, source.quote_source_id, rpc, source.quote)
        def completion_clock():
            if returned: raise OSError('SECRET_COMPLETION_CLOCK=fixture-do-not-retain')
            return self.at
        result = collect_observations(jobs=self.jobs, progress=self.progress, sources=self.sources,
            open_positions=(position,), candidates=(candidate,), wall_clock=completion_clock, monotonic=lambda: self.tick)
        self.assertEqual(result.stopped_reason, 'ACQUISITION_CLOCK_UNAVAILABLE')
        self.assertEqual(result.attempted_requests, 1)
        self.assertEqual(len(self.calls), 1); self.assertEqual(len(result.observations), 1)
        refs = result.observations[0].evidence_refs; self.assertEqual(len(refs), 1)
        raw = self.progress.store.load(refs[0])
        self.assertIsNone(raw['acquired_at']); self.assertEqual(raw['started_at'], self.at)
        self.assertEqual(raw['result'], returned[0])
        self.assertEqual(raw['result']['value']['data'], returned[0]['value']['data'])
        self.assertNotIn('SECRET_COMPLETION_CLOCK', canonical(raw) + repr(result))
        self.assertEqual(self.progress.admission(position.scan_id)['requests_used'], 1)
        self.assertEqual(self.progress.admission(candidate.scan_id)['requests_used'], 0)
        self.assertIsNone(result.observations[0].mint)

    def test_post_io_finalizer_preparation_stops_batch_and_preserves_response(self):
        position = self.target(1234567); candidate = self.other_target()
        source = self.sources[position.scan_id]; returned = []
        def finalized_rpc(*args, **kwargs):
            response = source.rpc(*args, **kwargs); returned.append(copy.deepcopy(response))
            self.prepare_finalizer(position)
            return response
        self.sources[position.scan_id] = BoundedSource(source.rpc_source_id, source.quote_source_id, finalized_rpc, source.quote)
        result = self.collect(open_positions=(position,), candidates=(candidate,))
        self.assertEqual(result.stopped_reason, 'PERSISTED_ADMISSION_REQUIRED')
        self.assertEqual(result.attempted_requests, 1)
        self.assertEqual(len(result.observations), 1); self.assertEqual(len(self.calls), 1)
        raw = self.progress.store.load(result.observations[0].evidence_refs[0])
        self.assertEqual(raw['result'], returned[0])
        self.assertEqual(self.progress.admission(position.scan_id)['state'], 'PREPARED')
        self.assertEqual(self.progress.admission(position.scan_id)['requests_used'], 1)
        self.assertEqual(self.progress.admission(candidate.scan_id)['requests_used'], 0)

    def test_admission_transition_before_next_io_also_stops_batch(self):
        import desk.paper_observation_collector as module
        position = self.target(1234567); candidate = self.other_target()
        original = module._admission; checks = []
        def transition_after_check(*args):
            result = original(*args); checks.append(True)
            if len(checks) == 3: self.prepare_finalizer(position)
            return result
        with patch.object(module, '_admission', side_effect=transition_after_check):
            result = self.collect(open_positions=(position,), candidates=(candidate,))
        self.assertEqual(result.stopped_reason, 'PERSISTED_ADMISSION_REQUIRED')
        self.assertEqual(result.attempted_requests, 1)
        self.assertIsNotNone(result.observations[0].mint)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.progress.admission(candidate.scan_id)['requests_used'], 0)

    def test_initial_no_io_admission_rejection_can_still_collect_approved_target(self):
        missing = self.target(1234567); candidate = self.other_target()
        with self.progress.store.connect() as c:
            c.execute('DELETE FROM ownership_admissions WHERE id=?', (missing.scan_id,))
        result = self.collect(open_positions=(missing,), candidates=(candidate,))
        self.assertIsNone(result.stopped_reason); self.assertEqual(result.attempted_requests, 4)
        self.assertEqual(result.observations[0].failure, 'PERSISTED_ADMISSION_REQUIRED')
        self.assertIsNone(result.observations[1].failure)
        self.assertTrue(all(call[0] == candidate.scan_id for call in self.calls))
        self.assertEqual(self.progress.admission(candidate.scan_id)['requests_used'], 4)

    def test_admission_uncertainty_after_prior_success_blocks_remaining_candidates(self):
        position = self.target(1234567); missing = self.other_target(); later = self.other_target(9)
        with self.progress.store.connect() as c:
            c.execute('DELETE FROM ownership_admissions WHERE id=?', (missing.scan_id,))
        result = self.collect(open_positions=(position,), candidates=(missing, later))
        self.assertEqual(result.stopped_reason, 'PERSISTED_ADMISSION_REQUIRED')
        self.assertEqual(result.attempted_requests, 4); self.assertEqual(len(result.observations), 2)
        self.assertIsNone(result.observations[0].failure)
        self.assertEqual(result.observations[1].failure, 'PERSISTED_ADMISSION_REQUIRED')
        self.assertTrue(all(call[0] == position.scan_id for call in self.calls))
        self.assertEqual(self.progress.admission(later.scan_id)['requests_used'], 0)
