"""Counterfactual candidate tracking: learn whether the filters pick winners.

Research only. Records the forward PumpSwap vault-reserve price path of every
migration candidate (admitted, rejected or never dispatched) in its OWN
append-only store. It never touches the entry/monitoring budgets, the ledger or
a fill; its requests are charged against its own recorded hourly allowance and
go through the shared provider pacing. Prices here are NOT fill evidence.

    python -m tools.research.counterfactual init --store S [--allowance-per-hour 300]
    python -m tools.research.counterfactual ingest --store S --discovery-db D [--journal J]
    python -m tools.research.counterfactual sample --store S [--systemd-credentials]
    python -m tools.research.counterfactual report --store S
"""
import argparse
import base64
import json
import math
import sqlite3
import statistics
import sys
import time
from contextlib import closing
from decimal import Decimal
from pathlib import Path

HORIZONS = (300, 900, 1800, 3600, 7200, 21600)  # +5m +15m +30m +1h +2h +6h
LABEL = 'RESEARCH_ONLY_NOT_FILL_EVIDENCE'
DEFAULT_ALLOWANCE = 300
DEFAULT_GRACE = 120
BATCH_TASKS = 50  # two vault accounts per task -> 100 accounts, the RPC maximum
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SCHEMA = '''
CREATE TABLE candidates(mint TEXT PRIMARY KEY,pool TEXT NOT NULL,signature TEXT NOT NULL,slot INTEGER NOT NULL,
  migrated_at REAL NOT NULL,discovery_seq INTEGER NOT NULL,added_at REAL NOT NULL);
CREATE TABLE outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT NOT NULL,at REAL NOT NULL,payload TEXT NOT NULL);
CREATE TABLE vaults(mint TEXT PRIMARY KEY,base_vault TEXT,quote_vault TEXT,status TEXT NOT NULL,code TEXT,at REAL NOT NULL);
CREATE TABLE requests(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,kind TEXT NOT NULL,accounts INTEGER NOT NULL);
CREATE TABLE request_results(request_id INTEGER PRIMARY KEY,status TEXT NOT NULL,code TEXT,slot INTEGER);
CREATE TABLE samples(mint TEXT NOT NULL,horizon INTEGER NOT NULL,due_at REAL NOT NULL,sampled_at REAL,slot INTEGER,
  base_raw TEXT,quote_raw TEXT,price TEXT,status TEXT NOT NULL,code TEXT,request_id INTEGER,PRIMARY KEY(mint,horizon));
CREATE TABLE policy(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,allowance_per_hour INTEGER NOT NULL,grace_seconds INTEGER NOT NULL);
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
'''
APPEND_ONLY = ('candidates', 'outcomes', 'vaults', 'requests', 'request_results', 'samples', 'policy')


class CounterfactualError(ValueError):
    pass


