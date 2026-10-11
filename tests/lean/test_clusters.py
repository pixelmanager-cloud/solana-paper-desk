"""SYNTHETIC_TEST_ONLY: L24 linked-wallet clustering (lean/clusters.py) and its learner guard. Fixtures only, no network.

The chain is ``tests.lean.test_wallet_signals.Chain`` (public jsonParsed response shapes) plus the two account reads the collector
adds (getMultipleAccounts of the holders' token accounts, getTokenLargestAccounts for a soft reject). The pool account bytes and the
holders' SPL token accounts come from ``tests.lean.fakeworld.Token`` (the desk's PumpSwap / SPL layouts).
"""
import json
import os
import tempfile
import threading
import unittest
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desk.providers import PUMPSWAP
from desk.security import TOKEN_PROGRAM
from lean import clusters as C
from lean import learn as LN
from lean import strategy as S
from lean import wallet_signals as W
from lean.providers import LANE_SHED, ProviderError
from lean.store import Store
from tests.lean import test_wallet_signals as WS
from tests.lean.fakeworld import Token
from tests.lean.test_labels import RecorderMixin

addr, sig = WS.addr, WS.sig
NOW = 1_800_000_000.0
DECISION = WS.GRAD_TIME + 100                    # 100 s after graduation: the window is cut at the decision
TIP = C.JITO_TIP_ACCOUNTS[0]
F_ = addr(90)                                     # the planted non-hub funder
HUB = addr(91)
CREATOR_TAG = 77
CREATOR = addr(CREATOR_TAG)
A = [addr(20 + i) for i in range(5)]             # 5 wallets funded by F_ (5.6 % each)
B = [addr(30 + i) for i in range(3)]             # 3 wallets in one Jito bundle with A[0] (4 % each)
O = [addr(40 + i) for i in range(4)]             # 4 organic holders (2 % each)
PCT = WS.SUPPLY // 1000                           # 0.1 % of the supply


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def tip_transfer(source, lamports=1_000_000, tip=TIP):
    return {'program': 'system', 'programId': W.SYSTEM_PROGRAM, 'parsed': {'type': 'transfer', 'info': {'source': source, 'destination': tip,
                                                                                                         'lamports': lamports}}}


class CChain(WS.Chain):
    """``WS.Chain`` + getMultipleAccounts (base64 token accounts) + getTokenLargestAccounts."""

    def __init__(self):
        super().__init__()
        self.accounts, self.largest = {}, {}

    def handle(self, method, params, t=None):
        if method in ('getMultipleAccounts', 'getTokenLargestAccounts'):
            self.calls.append((method, params[0]))
            failure = self.fail.get(method)
            if callable(failure):
                failure = failure(params)
            if failure is not None:
                raise failure
            if method == 'getMultipleAccounts':
                assert params[1]['encoding'] == 'base64'
                return {'context': {'slot': 1}, 'value': [self.accounts.get(k) for k in params[0]]}
            return {'context': {'slot': 1}, 'value': [{'address': a, 'amount': str(n), 'decimals': 6} for a, n in self.largest[params[0]]]}
        return super().handle(method, params, t)


class Limiter:
    def __init__(self, tokens):
        self.tokens = tokens

    def available(self):
        return self.tokens


