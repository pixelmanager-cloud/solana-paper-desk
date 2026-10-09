"""SYNTHETIC_TEST_ONLY integrated raw collector/window/USD; no providers.

The helper uses existing actual persisted JobPersistence/HistoryProgress fixture
admissions; protocol/trade bytes follow pinned layouts. Graduation/fee values are
explicit synthetic coordinator inputs, not a public/live acceptance claim.
"""
import copy
import base64
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from desk.model import (PAPER_EXPERIMENTAL, validate_event, OBSERVABLE_SIGNAL_PROFILE,
                        EXPERIMENTAL_HISTORY_FIELDS, digest)
from desk.paper_market_adapter import MarketContext, build_market_event
from desk.sol_usd_observation import JupiterPriceResponse, TrustedSlotBounds, PRICE_URL, SOL_MINT
from desk.strategy import experimental_scores, experimental_gates
from desk.programs import unbase58
from desk.security import base58
from tests import test_paper_observation_collector as collector_fixture
from tests.test_live_strategy_features import transaction, history_pages, POOL as EVENT_POOL, PROGRAM


class PaperMarketAdapterTests(unittest.TestCase):
    def setUp(self):
        self.fixture=collector_fixture.PaperObservationCollectorTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.target=self.fixture.target()
        self.observation=self.fixture.collect(candidates=(self.target,)).observations[0]
        self.now=self.fixture.at
        self.context=MarketContext(self.now,self.target,'fixture-protocol-rpc','fixture-protocol-quote',
                                   'SYNTHETIC_TEST_ONLY',self.now-600,None,(),'25')
        self.rows=[self.trade('buy',10,200),self.trade('sell',11,100)]
        self.pages=self.history(self.rows)
        raw=json.dumps({SOL_MINT:{'usdPrice':100,'blockId':100,'decimals':9}}).encode()
        self.usd=JupiterPriceResponse('GET',PRICE_URL,raw,self.now)
        self.bounds=TrustedSlotBounds(self.now,self.now,100,100,((100,self.now-1),))
        self.guard=patch('socket.socket',side_effect=AssertionError('no network'));self.guard.start();self.addCleanup(self.guard.stop)

    def trade(self,side,slot,quote):
        row=transaction(side+str(slot),slot,self.now-2+int(side=='sell'),side=side,quote=quote)
        ix=row['transaction']['message']['instructions'][0]
        raw=unbase58(ix['data'])
        # Raw event contains the pool public key; bind to this canonical fixture.
        raw=raw.replace(unbase58(EVENT_POOL),unbase58(self.target.pool))
        ix['data']=base58(raw)
        return row

    def history(self,rows):
        pages=history_pages(rows,self.now)
        for page in pages:page['request']['params'][0]=self.target.pool
        return pages

    def build(self,**changes):
        args=dict(context=self.context,load_evidence=self.fixture.progress.store.load,
                  raw_trades=self.rows,history_pages=self.pages,usd_response=self.usd,
                  usd_bounds=self.bounds,strategy_profile=OBSERVABLE_SIGNAL_PROFILE)
        args.update(changes)
        return build_market_event(self.observation,**args)

    def test_actual_collector_replay_produces_versioned_event_no_proof_defaults(self):
        before=copy.deepcopy(self.rows)
        out=self.build();self.assertEqual(out['blockers'],[])
        e=out['event'];self.assertIsNotNone(e)
        validate_event(e,mode=PAPER_EXPERIMENTAL,policy_version=3)
        self.assertEqual(e['taker'],self.target.taker)
        self.assertIsNone(e['holder_at'])
        self.assertEqual(e['paper_signal_profile']['holder_freshness'],'UNKNOWN_OMITTED')
        for field in (*EXPERIMENTAL_HISTORY_FIELDS,'flow','wash_score','manip_flow'):
            self.assertIsNone(e[field])
        self.assertEqual(e['price_at'],self.now-1)
        self.assertEqual(e['flow_at'],self.now-1)
        self.assertEqual(e['graduated_at'],self.now-600)
        self.assertFalse(e['flow_confirmed'])
        self.assertFalse(out['entry_authorized']);self.assertFalse(out['execution_verified'])
        self.assertIn('EXECUTION_UNVERIFIED',e['risk_flags'])
        self.assertEqual(e['paper_quote']['input_raw'],self.target.amount_raw)
        self.assertEqual(e['paper_source_evidence']['collector_refs'],sorted(self.observation.evidence_refs))
        self.assertEqual(e['sol_usd'],'100')
        self.assertGreater(Decimal(e['market_cap_usd']),0)
        self.assertEqual(self.rows,before)
        self.assertEqual(self.build()['event']['event_id'],e['event_id'])
        self.assertEqual(self.fixture.progress.admission(self.target.scan_id)['requests_used'],4)

    def test_profile_scoring_persists_definitions_and_known_adverse_signal_rejects(self):
        e=self.build()['event']
        result=experimental_scores(e,mode=PAPER_EXPERIMENTAL,policy_version=3)
        self.assertEqual(result['score_version'],'paper-observable-flow-momentum-v3')
        self.assertEqual(result['signal_profile'],e['paper_signal_profile'])
        self.assertIsNone(result['safety'])
        self.assertFalse(result['entry_authorized'])
        self.assertIn('EXECUTION_UNVERIFIED',result['risk_flags'])
        cfg=json.loads((Path(__file__).resolve().parents[1]/'config/paper.json').read_text())
        reasons=experimental_gates(e,cfg,mode=PAPER_EXPERIMENTAL,policy_version=3)
        self.assertIn('MOMENTUM_OR_OBSERVED_CHURN',reasons) # measured churn .666..., not proof
        self.assertNotIn('STALE_HOLDER',reasons)
        self.assertIsNone(e['wash_score'])

    def test_strict_v1_v2_cannot_accept_new_profile_or_implicit_opt_in(self):
        e=self.build()['event']
        for kwargs in ({},{'mode':PAPER_EXPERIMENTAL,'policy_version':1},
                       {'mode':PAPER_EXPERIMENTAL,'policy_version':2}):
            with self.assertRaises(ValueError):validate_event(e,**kwargs)
        for profile in (None,'legacy','observable-flow-churn-concentration-v0'):
            self.assertIsNone(self.build(strategy_profile=profile)['event'])

    def test_missing_actual_window_and_partial_event_data_never_become_zero(self):
        for changes in ({'history_pages':None},{'raw_trades':(), 'history_pages':self.history([])}):
            result=self.build(**changes)
            self.assertIsNone(result['event'])
            self.assertTrue(any('MISSING_WINDOW' in reason for reason in result['blockers']))
            self.assertIsNone(result['draft']['flow'])
        bad=copy.deepcopy(self.rows);bad[1]['transaction']['message']['instructions'][0]['data']=base58(unbase58(bad[1]['transaction']['message']['instructions'][0]['data'])+b'\0')
        self.assertIsNone(self.build(raw_trades=bad,history_pages=self.history(bad))['event'])

    def test_missing_usd_graduation_fee_and_stale_times_have_exact_diagnostics(self):
        cases=({'usd_response':None},'SOL_USD_SOURCE_OR_EXACT_BLOCK_TIME_MISSING'),
        for args,reason in cases:
            result=self.build(**args);self.assertIsNone(result['event']);self.assertIn(reason,result['blockers'])
        for ctx,reason in ((replace(self.context,graduated_at=None),'ORIGINAL_GRADUATED_AT_MISSING_OR_INVALID'),
                           (replace(self.context,pool_fee_bps=None),'EXPLICIT_PAPER_FEE_ASSUMPTION_REQUIRED'),
                           (replace(self.context,now=self.now+11),'COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID')):
            out=self.build(context=ctx);self.assertIsNone(out['event']);self.assertIn(reason,out['blockers'])

    def test_known_hazards_and_mutated_source_target_quote_reject(self):
        self.assertIsNone(self.build(context=replace(self.context,known_hazards=('KNOWN_BUNDLE_EXPOSURE',)))['event'])
        for change in ({'target':replace(self.target,scan_id='other')},
                       {'pool':replace(self.observation.pool,reserve_sol=Decimal('999'))},
                       {'quote':replace(self.observation.quote,input_raw=1)},
                       {'mint':replace(self.observation.mint,supply_raw=1)},
                       {'failure':'SOURCE_REQUEST_FAILED'}, {'evidence_refs':()}):
            old=self.observation;self.observation=replace(old,**change)
            try:self.assertIsNone(self.build()['event'])
            finally:self.observation=old
        store=self.fixture.progress.store
        def corrupt(key):
            r=copy.deepcopy(store.load(key));r['injected']=True;return r
        self.assertIsNone(self.build(load_evidence=corrupt)['event'])
        self.assertIsNone(self.build(context=replace(self.context,rpc_source_id='caller-selected'))['event'])

    def test_atomic_pool_supply_dates_marketcap_even_if_earlier_mint_supply_differs(self):
        # Real collector source shapes; burn between banks is not an invented
        # contradiction or a reason to price with obsolete circulating supply.
        original=self.fixture.sources[self.target.scan_id]
        def rpc(method,params,*,timeout_seconds):
            result=original.rpc(method,params,timeout_seconds=timeout_seconds)
            if method=='getAccountInfo' and params[0]==self.target.mint:
                raw=bytearray(base64.b64decode(result['value']['data'][0]))
                raw[36:44]=(2*10**15).to_bytes(8,'little')
                result['value']['data'][0]=base64.b64encode(raw).decode()
            return result
        self.fixture.sources[self.target.scan_id]=replace(original,rpc=rpc)
        self.observation=self.fixture.collect(candidates=(self.target,)).observations[0]
        e=self.build()['event'];self.assertIsNotNone(e)
        self.assertEqual(self.observation.mint.supply_raw,2*10**15)
        self.assertEqual(e['paper_source_evidence']['valuation_supply_raw'],str(10**15))
        self.assertEqual(e['paper_source_evidence']['valuation_supply_slot'],self.observation.pool.slot)
        self.assertEqual(Decimal(e['market_cap_usd']),Decimal('100000000'))

    def test_combined_price_time_keeps_older_reserve_observation(self):
        original=self.fixture.sources[self.target.scan_id]
        def quote(*args,**kwargs):
            self.fixture.at+=2
            return original.quote(*args,**kwargs)
        self.fixture.sources[self.target.scan_id]=replace(original,quote=quote)
        self.observation=self.fixture.collect(candidates=(self.target,)).observations[0]
        now=self.now+2
        ctx=replace(self.context,now=now)
        bounds=TrustedSlotBounds(now,now,100,100,((100,now-1),))
        pages=history_pages(self.rows,now)
        for page in pages:page['request']['params'][0]=self.target.pool
        result=self.build(context=ctx,usd_response=replace(self.usd,acquired_at=now),
                          usd_bounds=bounds,history_pages=pages)
        self.assertEqual(result['blockers'],[])
        self.assertEqual(result['event']['price_at'],self.now)
        self.assertEqual(result['event']['paper_source_evidence']['pool_at'],self.now)
        self.assertEqual(result['event']['paper_source_evidence']['quote_at'],now)

    def test_original_window_survives_later_quote_and_decision_without_relabel(self):
        baseline=self.build()['event']
        original_pages=copy.deepcopy(self.pages);original_rows=copy.deepcopy(self.rows)
        original=self.fixture.sources[self.target.scan_id]
        def quote(*args,**kwargs):
            self.fixture.at+=1
            return original.quote(*args,**kwargs)
        self.fixture.sources[self.target.scan_id]=replace(original,quote=quote)
        self.observation=self.fixture.collect(candidates=(self.target,)).observations[0]
        now=self.now+1
        out=self.build(context=replace(self.context,now=now,history_as_of=self.now),
                       usd_bounds=replace(self.bounds,now=now))
        self.assertEqual(out['blockers'],[])
        e=out['event'];self.assertEqual(e['ts'],now)
        p=e['paper_signal_profile'];prior=baseline['paper_signal_profile']
        for key in ('measurements','window','feature_manifest_hash','source_hashes','history_hashes'):
            self.assertEqual(p[key],prior[key])
        self.assertEqual(p['window']['end_inclusive'],self.now)
        self.assertEqual(p['window_identity'],{'captured_as_of':self.now,'decision_at':now,
            'gap_seconds':1,'scope':'CAPTURED_WINDOW_NOT_CONTINUOUS_COVERAGE'})
        self.assertEqual(e['paper_source_evidence']['quote_at'],now)
        self.assertEqual(e['paper_source_evidence']['pool_at'],self.now)
        self.assertEqual(e['price_at'],self.now-1)
        self.assertEqual(e['flow_at'],baseline['flow_at'])
        self.assertEqual(e['graduated_at'],baseline['graduated_at'])
        self.assertEqual(self.pages,original_pages);self.assertEqual(self.rows,original_rows)
        scored=experimental_scores(e,mode=PAPER_EXPERIMENTAL,policy_version=3)
        self.assertEqual(scored['signal_profile'],p)
        self.assertEqual(self.fixture.progress.admission(self.target.scan_id)['requests_used'],8)
        from desk import engine,quote_execution as qe
        cfg=json.loads((Path(__file__).resolve().parents[1]/'config/paper.json').read_text())
        cfg.update(experimental_policy_version=3,paper_signal_policy_version=3,paper_quote_execution_version=1)
        state=engine.initial_state(cfg);quotes=(self.observation.quote,)
        planned=qe.plan(state,e,cfg,quotes=quotes)
        _,outcomes=qe.bind_transition(e,quotes)(copy.deepcopy(state),e,cfg)
        self.assertEqual(outcomes,planned['outcomes'])

    def test_old_window_does_not_bypass_actual_component_or_strategy_freshness(self):
        # Keep current account/quote/USD evidence but retain the older actual
        # query/trade timestamps; nothing is shifted to decision time.
        self.context=replace(self.context,history_as_of=self.now-29)
        rows=[self.trade('buy',10,200)]
        rows[0]['blockTime']=self.now-30
        ix=rows[0]['transaction']['message']['instructions'][0]
        raw=bytearray(unbase58(ix['data']));raw[16:24]=(self.now-30).to_bytes(8,'little',signed=True)
        ix['data']=base58(raw)
        pages=history_pages(rows,self.now-29)
        for page in pages:page['request']['params'][0]=self.target.pool
        result=self.build(raw_trades=rows,history_pages=pages)
        self.assertEqual(result['blockers'],[])
        e=result['event'];self.assertEqual(e['momentum_at'],self.now-30)
        cfg=json.loads((Path(__file__).resolve().parents[1]/'config/paper.json').read_text())
        self.assertIn('STALE_MOMENTUM',experimental_gates(e,cfg,mode=PAPER_EXPERIMENTAL,policy_version=3))
        stale=copy.deepcopy(rows);stale[0]['blockTime']=self.now-31
        ix=stale[0]['transaction']['message']['instructions'][0]
        raw=bytearray(unbase58(ix['data']));raw[16:24]=(self.now-31).to_bytes(8,'little',signed=True);ix['data']=base58(raw)
        pages=history_pages(stale,self.now-29)
        for page in pages:page['request']['params'][0]=self.target.pool
        bad=self.build(raw_trades=stale,history_pages=pages)
        self.assertIsNone(bad['event'])
        self.assertTrue(any(x.startswith('STALE_WINDOW_MEASUREMENT:') for x in bad['blockers']))
        for history_end in (self.now+1,self.now-31,True):
            out=self.build(context=replace(self.context,history_as_of=history_end))
            self.assertIsNone(out['event'])
            self.assertIn('CAPTURED_HISTORY_WINDOW_STALE_OR_FUTURE',out['blockers'])

    def test_captured_window_identity_and_measurement_end_cannot_be_forged(self):
        e=self.build(context=replace(self.context,now=self.now+1,history_as_of=self.now),
                     usd_bounds=replace(self.bounds,now=self.now+1))['event']
        for attack in ('scope','gap','start','end','future-measurement','missing-identity'):
            bad=copy.deepcopy(e);p=bad['paper_signal_profile']
            if attack=='scope':p['window_identity']['scope']='CURRENT_CONTINUOUS_CERTIFICATION'
            if attack=='gap':p['window_identity']['gap_seconds']=0
            if attack=='start':p['window']['start_inclusive']+=1
            if attack=='end':p['window']['end_inclusive']+=1
            if attack=='future-measurement':p['measurements']['same_wallet_churn_proxy_v1']['observed_at']=e['ts']
            if attack=='missing-identity':p.pop('window_identity')
            with self.subTest(attack=attack),self.assertRaises(ValueError):
                validate_event(bad,mode=PAPER_EXPERIMENTAL,policy_version=3)
        # Previously recorded same-time profile remains valid, no migration.
        old=self.build()['event'];old['paper_signal_profile'].pop('window_identity')
        validate_event(old,mode=PAPER_EXPERIMENTAL,policy_version=3)

    def test_actual_quote_execution_plans_and_binds_trusted_builder_taker(self):
        from desk import engine, quote_execution as qe
        # Actual accepted55 + exact PR127 execution, with our producer/profile.
        # No fake event fixture/decoder/engine override. Existing gates may reject
        # this deliberately adverse fixture; quote replay must work regardless.
        e=self.build()['event']
        cfg=json.loads((Path(__file__).resolve().parents[1]/'config/paper.json').read_text())
        cfg.update(experimental_policy_version=3,paper_signal_policy_version=3,
                   paper_quote_execution_version=1)
        state=engine.initial_state(cfg);before=copy.deepcopy(state)
        quotes=(self.observation.quote,)
        plan=qe.plan(state,e,cfg,quotes=quotes)
        self.assertEqual(plan['event_hash'],digest(e))
        self.assertEqual(state,before)
        transitioned,outcomes=qe.bind_transition(e,quotes)(copy.deepcopy(state),e,cfg)
        self.assertEqual(outcomes,plan['outcomes'])
        self.assertEqual(transitioned['positions'],{})
        self.assertTrue(any(x['type']=='reject' for x in outcomes))
        self.assertEqual(e['taker'],self.target.taker)
        for taker in (None,'missing',SOL_MINT):
            forged=copy.deepcopy(e);forged['taker']=taker
            with self.subTest(taker=taker),self.assertRaises(ValueError):
                qe.plan(state,forged,cfg,quotes=quotes)
            with self.assertRaises(ValueError):
                qe.bind_transition(e,quotes)(copy.deepcopy(state),forged,cfg)
        wrong_target=replace(self.target,taker=SOL_MINT)
        old=self.observation;self.observation=replace(old,target=wrong_target)
        try:
            self.assertIsNone(self.build(context=replace(self.context,target=wrong_target))['event'])
        finally:self.observation=old

    def test_read_failure_is_static_and_adapter_never_reserves_or_writes(self):
        def failed_read(key):raise RuntimeError('DO_NOT_LEAK fixture secret')
        out=self.build(load_evidence=failed_read)
        self.assertIsNone(out['event'])
        self.assertNotIn('DO_NOT_LEAK',str(out))
        with (patch.object(self.fixture.progress,'reserve',side_effect=AssertionError('no charges')),
              patch.object(self.fixture.progress.store,'save',side_effect=AssertionError('no writes'))):
            self.assertIsNotNone(self.build()['event'])
        self.assertIsNone(self.build(raw_trades=iter(self.rows))['event'])

    def test_profile_missing_changed_measurements_formulas_legacy_values_and_times_reject(self):
        e=self.build()['event']
        for attack in ('coverage','formula','measurement','legacy','confirmed','holder','time','missing-window'):
            bad=copy.deepcopy(e);p=bad['paper_signal_profile']
            if attack=='missing-window':p['window']=None
            if attack=='coverage':p['window']['coverage_complete']=False
            if attack=='formula':p['formulas']['same_wallet_churn_proxy_v1']='0'
            if attack=='measurement':p['measurements']['net_buy_ratio']['value']='1'
            if attack=='legacy':bad['wash_score']='0'
            if attack=='confirmed':bad['flow_confirmed']=True
            if attack=='holder':p['holder_freshness']='OBSERVED'
            if attack=='time':bad['flow_at']=self.now
            with self.subTest(attack=attack),self.assertRaises(ValueError):
                validate_event(bad,mode=PAPER_EXPERIMENTAL,policy_version=3)
        actual=copy.deepcopy(e);actual['holder_at']=self.now-121;actual['paper_signal_profile']['holder_freshness']='OBSERVED'
        cfg=json.loads((Path(__file__).resolve().parents[1]/'config/paper.json').read_text())
        self.assertIn('STALE_HOLDER',experimental_gates(actual,cfg,mode=PAPER_EXPERIMENTAL,policy_version=3))

if __name__=='__main__':unittest.main()
