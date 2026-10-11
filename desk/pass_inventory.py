"""Append-only inventory of COMPLETED original passes (T24S item 1).

The global terminal gate used to decode the outcome page of EVERY completed pass on every call, in two different gates
(history-preparation rejection and cycle no-entry), so its cost was 17 + 2 x (completed passes) page loads and passed the 10 s
cycle deadline within days of 24/7 operation.

This table stores, for each completed pass, the scalars the gates classify an outcome page by (its ``kind``, ``scan_id`` and
``intent_hash`` fields). A row is written

  * by the two publishers that own a receipt kind, in the SAME transaction that sets the pass outcome, and
  * lazily, best effort, by the first gate call that meets a completed pass nobody indexed yet (outcomes written by other code,
    or by code that predates the table). A read-only store, a store inside a review/pin plan (``persist=False``) or a busy
    database simply keeps the work in memory for that call: nothing is waived, only not remembered.

A gate then needs page loads only for passes that are new since the last call, plus ``SAMPLE`` pseudo-random indexed rows (plus
the newest two) that it re-classifies from the retained page and compares with the row. Everything else is checked with SQL
scalars alone: every row must join to a pass with the same intent and outcome hash, the rows must be contiguous, scalar-clean
and match the stored proof digest format, and the table schema is compared byte for byte. The rows are trigger-guarded
(no UPDATE, no DELETE, id = count + 1, bounded). ``full=True`` / ``DESK_GATE_FULL_REPLAY=1`` re-classifies every row, as the gate
always did.

The inventory only caches a CLASSIFICATION of content-addressed pages; it never certifies a receipt (receipts keep their own
proofs and ``verified_index`` rows), and a missing, extra, altered or stale row fails closed.
"""
import sqlite3
from contextlib import closing

from .model import digest
from . import verified_index

TABLE = 'paper_pass_inventory'
VERSION = 1
SAMPLE = 8                      # indexed rows re-classified from their retained page per gate call (plus the newest two)
NEWEST = 2
MAX_ROWS = 16384                # above the 10000 original-pass ceiling terminal._passes enforces
SQL = (f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY,pass_id TEXT NOT NULL UNIQUE,intent_hash TEXT NOT NULL,'
       'outcome_hash TEXT NOT NULL,kind TEXT NOT NULL,scan_id TEXT,page_intent_hash TEXT,proof_digest TEXT NOT NULL)')
INDEX_NAME = f'{TABLE}_kind'
INDEX_SQL = f'CREATE INDEX {INDEX_NAME} ON {TABLE}(kind)'
GUARDS = {
    f'{TABLE}_insert': (f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN NEW.id!=(SELECT count(*) FROM {TABLE})+1 "
                        f"OR (SELECT count(*) FROM {TABLE})>={MAX_ROWS} BEGIN SELECT RAISE(ABORT,'Pass inventory is append-only'); END"),
    f'{TABLE}_update': f"CREATE TRIGGER {TABLE}_update BEFORE UPDATE ON {TABLE} BEGIN SELECT RAISE(ABORT,'Pass inventory is append-only'); END",
    f'{TABLE}_delete': f"CREATE TRIGGER {TABLE}_delete BEFORE DELETE ON {TABLE} BEGIN SELECT RAISE(ABORT,'Pass inventory is append-only'); END",
}
WARN_RATIO = 0.8


def _objects(c):
    return c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?",
                     (TABLE, len(TABLE) + 1, TABLE + '_', TABLE)).fetchall()


def _expected():
    wanted = {('table', TABLE, TABLE, SQL), ('index', INDEX_NAME, TABLE, INDEX_SQL), ('index', f'sqlite_autoindex_{TABLE}_1', TABLE, None)}
    return wanted | {('trigger', n, TABLE, s) for n, s in GUARDS.items()}


def _row_ok(row):
    _id, pass_id, intent, outcome, kind, scan, page_intent, stored = row
    if (type(kind) is not str or len(kind.encode()) > 128 or (scan is not None and (type(scan) is not str or len(scan.encode()) > 128))
            or (page_intent is not None and (type(page_intent) is not str or len(page_intent.encode()) > 128))
            or type(stored) is not str or len(stored) != 64):
        raise ValueError('Pass inventory scalar malformed')
    return row


