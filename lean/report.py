"""Read-only report on a lean.sqlite store: one self-contained HTML plus a JSON summary.

PAPER ONLY, EXECUTION_UNVERIFIED. Never writes to the store, never calls a provider, never imports the desk.

Input contract (what this report reads; see ``load_events``). The lean store is append-only; this module
accepts either layout and normalises both into events ``(kind, at, strategy_version, code_version, data)``:
  * one generic table ``records`` / ``events`` with columns ``kind`` and ``payload`` (JSON text), or
  * typed tables ``candidates``, ``screens``, ``decisions``, ``fills``, ``errors``, ``observations``
    that carry a JSON ``payload`` column or plain columns.
``strategy_version`` / ``code_version`` / ``at`` (or ``ts`` / ``recorded_at``) are read from columns first, then
from the payload. Payload field names relied on (first present wins):
  mint: ``mint|token|token_mint``;  fill: ``side`` (buy/sell), ``qty|quantity|tokens``, ``sol|sol_amount|notional_sol``
  (SOL exchanged at the fill price, fee excluded), ``fee_sol|fee``, ``reason|exit_reason``;
  screen: ``passed`` (bool), ``reasons`` (list);  decision: ``action|decision`` and ``reasons|reason``.
Buy cash out = sol + fee; sell cash in = sol - fee. A row that cannot be read is counted in ``skipped`` with a
reason and never stops the report; a trade whose quantities do not reconcile is flagged and excluded from PnL.
"""
import argparse
import datetime
import html
import json
import os
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

LABEL = 'PAPER ONLY / EXECUTION_UNVERIFIED'
MIN_SAMPLE = 30
MAX_ROWS = 2_000_000
BASELINE_HORIZON = 300
HORIZONS = (300, 900, 1800, 3600, 7200, 21600)
GENERIC_TABLES = ('records', 'events')
TYPED_TABLES = {'candidates': 'candidate', 'candidate': 'candidate', 'screens': 'screen', 'screen': 'screen',
                'decisions': 'decision', 'decision': 'decision', 'fills': 'fill', 'fill': 'fill',
                'errors': 'error', 'error': 'error', 'observations': 'observation', 'observation': 'observation'}
KIND_ALIASES = {'candidate': 'candidate', 'screen': 'screen', 'screened': 'screen', 'decision': 'decision',
                'fill': 'fill', 'error': 'error', 'observation': 'observation'}
EPS = Decimal('1e-9')


class ReportError(ValueError):
    pass


