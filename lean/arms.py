"""W2a: shadow paper ARMS, an A/B test of strategies on the SAME candidate stream as the main arm.

PAPER ONLY. Nothing here signs, builds or sends a transaction, and nothing here is ever read by the main arm.

An arm is ``{name, strategy_config, entry_filter}``. For every candidate that PASSED the main screen (so every hazard check of the
screen is behind it) each arm decides entry on its own: its own ``lean.strategy.entry_decision`` (own config, own virtual book:
cash, equity, positions, cooldowns, loss streak, day baseline), then its optional decision-time ``entry_filter``. Exits run the real
``lean.strategy.exit_decision`` of the arm's own config on the marks the main position loop already reads.

No extra provider calls for entry pricing
  * The main runner's entry quote (the q0/q1 fill quote of L10 and its sell leg) for the same mint is reused and scaled to the arm's
    size (linear in the amounts; refused outside ``max_scale``). A candidate the main arm hit with ENTRY_ABORTED_LATENCY is skipped.
  * If the main arm did not quote the candidate (its portfolio gate refused, it errored, or the size is out of range) the candidate costs
    ONE buy quote on the shared LOW lane, however many arms want it: the first arm pays, the others scale that quote (flag
    ``SHARED_QUOTE``). The sell leg of the round trip is then MODELED from the screen's reserves with the effective mark model, as a sell
    quote against the pool as it is (flag ``SELL_LEG_MODELED``), and the fill is flagged ``LATENCY_MODEL_ABSENT`` (one quote, no q0/q1
    pair). The low lane never waits: on a shed the arm skips and records ``ARM_SKIPPED_SHED``.
  * A candidate the main arm rejected at the L22 honeypot simulation is a hazard reject: arms skip it. (When the main gate refused the
    candidate the simulation did not run; the arm records ``honeypot: unchecked``.)

Exits
  * Marks: the raw accounts of the main marks read (``Runner._read_marks``) are captured and each arm values ITS OWN quantity from
    them (``MarkModel.value_accounts``), so the 'effective' marks of M1 apply unchanged. A mint only an arm holds is read in ONE batched
    ``getMultipleAccounts`` on the LOW lane (never the exit lane); a position that still has no fresh mark is valued by one low-lane
    sell quote (a quote-mark, like D1). The arm never makes a call on the exit lane.
  * Fills: the L10 model. An exit decided at t is filled at the quote taken ``exec_delay_s`` later (q1): the main arm's own q1 for the same
    mint when it exits at the same time (scaled; no call), else the latest main quote-mark, else ONE low-lane sell quote (shared by the
    arms that exit the same mint together). If no quote can be had for ``exit_wait_s`` because of a shed or a transient failure the arm
    fills at its own mark (flag ``MARK_FILL`` + ``LATENCY_MODEL_ABSENT``); a missing route never does: it is written off after
    ``unexitable_after_s`` like the main arm's D2. Fees, ATA rent and the
    Token-2022 transfer fee are ``lean.execution``'s, applied to the arm's own book. Every fill carries ``price_source`` and ``flags``.
  * ``full_take_profit_ratio`` (lean.strategy, optional) is the single full take-profit the ``tp110`` arm needs.

Entry filter (decision-time features only)
  ``{"feature": "largest_cluster_supply_pct", "op": "<=", "value": 0.71, "source": "clusters", "wait_s": 0}`` (or ``{"all": [...]}``).
  Sources: ``screen`` (the screen's own features, known at the decision) or an observation kind written by a collector (``clusters``,
  ``features``, ``flow``, ``wallet_signals``, ``risk_external``). A collector row counts only if it was WRITTEN at or before the arm's
  decision time and its figure is not flagged ``AT_COLLECTION``; otherwise the arm skips and records ``FEATURE_NOT_READY``. With
  ``wait_s`` > 0 the arm instead holds the candidate for up to ``wait_s`` and, once the row exists, decides AT THAT TIME (a fresh LOW-lane
  quote; the entry is later than the main arm's, ``deferred_s`` is in the decision).

Storage: its OWN ``arms.sqlite`` (``arms.db``), append-only (UPDATE/DELETE refused by triggers), schema version 1, a schema that differs
is refused (never migrated). Every row carries ``arm_name``, ``strategy_version``, ``config_hash`` and ``code_version``. Nothing here
writes ``lean.sqlite``; it is opened only READ-ONLY, for the entry-filter rows.

Isolation: one daemon thread (``Runner.run`` starts it; outside ``run`` the hooks process inline, for tests and ``--once``). The runner
hooks only append to a bounded queue / dictionary under a tiny lock (no I/O under any lock) and set an event. An exception in one arm is
caught, counted in ``health()`` (``failures``) and never reaches the runner; a book that cannot be persisted or reconciled is halted for
that arm alone.
"""
import collections
import dataclasses
import json
import logging
import operator
import os
import re
import sqlite3
import threading
import time
import types
from decimal import Decimal, InvalidOperation
from pathlib import Path

from lean import adapters as A, execution as X, marks as M, paper, strategy as S
from lean.paper import AccountingHalt
from lean.providers import ProviderError, is_shed

log = logging.getLogger('lean.arms')

