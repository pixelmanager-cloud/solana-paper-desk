"""SYNTHETIC_TEST_ONLY: entry feature capture (L12F). Synthetic screens built from the fake world's real account layouts, a scripted
low-lane Helius, the real Store. No network."""
import base64
import json
import random
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from desk.security import TOKEN_2022, TOKEN_PROGRAM, base58
from lean import features as F
from lean.candidates import Candidate, Screen
from lean.providers import LANE_SHED, ProviderError
from lean.store import Store
from tests.lean.fakeworld import SUPPLY, T0 as WORLD_T0, Token

CODE, STRATEGY = 'abc1234', 'default-v1'
T0 = 1_800_000_000.0
GRAD_SLOT = 1000
GRAD_TIME = 1_799_999_500                      # the BLOCK time of the graduation (the candidate was received later)


def pk(n):
    return base58(bytes([n % 256, (n // 256) % 256]) + bytes(30))


class Fixture:
    """One token of the fake world (real PumpSwap pool / mint layouts) and the screen the lean screen would produce for it."""

    def __init__(self, tag=21, *, creator_tag=77, holders=True, **token_kwargs):
        self.token = Token(tag, coin_creator_tag=creator_tag, **token_kwargs)
        self.candidate = Candidate(seq=tag, mint=self.token.mint, pool=self.token.pool, signature='sig%d' % tag, slot=GRAD_SLOT,
                                   migrated_at=GRAD_TIME + 30, payload_hash='h')     # received 30 s AFTER the block time
        accounts = self.token.accounts(WORLD_T0)
        self.pool_account, self.mint_account = accounts[self.token.pool], accounts[self.token.mint]
        self.has_holders = holders

    def raw(self, *, pool=True, holders=None):
        body = json.dumps({'result': {'context': {'slot': 1}, 'value': [self.mint_account, self.pool_account if pool else None]}}).encode()
        out = [('accounts_mint_pool', body)]
        if self.has_holders if holders is None else holders:
            rows = self.token.holders()
            out.append(('holders', json.dumps({'result': {'context': {'slot': 1}, 'value': rows}}).encode()))
        return tuple(out)

    def features(self, **over):
        f = {'age_seconds': 400.0, 'sol_usd': '150', 'token_program': TOKEN_PROGRAM, 'decimals': 6, 'supply_raw': str(SUPPLY),
             'mint': self.token.mint, 'pool_base_token_account': self.token.base_vault, 'market_cap_usd': '90000',
             'liquidity_usd': '30000', 'price_sol_per_token': '0.0000004', 'holder_check': 'OK'}
        f.update(over)
        return f

    def screen(self, passed=True, reasons=(), feats=None, raw=None):
        feats = self.features() if feats is None else feats
        return Screen(passed, tuple(reasons), feats, (), self.raw() if raw is None else tuple(raw))


def legacy_mint(supply=10 ** 15):
    data = bytearray(82)
    data[36:44] = supply.to_bytes(8, 'little')
    data[44], data[45] = 6, 1
    return bytes(data)


def token2022_mint(extensions):
    data = bytearray(legacy_mint()) + bytes(83) + bytes([1])
    for kind, body in extensions:
        data += kind.to_bytes(2, 'little') + len(body).to_bytes(2, 'little') + body
    return bytes(data)


def transfer_fee_body(bps):
    body = bytearray(108)
    body[106:108] = bps.to_bytes(2, 'little')
    return bytes(body)


def mint_only_raw(data):
    return json.dumps({'result': {'context': {'slot': 1}, 'value': [{'data': [base64.b64encode(data).decode(), 'base64'], 'owner': TOKEN_PROGRAM,
                                                                      'lamports': 1, 'executable': False}, None]}}).encode()


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class ScriptedHelius:
    """The low-lane client: ``rpc`` -> (result, raw, meta). ``fail`` maps a method to an exception (or a list consumed per call)."""

    def __init__(self, fx, *, born=GRAD_TIME, tx_offsets=(5, 60, 599, 600), errored=(), dev_amount=10 ** 13, extra_sigs=0):
        self.fx, self.calls, self.fail = fx, [], {}
        sigs = [{'signature': 'create', 'slot': GRAD_SLOT, 'blockTime': born, 'err': None}]
        sigs += [{'signature': 't%d' % i, 'slot': GRAD_SLOT + 1 + i, 'blockTime': born + off, 'err': None} for i, off in enumerate(tx_offsets)]
        sigs += [{'signature': 'e%d' % i, 'slot': GRAD_SLOT + 50 + i, 'blockTime': born + off, 'err': {'InstructionError': [0, 'x']}}
                 for i, off in enumerate(errored)]
        sigs += [{'signature': 'late%d' % i, 'slot': GRAD_SLOT + 900 + i, 'blockTime': born + 601 + i, 'err': None} for i in range(extra_sigs)]
        self.sigs = list(reversed(sigs))                       # newest first, like the RPC
        self.dev_amount = dev_amount

    def rpc(self, method, params):
        self.calls.append((method, params))
        failure = self.fail.get(method)
        if isinstance(failure, list):
            failure = failure.pop(0) if failure else None
        if failure is not None:
            raise failure
        if method == 'getSignaturesForAddress':
            return list(self.sigs), b'{}', {}
        if method == 'getTokenAccountsByOwner':
            return {'value': [{'account': {'data': {'parsed': {'info': {'tokenAmount': {'amount': str(self.dev_amount)}}}}}}]}, b'{}', {}
        if method == 'getTokenLargestAccounts':
            return {'value': self.fx.token.holders()}, b'{}', {}
        raise AssertionError(method)

    def methods(self):
        return [m for m, _ in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(T0)
        self.store = Store(Path(self.tmp.name) / 'lean.sqlite', initial_cash_sol='5', code_version=CODE, strategy_version=STRATEGY, clock=self.clock)
        self.addCleanup(self.store.close)
        self.fx = Fixture()
        self.helius = ScriptedHelius(self.fx)
        self.rec = self.make()

    def make(self, **kw):
        return F.FeatureRecorder(store=self.store, helius=self.helius, code_version=CODE, strategy_version=STRATEGY, clock=self.clock, **kw)

    def rows(self):
        return [json.loads(r['raw']) for r in self.store.rows('observations', kind='features')]


class TestConfig(unittest.TestCase):
    def test_defaults_off_and_validation(self):
        self.assertEqual(F.config(None), {'enabled': False, 'enrich': True, 'queue_size': 500})
        self.assertEqual(F.config({'enabled': True})['enabled'], True)
        for bad in ({'enabled': 1}, {'enrich': 'yes'}, {'queue_size': 0}, {'queue_size': True}, {'queue_size': 10 ** 7}, {'nope': 1}, [], 'on'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                F.config(bad)

    def test_disabled_builds_nothing(self):
        self.assertIsNone(F.build(F.config(None), store=None, keys={}, code_version='c', strategy_version='s'))

    def test_enabled_builds_a_recorder_on_the_low_lane(self):
        rec = F.build(F.config({'enabled': True, 'queue_size': 7}), store=None, keys={'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'},
                      code_version='c', strategy_version='s')
        self.assertEqual((rec.helius.transport.lane, rec.queue.maxsize), ('low', 7))


class TestSchema(Base):
    def test_every_field_is_a_value_or_a_reason(self):
        fx = self.fx
        for scr in (fx.screen(), fx.screen(False, ['LIQUIDITY_BELOW_MIN'], raw=fx.raw(holders=False)),
                    fx.screen(False, ['TOO_YOUNG'], {'age_seconds': 5.0}, raw=())):
            row = F.build_row(fx.candidate, scr, entered=False, reason='x')
            self.assertEqual(set(row['fields']), set(F.FIELDS))
            for name in F.FIELDS:
                self.assertEqual(row['fields'][name] is None, name in row['missing'], name)
                if name in row['missing']:
                    self.assertTrue(row['missing'][name])
            self.assertEqual(row['features_version'], F.FEATURES_VERSION)

    def test_fields_that_can_never_be_filled_are_not_columns(self):
        self.assertFalse(set(F.REMOVED_FIELDS) & set(F.FIELDS))
        for name, why in F.REMOVED_FIELDS.items():
            self.assertTrue(why and name not in F.build_row(self.fx.candidate, self.fx.screen(), entered=False)['fields'])
        self.assertEqual(F.FEATURES_VERSION, 2)

    def test_the_cheap_fields_cost_no_request(self):
        row = F.build_row(self.fx.candidate, self.fx.screen(), entered=True)
        f = row['fields']
        self.assertEqual((f['market_cap_usd'], f['liquidity_usd'], f['sol_usd']), ('90000', '30000', '150'))
        self.assertEqual((f['mint_authority_active'], f['freeze_authority_active']), (False, False))
        self.assertEqual((f['token_program'], f['token2022_extensions'], f['age_seconds_at_screen']), (TOKEN_PROGRAM, [], '400.0'))
        self.assertEqual(self.helius.calls, [])

    def test_a_value_supplied_later_removes_its_reason(self):
        row = F.build_row(self.fx.candidate, self.fx.screen(), entered=False, extra={'dev_holding_pct': '3.0000'})
        self.assertEqual(row['fields']['dev_holding_pct'], '3.0000')
        self.assertNotIn('dev_holding_pct', row['missing'])

    def test_unavailable_fields_say_why(self):
        row = F.build_row(self.fx.candidate, self.fx.screen(False, ['TOO_YOUNG'], {'age_seconds': 5.0}, raw=()), entered=False)
        self.assertEqual(row['missing']['market_cap_usd'], 'SCREEN_STOPPED_BEFORE_MARKET')
        self.assertEqual(row['missing']['token_program'], 'MINT_STAGE_NOT_REACHED')
        self.assertEqual(row['missing']['creator_wallet'], 'POOL_ACCOUNT_NOT_RETAINED')
        self.assertEqual(row['missing']['top10_pct'], 'HOLDERS_NOT_LOADED')

    def test_authority_hazards_are_recorded_true_when_the_screen_says_so(self):
        row = F.build_row(self.fx.candidate, self.fx.screen(False, ['ACTIVE_MINT_AUTHORITY'], {'age_seconds': 400.0}, raw=()), entered=False)
        self.assertEqual((row['fields']['mint_authority_active'], row['missing']['freeze_authority_active']), (True, 'MINT_STAGE_NOT_REACHED'))

    def test_garbled_numbers_are_missing_not_errors(self):
        row = F.build_row(self.fx.candidate, self.fx.screen(feats=self.fx.features(market_cap_usd='abc', liquidity_usd=float('nan'))), entered=False)
        self.assertIsNone(row['fields']['market_cap_usd'])
        self.assertIsNone(row['fields']['liquidity_usd'])


class TestDevWallet(Base):
    def test_the_dev_wallet_is_the_pools_coin_creator_with_no_request(self):
        row = F.build_row(self.fx.candidate, self.fx.screen(), entered=False)
        self.assertEqual(row['fields']['creator_wallet'], self.fx.token.coin_creator)
        self.assertEqual(self.helius.calls, [])

    def test_unknown_or_unreadable_creators_say_why(self):
        zero = Fixture(23, creator_tag=None)                                              # coin_creator = the system address
        self.assertEqual(F.build_row(zero.candidate, zero.screen(), entered=False)['missing']['creator_wallet'], 'CREATOR_UNKNOWN')
        gone = self.fx.screen(raw=self.fx.raw(pool=False))
        self.assertEqual(F.build_row(self.fx.candidate, gone, entered=False)['missing']['creator_wallet'], 'POOL_ACCOUNT_MISSING')
        for raw in ((('accounts_mint_pool', b'not json'),), (('accounts_mint_pool', json.dumps({'result': {'value': [None, {'owner': 'x'}]}}).encode()),)):
            row = F.build_row(self.fx.candidate, self.fx.screen(raw=raw), entered=False)
            self.assertEqual(row['missing']['creator_wallet'], 'POOL_ACCOUNT_UNREADABLE')


class TestHolders(Base):
    def test_burn_accounts_are_derived_for_both_burn_owners_and_depend_on_the_program(self):
        legacy = F.burn_accounts(self.fx.token.mint, TOKEN_PROGRAM)
        self.assertEqual(len(legacy), 2)
        self.assertEqual(legacy & F.burn_accounts(self.fx.token.mint, TOKEN_2022), set())
        self.assertEqual(legacy & F.burn_accounts(Fixture(23).token.mint, TOKEN_PROGRAM), set())

    def test_the_pool_vault_and_the_burn_accounts_are_not_holders(self):
        burn = sorted(F.burn_accounts(self.fx.token.mint, TOKEN_PROGRAM))
        rows = [{'address': self.fx.token.base_vault, 'amount': str(SUPPLY // 2)}, {'address': burn[0], 'amount': str(SUPPLY // 10)},
                {'address': burn[1], 'amount': str(SUPPLY // 20)}]
        rows += [{'address': pk(300 + i), 'amount': str(SUPPLY // 100)} for i in range(12)]
        top1, top10 = F.holder_shares(rows, SUPPLY, set(burn) | {self.fx.token.base_vault})
        self.assertEqual((top1, top10), ('1.0000', '10.0000'))              # ten holders of 1 %: vault and burns excluded
        self.assertEqual(F.holder_shares(rows, SUPPLY, {self.fx.token.base_vault}), ('10.0000', '23.0000'))      # without the burn exclusion: the burned 10 % and 5 % count as holders

    def test_malformed_rows_and_impossible_totals_raise(self):
        for rows in ([{'address': 'a', 'amount': 'x'}], [{'address': 'a', 'amount': '-1'}], [{'amount': '5'}], 'rows',
                     [{'address': 'a', 'amount': str(SUPPLY)}, {'address': 'b', 'amount': '1'}]):
            with self.subTest(rows=rows), self.assertRaises((ValueError, KeyError, TypeError)):
                F.holder_shares(rows, SUPPLY, set())
        with self.assertRaises(ValueError):
            F.holder_shares([], 0, set())

    def test_the_row_uses_the_holder_body_the_screen_retained_and_excludes_burn(self):
        burn = sorted(F.burn_accounts(self.fx.token.mint, TOKEN_PROGRAM))
        rows = [{'address': self.fx.token.base_vault, 'amount': str(SUPPLY // 2)}, {'address': burn[0], 'amount': str(SUPPLY // 4)},
                {'address': pk(301), 'amount': str(SUPPLY // 20)}]
        raw = (self.fx.raw(holders=False)[0], ('holders', json.dumps({'result': {'value': rows}}).encode()))
        row = F.build_row(self.fx.candidate, self.fx.screen(raw=raw), entered=False)
        self.assertEqual((row['fields']['top1_pct'], row['fields']['top10_pct']), ('5.0000', '5.0000'))
        self.assertEqual(self.helius.calls, [])

    def test_a_missing_or_garbled_holder_body_says_why(self):
        row = F.build_row(self.fx.candidate, self.fx.screen(raw=self.fx.raw(holders=False)), entered=False)
        self.assertEqual(row['missing']['top10_pct'], 'HOLDERS_NOT_LOADED')
        garbled = (self.fx.raw(holders=False)[0], ('holders', b'not json'))
        self.assertEqual(F.build_row(self.fx.candidate, self.fx.screen(raw=garbled), entered=False)['missing']['top1_pct'], 'HOLDER_ROWS_MALFORMED')
        unavailable = self.fx.screen(raw=self.fx.raw(holders=False), feats=self.fx.features(holder_check='UNAVAILABLE:HTTP_503'))
        self.assertEqual(F.build_row(self.fx.candidate, unavailable, entered=False)['missing']['top10_pct'], 'HOLDER_CHECK_UNAVAILABLE:HTTP_503')


class TestToken2022(unittest.TestCase):
    def test_legacy_has_no_extensions(self):
        self.assertEqual(F.mint_extensions(legacy_mint()), ([], None))

    def test_extensions_and_transfer_fee_known_answer(self):
        data = token2022_mint([(18, bytes(64)), (1, transfer_fee_body(250))])
        self.assertEqual(F.mint_extensions(data), (['MetadataPointer', 'TransferFeeConfig'], 250))

    def test_unknown_extension_is_named_not_dropped(self):
        self.assertEqual(F.mint_extensions(token2022_mint([(99, b'\x00')]))[0], ['UNKNOWN_99'])

    def test_zero_padding_after_the_last_extension_is_not_an_extension(self):
        self.assertEqual(F.mint_extensions(token2022_mint([(18, bytes(64))]) + bytes(12))[0], ['MetadataPointer'])

    def test_malformed_bytes_raise(self):
        for bad in (bytes(100), token2022_mint([(1, transfer_fee_body(1))])[:-5], bytes(166)):
            with self.assertRaises(ValueError):
                F.mint_extensions(bad)

    def test_row_records_extensions_and_fee_from_the_retained_mint_bytes(self):
        fx = Fixture()
        raw = (('accounts_mint_pool', mint_only_raw(token2022_mint([(1, transfer_fee_body(300))]))),)
        f = F.build_row(fx.candidate, fx.screen(raw=raw), entered=False)['fields']
        self.assertEqual((f['token2022_extensions'], f['transfer_fee_bps']), (['TransferFeeConfig'], 300))

    def test_row_without_fee_extension_says_so_and_garbage_does_not_raise(self):
        fx = Fixture()
        row = F.build_row(fx.candidate, fx.screen(raw=(('accounts_mint_pool', mint_only_raw(token2022_mint([(18, bytes(64))]))),)), entered=False)
        self.assertEqual(row['missing']['transfer_fee_bps'], 'NO_TRANSFER_FEE_EXTENSION')
        for raw in ((('accounts_mint_pool', b'not json'),), (('accounts_mint_pool', mint_only_raw(bytes(100))),), ()):
            self.assertIn('token2022_extensions', F.build_row(fx.candidate, fx.screen(raw=raw), entered=False)['missing'])


class TestEnrich(Base):
    SCREEN_TIME = GRAD_TIME + 30 + 400.0

    def run_enrich(self, *, need_holders=False, creator='default', screen=None, helius=None):
        creator = self.fx.token.coin_creator if creator == 'default' else creator
        return F.enrich(self.fx.candidate, screen or self.fx.screen(), helius or self.helius, creator=creator, need_holders=need_holders,
                        screen_time=self.SCREEN_TIME)

    def test_a_passed_candidate_costs_two_calls_and_gets_block_time_figures(self):
        fields, missing, calls = self.run_enrich()
        self.assertEqual(calls, 2)
        self.assertEqual(self.helius.methods(), ['getTokenAccountsByOwner', 'getSignaturesForAddress'])
        self.assertEqual(self.helius.calls[0][1][:2], [self.fx.token.coin_creator, {'mint': self.fx.token.mint}])
        self.assertEqual(self.helius.calls[1][1][0], self.fx.candidate.pool)                    # the POOL's history, not the mint's
        self.assertEqual(fields['dev_holding_pct'], '1.0000')                                   # 1e13 of 1e15
        self.assertEqual(fields['graduation_block_time'], GRAD_TIME)                            # the oldest signature's block time
        self.assertEqual(fields['tx_count_first_10m'], 4)                                       # offsets 5, 60, 599, 600 (inclusive)
        # block time, NOT the discovery receive time (30 s later): 430 s after graduation, not 400
        self.assertEqual(fields['seconds_graduation_to_screen'], 430.0)
        self.assertEqual(missing, {})

    def test_a_soft_reject_spends_the_third_call_on_the_holders(self):
        screen = self.fx.screen(False, ['LIQUIDITY_BELOW_MIN'], raw=self.fx.raw(holders=False))
        fields, missing, calls = self.run_enrich(need_holders=True, screen=screen)
        self.assertEqual((calls, self.helius.methods()), (3, ['getTokenAccountsByOwner', 'getSignaturesForAddress', 'getTokenLargestAccounts']))
        self.assertEqual(fields['top1_pct'], '1.0000')                                           # the fake world's ten holders of 1 %
        self.assertEqual(fields['top10_pct'], '10.0000')                                         # the pool vault is excluded
        self.assertEqual(missing, {})

    def test_never_more_than_three_calls(self):
        self.run_enrich(need_holders=True)
        self.assertLessEqual(len(self.helius.calls), F.MAX_EXTRA_CALLS)

    def test_the_tx_count_window_boundaries_and_failed_transactions(self):
        self.helius = ScriptedHelius(self.fx, tx_offsets=(0, 600, 601), errored=(10,), extra_sigs=3)
        fields, _, _ = self.run_enrich()
        self.assertEqual(fields['tx_count_first_10m'], 2)                                        # 0 and 600; 601+ and the failed one are out

    def test_unknown_creator_skips_the_dev_call(self):
        fields, missing, calls = self.run_enrich(creator=None)
        self.assertEqual((calls, self.helius.methods()), (1, ['getSignaturesForAddress']))
        self.assertEqual(missing['dev_holding_pct'], 'CREATOR_UNKNOWN')

    def test_lane_shed_ends_the_enrichment_and_sends_nothing_more(self):
        self.helius.fail['getTokenAccountsByOwner'] = ProviderError(LANE_SHED, True)
        fields, missing, calls = self.run_enrich(need_holders=True)
        self.assertEqual(self.helius.methods(), ['getTokenAccountsByOwner'])                     # nothing after the shed
        self.assertEqual(fields, {})
        self.assertEqual({v for v in missing.values()}, {LANE_SHED})
        self.assertEqual(set(missing), {'dev_holding_pct', 'graduation_block_time', 'seconds_graduation_to_screen', 'tx_count_first_10m',
                                        'top10_pct', 'top1_pct'})

    def test_a_shed_in_the_middle_keeps_what_was_learned(self):
        self.helius.fail['getSignaturesForAddress'] = ProviderError(LANE_SHED, True)
        fields, missing, _ = self.run_enrich(need_holders=True)
        self.assertEqual(fields['dev_holding_pct'], '1.0000')
        self.assertEqual(missing['tx_count_first_10m'], LANE_SHED)
        self.assertNotIn('getTokenLargestAccounts', self.helius.methods())

    def test_another_provider_error_costs_only_its_own_fields(self):
        self.helius.fail['getTokenAccountsByOwner'] = ProviderError('HTTP_503', True)
        fields, missing, calls = self.run_enrich(need_holders=True)
        self.assertEqual(missing['dev_holding_pct'], 'PROVIDER_HTTP_503')
        self.assertEqual(fields['tx_count_first_10m'], 4)
        self.assertEqual(fields['top10_pct'], '10.0000')
        self.assertEqual(calls, 3)

    def test_a_long_history_is_truncated_not_paged(self):
        self.helius.sigs = [{'signature': 's%d' % i, 'slot': GRAD_SLOT + i, 'blockTime': GRAD_TIME + i, 'err': None} for i in range(F.SIGNATURE_LIMIT)]
        fields, missing, _ = self.run_enrich()
        self.assertEqual(self.helius.methods().count('getSignaturesForAddress'), 1)              # one call, no paging
        self.assertEqual({missing[n] for n in ('graduation_block_time', 'tx_count_first_10m', 'seconds_graduation_to_screen')}, {'HISTORY_TRUNCATED'})

    def test_the_oldest_signature_must_be_the_graduation_slot(self):
        self.helius.sigs = [{'signature': 'x', 'slot': GRAD_SLOT + 5, 'blockTime': GRAD_TIME, 'err': None}]
        _, missing, _ = self.run_enrich()
        self.assertEqual(missing['graduation_block_time'], 'POOL_HISTORY_NOT_FROM_GRADUATION')

    def test_malformed_answers_are_reasons_not_errors(self):
        for sigs, why in (([], 'SIGNATURES_UNAVAILABLE'), ('x', 'SIGNATURES_UNAVAILABLE'), ([5], 'SIGNATURES_UNAVAILABLE'),
                          ([{'signature': 'x', 'slot': GRAD_SLOT, 'blockTime': None, 'err': None}], 'GRADUATION_BLOCK_TIME_UNKNOWN'),
                          ([{'signature': 'x', 'slot': GRAD_SLOT, 'blockTime': True, 'err': None}], 'GRADUATION_BLOCK_TIME_UNKNOWN')):
            self.helius.sigs = sigs
            _, missing, _ = self.run_enrich()
            self.assertEqual(missing['graduation_block_time'], why, sigs)

    def test_dev_balance_above_supply_is_unreadable_not_a_number(self):
        for amount in (SUPPLY + 1, -1):
            self.helius.dev_amount = amount
            _, missing, _ = self.run_enrich()
            self.assertEqual(missing['dev_holding_pct'], 'DEV_BALANCE_UNREADABLE')
        self.helius.dev_amount = SUPPLY
        self.assertEqual(self.run_enrich()[0]['dev_holding_pct'], '100.0000')

    def test_an_unknown_screen_time_is_a_reason(self):
        fields, missing, _ = F.enrich(self.fx.candidate, self.fx.screen(), self.helius, creator=self.fx.token.coin_creator, need_holders=False, screen_time=None)
        self.assertEqual(missing['seconds_graduation_to_screen'], 'SCREEN_TIME_UNKNOWN')
        self.assertEqual(fields['graduation_block_time'], GRAD_TIME)


class TestRecorder(Base):
    def test_one_row_with_enrichment_versions_and_candidate_id(self):
        self.assertTrue(self.rec.submit(self.fx.candidate, self.fx.screen(), entered=True, candidate_id=5, screen_at=T0))
        self.assertEqual(self.rows(), [])                                                       # queued: the caller never waits
        self.clock.t += 7
        self.assertTrue(self.rec.process_one())
        (row,) = self.rows()
        (obs,) = self.store.rows('observations', kind='features')
        self.assertEqual((obs['candidate_id'], obs['code_version'], obs['strategy_version']), (5, CODE, STRATEGY))
        self.assertEqual(json.loads(obs['meta']), row)                                          # the report reads meta, never the blob
        self.assertEqual((row['entered'], row['not_entered_reason'], row['calls'], row['features_version']), (True, None, 2, 2))
        self.assertEqual((row['screen_at'], row['collected_at']), (T0, T0 + 7))                  # the enrichment is later than the decision
        self.assertEqual(row['fields']['dev_holding_pct'], '1.0000')
        self.assertEqual(row['fields']['seconds_graduation_to_screen'], 430.0)

    def test_what_became_of_the_candidate_is_read_from_the_store(self):
        fx, mint = self.fx, self.fx.token.mint
        cid = self.store.add_candidate(mint, pool=fx.candidate.pool, slot=GRAD_SLOT, ts=T0)
        self.assertEqual(self.rec.outcome(cid, mint, fx.screen()), (False, 'ABORTED_BY_ERROR'))          # passed, nothing else recorded
        self.assertEqual(self.rec.outcome(cid, mint, fx.screen(False, ['LIQUIDITY_BELOW_MIN', 'X'])), (False, 'LIQUIDITY_BELOW_MIN,X'))
        self.store.add_decision('entry', 'SKIP', mint=mint, candidate_id=cid, reasons=['COST_BUDGET'], ts=T0)
        self.assertEqual(self.rec.outcome(cid, mint, fx.screen()), (False, 'COST_BUDGET'))
        self.store.add_decision('entry', 'SKIP', mint=mint, candidate_id=cid, reasons=['MAX_POSITIONS'], ts=T0 + 1)
        self.assertEqual(self.rec.outcome(cid, mint, fx.screen()), (False, 'MAX_POSITIONS'))               # the LAST entry decision
        other = self.store.add_candidate(pk(999), pool=pk(998), slot=1, ts=T0)
        self.assertEqual(self.rec.outcome(other, mint, fx.screen()), (False, 'ABORTED_BY_ERROR'))          # another candidate's decision is not ours
        from lean.paper import PaperConfig, Quote, buy
        self.store.add_fill(buy(Quote(mint, 'buy', 20_000_000, 10 ** 9, 6, T0, ref='r'), '0.02', PaperConfig()), candidate_id=cid,
                            state={'event': 'open', 'state': {}})
        self.assertEqual(self.rec.outcome(cid, mint, fx.screen()), (True, None))

    def test_on_candidate_hands_the_outcome_to_the_queue(self):
        cid = self.store.add_candidate(self.fx.token.mint, pool=self.fx.candidate.pool, slot=GRAD_SLOT, ts=T0)
        self.store.add_decision('entry', 'SKIP', mint=self.fx.token.mint, candidate_id=cid, reasons=['COST_BUDGET'], ts=T0)
        self.assertTrue(self.rec.on_candidate(self.fx.candidate, cid, self.fx.screen()))
        self.rec.process_one()
        self.assertEqual((self.rows()[0]['entered'], self.rows()[0]['not_entered_reason']), (False, 'COST_BUDGET'))

    def test_duplicate_submit_gives_one_row(self):
        for _ in range(3):
            self.rec.submit(self.fx.candidate, self.fx.screen(), entered=False, candidate_id=5)
        while self.rec.process_one():
            pass
        self.assertEqual(len(self.rows()), 1)

    def test_hazard_reject_gets_its_cheap_row_and_no_spend(self):
        self.rec.submit(self.fx.candidate, self.fx.screen(False, ['ACTIVE_MINT_AUTHORITY'], raw=()), entered=False, reason='r')
        self.rec.process_one()
        self.assertEqual(self.helius.calls, [])
        self.assertEqual(self.rows()[0]['missing']['dev_holding_pct'], 'NOT_ENRICHED_HAZARD_REJECT')

    def test_a_soft_reject_is_enriched_with_holders(self):
        screen = self.fx.screen(False, ['MARKET_CAP_ABOVE_MAX'], raw=self.fx.raw(holders=False))
        self.rec.submit(self.fx.candidate, screen, entered=False, reason='MARKET_CAP_ABOVE_MAX')
        self.rec.process_one()
        self.assertEqual(self.helius.methods(), ['getTokenAccountsByOwner', 'getSignaturesForAddress', 'getTokenLargestAccounts'])
        row = self.rows()[0]
        self.assertEqual((row['calls'], row['fields']['top10_pct']), (3, '10.0000'))

    def test_a_shed_candidate_still_gets_its_cheap_row(self):
        self.helius.fail['getTokenAccountsByOwner'] = ProviderError(LANE_SHED, True)
        self.rec.submit(self.fx.candidate, self.fx.screen(), entered=False, reason='r')
        self.rec.process_one()
        row = self.rows()[0]
        self.assertEqual((row['missing']['dev_holding_pct'], row['missing']['tx_count_first_10m'], row['fields']['market_cap_usd']), (LANE_SHED,) * 2 + ('90000',))
        self.assertEqual(self.rec.stats['shed'], 1)

    def test_provider_failure_never_loses_the_row(self):
        self.helius.fail.update({m: RuntimeError('boom') for m in ('getTokenAccountsByOwner', 'getSignaturesForAddress')})
        self.rec.submit(self.fx.candidate, self.fx.screen(), entered=False, reason='r')
        self.rec.process_one()
        self.assertEqual(len(self.rows()), 1)

    def test_queue_full_writes_the_cheap_row_at_once(self):
        rec = self.make(queue_size=1)
        rec.submit(self.fx.candidate, self.fx.screen(), entered=False, candidate_id=1)
        rec.submit(Fixture(23).candidate, Fixture(23).screen(), entered=False, candidate_id=2)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['missing']['dev_holding_pct'], 'QUEUE_FULL')
        self.assertEqual(rec.stats['queue_full'], 1)

    def test_enrichment_disabled_writes_at_submit(self):
        rec = self.make(enrich_enabled=False)
        rec.submit(self.fx.candidate, self.fx.screen(), entered=False)
        self.assertEqual(self.rows()[0]['missing']['dev_holding_pct'], 'ENRICHMENT_DISABLED')
        self.assertEqual(self.helius.calls, [])

    def test_submit_and_on_candidate_never_raise(self):
        def boom(*args, **kwargs):
            raise RuntimeError('recorder bug')
        self.rec.queue.put_nowait = boom
        self.assertFalse(self.rec.submit(self.fx.candidate, self.fx.screen(), entered=False))
        self.rec.outcome = boom
        self.assertFalse(self.rec.on_candidate(self.fx.candidate, 1, self.fx.screen()))
        self.assertEqual((self.rec.stats['dropped'], self.rec.stats['errors']), (2, 2))

    def test_store_failure_in_the_worker_is_counted_not_raised(self):
        self.rec.submit(self.fx.candidate, self.fx.screen(), entered=False)
        self.store.close()
        self.assertTrue(self.rec.process_one())
        self.assertGreaterEqual(self.rec.stats['errors'], 1)

    def test_rows_are_append_only(self):
        self.rec.submit(self.fx.candidate, self.fx.screen(), entered=False)
        self.rec.process_one()
        with self.assertRaises(Exception):
            self.store.db.execute('DELETE FROM observations')

    def test_run_drains_the_queue_on_stop(self):
        for i in range(3):
            fx = Fixture(30 + i)
            self.rec.submit(fx.candidate, fx.screen(), entered=False, candidate_id=i)
        stop = threading.Event()
        stop.set()
        self.rec.run(stop)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual(self.rec.health()['queued'], 0)


class TestQuantileTies(unittest.TestCase):
    def test_equal_values_never_share_two_buckets(self):
        pts = [(Decimal(v), 0) for v in (0, 0, 0, 0, 0, 0, 1, 2, 3, 4)]
        groups = F.quantile_groups(pts, 4)
        self.assertEqual([[p[0] for p in g] for g in groups], [[Decimal(0)] * 6, [Decimal(1)], [Decimal(2), Decimal(3), Decimal(4)]])
        self.assertEqual(sum(len(g) for g in groups), 10)

    def test_all_equal_is_one_bucket_and_distinct_values_split_evenly(self):
        self.assertEqual(len(F.quantile_groups([(Decimal(5), 0)] * 9, 4)), 1)
        groups = F.quantile_groups([(Decimal(i), 0) for i in range(8)], 4)
        self.assertEqual([len(g) for g in groups], [2, 2, 2, 2])
        self.assertEqual(F.quantile_groups([], 4), [])
        self.assertEqual(len(F.quantile_groups([(Decimal(1), 0)], 4)), 1)

    def test_property_no_value_is_split_and_nothing_is_lost(self):
        rng = random.Random(7)
        for _ in range(300):
            n = rng.randint(1, 60)
            pts = sorted(((Decimal(rng.randint(0, 6)), i) for i in range(n)), key=lambda p: p[0])
            groups = F.quantile_groups(pts, rng.randint(1, 8))
            self.assertEqual([p for g in groups for p in g], pts)
            seen = [{p[0] for p in g} for g in groups]
            for i, a in enumerate(seen):
                for b in seen[i + 1:]:
                    self.assertFalse(a & b, (a, b))


class TestFeatureOutcomeTable(unittest.TestCase):
    @staticmethod
    def rows(spec):
        return [{'mint': 'm%d' % i, 'entered': e, 'fields': {'dev_holding_pct': str(v), 'sol_usd': '150', 'creator_wallet': 'W'}} for i, (v, e) in enumerate(spec)]

    def test_known_answer_buckets(self):
        rows = self.rows([(i, True) for i in range(8)])
        realized = {'m%d' % i: (1000 if i >= 4 else -500) for i in range(8)}
        table = F.feature_outcome_table(rows, realized, buckets=2, min_total=1, min_bucket=1)
        self.assertEqual(set(table), {'dev_holding_pct'})                                          # non-numeric / sol_usd skipped
        low, high = table['dev_holding_pct']['buckets']
        self.assertEqual((low['n'], low['win_rate'], low['mean_pnl_lamports']), (4, 0.0, -500))
        self.assertEqual((high['n'], high['win_rate'], high['mean_pnl_lamports']), (4, 1.0, 1000))

    def test_tied_values_share_a_bucket_in_the_table(self):
        rows = self.rows([(0, True)] * 6 + [(5, True)] * 2)
        realized = {'m%d' % i: (100 if i < 3 else -100) for i in range(8)}
        table = F.feature_outcome_table(rows, realized, buckets=4, min_total=1, min_bucket=1)
        buckets = table['dev_holding_pct']['buckets']
        self.assertEqual([(b['lo'], b['hi'], b['n']) for b in buckets], [('0', '0', 6), ('5', '5', 2)])

    def test_small_samples_are_flagged_and_only_entered_closed_count(self):
        rows = self.rows([(i, i % 2 == 0) for i in range(6)])
        realized = {'m0': 5, 'm1': 5, 'm2': -5}
        table = F.feature_outcome_table(rows, realized)
        self.assertEqual(table['dev_holding_pct']['n'], 2)                                         # m0 and m2: entered AND closed
        self.assertTrue(table['dev_holding_pct']['insufficient'])
        self.assertTrue(all(b['insufficient'] for b in table['dev_holding_pct']['buckets']))
        self.assertEqual(F.feature_outcome_table([], {}), {})

    def test_outcomes_and_rows_from_a_real_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / 'lean.sqlite', initial_cash_sol='5', code_version=CODE, strategy_version=STRATEGY, clock=Clock(T0))
        self.addCleanup(store.close)
        from lean.paper import PaperConfig, Quote, buy, sell
        cfg = PaperConfig()
        fill = buy(Quote(pk(1), 'buy', 20_000_000, 10 ** 9, 6, 1.0, ref='r'), '0.02', cfg)
        open_id = store.add_fill(fill, state={'event': 'open', 'state': {}})
        position = store.positions()[pk(1)]
        s = sell(position, Quote(pk(1), 'sell', position.qty_raw, 45_000_000, 6, 2.0, ref='r'), cfg=cfg, qty_raw=position.qty_raw)
        store.add_fill(s, state={'event': 'closed', 'open_fill_id': open_id, 'state': {'reason': 'X', 'trade_pnl_lamports': position.realized_lamports + s.realized_lamports}})
        self.assertEqual(F.outcomes(store), {pk(1): position.realized_lamports + s.realized_lamports})
        store.add_observation('features', json.dumps({'mint': pk(1), 'entered': True, 'fields': {}, 'missing': {}}).encode(), mint=pk(1))
        store.add_observation('features', b'not json', mint=pk(2))
        self.assertEqual(len(F.feature_rows(store)), 1)


class TestReport(unittest.TestCase):
    def test_the_report_section_reads_meta_and_buckets_the_closed_entered_rows(self):
        from lean import report
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'lean.sqlite'
        store = Store(path, initial_cash_sol='5', code_version=CODE, strategy_version=STRATEGY, clock=Clock(T0))
        self.addCleanup(store.close)
        from lean.paper import PaperConfig, Quote, buy, sell
        cfg = PaperConfig()
        for i in range(8):
            mint = pk(10 + i)
            fx = Fixture(40 + i)
            store.add_candidate(mint, ts=T0)
            row = F.build_row(fx.candidate, fx.screen(), entered=True, extra={'dev_holding_pct': str(i)})
            row['mint'] = mint
            store.add_observation('features', F.canonical(row), mint=mint, meta=row)
            fill = buy(Quote(mint, 'buy', 20_000_000, 10 ** 9, 6, 1.0, ref='r'), '0.02', cfg)
            open_id = store.add_fill(fill, state={'event': 'open', 'state': {}})
            position = store.positions()[mint]
            exit_lamports = 45_000_000 if i >= 4 else 5_000_000
            s = sell(position, Quote(mint, 'sell', position.qty_raw, exit_lamports, 6, 2.0, ref='r'), cfg=cfg, qty_raw=position.qty_raw)
            store.add_fill(s, state={'event': 'closed', 'open_fill_id': open_id, 'state': {'reason': 'X', 'trade_pnl_lamports': position.realized_lamports + s.realized_lamports}})
        data = report.features_report(path)
        self.assertEqual((data['rows'], data['entered_rows'], data['closed_entered']), (8, 8, 8))
        table = data['table']['dev_holding_pct']
        self.assertTrue(table['insufficient'])                                                     # 8 < 30 samples: flagged
        self.assertEqual([b['win_rate'] for b in table['buckets']], [0.0, 0.0, 1.0, 1.0])
        summary = report.build(path, now=T0)
        self.assertEqual(summary['features']['status'], 'OK')
        self.assertIn('features vs outcome', report.render_html(summary))

    def test_a_store_without_feature_rows_reports_zeros(self):
        from lean import report
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'lean.sqlite'
        Store(path, initial_cash_sol='5', code_version=CODE, strategy_version=STRATEGY).close()
        self.assertEqual(report.features_report(path)['rows'], 0)


if __name__ == '__main__':
    unittest.main()
