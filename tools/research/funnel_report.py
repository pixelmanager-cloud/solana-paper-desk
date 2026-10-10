"""Funnel report: where do migration candidates die? (read-only research tool, paper only).

``python -m tools.research.funnel_report --discovery-db D --journal J --research-db R --ledger L
[--evidence-db E] [--decisions-db X] [--counterfactual-store C] [--out-html report.html]``

Cohorts are the migrations found in the continuous-discovery frames, bucketed by the hour (and day)
they were received. Per cohort and stage the report counts how many candidates reached the stage, why
the others died before it (typed codes from the dispatcher journal / cycle results), how many are
still pending, the request budget used by the stage where each candidate ended, and the median time
between stages.

"Ending stage" (request budget table) is the first stage a candidate did NOT reach, or ``buy`` when it
completed. Candidates still waiting are ``pending`` (not deaths); a dispatch with no admission or no
result is ``UNRESOLVED:*`` (an integrity-relevant latch, not a normal rejection).

Stages: discovered -> selectable (age window opened, not already scanned) -> dispatched (journal
intent) -> admitted (research scan) -> history_ok (token policy, migration intake and history
preparation passed) -> observations_ok (cycle completed its reads) -> engine_eligible (no engine
rejection) -> buy (ledger BUY fill).

Safety: every database is opened read-only (``mode=ro``; ``immutable=1`` only for a WAL-mode file with
no ``-wal``, never for a rollback-journal store), symlinked / multi-link stores are refused, nothing is
written except an optional HTML file created exclusively, and there is no network or provider access.
Discovery frames whose stored hash does not match are counted and never used. All figures are
research descriptions of EXECUTION_UNVERIFIED paper activity, not trading evidence.
"""
import sys
sys.dont_write_bytecode = True
import argparse
from contextlib import closing
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import sqlite3
import statistics
import stat
import time

LABEL = 'PAPER_ONLY_EXECUTION_UNVERIFIED_RESEARCH_REPORT'
STAGES = ('discovered', 'selectable', 'dispatched', 'admitted', 'history_ok', 'observations_ok',
          'engine_eligible', 'buy')
TRANSITIONS = ('discovery_to_dispatch', 'dispatch_to_admission', 'admission_to_result', 'result_to_buy')
WINDOW_MIN, WINDOW_MAX = 300, 7200          # same age window the dispatcher selects from
MAX_FRAMES, MAX_SCANS, MAX_LEDGER_FILLS = 50000, 500000, 200000
MAX_REASONS = 200
HOUR, DAY = 3600, 86400


class FunnelError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------- read-only access
def _canonical(path):
    p = Path(path)
    if os.path.islink(p):
        raise FunnelError('SYMLINKED_STORE')
    try:
        info = p.lstat()
    except OSError:
        raise FunnelError('STORE_MISSING') from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FunnelError('STORE_NOT_SINGLE_REGULAR_FILE')
    return p.resolve()


def _is_wal_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        head = os.read(fd, 100)
    finally:
        os.close(fd)
    return len(head) >= 20 and head[:16] == b'SQLite format 3\x00' and head[18] == 2


def open_mode(path):
    """``immutable`` only for a WAL file with no -wal (reading it plainly would create root-owned
    -wal/-shm beside a live store); a rollback-journal store can change under the read: always ``ro``."""
    p = Path(path)
    return 'immutable' if _is_wal_file(p) and not Path(str(p) + '-wal').exists() else 'ro'


def connect_ro(path):
    p = _canonical(path)
    uri = p.as_uri() + ('?immutable=1' if open_mode(p) == 'immutable' else '?mode=ro')
    c = sqlite3.connect(uri, uri=True, timeout=2)
    c.execute('PRAGMA query_only=1')
    return c


