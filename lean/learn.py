"""Selection learner (L19, reworked in LINT3): what did winners and rugs look like AT DECISION TIME, and which simple screen rule would
have kept the trader out of the rugs. PAPER ONLY, offline, read-only, deterministic (the only randomness is the SEEDED random-rule null;
the clock only names the files).
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

Learner (interpretable only; stdlib; statistics reworked in M2):
  * per-feature quantile tables: n, rug rate, win rate (each with a Wilson 95% interval), mean forward return and mean simulated PnL
    per bucket; a bucket / feature below its minimum is flagged INSUFFICIENT and must not be read as a finding;
  * "winners vs rugs": the median of each feature per label;
  * a greedy rule miner for 1- and 2-condition REJECT rules (``feature > t`` / ``feature <= t``, ``t`` a quantile of the development
    window's feature values: label-free). The PnL is the LIVE exit rules' (``lean.labels.simulate_exits``: the real
    ``lean.strategy.exit_decision`` on every recorded mark, fees and slippage included). The objective is the GAIN OVER A RANDOM RULE
    that rejects as many candidates: ``n_rejected * mean(all) - sum(rejected)``. (Rejecting anything "gains" when every candidate
    loses on average; only rejecting WORSE than average candidates beats a random rule.) Candidates are the ones the current strategy
    would enter (entered, or passed and blocked only by a portfolio limit): a screen rule cannot improve the others;
  * THE TEST of a rule (or a set) is KEPT vs REJECTED: the difference in mean PnL per candidate, under the same exit policy:
      - Welch's two-sample t (one-sided, H1: the kept mean is above the rejected mean);
      - a random-rule null: ``null_draws`` (>= 1000) seeded random rules that reject the SAME number of candidates (in the same time
        blocks); the rule must beat the null's 95th percentile (and its permutation p-value must be <= ``alpha``);
    a rule that rejects more than ``max_reject_fraction`` (70%) of the candidates is "stop trading", never a selection rule: the cap
    binds the rule's DESIGN (in its training fold, out of fold, and the chosen set's union on the development window); on the confirm
    block the realised share is reported (``coverage``) and at least ``min_confirm`` candidates must still be kept;
  * the windows: the NEWEST block (``confirm_fraction`` of the enterable candidates, at least 2 x ``min_confirm``) is the CONFIRM
    holdout; every older candidate whose decision is within ``embargo_s`` (the label horizon = ``paths.path_hours``) of it is purged;
    the rest is the DEVELOPMENT window. Development runs a purged ROLLING-ORIGIN (walk-forward) cross-validation by time over ``folds``
    contiguous blocks: test block k's rules are mined only on the candidates BEFORE it whose label ends before its first decision
    (purge = embargo = ``embargo_s``), and scored on block k only, so every tested candidate is scored out of sample and a block's
    inclusion in a rule's evidence depends on the past only. (A blocked K-fold that also trains on LATER blocks was tried and rejected:
    pooling a rule over the blocks it was mined for lets one test block's outcomes decide whether another is pooled, and the null
    "discovered" rules in 15-20% of pure-noise worlds instead of <= 5%.) Thresholds adapt to the data (the mining support is a share
    of the training set, between ``MIN_N_FLOOR`` and ``min_n``): there is no configuration that can never select;
  * multiple testing: every distinct rule mined in any fold is a tested hypothesis; Benjamini-Hochberg (``fdr_q``) runs over ALL of
    them (the count is reported), then the survivors must also beat their random-rule null on their pooled out-of-fold blocks;
  * the CONFIRM block is used EXACTLY ONCE per proposal: the chosen SET is tested once (Welch + random-rule null + both group sizes
    >= ``min_confirm``), it never chooses or drops a rule, and a block that was tested is recorded in the
    selection file (``confirm_consumed``) so a later run can only confirm on candidates decided after it plus the embargo
    (``CONFIRM_PENDING`` until then). The run reports the ETA in hours to the first feasible confirm at the current candidate rate;
  * a set that beats the null but whose KEPT candidates still lose on average (kept mean PnL <= 0 after costs) is reported as
    ``BLEED_REDUCER``: it reduces the bleed, it is NEVER a proposal (it is not a reason to keep or to increase trading).

Every run writes ``<out-dir>/proposals/<ts>-selection.json`` (never overwritten; ``status`` says whether it proposes anything) and the
HTML page "what winners vs rugs looked like at entry". A proposal needs the coordinator's review and CK's OK to become a screen /
strategy version; rules on features that are only known after the screen need the runner to wait for them (``requires_runner_change``).
"""
import argparse
import datetime
import itertools
import json
import math
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from lean import adapters as A, features as F, labels as LB, strategy as S
from lean import flow as FLW

SELECTION_VERSION = 3
PROPOSAL_STATUS = 'SELECTION_PROPOSAL_NOT_APPLIED'
NO_PROPOSAL = 'NO_PROPOSAL'
LABEL = 'PAPER ONLY / EXECUTION_UNVERIFIED'
MIN_N = 50                                # the most rejected candidates a rule needs in a training fold (the support adapts DOWN to this)
MIN_N_FLOOR = 10                          # ... and never below this (or min_n when that is smaller)
SUPPORT_SHARE = 0.10                      # the adaptive support: this share of the training fold, clamped to [MIN_N_FLOOR, min_n]
MIN_CONFIRM = 30                          # kept AND rejected candidates the confirm block needs (so the block holds >= 2 x this)
MIN_BUCKET = 20
CONFIRM_FRACTION = 0.3                    # the newest share of the enterable candidates is the confirm holdout
FOLDS = 5                                 # blocks of the purged rolling-origin cross-validation (block 0 only ever trains)
ALPHA = 0.05                              # one-sided level of the Welch test and of the random-rule null (its 95th percentile)
FDR_Q = 0.05                              # Benjamini-Hochberg false discovery rate over every tested rule
NULL_DRAWS = 1000                         # random rules per null (seeded); fewer is refused
SEED = 20261013
MAX_RULES = 3
MAX_REJECT_FRACTION = 0.7                 # a screen rule that rejects more than this share of the candidates is "stop trading", not selection
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


