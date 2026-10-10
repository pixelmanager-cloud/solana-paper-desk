"""Fixed, shared open-paper-position allowance; no investigation admission/reset.

Trusted coordinator provisions once and holds research -> evidence invocation ->
paper-cycle locks throughout reads. The existing checkpoint reader is the position
authority; caller target claims alone never authorize a reservation. Clock rollback
and corrupted accounting require recovery, never a new window/counter.
"""
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import time

from .evidence import EvidenceStore
from .history_progress import canonical_ownership_path
from .job_persistence import canonical_job_path
from .model import canonical, digest
from .paper_checkpoint import read_checkpoint
from .programs import address
from .providers import SOL
from . import allowance_policy as policy

CAP = 60
WINDOW_SECONDS = 3600
VERSION = 1
# A failed read is reserved, charged and retained with its outcome whatever the provider did, so it is
# not a store-wide latch: the next pass for the same position must be able to try again. Only a failure
# that makes the retained original itself untrustworthy keeps the one-way latch.
LATCHING_FAILURE_CODES = frozenset({'CLOCK_INVALID', 'OUTCOME_PERSISTENCE_FAILED'})
ABANDONED_KIND = 'paper_monitoring_abandoned_v1'
MAX_ABANDONED = 64


class MonitoringBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _implementation():
    root = Path(__file__).parent
    return digest({str(p.relative_to(root)): p.read_text()
                   for p in sorted(root.rglob('*'))
                   if p.is_file() and p.suffix in ('.py', '.json')})