SCHEMA_VERSION = 1
SOL_MINT = A.SOL_MINT
NAME_RE = re.compile(r'^[a-z][a-z0-9_]{0,31}$')
MAX_ARMS = 12
OPS = {'<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge, '==': operator.eq, '!=': operator.ne}
ROW_SOURCES = ('clusters', 'features', 'flow', 'wallet_signals', 'risk_external')
SOURCES = ('screen',) + ROW_SOURCES
EXIT_QUOTE_KINDS = ('quote:exit', 'quote:exit_requote')
QUOTE_MARK_KIND = 'quote:mark'
UNEXITABLE_BLOCKS = ('NO_ROUTE', 'STUCK_POSITION', 'SELL_QUOTE_INVALID', 'SELL_QUOTE_MISSING')
SKIP_SHED = 'ARM_SKIPPED_SHED'
FEATURE_NOT_READY = 'FEATURE_NOT_READY'
PEAK_STEP = Decimal('1.01')
CONFIG_DEFAULTS = {'enabled': True, 'db': None, 'initial_cash_sol': None, 'queue_size': 200, 'max_scale': 2.0, 'exit_wait_s': 120.0,
                   'exit_grace_s': 1.0, 'eval_interval_s': 15.0, 'quote_marks_per_pass': 3, 'peak_persist_s': 60.0}


class ArmsConfigError(ValueError):
    pass


class ArmsConfig(dict):
    """The normalised ``arms`` block. A plain dict for every comparison and for the config hash; ``base_dir`` (where lean.json lives, for the
    relative strategy paths) rides along as an attribute, so a copy of the same lean.json in another directory is the same config."""

    def __init__(self, values, base_dir=None):
        super().__init__(values)
        self.base_dir = None if base_dir is None else str(base_dir)


class ArmsStoreError(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------------------- config
def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not low <= value <= high:
        raise ArmsConfigError('arms.%s must be a number between %s and %s' % (name, low, high))
    return float(value)


def _strategy_ref(value, where):
    """The shape of an arm's strategy_config: ``{"path": p}`` (a strategy file, relative to lean.json), ``{"base": p, "set": {...}}`` (a
    strategy file with some keys replaced) or ``{"inline": {...}}`` (a full strategy object)."""
    if isinstance(value, str) and value:
        return {'path': value}
    if isinstance(value, dict) and 'base' in value:
        if set(value) - {'base', 'set', '_comment'} or not isinstance(value['base'], str) or not value['base'] \
                or not isinstance(value.get('set', {}), dict):
            raise ArmsConfigError('%s: strategy_config takes only base (a path) and set (an object)' % where)
        return {'base': value['base'], 'set': dict(value.get('set', {}))}
    if isinstance(value, dict):
        return {'inline': {k: v for k, v in value.items() if k != '_comment'}}
    raise ArmsConfigError('%s: strategy_config must be a path or an object' % where)


def resolve_strategy(ref, base_dir, where='arm'):
    """The arm's strategy as a plain dict, validated with the strict StrategyConfig (the dict is what gets hashed)."""
    def read(path):
        p = Path(path)
        p = p if p.is_absolute() or base_dir is None else Path(base_dir) / p
        try:
            raw = json.loads(p.read_text())
        except (OSError, ValueError) as error:
            raise ArmsConfigError('%s: strategy_config %s unreadable (%s)' % (where, path, type(error).__name__)) from None
        if not isinstance(raw, dict):
            raise ArmsConfigError('%s: strategy_config %s must be an object' % (where, path))
        return raw
    if 'inline' in ref:
        raw = dict(ref['inline'])
    elif 'path' in ref:
        raw = read(ref['path'])
    else:
        raw = {**read(ref['base']), **ref['set']}
    raw.pop('_comment', None)
    try:
        S.StrategyConfig.from_dict(raw)
    except S.ConfigError as error:
        raise ArmsConfigError('%s: strategy_config: %s' % (where, error)) from None
    return raw


def _referenced_file(ref, base_dir):
    path = ref.get('path') or ref.get('base')
    if path is None:
        return None
    p = Path(path)
    return p if p.is_absolute() or base_dir is None else Path(base_dir) / p


def filter_config(raw, where='entry_filter'):
    """Normalise an entry filter; None = no filter. Strict: unknown keys, operators, sources and values are refused."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ArmsConfigError('%s must be an object' % where)
    unknown = set(raw) - {'feature', 'op', 'value', 'source', 'all', 'wait_s', '_comment'}
    if unknown:
        raise ArmsConfigError('%s: unknown keys %s' % (where, sorted(unknown)))
    conditions = raw.get('all')
    if conditions is None:
        conditions = [{k: raw[k] for k in ('feature', 'op', 'value', 'source') if k in raw}]
    elif set(raw) & {'feature', 'op', 'value', 'source'}:
        raise ArmsConfigError('%s: give either all or a single condition' % where)
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 8:
        raise ArmsConfigError('%s: 1-8 conditions' % where)
    out = []
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict) or set(cond) - {'feature', 'op', 'value', 'source'}:
            raise ArmsConfigError('%s[%d]: feature, op, value, source' % (where, i))
        feature = cond.get('feature')
        if not isinstance(feature, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', feature):
            raise ArmsConfigError('%s[%d]: feature must be a lowercase name' % (where, i))
        if cond.get('op') not in OPS:
            raise ArmsConfigError('%s[%d]: op must be one of %s' % (where, i, sorted(OPS)))
        try:
            value = Decimal(str(cond.get('value')))
        except InvalidOperation:
            raise ArmsConfigError('%s[%d]: value must be a number' % (where, i)) from None
        if isinstance(cond.get('value'), bool) or not value.is_finite():
            raise ArmsConfigError('%s[%d]: value must be a finite number' % (where, i))
        from lean import clusters
        source = cond.get('source') or ('clusters' if feature in clusters.FIGURES else 'screen')
        if source not in SOURCES:
            raise ArmsConfigError('%s[%d]: source must be one of %s' % (where, i, list(SOURCES)))
        if source == 'clusters' and feature not in clusters.FIGURES:
            raise ArmsConfigError('%s[%d]: %s is not a clusters figure %s' % (where, i, feature, list(clusters.FIGURES)))
        out.append({'feature': feature, 'op': cond['op'], 'value': str(value), 'source': source})
    return {'all': out, 'wait_s': _number(raw.get('wait_s', 0), 'entry_filter.wait_s', 0, 3600)}


def config(raw, base_dir=None):
    """lean.json ``arms`` -> a validated, normalised dict (an ``ArmsConfig``: strategies stay references, resolved by ``resolve_strategy`` when
    the arms are built). None / absent = arms OFF. Unknown keys refused; a strategy that can be read now is validated now."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ArmsConfigError('arms must be an object')
    unknown = set(raw) - set(CONFIG_DEFAULTS) - {'arms', '_comment'}
    if unknown:
        raise ArmsConfigError('arms: unknown keys %s' % sorted(unknown))
    out = {k: raw.get(k, v) for k, v in CONFIG_DEFAULTS.items()}
    if type(out['enabled']) is not bool:
        raise ArmsConfigError('arms.enabled must be true or false')
    if out['db'] is not None and (not isinstance(out['db'], str) or not out['db'].strip()):
        raise ArmsConfigError('arms.db must be a path or null')
    if out['initial_cash_sol'] is not None:
        try:
            cash = Decimal(str(out['initial_cash_sol']))
        except InvalidOperation:
            raise ArmsConfigError('arms.initial_cash_sol must be a number') from None
        if isinstance(out['initial_cash_sol'], bool) or not cash.is_finite() or cash <= 0:
            raise ArmsConfigError('arms.initial_cash_sol must be positive')
        out['initial_cash_sol'] = str(cash)
    if type(out['queue_size']) is not int or not 1 <= out['queue_size'] <= 10_000:
        raise ArmsConfigError('arms.queue_size must be an integer between 1 and 10000')
    out['max_scale'] = _number(out['max_scale'], 'max_scale', 1, 100)
    out['exit_wait_s'] = _number(out['exit_wait_s'], 'exit_wait_s', 0, 86400)
    out['exit_grace_s'] = _number(out['exit_grace_s'], 'exit_grace_s', 0, 60)
    out['eval_interval_s'] = _number(out['eval_interval_s'], 'eval_interval_s', 1, 3600)
    out['peak_persist_s'] = _number(out['peak_persist_s'], 'peak_persist_s', 0, 3600)
    if type(out['quote_marks_per_pass']) is not int or not 0 <= out['quote_marks_per_pass'] <= 20:
        raise ArmsConfigError('arms.quote_marks_per_pass must be an integer between 0 and 20')
    listed = raw.get('arms')
    if not isinstance(listed, list) or not 1 <= len(listed) <= MAX_ARMS:
        raise ArmsConfigError('arms.arms must be a list of 1-%d arms' % MAX_ARMS)
    names, arms = set(), []
    for i, spec in enumerate(listed):
        where = 'arms.arms[%d]' % i
        if not isinstance(spec, dict) or set(spec) - {'name', 'strategy_config', 'entry_filter', '_comment'}:
            raise ArmsConfigError('%s: name, strategy_config, entry_filter' % where)
        name = spec.get('name')
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name == 'main':
            raise ArmsConfigError('%s: name must match %s and not be "main"' % (where, NAME_RE.pattern))
        if name in names:
            raise ArmsConfigError('%s: duplicate arm name %s' % (where, name))
        names.add(name)
        ref = _strategy_ref(spec.get('strategy_config'), where)
        # Strict at load whenever the strategy can be read now. A strategy FILE that is not there yet (the config was copied without it)
        # is read when the arms are built, like the main arm's strategy_config.
        file = _referenced_file(ref, base_dir)
        if file is None or file.is_file():
            resolve_strategy(ref, base_dir, where)
        arms.append({'name': name, 'strategy_config': ref, 'entry_filter': filter_config(spec.get('entry_filter'), where + '.entry_filter')})
    out['arms'] = arms
    return ArmsConfig(out, base_dir)


def db_path(cfg, state_dir):
    value = cfg.get('db') or 'arms.sqlite'
    path = Path(value)
    return path if path.is_absolute() else Path(state_dir) / path


# ------------------------------------------------------------------------------------------------------------- storage
TABLES = {
    'arm_meta': 'CREATE TABLE arm_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    'arm_events': ('CREATE TABLE arm_events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, arm_name TEXT NOT NULL, '
                   'strategy_version TEXT NOT NULL, config_hash TEXT NOT NULL, code_version TEXT NOT NULL, kind TEXT NOT NULL, '
                   'payload TEXT NOT NULL)'),
    'arm_decisions': ('CREATE TABLE arm_decisions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, arm_name TEXT NOT NULL, '
                      'strategy_version TEXT NOT NULL, config_hash TEXT NOT NULL, code_version TEXT NOT NULL, kind TEXT NOT NULL, '
                      'mint TEXT, candidate_id INTEGER, action TEXT NOT NULL, reasons TEXT NOT NULL, features TEXT NOT NULL)'),
    'arm_fills': ('CREATE TABLE arm_fills(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, arm_name TEXT NOT NULL, '
                  'strategy_version TEXT NOT NULL, config_hash TEXT NOT NULL, code_version TEXT NOT NULL, mint TEXT NOT NULL, '
                  "side TEXT NOT NULL CHECK(side IN ('buy','sell')), qty_raw INTEGER NOT NULL CHECK(qty_raw>0), "
                  'sol_lamports INTEGER NOT NULL CHECK(sol_lamports>=0), fee_lamports INTEGER NOT NULL CHECK(fee_lamports>=0), '
                  'slippage_bps INTEGER NOT NULL CHECK(slippage_bps>=0), decimals INTEGER NOT NULL, '
                  'cost_sold_lamports INTEGER NOT NULL CHECK(cost_sold_lamports>=0), realized_lamports INTEGER NOT NULL, '
                  f"quote_ref TEXT, label TEXT NOT NULL CHECK(label='{paper.LABEL}'), candidate_id INTEGER, "
                  'price_source TEXT NOT NULL, flags TEXT NOT NULL, cash_after INTEGER NOT NULL CHECK(cash_after>=0), '
                  'qty_after INTEGER NOT NULL CHECK(qty_after>=0), cost_after INTEGER NOT NULL CHECK(cost_after>=0))'),
    'arm_states': ('CREATE TABLE arm_states(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, arm_name TEXT NOT NULL, '
                   'strategy_version TEXT NOT NULL, config_hash TEXT NOT NULL, code_version TEXT NOT NULL, mint TEXT NOT NULL, '
                   'open_fill_id INTEGER NOT NULL, fill_id INTEGER, '
                   "event TEXT NOT NULL CHECK(event IN ('open','update','rung','closed')), state TEXT NOT NULL)"),
}
INDEXES = {
    'arm_events_arm': 'CREATE INDEX arm_events_arm ON arm_events(arm_name, id)',
    'arm_decisions_arm': 'CREATE INDEX arm_decisions_arm ON arm_decisions(arm_name, id)',
    'arm_decisions_cand': 'CREATE INDEX arm_decisions_cand ON arm_decisions(arm_name, candidate_id)',
    'arm_fills_arm': 'CREATE INDEX arm_fills_arm ON arm_fills(arm_name, id)',
    'arm_states_arm': 'CREATE INDEX arm_states_arm ON arm_states(arm_name, mint, id)',
}
GUARDS = {f'{table}_no_{action.lower()}': (f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                                           "BEGIN SELECT RAISE(ABORT,'arms store is append-only'); END")
          for table in TABLES for action in ('UPDATE', 'DELETE')}
MAX_JSON = 256 * 1024


def _json(value):
    text = json.dumps(_plain(value), sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)
    if len(text.encode()) > MAX_JSON:
        raise ArmsStoreError('payload too large')
    return text


def _plain(value):
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


class ArmsStore:
    """The append-only ``arms.sqlite``. One connection, one writer lock (private to this store: nothing else waits on it)."""

    def __init__(self, path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.Lock()
        existing = self.path.exists()
        if not existing:
            if not self.path.parent.is_dir():
                raise ArmsStoreError('arms store directory missing')
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        elif self.path.is_symlink() or not self.path.is_file():
            raise ArmsStoreError('arms store must be a regular file')
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.execute('PRAGMA recursive_triggers=ON')
        try:
            if not existing:
                self._create()
            else:
                self._validate()
        except BaseException:
            self.db.close()
            if not existing:
                for suffix in ('', '-wal', '-shm', '-journal'):
                    try:
                        os.unlink(str(self.path) + suffix)
                    except FileNotFoundError:
                        pass
            raise

    def _create(self):
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for sql in (*TABLES.values(), *INDEXES.values(), *GUARDS.values()):
                    self.db.execute(sql)
                self.db.execute('INSERT INTO arm_meta VALUES(?,?)', ('schema_version', str(SCHEMA_VERSION)))
                self.db.execute('INSERT INTO arm_meta VALUES(?,?)', ('created_at', repr(self.clock())))
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def _validate(self):
        schema = dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
        if schema != {**TABLES, **INDEXES, **GUARDS}:
            raise ArmsStoreError('arms store schema differs from this release (an arms store is never migrated)')
        meta = dict(self.db.execute('SELECT key,value FROM arm_meta'))
        if meta.get('schema_version') != str(SCHEMA_VERSION):
            raise ArmsStoreError('arms store version invalid')

    def close(self):
        with self._lock:
            self.db.close()

    def _write(self, function):
        with self._lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                result = function()
                self.db.execute('COMMIT')
                return result
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def _query(self, sql, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    # -- writes (every row carries the arm identity) -------------------------------------------------------------------
    def event(self, arm, kind, payload, *, ts=None):
        return self._write(lambda: self.db.execute(
            'INSERT INTO arm_events(ts,arm_name,strategy_version,config_hash,code_version,kind,payload) VALUES(?,?,?,?,?,?,?)',
            (self.clock() if ts is None else ts, arm.name, arm.strategy_version, arm.config_hash, arm.code_version, kind,
             _json(payload))).lastrowid)

    def decision(self, arm, kind, action, *, mint, candidate_id, reasons, features, ts=None):
        return self._write(lambda: self.db.execute(
            'INSERT INTO arm_decisions(ts,arm_name,strategy_version,config_hash,code_version,kind,mint,candidate_id,action,reasons,features) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (self.clock() if ts is None else ts, arm.name, arm.strategy_version, arm.config_hash, arm.code_version, kind, mint,
             candidate_id, action, _json(list(reasons)), _json(features))).lastrowid)

    def fill(self, arm, fill, *, cash_after, held, candidate_id, price_source, flags, event, state, open_fill_id):
        """The fill and its state row in ONE transaction. Returns (fill id, open_fill_id)."""
        state_text = _json(state)

        def write():
            row = self.db.execute(
                'INSERT INTO arm_fills(ts,arm_name,strategy_version,config_hash,code_version,mint,side,qty_raw,sol_lamports,fee_lamports,'
                'slippage_bps,decimals,cost_sold_lamports,realized_lamports,quote_ref,label,candidate_id,price_source,flags,cash_after,'
                'qty_after,cost_after) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (fill.ts, arm.name, arm.strategy_version, arm.config_hash, arm.code_version, fill.mint, fill.side, fill.qty_raw,
                 fill.sol_lamports, fill.fee_lamports, fill.slippage_bps, fill.decimals, fill.cost_sold_lamports, fill.realized_lamports,
                 None if fill.quote_ref is None else str(fill.quote_ref), fill.label, candidate_id, price_source, _json(flags), cash_after,
                 held.qty_raw if held else 0, held.cost_lamports if held else 0))
            opened = row.lastrowid if event == 'open' else open_fill_id
            self.db.execute(
                'INSERT INTO arm_states(ts,arm_name,strategy_version,config_hash,code_version,mint,open_fill_id,fill_id,event,state) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (fill.ts, arm.name, arm.strategy_version, arm.config_hash, arm.code_version, fill.mint, opened, row.lastrowid, event,
                 state_text))
            return row.lastrowid, opened
        return self._write(write)

    def update_state(self, arm, mint, open_fill_id, state, *, ts):
        text = _json(state)
        return self._write(lambda: self.db.execute(
            'INSERT INTO arm_states(ts,arm_name,strategy_version,config_hash,code_version,mint,open_fill_id,fill_id,event,state) '
            "VALUES(?,?,?,?,?,?,?,NULL,'update',?)",
            (ts, arm.name, arm.strategy_version, arm.config_hash, arm.code_version, mint, open_fill_id, text)).lastrowid)

    # -- reads ---------------------------------------------------------------------------------------------------------
    def load(self, arm_name):
        """(fills, latest state per mint, closed rows, events) of one arm, in id order."""
        fills = self._query('SELECT id,ts,mint,side,qty_raw,sol_lamports,fee_lamports,slippage_bps,decimals,cost_sold_lamports,'
                            'realized_lamports,quote_ref,label,candidate_id FROM arm_fills WHERE arm_name=? ORDER BY id', (arm_name,))
        latest = self._query('SELECT mint,open_fill_id,event,state,ts FROM arm_states WHERE id IN '
                             '(SELECT MAX(id) FROM arm_states WHERE arm_name=? GROUP BY mint)', (arm_name,))
        closed = self._query("SELECT mint,open_fill_id,fill_id,ts,state FROM arm_states WHERE arm_name=? AND event='closed' ORDER BY id",
                             (arm_name,))
        events = self._query('SELECT kind,payload,ts FROM arm_events WHERE arm_name=? ORDER BY id', (arm_name,))
        return fills, latest, closed, events

    def decided(self, arm_name):
        """Candidate ids this arm has decided (a DEFER is not a decision)."""
        return {r[0] for r in self._query("SELECT DISTINCT candidate_id FROM arm_decisions WHERE arm_name=? AND kind='entry' "
                                          "AND action!='DEFER' AND candidate_id IS NOT NULL", (arm_name,))}

    def rows(self, table, arm_name=None, limit=100000):
        if table not in TABLES or table == 'arm_meta':
            raise ArmsStoreError('unknown table')
        cur = self.db
        with self._lock:
            if arm_name is None:
                got = cur.execute('SELECT * FROM %s ORDER BY id LIMIT ?' % table, (limit,))
            else:
                got = cur.execute('SELECT * FROM %s WHERE arm_name=? ORDER BY id LIMIT ?' % table, (arm_name, limit))
            names = [d[0] for d in got.description]
            return [dict(zip(names, r)) for r in got.fetchall()]