def _mean_var(values):
    """(mean, sample variance) of a non-empty list."""
    n = len(values)
    mean = sum(values) / n
    return mean, (sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0)


def _betacf(a, b, x):
    """The continued fraction of the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 400):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 1e-15:
            break
    return h


def _betai(a, b, x):
    """The regularized incomplete beta function I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def t_sf(t, df):
    """P(T > t) for Student's t with ``df`` (> 0, may be fractional) degrees of freedom: the one-sided upper tail."""
    if t == 0:
        return 0.5
    tail = 0.5 * _betai(df / 2.0, 0.5, df / (df + t * t))
    return tail if t > 0 else 1.0 - tail


def welch(kept, rejected):
    """Welch's two-sample t of H1 "the kept mean is ABOVE the rejected mean" -> {'t', 'df', 'p'} (p one-sided), or None with fewer than
    two values in a group. Two constant groups: t = +-1e9 (p 0 / 1) or 0 (p 0.5)."""
    nk, nr = len(kept), len(rejected)
    if nk < 2 or nr < 2:
        return None
    mk, vk = _mean_var(kept)
    mr, vr = _mean_var(rejected)
    diff = mk - mr
    ek, er = vk / nk, vr / nr
    se2 = ek + er
    if se2 <= 0:
        t = 1e9 if diff > 0 else (-1e9 if diff < 0 else 0.0)
        return {'t': t, 'df': float(nk + nr - 2), 'p': 0.0 if diff > 0 else (1.0 if diff < 0 else 0.5)}
    df = se2 * se2 / (ek * ek / (nk - 1) + er * er / (nr - 1))
    t = diff / math.sqrt(se2)
    return {'t': round(t, 6), 'df': round(df, 3), 'p': t_sf(t, df)}