class World(unittest.TestCase):
    """A store + chain with one planted candidate (``plant``)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.now = NOW
        self.store = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s', clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.chain = CChain()
        self.n = 0

    def plant(self, *, tag=61, holders=None, creator=True, screen_holders=True, supply=True, funders=None, bundle=True, decision=DECISION,
              cid_slot=WS.GRAD):
        """One graduated, screened candidate. ``holders`` = [(owner, amount)]; default: A (5.6 % each), B (4 %), O (2 %)."""
        self.n += 1
        holders = holders if holders is not None else [(w, 56 * PCT) for w in A] + [(w, 40 * PCT) for w in B] + [(w, 20 * PCT) for w in O]
        rows = [(addr(150 + 10 * self.n + i), owner, amount) for i, (owner, amount) in enumerate(holders)]
        token = Token(tag, coin_creator_tag=CREATOR_TAG if creator else None, holder_rows=rows)
        self.token = token
        self.chain.accounts.update(token.holder_accounts())
        self.chain.largest[token.mint] = [(token.base_vault, 2 * 10 ** 14)] + [(a, n) for a, _, n in rows]
        cid = self.store.add_candidate(token.mint, pool=token.pool, slot=cid_slot, migrated_at=WS.GRAD_TIME, hint_seq=self.n, ts=decision - 1)
        features = {'mint': token.mint, 'token_program': TOKEN_PROGRAM, 'pool_base_token_account': token.base_vault}
        if supply:
            features['supply_raw'] = WS.SUPPLY
        if screen_holders:
            body = {'jsonrpc': '2.0', 'id': 1, 'result': {'context': {'slot': 1}, 'value': [{'address': token.base_vault, 'amount': str(2 * 10 ** 14)}]
                                                          + [{'address': a, 'amount': str(n)} for a, _, n in rows]}}
            self.store.add_observation('screen:holders', json.dumps(body).encode(), mint=token.mint, candidate_id=cid, ts=decision - 0.5)
        pool_body = {'jsonrpc': '2.0', 'id': 1, 'result': {'context': {'slot': 1}, 'value': [token.mint_account(), Token.account(token.pool_raw, PUMPSWAP)]}}
        self.store.add_observation('screen:accounts_mint_pool', json.dumps(pool_body).encode(), mint=token.mint, candidate_id=cid, ts=decision - 0.6)
        self.store.add_decision('screen', 'PASS', mint=token.mint, candidate_id=cid, reasons=[], features=features, ts=decision)
        # funding (1 hop) at distinct slots well before graduation
        funders = {**{w: F_ for w in A}, CREATOR: F_, **{w: addr(60 + i) for i, w in enumerate(B)}} if funders is None else funders
        for i, (wallet, source) in enumerate(sorted(funders.items())):
            self.chain.fund(wallet, source, slot=WS.GRAD - 100 - 7 * i)
        if bundle:                                       # 4 adjacent swaps in one slot of the window, the last pays a Jito tip
            for wallet in [A[0]] + B:
                last = self.chain.swap(WS.GRAD + 5, {wallet: 10 ** 12}, mint=token.mint, pool=token.pool)
            self.chain.bodies[last]['transaction']['message']['instructions'].append(tip_transfer(B[-1]))
        return cid, token

    def collector(self, cfg=None, *, helius=None, **kwargs):
        self.sleeps = []
        kwargs.setdefault('signals_enabled', False)
        return C.Clusters(store=self.store, helius=helius or WS.ChainHelius(self.chain), cfg={'max_per_pass': 10, **(cfg or {})}, code_version='t',
                          strategy_version='s', clock=lambda: self.now, sleep=self.sleeps.append, **kwargs)

    def rows(self, mint=None):
        return [json.loads(r['raw']) for r in self.store.rows('observations', mint=mint, kind='clusters', limit=1000)]

    def calls(self, method=None):
        return [p for m, p in self.chain.calls if method in (None, m)]


# ------------------------------------------------------------------------------------------------------------------- config
class ConfigTests(unittest.TestCase):
    def test_defaults_are_valid_and_the_cap_is_at_most_20(self):
        cfg = C.make_config(None)
        self.assertEqual(cfg, C.make_config({}))
        self.assertEqual((cfg['max_calls'], cfg['top_n'], cfg['holder_lookups'], cfg['max_window_txs']), (20, 20, 6, 4))
        self.assertEqual(C.worst_case_calls(cfg), 21)                     # the cap ends the last optional read (documented)
        self.assertEqual(sorted(cfg['jito_tip_accounts']), sorted(C.JITO_TIP_ACCOUNTS))

    def test_unknown_wrong_typed_and_impossible_values_are_refused(self):
        for bad in ({'max_cals': 5}, {'max_calls': True}, {'max_calls': 2.5}, {'max_calls': 0}, {'max_calls': 500}, {'top_n': 21},
                    {'top_n': 0}, {'pace_s': 0}, {'shed_wait_s': -1}, {'ignore_funders': 'x'}, {'ignore_funders': ['bad addr']},
                    {'jito_tip_accounts': [1]}, {'same_slot_max_group': 1}, {'hub_min_mints': 1}, {'signals_wait_s': -1},
                    {'reserve_tokens': 11}, {'history_limit': 1001}, {'holder_lookups': -1}, {'pace_s': float('nan')}, 'x', []):
            with self.subTest(bad=bad), self.assertRaises(C.ConfigError):
                C.make_config(bad)

    def test_comment_is_allowed(self):
        self.assertEqual(C.make_config({'_comment': 'x', 'pace_s': 1})['pace_s'], 1)


# ------------------------------------------------------------------------------------------------------------------- parsing
class ParseTests(unittest.TestCase):
    def test_tx_links_reads_signers_tips_and_wallet_to_wallet_token_moves(self):
        body = WS.swap_body(sig(1), 7, {addr(1): 5})
        body['transaction']['message']['accountKeys'].append({'pubkey': addr(2), 'signer': True})
        body['meta']['innerInstructions'] = [{'index': 0, 'instructions': [tip_transfer(addr(2))]}]
        links = C.tx_links(body, WS.MINT, WS.POOL, set(C.JITO_TIP_ACCOUNTS))
        self.assertEqual((links['signers'], links['tip']), ([addr(1), addr(2)], True))
        self.assertEqual((links['token_senders'], links['token_receivers']), ([], []))     # a buy is not a transfer
        move = {'slot': 7, 'blockTime': 1, 'transaction': {'message': {'accountKeys': [{'pubkey': addr(1), 'signer': True}], 'instructions': []}},
                'meta': {'err': None, 'preTokenBalances': [WS.tb(addr(1), 100, 1), WS.tb(addr(2), 0, 2)],
                         'postTokenBalances': [WS.tb(addr(1), 0, 1), WS.tb(addr(2), 100, 2)]}}
        links = C.tx_links(move, WS.MINT, WS.POOL, set())
        self.assertEqual((links['token_senders'], links['token_receivers'], links['tip']), ([addr(1)], [addr(2)], False))
        self.assertIsNone(C.tx_links(None, WS.MINT, WS.POOL, set()))
        for bad in ('x', {'transaction': {}, 'meta': {}}, {'transaction': {'message': {'accountKeys': 'x'}}, 'meta': {}}):
            with self.subTest(bad=bad), self.assertRaises(W.SignalError):
                C.tx_links(bad, WS.MINT, WS.POOL, set())

    def test_a_transfer_to_a_non_tip_account_is_not_a_tip(self):
        body = WS.swap_body(sig(1), 7, {addr(1): 5})
        body['transaction']['message']['instructions'].append(tip_transfer(addr(1), tip=addr(3)))
        self.assertFalse(C.tx_links(body, WS.MINT, WS.POOL, set(C.JITO_TIP_ACCOUNTS))['tip'])

    def test_token_accounts_and_largest_rows(self):
        token = Token(61, holder_rows=[(addr(150), addr(1), 7), (addr(151), addr(2), 9)])
        accounts = token.holder_accounts()
        result = {'value': [accounts[addr(150)], None, accounts[addr(151)]]}
        self.assertEqual(C.parse_token_accounts(result, [addr(150), addr(152), addr(151)], token.mint),
                         {addr(150): (addr(1), 7), addr(151): (addr(2), 9)})
        self.assertEqual(C.parse_token_accounts(result, [addr(150), addr(152), addr(151)], addr(5)), {})      # another mint: left out
        for bad in ({'value': [None]}, {'value': 'x'}, {'value': [{'data': ['!!', 'base64']}, None, None]}):
            with self.subTest(bad=bad), self.assertRaises(W.SignalError):
                C.parse_token_accounts(bad, [addr(150), addr(152), addr(151)], token.mint)
        self.assertEqual(C.largest_rows({'value': [{'address': addr(1), 'amount': '5'}]}), [(addr(1), 5)])
        for bad in ({}, {'value': [{'address': 'bad', 'amount': '5'}]}, {'value': [{'address': addr(1), 'amount': '-5'}]}):
            with self.subTest(bad=bad), self.assertRaises(W.SignalError):
                C.largest_rows(bad)


# ------------------------------------------------------------------------------------------------------------------- the graph
def tx(signature, slot, pos, buyers, *, signers=None, tip=False, senders=(), receivers=(), sol=()):
    return {'signature': signature, 'slot': slot, 'pos': pos, 'buyers': {w: 1 for w in buyers},
            'links': {'signers': list(signers if signers is not None else buyers[:1]), 'tip': tip, 'sol_transfers': list(sol),
                      'token_senders': list(senders), 'token_receivers': list(receivers)}}


class AnalyzeTests(unittest.TestCase):
    cfg = C.make_config({})

    def run_(self, *, holders, buyers=(), creator=None, window=(), lookups=None, l13=None, hubs=(), supply=WS.SUPPLY, **kw):
        return C.analyze(holders=holders, buyers=list(buyers), creator=creator, supply_raw=supply, window=list(window), lookups=lookups or {},
                         l13_funders=l13 or {}, hubs=set(hubs), cfg=kw.pop('cfg', self.cfg), **kw)

    def test_shared_funder_links_and_a_hub_does_not(self):
        holders = {addr(1): 100 * PCT, addr(2): 50 * PCT, addr(3): 10 * PCT}
        l13 = {addr(1): F_, addr(2): F_, addr(3): addr(9)}
        out, _ = self.run_(holders=holders, l13=l13)
        self.assertEqual((out['cluster_count'], out['largest_cluster_supply_pct'], out['top_cluster_holders']), (1, 15.0, 2))
        self.assertEqual(out['linked_supply_pct_total'], 15.0)
        self.assertEqual(out['clusters'][0]['edge_types'], ['shared_funder'])     # addr(3)'s own funding is another component
        out, _ = self.run_(holders=holders, l13=l13, hubs={F_})
        self.assertEqual((out['cluster_count'], out['linked_supply_pct_total'], out['hub_funders']), (0, 0.0, [F_]))
        out, _ = self.run_(holders=holders, l13=l13, cfg=C.make_config({'ignore_funders': [F_]}))
        self.assertEqual(out['cluster_count'], 0)

    def test_a_node_that_funded_another_node_is_a_direct_transfer(self):
        out, _ = self.run_(holders={addr(1): 10 * PCT, addr(2): 20 * PCT}, l13={addr(2): addr(1)})
        self.assertEqual((out['cluster_count'], out['linked_supply_pct_total'], out['clusters'][0]['edge_types']), (1, 3.0, ['transfer']))

    def test_jito_bundle_needs_adjacent_signatures_in_one_slot_and_a_tip(self):
        holders = {addr(1): PCT, addr(2): PCT, addr(3): PCT}
        cfg = C.make_config({'same_slot_max_group': 2})        # make the 3-buyer slot a "crowd" so only the bundle can link
        bundle = [tx(sig(1), 5, 10, [addr(1)]), tx(sig(2), 5, 11, [addr(2)]), tx(sig(3), 5, 12, [addr(3)], tip=True)]
        out, _ = self.run_(holders=holders, window=bundle, cfg=cfg)
        self.assertEqual((out['jito_bundled_buys'], out['cluster_count'], out['edge_counts']['jito_bundle']), (3, 1, 2))
        gap = [bundle[0], bundle[1], dict(bundle[2], pos=14)]                    # not adjacent: the tipped one stands alone
        out, _ = self.run_(holders=holders, window=gap, cfg=cfg)
        self.assertEqual((out['jito_bundled_buys'], out['edge_counts']['jito_bundle']), (0, 0))
        untipped = [bundle[0], bundle[1], tx(sig(3), 5, 12, [addr(3)])]
        out, _ = self.run_(holders=holders, window=untipped, cfg=cfg)
        self.assertEqual((out['jito_bundled_buys'], out['cluster_count']), (0, 0))
        other_slot = [bundle[0], bundle[1], dict(bundle[2], slot=6)]
        out, _ = self.run_(holders=holders, window=other_slot, cfg=cfg)
        self.assertEqual(out['jito_bundled_buys'], 0)

    def test_same_slot_first_buys_link_but_a_crowd_does_not(self):
        holders = {addr(i): PCT for i in range(1, 11)}
        pair = [tx(sig(1), 5, 1, [addr(1)]), tx(sig(2), 5, 3, [addr(2)])]
        out, _ = self.run_(holders=holders, window=pair)
        self.assertEqual((out['cluster_count'], out['edge_counts']['same_slot']), (1, 1))
        crowd = [tx(sig(i), 5, 2 * i, [addr(i)]) for i in range(1, 11)]          # 10 > same_slot_max_group (8)
        out, _ = self.run_(holders=holders, window=crowd)
        self.assertEqual(out['cluster_count'], 0)

    def test_co_signers_token_and_sol_transfers_link(self):
        holders = {addr(1): PCT, addr(2): PCT, addr(3): PCT, addr(4): PCT, addr(5): PCT, addr(6): PCT}
        window = [tx(sig(1), 5, 1, [addr(1)], signers=[addr(1), addr(2)]),
                  tx(sig(2), 9, 2, [], signers=[addr(3)], senders=[addr(3)], receivers=[addr(4)]),
                  tx(sig(3), 20, 3, [], signers=[addr(5)], sol=[(addr(5), addr(6), 10 ** 9)])]
        out, _ = self.run_(holders=holders, window=window)
        self.assertEqual(out['cluster_count'], 3)
        self.assertEqual(out['edge_counts']['same_tx'], 1)
        self.assertEqual(out['edge_counts']['transfer'], 2)

    def test_shared_history_signature_or_slot_links_examined_holders(self):
        holders = {addr(1): PCT, addr(2): PCT, addr(3): PCT}
        lookups = {addr(1): {'sigs': [(sig(1), 50)]}, addr(2): {'sigs': [(sig(1), 50)]}, addr(3): {'sigs': [(sig(9), 77)]}}
        out, _ = self.run_(holders=holders, lookups=lookups)
        self.assertEqual((out['cluster_count'], out['top_cluster_holders'], out['edge_counts']['same_tx']), (1, 2, 1))
        same_slot = {addr(1): {'sigs': [(sig(1), 50)]}, addr(2): {'sigs': [(sig(2), 50)]}, addr(3): {'sigs': [(sig(3), 51)]}}
        out, _ = self.run_(holders=holders, lookups=same_slot)
        self.assertEqual((out['cluster_count'], out['edge_counts']['same_slot'], out['edge_counts']['same_tx']), (1, 1, 0))
        many = {addr(i): PCT for i in range(1, 11)}
        crowd = {addr(i): {'sigs': [(sig(i), 50)]} for i in range(1, 11)}                # 10 wallets in one slot: a crowd
        out, _ = self.run_(holders=many, lookups=crowd)
        self.assertEqual(out['cluster_count'], 0)

    def test_creator_cluster_and_an_unlinked_creator_owns_only_itself(self):
        holders = {addr(1): 30 * PCT, addr(2): 70 * PCT, CREATOR: 50 * PCT}
        out, _ = self.run_(holders=holders, creator=CREATOR, l13={addr(1): CREATOR})
        self.assertEqual((out['creator_cluster_supply_pct'], out['largest_cluster_supply_pct']), (8.0, 8.0))
        out, _ = self.run_(holders=holders, creator=CREATOR)
        self.assertEqual((out['creator_cluster_supply_pct'], out['cluster_count'], out['largest_cluster_supply_pct']), (5.0, 0, 0.0))
        out, missing = self.run_(holders=holders)
        self.assertIsNone(out['creator_cluster_supply_pct'])
        self.assertEqual(missing['creator_cluster_supply_pct'], 'CREATOR_UNKNOWN')

    def test_fresh_share_counts_only_holders_with_a_known_age(self):
        holders = {addr(1): PCT, addr(2): PCT, addr(3): PCT}
        lookups = {addr(1): {'fresh': True}, addr(2): {'fresh': False}, addr(3): {'fresh': None}, CREATOR: {'fresh': True}}
        out, _ = self.run_(holders=holders, creator=CREATOR, lookups=lookups)
        self.assertEqual((out['fresh_wallet_share'], out['fresh_checked']), (0.5, 2))
        out, missing = self.run_(holders=holders)
        self.assertEqual((out['fresh_wallet_share'], missing['fresh_wallet_share']), (None, 'NO_HOLDER_AGE_KNOWN'))

    def test_unknown_supply_or_holders_are_null_with_a_reason_never_zero(self):
        holders = {addr(1): PCT, addr(2): PCT}
        out, missing = self.run_(holders=holders, l13={addr(1): F_, addr(2): F_}, supply=None)
        self.assertEqual((out['largest_cluster_supply_pct'], out['linked_supply_pct_total'], out['top_cluster_holders']), (None, None, 2))
        self.assertEqual(missing['linked_supply_pct_total'], 'SUPPLY_UNAVAILABLE')
        out, missing = self.run_(holders={}, holders_ok=False)
        self.assertEqual({out[n] for n in C.HOLDER_FIELDS}, {None})
        self.assertEqual(missing['linked_supply_pct_total'], 'HOLDERS_UNAVAILABLE')
        out, missing = self.run_(holders=holders, window_ok=False)
        self.assertEqual((out['jito_bundled_buys'], missing['jito_bundled_buys']), (None, 'WINDOW_UNAVAILABLE'))


# ------------------------------------------------------------------------------------------------------------------- collector
class CollectTests(World):
    def test_the_planted_cluster_is_detected_with_its_supply(self):
        cid, token = self.plant()
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual(row, self.rows(token.mint)[0])
        self.assertEqual(row['creator'], CREATOR)
        self.assertEqual((row['cluster_count'], row['largest_cluster_supply_pct'], row['linked_supply_pct_total']), (1, 40.0, 40.0))
        self.assertEqual((row['creator_cluster_supply_pct'], row['top_cluster_holders'], row['jito_bundled_buys']), (40.0, 8, 4))
        self.assertEqual(sorted(row['largest_cluster_wallets']), sorted(A + B))
        self.assertEqual(sorted(row['clusters'][0]['wallets']), sorted(A + B + [CREATOR]))
        self.assertIn('shared_funder', row['clusters'][0]['edge_types'])
        self.assertIn('jito_bundle', row['clusters'][0]['edge_types'])
        self.assertEqual((row['fresh_wallet_share'], row['fresh_checked']), (1.0, 5))
        self.assertEqual((row['holders_source'], row['supply_source'], row['flags']), ('screen', 'screen', {}))
        self.assertEqual(row['window_after_decision_s'], 0.0)
        self.assertEqual(self.calls('getTokenLargestAccounts'), [])                  # the screen's snapshot, no read
        self.assertLessEqual(row['calls'], 20)
        self.assertEqual(row['calls'], len(self.chain.calls))
        self.assertEqual(set(row['lookups']), set(A) | {CREATOR})                    # the creator first, then the 5 largest
        self.assertFalse(row['partial'] and set(row['unavailable']) - {k for k in row['unavailable'] if k.startswith('lookup:')}, row['unavailable'])
        funding = [json.loads(e['payload']) for e in self.store.rows('events', kind=C.FUNDING_EVENT)]
        self.assertEqual(sorted(e['wallet'] for e in funding), sorted(A + [CREATOR]))
        self.assertEqual(self.store.rows('events', kind='wallet_funding'), [])        # L13's own events are never written here

    def test_a_hub_funded_set_is_not_clustered(self):
        for i in range(4):                                                # the funder funded buyers of 4 other mints before the decision
            self.store.record('wallet_funding', {'funder': HUB, 'wallet': addr(100 + i), 'mint': addr(110 + i), 'candidate_id': 1},
                              code_version='t', strategy_version='s', ts=DECISION - 3600)
        cid, token = self.plant(funders={**{w: HUB for w in A}, CREATOR: addr(92)}, bundle=False)
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual((row['cluster_count'], row['linked_supply_pct_total'], row['largest_cluster_supply_pct']), (0, 0.0, 0.0))
        self.assertEqual(row['hub_funders'], [HUB])

    def test_hub_evidence_after_the_decision_or_too_thin_does_not_make_a_hub(self):
        for i in range(3):
            self.store.record('wallet_funding', {'funder': HUB, 'wallet': addr(100 + i), 'mint': addr(110 + i)}, code_version='t',
                              strategy_version='s', ts=DECISION - 3600)
        self.store.record(C.FUNDING_EVENT, {'funder': HUB, 'wallet': addr(104), 'mint': addr(114)}, code_version='t', strategy_version='s',
                          ts=DECISION + 5)                                       # after the decision: not known then
        cid, token = self.plant(funders={**{w: HUB for w in A}, CREATOR: addr(92)}, bundle=False)
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual((row['cluster_count'], row['top_cluster_holders'], row['hub_funders']), (1, 5, []))

    def test_nothing_after_the_decision_is_used(self):
        cid, token = self.plant(bundle=False)
        early = self.chain.swap(WS.GRAD + 5, {B[0]: 10 ** 12}, mint=token.mint, pool=token.pool)
        late_slot = WS.GRAD + 260                                                  # in the 120 s window but 4 s after the decision
        late = self.chain.swap(late_slot, {B[1]: 10 ** 12, B[2]: 10 ** 12}, mint=token.mint, pool=token.pool)
        # two organic holders share a transaction, but only AFTER the decision
        shared = self.chain.next_sig()
        for wallet in (A[3], A[4]):
            self.chain.add(wallet, shared, late_slot + 10)
        row = self.collector({'ignore_funders': [F_]}).collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        fetched = [p for m, p in self.chain.calls if m == 'getTransaction']
        self.assertIn(early, fetched)
        self.assertNotIn(late, fetched)
        self.assertEqual(row['cluster_count'], 0)                                 # F_ ignored, the late tx and late shared signature unseen
        self.assertEqual(row['jito_bundled_buys'], 0)
        self.assertEqual(row['window_txs'], 1)

    def test_a_first_transaction_after_the_decision_gives_no_funder(self):
        cid, token = self.plant(funders={}, bundle=False)
        self.chain.fund(A[0], F_, slot=WS.GRAD + 600)                                # funded after the decision
        self.chain.fund(A[1], F_, slot=WS.GRAD + 610)
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual(row['unavailable']['lookup:' + A[0][:8]], 'FIRST_TX_AFTER_DECISION')
        self.assertIsNone(row['lookups'][A[0]]['funder'])
        self.assertEqual(row['cluster_count'], 0)

    def test_freshness_from_one_bounded_history_page(self):
        cid, token = self.plant(funders={}, bundle=False)
        old = DECISION - 10 * 86400

        def add_row(wallet, signature, slot, at):
            rows = self.chain.addresses.setdefault(wallet, [])
            rows.append({'signature': signature, 'slot': slot, 'err': None, 'blockTime': int(at), 'memo': None, 'confirmationStatus': 'finalized',
                         '_seq': 0})
            rows.sort(key=lambda r: -r['blockTime'])                                # newest first, as the RPC returns them
        for i in range(30):                                                         # a busy old wallet: full page, old enough
            add_row(A[0], sig(500 + i), 1, old + i)
        for i in range(30):                                                         # a busy NEW wallet: full page, all recent -> unknown
            add_row(A[1], sig(600 + i), 900, DECISION - 60 - i)
        self.chain.fund(A[2], addr(93), slot=WS.GRAD - 100)                        # a 2-signature wallet funded 40 s before graduation
        add_row(A[3], sig(700), 2, old)                                             # one old transaction: its whole history
        self.chain.bodies[sig(700)] = WS.funding_body(sig(700), 2, A[3], addr(94))
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        fresh = {w: row['lookups'][w]['fresh'] for w in A[:5]}
        self.assertEqual(fresh, {A[0]: False, A[1]: None, A[2]: True, A[3]: False, A[4]: None})
        self.assertEqual(row['unavailable']['lookup:' + A[4][:8]], 'NO_HISTORY')
        self.assertEqual((row['fresh_wallet_share'], row['fresh_checked']), (round(1 / 3, 4), 3))

    def test_a_soft_reject_without_screen_holders_reads_them_and_flags_them(self):
        cid, token = self.plant(screen_holders=False)
        self.now = DECISION + 70
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual(self.calls('getTokenLargestAccounts'), [token.mint])
        self.assertEqual(row['holders_source'], 'rpc_at_collection')
        self.assertEqual(row['late_s'], 70.0)
        self.assertEqual(set(row['flags']), set(C.HOLDER_FIELDS) | {'cluster_count'})
        self.assertEqual(set(row['flags'].values()), {'AT_COLLECTION'})
        self.assertEqual(row['linked_supply_pct_total'], 40.0)

    def test_the_supply_is_the_screens_else_one_read_flagged(self):
        cid, token = self.plant(supply=False)
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual((row['supply_source'], row['linked_supply_pct_total']), ('rpc_after_decision', 40.0))
        self.assertEqual(len(self.calls('getTokenSupply')), 1)

    def test_the_pool_vault_and_burn_accounts_are_never_holders(self):
        from lean import features as F
        cid, token = self.plant(holders=[(A[0], 56 * PCT), (A[1], 56 * PCT)], bundle=False)
        burn = sorted(F.burn_accounts(token.mint, TOKEN_PROGRAM))[0]
        self.chain.largest[token.mint].append((burn, 500 * PCT))
        body = json.loads(self.store.rows('observations', kind='screen:holders')[0]['raw'])
        self.assertEqual(body['result']['value'][0]['address'], token.base_vault)
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual((row['holders_examined'], row['holders_supply_pct']), (2, 11.2))
        self.assertNotIn(token.base_vault, self.calls('getMultipleAccounts')[0])


class BudgetTests(World):
    def test_the_hard_cap_stops_a_candidate_and_keeps_what_it_learned(self):
        cid, token = self.plant()
        row = self.collector({'max_calls': 5}).collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual((row['calls'], len(self.chain.calls)), (5, 5))
        self.assertEqual(row['unavailable']['stopped'], 'CALL_CAP')
        self.assertTrue(row['partial'])
        self.assertEqual(row['holders_examined'], 12)
        self.assertIsNone(row['jito_bundled_buys'])

    def test_every_candidate_is_bounded_by_the_default_cap(self):
        cid, token = self.plant(supply=False, screen_holders=False)       # every optional read
        row = self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertLessEqual(row['calls'], 20)
        self.assertEqual(row['calls'], len(self.chain.calls))

    def test_a_stop_writes_nothing_and_sends_nothing_more(self):
        cid, token = self.plant()
        c = self.collector()
        c.stop.set()
        self.assertIsNone(c.collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION))
        self.assertEqual((self.rows(), self.chain.calls, c.counts['stopped']), ([], [], 1))

    def test_a_shed_before_anything_was_learned_is_retried_later_and_writes_nothing(self):
        cid, token = self.plant()
        self.chain.fail['getMultipleAccounts'] = ProviderError(LANE_SHED, True, meta={'why': 'backoff'})
        c = self.collector()
        self.assertEqual(c.clusters_pass(), 1)
        self.assertEqual(self.rows(), [])
        self.assertEqual(c.counts['transient_failures'], 1)
        self.assertEqual(c.clusters_pass(), 0)                                  # retry_delay_s not over yet
        del self.chain.fail['getMultipleAccounts']
        self.now += 61
        self.assertEqual(c.clusters_pass(), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_a_shed_after_something_was_learned_ends_the_candidate_with_a_partial_row(self):
        cid, token = self.plant()
        self.chain.fail['getSignaturesForAddress'] = ProviderError(LANE_SHED, True, meta={'why': 'backoff'})
        c = self.collector()
        row = c.collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual(row['unavailable']['stopped'], LANE_SHED)
        self.assertEqual((row['calls'], c.counts['shed_stops'], c.counts['sheds']), (1, 1, 1))
        self.assertEqual(row['holders_examined'], 12)

    def test_no_token_is_paced_and_the_lane_is_left_to_the_others(self):
        cid, token = self.plant()
        helius = WS.ChainHelius(self.chain)
        helius.transport = SimpleNamespace(limiter=Limiter(1.5))              # 1.5 < 1 + reserve_tokens: yield
        c = self.collector({'shed_wait_s': 2.0}, helius=helius)
        self.assertIsNone(c.collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION))     # gave up before the 1st call
        self.assertEqual((self.chain.calls, self.sleeps), ([], [0.5] * 4))
        self.assertGreater(c.counts['yielded'], 0)
        helius.transport.limiter.tokens = 5.0
        row = c.collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual(row['linked_supply_pct_total'], 40.0)
        c0 = self.collector({'reserve_tokens': 0}, helius=helius)
        helius.transport.limiter.tokens = 0.0
        self.assertTrue(c0._yield_lane() is False)

    def test_a_no_token_shed_from_the_lane_is_paced_then_succeeds(self):
        cid, token = self.plant()
        gate = lambda attempt: ProviderError(LANE_SHED, True, meta={'why': 'no_token'}) if attempt <= 2 else None
        c = self.collector(helius=WS.ChainHelius(self.chain, gate))
        row = c.collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        self.assertEqual(self.sleeps[:2], [0.5, 0.5])
        self.assertEqual((row['linked_supply_pct_total'], c.counts['sheds']), (40.0, 2))


class PassTests(World):
    def test_waits_for_the_wallet_signals_row_then_uses_its_funder_map(self):
        cid, token = self.plant()
        c = self.collector(signals_enabled=True)
        self.now = DECISION + 30
        self.assertEqual(c.clusters_pass(), 0)
        self.assertEqual(c.counts['waiting_for_signals'], 1)
        signals = {'version': W.VERSION, 'mint': token.mint, 'funders': {A[0]: F_}, 'buyer_wallets': [A[0]] + B, 'hub_funders': []}
        self.store.add_observation('wallet_signals', json.dumps(signals).encode(), mint=token.mint, candidate_id=cid, ts=self.now)
        self.assertEqual(c.clusters_pass(), 1)
        row = self.rows()[0]
        self.assertTrue(row['signals_row'])
        self.assertEqual(row['lookups'][A[0]]['funder_source'], 'l13')
        tx_of_a0_funding = [s['signature'] for s in self.chain.addresses[A[0]] if s['slot'] < WS.GRAD]
        self.assertNotIn(tx_of_a0_funding[0], [p for m, p in self.chain.calls if m == 'getTransaction'])
        self.assertEqual(row['linked_supply_pct_total'], 40.0)
        self.assertEqual(c.clusters_pass(), 0)                                      # one row per candidate

    def test_without_the_signals_row_it_goes_ahead_after_the_wait(self):
        cid, token = self.plant()
        c = self.collector(signals_enabled=True)
        self.now = DECISION + 601
        self.assertEqual(c.clusters_pass(), 1)
        self.assertFalse(self.rows()[0]['signals_row'])

    def test_old_candidates_are_never_collected_and_errors_close_out(self):
        cid, token = self.plant()
        c = self.collector()
        self.now = WS.GRAD_TIME + 1801
        self.assertEqual(c.pending(self.now, 10), [])
        self.now = DECISION + 10
        with mock.patch.object(C, 'analyze', side_effect=RuntimeError('boom')):
            for _ in range(3):
                c.clusters_pass()
                self.now += 61
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['unavailable'], {'collect': 'COLLECT_FAILED:RuntimeError'})
        self.assertEqual({rows[0][n] for n in C.FIGURES}, {None})
        self.assertEqual(c.counts['errors'], 3)
        self.assertEqual(len(self.store.rows('errors', limit=100)), 3)

    def test_run_loop_is_stop_aware(self):
        c = self.collector()
        stop = threading.Event()
        c.stop = stop
        passes = []
        c.clusters_pass = lambda: (passes.append(1), stop.set())
        c.run_loop()
        self.assertEqual(passes, [1])
        self.assertEqual(c.health()['enabled'], True)
        self.assertIn('sheds', c.health())


# ------------------------------------------------------------------------------------------------------------------- config wiring
class ConfigWiringTests(unittest.TestCase):
    """The shipped example turns the collector on; the LIVE configs (the r1 example before L24, with and without the LINT2 keys) load
    unchanged with it OFF; adding exactly the example's ``clusters`` key turns it on, on the shared low lane."""
    EXAMPLE = LN.ROOT / 'config' / 'lean' / 'lean.example.json'
    KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}

    def live_configs(self):
        from tests.lean.test_lint2 import production_r1
        lint2b = json.loads(self.EXAMPLE.read_text())
        lint2b.update(strategy_config=str(LN.ROOT / 'config' / 'lean' / 'strategy-default.json'), initial_cash_sol='100')
        out = []
        for raw in (production_r1(), lint2b):
            raw.pop('clusters')                                     # the live file predates L24
            out.append(raw)
        return out

    def build(self, raw, d):
        from lean import __main__ as entry
        from tests.lean.fakeworld import FakeTime, T0, World
        path = Path(d) / 'lean.json'
        path.write_text(json.dumps(raw))
        cfg = entry.load_config(path)
        world = World(Path(os.path.realpath(d)), [Token(41)], FakeTime(T0))
        r = entry.build_runner(cfg, state_dir=Path(d) / 'state', discovery_db=world.discovery_db, keys=self.KEYS, code_version='l24',
                               transport_kwargs={'opener': world.opener})
        self.addCleanup(r.store.close)
        return cfg, r

    def test_the_example_turns_it_on_and_every_key_is_known(self):
        from lean import __main__ as entry
        raw = json.loads(self.EXAMPLE.read_text())
        self.assertEqual(set(raw) - {'_comment'}, set(entry.CONFIG_KEYS))
        cfg = entry.load_config(self.EXAMPLE)
        self.assertEqual(C.make_config(cfg['clusters'])['max_calls'], 20)
        self.assertLessEqual(C.make_config(cfg['clusters'])['max_calls'], 20)

    def test_the_live_configs_load_unchanged_with_the_collector_off(self):
        for raw in self.live_configs():
            with tempfile.TemporaryDirectory() as d:
                cfg, r = self.build(raw, d)
                self.assertIsNone(cfg['clusters'])
                self.assertIsNone(r.clusters)
                self.assertEqual(r.health()['clusters'], {'enabled': False, 'alive': False})

    def test_adding_exactly_the_example_key_turns_it_on(self):
        shipped = json.loads(self.EXAMPLE.read_text())['clusters']
        for raw in self.live_configs():
            raw['clusters'] = shipped
            with tempfile.TemporaryDirectory() as d:
                cfg, r = self.build(raw, d)
                self.assertEqual(r.clusters.helius.transport.lane, 'low')
                self.assertEqual(r.clusters.signals_enabled, cfg['wallet_signals'] is not None)
                self.assertEqual(r.health()['clusters']['calls'], 0)

    def test_a_bad_clusters_block_fails_at_load(self):
        from lean import __main__ as entry
        raw = self.live_configs()[1]
        raw['clusters'] = {'max_cals': 3}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'lean.json'
            path.write_text(json.dumps(raw))
            with self.assertRaises(entry.ConfigError):
                entry.load_config(path)