def row_digest(pass_id, intent_hash, outcome_hash, kind, scan_id, page_intent):
    return digest({'pass_inventory_version': VERSION, 'pass': pass_id, 'intent': intent_hash, 'outcome': outcome_hash,
                   'kind': kind, 'scan': scan_id, 'page_intent': page_intent})


def scalars(kind, scan_id, page_intent):
    """The stored form of a classification: kind '' for 'no string kind', None for an absent scan / intent."""
    return (kind if type(kind) is str else '', scan_id if type(scan_id) is str else None,
            page_intent if type(page_intent) is str else None)


def record(c, pass_id, intent_hash, outcome_hash, kind, scan_id, page_intent):
    """Inside the caller's open write transaction, next to the UPDATE that sets the pass outcome."""
    if not _objects(c):
        c.execute(SQL)
        c.execute(INDEX_SQL)
        for sql in GUARDS.values():
            c.execute(sql)
    kind, scan_id, page_intent = scalars(kind, scan_id, page_intent)
    c.execute(f'INSERT INTO {TABLE}(id,pass_id,intent_hash,outcome_hash,kind,scan_id,page_intent_hash,proof_digest) '
              f'VALUES((SELECT count(*)+1 FROM {TABLE}),?,?,?,?,?,?,?)',
              (pass_id, intent_hash, outcome_hash, kind, scan_id, page_intent,
               row_digest(pass_id, intent_hash, outcome_hash, kind, scan_id, page_intent)))


def _validate(c):
    """Schema + contiguity of the whole table. Returns the row count; any deviation raises.

    Scalars are NOT scanned here: the join with the original passes (checked by the caller) already forces pass_id, intent_hash and
    outcome_hash to equal values ``terminal._passes`` validated, and the columns a caller relies on (kind, scan_id, page_intent_hash,
    proof_digest) are validated on exactly the rows it uses (``_row_ok``)."""
    objects = _objects(c)
    if not objects:
        return 0
    if set(objects) != _expected() or len(objects) != len(_expected()):
        raise ValueError('Pass inventory schema malformed')
    n, low, high = c.execute(f'SELECT count(*),COALESCE(min(id),1),COALESCE(max(id),0) FROM {TABLE}').fetchone()
    if n > MAX_ROWS or (n and (low != 1 or high != n)):
        raise ValueError('Pass inventory not contiguous')
    return n


class Completed:
    """The classified completed passes of one gate call."""

    def __init__(self, indexed, fresh, count, loads, persisted):
        self.indexed_count = indexed          # rows that were already in the table
        self.fresh = fresh                    # rows classified (page loaded) by THIS call
        self.count = count                    # completed passes in total
        self.loads = loads                    # page classifications performed by this call (new + sample)
        self.persisted = persisted
        self._kinds = {}

    def of(self, kind):
        """[(pass_id, scan_id, page_intent_hash, intent_hash, outcome_hash)] of every completed pass whose outcome is ``kind``."""
        return self._kinds.get(kind, [])