def benjamini_hochberg(pvalues):
    """BH-adjusted q-values (step-up, monotone, capped at 1), in the input order."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: (pvalues[i], i))
    q, running = [1.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = min(1.0, running)
    return q


class RandomRuleNull:
    """The random-rule null of a kept-vs-rejected contrast: a rule that rejects the SAME number of candidates in each time block, at
    random. ``draws`` seeded random orders of each block's PnL (lamports); the first k of an order are a uniformly random k-subset, so
    one set of orders serves every k (``cols[block][k]`` = the ``draws`` sums of the first k). Blocks are built on first use."""

    def __init__(self, blocks, draws, seed):
        if isinstance(draws, bool) or not isinstance(draws, int) or draws < NULL_DRAWS:
            raise LearnError('null_draws must be an integer >= %d' % NULL_DRAWS)
        self.blocks, self.draws, self.seed, self._cols = {k: list(v) for k, v in blocks.items()}, draws, seed, {}

    def _col(self, block):
        if block not in self._cols:
            rng = random.Random('%s:%s' % (self.seed, block))
            values, prefixes = self.blocks[block], []
            for _ in range(self.draws):
                order = values[:]
                rng.shuffle(order)
                prefixes.append(tuple(itertools.accumulate(order, initial=0)))
            self._cols[block] = list(zip(*prefixes))
        return self._cols[block]

    def rejected_sums(self, counts):
        """``draws`` sums of the rejected PnL of a random rule with ``counts`` = {block: number rejected in it}."""
        cols = [self._col(block)[k] for block, k in sorted(counts.items())]
        if not cols:
            return [0] * self.draws
        return list(cols[0]) if len(cols) == 1 else [sum(x) for x in zip(*cols)]


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
def _check_number(name, value, low, high, *, low_open=False, high_open=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or (value <= low if low_open else value < low) or (value >= high if high_open else value > high):
        raise LearnError('%s must be a finite number in %s%s, %s%s' % (name, '(' if low_open else '[', low, high, ')' if high_open else ']'))


def support(n_train, min_n=MIN_N):
    """The adaptive mining support of a training fold: ``SUPPORT_SHARE`` of it, clamped to [min(MIN_N_FLOOR, min_n), min_n]."""
    return max(min(MIN_N_FLOOR, min_n), min(min_n, math.ceil(SUPPORT_SHARE * n_train - 1e-9)))


def plan_windows(ts, *, confirm_fraction=CONFIRM_FRACTION, min_confirm=MIN_CONFIRM, embargo_s=0, folds=FOLDS, min_n=MIN_N,
                 max_reject_fraction=MAX_REJECT_FRACTION, consumed_until=None) -> dict:
    """The windows over the enterable candidates' decision times ``ts`` (sorted, oldest first). Index ranges only, no PnL is read.

      confirm  the newest ``max(ceil(confirm_fraction * n), 2 * min_confirm)`` candidates: [c0, n)
      dev      [0, d): every older candidate whose label window (at most ``embargo_s`` long) ends at or before the confirm block's
               first decision (the rest, [d, c0), is PURGED)
      folds    rolling origin: dev is cut into ``folds`` contiguous blocks; block k (1 .. folds-1) is a TEST block whose training set
               is the dev candidates before it whose label window ends at or before its first decision (purge + embargo).

    A fold is feasible when its adaptive support fits: ``support(n_train) <= max_reject_fraction * n_train`` and at least
    ``min(MIN_N_FLOOR, min_n)`` candidates would still be kept. The plan is FEASIBLE with a confirm block and >= 1 feasible fold; it
    is FRESH when the confirm block starts after ``consumed_until + embargo_s`` (the end of the last confirm block that was tested,
    see ``consumed_confirm_until``)."""
    import bisect
    n = len(ts)
    floor = min(MIN_N_FLOOR, min_n)
    n_conf = max(math.ceil(confirm_fraction * n - 1e-9), 2 * min_confirm)
    out = {'n': n, 'n_confirm': 0, 'confirm': None, 'dev': 0, 'purged': 0, 'folds': [], 'feasible_folds': 0, 'feasible': False,
           'fresh': False, 'why': []}
    if n_conf >= n:
        out['why'].append('CONFIRM_BLOCK_NEEDS_%d_OF_%d' % (n_conf, n))
        return out
    c0 = n - n_conf
    start = ts[c0]
    d = bisect.bisect_right(ts, start - embargo_s, 0, c0)
    out.update(n_confirm=n_conf, confirm=(c0, n), dev=d, purged=c0 - d,
               fresh=consumed_until is None or start > consumed_until + embargo_s)
    if not out['fresh']:
        out['why'].append('CONFIRM_BLOCK_ALREADY_TESTED')
    if d < folds:
        out['why'].append('DEV_WINDOW_HAS_%d_CANDIDATES' % d)
        return out
    for j in range(1, folds):
        a, b = d * j // folds, d * (j + 1) // folds
        before = bisect.bisect_right(ts, ts[a] - embargo_s, 0, a)
        m = support(before, min_n)
        ok = before > 0 and m <= max_reject_fraction * before and before - m >= floor
        out['folds'].append({'test': (a, b), 'train': (0, before), 'n_train': before, 'min_n': m, 'feasible': ok})
    out['feasible_folds'] = sum(1 for f in out['folds'] if f['feasible'])
    if not out['feasible_folds']:
        out['why'].append('NO_FEASIBLE_FOLD')
    out['feasible'] = out['feasible_folds'] >= 1
    return out


def confirm_eta(ts, **plan_kw) -> dict:
    """Hours (after the newest candidate) until a run would have a FEASIBLE and FRESH confirm block, if enterable candidates keep
    arriving at the observed rate (``n - 1`` over the span of ``ts``). 0 when it already has; None without a rate or beyond a year."""
    n = len(ts)
    rate = (n - 1) / ((ts[-1] - ts[0]) / 3600.0) if n >= 2 and ts[-1] > ts[0] else None
    out = {'rate_per_hour': None if rate is None else round(rate, 4), 'eta_hours': None, 'eta_ts': None, 'needed_enterable': None}

    def ok(extra):
        p = plan_windows(list(ts) + [ts[-1] + 3600.0 * (j + 1) / rate for j in range(extra)] if extra else ts, **plan_kw)
        return p['feasible'] and p['fresh']
    if n and ok(0):
        return dict(out, eta_hours=0.0, eta_ts=ts[-1], needed_enterable=n)
    if rate is None:
        return out
    limit, hi = max(1, int(rate * 24 * 365)), 1
    while not ok(hi):
        if hi >= limit:
            return out
        hi = min(limit, hi * 2)
    lo = hi // 2                                             # not ok (0 was checked above; a doubling step stopped above it)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        lo, hi = (lo, mid) if ok(mid) else (mid, hi)
    return dict(out, eta_hours=round(hi / rate, 2), eta_ts=round(ts[-1] + 3600.0 * hi / rate, 3), needed_enterable=n + hi)


def _name_time(name):
    try:
        return datetime.datetime.strptime(name.split('-selection.json')[0], '%Y%m%dT%H%M%SZ').replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def consumed_confirm_until(directory) -> dict:
    """The ledger of tested confirm blocks, read from this learner's own selection files in ``directory``: ``{'until': the latest end
    (epoch s) of a confirm block that was tested, or None, 'files': n, 'unreadable': n}``. A v3 file records ``confirm_consumed`` and
    the block's end; a v2 file whose outcome was CONFIRMED / NOT_CONFIRMED tested its confirm window up to the run (the time in its
    name). Fail closed: an unreadable file counts as consumed up to the time in its name."""
    out = {'until': None, 'files': 0, 'unreadable': 0}
    d = Path(directory)
    if d.is_symlink() or not d.is_dir():
        return out

    def bump(end):
        if _num(end):
            out['until'] = float(end) if out['until'] is None else max(out['until'], float(end))
    for path in sorted(d.glob('*-selection.json')):
        if path.is_symlink() or not path.is_file():
            continue
        out['files'] += 1
        try:
            doc = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(doc, dict):
                raise ValueError('not an object')
        except (OSError, ValueError, UnicodeDecodeError):
            out['unreadable'] += 1
            bump(_name_time(path.name))
            continue
        evidence = doc.get('evidence') if isinstance(doc.get('evidence'), dict) else {}
        if evidence.get('confirm_consumed') is True:
            split = evidence.get('split') if isinstance(evidence.get('split'), dict) else {}
            end = split.get('confirm_end_ts')
            bump(end if _num(end) else _name_time(path.name))
        elif doc.get('selection_version') == 2 and doc.get('outcome') in ('CONFIRMED', 'NOT_CONFIRMED'):
            bump(_name_time(path.name))
    return out


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


def _mask(rows, rules):
    return [any(rule.matches(r) for rule in rules) for r in rows]


def contrast(groups, null=None, *, alpha=ALPHA) -> dict:
    """KEPT vs REJECTED on ``groups`` = ``[(block, rows, rejected mask)]`` (the candidates the strategy would enter; a rejected one
    earns 0 instead of its simulated PnL). The headline is the difference in mean PnL per candidate, kept minus rejected, under the
    same exit policy: Welch's one-sided t, and with ``null`` the random-rule null that rejects as many candidates in each block.

    ``gain_per_candidate_sol`` = -(rejected PnL) / n: what the rule "saves" against entering everything. It is NOT evidence: when
    every candidate loses on average, ANY rule saves. ``gain_over_random_sol`` = (kept PnL - n_kept x mean) / n is the gain over a
    random rule that rejects as many, and ``beats_null`` says whether it is above the null's 95th percentile."""
    kept, hit, counts = [], [], {}
    for block, rows, mask in groups:
        counts[block] = counts.get(block, 0) + sum(1 for m in mask if m)
        for r, m in zip(rows, mask):
            (hit if m else kept).append(r)
    n, nr, nk = len(kept) + len(hit), len(hit), len(kept)
    total = sum(r.pnl_lamports for r in kept) + sum(r.pnl_lamports for r in hit)
    s_rej = sum(r.pnl_lamports for r in hit)
    kept_sol, rej_sol = [_sol(r.pnl_lamports) for r in kept], [_sol(r.pnl_lamports) for r in hit]
    w = welch(kept_sol, rej_sol)

    def rate(group, name):
        return round(sum(1 for r in group if r.label.label == name) / len(group), 6) if group else None
    diff = (_sol(total - s_rej) / nk - _sol(s_rej) / nr) if nk and nr else None
    out = {'n': n, 'n_rejected': nr, 'n_kept': nk,
           'reject_fraction': round(nr / n, 6) if n else None, 'coverage': round(nk / n, 6) if n else None,
           'baseline_mean_pnl_sol': round(_sol(total) / n, 9) if n else None,
           'policy_mean_pnl_sol': round(_sol(total - s_rej) / n, 9) if n else None,
           'gain_per_candidate_sol': round(-_sol(s_rej) / n, 9) if n else None,
           'gain_over_random_sol': round((nr * _sol(total) / n - _sol(s_rej)) / n, 9) if n else None,
           'kept_mean_pnl_sol': round(_sol(total - s_rej) / nk, 9) if nk else None,
           'rejected_mean_pnl_sol': round(_sol(s_rej) / nr, 9) if nr else None,
           'diff_kept_minus_rejected_sol': None if diff is None else round(diff, 9),
           'welch_t': None if w is None else w['t'], 'welch_df': None if w is None else w['df'],
           'welch_p': None if w is None else round(w['p'], 12),
           'rug_rate_rejected': rate(hit, 'RUG'), 'rug_rate_kept': rate(kept, 'RUG'),
           'win_rate_rejected': rate(hit, 'WIN'), 'win_rate_kept': rate(kept, 'WIN')}
    if null is not None:
        out.update({'null_draws': null.draws, 'null_p95_diff_sol': None, 'perm_p': None, 'beats_null': False})
        if nk and nr:
            sums = sorted(null.rejected_sums(counts))         # a random rule's rejected PnL; its diff falls as the sum rises
            at = len(sums) - math.ceil((1 - alpha) * len(sums) - 1e-9)      # the null diff's (1 - alpha) quantile
            s_q = sums[at]
            p = (1 + sum(1 for s in sums if s <= s_rej)) / (len(sums) + 1)
            out.update({'null_p95_diff_sol': round(_sol(total - s_q) / nk - _sol(s_q) / nr, 9), 'perm_p': round(p, 6),
                        'beats_null': s_rej < s_q and p <= alpha})
    return out


