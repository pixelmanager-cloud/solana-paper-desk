"""Selection learner (L19, reworked in LINT3): what did winners and rugs look like AT DECISION TIME, and which simple screen rule would
have kept the trader out of the rugs. PAPER ONLY, offline, read-only, deterministic (no randomness; the clock only names the files).
Nothing here trades, calls a provider or changes a config: the output is a REPORT and a proposal file that is NEVER applied.

Inputs, all opened read-only with plain ``mode=ro`` (``lean.report.open_ro``: works while the trader and the recorders write):
  * ``paths.sqlite`` (the L07R2 recorder, ``lean.paths``) and its archives ``paths-*.sqlite`` beside it: tables ``paths`` /
    ``path_marks`` / ``path_gaps`` / ``path_ends`` (``lean.labels.load_paths``);
  * ``lean.sqlite`` (the trader's store): ``decisions`` (the decision time of each candidate), ``observations(kind='features')``
    (the L12F v3 rows: ``decision_ts``, ``late_s``, ``flags``) and ``observations(kind='wallet_signals')`` (the L13 v2 rows:
    ``decision_ts``, the window cut at the decision, the smart table as of the decision, the funders map, detected hubs).

THE DECISION of a candidate is the trader's last screen decision at or before its path start, or the entry decision that followed it
(``decision_time``; the same definition the v3 feature row uses). Labels start there (``lean.labels``) and every feature must be KNOWN
there. No lookahead (``LookaheadPolicy``, every exclusion counted in ``stats['excluded']``):
  * a features row is used only if it is ``features_version`` >= 3 and its ``decision_ts`` is at or before the decision (the last such
    row of the candidate wins; a later row can never replace it);
  * a field flagged ``AT_COLLECTION`` (a snapshot read AFTER the decision: the dev balance, the holders of a soft reject) is used only
    when the row's ``late_s`` <= ``max_late_s`` (default 15 s); a field with any other flag except ``CUT_AT_DECISION`` is excluded;
  * a wallet-signals row is used only if it is ``wallet_signals_v2`` with a ``decision_ts`` at or before the decision and a window that
    ends at it (``window_after_decision_s == 0``); ``smart_buyers_count`` only when its table is as of that decision
    (``smart_asof_ts``); ``bundled_supply_pct`` only when the supply is the screen's (``supply_source == 'screen'``);
  * an order-flow row (L20, ``observations(kind='flow')``) is used only if it is ``flow_v1`` with a ``decision_ts`` at or before the
    decision and a window that ends at it (``window_after_decision_s == 0``); a ``TRUNCATED_SPAN`` rate is excluded;
  * a ``risk_external`` row (L21: RugCheck, creator history, copycat) is used only if it is ``risk_external_v1`` with a ``decision_ts`` at
    or before the decision; every value in it is an ``AT_COLLECTION`` snapshot, so it is used only when the row's ``late_s`` <=
    ``max_late_s`` (a value without that flag is excluded);
  * the marks never become features, and no mark before the decision is ever read by a label.
A feature that is unknown for a candidate never matches a rule condition.

Learner (interpretable only; stdlib):
  * per-feature quantile tables: n, rug rate, win rate (each with a Wilson 95% interval), mean forward return and mean simulated PnL
    per bucket; a bucket / feature below its minimum is flagged INSUFFICIENT and must not be read as a finding;
  * "winners vs rugs": the median of each feature per label;
  * a greedy rule miner for 1- and 2-condition REJECT rules (``feature > t`` / ``feature <= t``, ``t`` a quantile of the training
    values). The objective is the expected PnL per candidate under the LIVE exit rules (``lean.labels.simulate_exits``: the real
    ``lean.strategy.exit_decision`` on every recorded mark): rejecting a candidate replaces its PnL by 0, so a rule's gain is
    ``-(sum of the rejected PnL) / n_candidates``. Candidates are the ones the current strategy would enter (entered, or passed and
    blocked only by a portfolio limit): a screen rule cannot improve the others;
  * walk-forward by time: MINE on the oldest window, SELECT on the middle one, CONFIRM on the newest. The select window chooses the
    rules (each needs ``min_n`` rejected candidates, a positive gain and a one-sided t-test on the rejected PnL); the confirm window
    can only accept or reject the SET as a whole (``min_confirm`` rejected, positive gain, stricter t-test): it never chooses.

Every run writes ``<out-dir>/proposals/<ts>-selection.json`` (never overwritten; ``status`` says whether it proposes anything) and the
HTML page "what winners vs rugs looked like at entry". A proposal needs the coordinator's review and CK's OK to become a screen /
strategy version; rules on features that are only known after the screen need the runner to wait for them (``requires_runner_change``).
"""
import argparse
import datetime
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from lean import adapters as A, features as F, labels as LB, strategy as S
from lean import flow as FLW