# ------------------------------------------------------------------------------------------------------------------- learner
class LearnerGuardTests(World):
    """``lean.learn.cluster_values`` on rows written by the REAL collector."""

    def real_row(self, **plant):
        cid, token = self.plant(**plant)
        if not plant.get('screen_holders', True):
            self.now = DECISION + 70
        self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=DECISION)
        return self.rows(token.mint)[0]

    def values(self, row, **policy):
        counts = LN._Counts()
        return LN.cluster_values(row, row['decision_ts'], LN.LookaheadPolicy(**policy), counts), counts

    def test_an_on_time_row_is_used_whole(self):
        v, c = self.values(self.real_row())
        self.assertEqual(v, {'cluster_count': D(1), 'largest_cluster_supply_pct': D('40.0'), 'top_cluster_holders': D(8),
                             'creator_cluster_supply_pct': D('40.0'), 'linked_supply_pct_total': D('40.0'), 'jito_bundled_buys': D(4),
                             'fresh_wallet_share': D('1.0')})
        self.assertEqual((c.excluded, c.skipped), ({}, {}))

    def test_a_late_holder_snapshot_is_excluded_and_counted_and_a_window_figure_is_kept(self):
        row = self.real_row(screen_holders=False)
        v, c = self.values(row)
        self.assertEqual({n for n, x in v.items() if x is not None}, {'jito_bundled_buys'})
        self.assertEqual(c.excluded['linked_supply_pct_total'], {'AT_COLLECTION_LATE': 1})
        v, c = self.values(row, max_late_s=100)
        self.assertEqual(v['linked_supply_pct_total'], D('40.0'))

    def test_supply_after_the_decision_uncut_window_unknown_flags_and_old_versions_are_out(self):
        row = self.real_row(supply=False)
        v, c = self.values(row)
        self.assertEqual((v['linked_supply_pct_total'], v['top_cluster_holders']), (None, D(8)))
        self.assertEqual(c.excluded['linked_supply_pct_total'], {'SUPPLY_READ_AFTER_DECISION': 1})
        v, c = self.values({**row, 'window_after_decision_s': None})
        self.assertIsNone(v['jito_bundled_buys'])
        v, c = self.values({**row, 'flags': {'cluster_count': 'SOMETHING'}})
        self.assertEqual((v['cluster_count'], c.excluded['cluster_count']), (None, {'FLAG_SOMETHING': 1}))
        v, c = self.values({**row, 'holders_source': None})
        self.assertEqual(c.excluded['cluster_count'], {'HOLDERS_NOT_FROM_SCREEN': 1})
        v, c = self.values({**row, 'version': 'clusters_v0'})
        self.assertEqual((set(v.values()), c.skipped), ({None}, {'CLUSTERS_OLD_VERSION': 1}))
        self.assertEqual(LN.runner_changes([LN.Rule((LN.Condition('linked_supply_pct_total', '>', D(30)),))]),
                         ['selection_rules', 'wait_for_clusters:linked_supply_pct_total'])


