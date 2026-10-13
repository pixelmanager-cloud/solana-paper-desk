"""L24: linked-wallet clustering (the TRUE controlled supply). FEATURES ONLY: nothing here is read by ``lean.strategy`` and nothing
changes an entry decision or an exit. PAPER ONLY: read-only RPC methods on the shared LOW lane, no signing, no sending.

Why: research on pump.fun launches (MELT / MemeTrans, ~41k launches) finds coordinated accounts hold about a third of the supply,
and that linking accounts by shared non-exchange funders, shared Jito bundles and co-occurrence in one transaction is among the most
predictive signals. Our per-buyer L13 figures did not separate winners from losers; this records the supply held per CLUSTER.

The graph (built AS OF the candidate's decision, ``decision_ts`` = its first PASS/REJECT screen decision, the L13 definition):
  nodes  the top ``top_n`` holders (owners of the largest token accounts, pool vault and burn accounts excluded), the early buyers of
         the first pool window (L13's ``buyer_wallets`` and the buyers of the examined window transactions) and the creator
         (``coin_creator`` of the pool account the screen loaded).
  edges  shared_funder  two nodes whose first funding transfer came from the same source (L13's buyer -> funder map, plus bounded
                        lookups of the largest holders). A detected HUB (a funder of buyers of >= ``hub_min_mints`` distinct mints in
                        ``hub_window_s`` before the decision, from L13's ``wallet_funding`` and our ``cluster_funding`` events) or an
                        ``ignore_funders`` entry never links anything.
         transfer       a direct SOL or token transfer between two nodes (a node's funder is another node; a wallet-to-wallet token
                        move or a SOL transfer between nodes inside an examined window transaction).
         same_slot      first buys in the same slot of the first window (and pre-decision transactions of two examined holders in
                        one slot); a slot with more than ``same_slot_max_group`` such wallets is a crowd, not a link.
         jito_bundle    a run of >= 2 ADJACENT pool signatures in one slot, one of which pays a Jito tip account: the bundle members.
         same_tx        the signers and buyers of one transaction (co-signers, a wallet buying for another), and a signature shared
                        by two examined holders' histories.
  Connected components are the clusters. Shared funding / slots / bundles are SIGNALS, not proof of common ownership (CAVEAT).

Recorded (one ``observations(kind='clusters')`` row per screened candidate; canonical JSON in ``raw``, the figures in ``meta``):
  cluster_count               components with >= 2 nodes
  largest_cluster_supply_pct  supply share held by the holders of the biggest (by supply) such component
  top_cluster_holders         how many top-``top_n`` holders are in that component (``largest_cluster_wallets`` lists them)
  creator_cluster_supply_pct  the creator's wallet plus everything linked to it (the creator's own share when unlinked)
  linked_supply_pct_total     every holder in a component of >= 2 nodes
  jito_bundled_buys           buy transactions inside detected bundles among the examined window transactions
  fresh_wallet_share          examined holders whose FIRST transaction is within ``fresh_s`` (24 h) before the decision, of those whose
                              age could be decided from one bounded history page (``fresh_checked``)
All shares are of the TOTAL supply (the supply the screen read). A figure that cannot be computed is null with its reason in
``unavailable``; that is never an error.

No lookahead (``lean.learn`` guards): the holders are the screen's own ``getTokenLargestAccounts`` snapshot (before the decision,
``holders_source='screen'``); only a candidate whose screen never reached its holder stage reads them at collection
(``holders_source='rpc_at_collection'`` and every holder-based field flagged ``AT_COLLECTION``, ``late_s`` after the decision). The pool
window ends at ``min(graduation + window_s, decision_ts)`` by block time (``window_after_decision_s`` = 0). A holder's history counts only
transactions at or before the decision; its funding transfer is its first transaction (pre-decision, or it is ignored).

Budget: every request goes through the shared LOW lane (``lean.providers.low``) with L13's discipline: ``stop`` is checked before every
request, a request is only made while ``reserve_tokens`` more tokens stay for the other low-lane users (path recorder, L11, L12F,
L13), a ``no_token`` shed is paced (``pace_s``, up to ``shed_wait_s``) on this collector's own thread (no lock held), a shed window
ends the candidate with what it learned. A hard per-candidate cap ``max_calls`` (default 20) counts requests actually SENT. Worst
case with the defaults: 2 pool signature pages + 1 holders (soft rejects only) + 1 owners + 1 supply (only without the screen's)
+ 6 holder lookups x 2 + 4 window transactions = 21 > 20 (in that order, so the cap cuts the last window read); with the screen's
holders and supply (every PASS) it is <= 19, less when L13 already knows a funder or a history page is truncated. Credit shedding (L15) sheds the shared low lane itself, so it reaches this collector too.
"""
import base64
import json
import logging
import threading
import time
from decimal import Decimal

from lean import features as F
from lean import wallet_signals as W
from lean.providers import LANE_SHED, ProviderError, MAX_TX_VERSION

log = logging.getLogger('lean.clusters')

VERSION = 'clusters_v1'
OBSERVATION_KIND = 'clusters'
FUNDING_EVENT = 'cluster_funding'
CAVEAT = 'shared funding, slots and bundles are signals, not proof of common ownership'
SLOT_SECONDS = W.SLOT_SECONDS
# The eight Jito mainnet tip accounts (protocol constants of the Jito block engine), overridable with ``jito_tip_accounts``.
JITO_TIP_ACCOUNTS = (
    '96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5', 'HFqU5x63VTqvQss8hp11i4wVV8bD44PvwucfZ2bU7gRe',
    'Cw8CFyM9FkoMi7K7Crf6HNQqf4uEMzpKw6QNghXLvLkY', 'ADaUMid9yfUytqMBgopwjb2DTLSokTSzL1zt6iGPaS49',
    'DfXygSm4jCyNCybVYYK6DwvWqjKee8pbDmJGcLWNDXjh', 'ADuUkR4vqLUMWXxW9gh6D6L8pMSawimctcNZ5pGwDcEt',
    'DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL', '3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT')