class TransportError(CounterfactualError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _connect(path, *, readonly=False):
    path = Path(path)
    if readonly:
        return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    return sqlite3.connect(path, isolation_level=None, timeout=5)


def init(store, *, allowance_per_hour=DEFAULT_ALLOWANCE, grace_seconds=DEFAULT_GRACE, now=None):
    if type(allowance_per_hour) is not int or not 1 <= allowance_per_hour <= 3600 or type(grace_seconds) is not int or not 0 <= grace_seconds <= 3600:
        raise CounterfactualError('Allowance/grace out of range')
    path = Path(store)
    if path.exists():
        raise CounterfactualError('Store already exists; the counterfactual store is append-only')
    now = time.time() if now is None else now
    with closing(_connect(path)) as c:
        c.execute('BEGIN IMMEDIATE')
        for statement in SCHEMA.strip().split(';'):
            if statement.strip():
                c.execute(statement)
        for table in APPEND_ONLY:
            for action in ('UPDATE', 'DELETE'):
                c.execute(f"CREATE TRIGGER {table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'Counterfactual record is immutable'); END")
        c.execute('INSERT INTO policy(at,allowance_per_hour,grace_seconds) VALUES(?,?,?)', (now, allowance_per_hour, grace_seconds))
        c.execute('INSERT INTO meta VALUES(?,?)', ('label', LABEL))
        c.execute('INSERT INTO meta VALUES(?,?)', ('last_discovery_seq', '0'))
        c.commit()
    path.chmod(0o600)


def set_allowance(store, allowance_per_hour, *, grace_seconds=None, now=None):
    """Append a new recorded policy row; earlier rows stay as history."""
    with closing(_connect(store)) as c:
        grace = c.execute('SELECT grace_seconds FROM policy ORDER BY id DESC LIMIT 1').fetchone()[0] if grace_seconds is None else grace_seconds
        c.execute('INSERT INTO policy(at,allowance_per_hour,grace_seconds) VALUES(?,?,?)',
                  (time.time() if now is None else now, allowance_per_hour, grace))


def _policy(c):
    return c.execute('SELECT allowance_per_hour,grace_seconds FROM policy ORDER BY id DESC LIMIT 1').fetchone()


# ---------------------------------------------------------------- ingestion
def add_candidate(store, *, mint, pool, signature, slot, migrated_at, seq, now=None):
    with closing(_connect(store)) as c:
        c.execute('INSERT OR IGNORE INTO candidates VALUES(?,?,?,?,?,?,?)',
                  (mint, pool, signature, slot, float(migrated_at), seq, time.time() if now is None else now))
        return c.total_changes > 0


def discovery_hints(discovery_db, *, after_seq=0, limit=500):
    """Migration hints from continuous discovery (read-only), same decode as the dispatcher.

    Rows whose stored hash does not match their payload are skipped and counted:
    a research tool must never learn from altered originals.
    """
    from desk.decode import decode
    from desk.model import digest
    from tools import paper_entry_dispatcher as dispatcher
    hints, skipped = [], 0
    with closing(_connect(discovery_db, readonly=True)) as d:
        d.execute('BEGIN')
        rows = d.execute('SELECT seq,source_id,received_at,slot,payload_hash FROM raw_events WHERE seq>? ORDER BY seq LIMIT ?', (after_seq, limit)).fetchall()
        for seq, source, received, slot, stored_hash in rows:
            try:
                payload = d.execute('SELECT payload FROM raw_events WHERE seq=?', (seq,)).fetchone()[0]
                raw = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
                if digest(raw) != stored_hash or type(received) not in (int, float) or not math.isfinite(received):
                    raise ValueError('Original discovery record altered')
                decoded = decode(raw)
                if decoded['status'] != 'OBSERVED':
                    continue
                found = [o for o in decoded['program_observations']
                         if o.get('name') in ('migrate', 'migrate_v2') and o.get('status') == 'IDENTIFIED'
                         and dispatcher._migration_event_hint(raw, decoded, o)]
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
                skipped += 1
                continue
            if len(found) == 1:  # ambiguous notifications are never candidates
                hints.append({'seq': seq, 'mint': found[0]['mint'], 'pool': found[0]['pool'],
                              'signature': decoded['signature'], 'slot': slot, 'migrated_at': received})
    return hints, skipped, (rows[-1][0] if rows else after_seq)


def _outcome(journal_db, mint):
    """Dispatcher outcome for a mint (read-only join); never raises on odd records."""
    if journal_db is None:
        return {'dispatched': None, 'status': None, 'codes': []}
    try:
        with closing(_connect(journal_db, readonly=True)) as j:
            j.execute('BEGIN')
            row = j.execute('SELECT id FROM intents WHERE mint=?', (mint,)).fetchone()
            if row is None:
                return {'dispatched': False, 'status': None, 'codes': []}
            result = j.execute('SELECT payload FROM results WHERE id=?', (row[0],)).fetchone()
            if result is None:
                return {'dispatched': True, 'status': 'UNRESOLVED', 'codes': []}
            body = json.loads(result[0]).get('result') or {}
            codes = [x for x in (body.get('blockers') or ([body['reason']] if body.get('reason') else [])) if type(x) is str]
            return {'dispatched': True, 'status': body.get('status'), 'codes': codes}
    except (sqlite3.Error, ValueError, TypeError, AttributeError):
        return {'dispatched': None, 'status': 'OUTCOME_UNREADABLE', 'codes': []}


def ingest(store, discovery_db, *, journal_db=None, now=None, limit=500):
    now = time.time() if now is None else now
    with closing(_connect(store)) as c:
        after = int(c.execute("SELECT value FROM meta WHERE key='last_discovery_seq'").fetchone()[0])
    hints, skipped, last = discovery_hints(discovery_db, after_seq=after, limit=limit)
    added = 0
    for h in hints:
        added += bool(add_candidate(store, mint=h['mint'], pool=h['pool'], signature=h['signature'], slot=h['slot'],
                                    migrated_at=h['migrated_at'], seq=h['seq'], now=now))
    with closing(_connect(store)) as c:
        c.execute("INSERT OR REPLACE INTO meta VALUES('last_discovery_seq',?)", (str(last),))
    refresh_outcomes(store, journal_db, now=now)
    return {'candidates_added': added, 'rows_skipped': skipped, 'last_seq': last}


def refresh_outcomes(store, journal_db, *, now=None):
    """Append an outcome row only when the dispatcher outcome changed."""
    now = time.time() if now is None else now
    with closing(_connect(store)) as c:
        for (mint,) in c.execute('SELECT mint FROM candidates').fetchall():
            new = _outcome(journal_db, mint)
            last = c.execute('SELECT payload FROM outcomes WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,)).fetchone()
            if last is None or json.loads(last[0]) != new:
                c.execute('INSERT INTO outcomes(mint,at,payload) VALUES(?,?,?)', (mint, now, json.dumps(new, sort_keys=True)))


# ----------------------------------------------------------------- price
def token_account_amount(account, *, mint, owner):
    """Raw amount of an SPL token account; any deviation fails closed."""
    from desk.security import TOKEN_PROGRAM, TOKEN_2022, base58
    if type(account) is not dict or account.get('owner') not in (TOKEN_PROGRAM, TOKEN_2022) or account.get('executable') is not False:
        raise CounterfactualError('VAULT_ACCOUNT_INVALID')
    data = account.get('data')
    if type(data) is not list or len(data) != 2 or data[1] != 'base64' or type(data[0]) is not str:
        raise CounterfactualError('VAULT_ACCOUNT_ENCODING_INVALID')
    raw = base64.b64decode(data[0], validate=True)
    if len(raw) < 165 or base58(raw[0:32]) != mint or base58(raw[32:64]) != owner:
        raise CounterfactualError('VAULT_ACCOUNT_IDENTITY_MISMATCH')
    return int.from_bytes(raw[64:72], 'little')


def price_from_reserves(base_raw, quote_raw):
    """PumpSwap constant product spot: quote lamports per raw base unit."""
    if type(base_raw) is not int or type(quote_raw) is not int or base_raw < 0 or quote_raw < 0:
        raise CounterfactualError('RESERVES_INVALID')
    return None if base_raw == 0 or quote_raw == 0 else Decimal(quote_raw) / Decimal(base_raw)


def metrics(samples):
    """Max gain/drawdown, return and liquidity per horizon, pool death. Baseline = first priced sample."""
    priced = [(s['horizon'], Decimal(s['price'])) for s in samples if s['status'] == 'OK' and s['price'] is not None]
    out = {'baseline_horizon': None, 'returns': {}, 'liquidity_lamports': {}, 'max_gain': None, 'max_drawdown': None,
           'pool_died': any(s['status'] == 'POOL_DEAD' for s in samples)}
    for s in samples:
        if s['status'] in ('OK', 'POOL_DEAD') and s['quote_raw'] is not None:
            out['liquidity_lamports'][s['horizon']] = 2 * int(s['quote_raw'])
    if not priced:
        if out['pool_died']:
            out['max_drawdown'] = Decimal(-1)
        return out
    out['baseline_horizon'], p0 = priced[0]
    peak, worst = p0, Decimal(0)
    for horizon, price in priced:
        out['returns'][horizon] = price / p0 - 1
        peak = max(peak, price)
        worst = min(worst, price / peak - 1)
    out['max_gain'] = max(out['returns'].values())
    out['max_drawdown'] = Decimal(-1) if out['pool_died'] else worst
    return out


# --------------------------------------------------------------- sampling
class HeliusTransport:
    """Batched JSON-RPC through the shared Helius pacing; the key comes from the managed credential."""

    def __init__(self, pacer):
        from desk.coordinator_rpc import _ENDPOINT
        import os
        key = os.environ.get('HELIUS_API_KEY')
        if pacer is None:
            raise TransportError('PACING_NOT_CONFIGURED')
        if type(key) is not str or not 1 <= len(key) <= 512 or any(not 33 <= ord(ch) <= 126 for ch in key):
            raise TransportError('CREDENTIAL_UNAVAILABLE')
        self.pacer, self.key, self.endpoint = pacer, key, _ENDPOINT

    def __call__(self, method, params):
        from urllib.error import HTTPError, URLError
        from urllib.parse import urlencode
        from urllib.request import Request, build_opener, HTTPRedirectHandler
        from desk import provider_pacing
        try:
            ticket = self.pacer.acquire('helius', timeout_seconds=20)
        except provider_pacing.PacingError as error:
            raise TransportError(error.code) from None
        body = json.dumps({'jsonrpc': '2.0', 'id': 'counterfactual-v1', 'method': method, 'params': params}).encode()
        request = Request(self.endpoint + '?' + urlencode({'api-key': self.key}), data=body, method='POST',
                          headers={'Content-Type': 'application/json', 'Accept': 'application/json'})

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        released = False
        try:
            with build_opener(NoRedirect()).open(request, timeout=15) as response:
                if provider_pacing.should_throttle(response.status, response.headers):
                    self.pacer.throttle('helius', response.headers, ticket=ticket); released = True
                    raise TransportError('HTTP_THROTTLED')
                if response.status != 200:
                    raise TransportError('HTTP_REJECTED')
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            if provider_pacing.should_throttle(error.code, error.headers) and not released:
                self.pacer.throttle('helius', error.headers, ticket=ticket); released = True
            raise TransportError('HTTP_REJECTED') from None
        except (URLError, OSError, TimeoutError):
            raise TransportError('TRANSPORT_ERROR') from None
        finally:
            if not released:
                try:
                    self.pacer.finish('helius', ticket)
                except provider_pacing.PacingError:
                    pass
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError('RESPONSE_OVERSIZED')
        try:
            decoded = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
        except ValueError:
            raise TransportError('RESPONSE_INVALID') from None
        if type(decoded) is not dict or set(decoded) != {'jsonrpc', 'id', 'result'} or decoded['id'] != 'counterfactual-v1':
            raise TransportError('RESPONSE_INVALID')
        return decoded['result']


def _charge(c, now, kind, accounts):
    """Atomically enforce the rolling-hour allowance and record the request as charged."""
    c.execute('BEGIN IMMEDIATE')
    allowance = _policy(c)[0]
    used = c.execute('SELECT COUNT(*) FROM requests WHERE at>?', (now - 3600,)).fetchone()[0]
    if used >= allowance:
        c.rollback()
        return None
    rid = c.execute('INSERT INTO requests(at,kind,accounts) VALUES(?,?,?)', (now, kind, accounts)).lastrowid
    c.commit()
    return rid


def _call(c, transport, now, kind, addresses):
    """One charged batched getMultipleAccounts; returns (request_id, slot, values) or (request_id, None, code)."""
    rid = _charge(c, now, kind, len(addresses))
    if rid is None:
        return None, None, 'ALLOWANCE_EXHAUSTED'
    try:
        result = transport('getMultipleAccounts', [addresses, {'encoding': 'base64', 'commitment': 'confirmed'}])
        slot = result['context']['slot']
        values = result['value']
        if type(slot) is not int or type(values) is not list or len(values) != len(addresses):
            raise CounterfactualError('RESPONSE_INVALID')
    except TransportError as error:
        c.execute('INSERT INTO request_results VALUES(?,?,?,NULL)', (rid, 'FAILED', error.code))
        return rid, None, error.code
    except (CounterfactualError, KeyError, TypeError):
        c.execute('INSERT INTO request_results VALUES(?,?,?,NULL)', (rid, 'FAILED', 'RESPONSE_INVALID'))
        return rid, None, 'RESPONSE_INVALID'
    c.execute('INSERT INTO request_results VALUES(?,?,NULL,?)', (rid, 'OK', slot))
    return rid, slot, values


def _resolve_vaults(c, transport, now, summary):
    from desk.pools import parse_pool
    from desk.providers import SOL
    todo = c.execute('SELECT mint,pool FROM candidates WHERE mint NOT IN (SELECT mint FROM vaults) ORDER BY migrated_at').fetchall()
    for i in range(0, len(todo), 100):
        chunk = todo[i:i + 100]
        rid, slot, values = _call(c, transport, now, 'POOL', [p for _, p in chunk])
        if slot is None:
            summary['failed_requests'] += rid is not None
            if values == 'ALLOWANCE_EXHAUSTED':
                summary['allowance_exhausted'] = True
                return
            continue  # transient batch failure: candidates stay unresolved and retry next run
        summary['requests'] += 1
        for (mint, pool), account in zip(chunk, values):
            try:
                fields = parse_pool(account)
                if fields['base_mint'] != mint or fields['quote_mint'] != SOL:
                    raise CounterfactualError('POOL_IDENTITY_MISMATCH')
                row = (mint, fields['pool_base_token_account'], fields['pool_quote_token_account'], 'OK', None, now)
            except (ValueError, KeyError, TypeError, CounterfactualError, StopIteration):
                row = (mint, None, None, 'UNRESOLVED', 'POOL_ACCOUNT_INVALID', now)
            c.execute('INSERT INTO vaults VALUES(?,?,?,?,?,?)', row)


def due_tasks(c, now):
    grace = _policy(c)[1]
    rows = c.execute('''SELECT c.mint,c.pool,c.migrated_at,v.base_vault,v.quote_vault FROM candidates c
                        JOIN vaults v ON v.mint=c.mint WHERE v.status='OK' ORDER BY c.migrated_at''').fetchall()
    due, missed = [], []
    for mint, pool, migrated, base_vault, quote_vault in rows:
        done = {h for (h,) in c.execute('SELECT horizon FROM samples WHERE mint=?', (mint,))}
        for h in HORIZONS:
            if h in done or now < migrated + h:
                continue
            (missed if now > migrated + h + grace else due).append((mint, pool, h, migrated + h, base_vault, quote_vault))
    return due, missed


def sample(store, transport, *, now=None):
    now = time.time() if now is None else now
    summary = {'requests': 0, 'failed_requests': 0, 'samples': 0, 'missed': 0, 'allowance_exhausted': False}
    with closing(_connect(store)) as c:
        _resolve_vaults(c, transport, now, summary)
        due, missed = due_tasks(c, now)
        for mint, _, h, due_at, *_ in missed:  # late samples would be mislabeled; record the gap instead
            c.execute("INSERT OR IGNORE INTO samples(mint,horizon,due_at,status,code) VALUES(?,?,?,'MISSED','SCHEDULE_MISSED')", (mint, h, due_at))
            summary['missed'] += 1
        for i in range(0, len(due), BATCH_TASKS):
            if summary['allowance_exhausted']:
                break
            chunk = due[i:i + BATCH_TASKS]
            addresses = [a for t in chunk for a in (t[4], t[5])]
            rid, slot, values = _call(c, transport, now, 'SAMPLE', addresses)
            if slot is None:
                summary['failed_requests'] += rid is not None
                summary['allowance_exhausted'] |= values == 'ALLOWANCE_EXHAUSTED'
                continue
            summary['requests'] += 1
            c.execute('BEGIN IMMEDIATE')
            for n, (mint, pool, h, due_at, *_) in enumerate(chunk):
                base_account, quote_account = values[2 * n], values[2 * n + 1]
                try:
                    if base_account is None or quote_account is None:  # closed vault = dead pool, never a missing sample
                        row = ('POOL_DEAD', 'VAULT_CLOSED', None, None, None)
                    else:
                        from desk.providers import SOL
                        base_raw = token_account_amount(base_account, mint=mint, owner=pool)
                        quote_raw = token_account_amount(quote_account, mint=SOL, owner=pool)
                        price = price_from_reserves(base_raw, quote_raw)
                        row = ('OK', None, str(base_raw), str(quote_raw), None if price is None else format(price, 'f'))
                        if price is None:
                            row = ('POOL_DEAD', 'ZERO_RESERVE', str(base_raw), str(quote_raw), None)
                except (CounterfactualError, ValueError, TypeError):
                    row = ('FAILED', 'VAULT_ACCOUNT_INVALID', None, None, None)  # malformed provider data fails closed
                c.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                          (mint, h, due_at, now, slot, row[2], row[3], row[4], row[0], row[1], rid))
                summary['samples'] += 1
            c.commit()
    return summary


# ----------------------------------------------------------------- report
def _group(outcome):
    if outcome is None or outcome.get('dispatched') is None:
        return ['UNKNOWN_OUTCOME']
    if not outcome['dispatched']:
        return ['NOT_DISPATCHED']
    return list(outcome['codes']) or [f"ADMITTED:{outcome.get('status') or 'NO_RESULT'}"]


def _rank(values, q):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def report(store, *, horizon=3600):
    """Forward-return distribution per rejection group at one horizon (default +1h)."""
    groups = {}
    with closing(_connect(store, readonly=True)) as c:
        c.execute('BEGIN')
        for mint, in c.execute('SELECT mint FROM candidates').fetchall():
            samples = [dict(zip(('horizon', 'status', 'price', 'quote_raw'), r)) for r in c.execute(
                'SELECT horizon,status,price,quote_raw FROM samples WHERE mint=? ORDER BY horizon', (mint,))]
            last = c.execute('SELECT payload FROM outcomes WHERE mint=? ORDER BY id DESC LIMIT 1', (mint,)).fetchone()
            m = metrics(samples)
            for name in _group(json.loads(last[0]) if last else None):
                g = groups.setdefault(name, {'candidates': 0, 'with_return': 0, 'returns': [], 'died': 0, 'max_gains': []})
                g['candidates'] += 1
                g['died'] += m['pool_died']
                if horizon in m['returns']:
                    g['with_return'] += 1; g['returns'].append(m['returns'][horizon])
                if m['max_gain'] is not None:
                    g['max_gains'].append(m['max_gain'])
    result = {'label': LABEL, 'horizon_seconds': horizon,
              'baseline': 'first priced sample (+5m); the migration-time price is not observed', 'groups': {}}
    for name, g in sorted(groups.items()):
        r = g['returns']
        result['groups'][name] = {
            'candidates': g['candidates'], 'with_return': g['with_return'], 'pool_died': g['died'],
            'mean_return': str(sum(r) / len(r)) if r else None, 'median_return': str(statistics.median(r)) if r else None,
            'p10_return': str(_rank(r, .1)) if r else None, 'p90_return': str(_rank(r, .9)) if r else None,
            'share_positive': str(Decimal(sum(1 for x in r if x > 0)) / len(r)) if r else None,
            'median_max_gain': str(statistics.median(g['max_gains'])) if g['max_gains'] else None}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init', 'ingest', 'sample', 'report', 'set-allowance'):
        p = sub.add_parser(name)
        p.add_argument('--store', required=True)
        if name == 'init':
            p.add_argument('--allowance-per-hour', type=int, default=DEFAULT_ALLOWANCE)
            p.add_argument('--grace-seconds', type=int, default=DEFAULT_GRACE)
        if name == 'set-allowance':
            p.add_argument('--allowance-per-hour', type=int, required=True)
        if name == 'ingest':
            p.add_argument('--discovery-db', required=True)
            p.add_argument('--journal')
        if name == 'sample':
            p.add_argument('--systemd-credentials', action='store_true')
        if name == 'report':
            p.add_argument('--horizon', type=int, default=3600, choices=HORIZONS)
    args = parser.parse_args(argv)
    try:
        if args.command == 'init':
            init(args.store, allowance_per_hour=args.allowance_per_hour, grace_seconds=args.grace_seconds); out = {'status': 'INITIALIZED'}
        elif args.command == 'set-allowance':
            set_allowance(args.store, args.allowance_per_hour); out = {'status': 'RECORDED'}
        elif args.command == 'ingest':
            out = ingest(args.store, args.discovery_db, journal_db=args.journal)
        elif args.command == 'sample':
            from desk import provider_pacing
            if args.systemd_credentials:
                from desk.paper_cycle_cli import _credentials
                _credentials()
            out = sample(args.store, HeliusTransport(provider_pacing.configured(priority='investigation')))
        else:
            out = report(args.store, horizon=args.horizon)
    except (CounterfactualError, sqlite3.Error, OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': type(error).__name__, 'label': LABEL}))
        return 2
    print(json.dumps(out, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
