"""Selection learner (L19): what did winners and rugs look like AT ENTRY TIME, and which simple screen rule would have kept the trader
out of the rugs. PAPER ONLY, offline, read-only, deterministic (no clock, no randomness). Nothing here trades, calls a provider or
changes a config: the output is a REPORT and, at most, a proposal file that is NEVER applied automatically.

Inputs (all read-only):
  * ``paths.sqlite`` (``lean.paths``, the recorder; several files = the rolled-over archives, oldest first): the marks of every
    hazard-free candidate, entered or not. ``lean.labels`` turns each path into ONE label (RUG / WIN / LOSS / FLAT) with the live
    mark function; ``lean.replay`` replays the LIVE exit rules on it (first-mark entry) to give a per-candidate PnL.
  * ``lean.sqlite``: the ``observations(kind='features')`` rows (``lean.features``, one per screened candidate).

No lookahead: a feature row is used for a candidate only when it was decided AND collected no later than the path's start
(= the decision time) plus ``max_lag_s`` (the enrichment calls run a few seconds after the screen; default 300 s). A row whose screen
time is after the path's start, or that was collected later than that, is dropped and counted (``FEATURES_AFTER_DECISION``);
nothing from the marks ever becomes a feature. A feature that is unknown for a candidate never matches a rule condition.

Learner (interpretable only; stdlib):
  * per-feature quantile tables: n, rug rate, win rate (each with a Wilson 95% interval), mean forward return and mean simulated PnL
    per bucket; a bucket / feature below its minimum is flagged INSUFFICIENT and must not be read as a finding;
  * "winners vs rugs": the median of each feature per label;
  * a greedy rule miner for 1- and 2-condition REJECT rules (``feature > t`` / ``feature <= t``, ``t`` a quantile of the training
    values). The objective is the expected simulated PnL per candidate under the live exit rules: rejecting a candidate replaces its
    PnL by 0, so a rule's gain is ``-(sum of the rejected PnL) / n_candidates``. Candidates are the ones the current strategy would
    enter (non-enterable ones cannot be improved by a screen rule).
  * walk-forward by time: MINE on the oldest window, SELECT on the middle one, CONFIRM on the newest. The select window chooses the
    rules (each needs ``min_n`` rejected candidates, a positive gain and a one-sided t-test on the rejected PnL); the confirm window
    can only accept or reject the SET as a whole (``min_confirm`` rejected, positive gain, stricter t-test): it never chooses.

A confirmed set is written to ``config/lean/proposals/<ts>-selection.json`` (never overwritten) with the rule list, its diff against
the current screen and the evidence of all three windows. It is a proposal: a new screen / strategy version needs the coordinator's
review and CK's OK. Rules on features collected after the screen need the runner to wait for that enrichment (``requires_runner_change``).
"""
import argparse
import datetime
import html
import json
import math
import os
import sqlite3
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from lean import features as F, labels as LB, replay as R, strategy as S

SELECTION_VERSION = 1
PROPOSAL_STATUS = 'SELECTION_PROPOSAL_NOT_APPLIED'
LABEL = 'PAPER ONLY / EXECUTION_UNVERIFIED'
MIN_N = 50                                # rejected candidates a rule needs in the mine and the select windows
MIN_CONFIRM = 30                          # ... and in the confirm window
MIN_BUCKET = 20
DEFAULT_FRACTIONS = (0.5, 0.25, 0.25)
Z_SELECT = 1.645                          # one-sided 5%: the rejected PnL must be below zero on the select window
Z_CONFIRM = 2.326                         # one-sided 1% on the confirm window
MAX_RULES = 3
MAX_REJECT_FRACTION = 0.8                 # a screen rule that rejects more than this share of the candidates is "stop trading", not selection
TOP_CANDIDATES = 25
PAIR_SEEDS = 10
FEATURE_NAMES = ('top10_pct', 'top1_pct', 'dev_holding_pct', 'seconds_graduation_to_screen', 'tx_count_first_10m', 'market_cap_usd',
                 'liquidity_usd', 'age_seconds_at_screen', 'transfer_fee_bps')
# features known from the screen itself; the others come from the extra calls that run a few seconds later (lean.features)
SCREEN_TIME_FEATURES = frozenset({'market_cap_usd', 'liquidity_usd', 'age_seconds_at_screen', 'transfer_fee_bps'})
DEFAULT_MAX_LAG_S = 300.0
EPS_S = 1.0
ROOT = Path(__file__).resolve().parents[1]


class LearnError(ValueError):
    pass


