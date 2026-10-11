"""One bounded, explicitly configured paper cycle; no scheduler or live approval.

Trusted caller injects sources/context, holds stable single-host database paths,
reports pending dependency blockers, and never derives permission from event JSON.
Research worker -> evidence invocation -> cycle ledger locks. All reads spend the
original admitted lifetime 18 for candidates. Explicitly provisioned held-position
monitoring uses the separate shared rolling allowance; neither path admits/resets.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass, asdict
import base64
import fcntl
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

from . import engine, quote_execution as qe, paper_concurrency as concurrency, fill_realism, regime
from .graduation_witness import extract_graduation
from .ledger import Ledger
from .model import canonical, digest, validate_event
from .paper_checkpoint import read_checkpoint, RecoveryRequired
from .paper_observe_cli import _worker_lock, _existing_context, _attempt_record
from .history_progress import canonical_ownership_path, ownership_lock_path
from .job_persistence import canonical_job_path
from .paper_observation_collector import ObservationTarget, BoundedSource, collect_observations, TargetObservation, _Blocked
from .paper_read_sources import PaperReadSources, PaperReadError, RPC_ID
from .paper_history_source import PaperHistorySource
from .paper_exit_adapter import ExitContext, build_exit_event
from .paper_market_adapter import MarketContext, build_market_event, OBSERVABLE_FLOW_CHURN_CONCENTRATION_V1
from .live_observation import ProviderObservation, ingest_quote, ingest_mint, ingest_pool, ObservationError
from .monitoring_budget import MonitoringBudget, MonitoringBlocked
from .pools import verify_pool
from . import paper_terminal_reconciliation as terminal
from .token2022_paper import selected
from .providers import SOL
from .original_byte_slot_transport import _parse
from urllib.parse import urlencode
from .sol_usd_observation import JupiterPriceResponse, TrustedSlotBounds, PRICE_URL, MAX_RESPONSE_BYTES

MAX_EVENT_BYTES = 256*1024  # existing quote journal/reader ceiling
MARKER = 'explicit-quote-cycle-v1'
INIT = {'schema_version': 1, 'kind': 'clock', 'event_id': 'paper-cycle:init:v1',
        'ts': 0, 'actor': 'paper_monitor'}


@dataclass(frozen=True)
class CycleTarget:
    target: ObservationTarget
    provenance: str
    graduated_at: int | None
    holder_at: int | None
    pool_fee_bps: str | None
    known_hazards: tuple = ()
    history_as_of: int | None = None
    graduation_refs: tuple = ()  # retained history_request_v1 manifest hashes


class CycleBlocked(ValueError):
    def __init__(self, code, evidence_hash=None):
        self.code = code
        self.evidence_hash = evidence_hash      # retained attempt original of a failed provider read, when there is one
        super().__init__(code)


# T24R F9: the whole-pass read deadline is configurable (opt-in key; absent = the historical 10 s, byte-identical behaviour).
DEADLINE_KEY = 'paper_cycle_deadline_seconds'
DEFAULT_DEADLINE_SECONDS = 10
REQUEST_TIMEOUT_MAX = 15            # desk.paper_read_sources._attempt accepts 0 < timeout <= 15
DEADLINE_RANGE = (10, 20)          # whole seconds; see worst_case_seconds and docs/PERFORMANCE_BUDGETS.md for the arithmetic
# Provider cadences of the paid plans (T36): Helius 0.1 s, Jupiter 0.25 s; the shared Kraken lane stays at 2 s.
PACING_SECONDS = {'helius': 0.1, 'jupiter': 0.25, 'kraken': 2.0}
# Charged provider reads that one cycle may spend after its preparation: the entry reserve of history-first
# (history_preparation_rejection.intent: 9, or 7 with USD valuation v1, one of them the Kraken SOL/USD read) and one held leg.
CYCLE_REQUESTS = {'entry': (8, 1), 'entry_usd_v1': (6, 1), 'held_leg': (5, 1)}      # (non-Kraken reads, Kraken reads)


def deadline_seconds(cfg):
    """The configured whole-pass deadline: absent -> 10; otherwise an int inside DEADLINE_RANGE, else refused at load."""
    if DEADLINE_KEY not in cfg:
        return DEFAULT_DEADLINE_SECONDS
    value = cfg[DEADLINE_KEY]
    if type(value) is not int or not DEADLINE_RANGE[0] <= value <= DEADLINE_RANGE[1]:
        raise ValueError('Cycle deadline outside the reviewed range')
    return value


def worst_case_seconds(pass_type, latency, pacing=PACING_SECONDS):
    """Upper bound of one cycle's provider reads when every read has to wait out its provider cadence AND takes ``latency``.

    Reads are strictly sequential (one process, the shared pacer serialises providers), so each read costs at most its
    provider's cadence plus the response time; the non-Kraken reads are bounded by the slower of Helius/Jupiter."""
    other, kraken = CYCLE_REQUESTS[pass_type]
    return other * (max(pacing['helius'], pacing['jupiter']) + latency) + kraken * (pacing['kraken'] + latency)


def _config(cfg):
    if (type(cfg) is not dict or cfg.get('mode') != 'paper'
            or type(cfg.get('paper_signal_policy_version')) is not int
            or cfg['paper_signal_policy_version'] != 3
            or 'experimental_policy_version' in cfg or not qe.config(cfg)):
        raise ValueError('Explicit signal profile3 and quote execution1 required; no legacy policy field')
    if regime.enabled(cfg):regime.policy(cfg)  # invalid flag/policy refused at load, not at first entry
    deadline_seconds(cfg)