def completed(store, kinds, *, persist=True, full=False):
    """Classify the completed passes with page loads only for new passes and a bounded sample. See the module docstring.

    ``kinds``: the outcome kinds the caller needs listed (receipt kinds); other kinds are only counted."""
    kinds = tuple(kinds)
    from . import paper_terminal_reconciliation as terminal
    full = full or verified_index.full_replay_forced()
    with closing(store.connect()) as c:
        c.execute('BEGIN')
        n = _validate(c)
        done = c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NOT NULL').fetchone()[0]
        if n:
            joined = c.execute(f'SELECT count(*) FROM {TABLE} t JOIN paper_observation_passes p ON p.id=t.pass_id '
                               'AND p.intent_hash=t.intent_hash AND p.outcome_hash=t.outcome_hash').fetchone()[0]
            if joined != n:
                raise ValueError('Pass inventory disagrees with the original passes')
        if done < n:
            raise ValueError('Pass inventory has more rows than completed passes')
        fresh_passes = []
        if done > n:
            fresh_passes = c.execute(
                f'SELECT p.id,p.intent_hash,p.outcome_hash FROM paper_observation_passes p ' + (f'LEFT JOIN {TABLE} t ON t.pass_id=p.id ' if n or _objects(c) else '')
                + 'WHERE p.outcome_hash IS NOT NULL' + (' AND t.pass_id IS NULL' if n or _objects(c) else '') + ' ORDER BY p.rowid').fetchall()
            if len(fresh_passes) != done - n:
                raise ValueError('Pass inventory count mismatch')
        if full and n:
            sample_ids = list(range(1, n + 1))
        else:
            seed = digest({'passes': done, 'indexed': n})
            picks = {n - k for k in range(NEWEST) if n - k >= 1}
            picks |= verified_index.distinct_picks(n, {'seed': seed, 'inventory': True}, SAMPLE) if n else set()
            sample_ids = sorted(picks)
        sampled = []
        for rid in sample_ids:
            sampled.append(_row_ok(c.execute(f'SELECT id,pass_id,intent_hash,outcome_hash,kind,scan_id,page_intent_hash,proof_digest FROM {TABLE} WHERE id=?',
                                             (rid,)).fetchone()))
        by_kind = {}
        if n and kinds:
            marks = ','.join('?' * len(kinds))
            for row in c.execute(
                    f'SELECT id,pass_id,intent_hash,outcome_hash,kind,scan_id,page_intent_hash,proof_digest FROM {TABLE} WHERE kind IN ({marks}) ORDER BY id', kinds):
                _id, pass_id, intent, outcome, kind, scan_id, page_intent, stored = _row_ok(row)
                if stored != row_digest(pass_id, intent, outcome, kind, scan_id, page_intent):
                    raise ValueError('Pass inventory row digest mismatch')
                by_kind.setdefault(kind, []).append((pass_id, scan_id, page_intent, intent, outcome))
    loads = 0
    for rid, pass_id, intent, outcome, kind, scan_id, page_intent, stored in sampled:
        found = scalars(*terminal._classification(store, outcome))
        loads += 1
        if (kind, scan_id, page_intent) != found or stored != row_digest(pass_id, intent, outcome, kind, scan_id, page_intent):
            raise ValueError('Pass inventory disagrees with the retained outcome page')
    fresh = []
    for pass_id, intent, outcome in fresh_passes:
        kind, scan_id, page_intent = scalars(*terminal._classification(store, outcome))
        loads += 1
        fresh.append((pass_id, intent, outcome, kind, scan_id, page_intent))
    persisted = bool(fresh) and persist and _persist(store, fresh)
    result = Completed(n, fresh, done, loads, persisted)
    for kind, items in by_kind.items():
        result._kinds[kind] = list(items)
    for pass_id, intent, outcome, kind, scan_id, page_intent in fresh:
        if kind in kinds:
            result._kinds.setdefault(kind, []).append((pass_id, scan_id, page_intent, intent, outcome))
    return result


def _persist(store, fresh):
    """Best effort, on its own connection and without waiting: the gate never blocks on, or fails because of, a busy writer."""
    if getattr(store, 'read_only', False):
        return False
    try:
        with closing(sqlite3.connect(store.path, timeout=0.25, isolation_level=None)) as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                if not _objects(c):
                    c.execute(SQL)
                    c.execute(INDEX_SQL)
                    for sql in GUARDS.values():
                        c.execute(sql)
                for pass_id, intent, outcome, kind, scan_id, page_intent in fresh:
                    if c.execute(f'SELECT 1 FROM {TABLE} WHERE pass_id=?', (pass_id,)).fetchone():
                        continue
                    still = c.execute('SELECT 1 FROM paper_observation_passes WHERE id=? AND intent_hash=? AND outcome_hash=?',
                                      (pass_id, intent, outcome)).fetchone()
                    if not still:
                        raise sqlite3.IntegrityError('pass changed')
                    c.execute(f'INSERT INTO {TABLE}(id,pass_id,intent_hash,outcome_hash,kind,scan_id,page_intent_hash,proof_digest) '
                              f'VALUES((SELECT count(*)+1 FROM {TABLE}),?,?,?,?,?,?,?)',
                              (pass_id, intent, outcome, kind, scan_id, page_intent,
                               row_digest(pass_id, intent, outcome, kind, scan_id, page_intent)))
                c.execute('COMMIT')
            except BaseException:
                c.execute('ROLLBACK')
                raise
    except sqlite3.Error:
        return False
    return True


def usage(store):
    """(rows, ceiling) for the 80 % warning; (0, MAX_ROWS) when the table does not exist."""
    with closing(store.connect()) as c:
        if not _objects(c):
            return 0, MAX_ROWS
        return c.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0], MAX_ROWS