SELECTION_VERSION = 2
PROPOSAL_STATUS = 'SELECTION_PROPOSAL_NOT_APPLIED'
NO_PROPOSAL = 'NO_PROPOSAL'
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
# L12F v3 feature-row fields (lean.features.FIELDS that are numbers a screen rule can use)
ROW_FEATURES = ('top10_pct', 'top1_pct', 'dev_holding_pct', 'seconds_graduation_to_screen', 'tx_count_first_10m', 'market_cap_usd',
                'liquidity_usd', 'age_seconds_at_screen', 'transfer_fee_bps')
# L13 wallet-signal row figures
WALLET_FEATURES = ('bundle_score', 'sniper_count', 'same_slot_buyers', 'bundled_supply_pct', 'smart_buyers_count')
# L20 order-flow / bot-pump figures (lean.flow), as of the decision
FLOW_FEATURES = FLW.FEATURES                       # rate, buy/sell, wash trading (#1), path shape (#7)
FLOW_VERSION = FLW.VERSION
FLOW_OK_FLAGS = frozenset({'SAMPLE'})              # read off the pre-decision sample: pre-decision by construction
# --- L24: linked-wallet cluster row figures (lean.clusters) ---
CLUSTER_FEATURES = ('cluster_count', 'largest_cluster_supply_pct', 'top_cluster_holders', 'creator_cluster_supply_pct',
                    'linked_supply_pct_total', 'jito_bundled_buys', 'fresh_wallet_share')
CLUSTER_SUPPLY_FEATURES = frozenset({'largest_cluster_supply_pct', 'creator_cluster_supply_pct', 'linked_supply_pct_total'})
CLUSTERS_VERSION = 'clusters_v1'
# --- end L24 ---
# --- L21: RugCheck / creator history / copycat (observations kind 'risk_external'; every value is an AT_COLLECTION snapshot) ---
RISK_FEATURES = ('rc_score', 'rc_score_norm', 'rc_danger_count', 'rc_warn_count', 'rc_total_holders', 'rc_graph_insiders',
                 'rc_insider_network_count', 'rc_insider_largest_pct', 'rc_top10_pct', 'rc_lp_locked_pct', 'rc_rugged',
                 'creator_tokens_7d', 'creator_tokens_30d', 'creator_dead_30d', 'creator_rugged_sampled', 'copycat_of_recent',
                 'same_image_count')
RISK_VERSION = 'risk_external_v1'
# --- end L21 ---
FEATURE_NAMES = ROW_FEATURES + WALLET_FEATURES + FLOW_FEATURES + CLUSTER_FEATURES + RISK_FEATURES
# known from the screen itself; the others come from the extra calls (L12F) or the wallet collector (L13) that run after it
SCREEN_TIME_FEATURES = frozenset({'market_cap_usd', 'liquidity_usd', 'age_seconds_at_screen', 'transfer_fee_bps'})
MIN_FEATURES_VERSION = 3
WALLET_VERSION = 'wallet_signals_v2'
DEFAULT_MAX_LATE_S = 15.0
OK_FLAGS = frozenset({'CUT_AT_DECISION'})           # a field cut at the decision is pre-decision by construction
# a not-entered candidate that passed the screen and was refused ONLY by one of these would have been entered on its own merits
PORTFOLIO_REASONS = frozenset({'PORTFOLIO_INVALID', 'DAILY_LOSS_STOP', 'LOSS_STREAK_PAUSE', 'MAX_POSITIONS', 'ALREADY_HELD', 'COOLDOWN',
                               'ENTRY_THROTTLE', 'BELOW_MINIMUM', 'ENTRIES_STOPPED'})
ROOT = Path(__file__).resolve().parents[1]


class LearnError(ValueError):
    pass


class InsufficientData(LearnError):
    """Too few candidates for three non-empty windows: a STATUS of the run (INSUFFICIENT_DATA), not a usage error."""


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


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ----------------------------------------------------------------------------------------------------------- dataset
@dataclass(frozen=True)
class Row:
    mint: str
    ts: float                    # THE decision time
    label: LB.Label
    features: dict               # {name: Decimal | None}, only what was known at the decision
    pnl_lamports: int            # the live exit rules on the path from the decision (lean.labels.simulate_exits)
    enterable: bool              # the current strategy would have entered this candidate
    trade_status: Optional[str] = None


@dataclass
class Dataset:
    rows: list
    stats: dict


