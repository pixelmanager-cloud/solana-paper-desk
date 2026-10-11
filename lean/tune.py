"""Walk-forward tuning report for the lean strategy. PAPER ONLY, EXECUTION_UNVERIFIED. Never promotes anything.

`tune(db, grid, split)` replays every candidate config (the base config plus the grid product) on recorded price paths
(`lean.replay`), once on the OLDER `split` share of the paths (in-sample) and once on the NEWEST remainder
(out-of-sample, an independent portfolio starting from the same cash). Configs are RANKED BY OUT-OF-SAMPLE RESULT ONLY
(total PnL, then trade count); in-sample numbers are shown for overfitting diagnosis and never used to rank. A config
with fewer than `min_oos` (default 50) out-of-sample trades is flagged INSUFFICIENT and ranked below every sufficient one.

Selection bias: ranking N configs on the same out-of-sample set makes the winner's OOS number optimistic. The result says
so; a proposal is evidence for a forward paper run under a new strategy_version, not proof.

Promotion: a proposal file `<proposals_dir>/<UTC timestamp>.json` is written only when a non-base, sufficient config beats
the base out-of-sample (total PnL and positive mean). It carries the diff against the current strategy_version and the
evidence, and is `status: PROPOSAL_NOT_APPLIED`. Nothing reads it automatically; a human or the coordinator decides.
"""
from __future__ import annotations

import argparse
import copy
import datetime
import itertools
import json
import os
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional

from lean import replay as R
from lean import strategy as S

MIN_OOS = 50
MAX_CONFIGS = 2000
PROPOSAL_STATUS = 'PROPOSAL_NOT_APPLIED'
LABEL = 'PAPER ONLY / EXECUTION_UNVERIFIED'


class TuneError(ValueError):
    pass


def config_to_dict(cfg: S.StrategyConfig) -> dict:
    """The raw JSON form `StrategyConfig.from_dict` accepts, rebuilt from a config (Decimals as strings)."""
    raw = {'strategy_version': cfg.strategy_version,
           'tp_ladder': [{'trigger': str(r.trigger), 'fraction': str(r.fraction), 'stop_ratio': str(r.stop_ratio)} for r in cfg.tp_ladder]}
    for name in S.StrategyConfig._DECIMALS:
        raw[name] = str(getattr(cfg, name))
    for name in S.StrategyConfig._INTS:
        raw[name] = getattr(cfg, name)
    return raw


def _set(raw: dict, key: str, value) -> None:
    """`key` is a top-level config name or `tp_ladder.<i>.<field>`."""
    parts = key.split('.')
    if parts[0] == 'tp_ladder' and len(parts) == 3:
        try:
            rung = raw['tp_ladder'][int(parts[1])]
            if parts[2] not in rung:
                raise KeyError(parts[2])
            rung[parts[2]] = value
        except (ValueError, IndexError, KeyError):
            raise TuneError('bad ladder key %r' % key) from None
    elif len(parts) == 1 and (key in raw) and key != 'strategy_version':
        raw[key] = value
    else:
        raise TuneError('unknown or immutable grid key %r' % key)


def _canonical(cfg: S.StrategyConfig) -> str:
    """Identity by VALUE ('0.3' and '0.30' are the same setting), unlike config_hash which hashes the raw text."""
    def norm(v):
        if isinstance(v, str):
            try:
                return str(Decimal(v).normalize())
            except Exception:
                return v
        if isinstance(v, list):
            return [{k: norm(x) for k, x in r.items()} for r in v]
        return v
    return json.dumps({k: norm(v) for k, v in config_to_dict(cfg).items()}, sort_keys=True)


def expand_grid(grid: Mapping, base: S.StrategyConfig, max_configs: int = MAX_CONFIGS):
    """-> (candidates, invalid). candidates = [{label, overrides, cfg}] starting with the base; invalid = combos that
    StrategyConfig refused (reported, never silently dropped). Duplicates of an already seen config hash are dropped."""
    if not isinstance(grid, Mapping):
        raise TuneError('grid must be an object {param: [values]}')
    keys = sorted(grid)
    for key in keys:
        if not isinstance(grid[key], (list, tuple)) or not grid[key]:
            raise TuneError('grid[%r] must be a non-empty list' % key)
    total = 1
    for key in keys:
        total *= len(grid[key])
    if total > max_configs:
        raise TuneError('grid has %d combinations (max %d)' % (total, max_configs))
    base_raw = config_to_dict(base)
    seen = {_canonical(base): 'base'}
    candidates = [{'label': 'base', 'overrides': {}, 'cfg': base}]
    invalid = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        raw = copy.deepcopy(base_raw)
        overrides = dict(zip(keys, combo))
        for key, value in overrides.items():
            _set(raw, key, value)
        label = ' '.join('%s=%s' % (k, overrides[k]) for k in keys)
        try:
            cfg = S.StrategyConfig.from_dict(raw)
        except S.ConfigError as error:
            invalid.append({'label': label, 'overrides': overrides, 'error': str(error)})
            continue
        if _canonical(cfg) in seen:
            continue
        seen[_canonical(cfg)] = label
        candidates.append({'label': label, 'overrides': overrides, 'cfg': cfg})
    return candidates, invalid