# ----------------------------------------------------------------------------------------------------------- statistics
def wilson(k, n, z=1.96):
    """The Wilson score interval of k successes in n trials (None, None without trials)."""
    if n <= 0:
        return None, None
    p = k / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return round(max(0.0, (centre - spread) / denominator), 6), round(min(1.0, (centre + spread) / denominator), 6)


def _mean_sd(values):
    n = len(values)
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    return mean, math.sqrt(var)


def t_statistic(values):
    """mean / standard error of ``values`` (None with fewer than 2 values; -1e9 when every value is the same negative number)."""
    if len(values) < 2:
        return None
    mean, sd = _mean_sd(values)
    if sd == 0:
        return -1e9 if mean < 0 else (1e9 if mean > 0 else 0.0)
    return mean / (sd / math.sqrt(len(values)))


def _to_decimal(value) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def _sol(lamports):
    return float(Decimal(lamports) / Decimal(10 ** 9))


# ----------------------------------------------------------------------------------------------------------- dataset
@dataclass(frozen=True)
class Row:
    mint: str
    ts: float                    # the decision time = the path's start
    label: LB.Label
    features: dict               # {name: Decimal | None}, only what was known at the decision (+ lag)
    pnl_lamports: int            # the live exit rules replayed on the path (first-mark entry); 0 when not enterable
    enterable: bool              # the current strategy would have entered this candidate
    trade_status: Optional[str] = None


@dataclass
class Dataset:
    rows: list
    stats: dict


