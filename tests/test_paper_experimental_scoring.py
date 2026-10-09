"""Explicit paper scoring opt-in; synthetic measured fields, no live approval."""
import copy
import json
import unittest

from desk.model import (D, validate_event, canonical, PAPER_EXPERIMENTAL,
    EXPERIMENTAL_POLICY_VERSION, OWNERSHIP_METRICS, OWNERSHIP_HISTORY_RISK)
from desk.strategy import scores, gates, size, swap_quote, experimental_scores, experimental_gates
from tests.helpers import event, config


def experimental(**changes):
    e=event(**changes)
    for key in OWNERSHIP_METRICS:e[key]=None
    e['paper_experimental']={'mode':PAPER_EXPERIMENTAL,'policy_version':1,
        'risk_flag':OWNERSHIP_HISTORY_RISK,'ownership_unknowns':{
            key:{'status':'UNKNOWN','reasons':['PERSISTED_OWNERSHIP_HISTORY_INCOMPLETE']} for key in OWNERSHIP_METRICS}}
    return e


def score(e):return experimental_scores(e,mode=PAPER_EXPERIMENTAL,policy_version=EXPERIMENTAL_POLICY_VERSION)
def check(e):return experimental_gates(e,config(),mode=PAPER_EXPERIMENTAL,policy_version=EXPERIMENTAL_POLICY_VERSION)


