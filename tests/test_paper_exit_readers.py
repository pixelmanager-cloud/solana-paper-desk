"""Real fixture collector SELL -> held Ledger -> read/restart consumer seam."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from desk import engine, quote_execution as qe
from desk.ledger import Ledger
from desk.model import canonical, digest
from desk.paper_checkpoint import read_checkpoint, RecoveryRequired
from desk.experiment_report import experiment_report
from desk.paper_view import paper_status, _event_json
from desk.paper_exit_adapter import ExitContext, build_exit_event
from desk.paper_observation_collector import BoundedSource
from tests import test_paper_exit_adapter as exit_fixture
from tests import test_paper_observation_collector as collector_fixture
from tests.helpers import T, config
from tests import test_quote_execution_v3_seam as seam_fixture


class ExitReaderTests(unittest.TestCase):
    def setUp(self):
        self.s = exit_fixture.ExitObservationTests(); self.s.setUp()
        self.addCleanup(self.s.doCleanups)
        self.f = self.s.f

    def assert_read_restart_duplicate(self, event, quotes):
        f = self.f
        state = read_checkpoint(f.ledger.db)
        view = paper_status(f.path,now=event['ts'],expected_config=f.cfg)
        self.assertEqual(view['status'],'LEDGER_PRESENT')
        self.assertEqual(view['last_market_at'],T)
        self.assertEqual(view['last_exit_observation_at'],event['price_at'])
        self.assertEqual(view['last_exit_observation_age_seconds'],0)
        self.assertFalse(view['automatic_entry_enabled'])
        self.assertEqual(view['runner_liveness'],'UNKNOWN')
        stale = paper_status(f.path,now=event['ts']+11)
        self.assertEqual(stale['last_exit_observation_currentness'],'STALE')
        reopened = Ledger(f.path,must_exist=True)
        try:
            self.assertEqual(read_checkpoint(reopened.db),state)
            before=list(reopened.db.iterdump())
            self.assertEqual(reopened.apply(event,f.cfg,qe.bind_transition(event,quotes),engine.initial_state),[])
            self.assertEqual(list(reopened.db.iterdump()),before)
        finally: reopened.close()

    def test_actual_collector_full_exit_read_and_reopened_duplicate(self):
        e=self.s.build(context=replace(self.s.ctx,known_hazards=('KNOWN_ADVERSE_EVIDENCE',)))['event']
        out=self.s.apply(e)
        sell=next(o for o in out if o.get('side')=='sell')
        self.assertEqual(sell['quote_execution']['input_raw'],self.f.raw)
        self.assertEqual(read_checkpoint(self.f.ledger.db)['positions'],{})
        self.assert_read_restart_duplicate(e,(self.s.collected.quote,))

    def test_actual_collector_partial_then_full_exit_keeps_basis_and_original_buy(self):
        self.partial_then_full_lifecycle()

    def partial_then_full_lifecycle(self):
        s=self.s; f=self.f; collector=s.a.fixture
        source=collector.sources[s.target.scan_id]
        action_raw=f.raw*3//10
        def quote(input_mint,output_mint,amount,taker,*,timeout_seconds):
            # Synthetic provider fixture, captured by the actual collector; no
            # rewrite of stored originals or forged normalization/approval flags.
            value=source.quote(input_mint,output_mint,amount,taker,timeout_seconds=timeout_seconds)
            output=20000000 if amount==f.raw else 6000000
            value['response']['outAmount']=str(output)
            value['response']['otherAmountThreshold']=str(output-1)
            value['response']['routePlan'][0]['swapInfo']['outAmount']=str(output)
            return value
        collector.sources[s.target.scan_id]=BoundedSource(source.rpc_source_id,source.quote_source_id,source.rpc,quote)
        full=collector.collect(open_positions=(s.target,)).observations[0]
        action=collector.collect(open_positions=(replace(s.target,amount_raw=action_raw),)).observations[0]
        e=build_exit_event(full,context=s.ctx,position=f.state()['positions'][f.mint],cfg=f.cfg,
                           load_evidence=collector.progress.store.load)['event']
        self.assertIsNotNone(e)
        entry=copy.deepcopy(f.state()['positions'][f.mint]['quote_execution'])
        out=s.apply(e,(full.quote,action.quote))
        fill=next(o for o in out if o.get('side')=='sell')
        self.assertEqual(fill['reason'],'TAKE_PROFIT')
        self.assertEqual(fill['valuation_quote_execution']['quote_hash'],full.quote.source.raw_hash)
        self.assertNotEqual(fill['quote_execution']['quote_hash'],fill['valuation_quote_execution']['quote_hash'])
        self.assertEqual(fill['valuation_quote_execution']['original_quote_json'],full.quote.source.original_json)
        self.assertFalse(fill['valuation_quote_execution']['actual_fill_verified'])
        p=read_checkpoint(f.ledger.db)['positions'][f.mint]
        self.assertEqual(p['quote_execution'],entry)
        self.assertEqual(qe.raw_quantity(p['qty'],p['quote_execution']['mint_decimals']),f.raw-action_raw)
        self.assert_read_restart_duplicate(e,(full.quote,action.quote))
        # Independent fixture evidence DB/admission; retain the first fixture's
        # originals and charged requests without resetting its18-request budget.
        fresh=collector_fixture.PaperObservationCollectorTests();fresh.setUp()
        self.addCleanup(fresh.doCleanups);fresh.at=T+2
        target=fresh.target(f.raw-action_raw)
        final=fresh.collect(open_positions=(target,)).observations[0]
        context=replace(s.ctx,now=T+2,target=target,known_hazards=('KNOWN_ADVERSE_EVIDENCE',))
        closed=build_exit_event(final,context=context,position=p,cfg=f.cfg,load_evidence=fresh.progress.store.load)['event']
        self.assertIsNotNone(closed)
        s.apply(closed,(final.quote,))
        self.assertEqual(read_checkpoint(f.ledger.db)['positions'],{})
        self.assert_read_restart_duplicate(closed,(final.quote,))
        return e

    def test_v3_original_buy_risk_survives_exit_and_journal_risk_tampering_rejects(self):
        f=self.f
        f.path=Path(f.tmp.name)/'v3.sqlite'
        fresh=Ledger(f.path);self.addCleanup(fresh.close)
        f.ledger=fresh;f.cfg=f.cfg|{'paper_signal_policy_version':3}
        seam=seam_fixture.QuoteV3SeamTests();seam.fixture=f;seam.cfg=f.cfg
        e=seam.market()
        bought=f.apply(e,(f.buy,f.exit))
        policy=next(o for o in bought if o.get('side')=='buy')['entry_policy']
        exit_event=self.s.build(context=replace(self.s.ctx,known_hazards=('KNOWN_ADVERSE_EVIDENCE',)))['event']
        sold=self.s.apply(exit_event)
        sell=next(o for o in sold if o.get('side')=='sell')
        self.assertEqual(sell['entry_policy'],policy)
        self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',policy['risk_flags'])
        self.assertEqual(read_checkpoint(fresh.db)['positions'],{})
        self.assert_read_restart_duplicate(exit_event,(self.s.collected.quote,))
        row=fresh.db.execute('SELECT seq,payload FROM outcomes WHERE event_id=?',(exit_event['event_id'],)).fetchone()
        bad=json.loads(row[1]);bad['entry_policy']['risk_flags']=[]
        fresh.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(bad),row[0]))
        with self.assertRaises(RecoveryRequired):read_checkpoint(fresh.db)
        before=list(fresh.db.iterdump())
        with self.assertRaises(ValueError):self.s.apply(exit_event)
        self.assertEqual(list(fresh.db.iterdump()),before)

    def test_partial_primary_source_rehash_after_final_close_blocks_all_consumers(self):
        partial=self.partial_then_full_lifecycle()
        self.assertEqual(experiment_report(self.f.path,now=T+2)['closed_trade_count'],1)
        original=list(self.f.ledger.db.iterdump())
        for field,value in (('quote_hash','0'*64),('quote_source_id','rebound'),('quote_at',T),
                            ('mint_hash','0'*64),('rpc_source_id','rebound')):
            bad=copy.deepcopy(partial);bad['source_evidence'][field]=value
            if field=='quote_at':bad['price_at']=value
            self.check_corrupt_event(partial,bad,original,all_consumers=True)
        row=self.f.ledger.db.execute('SELECT seq,payload FROM outcomes WHERE event_id=?',(partial['event_id'],)).fetchone()
        original_outcome=json.loads(row[1])
        attacks=(lambda o:o.pop('valuation_quote_execution'),
                 lambda o:o.update(valuation_quote_execution=copy.deepcopy(o['quote_execution'])),
                 lambda o:o['valuation_quote_execution'].update(quote_hash='0'*64),
                 lambda o:o['valuation_quote_execution'].update(source_authenticated=True),
                 lambda o:o['valuation_quote_execution'].update(quote_observed_at=T-11),
                 lambda o:o['valuation_quote_execution'].update(input_raw=self.f.raw+1))
        for change in attacks:
            bad=copy.deepcopy(original_outcome);change(bad)
            self.f.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(bad),row[0]))
            self.assert_all_consumers_reject()
            self.f.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(row[1],row[0]))
            self.assertEqual(list(self.f.ledger.db.iterdump()),original)

    def assert_all_consumers_reject(self):
        with self.assertRaises(RecoveryRequired):read_checkpoint(self.f.ledger.db)
        self.assertEqual(paper_status(self.f.path,now=T+2)['status'],'RECOVERY_REQUIRED')
        with self.assertRaises(RecoveryRequired):experiment_report(self.f.path,now=T+2)
        reopened=Ledger(self.f.path,must_exist=True)
        try:
            with self.assertRaises(RecoveryRequired):read_checkpoint(reopened.db)
            row=reopened.db.execute('SELECT payload FROM events ORDER BY seq DESC LIMIT 1').fetchone()
            e=json.loads(row[0]);before=list(reopened.db.iterdump())
            with self.assertRaises(ValueError):reopened.apply(e,self.f.cfg,qe.bind_transition(e,()),engine.initial_state)
            self.assertEqual(list(reopened.db.iterdump()),before)
        finally:reopened.close()

    def test_rehashed_exit_source_identity_and_inventory_attacks_fail_closed(self):
        e=self.s.build(context=replace(self.s.ctx,known_hazards=('KNOWN_ADVERSE_EVIDENCE',)))['event']
        self.s.apply(e)
        original=list(self.f.ledger.db.iterdump())
        for field,value in (('current_quantity_raw',self.f.raw+1),('mint_decimals',7),
                            ('entry_authorized',True),('exit_contract_version',True)):
            bad=copy.deepcopy(e);bad[field]=value
            self.check_corrupt_event(e,bad,original)
        for field,value in (('quote_hash','0'*64),('mint_hash','0'*64),('rpc_source_id','rebound'),
                            ('quote_source_id','rebound'),('mint_at',T),('mint_slot',101)):
            bad=copy.deepcopy(e);bad['source_evidence'][field]=value
            self.check_corrupt_event(e,bad,original)

    def check_corrupt_event(self,event,bad,original,all_consumers=False):
        bad['event_id']='paper-exit:'+digest({k:v for k,v in bad.items() if k!='event_id'})
        # Keep SQL identity and original hash coherent: rejection must come from
        # schema/source/inventory replay, rather than only a stale checksum.
        db=self.f.ledger.db
        db.execute('UPDATE events SET event_id=?,payload=?,payload_hash=? WHERE event_id=?',
                   (bad['event_id'],canonical(bad),digest(bad),event['event_id']))
        db.execute('UPDATE outcomes SET event_id=? WHERE event_id=?',(bad['event_id'],event['event_id']))
        with self.assertRaises(RecoveryRequired):read_checkpoint(db)
        if all_consumers:self.assert_all_consumers_reject()
        db.execute('UPDATE events SET event_id=?,payload=?,payload_hash=? WHERE event_id=?',
                   (event['event_id'],canonical(event),digest(event),bad['event_id']))
        db.execute('UPDATE outcomes SET event_id=? WHERE event_id=?',(event['event_id'],bad['event_id']))
        self.assertEqual(list(db.iterdump()),original)

    def test_new_kind_never_rebinds_buy_and_default_view_does_not_opt_in(self):
        e=self.s.build()['event']
        with self.assertRaises(ValueError):_event_json(canonical(e),config())
        buy=self.f.ledger.db.execute("SELECT event_id,payload FROM events WHERE ts=?",(T,)).fetchone()
        original=list(self.f.ledger.db.iterdump())
        forged=e|{'event_id':buy[0],'ts':T}
        self.f.ledger.db.execute('UPDATE events SET payload=?,payload_hash=? WHERE event_id=?',
            (canonical(forged),digest(forged),buy[0]))
        with self.assertRaises(RecoveryRequired):read_checkpoint(self.f.ledger.db)
        self.f.ledger.db.execute('UPDATE events SET payload=?,payload_hash=? WHERE event_id=?',
            (buy[1],digest(json.loads(buy[1])),buy[0]))
        self.assertEqual(list(self.f.ledger.db.iterdump()),original)