@dataclass(frozen=True)
class LookaheadPolicy:
    max_late_s: float = DEFAULT_MAX_LATE_S          # an AT_COLLECTION snapshot later than this after the decision is excluded
    eps_s: float = 1e-6                             # the rows and the decision are stamped by the same store clock

    def __post_init__(self):
        for name in ('max_late_s', 'eps_s'):
            value = getattr(self, name)
            if not _num(value) or value < 0:
                raise LearnError('%s must be a finite number >= 0' % name)

    def to_dict(self):
        return {'max_late_s': float(self.max_late_s), 'eps_s': float(self.eps_s), 'min_features_version': MIN_FEATURES_VERSION,
                'wallet_version': WALLET_VERSION, 'clusters_version': CLUSTERS_VERSION, 'ok_flags': sorted(OK_FLAGS)}


class _Counts:
    def __init__(self):
        self.skipped, self.excluded = {}, {}

    def skip(self, name, n=1):
        self.skipped[name] = self.skipped.get(name, 0) + n

    def exclude(self, feature, why):
        bucket = self.excluded.setdefault(feature, {})
        bucket[why] = bucket.get(why, 0) + 1


def _observations(connection, kind, counts, prefix):
    """{mint: [payload dicts in id order]} of one observation kind, parsed from the canonical ``raw`` bytes."""
    out = {}
    try:
        cursor = connection.execute('SELECT id, mint, candidate_id, raw, raw_truncated FROM observations WHERE kind=? ORDER BY id', (kind,))
    except sqlite3.Error:
        counts.skip(prefix + '_NO_TABLE')
        return out
    for _id, mint, cid, raw, truncated in cursor:
        try:
            if truncated:
                raise ValueError('truncated')
            row = json.loads(bytes(raw).decode('utf-8'))
            if not isinstance(row, dict) or row.get('mint') != mint or not isinstance(mint, str):
                raise ValueError('mint')
        except (ValueError, TypeError, UnicodeDecodeError):
            counts.skip(prefix + '_UNREADABLE')
            continue
        row['_candidate_id'] = cid
        out.setdefault(mint, []).append(row)
    return out


def read_decisions(connection) -> dict:
    """{mint: [(ts, kind, candidate_id), ...] by time} of the screen and entry decisions (``lean.sqlite`` ``decisions``)."""
    out = {}
    for ts, kind, mint, cid in connection.execute("SELECT ts, kind, mint, candidate_id FROM decisions WHERE kind IN ('screen','entry') "
                                                  'AND mint IS NOT NULL ORDER BY ts, id'):
        if _num(ts):
            out.setdefault(mint, []).append((float(ts), kind, cid))
    return out


def decision_time(decisions, candidate_id, start_ts) -> Optional[float]:
    """The candidate's decision: its LAST screen decision at or before the path start, or the last entry decision after that screen
    (still at or before the start). None when the store has no screen decision for it."""
    rows = [d for d in decisions if d[0] <= start_ts and (candidate_id is None or d[2] == candidate_id)]
    screens = [d[0] for d in rows if d[1] == 'screen']
    if not screens:
        return None
    entries = [d[0] for d in rows if d[1] == 'entry' and d[0] >= screens[-1]]
    return entries[-1] if entries else screens[-1]


def _pick(rows, decision_ts, candidate_id, policy, counts, prefix):
    """The last row of the candidate whose own ``decision_ts`` is at or before the decision. Later rows are counted, never used."""
    best = None
    for row in rows:
        if candidate_id is not None and row.get('_candidate_id') not in (None, candidate_id):
            continue
        at = row.get('decision_ts')
        if not _num(at):
            counts.skip(prefix + '_NO_DECISION_TS')
            continue
        if at > decision_ts + policy.eps_s:
            counts.skip(prefix + '_AFTER_DECISION')
            continue
        best = row
    return best


def feature_values(row, decision_ts, policy, counts, names=ROW_FEATURES) -> dict:
    """``{name: Decimal | None}`` of one L12F v3 row, every post-decision field excluded (and counted)."""
    out = {name: None for name in names}
    if row is None:
        return out
    if not isinstance(row.get('features_version'), int) or row['features_version'] < MIN_FEATURES_VERSION:
        counts.skip('FEATURES_PRE_V3')                     # no decision_ts / flags: a v1/v2 tx count runs past the decision
        return out
    fields, flags = row.get('fields') or {}, row.get('flags') or {}
    late = row.get('late_s')
    for name in names:
        value = _to_decimal(fields.get(name)) if isinstance(fields, dict) else None
        if value is None:
            continue
        flag = flags.get(name) if isinstance(flags, dict) else None
        if flag == 'AT_COLLECTION':
            if not _num(late) or late > policy.max_late_s:
                counts.exclude(name, 'AT_COLLECTION_LATE')
                continue
        elif flag is not None and flag not in OK_FLAGS:
            counts.exclude(name, 'FLAG_' + str(flag)[:40])
            continue
        out[name] = value
    return out