def evaluate(rows, rules) -> dict:
    """``contrast`` of a rule set on one window (no null)."""
    return contrast([(0, rows, _mask(rows, rules))])


def _thresholds(values, count=9):
    ordered = sorted(values)
    n = len(ordered)
    if n < 2:
        return []
    return sorted({ordered[min(n - 1, max(0, n * i // (count + 1)))] for i in range(1, count + 1)})


def mine(rows, feature_names, *, min_n=MIN_N, top_k=TOP_CANDIDATES, pair_seeds=PAIR_SEEDS, thresholds=9, max_reject_fraction=MAX_REJECT_FRACTION,
         grid_rows=None):
    """Greedy 1- then 2-condition REJECT rules on ``rows`` (a training fold, enterable candidates), best first by the GAIN OVER A
    RANDOM RULE that rejects as many: ``n_rejected x mean(rows) - sum(rejected)`` (> 0: the rejected candidates are worse than
    average; rejecting at random earns 0 however much every candidate loses). The thresholds are quantiles of ``grid_rows`` (default
    ``rows``; feature values only, no PnL). A rule needs ``min_n`` rejected candidates and must keep at least
    ``1 - max_reject_fraction`` of them; a pair is kept only if it beats BOTH of its single conditions."""
    n = len(rows)
    if n == 0:
        return []
    grid = rows if grid_rows is None else grid_rows
    conds, hits = [], {}
    for name in feature_names:
        for t in _thresholds([r.features[name] for r in grid if r.features.get(name) is not None], thresholds):
            for op in ('>', '<='):
                c = Condition(name, op, t)
                idx = frozenset(i for i, r in enumerate(rows) if c.holds(r))
                if idx:
                    conds.append(c)
                    hits[c] = idx
    pnl = [r.pnl_lamports for r in rows]
    total_all = sum(pnl)

    def excess(idx):                                           # n x (gain over a random rule of the same size), exact integers
        return len(idx) * total_all - n * sum(pnl[i] for i in idx)

    def admissible(idx):
        return min_n <= len(idx) <= max_reject_fraction * n
    found = {}
    for c in conds:
        idx = hits[c]
        if admissible(idx) and excess(idx) > 0:
            found[Rule((c,))] = (excess(idx), idx)
    singles = sorted(found.items(), key=lambda kv: (-kv[1][0], kv[0].key()))
    for rule, (single, _idx) in singles[:pair_seeds]:
        first = rule.conditions[0]
        for c in conds:
            if c.feature == first.feature:
                continue
            idx = hits[first] & hits[c]
            if admissible(idx) and excess(idx) > max(single, excess(hits[c])):
                pair = Rule(tuple(sorted((first, c), key=lambda x: (x.feature, x.op, x.value))))
                if pair not in found:
                    found[pair] = (excess(idx), idx)
    ranked = sorted(found.items(), key=lambda kv: (-kv[1][0], kv[0].key()))
    return [(rule, {'train_n': n, 'train_rejected': len(idx), 'train_reject_fraction': round(len(idx) / n, 6),
                    'train_gain_over_random_sol': round(_sol(e) / n / n, 9)}) for rule, (e, idx) in ranked[:top_k]]


def cross_validate(enterable, plan, feature_names, *, min_n=MIN_N, max_reject_fraction=MAX_REJECT_FRACTION, fdr_q=FDR_Q, alpha=ALPHA,
                   null_draws=NULL_DRAWS, seed=SEED, max_rules=MAX_RULES):
    """The development stage: purged rolling-origin cross-validation by time (``plan_windows``). Each fold mines on its training set
    (the past) only and every rule it mines is scored on the fold's TEST block; a rule mined in several folds is scored on all of their
    test blocks (pooled, each block a stratum of its own null). Every distinct mined rule is one tested hypothesis; Benjamini-Hochberg over all of them
    (Welch p-values), then a survivor must beat its random-rule null. The chosen set (at most ``max_rules``, greedy, each adding
    rejections and gain over random on the development window, the union under the reject cap) goes to the confirm block.
    -> (chosen rules, evidence per rule, fold summaries)."""
    floor = min(MIN_N_FLOOR, min_n)
    dev = enterable[:plan['dev']]
    tests, folds, by_rule = {}, [], {}
    for j, fold in enumerate(plan['folds'], 1):
        a, b = fold['test']
        t0, t1 = fold['train']
        info = {'fold': j, 'test': b - a, 'train': fold['n_train'], 'min_n': fold['min_n'], 'feasible': fold['feasible'], 'mined': 0,
                'train_end_ts': enterable[t1 - 1].ts if t1 > t0 else None, 'test_start_ts': enterable[a].ts, 'test_end_ts': enterable[b - 1].ts}
        if fold['feasible']:
            tests[j] = enterable[a:b]
            mined = mine(enterable[t0:t1], feature_names, min_n=fold['min_n'], max_reject_fraction=max_reject_fraction, grid_rows=dev)
            info['mined'] = len(mined)
            for rule, stats in mined:
                by_rule.setdefault(rule, []).append(dict(stats, fold=j))
        folds.append(info)
    null = RandomRuleNull({j: [r.pnl_lamports for r in rows] for j, rows in tests.items()}, null_draws, seed)
    evidence = []
    for rule in sorted(by_rule, key=Rule.key):
        groups = [(s['fold'], tests[s['fold']], _mask(tests[s['fold']], [rule])) for s in by_rule[rule]]
        m = contrast(groups)
        flags = []
        if m['n_rejected'] < floor or m['n_kept'] < floor or m['welch_p'] is None:
            flags.append('INSUFFICIENT')
        if (m['reject_fraction'] or 0) > max_reject_fraction:
            flags.append('REJECTS_TOO_MUCH')
        if not (m['diff_kept_minus_rejected_sol'] or 0) > 0:
            flags.append('NO_GAIN')
        evidence.append({'rule': rule, 'folds': [s['fold'] for s in by_rule[rule]], 'train': by_rule[rule], 'oos': m, 'flags': flags,
                         'p': m['welch_p'] if not {'INSUFFICIENT', 'REJECTS_TOO_MUCH'} & set(flags) else 1.0, '_groups': groups})
    for e, q in zip(evidence, benjamini_hochberg([e['p'] for e in evidence])):
        e['q'] = round(q, 12)
        if q > fdr_q:
            e['flags'].append('NOT_SIGNIFICANT')
        if not e['flags']:                                     # the BH survivors face the random-rule null on their own blocks
            e['oos'] = contrast(e['_groups'], null, alpha=alpha)
            if not e['oos']['beats_null']:
                e['flags'].append('NOT_ABOVE_NULL')
        e['kind'] = 'PROFITABLE' if (e['oos']['kept_mean_pnl_sol'] or 0) > 0 else 'BLEED_REDUCER'
        del e['_groups']
    eligible = sorted((e for e in evidence if not e['flags']), key=lambda e: (-e['oos']['gain_over_random_sol'], e['q'], e['rule'].key()))
    chosen, before = [], evaluate(dev, [])
    marginal_min = max(1, floor // 2)
    for e in eligible:
        if len(chosen) >= max_rules:
            break
        after = evaluate(dev, chosen + [e['rule']])
        if after['reject_fraction'] > max_reject_fraction or after['n_rejected'] - before['n_rejected'] < marginal_min \
                or (chosen and after['gain_over_random_sol'] <= before['gain_over_random_sol']):
            continue
        chosen.append(e['rule'])
        before = after
    return chosen, evidence, folds


def confirm_verdict(metrics, *, min_confirm=MIN_CONFIRM, alpha=ALPHA) -> str:
    """The ONE test of the chosen set on the confirm block (``contrast`` with its null): CONFIRMED (both groups >= ``min_confirm``,
    Welch p <= ``alpha``, above the random-rule null's 95th percentile, AND the kept candidates make money: kept mean PnL > 0 after
    costs), BLEED_REDUCER (all of that except the kept candidates still lose: never a proposal) or NOT_CONFIRMED. The reject cap is not
    re-applied to the block's realised share (a small block's share is noisy; the set was capped on the development window), but at
    least ``min_confirm`` candidates must be kept."""
    ok = (metrics['n_rejected'] >= min_confirm and metrics['n_kept'] >= min_confirm
          and (metrics['diff_kept_minus_rejected_sol'] or 0) > 0
          and metrics['welch_p'] is not None and metrics['welch_p'] <= alpha
          and metrics.get('beats_null') is True and metrics.get('perm_p') is not None and metrics['perm_p'] <= alpha)
    if not ok:
        return 'NOT_CONFIRMED'
    return 'CONFIRMED' if (metrics['kept_mean_pnl_sol'] or 0) > 0 else 'BLEED_REDUCER'


def confirmed(metrics, **kw) -> bool:
    return confirm_verdict(metrics, **kw) == 'CONFIRMED'


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
def learn(rows, *, strategy: Optional[S.StrategyConfig] = None, confirm_fraction=CONFIRM_FRACTION, folds=FOLDS, min_n=MIN_N,
          min_confirm=MIN_CONFIRM, min_bucket=MIN_BUCKET, embargo_s=0, feature_names=FEATURE_NAMES, now=None, proposals_dir=None,
          dataset_stats=None, alpha=ALPHA, fdr_q=FDR_Q, null_draws=NULL_DRAWS, seed=SEED, max_rules=MAX_RULES,
          max_reject_fraction=MAX_REJECT_FRACTION, consumed_until='auto') -> dict:
    """Tables over every labelled candidate that has features; rules mined / cross-validated / confirmed on the ENTERABLE ones by time.
    ``embargo_s`` is the label horizon (``paths.path_hours`` x 3600). ``consumed_until`` (default: read from ``proposals_dir``'s own
    selection files) is the end of the last confirm block that was tested: a confirm block must start after it plus the embargo.
    With ``proposals_dir`` the selection file is written (always: it says whether it proposes anything)."""
    rows = list(rows)
    feature_names = tuple(feature_names)
    for name, value, low in (('min_n', min_n, 1), ('min_confirm', min_confirm, 2), ('min_bucket', min_bucket, 1), ('folds', folds, 2),
                             ('max_rules', max_rules, 1)):
        if isinstance(value, bool) or not isinstance(value, int) or value < low:
            raise LearnError('%s must be an integer >= %d' % (name, low))
    _check_number('embargo_s', embargo_s, 0, float('inf'), high_open=True)
    _check_number('confirm_fraction', confirm_fraction, 0, 1, low_open=True, high_open=True)
    _check_number('max_reject_fraction', max_reject_fraction, 0, 1, low_open=True)
    _check_number('alpha', alpha, 0, 0.5, low_open=True)
    _check_number('fdr_q', fdr_q, 0, 0.5, low_open=True)
    if isinstance(null_draws, bool) or not isinstance(null_draws, int) or null_draws < NULL_DRAWS:
        raise LearnError('null_draws must be an integer >= %d' % NULL_DRAWS)
    ledger = {'until': None, 'files': 0, 'unreadable': 0, 'source': 'none'}
    if consumed_until == 'auto':
        if proposals_dir is not None:
            ledger = dict(consumed_confirm_until(proposals_dir), source='proposals_dir')
    elif consumed_until is not None:
        _check_number('consumed_until', consumed_until, float('-inf'), float('inf'))
        ledger = dict(ledger, until=float(consumed_until), source='argument')
    enterable = sorted((r for r in rows if r.enterable), key=lambda r: (r.ts, r.mint))
    ts = [r.ts for r in enterable]
    plan_kw = dict(confirm_fraction=confirm_fraction, min_confirm=min_confirm, embargo_s=embargo_s, folds=folds, min_n=min_n,
                   max_reject_fraction=max_reject_fraction, consumed_until=ledger['until'])
    plan = plan_windows(ts, **plan_kw)
    eta = confirm_eta(ts, **plan_kw)
    dev_rows = enterable[:plan['dev']]
    confirm_rows = enterable[plan['confirm'][0]:] if plan['confirm'] else []
    chosen, evidence, fold_info = [], [], []
    if plan['feasible']:
        chosen, evidence, fold_info = cross_validate(enterable, plan, feature_names, min_n=min_n, max_reject_fraction=max_reject_fraction,
                                                     fdr_q=fdr_q, alpha=alpha, null_draws=null_draws, seed=seed, max_rules=max_rules)
    confirm_test, consumed = None, False
    if not plan['feasible']:
        status = 'INSUFFICIENT_DATA'
    elif not evidence:
        status = 'NO_RULE_FOUND'
    elif not chosen:
        status = 'NO_SELECTED_RULE'
    elif not plan['fresh']:
        status = 'CONFIRM_PENDING'                           # the newest block was already tested once: wait for fresh candidates
    else:                                                     # THE one test of the chosen set on the confirm block
        null = RandomRuleNull({'confirm': [r.pnl_lamports for r in confirm_rows]}, null_draws, seed)
        confirm_test = contrast([('confirm', confirm_rows, _mask(confirm_rows, chosen))], null, alpha=alpha)
        status, consumed = confirm_verdict(confirm_test, min_confirm=min_confirm, alpha=alpha), True
    stamp = datetime.datetime.fromtimestamp(0 if now is None else now, datetime.timezone.utc)
    by_label = {label: sum(1 for r in rows if r.label.label == label) for label in LB.LABELS}
    tested = len(evidence)
    result = {
        'label': LABEL, 'generated_at': stamp.strftime('%Y-%m-%dT%H:%M:%SZ'), 'selection_version': SELECTION_VERSION,
        'label_version': LB.LABEL_VERSION,
        'counts': {'candidates': len(rows), 'enterable': len(enterable), 'by_label': by_label, 'dataset': dataset_stats or {}},
        'split': {'scheme': 'purged rolling-origin CV by time (development) + single-use confirm holdout (newest block)',
                  'embargo_s': embargo_s, 'folds': folds, 'confirm_fraction': confirm_fraction, 'enterable': len(enterable),
                  'dev': plan['dev'], 'purged': plan['purged'], 'confirm': plan['n_confirm'],
                  'dev_start_ts': dev_rows[0].ts if dev_rows else None, 'dev_end_ts': dev_rows[-1].ts if dev_rows else None,
                  'confirm_start_ts': confirm_rows[0].ts if confirm_rows else None,
                  'confirm_end_ts': confirm_rows[-1].ts if confirm_rows else None,
                  'confirm_fresh': plan['fresh'], 'consumed_until': ledger['until'], 'ledger': ledger,
                  'feasible_folds': plan['feasible_folds'], 'why': plan['why'], 'cv': fold_info},
        'thresholds': {'min_n': min_n, 'min_n_floor': min(MIN_N_FLOOR, min_n), 'support_share': SUPPORT_SHARE,
                       'fold_min_n': [f['min_n'] for f in plan['folds']], 'min_confirm': min_confirm, 'min_bucket': min_bucket,
                       'alpha': alpha, 'fdr_q': fdr_q, 'null_draws': null_draws, 'seed': seed, 'max_rules': max_rules,
                       'max_reject_fraction': max_reject_fraction},
        'feasibility': dict(eta, feasible=plan['feasible'] and plan['fresh'], why=plan['why']),
        'tables': quantile_tables(rows, feature_names, min_bucket=min_bucket), 'winners_vs_rugs': winners_vs_rugs(rows, feature_names),
        'baseline': {'dev': evaluate(dev_rows, []), 'confirm': evaluate(confirm_rows, [])},
        'tested': {'rules': tested, 'test': 'Welch one-sided (kept mean > rejected mean), out of fold', 'correction': 'Benjamini-Hochberg',
                   'fdr_q': fdr_q, 'passed_bh': sum(1 for e in evidence if 'NOT_SIGNIFICANT' not in e['flags']
                                                     and not {'INSUFFICIENT', 'REJECTS_TOO_MUCH', 'NO_GAIN'} & set(e['flags'])),
                   'passed_null': sum(1 for e in evidence if not e['flags'])},
        'candidates': [{'rule': e['rule'].to_dict(), 'folds': e['folds'], 'train': e['train'], 'oos': e['oos'], 'p': e['p'], 'q': e['q'],
                        'flags': e['flags'], 'kind': e['kind'], 'chosen': e['rule'] in chosen} for e in evidence],
        'selected': [r.to_dict() for r in chosen],
        'union': {'dev': evaluate(dev_rows, chosen), 'confirm': confirm_test} if chosen else None,
        'confirm_test': confirm_test, 'confirm_consumed': consumed, 'confirm_used_for_selection': False,
        'confirmed': status == 'CONFIRMED', 'bleed_reducer': None, 'proposal': None, 'proposal_status': status, 'warnings': [],
    }
    if status == 'BLEED_REDUCER':
        result['bleed_reducer'] = {'rules': [r.to_dict() for r in chosen], 'confirm': confirm_test,
                                   'note': 'These rules beat a random rule on the untouched confirm block, but the candidates they KEEP still '
                                           'lose on average after costs. They reduce the bleed; they are NOT a proposal and NOT a reason to '
                                           'keep or increase trading.'}
    result['warnings'] = _warnings(result)
    if proposals_dir is not None:
        result['proposal'] = str(write_proposal(proposals_dir, result, chosen if result['confirmed'] else [], strategy, feature_names))
    return result


def _warnings(result):
    out = ['Outcomes are a SIMULATION: recorded vault amounts, constant-product quotes, the live exit rules (EXECUTION_UNVERIFIED); '
           'L10 latency fills and L11 DANGER exits are not modelled.']
    tested = result['tested']['rules']
    if tested:
        out.append('%d distinct rules were mined across the folds and tested out of fold (Benjamini-Hochberg at q=%s): the chosen set is '
                   'still the best of many (selection bias). The confirm block only accepts or rejects that set, once; confirm any proposal '
                   'with a forward paper run under a new version.' % (tested, result['tested']['fdr_q']))
    split, feas = result['split'], result['feasibility']
    if split['dev'] < 200 or split['confirm'] < 100:
        out.append('Only %d development / %d confirm candidates: rule evidence is noise-dominated.' % (split['dev'], split['confirm']))
    if not feas['feasible']:
        eta = feas['eta_hours']
        out.append('No feasible fresh confirm block yet (%s); at %s enterable candidates/h the first one is %s.' % (
            ', '.join(feas['why']) or 'n/a', feas['rate_per_hour'],
            'not foreseeable' if eta is None else 'about %.1f h after the newest candidate (%d enterable needed)' % (eta, feas['needed_enterable'])))
    base = result['baseline']['dev']['baseline_mean_pnl_sol']
    if base is not None and base < 0:
        out.append('The baseline loses %.5f SOL per candidate: rejecting ANYTHING "saves" money, so a rule is judged only against a '
                   'random rule that rejects as many, and a proposal needs the KEPT candidates to make money.' % -base)
    if result['proposal_status'] == 'BLEED_REDUCER':
        out.append('BLEED_REDUCER: ' + result['bleed_reducer']['note'])
    if split['ledger'].get('unreadable'):
        out.append('%d selection file(s) were unreadable: each counts as a tested confirm block up to the time in its name.'
                   % split['ledger']['unreadable'])
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
           'bleed_reducer': result['bleed_reducer'],
           'evidence': {'baseline': result['baseline'], 'union': result['union'], 'split': result['split'], 'thresholds': result['thresholds'],
                        'chosen_on': 'dev_cv', 'confirmed_on': 'confirm', 'candidates_mined': len(result['candidates']),
                        'tested': result['tested'], 'feasibility': result['feasibility'], 'confirm_test': result['confirm_test'],
                        'confirm_consumed': result['confirm_consumed'],
                        'selected_on_dev': result['selected'],
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
    split, feas = result['split'], result['feasibility']
    parts = ['<h2>What winners vs rugs looked like at entry (selection learner)</h2>',
             '<p class="flag">%s</p>' % e(result['label']),
             '<p>%d candidates with features (%s); %d would be entered by the current strategy. Labels v%d. On the entered-able ones, by '
             'time: development %d (purged %d at the confirm boundary; purged rolling-origin cross-validation over %d blocks, embargo %s s) / confirm %d. Rules '
             'are mined and chosen on the DEVELOPMENT window only (every rule is scored out of fold; Benjamini-Hochberg over all %d tested '
             'rules; each survivor must beat a random rule that rejects as many). The confirm block is tested ONCE and only accepts or '
             'rejects the chosen set. <b>Never applied automatically.</b></p>' % (
                 result['counts']['candidates'], ', '.join('%s %d' % kv for kv in result['counts']['by_label'].items()), result['counts']['enterable'],
                 result['label_version'], split['dev'], split['purged'], split['folds'], e(split['embargo_s']), split['confirm'],
                 result['tested']['rules'])]
    parts.append('<p>Feasible fresh confirm: <b>%s</b>%s. Enterable rate %s / h; ETA to the first feasible confirm: %s.</p>' % (
        'yes' if feas['feasible'] else 'no', '' if feas['feasible'] else ' (%s)' % e(', '.join(feas['why'])), e(feas['rate_per_hour']),
        'now' if feas['eta_hours'] == 0 else ('unknown' if feas['eta_hours'] is None else
                                              '%s h after the newest candidate (%s enterable)' % (e(feas['eta_hours']), e(feas['needed_enterable'])))))
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
    cand = [[c['rule']['text'], ','.join(str(f) for f in c['folds']), c['oos']['n_rejected'], c['oos']['coverage'], c['oos']['kept_mean_pnl_sol'],
             c['oos']['diff_kept_minus_rejected_sol'], c['oos']['gain_over_random_sol'], c['oos']['welch_p'], c['q'], c['oos'].get('perm_p'),
             c['kind'], ','.join(c['flags']) or 'ok', 'CHOSEN' if c['chosen'] else '']
            for c in result['candidates']]
    parts.append(table(['rule (REJECT if)', 'folds', 'oof rejected', 'coverage', 'kept mean SOL', 'kept - rejected SOL', 'gain over random/cand SOL',
                        'Welch p', 'BH q', 'null p', 'kind', 'flags', ''], cand)
                 if cand else '<p>No rule reached the minimum support in any training fold.</p>')
    if result['union']:
        windows = [(k, v) for k, v in result['union'].items() if v]
        parts.append('<h3>Chosen set</h3>' + table(['window', 'n', 'rejected', 'coverage', 'baseline mean pnl SOL', 'kept mean pnl SOL',
                                                    'kept - rejected SOL', 'gain over random/cand SOL', 'Welch p', 'null p95 diff SOL', 'null p',
                                                    'rug rate rejected', 'rug rate kept'],
                     [[k, v['n'], v['n_rejected'], v['coverage'], v['baseline_mean_pnl_sol'], v['kept_mean_pnl_sol'], v['diff_kept_minus_rejected_sol'],
                       v['gain_over_random_sol'], v['welch_p'], v.get('null_p95_diff_sol'), v.get('perm_p'), v['rug_rate_rejected'],
                       v['rug_rate_kept']] for k, v in windows]) +
                     '<p>* the confirm block is NOT used to choose or drop any rule; it is tested once%s.</p>' % (
                         '' if result['confirm_consumed'] else ' (not tested in this run)'))
    if result['bleed_reducer']:
        parts.append('<h3>BLEED_REDUCER (not a proposal)</h3><p>%s</p>' % e(result['bleed_reducer']['note']))
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
    p.add_argument('--confirm-fraction', type=float, default=CONFIRM_FRACTION, help='the newest share of the enterable candidates: the confirm block')
    p.add_argument('--folds', type=int, default=FOLDS, help='blocks of the purged rolling-origin cross-validation on the development window')
    p.add_argument('--min-n', type=int, default=MIN_N, help='the most rejected candidates a rule needs in a training fold (adapts down to %d)' % MIN_N_FLOOR)
    p.add_argument('--min-confirm', type=int, default=MIN_CONFIRM, help='kept AND rejected candidates the confirm block needs')
    p.add_argument('--max-reject-fraction', type=float, default=MAX_REJECT_FRACTION)
    p.add_argument('--alpha', type=float, default=ALPHA)
    p.add_argument('--fdr-q', type=float, default=FDR_Q)
    p.add_argument('--null-draws', type=int, default=NULL_DRAWS)
    p.add_argument('--seed', type=int, default=SEED)
    p.add_argument('--min-bucket', type=int, default=MIN_BUCKET)
    p.add_argument('--max-late-s', type=float, default=DEFAULT_MAX_LATE_S, help='AT_COLLECTION snapshots later than this after the decision are excluded')
    p.add_argument('--max-gap-fraction', type=float, default=LB.LabelConfig.max_gap_fraction)
    p.add_argument('--max-hole-s', type=float, default=LB.LabelConfig.max_hole_s)
    p.add_argument('--embargo-s', type=float, default=None,
                   help='the label horizon between training and test blocks (default: paths.path_hours x 3600 of --config, else the recorder default)')
    p.add_argument('--config-hash', default=None, help='M1: only candidates decided in runs whose lean.json or strategy hash starts with this (default: the LATEST run of the store, M1b)')
    p.add_argument('--all-configs', action='store_true', help='M1b: every run of the store, whatever its lean.json (vault and effective marks may mix)')
    p.add_argument('--now', type=float, default=None, help='epoch seconds for the file names (default: the current time)')
    args = p.parse_args(argv)
    try:
        from lean import paths as PTH
        path_hours = PTH.DEFAULTS['path_hours']
        if args.config:
            if args.pool_fee_bps is not None:
                raise LearnError('--pool-fee-bps comes from --config')
            from lean.__main__ import ConfigError, load_config
            try:
                cfg = load_config(args.config)
            except ConfigError as error:
                raise LearnError('config: %s' % error) from None
            strategy, pool_fee_bps = S.StrategyConfig.load(cfg['strategy_config']), int(cfg['pool_fee_bps'])
            if isinstance(cfg.get('paths'), dict) and _num(cfg['paths'].get('path_hours')):
                path_hours = cfg['paths']['path_hours']
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
        embargo_s = float(path_hours) * 3600 if args.embargo_s is None else args.embargo_s
        result = learn(dataset.rows, strategy=strategy, confirm_fraction=args.confirm_fraction, folds=args.folds, min_n=args.min_n,
                       min_confirm=args.min_confirm, min_bucket=args.min_bucket, embargo_s=embargo_s, now=now, proposals_dir=out / 'proposals',
                       dataset_stats=dataset.stats, alpha=args.alpha, fdr_q=args.fdr_q, null_draws=args.null_draws, seed=args.seed,
                       max_reject_fraction=args.max_reject_fraction)
        html_path, json_path = write_report(out, result)
    except Exception as error:                               # noqa: BLE001 - one JSON line, never a traceback with paths
        print(json.dumps({'status': 'ERROR', 'code': type(error).__name__, 'detail': str(error)[:200]}))
        return 2
    print(json.dumps({'status': 'OK', 'html': str(html_path), 'json': str(json_path), 'proposal': result['proposal'],
                      'proposal_status': result['proposal_status'], 'confirmed': result['confirmed'],
                      'candidates': result['counts']['candidates'], 'enterable': result['counts']['enterable'],
                      'embargo_s': result['split']['embargo_s'], 'rules_tested': result['tested']['rules'],
                      'eta_hours': result['feasibility']['eta_hours'], 'rate_per_hour': result['feasibility']['rate_per_hour'],
                      'config_hash': config_hash}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
