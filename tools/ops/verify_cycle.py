"""Read-only paper round-trip snapshot and cold-restart comparison.

    python -m tools.ops.verify_cycle snapshot --data DIR --ledger NAME --config PATH --out snap.json
        [--anchor-from earlier.json]
    python -m tools.ops.verify_cycle compare snapA.json snapB.json [--allow-progress]

Nothing here opens a store for writing, calls a provider, signs or broadcasts.
Fills are attributed per position (mint + entry fill identity), so several coins
may be open or closed at once. Every fill must keep the EXECUTION_UNVERIFIED label.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import stat
import sys
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from pathlib import Path
from urllib.parse import quote

from desk.model import canonical, decimal, digest
from desk.paper_checkpoint import RecoveryRequired, validate_checkpoint
from desk.paper_cycle_cli import _config
from desk.quote_execution import QuoteExecutionError, raw_quantity

KIND = 'paper_cycle_snapshot_v1'
LABEL = 'EXECUTION_UNVERIFIED'
LIVE_CAPTURE = 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'
TOLERANCE = Decimal('1e-18')
DUST = Decimal('1e-20')
MAX_OUTCOMES = 100_000
MAX_PAYLOAD = 1 << 20
MAX_ROLLING = 20_000
STATUSES = ('NO_FILLS', 'OPEN_POSITION', 'VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP')


class VerifyError(ValueError):
    """Evidence is missing, corrupt or contradictory; never repaired here."""


# ---------------------------------------------------------------- read access

def _regular(path, what):
    path = Path(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise VerifyError(f'{what} unreadable') from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise VerifyError(f'{what} must be a regular non-symlink file')
    if path.resolve() != path.absolute():
        raise VerifyError(f'{what} path must be canonical')
    return path


def _quiesced(path):
    """No WAL/SHM sidecar content: the main file is the whole database."""
    wal, shm = Path(str(path) + '-wal'), Path(str(path) + '-shm')
    return not shm.exists() and (not wal.exists() or wal.stat().st_size == 0)


def _is_wal(path):
    """SQLite header bytes 18/19 (file-format write/read versions) are 2 only for WAL."""
    with open(path, 'rb') as stream:
        head = stream.read(100)
    return len(head) == 100 and head[:16] == b'SQLite format 3\x00' and head[18] == 2 and head[19] == 2


def _connect(path, what):
    """Open without write access and without creating sidecar files.

    A reader of a WAL database normally creates -shm/-wal; run as root that would
    leave files the service user cannot open. Only a quiesced WAL database (header
    says WAL, no sidecar content) is therefore opened immutable. Everything else,
    including the live rollback-journal stores (provider-pacing.sqlite, dispatcher
    journals, evidence.sqlite), uses plain mode=ro so locks and any hot journal are
    honoured: immutable=1 on a store that another process can write may read a torn
    or stale view.
    """
    path = _regular(path, what)
    mode = 'immutable=1' if _is_wal(path) and _quiesced(path) else 'mode=ro'
    c = sqlite3.connect(f'file:{quote(str(path))}?{mode}', uri=True, timeout=5)
    c.execute('PRAGMA query_only=ON')
    return c


def _rolling(c, sql, stride=None, anchors=()):
    """Count, cumulative row digests (prefix proof) and the final digest.

    Marks are kept at every ``stride`` rows (bounded by MAX_ROLLING), at the final
    row, and at each anchor count: an anchor is the exact row count of an earlier
    snapshot, so a later snapshot can prove that exact old prefix is unchanged.
    """
    count = c.execute(f'SELECT count(*) FROM ({sql})').fetchone()[0]
    stride = stride or max(1, -(-count // MAX_ROLLING))
    anchors = set(anchors)
    h = hashlib.sha256()
    marks = {}
    n = 0
    for row in c.execute(sql):
        h.update(json.dumps(list(row), separators=(',', ':'), allow_nan=False).encode() + b'\n')
        n += 1
        if n % stride == 0 or n == count or n in anchors:
            marks[str(n)] = h.hexdigest()[:16]
    return {'count': n, 'stride': stride, 'final': h.hexdigest(), 'rolling': marks}


def _anchor_counts(anchor, *path):
    """Row count recorded for the same table in an earlier snapshot, if any."""
    for key in path:
        if type(anchor) is not dict or key not in anchor:
            return ()
        anchor = anchor[key]
    if type(anchor) is dict and type(anchor.get('count')) is int and anchor['count'] > 0:
        return (anchor['count'],)
    return ()


def _tables(c):
    return {n for (n,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ------------------------------------------------------------------ ledger

def _dec(value, what):
    try:
        return decimal(value)
    except (ValueError, TypeError) as exc:
        raise VerifyError(f'{what} is not a finite decimal') from exc


def _near(a, b):
    with localcontext() as ctx:
        ctx.prec = 100
        return abs(a - b) <= TOLERANCE


def _raw(fill, quantity):
    decimals = (fill.get('quote_execution') or {}).get('mint_decimals')
    if type(decimals) is not int:
        return None
    try:
        return raw_quantity(quantity, decimals)
    except (QuoteExecutionError, ValueError) as exc:
        raise VerifyError('fill quantity is not an exact raw token amount') from exc


def _fills(c, state):
    count = c.execute('SELECT count(*) FROM outcomes').fetchone()[0]
    if count > MAX_OUTCOMES or c.execute(
            f'SELECT 1 FROM outcomes WHERE length(CAST(payload AS BLOB))>{MAX_PAYLOAD} LIMIT 1').fetchone():
        raise VerifyError('outcome bound exceeded')
    fills = []
    for seq, event_id, payload in c.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq'):
        try:
            v = json.loads(payload)
        except ValueError as exc:
            raise VerifyError(f'outcome {seq} is not JSON') from exc
        if type(v) is not dict or v.get('type') != 'fill':
            continue
        if v.get('execution_status') != LABEL:
            raise VerifyError(f'fill {seq} lacks the {LABEL} label')
        if v.get('side') not in ('buy', 'sell') or type(v.get('mint')) is not str or not v['mint']:
            raise VerifyError(f'fill {seq} has no side/mint')
        row = c.execute('SELECT ts,payload FROM events WHERE event_id=?', (event_id,)).fetchone()
        if row is None or row[0] > state['last_ts']:
            raise VerifyError(f'fill {seq} is not bound to a journaled event at or before the checkpoint')
        event = json.loads(row[1])
        if event.get('mint') != v['mint'] or (
                'provenance' in v and event.get('provenance') != v['provenance']):
            raise VerifyError(f'fill {seq} contradicts its event identity')
        fills.append({'seq': seq, 'event_id': event_id, 'ts': row[0],
                      'utc': datetime.fromtimestamp(row[0], timezone.utc).isoformat(),
                      'event_pool': event.get('pool'), 'event_taker': event.get('taker'),
                      'event_source_hash': digest(event), 'fill_hash': digest(v), **v})
    ids = [(f['event_id'], f['side'], f['mint']) for f in fills]  # a tuple: no string-concatenation collisions
    if len(ids) != len(set(ids)) or len({f['seq'] for f in fills}) != len(fills):
        raise VerifyError('duplicate fill identity')
    return fills


def _attribute(fills, cfg, state):
    """Per-position episodes; returns (open, closed) and checks all arithmetic."""
    initial = _dec(cfg['initial_equity_sol'], 'initial equity')
    live = {}
    done = []
    cash = initial
    realized = Decimal(0)
    for f in fills:
        mint = f['mint']
        if f['side'] == 'buy':
            if mint in live:
                raise VerifyError(f'second entry into {mint} while its position is open')
            qty, amount, fee = (_dec(f[k], k) for k in ('quantity', 'amount_sol', 'fee_sol'))
            if qty <= 0 or amount <= 0 or fee < 0:
                raise VerifyError(f'buy {f["seq"]} has non-positive size')
            cost = amount + fee
            cash -= cost
            live[mint] = {'mint': mint, 'buy': f, 'qty': qty, 'cost': cost, 'sold': Decimal(0),
                          'basis_used': Decimal(0), 'proceeds': Decimal(0), 'pnl': Decimal(0),
                          'raw': _raw(f, f['quantity']), 'raw_sold': 0, 'sells': []}
            continue
        p = live.get(mint)
        if p is None:
            raise VerifyError(f'sell {f["seq"]} for {mint} has no open entry (sell before buy or oversell)')
        qty, proceeds, pnl, fee = (_dec(f[k], k) for k in ('quantity', 'proceeds_sol', 'realized_pnl_sol', 'fee_sol'))
        if qty <= 0 or proceeds < 0 or fee < 0:
            raise VerifyError(f'sell {f["seq"]} has invalid size')
        if f['ts'] <= p['buy']['ts']:
            raise VerifyError(f'sell {f["seq"]} is not after its entry')
        if (f['event_pool'], f['event_taker']) != (p['buy']['event_pool'], p['buy']['event_taker']):
            raise VerifyError(f'sell {f["seq"]} position identity mismatch')
        if p['sold'] + qty - p['qty'] > DUST:
            raise VerifyError(f'sell {f["seq"]} exceeds the entry quantity')
        raw = _raw(f, f['quantity'])
        if (raw is None) != (p['raw'] is None):
            raise VerifyError(f'sell {f["seq"]} raw inventory evidence inconsistent with its entry')
        if raw is not None:
            p['raw_sold'] += raw
            if p['raw_sold'] > p['raw']:
                raise VerifyError(f'sell {f["seq"]} exceeds the exact raw inventory')
        held = p['qty'] - p['sold']
        if held <= 0:
            raise VerifyError(f'sell {f["seq"]} exceeds the entry quantity')
        with localcontext() as ctx:
            ctx.prec = 100
            # The engine releases cost_left * qty / qty_held with each sell (engine.sell);
            # aggregate checks alone would let PnL be shifted between two sells of one position.
            expected_basis = (p['cost'] - p['basis_used']) * qty / held
        if not _near(proceeds - pnl, expected_basis):
            raise VerifyError(f'sell {f["seq"]} cost basis differs from the proportional cost basis '
                              f'of the remaining position')
        p['sold'] += qty
        p['proceeds'] += proceeds
        p['pnl'] += pnl
        p['basis_used'] += proceeds - pnl
        p['sells'].append(f)
        cash += proceeds
        realized += pnl
        if p['basis_used'] - p['cost'] > TOLERANCE:
            raise VerifyError(f'sell {f["seq"]} consumes more cost basis than was paid')
        if p['qty'] - p['sold'] <= DUST:
            if p['raw'] is not None and p['raw_sold'] != p['raw']:
                raise VerifyError(f'closed position {mint} raw inventory does not balance')
            if not _near(p['basis_used'], p['cost']):
                raise VerifyError(f'closed position {mint} cost basis does not balance')
            done.append(live.pop(mint))
    return list(live.values()), done, initial, cash, realized


def _check_state(state, open_, initial, cash, realized):
    if set(state['positions']) != {p['mint'] for p in open_}:
        raise VerifyError('checkpoint positions differ from the fill journal')
    for p in open_:
        s = state['positions'][p['mint']]
        remaining = p['qty'] - p['sold']
        checks = (('initial_qty', p['qty']), ('qty', remaining), ('initial_cost', p['cost']),
                  ('cost_left', p['cost'] - p['basis_used']), ('trade_pnl', p['pnl']))
        for key, expected in checks:
            if not _near(_dec(s[key], key), expected):
                raise VerifyError(f'checkpoint {key} for {p["mint"]} differs from fills')
    if not _near(_dec(state['realized_pnl'], 'realized_pnl'), realized):
        raise VerifyError('checkpoint realized PnL differs from fills')
    if not _near(_dec(state['cash'], 'cash'), cash):
        raise VerifyError('checkpoint cash differs from fills')
    # Equivalent portfolio identity: cash = initial + realized - cost still held.
    held = sum((_dec(s['cost_left'], 'cost_left') for s in state['positions'].values()), Decimal(0))
    if not _near(_dec(state['cash'], 'cash'), initial + _dec(state['realized_pnl'], 'pnl') - held):
        raise VerifyError('portfolio cash identity fails')


def _position_view(p, state=None):
    buy = p['buy']
    with localcontext() as ctx:
        ctx.prec = 40
        price = str(Decimal(buy['amount_sol']) / Decimal(buy['quantity']))
    return {'mint': p['mint'], 'entry_fill_seq': buy['seq'], 'entry_event_id': buy['event_id'],
            'entry_fill_hash': buy['fill_hash'], 'entry_ts': buy['ts'], 'entry_utc': buy['utc'],
            'size_sol': buy['amount_sol'], 'entry_fee_sol': buy['fee_sol'],
            'quantity': buy['quantity'], 'entry_price_sol_per_token': price,
            'execution_status': LABEL, 'provenance': buy.get('provenance'),
            'sells': [{'seq': s['seq'], 'event_id': s['event_id'], 'ts': s['ts'],
                       'quantity': s['quantity'], 'proceeds_sol': s['proceeds_sol'],
                       'realized_pnl_sol': s['realized_pnl_sol'], 'fill_hash': s['fill_hash']}
                      for s in p['sells']],
            'initial_cost_sol': str(p['cost']), 'proceeds_sol': str(p['proceeds']),
            'realized_pnl_sol': str(p['pnl'])}


def _ledger(path, cfg, anchor=None):
    with closing(_connect(path, 'ledger')) as c:
        c.execute('BEGIN')
        meta = dict(c.execute("SELECT key,value FROM metadata WHERE key IN "
                              "('config','config_hash','implementation_hash')"))
        row = c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        has = c.execute('SELECT 1 FROM events LIMIT 1').fetchone() or c.execute(
            'SELECT 1 FROM outcomes LIMIT 1').fetchone()
        if row is None:
            if has or meta:
                raise VerifyError('CHECKPOINT_MISSING: committed records without a checkpoint')
            raise VerifyError('LEDGER_NEVER_INITIALIZED')
        if not has or c.execute('SELECT 1 FROM outcomes o LEFT JOIN events e ON e.event_id=o.event_id '
                                'WHERE e.event_id IS NULL LIMIT 1').fetchone():
            raise VerifyError('EVENT_JOURNAL_INCOMPLETE')
        if meta.get('config_hash') != digest(cfg) or meta.get('config') != canonical(cfg):
            raise VerifyError('SAVED_CONFIG_MISMATCH')
        try:
            state = validate_checkpoint(c, row[0])
        except RecoveryRequired as exc:
            raise VerifyError('CHECKPOINT_INVALID') from exc
        fills = _fills(c, state)
        open_, closed, initial, cash, realized = _attribute(fills, cfg, state)
        _check_state(state, open_, initial, cash, realized)
        seqs = {t: c.execute(f'SELECT COALESCE(MAX(seq),0),count(*) FROM {t}').fetchone()
                for t in ('events', 'outcomes')}
        tables = {'events': _rolling(c, 'SELECT seq,event_id,ts,payload_hash FROM events ORDER BY seq',
                                     anchors=_anchor_counts(anchor, 'tables', 'events')),
                  'outcomes': _rolling(c, 'SELECT seq,event_id,payload FROM outcomes ORDER BY seq',
                                       anchors=_anchor_counts(anchor, 'tables', 'outcomes'))}
    status = 'OPEN_POSITION' if open_ else ('VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP' if closed else 'NO_FILLS')
    provenance = sorted({f.get('provenance') for f in fills if f.get('provenance')})
    return {
        'status': status, 'fill_count': len(fills),
        'open_positions': [_position_view(p) for p in open_],
        'round_trips': [_position_view(p) for p in closed],
        'cash_sol': state['cash'], 'realized_pnl_sol': state['realized_pnl'],
        'initial_equity_sol': str(initial), 'mode': state['mode'], 'last_ts': state['last_ts'],
        'checkpoint_digest': digest(state),
        'config_hash': meta['config_hash'], 'implementation_hash': meta['implementation_hash'],
        'fills': [{k: f[k] for k in ('seq', 'event_id', 'ts', 'side', 'mint', 'fill_hash',
                                     'event_source_hash')} for f in fills],
        'events': {'count': seqs['events'][1], 'max_seq': seqs['events'][0]},
        'outcomes': {'count': seqs['outcomes'][1], 'max_seq': seqs['outcomes'][0]},
        'tables': tables, 'provenance': provenance,
        'all_live_capture_provenance': bool(fills) and provenance == [LIVE_CAPTURE],
        'execution_status': LABEL,
    }


# ------------------------------------------------- budgets, pacing, journal

def _optional(path, reader, what):
    if not os.path.lexists(path):
        return {'present': False}
    with closing(_connect(path, what)) as c:
        c.execute('BEGIN')
        return {'present': True, **reader(c)}


def _monitoring(c, anchor=None):
    have = _tables(c)
    if 'paper_monitoring_budget' not in have:
        return {'provisioned': False}
    cap, window, high, total, blocked = c.execute(
        'SELECT cap,window_seconds,high_water,total,blocked FROM paper_monitoring_budget WHERE id=1').fetchone()
    pending = c.execute('SELECT count(*) FROM paper_monitoring_reservations r LEFT JOIN '
                        'paper_monitoring_outcomes o ON o.reservation_id=r.id '
                        'WHERE o.reservation_id IS NULL').fetchone()[0]
    dup = c.execute('SELECT count(*) FROM (SELECT 1 FROM paper_monitoring_reservations GROUP BY '
                    'scan_id,mint,checkpoint_hash,method,params_hash HAVING count(*)>1)').fetchone()[0]
    return {'provisioned': True, 'cap': cap, 'window_seconds': window, 'high_water': high,
            'used_total': total, 'blocked': blocked, 'reserved_pending': pending,
            'duplicate_reservation_groups': dup,
            'reservations': _rolling(c, 'SELECT id,at,scan_id,mint,checkpoint_hash,method,params_hash '
                                        'FROM paper_monitoring_reservations ORDER BY id',
                                     anchors=_anchor_counts(anchor, 'reservations')),
            'outcomes': _rolling(c, 'SELECT reservation_id,evidence_hash FROM paper_monitoring_outcomes '
                                    'ORDER BY reservation_id',
                                 anchors=_anchor_counts(anchor, 'outcomes'))}


def _ownership(c):
    if 'ownership_budgets' not in _tables(c):
        return {'budgets': []}
    rows = c.execute('SELECT id,source_hash,used,ceiling FROM ownership_budgets ORDER BY id').fetchall()
    return {'budgets': [list(r) for r in rows], 'used_total': sum(r[2] for r in rows)}


def _pacing(c):
    """Stable part only: high_water must never decrease. Cadence state is volatile."""
    rows = c.execute('SELECT provider,high_water FROM state ORDER BY provider').fetchall()
    return {'providers': [list(r) for r in rows]}


def _pacing_volatile(c):
    """next_at/blocked_until/pending/waiters change with every provider call: report only."""
    rows = c.execute('SELECT provider,next_at,blocked_until,pending IS NOT NULL '
                     'FROM state ORDER BY provider').fetchall()
    waiters = c.execute('SELECT count(*) FROM waiters').fetchone()[0]
    return {'providers': [list(r) for r in rows], 'waiters': waiters}


def _journal(directory, anchor=None):
    out = {}
    if not os.path.lexists(directory):
        return {'present': False}
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise VerifyError('journal directory must be a real directory')
    for path in sorted(root.glob('*.sqlite')):
        with closing(_connect(path, 'dispatcher journal')) as c:
            c.execute('BEGIN')
            have = _tables(c)
            out[path.name] = {t: _rolling(c, f'SELECT id,hash FROM {t} ORDER BY rowid',
                                          anchors=_anchor_counts(anchor, 'journals', path.name, t))
                              for t in ('context', 'intents', 'results') if t in have}
    return {'present': True, 'journals': out}


def snapshot(data, ledger, config, *, evidence='evidence.sqlite', pacing='provider-pacing.sqlite',
             journal='entry-dispatch', anchor=None):
    """Read-only snapshot. ``anchor`` is an earlier snapshot of the same stores: the new
    snapshot then records its row digests at the exact old row counts, which is what lets
    ``compare --allow-progress`` prove the old prefix beyond the rolling-mark bound."""
    data = Path(data)
    if data.resolve() != data.absolute() or not data.is_dir():
        raise VerifyError('data directory must be a canonical directory')
    for name in (ledger, evidence, pacing, journal):
        if Path(name).name != name or name in ('', '.', '..'):
            raise VerifyError('store names are plain names inside --data')
    if anchor is not None and (type(anchor) is not dict or anchor.get('kind') != KIND):
        raise VerifyError('anchor must be a paper_cycle_snapshot_v1 document')
    anchor = anchor or {}
    cfg = _config(config)
    result = _ledger(data / ledger, cfg, anchor)
    # paper_monitoring_* and ownership_budgets both live in the EvidenceStore.
    result.update({
        'kind': KIND, 'ledger': ledger,
        'monitoring': _optional(data / evidence, lambda c: _monitoring(c, anchor.get('monitoring')),
                                'evidence store'),
        'ownership': _optional(data / evidence, _ownership, 'evidence store'),
        'pacing': _optional(data / pacing, _pacing, 'pacing store'),
        'pacing_volatile': _optional(data / pacing, _pacing_volatile, 'pacing store'),
        'dispatcher': _journal(data / journal, anchor.get('dispatcher')),
        'taken_at': datetime.now(timezone.utc).isoformat()})
    return result


# ----------------------------------------------------------------- compare

# Not part of the strict equality: wall-clock, and pacing cadence state that changes with every
# provider call. The shared pacing high_water is checked separately (it may rise, never fall).
NOT_STRICT = ('taken_at', 'pacing', 'pacing_volatile')


def _comparable(s):
    return {k: v for k, v in s.items() if k not in NOT_STRICT}


def _check_snapshot(s, label, failures):
    if s.get('kind') != KIND or s.get('status') not in STATUSES:
        failures.append(f'{label}: not a {KIND}')
        return False
    fills = s['fills']
    keys = [(f['event_id'], f['side'], f['mint']) for f in fills]
    if len(keys) != len(set(keys)) or len({f['seq'] for f in fills}) != len(fills):
        failures.append(f'{label}: duplicate fill')
    return True


def _prefix(old, new, name, failures):
    """The exact old prefix must reappear unchanged: the new snapshot must hold the digest
    at the old row count (snapshot --anchor-from), never a nearby stride mark."""
    if new['count'] < old['count']:
        failures.append(f'{name}: rows were removed')
        return
    if old['count'] == 0:
        return
    if new['count'] == old['count']:
        if new['final'] != old['final']:
            failures.append(f'{name}: original rows changed')
        return
    mark = new['rolling'].get(str(old['count']))
    if mark is None:
        failures.append(f'{name}: old prefix cannot be verified at its exact row count '
                        f'{old["count"]}; retake the later snapshot with --anchor-from')
    elif mark != old['final'][:16]:
        failures.append(f'{name}: original rows changed within the first {old["count"]}')


def _pacing_checks(a, b, failures):
    pa = {r[0]: r for r in a['pacing'].get('providers', [])} if a['pacing'].get('present') else {}
    pb = {r[0]: r for r in b['pacing'].get('providers', [])} if b['pacing'].get('present') else {}
    for k, r in pa.items():
        if k not in pb or pb[k][1] < r[1]:
            failures.append('pacing high-water regressed')
            break


def compare(a, b, *, allow_progress=False):
    failures = []
    if not (_check_snapshot(a, 'A', failures) and _check_snapshot(b, 'B', failures)):
        return {'status': 'FAIL', 'failures': failures}
    for key in ('ledger', 'config_hash', 'implementation_hash', 'initial_equity_sol'):
        if a[key] != b[key]:
            failures.append(f'{key} differs')
    if not allow_progress:
        if _comparable(a) != _comparable(b):
            for key in sorted(set(a) | set(b)):
                if key != 'taken_at' and a.get(key) != b.get(key):
                    failures.append(f'{key} differs')
    else:
        if b['fills'][:len(a['fills'])] != a['fills']:
            failures.append('fills: old prefix differs')
        for t in ('events', 'outcomes'):
            if b[t]['max_seq'] < a[t]['max_seq'] or b[t]['count'] < a[t]['count']:
                failures.append(f'{t}: regressed')
            _prefix(a['tables'][t], b['tables'][t], f'ledger {t}', failures)
        if b['last_ts'] < a['last_ts']:
            failures.append('last_ts regressed')
        ma, mb = a['monitoring'], b['monitoring']
        if ma.get('provisioned'):
            if not mb.get('provisioned') or mb['used_total'] < ma['used_total'] or mb['cap'] != ma['cap']:
                failures.append('monitoring budget regressed')
            else:
                _prefix(ma['reservations'], mb['reservations'], 'monitoring reservations', failures)
                _prefix(ma['outcomes'], mb['outcomes'], 'monitoring outcomes', failures)
                if mb['high_water'] < ma['high_water']:
                    failures.append('monitoring high-water regressed')
        old = {r[0]: r for r in a['ownership'].get('budgets', [])} if a['ownership'].get('present') else {}
        new = {r[0]: r for r in b['ownership'].get('budgets', [])} if b['ownership'].get('present') else {}
        for k, r in old.items():
            if k not in new or new[k][1] != r[1] or new[k][3] != r[3] or new[k][2] < r[2]:
                failures.append('ownership budget regressed or rewritten')
                break
        ja, jb = a['dispatcher'], b['dispatcher']
        if ja.get('present'):
            if not jb.get('present'):
                failures.append('dispatcher journal removed')
            else:
                for name, tables in ja['journals'].items():
                    for t, old_t in tables.items():
                        if name not in jb['journals'] or t not in jb['journals'][name]:
                            failures.append(f'dispatcher {name}.{t} removed')
                        else:
                            _prefix(old_t, jb['journals'][name][t], f'dispatcher {name}.{t}', failures)
    _pacing_checks(a, b, failures)
    volatile = [k for k in ('pacing_volatile',) if a.get(k) != b.get(k)]
    da = a['monitoring'].get('duplicate_reservation_groups', 0)
    db = b['monitoring'].get('duplicate_reservation_groups', 0)
    if db > da:
        failures.append('duplicate monitoring charge appeared')
    return {'status': 'FAIL' if failures else 'PASS', 'mode': 'allow_progress' if allow_progress else 'strict',
            'failures': failures, 'volatile': volatile,
            'a_digest': digest(_comparable(a)), 'b_digest': digest(_comparable(b))}


# --------------------------------------------------------------------- CLI

def _write_new(path, value):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, indent=1, allow_nan=False)


def _load(path):
    with open(_regular(path, 'snapshot'), 'rb') as stream:
        raw = stream.read(64 * 1024 * 1024 + 1)
    if len(raw) > 64 * 1024 * 1024:
        raise VerifyError('snapshot exceeds bound')
    return json.loads(raw)


def main(argv=None):
    p = argparse.ArgumentParser(prog='verify_cycle', description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('snapshot')
    s.add_argument('--data', required=True)
    s.add_argument('--ledger', required=True)
    s.add_argument('--config', required=True)
    s.add_argument('--out', required=True)
    for name, default in (('evidence', 'evidence.sqlite'),
                          ('pacing', 'provider-pacing.sqlite'), ('journal', 'entry-dispatch')):
        s.add_argument(f'--{name}', default=default)
    s.add_argument('--anchor-from', help='earlier snapshot: record digests at its exact row counts '
                                         'so compare --allow-progress can prove the old prefix')
    c = sub.add_parser('compare')
    c.add_argument('a')
    c.add_argument('b')
    c.add_argument('--allow-progress', action='store_true')
    a = p.parse_args(argv)
    try:
        if a.command == 'snapshot':
            result = snapshot(a.data, a.ledger, a.config, evidence=a.evidence, pacing=a.pacing,
                              journal=a.journal, anchor=_load(a.anchor_from) if a.anchor_from else None)
            _write_new(a.out, result)
            print(json.dumps({'status': result['status'], 'fill_count': result['fill_count'],
                              'open_positions': len(result['open_positions']),
                              'round_trips': len(result['round_trips']), 'out': a.out,
                              'checkpoint_digest': result['checkpoint_digest'],
                              'execution_status': LABEL}, sort_keys=True))
            return 0
        result = compare(_load(a.a), _load(a.b), allow_progress=a.allow_progress)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'PASS' else 1
    except (VerifyError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({'status': 'ERROR', 'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