# ------------------------------------------------------------------------------------------------- the entry-filter rows
class RowsReader:
    """READ-ONLY access to ``lean.sqlite`` observations for the entry filters (its own connection and lock: the trader's store lock is
    never taken). ``latest`` answers 'what was known at ``upto_ts``'."""

    def __init__(self, path):
        self.path, self._db, self._lock = Path(path), None, threading.Lock()

    def _connect(self):
        if self._db is None:
            self._db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5, check_same_thread=False)
            self._db.execute('PRAGMA query_only=1')
        return self._db

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def latest(self, kind, mint, upto_ts):
        """('OK', ts, meta) for the newest row of ``kind`` written at or before ``upto_ts``; ('AFTER', ts, None) when rows exist but
        all were written later; ('NO_ROW', None, None); ('UNREADABLE', None, None)."""
        try:
            with self._lock:
                db = self._connect()
                row = db.execute('SELECT ts,meta FROM observations WHERE mint=? AND kind=? AND ts<=? ORDER BY id DESC LIMIT 1',
                                 (mint, kind, upto_ts)).fetchone()
                if row is not None:
                    return 'OK', row[0], json.loads(row[1])
                later = db.execute('SELECT MIN(ts) FROM observations WHERE mint=? AND kind=?', (mint, kind)).fetchone()[0]
            return ('NO_ROW', None, None) if later is None else ('AFTER', later, None)
        except (sqlite3.Error, ValueError, OSError):
            return 'UNREADABLE', None, None


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def evaluate_filter(flt, screen, reader, mint, t_dec):
    """('PASS' | 'REJECT' | 'NOT_READY', detail). Every condition must hold. A figure that is not known AT ``t_dec`` is NOT_READY, never
    passed: a collector row written after ``t_dec``, a null figure, or one flagged AT_COLLECTION (read after the decision)."""
    seen = []
    for cond in flt['all']:
        feature, source = cond['feature'], cond['source']
        info = {'feature': feature, 'source': source}
        if source == 'screen':
            value, why = _decimal((screen or {}).get(feature)), 'MISSING'
        else:
            status, ts, meta = reader.latest(source, mint, t_dec)
            if status != 'OK':
                value, why = None, {'AFTER': 'WRITTEN_AFTER_DECISION', 'NO_ROW': 'NO_ROW', 'UNREADABLE': 'STORE_UNREADABLE'}[status]
                info['row_written_at'] = ts
            elif ((meta or {}).get('flags') or {}).get(feature) == 'AT_COLLECTION':
                value, why = None, 'AT_COLLECTION'
            else:
                value, why = _decimal((meta or {}).get(feature)), 'VALUE_NULL'
                info['row_written_at'] = ts
                if value is None:
                    info['unavailable'] = ((meta or {}).get('unavailable') or {}).get(feature)
        if value is None:
            return 'NOT_READY', {**info, 'why': why, 'conditions': seen}
        info['value'] = str(value)
        seen.append(info)
        if not OPS[cond['op']](value, Decimal(cond['value'])):
            return 'REJECT', {**info, 'op': cond['op'], 'threshold': cond['value'], 'conditions': seen}
    return 'PASS', {'conditions': seen}


# --------------------------------------------------------------------------------------------------------- virtual book
class Book:
    """One arm's virtual ledger: cash, positions (``lean.paper`` integers), per-position strategy state, closed trades."""

    def __init__(self, initial_lamports):
        self.initial, self.cash = int(initial_lamports), int(initial_lamports)
        self.positions, self.states, self.closed = {}, {}, []
        self.last_buy_ts = None
        self.day = None                                      # (utc day, baseline equity Decimal)
        self.realized = 0


class BookView:
    """The four read methods ``lean.adapters.portfolio`` and ``lean.execution`` call on a store, over a Book."""

    def __init__(self, book):
        self.book = book

    def positions(self):
        return dict(self.book.positions)

    def cash(self):
        return self.book.cash

    def closed_positions(self):
        return list(self.book.closed)

    def last_fill_ts(self, side):
        return self.book.last_buy_ts if side == 'buy' else None


@dataclasses.dataclass
class PendingExit:
    mint: str
    open_id: int
    decided_at: float
    due_at: float
    decided_int: int
    qty: int
    reason: str
    mark: Decimal
    mark_at: int
    mark_source: str
    liquidate: bool
    failing_since: float = None
    retry_at: float = 0.0
    attempts: int = 0


@dataclasses.dataclass
class Evidence:
    """What the main runner knew about one candidate that PASSED its screen (built by the runner hook, read by the arm thread)."""
    mint: str
    cid: int
    at: int                      # runner clock, whole seconds, when the main arm finished with the candidate
    ts: float                    # the same instant, float
    features: dict               # lean.adapters.entry_features: what S.entry_decision reads
    screen: dict                 # the screen's own features
    raw: list                    # the screen's raw responses (transfer-fee extension)
    plan: tuple = None           # the main arm's fill plan: ((quote, ref, at), (sell leg quote, at), xmeta) or None
    aborted: bool = False        # the main arm dropped the entry (ENTRY_ABORTED_LATENCY)
    hazard: str = None           # the main arm rejected the candidate at the L22 honeypot simulation
    main_acted: bool = False     # the main arm filled a BUY
    attempt: int = 1
    low: tuple = None            # the ONE low-lane buy quote of this candidate, shared by every arm that needs one: (quote, at) or ('ERROR', code)