def split_paths(paths, split: float, embargo_s: float = 0):
    """Older `split` share (by candidate start time) vs the newest rest. `embargo_s` drops out-of-sample paths that start
    within that many seconds of the boundary so the two sides share no overlapping holding period."""
    if isinstance(split, bool) or not isinstance(split, (int, float)) or not 0 < split < 1:
        raise TuneError('split must be in (0, 1)')
    ordered = sorted(paths, key=lambda p: (p.start_ts, p.mint))
    k = int(len(ordered) * split)
    if k < 1 or k >= len(ordered):
        raise TuneError('need at least 2 paths so that both sides are non-empty (have %d)' % len(ordered))
    older, newer = ordered[:k], ordered[k:]
    boundary = newer[0].start_ts
    if embargo_s:
        newer = [p for p in newer if p.start_ts >= boundary + embargo_s]
        if not newer:
            raise TuneError('embargo removed every out-of-sample path')
    return older, newer, boundary


def _evaluate(paths, cfg, rcfg):
    result = R.replay(paths, cfg, rcfg)
    out = R.metrics(result.trades, rcfg.initial_cash_sol)
    out['open_at_end'] = len(result.open_at_end)
    out['entries_skipped'] = len(result.skipped_entries)
    out['anomalies'] = dict(result.anomalies)
    return out


def _total(entry) -> Decimal:
    return Decimal(entry['out_of_sample']['total_pnl_sol'])


def tune(db, grid: Mapping, split: float = 0.7, *, base: Optional[S.StrategyConfig] = None, rcfg: Optional[R.ReplayConfig] = None,
         min_oos: int = MIN_OOS, embargo_s: float = 0, now: Optional[float] = None, proposals_dir=None,
         max_configs: int = MAX_CONFIGS) -> dict:
    base = base or S.StrategyConfig.load(Path(__file__).resolve().parents[1] / 'config' / 'lean' / 'strategy-default.json')
    rcfg = rcfg or R.ReplayConfig()
    connection, owned = _connect(db)
    try:
        paths, load_skipped = R.load_paths(connection)
    finally:
        if owned:
            connection.close()
    older, newer, boundary = split_paths(paths, split, embargo_s)
    candidates, invalid = expand_grid(grid, base, max_configs)
    entries = []
    for c in candidates:
        ins = _evaluate(older, c['cfg'], rcfg)
        oos = _evaluate(newer, c['cfg'], rcfg)
        flags = ['INSUFFICIENT'] if oos['n_trades'] < min_oos else []
        entries.append({'label': c['label'], 'is_base': c['label'] == 'base', 'overrides': c['overrides'], 'config_hash': c['cfg'].config_hash,
                        'in_sample': ins, 'out_of_sample': oos, 'flags': flags})
    entries.sort(key=lambda e: ('INSUFFICIENT' in e['flags'], -_total(e), -e['out_of_sample']['n_trades'], e['label']))
    for rank, entry in enumerate(entries, 1):
        entry['rank'] = rank
    stamp = datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)
    result = {
        'label': LABEL, 'generated_at': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'base': {'strategy_version': base.strategy_version, 'config_hash': base.config_hash},
        'split': {'fraction': split, 'paths_total': len(paths), 'in_sample_paths': len(older), 'out_of_sample_paths': len(newer),
                  'boundary_ts': boundary, 'embargo_s': embargo_s},
        'min_oos_trades': min_oos, 'replay_assumptions': rcfg.to_dict(), 'load_skipped': load_skipped,
        'configs': entries, 'invalid_configs': invalid, 'warnings': _warnings(entries, older, newer, load_skipped), 'proposal': None,
        'proposal_status': None,
    }
    _maybe_propose(result, entries, base, proposals_dir, now)
    return result


