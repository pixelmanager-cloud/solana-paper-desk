"""Candidate feature store: as-of features for selection / entry / exit research (paper only, no provider access).

    python -m tools.research.features init   --store features.sqlite
    python -m tools.research.features ingest --store features.sqlite --ledger L [--discovery-db D] [--journal J]
        [--research-db R] [--decisions-db X] [--counterfactual-store C] [--since T] [--until T] [--now T]
    python -m tools.research.features export-jsonl     --store features.sqlite --out rows.jsonl
    python -m tools.research.features shadow-features  --store features.sqlite --out features.json [--grid-start T]
    python -m tools.research.features verify   --store features.sqlite

``features.sqlite`` is its own append-only store (UPDATE/DELETE are trigger-refused). It holds one row per
(mint, as_of, feature_set_version): the features that were KNOWN at ``as_of``, each with its provenance (source table, row
id or hash, and the time the evidence was observed). Everything is derived from evidence the desk already retains: the
ledger's recorded market events and their outcomes, the continuous-discovery frames, the dispatcher journal, the decision
journal and the counterfactual samples. No provider, RPC or network call is made, nothing is written to any input store
(each is opened ``mode=ro``; ``immutable=1`` only for a quiet WAL file), and nothing is signed or sent.

No look-ahead, by construction and re-verified by ``verify``:
  * a row's ``as_of`` is the time of the evidence row itself (event ``ts``, frame ``received_at``, result ``at``, sample
    ``sampled_at``), never the ingest time;
  * every feature carries the time it was measured (``at``) and ``at <= as_of`` must hold; a measurement stamped later than
    the event that carries it (for example ``holder_at > ts``) is dropped and counted, never kept;
  * evidence dated after ``--now`` is refused and counted.
Unknown stays unknown: a feature that the evidence does not carry is absent (no defaults, no neutral fill-in); corrupt
evidence is skipped and counted, never repaired.

``shadow-features`` writes the T27 ``--features`` input ({mint: {"as_of": epoch, <event feature fields>}}). Per mint it uses the
EARLIEST decision-time row (the first market event the ledger recorded) and only the field names the shadow engine accepts, with
the values exactly as recorded. Later rows are not merged in (that would be look-ahead for entries made before them), so a
variant sees real features from that first event onward and the neutral ones before it are refused by the shadow's own as_of rule.
All output is research on EXECUTION_UNVERIFIED paper activity.
"""
import sys
sys.dont_write_bytecode = True
import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import stat
import time

from desk.security import TOKEN_2022, TOKEN_PROGRAM
from tools.research import funnel_report as fr

LABEL = 'PAPER_ONLY_EXECUTION_UNVERIFIED_RESEARCH_FEATURES'
FEATURE_SET_VERSION = 'candidate-features-v1'
STORE_VERSION = 1
MAX_EVENTS, MAX_OUTCOMES, MAX_SAMPLES, MAX_DECISIONS = 500000, 1000000, 500000, 500000
MAX_FEATURE_BYTES = 16384

# event field -> (time key on the event, role). ``None`` time key = measured at the event's own ``ts``.
MARKET_GROUPS = {
    'price_at': ('market_cap_usd', 'reserve_sol', 'reserve_tokens', 'sol_usd', 'pool_fee_bps'),
    'holder_at': ('top10_pct', 'dev_pct', 'bundle_pct', 'cluster_pct', 'fresh_wallet_ratio'),
    'flow_at': ('flow', 'net_buy_ratio', 'unique_buyers_5m', 'volume_vs_liq', 'wash_score', 'manip_safety', 'manip_flow',
                'flow_confirmed'),
    'momentum_at': ('drawdown_from_high',),
    None: ('graduated', 'mint_revoked', 'freeze_revoked', 'lp_verified', 'extensions_safe', 'data_healthy', 'danger',
           'route_available', 'dev_launches_7d'),
}
DECIMAL_FIELDS = {'market_cap_usd', 'reserve_sol', 'reserve_tokens', 'sol_usd', 'pool_fee_bps', 'top10_pct', 'dev_pct',
                  'bundle_pct', 'cluster_pct', 'fresh_wallet_ratio', 'flow', 'net_buy_ratio', 'volume_vs_liq', 'wash_score',
                  'manip_safety', 'manip_flow', 'drawdown_from_high'}
