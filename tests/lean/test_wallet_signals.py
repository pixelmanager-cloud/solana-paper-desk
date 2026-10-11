"""SYNTHETIC_TEST_ONLY: L13 bundle / sniper detection and the smart-wallet signal. Fixtures only, no network.

``Chain`` is a tiny fake of the three RPC methods the collector uses (getSignaturesForAddress, getTransaction, getTokenSupply)
that answers with the public jsonParsed response shapes; ``ChainHelius`` puts it behind ``Helius.rpc``'s return convention so
unit tests need no HTTP, and ``Chain.rpc_handlers()`` plugs the same data into ``tests/lean/fakeworld.py`` for the e2e test.
"""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from desk.security import base58
from lean import wallet_signals as W
from lean.paper import Fill, LABEL
from lean.providers import ProviderError, RateLimiter
from lean.store import Store

GRAD = 1_000
SUPPLY = 10 ** 15
WINDOW_SLOTS = int(120 / 0.4)


def addr(n):
    return base58(bytes([n]) * 32)


def sig(n):
    return base58(bytes([n % 250 + 1, n // 250 + 1]) * 32)


POOL, MINT = addr(200), addr(201)


def tb(owner, amount, index, mint=MINT):
    return {'accountIndex': index, 'mint': mint, 'owner': owner, 'programId': 'Tokenkeg', 'uiTokenAmount': {'amount': str(amount), 'decimals': 6}}


def swap_body(signature, slot, buyers, *, mint=MINT, pool=POOL, pool_pre=10 ** 14, err=None, payer=None, block_time=1_800_000_000):
    """A buy: the pool pays out ``sum(buyers.values())``; ``buyers`` = {wallet: tokens received}."""
    out = sum(buyers.values())
    pre = [tb(pool, pool_pre, 1, mint)] + [tb(w, 0, 10 + i, mint) for i, w in enumerate(buyers)]
    post = [tb(pool, pool_pre - out, 1, mint)] + [tb(w, a, 10 + i, mint) for i, (w, a) in enumerate(buyers.items())]
    first = payer or next(iter(buyers), addr(250))
    return {'slot': slot, 'blockTime': block_time, 'version': 0,
            'transaction': {'signatures': [signature], 'message': {'accountKeys': [{'pubkey': first, 'signer': True, 'writable': True, 'source': 'transaction'}],
                                                                   'instructions': []}},
            'meta': {'err': err, 'fee': 5000, 'preTokenBalances': pre, 'postTokenBalances': post, 'innerInstructions': []}}


def funding_body(signature, slot, destination, source, lamports=500_000_000, *, inner=False):
    transfer = {'program': 'system', 'programId': W.SYSTEM_PROGRAM, 'parsed': {'type': 'transfer', 'info': {'source': source, 'destination': destination, 'lamports': lamports}}}
    message = {'accountKeys': [{'pubkey': source, 'signer': True, 'writable': True}], 'instructions': [] if inner else [transfer]}
    meta = {'err': None, 'preTokenBalances': [], 'postTokenBalances': [], 'innerInstructions': [{'index': 0, 'instructions': [transfer]}] if inner else []}
    return {'slot': slot, 'blockTime': 1_799_999_000, 'transaction': {'signatures': [signature], 'message': message}, 'meta': meta}


class Chain:
    """Per-address signature lists (newest first, as the RPC returns them) and transaction bodies."""

    def __init__(self):
        self.addresses, self.bodies, self.supply = {}, {}, {}
        self.calls, self._n, self._seq = [], 0, 0
        self.fail = {}                      # method -> ProviderError (or callable(params) -> error/None)
        self.null_tx = set()

    def next_sig(self):
        self._n += 1
        return sig(self._n)

    def add(self, address, signature, slot, body=None, *, err=None):
        """Index ``signature`` under ``address``; oldest first insertion order is irrelevant (kept sorted newest first)."""
        rows = self.addresses.setdefault(address, [])
        self._seq += 1
        rows.append({'signature': signature, 'slot': slot, 'err': err, 'blockTime': 1_800_000_000 + slot, 'memo': None,
                     'confirmationStatus': 'finalized', '_seq': self._seq})
        rows.sort(key=lambda r: (-r['slot'], -r['_seq']))                      # newest first; later in the slot = newer
        if body is not None:
            self.bodies[signature] = body
        return signature

    def swap(self, slot, buyers, **kwargs):
        signature = self.next_sig()
        body = swap_body(signature, slot, buyers, **kwargs)
        self.add(kwargs.get('pool', POOL), signature, slot, body, err=kwargs.get('err'))
        for wallet in buyers:
            self.add(wallet, signature, slot)
        return signature

    def fund(self, wallet, source, *, slot=GRAD - 50, lamports=500_000_000, inner=False):
        signature = self.next_sig()
        self.add(wallet, signature, slot, funding_body(signature, slot, wallet, source, lamports, inner=inner))
        return signature

    # -- the RPC surface
    def handle(self, method, params, t=None):
        self.calls.append((method, params[0] if params else None))
        failure = self.fail.get(method)
        if callable(failure):
            failure = failure(params)
        if failure is not None:
            raise failure
        if method == 'getSignaturesForAddress':
            address, options = params
            rows = self.addresses.get(address, [])
            if options.get('before'):
                at = next((i for i, r in enumerate(rows) if r['signature'] == options['before']), None)
                rows = [] if at is None else rows[at + 1:]
            return [{k: v for k, v in r.items() if k != '_seq'} for r in rows[:options['limit']]]
        if method == 'getTransaction':
            assert params[1]['encoding'] == 'jsonParsed' and params[1]['maxSupportedTransactionVersion'] == 0
            return None if params[0] in self.null_tx else json.loads(json.dumps(self.bodies[params[0]]))
        if method == 'getTokenSupply':
            return {'context': {'slot': 1}, 'value': {'amount': str(self.supply.get(params[0], SUPPLY)), 'decimals': 6}}
        raise AssertionError('unexpected RPC ' + method)

    def rpc_handlers(self):
        return {m: (lambda params, t, m=m: self.handle(m, params, t)) for m in ('getSignaturesForAddress', 'getTransaction', 'getTokenSupply')}

    def count(self, method=None):
        return sum(1 for m, _ in self.calls if method in (None, m))


class ChainHelius:
    """``Helius.rpc`` convention: (result, raw bytes, meta) or a typed ProviderError."""

    def __init__(self, chain):
        self.chain = chain

    def rpc(self, method, params):
        result = self.chain.handle(method, params)
        return result, json.dumps(result, sort_keys=True).encode(), {'attempts': 1}


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.now = 1_800_000_000.0
        self.store = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s', clock=lambda: self.now)
        self.addCleanup(self.store.close)

    def candidate(self, n=1, *, mint=None, pool=POOL, slot=GRAD, migrated_at=None, screen='PASS', age=300):
        """A screened candidate old enough that its window is complete."""
        mint = mint or MINT
        cid = self.store.add_candidate(mint, pool=pool, slot=slot, migrated_at=(self.now - age) if migrated_at is None else migrated_at,
                                       hint_seq=n, ts=self.now - age)
        if screen:
            self.store.add_decision('screen', screen, mint=mint, candidate_id=cid, reasons=[], features={}, ts=self.now - age)
        return cid, mint

    def collector(self, chain, cfg=None, **kwargs):
        cfg = {'max_per_pass': 10, **(cfg or {})}
        return W.WalletSignals(store=self.store, helius=ChainHelius(chain), cfg=cfg, code_version='t', strategy_version='s',
                               clock=lambda: self.now, **kwargs)

    def signals(self, mint=MINT):
        rows = self.store.rows('observations', mint=mint, kind='wallet_signals')
        return [json.loads(r['raw']) for r in rows]


class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid_and_complete(self):
        cfg = W.make_config(None)
        self.assertEqual(cfg, W.make_config({}))
        self.assertEqual(cfg['max_calls'], 16)
        self.assertEqual(cfg['retain_raw'], 'signatures')

    def test_the_default_call_budget_covers_the_documented_worst_case(self):
        cfg = W.make_config({})
        worst = cfg['sig_pages'] + 1 + cfg['max_swap_txs'] + 2 * cfg['funding_wallets']
        self.assertLessEqual(worst, cfg['max_calls'])

    def test_unknown_wrong_typed_and_impossible_values_are_refused(self):
        for bad in ({'max_cals': 5}, {'max_calls': True}, {'max_calls': 2.5}, {'max_calls': '16'}, {'max_calls': 0}, {'max_calls': 500},
                    {'window_s': -1}, {'retain_raw': 'raw'}, {'ignore_funders': 'abc'}, {'ignore_funders': ['not an address']},
                    {'low_rate_per_s': 0}, {'low_rate_per_s': 50}, {'win_multiple': 1}, {'rug_price_frac': 1},
                    {'smart_threshold': 1.5}, {'sig_limit': 5000}, {'win_multiple': float('nan')}, {'sniper_slots': -1}, 'text', []):
            with self.subTest(bad=bad), self.assertRaises(W.ConfigError):
                W.make_config(bad)

    def test_comment_key_is_allowed_and_floats_may_be_ints(self):
        self.assertEqual(W.make_config({'_comment': 'x', 'low_rate_per_s': 2, 'win_multiple': 3})['win_multiple'], 3)


class ParseTests(unittest.TestCase):
    def test_signatures_parse_and_keep_order(self):
        rows = [{'signature': sig(2), 'slot': 5, 'blockTime': 9, 'err': None}, {'signature': sig(1), 'slot': 4, 'blockTime': None, 'err': {'x': 1}}]
        parsed = W.parse_signatures(rows)
        self.assertEqual([(r['slot'], r['failed']) for r in parsed], [(5, False), (4, True)])

    def test_malformed_signature_lists_are_refused(self):
        good = {'signature': sig(1), 'slot': 5, 'blockTime': 9, 'err': None}
        for bad in (None, {}, 'x', [1], [{**good, 'slot': True}], [{**good, 'slot': -1}], [{**good, 'slot': '5'}], [{**good, 'signature': 'short'}],
                    [{**good, 'signature': 5}], [{**good, 'blockTime': '9'}], [{k: v for k, v in good.items() if k != 'slot'}]):
            with self.subTest(bad=bad), self.assertRaises(W.SignalError):
                W.parse_signatures(bad)

    def test_a_buy_credits_the_receivers_and_never_the_pool(self):
        body = swap_body(sig(1), 7, {addr(1): 500, addr(2): 300})
        view = W.parse_swap_tx(body, sig(1), MINT, POOL)
        self.assertEqual(view['buyers'], {addr(1): 500, addr(2): 300})
        self.assertEqual(view['pool_delta'], -800)
        self.assertNotIn(POOL, view['post'])
        self.assertEqual(view['post'], {addr(1): 500, addr(2): 300})

    def test_a_sell_or_a_wallet_to_wallet_transfer_is_not_a_buy(self):
        sell = swap_body(sig(1), 7, {addr(1): 500})
        for side in ('preTokenBalances', 'postTokenBalances'):                        # swap pre/post: the pool GAINS tokens
            pass
        sell['meta']['preTokenBalances'], sell['meta']['postTokenBalances'] = sell['meta']['postTokenBalances'], sell['meta']['preTokenBalances']
        self.assertEqual(W.parse_swap_tx(sell, sig(1), MINT, POOL)['buyers'], {})
        transfer = {'slot': 7, 'blockTime': 1, 'transaction': {'message': {'accountKeys': [{'pubkey': addr(1), 'signer': True}], 'instructions': []}},
                    'meta': {'err': None, 'preTokenBalances': [tb(addr(1), 100, 1), tb(addr(2), 0, 2)],
                             'postTokenBalances': [tb(addr(1), 0, 1), tb(addr(2), 100, 2)]}}
        self.assertEqual(W.parse_swap_tx(transfer, sig(2), MINT, POOL)['buyers'], {})

    def test_other_mints_and_failed_transactions_are_ignored(self):
        body = swap_body(sig(1), 7, {addr(1): 500})
        body['meta']['postTokenBalances'].append(tb(addr(9), 10 ** 9, 30, addr(77)))
        self.assertNotIn(addr(9), W.parse_swap_tx(body, sig(1), MINT, POOL)['buyers'])
        failed = swap_body(sig(2), 7, {addr(1): 500}, err={'InstructionError': [0, 'Custom']})
        view = W.parse_swap_tx(failed, sig(2), MINT, POOL)
        self.assertTrue(view['failed'])
        self.assertEqual(view['buyers'], {})

    def test_two_token_accounts_of_one_owner_are_summed(self):
        body = swap_body(sig(1), 7, {addr(1): 500})
        body['meta']['postTokenBalances'].append(tb(addr(1), 200, 40))
        body['meta']['preTokenBalances'].append(tb(addr(1), 0, 40))
        self.assertEqual(W.parse_swap_tx(body, sig(1), MINT, POOL)['buyers'], {addr(1): 700})

    def test_null_transaction_is_none_and_malformed_bodies_raise(self):
        self.assertIsNone(W.parse_swap_tx(None, sig(1), MINT, POOL))
        good = swap_body(sig(1), 7, {addr(1): 5})
        mutations = [lambda b: b.pop('slot'), lambda b: b.update(slot=True), lambda b: b.pop('meta'), lambda b: b['transaction'].pop('message'),
                     lambda b: b['meta'].update(postTokenBalances='x'), lambda b: b['meta']['postTokenBalances'][1].pop('owner'),
                     lambda b: b['meta']['postTokenBalances'][1]['uiTokenAmount'].update(amount='-5'),
                     lambda b: b['meta']['postTokenBalances'][1]['uiTokenAmount'].update(amount=5),
                     lambda b: b['meta']['postTokenBalances'][1].update(owner='bad owner'), lambda b: b.update(blockTime='x')]
        for i, mutate in enumerate(mutations):
            body = json.loads(json.dumps(good))
            mutate(body)
            with self.subTest(i=i), self.assertRaises(W.SignalError):
                W.parse_swap_tx(body, sig(1), MINT, POOL)
        with self.assertRaises(W.SignalError):
            W.parse_swap_tx('x', sig(1), MINT, POOL)

    def test_transfers_to_finds_top_level_and_inner_system_transfers_into_the_wallet_only(self):
        wallet, other = addr(1), addr(2)
        self.assertEqual(W.transfers_to(funding_body(sig(1), 5, wallet, addr(50)), wallet), [(addr(50), 500_000_000)])
        self.assertEqual(W.transfers_to(funding_body(sig(1), 5, wallet, addr(50), inner=True), wallet), [(addr(50), 500_000_000)])
        self.assertEqual(W.transfers_to(funding_body(sig(1), 5, other, addr(50)), wallet), [])
        self.assertEqual(W.transfers_to(None, wallet), [])
        spl = funding_body(sig(1), 5, wallet, addr(50))
        spl['transaction']['message']['instructions'][0]['program'] = 'spl-token'
        self.assertEqual(W.transfers_to(spl, wallet), [])
        for lamports in (0, -5, '500', True):
            body = funding_body(sig(1), 5, wallet, addr(50), lamports)
            self.assertEqual(W.transfers_to(body, wallet), [], lamports)
        self_transfer = funding_body(sig(1), 5, wallet, wallet)
        self.assertEqual(W.transfers_to(self_transfer, wallet), [])

    def test_supply_must_be_a_positive_integer_string(self):
        self.assertEqual(W.parse_supply({'value': {'amount': '1000'}}), 1000)
        for bad in (None, {}, {'value': {}}, {'value': {'amount': 1000}}, {'value': {'amount': '0'}}, {'value': {'amount': '-5'}}, {'value': {'amount': '1.5'}}):
            with self.subTest(bad=bad), self.assertRaises(W.SignalError):
                W.parse_supply(bad)


def tx(slot, order_buyers, post=None, signature=None):
    return {'signature': signature or sig(slot), 'slot': slot, 'buyers': dict(order_buyers), 'post': dict(post if post is not None else order_buyers)}


class AnalyzeTests(unittest.TestCase):
    cfg = W.make_config({})

    def run_analyze(self, txs, funding=None, supply=SUPPLY, cfg=None):
        return W.analyze(grad_slot=GRAD, txs=txs, funding=funding or {}, supply_raw=supply, cfg=cfg or self.cfg)

    def test_known_answer(self):
        a, b, c, d, e = (addr(i) for i in range(1, 6))
        txs = [tx(GRAD, {a: 10 ** 13, b: 2 * 10 ** 13}), tx(GRAD + 3, {c: 10 ** 12}), tx(GRAD + 4, {d: 5}), tx(GRAD + 20, {e: 7})]
        figures = self.run_analyze(txs)
        self.assertEqual(figures['buyers'], 5)
        self.assertEqual(figures['same_slot_buyers'], 2)
        self.assertEqual(figures['sniper_count'], 3)                    # <= GRAD + 3, inclusive; GRAD + 4 is not
        self.assertEqual(figures['bundled_wallets'], 2)
        self.assertEqual(figures['bundle_score'], 0.4)
        self.assertEqual(figures['bundled_supply_pct'], 3.0)            # (1e13 + 2e13) / 1e15
        self.assertEqual(figures['bundle_score_basis'], 'same_slot')

    def test_a_shared_funder_bundles_wallets_that_are_not_in_the_same_slot(self):
        a, b, c = addr(1), addr(2), addr(3)
        txs = [tx(GRAD + 10, {a: 100}), tx(GRAD + 40, {b: 100}), tx(GRAD + 60, {c: 100})]
        figures = self.run_analyze(txs, funding={a: addr(50), b: addr(50), c: addr(51)})
        self.assertEqual(figures['bundled_wallets'], 2)
        self.assertEqual(figures['bundle_score'], round(2 / 3, 4))
        self.assertEqual(figures['funding_clusters'], [{'source': addr(50), 'wallets': [a, b]}])
        self.assertEqual(figures['bundle_score_basis'], 'same_slot+funding')

    def test_ignored_funders_and_small_groups_do_not_cluster(self):
        a, b = addr(1), addr(2)
        txs = [tx(GRAD + 10, {a: 100}), tx(GRAD + 40, {b: 100})]
        hub = W.make_config({'ignore_funders': [addr(50)]})
        self.assertEqual(self.run_analyze(txs, funding={a: addr(50), b: addr(50)}, cfg=hub)['bundled_wallets'], 0)
        self.assertEqual(self.run_analyze(txs, funding={a: addr(50), b: None})['bundled_wallets'], 0)
        three = W.make_config({'min_cluster': 3})
        self.assertEqual(self.run_analyze(txs, funding={a: addr(50), b: addr(50)}, cfg=three)['bundled_wallets'], 0)

    def test_no_buyers_is_zero_counts_and_null_scores_with_a_reason(self):
        figures = self.run_analyze([])
        self.assertEqual((figures['buyers'], figures['same_slot_buyers'], figures['sniper_count']), (0, 0, 0))
        self.assertIsNone(figures['bundle_score'])
        self.assertEqual(figures['unavailable']['bundle_score'], 'NO_BUYERS_IN_WINDOW')

    def test_unknown_supply_nulls_only_the_supply_share(self):
        figures = self.run_analyze([tx(GRAD, {addr(1): 5})], supply=None)
        self.assertIsNone(figures['bundled_supply_pct'])
        self.assertEqual(figures['unavailable']['bundled_supply_pct'], 'SUPPLY_UNAVAILABLE')
        self.assertEqual(figures['bundle_score'], 1.0)

    def test_the_last_seen_balance_counts_not_the_first_buy(self):
        a = addr(1)
        figures = self.run_analyze([tx(GRAD, {a: 10 ** 13}), tx(GRAD + 5, {}, post={a: 4 * 10 ** 13})])
        self.assertEqual(figures['bundled_supply_pct'], 4.0)

    def test_a_wallet_that_buys_twice_is_one_buyer(self):
        a = addr(1)
        figures = self.run_analyze([tx(GRAD, {a: 5}), tx(GRAD + 1, {a: 5})])
        self.assertEqual((figures['buyers'], figures['same_slot_buyers']), (1, 1))


# ---------------------------------------------------------------------------------------------------- the collector

def bundle_chain(chain=None, *, mint=MINT, pool=POOL):
    """A bundled launch: four wallets buy in the graduation slot, three of them funded by one source; an organic buyer
    comes 60 slots later. Returns the chain and the wallet names."""
    chain = chain or Chain()
    w = {name: addr(i) for i, name in enumerate(('a', 'b', 'c', 'd', 'org'), start=1)}
    funder, hub = addr(90), addr(91)
    for name, source in (('a', funder), ('b', funder), ('c', funder), ('d', addr(92)), ('org', hub)):
        chain.fund(w[name], source)
    chain.swap(GRAD, {w['a']: 4 * 10 ** 13}, mint=mint, pool=pool)
    chain.swap(GRAD, {w['b']: 3 * 10 ** 13, w['c']: 2 * 10 ** 13}, mint=mint, pool=pool)
    chain.swap(GRAD, {w['d']: 10 ** 13}, mint=mint, pool=pool)
    chain.swap(GRAD + 60, {w['org']: 5 * 10 ** 12}, mint=mint, pool=pool)
    chain.swap(GRAD + WINDOW_SLOTS + 10, {addr(120): 10 ** 12}, mint=mint, pool=pool)       # after the window: never examined
    return chain, w


class CollectTests(Tmp):
    def test_a_bundled_launch_is_scored_and_recorded(self):
        chain, w = bundle_chain()
        self.candidate()
        collector = self.collector(chain)
        self.assertEqual(collector.signals_pass(), 1)
        (row,) = self.signals()
        self.assertEqual((row['version'], row['caveat']), (W.VERSION, W.CAVEAT))
        self.assertEqual((row['buyers'], row['same_slot_buyers'], row['sniper_count']), (5, 4, 4))
        self.assertEqual(row['funding_checked'], 3)                        # the three earliest buyers: a, b, c
        self.assertEqual(row['funding_clusters'], [{'source': addr(90), 'wallets': [w['a'], w['b'], w['c']]}])
        self.assertEqual(row['bundled_wallets'], 4)                        # d is bundled by slot, org is not
        self.assertEqual(row['bundle_score'], 0.8)
        self.assertEqual(row['bundled_supply_pct'], 10.0)                  # (4+3+2+1) e13 / 1e15
        self.assertEqual(row['bundle_score_basis'], 'same_slot+funding')
        self.assertEqual(row['window_txs'], 4)
        self.assertEqual(row['examined_txs'], 4)
        self.assertFalse(row['partial'], row['unavailable'])
        # exact call budget: 1 page + supply + 4 txs + 3 wallets x 2 = 12 calls, all within the cap
        self.assertEqual((row['calls'], chain.count()), (12, 12))
        # the same figures are in the queryable meta
        meta = json.loads(self.store.rows('observations', mint=MINT, kind='wallet_signals')[0]['meta'])
        self.assertEqual((meta['bundle_score'], meta['sniper_count'], meta['bundled_supply_pct']), (0.8, 4, 10.0))
        # every examined buyer is an append-only wallet_buy event
        buys = [json.loads(r['payload']) for r in self.store.rows('events', kind='wallet_buy')]
        self.assertEqual(sorted(b['wallet'] for b in buys), sorted(w.values()))
        self.assertEqual({b['slot_offset'] for b in buys}, {0, 60})

    def test_an_organic_launch_scores_low(self):
        chain = Chain()
        for i in range(1, 5):
            chain.fund(addr(i), addr(60 + i))
            chain.swap(GRAD + 8 * i, {addr(i): 10 ** 12})
        self.candidate()
        self.collector(chain).signals_pass()
        (row,) = self.signals()
        self.assertEqual((row['same_slot_buyers'], row['sniper_count'], row['bundle_score']), (0, 0, 0.0))
        self.assertEqual(row['funding_clusters'], [])
        self.assertEqual(row['bundled_supply_pct'], 0.0)

    def test_a_shared_exchange_hub_is_not_a_cluster_when_ignored(self):
        chain = Chain()
        for i in range(1, 4):
            chain.fund(addr(i), addr(99))
            chain.swap(GRAD + 8 * i, {addr(i): 10 ** 12})
        self.candidate()
        self.collector(chain, {'ignore_funders': [addr(99)]}).signals_pass()
        self.assertEqual(self.signals()[0]['bundle_score'], 0.0)

    def test_a_shared_funder_alone_is_labelled_a_signal_not_ownership(self):
        chain = Chain()
        for i in range(1, 4):
            chain.fund(addr(i), addr(99))
            chain.swap(GRAD + 8 * i, {addr(i): 10 ** 12})
        self.candidate()
        self.collector(chain).signals_pass()
        row = self.signals()[0]
        self.assertEqual(row['bundle_score'], 1.0)
        self.assertIn('not proof of common ownership', row['caveat'])
        self.assertNotIn('owner', json.dumps({k: v for k, v in row.items() if k not in ('caveat', 'buyer_wallets', 'smart_wallets')}))

    def test_the_hard_call_cap_stops_the_candidate_and_keeps_what_was_learned(self):
        chain, _ = bundle_chain()
        self.candidate()
        self.collector(chain, {'max_calls': 5}).signals_pass()
        (row,) = self.signals()
        self.assertEqual((chain.count(), row['calls']), (5, 5))
        self.assertEqual(row['unavailable']['stopped'], 'CALL_CAP')
        self.assertTrue(row['partial'])
        self.assertEqual(row['examined_txs'], 3)                           # sigs, supply, then 3 transactions
        self.assertEqual(row['funding_checked'], 0)
        self.assertEqual(row['buyers'], 4)                                 # a, b+c, d: the first three transactions

    def test_credit_shedding_stops_before_any_call(self):
        chain, _ = bundle_chain()
        self.candidate()
        collector = self.collector(chain, shed=lambda: True)
        collector.signals_pass()
        self.assertEqual(chain.count(), 0)
        (row,) = self.signals()
        self.assertEqual(row['unavailable']['stopped'], 'CREDIT_SHED')
        self.assertIsNone(row['bundle_score'])
        self.assertEqual(collector.counts['shed_stops'], 1)

    def test_shedding_part_way_keeps_the_partial_figures(self):
        chain, _ = bundle_chain()
        self.candidate()
        state = {'n': 0}

        def shed():
            state['n'] += 1
            return state['n'] > 6                                          # sigs + supply + 4 txs, then shed
        self.collector(chain, shed=shed).signals_pass()
        (row,) = self.signals()
        self.assertEqual((row['unavailable']['stopped'], row['examined_txs'], row['bundle_score']), ('CREDIT_SHED', 4, 0.8))

    def test_a_transient_failure_before_anything_was_learned_is_retried_later_then_recorded(self):
        chain, _ = bundle_chain()
        chain.fail['getSignaturesForAddress'] = ProviderError('HTTP_503', True)
        self.candidate()
        collector = self.collector(chain, {'max_retries': 2, 'retry_delay_s': 60})
        collector.signals_pass()
        self.assertEqual(self.signals(), [])                               # nothing written: it will be retried
        collector.signals_pass()                                           # still inside the retry delay
        self.assertEqual(chain.count(), 1)
        for _ in range(2):
            self.now += 61
            collector.signals_pass()
        self.assertEqual(chain.count(), 3)
        self.now += 61
        collector.signals_pass()                                           # retries exhausted: recorded as unavailable
        (row,) = self.signals()
        self.assertEqual(row['unavailable']['window'], 'SIGNATURES_UNAVAILABLE:HTTP_503')
        self.assertIsNone(row['bundle_score'])
        self.assertEqual(row['buyers'], None)

    def test_a_permanent_failure_is_recorded_at_once_and_never_retried(self):
        chain, _ = bundle_chain()
        chain.fail['getSignaturesForAddress'] = ProviderError('HTTP_401', False)
        self.candidate()
        collector = self.collector(chain)
        collector.signals_pass()
        collector.signals_pass()
        self.assertEqual(chain.count(), 1)
        self.assertEqual(self.signals()[0]['unavailable']['window'], 'SIGNATURES_UNAVAILABLE:HTTP_401')

    def test_a_window_that_cannot_be_reached_is_unavailable_not_guessed(self):
        chain = Chain()
        for i in range(6):                                                 # a full page that is entirely newer than the graduation
            chain.swap(GRAD + 500 + i, {addr(i + 1): 10})
        self.candidate()
        self.collector(chain, {'sig_limit': 6, 'sig_pages': 1}).signals_pass()
        row = self.signals()[0]
        self.assertEqual(row['unavailable']['window'], 'WINDOW_START_NOT_REACHED')
        self.assertEqual(row['oldest_slot_seen'], GRAD + 500)
        self.assertIsNone(row['bundle_score'])
        self.assertIsNone(row['same_slot_buyers'])
        self.assertEqual(chain.count('getTransaction'), 0)

    def test_a_second_page_reaches_the_start(self):
        chain = Chain()
        chain.swap(GRAD, {addr(1): 10 ** 12})
        for i in range(4):
            chain.swap(GRAD + 1000 + i, {addr(i + 10): 10})
        self.candidate()
        self.collector(chain, {'sig_limit': 3, 'sig_pages': 2}).signals_pass()
        row = self.signals()[0]
        self.assertNotIn('window', row['unavailable'])
        self.assertEqual(row['same_slot_buyers'], 1)
        self.assertEqual(chain.count('getSignaturesForAddress'), 2 + 1)    # 2 pages + the one buyer's history

    def test_failed_transactions_and_null_or_malformed_bodies_are_skipped_and_counted(self):
        chain = Chain()
        chain.swap(GRAD, {addr(1): 10 ** 12}, err={'InstructionError': [0, 'x']})
        bad = chain.swap(GRAD + 1, {addr(2): 10 ** 12})
        chain.bodies[bad]['meta']['postTokenBalances'] = 'garbage'
        gone = chain.swap(GRAD + 2, {addr(3): 10 ** 12})
        chain.null_tx.add(gone)
        chain.swap(GRAD + 3, {addr(4): 10 ** 12})
        self.candidate()
        self.collector(chain, {'funding_wallets': 0}).signals_pass()
        row = self.signals()[0]
        self.assertEqual((row['window_txs'], row['examined_txs'], row['tx_skipped'], row['buyers']), (3, 1, 2, 1))
        self.assertEqual(row['buyer_wallets'], [addr(4)])

    def test_a_failed_transaction_call_skips_that_transaction_only(self):
        chain, _ = bundle_chain()
        seen = {'n': 0}

        def flaky(params):
            seen['n'] += 1
            return ProviderError('TIMEOUT', True) if seen['n'] == 2 else None
        chain.fail['getTransaction'] = flaky
        self.candidate()
        self.collector(chain).signals_pass()
        row = self.signals()[0]
        self.assertEqual((row['examined_txs'], row['tx_skipped']), (3, 1))

    def test_funding_history_cut_by_the_page_limit_is_unavailable(self):
        chain = Chain()
        wallet = addr(1)
        for i in range(12):
            chain.fund(wallet, addr(70), slot=GRAD - 100 - i)
        chain.swap(GRAD, {wallet: 10 ** 12})
        self.candidate()
        self.collector(chain, {'funding_history': 5}).signals_pass()
        row = self.signals()[0]
        self.assertEqual(row['unavailable']['funding:' + wallet[:8]], 'FUNDING_HISTORY_TRUNCATED')
        self.assertEqual(row['funding_clusters'], [])
        self.assertEqual(chain.count('getTransaction'), 1 + 0)             # the swap only: no funding transaction was fetched

    def test_funding_reasons_for_missing_history_and_non_transfers(self):
        chain = Chain()
        chain.swap(GRAD, {addr(1): 10 ** 12})                                # no prior history
        chain.fund(addr(2), addr(80), lamports=10)                           # dust: below min_funding_lamports
        chain.swap(GRAD + 1, {addr(2): 10 ** 12})
        chain.fund(addr(3), addr(81), inner=True)                            # fine, inner transfer
        chain.swap(GRAD + 2, {addr(3): 10 ** 12})
        self.candidate()
        self.collector(chain).signals_pass()
        un = self.signals()[0]['unavailable']
        self.assertEqual(un['funding:' + addr(1)[:8]], 'NO_PRIOR_HISTORY')
        self.assertEqual(un['funding:' + addr(2)[:8]], 'NO_FUNDING_TRANSFER')
        self.assertNotIn('funding:' + addr(3)[:8], un)

    def test_supply_failure_nulls_only_the_supply_share(self):
        chain, _ = bundle_chain()
        chain.fail['getTokenSupply'] = ProviderError('HTTP_500', True)
        self.candidate()
        self.collector(chain).signals_pass()
        row = self.signals()[0]
        self.assertIsNone(row['bundled_supply_pct'])
        self.assertEqual(row['unavailable']['supply'], 'SUPPLY_UNAVAILABLE:HTTP_500')
        self.assertEqual(row['bundle_score'], 0.8)

    def test_a_candidate_is_collected_once_across_passes_and_restarts(self):
        chain, _ = bundle_chain()
        self.candidate()
        self.collector(chain).signals_pass()
        calls = chain.count()
        self.collector(chain).signals_pass()                                 # a fresh process on the same store
        self.collector(chain).signals_pass()
        self.assertEqual((chain.count(), len(self.signals())), (calls, 1))

    def test_pending_selection(self):
        chain, _ = bundle_chain()
        young, _ = self.candidate(1, mint=addr(31), age=60)                  # window not complete yet
        old, _ = self.candidate(2, mint=addr(32), age=30000)                 # past backfill_max_age_s
        failed, _ = self.candidate(3, mint=addr(33), screen='FAILED')
        unscreened, _ = self.candidate(4, mint=addr(34), screen=None)
        rejected, _ = self.candidate(5, mint=addr(35), screen='REJECT')
        passed, _ = self.candidate(6, mint=addr(36))
        nopool = self.store.add_candidate(addr(37), ts=self.now - 400)
        self.store.add_decision('screen', 'PASS', mint=addr(37), candidate_id=nopool, ts=self.now)
        collector = self.collector(chain)
        self.assertEqual([r[0] for r in collector.pending(self.now, 100)], [rejected, passed])

    def test_oldest_first_and_bounded_per_pass(self):
        chain = Chain()
        for i in range(5):
            self.candidate(i + 1, mint=addr(40 + i))
        collector = self.collector(chain, {'max_per_pass': 2})
        self.assertEqual(collector.signals_pass(), 2)
        self.assertEqual(sorted(r['mint'] for r in self.store.rows('observations', kind='wallet_signals')), [addr(40), addr(41)])

    def test_raw_retention_modes(self):
        for mode, expected in (('none', 0), ('signatures', 1), ('all', 1 + 1 + 4 + 3 + 3)):
            with self.subTest(mode=mode):
                chain, _ = bundle_chain()
                with tempfile.TemporaryDirectory() as d:
                    self.store = Store(Path(d) / 'x.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s', clock=lambda: self.now)
                    self.candidate()
                    self.collector(chain, {'retain_raw': mode}).signals_pass()
                    kinds = [r['kind'] for r in self.store.rows('observations', limit=1000) if r['kind'].startswith('wallet_signals:raw')]
                    self.store.close()
                self.assertEqual(len(kinds), expected if mode != 'signatures' else 1 + 3)    # 1 page + 3 wallet histories

    def test_a_collector_bug_never_escapes_and_is_recorded(self):
        chain, _ = bundle_chain()
        self.candidate()
        collector = self.collector(chain)
        collector.collect = lambda *a, **k: 1 / 0
        self.assertEqual(collector.signals_pass(), 1)
        self.assertEqual(collector.counts['errors'], 1)
        self.assertEqual(self.store.rows('errors')[0]['code'], 'WALLET_SIGNALS_FAILED')

    def test_an_unexpected_error_is_retried_a_bounded_number_of_times_then_closed_out(self):
        chain, _ = bundle_chain()
        self.candidate()
        collector = self.collector(chain, {'max_retries': 1, 'retry_delay_s': 10})
        collector.collect = lambda *a, **k: 1 / 0
        for _ in range(4):
            collector.signals_pass()
            self.now += 11
        (row,) = self.signals()
        self.assertEqual(row['unavailable']['window'], 'COLLECT_FAILED:ZeroDivisionError')
        self.assertIsNone(row['bundle_score'])
        self.assertEqual(collector.pending(self.now, 10), [])                 # closed: the queue is not blocked forever

    def test_a_failing_candidate_does_not_block_newer_ones(self):
        chain = Chain()
        for i in range(3):
            self.candidate(i + 1, mint=addr(40 + i))
        collector = self.collector(chain, {'max_per_pass': 1, 'max_retries': 5, 'retry_delay_s': 1000})
        real = collector.collect
        collector.collect = lambda cid, mint, pool, slot: 1 / 0 if mint == addr(40) else real(cid, mint, pool, slot)
        for _ in range(4):
            collector.signals_pass()
        self.assertEqual(sorted(r['mint'] for r in self.store.rows('observations', kind='wallet_signals')), [addr(41), addr(42)])

    def test_nothing_is_ever_signed_or_sent(self):
        source = (Path(W.__file__)).read_text()
        for needle in ('send_transaction', 'sendTransaction', 'simulateTransaction', 'Keypair', 'sign_transaction', 'signTransaction'):
            self.assertNotIn(needle, source)
        self.assertTrue(set(W.__dict__.get('READ_ONLY', set())) <= set())


# --------------------------------------------------------------------------------------------- outcomes and the smart table

def add_path(store, mint, prices, *, end=False, ts=1.0):
    for i, price in enumerate(prices):
        store.add_observation('path_mark', b'{}', mint=mint, meta={'price_sol': None if price is None else str(price), 'ts': ts + i}, ts=ts + i)
    if end:
        store.add_observation('path_end', b'{}', mint=mint, meta={}, ts=ts + len(prices))


class PathOutcomeTests(Tmp):
    cfg = W.make_config({})

    def outcome(self, prices, **kwargs):
        mint = addr(120 + len(self.store.rows('observations', limit=100000)) % 100)
        add_path(self.store, mint, prices, **kwargs)
        return W.path_outcome(self.store, mint, self.cfg)[0]

    def test_first_threshold_hit_decides(self):
        self.assertEqual(self.outcome([1, 1.4, 2.5, 0.1]), 'WIN')
        self.assertEqual(self.outcome([1, 0.9, 0.2, 5]), 'RUG')

    def test_thresholds_are_inclusive(self):
        self.assertEqual(self.outcome([1, 2]), 'WIN')
        self.assertEqual(self.outcome(['0.1', '0.03']), 'RUG')
        self.assertIsNone(self.outcome([1, 1.99, 0.31]))

    def test_unresolved_until_the_path_ends_then_flat(self):
        self.assertIsNone(self.outcome([1, 1.2, 0.8]))
        self.assertEqual(self.outcome([1, 1.2, 0.8], end=True), 'FLAT')

    def test_no_marks_and_invalid_marks(self):
        self.assertIsNone(W.path_outcome(self.store, addr(5), self.cfg)[0])
        self.assertIsNone(self.outcome([None, 0, '-1', 'abc']))
        self.assertEqual(self.outcome([None, 1, 3]), 'WIN')                    # an unparseable mark is skipped, not the reference


def traded(store, mint, *, sol='0.02', out=1_000_000_000, exit_lamports, ts=1.0, reenter=False):
    from lean.paper import PaperConfig, buy, sell, to_lamports, Quote
    cfg = PaperConfig()
    fill = buy(Quote(mint, 'buy', to_lamports(sol), out, 6, ts, ref='r'), sol, cfg)
    open_id = store.add_fill(fill, state={'event': 'open', 'state': {'x': 1}})
    position = store.positions()[mint]
    s = sell(position, Quote(mint, 'sell', position.qty_raw, exit_lamports, 6, ts + 1, ref='r'), cfg=cfg, qty_raw=position.qty_raw)
    pnl = position.realized_lamports + s.realized_lamports
    store.add_fill(s, state={'event': 'closed', 'open_fill_id': open_id, 'state': {'reason': 'X', 'trade_pnl_lamports': pnl}})
    return pnl


class TradeOutcomeTests(Tmp):
    cfg = W.make_config({})

    def test_win_rug_flat_by_pnl_over_cost(self):
        traded(self.store, addr(1), exit_lamports=45_000_000)                  # ~ +125 %
        traded(self.store, addr(2), exit_lamports=5_000_000)                   # ~ -75 %
        traded(self.store, addr(3), exit_lamports=21_000_000)                  # ~ +5 %
        self.assertEqual([W.trade_outcome(self.store, addr(i), self.cfg)[0] for i in (1, 2, 3)], ['WIN', 'RUG', 'FLAT'])
        self.assertEqual(W.trade_outcome(self.store, addr(2), self.cfg)[1], 'TRADE')

    def test_open_and_never_entered_tokens_are_unresolved(self):
        from lean.paper import PaperConfig, buy, Quote, to_lamports
        fill = buy(Quote(addr(4), 'buy', to_lamports('0.02'), 10 ** 9, 6, 1.0, ref='r'), '0.02', PaperConfig())
        self.store.add_fill(fill, state={'event': 'open', 'state': {}})
        self.assertIsNone(W.trade_outcome(self.store, addr(4), self.cfg)[0])
        self.assertIsNone(W.trade_outcome(self.store, addr(5), self.cfg)[0])


class SmartWalletTests(Tmp):
    """Four launches with known buyers; outcomes injected as L07 path marks; then a later launch with the same wallets."""

    def setUp(self):
        super().setUp()
        self.chain = Chain()
        self.s1, self.s2, self.s3, self.r1, self.n1 = (addr(i) for i in (1, 2, 3, 4, 5))
        self.launches = {}

    def launch(self, tag, buyers, *, slot=GRAD):
        mint, pool = addr(150 + 2 * tag), addr(151 + 2 * tag)
        self.chain.supply[mint] = SUPPLY
        for offset, wallet in enumerate(buyers):
            self.chain.swap(slot + 5 * offset, {wallet: 10 ** 12}, mint=mint, pool=pool)
        self.candidate(tag, mint=mint, pool=pool)
        self.launches[tag] = mint
        return mint

    def table(self, collector):
        return collector.wallet_table()[0]

    def test_wins_and_rugs_build_the_table_and_smart_needs_two_wins(self):
        collector = self.collector(self.chain, {'funding_wallets': 0})
        t1 = self.launch(1, [self.s1, self.s2, self.s3])
        t2 = self.launch(2, [self.s1, self.s2])
        t3 = self.launch(3, [self.s3, self.r1])
        add_path(self.store, t1, [1, 2.5]); add_path(self.store, t2, [1, 3]); add_path(self.store, t3, [1, 0.1])
        collector.signals_pass()
        table = self.table(collector)
        self.assertEqual({w: (r['wins'], r['rugs']) for w, r in table.items()}, {self.s1: (2, 0), self.s2: (2, 0), self.s3: (1, 1), self.r1: (0, 1)})
        self.assertEqual([w for w in table if collector.is_smart(table[w])], [self.s1, self.s2])
        self.assertAlmostEqual(collector.score(table[self.s1]), 0.75)
        self.assertAlmostEqual(collector.score(table[self.s3]), 0.5)

    def test_a_later_launch_counts_the_smart_wallets_among_its_early_buyers(self):
        collector = self.collector(self.chain, {'funding_wallets': 0})
        for tag, buyers in ((1, [self.s1, self.s2]), (2, [self.s1, self.s2]), (3, [self.r1])):
            self.launch(tag, buyers)
        for tag, prices in ((1, [1, 3]), (2, [1, 2]), (3, [1, 0.1])):
            add_path(self.store, self.launches[tag], prices)
        collector.signals_pass()
        late = self.launch(4, [self.n1, self.s1, self.r1, self.s2])
        collector.signals_pass()
        row = self.signals(late)[0]
        self.assertEqual((row['smart_buyers_count'], row['smart_wallets']), (2, [self.s1, self.s2]))

    def test_no_look_ahead_the_candidate_is_scored_only_with_outcomes_known_before_it(self):
        collector = self.collector(self.chain, {'funding_wallets': 0})
        first = self.launch(1, [self.s1, self.s2])
        second = self.launch(2, [self.s1, self.s2])
        for mint in (first, second):
            add_path(self.store, mint, [1, 4])                                  # both tokens end up winners
        collector.signals_pass()                                                # collects both, THEN resolves both
        for mint in (first, second):
            row = self.signals(mint)[0]
            self.assertEqual(row['smart_buyers_count'], 0, 'a wallet is never rated by the token it is scored on')
        self.assertTrue(all(collector.is_smart(r) for r in collector.wallet_table()[0].values()))

    def test_the_row_is_as_of_the_events_that_existed_when_it_was_written(self):
        collector = self.collector(self.chain, {'funding_wallets': 0})
        self.launch(1, [self.s1])
        collector.signals_pass()
        events = {r['id']: r['kind'] for r in self.store.rows('events', limit=1000)}
        row = self.signals(self.launches[1])[0]
        own_buys = [i for i, k in events.items() if k == 'wallet_buy']
        self.assertTrue(own_buys)
        self.assertLess(row['smart_asof_event_id'], min(own_buys))

    def test_a_trade_outcome_is_used_when_there_is_no_path(self):
        collector = self.collector(self.chain, {'funding_wallets': 0})
        t1 = self.launch(1, [self.s1])
        collector.signals_pass()
        traded(self.store, t1, exit_lamports=5_000_000)
        collector.resolve_outcomes(self.now)
        self.assertEqual(collector.wallet_table()[0][self.s1]['rugs'], 1)
        outcome = [json.loads(r['payload']) for r in self.store.rows('events', kind='wallet_outcome')]
        self.assertEqual([(o['mint'], o['outcome'], o['source']) for o in outcome], [(t1, 'RUG', 'TRADE')])

    def test_path_data_wins_over_the_trade_and_outcomes_are_written_once(self):
        collector = self.collector(self.chain, {'funding_wallets': 0})
        t1 = self.launch(1, [self.s1])
        collector.signals_pass()
        traded(self.store, t1, exit_lamports=5_000_000)
        add_path(self.store, t1, [1, 3])
        for _ in range(3):
            collector.resolve_outcomes(self.now)
        outcome = [json.loads(r['payload']) for r in self.store.rows('events', kind='wallet_outcome')]
        self.assertEqual([(o['outcome'], o['source']) for o in outcome], [('WIN', 'PATH')])

    def test_unresolved_tokens_age_out_as_unknown_and_are_never_scored(self):
        collector = self.collector(self.chain, {'funding_wallets': 0, 'outcome_max_age_s': 100})
        self.launch(1, [self.s1])
        collector.signals_pass()
        collector.resolve_outcomes(self.now + 50)
        self.assertEqual(collector.wallet_table()[0][self.s1]['unresolved'], 1)
        collector.resolve_outcomes(self.now + 150)
        record = collector.wallet_table()[0][self.s1]
        self.assertEqual((record['wins'], record['rugs'], record['unresolved']), (0, 0, 1))
        self.assertEqual(json.loads(self.store.rows('events', kind='wallet_outcome')[0]['payload'])['outcome'], 'UNKNOWN')

    def test_the_table_survives_a_restart(self):
        collector = self.collector(self.chain, {'funding_wallets': 0})
        for tag in (1, 2):
            add_path(self.store, self.launch(tag, [self.s1]), [1, 3])
        collector.signals_pass()
        fresh = self.collector(self.chain, {'funding_wallets': 0})
        self.assertEqual(fresh.wallet_table()[0], collector.wallet_table()[0])
        self.assertTrue(fresh.is_smart(fresh.wallet_table()[0][self.s1]))

    def test_the_wallet_table_is_built_only_from_this_desks_own_records(self):
        collector = self.collector(self.chain)
        self.assertEqual(collector.wallet_table(), ({}, 0))
        self.assertFalse(collector.is_smart(None))
        self.assertFalse(collector.is_smart({'wins': 1, 'rugs': 0, 'flat': 0, 'unresolved': 0, 'buys': 1}))      # one win is not enough
        self.assertFalse(collector.is_smart({'wins': 3, 'rugs': 2, 'flat': 0, 'unresolved': 0, 'buys': 5}))      # 4/7 < 0.7
        self.assertTrue(collector.is_smart({'wins': 4, 'rugs': 1, 'flat': 0, 'unresolved': 0, 'buys': 5}))       # 5/7 >= 0.7


# ------------------------------------------------------------------------------------------------- the low-priority lane

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class LowLaneTests(unittest.TestCase):
    def limiter(self, shed=None, own_rate=1.0, own_burst=2, base_rate=10.0, base_burst=10):
        clock = FakeClock()
        own = RateLimiter(own_rate, own_burst, clock=clock.time, sleep=clock.sleep)
        base = RateLimiter(base_rate, base_burst, clock=clock.time, sleep=clock.sleep)
        return clock, own, base, W.LowLaneLimiter(own, base, shed)

    def test_the_low_lane_never_exceeds_its_own_rate(self):
        clock, _own, _base, limiter = self.limiter()
        for _ in range(12):
            limiter.acquire()
        self.assertGreaterEqual(clock.t, 10.0 - 1e-9)                       # burst 2, then 1 per second

    def test_it_also_consumes_the_shared_main_lane_budget(self):
        clock, _own, base, limiter = self.limiter(own_rate=10, own_burst=10, base_rate=1.0, base_burst=2)
        limiter.acquire(); limiter.acquire()                                # the shared bucket (burst 2) is now empty
        before = clock.t
        base.acquire()                                                      # a main-lane caller must wait for the refill
        self.assertGreaterEqual(clock.t - before, 1.0 - 1e-9)
        limiter.acquire()                                                   # and the low lane waits behind the shared bucket too
        self.assertGreaterEqual(clock.t - before, 2.0 - 1e-9)

    def test_shedding_refuses_before_any_token_is_taken(self):
        clock, own, base, limiter = self.limiter(shed=lambda: True)
        with self.assertRaises(ProviderError) as caught:
            limiter.acquire()
        self.assertEqual((caught.exception.code, caught.exception.transient), ('CREDIT_SHED', True))
        self.assertEqual(clock.t, 0.0)
        self.assertEqual((own._tokens, base._tokens), (2.0, 10.0))

    def test_a_429_block_reaches_both_buckets(self):
        clock, own, base, limiter = self.limiter()
        limiter.block_for(7)
        limiter.acquire()
        self.assertGreaterEqual(clock.t, 7.0)

    def test_a_deadline_applies_to_the_whole_wait(self):
        clock, *_, limiter = self.limiter()
        limiter.acquire(); limiter.acquire()
        with self.assertRaises(ProviderError) as caught:
            limiter.acquire(deadline=clock.t + 0.1)
        self.assertEqual(caught.exception.code, 'RATE_LIMITED')

    def test_the_real_client_runs_on_the_low_lane(self):
        from lean.providers import Helius
        from tests.lean.fakeworld import FakeTime, Resp
        clock, seen = FakeTime(), []

        def opener(request, timeout):
            seen.append(clock.time())
            return Resp({'jsonrpc': '2.0', 'id': 1, 'result': 7})
        client = W.build_low_lane_helius('TEST-HELIUS-KEY-0000', W.make_config({'low_rate_per_s': 1, 'low_burst': 1}), opener=opener,
                                         clock=clock.time, monotonic=clock.monotonic, sleep=clock.sleep)
        self.assertIsInstance(client, Helius)
        for _ in range(3):
            self.assertEqual(client.rpc('getSlot', [])[0], 7)
        self.assertGreaterEqual(seen[2] - seen[0], 2.0 - 1e-6)
        self.assertNotIn('TEST-HELIUS-KEY', repr(client))
        self.assertEqual(client.transport.max_attempts, 1)                  # the cap counts requests actually sent
        again = W.build_low_lane_helius('TEST-HELIUS-KEY-0000', W.make_config({}), opener=opener, clock=clock.time,
                                        monotonic=clock.monotonic, sleep=clock.sleep, max_attempts=3)
        self.assertEqual(again.transport.max_attempts, 3)


class HookTests(unittest.TestCase):
    def test_load_config_validates_the_wallet_signals_object(self):
        from lean import __main__ as entry
        root = Path(__file__).resolve().parents[2]
        raw = json.loads((root / 'config' / 'lean' / 'lean.example.json').read_text())
        self.assertIsNone(raw['wallet_signals'])
        with tempfile.TemporaryDirectory() as d:
            for value, ok in ((None, True), ({}, True), ({'max_calls': 12}, True), ({'max_cals': 12}, False), ('on', False), ({'max_calls': 0}, False)):
                path = Path(d) / 'lean.json'
                path.write_text(json.dumps({**raw, 'wallet_signals': value, 'strategy_config': str(root / 'config' / 'lean' / 'strategy-default.json')}))
                with self.subTest(value=value):
                    if ok:
                        self.assertEqual(entry.load_config(path)['wallet_signals'], value)
                    else:
                        with self.assertRaises(entry.ConfigError):
                            entry.load_config(path)

    def test_the_example_file_is_valid(self):
        root = Path(__file__).resolve().parents[2]
        example = json.loads((root / 'config' / 'lean' / 'wallet_signals.example.json').read_text())
        cfg = W.make_config(example['wallet_signals'])
        self.assertEqual({k: cfg[k] for k in example['wallet_signals']}, example['wallet_signals'])
