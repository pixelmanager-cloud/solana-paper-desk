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

CAP = 60
WINDOW_SECONDS = 3600
VERSION = 1


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
        self.config_hash = digest(cfg)
        self.code_hash = _implementation()
        self.clock = clock

    def _checkpoint(self):
        canonical_job_path(self.ledger)
        with closing(sqlite3.connect(self.ledger.as_uri()+'?mode=ro', uri=True)) as c:
            c.execute('BEGIN')
            state = read_checkpoint(c)
            metadata = dict(c.execute('SELECT key,value FROM metadata'))
            if (state is None or metadata.get('config_hash') != self.config_hash
                    or metadata.get('implementation_hash') != self.code_hash):
                raise MonitoringBlocked('MONITORING_CHECKPOINT_IDENTITY_INVALID')
            return state, c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0]

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
                    for action in ('UPDATE', 'DELETE'):
                        c.execute(f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Original monitoring record is immutable'); END")
                c.execute('INSERT INTO paper_monitoring_budget VALUES(1,?,?,?,?,?,?,0,0,NULL)',
                          (VERSION, str(self.ledger), self.config_hash, self.code_hash, CAP, WINDOW_SECONDS))
                c.commit()
            except BaseException:
                c.rollback()
                raise

    def _accounting(self, c):
        row = c.execute('SELECT version,ledger,config_hash,code_hash,cap,window_seconds,high_water,total,blocked FROM paper_monitoring_budget WHERE id=1').fetchone()
        if (row is None or row[:6] != (VERSION, str(self.ledger), self.config_hash, self.code_hash, CAP, WINDOW_SECONDS)
                or type(row[6]) not in (int,float) or not math.isfinite(row[6])
                or type(row[7]) is not int or not 0 <= row[6] < 2**63 or row[7] < 0):
            raise MonitoringBlocked('MONITORING_ACCOUNTING_INVALID')
        count, largest, earliest, latest = c.execute('SELECT count(*),max(id),min(at),max(at) FROM paper_monitoring_reservations').fetchone()
        if (count != row[7] or (largest or 0) != count
                or (count and (earliest < 0 or latest > row[6]))
                or c.execute('SELECT 1 FROM paper_monitoring_outcomes o LEFT JOIN paper_monitoring_reservations r ON r.id=o.reservation_id WHERE r.id IS NULL LIMIT 1').fetchone()):
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
            buys = [(identity, json.loads(raw)) for identity, raw in c.execute(
                "SELECT event_id,payload FROM outcomes WHERE json_extract(payload,'$.type')='fill' AND json_extract(payload,'$.side')='buy' AND json_extract(payload,'$.mint')=?", (mint,))]
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
                    if c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone():
                        raise MonitoringBlocked('MONITORING_OUTCOME_PENDING')
                    if now < row[6]:
                        c.execute("UPDATE paper_monitoring_budget SET blocked='CLOCK_ROLLBACK' WHERE id=1")
                        c.commit()
                        raise MonitoringBlocked('MONITORING_CLOCK_ROLLBACK')
                    c.execute('UPDATE paper_monitoring_budget SET high_water=? WHERE id=1', (now,))
                    used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (now-WINDOW_SECONDS,)).fetchone()[0]
                    if used >= CAP:
                        c.commit()
                        raise MonitoringBlocked('MONITORING_REQUEST_BUDGET_EXHAUSTED')
                    identity = row[7]+1
                    c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)',
                              (identity, now, scan_id, admission['descriptor']['mint'], checkpoint_hash, method, digest(params)))
                    c.execute('UPDATE paper_monitoring_budget SET total=? WHERE id=1', (identity,))
                    c.commit()
                except BaseException:
                    c.rollback()
                    raise
            return {'kind':'open_paper_monitoring_reservation_v1', 'id':identity,
                    'total_used':identity, 'window_used':used+1, 'cap':CAP,
                    'window_seconds':WINDOW_SECONDS, 'checkpoint_hash':checkpoint_hash,
                    'investigation_requests_used':admission['requests_used']}
        except MonitoringBlocked:
            raise
        except Exception:
            raise MonitoringBlocked('MONITORING_EVIDENCE_INVALID') from None

    def snapshot(self):
        """Read-only diagnostics and monotonic sequence; never refresh/reset state."""
        self._checkpoint()
        now = self.clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now < 2**63:
            raise MonitoringBlocked('MONITORING_CLOCK_INVALID')
        with self.store.connect() as c:
            c.execute('BEGIN')
            row = self._accounting(c)
            used = c.execute('SELECT count(*) FROM paper_monitoring_reservations WHERE at>?', (now-WINDOW_SECONDS,)).fetchone()[0]
            pending = bool(c.execute('SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone())
        blockers = []
        if now < row[6]: blockers.append('MONITORING_CLOCK_ROLLBACK')
        if row[8] is not None: blockers.append('MONITORING_RECOVERY_REQUIRED')
        if pending: blockers.append('MONITORING_OUTCOME_PENDING')
        if used >= CAP: blockers.append('MONITORING_REQUEST_BUDGET_EXHAUSTED')
        return {'kind':'open_paper_monitoring_budget_v1',
                'status':'STALE_UNVERIFIED_BLOCKED' if blockers else 'AVAILABLE',
                'blockers':blockers, 'total_used':row[7], 'window_used':used,
                'remaining':max(0,CAP-used), 'cap':CAP, 'window_seconds':WINDOW_SECONDS,
                'high_water':row[6], 'entries_enabled':False, 'actual_fill_verified':False}

    def retain_outcome(self, reservation, evidence_hash):
        record = self.store.load(evidence_hash)
        if record.get('monitoring_reservation') != reservation:
            raise MonitoringBlocked('MONITORING_OUTCOME_BINDING_INVALID')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                self._accounting(c)
                row = c.execute('SELECT scan_id,method,params_hash FROM paper_monitoring_reservations WHERE id=?', (reservation['id'],)).fetchone()
                if row != (record['scan_id'], record['method'], digest(record['params'])):
                    raise MonitoringBlocked('MONITORING_OUTCOME_BINDING_INVALID')
                old = c.execute('SELECT evidence_hash FROM paper_monitoring_outcomes WHERE reservation_id=?', (reservation['id'],)).fetchone()
                if old and old[0] != evidence_hash:
                    raise MonitoringBlocked('MONITORING_OUTCOME_BINDING_INVALID')
                c.execute('INSERT OR IGNORE INTO paper_monitoring_outcomes VALUES(?,?)', (reservation['id'], evidence_hash))
                if record.get('failure_code') is not None:
                    c.execute("UPDATE paper_monitoring_budget SET blocked='SOURCE_FAILURE' WHERE id=1")
                c.commit()
            except BaseException:
                c.rollback()
                raise