class PaperExperimentalScoringTests(unittest.TestCase):
    def test_strict_validation_scoring_and_gates_unchanged(self):
        e=event();validate_event(e);s=scores(e)
        self.assertEqual(s,{'safety':D(100),'momentum':D(93),'flow':D(90),'entry':D('94.72222222222222222222222222')})
        self.assertEqual(gates(e,config(),s),[])
        unknown=experimental()
        with self.assertRaises(ValueError):validate_event(unknown)
        with self.assertRaises(ValueError):scores(unknown)
        # Metadata alone never changes the existing engine's strict call.
        self.assertIsNone(unknown['top10_pct'])

    def test_explicit_unknown_validates_and_scores_only_measured_components(self):
        e=experimental();before=copy.deepcopy(e)
        validate_event(e,mode=PAPER_EXPERIMENTAL,policy_version=1)
        s=score(e)
        expected=(30*D(90)+25*D(93))/55
        self.assertEqual(D(s['entry']),expected);self.assertEqual(D(s['confidence']),(expected-60)/40)
        self.assertIsNone(s['safety']);self.assertEqual(s['ownership_component']['status'],'OMITTED')
        self.assertEqual(s['entry_basis'],'FLOW_MOMENTUM_30_25')
        self.assertEqual(s['risk_flags'],[OWNERSHIP_HISTORY_RISK]);self.assertEqual(s['policy_version'],1)
        self.assertEqual(set(s['ownership_component']['unknown_fields']),set(OWNERSHIP_METRICS))
        self.assertFalse(s['ownership_verified']);self.assertFalse(s['ownership_complete'])
        self.assertFalse(s['entry_authorized']);self.assertFalse(s['source_authenticated'])
        self.assertEqual(check(e),[]);self.assertEqual(e,before)
        self.assertEqual(json.loads(canonical(s)),s)

    def test_explicit_mode_version_metadata_and_null_binding_required(self):
        e=experimental()
        for mode,version in ((None,1),('LIVE',1),(PAPER_EXPERIMENTAL,None),(PAPER_EXPERIMENTAL,True),(PAPER_EXPERIMENTAL,2)):
            with self.assertRaises(ValueError):experimental_scores(e,mode=mode,policy_version=version)
        variants=[]
        for key in ('mode','policy_version','risk_flag','ownership_unknowns'):
            bad=copy.deepcopy(e);del bad['paper_experimental'][key];variants.append(bad)
        for value in (True,2):
            bad=copy.deepcopy(e);bad['paper_experimental']['policy_version']=value;variants.append(bad)
        for value in (0,'UNKNOWN',False):
            bad=copy.deepcopy(e);bad['top10_pct']=value;variants.append(bad)
        bad=copy.deepcopy(e);del bad['top10_pct'];variants.append(bad)
        bad=copy.deepcopy(e);bad['paper_experimental']['ownership_unknowns']['flow']={'status':'UNKNOWN','reasons':['missing']};variants.append(bad)
        bad=copy.deepcopy(e);bad['paper_experimental']['ownership_unknowns']['dev_pct']['reasons']=[];variants.append(bad)
        for bad in variants:
            with self.assertRaises(ValueError):score(bad)

    def test_no_unrelated_missing_numeric_boolean_or_time_is_waived(self):
        names=('flow','net_buy_ratio','unique_buyers_5m','volume_vs_liq','drawdown_from_high','wash_score',
            'fresh_wallet_ratio','manip_safety','manip_flow','dev_launches_7d',
            'reserve_sol','reserve_tokens','sol_usd','market_cap_usd','pool_fee_bps',
            'mint_revoked','freeze_revoked','lp_verified','extensions_safe','data_healthy',
            'route_available','danger','graduated','flow_confirmed','price_at','holder_at','flow_at','momentum_at','graduated_at')
        for name in names:
            for missing in (False,True):
                e=experimental()
                if missing:del e[name]
                else:e[name]=None
                with self.subTest(name=name,missing=missing):
                    with self.assertRaises(ValueError):score(e)
        for value in (True,'NaN','Infinity',-1,101):
            e=experimental(flow=value)
            with self.assertRaises(ValueError):score(e)

    def test_known_nonownership_hazards_all_still_gate(self):
        cases=[('mint_revoked',False,'UNVERIFIED_SAFETY'),('freeze_revoked',False,'UNVERIFIED_SAFETY'),
            ('lp_verified',False,'UNVERIFIED_SAFETY'),('extensions_safe',False,'UNVERIFIED_SAFETY'),
            ('danger',True,'DANGER'),('data_healthy',False,'DATA_UNHEALTHY'),('route_available',False,'NO_ROUTE'),
            ('graduated',False,'UNSUPPORTED_POOL'),('dev_launches_7d',3,'REPEAT_DEPLOYER'),
            ('wash_score','.4','MOMENTUM_OR_WASH'),('reserve_sol','1','LIQUIDITY'),
            ('market_cap_usd','10','MARKET_CAP'),('graduated_at',event()['ts']-1,'AGE')]
        for key,value,reason in cases:
            with self.subTest(key=key):self.assertIn(reason,check(experimental(**{key:value})))

    def test_freshness_and_low_flow_momentum_thresholds_unchanged(self):
        cfg=config()
        for name in ('price','holder','flow','momentum'):
            e=experimental();e[name+'_at']=e['ts']-cfg[name+'_ttl_seconds']-1
            self.assertIn('STALE_'+name.upper(),check(e))
            e[name+'_at']=e['ts']+1
            with self.assertRaises(ValueError):score(e)
        self.assertIn('ENTRY_SCORE',check(experimental(flow=0)))
        self.assertIn('MOMENTUM_OR_WASH',check(experimental(net_buy_ratio=0,unique_buyers_5m=0,volume_vs_liq=0,drawdown_from_high=1)))

    def test_partial_known_ownership_hazard_never_hidden_by_omission(self):
        for key,value in [('top10_pct',50),('dev_pct',20),('bundle_pct',30),('cluster_pct',40)]:
            e=experimental();e[key]=value;del e['paper_experimental']['ownership_unknowns'][key]
            s=score(e);self.assertIsNone(s['safety'])
            self.assertEqual(s['ownership_component']['measured_fields'][key],value)
            self.assertIn('KNOWN_OWNERSHIP_HAZARD',check(e))
        e=experimental(fresh_wallet_ratio=1);e['top10_pct']=20
        del e['paper_experimental']['ownership_unknowns']['top10_pct']
        self.assertIn('KNOWN_OWNERSHIP_HAZARD',check(e))

    def test_fully_measured_ownership_retains_original_safety_and_weighting(self):
        e=experimental();measured=event()
        for key in OWNERSHIP_METRICS:e[key]=measured[key]
        e['paper_experimental']['ownership_unknowns']={}
        s=score(e);strict=scores(e)
        self.assertEqual(s['ownership_component']['status'],'MEASURED')
        for key in ('safety','flow','momentum','entry'):self.assertEqual(D(s[key]),strict[key])
        self.assertEqual(check(e),gates(e,config(),strict))
        e['top10_pct']=100
        self.assertIn('SAFETY',check(e))

    def test_manipulation_penalty_and_capital_cost_inputs_not_bypassed(self):
        e=experimental(manip_flow='.5');s=score(e)
        self.assertEqual(D(s['entry']),((30*D(90)+25*D(93))/55)*D('.7'))
        cfg=config();capital=D(5);cash=D(5)
        amount,limits=size(e,cfg,D(s['entry']),capital,cash,D(0),D('.3'),D(0))
        self.assertLessEqual(amount,D(cfg['max_position_fraction'])*capital*D(s['confidence']))
        self.assertEqual(limits['cash'],cash-D(cfg['fee_reserve_sol'])-D(cfg['fixed_fee_sol']))
        zero,_=size(e,cfg,D(s['entry']),capital,D(cfg['fee_reserve_sol']),D(0),D('.3'),D(0))
        self.assertEqual(zero,0)
        exhausted,_=size(e,cfg,D(s['entry']),capital,cash,D(0),D('.3'),D('.3'))
        self.assertEqual(exhausted,0)
        expensive=copy.deepcopy(e);expensive['pool_fee_bps']=9999
        self.assertLess(swap_quote(expensive,D(1),'buy',cfg),swap_quote(e,D(1),'buy',cfg))
        # No engine/ledger route, quantity, roundtrip cost or portfolio gate is
        # bypassed; this score cannot enable the unchanged strict entry path.
        from desk.engine import transition,initial_state
        with self.assertRaises(ValueError):transition(initial_state(cfg),e,cfg)

    def test_explicit_paper_configuration_and_event_version(self):
        e=experimental();e['schema_version']=True
        with self.assertRaises(ValueError):score(e)
        cfg=config();cfg['mode']='live'
        with self.assertRaises(ValueError):experimental_gates(experimental(),cfg,mode=PAPER_EXPERIMENTAL,policy_version=1)