def wallet_values(row, decision_ts, policy, counts, names=WALLET_FEATURES) -> dict:
    """``{name: Decimal | None}`` of one L13 row, as of the decision only (and every exclusion counted)."""
    out = {name: None for name in names}
    if row is None:
        return out
    if row.get('version') != WALLET_VERSION:
        counts.skip('WALLET_SIGNALS_OLD_VERSION')
        return out
    after = row.get('window_after_decision_s')
    for name in names:
        value = _to_decimal(row.get(name))
        if value is None:
            continue
        if not _num(after) or after > 0:                     # the window was not cut at the decision (unknown decision)
            counts.exclude(name, 'WINDOW_NOT_CUT_AT_DECISION')
            continue
        if name == 'smart_buyers_count' and (not _num(row.get('smart_asof_ts')) or row['smart_asof_ts'] > row['decision_ts'] + policy.eps_s):
            counts.exclude(name, 'SMART_TABLE_NOT_AS_OF_DECISION')
            continue
        if name == 'bundled_supply_pct' and row.get('supply_source') != 'screen':
            counts.exclude(name, 'SUPPLY_READ_AFTER_DECISION')
            continue
        out[name] = value
    return out


def flow_values(row, decision_ts, policy, counts, names=FLOW_FEATURES) -> dict:
    """``{name: Decimal | None}`` of one L20 flow row, as of the decision only (and every exclusion counted)."""
    out = {name: None for name in names}
    if row is None:
        return out
    if row.get('version') != FLOW_VERSION:
        counts.skip('FLOW_OLD_VERSION')
        return out
    after, flags = row.get('window_after_decision_s'), row.get('flags') or {}
    for name in names:
        value = _to_decimal(row.get(name))
        if value is None:
            continue
        if not _num(after) or after > 0:                     # the window ran past the decision
            counts.exclude(name, 'WINDOW_NOT_CUT_AT_DECISION')
            continue
        flag = flags.get(name) if isinstance(flags, dict) else None
        if flag is not None and flag not in FLOW_OK_FLAGS:
            counts.exclude(name, 'FLAG_' + str(flag)[:40])
            continue
        out[name] = value
    return out


# --- L24 ---
def cluster_values(row, decision_ts, policy, counts, names=CLUSTER_FEATURES) -> dict:
    """``{name: Decimal | None}`` of one L24 clusters row, as of the decision only (every exclusion counted): the pool-window figure
    only when the window was cut at the decision; a holder snapshot read at collection (``AT_COLLECTION``) only within ``max_late_s``;
    any other flag excluded; a supply share only with the screen's supply."""
    out = {name: None for name in names}
    if row is None:
        return out
    if row.get('version') != CLUSTERS_VERSION:
        counts.skip('CLUSTERS_OLD_VERSION')
        return out
    flags, late = row.get('flags') or {}, row.get('late_s')
    after = row.get('window_after_decision_s')
    for name in names:
        value = _to_decimal(row.get(name))
        if value is None:
            continue
        flag = flags.get(name) if isinstance(flags, dict) else None
        if flag == 'AT_COLLECTION':
            if not (_num(late) and late <= policy.max_late_s):          # a snapshot read too long after the decision
                counts.exclude(name, 'AT_COLLECTION_LATE')
                continue
        elif flag not in (None, *OK_FLAGS):
            counts.exclude(name, 'FLAG_' + str(flag)[:40])
            continue
        elif name != 'jito_bundled_buys' and row.get('holders_source') != 'screen':
            counts.exclude(name, 'HOLDERS_NOT_FROM_SCREEN')         # an unflagged holder figure must come from the screen's snapshot
            continue
        if name == 'jito_bundled_buys' and (not _num(after) or after > 0):
            counts.exclude(name, 'WINDOW_NOT_CUT_AT_DECISION')
            continue
        if name in CLUSTER_SUPPLY_FEATURES and row.get('supply_source') != 'screen':
            counts.exclude(name, 'SUPPLY_READ_AFTER_DECISION')
            continue
        out[name] = value
    return out
# --- end L24 ---


# --- L21 ---
def risk_values(row, decision_ts, policy, counts, names=RISK_FEATURES) -> dict:
    """``{name: Decimal | None}`` of one L21 ``risk_external`` row under the same guard as the feature rows: the row's ``decision_ts`` is
    at or before the decision (``_pick``), and every value is an ``AT_COLLECTION`` snapshot used only when ``late_s <= max_late_s``. A
    value without that flag, or with any other flag, is excluded. ``rc_rugged`` (a bool) is 1 / 0."""
    out = {name: None for name in names}
    if row is None:
        return out
    if row.get('version') != RISK_VERSION:
        counts.skip('RISK_EXTERNAL_OLD_VERSION')
        return out
    fields, flags = row.get('fields') or {}, row.get('flags') or {}
    late = row.get('late_s')
    for name in names:
        raw = fields.get(name) if isinstance(fields, dict) else None
        value = Decimal(int(raw)) if isinstance(raw, bool) else _to_decimal(raw)
        if value is None:
            continue
        flag = flags.get(name) if isinstance(flags, dict) else None
        if flag != 'AT_COLLECTION':
            counts.exclude(name, 'FLAG_' + str(flag)[:40])
            continue
        if not (_num(late) and late <= policy.max_late_s):
            counts.exclude(name, 'AT_COLLECTION_LATE')
            continue
        out[name] = value
    return out
