"""Append-only verified-digest index for retained terminal receipts (T24R F7).

Every retained receipt (history-preparation rejection, cycle no-entry) used to be replayed in full by EVERY terminal gate call,
so a gate cost O(receipts x proof) and the dispatcher calls the gate several times per dispatch. This table records, in the SAME
transaction that retains a receipt (after its full proof passed), the digest of what was proved. A gate then

  * fully replays every receipt that has NO index row (published before this table existed, or by older code: nothing is waived),
  * fully replays a bounded, deterministic sample of the indexed ones (``SAMPLE`` per kind and call; the sample changes with
    every new pass, so every receipt is re-proved over time), and
  * for the other indexed receipts re-checks the cheap binding only: the retained outcome page still loads and hashes to the
    retained key, and its identity fields and index digest still match.

Nothing is waived by the index: a missing, extra, duplicated or disagreeing index row fails the gate closed. The rows are
trigger-guarded (no UPDATE, no DELETE, contiguous ids, unique per (kind, pass)) and the table schema is compared byte for byte.
``full=True`` (or ``DESK_GATE_FULL_REPLAY=1``) replays everything, as the gate always did.
"""
import os
from .model import digest

TABLE = 'paper_verified_receipts'
VERSION = 1
SAMPLE = 8                       # indexed receipts re-proved per kind per gate call
QUICK_SAMPLE = 8                 # of the others, how many get the page-level binding check per gate call (T24S); the rest a presence check
MAX_ROWS = 16384
SQL = (f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,pass_id TEXT NOT NULL,'
       'outcome_hash TEXT NOT NULL,proof_digest TEXT NOT NULL,UNIQUE(kind,pass_id))')
GUARDS = {
    f'{TABLE}_insert': (f"CREATE TRIGGER {TABLE}_insert BEFORE INSERT ON {TABLE} WHEN NEW.id!=(SELECT count(*) FROM {TABLE})+1 "
                        f"OR (SELECT count(*) FROM {TABLE})>={MAX_ROWS} BEGIN SELECT RAISE(ABORT,'Verified receipt index is append-only'); END"),
    f'{TABLE}_update': f"CREATE TRIGGER {TABLE}_update BEFORE UPDATE ON {TABLE} BEGIN SELECT RAISE(ABORT,'Verified receipt index is append-only'); END",
    f'{TABLE}_delete': f"CREATE TRIGGER {TABLE}_delete BEFORE DELETE ON {TABLE} BEGIN SELECT RAISE(ABORT,'Verified receipt index is append-only'); END",
}


def full_replay_forced():
    return os.environ.get('DESK_GATE_FULL_REPLAY') == '1'


def proof_digest(kind, outcome_key, pass_id, scan_id, intent_hash):
    return digest({'verified_index_version': VERSION, 'kind': kind, 'outcome': outcome_key, 'pass': pass_id,
                   'scan': scan_id, 'intent': intent_hash})


def _objects(c):
    return c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name=? OR substr(name,1,?)=? OR tbl_name=?",
                     (TABLE, len(TABLE) + 1, TABLE + '_', TABLE)).fetchall()


def rows(c):
    """{(kind, pass_id): (outcome_hash, proof_digest)}; {} when the table does not exist. Any deviation raises."""
    objects = _objects(c)
    if not objects:
        return {}
    expected = {('table', TABLE, TABLE, SQL)} | {('trigger', n, TABLE, s) for n, s in GUARDS.items()}
    expected |= {('index', f'sqlite_autoindex_{TABLE}_1', TABLE, None)}
    if set(objects) != expected or len(objects) != len(expected):
        raise ValueError('Verified receipt index schema malformed')
    n = c.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0]
    if n > MAX_ROWS or c.execute(
            f"SELECT 1 FROM {TABLE} WHERE typeof(id)!='integer' OR typeof(kind)!='text' OR length(CAST(kind AS BLOB)) NOT BETWEEN 1 AND 64 "
            "OR typeof(pass_id)!='text' OR length(CAST(pass_id AS BLOB))!=32 OR typeof(outcome_hash)!='text' "
            "OR length(CAST(outcome_hash AS BLOB))!=64 OR typeof(proof_digest)!='text' OR length(CAST(proof_digest AS BLOB))!=64 LIMIT 1").fetchone():
        raise ValueError('Verified receipt index scalar malformed')
    ids = [r[0] for r in c.execute(f'SELECT id FROM {TABLE} ORDER BY id')]
    if ids != list(range(1, n + 1)):
        raise ValueError('Verified receipt index not contiguous')
    return {(k, p): (o, d) for k, p, o, d in c.execute(f'SELECT kind,pass_id,outcome_hash,proof_digest FROM {TABLE}')}