BOOL_FIELDS = {'flow_confirmed', 'graduated', 'mint_revoked', 'freeze_revoked', 'lp_verified', 'extensions_safe',
               'data_healthy', 'danger', 'route_available'}
INT_FIELDS = {'unique_buyers_5m', 'dev_launches_7d'}


class FeatureError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


SCHEMA = '''
CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE feature_rows(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT NOT NULL,as_of REAL NOT NULL,
  feature_set_version TEXT NOT NULL,features TEXT NOT NULL,provenance TEXT NOT NULL,row_hash TEXT NOT NULL,
  UNIQUE(mint,as_of,feature_set_version));
CREATE TABLE feature_conflicts(id INTEGER PRIMARY KEY AUTOINCREMENT,mint TEXT NOT NULL,as_of REAL NOT NULL,
  feature_set_version TEXT NOT NULL,kept_hash TEXT NOT NULL,offered_hash TEXT NOT NULL,offered TEXT NOT NULL,run_id INTEGER NOT NULL);
CREATE TABLE ingest_runs(id INTEGER PRIMARY KEY AUTOINCREMENT,now REAL NOT NULL,summary TEXT NOT NULL);
CREATE INDEX feature_rows_mint_time ON feature_rows(mint,as_of);
'''
GUARDED = ('feature_rows', 'feature_conflicts', 'ingest_runs', 'meta')


# --------------------------------------------------------------------------- the store
def _canonical_store(path):
    p = Path(path)
    if os.path.islink(p):
        raise FeatureError('SYMLINKED_STORE')
    try:
        info = p.lstat()
    except OSError:
        raise FeatureError('STORE_MISSING') from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FeatureError('STORE_NOT_SINGLE_REGULAR_FILE')
    return p.resolve()


def init(store):
    path = Path(store)
    if path.exists() or os.path.islink(path):
        raise FeatureError('STORE_EXISTS')
    if os.path.islink(path.parent):
        raise FeatureError('SYMLINKED_PARENT')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path)) as c:
        c.executescript(SCHEMA)
        for table in GUARDED:
            for action in ('UPDATE', 'DELETE'):
                c.execute(f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                          f"BEGIN SELECT RAISE(ABORT,'Feature store records are append-only'); END")
        c.execute("INSERT INTO meta VALUES('store_version',?)", (str(STORE_VERSION),))
        c.execute("INSERT INTO meta VALUES('feature_set_version',?)", (FEATURE_SET_VERSION,))
        c.commit()
    return path


def _open_rw(store):
    c = sqlite3.connect(_canonical_store(store), timeout=20)
    row = c.execute("SELECT value FROM meta WHERE key='store_version'").fetchone()
    if not row or row[0] != str(STORE_VERSION):
        c.close()
        raise FeatureError('STORE_VERSION_UNSUPPORTED')
    return c