def _has_table(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


# --------------------------------------------------------------------------- inputs
def discovery_hints(discovery_db, *, since, until, limit=MAX_FRAMES):
    """Migration hints from the discovery frames (same decode/selection as the dispatcher).

    Returns (hints, stats). Frames whose stored hash differs from their payload are counted as
    ``altered`` and never used; ambiguous or non-migration frames are counted, not guessed.
    """
    from desk.decode import decode
    from desk.model import digest
    from tools import paper_entry_dispatcher as dispatcher
    stats = {'frames': 0, 'altered': 0, 'undecodable': 0, 'not_migration': 0, 'ambiguous': 0, 'truncated': False}
    hints = []
    with closing(connect_ro(discovery_db)) as d:
        d.execute('BEGIN')
        rows = d.execute('SELECT seq,source_id,received_at,slot,payload_hash FROM raw_events '
                         'WHERE received_at>=? AND received_at<? ORDER BY seq LIMIT ?',
                         (since, until, limit + 1)).fetchall()
        if len(rows) > limit:
            stats['truncated'], rows = True, rows[:limit]
        for seq, source, received, slot, stored_hash in rows:
            stats['frames'] += 1
            try:
                payload = d.execute('SELECT payload FROM raw_events WHERE seq=?', (seq,)).fetchone()[0]
                raw = json.loads(payload, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite')))
                if (digest(raw) != stored_hash or type(received) not in (int, float) or not math.isfinite(received)):
                    stats['altered'] += 1
                    continue
                decoded = decode(raw)
                if decoded['status'] != 'OBSERVED':
                    stats['not_migration'] += 1
                    continue
                found = [o for o in decoded['program_observations']
                         if o.get('name') in ('migrate', 'migrate_v2') and o.get('status') == 'IDENTIFIED'
                         and dispatcher._migration_event_hint(raw, decoded, o)]
            except (ValueError, KeyError, TypeError, IndexError, AttributeError, OverflowError):
                stats['undecodable'] += 1
                continue
            if not found:
                stats['not_migration'] += 1
            elif len(found) > 1:
                stats['ambiguous'] += 1
            else:
                hints.append({'seq': seq, 'mint': found[0]['mint'], 'pool': found[0]['pool'],
                              'signature': decoded['signature'], 'slot': slot, 'migrated_at': float(received)})
    return hints, stats


def read_journal(journal_db):
    """mint -> {'intent_at', 'result': dict|None, 'result_at'}; unreadable rows are marked, not guessed."""
    out, bad = {}, 0
    with closing(connect_ro(journal_db)) as j:
        j.execute('BEGIN')
        if not (_has_table(j, 'intents') and _has_table(j, 'results')):
            raise FunnelError('JOURNAL_TABLES_MISSING')
        results = {}
        for ident, payload in j.execute('SELECT id,payload FROM results'):
            try:
                results[ident] = json.loads(payload)
            except ValueError:
                results[ident] = None
        for ident, mint, payload in j.execute('SELECT id,mint,payload FROM intents'):
            try:
                intent = json.loads(payload)
                at = float(intent['at'])
            except (ValueError, KeyError, TypeError):
                bad += 1
                out[mint] = {'intent_at': None, 'result': None, 'result_at': None, 'unreadable': True}
                continue
            res = results.get(ident)
            body = res.get('result') if isinstance(res, dict) else None
            out[mint] = {'intent_at': at, 'result': body if isinstance(body, dict) else None,
                         'result_at': float(res['at']) if isinstance(res, dict) and isinstance(res.get('at'), (int, float)) else None,
                         'unreadable': ident in results and res is None, 'has_result_row': ident in results}
    return out, bad


def read_scans(research_db):
    with closing(connect_ro(research_db)) as r:
        r.execute('BEGIN')
        if not _has_table(r, 'scans'):
            raise FunnelError('SCANS_TABLE_MISSING')
        rows = r.execute('SELECT mint,created,status FROM scans ORDER BY created LIMIT ?', (MAX_SCANS,)).fetchall()
    scans = {}
    for mint, created, status in rows:
        scans.setdefault(mint, {'created': float(created) if isinstance(created, (int, float)) else None, 'status': status})
    return scans


def read_ledger(ledger_db):
    """mint -> first BUY fill timestamp (authoritative BUY), plus total fill counts."""
    buys, sells = {}, 0
    with closing(connect_ro(ledger_db)) as c:
        c.execute('BEGIN')
        if not (_has_table(c, 'events') and _has_table(c, 'outcomes')):
            raise FunnelError('LEDGER_TABLES_MISSING')
        for ts, payload in c.execute("SELECT e.ts,o.payload FROM outcomes o JOIN events e ON e.event_id=o.event_id "
                                     "WHERE json_extract(o.payload,'$.type')='fill' ORDER BY o.seq LIMIT ?",
                                     (MAX_LEDGER_FILLS,)):
            try:
                fill = json.loads(payload)
            except ValueError:
                continue
            if fill.get('side') == 'buy':
                buys.setdefault(fill.get('mint'), ts)
            elif fill.get('side') == 'sell':
                sells += 1
    return buys, sells


def read_budgets(evidence_db):
    with closing(connect_ro(evidence_db)) as e:
        e.execute('BEGIN')
        if not _has_table(e, 'ownership_budgets'):
            return {'status': 'NO_OWNERSHIP_BUDGET_TABLE'}
        n, used, ceiling, exhausted = e.execute(
            'SELECT COUNT(*),COALESCE(SUM(used),0),COALESCE(SUM(ceiling),0),COALESCE(SUM(used>=ceiling),0) '
            'FROM ownership_budgets').fetchone()
        passes = None
        if _has_table(e, 'paper_observation_passes'):
            total, null = e.execute('SELECT COUNT(*),COALESCE(SUM(outcome_hash IS NULL),0) FROM paper_observation_passes').fetchone()
            passes = {'total': total, 'unresolved_null': null}
    return {'status': 'READ', 'ownership_budgets': n, 'requests_used': used, 'request_ceiling_sum': ceiling,
            'budgets_exhausted': exhausted, 'observation_passes': passes}


def read_decisions(decisions_db):
    with closing(connect_ro(decisions_db)) as x:
        x.execute('BEGIN')
        if not _has_table(x, 'decisions'):
            return {'status': 'NO_DECISIONS_TABLE'}
        counts = {}
        for (text,) in x.execute('SELECT decision FROM decisions LIMIT 200000'):
            try:
                value = json.loads(text)
            except ValueError:
                value = text
            label = (next((str(value[k]) for k in ('action', 'decision', 'verdict') if isinstance(value, dict) and k in value), None)
                     if isinstance(value, dict) else str(value))
            label = (label or 'UNLABELLED')[:64]
            counts[label] = counts.get(label, 0) + 1
    return {'status': 'READ', 'decisions': sum(counts.values()), 'by_label': dict(sorted(counts.items())[:MAX_REASONS])}


# --------------------------------------------------------------------------- classification
def _reason(value, default):
    if isinstance(value, str) and value:
        return value[:96]
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], str):
        return value[0][:96]
    if isinstance(value, dict):
        for key in ('code', 'reason', 'status'):
            if isinstance(value.get(key), str):
                return value[key][:96]
    return default


