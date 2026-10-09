"""Bounded coordinator collection; no default transport, retries or admission creation.

Inject trusted one-I/O wrappers that reserve HistoryProgress's shared counter
BEFORE transport and honor timeout_seconds. This module checks the existing job
and admission binding and verifies exactly one persisted charge per invocation;
that check does not authenticate a transport or prove when it reserved. Legacy
SCREEN jobs are unsupported. The coordinator must latch unresolved ambiguous
failures outside this pass before explicitly scheduling another pass; no new
persistent failure state is introduced here. No journal, budget reset, quote-as-fill or event gate.
PR104 ingestion is a prerequisite. All original successful responses are saved in
its existing evidence store before normalization; error strings are never retained.
"""
from dataclasses import dataclass
import math
import json
import re
import time
from typing import Callable

from .history_progress import HistoryProgress, canonical_ownership_path
from .job_persistence import JobPersistence, BIRTH_ACQUISITION_V1
from .live_observation import (ProviderObservation, MintObservation, PoolObservation,
                               QuoteObservation, ingest_mint, ingest_pool, ingest_quote)
from .model import canonical, digest
from .pools import verify_pool
from .programs import address
from .providers import SOL


@dataclass(frozen=True)
class ObservationTarget:
    scan_id: str
    mint: str
    pool: str
    taker: str
    amount_raw: int  # candidate SOL lamports, or exact open-position token quantity


@dataclass(frozen=True)
class BoundedSource:
    rpc_source_id: str  # out-of-band identities, never target fields
    quote_source_id: str
    rpc: Callable  # (method, params, *, timeout_seconds)
    quote: Callable  # (input_mint, output_mint, amount, taker, *, timeout_seconds)


@dataclass(frozen=True)
class TargetObservation:
    target: ObservationTarget
    direction: str
    mint: MintObservation | None
    pool: PoolObservation | None
    quote: QuoteObservation | None
    failure: str | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class CollectionResult:
    observations: tuple[TargetObservation, ...]
    attempted_requests: int
    stopped_reason: str | None
    missing_sources: tuple[str, ...] = ('SOL_USD_PRICE_SOURCE_MISSING',)


class _Blocked(Exception):
    def __init__(self, code, stop=False):
        self.code, self.stop = code, stop


def _admission(jobs, progress, target):
    try:
        descriptor = jobs.descriptor(target.scan_id)
        admission = progress.admission(target.scan_id)
        if (descriptor['kind'] != BIRTH_ACQUISITION_V1 or descriptor['mint'] != target.mint
                or descriptor['evidence_db'] != str(canonical_ownership_path(progress.store.path))
                or admission is None or admission['state'] not in ('ADMITTED', 'SEALED')
                or admission['descriptor'] != {'kind': 'ownership_admission_v1', 'scan_id': target.scan_id,
                                               'mint': target.mint, 'created': descriptor['admitted_at']}
                or admission['request_ceiling'] != descriptor['request_ceiling']):
            raise ValueError()
        source = jobs.source(target.scan_id)
        if admission['state'] == 'SEALED' and source != admission['prepared_source']:
            raise ValueError()
        if (source['status'] not in ('QUEUED', 'RUNNING', 'INTERRUPTED', 'COMPLETE')
                or (source['status'] == 'COMPLETE' and admission['state'] != 'SEALED')):
            raise ValueError()
        return admission
    except Exception:
        raise _Blocked('PERSISTED_ADMISSION_REQUIRED') from None


