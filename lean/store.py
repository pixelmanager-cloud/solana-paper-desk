"""One SQLite store per lean run (``lean.sqlite``): append-only tables, WAL, raw provider responses retained.

Paper only. Every row carries ``code_version`` and ``strategy_version``, so updating code never needs a migration, a pin or a rotation.
Nothing here halts the trader except an accounting violation (``AccountingHalt``) or a write failure.

Tables (all append-only: UPDATE and DELETE are refused by triggers):
  meta          immutable key/value (store version, initial cash)
  events        generic audit rows written by ``record`` (kind, JSON payload)
  candidates    one row per mint seen
  observations  raw provider bytes (BLOB + sha256) for research
  decisions     screen / entry / exit decisions with reasons and features
  fills         paper fills with the running balances AFTER each fill (tamper evidence)
  errors        typed per-candidate / per-check failures

Positions and cash are DERIVED from fills by replaying ``lean.paper.apply_fill`` (the one implementation of the accounting rules).
Each fill row also stores ``cash_after`` / ``qty_after`` / ``cost_after``; ``check_invariants`` replays the whole history and compares, so
an edited, inserted or reordered fill is caught even if the table triggers were removed.

Thread safety: one connection guarded by an RLock; every write is a single ``BEGIN IMMEDIATE`` transaction.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from lean.paper import AccountingHalt, Fill, LABEL, LAMPORTS, Position, apply_fill, to_lamports

STORE_VERSION = 1
MAX_RAW_BYTES = 4 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
MAX_MESSAGE = 500
_BEARER = re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+')
_KEYED = re.compile(r'(?i)(\b(?:x-)?api[-_]?key|\bapikey|\bauthorization|\baccess[-_]?token|\bsecret|\bpassword)(\s*[=:]\s*)(?!\[REDACTED\])[^&\s"\',;}]+')

__all__ = ['Store', 'StoreError', 'AccountingHalt', 'redact']


class StoreError(RuntimeError):
    pass


def redact(text):
    """Remove anything shaped like a credential (``api-key=...``, ``x-api-key: ...``, ``Authorization: ...``, ``Bearer ...``)."""
    text = _BEARER.sub(r'\1[REDACTED]', str(text))
    return _KEYED.sub(r'\1\2[REDACTED]', text)[:MAX_MESSAGE * 4]


def _json(value, limit=MAX_JSON_BYTES):
    text = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)
    if len(text.encode()) > limit:
        raise StoreError('payload too large')
    return text


TABLES = {
    'meta': 'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'events': ('CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, '
               'code_version TEXT NOT NULL CHECK(length(code_version)>0), strategy_version TEXT NOT NULL CHECK(length(strategy_version)>0))'),
    'candidates': ('CREATE TABLE candidates(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mint TEXT NOT NULL UNIQUE, pool TEXT, '
                   'signature TEXT, slot INTEGER, migrated_at REAL, hint_seq INTEGER, meta TEXT NOT NULL, '
                   'code_version TEXT NOT NULL CHECK(length(code_version)>0), strategy_version TEXT NOT NULL CHECK(length(strategy_version)>0))'),
    'observations': ('CREATE TABLE observations(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, mint TEXT, '
                     'candidate_id INTEGER, raw BLOB NOT NULL, sha256 TEXT NOT NULL, meta TEXT NOT NULL, '
                     'code_version TEXT NOT NULL CHECK(length(code_version)>0), strategy_version TEXT NOT NULL CHECK(length(strategy_version)>0))'),
    'decisions': ('CREATE TABLE decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, mint TEXT, '
                  'candidate_id INTEGER, action TEXT NOT NULL, reasons TEXT NOT NULL, features TEXT NOT NULL, '
                  'code_version TEXT NOT NULL CHECK(length(code_version)>0), strategy_version TEXT NOT NULL CHECK(length(strategy_version)>0))'),
    'fills': ('CREATE TABLE fills(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, mint TEXT NOT NULL, '
              "side TEXT NOT NULL CHECK(side IN ('buy','sell')), qty_raw INTEGER NOT NULL CHECK(qty_raw>0), "
              'sol_lamports INTEGER NOT NULL CHECK(sol_lamports>=0), fee_lamports INTEGER NOT NULL CHECK(fee_lamports>=0), '
              'slippage_bps INTEGER NOT NULL CHECK(slippage_bps>=0), decimals INTEGER NOT NULL, '
              'cost_sold_lamports INTEGER NOT NULL CHECK(cost_sold_lamports>=0), realized_lamports INTEGER NOT NULL, '
              f"quote_ref TEXT, label TEXT NOT NULL CHECK(label='{LABEL}'), candidate_id INTEGER, "
              'cash_after INTEGER NOT NULL CHECK(cash_after>=0), qty_after INTEGER NOT NULL CHECK(qty_after>=0), '
              'cost_after INTEGER NOT NULL CHECK(cost_after>=0), '
              'code_version TEXT NOT NULL CHECK(length(code_version)>0), strategy_version TEXT NOT NULL CHECK(length(strategy_version)>0))'),
    'errors': ('CREATE TABLE errors(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, code TEXT NOT NULL, transient INTEGER NOT NULL '
               'CHECK(transient IN (0,1)), mint TEXT, scope TEXT NOT NULL, message TEXT NOT NULL, '
               'code_version TEXT NOT NULL CHECK(length(code_version)>0), strategy_version TEXT NOT NULL CHECK(length(strategy_version)>0))'),
}
INDEXES = {
    'fills_mint': 'CREATE INDEX fills_mint ON fills(mint, id)',
    'decisions_mint': 'CREATE INDEX decisions_mint ON decisions(mint, id)',
    'observations_mint': 'CREATE INDEX observations_mint ON observations(mint, id)',
    'errors_code': 'CREATE INDEX errors_code ON errors(code, id)',
}


def _guards():
    out = {}
    for table in TABLES:
        for action in ('UPDATE', 'DELETE'):
            out[f'{table}_no_{action.lower()}'] = (f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                                                  "BEGIN SELECT RAISE(ABORT,'lean store is append-only'); END")
    out['fills_sell_exceeds_position'] = (
        "CREATE TRIGGER fills_sell_exceeds_position BEFORE INSERT ON fills WHEN NEW.side='sell' AND NEW.qty_raw > "
        "COALESCE((SELECT qty_after FROM fills WHERE mint=NEW.mint ORDER BY id DESC LIMIT 1),0) "
        "BEGIN SELECT RAISE(ABORT,'sell exceeds the position'); END")
    return out


GUARDS = _guards()


class Store:
    """``Store(path, initial_cash_sol=..., code_version=..., strategy_version=...)``.

    A new file needs ``initial_cash_sol``; an existing one keeps its own (a different value is refused). ``code_version`` and
    ``strategy_version`` are the defaults for rows written without an explicit value."""

    def __init__(self, path, *, initial_cash_sol=None, code_version=None, strategy_version=None, clock=time.time):
        self.path = Path(path)
        self.code_version, self.strategy_version, self.clock = code_version, strategy_version, clock
        self._lock = threading.RLock()
        self._cache = None
        existing = self.path.exists()
        if not existing:
            if initial_cash_sol is None:
                raise StoreError('initial_cash_sol is required for a new store')
            if not self.path.parent.is_dir():
                raise StoreError('store directory missing')
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        elif self.path.is_symlink() or not self.path.is_file():
            raise StoreError('store must be a regular file')
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=10000')
        try:
            if not existing:
                self._create(to_lamports(initial_cash_sol))
            else:
                self._validate(None if initial_cash_sol is None else to_lamports(initial_cash_sol))
        except BaseException:
            self.db.close()
            raise

    # -- schema ------------------------------------------------------------------------------------------------------------
    def _create(self, initial_lamports):
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for sql in (*TABLES.values(), *INDEXES.values(), *GUARDS.values()):
                    self.db.execute(sql)
                self.db.execute('INSERT INTO meta VALUES(?,?)', ('store_version', str(STORE_VERSION)))
                self.db.execute('INSERT INTO meta VALUES(?,?)', ('initial_cash_lamports', str(initial_lamports)))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def _validate(self, initial_lamports):
        schema = dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        expected = {**{k: v for k, v in TABLES.items()}, **INDEXES, **GUARDS}
        if schema != expected:
            raise StoreError('store schema differs from this release (a lean store is never migrated)')
        meta = dict(self.db.execute('SELECT key,value FROM meta'))
        if meta.get('store_version') != str(STORE_VERSION) or not meta.get('initial_cash_lamports', '').isdigit():
            raise StoreError('store metadata invalid')
        if initial_lamports is not None and initial_lamports != int(meta['initial_cash_lamports']):
            raise StoreError('initial cash differs from the store')

    @property
    def initial_cash(self):
        return int(self.db.execute("SELECT value FROM meta WHERE key='initial_cash_lamports'").fetchone()[0])

    def close(self):
        with self._lock:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- writes ------------------------------------------------------------------------------------------------------------
    def _versions(self, code_version, strategy_version):
        code = code_version or self.code_version
        strategy = strategy_version or self.strategy_version
        if not code or not strategy:
            raise StoreError('code_version and strategy_version are required')
        return str(code), str(strategy)

    def _write(self, function):
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                value = function()
                self.db.execute('COMMIT')
                return value
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def _ts(self, ts):
        value = self.clock() if ts is None else ts
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float('inf'), float('-inf')):
            raise StoreError('timestamp invalid')
        return float(value)

    def record(self, kind, payload, *, code_version, strategy_version, ts=None):
        """A generic audit row (config, health, halt reasons, anything not covered by a typed helper). Returns its id."""
        if not isinstance(kind, str) or not 0 < len(kind) <= 64 or not isinstance(payload, dict):
            raise StoreError('kind must be a short string and payload a dict')
        code, strategy = self._versions(code_version, strategy_version)
        text, stamp = _json(payload), self._ts(ts)
        return self._write(lambda: self.db.execute('INSERT INTO events(ts,kind,payload,code_version,strategy_version) VALUES(?,?,?,?,?)',
                                                   (stamp, kind, text, code, strategy)).lastrowid)

    def add_candidate(self, mint, *, pool=None, signature=None, slot=None, migrated_at=None, hint_seq=None, meta=None,
                      code_version=None, strategy_version=None, ts=None):
        """Idempotent per mint: a repeated mint returns the existing id and writes nothing."""
        if not isinstance(mint, str) or not mint:
            raise StoreError('mint required')
        code, strategy = self._versions(code_version, strategy_version)
        text, stamp = _json(meta or {}), self._ts(ts)

        def write():
            row = self.db.execute('SELECT id FROM candidates WHERE mint=?', (mint,)).fetchone()
            if row:
                return row[0]
            return self.db.execute('INSERT INTO candidates(ts,mint,pool,signature,slot,migrated_at,hint_seq,meta,code_version,strategy_version) '
                                   'VALUES(?,?,?,?,?,?,?,?,?,?)', (stamp, mint, pool, signature, slot, migrated_at, hint_seq, text, code, strategy)).lastrowid
        return self._write(write)

    def candidate_id(self, mint):
        with self._lock:
            row = self.db.execute('SELECT id FROM candidates WHERE mint=?', (mint,)).fetchone()
        return row[0] if row else None

    def add_observation(self, kind, raw, *, mint=None, candidate_id=None, meta=None, code_version=None, strategy_version=None, ts=None):
        """Retain the exact bytes of a provider response (up to 4 MiB) with its sha256. Returns the id (use it as a quote ``ref``)."""
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RAW_BYTES:
            raise StoreError('raw must be bytes of at most 4 MiB')
        code, strategy = self._versions(code_version, strategy_version)
        raw, text, stamp = bytes(raw), _json(self._clean(meta or {})), self._ts(ts)
        return self._write(lambda: self.db.execute(
            'INSERT INTO observations(ts,kind,mint,candidate_id,raw,sha256,meta,code_version,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)',
            (stamp, str(kind)[:64], mint, candidate_id, raw, hashlib.sha256(raw).hexdigest(), text, code, strategy)).lastrowid)

    def observation(self, observation_id):
        with self._lock:
            row = self.db.execute('SELECT raw,sha256,kind,mint,meta FROM observations WHERE id=?', (observation_id,)).fetchone()
        if row is None:
            return None
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise StoreError('observation bytes do not match their hash')
        return {'raw': bytes(row[0]), 'sha256': row[1], 'kind': row[2], 'mint': row[3], 'meta': json.loads(row[4])}

    def add_decision(self, kind, action, *, mint=None, candidate_id=None, reasons=(), features=None, code_version=None,
                     strategy_version=None, ts=None):
        code, strategy = self._versions(code_version, strategy_version)
        reasons_text, features_text, stamp = _json(list(reasons)), _json(features or {}), self._ts(ts)
        return self._write(lambda: self.db.execute(
            'INSERT INTO decisions(ts,kind,mint,candidate_id,action,reasons,features,code_version,strategy_version) VALUES(?,?,?,?,?,?,?,?,?)',
            (stamp, str(kind)[:32], mint, candidate_id, str(action)[:64], reasons_text, features_text, code, strategy)).lastrowid)

    def add_error(self, code, *, transient, scope='candidate', mint=None, message='', code_version=None, strategy_version=None, ts=None):
        """A failed candidate / position check. The message is redacted (credentials) and truncated."""
        cv, sv = self._versions(code_version, strategy_version)
        text, stamp = redact(message)[:MAX_MESSAGE], self._ts(ts)
        return self._write(lambda: self.db.execute(
            'INSERT INTO errors(ts,code,transient,mint,scope,message,code_version,strategy_version) VALUES(?,?,?,?,?,?,?,?)',
            (stamp, str(code)[:96], 1 if transient else 0, mint, str(scope)[:32], text, cv, sv)).lastrowid)

    def add_fill(self, fill, *, candidate_id=None, code_version=None, strategy_version=None):
        """Append a paper fill. Raises AccountingHalt (and writes nothing) if it would break an invariant."""
        if not isinstance(fill, Fill):
            raise StoreError('a lean.paper.Fill is required')
        code, strategy = self._versions(code_version, strategy_version)

        def write():
            positions, cash, _ = self._replay_locked()
            after_positions, after_cash = apply_fill(positions, cash, fill)
            held = after_positions.get(fill.mint)
            try:
                row = self.db.execute(
                    'INSERT INTO fills(ts,mint,side,qty_raw,sol_lamports,fee_lamports,slippage_bps,decimals,cost_sold_lamports,realized_lamports,'
                    'quote_ref,label,candidate_id,cash_after,qty_after,cost_after,code_version,strategy_version) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self._ts(fill.ts), fill.mint, fill.side, fill.qty_raw, fill.sol_lamports, fill.fee_lamports, fill.slippage_bps, fill.decimals,
                     fill.cost_sold_lamports, fill.realized_lamports, None if fill.quote_ref is None else str(fill.quote_ref), fill.label,
                     candidate_id, after_cash, held.qty_raw if held else 0, held.cost_lamports if held else 0, code, strategy))
            except sqlite3.IntegrityError as error:
                raise AccountingHalt('store refused the fill: %s' % error) from None
            self._cache = None
            return row.lastrowid
        return self._write(write)

    # -- derived state ---------------------------------------------------------------------------------------------------------
    def _fill_rows(self):
        return self.db.execute(
            'SELECT id,ts,mint,side,qty_raw,sol_lamports,fee_lamports,slippage_bps,decimals,cost_sold_lamports,realized_lamports,quote_ref,label,'
            'cash_after,qty_after,cost_after FROM fills ORDER BY id').fetchall()

    @staticmethod
    def _fill(row):
        return Fill(ts=row[1], mint=row[2], side=row[3], qty_raw=row[4], sol_lamports=row[5], fee_lamports=row[6], slippage_bps=row[7],
                    decimals=row[8], cost_sold_lamports=row[9], realized_lamports=row[10], quote_ref=row[11], label=row[12])

    def _replay_locked(self, verify=False):
        """(positions, cash, realized) by replaying every fill through the accounting rules. With ``verify`` the stored running
        balances must match the replay row by row."""
        last = self.db.execute('SELECT COALESCE(MAX(id),0) FROM fills').fetchone()[0]
        if not verify and self._cache is not None and self._cache[0] == last:
            return self._cache[1]
        positions, cash, realized = {}, self.initial_cash, 0
        for row in self._fill_rows():
            fill = self._fill(row)
            positions, cash = apply_fill(positions, cash, fill)
            realized += fill.realized_lamports
            if verify:
                held = positions.get(fill.mint)
                if (row[13], row[14], row[15]) != (cash, held.qty_raw if held else 0, held.cost_lamports if held else 0):
                    raise AccountingHalt(f'fill {row[0]}: stored balances differ from the replay')
        result = (positions, cash, realized)
        if not verify:
            self._cache = (last, result)
        return result

    def positions(self):
        """{mint: Position} of what is held now, derived from fills."""
        with self._lock:
            return dict(self._replay_locked()[0])

    def cash(self):
        """Cash in lamports, derived from fills: initial - buys - fees + sells."""
        with self._lock:
            return self._replay_locked()[1]

    def realized(self):
        """Realized PnL in lamports over all sells (fees included)."""
        with self._lock:
            return self._replay_locked()[2]

    def check_invariants(self):
        """Raise AccountingHalt unless the stored history reconciles. Pure SQL sums are compared with the replay as a second opinion."""
        with self._lock:
            positions, cash, realized = self._replay_locked(verify=True)
            buys, sells, fees = self.db.execute(
                "SELECT COALESCE(SUM(CASE WHEN side='buy' THEN sol_lamports END),0), COALESCE(SUM(CASE WHEN side='sell' THEN sol_lamports END),0), "
                'COALESCE(SUM(fee_lamports),0) FROM fills').fetchone()
            if cash != self.initial_cash - buys + sells - fees or cash < 0:
                raise AccountingHalt('cash does not equal initial - buys + sells - fees')
            open_cost = sum(p.cost_lamports for p in positions.values())
            if cash + open_cost != self.initial_cash + realized:
                raise AccountingHalt('cash + open cost does not equal initial + realized')
            net = dict(self.db.execute("SELECT mint, SUM(CASE side WHEN 'buy' THEN qty_raw ELSE -qty_raw END) FROM fills GROUP BY mint"))
            if any(q < 0 for q in net.values()):
                raise AccountingHalt('negative quantity')
            if {m: q for m, q in net.items() if q} != {m: p.qty_raw for m, p in positions.items()}:
                raise AccountingHalt('positions differ from the fills')
        return True

    # -- small read helpers (used by the runner and the reports) --------------------------------------------------------------------
    def counts(self):
        with self._lock:
            return {t: self.db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in TABLES if t != 'meta'}

    def rows(self, table, *, where='', args=(), limit=1000):
        """Read-only rows of one table as dicts (``table`` must be one of the store's own tables)."""
        if table not in TABLES or table == 'meta' or not isinstance(limit, int) or not 0 < limit <= 100000:
            raise StoreError('unknown table or limit')
        with self._lock:
            cur = self.db.execute(f'SELECT * FROM {table} {where} ORDER BY id LIMIT {limit}', args)
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]

    @staticmethod
    def _clean(meta):
        """Metadata is JSON-able and credential-free: every string value is redacted."""
        def walk(v):
            if isinstance(v, str):
                return redact(v)
            if isinstance(v, dict):
                return {str(k): walk(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [walk(x) for x in v]
            return v
        return walk(meta)
