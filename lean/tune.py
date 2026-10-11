"""Walk-forward tuning report for the lean strategy. PAPER ONLY, EXECUTION_UNVERIFIED. Never promotes anything.

`tune(db, paths, grid, fractions)` replays every candidate config (the base config plus the grid product) on the recorded price
paths (`lean.replay`) in THREE windows by candidate start time, each an independent portfolio starting from the same cash:

  TRAIN   (oldest, default 50%)   shown for overfitting diagnosis
  SELECT  (middle, default 25%)   the ONLY window configs are ranked and the winner is chosen on
  CONFIRM (newest, default 25%)   untouched by the selection: it can only ACCEPT or REJECT the winner, never pick it

A config with fewer than `min_select` trades in the select window is flagged INSUFFICIENT and ranked below every sufficient one.
A winner is confirmed only when, on the confirm window, it has at least `min_confirm` trades, a positive mean PnL and a higher
total PnL than the base config; otherwise the status is NOT_CONFIRMED and nothing is proposed. The confirm numbers of the other
configs are printed for transparency and marked as not used for selection.

Selection bias still exists (N configs ranked on one select window); the confirm window removes the optimism of the winner's
number but not the multiple-comparison problem. A proposal is evidence for a forward paper run under a new strategy_version.

Promotion: a proposal file `<proposals_dir>/<UTC timestamp>.json` is written only for a confirmed non-base winner. It carries
the diff against the current strategy_version and the evidence, and is `status: PROPOSAL_NOT_APPLIED`. Nothing reads it
automatically; a human or the coordinator decides.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime
import itertools
import json
import math
import os
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional

from lean import replay as R
from lean import strategy as S

MIN_SELECT = 50
MIN_CONFIRM = 30
DEFAULT_FRACTIONS = (0.5, 0.25, 0.25)
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


REPLAY_KEYS = ('entry_timing', 'entry_delay_s')


def _canonical(cfg: S.StrategyConfig, rcfg: Optional[R.ReplayConfig] = None) -> str:
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
    body = {k: norm(v) for k, v in config_to_dict(cfg).items()}
    return json.dumps({'strategy': body, 'replay': None if rcfg is None else rcfg.to_dict()}, sort_keys=True)


def _replay_override(rcfg: R.ReplayConfig, name: str, value) -> R.ReplayConfig:
    try:
        if name == 'entry_timing':
            return dataclasses.replace(rcfg, entry_timing=R.EntryTiming.from_spec(value))
        return dataclasses.replace(rcfg, entry_delay_s=value)
    except (R.ReplayError, TypeError) as error:
        raise S.ConfigError('replay.%s: %s' % (name, error)) from None


def _label_part(key, value):
    if key == 'replay.entry_timing':
        try:
            return 'entry_timing=%s' % R.EntryTiming.from_spec(value).label
        except R.ReplayError:
            pass
    return '%s=%s' % (key, value)


def expand_grid(grid: Mapping, base: S.StrategyConfig, max_configs: int = MAX_CONFIGS, base_rcfg: Optional[R.ReplayConfig] = None):
    """-> (candidates, invalid). candidates = [{label, overrides, cfg, rcfg}] starting with the base; invalid = combos that
    StrategyConfig / ReplayConfig refused (reported, never silently dropped). Value-duplicates of a seen setting are dropped.
    Grid keys are strategy config names, `tp_ladder.<i>.<field>`, or `replay.entry_timing` / `replay.entry_delay_s`
    (entry-timing variants: 'first_mark', {'kind': 'pullback', 'pullback_pct': '0.1'}, {'kind': 'momentum', 'momentum_minutes': 3})."""
    base_rcfg = base_rcfg or R.ReplayConfig()
    if not isinstance(grid, Mapping):
        raise TuneError('grid must be an object {param: [values]}')
    keys = sorted(grid)
    for key in keys:
        if not isinstance(grid[key], (list, tuple)) or not grid[key]:
            raise TuneError('grid[%r] must be a non-empty list' % key)
        if key.startswith('replay.') and key[7:] not in REPLAY_KEYS:
            raise TuneError('unknown replay grid key %r (allowed: %s)' % (key, ', '.join('replay.' + k for k in REPLAY_KEYS)))
    total = 1
    for key in keys:
        total *= len(grid[key])
    if total > max_configs:
        raise TuneError('grid has %d combinations (max %d)' % (total, max_configs))
    base_raw = config_to_dict(base)
    seen = {_canonical(base, base_rcfg): 'base'}
    candidates = [{'label': 'base', 'overrides': {}, 'cfg': base, 'rcfg': base_rcfg}]
    invalid = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        raw = copy.deepcopy(base_raw)
        overrides = dict(zip(keys, combo))
        label = ' '.join(_label_part(k, overrides[k]) for k in keys)
        rcfg = base_rcfg
        try:
            for key, value in overrides.items():
                if key.startswith('replay.'):
                    rcfg = _replay_override(rcfg, key[7:], value)
                else:
                    _set(raw, key, value)
            cfg = S.StrategyConfig.from_dict(raw)
        except S.ConfigError as error:
            invalid.append({'label': label, 'overrides': overrides, 'error': str(error)})
            continue
        if _canonical(cfg, rcfg) in seen:
            continue
        seen[_canonical(cfg, rcfg)] = label
        candidates.append({'label': label, 'overrides': overrides, 'cfg': cfg, 'rcfg': rcfg})
    return candidates, invalid


def split_paths(paths, fractions=DEFAULT_FRACTIONS, embargo_s: float = 0):
    """Train / select / confirm by candidate start time (oldest first). `fractions` are three positive numbers summing to 1.
    `embargo_s` drops select and confirm paths that start within that many seconds of the window's start boundary so that no two
    windows share an overlapping holding period. Returns (train, select, confirm, boundaries) with boundaries = (select_start,
    confirm_start)."""
    if not isinstance(fractions, (tuple, list)) or len(fractions) != 3 or any(isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0 for f in fractions) \
            or abs(sum(fractions) - 1) > 1e-9:
        raise TuneError('fractions must be three positive numbers summing to 1')
    ordered = sorted(paths, key=lambda p: (p.start_ts, p.mint))
    n = len(ordered)
    a, b = int(n * fractions[0]), int(n * (fractions[0] + fractions[1]))
    train, select, confirm = ordered[:a], ordered[a:b], ordered[b:]
    if not (train and select and confirm):
        raise TuneError('need at least 3 paths so that all three windows are non-empty (have %d)' % n)
    bounds = (select[0].start_ts, confirm[0].start_ts)
    if embargo_s:
        select = [p for p in select if p.start_ts >= bounds[0] + embargo_s]
        confirm = [p for p in confirm if p.start_ts >= bounds[1] + embargo_s]
        if not select or not confirm:
            raise TuneError('embargo removed every select or confirm path')
    return train, select, confirm, bounds


def _evaluate(paths, cfg, rcfg):
    result = R.replay(paths, cfg, rcfg)
    out = R.metrics(result.trades, rcfg.initial_cash_sol)
    out['open_at_end'] = len(result.open_at_end)
    out['entries_skipped'] = len(result.skipped_entries)
    out['anomalies'] = dict(result.anomalies)
    return out


def _total(entry, window='select') -> Decimal:
    return Decimal(entry[window]['total_pnl_sol'])


def tune(db, paths, grid: Mapping, fractions=DEFAULT_FRACTIONS, *, base: Optional[S.StrategyConfig] = None, rcfg: Optional[R.ReplayConfig] = None,
         min_select: int = MIN_SELECT, min_confirm: int = MIN_CONFIRM, embargo_s: float = 0, now: Optional[float] = None, proposals_dir=None,
         max_configs: int = MAX_CONFIGS) -> dict:
    """`db` = the live trader's lean.sqlite (only its starting cash is read); `paths` = the recorder's paths.sqlite file(s) or open
    connection(s) (rolled-over archives first)."""
    base = base or S.StrategyConfig.load(Path(__file__).resolve().parents[1] / 'config' / 'lean' / 'strategy-default.json')
    live, live_owned = _connect(db)
    try:
        # the replay starts from the cash the live store started from unless the caller says otherwise
        rcfg = rcfg or R.ReplayConfig(initial_cash_sol=R.store_initial_cash_sol(live) or R.ReplayConfig().initial_cash_sol)
    finally:
        if live_owned:
            live.close()
    connections, owned = _path_connections(paths)
    try:
        loaded, load_skipped = R.load_paths(connections)
    finally:
        if owned:
            for connection in connections:
                connection.close()
    train, select, confirm, bounds = split_paths(loaded, fractions, embargo_s)
    candidates, invalid = expand_grid(grid, base, max_configs, rcfg)
    entries = []
    for c in candidates:
        tr, se, co = (_evaluate(window, c['cfg'], c['rcfg']) for window in (train, select, confirm))
        flags = ['INSUFFICIENT'] if se['n_trades'] < min_select else []
        entries.append({'label': c['label'], 'is_base': c['label'] == 'base', 'overrides': c['overrides'], 'config_hash': c['cfg'].config_hash,
                        'replay': c['rcfg'].to_dict(), 'train': tr, 'select': se, 'confirm': co, 'flags': flags,
                        'confirm_used_for_selection': False})
    entries.sort(key=lambda e: ('INSUFFICIENT' in e['flags'], -_total(e), -e['select']['n_trades'], e['label']))
    for rank, entry in enumerate(entries, 1):
        entry['rank'] = rank
    stamp = datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc)
    result = {
        'label': LABEL, 'generated_at': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'base': {'strategy_version': base.strategy_version, 'config_hash': base.config_hash},
        'split': {'fractions': list(fractions), 'paths_total': len(loaded), 'train_paths': len(train), 'select_paths': len(select),
                  'confirm_paths': len(confirm), 'select_start_ts': bounds[0], 'confirm_start_ts': bounds[1], 'embargo_s': embargo_s},
        'min_select_trades': min_select, 'min_confirm_trades': min_confirm, 'replay_assumptions': rcfg.to_dict(), 'load_skipped': load_skipped,
        'configs': entries, 'invalid_configs': invalid, 'warnings': _warnings(entries, select, confirm, load_skipped), 'proposal': None,
        'proposal_status': None, 'winner': None,
    }
    _maybe_propose(result, entries, base, proposals_dir, now, min_confirm)
    return result


def _path_connections(paths):
    """-> (connections, owned). Accepts a path, a connection, or a list of those."""
    items = list(paths) if isinstance(paths, (list, tuple)) else [paths]
    if not items:
        raise TuneError('no paths database given')
    if all(isinstance(i, sqlite3.Connection) for i in items):
        return items, False
    return R.open_paths(*items), True


def _warnings(entries, select, confirm, skipped):
    out = ['Replay is a SIMULATION on synthesised constant-product quotes from recorded vault amounts (EXECUTION_UNVERIFIED); see replay_assumptions.']
    if len(entries) > 1:
        out.append('%d configs were ranked on the same select window: the best one is optimistic (selection bias). The confirm window only '
                   'accepts or rejects the winner; confirm any proposal with a forward paper run under a new strategy_version.' % len(entries))
    if len(select) < 30 or len(confirm) < 30:
        out.append('Only %d select / %d confirm paths: results are noise-dominated.' % (len(select), len(confirm)))
    if skipped:
        out.append('Some rows/paths were unusable (see load_skipped).')
    if all('INSUFFICIENT' in e['flags'] for e in entries):
        out.append('Every config has fewer than the minimum select-window trades: no ranking is reliable and no proposal is emitted.')
    return out


def _maybe_propose(result, entries, base, proposals_dir, now, min_confirm):
    base_entry = next(e for e in entries if e['is_base'])
    best = next((e for e in entries if not e['is_base'] and 'INSUFFICIENT' not in e['flags']), None)
    if best is None:
        result['proposal_status'] = 'NO_SUFFICIENT_ALTERNATIVE'
        return
    if 'INSUFFICIENT' in base_entry['flags']:
        # a base with too few select-window trades gives nothing reliable to beat
        result['proposal_status'] = 'BASE_INSUFFICIENT'
        return
    result['winner'] = best['label']                                  # chosen on the SELECT window alone
    if not (_total(best) > _total(base_entry) and Decimal(best['select']['mean_pnl_sol']) > 0):
        result['proposal_status'] = 'NO_SELECTION_IMPROVEMENT'
        return
    co, base_co = best['confirm'], base_entry['confirm']
    if co['n_trades'] < min_confirm or co['mean_pnl_sol'] is None or Decimal(co['mean_pnl_sol']) <= 0 or _total(best, 'confirm') <= _total(base_entry, 'confirm'):
        result['proposal_status'] = 'NOT_CONFIRMED'                    # the untouched window does not support the winner
        return
    if proposals_dir is None:
        result['proposal_status'] = 'WOULD_PROPOSE_NO_DIRECTORY_GIVEN'
        return
    ts = result['generated_at'].replace('-', '').replace(':', '')
    strategy_overrides = {k: v for k, v in best['overrides'].items() if not k.startswith('replay.')}
    replay_overrides = {k: v for k, v in best['overrides'].items() if k.startswith('replay.')}
    raw = config_to_dict(next(c for c in _rebuild(base, strategy_overrides)))
    raw['strategy_version'] = '%s-proposal-%s' % (base.strategy_version, ts)
    S.StrategyConfig.from_dict(raw)  # a proposal must itself be a valid config
    doc = {'status': PROPOSAL_STATUS, 'label': LABEL, 'generated_at': result['generated_at'],
           'current': result['base'], 'proposed_strategy_version': raw['strategy_version'],
           'diff': {k: {'from': _get(config_to_dict(base), k), 'to': v} for k, v in strategy_overrides.items()},
           'replay_overrides': replay_overrides,
           'requires_runner_change': sorted(replay_overrides),  # entry timing is not a runner setting yet: code change needed
           'proposed_config': raw, 'evidence': {'train': best['train'], 'select': best['select'], 'confirm': co,
                                                'base_select': base_entry['select'], 'base_confirm': base_co, 'split': result['split'],
                                                'configs_evaluated': len(entries), 'replay_assumptions': result['replay_assumptions'],
                                                'chosen_on': 'select', 'confirmed_on': 'confirm'},
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
    parts.append('<p>Walk-forward split: %d paths = %d train / %d select / %d confirm (select starts %s, confirm starts %s). '
                 'Ranked and the winner chosen on the SELECT window only; the CONFIRM window can only accept or reject the winner '
                 '(at least %d trades, positive mean, more than the base). INSUFFICIENT below %d select trades.</p>' % (
                     sp['paths_total'], sp['train_paths'], sp['select_paths'], sp['confirm_paths'], e(sp['select_start_ts']), e(sp['confirm_start_ts']),
                     result['min_confirm_trades'], result['min_select_trades']))
    parts.append('<h2>Warnings</h2><ul>%s</ul>' % ''.join('<li>%s</li>' % e(w) for w in result['warnings']))

    def cells(m):
        pf = m['profit_factor'] if m['profit_factor'] is not None else m['profit_factor_note']
        return [m['n_trades'], _pct(m['win_rate']), _num(m['mean_pnl_sol']), _num(m['median_pnl_sol']), _num(m['max_drawdown_sol']), _num(pf)]
    headers = ['rank', 'config', 'flags'] + [w + ' ' + h for w in ('TRAIN', 'SELECT', 'CONFIRM*') for h in ('n', 'win', 'mean', 'median', 'max DD', 'PF')]
    rows = [[x['rank'], x['label'], ' '.join(x['flags']) or 'ok'] + cells(x['train']) + cells(x['select']) + cells(x['confirm']) for x in result['configs']]
    parts.append('<h2>Configs (ranked on the select window)</h2>' + table(headers, rows)
                 + '<p>* the confirm window is NOT used for ranking or for choosing the winner.</p>')
    parts.append('<h2>Proposal</h2><p>%s%s%s</p><p>Never applied automatically.</p>' % (
        e(result['proposal_status']), (' &middot; winner on select: ' + e(result['winner'])) if result['winner'] else '',
        (' &middot; ' + e(result['proposal'])) if result['proposal'] else ''))
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
    p.add_argument('--db', required=True, help="the live trader's lean.sqlite (opened read-only; only its starting cash and exit reasons are read)")
    p.add_argument('--paths-db', required=True, action='append', help="the recorder's paths.sqlite; repeat for rolled-over archives (oldest first)")
    p.add_argument('--grid', required=True, help='JSON file: {"param": [values, ...]}')
    p.add_argument('--out-dir', required=True)
    p.add_argument('--proposals-dir', help='where a proposal may be written (default: none = never write one)')
    p.add_argument('--fractions', default='0.5,0.25,0.25', help='train,select,confirm shares of the paths by start time')
    p.add_argument('--min-select', type=int, default=MIN_SELECT)
    p.add_argument('--min-confirm', type=int, default=MIN_CONFIRM)
    p.add_argument('--embargo-s', type=float, default=0)
    p.add_argument('--pool-fee-bps', type=int, default=30, help='pool fee assumed when synthesising quotes (the runner config\'s pool_fee_bps)')
    p.add_argument('--calibrate', action='store_true', help='also compare replay with the live exit reasons')
    p.add_argument('--now', type=float, required=True, help='epoch seconds used for names/timestamps (explicit: no hidden clock)')
    args = p.parse_args(argv)
    try:
        with open(args.grid, 'r', encoding='utf-8') as stream:
            grid = json.load(stream)
        try:
            fractions = tuple(float(x) for x in args.fractions.split(','))
        except ValueError:
            raise TuneError('fractions must be three numbers like 0.5,0.25,0.25') from None
        rcfg = R.ReplayConfig(pool_fee_bps=args.pool_fee_bps)
        result = tune(args.db, args.paths_db, grid, fractions, rcfg=rcfg, min_select=args.min_select, min_confirm=args.min_confirm,
                      embargo_s=args.embargo_s, now=args.now, proposals_dir=args.proposals_dir)
        calibration = None
        if args.calibrate:
            from lean.report import open_ro
            connection = open_ro(args.db)
            path_connections = R.open_paths(*args.paths_db)
            try:
                calibration = R.calibrate(connection, path_connections,
                                          S.StrategyConfig.load(Path(__file__).resolve().parents[1] / 'config' / 'lean' / 'strategy-default.json'),
                                          R.ReplayConfig(initial_cash_sol=R.store_initial_cash_sol(connection) or R.ReplayConfig().initial_cash_sol, pool_fee_bps=args.pool_fee_bps))
            finally:
                connection.close()
                for c in path_connections:
                    c.close()
        html_path, json_path = write_report(args.out_dir, result, calibration)
    except (TuneError, R.ReplayError, S.ConfigError, sqlite3.Error, OSError, ValueError) as error:
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps({'status': 'OK', 'html': str(html_path), 'json': str(json_path), 'proposal': result['proposal'],
                      'calibration': calibration['status'] if calibration else None}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