EDGE_TYPES = ('shared_funder', 'transfer', 'same_slot', 'jito_bundle', 'same_tx', 'funding')   # funding: one wallet <- its non-node funder
HOLDER_FIELDS = ('largest_cluster_supply_pct', 'top_cluster_holders', 'creator_cluster_supply_pct', 'linked_supply_pct_total',
                 'fresh_wallet_share')
FIGURES = ('cluster_count',) + HOLDER_FIELDS[:4] + ('jito_bundled_buys', 'fresh_wallet_share')
MAX_EDGES_KEPT = 200

DEFAULTS = {
    'top_n': 20,                   # largest holders in the graph (getTokenLargestAccounts returns at most 20 accounts)
    'window_s': 120,               # the first pool window (cut at the decision)
    'sig_limit': 1000,             # pool signatures per page
    'sig_pages': 2,                # pages walked back looking for the graduation slot
    'max_window_txs': 4,           # earliest window transactions fetched (same slot / bundles / co-signers / transfers)
    'holder_lookups': 6,           # wallets (creator first, then holders by balance) whose history page + funding tx are read
    'history_limit': 25,           # signatures per holder history page (a shorter page is the COMPLETE history: first tx known)
    'min_funding_lamports': 1_000_000,
    'same_slot_max_group': 8,      # a slot shared by more wallets than this is a crowd, not a link
    'fresh_s': 86400,              # a holder whose first transaction is this recent before the decision is "fresh"
    'ignore_funders': [],          # known hubs that must never link
    'jito_tip_accounts': list(JITO_TIP_ACCOUNTS),
    'hub_min_mints': 5,            # a funder of buyers of >= this many distinct mints ...
    'hub_window_s': 86400.0,       # ... within this window before the decision is a hub (never a link)
    'max_calls': 20,               # HARD cap of requests SENT per candidate
    'pace_s': 0.5,                 # wait between tries when the shared low lane has no token right now
    'reserve_tokens': 1,           # only call while this many MORE tokens would remain for the other low-lane users
    'shed_wait_s': 30.0,           # total patience per request; then the candidate stops (or is retried later)
    'max_per_pass': 2,
    'pass_interval_s': 5.0,
    'max_retries': 2,
    'retry_delay_s': 60.0,
    'backfill_max_age_s': 1800,    # candidates graduated longer ago than this are never collected
    'backfill_limit': 50,
    'signals_wait_s': 600.0,       # wait this long after the decision for L13's row (its funder map) before collecting without it
}


class ConfigError(ValueError):
    pass


def make_config(overrides=None):
    """Strict: unknown keys, wrong types and impossible numbers are refused (a typo must not fall back to a default)."""
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict):
        raise ConfigError('clusters must be an object')
    unknown = set(overrides) - set(DEFAULTS) - {'_comment'}
    if unknown:
        raise ConfigError('unknown clusters keys: %s' % sorted(unknown))
    cfg = {**DEFAULTS, **{k: v for k, v in overrides.items() if k != '_comment'}}
    for key, default in DEFAULTS.items():
        value = cfg[key]
        if isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(x, str) and W._BASE58.fullmatch(x) for x in value):
                raise ConfigError('%s must be a list of addresses' % key)
            cfg[key] = list(value)
        elif isinstance(default, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float('inf'), float('-inf')):
                raise ConfigError('%s must be a number' % key)
        elif isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError('%s must be an integer' % key)
    for key in ('top_n', 'window_s', 'sig_limit', 'sig_pages', 'history_limit', 'max_calls', 'max_per_pass', 'backfill_max_age_s',
                'backfill_limit', 'fresh_s'):
        if cfg[key] < 1:
            raise ConfigError('%s must be at least 1' % key)
    if cfg['top_n'] > 20 or cfg['sig_limit'] > 1000 or cfg['history_limit'] > 1000 or cfg['max_calls'] > 200 or cfg['sig_pages'] > 10:
        raise ConfigError('sizes out of range')
    if cfg['max_window_txs'] < 0 or cfg['holder_lookups'] < 0 or cfg['min_funding_lamports'] < 0 or cfg['max_retries'] < 0 \
            or cfg['same_slot_max_group'] < 2:
        raise ConfigError('negative setting')
    if not 0 < cfg['pace_s'] <= 60 or not 0 <= cfg['shed_wait_s'] <= 600 or cfg['pass_interval_s'] <= 0 or cfg['retry_delay_s'] < 0 \
            or not 0 <= cfg['signals_wait_s'] <= 86400:
        raise ConfigError('pace or interval out of range')
    if not 0 <= cfg['reserve_tokens'] <= 10 or cfg['hub_min_mints'] < 2 or not 0 < cfg['hub_window_s'] <= 30 * 86400:
        raise ConfigError('reserve_tokens, hub_min_mints or hub_window_s out of range')
    return cfg


def worst_case_calls(cfg):
    """Requests one candidate can need with every optional read (the cap ``max_calls`` bounds what is actually sent)."""
    return cfg['sig_pages'] + 1 + 1 + 1 + cfg['max_window_txs'] + 2 * cfg['holder_lookups']


# ---------------------------------------------------------------------------------------------------------- parsing (pure)

def _b58(raw):
    from desk.security import base58
    return base58(bytes(raw))


def parse_token_accounts(result, addresses, mint):
    """getMultipleAccounts (base64) of SPL / Token-2022 token accounts -> {token account: (owner, amount)}. A missing account or one
    of another mint is left out (its holder is unknown). Raises ``W.SignalError`` on a malformed body."""
    values = result.get('value') if isinstance(result, dict) else None
    if not isinstance(values, list) or len(values) != len(addresses):
        raise W.SignalError('ACCOUNTS_MALFORMED')
    out = {}
    for address, account in zip(addresses, values):
        if account is None:
            continue
        try:
            data = base64.b64decode(account['data'][0], validate=True)
        except (KeyError, IndexError, TypeError, ValueError):
            raise W.SignalError('ACCOUNTS_MALFORMED') from None
        if len(data) < 165:
            raise W.SignalError('ACCOUNTS_MALFORMED')
        if _b58(data[:32]) != mint:
            continue
        out[address] = (_b58(data[32:64]), int.from_bytes(data[64:72], 'little'))
    return out