# --- end L21 ---


def enterable(path: LB.PathRecord) -> bool:
    """Entered, or passed the screen and refused ONLY by a portfolio limit (it would have been entered on its own merits)."""
    if path.entered:
        return True
    if path.screen_reasons:
        return False
    return bool(path.reasons) and all(str(r) in PORTFOLIO_REASONS for r in path.reasons)


def build_dataset(lean_db, paths_db, strategy: S.StrategyConfig, *, label_cfg=None, policy=None, pool_fee_bps=LB.DEFAULT_POOL_FEE_BPS,
                  feature_names=FEATURE_NAMES, extra_paths=(), config_hash=None) -> Dataset:
    """Label every recorded path from its decision, join the features known at that decision and replay the live exit rules on it.
    ``lean_db`` / ``paths_db`` are file paths (opened ``mode=ro``) or open sqlite3 connections (``paths_db`` may be a list)."""
    from lean.report import open_ro
    label_cfg = label_cfg or LB.LabelConfig.from_strategy(strategy)
    policy = policy or LookaheadPolicy()
    feature_names = tuple(feature_names)
    unknown = set(feature_names) - set(FEATURE_NAMES)
    if unknown:
        raise LearnError('unknown features %s' % sorted(unknown))
    pcfg = A.paper_config(strategy)
    counts = _Counts()
    live, live_owned = (lean_db, False) if isinstance(lean_db, sqlite3.Connection) else (open_ro(lean_db), True)
    if isinstance(paths_db, sqlite3.Connection):
        connections, owned = [paths_db], False
    elif isinstance(paths_db, (list, tuple)) and paths_db and all(isinstance(c, sqlite3.Connection) for c in paths_db):
        connections, owned = list(paths_db), False
    else:
        connections, owned = None, True
    rows, by_label, n_paths, load_skipped = [], {}, 0, {}
    try:
        if connections is None:
            connections = LB.open_paths(paths_db, extra_paths)
        decisions = read_decisions(live)
        # --- M1 #10: only candidates decided inside the runs of one lean.json / strategy hash ---
        windows = None
        if config_hash:
            from lean import report as _report
            windows = _report.matching_windows(_report.run_windows(live), config_hash)
        # --- end M1 ---
        feature_rows = _observations(live, 'features', counts, 'FEATURES')
        wallet_rows = _observations(live, 'wallet_signals', counts, 'WALLET_SIGNALS')
        flow_rows = _observations(live, 'flow', counts, 'FLOW')                                 # L20
        cluster_rows = _observations(live, 'clusters', counts, 'CLUSTERS')          # L24
        risk_rows = _observations(live, 'risk_external', counts, 'RISK_EXTERNAL')                    # L21
        for path in LB.iter_paths(connections, load_skipped):           # one path's marks in memory at a time
            n_paths += 1
            decision = decision_time(decisions.get(path.mint, []), path.candidate_id, path.start_ts)
            if decision is None:
                counts.skip('DECISION_UNKNOWN')
                continue
            if windows is not None and not _report.in_windows(float(decision), windows):         # M1 #10
                counts.skip('OTHER_CONFIG_HASH')
                continue
            try:
                ref = LB.reference(path, pcfg)
            except LB.LabelError:
                counts.skip('REFERENCE_UNUSABLE')
                continue
            label = LB.label_path(path, decision, ref, label_cfg, pool_fee_bps=pool_fee_bps, pcfg=pcfg)
            by_label[label.label] = by_label.get(label.label, 0) + 1
            if not label.usable:
                counts.skip('%s:%s' % (LB.INSUFFICIENT, label.why_insufficient))
                continue
            frow = _pick(feature_rows.get(path.mint, []), decision, path.candidate_id, policy, counts, 'FEATURES')
            wrow = _pick(wallet_rows.get(path.mint, []), decision, path.candidate_id, policy, counts, 'WALLET_SIGNALS')
            xrow = _pick(flow_rows.get(path.mint, []), decision, path.candidate_id, policy, counts, 'FLOW')     # L20
            crow = _pick(cluster_rows.get(path.mint, []), decision, path.candidate_id, policy, counts, 'CLUSTERS')     # L24
            rrow = _pick(risk_rows.get(path.mint, []), decision, path.candidate_id, policy, counts, 'RISK_EXTERNAL')   # L21
            if frow is None and wrow is None and xrow is None and crow is None and rrow is None:
                counts.skip('NO_FEATURES_AT_DECISION')
                continue
            values = {**feature_values(frow, decision, policy, counts), **wallet_values(wrow, decision, policy, counts),
                      **flow_values(xrow, decision, policy, counts),
                      **cluster_values(crow, decision, policy, counts),
                      **risk_values(rrow, decision, policy, counts)}
            trade = LB.simulate_exits(path, decision, ref, strategy, pool_fee_bps=pool_fee_bps, pcfg=pcfg)
            rows.append(Row(path.mint, decision, label, {n: values[n] for n in feature_names}, trade.pnl_lamports, enterable(path),
                            trade.status))
    finally:
        if live_owned:
            live.close()
        if owned and connections:
            for c in connections:
                c.close()
    for name, n in load_skipped.items():
        counts.skip(name, n)
    rows.sort(key=lambda r: (r.ts, r.mint))
    return Dataset(rows, {'paths': n_paths, 'labels': dict(sorted(by_label.items())), 'with_features': len(rows),
                          'enterable': sum(1 for r in rows if r.enterable), 'skipped': dict(sorted(counts.skipped.items())),
                          'excluded': {k: dict(sorted(v.items())) for k, v in sorted(counts.excluded.items())},
                          'label_config': label_cfg.to_dict(), 'lookahead': policy.to_dict(), 'pool_fee_bps': pool_fee_bps, 'config_hash_filter': config_hash,
                          'strategy_version': strategy.strategy_version, 'feature_names': list(feature_names),
                          'pnl_model': 'lean.labels.simulate_exits: lean.strategy.exit_decision on every recorded mark (adapters.mark), '
                                       'one isolated position per path; L10 latency and L11 DANGER exits not modelled'})


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
        raise InsufficientData('need at least 3 candidates so that all three windows are non-empty (have %d)' % n)
    bounds = (select[0].ts, confirm[0].ts)
    if embargo_s:
        select = [r for r in select if r.ts >= bounds[0] + embargo_s]
        confirm = [r for r in confirm if r.ts >= bounds[1] + embargo_s]
        if not select or not confirm:
            raise InsufficientData('embargo removed every select or confirm candidate')
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
    """Tables over every labelled candidate that has features; rules mined / selected / confirmed on the ENTERABLE ones by time.
    With ``proposals_dir`` the selection file is written (always: it says whether it proposes anything)."""
    rows = list(rows)
    feature_names = tuple(feature_names)
    for name, value in (('min_n', min_n), ('min_confirm', min_confirm), ('min_bucket', min_bucket)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LearnError('%s must be an integer >= 1' % name)
    enterable = [r for r in rows if r.enterable]
    try:
        mine_rows, select_rows, confirm_rows, bounds = split_rows(enterable, fractions, embargo_s)
        enough = True
    except InsufficientData:                                # (bad fractions / embargo values still raise LearnError)
        mine_rows, select_rows, confirm_rows, bounds, enough = [], [], [], (None, None), False
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
        'confirmed': False, 'proposal': None, 'proposal_status': None, 'warnings': [],
    }
    result['warnings'] = _warnings(result, len(candidates))
    if not enough:
        result['proposal_status'] = 'INSUFFICIENT_DATA'
    elif not candidates:
        result['proposal_status'] = 'NO_RULE_FOUND'
    elif not chosen:
        result['proposal_status'] = 'NO_SELECTED_RULE'
    elif not confirmed(union['confirm'], min_confirm=min_confirm, z_confirm=z_confirm):
        result['proposal_status'] = 'NOT_CONFIRMED'
    else:
        result['proposal_status'], result['confirmed'] = 'CONFIRMED', True
    if proposals_dir is not None:
        result['proposal'] = str(write_proposal(proposals_dir, result, chosen if result['confirmed'] else [], strategy, feature_names))
    return result


