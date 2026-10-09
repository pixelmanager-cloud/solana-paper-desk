"""Synthetic raw events and rewrapped public event bytes; no provider calls.

Minimal live-input contract (report kept here to honor module/tests-only scope):
- successful timestamped full transactions, signature/slot, event instruction
  bytes + inner instruction paths; pinned complete PumpSwap event schema;
- canonical pool/base mint/WSOL binding, same-bank vault reserves, mint decimals,
  supply and original chain observation times; no receive-time substitution;
- bounded five-minute history with retained page hashes/cursors/gaps; sparse
  stream/discovery sampling MUST remain explicitly incomplete;
- separately timestamped SOL/USD, creator identity + 7-day history, wallet ages,
  reviewed flow/wash/manipulation measurements; absent inputs remain UNKNOWN.
Existing read paths: history.collect_history/getTransactionsForAddress and
providers.backfill persist full histories, providers.record_stream persists
notifications (confirmed/partial, not complete); pools.verify_pool reads pool,
vaults/LP/mint/global/dynamic fees via getAccountInfo/getMultipleAccounts;
inspect_mint supplies mint bytes/supply; launch_anchor supplies creation anchors;
ordering.collect_ordering/getBlock supplies actual same-slot signature ordering.
Jupiter probe supplies route quote/cost diagnostics, not SOL/USD or executed flow.
No existing provider path supplies a reviewed SOL/USD oracle, creator seven-day
coverage or wallet-age certification. This module adds deterministic v1 flow,
wallet-churn and buyer-concentration PROXIES under exact exhausted pool queries;
these are observable heuristics, never authoritative wash/manipulation facts. Coordinator
must wire missing sources/definitions before building a mandatory complete event. Proposed explicit versioned paper mapping (coordinator
owns integration): flow <- directional_flow_proxy_v1; experimental wash penalty
<- same_wallet_churn_proxy_v1; experimental manip_flow penalty <-
buyer_volume_concentration_proxy_v1. Preserve proxy_version/coverage/risk labels;
never silently reinterpret UNKNOWN legacy fields or claim common ownership.
This calculator gives observed sample values, not completeness or approval.
"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from desk.decode import decode
from desk.live_strategy_features import calculate,MAX_RECORDS,MAX_RECORD_BYTES
from desk.model import digest
from desk.programs import unbase58
from desk.security import base58

ROOT=Path(__file__).resolve().parents[1]
PUBLIC=json.loads((ROOT/'fixtures/mainnet-sell-event.json').read_text())
POOL=PUBLIC['decoded']['fields']['pool']
PROGRAM=PUBLIC['instruction']['programId']
T=PUBLIC['decoded']['fields']['timestamp']
SCHEMA=json.loads((ROOT/'desk/schemas/pump_amm_sdk_2_1_0.json').read_text())


def transaction(signature='synthetic-signature',slot=10,ts=T,side='buy',quote=200,base=100,wallet=None):
    name='BuyEvent' if side=='buy' else 'SellEvent'
    fields=next(t['type']['fields'] for t in SCHEMA['types'] if t['name']==name)
    values=dict(PUBLIC['decoded']['fields'])
    values.update(timestamp=ts,pool_quote_token_reserves=1000,virtual_quote_reserves=0,can_boost=False,
                  quote_amount_in=quote,quote_amount_out=quote,base_amount_in=base,base_amount_out=base)
    if wallet:values['user']=wallet
    raw=bytes.fromhex('e445a52e51cb9a1d')+bytes(next(e['discriminator'] for e in SCHEMA['events'] if e['name']==name))
    for field in fields:
        t=field['type'];v=values.get(field['name'],False if t=='bool' else 0)
        if t=='string':raw+=(0).to_bytes(4,'little')
        else:raw+=unbase58(v) if t=='pubkey' else bytes([v]) if t=='bool' else v.to_bytes(int(t[1:])//8,'little',signed=t[0]=='i')
    ix={'programId':PROGRAM,'accounts':PUBLIC['instruction']['accounts'],'data':base58(raw)}
    return {'slot':slot,'blockTime':ts,'transaction':{'signatures':[signature],
            'message':{'accountKeys':[],'instructions':[ix]}},
            'meta':{'err':None,'preTokenBalances':[],'postTokenBalances':[],'innerInstructions':[]}}


def history_pages(rows,now=T+2,*,split=None):
    chunks=[rows] if split is None else [rows[:split],rows[split:]]
    pages=[]
    for i,chunk in enumerate(chunks):
        opts={'transactionDetails':'full','sortOrder':'asc','limit':100,'commitment':'finalized',
              'encoding':'jsonParsed','maxSupportedTransactionVersion':1,
              'filters':{'blockTime':{'gte':max(0,now-300),'lt':now+1},'status':'any','tokenAccounts':'none'}}
        if i:opts['paginationToken']='next'
        response={'data':chunk,'paginationToken':'next' if i<len(chunks)-1 else None}
        request={'kind':'history_request_v1','method':'getTransactionsForAddress',
                 'params':[POOL,opts],'response_hash':digest(response)}
        pages.append({'request':request,'response':response})
    return pages


class StrategyFeatureTests(unittest.TestCase):
    def setUp(self):
        guard=patch('socket.socket',side_effect=AssertionError('No network'));guard.start();self.addCleanup(guard.stop)

    def result(self,rows,now=T+2,**kwargs):
        return calculate(rows,pool=POOL,as_of=now,provenance='SYNTHETIC_TEST_ONLY',**kwargs)

    def test_measured_sample_ratios_buyers_volume_drawdown_and_original_times(self):
        rows=[transaction(),transaction('second',11,T+1,side='sell',quote=100,base=100)]
        before=copy.deepcopy(rows)
        result=self.result(rows)
        fields=result['fields']
        self.assertTrue(fields['net_buy_ratio']['value'].startswith('0.666666'))
        self.assertEqual(fields['unique_buyers_5m']['value'],1)
        self.assertEqual(fields['volume_vs_liq']['value'],'0.15')
        self.assertEqual(fields['drawdown_from_high']['value'],'0.5')
        self.assertEqual(fields['momentum_at']['observed_at'],T+1)
        self.assertEqual(fields['price_at']['value'],T+1)
        self.assertFalse(result['window']['coverage_complete'])
        self.assertIn('WINDOW_COVERAGE_UNVERIFIED',fields['net_buy_ratio']['blockers'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(rows,before)
        unsigned=dict(result);unsigned.pop('manifest_hash')
        self.assertEqual(result['manifest_hash'],digest(unsigned))

    def test_input_arrival_order_and_exact_duplicate_are_not_chain_order_or_extra_volume(self):
        rows=[transaction(),transaction('second',11,T+1,quote=100)]
        first=self.result(rows);second=self.result(list(reversed(rows)))
        self.assertEqual(first,second)
        duplicate=self.result(rows+[rows[0]])
        self.assertEqual(duplicate['fields'],first['fields'])
        self.assertEqual(duplicate['duplicate_records'],1)
        self.assertEqual(duplicate['trade_count'],2)

    def test_conflicting_raw_identity_withholds_all_measurements(self):
        result=self.result([transaction(),transaction(quote=1)])
        self.assertIn('CONFLICTING_TRANSACTION_IDENTITY',result['blockers'])
        self.assertIsNone(result['fields']['net_buy_ratio']['value'])

    def test_failed_transaction_does_not_count_as_executed_buy(self):
        failed=transaction('failed');failed['meta']['err']={'InstructionError':[0,'Custom']}
        result=self.result([failed,transaction('sell',11,T+1,side='sell')])
        self.assertEqual(result['failed_records'],1)
        self.assertEqual(result['fields']['unique_buyers_5m']['value'],0)
        self.assertEqual(result['fields']['net_buy_ratio']['value'],'0')

    def test_same_slot_signatures_do_not_invent_latest_price_or_reserve_order(self):
        result=self.result([transaction(),transaction('other',10,T,quote=100)])
        self.assertEqual(result['fields']['net_buy_ratio']['status'],'MEASURED_SAMPLE')
        for field in ('drawdown_from_high','volume_vs_liq','price_at'):
            self.assertIsNone(result['fields'][field]['value'])
            self.assertIn('LATEST_SLOT_ORDER_UNRESOLVED',result['fields'][field]['blockers'])

    def test_stale_future_missing_time_and_time_slot_conflict_fail_closed(self):
        cases=([transaction()],T+31),([transaction(ts=T+3)],T+2),([transaction(),transaction('other',11,T-1)],T+2),([transaction(),transaction('other',10,T+1)],T+2)
        for rows,now in cases:
            result=self.result(rows,now)
            self.assertIsNone(result['fields']['net_buy_ratio']['value'])
        row=transaction();row['blockTime']=None
        self.assertIsNone(self.result([row])['fields']['price_at']['value'])
        self.assertEqual(self.result([transaction()],T+30)['fields']['net_buy_ratio']['status'],'MEASURED_SAMPLE')

    def test_raw_event_time_must_match_chain_metadata_and_partial_layout_not_approved(self):
        row=transaction();row['blockTime']=T+1
        self.assertIn('EVENT_CHAIN_TIME_MISMATCH',self.result([row])['blockers'])
        row=transaction();ix=row['transaction']['message']['instructions'][0]
        ix['data']=base58(unbase58(ix['data'])+b'\0')
        self.assertIn('UNSUPPORTED_OR_PARTIAL_EVENT',self.result([row])['blockers'])

    def test_record_byte_and_iterator_bounds_preserve_unknown(self):
        result=self.result(transaction(str(i)) for i in range(MAX_RECORDS+2))
        self.assertEqual(result['records_seen'],MAX_RECORDS+1)
        self.assertIsNone(result['fields']['net_buy_ratio']['value'])
        row=transaction();row['padding']='x'*(MAX_RECORD_BYTES+1)
        self.assertIsNone(self.result([row])['fields']['net_buy_ratio']['value'])

    def test_empty_intent_only_and_other_pool_samples_never_become_complete_or_zero_defaults(self):
        row=transaction();row['transaction']['message']['instructions']=[]
        for rows in ([],[row]):
            result=self.result(rows)
            for name in ('flow','wash_score','sol_usd','dev_launches_7d','net_buy_ratio'):
                self.assertEqual(result['fields'][name]['status'],'UNKNOWN')
                self.assertIsNone(result['fields'][name]['value'])
                self.assertTrue(result['fields'][name]['blockers'])
            self.assertFalse(result['window']['coverage_complete'])
        result=calculate([transaction()],pool='OTHER_POOL',as_of=T+2,provenance='SYNTHETIC_TEST_ONLY')
        self.assertEqual(result['trade_count'],0)

    def test_window_expiry_and_unique_buyers_do_not_use_receive_time(self):
        rows=[transaction(ts=T-301),transaction('fresh',11,T,wallet=POOL),transaction('same-buyer',12,T+1,wallet=POOL)]
        result=self.result(rows,T)
        self.assertIn('FUTURE_TRANSACTION_TIME',result['blockers'])
        result=self.result(rows,T+1)
        self.assertEqual(result['trade_count'],2)
        self.assertEqual(result['fields']['unique_buyers_5m']['value'],1)
        self.assertEqual(result['window']['observed_start'],T)

    def test_public_recorded_event_bytes_replayed_with_explicit_rewrapped_limit(self):
        # Original event instruction bytes; transaction shell is synthetic and
        # makes NO claim this is a captured authenticated full transaction.
        row=transaction(ts=T,side='sell',slot=PUBLIC['slot'],signature=PUBLIC['signature'])
        row['transaction']['message']['instructions']=[PUBLIC['instruction']]
        result=calculate([row],pool=POOL,as_of=T,provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        self.assertEqual(result['trade_count'],1)
        self.assertEqual(result['fields']['net_buy_ratio']['value'],'0')
        self.assertFalse(result['window']['coverage_complete'])
        self.assertFalse(result['eligible_for_trading'])
        self.assertIsNone(result['fields']['volume_vs_liq']['value'])
        self.assertIn('SPECIAL_POOL_LIQUIDITY_PROFILE_UNSUPPORTED',result['fields']['volume_vs_liq']['blockers'])

    def test_missing_pool_profile_fields_do_not_default_to_safe_liquidity(self):
        row=transaction();observation=decode(row)
        observation['program_observations'][0]['fields'].pop('can_boost')
        with patch('desk.live_strategy_features.decode',return_value=observation):
            result=self.result([row])
        self.assertIsNone(result['fields']['volume_vs_liq']['value'])
        self.assertIn('SPECIAL_POOL_LIQUIDITY_PROFILE_UNSUPPORTED',result['fields']['volume_vs_liq']['blockers'])

    def test_complete_query_supports_versioned_flow_churn_and_concentration(self):
        rows=[transaction(quote=200),transaction('sell',11,T+1,side='sell',quote=100),
              transaction('second-buyer',12,T+2,quote=100,wallet=POOL)]
        result=self.result(rows,history_pages=history_pages(rows,split=2))
        self.assertTrue(result['window']['coverage_complete'])
        fields=result['fields']
        self.assertEqual(fields['directional_flow_proxy_v1']['value'],'75')
        self.assertEqual(fields['same_wallet_churn_proxy_v1']['value'],'0.5')
        self.assertTrue(fields['buyer_volume_concentration_proxy_v1']['value'].startswith('0.666666'))
        self.assertEqual(fields['net_buy_ratio']['status'],'MEASURED_WINDOW')
        self.assertEqual(fields['flow']['status'],'UNKNOWN')
        self.assertEqual(fields['wash_score']['status'],'UNKNOWN')
        self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(result['proxy_version'],'observable-flow-churn-concentration-v1')
        self.assertEqual(len(result['history_hashes']),4)

    def test_partial_sample_does_not_supply_proxy_neutral_scores(self):
        result=self.result([transaction()])
        for name in ('directional_flow_proxy_v1','same_wallet_churn_proxy_v1','buyer_volume_concentration_proxy_v1'):
            self.assertIsNone(result['fields'][name]['value'])
            self.assertIn('WINDOW_COVERAGE_UNVERIFIED',result['fields'][name]['blockers'])

    def test_complete_empty_window_actual_zero_counts_and_churn_not_undefined_ratios(self):
        result=self.result([],history_pages=history_pages([]))
        self.assertTrue(result['window']['coverage_complete'])
        self.assertEqual(result['fields']['unique_buyers_5m']['value'],0)
        self.assertEqual(result['fields']['same_wallet_churn_proxy_v1']['value'],'0')
        self.assertIsNone(result['fields']['same_wallet_churn_proxy_v1']['observed_at'])
        for name in ('directional_flow_proxy_v1','buyer_volume_concentration_proxy_v1'):
            self.assertIsNone(result['fields'][name]['value'])
            self.assertIn('NO_VOLUME_DENOMINATOR',result['fields'][name]['blockers'])

    def test_complete_buy_only_zero_churn_is_distinct_from_missing_coverage(self):
        rows=[transaction()]
        result=self.result(rows,history_pages=history_pages(rows))
        self.assertEqual(result['fields']['same_wallet_churn_proxy_v1']['value'],'0')
        self.assertEqual(result['fields']['directional_flow_proxy_v1']['value'],'100')
        self.assertEqual(result['fields']['buyer_volume_concentration_proxy_v1']['value'],'1')
        stale=self.result(rows,T+31,history_pages=history_pages(rows,T+31))
        self.assertIsNone(stale['fields']['directional_flow_proxy_v1']['value'])
        self.assertIn('TRADE_COMPONENT_STALE',stale['fields']['directional_flow_proxy_v1']['blockers'])

    def test_coverage_cannot_be_promoted_by_flags_cursor_gaps_queries_hashes_or_omissions(self):
        rows=[transaction(),transaction('other',11,T+1)]
        for attack in ('flag','cursor','pool','window','scope','hash','omit','order'):
            pages=history_pages(rows,split=1)
            if attack=='flag':pages=[{'complete':True}]
            if attack=='cursor':pages[1]['request']['params'][1]['paginationToken']='gap'
            if attack=='pool':pages[0]['request']['params'][0]='OTHER_POOL'
            if attack=='window':pages[0]['request']['params'][1]['filters']['blockTime']['gte']+=1
            if attack=='scope':pages[0]['request']['params'][1]['filters']['tokenAccounts']='balanceChanged'
            if attack=='hash':pages[0]['request']['response_hash']='a'*64
            if attack=='omit':pages=history_pages(rows[:1])
            if attack=='order':pages=history_pages(list(reversed(rows)))
            with self.subTest(attack=attack):
                result=self.result(rows,history_pages=pages)
                self.assertFalse(result['window']['coverage_complete'])
                self.assertIsNone(result['fields']['directional_flow_proxy_v1']['value'])

    def test_invalid_window_clock_and_provenance_cannot_select_live_approval(self):
        for kwargs in ({'window_seconds':60},{'ttl_seconds':0}):
            with self.assertRaises(ValueError):self.result([],**kwargs)
        for now in (True,-1):
            with self.assertRaises(ValueError):self.result([],now)
        with self.assertRaises(ValueError):calculate([],pool=POOL,as_of=T,provenance='LIVE_APPROVED')


if __name__=='__main__':unittest.main()