def largest_rows(result):
    """getTokenLargestAccounts result (or the screen's retained body's ``result``) -> [(token account, amount)]."""
    rows = result.get('value') if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise W.SignalError('HOLDERS_MALFORMED')
    out = []
    for row in rows:
        try:
            address, amount = row['address'], int(row['amount'])
        except (KeyError, TypeError, ValueError):
            raise W.SignalError('HOLDERS_MALFORMED') from None
        if not isinstance(address, str) or not W._BASE58.fullmatch(address) or amount < 0:
            raise W.SignalError('HOLDERS_MALFORMED')
        out.append((address, amount))
    return out


def _instructions(result):
    message = result['transaction'].get('message')
    if not isinstance(message, dict) or not isinstance(message.get('instructions'), list):
        raise W.SignalError('TRANSACTION_MALFORMED')
    instructions = list(message['instructions'])
    inner = result['meta'].get('innerInstructions') or []
    if not isinstance(inner, list):
        raise W.SignalError('TRANSACTION_MALFORMED')
    for group in inner:
        if isinstance(group, dict) and isinstance(group.get('instructions'), list):
            instructions.extend(group['instructions'])
    return instructions


def tx_links(result, mint, pool, tips):
    """What links wallets inside one jsonParsed transaction: ``{'signers', 'sol_transfers': [(src, dst, lamports)], 'tip': bool,
    'token_senders', 'token_receivers'}``. Token senders / receivers are only filled for a wallet-to-wallet move (the pool's own
    balance of the mint did not change). Raises ``W.SignalError`` on a malformed body; None for no transaction."""
    if result is None:
        return None
    if not isinstance(result, dict) or not isinstance(result.get('transaction'), dict) or not isinstance(result.get('meta'), dict):
        raise W.SignalError('TRANSACTION_MALFORMED')
    message = result['transaction'].get('message')
    if not isinstance(message, dict) or not isinstance(message.get('accountKeys'), list):
        raise W.SignalError('TRANSACTION_MALFORMED')
    signers = [k.get('pubkey') for k in message['accountKeys'] if isinstance(k, dict) and k.get('signer') is True
               and isinstance(k.get('pubkey'), str) and W._BASE58.fullmatch(k['pubkey'])]
    transfers = []
    for ix in _instructions(result):
        parsed = ix.get('parsed') if isinstance(ix, dict) else None
        if not isinstance(parsed, dict) or ix.get('program') != 'system' or parsed.get('type') not in ('transfer', 'transferWithSeed'):
            continue
        info = parsed.get('info')
        if isinstance(info, dict) and isinstance(info.get('source'), str) and isinstance(info.get('destination'), str) \
                and W._is_int(info.get('lamports')) and info['lamports'] > 0 and info['source'] != info['destination']:
            transfers.append((info['source'], info['destination'], info['lamports']))
    out = {'signers': signers, 'sol_transfers': transfers, 'tip': any(dst in tips for _, dst, _ in transfers),
           'token_senders': [], 'token_receivers': []}
    if result['meta'].get('err') is not None:
        return out
    pre, post = W._token_amounts(result['meta'].get('preTokenBalances'), mint), W._token_amounts(result['meta'].get('postTokenBalances'), mint)
    delta = {}
    for _, (owner, amount) in pre.items():
        delta[owner] = delta.get(owner, 0) - amount
    for _, (owner, amount) in post.items():
        delta[owner] = delta.get(owner, 0) + amount
    if delta.get(pool, 0) == 0:
        out['token_senders'] = sorted(o for o, d in delta.items() if d < 0 and o != pool)
        out['token_receivers'] = sorted(o for o, d in delta.items() if d > 0 and o != pool)
    return out


# ---------------------------------------------------------------------------------------------------------- the graph (pure)