def row_digest(mint, as_of, version, features, provenance):
    body = json.dumps([mint, as_of, version, features, provenance], sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(body.encode()).hexdigest()


def validate_row(mint, as_of, features, provenance):
    """The no-look-ahead rule, checked on every insert and again by ``verify``. Returns an error code or None."""
    if not isinstance(mint, str) or not mint:
        return 'BAD_MINT'
    if type(as_of) not in (int, float) or not math.isfinite(as_of):
        return 'BAD_AS_OF'
    if not isinstance(features, dict) or not features or set(features) != set(provenance):
        return 'PROVENANCE_MISMATCH'
    for name, prov in provenance.items():
        at = prov.get('at') if isinstance(prov, dict) else None
        if type(at) not in (int, float) or not math.isfinite(at):
            return 'BAD_FEATURE_TIME'
        if at > as_of:
            return 'LOOK_AHEAD'
        if not prov.get('source') or not prov.get('ref'):
            return 'NO_PROVENANCE'
        if prov.get('role') not in ('input', 'decision'):
            return 'BAD_ROLE'
    if len(json.dumps(features)) > MAX_FEATURE_BYTES:
        return 'ROW_TOO_LARGE'
    return None


def append_rows(store, records, *, now):
    """Insert merged records. Identical re-runs are no-ops; a different row for an existing key is recorded as a conflict and
    never overwrites. Returns counts."""
    counts = {'inserted': 0, 'duplicate': 0, 'conflict': 0, 'invalid': 0}
    with closing(_open_rw(store)) as c:
        c.execute('BEGIN IMMEDIATE')
        run = c.execute('INSERT INTO ingest_runs(now,summary) VALUES(?,?)', (float(now), '{}')).lastrowid
        for (mint, as_of), rec in sorted(records.items()):
            features, provenance = rec['features'], rec['provenance']
            if validate_row(mint, as_of, features, provenance):
                counts['invalid'] += 1
                continue
            digest = row_digest(mint, as_of, FEATURE_SET_VERSION, features, provenance)
            existing = c.execute('SELECT row_hash FROM feature_rows WHERE mint=? AND as_of=? AND feature_set_version=?',
                                 (mint, as_of, FEATURE_SET_VERSION)).fetchone()
            if existing is None:
                c.execute('INSERT INTO feature_rows(mint,as_of,feature_set_version,features,provenance,row_hash) VALUES(?,?,?,?,?,?)',
                          (mint, as_of, FEATURE_SET_VERSION, json.dumps(features, sort_keys=True), json.dumps(provenance, sort_keys=True), digest))
                counts['inserted'] += 1
            elif existing[0] == digest:
                counts['duplicate'] += 1
            else:
                already = c.execute('SELECT 1 FROM feature_conflicts WHERE mint=? AND as_of=? AND feature_set_version=? AND offered_hash=?',
                                    (mint, as_of, FEATURE_SET_VERSION, digest)).fetchone()
                if already is None:
                    c.execute('INSERT INTO feature_conflicts(mint,as_of,feature_set_version,kept_hash,offered_hash,offered,run_id) VALUES(?,?,?,?,?,?,?)',
                              (mint, as_of, FEATURE_SET_VERSION, existing[0], digest,
                               json.dumps({'features': features, 'provenance': provenance}, sort_keys=True), run))
                counts['conflict'] += 1
        c.execute('INSERT INTO ingest_runs(now,summary) VALUES(?,?)', (float(now), json.dumps(counts, sort_keys=True)))
        c.execute('COMMIT')
    return counts


def read_rows(store, *, since=None, until=None):
    with closing(sqlite3.connect(_canonical_store(store).as_uri() + '?mode=ro', uri=True)) as c:
        c.execute('PRAGMA query_only=1')
        q, args = 'SELECT mint,as_of,feature_set_version,features,provenance,row_hash FROM feature_rows', []
        where = []
        if since is not None:
            where.append('as_of>=?'); args.append(since)
        if until is not None:
            where.append('as_of<?'); args.append(until)
        if where:
            q += ' WHERE ' + ' AND '.join(where)
        for mint, as_of, version, features, provenance, digest in c.execute(q + ' ORDER BY as_of,mint', args):
            yield {'mint': mint, 'as_of': as_of, 'feature_set_version': version, 'features': json.loads(features),
                   'provenance': json.loads(provenance), 'row_hash': digest}


def verify(store):
    """Re-prove every stored row: hash, provenance coverage and the no-look-ahead rule."""
    bad, n = [], 0
    for row in read_rows(store):
        n += 1
        code = validate_row(row['mint'], row['as_of'], row['features'], row['provenance'])
        if code is None and row_digest(row['mint'], row['as_of'], row['feature_set_version'], row['features'], row['provenance']) != row['row_hash']:
            code = 'ROW_HASH_MISMATCH'
        if code:
            bad.append({'mint': row['mint'], 'as_of': row['as_of'], 'code': code})
    return {'kind': 'features_verify_v1', 'rows': n, 'bad': bad[:50], 'bad_count': len(bad), 'ok': not bad, 'label': LABEL}


# --------------------------------------------------------------------------- record building
class Collector:
    """Merges per-source records into rows keyed by (mint, as_of) and keeps typed skip counters."""

    def __init__(self, now):
        self.now = now
        self.records = {}
        self.skipped = {}

    def skip(self, code):
        self.skipped[code] = self.skipped.get(code, 0) + 1

    def add(self, mint, as_of, name, value, *, source, ref, at=None, role='input', **extra):
        at = as_of if at is None else at
        if type(as_of) not in (int, float) or not math.isfinite(as_of) or type(at) not in (int, float) or not math.isfinite(at):
            self.skip('BAD_TIME')
            return False
        if as_of > self.now:
            self.skip('FUTURE_DATED_EVIDENCE')
            return False
        if at > as_of:
            self.skip('LOOK_AHEAD_DROPPED')
            return False
        rec = self.records.setdefault((mint, float(as_of)), {'features': {}, 'provenance': {}})
        if name in rec['features']:
            if rec['features'][name] != value:
                self.skip('SAME_TIME_SOURCE_DISAGREEMENT')   # first writer wins; the disagreement is counted, not hidden
            return False
        rec['features'][name] = value
        rec['provenance'][name] = dict({'source': source, 'ref': str(ref), 'at': float(at), 'role': role}, **extra)
        return True


def _num(value):
    return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)


