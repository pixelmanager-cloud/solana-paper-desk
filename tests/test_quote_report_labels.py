"""Offline original-bound quote roundtrip labels, not fill authentication."""
import json
import unittest
from desk.experiment_report import experiment_report
from desk.model import canonical, digest
from desk.paper_checkpoint import RecoveryRequired
from tests import test_quote_execution as fixtures
from tests.helpers import T
from tests.test_paper_experimental_scoring import experimental

class QuoteReportLabelTests(unittest.TestCase):
    def setUp(self):
        self.case=fixtures.QuoteExecutionTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
    def roundtrip(self):
        c=self.case;c.cfg['experimental_policy_version']=1
        buy_event=experimental(mint=c.mint,pool=c.pool,taker=c.wallet)
        buy=next(x for x in c.apply(buy_event,(c.buy,c.exit)) if x['type']=='fill')
        sell_event=experimental(ts=T+1,mint=c.mint,pool=c.pool,taker=c.wallet,danger=True)
        sell=next(x for x in c.apply(sell_event,(c.quote('sell',c.raw,10_000_000,at=T+1),)) if x['type']=='fill')
        return buy_event,buy,sell_event,sell
    def test_closed_roundtrip_carries_unverified_assumptions_risks_and_original_refs(self):
        buy_event,buy,sell_event,sell=self.roundtrip();c=self.case
        before=list(c.ledger.db.iterdump());r=experiment_report(c.path,now=T+1)
        self.assertEqual(before,list(c.ledger.db.iterdump()))
        execution=r['execution'];self.assertEqual(execution['status'],'EXECUTION_UNVERIFIED')
        self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',execution['risk_flags'])
        self.assertEqual(r['open_position_count'],0);self.assertEqual(r['closed_trade_count'],1)
        self.assertEqual(r['profitability_verdict'],'NOT_ASSESSED')
        self.assertEqual(len(execution['fills']),2)
        for label,event,fill in zip(execution['fills'],(buy_event,sell_event),(buy,sell)):
            record=fill['quote_execution']
            for key in ('source_authenticated','transaction_verified','actual_fill_verified'):
                self.assertIs(execution[key],False);self.assertIs(label[key],False)
            self.assertEqual(label['assumptions'],record['assumptions'])
            self.assertEqual(label['event_hash'],digest(event))
            self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',label['risk_flags'])
            for prefix in ('quote','mint'):
                self.assertEqual(label[prefix+'_hash'],digest(json.loads(record['original_'+prefix+'_json'])))
                self.assertEqual(label[prefix+'_source_id'],record[prefix+'_source_id'])
                self.assertEqual(label[prefix+'_observed_at'],record[prefix+'_observed_at'])
    def test_corrupt_closed_entry_risk_summary_rejects_without_repair(self):
        self.roundtrip();c=self.case
        row=c.ledger.db.execute("SELECT seq,payload FROM outcomes WHERE json_extract(payload,'$.side')='buy'").fetchone()
        fill=json.loads(row[1]);fill['entry_policy']['risk_flags']=[]
        c.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(fill),row[0]))
        before=list(c.ledger.db.iterdump())
        with self.assertRaises(RecoveryRequired):experiment_report(c.path,now=T+1)
        self.assertEqual(before,list(c.ledger.db.iterdump()))
    def test_quote_label_cannot_promote_original_record_authentication(self):
        c=self.case;c.buy_fill()
        row=c.ledger.db.execute("SELECT seq,payload FROM outcomes WHERE json_extract(payload,'$.side')='buy'").fetchone()
        fill=json.loads(row[1]);fill['quote_execution']['actual_fill_verified']=True
        c.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(fill),row[0]))
        with self.assertRaises(RecoveryRequired):experiment_report(c.path,now=T)