class MonitoringBudget:
    def __init__(self, store, ledger_db, cfg, *, clock=time.time):
        if type(store) is not EvidenceStore or store.read_only or not callable(clock):
            raise MonitoringBlocked('MONITORING_CONFIGURATION_INVALID')
        self.store = store
        self.path = canonical_ownership_path(store.path)
        self.ledger = canonical_job_path(ledger_db)
        if self.path == self.ledger or not self.path.is_file() or not self.ledger.is_file():
            raise MonitoringBlocked('MONITORING_CONFIGURATION_INVALID')
        if (type(cfg) is not dict or cfg.get('mode') != 'paper'
                or type(cfg.get('paper_quote_execution_version')) is not int
                or cfg['paper_quote_execution_version'] != 1):
            raise MonitoringBlocked('MONITORING_CONFIGURATION_INVALID')
        from .kraken_usd_observation import selected as usd_selected
        self.usd_valuation_version=usd_selected(cfg)
        self.config_hash = digest(cfg)
        self.code_hash = _implementation()
        self.clock = clock

    def _checkpoint(self):
        from .monitoring_handoff import validate_active
        try:
            with self.store.connect() as evidence:
                evidence.execute('BEGIN')
                verified=validate_active(evidence,self,checkpoint=True)
                if verified is not None:
                    # Reuse only the checkpoint just validated in this call; no
                    # cache survives transaction close or provider I/O.
                    return verified[1]['state'],verified[1]['payload']
        except (ValueError,sqlite3.Error,OSError,TypeError,KeyError,RecursionError):
            raise MonitoringBlocked('MONITORING_CONTEXT_BINDING_INVALID') from None
        canonical_job_path(self.ledger)
        with closing(sqlite3.connect(self.ledger.as_uri()+'?mode=ro', uri=True)) as c:
            c.execute('BEGIN')
            state = read_checkpoint(c)
            metadata = dict(c.execute('SELECT key,value FROM metadata'))
            if (state is None or metadata.get('config_hash') != self.config_hash
                    or not self._compatible(c, metadata)):
                raise MonitoringBlocked('MONITORING_CHECKPOINT_IDENTITY_INVALID')
            return state, c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]

    def _compatible(self, c, metadata):
        try:
            from .runtime_compatibility import require_runtime
        except ImportError:
            # Standalone component retains the existing exact native-code rule;
            # predecessor adoption requires worker07's reviewed resolver.
            return metadata.get('implementation_hash') == self.code_hash
        try:
            return require_runtime(c, implementation=self.code_hash) == self.code_hash
        except ValueError:
            return False

    def prepare_upgrade(self, c, *, at, provenance):
        """Prepare immutable transition data under caller's existing locks/txn.

        No publication or ledger authority is inferred. The runtime coordinator
        must bind this exact hash into its compatibility prepare/seal protocol.
        """
        if provenance != policy.PROVENANCE or type(at) not in (int, float) or not math.isfinite(at):
            raise MonitoringBlocked('MONITORING_UPGRADE_INVALID')
        existing = policy.monitoring_policy(c)
        if existing:
            self._accounting(c)
            return existing
        row = self._accounting(c)
        if not row[6] <= at < 2**63:
            raise MonitoringBlocked('MONITORING_CLOCK_ROLLBACK')
        body = {'kind':'monitoring_allowance_upgrade_v1', 'ledger':str(self.ledger),
                'evidence':str(self.path), 'config_hash':self.config_hash,
                'predecessor_code':row[3], 'successor_code':self.code_hash,
                'old_budget':list(row), 'reservation_cutoff':row[7],
                'cap':policy.NEW_MONITORING, 'at':at, 'provenance':provenance}
        return body, digest(body)

    def activate_upgrade(self, c, prepared):
        """Exact evidence-side atomic publication; caller owns BEGIN IMMEDIATE.

        No checkpoint/code trust is granted here. Runtime readers must reject
        until their independently bound compatibility seal is complete.
        """
        if not c.in_transaction or Path(c.execute('PRAGMA database_list').fetchone()[2]).resolve() != self.path:
            raise MonitoringBlocked('MONITORING_UPGRADE_TRANSACTION_REQUIRED')
        body, key = prepared
        if digest(body) != key:
            raise MonitoringBlocked('MONITORING_UPGRADE_INVALID')
        existing = policy.monitoring_policy(c)
        if existing:
            if existing != prepared:
                raise MonitoringBlocked('MONITORING_UPGRADE_ALREADY_BOUND')
            self._accounting(c)
            return key
        actual = self.prepare_upgrade(c, at=body['at'], provenance=body['provenance'])
        if actual != prepared:
            raise MonitoringBlocked('MONITORING_UPGRADE_CHANGED')
        policy.install(c, policy.MONITORING, body)
        c.execute('UPDATE paper_monitoring_budget SET version=2,cap=? WHERE id=1',
                  (policy.NEW_MONITORING,))
        self._accounting(c)
        return key

    def provision(self):
        """Explicit coordinator operation; never called by provider read paths."""
        self._checkpoint()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                # Refuse adoption of partial/pre-existing schemas.
                names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'paper_monitoring_%'")}
                if names:
                    self._accounting(c)
                    c.commit()
                    return
                c.execute('CREATE TABLE paper_monitoring_budget(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,ledger TEXT NOT NULL,config_hash TEXT NOT NULL,code_hash TEXT NOT NULL,cap INTEGER NOT NULL,window_seconds INTEGER NOT NULL,high_water REAL NOT NULL,total INTEGER NOT NULL,blocked TEXT)')
                c.execute('CREATE TABLE paper_monitoring_reservations(id INTEGER PRIMARY KEY,at REAL NOT NULL,scan_id TEXT NOT NULL,mint TEXT NOT NULL,checkpoint_hash TEXT NOT NULL,method TEXT NOT NULL,params_hash TEXT NOT NULL)')
                c.execute('CREATE TABLE paper_monitoring_outcomes(reservation_id INTEGER PRIMARY KEY REFERENCES paper_monitoring_reservations(id),evidence_hash TEXT NOT NULL)')
                for table in ('paper_monitoring_reservations', 'paper_monitoring_outcomes'):
                    primary = 'id' if table == 'paper_monitoring_reservations' else 'reservation_id'
                    # REPLACE's implicit DELETE does not run DELETE triggers on
                    # fresh SQLite connections. Check NEW PK (also every rowid
                    # alias) before the conflict resolution can delete originals.
                    c.execute(f"CREATE TRIGGER {table}_insert BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {primary}=NEW.{primary}) BEGIN SELECT RAISE(ABORT,'Original monitoring identity already exists'); END")
                    for action in ('UPDATE', 'DELETE'):
                        c.execute(f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Original monitoring record is immutable'); END")
                c.execute('INSERT INTO paper_monitoring_budget VALUES(1,?,?,?,?,?,?,0,0,NULL)',
                          (VERSION, str(self.ledger), self.config_hash, self.code_hash, CAP, WINDOW_SECONDS))
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _accounting(self, c, *, checkpoint=False):
        row = c.execute('SELECT version,ledger,config_hash,code_hash,cap,window_seconds,high_water,total,blocked FROM paper_monitoring_budget WHERE id=1').fetchone()
        from .monitoring_handoff import validate_active
        try:
            verified=validate_active(c,self,checkpoint=checkpoint)
            handoff=verified[0] if checkpoint and verified is not None else verified
        except (ValueError,sqlite3.Error,OSError,TypeError,KeyError,RecursionError):
            raise MonitoringBlocked('MONITORING_CONTEXT_BINDING_INVALID') from None
        if checkpoint and handoff is None:self._checkpoint()
        upgraded=policy.monitoring_policy(c)
        with closing(sqlite3.connect(self.ledger.as_uri()+'?mode=ro',uri=True)) as ledger:
            origin=ledger.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone()
        origin=origin[0] if origin else None
        expected=(VERSION,str(self.ledger),self.config_hash,origin,CAP,WINDOW_SECONDS)
        if upgraded:
            grant,key=upgraded
            binding=handoff[0] if handoff else None
            grant_ledger=binding['context']['old_ledger_db'] if binding else str(self.ledger)
            grant_config=binding['old_config_hash'] if binding else self.config_hash
            grant_source=binding['old_source'] if binding else self.code_hash
            grant_origin=binding['original_budget'][3] if binding else origin
            if (grant['ledger']!=grant_ledger or grant['evidence']!=str(self.path)
                    or grant['config_hash']!=grant_config or grant['predecessor_code']!=grant_origin
                    or grant['successor_code']!=grant_source):
                raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
            expected=(3 if handoff else 2,grant_ledger,grant_config,grant_origin,policy.NEW_MONITORING,WINDOW_SECONDS)
        if (row is None or row[:6] != expected
                or type(row[6]) not in (int,float) or not math.isfinite(row[6])
                or type(row[7]) is not int or not 0 <= row[6] < 2**63 or row[7] < 0):
            raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
        if handoff and (row[7]<handoff[0]['reservation_cutoff'] or row[6]<handoff[0]['original_budget'][6]
                or (handoff[0]['original_budget'][8] is not None and row[8]!=handoff[0]['original_budget'][8])):
            raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
        if upgraded and (row[7]<grant['reservation_cutoff'] or row[6]<grant['old_budget'][6]
                or (grant['old_budget'][8] is not None and row[8]!=grant['old_budget'][8])):
            raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
        if upgraded and c.execute(
                'SELECT 1 FROM paper_monitoring_reservations WHERE id>? AND at<? LIMIT 1',
                (grant['reservation_cutoff'], grant['at'])).fetchone():
            raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
        count, largest, earliest, latest = c.execute('SELECT count(*),max(id),min(at),max(at) FROM paper_monitoring_reservations').fetchone()
        if (count != row[7] or (largest or 0) != count
                or (count and (earliest < 0 or latest > row[6]))
                or c.execute('SELECT 1 FROM paper_monitoring_outcomes o LEFT JOIN paper_monitoring_reservations r ON r.id=o.reservation_id WHERE r.id IS NULL LIMIT 1').fetchone()):
            raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
        # Check restored/corrupt completed rows against original hash-addressed
        # transport receipts, not merely count/min/max. Pending rows never permit
        # subsequent I/O, so they cannot silently become replacement allowances.
        from .monitoring_successor import rows as successor_rows
        history=successor_rows(c,handoff) if handoff else []
        for identity, at, scan, mint, checkpoint, method, params_hash, evidence_hash in c.execute(
                'SELECT r.id,r.at,r.scan_id,r.mint,r.checkpoint_hash,r.method,r.params_hash,o.evidence_hash FROM paper_monitoring_reservations r JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id'):
            try:
                original = self.store.load(evidence_hash)
                receipt = original.get('monitoring_reservation', {})
            except (ValueError, TypeError, AttributeError):
                raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID') from None
            if handoff and identity>handoff[0]['reservation_cutoff'] and receipt.get('context_hash')!=handoff[1]:
                raise MonitoringBlocked('MONITORING_CONTEXT_BINDING_INVALID')
            expected_successor=next((k for v,k in reversed(history) if identity>v['reservation_cutoff']),None)
            if receipt.get('successor_context_hash')!=expected_successor:raise MonitoringBlocked('MONITORING_CONTEXT_BINDING_INVALID')
            new_receipt=bool(upgraded and identity>grant['reservation_cutoff'])
            cap=policy.NEW_MONITORING if new_receipt else CAP
            kind='open_paper_monitoring_reservation_v2' if new_receipt else 'open_paper_monitoring_reservation_v1'
            if (type(receipt) is not dict or type(receipt.get('id')) is not int
                    or type(receipt.get('total_used')) is not int
                    or receipt.get('kind') != kind
                    or receipt.get('cap') != cap
                    or (new_receipt and receipt.get('policy_hash')!=key) or receipt.get('window_seconds') != WINDOW_SECONDS
                    or receipt.get('id') != identity or receipt.get('total_used') != identity
                    or receipt.get('reserved_at') != at or receipt.get('checkpoint_hash') != checkpoint
                    or receipt.get('mint') != mint
                    or original.get('scan_id') != scan or original.get('method') != method
                    or (original.get('params_hash') != params_hash or original.get('failure_code') != 'ABANDONED'
                        or original.get('request_completed') is not None
                        if original.get('kind') == ABANDONED_KIND
                        else digest(original.get('params')) != params_hash)):
                raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
        return row

    def _held(self, progress, scan_id):
        if canonical_ownership_path(progress.store.path) != self.path:
            raise MonitoringBlocked('MONITORING_EVIDENCE_STORE_MISMATCH')
        admission = progress.admission(scan_id)
        if admission is None or admission['state'] not in ('ADMITTED', 'SEALED'):
            raise MonitoringBlocked('MONITORING_ORIGINAL_ADMISSION_REQUIRED')
        mint = admission['descriptor']['mint']
        state, payload = self._checkpoint()
        position = state['positions'].get(mint)
        if position is None:
            raise MonitoringBlocked('MONITORING_OPEN_POSITION_REQUIRED')
        # The checkpoint validates original quote journals and raw inventory.
        # Recover the original buy identity even for compatible v1 experiments.
        with closing(sqlite3.connect(self.ledger.as_uri()+'?mode=ro', uri=True)) as c:
            c.execute('BEGIN')
            if c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0] != payload:
                raise MonitoringBlocked('MONITORING_CHECKPOINT_CHANGED')
            # The checkpoint reader already verified active entry identity and
            # replayed the entire original journal, including closed lifecycles.
            # V3 pins entry_event_id; compatible v1 pins its unique opened_at buy.
            # Neither historical BUY count nor arbitrary latest BUY is authority.
            entry_id = position.get('entry_event_id')
            buys = [(identity, json.loads(raw)) for identity, raw in c.execute(
                "SELECT o.event_id,o.payload FROM outcomes o JOIN events e ON e.event_id=o.event_id WHERE json_extract(o.payload,'$.type')='fill' AND json_extract(o.payload,'$.side')='buy' AND json_extract(o.payload,'$.mint')=? AND e.ts=? AND (? IS NULL OR o.event_id=?)",
                (mint,position['opened_at'],entry_id,entry_id))]
            if len(buys) != 1:
                raise MonitoringBlocked('MONITORING_ENTRY_IDENTITY_INVALID')
            event_row = c.execute('SELECT payload,payload_hash FROM events WHERE event_id=?', (buys[0][0],)).fetchone()
            original = json.loads(event_row[0]) if event_row else None
            if (original is None or digest(original) != event_row[1]
                    or original.get('paper_source_evidence', {}).get('scan_id') != scan_id
                    or original.get('mint') != mint or original.get('pool') != position['pool']
                    or original.get('taker') != position['taker']):
                raise MonitoringBlocked('MONITORING_ENTRY_IDENTITY_INVALID')
        from .quote_execution import raw_quantity
        raw = raw_quantity(position['qty'], position['quote_execution']['mint_decimals'])
        for key in (mint, position['pool'], position['taker']):
            address(key)
        return admission, position, original, raw, digest(json.loads(payload))

    def _request(self, method, params, position, original, mint, raw):
        if method == 'jupiter_probe':
            if (type(params) is not dict or params.get('inputMint') != mint
                    or params.get('outputMint') != SOL or params.get('taker') != position['taker']
                    or type(params.get('amount')) is not str or not params['amount'].isascii()
                    or not params['amount'].isdigit() or len(params['amount']) > 20
                    or not 0 < int(params['amount']) <= raw):
                raise MonitoringBlocked('MONITORING_SELL_QUOTE_REQUIRED')
        elif method == 'getAccountInfo':
            if type(params) is not list or len(params) != 2 or params[0] not in (mint, position['pool']):
                raise MonitoringBlocked('MONITORING_POSITION_REQUEST_REQUIRED')
        elif method == 'getMultipleAccounts':
            refs = original['paper_source_evidence'].get('collector_refs', [])
            if type(refs) is not list or not 1 <= len(refs) <= 20:
                raise MonitoringBlocked('MONITORING_ORIGINAL_POOL_REQUIRED')
            pools = [record for record in (self.store.load(key) for key in refs)
                     if record.get('kind') == 'pool_snapshot']
            if len(pools) != 1 or digest(pools[0]) != original['paper_source_evidence'].get('pool_hash'):
                raise MonitoringBlocked('MONITORING_ORIGINAL_POOL_REQUIRED')
            from .pools import parse_pool
            from .fee_config import config_address
            from .dynamic_fees import fee_address
            fields = parse_pool(pools[0]['discovery']['value'])
            expected = [fields['pool_base_token_account'],fields['pool_quote_token_account'],fields['lp_mint'],
                        position['pool'],config_address(),str(fee_address()[0]),mint]
            if type(params) is not list or len(params) != 2 or params[0] != expected:
                raise MonitoringBlocked('MONITORING_POSITION_REQUEST_REQUIRED')
        elif method == 'getSlot':
            if params != [{'commitment':'finalized'}]:
                raise MonitoringBlocked('MONITORING_POSITION_REQUEST_REQUIRED')
        elif method == 'getBlockTime':
            if type(params) is not list or len(params) != 1 or type(params[0]) is not int or not 0 <= params[0] < 2**63:
                raise MonitoringBlocked('MONITORING_POSITION_REQUEST_REQUIRED')
        elif method == 'kraken_solusd_trades_v1':
            from .kraken_usd_observation import selected as usd_selected,PARAMS
            if not self.usd_valuation_version or params!=PARAMS:raise MonitoringBlocked('MONITORING_POSITION_REQUEST_REQUIRED')
        elif method == 'jupiter_price_v3':
            if params != {'ids':SOL}:
                raise MonitoringBlocked('MONITORING_POSITION_REQUEST_REQUIRED')
        else:
            raise MonitoringBlocked('MONITORING_METHOD_NOT_ALLOWED')

    def reserve_read(self, progress, scan_id, method, params):
        """One committed attempted read shared by all currently held positions."""
        try:
            admission, position, original, raw, checkpoint_hash = self._held(progress, scan_id)
            self._request(method, params, position, original, admission['descriptor']['mint'], raw)
            now = self.clock()
            if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now < 2**63:
                raise MonitoringBlocked('MONITORING_CLOCK_INVALID')
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                try:
                    row = self._accounting(c)
                    if row[8] is not None:
                        raise MonitoringBlocked('MONITORING_RECOVERY_REQUIRED')
                    transition = policy.monitoring_policy(c)
                    clock_floor = max(row[6], transition[0]['at']) if transition else row[6]
                    from .monitoring_handoff import read as read_handoff
                    handoff=read_handoff(c)
                    from .monitoring_successor import active as active_handoff
                    successor=active_handoff(c,handoff) if handoff else None
                    if successor:clock_floor=max(clock_floor,successor[0]['at'])
                    if now < clock_floor:
                        c.execute("UPDATE paper_monitoring_budget SET blocked='CLOCK_ROLLBACK' WHERE id=1")
                        c.commit()
                        raise MonitoringBlocked('MONITORING_CLOCK_ROLLBACK')
                    if c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone():
                        raise MonitoringBlocked('MONITORING_OUTCOME_PENDING')
                    c.execute('UPDATE paper_monitoring_budget SET high_water=? WHERE id=1', (now,))
                    used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (now-WINDOW_SECONDS,)).fetchone()[0]
                    cap=row[4]
                    if used >= cap:
                        c.commit()
                        raise MonitoringBlocked('MONITORING_REQUEST_BUDGET_EXHAUSTED')
                    identity = row[7]+1
                    values=(identity, now, scan_id, admission['descriptor']['mint'], checkpoint_hash, method, digest(params))
                    if handoff and successor[1]!=handoff[1]:
                        c.execute('INSERT INTO paper_monitoring_reservations(id,at,scan_id,mint,checkpoint_hash,method,params_hash,context_hash,successor_context_hash) VALUES(?,?,?,?,?,?,?,?,?)',values+(handoff[1],successor[1]))
                    elif handoff:
                        c.execute('INSERT INTO paper_monitoring_reservations(id,at,scan_id,mint,checkpoint_hash,method,params_hash,context_hash) VALUES(?,?,?,?,?,?,?,?)',values+(handoff[1],))
                    else:c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)',values)
                    c.execute('UPDATE paper_monitoring_budget SET total=? WHERE id=1', (identity,))
                    transition_hash = policy.monitoring_policy(c)[1] if row[0] in (2,3) else None
                    c.commit()
                except BaseException:
                    c.rollback()
                    raise
            receipt={'kind':'open_paper_monitoring_reservation_v2' if row[0] in (2,3) else 'open_paper_monitoring_reservation_v1', 'id':identity,
                    'reserved_at':now,
                    'mint':admission['descriptor']['mint'],
                    'total_used':identity, 'window_used':used+1, 'cap':cap,
                    'window_seconds':WINDOW_SECONDS, 'checkpoint_hash':checkpoint_hash,
                    'investigation_requests_used':admission['requests_used']}
            if row[0] in (2,3):receipt['policy_hash']=transition_hash
            if handoff:receipt['context_hash']=handoff[1]
            if handoff and successor[1]!=handoff[1]:receipt['successor_context_hash']=successor[1]
            return receipt
        except MonitoringBlocked:
            raise
        except Exception:
            raise MonitoringBlocked('MONITORING_EVIDENCE_INVALID') from None

    def snapshot(self):
        """Read-only diagnostics and monotonic sequence; never refresh/reset state."""
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now < 2**63:
            raise MonitoringBlocked('MONITORING_CLOCK_INVALID')
        with self.store.connect() as c:
            c.execute('BEGIN')
            # Handoff accounting already verifies the complete active checkpoint
            # once. Native contexts still require the explicit checkpoint read.
            row = self._accounting(c,checkpoint=True)
            transition = policy.monitoring_policy(c)
            clock_floor = max(row[6], transition[0]['at']) if transition else row[6]
            from .monitoring_handoff import read as read_handoff
            handoff=read_handoff(c)
            from .monitoring_successor import active as active_handoff
            tail=active_handoff(c,handoff) if handoff else None
            if tail:clock_floor=max(clock_floor,tail[0]['at'])
            used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (now-WINDOW_SECONDS,)).fetchone()[0]
            pending = bool(c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone())
        blockers = []
        if now < clock_floor: blockers.append('MONITORING_CLOCK_ROLLBACK')
        if row[8] is not None: blockers.append('MONITORING_RECOVERY_REQUIRED')
        if pending: blockers.append('MONITORING_OUTCOME_PENDING')
        if used >= row[4]: blockers.append('MONITORING_REQUEST_BUDGET_EXHAUSTED')
        return {'kind':'open_paper_monitoring_budget_v1',
                'status':'STALE_UNVERIFIED_BLOCKED' if blockers else 'AVAILABLE',
                'blockers':blockers, 'total_used':row[7], 'window_used':used,
                'remaining':max(0,row[4]-used), 'cap':row[4], 'window_seconds':WINDOW_SECONDS,
                'high_water':row[6], 'entries_enabled':False, 'actual_fill_verified':False}

    def abandon_pending(self, *, cause='LEASE_GONE'):
        """Append an explicit ABANDONED outcome for every reservation whose reader is gone.

        The caller holds research -> evidence -> ledger locks, so no reader can be mid-request: a reservation
        without an outcome was committed by a process that died before recording one. Nothing is deleted, reset
        or refunded (the reservation stays charged and counted); the unknown request is recorded as never
        completed. Returns the resolved reservation ids.
        """
        if type(cause) is not str or not 1 <= len(cause) <= 128:
            raise MonitoringBlocked('MONITORING_OUTCOME_BINDING_INVALID')
        planned = []
        with self.store.connect() as c:          # read phase: EvidenceStore.save opens its own writer
            c.execute('BEGIN')
            row = self._accounting(c, checkpoint=True)
            pending = c.execute('SELECT r.id,r.at,r.scan_id,r.mint,r.checkpoint_hash,r.method,r.params_hash '
                                'FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o '
                                'ON o.reservation_id=r.id WHERE o.reservation_id IS NULL ORDER BY r.id LIMIT ?',
                                (MAX_ABANDONED + 1,)).fetchall()
            if len(pending) > MAX_ABANDONED:
                raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
            transition = policy.monitoring_policy(c)
            from .monitoring_handoff import read as read_handoff
            handoff = read_handoff(c)
            from .monitoring_successor import rows as successor_rows
            history = successor_rows(c, handoff) if handoff else []
        for identity, at, scan, mint, checkpoint, method, params_hash in pending:
            receipt = {'kind': 'open_paper_monitoring_reservation_v2' if row[0] in (2, 3) else 'open_paper_monitoring_reservation_v1',
                       'id': identity, 'reserved_at': at, 'mint': mint, 'total_used': identity,
                       'window_used': None, 'cap': row[4], 'window_seconds': WINDOW_SECONDS,
                       'checkpoint_hash': checkpoint, 'investigation_requests_used': None}
            if row[0] in (2, 3):
                receipt['policy_hash'] = transition[1]
            if handoff:
                receipt['context_hash'] = handoff[1]
                successor = next((k for v, k in reversed(history) if identity > v['reservation_cutoff']), None)
                if successor is not None:
                    receipt['successor_context_hash'] = successor
            record = {'kind': ABANDONED_KIND, 'failure_code': 'ABANDONED', 'cause': cause, 'scan_id': scan,
                      'method': method, 'params_hash': params_hash, 'request_completed': None,
                      'monitoring_reservation': receipt}
            planned.append((identity, self.store.save(record)))
        if not planned:
            return []
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                self._accounting(c, checkpoint=True)
                still = [r[0] for r in c.execute(
                    'SELECT r.id FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o '
                    'ON o.reservation_id=r.id WHERE o.reservation_id IS NULL ORDER BY r.id')]
                if still != [identity for identity, _ in planned]:
                    raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
                for identity, key in planned:
                    c.execute('INSERT INTO paper_monitoring_outcomes VALUES(?,?)', (identity, key))
                self._accounting(c, checkpoint=True)
                c.commit()
            except BaseException:
                c.rollback()
                raise
        return [identity for identity, _ in planned]

    def retain_outcome(self, reservation, evidence_hash):
        record = self.store.load(evidence_hash)
        if record.get('monitoring_reservation') != reservation:
            raise MonitoringBlocked('MONITORING_OUTCOME_BINDING_INVALID')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                self._accounting(c,checkpoint=True)
                row = c.execute('SELECT scan_id,method,params_hash FROM paper_monitoring_reservations WHERE id=?', (reservation['id'],)).fetchone()
                if row != (record['scan_id'], record['method'], digest(record['params'])):
                    raise MonitoringBlocked('MONITORING_OUTCOME_BINDING_INVALID')
                # No mutation or I/O since the complete preflight above: this
                # BEGIN IMMEDIATE snapshot cannot change between these SELECTs.
                old = c.execute('SELECT evidence_hash FROM paper_monitoring_outcomes WHERE reservation_id=?', (reservation['id'],)).fetchone()
                if old and old[0] != evidence_hash:
                    raise MonitoringBlocked('MONITORING_OUTCOME_BINDING_INVALID')
                if old is None:
                    c.execute('INSERT INTO paper_monitoring_outcomes VALUES(?,?)', (reservation['id'], evidence_hash))
                self._accounting(c,checkpoint=True)
                if _latching_failure(record):
                    c.execute("UPDATE paper_monitoring_budget SET blocked='SOURCE_FAILURE' WHERE id=1 AND blocked IS NULL")
                c.commit()
            except BaseException:
                c.rollback()
                raise

