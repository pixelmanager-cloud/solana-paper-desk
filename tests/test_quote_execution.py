"""Synthetic quote envelopes with public mint layout; no provider or signer I/O."""
import base64
import copy
from dataclasses import replace
from decimal import Decimal, localcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desk import engine,quote_execution as q
from desk.ledger import Ledger
from desk.live_observation import ProviderObservation,ingest_mint,ingest_quote
from desk.model import canonical,digest
from desk.providers import SOL
from tests.helpers import config,event,T,control
from tests.test_paper_experimental_scoring import experimental


class QuoteExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'paper.sqlite'
        self.cfg={**config(),'paper_quote_execution_version':1}
        public=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-roundtrip-simulation.json').read_text())
        self.mint=public['result']['mint'];self.wallet=public['result']['wallet']
        self.pool=public['quote_responses'][0]['routePlan'][0]['swapInfo']['ammKey']
        self.buy=self.quote('buy',10_000_000,1_000_000)
        self.raw=q.output_raw(self.buy,self.cfg)
        self.exit=self.quote('sell',self.raw,10_000_000)
        self.ledger=Ledger(self.path);self.addCleanup(self.ledger.close)
        guard=patch('socket.socket',side_effect=AssertionError('Fixture only'))
        guard.start();self.addCleanup(guard.stop)

    def market(self,ts=T,**changes):
        return event(ts,**({'mint':self.mint,'pool':self.pool,'taker':self.wallet}|changes))

    def quote(self,direction,amount,output,*,at=T,decimals=6,minimum=None):
        mint_event=event(at,mint=self.mint)
        raw=bytearray(base64.b64decode(mint_event['token_evidence']['account']['data'][0]));raw[44]=decimals
        account=mint_event['token_evidence']['account'];account['data'][0]=base64.b64encode(raw).decode()
        mint={'mint':self.mint,'slot':100,'observed_at':at,'account':account}
        token=ingest_mint(lambda:ProviderObservation('fixture:synthetic-mint',at,mint),mint=self.mint,now=at)
        source,target=(SOL,self.mint) if direction=='buy' else (self.mint,SOL)
        payload={'kind':'unsigned_route_probe','observed_at':at,
            'request':{'inputMint':source,'outputMint':target,'amount':str(amount),'taker':self.wallet,'slippageBps':'100'},
            'response':{'inputMint':source,'outputMint':target,'swapMode':'ExactIn','inAmount':str(amount),
                'outAmount':str(output),'otherAmountThreshold':str(output*99//100 if minimum is None else minimum),
                'slippageBps':100,'routePlan':[{'percent':100,'swapInfo':{'inputMint':source,'outputMint':target,
                    'inAmount':str(amount),'outAmount':str(output),'ammKey':self.pool}}]},
            'fixture_provenance':'SYNTHETIC_QUOTE_NOT_PROVIDER_RESPONSE'}
        return ingest_quote(lambda:ProviderObservation('fixture:synthetic-quote',at,payload),mint=token,direction=direction,
                            amount_raw=amount,taker=self.wallet,now=at,expected_pool=self.pool)

    def apply(self,e,quotes=()):
        return self.ledger.apply(e,self.cfg,q.bind_transition(e,quotes),engine.initial_state)

    def state(self):return self.ledger.report()['state']
    def buy_fill(self):
        out=self.apply(self.market(),(self.buy,self.exit))
        return next(row for row in out if row['type']=='fill')

    def test_buy_full_exit_exact_cash_fees_and_assumptions(self):
        buy=self.buy_fill();qty=q.units(self.raw,6)
        self.assertEqual(Decimal(buy['quantity']),qty)
        self.assertEqual(buy['amount_sol'],'0.010000000')
        self.assertEqual(self.state()['positions'][self.mint]['quote_execution']['status'],q.STATUS)
        sell_quote=self.quote('sell',self.raw,9_500_000,at=T+1)
        sold=self.apply(self.market(T+1,danger=True),(sell_quote,))
        sell=next(row for row in sold if row['type']=='fill')
        proceeds=q.units(q.output_raw(sell_quote,self.cfg),9)-Decimal(self.cfg['fixed_fee_sol'])
        cost=Decimal('.01')+Decimal(self.cfg['fixed_fee_sol'])
        self.assertEqual(Decimal(sell['proceeds_sol']),proceeds)
        self.assertEqual(Decimal(sell['realized_pnl_sol']),proceeds-cost)
        self.assertEqual(Decimal(self.state()['cash']),Decimal('5')-cost+proceeds)
        self.assertEqual(self.state()['positions'],{})
        self.assertEqual(Decimal(self.state()['day_gross_losses']),cost-proceeds)
        for fill in (buy,sell):
            self.assertEqual(fill['execution_status'],q.STATUS)
            record=fill['quote_execution']
            self.assertFalse(record['source_authenticated']);self.assertFalse(record['transaction_verified'])
            self.assertFalse(record['actual_fill_verified'])
            self.assertEqual(digest(json.loads(record['original_quote_json'])),record['quote_hash'])
            self.assertEqual(record['assumptions']['additional_adverse_slippage_bps'],50)

    def test_missing_reverse_exact_amount_or_all_quotes_never_fills(self):
        for quotes in ((),(self.buy,),(self.buy,self.quote('sell',self.raw+1,10_000_000))):
            with self.subTest(quotes=len(quotes)):
                state,out=q.bind_transition(self.market(),quotes)(engine.initial_state(self.cfg),self.market(),self.cfg)
                self.assertFalse(any(row['type']=='fill' for row in out));self.assertEqual(state['cash'],'5')
        state,out=engine.transition(engine.initial_state(self.cfg),self.market(),self.cfg)
        self.assertFalse(any(row['type']=='fill' for row in out))

    def test_stale_future_and_forged_quote_or_source_roll_back(self):
        original=list(self.ledger.db.iterdump())
        for buy in (replace(self.buy,input_raw=True),replace(self.buy,estimated_output_raw=self.buy.estimated_output_raw+1),
                    replace(self.buy,minimum_output_units=Decimal('999')),
                    replace(self.buy,source=replace(self.buy.source,raw_hash='0'*64)),
                    replace(self.buy,source=replace(self.buy.source,observed_at=T+1)),
                    self.quote('buy',10_000_000,1_000_000,at=T-11)):
            with self.subTest(buy=buy.source.observed_at),self.assertRaises(ValueError):
                self.apply(self.market(),(buy,self.exit))
            self.assertEqual(list(self.ledger.db.iterdump()),original)

    def test_wrong_wallet_mint_pool_and_event_binding_reject(self):
        for change in ({'taker':SOL},{'mint':SOL},{'pool':SOL}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.apply(self.market(**change),(self.buy,self.exit))
        bound=q.bind_transition(self.market(),(self.buy,self.exit))
        with self.assertRaisesRegex(ValueError,'QUOTE_EVENT_BINDING_MISMATCH'):
            bound(engine.initial_state(self.cfg),self.market(T+1),self.cfg)

    def test_duplicate_contradictory_suffix_and_count_bound_reject(self):
        for quotes in ((self.buy,self.exit,self.buy),(self.buy,self.exit,replace(self.exit,input_raw=False)),
                       tuple([self.buy]*9)):
            with self.subTest(count=len(quotes)),self.assertRaises(ValueError):self.apply(self.market(),quotes)

    def test_opt_in_required_defaults_unchanged_and_config_rebind_refused(self):
        strict=engine.initial_state(config())
        normal=event();_,out=engine.transition(strict,normal,config())
        self.assertEqual(next(r for r in out if r['type']=='fill')['simulation'],'constant_product')
        with self.assertRaisesRegex(ValueError,'QUOTE_EXECUTION_CONFIG_REQUIRED'):
            q.bind_transition(self.market(),(self.buy,self.exit))(engine.initial_state(config()),self.market(),config())
        flagged=event(paper_quote_execution_version=1,execution_allowed=True)
        _,out=engine.transition(engine.initial_state(config()),flagged,config())
        self.assertEqual(next(r for r in out if r['type']=='fill')['simulation'],'constant_product')
        self.buy_fill()
        for cfg in (config(),{**self.cfg,'fixed_fee_sol':'.00006'},{**self.cfg,'paper_quote_execution_version':2}):
            with self.assertRaises(ValueError):
                self.ledger.apply(self.market(T+1),cfg,engine.transition,engine.initial_state)

    def test_known_hazards_and_risk_limits_still_reject(self):
        for changes in ({'danger':True},{'mint_revoked':False},{'data_healthy':False},{'wash_score':'.9'},
                        {'market_cap_usd':'10'},{'price_at':T-11},{'route_available':False}):
            with self.subTest(changes=changes):
                state,out=q.bind_transition(self.market(**changes),(self.buy,self.exit))(
                    engine.initial_state(self.cfg),self.market(**changes),self.cfg)
                self.assertFalse(any(row['type']=='fill' for row in out));self.assertEqual(state['cash'],'5')
        large=self.quote('buy',1_000_000_000,100_000_000)
        reverse=self.quote('sell',q.output_raw(large,self.cfg),1_000_000_000)
        _,out=q.bind_transition(self.market(),(large,reverse))(engine.initial_state(self.cfg),self.market(),self.cfg)
        self.assertEqual(out[-1]['reason'],'EXACT_FRESH_ROUNDTRIP_QUOTES_REQUIRED')

    def test_missing_exit_quote_latches_and_resume_cannot_invent_fill(self):
        self.buy_fill();cash=self.state()['cash'];qty=self.state()['positions'][self.mint]['qty']
        out=self.apply(self.market(T+1,danger=True))
        self.assertFalse(any(row['type']=='fill' for row in out))
        self.assertEqual(self.state()['cash'],cash);self.assertEqual(self.state()['positions'][self.mint]['qty'],qty)
        self.assertEqual(self.state()['mode'],'EXIT_ONLY')
        self.apply(control(T+2,'RESUME'))
        self.assertEqual(self.state()['mode'],'EXIT_ONLY')

    def test_partial_exact_quote_and_raw_floor_no_scaled_proceeds(self):
        self.buy_fill()
        full=self.quote('sell',self.raw,20_000_000,at=T+1)
        action_raw=self.raw*3//10
        action=self.quote('sell',action_raw,6_000_000,at=T+1)
        out=self.apply(self.market(T+1),(full,action))
        sell=next(r for r in out if r['type']=='fill')
        self.assertEqual(sell['reason'],'TAKE_PROFIT');self.assertEqual(Decimal(sell['quantity']),q.units(action_raw,6))
        self.assertEqual(Decimal(sell['proceeds_sol']),q.units(q.output_raw(action,self.cfg),9)-Decimal(self.cfg['fixed_fee_sol']))
        pos=self.state()['positions'][self.mint]
        self.assertEqual(Decimal(pos['qty']),q.units(self.raw-action_raw,6))
        self.assertEqual(pos['exit_blocked'],'REMAINING_POSITION_VALUATION_REQUIRED')
        self.assertEqual(pos['mark_value'],'0')

    def test_full_quote_cannot_authorize_partial_exit(self):
        self.buy_fill();cash=self.state()['cash']
        out=self.apply(self.market(T+1),(self.quote('sell',self.raw,20_000_000,at=T+1),))
        self.assertFalse(any(row['type']=='fill' for row in out));self.assertEqual(self.state()['cash'],cash)
        self.assertEqual(self.state()['positions'][self.mint]['qty'],str(q.units(self.raw,6)))

    def test_restart_duplicate_and_metadata_tamper_never_adopts_model(self):
        self.buy_fill();before=list(self.ledger.db.iterdump())
        self.assertEqual(self.apply(self.market(),(self.buy,self.exit)),[])
        self.assertEqual(list(self.ledger.db.iterdump()),before)
        reopened=Ledger(self.path);self.addCleanup(reopened.close)
        out=reopened.apply(self.market(T+1,danger=True),self.cfg,
            q.bind_transition(self.market(T+1,danger=True),(self.quote('sell',self.raw,10_000_000,at=T+1),)),engine.initial_state)
        self.assertTrue(any(r.get('side')=='sell' for r in out))
        state=engine.initial_state(self.cfg)
        state,_=q.bind_transition(self.market(),(self.buy,self.exit))(state,self.market(),self.cfg)
        for changes in ({'status':'VERIFIED'},{'version':True},{'original_quote_json':'{}'},{'simulated_output_raw':self.raw+1}):
            bad=copy.deepcopy(state);bad['positions'][self.mint]['quote_execution'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                engine.transition(bad,control(T+1,'RESUME'),self.cfg)

    def test_explicit_unknown_history_labels_remain_on_entry_and_exit(self):
        self.cfg['experimental_policy_version']=1
        buy_event=experimental(mint=self.mint,pool=self.pool,taker=self.wallet)
        out=self.apply(buy_event,(self.buy,self.exit))
        buy=next(r for r in out if r['type']=='fill')
        self.assertIsNone(buy['scores']['safety']);self.assertFalse(buy['entry_policy']['ownership_complete'])
        sell_event=experimental(ts=T+1,mint=self.mint,pool=self.pool,taker=self.wallet,danger=True)
        sold=self.apply(sell_event,(self.quote('sell',self.raw,10_000_000,at=T+1),))
        sell=next(r for r in sold if r['type']=='fill')
        self.assertEqual(sell['entry_policy'],buy['entry_policy'])

    def test_low_decimal_context_does_not_round_raw_units_or_accounting(self):
        with localcontext() as ctx:
            ctx.prec=6
            out=q.bind_transition(self.market(),(self.buy,self.exit))(engine.initial_state(self.cfg),self.market(),self.cfg)
        normal=q.bind_transition(self.market(),(self.buy,self.exit))(engine.initial_state(self.cfg),self.market(),self.cfg)
        self.assertEqual(out,normal)
        huge=self.quote('buy',2**64-1,2**64-1,decimals=255)
        with localcontext() as ctx:
            ctx.prec=6
            self.assertEqual(q.raw_quantity(q.units(huge.estimated_output_raw,255),255),2**64-1)
            self.assertEqual(q.output_raw(huge,self.cfg),huge.minimum_output_raw*9950//10000)

    def test_planner_risk_allocation_reverse_and_partial_demands_without_mutation(self):
        state=engine.initial_state(self.cfg);e=self.market()
        original=copy.deepcopy((state,e))
        first=q.plan(state,e,self.cfg)
        self.assertEqual((state,e),original)
        demand=first['quote_demands'][0]
        self.assertEqual(demand['direction'],'buy')
        self.assertEqual(demand['minimum_input_raw'],10_000_000)
        self.assertGreaterEqual(demand['maximum_input_raw'],self.buy.input_raw)
        reduced=copy.deepcopy(state);reduced['loss_streak']=4
        half=q.plan(reduced,e,self.cfg)['quote_demands'][0]
        self.assertEqual(half['maximum_input_raw'],demand['maximum_input_raw']//2)
        reverse=q.plan(state,e,self.cfg,(self.buy,))
        self.assertEqual(reverse['quote_demands'],[{'direction':'sell','input_raw':self.raw}])
        complete=q.plan(state,e,self.cfg,(self.buy,self.exit))
        self.assertEqual(complete['quote_demands'],[])
        self.assertTrue(any(r['type']=='fill' for r in complete['outcomes']))
        self.assertEqual((state,e),original)
        self.buy_fill();saved=self.state();before=copy.deepcopy(saved)
        full=q.plan(saved,self.market(T+1),self.cfg)
        self.assertEqual(full['quote_demands'],[{'direction':'sell','input_raw':self.raw}])
        partial=q.plan(saved,self.market(T+1),self.cfg,(self.quote('sell',self.raw,20_000_000,at=T+1),))
        self.assertEqual(partial['quote_demands'],[{'direction':'sell','input_raw':self.raw*3//10}])
        self.assertEqual(saved,before)
        hazard=q.plan(engine.initial_state(self.cfg),self.market(danger=True),self.cfg)
        self.assertEqual(hazard['quote_demands'],[])
        self.assertEqual(hazard['outcomes'][-1]['reason'],'DANGER')

    def test_real_provenance_uses_only_bound_quotes_not_asserted_execution_flags(self):
        real=self.market(provenance='MAINNET_OBSERVATION',sellability={'kind':'synthetic_model'},
                         transaction_policy_ok=False,eligible_for_trading=False)
        _,out=q.bind_transition(real,(self.buy,self.exit))(engine.initial_state(self.cfg),real,self.cfg)
        self.assertTrue(any(r['type']=='fill' for r in out))
        self.assertFalse(next(r for r in out if r['type']=='fill')['quote_execution']['transaction_verified'])
        _,missing=engine.transition(engine.initial_state(self.cfg),real,self.cfg)
        self.assertFalse(any(r['type']=='fill' for r in missing))
        _,strict=engine.transition(engine.initial_state(config()),real,config())
        self.assertFalse(any(r['type']=='fill' for r in strict))
        self.assertIn('LIVE_FEATURE_ADAPTER_NOT_READY',strict[-1]['reasons'])

    def test_no_profitable_fill_from_cost_budget_or_insolvent_exit(self):
        bad=self.quote('sell',self.raw,1_000_000)
        _,out=q.bind_transition(self.market(),(self.buy,bad))(engine.initial_state(self.cfg),self.market(),self.cfg)
        self.assertEqual(out[-1]['reason'],'COST_BUDGET')
        self.buy_fill();before=self.state()['cash']
        out=self.apply(self.market(T+1,danger=True),(self.quote('sell',self.raw,100,at=T+1),))
        self.assertFalse(any(r['type']=='fill' for r in out));self.assertEqual(self.state()['cash'],before)
        self.assertEqual(self.state()['positions'][self.mint]['mark_value'],'0')

    def test_invalid_config_fee_precision_and_slippage_bounds(self):
        for changes in ({'paper_quote_execution_version':True},{'paper_quote_execution_version':'1'},
                        {'paper_quote_execution_version':2},{'mode':'live'},{'fixed_fee_sol':'NaN'},
                        {'fixed_fee_sol':'0.0000000001'},{'fixed_fee_sol':'-1'},
                        {'adverse_slippage_bps':'10000'},{'adverse_slippage_bps':'1.5'},
                        {'price_ttl_seconds':True},{'price_ttl_seconds':61}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):q.config(self.cfg|changes)