def classify_result(body):
    """Typed dispatcher result -> (furthest stage passed, death reason or None, extra reasons, requests, used)."""
    if not isinstance(body, dict):
        return 'admitted', 'UNRESOLVED:RESULT_UNREADABLE', [], None, None
    kind = body.get('kind')
    requests = body.get('attempted_requests') if isinstance(body.get('attempted_requests'), int) else (
        body.get('requests_after') if isinstance(body.get('requests_after'), int) else None)
    used = None
    budget = body.get('budget')
    if isinstance(budget, dict):
        used = sum(v.get('used', 0) for v in budget.values() if isinstance(v, dict) and isinstance(v.get('used'), int)) or None
    if kind == 'dispatcher_token_rejection_v1':
        policy = body.get('token_policy') if isinstance(body.get('token_policy'), dict) else {}
        reasons = [r for r in policy.get('reasons', []) if isinstance(r, str)] or ['TOKEN_POLICY_SKIP']
        return 'admitted', 'TOKEN:' + reasons[0], ['TOKEN:' + r for r in reasons[1:]], requests, used
    if kind == 'dispatcher_migration_no_entry_v1':
        return 'admitted', 'MIGRATION:' + _reason(body.get('reason'), 'NO_ENTRY'), [], requests, used
    if kind == 'history_preparation_no_entry_v1':
        return 'admitted', 'HISTORY:' + _reason(body.get('reason'), 'NO_ENTRY'), [], requests, used
    if kind == 'paper_cycle_v1':
        status = body.get('status')
        outcomes = [o for o in body.get('outcomes', []) if isinstance(o, dict)]
        if any(o.get('type') == 'fill' and o.get('side') == 'buy' for o in outcomes):
            return 'buy', None, [], requests, used
        if status == 'COMPLETE':
            rejects = [o for o in outcomes if o.get('type') == 'reject']
            if rejects:
                first = rejects[0]
                primary = _reason(first.get('reason'), 'REJECTED')
                extra = [r for r in first.get('reasons', []) if isinstance(r, str) and r != primary]
                return 'observations_ok', 'ENGINE:' + primary, ['ENGINE:' + r for r in extra], requests, used
            return 'observations_ok', 'ENGINE:NO_FILL_NO_REJECT', [], requests, used
        blockers = [b for b in body.get('blockers', []) if isinstance(b, str)]
        prefix = 'UNRESOLVED:' if status == 'RECOVERY_REQUIRED' else 'OBSERVATIONS:'
        return 'history_ok', prefix + (blockers[0] if blockers else str(status or 'BLOCKED'))[:96], \
            [prefix + b for b in blockers[1:]], requests, used
    return 'admitted', 'UNRESOLVED:UNKNOWN_RESULT_KIND', [], requests, used