def _latching_failure(record):
    return record.get('failure_code') in LATCHING_FAILURE_CODES


def _research_binding(research, evidence):
    """Existing-only read proof of the actual dispatch/admission context.

    A path or schema alone cannot establish which worker lock protects evidence.
    Require retained admission identities bound through original immutable birth
    dispatch descriptors. No admissions means no available binding proof.
    """
    from contextlib import contextmanager
    from .job_persistence import JobPersistence, BIRTH_ACQUISITION_V1
    from .history_progress import HistoryProgress
    with closing(sqlite3.connect(research.as_uri()+'?mode=ro',uri=True)) as jobs_db, \
            closing(sqlite3.connect(evidence.as_uri()+'?mode=ro',uri=True)) as evidence_db:
        jobs_db.row_factory=sqlite3.Row
        jobs_db.execute('BEGIN');evidence_db.execute('BEGIN')
        jobs_db.execute('SELECT id,mint,created,status,result FROM scans LIMIT 0')
        jobs_db.execute('SELECT scan_id,kind,descriptor_version,descriptor,descriptor_hash,generation,claim_token FROM scan_jobs LIMIT 0')
        evidence_db.execute('SELECT scan_id,job_descriptor_hash,mint_hash,cutoff_hash FROM ownership_acquisition_setup LIMIT 0')
        marker=jobs_db.execute("SELECT version FROM scan_job_migrations WHERE name='legacy_screen_descriptors'").fetchone()
        if marker is None or tuple(marker)!=(1,):
            raise MonitoringBlocked('MONITORING_RESEARCH_BINDING_INVALID')
        # Scalar preflight before rows/JSON. Fail closed on unreviewably large
        # contexts rather than silently checking a subset of admissions.
        count,total,largest,prepared_total,prepared_largest=evidence_db.execute(
            'SELECT count(*),COALESCE(sum(length(CAST(descriptor AS BLOB))),0),'
            'COALESCE(max(length(CAST(descriptor AS BLOB))),0),'
            'COALESCE(sum(length(CAST(prepared_source AS BLOB))),0),'
            'COALESCE(max(length(CAST(prepared_source AS BLOB))),0) FROM ownership_admissions').fetchone()
        if not 1<=count<=10000:
            raise MonitoringBlocked('MONITORING_RESEARCH_BINDING_UNAVAILABLE')
        if total>32*1024*1024 or largest>8192 or prepared_total>32*1024*1024 or prepared_largest>2*1024*1024:
            raise MonitoringBlocked('MONITORING_RESEARCH_BINDING_BOUND')
        jobs=JobPersistence.__new__(JobPersistence);jobs.path=research
        @contextmanager
        def existing_snapshot():
            yield jobs_db
        jobs.connect=existing_snapshot
        for (identity,) in evidence_db.execute('SELECT id FROM ownership_admissions ORDER BY id'):
            size=jobs_db.execute('SELECT length(CAST(descriptor AS BLOB)),length(CAST(s.result AS BLOB)) '
                                 'FROM scan_jobs j JOIN scans s ON s.id=j.scan_id WHERE j.scan_id=?',(identity,)).fetchone()
            if size is None or not 1<=size[0]<=8192 or (size[1] is not None and size[1]>2*1024*1024):
                raise MonitoringBlocked('MONITORING_RESEARCH_BINDING_INVALID')
            dispatch=jobs.descriptor(identity)
            admission=HistoryProgress.inspect_admission(evidence_db,identity)
            setup=evidence_db.execute('SELECT job_descriptor_hash FROM ownership_acquisition_setup WHERE scan_id=?',
                                      (identity,)).fetchone()
            # The durable setup is the evidence-side pin of the original full
            # dispatch, including its canonical research/evidence paths. Matching
            # only mint/created/scan fields would accept a rehashed copied queue.
            if setup is None or tuple(setup)!=(digest(dispatch),):
                raise MonitoringBlocked('MONITORING_RESEARCH_BINDING_INVALID')
            if (dispatch['kind']!=BIRTH_ACQUISITION_V1 or dispatch['evidence_db']!=str(evidence)
                    or admission is None or admission['descriptor']!={'kind':'ownership_admission_v1',
                        'scan_id':identity,'mint':dispatch['mint'],'created':dispatch['admitted_at']}
                    or admission['request_ceiling']!=dispatch['request_ceiling']):
                raise MonitoringBlocked('MONITORING_RESEARCH_BINDING_INVALID')
            if admission['state']=='SEALED' and jobs.source(identity)!=admission['prepared_source']:
                raise MonitoringBlocked('MONITORING_RESEARCH_BINDING_INVALID')