def _warnings(entries, older, newer, skipped):
    out = ['Replay is a SIMULATION on synthesised constant-product quotes from recorded marks (EXECUTION_UNVERIFIED); see replay_assumptions.']
    if len(entries) > 1:
        out.append('%d configs were ranked on the same out-of-sample set: the best one is optimistic (selection bias). '
                   'Confirm any proposal with a forward paper run under a new strategy_version.' % len(entries))
    if len(newer) < 30:
        out.append('Only %d out-of-sample paths: results are noise-dominated.' % len(newer))
    if skipped:
        out.append('Some rows/paths were unusable (see load_skipped).')
    if all('INSUFFICIENT' in e['flags'] for e in entries):
        out.append('Every config has fewer than the minimum out-of-sample trades: no ranking is reliable and no proposal is emitted.')
    return out


def _maybe_propose(result, entries, base, proposals_dir, now):
    base_entry = next(e for e in entries if e['is_base'])
    best = next((e for e in entries if not e['is_base'] and 'INSUFFICIENT' not in e['flags']), None)
    if best is None:
        result['proposal_status'] = 'NO_SUFFICIENT_ALTERNATIVE'
        return
    oos, base_oos = best['out_of_sample'], base_entry['out_of_sample']
    if 'INSUFFICIENT' in base_entry['flags']:
        # a base with too few out-of-sample trades gives nothing reliable to beat
        result['proposal_status'] = 'BASE_INSUFFICIENT'
        return
    if not (_total(best) > Decimal(base_oos['total_pnl_sol']) and Decimal(oos['mean_pnl_sol']) > 0):
        result['proposal_status'] = 'NO_OUT_OF_SAMPLE_IMPROVEMENT'
        return
    if proposals_dir is None:
        result['proposal_status'] = 'WOULD_PROPOSE_NO_DIRECTORY_GIVEN'
        return
    ts = result['generated_at'].replace('-', '').replace(':', '')
    raw = config_to_dict(next(c for c in _rebuild(base, best['overrides'])))
    raw['strategy_version'] = '%s-proposal-%s' % (base.strategy_version, ts)
    S.StrategyConfig.from_dict(raw)  # a proposal must itself be a valid config
    doc = {'status': PROPOSAL_STATUS, 'label': LABEL, 'generated_at': result['generated_at'],
           'current': result['base'], 'proposed_strategy_version': raw['strategy_version'],
           'diff': {k: {'from': _get(config_to_dict(base), k), 'to': v} for k, v in best['overrides'].items()},
           'proposed_config': raw, 'evidence': {'in_sample': best['in_sample'], 'out_of_sample': oos,
                                                'base_out_of_sample': base_oos, 'split': result['split'],
                                                'configs_evaluated': len(entries), 'replay_assumptions': result['replay_assumptions']},
           'warnings': result['warnings']}
    out = Path(proposals_dir)
    if out.is_symlink():
        raise TuneError('proposals_dir must not be a symlink')
    out.mkdir(parents=True, exist_ok=True)
    path = out / ('%s.json' % ts)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + '\n')
    result['proposal'] = str(path)
    result['proposal_status'] = 'WRITTEN'


def _rebuild(base, overrides):
    raw = config_to_dict(base)
    for key, value in overrides.items():
        _set(raw, key, value)
    yield S.StrategyConfig.from_dict(raw)


def _get(raw, key):
    parts = key.split('.')
    if parts[0] == 'tp_ladder' and len(parts) == 3:
        return raw['tp_ladder'][int(parts[1])][parts[2]]
    return raw[key]


def _connect(db):
    if isinstance(db, sqlite3.Connection):
        return db, False
    from lean.report import open_ro
    return open_ro(db), True