def classify(hint, journal, scans, ledger_buys, now, window=(WINDOW_MIN, WINDOW_MAX)):
    """One candidate -> reached stage names, death/pending info and stage timestamps."""
    wmin, wmax = window
    mint, received = hint['mint'], hint['migrated_at']
    entry = journal.get(mint)
    scan = scans.get(mint)
    out = {'mint': mint, 'received': received, 'reached': ['discovered'], 'death': None, 'pending': None,
           'extra': [], 'requests': None, 'used': None, 'times': {}, 'flags': []}

    def die(before, reason):
        out['death'] = (before, reason)

    if entry is None:
        if scan is not None:
            die('selectable', 'ALREADY_SCANNED_NO_INTENT')
        elif now < received + wmin:
            out['pending'] = ('selectable', 'WINDOW_NOT_OPEN')
        else:
            out['reached'].append('selectable')
            if now > received + wmax:
                die('dispatched', 'EXPIRED_NOT_DISPATCHED')
            else:
                out['pending'] = ('dispatched', 'AWAITING_DISPATCH')
        buy_ts = ledger_buys.get(mint)
        if buy_ts is not None:
            out['flags'].append('LEDGER_BUY_WITHOUT_INTENT')
        return out
    out['reached'] += ['selectable', 'dispatched']
    if entry.get('intent_at') is not None:
        out['times']['discovery_to_dispatch'] = entry['intent_at'] - received
    if scan is None:
        die('admitted', 'UNRESOLVED:DISPATCHED_NOT_ADMITTED')
        return out
    out['reached'].append('admitted')
    if entry.get('intent_at') is not None and scan.get('created') is not None:
        out['times']['dispatch_to_admission'] = scan['created'] - entry['intent_at']
    body = entry.get('result')
    buy_ts = ledger_buys.get(mint)
    if body is None:
        if buy_ts is not None:      # the ledger fill is the accounting truth even if the journal row is missing/unreadable
            out['reached'] = list(STAGES)
            out['flags'].append('LEDGER_BUY_NOT_IN_RESULT')
        else:
            die('history_ok', 'UNRESOLVED:NO_RESULT' if not entry.get('unreadable') else 'UNRESOLVED:RESULT_UNREADABLE')
        return out
    furthest, reason, extra, requests, used = classify_result(body)
    out['extra'], out['requests'], out['used'] = extra, requests, used
    if entry.get('result_at') is not None and scan.get('created') is not None:
        out['times']['admission_to_result'] = entry['result_at'] - scan['created']
    order = list(STAGES)
    for stage in order[order.index('admitted') + 1: order.index(furthest) + 1]:
        out['reached'].append(stage)
    # The first stage NOT reached is the one after the furthest stage the typed result proves.
    nxt = {'admitted': 'history_ok', 'history_ok': 'observations_ok', 'observations_ok': 'engine_eligible'}.get(furthest)
    if reason is not None:
        die(nxt, reason)
    if furthest == 'buy' or buy_ts is not None:
        # The ledger fill is the accounting truth: a ledger BUY promotes the candidate; a result BUY the
        # ledger does not contain (for example an archived ledger) stays counted but is flagged.
        if furthest != 'buy':
            out['flags'].append('LEDGER_BUY_NOT_IN_RESULT')
            out['reached'] = [s for s in STAGES]
            out['death'] = None
        elif buy_ts is None:
            out['flags'].append('RESULT_BUY_NOT_IN_LEDGER')
        if buy_ts is not None and entry.get('result_at') is not None:
            out['times']['result_to_buy'] = buy_ts - entry['result_at']
    return out


