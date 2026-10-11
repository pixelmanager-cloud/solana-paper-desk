"""Stored positions and original collector SELL reads; no provider calls."""
import copy
from dataclasses import replace
import json
import unittest

from desk import engine, quote_execution as qe
from desk.model import digest,validate_event
from desk.paper_exit_adapter import ExitContext,build_exit_event
from tests import test_quote_execution as execution_fixture
from tests import test_paper_market_adapter as adapter_fixture
from tests.helpers import T,config


class ExitObservationTests(unittest.TestCase):
    def setUp(self):
        self.a=adapter_fixture.PaperMarketAdapterTests();self.addCleanup(self.a.doCleanups);self.a.setUp()
        self.f=execution_fixture.QuoteExecutionTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.a.fixture.at=T;self.a.now=T
        self.a.context=replace(self.a.context,now=T,graduated_at=T-600)
        self.f.mint=self.a.target.mint;self.f.pool=self.a.target.pool
        self.f.buy=self.f.quote('buy',10_000_000,1_000_000)
        self.f.raw=qe.output_raw(self.f.buy,self.f.cfg)
        self.f.exit=self.f.quote('sell',self.f.raw,10_000_000)
        self.f.buy_fill()  # real Ledger.apply, then public checkpoint reader
        self.original=self.f.state()
        self.a.fixture.at=T+1
        self.target=replace(self.a.target,amount_raw=self.f.raw)
        self.collected=self.a.fixture.collect(open_positions=(self.target,)).observations[0]
        self.ctx=ExitContext(T+1,self.target,'fixture-protocol-rpc','fixture-protocol-quote',
                             'SYNTHETIC_TEST_ONLY',())

    def build(self,**changes):
        args=dict(context=self.ctx,position=self.f.state()['positions'][self.f.mint],
                  cfg=self.f.cfg,load_evidence=self.a.fixture.progress.store.load)
        args.update(changes)
        return build_exit_event(self.collected,**args)

    def apply(self,e,quotes=None):
        return self.f.ledger.apply(e,self.f.cfg,qe.bind_transition(e,
            (self.collected.quote,) if quotes is None else quotes),engine.initial_state)

    def test_reproduce_entry_builder_failure_then_mark_actual_held_position(self):
        self.a.observation=self.collected
        rows=[self.a.trade('sell',10,100)]
        rejected=self.a.build(context=replace(self.a.context,now=T+1,target=self.target),
            raw_trades=rows,history_pages=self.a.history(rows))
        self.assertIsNone(rejected['event'])
        self.assertIn('MISSING_WINDOW_MEASUREMENT:buyer_volume_concentration_proxy_v1',rejected['blockers'])
        before=copy.deepcopy(self.f.state());e=self.build()['event'];self.assertIsNotNone(e)
        self.assertNotIn('paper_signal_profile',e);self.assertNotIn('flow',e)
        self.assertFalse(e['entry_authorized']);self.assertEqual(e['execution_status'],'EXECUTION_UNVERIFIED')
        planned=qe.plan(before,e,self.f.cfg,(self.collected.quote,))
        self.assertEqual(self.f.state(),before)
        outcomes=self.apply(e);self.assertEqual(outcomes,planned['outcomes'])
        state=self.f.state()
        if self.f.mint in state['positions']:
            p=state['positions'][self.f.mint]
            self.assertEqual(p['mark_at'],T+1);self.assertEqual(p['mark_status'],'MODEL_ESTIMATE')
            self.assertEqual(p['last_quote_execution']['quote_hash'],self.collected.quote.source.raw_hash)
        else:self.assertTrue(any(o.get('side')=='sell' for o in outcomes))
        self.assertFalse(any(o.get('side')=='buy' for o in outcomes))

    def test_known_entry_hazard_triggers_exact_sell_and_idempotent_restart(self):
        e=self.build(context=replace(self.ctx,known_hazards=('KNOWN_ADVERSE_EVIDENCE',)))['event']
        self.assertIsNotNone(e);self.assertTrue(e['danger'])
        planned=qe.plan(self.original,e,self.f.cfg,(self.collected.quote,))
        sold=self.apply(e);self.assertEqual(sold,planned['outcomes'])
        fill=next(o for o in sold if o.get('side')=='sell')
        self.assertEqual(fill['reason'],'DANGER')
        self.assertEqual(fill['quote_execution']['input_raw'],self.f.raw)
        self.assertEqual(fill['execution_status'],'EXECUTION_UNVERIFIED')
        self.assertEqual(self.f.state()['positions'],{})
        self.assertEqual(self.apply(e),[])
        self.assertEqual(self.f.state()['positions'],{})

    def test_no_position_cannot_enter_and_default_strict_cannot_opt_in(self):
        e=self.build()['event']
        state=engine.initial_state(self.f.cfg);before=copy.deepcopy(state)
        planned=qe.plan(state,e,self.f.cfg,(self.collected.quote,))
        self.assertEqual(planned['quote_demands'],[])
        self.assertEqual(planned['outcomes'][0]['reason'],'EXIT_POSITION_REQUIRED')
        self.assertEqual(state,before)
        with self.assertRaises(ValueError):engine.transition(engine.initial_state(config()),e,config())
        bad=copy.deepcopy(e);bad['entry_authorized']=True
        with self.assertRaises(ValueError):validate_event(bad)
        bad=copy.deepcopy(e);bad['kind']='market'
        with self.assertRaises(ValueError):qe.plan(self.original,bad,self.f.cfg,(self.collected.quote,))

    def test_stale_corrupt_direction_decimals_and_exact_size_refuse_publication(self):
        original=self.collected
        for attack in ('stale','hash','buy','size','decimals','wallet','pool'):
            self.collected=original;ctx=self.ctx
            if attack=='stale':ctx=replace(ctx,now=T+12)
            elif attack=='hash':
                q=original.quote;self.collected=replace(original,quote=replace(q,source=replace(q.source,raw_hash='0'*64)))
            elif attack=='buy':self.collected=replace(original,direction='buy')
            elif attack=='size':
                target=replace(self.target,amount_raw=self.f.raw+1)
                ctx=replace(ctx,target=target);self.collected=replace(original,target=target)
            elif attack=='decimals':self.collected=replace(original,mint=replace(original.mint,decimals=7))
            elif attack=='wallet':ctx=replace(ctx,target=replace(self.target,taker='So11111111111111111111111111111111111111112'))
            elif attack=='pool':ctx=replace(ctx,target=replace(self.target,pool='So11111111111111111111111111111111111111112'))
            with self.subTest(attack=attack):
                out=self.build(context=ctx);self.assertIsNone(out['event'])
                self.assertEqual(self.f.state(),self.original)
        self.collected=original

    def test_missing_quote_marks_unverified_and_forged_source_rolls_back(self):
        e=self.build()['event']
        missing=qe.plan(self.original,e,self.f.cfg)
        self.assertEqual(missing['quote_demands'][0]['input_raw'],self.f.raw)
        self.apply(e,())
        p=self.f.state()['positions'][self.f.mint]
        self.assertEqual(p['mark_status'],'UNVERIFIED_EXIT')
        self.assertEqual(p['exit_blocked'],'EXIT_SELLABILITY_UNVERIFIED')
        # Source-hash substitution cannot replay a different bound full-size quote.
        bad=copy.deepcopy(e);bad['source_evidence']['quote_hash']='0'*64
        bad['event_id']='paper-exit:'+digest({k:v for k,v in bad.items() if k!='event_id'})
        before=list(self.f.ledger.db.iterdump())
        with self.assertRaises(ValueError):self.apply(bad)
        self.assertEqual(list(self.f.ledger.db.iterdump()),before)
        for change in ({'current_quantity_raw':self.f.raw+1},{'mint_decimals':7},
                       {'paper_signal_profile':{}},{'danger':True}):
            bad=copy.deepcopy(e);bad.update(change)
            bad['event_id']='paper-exit:'+digest({k:v for k,v in bad.items() if k!='event_id'})
            with self.subTest(change=change),self.assertRaises(ValueError):self.apply(bad)

    def test_stale_collection_plus_existing_clock_expires_mark_without_fill(self):
        self.assertIsNone(self.build(context=replace(self.ctx,now=T+12))['event'])
        clock={'schema_version':1,'event_id':'exit-outage-clock','kind':'clock',
               'ts':T+12,'actor':'paper_monitor'}
        out=self.f.ledger.apply(clock,self.f.cfg,qe.bind_transition(clock,()),engine.initial_state)
        self.assertFalse(any(o.get('type')=='fill' for o in out))
        held=self.f.state()['positions'][self.f.mint]
        self.assertEqual(held['mark_status'],'STALE')
        self.assertTrue(held['exit_blocked'])

    def test_bounded_exit_grammar_refuses_hostile_flags_refs_and_scalars(self):
        e=self.build()['event']
        for change in ({'mint_decimals':True},{'current_quantity_raw':2**64},
                       {'known_hazards':['x']*33,'danger':True},{'ts':True},
                       {'execution_status':'VERIFIED'},{'source_evidence':[]},
                       {'exit_contract_version':True},{'price_at':T-10}):
            bad=copy.deepcopy(e);bad.update(change)
            bad['event_id']='paper-exit:'+digest({k:v for k,v in bad.items() if k!='event_id'})
            with self.subTest(change=change),self.assertRaises(ValueError):validate_event(bad)
        bad=copy.deepcopy(e);bad['source_evidence']['collector_refs']*=2
        bad['event_id']='paper-exit:'+digest({k:v for k,v in bad.items() if k!='event_id'})
        with self.assertRaises(ValueError):validate_event(bad)


if __name__=='__main__':unittest.main()
