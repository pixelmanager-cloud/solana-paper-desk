"""Actual collector -> exit adapter -> Ledger -> read-only report fixtures."""
import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch
from desk.experiment_report import experiment_report
from desk.model import canonical,digest
from desk.paper_checkpoint import RecoveryRequired
from tests import test_paper_exit_adapter as fixtures
from tests import test_quote_execution as quote_fixtures
from tests.test_paper_experimental_scoring import experimental
from tests.helpers import T

class ExitReportTests(unittest.TestCase):
    def setup_case(self,risk=False):
        c=fixtures.ExitObservationTests()
        if risk:
            def buy(f):
                f.cfg['experimental_policy_version']=1
                e=experimental(mint=f.mint,pool=f.pool,taker=f.wallet)
                return next(row for row in f.apply(e,(f.buy,f.exit)) if row['type']=='fill')
            with patch.object(quote_fixtures.QuoteExecutionTests,'buy_fill',buy):c.setUp()
        else:c.setUp()
        self.addCleanup(c.doCleanups)
        return c
    def sell(self,c):
        e=c.build(context=replace(c.ctx,known_hazards=('KNOWN_ADVERSE_EVIDENCE',)))['event']
        self.assertIsNotNone(e);out=c.apply(e)
        self.assertTrue(any(x.get('side')=='sell' for x in out))
        return e,next(x for x in out if x.get('side')=='sell')
    def test_actual_exit_only_sale_report_risks_sources_and_read_only(self):
        c=self.setup_case(risk=True);e,fill=self.sell(c)
        before=list(c.f.ledger.db.iterdump());r=experiment_report(c.f.path,now=T+1)
        self.assertEqual(before,list(c.f.ledger.db.iterdump()))
        self.assertEqual(r['closed_trade_count'],1);self.assertEqual(r['open_position_count'],0)
        self.assertEqual(r['buy_fill_count'],1);self.assertEqual(r['quote_exit_observation_count'],1)
        label=r['execution']['fills'][-1]
        self.assertEqual(label['exit_source_evidence'],e['source_evidence'])
        self.assertEqual(label['quote_hash'],fill['quote_execution']['quote_hash'])
        self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',label['risk_flags'])
        self.assertEqual(r['execution']['status'],'EXECUTION_UNVERIFIED')
        self.assertFalse(label['actual_fill_verified'])
    def test_exit_event_without_sale_retains_existing_inventory(self):
        c=self.setup_case();e=c.build()['event'];c.apply(e,())
        r=experiment_report(c.f.path,now=T+1)
        self.assertEqual(r['open_position_count'],1);self.assertEqual(r['sell_fill_count'],0)
        self.assertEqual(r['quote_exit_observation_count'],1)
    def test_forged_buy_and_rehashed_exit_bindings_reject_without_mutation(self):
        for attack in ('buy','quantity','source','entry_authorized'):
            with self.subTest(attack=attack):
                c=self.setup_case();e,fill=self.sell(c)
                if attack=='buy':
                    row=c.f.ledger.db.execute("SELECT seq,payload FROM outcomes WHERE json_extract(payload,'$.side')='sell'").fetchone()
                    o=json.loads(row[1]);o['side']='buy'
                    c.f.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(o),row[0]))
                else:
                    bad=copy.deepcopy(e)
                    if attack=='quantity':bad['current_quantity_raw']+=1
                    elif attack=='source':bad['source_evidence']['quote_hash']='0'*64
                    else:bad['entry_authorized']=True
                    bad['event_id']='paper-exit:'+digest({k:v for k,v in bad.items() if k!='event_id'})
                    c.f.ledger.db.execute('UPDATE events SET event_id=?,payload=?,payload_hash=? WHERE event_id=?',
                        (bad['event_id'],canonical(bad),digest(bad),e['event_id']))
                    c.f.ledger.db.execute('UPDATE outcomes SET event_id=? WHERE event_id=?',(bad['event_id'],e['event_id']))
                before=list(c.f.ledger.db.iterdump())
                with self.assertRaises(RecoveryRequired):experiment_report(c.f.path,now=T+1)
                self.assertEqual(before,list(c.f.ledger.db.iterdump()))