# --------------------------------------------------------------------------- aggregation
def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _new_bucket():
    return {'candidates': 0, 'reached': {s: 0 for s in STAGES}, 'died_before': {}, 'pending': {}, 'extra_reasons': {},
            'requests': {}, 'times': {t: [] for t in TRANSITIONS}, 'flags': {}}


def _add(bucket, c):
    bucket['candidates'] += 1
    for s in c['reached']:
        bucket['reached'][s] += 1
    if c['death']:
        stage, reason = c['death']
        bucket['died_before'].setdefault(stage, {})
        bucket['died_before'][stage][reason] = bucket['died_before'][stage].get(reason, 0) + 1
        end = stage
    elif c['pending']:
        stage, reason = c['pending']
        bucket['pending'].setdefault(stage, {})
        bucket['pending'][stage][reason] = bucket['pending'][stage].get(reason, 0) + 1
        end = None
    else:
        end = 'buy'
    for r in c['extra']:
        bucket['extra_reasons'][r] = bucket['extra_reasons'].get(r, 0) + 1
    for f in c['flags']:
        bucket['flags'][f] = bucket['flags'].get(f, 0) + 1
    if end and (c['requests'] is not None or c['used'] is not None):
        slot = bucket['requests'].setdefault(end, {'n': 0, 'attempted': [], 'investigation_used': []})
        slot['n'] += 1
        if c['requests'] is not None:
            slot['attempted'].append(c['requests'])
        if c['used'] is not None:
            slot['investigation_used'].append(c['used'])
    for t, v in c['times'].items():
        bucket['times'][t].append(v)


def _median(values):
    return float(statistics.median(values)) if values else None


def _finish(bucket):
    out = {k: v for k, v in bucket.items() if k not in ('times', 'requests')}
    out['requests_by_ending_stage'] = {
        stage: {'candidates_with_data': v['n'], 'attempted_total': sum(v['attempted']),
                'attempted_median': _median(v['attempted']), 'investigation_used_total': sum(v['investigation_used']),
                'investigation_used_median': _median(v['investigation_used'])}
        for stage, v in sorted(bucket['requests'].items())}
    out['median_seconds'] = {t: _median(v) for t, v in bucket['times'].items()}
    out['samples_seconds'] = {t: len(v) for t, v in bucket['times'].items()}
    return out