class PaperExperimentalHistoryProfileTests(unittest.TestCase):
    def event(self):
        e=experimental();e['paper_experimental']['policy_version']=2
        for key in ('fresh_wallet_ratio','dev_launches_7d','manip_safety'):
            e[key]=None;e['paper_experimental']['ownership_unknowns'][key]={'status':'UNKNOWN','reasons':['OWNERSHIP_HISTORY_INCOMPLETE']}
        return e

    def score(self,e):return experimental_scores(e,mode=PAPER_EXPERIMENTAL,policy_version=2)
    def gates(self,e):return experimental_gates(e,config(),mode=PAPER_EXPERIMENTAL,policy_version=2)

    def test_history_dependent_omissions_explicit_no_safety_or_fake_zero(self):
        e=self.event();before=copy.deepcopy(e);s=self.score(e)
        self.assertEqual(s['policy_version'],2);self.assertEqual(s['score_version'],'paper-experimental-history-components-v2')
        self.assertIsNone(s['safety']);self.assertEqual(s['manipulation_safety']['status'],'UNKNOWN')
        self.assertIsNone(s['manipulation_safety']['value']);self.assertTrue(s['manipulation_safety']['reasons'])
        self.assertEqual(s['manipulation_penalty_basis'],['manip_flow'])
        self.assertEqual(set(s['omitted_components']),set(OWNERSHIP_METRICS)|{'fresh_wallet_ratio','dev_launches_7d','manip_safety'})
        self.assertEqual(self.gates(e),[]);self.assertEqual(e,before)
        self.assertEqual(json.loads(canonical(s)),s)
        with self.assertRaises(ValueError):validate_event(e)
        with self.assertRaises(ValueError):experimental_scores(e,mode=PAPER_EXPERIMENTAL,policy_version=1)

    def test_known_bad_developer_wallet_and_manipulation_not_erased(self):
        for key,value,reason in (('dev_launches_7d',3,'REPEAT_DEPLOYER'),('manip_safety',1,'ENTRY_SCORE')):
            e=self.event();e[key]=value;del e['paper_experimental']['ownership_unknowns'][key]
            s=self.score(e);self.assertNotIn(key,s['omitted_components']);self.assertIn(reason,self.gates(e))
        e=self.event();e['fresh_wallet_ratio']=1;e['top10_pct']=20
        for key in ('fresh_wallet_ratio','top10_pct'):del e['paper_experimental']['ownership_unknowns'][key]
        self.assertIn('KNOWN_OWNERSHIP_HAZARD',self.gates(e))

    def test_manipulation_safety_unknown_never_shown_as_safe_scalar(self):
        e=experimental();full=event()
        for key in OWNERSHIP_METRICS:e[key]=full[key]
        e['paper_experimental'].update(policy_version=2,ownership_unknowns={'manip_safety':{'status':'UNKNOWN','reasons':['CLASSIFIER_UNAVAILABLE']}})
        e['manip_safety']=None
        s=self.score(e);self.assertIsNone(s['safety']);self.assertEqual(s['manipulation_safety']['status'],'UNKNOWN')
        self.assertEqual(s['entry_basis'],'FLOW_MOMENTUM_30_25')

    def test_remaining_flow_momentum_price_and_controls_still_mandatory(self):
        for key in ('flow','manip_flow','net_buy_ratio','unique_buyers_5m','volume_vs_liq','wash_score','drawdown_from_high',
                    'sol_usd','reserve_sol','reserve_tokens','market_cap_usd','price_at','pool_fee_bps','route_available','danger'):
            e=self.event();e[key]=None
            with self.subTest(key=key):
                with self.assertRaises(ValueError):self.score(e)
        e=self.event();e['danger']=True;self.assertIn('DANGER',self.gates(e))
        e=self.event();e['mint_revoked']=False;self.assertIn('UNVERIFIED_SAFETY',self.gates(e))
