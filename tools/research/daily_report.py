"""Daily strategy research report: one self-contained HTML file plus a JSON summary (read-only, paper only).

    python -m tools.research.daily_report --root <FRESH_ROOT> --discovery-db D --pacing-db P --out-dir <STATE_DIR>/reports
        [--counterfactual-store C] [--grid G] [--features F] [--holdout-from UTC|epoch] [--lookback-days 7]

Combines the existing read-only research tools (imported, never re-implemented): the T29 funnel, the T26
counterfactual, the T27 shadow leaderboard, the T10 forward evaluation of real paper trades, the T14 fill
realism report, the healthcheck and the budget table. Every section is built independently: a section that
cannot be read is shown as ERROR with its exception class (never hidden, never fabricated), and a section whose
input was not given is NOT_PROVIDED. The "what changed / what to try next" block is computed from the data and
the previous day's JSON summary (no model, no network).

Safety: no provider request is made (no network access at all), every database is opened by the imported tools
read-only, and the only writes are ``YYYY-MM-DD.html`` / ``YYYY-MM-DD.json`` created exclusively (0600) in
``--out-dir``; an existing day is never overwritten. Everything here is a description of EXECUTION_UNVERIFIED
paper activity, not trading evidence, and carries an explicit sample size and an INSUFFICIENT_SAMPLE flag.
"""
import sys
sys.dont_write_bytecode = True
import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time

LABEL = 'EXECUTION_UNVERIFIED'
KIND = 'daily_strategy_report_v1'
MIN_SAMPLE = 30
MAX_LINES = 10
MAX_PREVIOUS_BYTES = 2 * 1024 * 1024
SECTIONS = ('health', 'budgets', 'funnel', 'counterfactual', 'shadow', 'forward', 'realism')
DAY_FILE = re.compile(r'^(\d{4}-\d{2}-\d{2})\.json$')
REPO = Path(__file__).resolve().parents[2]
DEFAULT_GRID = REPO / 'config' / 'experiments' / 'shadow' / 'exit-grid.json'
FRESH_NAMES = {'research': 'research.sqlite', 'evidence': 'evidence.sqlite', 'ledger': 'paper-ledger.sqlite',
               'decisions': 'paper-decisions.sqlite', 'journal': 'entry-dispatch/dispatch.sqlite',
               'counterfactual': 'counterfactual/counterfactual.sqlite', 'features': 'features/features.sqlite'}


class ReportError(ValueError):
    pass


