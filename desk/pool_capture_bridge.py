"""Explicit local coordinator capture, never entry/CLI wiring or chain proof.

The caller provisions the ledger, existing investigation and transport OUT OF
BAND. Candidate JSON cannot select a source or issue receipts. Source approval
is trust in the provider's responses, not cryptographic Solana authentication.
Linux stable-file contract inherits the receipt ledger boundary. Lock order:
research worker (if caller holds one), evidence invocation, evidence request,
then receipt ledger. No network setup is performed outside the charged RPC
callable. Every genesis/slot/bank/time attempt uses HistoryProgress.reserve;
there is no new budget and no reset. A transport must perform exactly ONE
request per call, with no hidden discovery, retries or credential/setup I/O.

A durable PENDING attempt after process death, any failed request, missing
checkpoint, or malformed observation permanently blocks this pool/mint in this
journal. A fresh ID cannot hide it. Completed observations (including invalid
protocol state) are published before admission, across all sources and runs.
No privileged filesystem rewrite/rollback defense or live verification claimed.
"""
from contextlib import contextmanager, closing
from dataclasses import dataclass, asdict
import fcntl
import json
import os
from stat import S_ISREG
import sqlite3
import time
import zlib
from collections.abc import Callable

from .evidence import EvidenceStore
from .history_progress import HistoryProgress, ownership_lock_path
from .model import canonical, digest
from .pool_receipt_ledger import ApprovedSource, PoolReceiptLedger
from .pool_vault_admission import AcquisitionReceipt, EvidenceRefs, GENESIS, NETWORK, PROFILE, admit_pool_vault
from .pools import ATA
from .providers import PUMP, PUMPSWAP, SOL
from .security import TOKEN_PROGRAM
from .programs import address

MAX_EVENTS = 4096
SCHEMA = {
 'pool_capture_config': 'CREATE TABLE pool_capture_config(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL)',
 'pool_capture_events': 'CREATE TABLE pool_capture_events(seq INTEGER PRIMARY KEY,capture TEXT NOT NULL,body TEXT NOT NULL,previous TEXT NOT NULL,hash TEXT NOT NULL UNIQUE)',
 'pool_capture_head': 'CREATE TABLE pool_capture_head(id INTEGER PRIMARY KEY CHECK(id=1),count INTEGER NOT NULL,hash TEXT NOT NULL)',
}
for table in ('pool_capture_config', 'pool_capture_events'):
    for verb in ('UPDATE', 'DELETE'):
        name = 'protect_' + table + '_' + verb.lower()
        SCHEMA[name] = f"CREATE TRIGGER {name} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable capture journal'); END"
    name = 'protect_' + table + '_insert'
    column = 'id' if table.endswith('config') else 'seq'
    collisions = f'{column}=NEW.{column} OR rowid=NEW.rowid'
    if table == 'pool_capture_events':
        collisions += ' OR hash=NEW.hash'
    # REPLACE implicitly deletes conflicting rows without firing DELETE
    # triggers on a plain connection. Guard every UNIQUE key before insertion.
    SCHEMA[name] = f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {collisions}) BEGIN SELECT RAISE(ABORT,'Capture journal identity'); END"


class CaptureBlocked(ValueError):
    pass


def need(condition, message):
    if not condition: raise CaptureBlocked(message)


def integer(value):
    return type(value) is int and 0 <= value < 2**63