class Arm:
    def __init__(self, name, raw_strategy, flt, runner, code_version, initial_lamports):
        self.name = name
        self.cfg = S.StrategyConfig.from_dict(raw_strategy)               # strict, hash of the raw dict
        self.strategy_version, self.config_hash, self.code_version = self.cfg.strategy_version, self.cfg.config_hash, code_version
        if runner.execution is not None:                                 # the same cost model as live: fees + priority/tip in fixed_fee
            runner.execution.validate_against(self.cfg)
            self.cfg = runner.execution.strategy_cfg(self.cfg)
        self.pcfg = A.paper_config(self.cfg)
        self.filter = flt
        self.book = Book(initial_lamports)
        self.view = BookView(self.book)
        self.shim = types.SimpleNamespace(store=self.view)               # lean.execution.finish_buy reads runner.store.positions()
        self.marks = {}                                                  # mint -> (Decimal SOL, mark_at, source)
        self.pending_exits, self.pending_entries = {}, []
        self.persisted = {}                                              # mint -> (ts, peak Decimal)
        self.decided = set()
        self.halted = None
        self.store = None                                                # set by Arms: the day baseline is persisted
        self.counts = collections.Counter()
        self.skips = collections.Counter()

    def portfolio(self, now, usable_s):
        marks = {m: v for m, v in self.marks.items() if 0 <= now - v[1] <= usable_s}
        return A.portfolio(self.view, self.cfg, now, marks=marks, day_start_equity=self.day_start(now, marks))

    def day_start(self, now, marks):
        """The UTC-day baseline of the daily loss stop (strategy.roll_day_start: rolled only on fresh marks), persisted when it moves."""
        equity, fresh = A.equity_and_freshness(self.view, now, marks, self.cfg.price_ttl_seconds)
        current = S.roll_day_start(self.book.day, now, equity, marks_fresh=fresh)
        if current != self.book.day:
            if self.store is not None:
                self.store.event(self, 'day_start', {'day': current[0], 'equity': str(current[1]), 'marks_fresh': fresh})
            self.book.day = current
        return current[1]


# ---------------------------------------------------------------------------------------------------------------- Arms
def scale_quote(quote, new_in):
    """The same route quote for ``new_in`` input units (linear in the amounts: price impact at these sizes is small)."""
    out = quote.out_amount * new_in // quote.in_amount
    if new_in <= 0 or out <= 0:
        raise ValueError('quote scales to nothing')
    return dataclasses.replace(quote, in_amount=new_in, out_amount=out, other_amount_threshold=None)


class _SharedScheduleModel(M.MarkModel):
    """The runner's mark model, with its own counters: the fee schedule the main reads is shared, the arms' stats are not mixed in."""

    def __init__(self, parent):
        super().__init__(parent.cfg, pool_fee_bps=parent.pool_fee_bps)
        self.parent = parent

    def schedule(self, now):
        return super().schedule(now) or self.parent.schedule(now)