def record(c, kind, pass_id, outcome_key, digest_value):
    """Inside the caller's open write transaction, next to the receipt row it vouches for."""
    if not _objects(c):
        c.execute(SQL)
        for sql in GUARDS.values():
            c.execute(sql)
    c.execute(f'INSERT INTO {TABLE}(id,kind,pass_id,outcome_hash,proof_digest) VALUES((SELECT count(*)+1 FROM {TABLE}),?,?,?,?)',
              (kind, pass_id, outcome_key, digest_value))


def plan(index, kind, items, *, seed, full=False, sample=None):
    """Split retained receipts into (replay_fully, quick_check).

    ``items`` are (pass_id, scan_id, intent_hash, outcome_key). Raises ValueError for an index row with no retained receipt or
    one that disagrees with it (either side was altered)."""
    mine = {p: v for (k, p), v in index.items() if k == kind}
    identities = {i[0] for i in items}
    if set(mine) - identities:
        raise ValueError('Verified receipt index has a row without a retained receipt')
    full_items, indexed = [], []
    for item in items:
        stored = mine.get(item[0])
        if stored is None or full:
            full_items.append(item)
            continue
        if stored[0] != item[3] or stored[1] != proof_digest(kind, item[3], item[0], item[1], item[2]):
            raise ValueError('Verified receipt index disagrees with the retained receipt')
        indexed.append(item)
    sample = SAMPLE if sample is None else sample
    ranked = sorted(indexed, key=lambda i: digest({'seed': seed, 'kind': kind, 'pass': i[0]}))
    chosen = ranked[:max(0, sample)]
    picked = {i[0] for i in chosen}
    return full_items + chosen, [i for i in indexed if i[0] not in picked]


def distinct_picks(upto, seed, k):
    """``min(k, upto)`` DISTINCT numbers in 1..upto, pseudo-randomly but deterministically in ``seed`` (bounded draws)."""
    chosen, i = set(), 0
    while len(chosen) < min(k, upto) and i < 64 * max(1, k):
        chosen.add(int(digest({'seed': seed, 'i': i})[:12], 16) % upto + 1)
        i += 1
    n = 1
    while len(chosen) < min(k, upto):        # vanishingly unlikely: fill deterministically
        chosen.add(n)
        n += 1
    return chosen


def split_quick(quick, *, seed, kind, sample=None):
    """T24S: of the indexed receipts that are not replayed, a bounded deterministic sample gets the page-level binding check
    (outcome page loads, hashes to its key, identity fields agree); the others are checked as SQL scalars only (``missing_pages``
    plus the index digest ``plan`` already compared), so a gate call decodes a bounded number of pages whatever the receipt count.
    Returns (checked, rest); the sample moves with ``seed`` so every receipt is checked over time."""
    ordered = sorted(quick, key=lambda i: i[0])
    k = QUICK_SAMPLE if sample is None else sample
    picks = distinct_picks(len(ordered), {'seed': seed, 'kind': kind, 'quick': True}, max(0, k)) if ordered else set()
    return [item for n, item in enumerate(ordered, 1) if n in picks], [item for n, item in enumerate(ordered, 1) if n not in picks]


def missing_pages(c, hashes):
    """Hashes that are not retained evidence pages (a primary-key lookup each; nothing is decoded)."""
    return [h for h in hashes if not c.execute('SELECT 1 FROM pages WHERE hash=?', (h,)).fetchone()]