def identity(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and all(c.isalnum() or c in '-_:.' for c in value)


@dataclass(frozen=True)
class CoordinatorTransport:
    """Trusted local dependency, NEVER constructed from candidate fields.

    rpc is one read-only, nonretrying request. Tests use synthetic callables;
    production provisioning and independent review remain coordinator duties.
    """
    source: ApprovedSource
    rpc: Callable


@dataclass(frozen=True)
class Investigation:
    scan_id: str
    descriptor_hash: str
    mint: str


def canonical_accounts(mint):
    from solders.pubkey import Pubkey
    def pda(seeds, program): return str(Pubkey.find_program_address(seeds, Pubkey.from_string(program))[0])
    raw = bytes(Pubkey.from_string(mint)); sol = bytes(Pubkey.from_string(SOL))
    creator = pda([b'pool-authority', raw], PUMP)
    pool = pda([b'pool', bytes(2), bytes(Pubkey.from_string(creator)), raw, sol], PUMPSWAP)
    pool_raw = bytes(Pubkey.from_string(pool))
    lp = pda([b'pool_lp_mint', pool_raw], PUMPSWAP)
    token = bytes(Pubkey.from_string(TOKEN_PROGRAM))
    return [pool, mint, SOL, lp, pda([pool_raw, token, raw], ATA), pda([pool_raw, token, sol], ATA)]


class PoolCaptureBridge:
    def __init__(self, ledger, transport, *, clock=time.time):
        need(type(ledger) is PoolReceiptLedger and ledger.writer, 'COORDINATOR_WRITER_REQUIRED')
        need(type(transport) is CoordinatorTransport and type(transport.source) is ApprovedSource
             and callable(transport.rpc) and callable(clock), 'TRUSTED_TRANSPORT_REQUIRED')
        need(transport.source in ledger.config.sources and transport.source.network == NETWORK
             and transport.source.genesis_hash == GENESIS and transport.source.profile == PROFILE,
             'FIXED_SOURCE_REGISTRY_REQUIRED')
        need(transport.source.source_kind == 'coordinator_capture' or
             (transport.source.source_kind == 'synthetic_fixture' and ledger.config.allow_synthetic_fixtures),
             'SOURCE_KIND_UNSUPPORTED')
        ledger._guard()
        self.ledger = ledger; self.transport = transport; self.clock = clock
        self.store = EvidenceStore(ledger.evidence_path)
        self.config = {'version': 1, 'ledger': ledger.descriptor, 'evidence_identity': ledger.evidence_identity}
        self.seed = digest(self.config)

    @contextmanager
    def _locks(self):
        # Nonblocking canonical ownership guards; never invert with ledger locks.
        self.ledger._guard()
        with self._ownership_guard(invocation=True) as invocation:
            try: fcntl.flock(invocation, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise CaptureBlocked('BUSY') from None
            with self._ownership_guard() as request:
                try: fcntl.flock(request, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise CaptureBlocked('BUSY') from None
                self.ledger._guard()
                yield
                self.ledger._guard()

    @contextmanager
    def _ownership_guard(self, *, invocation=False):
        path = ownership_lock_path(self.store, invocation=invocation)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            info = os.fstat(fd)
            need(S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                 and not info.st_mode & 0o022, 'OWNERSHIP_LOCK_INVALID')
            need((info.st_dev, info.st_ino) == (os.stat(path).st_dev, os.stat(path).st_ino), 'OWNERSHIP_LOCK_REPLACED')
            yield fd
            need((info.st_dev, info.st_ino) == (os.stat(path).st_dev, os.stat(path).st_ino), 'OWNERSHIP_LOCK_REPLACED')
        finally:
            os.close(fd)

    @contextmanager
    def _db(self):
        with closing(self.store.connect()) as c:
            c.execute('PRAGMA synchronous=FULL'); c.execute('BEGIN IMMEDIATE')
            try:
                yield c
                self.ledger._guard(); c.commit()
            except BaseException:
                c.rollback(); raise

    def _initialize(self, c):
        names = {r[0] for r in c.execute('SELECT name FROM sqlite_master')}
        present = names.intersection(SCHEMA)
        if not present:
            for sql in SCHEMA.values(): c.execute(sql)
            c.execute('INSERT INTO pool_capture_config VALUES(1,?)', (canonical(self.config),))
            c.execute('INSERT INTO pool_capture_head VALUES(1,0,?)', (self.seed,))
        self._audit(c)

    def _audit(self, c):
        schema = dict(c.execute('SELECT name,sql FROM sqlite_master WHERE sql IS NOT NULL'))
        need(all(schema.get(name) == sql for name, sql in SCHEMA.items()), 'CAPTURE_SCHEMA_INVALID')
        need(c.execute('SELECT body FROM pool_capture_config WHERE id=1').fetchone() == (canonical(self.config),),
             'CAPTURE_BOUNDARY_MISMATCH')
        head = c.execute('SELECT count,hash FROM pool_capture_head WHERE id=1').fetchone()
        rows = c.execute('SELECT seq,capture,body,previous,hash FROM pool_capture_events ORDER BY seq LIMIT ?', (MAX_EVENTS+1,)).fetchall()
        need(head is not None and type(head[0]) is int and head[0] == len(rows) <= MAX_EVENTS, 'CAPTURE_HEAD_INVALID')
        previous = self.seed; runs = {}
        for i, (seq, capture, body, prior, key) in enumerate(rows, 1):
            event = json.loads(body)
            need(identity(capture) and seq == i and prior == previous and canonical(event) == body
                 and key == digest({'seq': seq, 'capture': capture, 'event': event, 'previous': prior}), 'CAPTURE_JOURNAL_INVALID')
            if capture not in runs:
                need(set(event) == {'descriptor'} and type(event['descriptor']) is dict, 'CAPTURE_DESCRIPTOR_REQUIRED')
                d = event['descriptor']
                need(set(d) == {'investigation', 'pool', 'mint', 'source', 'profile'} and d['profile'] == PROFILE,
                     'CAPTURE_DESCRIPTOR_INVALID')
                need(d['source'] in [asdict(s) for s in self.ledger.config.sources], 'CAPTURE_SOURCE_INVALID')
                need(type(d['investigation']) is dict and set(d['investigation']) == set(Investigation.__dataclass_fields__),
                     'CAPTURE_INVESTIGATION_INVALID')
                address(d['mint']); need(canonical_accounts(d['mint'])[0] == d['pool'], 'CAPTURE_POOL_INVALID')
                need(d['investigation']['mint'] == d['mint'], 'CAPTURE_MINT_INVALID')
                runs[capture] = {'descriptor': d, 'steps': []}
            else:
                steps = runs[capture]['steps']
                need(set(event) == {'stage', 'state', 'record', 'captured_at'}
                     and event['stage'] in ('genesis', 'slot', 'snapshot', 'time')
                     and event['state'] in ('PENDING', 'DONE', 'FAILED')
                     and integer(event['captured_at']), 'CAPTURE_EVENT_INVALID')
                stages = ['genesis', 'slot', 'snapshot', 'time']
                done = [s for s in steps if s['state'] == 'DONE']
                if event['state'] == 'PENDING':
                    need(all(s['state'] == 'DONE' for s in steps) and len(done) < 4
                         and event['stage'] == stages[len(done)] and event['record'] is None, 'CAPTURE_TRANSITION_INVALID')
                else:
                    need(steps and steps[-1]['state'] == 'PENDING' and steps[-1]['stage'] == event['stage']
                         and isinstance(event['record'], str), 'CAPTURE_TRANSITION_INVALID')
                    steps.pop()  # Replace the in-memory pending projection only, never persisted rows.
                steps.append(event)
            previous = key
        need(head[1] == previous, 'CAPTURE_HEAD_INVALID')
        return runs, previous, len(rows)

    def _append(self, c, capture, event):
        _, previous, count = self._audit(c)
        need(count < MAX_EVENTS, 'CAPTURE_CAPACITY_EXHAUSTED')
        key = digest({'seq': count+1, 'capture': capture, 'event': event, 'previous': previous})
        c.execute('INSERT INTO pool_capture_events VALUES(?,?,?,?,?)', (count+1, capture, canonical(event), previous, key))
        c.execute('UPDATE pool_capture_head SET count=?,hash=? WHERE id=1', (count+1, key))

    def _save(self, c, payload):
        raw = canonical(payload).encode(); need(len(raw) <= 64*1024, 'CAPTURE_EVIDENCE_TOO_LARGE')
        key = digest(payload); compressed = zlib.compress(raw)
        old = c.execute('SELECT payload,raw_bytes FROM pages WHERE hash=?', (key,)).fetchone()
        if old:
            need(old == (compressed, len(raw)), 'CAPTURE_EVIDENCE_COLLISION')
        else:
            used = c.execute('SELECT COALESCE(SUM(length(payload)),0) FROM pages').fetchone()[0]
            need(used+len(compressed) <= self.store.max_bytes, 'CAPTURE_STORAGE_EXHAUSTED')
            c.execute('INSERT INTO pages VALUES(?,?,?)', (key, compressed, len(raw)))
        return key

    def _binding(self, progress, binding):
        need(type(binding) is Investigation, 'TRUSTED_INVESTIGATION_REQUIRED')
        admission = progress.admission(binding.scan_id)
        need(admission is not None and admission['descriptor_hash'] == binding.descriptor_hash
             and admission['descriptor']['mint'] == binding.mint, 'EXISTING_INVESTIGATION_MISMATCH')
        return admission

    def _request(self, progress, binding, capture, stage, method, params):
        # Reserve BEFORE the transport, including failed genesis/slot/time. A
        # crash between reserve and PENDING spends budget but performs no I/O.
        need(progress.reserve(binding.scan_id), 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
        at = self._now()
        with self._db() as c:
            self._append(c, capture, {'stage': stage, 'state': 'PENDING', 'record': None, 'captured_at': at})
        try:
            result = self.transport.rpc(method, json.loads(canonical(params)))
            record = {'kind': 'rpc_response_v1', 'method': method, 'params': params, 'result': result}
            # Raw responses are saved even when they cannot support admission.
            with self._db() as c:
                key = self._save(c, record)
                self._append(c, capture, {'stage': stage, 'state': 'DONE', 'record': key, 'captured_at': self._now()})
            return record, key
        except Exception:
            # No raw provider exception bodies/URLs/credentials in checkpoints.
            with self._db() as c:
                key = self._save(c, {'kind': 'pool_capture_failure_v1', 'method': method, 'params': params})
                self._append(c, capture, {'stage': stage, 'state': 'FAILED', 'record': key, 'captured_at': self._now()})
            raise CaptureBlocked('CAPTURE_ATTEMPT_FAILED') from None

    def _now(self):
        value = self.clock()
        need(type(value) in (int, float) and value >= 0 and value < 2**63, 'COORDINATOR_CLOCK_INVALID')
        return int(value)

    def _records(self, run):
        records = {s['stage']: self.store.load(s['record']) for s in run['steps'] if s['state'] == 'DONE'}
        keys = canonical_accounts(run['descriptor']['mint'])
        expected = {'genesis': ('getGenesisHash', []), 'slot': ('getSlot', [{'commitment': 'finalized'}])}
        if 'slot' in records and integer(records['slot'].get('result')):
            expected['snapshot'] = ('getMultipleAccounts', [keys, {'encoding': 'base64', 'commitment': 'finalized',
                                                                  'minContextSlot': records['slot']['result']}])
        if 'snapshot' in records:
            result = records['snapshot'].get('result')
            if isinstance(result, dict) and isinstance(result.get('context'), dict) and integer(result['context'].get('slot')):
                expected['time'] = ('getBlockTime', [result['context']['slot']])
        for stage, record in records.items():
            need(stage in expected and type(record) is dict and set(record) == {'kind', 'method', 'params', 'result'}
                 and record['kind'] == 'rpc_response_v1'
                 and (record['method'], canonical(record['params'])) == (expected[stage][0], canonical(expected[stage][1])),
                 'CAPTURE_REQUEST_BINDING_INVALID')
        return records

    def _publish_run(self, capture, run, pool, mint):
        records = self._records(run)
        need(records['genesis']['result'] == GENESIS, 'CAPTURE_GENESIS_MISMATCH')
        slot = records['snapshot']['result']['context']['slot']
        at = records['time']['result']
        need(integer(slot) and type(at) is int and -2**63 <= at < 2**63, 'CAPTURE_SCOPE_AMBIGUOUS')
        source = run['descriptor']['source']; refs = []
        # Persist BOTH request manifests and response refs. Exact-slot/min
        # context mismatch remains visible to actual admission, never fixed.
        with self._db() as c:
            for stage in ('snapshot', 'time'):
                response = next(s['record'] for s in run['steps'] if s['stage'] == stage)
                record = records[stage]
                request = {'kind': 'pool_vault_request_v1', 'network': NETWORK, 'genesis_hash': GENESIS,
                           'method': record['method'], 'params': record['params'], 'response_hash': response}
                refs.extend((response, self._save(c, request)))
        receipt = AcquisitionReceipt(source['source_id'], source['source_kind'], NETWORK, GENESIS,
                    pool, mint, slot, at, next(s['captured_at'] for s in run['steps'] if s['stage'] == 'snapshot'),
                    EvidenceRefs(*refs))
        # Snapshot acquisition wall time defines freshness; a delayed clock
        # lookup/resume must not refresh an old bank. Identical observations
        # share an idempotent publication key (not a chain proof).
        self.ledger.publish('pool-observation:' + digest(asdict(receipt)), receipt)
        return receipt

    def _publish_all(self, runs, pool, mint, *, block_incomplete=True):
        incomplete = False; errors = []; published = {}
        for capture, run in runs.items():
            d = run['descriptor']
            if (d['pool'], d['mint']) != (pool, mint): continue
            if len(run['steps']) != 4 or any(s['state'] != 'DONE' for s in run['steps']):
                incomplete = True; continue
            try:
                published[capture] = self._publish_run(capture, run, pool, mint)
            except Exception as exc:
                # A broken older observation cannot prevent publication of a
                # later completed contradiction. Still block ALL admission.
                errors.append(exc)
        if errors: raise errors[0]
        if block_incomplete: need(not incomplete, 'PRIOR_CAPTURE_UNRESOLVED')
        return published

    def capture(self, *, capture_id, investigation, pool):
        """One immutable capture ID; resume checkpoints, never silently retry I/O.

        pool/mint are checked against independently derived canonical identities.
        Receipt/source/network/genesis/time/refs are not candidate arguments.
        Results are point labels only. Any exception yields an explicit blocker.
        """
        outcome = {'status': 'BLOCKED', 'eligible_for_trading': False, 'chain_authenticated': False,
                   'historical_interval_exclusion_allowed': False, 'provider_calls': 0}
        try:
            need(identity(capture_id), 'CAPTURE_ID_INVALID')
            with self._locks():
                progress = HistoryProgress(self.store)
                self._binding(progress, investigation)
                keys = canonical_accounts(investigation.mint)
                need(pool == keys[0] and investigation.mint != SOL, 'CANONICAL_POOL_MINT_MISMATCH')
                descriptor = {'investigation': asdict(investigation), 'pool': pool, 'mint': investigation.mint,
                              'source': asdict(self.transport.source), 'profile': PROFILE}
                with self._db() as c:
                    self._initialize(c); runs, _, _ = self._audit(c)
                self._publish_all(runs, pool, investigation.mint, block_incomplete=False)
                self._check_prior({k: v for k, v in runs.items() if k != capture_id}, pool, investigation.mint)
                with self._db() as c:
                    if capture_id in runs:
                        need(runs[capture_id]['descriptor'] == descriptor, 'CAPTURE_IDENTITY_MISMATCH')
                    else:
                        # No fresh ID may replace a prior ambiguous observation.
                        self._check_prior(runs, pool, investigation.mint)
                        self._append(c, capture_id, {'descriptor': descriptor})
                for stage in ('genesis', 'slot', 'snapshot', 'time'):
                    with self._db() as c: runs, _, _ = self._audit(c)
                    run = runs[capture_id]; steps = run['steps']
                    need(all(s['state'] == 'DONE' for s in steps), 'CAPTURE_IN_FLIGHT_OR_FAILED')
                    records = self._records(run)
                    if 'genesis' in records: need(records['genesis']['result'] == GENESIS, 'CAPTURE_GENESIS_MISMATCH')
                    if 'slot' in records: need(integer(records['slot']['result']), 'CAPTURE_SLOT_AMBIGUOUS')
                    if stage in records: continue
                    if stage == 'genesis': method, params = 'getGenesisHash', []
                    elif stage == 'slot': method, params = 'getSlot', [{'commitment': 'finalized'}]
                    elif stage == 'snapshot':
                        method, params = 'getMultipleAccounts', [keys, {'encoding': 'base64', 'commitment': 'finalized',
                                                                     'minContextSlot': records['slot']['result']}]
                    else:
                        result = records['snapshot']['result']
                        need(isinstance(result, dict) and isinstance(result.get('context'), dict)
                             and integer(result['context'].get('slot')), 'CAPTURE_SCOPE_AMBIGUOUS')
                        method, params = 'getBlockTime', [result['context']['slot']]
                    # Report charged attempts, including failures, not refused reservations.
                    before = progress.admission(investigation.scan_id)['requests_used']
                    try: self._request(progress, investigation, capture_id, stage, method, params)
                    finally:
                        after = progress.admission(investigation.scan_id)['requests_used']
                        outcome['provider_calls'] += after-before
                with self._db() as c: runs, _, _ = self._audit(c)
                published = self._publish_all(runs, pool, investigation.mint)
                run = runs[capture_id]; records = self._records(run)
                slot = records['snapshot']['result']['context']['slot']; at = records['time']['result']
                # Use the freshly reconstructed guarded policy, never a detached
                # policy. Publication from other sources cannot race replay.
                with self.ledger.policy_view(pool=pool, mint=investigation.mint, slot=slot) as view:
                    receipt = published[capture_id]
                    labels = [admit_pool_vault(account=account, pool=pool, mint=investigation.mint,
                              snapshot_slot=slot, snapshot_time=at, now=self._now(), refs=receipt.refs,
                              policy=view.policy, load=view.load_evidence) for account in keys[4:]]
                outcome.update(status='CAPTURED_POINT' if all(r['snapshot_label_admitted'] for r in labels) else 'BLOCKED',
                               labels=labels, snapshot_slot=slot, snapshot_time=at)
                if outcome['status'] == 'BLOCKED': outcome['reason'] = 'POINT_ADMISSION_REJECTED'
        except Exception as exc:
            outcome['reason'] = str(exc) if type(exc) is CaptureBlocked else 'CAPTURE_EVIDENCE_BLOCKED'
        return outcome

    @staticmethod
    def _check_prior(runs, pool, mint):
        need(all(len(r['steps']) == 4 and all(s['state'] == 'DONE' for s in r['steps'])
                 for r in runs.values() if (r['descriptor']['pool'], r['descriptor']['mint']) == (pool, mint)),
             'PRIOR_CAPTURE_UNRESOLVED')