def _warnings(result, n_mined):
    out = ['Outcomes are a SIMULATION: recorded vault amounts, constant-product quotes, the live exit rules (EXECUTION_UNVERIFIED); '
           'L10 latency fills and L11 DANGER exits are not modelled.']
    if n_mined:
        out.append('%d candidate rules were mined and the best chosen on the select window: that choice is optimistic (selection bias). '
                   'The confirm window only accepts or rejects the chosen set; confirm any proposal with a forward paper run under a new version.' % n_mined)
    if result['split']['select'] < 100 or result['split']['confirm'] < 100:
        out.append('Only %d select / %d confirm candidates: rule evidence is noise-dominated.' % (result['split']['select'], result['split']['confirm']))
    dataset = result['counts']['dataset'] or {}
    if dataset.get('skipped'):
        out.append('Some rows were unusable or dropped (see counts.dataset.skipped).')
    if dataset.get('excluded'):
        out.append('Some feature values were excluded as known only after the decision (see counts.dataset.excluded).')
    if result['counts']['by_label'].get('RUG', 0) == 0:
        out.append('No candidate was labelled RUG: there is nothing to learn to avoid yet.')
    return out


def runner_changes(chosen):
    """What the runner must do before a proposed rule can be applied at the decision."""
    names = {c.feature for rule in chosen for c in rule.conditions}
    out = ['selection_rules'] if chosen else []
    enriched = sorted((names & set(ROW_FEATURES)) - SCREEN_TIME_FEATURES)
    wallet = sorted(names & set(WALLET_FEATURES))
    cluster = sorted(names & set(CLUSTER_FEATURES))
    if enriched:
        out.append('wait_for_enrichment:' + ','.join(enriched))
    if wallet:
        out.append('wait_for_wallet_signals:' + ','.join(wallet))
    flow = sorted(names & set(FLOW_FEATURES))                                            # L20: collected after the screen
    if flow:
        out.append('wait_for_flow:' + ','.join(flow))
    if cluster:
        out.append('wait_for_clusters:' + ','.join(cluster))           # L24
    risk = sorted(names & set(RISK_FEATURES))                                                     # L21
    if risk:
        out.append('wait_for_risk_external:' + ','.join(risk))
    return out


