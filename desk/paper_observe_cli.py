"""One explicit bounded observation pass; no paper engine, entries or retries.

Operator target files select diagnostic reads, never authorize trades or prove
ledger positions. Existing acquisition jobs/admissions are required. Lock order:
research worker, evidence invocation. Price parsing is limited to 64 KiB;
account/quote transport is limited to 2 MiB by PaperReadSources. USD is valuation
only. Source hashes retain originals, not independent provider authentication.
"""
import argparse
import base64
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid
from contextlib import contextmanager

from .evidence import EvidenceStore
from .history_progress import HistoryProgress, canonical_ownership_path, ownership_lock_path
from .job_persistence import JobPersistence, canonical_job_path
from .model import digest
from .paper_observation_collector import ObservationTarget, BoundedSource, collect_observations, _admission, _Blocked
from .paper_read_sources import PaperReadSources, PaperReadError
from .providers import SOL
from .sol_usd_observation import JupiterPriceResponse, TrustedSlotBounds, PRICE_URL, MAX_RESPONSE_BYTES, parse_sol_usd


def load_targets(path):
    with open(path, 'rb') as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError('Target file exceeds 64 KiB')
    value = json.loads(raw)
    if type(value) is not dict or set(value) != {'open_positions', 'candidates'}:
        raise ValueError('Explicit observation target lists required')
    fields = {'scan_id', 'mint', 'pool', 'taker', 'amount_raw'}
    if any(type(value[k]) is not list for k in value) or sum(map(len, value.values())) > 18:
        raise ValueError('At most 18 observation targets permitted')
    output = {}
    for key, rows in value.items():
        if any(type(row) is not dict or set(row) != fields for row in rows):
            raise ValueError('Exact observation target shape required')
        output[key] = tuple(ObservationTarget(**row) for row in rows)
    return output


def _attempt_record(progress, key, scan, source_id, method, params):
    record = progress.store.load(key)
    if (record.get('kind') != 'paper_read_attempt_v1' or record.get('scan_id') != scan
            or record.get('source_id') != source_id or record.get('method') != method
            or record.get('params') != params or record.get('failure_code') is not None
            or record.get('http_status') != 200 or type(record.get('observed_at')) is not int):
        raise ValueError('Original attempt binding invalid')
    return record