# --------------------------------------------------------------------------- helpers
def dec(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def num(value, places=6):
    return None if value is None else round(float(value), places)


def pick(data, *names):
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def percentile(values, q):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * q
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def dist(values):
    values = [Decimal(v) for v in values]
    if not values:
        return {'n': 0, 'mean': None, 'median': None, 'p10': None, 'p90': None, 'min': None, 'max': None}
    return {'n': len(values), 'mean': num(sum(values) / len(values)), 'median': num(percentile(values, Decimal('0.5'))),
            'p10': num(percentile(values, Decimal('0.1'))), 'p90': num(percentile(values, Decimal('0.9'))),
            'min': num(min(values)), 'max': num(max(values))}


def sample_flag(n):
    return 'INSUFFICIENT_SAMPLE' if n < MIN_SAMPLE else 'OK'


# --------------------------------------------------------------------------- read-only access
def open_ro(path):
    """Read-only. ``immutable`` only for a quiet WAL file (a plain ro open would create -wal/-shm beside a
    live store); a rollback-journal store can change under the read, so it is always ``mode=ro``."""
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReportError('DB_MISSING_OR_NOT_REGULAR: %s' % path.name)
    resolved = path.resolve()
    try:
        with open(resolved, 'rb') as stream:
            header = stream.read(20)
        wal = len(header) >= 20 and header[18] == 2 and header[19] == 2
    except OSError as error:
        raise ReportError('DB_UNREADABLE') from error
    flag = 'immutable=1' if wal and not Path(str(resolved) + '-wal').exists() else 'mode=ro'
    connection = sqlite3.connect(resolved.as_uri() + '?' + flag, uri=True, timeout=5)
    connection.execute('PRAGMA query_only=1')
    return connection


def _columns(connection, table):
    return [row[1] for row in connection.execute('PRAGMA table_info("%s")' % table.replace('"', ''))]


def load_events(connection):
    """Normalised events plus ``skipped`` (reason -> count) and ``truncated`` tables."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    events, skipped, truncated, used = [], Counter(), [], []
    sources = [(t, None) for t in GENERIC_TABLES if t in tables] + \
              [(t, kind) for t, kind in TYPED_TABLES.items() if t in tables]
    for table, fixed_kind in sources:
        columns = _columns(connection, table)
        used.append(table)
        count = 0
        for raw in connection.execute('SELECT * FROM "%s" ORDER BY rowid LIMIT ?' % table, (MAX_ROWS + 1,)):
            count += 1
            if count > MAX_ROWS:
                truncated.append(table)
                break
            row = dict(zip(columns, raw))
            data = row.get('payload')
            if isinstance(data, (bytes, bytearray)):
                data = bytes(data).decode('utf-8', 'replace')
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    skipped['PAYLOAD_NOT_JSON:' + table] += 1
                    continue
            elif data is None:
                data = {k: v for k, v in row.items() if k not in ('id', 'kind', 'payload')}
            if not isinstance(data, dict):
                skipped['PAYLOAD_NOT_OBJECT:' + table] += 1
                continue
            kind = fixed_kind or KIND_ALIASES.get(str(row.get('kind') or data.get('kind') or '').lower())
            if kind is None:
                skipped['UNKNOWN_KIND:' + table] += 1
                continue
            at = dec(next((row[c] for c in ('at', 'ts', 'recorded_at') if row.get(c) is not None), None))
            if at is None:
                at = dec(pick(data, 'at', 'ts', 'recorded_at'))
            events.append({'kind': kind, 'at': at, 'strategy': str(row.get('strategy_version') or data.get('strategy_version') or 'UNKNOWN'),
                           'code': str(row.get('code_version') or data.get('code_version') or 'UNKNOWN'), 'data': data})
    return events, skipped, truncated, used


# --------------------------------------------------------------------------- sections
def funnel(events):
    last_screen, candidates, screened, entered = {}, set(), set(), set()
    decision_rejects = Counter()
    for e in events:
        mint = pick(e['data'], 'mint', 'token', 'token_mint')
        if not isinstance(mint, str):
            continue
        if e['kind'] == 'candidate':
            candidates.add(mint)
        elif e['kind'] == 'screen':
            screened.add(mint)
            last_screen[mint] = e
        elif e['kind'] == 'decision':
            action = str(pick(e['data'], 'action', 'decision') or '').upper()
            if action in ('REJECT', 'REJECTED', 'SKIP', 'NO_ENTRY'):
                reasons = pick(e['data'], 'reasons', 'reason')
                for reason in (reasons if isinstance(reasons, list) else [reasons or 'UNSPECIFIED']):
                    decision_rejects[str(reason)] += 1
        elif e['kind'] == 'fill' and str(e['data'].get('side', '')).lower() == 'buy':
            entered.add(mint)
    candidates |= screened
    passed = {m for m, e in last_screen.items() if e['data'].get('passed') is True}
    reasons = Counter()
    for mint, e in last_screen.items():
        if e['data'].get('passed') is True:
            continue
        listed = e['data'].get('reasons')
        for reason in set(str(r) for r in (listed if isinstance(listed, list) and listed else ['UNSPECIFIED'])):
            reasons[reason] += 1
    return {'candidates': len(candidates), 'screened': len(screened), 'passed': len(passed), 'entered': len(entered),
            'rejection_reasons': dict(reasons.most_common()), 'entry_decision_rejections': dict(decision_rejects.most_common()),
            'rejected_mints': sorted(m for m in last_screen if m not in passed)}, last_screen


def trades(events):
    """Group fills per mint into position lifecycles. Open or non-reconciling trades are listed, not scored."""
    fills = defaultdict(list)
    anomalies = []
    for e in events:
        if e['kind'] != 'fill':
            continue
        d = e['data']
        mint = pick(d, 'mint', 'token', 'token_mint')
        side = str(d.get('side', '')).lower()
        qty = dec(pick(d, 'qty', 'quantity', 'tokens'))
        sol = dec(pick(d, 'sol', 'sol_amount', 'notional_sol'))
        fee = dec(pick(d, 'fee_sol', 'fee')) or Decimal(0)
        if not isinstance(mint, str) or side not in ('buy', 'sell') or qty is None or qty <= 0 or sol is None or sol < 0 or fee < 0 or e['at'] is None:
            anomalies.append({'code': 'FILL_UNREADABLE', 'mint': mint if isinstance(mint, str) else None})
            continue
        fills[mint].append({'at': e['at'], 'side': side, 'qty': qty, 'sol': sol, 'fee': fee, 'strategy': e['strategy'],
                            'reason': str(pick(d, 'reason', 'exit_reason') or 'UNSPECIFIED')})
    out = []
    for mint, rows in fills.items():
        rows.sort(key=lambda r: (r['at'], r['side'] == 'sell'))
        current = None
        for r in rows:
            if current is None:
                if r['side'] == 'sell':
                    anomalies.append({'code': 'SELL_WITHOUT_POSITION', 'mint': mint})
                    continue
                current = {'mint': mint, 'strategy': r['strategy'], 'entry_at': r['at'], 'cost': Decimal(0), 'proceeds': Decimal(0),
                           'qty': Decimal(0), 'exit_at': None, 'reasons': [], 'status': 'OPEN', 'flag': None}
                out.append(current)
            if r['side'] == 'buy':
                current['cost'] += r['sol'] + r['fee']
                current['qty'] += r['qty']
            else:
                if r['qty'] > current['qty'] + EPS * max(Decimal(1), current['qty']):
                    current['flag'] = 'SELL_EXCEEDS_POSITION'
                    anomalies.append({'code': 'SELL_EXCEEDS_POSITION', 'mint': mint})
                current['proceeds'] += r['sol'] - r['fee']
                current['qty'] -= r['qty']
                current['reasons'].append(r['reason'])
                current['exit_at'] = r['at']
                if current['qty'] <= EPS * max(Decimal(1), r['qty']):
                    current['status'] = 'CLOSED'
                    current = None
    for t in out:
        t['scored'] = t['status'] == 'CLOSED' and t['flag'] is None and t['cost'] > 0
    return out, anomalies


def trade_row(t):
    row = {'mint': t['mint'], 'strategy_version': t['strategy'], 'status': t['status'], 'flag': t['flag'],
           'entry_at': num(t['entry_at'], 3), 'exit_at': num(t['exit_at'], 3) if t['exit_at'] is not None else None,
           'cost_sol': num(t['cost']), 'proceeds_sol': num(t['proceeds']), 'exit_reasons': t['reasons'],
           'final_exit_reason': t['reasons'][-1] if t['reasons'] and t['status'] == 'CLOSED' else None}
    if t['scored']:
        row['pnl_sol'] = num(t['proceeds'] - t['cost'])
        row['return'] = num((t['proceeds'] - t['cost']) / t['cost'])
        row['hold_seconds'] = num(t['exit_at'] - t['entry_at'], 3)
    else:
        row['pnl_sol'] = row['return'] = row['hold_seconds'] = None
    return row


def pnl_by_strategy(all_trades):
    groups = defaultdict(list)
    for t in all_trades:
        if t['scored']:
            groups[t['strategy']].append(t)
    result = {}
    for strategy, items in sorted(groups.items()):
        pnls = [t['proceeds'] - t['cost'] for t in items]
        returns = [(t['proceeds'] - t['cost']) / t['cost'] for t in items]
        result[strategy] = {'trades': len(items), 'wins': sum(1 for p in pnls if p > 0), 'pnl_sol': num(sum(pnls)),
                            'cost_sol': num(sum(t['cost'] for t in items)), 'return': dist(returns), 'sample': sample_flag(len(items))}
    return result


def exit_breakdown(all_trades):
    groups = defaultdict(list)
    for t in all_trades:
        if t['scored']:
            groups[t['reasons'][-1]].append(t)
    return {reason: {'trades': len(items), 'pnl_sol': num(sum(t['proceeds'] - t['cost'] for t in items)),
                     'return': dist([(t['proceeds'] - t['cost']) / t['cost'] for t in items]),
                     'hold_seconds': dist([t['exit_at'] - t['entry_at'] for t in items]), 'sample': sample_flag(len(items))}
            for reason, items in sorted(groups.items())}


def counterfactual(path, rejected_by_reason, horizon):
    """Forward return of rejected mints from T26's counterfactual store: +5m baseline (never re-baselined);
    a dead pool is -100%; a candidate without a usable baseline or horizon sample is counted, not guessed."""
    if horizon not in HORIZONS:
        raise ReportError('HORIZON_NOT_SAMPLED')
    connection = open_ro(path)
    try:
        samples = defaultdict(dict)
        for mint, h, status, price in connection.execute('SELECT mint,horizon,status,price FROM samples'):
            samples[mint][h] = (status, dec(price))
    finally:
        connection.close()
    groups = {}
    for reason, mints in sorted(rejected_by_reason.items()):
        returns, no_baseline, no_horizon, dead = [], 0, 0, 0
        for mint in mints:
            s = samples.get(mint, {})
            base = s.get(BASELINE_HORIZON)
            if base is None or base[0] != 'OK' or base[1] is None or base[1] <= 0:
                no_baseline += 1
                continue
            at = s.get(horizon)
            if at is not None and at[0] == 'POOL_DEAD':
                returns.append(Decimal(-1)); dead += 1
            elif at is not None and at[0] == 'OK' and at[1] is not None:
                returns.append(at[1] / base[1] - 1)
            else:
                no_horizon += 1
        groups[reason] = {'rejected': len(mints), 'with_return': len(returns), 'baseline_missing': no_baseline,
                          'horizon_missing': no_horizon, 'pool_dead': dead, 'return': dist(returns),
                          'share_positive': num(Decimal(sum(1 for r in returns if r > 0)) / len(returns)) if returns else None,
                          'sample': sample_flag(len(returns))}
    return {'horizon_seconds': horizon, 'baseline_seconds': BASELINE_HORIZON, 'groups': groups}


def errors_by_code(events):
    counts = Counter()
    for e in events:
        if e['kind'] == 'error':
            counts[str(pick(e['data'], 'code', 'error', 'reason') or 'UNSPECIFIED')] += 1
    return dict(counts.most_common())


# --------------------------------------------------------------------------- build
def _section(function, *args):
    try:
        return {'status': 'OK', 'data': function(*args)}
    except (ReportError, sqlite3.Error, OSError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        return {'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}


def build(db, *, counterfactual_db=None, horizon=3600, now=None):
    now = time.time() if now is None else now
    connection = open_ro(db)
    try:
        events, skipped, truncated, used = load_events(connection)
    finally:
        connection.close()
    summary = {'kind': 'lean_report_v1', 'label': LABEL, 'generated_at': datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
               'min_sample': MIN_SAMPLE, 'tables': used, 'events': len(events), 'skipped': dict(skipped), 'truncated_tables': truncated,
               'versions': {'strategy': dict(Counter(e['strategy'] for e in events)), 'code': dict(Counter(e['code'] for e in events))},
               'profitability_verdict': 'NOT_ASSESSED'}
    funnel_result = _section(funnel, events)
    all_trades, anomalies = trades(events)
    summary['funnel'] = ({'status': 'OK', 'data': {k: v for k, v in funnel_result['data'][0].items() if k != 'rejected_mints'}}
                         if funnel_result['status'] == 'OK' else funnel_result)
    scored = [t for t in all_trades if t['scored']]
    summary['trades'] = {'status': 'OK', 'data': {
        'closed_scored': len(scored), 'open': sum(1 for t in all_trades if t['status'] == 'OPEN'),
        'flagged': sum(1 for t in all_trades if t['flag']), 'anomalies': anomalies[:200], 'anomaly_count': len(anomalies),
        'sample': sample_flag(len(scored)), 'rows': [trade_row(t) for t in all_trades[:5000]], 'rows_truncated': len(all_trades) > 5000,
        'hold_seconds': dist([t['exit_at'] - t['entry_at'] for t in scored]),
        'overall_pnl_sol': num(sum(t['proceeds'] - t['cost'] for t in scored)), 'wins': sum(1 for t in scored if t['proceeds'] > t['cost'])}}
    summary['pnl_by_strategy'] = {'status': 'OK', 'data': pnl_by_strategy(all_trades)}
    summary['exit_reasons'] = {'status': 'OK', 'data': exit_breakdown(all_trades)}
    summary['errors'] = {'status': 'OK', 'data': errors_by_code(events)}
    if counterfactual_db is None:
        summary['counterfactual'] = {'status': 'NOT_PROVIDED'}
    elif funnel_result['status'] != 'OK':
        summary['counterfactual'] = {'status': 'ERROR', 'code': 'FUNNEL_UNAVAILABLE'}
    else:
        by_reason = defaultdict(list)
        last_screen = funnel_result['data'][1]
        for mint in funnel_result['data'][0]['rejected_mints']:
            listed = last_screen[mint]['data'].get('reasons')
            for reason in set(str(r) for r in (listed if isinstance(listed, list) and listed else ['UNSPECIFIED'])):
                by_reason[reason].append(mint)
        summary['counterfactual'] = _section(counterfactual, counterfactual_db, by_reason, horizon)
    return summary


# --------------------------------------------------------------------------- render
CSS = ('body{font:14px/1.45 system-ui,sans-serif;margin:0;background:#fafafa;color:#1a1a1a}main{max-width:1000px;margin:0 auto;padding:16px}'
       'h1{font-size:20px}h2{font-size:16px;margin-top:24px}table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}'
       'th,td{border:1px solid #ddd;padding:4px 8px;text-align:left}td.n{text-align:right;font-variant-numeric:tabular-nums}'
       '.banner{background:#fff3cd;border:1px solid #e0c36a;padding:8px}.flag{color:#a15c00;font-weight:600}.err{color:#b00020}'
       '.wrap{overflow-x:auto}@media(prefers-color-scheme:dark){body{background:#161616;color:#e8e8e8}table{background:#1f1f1f}'
       'th,td{border-color:#444}.banner{background:#3a3217;border-color:#7a6a2a}.flag{color:#f0b64a}}')


def e(value):
    return html.escape('' if value is None else str(value))


def table(headers, rows):
    head = ''.join('<th>%s</th>' % e(h) for h in headers)
    body = ''.join('<tr>%s</tr>' % ''.join('<td class="%s">%s</td>' % ('n' if isinstance(c, (int, float)) and not isinstance(c, bool) else '', e(c)) for c in r) for r in rows)
    return '<div class="wrap"><table><tr>%s</tr>%s</table></div>' % (head, body)


def note(section):
    if section['status'] == 'OK':
        return ''
    return '<p class="err">%s %s</p>' % (e(section['status']), e(section.get('code', '')))


def flag(sample):
    return '<span class="flag">INSUFFICIENT_SAMPLE (n&lt;%d)</span>' % MIN_SAMPLE if sample != 'OK' else 'ok'


def render_html(s):
    parts = ['<h1>Lean desk report</h1><p class="banner">%s &middot; generated %s &middot; not trading evidence, not advice. '
             'Profitability verdict: NOT_ASSESSED. Any n below %d is flagged.</p>' % (e(LABEL), e(s['generated_at']), MIN_SAMPLE)]
    if s['skipped'] or s['truncated_tables']:
        parts.append('<p class="flag">Skipped rows: %s; truncated tables: %s</p>' % (e(json.dumps(s['skipped'], sort_keys=True)), e(s['truncated_tables'])))
    f = s['funnel']
    parts.append('<h2>Funnel</h2>' + note(f))
    if f['status'] == 'OK':
        d = f['data']
        parts.append(table(['stage', 'mints'], [[k, d[k]] for k in ('candidates', 'screened', 'passed', 'entered')]))
        parts.append(table(['rejection reason', 'mints'], list(d['rejection_reasons'].items())) if d['rejection_reasons'] else '<p>No rejections recorded.</p>')
    t = s['trades']['data']
    parts.append('<h2>Trades</h2><p>Closed and scored: %d (%s) &middot; open: %d &middot; flagged: %d &middot; anomalies: %d &middot; overall PnL %s SOL</p>'
                 % (t['closed_scored'], flag(t['sample']), t['open'], t['flagged'], t['anomaly_count'], e(t['overall_pnl_sol'])))
    hs = t['hold_seconds']
    parts.append('<p>Hold seconds: n=%d median=%s p90=%s max=%s</p>' % (hs['n'], e(hs['median']), e(hs['p90']), e(hs['max'])))
    parts.append('<h2>PnL per strategy_version</h2>' + (table(['strategy', 'trades', 'wins', 'pnl SOL', 'mean return', 'median return', 'sample'],
        [[k, v['trades'], v['wins'], v['pnl_sol'], v['return']['mean'], v['return']['median'], v['sample']] for k, v in s['pnl_by_strategy']['data'].items()]) or ''))
    parts.append('<h2>Exit reasons</h2>' + table(['reason', 'trades', 'pnl SOL', 'mean return', 'median hold s', 'sample'],
        [[k, v['trades'], v['pnl_sol'], v['return']['mean'], v['hold_seconds']['median'], v['sample']] for k, v in s['exit_reasons']['data'].items()]))
    parts.append('<h2>Per-trade table</h2>' + table(['mint', 'strategy', 'status', 'cost', 'proceeds', 'pnl', 'return', 'hold s', 'exit reasons', 'flag'],
        [[r['mint'], r['strategy_version'], r['status'], r['cost_sol'], r['proceeds_sol'], r['pnl_sol'], r['return'], r['hold_seconds'],
          ','.join(r['exit_reasons']), r['flag'] or ''] for r in t['rows']]))
    c = s['counterfactual']
    parts.append('<h2>Rejections vs forward price (counterfactual)</h2>' + note(c) + ('<p>Not provided.</p>' if c['status'] == 'NOT_PROVIDED' else ''))
    if c['status'] == 'OK':
        d = c['data']
        parts.append('<p>Horizon +%ds against the +%ds baseline; dead pool = -100%%.</p>' % (d['horizon_seconds'], d['baseline_seconds']) +
                     table(['reason', 'rejected', 'with return', 'baseline missing', 'mean', 'median', 'share positive', 'dead', 'sample'],
                           [[k, v['rejected'], v['with_return'], v['baseline_missing'], v['return']['mean'], v['return']['median'],
                             v['share_positive'], v['pool_dead'], v['sample']] for k, v in d['groups'].items()]))
    parts.append('<h2>Errors by code</h2>' + (table(['code', 'count'], list(s['errors']['data'].items())) if s['errors']['data'] else '<p>None recorded.</p>'))
    parts.append('<h2>Versions</h2>' + table(['kind', 'version', 'events'], [['strategy', k, v] for k, v in s['versions']['strategy'].items()] +
                                              [['code', k, v] for k, v in s['versions']['code'].items()]))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Lean desk report</title><style>%s</style></head><body><main>%s</main></body></html>' % (CSS, ''.join(parts)))


def write_exclusive(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def write_report(out_dir, summary):
    out_dir = Path(out_dir)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise ReportError('OUT_DIR_MISSING_OR_SYMLINK')
    stamp = summary['generated_at'][:19].replace(':', '').replace('-', '')
    html_path, json_path = out_dir / ('lean-report-%s.html' % stamp), out_dir / ('lean-report-%s.json' % stamp)
    text = json.dumps(summary, sort_keys=True, allow_nan=False) + '\n'
    write_exclusive(html_path, render_html(summary))
    try:
        write_exclusive(json_path, text)
    except BaseException:
        os.unlink(html_path)
        raise
    return html_path, json_path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--db', required=True, help='lean.sqlite (opened read-only)')
    p.add_argument('--out-dir', required=True)
    p.add_argument('--counterfactual', help='optional T26 counterfactual.sqlite (opened read-only)')
    p.add_argument('--horizon', type=int, default=3600)
    p.add_argument('--now', type=float)
    args = p.parse_args(argv)
    try:
        summary = build(args.db, counterfactual_db=args.counterfactual, horizon=args.horizon, now=args.now)
        html_path, json_path = write_report(args.out_dir, summary)
    except (ReportError, sqlite3.Error, OSError) as error:
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps({'status': 'OK', 'html': str(html_path), 'json': str(json_path)}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