def collect_observations(*, jobs: JobPersistence, progress: HistoryProgress,
                         sources: dict[str, BoundedSource],
                         open_positions: tuple[ObservationTarget, ...] = (),
                         candidates: tuple[ObservationTarget, ...] = (),
                         request_ceiling: int = 18, deadline_seconds: float = 10,
                         max_age_seconds: int = 10, wall_clock=time.time,
                         monotonic=time.monotonic) -> CollectionResult:
    """Open-position sell observations precede all candidate buy reads.

    sources is a trusted coordinator map keyed by existing scan ID. Candidate JSON
    cannot choose source identity or issue an admission. Each wrapper must perform
    exactly one charged I/O, with no internal retry/fallback/discovery. Timeout is
    cooperative: a callable ignoring it cannot be preempted by this synchronous
    collector; late responses are saved but rejected, and no later I/O is allowed.
    At most 18 input targets and 18 wrapper invocations per pass are allowed.
    Returned quotes/spot marks never assert fill or event eligibility. SOL/USDC
    quotes alone cannot establish USD valuation; no peg is assumed.
    """
    if (type(jobs) is not JobPersistence or type(progress) is not HistoryProgress
            or type(request_ceiling) is not int or not 1 <= request_ceiling <= 18
            or type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= 60 or type(max_age_seconds) is not int
            or not 0 <= max_age_seconds <= 60
            or type(open_positions) not in (tuple, list) or type(candidates) not in (tuple, list)
            or len(open_positions) + len(candidates) > 18):
        raise ValueError('Invalid trusted collector configuration')
    started = monotonic(); last_tick = started
    if type(started) not in (int, float) or not math.isfinite(started):
        raise ValueError('Invalid monotonic clock')
    deadline = started + deadline_seconds
    attempted = 0; stopped = None; results = []

    def remaining():
        nonlocal last_tick
        now = monotonic()
        if type(now) not in (int, float) or not math.isfinite(now) or now < last_tick:
            raise _Blocked('MONOTONIC_CLOCK_INVALID', True)
        last_tick = now
        if now >= deadline: raise _Blocked('PASS_DEADLINE_EXCEEDED', True)
        return deadline - now

    def timestamp():
        value = wall_clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise _Blocked('ACQUISITION_CLOCK_INVALID', True)
        return int(value)

    for target, direction in [(t, 'sell') for t in open_positions] + [(t, 'buy') for t in candidates]:
        token = pool = quote = None; refs = []; failure = None
        try:
            remaining()
            if type(target) is not ObservationTarget: raise _Blocked('TARGET_INVALID')
            for key in (target.mint, target.pool, target.taker): address(key)
            if type(target.amount_raw) is not int or not 0 < target.amount_raw < 2**64:
                raise _Blocked('TARGET_AMOUNT_INVALID')
            _admission(jobs, progress, target)
            source = sources.get(target.scan_id)
            if (type(source) is not BoundedSource or not all(isinstance(identity, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', identity)
                            for identity in (source.rpc_source_id, source.quote_source_id))
                    or not callable(source.rpc) or not callable(source.quote)):
                raise _Blocked('TRUSTED_SOURCE_REQUIRED')

            def request(kind, params, invoke):
                nonlocal attempted
                timeout = remaining()
                if attempted >= request_ceiling: raise _Blocked('PASS_REQUEST_CEILING', True)
                before = _admission(jobs, progress, target)
                if before['requests_used'] >= before['request_ceiling']:
                    raise _Blocked('SHARED_REQUEST_BUDGET_EXHAUSTED')
                original_params = json.loads(canonical(params))
                at = timestamp()
                timeout = remaining()  # admission/storage lookups may have consumed the deadline
                attempted += 1
                try:
                    response = invoke(timeout)
                except Exception:
                    # Ambiguous failure: never retry, continue this target, or
                    # start another target in this pass. No exception content.
                    raise _Blocked('SOURCE_REQUEST_FAILED', True) from None
                clock_failure = None
                try:
                    completed = timestamp()
                except _Blocked as error:
                    completed = None; clock_failure = error
                if completed is not None and completed < at:
                    clock_failure = _Blocked('ACQUISITION_CLOCK_REGRESSED', True)
                envelope = {'source_id': source.quote_source_id if kind == 'jupiter_probe' else source.rpc_source_id, 'acquired_at': completed,
                            'started_at': at, 'method': kind, 'params': original_params, 'result': response}
                try:
                    key = progress.store.save(envelope)
                    if key != digest(envelope): raise ValueError()
                    refs.append(key)
                except Exception:
                    raise _Blocked('ORIGINAL_RESPONSE_PERSISTENCE_FAILED', True) from None
                if clock_failure is not None: raise clock_failure
                if params != original_params: raise _Blocked('SOURCE_REQUEST_MUTATED', True)
                after = _admission(jobs, progress, target)
                if after['requests_used'] != before['requests_used'] + 1:
                    raise _Blocked('SHARED_BUDGET_CHARGE_MISMATCH', True)
                remaining()  # retain late raw evidence, but do not use it
                if completed - at > max_age_seconds:
                    raise _Blocked('SOURCE_RESPONSE_STALE', True)
                return response, completed, envelope

            pool_acquisitions = []
            def rpc(method, params):
                result, acquired_at, _ = request(method, params, lambda timeout: source.rpc(method, params,
                                                timeout_seconds=timeout))
                pool_acquisitions.append(acquired_at)
                return result

            mint_params = [target.mint, {'encoding': 'base64', 'commitment': 'confirmed'}]
            raw, at, envelope = request('getAccountInfo', mint_params,
                lambda timeout: source.rpc('getAccountInfo', mint_params, timeout_seconds=timeout))
            mint_payload = {'mint': target.mint, 'observed_at': at,
                            'slot': raw.get('context', {}).get('slot'), 'account': raw.get('value'),
                            'original_rpc_observation': envelope}
            token = ingest_mint(lambda: ProviderObservation(source.rpc_source_id, at, mint_payload),
                                mint=target.mint, now=timestamp(), max_age_seconds=max_age_seconds)
            pool_capture = []
            def capture_pool(payload):
                key = progress.store.save(payload)
                if key != digest(payload): raise _Blocked('POOL_CAPTURE_PERSISTENCE_FAILED', True)
                refs.append(key); pool_capture.append(payload)
                return key
            verify_pool(target.pool, target.mint, rpc, capture=capture_pool)
            pool_at = pool_acquisitions[-1]
            pool = ingest_pool(lambda: ProviderObservation(source.rpc_source_id, pool_at, pool_capture[0]),
                               mint=target.mint, pool=target.pool, now=timestamp(), max_age_seconds=max_age_seconds)
            if token.decimals != pool.decimals or pool.slot < token.slot:
                raise _Blocked('MINT_POOL_BANK_MISMATCH', True)
            input_mint, output_mint = (SOL, target.mint) if direction == 'buy' else (target.mint, SOL)
            quote_args = [input_mint, output_mint, target.amount_raw, target.taker]
            raw_quote, at, _ = request('jupiter_probe', quote_args,
                lambda timeout: source.quote(*quote_args, timeout_seconds=timeout))
            quote = ingest_quote(lambda: ProviderObservation(source.quote_source_id, raw_quote.get('observed_at'), raw_quote),
                mint=token, direction=direction, amount_raw=target.amount_raw, taker=target.taker,
                expected_pool=target.pool, now=timestamp(), max_age_seconds=max_age_seconds)
            remaining()
        except _Blocked as error:
            failure = error.code
            if error.stop: stopped = error.code
        except Exception:
            failure = 'SOURCE_CONTENT_REJECTED'
            # Invalid/ambiguous raw content must not trigger a healthy retry.
            stopped = failure
        results.append(TargetObservation(target, direction, token, pool, quote, failure, tuple(refs)))
        if stopped: break
    return CollectionResult(tuple(results), attempted, stopped)