@contextmanager
def _lock(path):
    with open(canonical_job_path(path), 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def initialize(path, cfg):
    """Exclusive new experiment only; never adopt an existing ledger."""
    _config(cfg)
    path = canonical_job_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb'):
        pass
    ledger = Ledger(path, must_exist=True)
    try:
        def bootstrap(state, event, config):
            ledger.db.execute('INSERT INTO metadata VALUES(?,?)', ('paper_cycle', MARKER))
            return engine.transition(state, event, config)
        ledger.apply(INIT, cfg, bootstrap, engine.initial_state)
    finally:
        ledger.close()


def _state(path, cfg):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as c:
        c.execute('BEGIN')
        state = read_checkpoint(c)
        if state is None or c.execute("SELECT value FROM metadata WHERE key='paper_cycle'").fetchone() != (MARKER,):
            raise CycleBlocked('EXPLICIT_NEW_EXPERIMENT_REQUIRED')
        if (c.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone() != (digest(cfg),)
                or c.execute("SELECT value FROM metadata WHERE key='config'").fetchone() != (canonical(cfg),)):
            raise CycleBlocked('SAVED_CONFIG_MISMATCH')
        implementation = digest({str(p.relative_to(Path(__file__).parent)): p.read_text()
            for p in sorted(Path(__file__).parent.rglob('*')) if p.is_file() and p.suffix in ('.py','.json')})
        from .runtime_compatibility import require_runtime
        try: require_runtime(c, implementation=implementation)
        except ValueError: raise CycleBlocked('SAVED_IMPLEMENTATION_MISMATCH') from None
        return state


def _graduation(store, item, now):
    refs = item.graduation_refs
    if type(refs) is not tuple or len(refs)>8:
        raise CycleBlocked('BOUNDED_GRADUATION_ORIGINALS_REQUIRED')
    raw = []; total = 0
    for key in refs:
        manifest = store.load(key)
        if (manifest.get('kind')!='history_request_v1' or manifest.get('method')!='getTransactionsForAddress'
                or type(manifest.get('params')) is not list or len(manifest['params'])!=2
                or manifest['params'][0] not in (item.target.mint,item.target.pool)):
            raise CycleBlocked('GRADUATION_SOURCE_BINDING_INVALID')
        options = manifest['params'][1]
        if (type(options) is not dict or options.get('transactionDetails')!='full'
                or options.get('commitment')!='finalized' or options.get('encoding')!='jsonParsed'):
            raise CycleBlocked('GRADUATION_SOURCE_BINDING_INVALID')
        response = store.load(manifest['response_hash'])
        total += len(canonical(response).encode())
        if total>2*1024*1024 or type(response.get('data')) is not list or len(raw)+len(response['data'])>256:
            raise CycleBlocked('BOUNDED_GRADUATION_ORIGINALS_REQUIRED')
        raw.extend(response['data'])
    observed = extract_graduation(raw,mint=item.target.mint,pool=item.target.pool,now=now,provenance=item.provenance)
    if (observed['status']=='OBSERVED_MIGRATION' and item.graduated_at is not None
            and item.graduated_at!=observed['graduated_at']):
        raise CycleBlocked('GRADUATION_TIMESTAMP_CONFLICT')
    return {**observed,'request_evidence_refs':list(refs)}


class _Budget:
    def __init__(self, progress, wall_clock, monotonic, deadline=DEFAULT_DEADLINE_SECONDS):
        self.progress, self.wall_clock, self.monotonic = progress, wall_clock, monotonic
        self.deadline = deadline
        self.start = self.last = monotonic()
        self.attempted = 0
        self.monitoring_attempted = 0
        self.remaining()

    def now(self):
        value = self.wall_clock()
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 2**63:
            raise CycleBlocked('WALL_CLOCK_UNAVAILABLE')
        return int(value)

    def remaining(self):
        value = self.monotonic()
        if (type(value) not in (int, float) or type(self.start) not in (int, float)
                or not math.isfinite(value) or not math.isfinite(self.start)
                or value < self.last or value >= self.start+self.deadline):
            raise CycleBlocked('CYCLE_DEADLINE_UNAVAILABLE')
        self.last = value
        return self.start+self.deadline-value

    def call(self, scan, invoke):
        before = self.progress.admission(scan)
        if before is None or before['state'] not in ('ADMITTED', 'SEALED'):
            raise CycleBlocked('PERSISTED_ADMISSION_REQUIRED')
        if before['requests_used'] >= before['request_ceiling']:
            raise CycleBlocked('INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
        if self.attempted >= 18:
            raise CycleBlocked('CYCLE_REQUEST_BUDGET_EXHAUSTED')
        timeout = min(self.remaining(), REQUEST_TIMEOUT_MAX)   # a source refuses a per-request timeout above its own 15 s ceiling
        self.attempted += 1
        try:
            result = invoke(timeout)
        except PaperReadError as error:
            raise CycleBlocked(error.code, getattr(error, 'evidence_hash', None)) from error
        after = self.progress.admission(scan)
        expected = {**before, 'requests_used': before['requests_used']+1}
        if after != expected:
            raise CycleBlocked('SHARED_BUDGET_CHARGE_OR_IDENTITY_MISMATCH')
        self.remaining()
        return result


class _HeldBudget:
    """Compare durable monotonic monitoring charges, never rolling count deltas."""
    def __init__(self, parent, monitoring):
        self.parent, self.monitoring = parent, monitoring
    def wall_clock(self):return self.parent.wall_clock()
    def now(self): return self.parent.now()
    def remaining(self): return self.parent.remaining()
    def call(self, scan, invoke):
        admission = self.parent.progress.admission(scan)
        before = self.monitoring.snapshot()
        if before['blockers']: raise CycleBlocked(before['blockers'][0])
        timeout = self.remaining()
        self.parent.monitoring_attempted += 1
        try:
            result = invoke(timeout)
        except PaperReadError as error:
            raise CycleBlocked(error.code, getattr(error, 'evidence_hash', None)) from error
        after = self.monitoring.snapshot()
        if (after['total_used'] != before['total_used']+1
                or self.parent.progress.admission(scan) != admission):
            raise CycleBlocked('MONITORING_CHARGE_OR_ADMISSION_MISMATCH')
        if after['blockers'] and after['blockers'] != ['MONITORING_REQUEST_BUDGET_EXHAUSTED']:
            raise CycleBlocked(after['blockers'][0])
        self.remaining()
        return result


def _collect_held(target, source, budget, store, token_profile_version=0):
    """Exit-only typed ingestion; investigation collector remains unchanged.

    Preserve the same original envelope grammar used by the exit producer. Each
    source call is independently fenced by the provisioned monitoring sequence.
    """
    refs = []
    def request(method, params, invoke, identity):
        original = json.loads(canonical(params)); started = budget.now()
        response = budget.call(target.scan_id, invoke)
        completed = budget.now()
        acquired = response.get('observed_at') if method == 'jupiter_probe' else completed
        envelope = {'source_id':identity,'acquired_at':acquired,'started_at':started,
                    'method':method,'params':original,'result':response}
        key = store.save(envelope)
        if key != digest(envelope): raise CycleBlocked('ORIGINAL_RESPONSE_PERSISTENCE_FAILED')
        refs.append(key)
        if type(acquired) is not int or not started <= acquired <= completed:
            raise CycleBlocked('SOURCE_ACQUISITION_CLOCK_INVALID')
        if params != original: raise CycleBlocked('SOURCE_REQUEST_MUTATED')
        if completed < started or completed-started > 10:
            raise CycleBlocked('SOURCE_RESPONSE_STALE')
        return response, completed, envelope
    params = [target.mint, {'encoding':'base64','commitment':'confirmed'}]
    raw, at, envelope = request('getAccountInfo',params,
        lambda timeout:source.rpc('getAccountInfo',params,timeout_seconds=timeout),source.rpc_source_id)
    token = ingest_mint(lambda:ProviderObservation(source.rpc_source_id,at,
        {'mint':target.mint,'observed_at':at,'slot':raw.get('context',{}).get('slot'),
         'account':raw.get('value'),'original_rpc_observation':envelope}),
        mint=target.mint,now=budget.now(),max_age_seconds=10,token_profile_version=token_profile_version)
    acquisitions = []; captures = []
    def rpc(method, params):
        result, at, _ = request(method,params,
            lambda timeout:source.rpc(method,params,timeout_seconds=timeout),source.rpc_source_id)
        acquisitions.append(at)
        return result
    def capture(payload):
        key = store.save(payload)
        if key != digest(payload): raise CycleBlocked('POOL_CAPTURE_PERSISTENCE_FAILED')
        refs.append(key); captures.append(payload)
        return key
    verify_pool(target.pool,target.mint,rpc,capture=capture,token_profile_version=token_profile_version)
    atomic_mint=captures[0]['result']['value'][captures[0]['params'][0].index(target.mint)]
    if raw['value']['owner']!=atomic_mint['owner']:
        raise CycleBlocked('MINT_POOL_TOKEN_PROGRAM_MISMATCH')
    pool_at = acquisitions[-1]
    pool = ingest_pool(lambda:ProviderObservation(source.rpc_source_id,pool_at,captures[0]),
        mint=target.mint,pool=target.pool,now=budget.now(),max_age_seconds=10,token_profile_version=token_profile_version)
    if token.decimals != pool.decimals or pool.slot < token.slot:
        raise CycleBlocked('MINT_POOL_BANK_MISMATCH')
    args = [target.mint,SOL,target.amount_raw,target.taker]
    payload, _, _ = request('jupiter_probe',args,
        lambda timeout:source.quote(*args,timeout_seconds=timeout),source.quote_source_id)
    quote = ingest_quote(lambda:ProviderObservation(source.quote_source_id,payload.get('observed_at'),payload),
        mint=token,direction='sell',amount_raw=target.amount_raw,taker=target.taker,
        expected_pool=target.pool,now=budget.now(),max_age_seconds=10)
    return TargetObservation(target,'sell',token,pool,quote,None,tuple(refs))


def _history(progress, item, budget, source_factory):
    target = item.target
    as_of = budget.now() if item.history_as_of is None else item.history_as_of
    if type(as_of) is not int or as_of < 300:
        raise CycleBlocked('CAPTURED_HISTORY_WINDOW_INVALID')
    from .paper_history_source import PAPER_HISTORY_PAGE_SIZE
    key = progress.create(target.scan_id, target.pool, as_of-300, as_of+1,
                          page_size=PAPER_HISTORY_PAGE_SIZE)
    state = progress.snapshot(key)
    while state['status'] != 'DONE':
        if state['status'] == 'RETRYABLE_ERROR':
            raise CycleBlocked('HISTORY_RECOVERY_REQUIRED')
        def advance(timeout):
            source = source_factory(progress, target.scan_id, key, timeout_seconds=timeout)
            return progress.advance(key, source)
        state = budget.call(target.scan_id, advance)
        if state.get('busy') or state.get('blocked') or state['status'] == 'RETRYABLE_ERROR':
            # T22H: HISTORY_RECOVERY_REQUIRED carries the failed page's retained attempt original; the closure accepts it
            # only if that original proves a transient failure (timeout / reset / 429 / 5xx).
            raise CycleBlocked(state.get('blocked') or ('HISTORY_BUSY' if state.get('busy') else 'HISTORY_RECOVERY_REQUIRED'),
                               state.get('failure_evidence'))
        if state['coverage'] and len(state['coverage']['pages']) >= 8 and state['status'] != 'DONE':
            raise CycleBlocked('FEATURE_HISTORY_PAGE_LIMIT')
    from .streaming_history import RetainedHistoryPages
    pairs=RetainedHistoryPages(progress.store,state['coverage'])
    return as_of, pairs.records(), pairs


def _usd(progress, source, scan, budget, refs=(), *, valuation_version=0):
    if valuation_version==1:
        from .kraken_usd_observation import from_attempt
        if refs:
            if len(refs)!=1:raise CycleBlocked('EXACT_KRAKEN_REF_REQUIRED')
            key=refs[0]
        else:key=budget.call(scan,lambda timeout:source.kraken_sol_price(timeout_seconds=timeout))['evidence_hash']
        record=progress.store.load(key)
        if digest(record)!=key:raise CycleBlocked('KRAKEN_ORIGINAL_BINDING_INVALID')
        from decimal import Decimal
        response,_=from_attempt(record,now=format(Decimal(str(budget.wall_clock())),'f'),scan=scan)
        return response,None,None,None,(),record,(key,)

    if refs:
        return _retained_usd(progress,source,scan,refs)
    price = budget.call(scan, lambda timeout: source.sol_price(timeout_seconds=timeout))
    original = _attempt_record(progress, price['evidence_hash'], scan, source.price_source_id, 'jupiter_price_v3', {'ids': SOL})
    raw = base64.b64decode(original['response_bytes_base64'], validate=True)
    if len(raw) > MAX_RESPONSE_BYTES or original['observed_at'] != price['observed_at']:
        raise CycleBlocked('PRICE_ORIGINAL_BINDING_INVALID')
    slot, slot_key = budget.call(scan, lambda timeout: source.rpc_with_evidence('getSlot', [{'commitment': 'finalized'}], timeout_seconds=timeout))
    slot_record = _attempt_record(progress, slot_key, scan, source.rpc_source_id, 'getSlot', [{'commitment': 'finalized'}])
    item = price['response'].get(SOL)
    block = item.get('blockId') if type(item) is dict else None
    if type(slot) is not int or not 0 <= slot < 2**63:
        raise CycleBlocked('FINALIZED_SLOT_INVALID')
    low = max(0, slot-32)
    witness = block if type(block) is int and low <= block <= slot else slot
    block_time, time_key = budget.call(scan, lambda timeout: source.rpc_with_evidence('getBlockTime', [witness], timeout_seconds=timeout))
    _attempt_record(progress, time_key, scan, source.rpc_source_id, 'getBlockTime', [witness])
    if type(block_time) is not int or block_time < 0:
        raise CycleBlocked('EXACT_BLOCK_TIME_UNAVAILABLE')
    return (JupiterPriceResponse('GET', PRICE_URL, raw, original['observed_at'], original['http_status']),
            slot_record['observed_at'], low, slot, ((witness, block_time),),
            (price['evidence_hash'],slot_key,time_key))


def _retained_usd(progress, source, scan, refs):
    """Reuse originals only; parser independently checks age at decision time."""
    if type(refs) is not tuple or len(refs)!=3 or any(type(x) is not str or len(x)!=64 for x in refs):
        raise CycleBlocked('EXACT_USD_ORIGINAL_REFS_REQUIRED')
    price = _attempt_record(progress,refs[0],scan,source.price_source_id,'jupiter_price_v3',{'ids':SOL})
    slot = _attempt_record(progress,refs[1],scan,source.rpc_source_id,'getSlot',[{'commitment':'finalized'}])
    raw = base64.b64decode(price['response_bytes_base64'],validate=True)
    if (len(raw)>MAX_RESPONSE_BYTES or base64.b64decode(price['request_bytes_base64'],validate=True)!=urlencode({'ids':SOL}).encode()):
        raise CycleBlocked('PRICE_ORIGINAL_BINDING_INVALID')
    def rpc_result(record,method,params):
        request = _parse(base64.b64decode(record['request_bytes_base64'],validate=True))
        response_bytes = base64.b64decode(record['response_bytes_base64'],validate=True)
        if len(response_bytes)>2*1024*1024:
            raise CycleBlocked('USD_WITNESS_RESPONSE_OVERSIZED')
        response = _parse(response_bytes)
        if (request!={'jsonrpc':'2.0','id':RPC_ID,'method':method,'params':params}
                or set(response)!={'jsonrpc','id','result'} or response['jsonrpc']!='2.0' or response['id']!=RPC_ID):
            raise CycleBlocked('USD_WITNESS_ORIGINAL_BINDING_INVALID')
        return response['result']
    height = rpc_result(slot,'getSlot',[{'commitment':'finalized'}])
    if type(height) is not int or not 0<=height<2**63:
        raise CycleBlocked('FINALIZED_SLOT_INVALID')
    item = _parse(raw).get(SOL); block = item.get('blockId') if type(item) is dict else None
    low = max(0,height-32); witness = block if type(block) is int and low<=block<=height else height
    time_record = _attempt_record(progress,refs[2],scan,source.rpc_source_id,'getBlockTime',[witness])
    at = rpc_result(time_record,'getBlockTime',[witness])
    if type(at) is not int or at<0:
        raise CycleBlocked('EXACT_BLOCK_TIME_UNAVAILABLE')
    return (JupiterPriceResponse('GET',PRICE_URL,raw,price['observed_at'],price['http_status']),
            slot['observed_at'],low,height,((witness,at),),refs)


def fulfill_quotes(state, cfg, event_builder, collected, source, budget):
    """Plan exact demands; one charged quote-only read per missing demand.

    event_builder rebinds actual decision time after reads, preserving original
    history/mint/pool/USD acquisition times. No quantity-scaled quote reuse.
    Returns the final original-bound event, typed book and detached diagnostics.
    """
    quotes = (); available = collected.quote
    for _ in range(qe.MAX_QUOTES+1):
        event = event_builder()
        if event is None:
            raise CycleBlocked('MARKET_PRODUCER_BLOCKED')
        if len(canonical(event).encode())>MAX_EVENT_BYTES:
            raise CycleBlocked('EVENT_READER_SIZE_LIMIT')
        planned = qe.plan(state, event, cfg, quotes)
        demands = planned['quote_demands']
        if not demands:
            return event, quotes, planned
        demand = demands[0]; direction = demand['direction']
        raw = demand.get('input_raw', demand.get('maximum_input_raw'))
        if type(raw) is not int or not 0 < raw < 2**64 or direction not in ('buy', 'sell'):
            raise CycleBlocked('QUOTE_DEMAND_INVALID')
        if any(q.direction == direction and q.input_raw == raw for q in quotes):
            raise CycleBlocked('UNRESOLVED_QUOTE_DEMAND')
        if (available is not None and available.direction == direction
                and (available.input_raw == raw or (direction == 'buy'
                     and demand['minimum_input_raw'] <= available.input_raw <= raw))):
            quote = available; available = None
        else:
            input_mint, output_mint = (SOL,collected.target.mint) if direction == 'buy' else (collected.target.mint,SOL)
            payload = budget.call(collected.target.scan_id, lambda timeout: source.quote(
                input_mint, output_mint, raw, collected.target.taker, timeout_seconds=timeout))
            quote = ingest_quote(lambda: ProviderObservation(source.quote_source_id,payload['observed_at'],payload),
                mint=collected.mint, direction=direction, amount_raw=raw, taker=collected.target.taker,
                expected_pool=collected.target.pool, now=budget.now(), max_age_seconds=cfg['price_ttl_seconds'])
        quotes += (quote,)
    raise CycleBlocked('QUOTE_DEMAND_LIMIT')


def _close_failed(store, progress, identity, error, attempt_refs, ledger_before):
    """FAILED_CHARGED for a pass that raised, ONLY for an allow-listed transient cause (T22G).

    Everything else (an integrity cause, a free-text ValueError, an unclassified exception, RecoveryRequired) is
    held: the hold is written durably (retry, then a sentinel) so recovery can never abandon it later.
    """
    try:
        from . import paper_pass_closure as closure
        cause, transient = closure.classify(error, store)
        if isinstance(error, RecoveryRequired) or not transient:
            closure.hold_durably(store, pass_id=identity, cause=cause)
            return
        evidence = getattr(error, 'evidence_hash', None)      # the failed read's retained attempt original, if any
        refs = tuple(attempt_refs) + ((evidence,) if type(evidence) is str and evidence not in attempt_refs else ())
        try:
            closure.close(store, progress, pass_id=identity, status='FAILED_CHARGED', cause=cause,
                          attempt_refs=refs, ledger_before=ledger_before)
        except closure.HoldRequired:
            closure.hold_durably(store, pass_id=identity, cause=cause)
    except Exception:
        pass


def _close_unfinished(store, progress, identity, result, outcome, attempt_refs, ledger_before):
    """FAILED_CHARGED for a pass that ended BLOCKED with charges and no other terminal outcome.

    Allow-list: every top-level blocker must be an allow-listed cause and every blocker nested in the diagnostics
    must be ordinary producer vocabulary, otherwise the pass is held (so a MARKET_PRODUCER_BLOCKED with integrity
    codes nested inside, whose no-entry publication was refused, is NOT closed here).
    """
    try:
        from . import paper_pass_closure as closure
        causes = list(result['blockers'])
        with closing(store.connect()) as c:
            row = c.execute('SELECT outcome_hash,intent_hash FROM paper_observation_passes WHERE id=?', (identity,)).fetchone()
        if row is None or row[0] is not None:
            return
        hazards = closure.declared_hazards(terminal._load(store, row[1]))
        if (result['status'] == 'RECOVERY_REQUIRED' or not causes or not closure.nested_blockers_clear(result, hazards)):
            closure.hold_durably(store, pass_id=identity, cause=causes[0] if causes else result['status'], result_hash=outcome)
            return
        try:
            closure.close(store, progress, pass_id=identity, status='FAILED_CHARGED', cause=causes[0],
                          result_hash=outcome, attempt_refs=tuple(attempt_refs) + tuple(result.get('usd_evidence_refs', ())),
                          ledger_before=ledger_before)
        except closure.HoldRequired:
            closure.hold_durably(store, pass_id=identity, cause=causes[0], result_hash=outcome)
    except Exception:
        pass


def run_once(research_db, evidence_db, ledger_db, cfg, *, position_targets=(), candidates=(),
             dependency_blockers, controls=(), usd_evidence_refs=(), monitoring=False, source_factory=PaperReadSources,
             history_source_factory=PaperHistorySource, wall_clock=time.time, monotonic=time.monotonic,
             history_first=False, preparation_publication=None):
    """Trusted callable, no automatic runner activation or permission from JSON.

    Pending dependency blockers are explicit caller-supplied review/integration
    facts, not claims of live acceptance. They prevent reads and fills. Every open
    ledger position must have its original admission and exact current raw size.
    """
    _config(cfg)
    from .kraken_usd_observation import selected as usd_selected
    valuation_version=usd_selected(cfg)
    if type(monitoring) is not bool or type(position_targets) is not tuple or type(candidates) is not tuple:
        raise ValueError('Explicit bounded cycle target tuples required')
    if type(history_first) is not bool or (preparation_publication is not None and not callable(preparation_publication)):
        raise ValueError('Explicit history-first mode and publication required')
    if history_first and (len(candidates)!=1 or position_targets or controls or monitoring or usd_evidence_refs):
        raise ValueError('History-first requires exactly one new candidate')
    if not history_first and preparation_publication is not None:
        raise ValueError('Publication requires history-first mode')
    items = position_targets+candidates
    if (type(position_targets) is not tuple or type(candidates) is not tuple or len(items)>18
            or any(type(x) is not CycleTarget or type(x.target) is not ObservationTarget for x in items)
            or type(dependency_blockers) is not tuple or len(dependency_blockers)>16
            or any(type(x) is not str or not 1<=len(x)<=128 for x in dependency_blockers)
            or type(controls) is not tuple or len(controls)>18
            or type(usd_evidence_refs) is not tuple or len(usd_evidence_refs) not in ((0,1) if valuation_version else (0,3))):
        raise ValueError('Explicit bounded cycle inputs required')
    result = {'kind':'paper_cycle_v1','status':'BLOCKED','execution_status':'EXECUTION_UNVERIFIED',
              'live_readiness':False,'attempted_requests':0,'outcomes':[],'events':[],
              'blockers':list(dependency_blockers),'budget':{},'diagnostics':[],'usd_evidence_refs':[]}
    if dependency_blockers:
        return result
    research, evidence, path = canonical_job_path(research_db), canonical_ownership_path(evidence_db), canonical_job_path(ledger_db)
    if len({research,evidence,path}) != 3 or not all(p.is_file() for p in (research,evidence,path)):
        raise ValueError('Three distinct existing databases required')
    with _worker_lock(research) as worker:
        if worker is None:
            return {**result,'blockers':['RESEARCH_WORKER_BUSY']}
        with _lock(str(evidence)+'.ownership-invocation.lock') as acquired:
            if not acquired:
                return {**result,'blockers':['EVIDENCE_INVOCATION_BUSY']}
            with _lock(str(path)+'.paper-cycle.lock') as acquired:
                if not acquired:
                    return {**result,'blockers':['PAPER_CYCLE_BUSY']}
                try:
                    jobs, store, progress = _existing_context(research,evidence,tuple(x.target for x in items))
                    state = _state(path,cfg)
                    allowance = MonitoringBudget(store,path,cfg,clock=wall_clock) if monitoring else None
                    monitoring_total = allowance.snapshot()['total_used'] if allowance is not None else None  # Never provision on a read path.
                except MonitoringBlocked as error:
                    return {**result,'blockers':[error.code]}
                except RecoveryRequired as error:
                    return {**result,'status':'RECOVERY_REQUIRED','blockers':[str(error)]}
                except CycleBlocked as error:
                    return {**result,'blockers':[error.code]}
                except (_Blocked, sqlite3.Error):
                    return {**result,'blockers':['PERSISTED_ADMISSION_OR_SCHEMA_REQUIRED']}
                supplied = {x.target.mint:x for x in position_targets}
                # Explicit concurrent-entries experiments may monitor a subset of the
                # open positions per pass (held legs); otherwise all must be supplied.
                if len(supplied)!=len(position_targets) or (
                        not set(supplied)<=set(state['positions']) if concurrency.selected(cfg)
                        else set(supplied)!=set(state['positions'])):
                    return {**result,'blockers':['ALL_OPEN_POSITION_TARGETS_REQUIRED']}
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
                    for mint,p in state['positions'].items():
                        if mint not in supplied:continue
                        target = supplied[mint].target
                        row = c.execute('SELECT payload,payload_hash FROM events WHERE event_id=?',(p.get('entry_event_id'),)).fetchone()
                        original = json.loads(row[0]) if row else None
                        if (not row or digest(original)!=row[1] or target.scan_id!=original['paper_source_evidence']['scan_id']
                                or target.pool!=p['pool'] or target.taker!=p['taker']
                                or supplied[mint].provenance!=p['provenance']
                                or target.amount_raw!=qe.raw_quantity(p['qty'],p['quote_execution']['mint_decimals'])):
                            return {**result,'blockers':['POSITION_ORIGINAL_ADMISSION_OR_SIZE_MISMATCH']}
                with store.connect() as c:
                    c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
                try:
                    # All three locks are held, so an unresolved pass belongs to a process that is gone.
                    from .paper_pass_closure import recover_abandoned
                    recover_abandoned(store,progress,ledger_db=path,cfg=cfg,clock=wall_clock,research_db=research)
                except (ValueError,OSError,sqlite3.Error,TypeError,KeyError,RecoveryRequired,MonitoringBlocked):
                    pass  # Unprovable recovery keeps the latch; the gate below decides.
                try:
                    blocked = terminal.gate(store,research,tuple(x.target.scan_id for x in items),ledger_locked=str(path))
                except (ValueError,OSError,sqlite3.Error,TypeError,KeyError):
                    blocked = 'OBSERVATION_RECOVERY_REQUIRED'
                if blocked:
                    return {**result,'status':'RECOVERY_REQUIRED','blockers':[blocked]}
                if history_first:
                    from .paper_history_preparation import prepare
                    from . import provider_pacing
                    pacer=provider_pacing.configured(priority='investigation')
                    if pacer is None:raise ValueError('Configured durable pacer required')
                    pacer._validate();pacing_identity=(pacer.path,pacer.identity)
                    prepared=prepare(store,progress,path,cfg,candidates[0],research_db=research,pacing_path=pacer.path)
                    if type(prepared) is dict:
                        # Only independently replayed typed rejection can publish.
                        from .history_preparation_rejection import verify
                        verify(store,progress,prepared)
                        if preparation_publication is not None:preparation_publication(prepared)
                        return prepared
                    candidates=(prepared,);items=candidates
                    time.sleep(2.0)
                    pacer=provider_pacing.configured(priority='investigation')
                    if pacer is None or (pacer.path,pacer.identity)!=pacing_identity:
                        raise ValueError('Pacer identity changed')
                    pacer._validate()
                budget = _Budget(progress,wall_clock,monotonic,deadline_seconds(cfg))
                for control in controls:
                    validate_event(control)
                    if control['kind']!='control' or control['ts']!=budget.now():
                        raise ValueError('Current explicit operator controls required')
                intent = {'kind':'paper_cycle_intent_v1','closure_v1':True,'config_hash':digest(cfg),'ledger':str(path),
                          'targets':[asdict(x) for x in items],
                          'admissions':{x.target.scan_id:progress.admission(x.target.scan_id) for x in items}}
                # Intrinsic certification is intentionally restricted to a new
                # single-candidate pass against a verified INIT-only checkpoint.
                if len(candidates)==1 and not position_targets and not controls:
                    try:
                        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
                            anchors,_=terminal._ledger(c,cfg,initial=True)
                        context=terminal._context(research,evidence,path,os.environ.get('DESK_PROVIDER_PACING_DB'))
                        intent.update(ledger_anchors=anchors,terminal_context=context,source_hash=terminal.runtime.implementation_hash())
                    except (ValueError,OSError,sqlite3.Error):pass
                if position_targets:
                    intent['positions'] = [x.target.scan_id for x in position_targets]
                if monitoring_total is not None:
                    intent['monitoring_total'] = monitoring_total
                identity = uuid.uuid4().hex; key = store.save(intent)
                attempt_refs=[]; terminal_hazards=(); realism_jobs=[]
                result.update(pass_id=identity,intent_hash=key,attempt_refs=attempt_refs)
                with store.connect() as c:c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',(identity,key))
                # Category (a) rejections may retire this pass; only a lone,
                # control-free, non-monitoring candidate qualifies.
                ledger_before=None
                if len(candidates)==1 and not position_targets and not controls and not monitoring:
                    try:
                        from .paper_cycle_no_entry import ledger_snapshot
                        ledger_before=ledger_snapshot(path)
                    except (ValueError,OSError,sqlite3.Error):pass
                try:
                    ledger = Ledger(path,must_exist=True)
                    try:
                        ledger.apply(INIT,cfg,engine.transition,engine.initial_state)
                        def deliver(event,quotes=()):
                            if len(canonical(event).encode())>MAX_EVENT_BYTES:
                                raise CycleBlocked('EVENT_READER_SIZE_LIMIT')
                            # Recheck inside the same write transaction, including
                            # checkpoint loss after the read-only preflight.
                            bound = qe.bind_transition(event,quotes)
                            def checked(saved,delivered,config):
                                read_checkpoint(ledger.db)
                                return bound(saved,delivered,config)
                            result['outcomes'].extend(ledger.apply(event,cfg,checked,engine.initial_state))
                            result['events'].append(event['event_id'])
                        for control in controls:deliver(control)
                        usd = None
                        for item in items:
                            state = _state(path,cfg)
                            target = item.target; is_position = item in position_targets
                            if not is_position and (state['mode']!='RUNNING' or target.mint in state['positions']):
                                result['diagnostics'].append({'scan_id':target.scan_id,'blockers':['ENTRY_CONTROL_OR_EXISTING_POSITION']})
                                continue
                            if not is_position:
                                graduation = _graduation(store,item,budget.now())
                                result['diagnostics'].append({'scan_id':target.scan_id,'graduation':graduation})
                                if graduation['status']!='OBSERVED_MIGRATION':
                                    raise CycleBlocked('RETAINED_MIGRATION_WITNESS_REQUIRED')
                            action_budget = _HeldBudget(budget,allowance) if is_position and allowance is not None else budget
                            if is_position and allowance is not None:
                                source = source_factory(progress,target.scan_id,monitoring_budget=allowance)
                                collected = _collect_held(target,source,action_budget,store,selected(cfg))
                            else:
                                if budget.attempted >= 18:
                                    raise CycleBlocked('CYCLE_REQUEST_BUDGET_EXHAUSTED')
                                source = source_factory(progress,target.scan_id)
                                collector = collect_observations(jobs=jobs,progress=progress,
                                    sources={target.scan_id:BoundedSource(source.rpc_source_id,source.quote_source_id,source.rpc,source.quote)},
                                    open_positions=(target,) if is_position else (), candidates=() if is_position else (target,),
                                    request_ceiling=18-budget.attempted,deadline_seconds=min(budget.remaining(),REQUEST_TIMEOUT_MAX),max_age_seconds=10,
                                    wall_clock=wall_clock,monotonic=monotonic,token_profile_version=selected(cfg))
                                budget.attempted += collector.attempted_requests
                                # Only this bounded transport's original persisted
                                # attempt hashes can certify future terminal closure.
                                if type(source) is PaperReadSources:
                                    attempt_refs.extend(source.attempt_evidence_refs)
                                terminal_hazards=collector.terminal_hazards
                                if terminal_hazards:result['terminal_hazards']=list(terminal_hazards)
                                if collector.stopped_reason or not collector.observations or collector.observations[0].failure:
                                    raise CycleBlocked(collector.stopped_reason or (collector.observations[0].failure if collector.observations else 'OBSERVATIONS_INCOMPLETE'))
                                collected = collector.observations[0]
                            if not is_position:
                                as_of, raw, pages = _history(progress,item,budget,history_source_factory)
                                if usd is None or valuation_version:
                                    try:
                                        usd = _usd(progress,source,target.scan_id,budget,usd_evidence_refs,valuation_version=valuation_version)
                                    except CycleBlocked:
                                        raise
                                    except (ValueError,TypeError,KeyError,AttributeError):
                                        raise CycleBlocked('USD_ORIGINAL_BINDING_INVALID') from None
                                    result['usd_evidence_refs']=list(usd[-1])
                            if is_position and valuation_version:
                                held_budget=_HeldBudget(budget,allowance) if allowance else budget
                                usd=_usd(progress,source,target.scan_id,held_budget,valuation_version=valuation_version)
                                result['usd_evidence_refs']=list(usd[-1])
                            diagnostic = None
                            def event_builder():
                                nonlocal diagnostic
                                if valuation_version:
                                    from decimal import Decimal
                                    from .kraken_usd_observation import timestamp
                                    decision_at=format(Decimal(str(budget.wall_clock())),'f')
                                    now=int(timestamp(decision_at))
                                else:now=budget.now()
                                if is_position:
                                    context = ExitContext(now,target,source.rpc_source_id,source.quote_source_id,
                                                          item.provenance,item.known_hazards,selected(cfg))
                                    diagnostic = build_exit_event(collected,context=context,
                                        position=state['positions'][target.mint],cfg=cfg,load_evidence=store.load)
                                    if valuation_version and diagnostic['event'] is not None:
                                        from .kraken_usd_observation import evidence as usd_evidence
                                        from decimal import Decimal
                                        event=diagnostic['event']
                                        event['source_evidence']['scan_id']=target.scan_id
                                        event['paper_usd_valuation']=usd_evidence(usd[-2],now=decision_at,scan=target.scan_id)
                                        event['event_id']='paper-exit:'+digest({k:v for k,v in event.items() if k!='event_id'})
                                else:
                                    context = MarketContext(now,target,source.rpc_source_id,source.quote_source_id,
                                        item.provenance,graduation['graduated_at'],item.holder_at,item.known_hazards,item.pool_fee_bps,as_of,selected(cfg),valuation_version)
                                    if valuation_version:
                                        response,at,low,high,times,usd_attempt,_refs=usd
                                        from .kraken_usd_observation import TrustedTimeBounds
                                        from decimal import Decimal
                                        usd_bounds=TrustedTimeBounds(decision_at)
                                    else:
                                        response,at,low,high,times,_refs = usd
                                        usd_attempt=None;usd_bounds=TrustedSlotBounds(now,at,low,high,times)
                                    diagnostic = build_market_event(collected,context=context,load_evidence=store.load,
                                        raw_trades=raw,history_pages=pages,usd_response=response,
                                        usd_bounds=usd_bounds,usd_attempt=usd_attempt,strategy_profile=OBSERVABLE_FLOW_CHURN_CONCENTRATION_V1)
                                if not is_position and diagnostic['event'] is not None and regime.enabled(cfg):
                                    from . import regime_producer
                                    event=diagnostic['event']
                                    found=regime_producer.evidence(research,evidence,event['ts'],regime.policy(cfg)['ttl_seconds'])
                                    if found is not None:
                                        event['regime']=found
                                        event['event_id']='paper-market:'+digest({k:v for k,v in event.items() if k!='event_id'})
                                if diagnostic['event'] is None:
                                    result['diagnostics'].append({'scan_id':target.scan_id,'blockers':diagnostic['blockers']})
                                return diagnostic['event']
                            event, quotes, planned = fulfill_quotes(state,cfg,event_builder,collected,source,action_budget)
                            result['diagnostics'].append({'scan_id':target.scan_id,'blockers':diagnostic['blockers'],
                                                          'planned_outcomes':planned['outcomes']})
                            before_outcomes=len(result['outcomes'])
                            deliver(event,quotes)
                            realism_jobs+=fill_realism.capture(cfg,result['outcomes'][before_outcomes:],event,collected,is_position)  # never raises
                            _state(path,cfg)
                            if is_position and any(x['type']=='blocked_exit' for x in planned['outcomes']):
                                raise CycleBlocked('UNRESOLVED_POSITION_EXIT')
                        result['status']='COMPLETE'
                    except ObservationError:
                        result['blockers'].append('HELD_OBSERVATION_CONTENT_REJECTED')
                    except (CycleBlocked, MonitoringBlocked) as error:
                        result['blockers'].append(error.code)
                        if getattr(error,'evidence_hash',None) and error.evidence_hash not in attempt_refs:
                            attempt_refs.append(error.evidence_hash)    # the failed read's original can classify the closure
                    except RecoveryRequired as error:
                        result['status']='RECOVERY_REQUIRED';result['blockers'].append(str(error))
                    except ValueError as error:
                        # Only known Ledger integrity diagnostics are converted;
                        # unrelated programming/validator defects remain visible.
                        if str(error).startswith(('ledger checkpoint missing: recovery required;',
                                                  'ledger checkpoint invalid: recovery required;',
                                                  'ledger event journal incomplete: recovery required;')):
                            result['status']='RECOVERY_REQUIRED';result['blockers'].append('LEDGER_INTEGRITY_FAILURE')
                        else:raise
                    finally:
                        ledger.close()
                        result['attempted_requests']=budget.attempted+budget.monitoring_attempted
                        result['investigation_attempted_requests']=budget.attempted
                        result['monitoring_attempted_requests']=budget.monitoring_attempted
                        if allowance is not None:
                            try: result['monitoring_budget']=allowance.snapshot()
                            except (MonitoringBlocked,RecoveryRequired,sqlite3.Error):
                                result['monitoring_budget']={'status':'RECOVERY_REQUIRED','blockers':['MONITORING_SNAPSHOT_UNAVAILABLE']}
                        result['budget']={scan:{'used':progress.admission(scan)['requests_used'],
                            'ceiling':progress.admission(scan)['request_ceiling']} for scan in intent['admissions']}
                    if result['status']!='COMPLETE' and (budget.attempted>0 or budget.monitoring_attempted>0):
                        # T22H L4: when this pass can not be closed, the hold goes to disk BEFORE the result is saved, so a kill in
                        # between cannot leave a NULL pass that a later recovery would abandon.
                        from . import paper_pass_closure as closure
                        if closure.result_requires_hold(store,result,key,attempt_refs):
                            closure.write_sentinel(store,identity,(result['blockers'] or [result['status']])[0])
                    outcome = store.save(result)
                    if realism_jobs:fill_realism.enqueue(path,realism_jobs,digest(cfg))  # opt-in; result page durable; before every early return; never raises
                    if terminal_hazards and 'terminal_context' in intent:
                        try:
                            receipt=terminal.certify_intrinsic(store,progress,cfg,pass_id=identity,intent_hash=key,
                                outcome_hash=outcome,attempt_refs=attempt_refs,hazards=terminal_hazards)
                            return {**result,'evidence_hash':outcome,'terminal_receipt_hash':receipt}
                        except (ValueError,TypeError,KeyError,OSError,sqlite3.Error):
                            # Missing/contradictory proof never clears the NULL latch.
                            pass
                    if (ledger_before is not None and result['status']=='BLOCKED' and budget.attempted>0
                            and len(result['blockers'])==1):
                        from . import paper_cycle_no_entry as no_entry
                        if result['blockers'][0] in no_entry.NORMAL:
                            try:
                                no_entry.publish(store,progress,pass_id=identity,intent_hash=key,result=result,ledger=ledger_before)
                                return {**result,'evidence_hash':outcome}
                            except (ValueError,TypeError,KeyError,OSError,sqlite3.Error) as refused:
                                # Unproved rejection is not retired as a normal no-entry (fail closed). T22F R3: the
                                # refusal is typed and visible (log + <store>.publish-refused.jsonl) instead of silent.
                                no_entry.record_refusal(store,pass_id=identity,scan_id=next(iter(intent['admissions']),None),
                                                        blocker=result['blockers'][0],error=refused,at=wall_clock())
                    if result['status']=='COMPLETE' or (budget.attempted==0 and budget.monitoring_attempted==0 and all(
                            progress.admission(scan)==admission for scan,admission in intent['admissions'].items())):
                        with store.connect() as c:c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND outcome_hash IS NULL',(outcome,identity))
                    if result['status']!='COMPLETE':
                        _close_unfinished(store,progress,identity,result,outcome,attempt_refs,ledger_before)
                    return {**result,'evidence_hash':outcome}
                except BaseException as error:
                    # The pass died with a charge outstanding: retire it as FAILED_CHARGED when that is provable
                    # (never for integrity causes). The exception still propagates unchanged.
                    _close_failed(store,progress,identity,error,attempt_refs,ledger_before)
                    raise