def forward_returns(store, assignments, horizon):
    """Optional T26 counterfactual store: forward return per death/reason group (read-only).

    Same definition as the counterfactual tool: baseline = first priced sample (status OK), return at
    ``horizon`` seconds; pools that died count as -100%.
    """
    from decimal import Decimal
    groups = {}
    with closing(connect_ro(store)) as c:
        c.execute('BEGIN')
        if not _has_table(c, 'samples'):
            return {'status': 'NO_SAMPLES_TABLE'}
        for mint, group in assignments.items():
            rows = c.execute('SELECT horizon,status,price FROM samples WHERE mint=? ORDER BY horizon', (mint,)).fetchall()
            priced = [(h, Decimal(p)) for h, s, p in rows if s == 'OK' and p is not None]
            died = any(s == 'POOL_DEAD' for _h, s, _p in rows)
            g = groups.setdefault(group, {'candidates': 0, 'with_return': 0, 'pool_died': 0, 'returns': []})
            g['candidates'] += 1
            g['pool_died'] += died
            if priced:
                p0 = priced[0][1]
                match = [p for h, p in priced if h == horizon]
                if match and p0 != 0:
                    g['with_return'] += 1
                    g['returns'].append(match[0] / p0 - 1)
    result = {}
    for group, g in sorted(groups.items()):
        r = sorted(g['returns'])
        result[group] = {'candidates': g['candidates'], 'with_return': g['with_return'], 'pool_died': g['pool_died'],
                         'median_return': str(statistics.median(r)) if r else None,
                         'share_positive': str(Decimal(sum(1 for x in r if x > 0)) / len(r)) if r else None}
    return {'status': 'READ', 'horizon_seconds': horizon, 'baseline': 'first priced sample', 'groups': result}