class Arms:
    def __init__(self, cfg, *, runner, store, reader, low, code_version, clock=time.time):
        self.cfg, self.runner, self.store, self.reader, self.low = cfg, runner, store, reader, low
        self.clock, self.code_version = clock, code_version
        self.model = _SharedScheduleModel(runner.mark_model)
        self.estimator = M.MarkModel({'model': 'effective', 'fee_source': 'config'}, pool_fee_bps=120)   # sell leg of a low-lane entry
        self.threaded = False
        self._lock = threading.Lock()                                    # guards the queue and the two capture dicts; never held over I/O
        self._queue = collections.deque()
        self._sell_quotes = collections.OrderedDict()                    # mint -> deque[(kind, quote, ref, at)]
        self._accounts = collections.OrderedDict()                       # mint -> (accounts, mark_at)
        self._low_sells = collections.OrderedDict()                      # mint -> (quote, at): the last low-lane sell quote, shared by the arms
        self._wake = threading.Event()
        self._busy = threading.Lock()
        self._marks_kicked = False
        self.last_eval = 0.0
        self.counts = collections.Counter()
        self.calls = collections.Counter()
        self.last_error = None
        self.last_at = None
        initial = self._initial_lamports()
        specs = [(spec['name'], resolve_strategy(spec['strategy_config'], getattr(cfg, 'base_dir', None), 'arm %s' % spec['name']), spec['entry_filter'])
                 for spec in cfg['arms']]
        self.arms = [self._make_arm(spec, initial) for spec in specs]

    # -- construction ----------------------------------------------------------------------------------------------------
    def _initial_lamports(self):
        if self.cfg.get('initial_cash_sol') is not None:
            return A.lamports(Decimal(self.cfg['initial_cash_sol']))
        return int(self.runner.store.initial_cash)

    def _make_arm(self, spec, initial):
        arm = Arm(spec[0], spec[1], spec[2], self.runner, self.code_version, initial)
        arm.store = self.store
        try:
            self._restore(arm, initial)
        except Exception as error:                                       # noqa: BLE001 - a corrupt book halts THIS arm only
            arm.halted = 'RESTORE_FAILED:%s' % type(error).__name__
            log.exception('arm %s could not be restored; it is halted', arm.name)
        return arm

    def _restore(self, arm, initial):
        fills, latest, closed, events = self.store.load(arm.name)
        stored = [json.loads(p) for k, p, _ in events if k == 'arm_init']
        if stored:
            initial = int(stored[0]['initial_cash_lamports'])
            arm.book = Book(initial)
            arm.view.book = arm.book
        else:
            self.store.event(arm, 'arm_init', {'initial_cash_lamports': initial, 'filter': arm.filter, 'strategy_config': arm.cfg.config_hash,
                                               'tp_ladder_stages': len(arm.cfg.tp_ladder), 'max_hold_seconds': arm.cfg.max_hold_seconds})
        known = [json.loads(p).get('config_hash') for k, p, _ in events if k == 'arm_start']
        for row in fills:
            fill = paper.Fill(ts=row[1], mint=row[2], side=row[3], qty_raw=row[4], sol_lamports=row[5], fee_lamports=row[6],
                              slippage_bps=row[7], decimals=row[8], cost_sold_lamports=row[9], realized_lamports=row[10], quote_ref=row[11],
                              label=row[12])
            arm.book.positions, arm.book.cash = paper.apply_fill(arm.book.positions, arm.book.cash, fill)
            arm.book.realized += fill.realized_lamports
            if fill.side == 'buy':
                arm.book.last_buy_ts = fill.ts
            if row[13] is not None:
                arm.decided.add(row[13])
        arm.decided |= self.store.decided(arm.name)
        for mint, open_id, event, state, ts in latest:
            if event != 'closed':
                arm.book.states[mint] = {'open_fill_id': open_id, 'state': json.loads(state)}
        arm.book.closed = [{'mint': m, 'open_fill_id': o, 'fill_id': f, 'ts': ts, 'state': json.loads(s)} for m, o, f, ts, s in closed]
        day = [json.loads(p) for k, p, _ in events if k == 'day_start']
        if day:
            arm.book.day = (int(day[-1]['day']), Decimal(day[-1]['equity']))
        if set(arm.book.positions) != set(arm.book.states):
            raise ArmsStoreError('arm positions differ from their states')
        changed = bool(known) and known[-1] != arm.config_hash
        if changed:
            log.warning('arm %s: config_hash changed since the last start (%s -> %s); rows carry the hash they were written under',
                        arm.name, known[-1], arm.config_hash)
        self.store.event(arm, 'arm_start', {'config_hash': arm.config_hash, 'strategy_version': arm.strategy_version,
                                            'config_changed': changed, 'open_positions': len(arm.book.positions),
                                            'cash_lamports': arm.book.cash})

    # -- clock -------------------------------------------------------------------------------------------------------------
    def _now(self):
        return int(self.clock())

    # -- runner hooks (cheap, never raise, no I/O) ----------------------------------------------------------------------
    def seen_sell_quote(self, kind, mint, quote, ref, at):
        """A sell quote the main runner just fetched (its exit q0/q1 or a quote-mark): reusable by an arm exiting the same mint."""
        if kind not in EXIT_QUOTE_KINDS and kind != QUOTE_MARK_KIND:
            return
        try:
            with self._lock:
                held = self._sell_quotes.setdefault(mint, collections.deque(maxlen=6))
                held.append((kind, quote, ref, int(at)))
                self._sell_quotes.move_to_end(mint)
                while len(self._sell_quotes) > 64:
                    self._sell_quotes.popitem(last=False)
        except Exception:                                                # noqa: BLE001 - the main arm never notices
            self.counts['hook_failures'] += 1

    def seen_accounts(self, mint, accounts, mark_at):
        """The raw accounts of one main marks read, in ``model.position_keys`` order."""
        try:
            with self._lock:
                self._accounts[mint] = (accounts, int(mark_at))
                self._accounts.move_to_end(mint)
                while len(self._accounts) > 64:
                    self._accounts.popitem(last=False)
        except Exception:                                                # noqa: BLE001
            self.counts['hook_failures'] += 1

    def submit(self, ev):
        """A candidate that passed the main screen (runner hook)."""
        with self._lock:
            if len(self._queue) >= self.cfg['queue_size']:
                self._queue.popleft()
                self.counts['dropped'] += 1
            self._queue.append(ev)
        self._poke()

    def marks_ready(self):
        """The main position loop has just read its marks (or has nothing to read): evaluate the arms' exits now."""
        self._marks_kicked = True
        self._poke()

    def _poke(self):
        """Something to do: the arm thread wakes; outside ``run`` the work is done right here (tests, ``--once``)."""
        if self.threaded:
            self._wake.set()
        else:
            self.process()

    # -- the worker -------------------------------------------------------------------------------------------------------
    def run_loop(self, stop):
        """The arm thread (started by ``Runner.run``, which has set ``threaded`` before any trader thread starts)."""
        while not stop.is_set():
            self._wake.wait(0.5)
            self._wake.clear()
            try:
                self.process()
            except Exception:                                            # noqa: BLE001 - the loop itself must survive
                self.counts['failures'] += 1
                log.exception('arms pass failed')
        try:
            self.process()
        except Exception:                                                # noqa: BLE001
            pass

    def process(self):
        """Candidates, deferred entries, marks + exit decisions (when the main marks were read, or every ``eval_interval_s``), due exits.
        A re-entrant call returns at once; no caller ever waits for another pass."""
        if not self._busy.acquire(False):
            return
        try:
            now_f = self.clock()
            self.last_at = now_f
            self._drain_candidates(now_f)
            self._retry_deferred(now_f)
            kicked, self._marks_kicked = self._marks_kicked, False
            if any(arm.book.positions for arm in self.arms) and (kicked or now_f - self.last_eval >= self.cfg['eval_interval_s']):
                self.last_eval = now_f
                self._evaluate(now_f)
            self._fulfil_exits(now_f)
        finally:
            self._busy.release()

    def _guard(self, arm, what, function, *args):
        """One arm's step. A failure is counted and logged; it never reaches the runner or the other arms."""
        try:
            return function(*args)
        except AccountingHalt as error:
            arm.halted = 'ACCOUNTING:%s' % error
            self.counts['failures'] += 1
            self.last_error = '%s:%s:%s' % (arm.name, what, type(error).__name__)
            log.error('arm %s halted: %s', arm.name, error)
        except Exception as error:                                       # noqa: BLE001
            self.counts['failures'] += 1
            arm.counts['failures'] += 1
            self.last_error = '%s:%s:%s' % (arm.name, what, type(error).__name__)
            log.exception('arm %s %s failed', arm.name, what)
        return None

    # -- entries -----------------------------------------------------------------------------------------------------------
    def _drain_candidates(self, now_f):
        while True:
            with self._lock:
                ev = self._queue.popleft() if self._queue else None
            if ev is None:
                return
            for arm in self.arms:
                if arm.halted or ev.cid in arm.decided or any(p.cid == ev.cid for p in arm.pending_entries):
                    continue
                if self._guard(arm, 'entry', self._entry, arm, ev, now_f, False) == 'DEFER':
                    arm.pending_entries.append(ev)

    def _retry_deferred(self, now_f):
        for arm in self.arms:
            if arm.halted or not arm.pending_entries:
                continue
            waiting, arm.pending_entries = arm.pending_entries, []
            for ev in waiting:
                if ev.cid in arm.decided:
                    continue
                verdict = self._guard(arm, 'entry', self._entry, arm, ev, now_f, True)
                if verdict == 'DEFER':
                    arm.pending_entries.append(ev)

    def _record(self, arm, ev, action, reasons, features, ts):
        self.store.decision(arm, 'entry', action, mint=ev.mint, candidate_id=ev.cid, reasons=reasons, features=features, ts=ts)
        if action != 'DEFER':
            arm.decided.add(ev.cid)

    def _honeypot(self, ev):
        if ev.hazard:
            return 'blocked'
        if self.runner.honeypot is None:
            return 'off'
        return 'main_passed' if ev.plan is not None else 'unchecked'

    def _skip(self, arm, ev, reasons, features, now_f):
        for reason in reasons:
            arm.skips[reason.split(':')[0]] += 1
        self._record(arm, ev, 'SKIP', reasons, {'honeypot': self._honeypot(ev), **_plain(features)}, now_f)
        return 'DONE'

    def _entry(self, arm, ev, now_f, deferred):
        """One arm, one candidate. Returns 'DEFER' (hold it for the feature) or 'DONE'."""
        usable = self.runner.stale_mark_s
        t_dec = now_f if deferred else ev.ts
        t_int = int(t_dec)
        if ev.hazard:
            return self._skip(arm, ev, ['MAIN_HAZARD:' + ev.hazard], {}, now_f)
        gate = S.entry_decision(ev.features, None, arm.portfolio(t_int, usable), arm.cfg)
        if gate.action != 'BUY' or not gate.quote_required:
            return self._skip(arm, ev, gate.reasons or (gate.action,), gate.detail, now_f)
        detail = {}
        if arm.filter is not None:
            verdict, info = evaluate_filter(arm.filter, ev.screen, self.reader, ev.mint, t_dec)
            detail['entry_filter'] = info
            if verdict == 'NOT_READY':
                if arm.filter['wait_s'] > 0 and now_f - ev.ts < arm.filter['wait_s']:
                    if not deferred:
                        self._record(arm, ev, 'DEFER', [FEATURE_NOT_READY], {'wait_s': arm.filter['wait_s'], **_plain(detail)}, now_f)
                    return 'DEFER'
                return self._skip(arm, ev, [FEATURE_NOT_READY], detail, now_f)
            if verdict == 'REJECT':
                return self._skip(arm, ev, ['ENTRY_FILTER'], detail, now_f)
        priced = self._price_entry(arm, ev, gate.size_sol, now_f, deferred)
        if priced[0] == 'SKIP':
            return self._skip(arm, ev, priced[1], {**detail, **priced[2]}, now_f)
        buy, sell, meta, source, flags, fill_ts = priced
        when = max(t_int if source.startswith('main_') else int(now_f), buy[2], sell[1])
        decision = S.entry_decision(ev.features, A.roundtrip(buy[0], buy[2], sell[0], sell[1]), arm.portfolio(when, usable), arm.cfg)
        info = {**detail, 'price_source': source, 'flags': flags, 'size_sol': str(decision.size_sol), 'decision_detail': decision.detail,
                'deferred_s': round(now_f - ev.ts, 3) if deferred else 0.0,
                'buy_quote': {'in': buy[0].in_amount, 'out': buy[0].out_amount, 'at': buy[2], 'ref': buy[1]},
                'sell_leg': {'in': sell[0].in_amount, 'out': sell[0].out_amount, 'at': sell[1]}}
        if decision.action != 'BUY':
            return self._skip(arm, ev, decision.reasons or (decision.action,), info, now_f)
        features = ev.features
        fill = paper.buy(A.paper_quote(buy[0], mint=ev.mint, side='buy', decimals=features['decimals'], ts=fill_ts, ref=buy[1]),
                         decision.size_sol, arm.pcfg)
        if meta is not None:                                             # L10: ATA rent, transfer-fee haircut, both quotes + latency tax
            fill, extra = self.runner.execution.finish_buy(arm.shim, fill, meta)
            features = {**features, '_exec_state': extra}
        if self.runner.mark_model.effective:                             # M1 #7: the screen's pool fields, for the marks
            features = {**features, '_pool_info': M.screen_pool_info(ev.screen)}
        self._book(arm, fill, ev.cid, source, flags, 'open', A.open_state(features, fill, arm.cfg), None,
                   {'action': 'BUY', 'reasons': ['ENTRY'], 'features': {**info, 'honeypot': self._honeypot(ev)}}, kind='entry', mint=ev.mint)
        arm.counts['entries'] += 1
        return 'DONE'

    def _price_entry(self, arm, ev, size_sol, now_f, deferred):
        """(buy (quote, ref, at), sell leg (quote, at), L10 meta, source, flags, fill ts) or ('SKIP', reasons, detail)."""
        lamports = A.lamports(size_sol)
        plan = None if deferred else ev.plan
        if plan is not None:
            (mq, mref, mat), (sq, sat), xmeta = plan
            ratio = lamports / mq.in_amount
            if 1 / self.cfg['max_scale'] <= ratio <= self.cfg['max_scale']:
                try:
                    buy_q = scale_quote(mq, lamports)
                    sell_q = scale_quote(sq, A.sell_leg_qty(buy_q.out_amount, arm.pcfg))
                except ValueError:
                    return 'SKIP', ['QUOTE_SCALE_FAILED'], {}
                meta = None
                if xmeta is not None:
                    meta = dict(xmeta)
                    for key in ('q0_out', 'q1_out', 'fill_out'):
                        if meta.get(key):
                            meta[key] = meta[key] * lamports // mq.in_amount
                flags = ['SCALED_FROM_MAIN'] + ([] if xmeta is not None else ['LATENCY_MODEL_ABSENT'])
                return ((buy_q, 'main:%s' % mref, mat), (sell_q, sat), meta,
                        'main_entry_quote:%s' % (xmeta or {}).get('fill', 'q0'), flags, ev.ts)
        elif ev.aborted and not deferred:
            return 'SKIP', ['MAIN_ENTRY_ABORTED_LATENCY'], {}
        return self._price_low_lane(arm, ev, lamports, now_f)

    def _price_low_lane(self, arm, ev, lamports, now_f):
        """ONE buy quote on the shared LOW lane; the sell leg is MODELED from the screen's reserves (no second call)."""
        try:
            fee_bps = X.transfer_fee_bps_from_screen(ev.raw) if self.runner.execution is not None else 0
        except X.ExecutionError:
            return 'SKIP', ['TRANSFER_FEE_UNREADABLE'], {}
        shared = ev.low
        ttl = self.runner.cfg.price_ttl_seconds
        if shared is not None and shared[0] == 'ERROR':
            return 'SKIP', ['ARM_QUOTE_FAILED:%s' % shared[1]], {'shared_quote': True}
        if (shared is not None and 0 <= int(now_f) - shared[1] <= ttl
                and 1 / self.cfg['max_scale'] <= lamports / shared[0].in_amount <= self.cfg['max_scale']):
            try:
                quote, at = scale_quote(shared[0], lamports), shared[1]            # another arm paid for this candidate's quote already
            except ValueError:
                return 'SKIP', ['QUOTE_SCALE_FAILED'], {}
            shared_flag = ['SHARED_QUOTE']
        else:
            try:
                quote, _raw, _meta = self.low.jupiter.quote(SOL_MINT, ev.mint, lamports, self.runner.taker)
            except ProviderError as error:
                if is_shed(error):                                       # nothing was sent: no call spent
                    self.counts['entry_shed'] += 1
                    return 'SKIP', [SKIP_SHED], {'shed': (getattr(error, 'meta', None) or {}).get('why')}
                self.calls['jupiter_entry_quote'] += 1
                ev.low = ('ERROR', error.code)
                return 'SKIP', ['ARM_QUOTE_FAILED:%s' % error.code], {}
            self.calls['jupiter_entry_quote'] += 1
            at = self._now()
            ev.low = (quote, at)
            shared_flag = []
        tokens = A.sell_leg_qty(quote.out_amount, arm.pcfg)
        out = self._modeled_sell(ev.screen, quote, tokens, at)
        if out is None:
            return 'SKIP', ['ARM_SKIPPED_NO_SELL_LEG_MODEL'], {}
        sell_q = dataclasses.replace(quote, in_amount=tokens, out_amount=out, other_amount_threshold=None)
        meta = None
        if self.runner.execution is not None:
            meta = {'delay_s': self.runner.execution.cfg.exec_delay_s, 'q0_ref': None, 'q0_out': quote.out_amount, 'q1_ref': None,
                    'q1_out': None, 'transfer_fee_bps': fee_bps, 'fill': 'q0', 'fill_out': quote.out_amount, 'fill_ref': None}
        return ((quote, None, at), (sell_q, at), meta, 'low_lane_entry_quote',
                ['LATENCY_MODEL_ABSENT', 'SELL_LEG_MODELED'] + shared_flag, self.clock())

    def _modeled_sell(self, screen, quote, tokens, now):
        """Lamports a sell quote for ``tokens`` would return from the screen's reserves: the effective mark model, constant product, the
        pool fee and the Pump fee tier, exactly as the main arm's sell-leg quote (taken against the pool as it is, before the entry).
        None when the screen did not carry the reserves."""
        try:
            base, gross = int(screen['base_reserve_raw']), int(screen['quote_gross_raw'])
            virtual, supply = int(screen.get('virtual_quote_reserves_raw') or 0), int(screen['supply_raw'])
            if not 0 < quote.out_amount < base:
                return None
            info = {'virtual_quote_reserves_raw': virtual, 'accrued_fees_raw': 0, 'supply_raw': supply}
            net, _ = self.estimator.value(tokens, base, gross, pool_info=info, now=now, fee_lamports=0)
            return net if net > 0 else None
        except (KeyError, TypeError, ValueError, M.MarkError):
            return None

    # -- persistence of a fill ---------------------------------------------------------------------------------------------
    def _book(self, arm, fill, cid, source, flags, event, state, open_fill_id, decision, *, kind, mint):
        """Apply a fill to the arm's book and persist it with its state (one transaction). Persist first: a failed write leaves the book as
        it was. The decision row follows (a failure to write it is counted, the fill stands)."""
        positions, cash = paper.apply_fill(arm.book.positions, arm.book.cash, fill)
        held = positions.get(fill.mint)
        fill_id, opened = self.store.fill(arm, fill, cash_after=cash, held=held, candidate_id=cid if fill.side == 'buy' else None,
                                          price_source=source, flags=flags, event=event, state=state, open_fill_id=open_fill_id)
        arm.book.positions, arm.book.cash = positions, cash
        arm.book.realized += fill.realized_lamports
        if fill.side == 'buy':
            arm.book.last_buy_ts = fill.ts
        if event == 'closed':
            arm.book.states.pop(fill.mint, None)
            arm.book.closed.append({'mint': fill.mint, 'open_fill_id': opened, 'fill_id': fill_id, 'ts': fill.ts, 'state': state})
        else:
            arm.book.states[fill.mint] = {'open_fill_id': opened, 'state': state}
        arm.persisted[fill.mint] = (fill.ts, Decimal(str(state.get('peak_ratio', 1))))
        if cid is not None:
            arm.decided.add(cid)
        try:
            self.store.decision(arm, kind, decision['action'], mint=mint, candidate_id=cid, reasons=decision['reasons'],
                                features={**_plain(decision['features']), 'fill_id': fill_id}, ts=fill.ts)
        except (sqlite3.Error, ArmsStoreError):
            self.counts['decision_write_failures'] += 1
        return fill_id

    # -- marks -------------------------------------------------------------------------------------------------------------
    def _evaluate(self, now_f):
        mints = {m for arm in self.arms if not arm.halted for m in arm.book.positions}
        if not mints:
            return
        self._refresh_marks(now_f, mints)
        now = int(now_f)
        for arm in self.arms:
            if arm.halted:
                continue
            self._guard(arm, 'exits', self._evaluate_arm, arm, now, now_f)

    def _refresh_marks(self, now_f, mints):
        """Value every arm position: from the accounts the main read just now when it holds the mint, else ONE batched low-lane read,
        else a low-lane quote-mark. The exit lane is never used."""
        ttl = self.runner.cfg.price_ttl_seconds
        with self._lock:
            captured = {m: v for m, v in self._accounts.items() if m in mints}
        fresh = {m: v for m, v in captured.items() if 0 <= now_f - v[1] <= ttl}
        missing = [m for m in mints if m not in fresh]
        source = {m: 'main_marks' for m in fresh}
        if missing:
            read = self._read_low(missing, now_f)
            fresh.update(read)
            source.update({m: 'low_lane_marks' for m in read})
        for arm in self.arms:
            if arm.halted:
                continue
            for mint, position in arm.book.positions.items():
                accounts = fresh.get(mint)
                row = arm.book.states.get(mint)
                if accounts is None or row is None:
                    continue
                try:
                    net, _info = self.model.value_accounts(position.qty_raw, accounts[0], pool_info=row['state'].get('pool_info'),
                                                           now=accounts[1], fee_lamports=arm.pcfg.fee_lamports)
                except (A.AdapterError, KeyError, IndexError):
                    continue
                arm.marks[mint] = (A.sol(net), accounts[1], source[mint])
        budget = self.cfg['quote_marks_per_pass']
        for arm in self.arms:
            if arm.halted:
                continue
            for mint, position in arm.book.positions.items():
                mark = arm.marks.get(mint)
                if budget <= 0 or (mark is not None and now_f - mark[1] <= self.runner.stale_mark_s):
                    continue
                budget -= 1
                got = self._quote_mark(arm, mint, position, now_f)
                if got is not None:
                    arm.marks[mint] = got
        for arm in self.arms:
            for mint in [m for m in arm.marks if m not in arm.book.positions]:
                del arm.marks[mint]

    def _read_low(self, mints, now_f):
        """{mint: (accounts, read_at)} from ONE (chunked) getMultipleAccounts on the low lane. A shed or an error: nothing."""
        model = self.model
        per_key, call_keys = model.keys_per_position, model.call_keys()
        per_call = (100 - len(call_keys)) // per_key
        owners = {}
        for arm in self.arms:
            for mint in arm.book.positions:
                row = arm.book.states.get(mint)
                if row is not None and mint in mints:
                    owners.setdefault(mint, row['state'])
        todo = [m for m in mints if m in owners]
        out = {}
        for start in range(0, len(todo), per_call):
            chunk = todo[start:start + per_call]
            keys = [k for m in chunk for k in model.position_keys(owners[m]['base_vault'], owners[m]['quote_vault'], owners[m].get('pool'))]
            keys += call_keys
            try:
                result, _raw, _meta = self.low.helius.get_multiple_accounts(keys)
                self.calls['helius_marks'] += 1
            except ProviderError as error:
                if is_shed(error):
                    self.counts['marks_shed'] += 1
                else:
                    self.calls['helius_marks'] += 1
                    self.counts['marks_errors'] += 1
                continue
            at = self._now()
            values = result['value']
            if call_keys:
                model.observe_call(values[len(chunk) * per_key:], at)
            for i, mint in enumerate(chunk):
                accounts = values[per_key * i:per_key * (i + 1)]
                if None not in accounts[:2]:
                    out[mint] = (accounts, at)
        return out

    def _quote_mark(self, arm, mint, position, now_f):
        """D1 for an arm: no usable mark, so one low-lane sell quote of the whole quantity values the position."""
        try:
            quote, _raw, _meta = self.low.jupiter.quote(mint, SOL_MINT, position.qty_raw, self.runner.taker)
            self.calls['jupiter_quote_mark'] += 1
        except ProviderError as error:
            if is_shed(error):
                self.counts['quote_mark_shed'] += 1
            else:
                self.calls['jupiter_quote_mark'] += 1
            return None
        return A.sol(max(0, quote.out_amount - arm.pcfg.fee_lamports)), self._now(), 'quote'

    # -- exits -------------------------------------------------------------------------------------------------------------
    def _evaluate_arm(self, arm, now, now_f):
        portfolio = arm.portfolio(now, self.runner.stale_mark_s)
        liquidate = S.should_liquidate(portfolio, arm.cfg)
        delay = 0.0 if self.runner.execution is None else float(self.runner.execution.cfg.exec_delay_s)
        for mint, position in list(arm.book.positions.items()):
            if mint in arm.pending_exits:
                continue
            row = arm.book.states[mint]
            mark, mark_at, source = arm.marks.get(mint, (None, None, None))
            held = A.strategy_position(position, row['state'])
            decision = S.exit_decision(held, mark, None, now, arm.cfg, mark_at=mark_at, liquidate=liquidate)
            if decision.position_updates:
                row['state'] = A.apply_updates(row['state'], decision.position_updates)
                self._persist_update(arm, mint, row, now_f, force='touched_15' in decision.position_updates)
            if decision.action != 'BLOCKED':
                row.pop('no_mark_since', None)
            else:
                arm.counts['exit_blocked'] += 1
                self._write_off_if_stuck(arm, mint, position, row, now_f, decision)
                continue
            if decision.action != 'SELL' or not decision.quote_required:
                continue
            arm.pending_exits[mint] = PendingExit(mint=mint, open_id=row['open_fill_id'], decided_at=now_f, due_at=now_f + delay,
                                                  decided_int=now, qty=A.sell_qty_raw(decision.qty, position.qty_raw),
                                                  reason=decision.reason, mark=mark, mark_at=mark_at, mark_source=source,
                                                  liquidate=liquidate)

    def _persist_update(self, arm, mint, row, now_f, force=False):
        """A peak-only change is written at most once per ``peak_persist_s`` unless it rose >= 1%; memory is authoritative."""
        last = arm.persisted.get(mint)
        try:
            peak = Decimal(str(row['state']['peak_ratio']))
        except (KeyError, InvalidOperation):
            peak = None
        if (not force and last is not None and peak is not None and 0 <= now_f - last[0] < self.cfg['peak_persist_s']
                and peak < last[1] * PEAK_STEP):
            return
        self.store.update_state(arm, mint, row['open_fill_id'], row['state'], ts=now_f)
        arm.persisted[mint] = (now_f, peak if peak is not None else Decimal(1))

    def _write_off_if_stuck(self, arm, mint, position, row, now_f, decision):
        """A position that has had no usable mark for ``unexitable_after_s`` is written off at zero proceeds (a rug the main arm's
        held-risk would have caught); the loss is booked."""
        since = row.setdefault('no_mark_since', now_f)
        if now_f - since >= self.runner.unexitable_after_s:
            self._write_off(arm, mint, position, row, now_f, 'UNEXITABLE_NO_MARK', {'why': 'NO_MARK', 'blocked': list(decision.reasons)})

    def _write_off(self, arm, mint, position, row, now_f, reason, detail):
        state = {k: v for k, v in row['state'].items() if not k.startswith('_')}
        fill = paper.write_off(position, ts=now_f)
        if self.runner.execution is not None:
            state = {**state, **self.runner.execution.write_off_state(state)}
        closed = dict(state, reason=reason, trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                      cooldown_until=S.cooldown_until('STOP', int(now_f), arm.cfg))
        self._book(arm, fill, None, 'write_off', ['WRITE_OFF'], 'closed', closed, row['open_fill_id'],
                   {'action': 'WRITE_OFF', 'reasons': [reason], 'features': detail}, kind='exit', mint=mint)
        arm.pending_exits.pop(mint, None)
        arm.marks.pop(mint, None)
        arm.counts['write_offs'] += 1
        arm.counts['closed'] += 1

    def _fulfil_exits(self, now_f):
        for arm in self.arms:
            if arm.halted or not arm.pending_exits:
                continue
            for mint in list(arm.pending_exits):
                pend = arm.pending_exits.get(mint)
                if pend is not None and now_f >= pend.due_at and now_f >= pend.retry_at:
                    self._guard(arm, 'exit_fill', self._fulfil, arm, pend, now_f)

    def _main_quote(self, mint, qty, pend, now_f):
        """(quote, ref, at, kind) the main runner fetched for ``mint`` that this exit may use, or None."""
        with self._lock:
            held = list(self._sell_quotes.get(mint, ()))
        floor_after_delay = int(pend.due_at)
        for kind, quote, ref, at in reversed(held):
            if kind in EXIT_QUOTE_KINDS and at >= floor_after_delay and 0 <= int(now_f) - at <= self.runner.cfg.price_ttl_seconds:
                if 1 / self.cfg['max_scale'] <= qty / quote.in_amount <= self.cfg['max_scale']:
                    return quote, ref, at, kind
        for kind, quote, ref, at in reversed(held):
            if kind == QUOTE_MARK_KIND and at >= int(pend.decided_at) and 0 <= int(now_f) - at <= self.runner.cfg.price_ttl_seconds:
                if 1 / self.cfg['max_scale'] <= qty / quote.in_amount <= self.cfg['max_scale']:
                    return quote, ref, at, kind
        return None

    def _main_exit_started(self, mint, pend):
        """The main arm has fetched an exit q0 for this mint since the decision: its q1 is coming."""
        with self._lock:
            held = list(self._sell_quotes.get(mint, ()))
        return any(kind in EXIT_QUOTE_KINDS and at >= pend.decided_int for kind, _q, _r, at in held)

    def _price_exit(self, arm, pend, position, now_f):
        """(quote, ref, at, meta, source, flags) or None (not yet: waiting for the main q1, the low lane, or the retry time)."""
        execution = self.runner.execution
        qty = pend.qty
        got = self._main_quote(pend.mint, qty, pend, now_f)
        if got is not None:
            mq, mref, mat, kind = got
            quote = scale_quote(mq, qty)
            if kind == QUOTE_MARK_KIND:
                source, flags = 'main_quote_mark', ['LATENCY_MODEL_ABSENT', 'SCALED_FROM_MAIN']
            else:
                source, flags = 'main_exit_quote', ['SCALED_FROM_MAIN']
            meta = self._exit_meta(execution, None, None, quote.out_amount, 'main:%s' % mref)
            return quote, 'main:%s' % mref, mat, meta, source, flags
        if now_f < pend.due_at + self.cfg['exit_grace_s'] or (self._main_exit_started(pend.mint, pend)
                                                              and now_f < pend.due_at + self.cfg['exit_grace_s'] + 3.0):
            return None                                              # the main arm may be about to quote the same mint
        shared = self._low_sells.get(pend.mint)
        if (shared is not None and shared[1] >= int(pend.due_at) and 0 <= int(now_f) - shared[1] <= self.runner.cfg.price_ttl_seconds
                and 1 / self.cfg['max_scale'] <= qty / shared[0].in_amount <= self.cfg['max_scale']):
            quote, at, shared_flag = scale_quote(shared[0], qty), shared[1], ['SHARED_QUOTE']      # another arm just paid for this quote
        else:
            try:
                quote, _raw, _meta = self.low.jupiter.quote(pend.mint, SOL_MINT, qty, self.runner.taker)
                self.calls['jupiter_exit_quote'] += 1
            except ProviderError as error:
                if is_shed(error):
                    self.counts['exit_shed_waits'] += 1
                    pend.retry_at = now_f + 1.0
                else:
                    self.calls['jupiter_exit_quote'] += 1
                    pend.retry_at = now_f + min(60.0, 10.0 * 2 ** min(pend.attempts, 3))
                    if not error.transient and pend.failing_since is None:
                        pend.failing_since = now_f
                pend.attempts += 1
                # only a shed or a transient failure falls back to the flagged mark fill; "no route" never does (it is unexitable, and
                # is written off after unexitable_after_s like the main arm's D2)
                if error.transient and now_f - pend.decided_at >= self.cfg['exit_wait_s']:
                    return self._mark_fill(arm, pend, position, now_f)
                return None
            at, shared_flag = self._now(), []
            self._low_sells[pend.mint] = (quote, at)
            while len(self._low_sells) > 64:
                self._low_sells.popitem(last=False)
        meta = self._exit_meta(execution, None, None, quote.out_amount, None)
        return quote, None, at, meta, 'low_lane_exit_quote', ([] if execution is not None else ['LATENCY_MODEL_ABSENT']) + shared_flag

    @staticmethod
    def _exit_meta(execution, q0_out, q0_ref, q1_out, q1_ref):
        if execution is None:
            return None
        return {'delay_s': execution.cfg.exec_delay_s, 'q0_ref': q0_ref, 'q0_out': q0_out, 'q1_ref': q1_ref, 'q1_out': q1_out,
                'fill': 'q1', 'fill_out': q1_out, 'fill_ref': q1_ref}

    def _mark_fill(self, arm, pend, position, now_f):
        """No quote could be had for ``exit_wait_s``: fill at the arm's own mark, FLAGGED (no latency model, no executable quote)."""
        if pend.mark is None or pend.mark <= 0:
            return None
        gross = int(pend.mark * paper.LAMPORTS) + arm.pcfg.fee_lamports
        quote = types.SimpleNamespace(in_amount=pend.qty, out_amount=max(1, gross))
        meta = self._exit_meta(self.runner.execution, None, None, quote.out_amount, None)
        return quote, None, self._now(), meta, 'mark_model', ['MARK_FILL', 'LATENCY_MODEL_ABSENT']

    def _fulfil(self, arm, pend, now_f):
        mint = pend.mint
        position, row = arm.book.positions.get(mint), arm.book.states.get(mint)
        if position is None or row is None or row['open_fill_id'] != pend.open_id:
            arm.pending_exits.pop(mint, None)
            return
        if pend.failing_since is not None and now_f - pend.failing_since >= self.runner.unexitable_after_s:
            self._write_off(arm, mint, position, row, now_f, 'UNEXITABLE', {'why': 'NO_ROUTE', 'failing_since': pend.failing_since})
            return
        priced = self._price_exit(arm, pend, position, now_f)
        if priced is None:
            return
        quote, ref, quote_at, meta, source, flags = priced
        now_i = max(int(now_f), quote_at)
        state = row['state']
        held = A.strategy_position(position, state)
        final = S.exit_decision(held, pend.mark, A.strategy_quote(quote, 'sell', quote_at), now_i, arm.cfg,
                                mark_at=None if pend.mark_at is None else pend.mark_at + max(0, now_i - pend.decided_int),
                                liquidate=pend.liquidate)
        if final.action != 'SELL' or A.sell_qty_raw(final.qty, position.qty_raw) != pend.qty:
            if final.action == 'BLOCKED' and final.reason in UNEXITABLE_BLOCKS and pend.failing_since is None:
                pend.failing_since = now_f
            pend.retry_at = now_f + 10.0
            arm.counts['exit_blocked'] += 1
            return
        fill_ts = float(quote_at) if source.startswith('main_') else now_f
        fill = paper.sell(position, A.paper_quote(quote, mint=mint, side='sell', decimals=position.decimals, ts=fill_ts, ref=ref),
                          cfg=arm.pcfg, qty_raw=pend.qty)
        state = {k: v for k, v in state.items() if not k.startswith('_') and k not in ('exit_failing_since', 'exit_failing_reason')}
        if meta is not None:
            fill, extra = self.runner.execution.finish_sell(fill, position, state, meta)
            state = {**state, **extra}
        detail = {'reason': final.reason, 'mark_sol': None if pend.mark is None else str(pend.mark), 'mark_source': pend.mark_source,
                  'price_source': source, 'flags': flags, 'decided_at': pend.decided_at, 'delay_s': round(fill_ts - pend.decided_at, 3),
                  'qty_raw': pend.qty, 'ratio': final.detail.get('ratio')}
        if fill.qty_raw == position.qty_raw:
            closed = dict(state, reason=final.reason, trade_pnl_lamports=position.realized_lamports + fill.realized_lamports,
                          cooldown_until=S.cooldown_until(final.reason, now_i, arm.cfg))
            self._book(arm, fill, None, source, flags, 'closed', closed, row['open_fill_id'],
                       {'action': 'SELL', 'reasons': [final.reason], 'features': detail}, kind='exit', mint=mint)
            arm.marks.pop(mint, None)
            arm.counts['closed'] += 1
        else:
            rung = dict(A.apply_updates(state, final.after_fill), last_reason=final.reason)
            self._book(arm, fill, None, source, flags, 'rung', rung, row['open_fill_id'],
                       {'action': 'SELL', 'reasons': [final.reason], 'features': detail}, kind='exit', mint=mint)
        arm.pending_exits.pop(mint, None)
        arm.counts['exits'] += 1

    # -- health ------------------------------------------------------------------------------------------------------------
    def health(self):
        with self._lock:
            queued, held_quotes, held_accounts = len(self._queue), len(self._sell_quotes), len(self._accounts)
        arms = {}
        for arm in self.arms:
            arms[arm.name] = {'strategy_version': arm.strategy_version, 'config_hash': arm.config_hash, 'halted': arm.halted,
                              'open_positions': len(arm.book.positions), 'pending_exits': len(arm.pending_exits),
                              'pending_entries': len(arm.pending_entries), 'cash_lamports': arm.book.cash,
                              'realized_lamports': arm.book.realized, **dict(arm.counts), 'skips': dict(arm.skips)}
        return {'enabled': True, 'threaded': self.threaded, 'queued': queued, 'captured_quotes': held_quotes,
                'captured_accounts': held_accounts, **dict(self.counts), 'failures': self.counts['failures'],
                'last_error': self.last_error, 'last_at': self.last_at, 'calls': dict(self.calls), 'arms': arms,
                'db': str(self.store.path)}

    def close(self):
        for closer in (self.reader.close, self.store.close):
            try:
                closer()
            except Exception:                                            # noqa: BLE001
                pass