class Graph:
    """Union-find over addresses with typed edges. ``blocked`` addresses (hubs, ignored funders, the pool, burn, tip accounts) never
    join anything."""

    def __init__(self, blocked=()):
        self.parent, self.blocked, self.edges = {}, set(blocked), {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def link(self, wallets, kind):
        members = sorted({w for w in wallets if isinstance(w, str) and w not in self.blocked})
        for other in members[1:]:
            a, b = members[0], other
            ra, rb = self.find(a), self.find(b)
            if ra != rb:
                self.parent[max(ra, rb)] = min(ra, rb)
            self.edges.setdefault((a, b), set()).add(kind)
        return len(members) >= 2

    def components(self, nodes):
        groups = {}
        for node in sorted(set(nodes)):
            groups.setdefault(self.find(node), []).append(node)
        return list(groups.values())


def _pct(amount, supply):
    return float((Decimal(amount) * 100 / Decimal(supply)).quantize(Decimal('0.0001')))


def analyze(*, holders, buyers, creator, supply_raw, window, lookups, l13_funders, hubs, cfg, window_ok=True, holders_ok=True):
    """The cluster figures from already collected facts. Pure.

    ``holders`` = {owner: amount} (top-N, exclusions applied); ``buyers`` = [early buyer wallets]; ``window`` = examined window
    transactions oldest first, each ``{'signature', 'slot', 'pos', 'buyers': {w: amount}, 'links': tx_links(...)}``; ``lookups`` =
    {wallet: {'funder', 'fresh', 'sigs': [(signature, slot)]}}; ``l13_funders`` = L13's {buyer: funder or None}; ``hubs`` = detected hub
    funders. Returns a dict with the figures (null + a reason in ``unavailable`` when they cannot be computed)."""
    unavailable = {}
    tips = set(cfg['jito_tip_accounts'])
    blocked = set(hubs) | set(cfg['ignore_funders']) | tips | {W.SYSTEM_PROGRAM, F.INCINERATOR}
    graph = Graph(blocked)
    nodes = set(holders) | set(buyers) | ({creator} if creator else set())
    # shared funder / direct transfer from a funder that is itself a node
    funders = {w: s for w, s in l13_funders.items() if s is not None}
    for wallet, found in lookups.items():
        if found.get('funder') is not None:
            funders[wallet] = found['funder']
    by_source = {}
    for wallet, source in sorted(funders.items()):
        if source in blocked:
            continue
        by_source.setdefault(source, set()).add(wallet)
    for source, wallets in sorted(by_source.items()):
        if source in nodes:
            graph.link(wallets | {source}, 'transfer')            # a node funded another node directly
        else:
            graph.link(wallets | {source}, 'shared_funder' if len(wallets) >= 2 else 'funding')
    # the first pool window: same-slot first buys, bundles, co-signers, transfers
    jito_buys = None
    if window_ok:
        jito_buys = 0
        first_slot = {}
        for tx in window:
            for wallet in tx['buyers']:
                first_slot.setdefault(wallet, tx['slot'])
        by_slot = {}
        for wallet, slot in first_slot.items():
            by_slot.setdefault(slot, set()).add(wallet)
        for slot, wallets in sorted(by_slot.items()):
            if 2 <= len(wallets) <= cfg['same_slot_max_group']:
                graph.link(wallets, 'same_slot')
        runs, current = [], []
        for tx in sorted(window, key=lambda t: t['pos']):
            if current and tx['slot'] == current[-1]['slot'] and tx['pos'] == current[-1]['pos'] + 1:
                current.append(tx)
            else:
                if current:
                    runs.append(current)
                current = [tx]
        if current:
            runs.append(current)
        for run in runs:
            if len(run) >= 2 and any(tx['links']['tip'] for tx in run):
                members = set()
                for tx in run:
                    members |= set(tx['buyers']) | set(tx['links']['signers'])
                graph.link(members, 'jito_bundle')
                jito_buys += sum(1 for tx in run if tx['buyers'])
        known = nodes | {s for tx in window for s in tx['links']['signers']}
        for tx in window:
            graph.link(set(tx['buyers']) | set(tx['links']['signers']), 'same_tx')
            if tx['links']['token_senders'] and tx['links']['token_receivers']:
                graph.link(set(tx['links']['token_senders']) | set(tx['links']['token_receivers']), 'transfer')
            for src, dst, _ in tx['links']['sol_transfers']:
                if src in known and dst in known:
                    graph.link({src, dst}, 'transfer')
    else:
        unavailable['jito_bundled_buys'] = 'WINDOW_UNAVAILABLE'
    # the examined holders' own pre-decision histories: a shared signature (same tx) or slot (same slot)
    by_sig, by_hslot = {}, {}
    for wallet, found in lookups.items():
        for signature, slot in found.get('sigs') or ():
            by_sig.setdefault(signature, set()).add(wallet)
            by_hslot.setdefault(slot, set()).add(wallet)
    for _, wallets in sorted(by_sig.items()):
        if len(wallets) >= 2:
            graph.link(wallets, 'same_tx')
    for _, wallets in sorted(by_hslot.items()):
        if 2 <= len(wallets) <= cfg['same_slot_max_group']:
            graph.link(wallets, 'same_slot')
    # components over the reported nodes
    groups = [g for g in graph.components(nodes) if len(g) >= 2]
    edge_types = {}
    for (a, b), kinds in graph.edges.items():
        edge_types[graph.find(a)] = edge_types.get(graph.find(a), set()) | kinds
    clusters = []
    for group in groups:
        held = sum(holders.get(w, 0) for w in group)
        clusters.append({'wallets': group, 'size': len(group), 'holders': sorted((w for w in group if w in holders), key=lambda w: (-holders[w], w)),
                         'held_raw': held, 'supply_pct': None if supply_raw is None or not holders_ok else _pct(held, supply_raw),
                         'edge_types': sorted(edge_types.get(graph.find(group[0]), ()))})
    clusters.sort(key=lambda c: (-c['held_raw'], -c['size'], c['wallets'][0]))
    out = {'nodes': len(nodes), 'holders_examined': len(holders), 'buyers_examined': len(set(buyers)), 'creator': creator,
           'cluster_count': len(clusters), 'clusters': clusters[:20], 'jito_bundled_buys': jito_buys,
           'edge_counts': {k: sum(1 for kinds in graph.edges.values() if k in kinds) for k in EDGE_TYPES},
           'edges': [[a, b, sorted(k)] for (a, b), k in sorted(graph.edges.items())][:MAX_EDGES_KEPT],
           'hub_funders': sorted({s for s in funders.values() if s in set(hubs)}),
           'largest_cluster_supply_pct': None, 'top_cluster_holders': None, 'largest_cluster_wallets': [],
           'creator_cluster_supply_pct': None, 'linked_supply_pct_total': None, 'holders_supply_pct': None}
    fresh = [found['fresh'] for w, found in lookups.items() if w in holders and found.get('fresh') is not None]
    out['fresh_checked'] = len(fresh)
    out['fresh_wallet_share'] = round(sum(1 for f in fresh if f) / len(fresh), 4) if fresh else None
    if not fresh:
        unavailable['fresh_wallet_share'] = 'NO_HOLDER_AGE_KNOWN'
    if not holders_ok:
        for name in HOLDER_FIELDS:
            out[name] = None
            unavailable[name] = 'HOLDERS_UNAVAILABLE'
        return out, unavailable
    if supply_raw is None:
        for name in ('largest_cluster_supply_pct', 'creator_cluster_supply_pct', 'linked_supply_pct_total'):
            unavailable[name] = 'SUPPLY_UNAVAILABLE'
        out['top_cluster_holders'] = len(clusters[0]['holders']) if clusters else 0
        out['largest_cluster_wallets'] = clusters[0]['holders'] if clusters else []
        return out, unavailable
    out['holders_supply_pct'] = _pct(sum(holders.values()), supply_raw)
    out['largest_cluster_supply_pct'] = clusters[0]['supply_pct'] if clusters else 0.0
    out['top_cluster_holders'] = len(clusters[0]['holders']) if clusters else 0
    out['largest_cluster_wallets'] = clusters[0]['holders'] if clusters else []
    out['linked_supply_pct_total'] = _pct(sum(c['held_raw'] for c in clusters), supply_raw)
    if creator is None:
        unavailable['creator_cluster_supply_pct'] = 'CREATOR_UNKNOWN'
    else:
        root = graph.find(creator)
        out['creator_cluster_supply_pct'] = _pct(sum(a for w, a in holders.items() if graph.find(w) == root), supply_raw)
    return out, unavailable


# ---------------------------------------------------------------------------------------------------------- collection

def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str).encode()