class LearnerWriterIntegration(RecorderMixin, World):
    """The REAL writers end to end: decisions + screen evidence (Store), the clusters row (Clusters.collect on the fake chain) and the
    L07R2 path (PathRecorder); ``build_dataset`` reads both files mode=ro. A clusters row written for a LATER decision never reaches the
    learning row; a late holder snapshot is excluded unless the policy is relaxed."""

    def test_cluster_fields_reach_the_learning_row_only_as_of_the_decision(self):
        lean = self.store
        self.setup_recorder(self.dir)
        self.store = lean                                       # World's store is the trader's lean.sqlite (the mixin's is paths)
        decision = self.clock.now()                             # the recorder starts its path just after the decision
        cid, token = self.plant(decision=decision)
        self.collector().collect(cid, token.mint, token.pool, WS.GRAD, decision_ts=decision)
        # the same candidate re-screened later: a canary clusters row stamped with that later decision
        canary = {**self.rows(token.mint)[0], 'decision_ts': decision + 500, 'linked_supply_pct_total': 666.0}
        self.store.add_observation('clusters', json.dumps(canary).encode(), mint=token.mint, candidate_id=cid, ts=decision + 501)
        self.store.add_decision('entry', 'SKIP', mint=token.mint, candidate_id=cid, reasons=['MAX_POSITIONS'], ts=decision)
        mint, _ = self.start(cid=cid, mint=token.mint)
        self.walk(mint, [1, 1.5])
        self.store.close()
        self.rec.db.close()
        ds = LN.build_dataset(self.dir / 'lean.sqlite', self.dir / 'paths.sqlite', S.StrategyConfig.load(LN.ROOT / 'config' / 'lean' / 'strategy-default.json'),
                              pool_fee_bps=25)
        self.assertEqual(len(ds.rows), 1, ds.stats)
        features = ds.rows[0].features
        self.assertEqual((features['linked_supply_pct_total'], features['creator_cluster_supply_pct'], features['jito_bundled_buys']),
                         (D('40.0'), D('40.0'), D(4)))
        self.assertEqual(ds.stats['skipped'].get('CLUSTERS_AFTER_DECISION'), 1)
        self.assertIn('clusters_version', ds.stats['lookahead'])
        self.assertTrue(set(LN.CLUSTER_FEATURES) <= set(ds.stats['feature_names']))


if __name__ == '__main__':
    unittest.main()