@contextmanager
def _worker_lock(path):
    # Take the existing canonical worker lock without initializing or recovering
    # the research queue: observation does not own dispatch recovery.
    with open(canonical_job_path(str(path)+'.jobs-worker.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        yield lock


def _existing_context(research, evidence, targets):
    """Read-only admission check; never migrate either supplied database."""
    jobs = JobPersistence.__new__(JobPersistence)
    jobs.path = research
    def readonly_jobs():
        connection = sqlite3.connect(research.as_uri()+'?mode=ro', uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    jobs.connect = readonly_jobs
    store = EvidenceStore(evidence, read_only=True)
    progress = HistoryProgress.__new__(HistoryProgress)
    progress.store = store
    with store.connect() as c:
        c.execute('SELECT hash,payload,raw_bytes FROM pages LIMIT 0')
        c.execute('SELECT id,budget,query,coverage,status,attempts FROM ownership_history LIMIT 0')
        c.execute('SELECT id,descriptor,state,prepared_source,prepared_used,completed_source_hash FROM ownership_admissions LIMIT 0')
        c.execute('SELECT id,source_hash,used,ceiling FROM ownership_budgets LIMIT 0')
    with jobs.connect() as c:
        c.execute('SELECT id,mint,created,status,result FROM scans LIMIT 0')
        c.execute('SELECT scan_id,kind,descriptor_version,descriptor,descriptor_hash,generation,claim_token FROM scan_jobs LIMIT 0')
        marker = c.execute("SELECT version FROM scan_job_migrations WHERE name='legacy_screen_descriptors'").fetchone()
        if marker is None or marker[0] != 1:
            raise ValueError('Existing dispatch schema required')
    for target in targets:
        _admission(jobs, progress, target)
    # No constructors that initialize schema, even after successful preflight.
    store.read_only = False
    return jobs, store, progress


def observe(research_db, evidence_db, *, open_positions=(), candidates=(), sol_usd=False):
    """Save observations only. Existing IDs/counters are never created or reset.

    All supplied position reads precede candidates. Any position failure prevents
    candidate reads; an incomplete/ambiguous pass never starts USD reads. A manual
    invocation is not an automatic recovery/resume or a continuous monitor.
    """
    if (type(sol_usd) is not bool or type(open_positions) is not tuple or type(candidates) is not tuple
            or len(open_positions) + len(candidates) > 18
            or any(type(t) is not ObservationTarget for t in open_positions + candidates)):
        raise ValueError('Invalid observation targets')
    research, evidence = canonical_job_path(research_db), canonical_ownership_path(evidence_db)
    if research == evidence or not research.is_file() or not evidence.is_file():
        raise ValueError('Separate existing research and evidence databases required')
    output = {'kind': 'paper_observation_pass_v1', 'status': 'UNKNOWN', 'mode': 'OBSERVATION_ONLY',
              'eligible_for_trading': False, 'entries_enabled': False, 'quote_is_fill': False,
              'attempted_requests': 0, 'stopped_reason': None, 'observations': [],
              'sol_usd': {'status': 'UNKNOWN', 'blockers': ['NOT_REQUESTED'], 'evidence_refs': []},
              'requested_scan_ids': [t.scan_id for t in open_positions + candidates],
              'limits': {'pass_requests': 18, 'deadline_seconds': 10, 'price_bytes': 65536,
                         'account_quote_bytes': 2097152}}
    with _worker_lock(research) as worker:
        if worker is None:
            output['stopped_reason'] = 'RESEARCH_WORKER_BUSY'
            return output
        store = EvidenceStore(evidence, read_only=True)
        with open(ownership_lock_path(store, invocation=True), 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                output['stopped_reason'] = 'EVIDENCE_INVOCATION_BUSY'
                return output
            try:
                jobs, store, progress = _existing_context(research, evidence, open_positions+candidates)
            except (_Blocked, ValueError, sqlite3.Error):
                output['stopped_reason'] = 'PERSISTED_ADMISSION_REQUIRED'
                return output
            # Persist intent before any possible reservation/I/O. An interrupted
            # invocation or failed pass remains unresolved across processes and
            # target changes. There is deliberately no reset/clear CLI: recovery
            # requires a separate source-bound reconciliation, not a healthy retry.
            with store.connect() as c:
                c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            from .paper_terminal_reconciliation import gate
            try:
                blocked=gate(store,research,tuple(t.scan_id for t in open_positions+candidates))
            except (ValueError,TypeError,KeyError,OSError,sqlite3.Error):
                blocked='OBSERVATION_RECOVERY_REQUIRED'
            if blocked:
                output['stopped_reason']=blocked
                return output
            intent = {'kind': 'paper_observation_intent_v1', 'research_db': str(research),
                      'evidence_db': str(evidence), 'targets': [vars(t) for t in open_positions+candidates],
                      'sol_usd': sol_usd, 'admissions': {t.scan_id: progress.admission(t.scan_id)
                                                     for t in open_positions+candidates}}
            intent_hash = store.save(intent)
            invocation_id = uuid.uuid4().hex
            with store.connect() as c:
                c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', (invocation_id,intent_hash))
            started = last = time.monotonic()
            if type(started) not in (int, float) or not math.isfinite(started):
                raise ValueError('Invalid monotonic clock')
            def remaining():
                nonlocal last
                now = time.monotonic()
                if type(now) not in (int, float) or not math.isfinite(now) or now < last or now >= started + 10:
                    raise ValueError('Pass deadline unavailable or exceeded')
                last = now
                return started + 10 - now
            readers = {t.scan_id: PaperReadSources(progress, t.scan_id) for t in open_positions + candidates}
            sources = {scan: BoundedSource(s.rpc_source_id, s.quote_source_id, s.rpc, s.quote)
                       for scan, s in readers.items()}
            for positions, buys in ((open_positions, ()), ((), candidates)):
                if not positions and not buys:
                    continue
                try:
                    result = collect_observations(jobs=jobs, progress=progress, sources=sources,
                        open_positions=positions, candidates=buys,
                        request_ceiling=18-output['attempted_requests'], deadline_seconds=remaining(), max_age_seconds=10,
                        wall_clock=time.time, monotonic=time.monotonic)
                except ValueError:
                    output['stopped_reason'] = 'PASS_LIMIT_OR_CLOCK_UNAVAILABLE'
                    break
                output['attempted_requests'] += result.attempted_requests
                for row in result.observations:
                    output['observations'].append({'scan_id': row.target.scan_id, 'mint': row.target.mint,
                        'pool': row.target.pool, 'direction': row.direction, 'amount_raw': row.target.amount_raw,
                        'failure': row.failure, 'evidence_refs': list(row.evidence_refs),
                        'reserve_sol': str(row.pool.reserve_sol) if row.pool else None,
                        'reserve_tokens': str(row.pool.reserve_tokens) if row.pool else None,
                        'pool_observed_at': row.pool.source.observed_at if row.pool else None,
                        'estimated_output_raw': row.quote.estimated_output_raw if row.quote else None,
                        'minimum_output_raw': row.quote.minimum_output_raw if row.quote else None,
                        'quote_observed_at': row.quote.source.observed_at if row.quote else None})
                failures = [r.failure for r in result.observations if r.failure]
                if result.stopped_reason or failures:
                    output['stopped_reason'] = result.stopped_reason or failures[0]
                    break
                if len(result.observations) != len(positions) + len(buys):
                    output['stopped_reason'] = 'OBSERVATIONS_INCOMPLETE'
                    break
            targets = open_positions + candidates
            if sol_usd and targets and output['stopped_reason'] is None:
                refs = output['sol_usd']['evidence_refs']
                scan = targets[0].scan_id; source = readers[scan]
                def call(invoke):
                    if output['attempted_requests'] >= 18:
                        raise ValueError('Pass request ceiling')
                    before = progress.admission(scan)
                    if before is None or before['requests_used'] >= before['request_ceiling']:
                        raise ValueError('Shared request budget exhausted')
                    timeout = remaining()
                    output['attempted_requests'] += 1
                    result = invoke(timeout)
                    refs.append(result['evidence_hash'] if type(result) is dict else result[1])
                    if progress.admission(scan)['requests_used'] != before['requests_used'] + 1:
                        raise ValueError('Shared charge mismatch')
                    remaining()
                    return result
                try:
                    price = call(lambda timeout: source.sol_price(timeout_seconds=timeout))
                    original = _attempt_record(progress, refs[-1], scan, source.price_source_id, 'jupiter_price_v3', {'ids': SOL})
                    raw = base64.b64decode(original['response_bytes_base64'], validate=True)
                    if len(raw) > MAX_RESPONSE_BYTES or original['observed_at'] != price['observed_at'] or original['http_status'] != price['http_status']:
                        raise ValueError('Price source binding invalid')
                    slot, key = call(lambda timeout: source.rpc_with_evidence('getSlot', [{'commitment': 'finalized'}], timeout_seconds=timeout))
                    capture = _attempt_record(progress, key, scan, source.rpc_source_id, 'getSlot', [{'commitment': 'finalized'}])
                    item = price['response'].get(SOL)
                    block = item.get('blockId') if type(item) is dict else None
                    lower = max(0, slot-32)
                    # Obtain an actual time, never interpolate. Invalid/outside
                    # price blocks use the captured slot solely for bounds; the
                    # existing parser still rejects their price identity.
                    witness = block if type(block) is int and lower <= block <= slot else slot
                    block_time, key = call(lambda timeout: source.rpc_with_evidence('getBlockTime', [witness], timeout_seconds=timeout))
                    _attempt_record(progress, key, scan, source.rpc_source_id, 'getBlockTime', [witness])
                    if type(block_time) is not int or block_time < 0:
                        raise ValueError('Exact block time unavailable')
                    now = time.time()
                    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
                        raise ValueError('Wall clock unavailable')
                    parsed = parse_sol_usd(JupiterPriceResponse('GET', PRICE_URL, raw, original['observed_at'], original['http_status']),
                        bounds=TrustedSlotBounds(int(now), capture['observed_at'], lower, slot, ((witness, block_time),)))
                    output['sol_usd'].update(status=parsed.status, blockers=list(parsed.blockers),
                        usd_price=str(parsed.usd_price) if parsed.usd_price is not None else None,
                        price_at=parsed.price_at, block_id=parsed.block_id, payload_sha256=parsed.payload_sha256)
                except PaperReadError as error:
                    if error.evidence_hash:
                        refs.append(error.evidence_hash)
                    output['stopped_reason'] = error.code
                    output['sol_usd']['blockers'] = [error.code]
                except ValueError:
                    output['stopped_reason'] = 'USD_BINDING_BUDGET_OR_CLOCK_UNAVAILABLE'
                    output['sol_usd']['blockers'] = [output['stopped_reason']]
            if output['observations'] and output['stopped_reason'] is None:
                output['status'] = 'OBSERVED'
            if sol_usd and output['sol_usd']['status'] != 'MEASURED':
                output['status'] = 'UNKNOWN'
            key = store.save(output)
            if key != digest(output):
                raise ValueError('Observation persistence failed')
            # Only an unambiguously completed pass permits a subsequent pass.
            # Budget/validation stops without any requests are also complete;
            # failures after a charged attempt remain conservatively latched.
            if output['stopped_reason'] is None or all(
                    progress.admission(scan) == admission for scan, admission in intent['admissions'].items()):
                with store.connect() as c:
                    c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND outcome_hash IS NULL',
                              (key,invocation_id))
            return {**output, 'evidence_hash': key}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--research-db', required=True)
    parser.add_argument('--evidence-db', required=True)
    parser.add_argument('--targets', required=True, help='Operator diagnostic targets; not entry authorization')
    parser.add_argument('--sol-usd', action='store_true', help='Three additional charged valuation/witness reads')
    args = parser.parse_args(argv)
    try:
        targets = load_targets(args.targets)
        result = observe(args.research_db, args.evidence_db, **targets, sol_usd=args.sol_usd)
    except (ValueError, OSError, sqlite3.Error, RecursionError):
        result = {'status': 'UNKNOWN', 'stopped_reason': 'OBSERVATION_UNAVAILABLE',
                  'eligible_for_trading': False, 'entries_enabled': False}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'OBSERVED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