# --------------------------------------------------------------------------- small helpers
def _dec(value):
    """Decimal or None; never raises (a malformed source value is simply not a number)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _fmt(value, places=4):
    number = _dec(value)
    if number is None:
        return 'n/a' if value is None else str(value)
    if number == number.to_integral_value():
        return str(int(number))
    return format(round(number, places), 'f')


def _pct(value):
    number = _dec(value)
    return 'n/a' if number is None else format(round(number * 100, 2), 'f') + '%'


def _day(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d')


def _iso(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _safe(function):
    """One section: OK / ERROR(class only, never a path or a secret-bearing message)."""
    try:
        return {'status': 'OK', 'data': function()}
    except Exception as error:  # a broken input must degrade one section, not the whole report
        return {'status': 'ERROR', 'error': type(error).__name__}   # never str(error): it can carry paths or provider text


def _not_provided(why):
    return {'status': 'NOT_PROVIDED', 'detail': why}


def _derive(opts):
    """Fresh-layout default paths for what ``--root`` implies, explicit options always win."""
    out = dict(opts)
    root = opts.get('root')
    if root:
        root = Path(root)
        for key, name in (('research_db', 'research'), ('evidence_db', 'evidence'), ('ledger', 'ledger'),
                          ('decisions_db', 'decisions'), ('journal', 'journal'),
                          ('counterfactual_store', 'counterfactual'), ('features_store', 'features')):
            if not out.get(key):
                out[key] = str(root / FRESH_NAMES[name])
    return out


def _shadow_features(ss, opts, candidates):
    """The T27 features for the shadow section and a label saying which candidates had REAL features and which fell back to the
    neutral ones. An explicit ``--features`` file wins; otherwise the T40 features store is used when it exists (a missing store
    is not an error: everything is then neutral). Store rows dated after a candidate's price path ends are look-ahead: dropped
    and counted, never used."""
    source, features, refused = None, None, 0
    if opts.get('features'):
        source, features = 'file', ss.load_features(opts['features'], candidates)
    elif opts.get('features_store') and Path(opts['features_store']).is_file():
        from tools.research import features as ft
        ends = {c['mint']: c['migrated_at'] + max((x['horizon'] for x in c['samples']), default=0) for c in candidates}
        wanted = {}
        for mint, entry in ft.shadow_features(opts['features_store']).items():
            if mint not in ends:
                continue
            if entry['as_of'] > ends[mint]:
                refused += 1
                continue
            wanted[mint] = entry
        if wanted:
            with tempfile.TemporaryDirectory() as tmp:       # reuse the shadow engine's own validation of the file format
                path = Path(tmp) / 'features.json'
                path.write_text(json.dumps(wanted, sort_keys=True))
                features = ss.load_features(path, candidates)
        source = 'features_store'
    real = sorted(m for m in (features or {}) if any(c['mint'] == m for c in candidates))
    fields = sorted({k for m in real for k in features[m]['fields']})
    label = {'source': source, 'mode': 'REAL_FEATURES_FOR_SOME_CANDIDATES' if real else 'NEUTRAL_FEATURES_ONLY',
             'candidates': len(candidates), 'real_candidates': len(real), 'neutral_candidates': len(candidates) - len(real),
             'fields': fields, 'look_ahead_refused': refused}
    if source is None:
        label['source'] = None
    return features, label


# --------------------------------------------------------------------------- collection (the only I/O)
def collect(opts, now):
    """Run every imported read-only tool on its own; returns {section: {status, data|error}}."""
    opts = _derive(opts)
    sections = {}

    def need(*keys):
        missing = [k for k in keys if not opts.get(k)]
        return 'missing ' + ', '.join(missing) if missing else None

    why = need('discovery_db', 'pacing_db', 'root')
    if why:
        sections['health'] = _not_provided(why)
    else:
        def health():
            from argparse import Namespace
            from tools.ops import healthcheck
            report = healthcheck.build_report(Namespace(
                root=opts['root'], discovery_db=opts['discovery_db'], pacing_db=opts['pacing_db'],
                backup_root=opts.get('backup_root'), config=opts.get('config'), no_systemd=bool(opts.get('no_systemd')),
                threshold=[]))
            return report
        sections['health'] = _safe(health)

    why = need('evidence_db', 'ledger')
    if why:
        sections['budgets'] = _not_provided(why)
    else:
        def budgets():
            from tools.ops import status
            return status.budgets_section(opts['evidence_db'], now, ledger_path=opts['ledger'])
        sections['budgets'] = _safe(budgets)

    why = need('discovery_db', 'journal', 'research_db', 'ledger')
    if why:
        sections['funnel'] = _not_provided(why)
    else:
        def funnel():
            from tools.research import funnel_report
            return funnel_report.build(
                discovery_db=opts['discovery_db'], journal=opts['journal'], research_db=opts['research_db'],
                ledger=opts['ledger'], evidence_db=opts.get('evidence_db'), decisions_db=opts.get('decisions_db'),
                counterfactual_store=opts.get('counterfactual_store'), since=now - 86400 * opts.get('lookback_days', 7),
                until=now + 1, now=now, horizon=opts.get('horizon', 3600))
        sections['funnel'] = _safe(funnel)

    why = need('counterfactual_store')
    if why:
        sections['counterfactual'] = sections['shadow'] = _not_provided(why)
    else:
        def counterfactual():
            from tools.research import counterfactual as cf
            return cf.report(opts['counterfactual_store'], horizon=opts.get('horizon', 3600))
        sections['counterfactual'] = _safe(counterfactual)

        def shadow():
            from tools.research import shadow_strategies as ss
            selection = {}
            candidates = ss.load_candidates(opts['counterfactual_store'], selection)
            features, label = _shadow_features(ss, opts, candidates)
            result = ss.run(candidates, ss.load_grid(opts.get('grid') or DEFAULT_GRID),
                            holdout_from=ss._time(opts.get('holdout_from')), features=features,
                            outcomes=ss.load_outcomes(opts['counterfactual_store'], now=now), selection=selection,
                            min_trades=opts.get('min_trades', ss.MIN_TRADES))
            result['features_label'] = label
            return result
        sections['shadow'] = _safe(shadow)

    why = need('ledger')
    if why:
        sections['forward'] = sections['realism'] = _not_provided(why)
    else:
        def forward():
            from tools.research import forward_eval
            from tools.research.shadow_strategies import _time
            return forward_eval.evaluate([opts['ledger']], holdout_from=_time(opts.get('holdout_from')), now=int(now),
                                         resamples=opts.get('resamples', 1000), seed=0, single=True)
        sections['forward'] = _safe(forward)

        def realism():
            from tools.research import fill_realism_report
            return fill_realism_report.report(opts['ledger'])
        sections['realism'] = _safe(realism)
    return sections


# --------------------------------------------------------------------------- metrics / insights (pure)
def metrics_of(sections):
    """Flat numbers used for the day-over-day comparison; None when the source section is not OK."""
    def data(name):
        s = sections.get(name) or {}
        return s.get('data') if s.get('status') == 'OK' else None

    m = {}
    f = data('funnel')
    if f:
        total = f.get('total') or {}
        m['funnel_candidates'] = f.get('candidates')
        m['funnel_buys'] = (total.get('reached') or {}).get('buy')
        m['funnel_unresolved'] = (f.get('unresolved') or {}).get('count')
    c = data('counterfactual')
    if c:
        m['counterfactual_candidates'] = c.get('distinct_candidates')
    s = data('shadow')
    if s:
        ranked = [r for r in s.get('leaderboard', ()) if r.get('rank')]
        m['shadow_ranked_variants'] = len(ranked)
        m['shadow_top_variant'] = ranked[0]['variant'] if ranked else None
        m['shadow_candidates'] = sum((s.get('candidates') or {}).values())
    fw = data('forward')
    if fw and fw.get('experiments'):
        row = fw['experiments'][-1]
        m['forward_closed_trades'] = (row.get('all') or {}).get('closed_trades')
        m['forward_net_pnl_sol'] = (row.get('all') or {}).get('net_pnl_sol')
    h = data('health')
    if h:
        m['health_status'] = h.get('status')
    b = data('budgets')
    if b and b.get('monitoring'):
        m['monitoring_used_in_window'] = b['monitoring'].get('used_in_window')
        m['monitoring_cap'] = b['monitoring'].get('cap')
    r = data('realism')
    if r:
        m['realism_fills'] = r.get('fills')
        m['realism_verified_samples'] = r.get('verified_samples')
    return m


def _delta(label, now_value, before_value, previous_day):
    a, b = _dec(now_value), _dec(before_value)
    if a is None:
        return None
    if b is None:
        return '%s: %s (no figure on %s)' % (label, _fmt(a), previous_day)
    change = a - b
    return '%s: %s (%s%s vs %s)' % (label, _fmt(a), '+' if change >= 0 else '', _fmt(change), previous_day)


CHANGE_METRICS = (('funnel_candidates', 'candidates seen'), ('funnel_buys', 'paper buys'),
                  ('counterfactual_candidates', 'counterfactual candidates'), ('forward_closed_trades', 'closed paper trades'),
                  ('forward_net_pnl_sol', 'net paper PnL (SOL)'), ('shadow_candidates', 'shadow-evaluated candidates'),
                  ('realism_verified_samples', 'verified realism samples'))


def what_changed(sections, metrics, previous):
    lines = []
    unresolved = metrics.get('funnel_unresolved')
    if unresolved:
        lines.append('ALERT: %d UNRESOLVED latch(es) in the funnel: the global gate blocks new entries until they are resolved' % unresolved)
    health = metrics.get('health_status')
    if health not in (None, 'OK'):
        lines.append('ALERT: healthcheck status is %s' % health)
    for name in SECTIONS:
        if (sections.get(name) or {}).get('status') == 'ERROR':
            lines.append('ERROR: section %s could not be built (%s)' % (name, sections[name].get('error')))
    if previous is None:
        lines.append('No earlier report found: nothing to compare against yet')
        return lines
    before = previous.get('metrics') or {}
    day = previous.get('date', 'the previous report')
    for key, label in CHANGE_METRICS:
        text = _delta(label, metrics.get(key), before.get(key), day)
        if text:
            lines.append(text)
    if metrics.get('shadow_top_variant') != before.get('shadow_top_variant') and 'shadow_top_variant' in metrics:
        lines.append('shadow leaderboard leader: %s (was %s on %s)' % (metrics.get('shadow_top_variant') or 'none ranked',
                                                                       before.get('shadow_top_variant') or 'none ranked', day))
    if health != before.get('health_status') and health is not None and 'health_status' in before:
        lines.append('health status changed: %s -> %s' % (before.get('health_status'), health))
    return lines


def what_next(sections, metrics):
    lines = []

    def data(name):
        s = sections.get(name) or {}
        return s.get('data') if s.get('status') == 'OK' else None

    if metrics.get('funnel_unresolved'):
        lines.append('Resolve the UNRESOLVED latch first: nothing else matters while the entry gate is blocked')
    if metrics.get('health_status') not in (None, 'OK'):
        lines.append('Fix the failing healthcheck items listed below before reading any strategy number')
    trades = metrics.get('forward_closed_trades')
    if trades is not None and trades < MIN_SAMPLE:
        lines.append('INSUFFICIENT_SAMPLE: %d closed paper trades < %d; change nothing in the strategy, keep collecting' % (trades, MIN_SAMPLE))
    cf = data('counterfactual')
    if cf:
        groups = cf.get('groups') or {}
        bought = groups.get('BOUGHT') or {}
        base = _dec(bought.get('mean_return')) if (bought.get('with_return') or 0) >= MIN_SAMPLE else None
        winners = []
        for name, g in groups.items():
            if not name.startswith('REJECTED') or (g.get('with_return') or 0) < MIN_SAMPLE:
                continue
            mean = _dec(g.get('mean_return'))
            if mean is not None and mean > (base if base is not None else Decimal(0)):
                winners.append((mean, name, g['with_return']))
        for mean, name, n in sorted(winners, reverse=True)[:3]:
            lines.append('Review the filter behind %s: its rejects average %s at +%ds (n=%d) vs %s for bought candidates'
                         % (name, _pct(mean), cf.get('horizon_seconds', 0), n, _pct(base) if base is not None else 'no bought baseline'))
        adequate = [g for name, g in groups.items() if name.startswith('REJECTED') and (g.get('with_return') or 0) >= MIN_SAMPLE]
        if not winners and groups and adequate:
            lines.append('Counterfactual: no rejection group with n>=%d outperforms the bought baseline; filters look non-harmful so far' % MIN_SAMPLE)
        elif not winners and groups:
            largest = max([g.get('with_return') or 0 for name, g in groups.items() if name.startswith('REJECTED')] or [0])
            lines.append('INSUFFICIENT_SAMPLE: every counterfactual group has n<%d (largest rejection group n=%d); '
                         'nothing can be said about whether the filters are harmful' % (MIN_SAMPLE, largest))
        elif not groups:
            lines.append('Counterfactual store has no outcome groups yet: let ingest/sample run')
    shadow = data('shadow')
    if shadow:
        ranked = [r for r in shadow.get('leaderboard', ()) if r.get('rank')]
        if not ranked:
            lines.append('Shadow: no variant has the minimum holdout trades to be ranked; keep collecting (do not pick a variant yet)')
        else:
            top = ranked[0]
            low = (top['holdout'].get('bootstrap_ci') or (None, None))[0]
            if _dec(low) is not None and _dec(low) > 0:
                lines.append('Shadow candidate: %s has a holdout bootstrap CI lower bound %s > 0 over %d trades; replicate on new data before any change'
                             % (top['variant'], _pct(low), top['holdout']['trades']))
            else:
                lines.append('Shadow: best ranked variant %s still has a CI lower bound <= 0; no variant is distinguishable from noise' % top['variant'])
    realism = data('realism')
    if realism and realism.get('status') == 'REPORTED' and not realism.get('coverage_complete'):
        lines.append('Fill realism coverage is partial: latency-adjusted PnL only covers fully measured trades')
    if not lines:
        lines.append('Nothing actionable from the available data')
    return lines


# --------------------------------------------------------------------------- previous summary
def find_previous(out_dir, today):
    """The newest ``YYYY-MM-DD.json`` strictly before ``today`` that is a readable report of this kind, else None."""
    try:
        names = sorted((m.group(1) for m in (DAY_FILE.match(n) for n in os.listdir(out_dir)) if m and m.group(1) < today), reverse=True)
    except OSError:
        return None
    for day in names:
        path = Path(out_dir) / (day + '.json')
        try:
            if path.is_symlink() or path.stat().st_size > MAX_PREVIOUS_BYTES:
                continue
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get('kind') == KIND and value.get('date') == day and isinstance(value.get('metrics'), dict):
            return value
    return None


def build(sections, *, now, previous=None, source=None):
    metrics = metrics_of(sections)
    summary = {'kind': KIND, 'date': _day(now), 'generated_at': _iso(now), 'now': now, 'label': LABEL, 'paper_only': True,
               'live_readiness': False, 'profitability_verdict': 'NOT_ASSESSED', 'min_sample': MIN_SAMPLE,
               'section_status': {n: (sections.get(n) or _not_provided('absent'))['status'] for n in SECTIONS},
               'metrics': metrics, 'changed': what_changed(sections, metrics, previous),
               'next': what_next(sections, metrics), 'previous_date': previous.get('date') if previous else None}
    if source:
        summary['source'] = source
    return summary


# --------------------------------------------------------------------------- HTML
CSS = """
:root{--bg:#fbfaf7;--fg:#1f1d1a;--muted:#6b665e;--card:#fff;--line:#e4dfd5;--warn:#a23b2a;--ok:#2f6f62;--tag:#efe9dc}
@media (prefers-color-scheme:dark){:root{--bg:#171614;--fg:#ece8df;--muted:#9a948a;--card:#1f1d1a;--line:#34312b;--warn:#e27d6a;--ok:#5fb39f;--tag:#2a2722}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1000px;margin:0 auto}h1{font-size:1.4rem;margin:.2rem 0}h2{font-size:1.1rem;margin:1.6rem 0 .4rem}
.banner{background:var(--tag);border:1px solid var(--line);border-radius:8px;padding:8px 12px;font-size:.85rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin:10px 0;overflow-x:auto}
.note{color:var(--muted);font-size:.85rem}.warn{color:var(--warn);font-weight:600}.ok{color:var(--ok)}
table{border-collapse:collapse;width:100%;font-size:.88rem}th,td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
ol{margin:.3rem 0;padding-left:1.4rem}code{background:var(--tag);padding:0 4px;border-radius:3px}
"""


def _e(value):
    return html.escape('' if value is None else str(value), quote=True)


def _table(headers, rows, numeric_from=1):
    head = ''.join('<th%s>%s</th>' % (' class="n"' if i >= numeric_from else '', _e(h)) for i, h in enumerate(headers))
    body = ''.join('<tr>' + ''.join('<td%s>%s</td>' % (' class="n"' if i >= numeric_from else '', _e(c)) for i, c in enumerate(row)) + '</tr>'
                   for row in rows)
    return '<table><tr>%s</tr>%s</table>' % (head, body)


def _flag(n, label='n'):
    return '%s=%s%s' % (label, n, '' if (n or 0) >= MIN_SAMPLE else ' INSUFFICIENT_SAMPLE')


def _status_note(section):
    if section['status'] == 'OK':
        return ''
    text = section['status'] + (': ' + section['error'] if section.get('error') else '') + (' (%s)' % section['detail'] if section.get('detail') else '')
    return '<p class="warn">%s</p>' % _e(text)


def _render_health(s):
    d = s['data']
    rows = [(c.get('check'), c.get('severity'), c.get('detail')) for c in d.get('checks', ()) if c.get('severity') != 'OK']
    out = '<p>Status: <b class="%s">%s</b>; %d checks, %d not OK.</p>' % ('ok' if d.get('status') == 'OK' else 'warn', _e(d.get('status')),
                                                                          len(d.get('checks', ())), len(rows))
    return out + (_table(('check', 'severity', 'detail'), rows, 99) if rows else '')


def _render_budgets(s):
    m = s['data'].get('monitoring')
    own = s['data'].get('ownership') or {}
    if not m:
        return '<p class="note">No monitoring budget row.</p>'
    rows = [('monitoring used in window', '%s / %s' % (m.get('used_in_window'), m.get('cap'))), ('remaining', m.get('remaining_in_window')),
            ('pending reservations', m.get('pending_reservations')), ('blocked', m.get('blocked')),
            ('lifetime total charged', m.get('total')), ('high water', m.get('high_water')),
            ('ownership budgets exhausted', '%s / %s' % (own.get('exhausted'), own.get('total')))]
    return _table(('budget', 'value'), rows, 99)


def _render_funnel(s):
    d = s['data']
    parts = []
    if (d.get('unresolved') or {}).get('count'):
        parts.append('<p class="warn">UNRESOLVED latch: %s</p>' % _e(json.dumps(d['unresolved']['by_code'], sort_keys=True)))
    total = d.get('total') or {}
    reached = total.get('reached') or {}
    first = d.get('candidates') or 0
    parts.append('<p class="note">%s candidates in the lookback window.</p>' % _e(_flag(first, 'n')))
    parts.append(_table(('stage', 'reached', 'share of candidates'),
                        [(st, reached.get(st, 0), _pct(Decimal(reached.get(st, 0)) / Decimal(first)) if first else 'n/a') for st in d.get('stages', ())]))
    deaths = sorted(((n, stage, reason) for stage, reasons in (total.get('died_before') or {}).items() for reason, n in reasons.items()), reverse=True)[:8]
    if deaths:
        parts.append('<p class="note">Top ending reasons (stage the candidate died before):</p>')
        parts.append(_table(('reason', 'stage', 'candidates'), [(r, st, n) for n, st, r in deaths], 2))
    latch = d.get('latch_check') or {}
    parts.append('<p class="note">Latch check: %s</p>' % _e(latch.get('status')))
    return ''.join(parts)


def _render_counterfactual(s):
    d = s['data']
    rows = [(name, g.get('candidates'), _flag(g.get('with_return')), _pct(g.get('mean_return')), _pct(g.get('median_return')),
             _pct(g.get('share_positive')), g.get('pool_died'), g.get('baseline_missing'))
            for name, g in sorted((d.get('groups') or {}).items())]
    return ('<p class="note">Forward return at +%ss of the +5m baseline; dead pools count as -100%%. %s distinct candidates. Research only, not fill evidence.</p>'
            % (_e(d.get('horizon_seconds')), _e(d.get('distinct_candidates')))
            + _table(('outcome group', 'candidates', 'with return', 'mean', 'median', 'share positive', 'pool died', 'baseline missing'), rows))


def _render_shadow(s):
    d = s['data']
    rows = []
    for r in d.get('leaderboard', ())[:10]:
        h = r['holdout']
        ci = h.get('bootstrap_ci') or (None, None)
        mean = h.get('mean_return') or (None, None)
        rows.append((r.get('rank') or '-', r['variant'], r.get('rank_status'), _flag(h.get('trades'), 'holdout n'), _pct(h.get('ambiguous_share')),
                     '%s .. %s' % (_pct(mean[0]), _pct(mean[1])), '%s .. %s' % (_pct(ci[0]), _pct(ci[1]))))
    label = d.get('features_label') or {}
    features = ('<p class="note">Features: real features for %s of %s candidates (fields: %s); neutral features for %s.%s</p>'
                % (_e(label.get('real_candidates', 0)), _e(label.get('candidates', '?')), _e(', '.join(label.get('fields') or ()) or 'none'),
                   _e(label.get('neutral_candidates', '?')),
                   (' %s store rows dated after the price path were refused as look-ahead.' % _e(label['look_ahead_refused']))
                   if label.get('look_ahead_refused') else ''))
    return (features + '<p class="note">SIMULATED_SHADOW on the counterfactual price paths; holdout from %s; %s variants tried (Bonferroni). '
            'Candidates: %s. Mean return and CI are worst/best-case bounds over unsampled gaps.</p>'
            % (_e(d.get('holdout_from')), _e(d.get('variants_tried')), _e(json.dumps(d.get('candidates'), sort_keys=True)))
            + _table(('rank', 'variant', 'status', 'holdout trades', 'ambiguous share', 'mean return (lo..hi)', 'CI95 (lo..hi)'), rows, 3))


def _render_forward(s):
    out = []
    for row in s['data'].get('experiments', ()):
        out.append('<p><b>%s</b> config <code>%s</code></p>' % (_e(row.get('version')), _e(str(row.get('config_hash'))[:12])))
        rows = []
        for part in ('all', 'train', 'holdout'):
            b = row.get(part)
            if not b:
                continue
            ci = b.get('mean_return_bootstrap_ci95')
            rows.append((part, _flag(b.get('closed_trades'), 'trades'), b.get('wins'), b.get('losses'), _fmt(b.get('net_pnl_sol'), 6),
                         _pct(b.get('mean_return')), _pct(b.get('max_drawdown_fraction')),
                         ('%s .. %s' % (_pct(ci[0]), _pct(ci[1]))) if isinstance(ci, (list, tuple)) and len(ci) == 2 else 'n/a'))
        out.append(_table(('part', 'sample', 'wins', 'losses', 'net PnL SOL', 'mean return', 'max drawdown', 'CI95 mean return'), rows))
        out.append('<p class="note">Open positions excluded: %s. Paper only; profitability verdict NOT_ASSESSED.</p>' % _e(row.get('open_positions_excluded')))
    return ''.join(out) or '<p class="note">No experiment.</p>'


def _render_realism(s):
    d = s['data']
    if d.get('status') != 'REPORTED':
        return '<p class="note">%s: no latency measurements yet.</p>' % _e(d.get('status'))
    rows = [(delay, v.get('trades_measured'), v.get('trades_unmeasured'), _fmt(v.get('paper_pnl_sol'), 6), _fmt(v.get('latency_adjusted_pnl_sol'), 6),
             _fmt(v.get('delta_sol'), 6)) for delay, v in sorted((d.get('pnl_by_delay') or {}).items())]
    return ((('<p class="warn">%s</p>' % _e(d['coverage_warning'])) if d.get('coverage_warning') else '')
            + _table(('delay s', 'trades measured', 'unmeasured', 'paper PnL', 'latency-adjusted', 'delta'), rows))


RENDERERS = {'health': ('Health', _render_health), 'budgets': ('Budget usage', _render_budgets), 'funnel': ('Funnel: where candidates die', _render_funnel),
             'counterfactual': ('Counterfactual: do the filters reject winners?', _render_counterfactual),
             'shadow': ('Shadow strategy leaderboard', _render_shadow), 'forward': ('Forward evaluation of real paper trades', _render_forward),
             'realism': ('Fill realism (latency-adjusted)', _render_realism)}


def render_html(summary, sections):
    def lines(title, items):
        shown, rest = items[:MAX_LINES], len(items) - MAX_LINES
        more = '<li class="note">+%d more in the JSON summary</li>' % rest if rest > 0 else ''
        return '<h2>%s</h2><div class="card"><ol>%s%s</ol></div>' % (_e(title), ''.join('<li>%s</li>' % _e(i) for i in shown), more)

    body = [
        '<h1>Daily strategy research report %s</h1>' % _e(summary['date']),
        '<p class="banner">PAPER ONLY &middot; %s &middot; not trading evidence, not advice. Generated %s. Sample sizes are shown; any n below %d is flagged INSUFFICIENT_SAMPLE. '
        'Profitability verdict: NOT_ASSESSED.</p>' % (LABEL, _e(summary['generated_at']), MIN_SAMPLE),
        lines('What changed since the previous report' + (' (%s)' % summary['previous_date'] if summary.get('previous_date') else ''), summary['changed']),
        lines('What to try next', summary['next'])]
    for name in SECTIONS:
        title, render = RENDERERS[name]
        section = sections.get(name) or _not_provided('absent')
        body.append('<h2>%s</h2><div class="card">%s%s</div>' % (_e(title), _status_note(section),
                                                                  render(section) if section['status'] == 'OK' else ''))
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Daily strategy report %s</title><style>%s</style></head><body><main>%s</main></body></html>'
            % (_e(summary['date']), CSS, ''.join(body)))


# --------------------------------------------------------------------------- output
def write_exclusive(path, text):
    """Create ``path`` (0600, never following a symlink, never replacing an existing file)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def write_report(out_dir, summary, sections):
    out_dir = Path(out_dir)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise ReportError('OUT_DIR_MISSING_OR_SYMLINK')
    html_path, json_path = out_dir / (summary['date'] + '.html'), out_dir / (summary['date'] + '.json')
    if html_path.exists() or html_path.is_symlink() or json_path.exists() or json_path.is_symlink():
        raise ReportError('REPORT_EXISTS')
    text = render_html(summary, sections)
    write_exclusive(html_path, text)
    try:
        write_exclusive(json_path, json.dumps(summary, sort_keys=True, default=_jsonable, allow_nan=False) + '\n')
    except BaseException:
        os.unlink(html_path)   # never leave a day with an HTML but no summary (the next day's comparison needs the JSON)
        raise
    return html_path, json_path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--out-dir', required=True, help='existing directory; writes YYYY-MM-DD.html and YYYY-MM-DD.json (never overwritten)')
    p.add_argument('--root', help='fresh-start store root: implies the ledger/evidence/research/journal/decisions/counterfactual paths')
    p.add_argument('--discovery-db')
    p.add_argument('--pacing-db')
    p.add_argument('--ledger')
    p.add_argument('--evidence-db')
    p.add_argument('--research-db')
    p.add_argument('--journal')
    p.add_argument('--decisions-db')
    p.add_argument('--counterfactual-store')
    p.add_argument('--backup-root')
    p.add_argument('--config')
    p.add_argument('--no-systemd', action='store_true')
    p.add_argument('--grid', help='shadow grid (default config/experiments/shadow/exit-grid.json)')
    p.add_argument('--features', help='explicit T27 features file; wins over the features store')
    p.add_argument('--features-store', help='T40 features.sqlite (default with --root: <root>/features/features.sqlite)')
    p.add_argument('--holdout-from')
    p.add_argument('--min-trades', type=int)
    p.add_argument('--lookback-days', type=int, default=7)
    p.add_argument('--horizon', type=int, default=3600)
    p.add_argument('--resamples', type=int, default=1000)
    p.add_argument('--now', type=float, help='override the report time (testing)')
    args = p.parse_args(argv)
    now = time.time() if args.now is None else args.now
    opts = {k: v for k, v in vars(args).items() if v is not None and k not in ('out_dir', 'now')}
    if args.no_systemd is False:
        opts.pop('no_systemd', None)
    try:
        sections = collect(opts, now)
        summary = build(sections, now=now, previous=find_previous(args.out_dir, _day(now)),
                        source={k: bool(opts.get(k)) for k in ('root', 'discovery_db', 'ledger', 'counterfactual_store')})
        html_path, json_path = write_report(args.out_dir, summary, sections)
    except (ReportError, OSError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': str(error) if isinstance(error, ReportError) else type(error).__name__, 'label': LABEL}))
        return 2
    print(json.dumps({'status': 'WRITTEN', 'html': str(html_path), 'json': str(json_path), 'section_status': summary['section_status'],
                      'label': LABEL}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
