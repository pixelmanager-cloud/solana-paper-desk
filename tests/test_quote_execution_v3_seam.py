"""Explicit V3 missing-history seam; pinned raw event windows, fixture only."""
import copy
import unittest
from unittest.mock import patch

from desk import engine,quote_execution as qe
from desk.live_strategy_features import calculate
from desk.model import (canonical,digest,PAPER_EXPERIMENTAL,EXPERIMENTAL_HISTORY_FIELDS,
                        OBSERVABLE_SIGNAL_PROFILE,OBSERVABLE_FORMULAS,OBSERVABLE_LIMITATIONS,validate_event)
from desk.programs import unbase58
from desk.security import base58
from tests import test_quote_execution as execution_fixture
from tests import test_paper_market_adapter as adapter_fixture
from tests.test_live_strategy_features import transaction,history_pages,POOL
from tests.helpers import T,config,event
from tests.test_paper_experimental_scoring import experimental


class QuoteV3SeamTests(unittest.TestCase):
    def setUp(self):
        self.fixture=execution_fixture.QuoteExecutionTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.cfg={**self.fixture.cfg,'paper_signal_policy_version':3}

    def market(self,at=T):
        e=self.fixture.market(at)
        rows=[]
        for i in range(40):
            row=transaction('v3-window-'+str(i),10+i,at-1,quote=200,base=100,
                            wallet=base58((i+1).to_bytes(32,'big')))
            ix=row['transaction']['message']['instructions'][0]
            ix['data']=base58(unbase58(ix['data']).replace(unbase58(POOL),unbase58(e['pool'])))
            rows.append(row)
        pages=history_pages(rows,at)
        for page in pages:page['request']['params'][0]=e['pool']
        f=calculate(rows,pool=e['pool'],as_of=at,provenance='SYNTHETIC_TEST_ONLY',history_pages=pages)
        self.assertTrue(f['window']['coverage_complete'])
        names=set(OBSERVABLE_FORMULAS)|{'net_buy_ratio','unique_buyers_5m','volume_vs_liq','drawdown_from_high'}
        m={name:copy.deepcopy(f['fields'][name]) for name in names}
        e['paper_signal_profile']={'name':OBSERVABLE_SIGNAL_PROFILE,'version':1,
            'formulas':dict(OBSERVABLE_FORMULAS),'limitations':list(OBSERVABLE_LIMITATIONS),
            'measurements':m,'window':f['window'],'feature_manifest_hash':f['manifest_hash'],
            'source_hashes':f['source_hashes'],'history_hashes':f['history_hashes'],'holder_freshness':'UNKNOWN_OMITTED'}
        for name in ('net_buy_ratio','unique_buyers_5m','volume_vs_liq','drawdown_from_high'):e[name]=m[name]['value']
        e['flow_at']=m['directional_flow_proxy_v1']['observed_at']
        e['momentum_at']=f['fields']['momentum_at']['observed_at']
        e['holder_at']=None;e['flow']=None;e['wash_score']=None;e['manip_flow']=None
        e['paper_experimental']={'mode':PAPER_EXPERIMENTAL,'policy_version':3,
            'risk_flag':'UNRESOLVED_OWNERSHIP_HISTORY','ownership_unknowns':{}}
        for name in EXPERIMENTAL_HISTORY_FIELDS:
            e[name]=None;e['paper_experimental']['ownership_unknowns'][name]={'status':'UNKNOWN','reasons':['SYNTHETIC_HISTORY_UNAVAILABLE']}
        del e['bundle_evidence']
        validate_event(e,mode=PAPER_EXPERIMENTAL,policy_version=3)
        return e

    def test_refresh_cannot_reinterpret_saved_raw_quantity(self):
        f=self.fixture
        f.buy_fill()
        original=f.state()
        for decimals in (5,7):
            with self.subTest(decimals=decimals):
                refreshed=f.quote('sell',qe.raw_quantity(original['positions'][f.mint]['qty'],decimals),
                                  9_500_000,at=T+1,decimals=decimals)
                e=f.market(T+1,danger=True)
                for action in (lambda:qe.plan(original,e,f.cfg,(refreshed,)),
                               lambda:f.apply(e,(refreshed,))):
                    with self.assertRaisesRegex(qe.QuoteExecutionError,'QUOTE_POSITION_DECIMALS_MISMATCH'):
                        action()
                self.assertEqual(f.state(),original)

    def test_actual_v3_config_reaches_quote_demands_without_safe_legacy_metrics(self):
        e=self.market();original=copy.deepcopy(e)
        state=engine.initial_state(self.cfg);before=copy.deepcopy(state)
        planned=qe.plan(state,e,self.cfg)
        self.assertEqual(planned['quote_demands'][0]['direction'],'buy')
        self.assertGreaterEqual(planned['quote_demands'][0]['maximum_input_raw'],self.fixture.buy.input_raw)
        self.assertEqual(e,original);self.assertEqual(state,before)
        self.assertIsNone(e['flow']);self.assertIsNone(e['wash_score']);self.assertIsNone(e['manip_flow'])
        with patch.object(engine,'audit',return_value={'decision':'SKIP','reasons':['BUNDLE_EVIDENCE_MISSING']}):
            # The actual omitted-evidence branch avoids the old unconditional
            # audit entirely; present evidence below still goes through audit.
            self.assertTrue(qe.plan(state,e,self.cfg)['quote_demands'])

    def test_v3_buy_exit_preserves_unknown_audit_and_execution_labels(self):
        e=self.market()
        state,out=qe.bind_transition(e,(self.fixture.buy,self.fixture.exit))(engine.initial_state(self.cfg),e,self.cfg)
        buy=next(row for row in out if row['type']=='fill')
        self.assertEqual(buy['bundle_audit']['decision'],'UNKNOWN')
        self.assertFalse(buy['bundle_audit']['ownership_complete'])
        self.assertFalse(buy['bundle_audit']['entry_authorized'])
        self.assertEqual(buy['entry_policy']['policy_version'],3)
        self.assertEqual(buy['entry_policy']['signal_profile'],e['paper_signal_profile'])
        self.assertIsNone(buy['scores']['safety']);self.assertEqual(buy['execution_status'],'EXECUTION_UNVERIFIED')
        self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',buy['entry_policy']['risk_flags'])
        exited=self.market(T+1);exited['danger']=True
        sell_quote=self.fixture.quote('sell',self.fixture.raw,10_000_000,at=T+1)
        state,out=qe.bind_transition(exited,(sell_quote,))(state,exited,self.cfg)
        sell=next(row for row in out if row.get('side')=='sell')
        self.assertEqual(sell['entry_policy'],buy['entry_policy'])
        self.assertEqual(state['positions'],{})

    def test_strict_v1_v2_missing_bundle_still_reject_and_flags_do_not_opt_in(self):
        for version in (None,1,2):
            cfg={**self.fixture.cfg}
            if version is not None:cfg['experimental_policy_version']=version
            e=self.fixture.market() if version is None else experimental(mint=self.fixture.mint,pool=self.fixture.pool,taker=self.fixture.wallet)
            if version==2:e['paper_experimental']['policy_version']=2
            del e['bundle_evidence']
            e['paper_signal_policy_version']=3
            plan=qe.plan(engine.initial_state(cfg),e,cfg)
            self.assertEqual(plan['quote_demands'],[])
            self.assertIn('BUNDLE_EVIDENCE_MISSING',plan['outcomes'][-1]['reasons'])
        e=self.market()
        with self.assertRaises(ValueError):qe.plan(engine.initial_state(config()),e,config())
        with self.assertRaises(ValueError):qe.plan(engine.initial_state(self.fixture.cfg),e,self.fixture.cfg)

    def test_conflicting_unknown_unsupported_or_unquoted_config_rejects(self):
        e=self.market()
        for changes in ({'paper_signal_policy_version':True},{'paper_signal_policy_version':'3'},
                        {'paper_signal_policy_version':1},{'experimental_policy_version':1},
                        {'experimental_policy_version':True},{'paper_quote_execution_version':None}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                qe.plan(engine.initial_state(self.cfg|changes),e,self.cfg|changes)

    def test_present_null_empty_stale_incomplete_or_known_bad_bundle_never_waived(self):
        e=self.market()
        for evidence in (None,{},[],{'as_of':T-121},
                         {'as_of':T,'launch_history_complete':False,'funding_history_complete':False}):
            with self.subTest(evidence=evidence):
                bad=e|{'bundle_evidence':evidence}
                plan=qe.plan(engine.initial_state(self.cfg),bad,self.cfg)
                self.assertEqual(plan['quote_demands'],[])
                self.assertTrue(any('BUNDLE' in reason or 'HISTORY' in reason for reason in plan['outcomes'][-1]['reasons']))
        known=copy.deepcopy(self.fixture.market()['bundle_evidence'])
        known['known_bad_wallets']=['w000']
        plan=qe.plan(engine.initial_state(self.cfg),e|{'bundle_evidence':known},self.cfg)
        self.assertEqual(plan['quote_demands'],[])
        self.assertNotEqual(plan['outcomes'][-1]['bundle_audit']['decision'],'UNKNOWN')

    def test_corrupt_bundle_rolls_back_original_records(self):
        e=self.market();e['bundle_evidence']={'as_of':T,'holders':[{'wallet':'bad','pct':'NaN'}]}
        before=list(self.fixture.ledger.db.iterdump())
        with self.assertRaises(ValueError):
            self.fixture.ledger.apply(e,self.cfg,qe.bind_transition(e,(self.fixture.buy,self.fixture.exit)),engine.initial_state)
        self.assertEqual(list(self.fixture.ledger.db.iterdump()),before)

    def test_known_token_and_named_proxy_hazards_still_reject(self):
        for change in ({'danger':True},{'mint_revoked':False},{'data_healthy':False},{'route_available':False}):
            with self.subTest(change=change):
                planned=qe.plan(engine.initial_state(self.cfg),self.market()|change,self.cfg)
                self.assertEqual(planned['quote_demands'],[])
        e=self.market();bad=copy.deepcopy(e)
        bad['paper_signal_profile']['measurements']['same_wallet_churn_proxy_v1']['value']='.9'
        planned=qe.plan(engine.initial_state(self.cfg),bad,self.cfg)
        self.assertEqual(planned['quote_demands'],[])
        self.assertIn('MOMENTUM_OR_OBSERVED_CHURN',planned['outcomes'][-1]['reasons'])
        bad=copy.deepcopy(e);bad['flow']=100
        with self.assertRaises(ValueError):qe.plan(engine.initial_state(self.cfg),bad,self.cfg)
        bad=copy.deepcopy(e);bad['paper_signal_profile']['window']['coverage_complete']=False
        with self.assertRaises(ValueError):qe.plan(engine.initial_state(self.cfg),bad,self.cfg)

    def test_repaired_builder_wallet_binding_replays_actual_quote_without_fallback(self):
        f=adapter_fixture.PaperMarketAdapterTests();f.setUp();self.addCleanup(f.doCleanups)
        built=f.build()['event'];self.assertIsNotNone(built)
        self.assertEqual(built['taker'],f.target.taker)
        state=engine.initial_state(self.cfg);before=copy.deepcopy(state)
        planned=qe.plan(state,built,self.cfg,(f.observation.quote,))
        self.assertEqual(state,before)
        _,outcomes=qe.bind_transition(built,(f.observation.quote,))(copy.deepcopy(state),built,self.cfg)
        self.assertEqual(planned['outcomes'],outcomes)
        # Original bound mint account now reaches the unchanged token gate.
        # This adverse measured-window fixture must still reject known churn.
        from desk.security import entry_token_policy
        self.assertEqual(entry_token_policy(built),[])
        self.assertEqual(built['token_evidence']['source_hash'],built['paper_source_evidence']['mint_hash'])
        self.assertNotIn('TOKEN_EVIDENCE_MISSING_OR_MISMATCHED',outcomes[-1]['reasons'])
        self.assertIn('MOMENTUM_OR_OBSERVED_CHURN',outcomes[-1]['reasons'])
        self.assertFalse(planned['quote_demands'])
        for value in (None,'invalid','So11111111111111111111111111111111111111112'):
            bad=copy.deepcopy(built);bad['taker']=value
            with self.subTest(taker=value),self.assertRaises(ValueError):
                qe.plan(state,bad,self.cfg,(f.observation.quote,))
        compatible={**self.cfg,'experimental_policy_version':3}
        self.assertEqual(qe.plan(state,built,compatible,(f.observation.quote,))['outcomes'],outcomes)