# ------------------------------------------------------------------------------------------------- report
def render_html(result: dict, calibration: Optional[dict] = None) -> str:
    from lean.report import CSS, e, table
    parts = ['<h1>Lean tuning report</h1><p class="flag">%s</p><p>Generated %s &middot; base %s (%s)</p>' % (
        e(LABEL), e(result['generated_at']), e(result['base']['strategy_version']), e(result['base']['config_hash'][:12]))]
    sp = result['split']
    parts.append('<p>Walk-forward split: %d paths, older %d in-sample / newest %d out-of-sample (boundary %s). '
                 'Ranked by OUT-OF-SAMPLE total PnL only; INSUFFICIENT below %d OOS trades.</p>' % (
                     sp['paths_total'], sp['in_sample_paths'], sp['out_of_sample_paths'], e(sp['boundary_ts']), result['min_oos_trades']))
    parts.append('<h2>Warnings</h2><ul>%s</ul>' % ''.join('<li>%s</li>' % e(w) for w in result['warnings']))

    def cells(m):
        pf = m['profit_factor'] if m['profit_factor'] is not None else m['profit_factor_note']
        return [m['n_trades'], _pct(m['win_rate']), _num(m['mean_pnl_sol']), _num(m['median_pnl_sol']), _num(m['max_drawdown_sol']), _num(pf)]
    headers = ['rank', 'config', 'flags'] + ['IS ' + h for h in ('n', 'win', 'mean', 'median', 'max DD', 'PF')] + ['OOS ' + h for h in ('n', 'win', 'mean', 'median', 'max DD', 'PF')]
    rows = [[x['rank'], x['label'], ' '.join(x['flags']) or 'ok'] + cells(x['in_sample']) + cells(x['out_of_sample']) for x in result['configs']]
    parts.append('<h2>Configs (ranked out-of-sample)</h2>' + table(headers, rows))
    parts.append('<h2>Proposal</h2><p>%s%s</p><p>Never applied automatically.</p>' % (e(result['proposal_status']), (' &middot; ' + e(result['proposal'])) if result['proposal'] else ''))
    if result['invalid_configs']:
        parts.append('<h2>Invalid grid combinations</h2>' + table(['combination', 'error'], [[i['label'], i['error']] for i in result['invalid_configs']]))
    parts.append('<h2>Replay assumptions</h2><pre>%s</pre>' % e(json.dumps(result['replay_assumptions'], sort_keys=True)))
    if result['load_skipped']:
        parts.append('<h2>Unusable rows</h2>' + table(['reason', 'count'], sorted(result['load_skipped'].items())))
    if calibration is not None:
        parts.append('<h2>Calibration: replay vs live exit reasons</h2><p>%s &middot; matched %d of %d tokens</p>' % (
            e(calibration['status']), calibration['matched'], calibration['tokens']))
        bad = [r for r in calibration['rows'] if not r['match']]
        parts.append(table(['mint', 'live', 'replay', 'note'], [[r['mint'], ','.join(map(str, r['live_reasons'])), ','.join(map(str, r['replay_reasons'] or [])), r['note']] for r in bad])
                     if bad else '<p>No mismatches.</p>')
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Lean tuning report</title><style>%s</style></head><body><main>%s</main></body></html>' % (CSS, ''.join(parts)))


def _pct(v):
    return 'n/a' if v is None else '%.1f%%' % (v * 100)


def _num(v):
    if v is None:
        return 'n/a'
    try:
        return '%.6g' % Decimal(str(v))
    except Exception:
        return str(v)


def write_report(out_dir, result, calibration=None):
    out_dir = Path(out_dir)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise TuneError('out_dir missing or a symlink')
    stamp = result['generated_at'].replace('-', '').replace(':', '')
    html_path, json_path = out_dir / ('lean-tune-%s.html' % stamp), out_dir / ('lean-tune-%s.json' % stamp)
    from lean.report import write_exclusive
    write_exclusive(html_path, render_html(result, calibration))
    try:
        write_exclusive(json_path, json.dumps({**result, 'calibration': calibration}, sort_keys=True, allow_nan=False) + '\n')
    except BaseException:
        os.unlink(html_path)
        raise
    return html_path, json_path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--db', required=True, help='lean.sqlite (opened read-only)')
    p.add_argument('--grid', required=True, help='JSON file: {"param": [values, ...]}')
    p.add_argument('--out-dir', required=True)
    p.add_argument('--proposals-dir', help='where a proposal may be written (default: none = never write one)')
    p.add_argument('--split', type=float, default=0.7)
    p.add_argument('--min-oos', type=int, default=MIN_OOS)
    p.add_argument('--embargo-s', type=float, default=0)
    p.add_argument('--calibrate', action='store_true', help='also compare replay with the live exit reasons')
    p.add_argument('--now', type=float, required=True, help='epoch seconds used for names/timestamps (explicit: no hidden clock)')
    args = p.parse_args(argv)
    try:
        with open(args.grid, 'r', encoding='utf-8') as stream:
            grid = json.load(stream)
        result = tune(args.db, grid, args.split, min_oos=args.min_oos, embargo_s=args.embargo_s, now=args.now, proposals_dir=args.proposals_dir)
        calibration = None
        if args.calibrate:
            from lean.report import open_ro
            connection = open_ro(args.db)
            try:
                calibration = R.calibrate(connection, S.StrategyConfig.load(Path(__file__).resolve().parents[1] / 'config' / 'lean' / 'strategy-default.json'))
            finally:
                connection.close()
        html_path, json_path = write_report(args.out_dir, result, calibration)
    except (TuneError, R.ReplayError, S.ConfigError, sqlite3.Error, OSError, ValueError) as error:
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps({'status': 'OK', 'html': str(html_path), 'json': str(json_path), 'proposal': result['proposal'],
                      'calibration': calibration['status'] if calibration else None}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