def write_proposal(directory, result, chosen, strategy, feature_names):
    """``<directory>/<ts>-selection.json``, exclusive (never overwritten). ``status`` is PROPOSAL_STATUS only for a confirmed set."""
    ts = result['generated_at'].replace('-', '').replace(':', '')
    current = {'strategy_version': strategy.strategy_version, 'config_hash': strategy.config_hash,
               'min_market_cap_usd': str(strategy.min_market_cap_usd), 'max_market_cap_usd': str(strategy.max_market_cap_usd),
               'min_liquidity_usd': str(strategy.min_liquidity_usd), 'selection_rules': []} if strategy is not None else {'selection_rules': []}
    doc = {'status': PROPOSAL_STATUS if chosen else NO_PROPOSAL, 'outcome': result['proposal_status'], 'label': LABEL,
           'generated_at': result['generated_at'], 'selection_version': SELECTION_VERSION,
           'label_version': LB.LABEL_VERSION, 'current': current,
           'proposed': {'selection_rules': [r.to_dict() for r in chosen]},
           'diff': {'selection_rules': {'from': [], 'to': [r.to_dict() for r in chosen]}},
           'requires_runner_change': runner_changes(chosen),
           'evidence': {'baseline': result['baseline'], 'union': result['union'], 'split': result['split'], 'thresholds': result['thresholds'],
                        'chosen_on': 'select', 'confirmed_on': 'confirm', 'candidates_mined': len(result['candidates']),
                        'selected_on_select_window': result['selected'],
                        'counts': {'candidates': result['counts']['candidates'], 'enterable': result['counts']['enterable'],
                                   'by_label': result['counts']['by_label']},
                        'dataset': result['counts']['dataset'], 'features_available_to_rules': list(feature_names)},
           'warnings': result['warnings'], 'never_applied_automatically': True}
    out = Path(directory)
    if out.is_symlink():
        raise LearnError('proposals_dir must not be a symlink')
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = out / ('%s-selection.json' % ts)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(json.dumps(doc, sort_keys=True, indent=2, allow_nan=False, default=str) + '\n')
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
    dataset = result['counts']['dataset'] or {}
    if dataset:
        parts.append('<h3>Data used and excluded (no lookahead)</h3><p>Every label starts at the decision; every feature must be known at it. '
                     'Policy: %s</p>' % e(json.dumps(dataset.get('lookahead', {}), sort_keys=True)))
        parts.append(table(['labels (all paths)', 'n'], [[k, v] for k, v in (dataset.get('labels') or {}).items()]))
        parts.append(table(['rows skipped', 'n'], [[k, v] for k, v in (dataset.get('skipped') or {}).items()]) if dataset.get('skipped')
                     else '<p>No row skipped.</p>')
        excluded = [[name, why, n] for name, reasons in (dataset.get('excluded') or {}).items() for why, n in reasons.items()]
        parts.append(table(['feature value excluded', 'why (known only after the decision)', 'n'], excluded) if excluded
                     else '<p>No feature value had to be excluded.</p>')
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