def _dec(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        d = Decimal(value)
    except InvalidOperation:
        return None
    return d if d.is_finite() and (d == 0 or abs(d.adjusted()) <= 60) else None   # absurd magnitudes are corrupt evidence


def _fmt(d):
    text = format(d.normalize(), 'f')
    return text


def extract_ledger(col, ledger_db, *, since=None, until=None):
    """Market events as recorded (features at decision time) plus their outcomes (rejection reasons, entry)."""
    with closing(fr.connect_ro(ledger_db)) as c:
        c.execute('BEGIN')
        if not (fr._has_table(c, 'events') and fr._has_table(c, 'outcomes')):
            raise FeatureError('LEDGER_TABLES_MISSING')
        where, args = '', []
        if since is not None:
            where += ' AND ts>=?'; args.append(since)
        if until is not None:
            where += ' AND ts<?'; args.append(until)
        events = c.execute('SELECT event_id,ts,payload,payload_hash FROM events WHERE 1=1' + where + ' ORDER BY seq LIMIT ?',
                           (*args, MAX_EVENTS)).fetchall()
        outcomes = {}
        ids = {e[0] for e in events}
        for event_id, payload in c.execute('SELECT event_id,payload FROM outcomes ORDER BY seq LIMIT ?', (MAX_OUTCOMES,)):
            if event_id in ids:
                outcomes.setdefault(event_id, []).append(payload)
    for event_id, ts, payload, payload_hash in events:
        try:
            event = json.loads(payload)
        except ValueError:
            col.skip('EVENT_UNREADABLE')
            continue
        if not isinstance(event, dict) or event.get('kind') != 'market' or not isinstance(event.get('mint'), str) or not _num(ts):
            col.skip('NOT_A_MARKET_EVENT')
            continue
        _market_event(col, event, event_id, ts, payload_hash)
        _event_outcomes(col, event['mint'], event_id, ts, outcomes.get(event_id, ()))


def _market_event(col, event, event_id, ts, payload_hash):
    mint = event['mint']
    ref = f'{event_id}#{payload_hash}'
    src = 'ledger.events'
    for time_key, names in MARKET_GROUPS.items():
        at = ts
        if time_key is not None:
            at = event.get(time_key)
            if not _num(at):
                if any(event.get(n) is not None for n in names):
                    col.skip('MEASUREMENT_TIME_UNKNOWN')   # value present, time absent: cannot place it, so it is not used
                continue
            if at > ts:
                col.skip('LOOK_AHEAD_DROPPED')
                continue
        for name in names:
            if name not in event or event[name] is None:
                continue
            value = event[name]
            if name in DECIMAL_FIELDS and _dec(value) is None or name in BOOL_FIELDS and type(value) is not bool \
                    or name in INT_FIELDS and (type(value) is not int):
                col.skip('FEATURE_VALUE_INVALID')
                continue
            col.add(mint, ts, name, value, source=src, ref=ref, at=at)
    reserve_sol, sol_usd = _dec(event.get('reserve_sol')), _dec(event.get('sol_usd'))
    price_at = event.get('price_at')
    if reserve_sol is not None and sol_usd is not None and _num(price_at) and price_at <= ts:
        col.add(mint, ts, 'liquidity_usd', _fmt(2 * reserve_sol * sol_usd), source=src, ref=ref, at=price_at, derived='2*reserve_sol*sol_usd')
    graduated_at = event.get('graduated_at')
    if _num(graduated_at):
        if graduated_at <= ts:
            col.add(mint, ts, 'age_since_migration_seconds', ts - graduated_at, source=src, ref=ref, at=ts, derived='ts-graduated_at')
            col.add(mint, ts, 'graduated_at', graduated_at, source=src, ref=ref, at=graduated_at)
        else:
            col.skip('LOOK_AHEAD_DROPPED')
    token = event.get('token_evidence')
    if isinstance(token, dict) and isinstance(token.get('account'), dict):
        observed = token.get('observed_at')
        owner = token['account'].get('owner')
        if _num(observed) and observed <= ts and owner in (TOKEN_2022, TOKEN_PROGRAM):
            col.add(mint, ts, 'token_2022', owner == TOKEN_2022, source=src, ref=ref, at=observed)
        elif owner is not None:
            col.skip('TOKEN_PROFILE_UNUSABLE')
    bundle = event.get('bundle_evidence')
    if isinstance(bundle, dict):
        at = bundle.get('as_of')
        if _num(at) and at <= ts:
            if isinstance(bundle.get('holders'), list):
                col.add(mint, ts, 'holders_listed', len(bundle['holders']), source=src, ref=ref, at=at)
            if isinstance(bundle.get('early_buys'), list):
                col.add(mint, ts, 'early_buys_listed', len(bundle['early_buys']), source=src, ref=ref, at=at)
            if isinstance(bundle.get('holder_coverage_pct'), str) and _dec(bundle['holder_coverage_pct']) is not None:
                col.add(mint, ts, 'holder_coverage_pct', bundle['holder_coverage_pct'], source=src, ref=ref, at=at)
        else:
            col.skip('BUNDLE_EVIDENCE_TIME_UNUSABLE')


def _event_outcomes(col, mint, event_id, ts, payloads):
    reasons, entered = [], False
    for payload in payloads:
        try:
            out = json.loads(payload)
        except ValueError:
            col.skip('OUTCOME_UNREADABLE')
            continue
        if not isinstance(out, dict) or out.get('mint', mint) != mint:
            continue
        if out.get('type') == 'reject':
            for r in ([out.get('reason')] + list(out.get('reasons') or [])):
                if isinstance(r, str) and r and r not in reasons:
                    reasons.append(r[:96])
        elif out.get('type') == 'fill' and out.get('side') == 'buy':
            entered = True
    if reasons:
        col.add(mint, ts, 'reject_reasons', sorted(reasons), source='ledger.outcomes', ref=event_id, role='decision')
    if entered:
        col.add(mint, ts, 'entered', True, source='ledger.outcomes', ref=event_id, role='decision')


def extract_discovery(col, *, discovery_db=None, hints=None, since=0, until=None):
    if hints is None:
        hints, stats = fr.discovery_hints(discovery_db, since=since, until=until if until is not None else col.now + 1)
        for key in ('altered', 'undecodable', 'ambiguous'):
            for _ in range(stats[key]):
                col.skip('DISCOVERY_FRAME_' + key.upper())
    for h in hints:
        ref = f"discovery.raw_events#{h['seq']}"
        col.add(h['mint'], h['migrated_at'], 'migrated_at', h['migrated_at'], source='discovery.raw_events', ref=ref)
        col.add(h['mint'], h['migrated_at'], 'migration_slot', h['slot'], source='discovery.raw_events', ref=ref)


def extract_journal(col, journal_db):
    journal, _ = fr.read_journal(journal_db)
    for mint, row in journal.items():
        if row.get('unreadable') or row.get('intent_at') is None:
            col.skip('JOURNAL_ROW_UNREADABLE')
            continue
        col.add(mint, row['intent_at'], 'dispatched', True, source='dispatch.intents', ref=mint, role='decision')
        body, result_at = row.get('result'), row.get('result_at')
        if body is None or result_at is None:
            continue
        stage, death, extra, requests, used = fr.classify_result(body, mint)
        ref = f"dispatch.results#{row.get('scan_id') or mint}"
        col.add(mint, result_at, 'dispatch_stage_reached', stage, source='dispatch.results', ref=ref, role='decision')
        col.add(mint, result_at, 'dispatch_death_reason', death or 'BUY', source='dispatch.results', ref=ref, role='decision')
        if requests is not None:
            col.add(mint, result_at, 'requests_charged', requests, source='dispatch.results', ref=ref, role='decision')


def extract_counterfactual(col, store):
    with closing(fr.connect_ro(store)) as c:
        c.execute('BEGIN')
        if not (fr._has_table(c, 'samples') and fr._has_table(c, 'candidates')):
            raise FeatureError('COUNTERFACTUAL_TABLES_MISSING')
        for mint, horizon, sampled_at, base_raw, quote_raw, status in c.execute(
                "SELECT mint,horizon,sampled_at,base_raw,quote_raw,status FROM samples WHERE sampled_at IS NOT NULL "
                "AND status='OK' ORDER BY mint,horizon LIMIT ?", (MAX_SAMPLES,)):
            if not _num(sampled_at):
                col.skip('SAMPLE_TIME_UNREADABLE')
                continue
            try:
                base, quote = int(base_raw), int(quote_raw)
            except (TypeError, ValueError):
                col.skip('SAMPLE_VALUE_UNREADABLE')
                continue
            ref = f'counterfactual.samples#{mint}/{horizon}'
            col.add(mint, sampled_at, 'cf_base_vault_raw', str(base), source='counterfactual.samples', ref=ref)
            col.add(mint, sampled_at, 'cf_quote_vault_raw', str(quote), source='counterfactual.samples', ref=ref)
            col.add(mint, sampled_at, 'cf_sample_horizon_seconds', horizon, source='counterfactual.samples', ref=ref)


def extract_decisions(col, decisions_db, research_db=None):
    """Decision journal rows that carry their own time. A decision without a time is skipped: it could not be placed."""
    scan_mint = {}
    if research_db:
        with closing(fr.connect_ro(research_db)) as r:
            r.execute('BEGIN')
            if fr._has_table(r, 'scans'):
                scan_mint = {i: m for i, m in r.execute('SELECT id,mint FROM scans LIMIT 500000')}
    with closing(fr.connect_ro(decisions_db)) as x:
        x.execute('BEGIN')
        if not fr._has_table(x, 'decisions'):
            raise FeatureError('DECISIONS_TABLE_MISSING')
        for scan_id, text, source in x.execute('SELECT scan_id,decision,source_payload FROM decisions LIMIT ?', (MAX_DECISIONS,)):
            try:
                decision = json.loads(text)
                payload = json.loads(source)
            except ValueError:
                col.skip('DECISION_UNREADABLE')
                continue
            if not isinstance(decision, dict):
                col.skip('DECISION_UNLABELLED')
                continue
            at = next((decision[k] for k in ('ts', 'at', 'decided_at') if _num(decision.get(k))), None)
            mint = (payload.get('mint') if isinstance(payload, dict) else None) or scan_mint.get(scan_id)
            if at is None or not isinstance(mint, str):
                col.skip('DECISION_NOT_PLACEABLE')
                continue
            label = next((str(decision[k]) for k in ('action', 'decision', 'verdict') if k in decision), None)
            if label:
                col.add(mint, at, 'decision_label', label[:64], source='decisions.decisions', ref=scan_id, role='decision')


def ingest(store, *, ledger_db=None, discovery_db=None, hints=None, journal_db=None, research_db=None, decisions_db=None,
           counterfactual_store=None, since=None, until=None, now=None):
    now = time.time() if now is None else float(now)
    col = Collector(now)
    if ledger_db:
        extract_ledger(col, ledger_db, since=since, until=until)
    if discovery_db or hints is not None:
        extract_discovery(col, discovery_db=discovery_db, hints=hints, since=since or 0, until=until)
    if journal_db:
        extract_journal(col, journal_db)
    if counterfactual_store:
        extract_counterfactual(col, counterfactual_store)
    if decisions_db:
        extract_decisions(col, decisions_db, research_db)
    counts = append_rows(store, col.records, now=now)
    return {'kind': 'features_ingest_v1', 'label': LABEL, 'paper_only': True, 'feature_set_version': FEATURE_SET_VERSION,
            'now': now, 'candidate_rows': len(col.records), **counts, 'skipped': dict(sorted(col.skipped.items()))}


# --------------------------------------------------------------------------- exports
def export_jsonl(store, out, *, since=None, until=None):
    n = 0
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        for row in read_rows(store, since=since, until=until):
            stream.write(json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n')
            n += 1
    return n


def shadow_features(store, *, until=None):
    """T27 ``--features`` object: earliest decision-time row per mint, restricted to the fields the shadow engine accepts."""
    from tools.research.shadow_strategies import NEUTRAL_FEATURES
    out = {}
    for row in read_rows(store, until=until):
        mint = row['mint']
        if mint in out:
            continue                      # rows are ordered by as_of: the first one with usable fields wins, later ones are look-ahead
        fields = {k: v for k, v in row['features'].items()
                  if k in NEUTRAL_FEATURES and row['provenance'][k]['role'] == 'input' and row['provenance'][k]['source'] == 'ledger.events'}
        if fields:
            out[mint] = dict({'as_of': row['as_of']}, **fields)
    return out


def write_json_exclusive(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=1, allow_nan=False) + '\n')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    sub = p.add_subparsers(dest='command', required=True)
    for name in ('init', 'verify', 'export-jsonl', 'shadow-features', 'ingest'):
        s = sub.add_parser(name)
        s.add_argument('--store', required=True)
        if name in ('export-jsonl', 'shadow-features'):
            s.add_argument('--out', required=True, help='new file (must not exist)')
            s.add_argument('--until', type=float, help='only rows known before this UTC epoch')
        if name == 'export-jsonl':
            s.add_argument('--since', type=float)
        if name == 'ingest':
            for flag in ('ledger', 'discovery-db', 'journal', 'research-db', 'decisions-db', 'counterfactual-store'):
                s.add_argument('--' + flag)
            s.add_argument('--since', type=float)
            s.add_argument('--until', type=float)
            s.add_argument('--now', type=float, help='evidence dated after this is refused (default: the wall clock)')
    args = p.parse_args(argv)
    try:
        if args.command == 'init':
            init(args.store)
            report = {'kind': 'features_init_v1', 'store': str(args.store), 'label': LABEL}
        elif args.command == 'verify':
            report = verify(args.store)
        elif args.command == 'ingest':
            report = ingest(args.store, ledger_db=args.ledger, discovery_db=args.discovery_db, journal_db=args.journal,
                            research_db=args.research_db, decisions_db=args.decisions_db,
                            counterfactual_store=args.counterfactual_store, since=args.since, until=args.until, now=args.now)
        elif args.command == 'export-jsonl':
            report = {'kind': 'features_export_v1', 'rows': export_jsonl(args.store, args.out, since=args.since, until=args.until), 'label': LABEL}
        else:
            features = shadow_features(args.store, until=args.until)
            write_json_exclusive(args.out, features)
            report = {'kind': 'features_shadow_export_v1', 'mints': len(features), 'label': LABEL}
    except FeatureError as error:
        print(json.dumps({'status': 'UNAVAILABLE', 'code': error.code, 'label': LABEL}, sort_keys=True))
        return 2
    except fr.FunnelError as error:
        print(json.dumps({'status': 'UNAVAILABLE', 'code': error.code, 'label': LABEL}, sort_keys=True))
        return 2
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'UNAVAILABLE', 'code': 'STORE_UNREADABLE_OR_INVALID', 'label': LABEL}, sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, default=str))
    return 0 if report.get('ok', True) else 2


if __name__ == '__main__':
    sys.exit(main())