class Clusters:
    """The collector. ``helius`` is a low-lane client; ``store`` is the lean store. ``clusters_pass()`` does a bounded amount of work
    and is meant for its own thread (``run_loop``). Candidates come straight from the store (screened, no ``clusters`` row yet), so a
    restart loses nothing and no queue can overflow."""

    def __init__(self, *, store, helius, cfg=None, code_version, strategy_version, clock=time.time, sleep=time.sleep, stop=None,
                 signals_enabled=True):
        self.store, self.helius, self.clock, self.sleep = store, helius, clock, sleep
        self.cfg = make_config(cfg)
        self.code_version, self.strategy_version = code_version, strategy_version
        self.stop = stop or threading.Event()
        self.signals_enabled = signals_enabled     # False: L13 is off, never wait for its row
        self._attempts = {}
        self.last_pending = None
        self.prefetch = None                       # W2b #8: lean.prefetch.Prefetch (set by build_runner when configured)
        self.counts = {'collected': 0, 'partial': 0, 'calls': 0, 'sheds': 0, 'yielded': 0, 'shed_stops': 0, 'cap_stops': 0,
                       'transient_failures': 0, 'errors': 0, 'stopped': 0, 'waiting_for_signals': 0}

    # -- plumbing ---------------------------------------------------------------------------------------------------------
    def _query(self, sql, args=()):
        with self.store._lock:
            return self.store.db.execute(sql, args).fetchall()

    def _write(self, function, *args, **kwargs):
        return function(*args, code_version=self.code_version, strategy_version=self.strategy_version, **kwargs)

    def health(self):
        return {'enabled': True, **self.counts, 'pending': self.last_pending}

    def _record_error(self, code, mint, message):
        try:
            self._write(self.store.add_error, code, transient=True, scope='clusters', mint=mint, message=message, ts=self.clock())
        except Exception:
            pass

    def pending(self, now, limit):
        """[(cid, mint, pool, slot, decision_ts, has_signals)]: screened (first PASS/REJECT decision known), graduated within
        ``backfill_max_age_s``, no ``clusters`` row yet. Oldest first."""
        return self._query(
            "SELECT c.id,c.mint,c.pool,c.slot,(SELECT MIN(d.ts) FROM decisions d WHERE d.mint=c.mint AND d.candidate_id=c.id "
            "AND d.kind='screen' AND d.action IN ('PASS','REJECT')) AS decision_ts, "
            "EXISTS(SELECT 1 FROM observations s WHERE s.mint=c.mint AND s.kind='wallet_signals') FROM candidates c "
            "WHERE c.migrated_at>? AND c.pool IS NOT NULL AND c.slot IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.mint=c.mint AND o.kind=?) AND decision_ts IS NOT NULL "
            'ORDER BY c.id LIMIT ?', (now - self.cfg['backfill_max_age_s'], OBSERVATION_KIND, int(limit)))

    def clusters_pass(self):
        """One bounded pass: collect up to ``max_per_pass`` candidates whose decision is known (and whose L13 row exists, or
        ``signals_wait_s`` passed since the decision)."""
        now = self.clock()
        done = 0
        rows = self.pending(now, self.cfg['backfill_limit'])
        self.last_pending = len(rows)
        for cid, mint, pool, slot, decision_ts, has_signals in rows:
            if self.stop.is_set() or done >= self.cfg['max_per_pass']:
                break
            attempts, not_before = self._attempts.get(mint, (0, 0.0))
            if now < not_before:
                continue
            if self.signals_enabled and not has_signals and now - decision_ts < self.cfg['signals_wait_s']:
                self.counts['waiting_for_signals'] += 1
                continue
            try:
                self.collect(cid, mint, pool, slot, decision_ts=decision_ts)
            except Exception as error:              # recorded; a recorder failure never reaches entries or exits
                self.counts['errors'] += 1
                self._record_error('CLUSTERS_FAILED', mint, type(error).__name__)
                self._attempts[mint] = (attempts + 1, now + self.cfg['retry_delay_s'])
                if attempts + 1 > self.cfg['max_retries']:
                    self._close_out(cid, mint, decision_ts, 'COLLECT_FAILED:' + type(error).__name__)
            done += 1
        return done

    def run_loop(self):
        while not self.stop.is_set():
            try:
                self.clusters_pass()
            except Exception:
                self.counts['errors'] += 1
            self.stop.wait(self.cfg['pass_interval_s'])

    def _close_out(self, cid, mint, decision_ts, reason):
        try:
            self._attempts.pop(mint, None)
            now = self.clock()
            row = {'version': VERSION, 'caveat': CAVEAT, 'mint': mint, 'partial': True, 'calls': 0, 'collected_at': now,
                   'decision_ts': decision_ts, 'late_s': round(now - decision_ts, 3), 'window_after_decision_s': None,
                   'unavailable': {'collect': reason}, 'flags': {}, **{name: None for name in FIGURES}}
            self._write(self.store.add_observation, OBSERVATION_KIND, _canonical(row), mint=mint, candidate_id=cid, meta=_meta(row), ts=now)
        except Exception:
            self.counts['errors'] += 1

    # -- the shared low lane (L13's discipline) ------------------------------------------------------------------------------
    def _yield_lane(self):
        reserve = self.cfg['reserve_tokens']
        limiter = getattr(getattr(self.helius, 'transport', None), 'limiter', None)
        available = getattr(limiter, 'available', None)
        if not reserve or available is None:
            return False
        return available() < 1 + reserve

    def _rpc(self, calls, method, params):
        """One request. ``LANE_SHED`` sent nothing and does not count; a ``no_token`` shed (or a yield) is paced up to
        ``shed_wait_s``; a shed window or lost patience stops the candidate (or, before anything was learned, raises a transient
        error so it is retried later). ``stop``, the cap: checked before every try."""
        waited = 0.0
        while True:
            calls.check()
            if self._yield_lane():
                self.counts['yielded'] += 1
                why, error = 'no_token', ProviderError(LANE_SHED, True, meta={'why': 'yield'})
            else:
                try:
                    result, _raw, _meta = self.helius.rpc(method, params)
                    break
                except ProviderError as failure:
                    if failure.code != LANE_SHED:
                        calls.used += 1
                        self.counts['calls'] += 1
                        raise
                    self.counts['sheds'] += 1
                    why, error = failure.meta.get('why'), failure
            if why != 'no_token' or waited >= (self.cfg['shed_wait_s'] if calls.patience is None else calls.patience):
                if not calls.learned:
                    raise error
                raise W._Cap(LANE_SHED) from None
            self.sleep(self.cfg['pace_s'])
            waited += self.cfg['pace_s']
        calls.used += 1
        self.counts['calls'] += 1
        calls.learned = True
        return result

    # -- free evidence from the store -----------------------------------------------------------------------------------------
    def _screen(self, cid, mint, decision_ts):
        """(screen features of the first PASS/REJECT decision, holders body, pool-account body), all written before the decision."""
        feats = {}
        for row in self.store.rows('decisions', mint=mint, kind='screen', limit=100):
            if row['candidate_id'] == cid and row['action'] in ('PASS', 'REJECT'):
                try:
                    feats = json.loads(row['features']) or {}
                except ValueError:
                    feats = {}
                break
        bodies = {}
        for label in ('holders', 'accounts_mint_pool'):
            got = self._query('SELECT raw, raw_truncated FROM observations WHERE mint=? AND candidate_id=? AND kind=? AND ts<=? '
                              'ORDER BY id DESC LIMIT 1', (mint, cid, 'screen:' + label, decision_ts + 1e-6))
            bodies[label] = bytes(got[0][0]) if got and not got[0][1] else None
        return feats, bodies['holders'], bodies['accounts_mint_pool']

    def _signals(self, mint):
        got = self._query("SELECT raw FROM observations WHERE mint=? AND kind='wallet_signals' ORDER BY id LIMIT 1", (mint,))
        try:
            return json.loads(bytes(got[0][0])) if got else None
        except (ValueError, TypeError):
            return None

    def hubs_asof(self, asof, mint, sources):
        """Funders among ``sources`` that funded buyers of >= ``hub_min_mints`` distinct mints (this one included) within
        ``hub_window_s`` before ``asof``, from the persisted L13 ``wallet_funding`` and our ``cluster_funding`` events."""
        sources = sorted({s for s in sources if s is not None})
        if not sources:
            return set()
        rows = self._query("SELECT payload, ts FROM events WHERE kind IN ('wallet_funding', ?) AND ts>? AND ts<=?",
                           (FUNDING_EVENT, asof - self.cfg['hub_window_s'], asof))
        mints = {}
        for payload, _ts in rows:
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if data.get('funder') in sources and data.get('mint') != mint:
                mints.setdefault(data['funder'], set()).add(data.get('mint'))
        return {s for s in sources if len(mints.get(s, ())) + 1 >= self.cfg['hub_min_mints']}

    # -- one candidate --------------------------------------------------------------------------------------------------------
    def collect(self, cid, mint, pool, grad_slot, *, decision_ts, sink=None, screen_data=None, signals=None):
        """Collect, analyze and record one candidate AS OF ``decision_ts``. A stop writes nothing; a transient failure before anything
        was learned is retried later; everything else is recorded with its reasons.

        W2b #8: with a ``sink`` (a dict) this is a PREFETCH: ``decision_ts`` is the AS-OF time the window ends at (never later than the
        decision the row will be committed to); ``screen_data`` = (screen features, holders body, pool-account body) of the shadow screen
        and ``signals`` = the prefetched wallet-signals row replace the two store reads; nothing is written, ``sink`` receives ``row`` /
        ``lookups`` / ``finished_at``; a failure leaves no trace in this collector's retry bookkeeping. Without a sink, a finished prefetch
        of this candidate is committed instead of collecting (no request)."""
        # --- W2b hook ---
        if sink is None and self.prefetch is not None:
            committed = self.prefetch.commit_clusters(self, cid, mint, pool, grad_slot, decision_ts)
            if committed is not None:
                return committed
        # --- end W2b ---
        cfg, started = self.cfg, self.clock()
        calls = W._Calls(cfg['max_calls'], None, self.stop)
        if sink is not None:
            calls.patience = sink.get('patience_s')
        unavailable, flags, notes = {}, {}, {}
        feats, holders_body, pool_body = self._screen(cid, mint, decision_ts) if sink is None else screen_data
        signals = self._signals(mint) if sink is None else signals
        l13_funders = dict((signals or {}).get('funders') or {})
        l13_hubs = set((signals or {}).get('hub_funders') or ())
        buyers = list((signals or {}).get('buyer_wallets') or [])
        creator, why = F.dev_wallet({'raw': [('accounts_mint_pool', pool_body)]} if pool_body is not None else {'raw': []})
        if creator is None:
            unavailable['creator'] = why
        supply = feats.get('supply_raw')
        supply = int(supply) if isinstance(supply, (int, str)) and str(supply).isdecimal() and int(supply) > 0 else None
        supply_source = ('screen' if sink is None else 'prefetch') if supply is not None else None
        holders, holders_source, holders_ok, holders_at = {}, None, False, None
        window, window_ok = [], False
        lookups = {}
        try:
            # -- 1. the top holders: the screen's snapshot (pre-decision), else one read at collection (flagged)
            try:
                if holders_body is not None:
                    rows = largest_rows(json.loads(holders_body)['result'])
                    holders_source = 'screen' if sink is None else 'prefetch'
                else:
                    rows = largest_rows(self._rpc(calls, 'getTokenLargestAccounts', [mint, {'commitment': 'confirmed'}]))
                    holders_source = 'rpc_at_collection'
                    holders_at = self.clock()                        # W2b2 #2: when this snapshot was read
                skip = F.exclusions({'features': feats}) or set()
                skip.add(feats.get('pool_base_token_account'))
                rows = [(a, n) for a, n in rows if a not in skip and n > 0]
                if rows:
                    accounts = [a for a, _ in rows]
                    owned = parse_token_accounts(self._rpc(calls, 'getMultipleAccounts', [accounts, {'encoding': 'base64', 'commitment': 'confirmed'}]),
                                                 accounts, mint)
                    for address, amount in rows:
                        if address not in owned:
                            notes['owners_missing'] = notes.get('owners_missing', 0) + 1
                            continue
                        owner, _ = owned[address]
                        if owner in (pool, F.INCINERATOR, W.SYSTEM_PROGRAM):
                            continue
                        holders[owner] = holders.get(owner, 0) + amount
                holders = dict(sorted(holders.items(), key=lambda kv: (-kv[1], kv[0]))[:cfg['top_n']])
                holders_ok = True
            except ProviderError as error:
                if not calls.learned and error.transient:        # nothing learned yet: retried later (a stop or shed sent nothing)
                    raise
                unavailable['holders'] = 'HOLDERS_UNAVAILABLE:' + error.code
            except (W.SignalError, ValueError, KeyError, TypeError) as error:
                unavailable['holders'] = getattr(error, 'code', 'HOLDERS_MALFORMED')
            if supply is None:
                try:
                    supply = W.parse_supply(self._rpc(calls, 'getTokenSupply', [mint]))
                    supply_source = 'rpc_after_decision'
                except ProviderError as error:
                    unavailable['supply'] = 'SUPPLY_UNAVAILABLE:' + error.code
                except W.SignalError as error:
                    unavailable['supply'] = error.code
            # -- 2. bounded wallet lookups: the creator first, then the largest holders (history page + funding transaction)
            targets = ([creator] if creator else []) + [w for w in holders if w != creator]
            for wallet in targets[:cfg['holder_lookups']]:
                lookups[wallet] = self._lookup(calls, wallet, decision_ts, l13_funders.get(wallet), unavailable)
            # -- 3. the first pool window, cut at the decision
            window, window_ok = self._window(calls, mint, pool, grad_slot, decision_ts, unavailable, notes)
        except W._Cap as stop:
            if stop.code == 'STOPPED':
                self.counts['stopped'] += 1
                return None
            unavailable['stopped'] = stop.code
            self.counts['shed_stops' if stop.code == LANE_SHED else 'cap_stops'] += 1
        except ProviderError as error:
            if sink is not None and not calls.learned and error.transient:      # W2b #8: the prefetch retries on its own schedule
                return None
            if not calls.learned and error.transient:
                attempts = self._attempts.get(mint, (0, 0.0))[0] + 1
                if attempts <= cfg['max_retries']:
                    self._attempts[mint] = (attempts, self.clock() + cfg['retry_delay_s'])
                    self.counts['transient_failures'] += 1
                    return None
            unavailable['collect'] = 'PROVIDER_' + error.code
        self._attempts.pop(mint, None)
        for tx in window:
            buyers.extend(w for w in tx['buyers'] if w not in buyers)
        hubs = l13_hubs | self.hubs_asof(decision_ts, mint, list(l13_funders.values()) + [x.get('funder') for x in lookups.values()])
        figures, missing = analyze(holders=holders, buyers=buyers, creator=creator, supply_raw=supply, window=window, lookups=lookups,
                                   l13_funders=l13_funders, hubs=hubs, cfg=cfg, window_ok=window_ok, holders_ok=holders_ok)
        unavailable = {**missing, **unavailable}
        if holders_source == 'rpc_at_collection':                # a snapshot read AFTER the decision (soft rejects only)
            for name in HOLDER_FIELDS + ('cluster_count',):
                if figures.get(name) is not None:
                    flags[name] = 'AT_COLLECTION'
        row = {'version': VERSION, 'caveat': CAVEAT, 'mint': mint, 'pool': pool, 'grad_slot': grad_slot, 'window_after_decision_s': None,
               **notes, **figures,
               'holders_source': holders_source, 'supply_source': supply_source, 'signals_row': signals is not None,
               'lookups': {w: {'funder': x.get('funder'), 'funder_source': x.get('funder_source'), 'fresh': x.get('fresh'),
                               'first_ts': x.get('first_ts')} for w, x in sorted(lookups.items())},
               'calls': calls.used, 'call_cap': cfg['max_calls'], 'decision_ts': decision_ts, 'collected_at': started,
               'late_s': round(started - decision_ts, 3), 'flags': flags, 'unavailable': unavailable, 'partial': bool(unavailable)}
        # --- W2b hook: a prefetch keeps the row; the commit writes it stamped with the real decision ---
        if sink is not None:
            if holders_at is not None:
                row['snapshot_at'] = {'holders': holders_at}          # W2b2 #2: the learner refuses a holder list older than max_snapshot_age_s
            sink.update(row=row, lookups=lookups, finished_at=self.clock(), window_end_ts=decision_ts)
            return row
        # --- end W2b ---
        self._persist(row, lookups, cid, mint)
        return row

    def _persist(self, row, lookups, cid, mint):
        """Write one ``clusters`` row and the ``cluster_funding`` events of the funders it looked up itself."""
        self._write(self.store.add_observation, OBSERVATION_KIND, _canonical(row), mint=mint, candidate_id=cid, meta=_meta(row), ts=self.clock())
        for wallet, found in sorted(lookups.items()):
            if found.get('funder') is not None and found.get('funder_source') == 'lookup':
                self._write(self.store.record, FUNDING_EVENT, {'funder': found['funder'], 'wallet': wallet, 'mint': mint, 'candidate_id': cid},
                            ts=self.clock())
        self.counts['collected'] += 1
        self.counts['partial'] += 1 if row['partial'] else 0

    def _lookup(self, calls, wallet, decision_ts, known_funder, unavailable):
        """One history page of ``wallet`` (+ its first transaction when the page is its complete history and its funder is not
        known from L13). -> {'funder', 'funder_source', 'fresh', 'first_ts', 'sigs': [(signature, slot)] at or before the decision}."""
        cfg = self.cfg
        key = 'lookup:' + wallet[:8]
        out = {'funder': known_funder, 'funder_source': 'l13' if known_funder else None, 'fresh': None, 'first_ts': None, 'sigs': []}
        try:
            page = W.parse_signatures(self._rpc(calls, 'getSignaturesForAddress', [wallet, {'limit': cfg['history_limit'], 'commitment': 'confirmed'}]))
        except ProviderError as error:
            if error.code == LANE_SHED:
                raise                                 # nothing learned yet (else _rpc stops the candidate itself)
            unavailable[key] = 'HISTORY_UNAVAILABLE:' + error.code
            return out
        except W.SignalError as error:
            unavailable[key] = error.code
            return out
        out['sigs'] = [(s['signature'], s['slot']) for s in page if s['block_time'] is not None and s['block_time'] <= decision_ts]
        complete = len(page) < cfg['history_limit']
        oldest = page[-1] if page else None
        if not page:
            unavailable[key] = 'NO_HISTORY'
            return out
        if complete:
            out['first_ts'] = oldest['block_time']
            if oldest['block_time'] is None:
                unavailable[key] = 'FIRST_TX_TIME_UNKNOWN'
            elif oldest['block_time'] > decision_ts:
                unavailable[key] = 'FIRST_TX_AFTER_DECISION'
            else:
                out['fresh'] = oldest['block_time'] >= decision_ts - cfg['fresh_s']
        elif oldest['block_time'] is not None and oldest['block_time'] < decision_ts - cfg['fresh_s']:
            out['fresh'] = False                      # even the oldest transaction on the page is older than the fresh window
        if known_funder is not None:
            return out
        if not complete:
            unavailable[key] = 'HISTORY_TRUNCATED'
            return out
        if oldest['block_time'] is None or oldest['block_time'] > decision_ts or oldest['failed']:
            return out
        try:
            body = self._rpc(calls, 'getTransaction', [oldest['signature'], {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                                             'maxSupportedTransactionVersion': MAX_TX_VERSION}])
            options = [t for t in W.transfers_to(body, wallet) if t[1] >= cfg['min_funding_lamports']]
        except ProviderError as error:
            if error.code == LANE_SHED:
                raise                                 # nothing learned yet (else _rpc stops the candidate itself)
            unavailable[key] = 'FUNDING_UNAVAILABLE:' + error.code
            return out
        except W.SignalError as error:
            unavailable[key] = error.code
            return out
        if options:
            out['funder'], out['funder_source'] = max(options, key=lambda t: t[1])[0], 'lookup'
        else:
            unavailable[key] = 'NO_FUNDING_TRANSFER'
        return out

    def _window(self, calls, mint, pool, grad_slot, decision_ts, unavailable, notes):
        """The examined first-window transactions (oldest first, each with its position in the pool's signature list) and whether
        the window could be read. Nothing after the decision (by block time; an unknown block time is out)."""
        cfg = self.cfg
        seen, before, reached = [], None, False
        try:
            for page in range(cfg['sig_pages']):
                options = {'limit': cfg['sig_limit'], 'commitment': 'confirmed'}
                if before:
                    options['before'] = before
                rows = W.parse_signatures(self._rpc(calls, 'getSignaturesForAddress', [pool, options]))
                seen.extend(rows)
                if len(rows) < cfg['sig_limit'] or rows[-1]['slot'] <= grad_slot:
                    reached = True
                    break
                before = rows[-1]['signature']
        except ProviderError as error:
            if error.code == LANE_SHED:
                raise                                 # nothing learned yet (else _rpc stops the candidate itself)
            unavailable['window'] = 'SIGNATURES_UNAVAILABLE:' + error.code
            return [], False
        except W.SignalError as error:
            unavailable['window'] = error.code
            return [], False
        if not reached:
            unavailable['window'] = 'WINDOW_START_NOT_REACHED'
            return [], False
        ordered = list(reversed(seen))
        last_slot = grad_slot + int(cfg['window_s'] / SLOT_SECONDS)
        window = [(pos, s) for pos, s in enumerate(ordered) if not s['failed'] and grad_slot <= s['slot'] <= last_slot
                  and s['block_time'] is not None and s['block_time'] <= decision_ts]
        notes.update(window_txs=len(window), window_after_decision_s=0.0, window_truncated=len(window) > cfg['max_window_txs'])
        tips = set(cfg['jito_tip_accounts'])
        out, skipped = [], 0
        for pos, entry in window[:cfg['max_window_txs']]:
            try:
                body = self._rpc(calls, 'getTransaction', [entry['signature'], {'encoding': 'jsonParsed', 'commitment': 'confirmed',
                                                                                'maxSupportedTransactionVersion': MAX_TX_VERSION}])
                view = W.parse_swap_tx(body, entry['signature'], mint, pool)
                links = tx_links(body, mint, pool, tips)
            except ProviderError as error:
                if error.code == LANE_SHED:
                    raise
                skipped += 1
                continue
            except W.SignalError:
                skipped += 1
                continue
            if view is None or view['failed'] or view['block_time'] is None or view['block_time'] > decision_ts:
                skipped += 1
                continue
            out.append({'signature': entry['signature'], 'slot': view['slot'], 'pos': pos, 'buyers': view['buyers'], 'links': links})
        notes.update(examined_txs=len(out), tx_skipped=skipped)
        return out, True


def _meta(row):
    keys = ('version',) + FIGURES + ('fresh_checked', 'holders_examined', 'buyers_examined', 'nodes', 'edge_counts', 'holders_source',
                                     'supply_source', 'calls', 'partial', 'unavailable', 'flags', 'decision_ts', 'collected_at', 'late_s',
                                     'window_after_decision_s', 'hub_funders', 'signals_row')
    return {k: row.get(k) for k in keys}


def build(cfg, *, store, keys, code_version, strategy_version, clock=time.time, transport_kwargs=None, signals_enabled=True):
    """The collector of a validated config, on the shared LOW lane (``lean.providers.low``)."""
    from lean import providers
    transport_kwargs = dict(transport_kwargs or {})
    helius = providers.low(keys, **transport_kwargs).helius
    return Clusters(store=store, helius=helius, cfg=cfg, code_version=code_version, strategy_version=strategy_version, clock=clock,
                    sleep=transport_kwargs.get('sleep', time.sleep), signals_enabled=signals_enabled)