def latest_config_hash(lean_db):
    """M1b #5: the ``lean_config_hash`` of the store's newest ``start`` event (opened read-only), or None (a pre-M1 store)."""
    from lean.report import open_ro, run_windows
    connection = open_ro(lean_db)
    try:
        windows = run_windows(connection)
    finally:
        connection.close()
    return windows[-1][2] if windows else None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--lean-db', required=True, help="the trader's lean.sqlite (opened mode=ro: decisions, features and wallet-signal rows)")
    p.add_argument('--paths-db', required=True, help="the recorder's paths.sqlite (mode=ro); its archives paths-*.sqlite beside it are read too")
    p.add_argument('--paths-archive', action='append', default=[], help='an extra archived recorder file elsewhere (repeatable)')
    p.add_argument('--out-dir', required=True, help='an existing directory: the HTML/JSON report and proposals/<ts>-selection.json go here')
    group = p.add_mutually_exclusive_group()
    group.add_argument('--config', help="the trader's lean.json: its strategy_config and pool_fee_bps (the LIVE exit rules and mark)")
    group.add_argument('--strategy', help='a strategy JSON (default config/lean/strategy-default.json) when --config is not given')
    p.add_argument('--pool-fee-bps', type=int, default=None, help='only without --config (default %d)' % LB.DEFAULT_POOL_FEE_BPS)
    p.add_argument('--fractions', default='0.5,0.25,0.25', help='mine,select,confirm shares of the candidates by decision time')
    p.add_argument('--min-n', type=int, default=MIN_N)
    p.add_argument('--min-confirm', type=int, default=MIN_CONFIRM)
    p.add_argument('--min-bucket', type=int, default=MIN_BUCKET)
    p.add_argument('--max-late-s', type=float, default=DEFAULT_MAX_LATE_S, help='AT_COLLECTION snapshots later than this after the decision are excluded')
    p.add_argument('--max-gap-fraction', type=float, default=LB.LabelConfig.max_gap_fraction)
    p.add_argument('--max-hole-s', type=float, default=LB.LabelConfig.max_hole_s)
    p.add_argument('--embargo-s', type=float, default=0)
    p.add_argument('--config-hash', default=None, help='M1: only candidates decided in runs whose lean.json or strategy hash starts with this (default: the LATEST run of the store, M1b)')
    p.add_argument('--all-configs', action='store_true', help='M1b: every run of the store, whatever its lean.json (vault and effective marks may mix)')
    p.add_argument('--now', type=float, default=None, help='epoch seconds for the file names (default: the current time)')
    args = p.parse_args(argv)
    try:
        try:
            fractions = tuple(float(x) for x in args.fractions.split(','))
        except ValueError:
            raise LearnError('fractions must be three numbers like 0.5,0.25,0.25') from None
        if args.config:
            if args.pool_fee_bps is not None:
                raise LearnError('--pool-fee-bps comes from --config')
            from lean.__main__ import ConfigError, load_config
            try:
                cfg = load_config(args.config)
            except ConfigError as error:
                raise LearnError('config: %s' % error) from None
            strategy, pool_fee_bps = S.StrategyConfig.load(cfg['strategy_config']), int(cfg['pool_fee_bps'])
        else:
            strategy = S.StrategyConfig.load(args.strategy or str(ROOT / 'config' / 'lean' / 'strategy-default.json'))
            pool_fee_bps = LB.DEFAULT_POOL_FEE_BPS if args.pool_fee_bps is None else args.pool_fee_bps
        out = Path(args.out_dir)
        if out.is_symlink() or not out.is_dir():
            raise LearnError('out_dir missing or a symlink')
        # M1b #5: by default only the store's LATEST run (one lean.json, so one mark model): vault and effective data are never
        # pooled silently. --all-configs opts out; a store written before M1 (no hash) is read whole.
        if args.config_hash and args.all_configs:
            raise LearnError('--config-hash and --all-configs exclude each other')
        config_hash = None if args.all_configs else (args.config_hash or latest_config_hash(args.lean_db))
        label_cfg = LB.LabelConfig.from_strategy(strategy, max_gap_fraction=args.max_gap_fraction, max_hole_s=args.max_hole_s)
        dataset = build_dataset(args.lean_db, args.paths_db, strategy, label_cfg=label_cfg, policy=LookaheadPolicy(max_late_s=args.max_late_s),
                                pool_fee_bps=pool_fee_bps, extra_paths=args.paths_archive, config_hash=config_hash)
        now = time.time() if args.now is None else args.now
        result = learn(dataset.rows, strategy=strategy, fractions=fractions, min_n=args.min_n, min_confirm=args.min_confirm,
                       min_bucket=args.min_bucket, embargo_s=args.embargo_s, now=now, proposals_dir=out / 'proposals',
                       dataset_stats=dataset.stats)
        html_path, json_path = write_report(out, result)
    except Exception as error:                               # noqa: BLE001 - one JSON line, never a traceback with paths
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps({'status': 'OK', 'html': str(html_path), 'json': str(json_path), 'proposal': result['proposal'],
                      'proposal_status': result['proposal_status'], 'confirmed': result['confirmed'],
                      'candidates': result['counts']['candidates'], 'enterable': result['counts']['enterable'], 'config_hash': config_hash}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