def read_feature_rows(connection) -> tuple:
    """-> ({mint: [row dicts oldest first]}, skipped). The payload is read from the row's ``meta`` (it holds the same JSON as ``raw``)."""
    out, skipped = {}, {}
    try:
        cursor = connection.execute("SELECT id, ts, meta FROM observations WHERE kind='features' ORDER BY id")
    except sqlite3.Error:
        return out, {'FEATURES_NO_TABLE': 1}
    for _id, ts, meta in cursor:
        try:
            row = json.loads(meta)
            mint, fields = row['mint'], row['fields']
            if not isinstance(mint, str) or not mint or not isinstance(fields, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            skipped['FEATURES_UNREADABLE'] = skipped.get('FEATURES_UNREADABLE', 0) + 1
            continue
        screen_at, collected_at = _to_decimal(row.get('screen_at')), _to_decimal(row.get('collected_at'))
        row = dict(row, _ts=ts, _screen_at=float(screen_at) if screen_at is not None else ts,
                   _collected_at=float(collected_at) if collected_at is not None else ts)
        out.setdefault(mint, []).append(row)
    return out, skipped


def features_known_at(rows, decision_ts, names, max_lag_s=DEFAULT_MAX_LAG_S):
    """The last feature row of a mint that was screened no later than the decision and collected no later than it plus
    ``max_lag_s``, as ``{name: Decimal|None}``; (None, reason) when there is none. This is THE lookahead guard."""
    usable = [r for r in rows if r['_screen_at'] <= decision_ts + EPS_S and r['_collected_at'] <= decision_ts + max_lag_s]
    dropped = len(rows) - len(usable)
    if not usable:
        return None, 'FEATURES_AFTER_DECISION' if rows else 'NO_FEATURES_ROW', dropped
    fields = usable[-1]['fields']
    return {name: _to_decimal(fields.get(name)) for name in names}, None, dropped


def _connections(items):
    if not isinstance(items, (list, tuple)):
        items = [items]
    if not items:
        raise LearnError('no paths database given')
    if all(isinstance(i, sqlite3.Connection) for i in items):
        return list(items), False
    return R.open_paths(*items), True


def build_dataset(live_db, paths_dbs, strategy: S.StrategyConfig, *, rcfg=None, label_cfg=None, feature_names=FEATURE_NAMES,
                  max_lag_s=DEFAULT_MAX_LAG_S, pool_fee_bps=LB.DEFAULT_POOL_FEE_BPS, since=None, until=None) -> Dataset:
    """Label every recorded path, join the features known at its decision time and replay the live exit rules on it."""
    from lean.report import open_ro
    label_cfg = label_cfg or LB.LabelConfig.from_strategy(strategy, pool_fee_bps)
    feature_names = tuple(feature_names)
    live, live_owned = (live_db, False) if isinstance(live_db, sqlite3.Connection) else (open_ro(live_db), True)
    connections, owned = _connections(paths_dbs)
    try:
        rcfg = rcfg or R.ReplayConfig(initial_cash_sol=R.store_initial_cash_sol(live) or R.ReplayConfig().initial_cash_sol,
                                      pool_fee_bps=label_cfg.pool_fee_bps)
        paths, skipped = R.load_paths(connections, since=since, until=until)
        refs = LB.references(connections)
        feature_rows, feature_skipped = read_feature_rows(live)
    finally:
        if live_owned:
            live.close()
        if owned:
            for c in connections:
                c.close()
    skipped = dict(skipped)
    for name, n in feature_skipped.items():
        skipped[name] = skipped.get(name, 0) + n
    labels, label_skipped = LB.label_paths(paths, refs, label_cfg)
    for name, n in label_skipped.items():
        skipped[name] = skipped.get(name, 0) + n
    rows = []
    for path in paths:
        label = labels.get(path.mint)
        if label is None:
            continue
        known, why, dropped = features_known_at(feature_rows.get(path.mint, []), path.start_ts, feature_names, max_lag_s)
        if dropped:
            skipped['FEATURES_AFTER_DECISION'] = skipped.get('FEATURES_AFTER_DECISION', 0) + dropped
        if known is None:
            skipped[why] = skipped.get(why, 0) + (0 if why == 'FEATURES_AFTER_DECISION' else 1)
            continue
        result = R.replay([path], strategy, rcfg)
        trade = result.trades[0] if result.trades else None
        rows.append(Row(path.mint, path.start_ts, label, known, trade.realized_lamports if trade else 0, trade is not None,
                        trade.status if trade else None))
    rows.sort(key=lambda r: (r.ts, r.mint))
    return Dataset(rows, {'paths': len(paths), 'labelled': len(labels), 'with_features': len(rows),
                          'enterable': sum(1 for r in rows if r.enterable), 'skipped': dict(sorted(skipped.items())),
                          'label_config': label_cfg.to_dict(), 'replay_assumptions': rcfg.to_dict(), 'max_lag_s': max_lag_s,
                          'feature_names': list(feature_names)})


# ----------------------------------------------------------------------------------------------------------- windows
def split_rows(rows, fractions=DEFAULT_FRACTIONS, embargo_s: float = 0):
    """Mine / select / confirm by decision time, oldest first. ``embargo_s`` drops select and confirm rows that start within that many
    seconds of the window's start so that no holding period crosses a boundary. -> (mine, select, confirm, (select_start, confirm_start))."""
    if not isinstance(fractions, (tuple, list)) or len(fractions) != 3 or any(isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0 for f in fractions) \
            or abs(sum(fractions) - 1) > 1e-9:
        raise LearnError('fractions must be three positive numbers summing to 1')
    if isinstance(embargo_s, bool) or not isinstance(embargo_s, (int, float)) or not math.isfinite(embargo_s) or embargo_s < 0:
        raise LearnError('embargo_s must be a finite number >= 0')
    ordered = sorted(rows, key=lambda r: (r.ts, r.mint))
    n = len(ordered)
    a, b = int(n * fractions[0]), int(n * (fractions[0] + fractions[1]))
    mine, select, confirm = ordered[:a], ordered[a:b], ordered[b:]
    if not (mine and select and confirm):
        raise LearnError('need at least 3 candidates so that all three windows are non-empty (have %d)' % n)
    bounds = (select[0].ts, confirm[0].ts)
    if embargo_s:
        select = [r for r in select if r.ts >= bounds[0] + embargo_s]
        confirm = [r for r in confirm if r.ts >= bounds[1] + embargo_s]
        if not select or not confirm:
            raise LearnError('embargo removed every select or confirm candidate')
    return mine, select, confirm, bounds


# ----------------------------------------------------------------------------------------------------------- rules
@dataclass(frozen=True)
class Condition:
    feature: str
    op: str                      # '>' or '<='
    value: Decimal

    def holds(self, row) -> bool:
        v = row.features.get(self.feature)
        if v is None:
            return False                      # an unknown value never matches
        return v > self.value if self.op == '>' else v <= self.value

    def text(self):
        return '%s %s %s' % (self.feature, self.op, format(self.value.normalize(), 'f'))

    def to_dict(self):
        return {'feature': self.feature, 'op': self.op, 'value': format(self.value.normalize(), 'f')}


@dataclass(frozen=True)
class Rule:
    conditions: tuple

    def matches(self, row) -> bool:
        return all(c.holds(row) for c in self.conditions)

    def text(self):
        return ' AND '.join(c.text() for c in self.conditions)

    def key(self):
        return (len(self.conditions), self.text())

    def to_dict(self):
        return {'if': [c.to_dict() for c in self.conditions], 'action': 'REJECT', 'text': self.text()}


def rejected(rows, rules):
    return [r for r in rows if any(rule.matches(r) for rule in rules)]


def evaluate(rows, rules) -> dict:
    """What the rule set would have done on ``rows`` (the candidates the strategy would enter): rejected ones earn 0 instead of their
    simulated PnL. ``gain_per_candidate_sol`` > 0 means the rules beat entering everything."""
    n = len(rows)
    hits = rejected(rows, rules)
    hit_ids = {id(r) for r in hits}
    kept = [r for r in rows if id(r) not in hit_ids]
    total = sum(r.pnl_lamports for r in rows)
    rej_total = sum(r.pnl_lamports for r in hits)
    pnl = [_sol(r.pnl_lamports) for r in hits]
    z = t_statistic(pnl)

    def rate(group, name):
        return round(sum(1 for r in group if r.label.label == name) / len(group), 6) if group else None
    return {'n': n, 'n_rejected': len(hits), 'n_kept': len(kept),
            'baseline_mean_pnl_sol': round(_sol(total) / n, 9) if n else None,
            'policy_mean_pnl_sol': round(_sol(total - rej_total) / n, 9) if n else None,
            'gain_per_candidate_sol': round(-_sol(rej_total) / n, 9) if n else None,
            'rejected_mean_pnl_sol': round(_sol(rej_total) / len(hits), 9) if hits else None,
            'rejected_t': None if z is None else round(z, 4),
            'rug_rate_rejected': rate(hits, 'RUG'), 'rug_rate_kept': rate(kept, 'RUG'),
            'win_rate_rejected': rate(hits, 'WIN'), 'win_rate_kept': rate(kept, 'WIN')}


def _thresholds(values, count=9):
    ordered = sorted(values)
    n = len(ordered)
    if n < 2:
        return []
    return sorted({ordered[min(n - 1, max(0, n * i // (count + 1)))] for i in range(1, count + 1)})


def mine(rows, feature_names, *, min_n=MIN_N, top_k=TOP_CANDIDATES, pair_seeds=PAIR_SEEDS, thresholds=9, max_reject_fraction=MAX_REJECT_FRACTION):
    """Greedy 1- then 2-condition REJECT rules on ``rows`` (the mine window, enterable candidates), best first by the training gain.
    A rule needs ``min_n`` rejected candidates, a positive gain and must keep at least ``1 - max_reject_fraction`` of them; a pair is kept
    only if it beats BOTH of its single conditions (an extra condition that adds nothing is noise)."""
    n = len(rows)
    if n == 0:
        return []
    conds, hits = [], {}
    for name in feature_names:
        for t in _thresholds([r.features[name] for r in rows if r.features.get(name) is not None], thresholds):
            for op in ('>', '<='):
                c = Condition(name, op, t)
                idx = frozenset(i for i, r in enumerate(rows) if c.holds(r))
                if idx:
                    conds.append(c)
                    hits[c] = idx
    pnl = [r.pnl_lamports for r in rows]

    def total(idx):
        return sum(pnl[i] for i in idx)
    found = {}
    for c in conds:
        idx = hits[c]
        if len(idx) >= min_n and len(idx) <= max_reject_fraction * n and total(idx) < 0:
            found[Rule((c,))] = (total(idx), idx)
    singles = sorted(found.items(), key=lambda kv: (kv[1][0], kv[0].key()))
    for rule, (single_total, _idx) in singles[:pair_seeds]:
        first = rule.conditions[0]
        for c in conds:
            if c.feature == first.feature:
                continue
            idx = hits[first] & hits[c]
            if len(idx) >= min_n and len(idx) <= max_reject_fraction * n and total(idx) < min(single_total, total(hits[c])):
                pair = Rule(tuple(sorted((first, c), key=lambda x: (x.feature, x.op, x.value))))
                if pair not in found:
                    found[pair] = (total(idx), idx)
    ranked = sorted(found.items(), key=lambda kv: (kv[1][0], kv[0].key()))
    return [(rule, {'train_rejected': len(idx), 'train_gain_per_candidate_sol': round(-_sol(t) / n, 9)}) for rule, (t, idx) in ranked[:top_k]]


def select_rules(candidates, select_rows, *, min_n=MIN_N, z_select=Z_SELECT, max_rules=MAX_RULES, max_reject_fraction=MAX_REJECT_FRACTION):
    """Choose from the mined candidates on the SELECT window only. Returns (chosen rules, per-candidate evidence rows)."""
    evidence = []
    for rule, mined in candidates:
        metrics = evaluate(select_rows, [rule])
        z = metrics['rejected_t']
        flags = []
        if metrics['n_rejected'] < min_n:
            flags.append('INSUFFICIENT')
        if metrics['n_rejected'] > max_reject_fraction * len(select_rows):
            flags.append('REJECTS_TOO_MUCH')
        if not (metrics['gain_per_candidate_sol'] or 0) > 0:
            flags.append('NO_GAIN')
        if z is None or z > -z_select:
            flags.append('NOT_SIGNIFICANT')
        evidence.append({'rule': rule, 'mined': mined, 'select': metrics, 'flags': flags})
    eligible = sorted((e for e in evidence if not e['flags']), key=lambda e: (-e['select']['gain_per_candidate_sol'], e['rule'].key()))
    chosen = []
    marginal_min = max(1, min_n // 2)
    for e in eligible:
        if len(chosen) >= max_rules:
            break
        if chosen:
            before, after = evaluate(select_rows, chosen), evaluate(select_rows, chosen + [e['rule']])
            if after['n_rejected'] - before['n_rejected'] < marginal_min or after['gain_per_candidate_sol'] <= before['gain_per_candidate_sol']:
                continue
        chosen.append(e['rule'])
    return chosen, evidence


def confirmed(metrics, *, min_confirm=MIN_CONFIRM, z_confirm=Z_CONFIRM):
    """Does the untouched window support the chosen SET as a whole? (It can never pick or drop an individual rule.)"""
    z = metrics['rejected_t']
    return (metrics['n_rejected'] >= min_confirm and (metrics['gain_per_candidate_sol'] or 0) > 0 and z is not None and z <= -z_confirm)


# ----------------------------------------------------------------------------------------------------------- tables
def _bucket_row(group, enterable_only_pnl=True):
    rows = [r for _, r in group]
    n = len(rows)
    rugs, wins = sum(1 for r in rows if r.label.label == 'RUG'), sum(1 for r in rows if r.label.label == 'WIN')
    ent = [r for r in rows if r.enterable]
    return {'lo': str(group[0][0]), 'hi': str(group[-1][0]), 'n': n, 'rug_rate': round(rugs / n, 6), 'rug_ci': list(wilson(rugs, n)),
            'win_rate': round(wins / n, 6), 'win_ci': list(wilson(wins, n)),
            'mean_forward_return': round(sum(r.label.end_return for r in rows) / n, 6),
            'n_enterable': len(ent), 'mean_sim_pnl_sol': round(sum(_sol(r.pnl_lamports) for r in ent) / len(ent), 9) if ent else None}


def quantile_tables(rows, feature_names, *, buckets=5, min_bucket=MIN_BUCKET):
    """Per feature: quantile buckets (equal values share one) of n, rug / win rate with Wilson intervals, mean forward return and mean
    simulated PnL. ``insufficient`` flags the buckets (and the feature) that are too small to read as a finding."""
    out = {}
    for name in feature_names:
        pts = sorted(((r.features[name], r) for r in rows if r.features.get(name) is not None), key=lambda p: (p[0], p[1].mint))
        if not pts:
            out[name] = {'n': 0, 'insufficient': True, 'buckets': []}
            continue
        groups = F.quantile_groups(pts, buckets)
        table = []
        for g in groups:
            b = _bucket_row(g)
            b['insufficient'] = b['n'] < min_bucket
            table.append(b)
        out[name] = {'n': len(pts), 'insufficient': len(pts) < buckets * min_bucket or any(b['insufficient'] for b in table), 'buckets': table}
    return out


def winners_vs_rugs(rows, feature_names):
    """The median (and the number of known values) of each feature for each label."""
    out = {}
    for label in LB.LABELS:
        group = [r for r in rows if r.label.label == label]
        entry = {'n': len(group), 'mean_max_up': round(sum(r.label.max_up for r in group) / len(group), 6) if group else None,
                 'mean_max_down': round(sum(r.label.max_down for r in group) / len(group), 6) if group else None, 'features': {}}
        for name in feature_names:
            vals = sorted(r.features[name] for r in group if r.features.get(name) is not None)
            if vals:
                mid = len(vals) // 2
                median = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
                entry['features'][name] = {'n': len(vals), 'median': format(median.normalize(), 'f')}
            else:
                entry['features'][name] = {'n': 0, 'median': None}
        out[label] = entry
    return out


# ----------------------------------------------------------------------------------------------------------- learn
def learn(rows, *, strategy: Optional[S.StrategyConfig] = None, fractions=DEFAULT_FRACTIONS, min_n=MIN_N, min_confirm=MIN_CONFIRM,
          min_bucket=MIN_BUCKET, embargo_s=0, feature_names=FEATURE_NAMES, now=None, proposals_dir=None, dataset_stats=None,
          z_select=Z_SELECT, z_confirm=Z_CONFIRM, max_rules=MAX_RULES, max_reject_fraction=MAX_REJECT_FRACTION) -> dict:
    """Tables over every labelled candidate that has features; rules mined / selected / confirmed on the ENTERABLE ones by time."""
    rows = list(rows)
    feature_names = tuple(feature_names)
    for name, value in (('min_n', min_n), ('min_confirm', min_confirm), ('min_bucket', min_bucket)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LearnError('%s must be an integer >= 1' % name)
    enterable = [r for r in rows if r.enterable]
    mine_rows, select_rows, confirm_rows, bounds = split_rows(enterable, fractions, embargo_s)
    candidates = mine(mine_rows, feature_names, min_n=min_n, max_reject_fraction=max_reject_fraction)
    chosen, evidence = select_rules(candidates, select_rows, min_n=min_n, z_select=z_select, max_rules=max_rules,
                                    max_reject_fraction=max_reject_fraction)
    windows = {'mine': mine_rows, 'select': select_rows, 'confirm': confirm_rows}
    union = {k: evaluate(v, chosen) for k, v in windows.items()} if chosen else None
    baseline = {k: evaluate(v, []) for k, v in windows.items()}
    stamp = datetime.datetime.fromtimestamp(0 if now is None else now, datetime.timezone.utc)
    by_label = {label: sum(1 for r in rows if r.label.label == label) for label in LB.LABELS}
    result = {
        'label': LABEL, 'generated_at': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'), 'selection_version': SELECTION_VERSION,
        'label_version': LB.LABEL_VERSION,
        'counts': {'candidates': len(rows), 'enterable': len(enterable), 'by_label': by_label, 'dataset': dataset_stats or {}},
        'split': {'fractions': list(fractions), 'embargo_s': embargo_s, 'mine': len(mine_rows), 'select': len(select_rows),
                  'confirm': len(confirm_rows), 'select_start_ts': bounds[0], 'confirm_start_ts': bounds[1]},
        'thresholds': {'min_n': min_n, 'min_confirm': min_confirm, 'min_bucket': min_bucket, 'z_select': z_select, 'z_confirm': z_confirm,
                       'max_rules': max_rules, 'max_reject_fraction': max_reject_fraction},
        'tables': quantile_tables(rows, feature_names, min_bucket=min_bucket), 'winners_vs_rugs': winners_vs_rugs(rows, feature_names),
        'baseline': baseline,
        'candidates': [{'rule': e['rule'].to_dict(), 'train': e['mined'], 'select': e['select'], 'flags': e['flags'],
                        'chosen': e['rule'] in chosen} for e in evidence],
        'selected': [r.to_dict() for r in chosen], 'union': union, 'confirm_used_for_selection': False,
        'proposal': None, 'proposal_status': None, 'warnings': [],
    }
    result['warnings'] = _warnings(result, len(candidates))
    if not candidates:
        result['proposal_status'] = 'NO_RULE_FOUND'
    elif not chosen:
        result['proposal_status'] = 'NO_SELECTED_RULE'
    elif not confirmed(union['confirm'], min_confirm=min_confirm, z_confirm=z_confirm):
        result['proposal_status'] = 'NOT_CONFIRMED'
    elif proposals_dir is None:
        result['proposal_status'] = 'CONFIRMED_NO_DIRECTORY_GIVEN'
    else:
        result['proposal'] = str(write_proposal(proposals_dir, result, chosen, strategy, feature_names))
        result['proposal_status'] = 'WRITTEN'
    return result


def _warnings(result, n_mined):
    out = ['Outcomes are a SIMULATION: recorded vault amounts, constant-product quotes, the live exit rules (EXECUTION_UNVERIFIED).']
    if n_mined:
        out.append('%d candidate rules were mined and the best chosen on the select window: that choice is optimistic (selection bias). '
                   'The confirm window only accepts or rejects the chosen set; confirm any proposal with a forward paper run under a new version.' % n_mined)
    if result['split']['select'] < 100 or result['split']['confirm'] < 100:
        out.append('Only %d select / %d confirm candidates: rule evidence is noise-dominated.' % (result['split']['select'], result['split']['confirm']))
    skipped = result['counts']['dataset'].get('skipped') if result['counts']['dataset'] else None
    if skipped:
        out.append('Some rows were unusable or dropped (see counts.dataset.skipped).')
    if result['counts']['by_label'].get('RUG', 0) == 0:
        out.append('No candidate was labelled RUG: there is nothing to learn to avoid yet.')
    return out


def write_proposal(directory, result, chosen, strategy, feature_names):
    ts = result['generated_at'].replace('-', '').replace(':', '')
    enriched = sorted({c.feature for rule in chosen for c in rule.conditions} - SCREEN_TIME_FEATURES)
    current = {'strategy_version': strategy.strategy_version, 'config_hash': strategy.config_hash,
               'min_market_cap_usd': str(strategy.min_market_cap_usd), 'max_market_cap_usd': str(strategy.max_market_cap_usd),
               'min_liquidity_usd': str(strategy.min_liquidity_usd), 'selection_rules': []} if strategy is not None else {'selection_rules': []}
    doc = {'status': PROPOSAL_STATUS, 'label': LABEL, 'generated_at': result['generated_at'], 'selection_version': SELECTION_VERSION,
           'label_version': LB.LABEL_VERSION, 'current': current,
           'proposed': {'selection_rules': [r.to_dict() for r in chosen]},
           'diff': {'selection_rules': {'from': [], 'to': [r.to_dict() for r in chosen]}},
           'requires_runner_change': ['selection_rules'] + (['wait_for_enrichment:' + ','.join(enriched)] if enriched else []),
           'evidence': {'baseline': result['baseline'], 'union': result['union'], 'split': result['split'], 'thresholds': result['thresholds'],
                        'chosen_on': 'select', 'confirmed_on': 'confirm', 'candidates_mined': len(result['candidates']),
                        'counts': {'candidates': result['counts']['candidates'], 'enterable': result['counts']['enterable'],
                                   'by_label': result['counts']['by_label']}, 'features_available_to_rules': list(feature_names)},
           'warnings': result['warnings'], 'never_applied_automatically': True}
    out = Path(directory)
    if out.is_symlink():
        raise LearnError('proposals_dir must not be a symlink')
    out.mkdir(parents=True, exist_ok=True)
    path = out / ('%s-selection.json' % ts)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + '\n')
    return path


# ----------------------------------------------------------------------------------------------------------- report
def render_section(result: dict) -> str:
    """The HTML section "what winners vs rugs looked like at entry" (a fragment: it can be embedded in the report page)."""
    from lean.report import e, table
    parts = ['<h2>What winners vs rugs looked like at entry (selection learner)</h2>',
             '<p class="flag">%s</p>' % e(result['label']),
             '<p>%d candidates with features (%s); %d would be entered by the current strategy. Labels v%d. Walk-forward on the entered-able ones: '
             'mine %d / select %d / confirm %d. Rules are chosen on the SELECT window only; the confirm window only accepts or rejects the set. '
             '<b>Never applied automatically.</b></p>' % (
                 result['counts']['candidates'], ', '.join('%s %d' % kv for kv in result['counts']['by_label'].items()), result['counts']['enterable'],
                 result['label_version'], result['split']['mine'], result['split']['select'], result['split']['confirm'])]
    wvr = result['winners_vs_rugs']
    names = sorted(next(iter(wvr.values()))['features'])
    parts.append('<h3>Median feature value per label</h3>' + table(['feature'] + ['%s (n=%d)' % (label, wvr[label]['n']) for label in LB.LABELS],
                 [[name] + ['%s (%d)' % (wvr[label]['features'][name]['median'], wvr[label]['features'][name]['n']) for label in LB.LABELS] for name in names]))
    rows = []
    for name, t in result['tables'].items():
        for b in t['buckets']:
            rows.append([name, b['lo'], b['hi'], b['n'], b['rug_rate'], '%s-%s' % tuple(b['rug_ci']), b['win_rate'], '%s-%s' % tuple(b['win_ci']),
                         b['mean_forward_return'], b['mean_sim_pnl_sol'], 'INSUFFICIENT' if b['insufficient'] else ''])
    parts.append('<h3>Quantile tables</h3>' + (table(['feature', 'lo', 'hi', 'n', 'rug rate', 'rug 95% CI', 'win rate', 'win 95% CI', 'mean fwd return',
                                                     'mean sim pnl SOL', 'flag'], rows) if rows else '<p>No feature values yet.</p>'))
    parts.append('<h3>Rules (status: %s)</h3>' % e(result['proposal_status']))
    cand = [[c['rule']['text'], c['train']['train_rejected'], c['train']['train_gain_per_candidate_sol'], c['select']['n_rejected'],
             c['select']['gain_per_candidate_sol'], c['select']['rejected_t'], ','.join(c['flags']) or 'ok', 'CHOSEN' if c['chosen'] else '']
            for c in result['candidates']]
    parts.append(table(['rule (REJECT if)', 'mine n', 'mine gain/cand SOL', 'select n', 'select gain/cand SOL', 'select t', 'flags', ''], cand)
                 if cand else '<p>No rule reached the minimum support on the mine window.</p>')
    if result['union']:
        parts.append('<h3>Chosen set</h3>' + table(['window', 'n', 'rejected', 'baseline mean pnl SOL', 'policy mean pnl SOL', 'gain/cand SOL', 'rejected t',
                                                    'rug rate rejected', 'rug rate kept'],
                     [[k, v['n'], v['n_rejected'], v['baseline_mean_pnl_sol'], v['policy_mean_pnl_sol'], v['gain_per_candidate_sol'], v['rejected_t'],
                       v['rug_rate_rejected'], v['rug_rate_kept']] for k, v in result['union'].items()]) +
                     '<p>* the confirm window is NOT used to choose or drop any rule.</p>')
    parts.append('<h3>Warnings</h3><ul>%s</ul>' % ''.join('<li>%s</li>' % e(w) for w in result['warnings']))
    return ''.join(parts)


def render_html(result: dict) -> str:
    from lean.report import CSS, e
    return ('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Lean selection learner</title><style>%s</style></head><body><main><h1>Lean selection learner</h1>'
            '<p>Generated %s</p>%s</main></body></html>' % (CSS, e(result['generated_at']), render_section(result)))


def write_report(out_dir, result):
    out_dir = Path(out_dir)
    if out_dir.is_symlink() or not out_dir.is_dir():
        raise LearnError('out_dir missing or a symlink')
    stamp = result['generated_at'].replace('-', '').replace(':', '')
    html_path, json_path = out_dir / ('lean-learn-%s.html' % stamp), out_dir / ('lean-learn-%s.json' % stamp)
    from lean.report import write_exclusive
    write_exclusive(html_path, render_html(result))
    try:
        write_exclusive(json_path, json.dumps(result, sort_keys=True, allow_nan=False, default=str) + '\n')
    except BaseException:
        os.unlink(html_path)
        raise
    return html_path, json_path


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--db', required=True, help="the live trader's lean.sqlite (read-only: features rows and starting cash)")
    p.add_argument('--paths-db', required=True, action='append', help="the recorder's paths.sqlite; repeat for rolled-over archives (oldest first)")
    p.add_argument('--strategy', default=str(ROOT / 'config' / 'lean' / 'strategy-default.json'))
    p.add_argument('--out-dir', required=True)
    p.add_argument('--proposals-dir', default=str(ROOT / 'config' / 'lean' / 'proposals'),
                   help='where a CONFIRMED rule set is written (never applied); default config/lean/proposals')
    p.add_argument('--fractions', default='0.5,0.25,0.25', help='mine,select,confirm shares of the candidates by decision time')
    p.add_argument('--min-n', type=int, default=MIN_N)
    p.add_argument('--min-confirm', type=int, default=MIN_CONFIRM)
    p.add_argument('--max-lag-s', type=float, default=DEFAULT_MAX_LAG_S)
    p.add_argument('--embargo-s', type=float, default=0)
    p.add_argument('--pool-fee-bps', type=int, default=LB.DEFAULT_POOL_FEE_BPS)
    p.add_argument('--now', type=float, required=True, help='epoch seconds used for names/timestamps (explicit: no hidden clock)')
    args = p.parse_args(argv)
    try:
        try:
            fractions = tuple(float(x) for x in args.fractions.split(','))
        except ValueError:
            raise LearnError('fractions must be three numbers like 0.5,0.25,0.25') from None
        strategy = S.StrategyConfig.load(args.strategy)
        dataset = build_dataset(args.db, args.paths_db, strategy, max_lag_s=args.max_lag_s, pool_fee_bps=args.pool_fee_bps)
        result = learn(dataset.rows, strategy=strategy, fractions=fractions, min_n=args.min_n, min_confirm=args.min_confirm,
                       embargo_s=args.embargo_s, now=args.now, proposals_dir=args.proposals_dir, dataset_stats=dataset.stats)
        html_path, json_path = write_report(args.out_dir, result)
    except (LearnError, LB.LabelError, R.ReplayError, S.ConfigError, sqlite3.Error, OSError, ValueError) as error:
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps({'status': 'OK', 'html': str(html_path), 'json': str(json_path), 'proposal': result['proposal'],
                      'proposal_status': result['proposal_status']}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
