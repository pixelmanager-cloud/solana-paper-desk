"""SYNTHETIC_TEST_ONLY: entry feature capture (L12). Synthetic screens, a scripted Helius, the real Store. No network."""
import base64
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path

from desk.security import TOKEN_2022, TOKEN_PROGRAM, base58
from lean import features as F
from lean.candidates import Candidate, Screen
from lean.providers import ProviderError
from lean.store import Store

CODE, STRATEGY = 'abc1234', 'default-v1'
T0 = 1_800_000_000.0


def pk(n):
    return base58(bytes([n % 256, (n // 256) % 256]) + bytes(30))


def cand(n=1, migrated_at=T0):
    return Candidate(seq=n, mint=pk(100 + n), pool=pk(200 + n), signature='sig%d' % n, slot=1000, migrated_at=migrated_at, payload_hash='h')


def legacy_mint(supply=10 ** 15):
    data = bytearray(82)
    data[36:44] = supply.to_bytes(8, 'little')
    data[44], data[45] = 6, 1
    return bytes(data)


def token2022_mint(extensions):
    """82-byte base + padding to 165 + account type 1 + TLV extensions [(type, body)]."""
    data = bytearray(legacy_mint()) + bytes(83) + bytes([1])
    for kind, body in extensions:
        data += kind.to_bytes(2, 'little') + len(body).to_bytes(2, 'little') + body
    return bytes(data)


def transfer_fee_body(bps):
    body = bytearray(108)
    body[106:108] = bps.to_bytes(2, 'little')
    return bytes(body)


def mint_raw(data):
    return json.dumps({'result': {'context': {'slot': 1}, 'value': [{'data': [base64.b64encode(data).decode(), 'base64'], 'owner': TOKEN_PROGRAM,
                                                                      'lamports': 1, 'executable': False}, None]}}).encode()


def good_features(**over):
    f = {'age_seconds': 400.0, 'sol_usd': '150', 'token_program': TOKEN_PROGRAM, 'decimals': 6, 'supply_raw': str(10 ** 15),
         'market_cap_usd': '90000', 'liquidity_usd': '30000', 'price_sol_per_token': '0.0000004', 'holder_check': 'OK',
         'top10_pct_excluding_pool': '31.5', 'top1_pct_excluding_pool': '9.1'}
    f.update(over)
    return f


def screen(passed=True, reasons=(), feats=None, raw=None):
    return Screen(passed, tuple(reasons), good_features() if feats is None else feats, (), tuple(raw) if raw is not None else
                  (('accounts_mint_pool', mint_raw(legacy_mint())),))


class Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class ScriptedHelius:
    """Answers the three enrichment calls; ``fail`` maps a method to an exception to raise."""

    def __init__(self, creation=T0 - 600, extra_sigs=(), payer=None, dev_amount=10 ** 13):
        self.calls, self.fail = [], {}
        self.payer = payer or pk(900)
        sigs = [{'signature': 'create', 'slot': 10, 'blockTime': int(creation), 'err': None}]
        sigs += [{'signature': 's%d' % i, 'slot': 11 + i, 'blockTime': int(creation) + off, 'err': err} for i, (off, err) in enumerate(extra_sigs)]
        self.sigs = list(reversed(sigs))                   # newest first, like the RPC
        self.dev_amount = dev_amount

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method in self.fail:
            raise self.fail[method]
        if method == 'getSignaturesForAddress':
            return self.sigs, b'{}', {}
        if method == 'getTransaction':
            return {'transaction': {'message': {'accountKeys': [{'pubkey': self.payer, 'signer': True}, {'pubkey': pk(1), 'signer': False}]}}}, b'{}', {}
        if method == 'getTokenAccountsByOwner':
            return {'value': [{'account': {'data': {'parsed': {'info': {'tokenAmount': {'amount': str(self.dev_amount)}}}}}}]}, b'{}', {}
        raise AssertionError(method)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock(T0)
        self.store = Store(Path(self.tmp.name) / 'lean.sqlite', initial_cash_sol='5', code_version=CODE, strategy_version=STRATEGY, clock=self.clock)
        self.helius = ScriptedHelius()
        self.bclock = Clock(0.0)
        self.rec = self.make()

    def make(self, **kw):
        kw.setdefault('bucket', F.LowPriorityBucket(2.0, 3, clock=self.bclock))
        return F.FeatureRecorder(store=self.store, helius=self.helius, code_version=CODE, strategy_version=STRATEGY, clock=self.clock, **kw)

    def rows(self):
        return [json.loads(r['raw']) for r in self.store.rows('observations', kind='features')]


class TestSchema(Base):
    def test_every_field_is_a_value_or_a_reason(self):
        for scr in (screen(), screen(False, ['LIQUIDITY_BELOW_MIN']), screen(False, ['TOO_YOUNG'], {'age_seconds': 5.0}, raw=())):
            row = F.build_row(cand(), scr, entered=False, reason='x')
            self.assertEqual(set(row['fields']), set(F.FIELDS))
            for name in F.FIELDS:
                self.assertEqual(row['fields'][name] is None, name in row['missing'], name)
                if name in row['missing']:
                    self.assertTrue(row['missing'][name])
            self.assertEqual(row['features_version'], F.FEATURES_VERSION)

    def test_cheap_fields_come_from_the_screen_without_any_call(self):
        f = F.build_row(cand(), screen(), entered=True)['fields']
        self.assertEqual((f['market_cap_usd'], f['liquidity_usd'], f['sol_usd']), ('90000', '30000', '150'))
        self.assertEqual((f['top10_pct_excl_pool'], f['top1_pct_excl_pool']), ('31.5', '9.1'))
        self.assertEqual((f['mint_authority_active'], f['freeze_authority_active']), (False, False))
        self.assertEqual((f['token_program'], f['token2022_extensions'], f['age_seconds_at_screen']), (TOKEN_PROGRAM, [], '400.0'))
        self.assertEqual(self.helius.calls, [])

    def test_a_value_supplied_later_removes_its_reason(self):
        row = F.build_row(cand(), screen(), entered=False, extra={'holder_count': 12})
        self.assertEqual(row['fields']['holder_count'], 12)
        self.assertNotIn('holder_count', row['missing'])

    def test_unavailable_fields_say_why(self):
        m = F.build_row(cand(), screen(), entered=False)['missing']
        self.assertEqual(m['holder_count'], 'REQUIRES_FULL_HOLDER_SCAN')
        self.assertEqual({m[k] for k in ('buys_first_10m', 'sells_first_10m', 'unique_buyers_first_10m', 'buy_volume_sol_first_10m')}, {F.NEEDS_PARSED})
        self.assertEqual({m[k] for k in ('has_twitter', 'has_telegram', 'has_website')}, {F.NEEDS_METADATA})

    def test_authority_hazards_are_recorded_true_when_the_screen_says_so(self):
        scr = screen(False, ['ACTIVE_MINT_AUTHORITY', 'ACTIVE_FREEZE_AUTHORITY'], {'age_seconds': 400.0}, raw=())
        row = F.build_row(cand(), scr, entered=False)
        self.assertEqual((row['fields']['mint_authority_active'], row['fields']['freeze_authority_active']), (True, True))

    def test_rejected_before_the_mint_stage_has_unknown_authorities(self):
        row = F.build_row(cand(), screen(False, ['TOO_YOUNG'], {'age_seconds': 5.0}, raw=()), entered=False)
        self.assertIsNone(row['fields']['mint_authority_active'])
        self.assertEqual(row['missing']['mint_authority_active'], 'MINT_STAGE_NOT_REACHED')
        self.assertEqual(row['missing']['market_cap_usd'], 'SCREEN_STOPPED_BEFORE_MARKET')

    def test_holder_unavailable_reason_is_kept(self):
        row = F.build_row(cand(), screen(False, ['HOLDERS_UNAVAILABLE'], good_features(holder_check='UNAVAILABLE:HTTP_503',
                                                                                        top10_pct_excluding_pool=None, top1_pct_excluding_pool=None)), entered=False)
        self.assertEqual(row['missing']['top10_pct_excl_pool'], 'HOLDER_CHECK_UNAVAILABLE:HTTP_503')

    def test_garbled_numbers_are_missing_not_errors(self):
        row = F.build_row(cand(), screen(feats=good_features(market_cap_usd='NaN', liquidity_usd='abc', sol_usd=None)), entered=False)
        for name in ('market_cap_usd', 'liquidity_usd', 'sol_usd'):
            self.assertIsNone(row['fields'][name])
            self.assertIn(name, row['missing'])


class TestToken2022(unittest.TestCase):
    def test_legacy_has_no_extensions(self):
        self.assertEqual(F.mint_extensions(legacy_mint()), ([], None))

    def test_extensions_and_transfer_fee_known_answer(self):
        data = token2022_mint([(18, bytes(64)), (1, transfer_fee_body(250))])
        self.assertEqual(F.mint_extensions(data), (['MetadataPointer', 'TransferFeeConfig'], 250))

    def test_unknown_extension_is_named_not_dropped(self):
        self.assertEqual(F.mint_extensions(token2022_mint([(99, b'\x00')]))[0], ['UNKNOWN_99'])

    def test_zero_padding_after_the_last_extension_is_not_an_extension(self):
        data = token2022_mint([(18, bytes(64))]) + bytes(12)
        self.assertEqual(F.mint_extensions(data)[0], ['MetadataPointer'])

    def test_malformed_bytes_raise(self):
        for bad in (bytes(100), token2022_mint([(1, transfer_fee_body(1))])[:-5], bytes(166)):
            with self.assertRaises(ValueError):
                F.mint_extensions(bad)

    def test_row_records_extensions_and_fee_from_the_retained_mint_bytes(self):
        raw = (('accounts_mint_pool', mint_raw(token2022_mint([(1, transfer_fee_body(300))]))),)
        f = F.build_row(cand(), screen(raw=raw), entered=False)['fields']
        self.assertEqual((f['token2022_extensions'], f['transfer_fee_bps']), (['TransferFeeConfig'], 300))

    def test_row_without_fee_extension_says_so(self):
        row = F.build_row(cand(), screen(raw=(('accounts_mint_pool', mint_raw(token2022_mint([(18, bytes(64))]))),)), entered=False)
        self.assertEqual(row['missing']['transfer_fee_bps'], 'NO_TRANSFER_FEE_EXTENSION')

    def test_garbage_retained_bytes_do_not_raise(self):
        for raw in ((('accounts_mint_pool', b'not json'),), (('accounts_mint_pool', mint_raw(bytes(100))),), ()):
            row = F.build_row(cand(), screen(raw=raw), entered=False)
            self.assertIn('token2022_extensions', row['missing'])


class TestEnrich(Base):
    def test_three_calls_and_known_answers(self):
        h = ScriptedHelius(creation=T0 - 700, extra_sigs=[(30, None), (500, None), (601, None), (200, {'InstructionError': 1})], dev_amount=2 * 10 ** 13)
        fields, missing, calls = F.enrich(cand(), h, str(10 ** 15))
        self.assertEqual(calls, 3)
        self.assertEqual([m for m, _ in h.calls], ['getSignaturesForAddress', 'getTransaction', 'getTokenAccountsByOwner'])
        self.assertEqual(fields['seconds_creation_to_graduation'], 700.0)
        self.assertEqual(fields['tx_count_first_10m'], 3)                 # create + 30 s + 500 s; the 601 s one and the failed one do not count
        self.assertEqual((fields['creator_wallet'], fields['dev_holding_pct']), (h.payer, '2.0000'))
        self.assertEqual(missing, {})

    def test_truncated_history_stops_after_one_call(self):
        h = ScriptedHelius()
        h.sigs = [{'signature': 's%d' % i, 'slot': i, 'blockTime': 1, 'err': None} for i in range(F.SIGNATURE_LIMIT)]
        fields, missing, calls = F.enrich(cand(), h, str(10 ** 15))
        self.assertEqual((calls, fields), (1, {}))
        self.assertEqual(set(missing.values()), {'HISTORY_TRUNCATED'})

    def test_provider_error_on_each_call_becomes_reasons(self):
        for method, gone in (('getSignaturesForAddress', {'creator_wallet', 'dev_holding_pct', 'seconds_creation_to_graduation', 'tx_count_first_10m'}),
                             ('getTransaction', {'creator_wallet', 'dev_holding_pct'}), ('getTokenAccountsByOwner', {'dev_holding_pct'})):
            h = ScriptedHelius()
            h.fail[method] = ProviderError('HTTP_429', True)
            fields, missing, calls = F.enrich(cand(), h, str(10 ** 15))
            self.assertEqual(set(missing), gone, method)
            self.assertTrue(all(v == 'PROVIDER_HTTP_429' for v in missing.values()))

    def test_never_more_than_three_calls(self):
        h = ScriptedHelius()
        F.enrich(cand(), h, str(10 ** 15))
        self.assertLessEqual(len(h.calls), F.MAX_EXTRA_CALLS)

    def test_dev_balance_above_supply_is_unreadable_not_a_number(self):
        h = ScriptedHelius(dev_amount=10 ** 16)
        fields, missing, _ = F.enrich(cand(), h, str(10 ** 15))
        self.assertNotIn('dev_holding_pct', fields)
        self.assertEqual(missing['dev_holding_pct'], 'DEV_BALANCE_UNREADABLE')

    def test_dev_balance_one_above_supply_is_unreadable(self):
        fields, missing, _ = F.enrich(cand(), ScriptedHelius(dev_amount=10 ** 15 + 1), str(10 ** 15))
        self.assertEqual(missing['dev_holding_pct'], 'DEV_BALANCE_UNREADABLE')
        fields, missing, _ = F.enrich(cand(), ScriptedHelius(dev_amount=10 ** 15), str(10 ** 15))
        self.assertEqual(fields['dev_holding_pct'], '100.0000')

    def test_malformed_answers_are_reasons(self):
        h = ScriptedHelius()
        h.rpc = lambda method, params: ([], b'', {}) if method == 'getSignaturesForAddress' else (None, b'', {})
        self.assertEqual(F.enrich(cand(), h, '1')[1]['creator_wallet'], 'SIGNATURES_UNAVAILABLE')
        h2 = ScriptedHelius()
        orig = h2.rpc
        h2.rpc = lambda method, params: ({'transaction': {}}, b'', {}) if method == 'getTransaction' else orig(method, params)
        self.assertEqual(F.enrich(cand(), h2, str(10 ** 15))[1]['creator_wallet'], 'CREATION_TX_UNREADABLE')

    def test_graduation_before_creation_is_unknown(self):
        fields, missing, _ = F.enrich(cand(migrated_at=T0 - 5000), ScriptedHelius(), str(10 ** 15))
        self.assertEqual(missing['seconds_creation_to_graduation'], 'GRADUATION_TIME_UNKNOWN')


class TestRecorder(Base):
    def process_all(self):
        while self.rec.process_one():
            pass

    def test_one_row_with_enrichment_versions_and_candidate_id(self):
        self.assertTrue(self.rec.submit(cand(), screen(), entered=True, candidate_id=7))
        self.assertEqual(self.rows(), [])                                 # nothing synchronous: the trader is not delayed
        self.process_all()
        [row] = self.rows()
        self.assertEqual((row['entered'], row['fields']['creator_wallet'], row['fields']['dev_holding_pct']), (True, self.helius.payer, '1.0000'))
        db = self.store.rows('observations', kind='features')[0]
        self.assertEqual((db['candidate_id'], db['code_version'], db['strategy_version'], db['mint']), (7, CODE, STRATEGY, cand().mint))
        self.assertEqual(json.loads(db['meta'])['features_version'], 1)

    def test_not_entered_reason_is_kept(self):
        self.rec.submit(cand(), screen(), entered=False, reason='COST_BUDGET', candidate_id=1)
        self.process_all()
        self.assertEqual(self.rows()[0]['not_entered_reason'], 'COST_BUDGET')

    def test_duplicate_submit_gives_one_row(self):
        self.assertTrue(self.rec.submit(cand(), screen(), entered=False, candidate_id=1))
        self.assertFalse(self.rec.submit(cand(), screen(), entered=False, candidate_id=1))
        self.process_all()
        self.assertEqual(len(self.rows()), 1)

    def test_low_priority_bucket_sheds_but_the_cheap_row_is_still_written(self):
        for n in range(1, 4):
            self.rec.submit(cand(n), screen(), entered=False, candidate_id=n)
        self.process_all()
        rows = self.rows()
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(1 for r in rows if r['fields']['creator_wallet']), 1)       # burst 3 = one candidate of 3 calls
        shed = [r for r in rows if not r['fields']['creator_wallet']]
        self.assertEqual({r['missing']['creator_wallet'] for r in shed}, {'LOW_PRIORITY_SHED'})
        self.assertEqual(len(self.helius.calls), 3)
        self.assertEqual(self.rec.health()['shed'], 2)
        self.assertTrue(all(r['fields']['market_cap_usd'] == '90000' for r in rows))

    def test_bucket_refills_with_time(self):
        b = F.LowPriorityBucket(1.0, 3, clock=self.bclock)
        self.assertTrue(b.take(3))
        self.assertFalse(b.take(3))
        self.bclock.t += 2
        self.assertFalse(b.take(3))
        self.bclock.t += 1
        self.assertTrue(b.take(3))

    def test_hazard_reject_gets_its_cheap_row_and_no_spend(self):
        self.rec.submit(cand(), screen(False, ['ACTIVE_MINT_AUTHORITY'], {'age_seconds': 400.0, 'supply_raw': '1'}, raw=()), entered=False)
        self.process_all()
        self.assertEqual(self.helius.calls, [])
        self.assertEqual(self.rows()[0]['missing']['creator_wallet'], 'NOT_ENRICHED_HAZARD_REJECT')

    def test_market_stage_reject_is_enriched(self):
        self.rec.submit(cand(), screen(False, ['LIQUIDITY_BELOW_MIN']), entered=False)
        self.process_all()
        self.assertEqual(len(self.helius.calls), 3)

    def test_provider_failure_never_loses_the_row(self):
        self.helius.fail['getSignaturesForAddress'] = ProviderError('HTTP_503', True)
        self.rec.submit(cand(), screen(), entered=False)
        self.process_all()
        self.assertEqual(self.rows()[0]['missing']['creator_wallet'], 'PROVIDER_HTTP_503')

    def test_queue_full_writes_the_cheap_row_at_once(self):
        self.rec = self.make(queue_size=1)
        self.rec.submit(cand(1), screen(), entered=False, candidate_id=1)
        self.rec.submit(cand(2), screen(), entered=False, candidate_id=2)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['missing']['creator_wallet'], 'QUEUE_FULL')
        self.assertEqual(self.rec.health()['queue_full'], 1)

    def test_enrichment_disabled_writes_at_submit(self):
        self.rec = self.make(enrich_enabled=False)
        self.rec.submit(cand(), screen(), entered=False)
        self.assertEqual(self.rows()[0]['missing']['creator_wallet'], 'ENRICHMENT_DISABLED')
        self.assertEqual(self.helius.calls, [])

    def test_submit_never_raises(self):
        for c, s in ((None, None), (cand(), None), (object(), screen())):
            self.rec.submit(c, s, entered=False)
        self.store.add_observation = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk'))
        self.rec = self.make(enrich_enabled=False)
        self.assertFalse(self.rec.submit(cand(5), screen(), entered=False))
        self.assertEqual(self.rec.health()['dropped'], 1)

    def test_store_failure_in_the_worker_is_counted_not_raised(self):
        self.rec.submit(cand(), screen(), entered=False)
        self.store.add_observation = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('disk'))
        self.assertTrue(self.rec.process_one())
        self.assertGreaterEqual(self.rec.health()['errors'], 1)

    def test_rows_are_append_only(self):
        self.rec.submit(cand(), screen(), entered=False)
        self.process_all()
        for sql in ("UPDATE observations SET kind='x'", 'DELETE FROM observations'):
            with self.assertRaises(Exception):
                self.store.db.execute(sql)

    def test_run_drains_the_queue_on_stop(self):
        for n in range(1, 4):
            self.rec.submit(cand(n), screen(), entered=False, candidate_id=n)
        stop = threading.Event()
        stop.set()
        self.rec.run(stop, tick=0.01)
        self.assertEqual(len(self.rows()), 3)


class TestFeatureOutcomeTable(unittest.TestCase):
    def rows(self, n, value_of, entered=True):
        return [{'mint': 'm%d' % i, 'entered': entered, 'fields': {'top10_pct_excl_pool': str(value_of(i)), 'sol_usd': '150', 'creator_wallet': 'x',
                                                                   'holder_count': None}} for i in range(n)]

    def test_known_answer_buckets(self):
        rows = self.rows(40, lambda i: i)
        realized = {'m%d' % i: (1000 if i >= 20 else -500) for i in range(40)}            # high top10 wins
        t = F.feature_outcome_table(rows, realized, buckets=4, min_total=30, min_bucket=10)['top10_pct_excl_pool']
        self.assertEqual((t['n'], t['insufficient']), (40, False))
        self.assertEqual([(b['n'], b['win_rate'], b['mean_pnl_lamports']) for b in t['buckets']],
                         [(10, 0.0, -500), (10, 0.0, -500), (10, 1.0, 1000), (10, 1.0, 1000)])
        self.assertEqual((t['buckets'][0]['lo'], t['buckets'][0]['hi'], t['buckets'][3]['hi']), ('0', '9', '39'))

    def test_small_samples_are_flagged(self):
        rows = self.rows(12, lambda i: i)
        t = F.feature_outcome_table(rows, {r['mint']: 1 for r in rows})['top10_pct_excl_pool']
        self.assertTrue(t['insufficient'])
        self.assertTrue(all(b['insufficient'] for b in t['buckets']))

    def test_only_entered_and_closed_count_and_non_numeric_or_null_fields_are_skipped(self):
        rows = self.rows(40, lambda i: i) + self.rows(5, lambda i: 99, entered=False)
        realized = {'m%d' % i: 1 for i in range(30)}                                  # 10 entered positions still open
        table = F.feature_outcome_table(rows, realized)
        self.assertEqual(table['top10_pct_excl_pool']['n'], 30)
        self.assertNotIn('creator_wallet', table)
        self.assertNotIn('holder_count', table)
        self.assertNotIn('sol_usd', table)

    def test_empty_input(self):
        self.assertEqual(F.feature_outcome_table([], {}), {})

    def test_outcomes_and_rows_from_a_real_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        from lean import paper
        store = Store(Path(tmp.name) / 's.sqlite', initial_cash_sol='5', code_version=CODE, strategy_version=STRATEGY, clock=Clock(T0))
        cfg = paper.PaperConfig()
        mint = pk(300)
        fill = paper.buy(paper.Quote(mint, 'buy', 20_000_000, 1_000_000, 6, T0), '0.02', cfg)
        store.add_fill(fill)
        self.assertEqual(F.outcomes(store), {})                                           # still open
        pos = store.positions()[mint]
        store.add_fill(paper.sell(pos, paper.Quote(mint, 'sell', pos.qty_raw // 2, 10_000_000, 6, T0 + 1), '0.5', cfg))
        self.assertEqual(F.outcomes(store), {})                                           # half sold: not closed, no outcome yet
        pos = store.positions()[mint]
        store.add_fill(paper.sell(pos, paper.Quote(mint, 'sell', pos.qty_raw, 30_000_000, 6, T0 + 1), 1, cfg))
        self.assertEqual(F.outcomes(store), {mint: store.realized()})
        rec = F.FeatureRecorder(store=store, helius=None, code_version=CODE, strategy_version=STRATEGY, clock=Clock(T0))
        rec.submit(cand(), screen(), entered=True)
        self.assertEqual(len(F.feature_rows(store)), 1)


if __name__ == '__main__':
    unittest.main()