def build(cfg, *, runner, state_dir, keys, code_version, clock=time.time, transport_kwargs=None):
    """The shadow arms of a validated ``arms`` config on the shared LOW lane; None when absent or disabled."""
    if cfg is None or not cfg.get('enabled', True):
        return None
    from lean import providers
    store = ArmsStore(db_path(cfg, state_dir), clock=clock)
    try:
        arms = Arms(cfg, runner=runner, store=store, reader=RowsReader(runner.store.path), low=providers.low(keys, **dict(transport_kwargs or {})),
                    code_version=code_version, clock=clock)
    except BaseException:
        store.close()
        raise
    return arms


# ------------------------------------------------------------------------------------------------------- reading (report, /arms)
def open_ro(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ArmsStoreError('ARMS_DB_MISSING_OR_NOT_REGULAR')
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    connection.execute('PRAGMA query_only=1')
    return connection


def closed_trades(connection, arm=None):
    """Closed arm trades, rent-free like the main report: [{'arm','mint','entry_at','exit_at','cost','proceeds','ret','reason',...}].
    A trade = every fill with one open_fill_id; it is closed when a ``closed`` state row exists."""
    where, args = ('WHERE s.arm_name=?', (arm,)) if arm else ('', ())
    rows = connection.execute(
        'SELECT s.arm_name,s.open_fill_id,s.event,s.state,f.ts,f.mint,f.side,f.sol_lamports,f.fee_lamports,f.realized_lamports,'
        'f.cost_sold_lamports,f.price_source,f.flags,f.id FROM arm_states s JOIN arm_fills f ON f.id=s.fill_id %s ORDER BY s.id' % where,
        args).fetchall()
    trades = collections.OrderedDict()
    for name, open_id, event, state_text, ts, mint, side, sol, fee, realized, cost_sold, source, flags, fill_id in rows:
        payload = (json.loads(state_text) or {}).get('execution')
        row = X.economic({'side': side, 'sol_lamports': sol, 'fee_lamports': fee, 'realized_lamports': realized,
                          'cost_sold_lamports': cost_sold}, payload)
        t = trades.setdefault((name, open_id), {'arm': name, 'open_fill_id': open_id, 'mint': mint, 'entry_at': ts, 'exit_at': None,
                                                'cost': 0, 'proceeds': 0, 'reason': None, 'closed': False, 'sources': set(),
                                                'flags': set(), 'reasons': []})
        t['sources'].add(source)
        t['flags'].update(json.loads(flags))
        if side == 'buy':
            t['cost'] += row['sol_lamports'] + row['fee_lamports']
            t['entry_at'] = min(t['entry_at'], ts)
        else:
            t['proceeds'] += row['sol_lamports'] - row['fee_lamports']
            t['exit_at'] = ts
            t['reasons'].append((json.loads(state_text) or {}).get('last_reason') or (json.loads(state_text) or {}).get('reason'))
        if event == 'closed':
            t['closed'], t['reason'] = True, (json.loads(state_text) or {}).get('reason')
    out = []
    for t in trades.values():
        if t['closed'] and t['cost'] > 0:
            t['ret'] = Decimal(t['proceeds'] - t['cost']) / Decimal(t['cost'])
            t['pnl_lamports'] = t['proceeds'] - t['cost']
            t['hold_s'] = t['exit_at'] - t['entry_at']
            out.append(t)
    return out


def stats(trades, initial_lamports=None, points=200):
    """EV per trade (mean return on cost), win rate, P(<= -50%), PnL, hold, equity curve (initial + realized, stepping at each close)."""
    n = len(trades)
    returns = [t['ret'] for t in trades]
    pnl = sum(t['pnl_lamports'] for t in trades)
    out = {'trades': n, 'wins': sum(1 for r in returns if r > 0), 'pnl_sol': float(Decimal(pnl) / paper.LAMPORTS),
           'ev_per_trade': None, 'win_rate': None, 'p_le_minus_50': None, 'median_return': None, 'median_hold_s': None, 'ev_ci95': None}
    if n:
        mean = sum(returns) / n
        ordered = sorted(returns)
        out.update(ev_per_trade=float(mean), win_rate=out['wins'] / n, p_le_minus_50=sum(1 for r in returns if r <= Decimal('-0.5')) / n,
                   median_return=float(ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2),
                   median_hold_s=sorted(t['hold_s'] for t in trades)[n // 2])
        if n >= 2:
            variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
            half = 1.96 * float(variance.sqrt()) / (n ** 0.5)
            out['ev_ci95'] = [float(mean) - half, float(mean) + half]
    base = Decimal(initial_lamports if initial_lamports is not None else 0)
    running, curve = base, []
    for t in sorted(trades, key=lambda t: t['exit_at']):
        running += t['pnl_lamports']
        curve.append([t['exit_at'], float(running / paper.LAMPORTS)])
    if len(curve) > points:
        step = len(curve) / points
        curve = [curve[int(i * step)] for i in range(points - 1)] + [curve[-1]]
    out['equity_curve'] = curve
    return out


def paired(arm_trades, main_trades):
    """The arm against the main arm on the SAME candidates (a mint both traded; first trade of each). ``main_trades``: [{'mint','ret'}]."""
    first = {}
    for t in sorted(main_trades, key=lambda t: t['entry_at']):
        first.setdefault(t['mint'], t)
    mine = {}
    for t in sorted(arm_trades, key=lambda t: t['entry_at']):
        mine.setdefault(t['mint'], t)
    both = [(mine[m], first[m]) for m in mine if m in first]
    diffs = [a['ret'] - b['ret'] for a, b in both]
    out = {'pairs': len(both), 'arm_only': len(set(mine) - set(first)), 'main_only': len(set(first) - set(mine)),
           'mean_diff': None, 'median_diff': None, 'arm_better_share': None, 'diff_ci95': None,
           'arm_mean': None, 'main_mean': None}
    if both:
        n = len(diffs)
        mean = sum(diffs) / n
        ordered = sorted(diffs)
        out.update(mean_diff=float(mean), median_diff=float(ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2),
                   arm_better_share=sum(1 for d in diffs if d > 0) / n,
                   arm_mean=float(sum(a['ret'] for a, _ in both) / n), main_mean=float(sum(b['ret'] for _, b in both) / n))
        if n >= 2:
            variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
            half = 1.96 * float(variance.sqrt()) / (n ** 0.5)
            out['diff_ci95'] = [float(mean) - half, float(mean) + half]
    return out


def report_section(connection, main_trades=None, *, min_sample=30):
    """The per-arm section of lean.report: trades, EV per trade, win rate, P(<= -50%), equity curve, skips, price sources, health of the
    pricing and the paired comparison vs the main arm. ``connection`` is an open READ-ONLY connection to arms.sqlite."""
    main_trades = main_trades or []
    names = [r[0] for r in connection.execute('SELECT DISTINCT arm_name FROM arm_events ORDER BY arm_name')]
    arms = {}
    for name in names:
        initial = None
        for kind, payload in connection.execute("SELECT kind,payload FROM arm_events WHERE arm_name=? AND kind='arm_init'", (name,)):
            initial = json.loads(payload).get('initial_cash_lamports')
        hashes = [r[0] for r in connection.execute('SELECT DISTINCT config_hash FROM arm_fills WHERE arm_name=?', (name,))] or [
            r[0] for r in connection.execute('SELECT DISTINCT config_hash FROM arm_events WHERE arm_name=?', (name,))]
        versions = [r[0] for r in connection.execute('SELECT DISTINCT strategy_version FROM arm_events WHERE arm_name=?', (name,))]
        trades = closed_trades(connection, name)
        entry = stats(trades, initial)
        skips = collections.Counter()
        for (reasons,) in connection.execute("SELECT reasons FROM arm_decisions WHERE arm_name=? AND kind='entry' AND action='SKIP'", (name,)):
            for reason in set(json.loads(reasons)):
                skips[reason.split(':')[0]] += 1
        decisions = collections.Counter(a for (a,) in connection.execute(
            "SELECT action FROM arm_decisions WHERE arm_name=? AND kind='entry'", (name,)))
        sources = collections.Counter(s for (s,) in connection.execute('SELECT price_source FROM arm_fills WHERE arm_name=?', (name,)))
        flags = collections.Counter()
        for (text,) in connection.execute('SELECT flags FROM arm_fills WHERE arm_name=?', (name,)):
            flags.update(json.loads(text))
        reasons = collections.Counter(t['reason'] for t in trades)
        open_positions = connection.execute(
            "SELECT COUNT(*) FROM (SELECT mint, event FROM arm_states WHERE arm_name=? AND id IN (SELECT MAX(id) FROM arm_states "
            "WHERE arm_name=? GROUP BY mint)) WHERE event!='closed'", (name, name)).fetchone()[0]
        arms[name] = {**entry, 'config_hashes': hashes, 'strategy_versions': versions, 'sample': 'INSUFFICIENT_SAMPLE' if len(trades) < min_sample else 'OK',
                      'open': open_positions, 'entry_decisions': dict(decisions), 'skips': dict(skips.most_common()),
                      'price_sources': dict(sources), 'fill_flags': dict(flags), 'exit_reasons': dict(reasons.most_common()),
                      'paired_vs_main': paired(trades, main_trades) if main_trades else None}
    return {'arms': arms, 'schema_version': SCHEMA_VERSION}