def build(*, discovery_db=None, journal, research_db, ledger, evidence_db=None, decisions_db=None,
          counterfactual_store=None, hints=None, since=0, until=None, now=None, horizon=3600, window=(WINDOW_MIN, WINDOW_MAX)):
    now = time.time() if now is None else float(now)
    until = now + 1 if until is None else until
    frame_stats = None
    if hints is None:
        if discovery_db is None:
            raise FunnelError('DISCOVERY_REQUIRED')
        hints, frame_stats = discovery_hints(discovery_db, since=since, until=until)
    journal_map, journal_unreadable = read_journal(journal)
    scans = read_scans(research_db)
    ledger_buys, ledger_sells = read_ledger(ledger)
    seen, candidates, duplicates = set(), [], 0
    for h in sorted(hints, key=lambda x: x['migrated_at']):
        if h['mint'] in seen:
            duplicates += 1
            continue
        seen.add(h['mint'])
        candidates.append(classify(h, journal_map, scans, ledger_buys, now, window))
    total, hours, days = _new_bucket(), {}, {}
    assignments = {}
    for c in candidates:
        _add(total, c)
        _add(hours.setdefault(int(c['received'] // HOUR) * HOUR, _new_bucket()), c)
        _add(days.setdefault(int(c['received'] // DAY) * DAY, _new_bucket()), c)
        group = ('DIED:' + c['death'][1]) if c['death'] else ('PENDING:' + c['pending'][1] if c['pending'] else 'BUY')
        assignments[c['mint']] = group
    report = {'kind': 'funnel_report_v1', 'label': LABEL, 'paper_only': True, 'live_readiness': False,
              'generated_at': _iso(now), 'now': now, 'window_seconds': list(window), 'stages': list(STAGES),
              'candidates': len(candidates), 'duplicate_hints_ignored': duplicates,
              'frames': frame_stats, 'journal_unreadable_intents': journal_unreadable,
              'ledger': {'buy_fills_distinct_mints': len(ledger_buys), 'sell_fills': ledger_sells},
              'total': _finish(total),
              'by_hour': {_iso(k): _finish(v) for k, v in sorted(hours.items())},
              'by_day': {_iso(k)[:10]: _finish(v) for k, v in sorted(days.items())},
              'budgets': read_budgets(evidence_db) if evidence_db else {'status': 'NOT_PROVIDED'},
              'decisions': read_decisions(decisions_db) if decisions_db else {'status': 'NOT_PROVIDED'},
              'counterfactual': forward_returns(counterfactual_store, assignments, horizon) if counterfactual_store
              else {'status': 'NOT_PROVIDED'}}
    return report


# --------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#fbfaf7;--fg:#1f1d1a;--muted:#6b665e;--card:#fff;--line:#e4dfd5;--bar:#c8602b;--bar2:#2f6f62;--warn:#a23b2a}
@media (prefers-color-scheme:dark){:root{--bg:#171614;--fg:#ece8df;--muted:#9a948a;--card:#1f1d1a;--line:#34312b;--bar:#e08a55;--bar2:#5fb39f;--warn:#e27d6a}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:980px;margin:0 auto}h1{font-size:1.4rem;margin:.2rem 0}h2{font-size:1.1rem;margin:1.6rem 0 .4rem}
.note{color:var(--muted);font-size:.85rem}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:.88rem}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.barcell{width:38%;min-width:120px}.bar{height:10px;background:var(--bar);border-radius:3px;min-width:2px}.warn{color:var(--warn)}
"""


def _e(value):
    return html.escape(str(value), quote=True)


def _funnel_table(section):
    top = max(section['reached']['discovered'], 1)
    rows = []
    previous = None
    for stage in STAGES:
        n = section['reached'][stage]
        pct = 100.0 * n / top
        step = '' if previous in (None, 0) else f'{100.0 * n / previous:.0f}%'
        rows.append(f'<tr><td>{_e(stage)}</td><td class="n">{n}</td><td class="n">{pct:.0f}%</td><td class="n">{step}</td>'
                    f'<td class="barcell"><div class="bar" style="width:{pct:.1f}%"></div></td></tr>')
        previous = n
    return ('<table><thead><tr><th>stage</th><th class="n">reached</th><th class="n">of discovered</th>'
            '<th class="n">of previous</th><th></th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table>')


def _reason_table(mapping, title):
    rows = [f'<tr><td>{_e(stage)}</td><td>{_e(reason)}</td><td class="n">{n}</td></tr>'
            for stage in STAGES if stage in mapping
            for reason, n in sorted(mapping[stage].items(), key=lambda kv: (-kv[1], kv[0]))]
    if not rows:
        return f'<p class="note">{_e(title)}: none</p>'
    return (f'<table><thead><tr><th>{_e(title)} (before stage)</th><th>code</th><th class="n">count</th></tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table>')


def render_html(report):
    t = report['total']
    parts = [f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
             f'<title>Candidate funnel</title><style>{CSS}</style></head><body><main>',
             '<h1>Candidate funnel: where do migrations die?</h1>',
             f'<p class="note">{_e(report["label"])} · generated {_e(report["generated_at"])} · {report["candidates"]} candidates · '
             'research description only, no trading evidence, no live readiness.</p>',
             '<h2>All candidates</h2><div class="card">', _funnel_table(t), '</div>',
             '<div class="card">', _reason_table(t['died_before'], 'died'), '</div>',
             '<div class="card">', _reason_table(t['pending'], 'pending'), '</div>',
             '<h2>Request budget by ending stage</h2><p class="note">Ending stage = the first stage a candidate did not reach; '
             '<em>buy</em> = completed. Only candidates whose result carries request counts are included.</p><div class="card"><table><thead><tr><th>stage</th><th class="n">candidates</th>'
             '<th class="n">requests total</th><th class="n">median</th><th class="n">investigation used</th></tr></thead><tbody>']
    for stage, v in t['requests_by_ending_stage'].items():
        parts.append(f'<tr><td>{_e(stage)}</td><td class="n">{v["candidates_with_data"]}</td><td class="n">{v["attempted_total"]}</td>'
                     f'<td class="n">{_e(v["attempted_median"])}</td><td class="n">{v["investigation_used_total"]}</td></tr>')
    parts.append('</tbody></table></div><h2>Median seconds between stages</h2><div class="card"><table><tbody>')
    for name, value in t['median_seconds'].items():
        parts.append(f'<tr><td>{_e(name)}</td><td class="n">{_e("n/a" if value is None else round(value, 1))}</td>'
                     f'<td class="n note">n={t["samples_seconds"][name]}</td></tr>')
    parts.append('</tbody></table></div><h2>By day</h2><div class="card"><table><thead><tr><th>day (UTC)</th>'
                 + ''.join(f'<th class="n">{_e(s)}</th>' for s in STAGES) + '</tr></thead><tbody>')
    for day, v in report['by_day'].items():
        parts.append(f'<tr><td>{_e(day)}</td>' + ''.join(f'<td class="n">{v["reached"][s]}</td>' for s in STAGES) + '</tr>')
    parts.append('</tbody></table></div><h2>By hour</h2><div class="card"><table><thead><tr><th>hour (UTC)</th>'
                 + ''.join(f'<th class="n">{_e(s)}</th>' for s in STAGES) + '</tr></thead><tbody>')
    for hour, v in report['by_hour'].items():
        parts.append(f'<tr><td>{_e(hour)}</td>' + ''.join(f'<td class="n">{v["reached"][s]}</td>' for s in STAGES) + '</tr>')
    parts.append('</tbody></table></div>')
    flags = t['flags']
    frames = report['frames']
    parts.append('<h2>Integrity</h2><div class="card"><table><tbody>')
    if frames:
        for key in ('frames', 'altered', 'undecodable', 'not_migration', 'ambiguous', 'truncated'):
            cls = ' class="warn"' if key in ('altered', 'truncated') and frames[key] else ''
            parts.append(f'<tr><td{cls}>discovery frames {_e(key)}</td><td class="n">{_e(frames[key])}</td></tr>')
    for key, n in sorted(flags.items()):
        parts.append(f'<tr><td class="warn">{_e(key)}</td><td class="n">{n}</td></tr>')
    parts.append(f'<tr><td>unreadable journal intents</td><td class="n">{report["journal_unreadable_intents"]}</td></tr></tbody></table></div>')
    cf = report['counterfactual']
    if cf.get('status') == 'READ':
        parts.append(f'<h2>Forward return by ending group (+{cf["horizon_seconds"]}s, counterfactual)</h2><div class="card"><table><thead>'
                     '<tr><th>group</th><th class="n">candidates</th><th class="n">with return</th><th class="n">pool died</th>'
                     '<th class="n">median</th><th class="n">share positive</th></tr></thead><tbody>')
        for group, g in cf['groups'].items():
            parts.append(f'<tr><td>{_e(group)}</td><td class="n">{g["candidates"]}</td><td class="n">{g["with_return"]}</td>'
                         f'<td class="n">{g["pool_died"]}</td><td class="n">{_e(g["median_return"])}</td><td class="n">{_e(g["share_positive"])}</td></tr>')
        parts.append('</tbody></table></div>')
    parts.append('</main></body></html>')
    return ''.join(parts)


def write_html(path, text):
    p = Path(path)
    if not p.parent.is_dir():
        raise FunnelError('OUTPUT_DIRECTORY_MISSING')
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)   # never overwrite
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


# --------------------------------------------------------------------------- CLI
def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    p.add_argument('--discovery-db', required=True)
    p.add_argument('--journal', required=True)
    p.add_argument('--research-db', required=True)
    p.add_argument('--ledger', required=True)
    p.add_argument('--evidence-db')
    p.add_argument('--decisions-db')
    p.add_argument('--counterfactual-store')
    p.add_argument('--since', type=float, default=0.0, help='UTC epoch seconds (inclusive)')
    p.add_argument('--until', type=float, help='UTC epoch seconds (exclusive)')
    p.add_argument('--now', type=float, help='override the report time (testing)')
    p.add_argument('--horizon', type=int, default=3600)
    p.add_argument('--out-html', help='write a single self-contained HTML file (must not exist)')
    args = p.parse_args(argv)
    try:
        report = build(discovery_db=args.discovery_db, journal=args.journal, research_db=args.research_db,
                       ledger=args.ledger, evidence_db=args.evidence_db, decisions_db=args.decisions_db,
                       counterfactual_store=args.counterfactual_store, since=args.since, until=args.until,
                       now=args.now, horizon=args.horizon)
        if args.out_html:
            write_html(args.out_html, render_html(report))
            report['html_written'] = True
    except FunnelError as error:
        print(json.dumps({'kind': 'funnel_report_v1', 'status': 'UNAVAILABLE', 'code': error.code, 'label': LABEL}, sort_keys=True))
        return 2
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'kind': 'funnel_report_v1', 'status': 'UNAVAILABLE', 'code': 'STORE_UNREADABLE_OR_INVALID', 'label': LABEL},
                         sort_keys=True))
        return 2
    print(json.dumps(report, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