def upgrade_existing(research_db, evidence_db, ledger_db, cfg, *, provenance, clock=time.time):
    """Explicit coordinator CLI seam: existing DBs only, no provisioning."""
    from .paper_observe_cli import _worker_lock
    from .paper_cycle import _lock
    research=canonical_job_path(research_db)
    evidence=canonical_ownership_path(evidence_db)
    ledger=canonical_job_path(ledger_db)
    if len({research,evidence,ledger})!=3 or not all(p.is_file() for p in (research,evidence,ledger)):
        raise MonitoringBlocked('MONITORING_CONFIGURATION_INVALID')
    with _worker_lock(research) as worker:
        if worker is None:raise MonitoringBlocked('MONITORING_RESEARCH_BUSY')
        with _lock(str(evidence)+'.ownership-invocation.lock') as locked:
            if not locked:raise MonitoringBlocked('MONITORING_EVIDENCE_BUSY')
            with _lock(str(ledger)+'.paper-cycle.lock') as locked:
                if not locked:raise MonitoringBlocked('MONITORING_LEDGER_BUSY')
                _research_binding(research, evidence)
                store=EvidenceStore(evidence,read_only=True)
                store.read_only=False
                # Existing only: neither constructor nor connection may create
                # a database or table if deployment paths are wrong.
                store.connect=lambda:sqlite3.connect(evidence.as_uri()+'?mode=rw',uri=True,
                                                    timeout=20,isolation_level=None)
                budget=MonitoringBudget(store,ledger,cfg,clock=clock)
                budget._checkpoint()
                with closing(store.connect()) as c:
                    c.execute('BEGIN IMMEDIATE')
                    try:
                        prepared=budget.prepare_upgrade(c,at=clock(),provenance=provenance)
                        budget.activate_upgrade(c,prepared)
                        c.commit()
                    except BaseException:
                        c.rollback();raise
                return {'status':'EXPLICITLY_ACTIVATED','policy_hash':prepared[1],
                        'reservation_cutoff':prepared[0]['reservation_cutoff'],
                        'budget':budget.snapshot(),
                        'provider_pacing':'UNVERIFIED_SHARED_PACING_REQUIRED'}


def main(argv=None):
    import argparse
    from .paper_cycle_cli import _config
    parser=argparse.ArgumentParser(description='Explicit monitoring allowance upgrade; existing databases only')
    for name in ('research-db','evidence-db','ledger-db','config','provenance'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args(argv)
    try:
        result=upgrade_existing(args.research_db,args.evidence_db,args.ledger_db,_config(args.config),provenance=args.provenance)
        print(json.dumps(result,sort_keys=True));return 0
    except (ValueError,OSError,sqlite3.Error,TypeError,KeyError):
        print(json.dumps({'status':'BLOCKED','blockers':['EXPLICIT_ALLOWANCE_UPGRADE_UNAVAILABLE']}));return 2


if __name__=='__main__':raise SystemExit(main())
