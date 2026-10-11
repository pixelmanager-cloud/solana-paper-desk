"""Actual synthetic quote ledger accounting; no providers or checkpoint repairs."""
import unittest
from decimal import Decimal as D
from desk.experiment_report import experiment_report
from desk.paper_checkpoint import RecoveryRequired
from desk.model import canonical
from desk import quote_execution as q
from tests import test_quote_execution as fixtures
from tests.helpers import T

class QuoteReportTests(unittest.TestCase):
    def lifecycle(self, decimals=6):
        c=fixtures.QuoteExecutionTests();self.addCleanup(c.doCleanups);c.setUp()
        buy=c.quote('buy',10_000_000,1_000_003,decimals=decimals)
        raw=q.output_raw(buy,c.cfg)
        c.apply(c.market(),(buy,c.quote('sell',raw,10_000_000,decimals=decimals)))
        out=c.apply(c.market(T+1),(c.quote('sell',raw,20_000_000,at=T+1,decimals=decimals),
            c.quote('sell',raw*3//10,6_000_000,at=T+1,decimals=decimals)))
        return c,raw,next(x for x in out if x.get('side')=='sell')
    def test_raw_floor_partial_remaining_basis_pnl_and_no_mutation(self):
        c,raw,sell=self.lifecycle()
        before=list(c.ledger.db.iterdump());report=experiment_report(c.path,now=T+1)
        self.assertEqual(before,list(c.ledger.db.iterdump()))
        self.assertEqual(report['open_position_count'],1);self.assertEqual(report['partial_sell_fill_count'],1)
        self.assertEqual(report['net_realized_pnl_sol'],sell['realized_pnl_sol'])
        self.assertEqual(D(report['open_inventory'][0]['cost_left_sol']),D(c.state()['positions'][c.mint]['cost_left']))
        self.assertEqual(q.raw_quantity(report['open_inventory'][0]['quantity'],6),raw-raw*3//10)
    def test_high_decimals_nonzero_raw_dust_stays_open_then_closes(self):
        for decimals in (27,255):
            with self.subTest(decimals=decimals):
                c,raw,sell=self.lifecycle(decimals)
                r=experiment_report(c.path,now=T+1)
                self.assertEqual(r['closed_trade_count'],0);self.assertEqual(r['open_position_count'],1)
                remaining=raw-raw*3//10
                c.apply(c.market(T+2,danger=True),(c.quote('sell',remaining,9_000_000,at=T+2,decimals=decimals),))
                closed=experiment_report(c.path,now=T+2)
                self.assertEqual(closed['closed_trade_count'],1);self.assertEqual(closed['open_position_count'],0)
    def test_corrupt_remaining_basis_quantity_and_declared_pnl_reject(self):
        for attack in ('basis','quantity','pnl'):
            with self.subTest(attack=attack):
                c,raw,sell=self.lifecycle()
                if attack=='pnl':
                    row=c.ledger.db.execute("SELECT seq,payload FROM outcomes WHERE json_extract(payload, '$.side')='sell' ORDER BY seq DESC LIMIT 1").fetchone()
                    import json
                    outcome=json.loads(row[1]);outcome['realized_pnl_sol']=str(D(outcome['realized_pnl_sol'])+D('.001'))
                    c.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(outcome),row[0]))
                else:
                    state=c.state();p=state['positions'][c.mint]
                    p['cost_left' if attack=='basis' else 'qty']=str(D(p['cost_left' if attack=='basis' else 'qty'])/2)
                    c.ledger.db.execute('UPDATE state SET payload=?',(canonical(state),))
                before=list(c.ledger.db.iterdump())
                with self.assertRaises(RecoveryRequired):experiment_report(c.path,now=T+1)
                self.assertEqual(before,list(c.ledger.db.iterdump()))
