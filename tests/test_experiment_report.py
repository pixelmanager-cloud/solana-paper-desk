"""Known synthetic trade lifecycle and read-only journal/checkpoint attacks."""
import copy
import io
import json
import sqlite3
import subprocess
import sys
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from desk.engine import initial_state, transition
from desk.experiment_report import experiment_report, main
from desk.ledger import Ledger
from desk.model import canonical, digest
from desk.paper_checkpoint import RecoveryRequired
from tests.helpers import T, config, event


class ExperimentReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'experiment.sqlite';self.cfg=config()
        self.ledger=Ledger(self.path);self.addCleanup(lambda:self.ledger.close())

    def apply(self, e):
        return self.ledger.apply(e,self.cfg,transition,initial_state)

    def report(self, now=T+10, path=None):
        with patch('socket.socket',side_effect=AssertionError('No network')):
            return experiment_report(path or self.path,now=now)

    def dump(self):
        with sqlite3.connect(self.path) as c:return list(c.iterdump())

    def test_partial_then_full_exit_restart_counts_one_closed_trade_and_net_fees(self):
        outcomes=self.apply(event())+self.apply(event(T+5,reserve_sol='160'))
        before=self.report(T+5)
        self.assertEqual(before['closed_trade_count'],0)
        self.assertEqual(D(before['closed_trade_realized_pnl_sol']),0)
        self.assertEqual(before['net_realized_pnl_sol'],before['open_trade_realized_pnl_sol'])
        self.assertEqual(before['partial_sell_fill_count'],1)
        self.assertEqual(before['open_position_count'],1)
        self.assertEqual(before['data_provenance'],'SYNTHETIC_ONLY')
        self.ledger.close();self.ledger=Ledger(self.path,must_exist=True)
        outcomes+=self.apply(event(T+10,reserve_sol='160',danger=True))
        saved=self.dump();result=self.report()
        fills=[o for o in outcomes if o['type']=='fill']
        self.assertEqual(result['closed_trade_count'],1)
        self.assertEqual(result['partial_sell_fill_count'],1)
        self.assertEqual((result['buy_fill_count'],result['sell_fill_count']),(1,2))
        self.assertEqual(result['open_position_count'],0)
        pnl=sum((D(o['realized_pnl_sol']) for o in fills if o['side']=='sell'),D(0))
        fees=sum((D(o['fee_sol']) for o in fills),D(0))
        self.assertAlmostEqual(D(result['net_realized_pnl_sol']),pnl,places=25)
        self.assertEqual(D(result['recorded_fill_fees_sol']),fees)
        self.assertNotEqual(D(result['net_realized_pnl_sol']),pnl-fees)
        self.assertEqual(result['config_hash'],digest(self.cfg))
        self.assertEqual(len(result['implementation_hash']),64)
        self.assertEqual(result['event_span_seconds'],10)
        self.assertEqual(result['observation_span_seconds'],10)
        self.assertEqual(result['profitability_verdict'],'NOT_ASSESSED')
        self.assertEqual(saved,self.dump())

    def test_open_expired_inventory_and_blocked_exit_are_not_closed_success(self):
        self.apply(event())
        self.apply(event(T+5,route_available=False))
        r=self.report(T+100)
        self.assertEqual(r['closed_trade_count'],0)
        self.assertEqual(r['blocked_exit_outcome_count'],1)
        self.assertEqual(r['unvalued_or_blocked_position_count'],1)
        self.assertIsNone(r['open_inventory'][0]['model_unrealized_pnl_sol'])
        self.assertFalse(r['open_inventory'][0]['valuation_verified'])
        self.assertIn('modeled equity',r['drawdown_scope'])

    def test_non_synthetic_and_missing_provenance_are_unverified_claims(self):
        self.apply(event(provenance='helius'))
        self.apply(event(T+60,mint='SYNTHETIC_B',provenance='UNKNOWN'))
        r=self.report(T+60)
        self.assertEqual(r['data_provenance_counts'],{'synthetic':0,'non_synthetic_claims':1,'unknown':1})
        self.assertEqual(r['data_provenance'],'LIVE_CLAIMS_OR_UNKNOWN_UNVERIFIED')
        self.assertEqual(r['reject_outcome_count'],2)
        self.assertEqual(r['closed_trade_count'],0)

    def test_missing_or_partial_checkpoint_and_config_hash_reject_preserving_originals(self):
        self.apply(event());original=self.dump()
        for sql in ('DELETE FROM state', "UPDATE state SET payload='{}'", 'DELETE FROM events',
                    "UPDATE metadata SET value='bad' WHERE key='config_hash'",
                    "UPDATE metadata SET value='bad' WHERE key='implementation_hash'"):
            with self.subTest(sql=sql):
                target=Path(self.tmp.name)/('attack-'+str(len(sql))+'.sqlite')
                with sqlite3.connect(target) as c:
                    self.ledger.db.backup(c);c.execute(sql);c.commit()
                    damaged=list(c.iterdump())
                with self.assertRaises(RecoveryRequired):self.report(path=target)
                with sqlite3.connect(target) as c:self.assertEqual(damaged,list(c.iterdump()))
        self.assertEqual(original,self.dump())

    def test_hash_journal_fill_and_checkpoint_substitutions_reject(self):
        self.apply(event())
        original=self.dump()
        for attack in ('event_hash','fill_quantity','huge_quantity','realized','state_qty'):
            with self.subTest(attack=attack):
                target=Path(self.tmp.name)/('attack-'+attack+'.sqlite')
                c=sqlite3.connect(target);self.ledger.db.backup(c)
                if attack=='event_hash':c.execute("UPDATE events SET payload_hash=?",('0'*64,))
                elif attack in ('fill_quantity','huge_quantity'):
                    row=c.execute('SELECT payload FROM outcomes LIMIT 1').fetchone()
                    o=json.loads(row[0]);o['quantity']='1e999' if attack=='huge_quantity' else '1'
                    c.execute('UPDATE outcomes SET payload=?',(canonical(o),))
                else:
                    row=c.execute('SELECT payload FROM state').fetchone();s=json.loads(row[0])
                    if attack=='realized':s['realized_pnl']='123'
                    else:s['positions']['SYNTHETIC_A']['qty']='1'
                    c.execute('UPDATE state SET payload=?',(canonical(s),))
                c.commit();c.close()
                with self.assertRaises(RecoveryRequired):self.report(path=target)
        self.assertEqual(original,self.dump())

    def test_bounded_reads_and_missing_database_do_not_create_schema(self):
        absent=Path(self.tmp.name)/'absent.sqlite'
        with self.assertRaises(RecoveryRequired):experiment_report(absent,now=T)
        self.assertFalse(absent.exists())
        self.apply(event());before=self.dump()
        with patch('desk.experiment_report.MAX_ROWS',0),self.assertRaises(RecoveryRequired):self.report()
        with patch('desk.experiment_report.MAX_BYTES',1),self.assertRaises(RecoveryRequired):self.report()
        self.assertEqual(before,self.dump())

    def test_original_raw_observation_span_and_out_of_order_reject_are_distinct(self):
        self.ledger.record_raw('first',T-100,1,{'provenance':'UNKNOWN'})
        self.ledger.record_raw('last',T-10,2,{'provenance':'UNKNOWN'})
        self.apply(event());self.apply(event(T-1))
        result=self.report()
        self.assertEqual(result['event_span_seconds'],1)
        self.assertEqual(result['raw_observation_count'],2)
        self.assertEqual(result['observation_span_seconds'],90)
        self.assertEqual(result['observation_span_basis'],'RAW_RECEIPT_TIMES')
        self.assertEqual(result['data_provenance_counts']['unknown'],2)
        self.assertEqual(result['data_provenance'],'LIVE_CLAIMS_OR_UNKNOWN_UNVERIFIED')
        self.assertEqual(result['reject_outcome_count'],1)
        self.assertEqual(result['closed_trade_count'],0)

    def test_cli_json_success_and_nonzero_corruption(self):
        self.apply(event());out=io.StringIO()
        with redirect_stdout(out):code=main([str(self.path),'--now',str(T)])
        self.assertEqual(code,0);self.assertEqual(json.loads(out.getvalue())['status'],'REPORTED')
        child=subprocess.run([sys.executable,'-m','desk.experiment_report',str(self.path),'--now',str(T)],
            cwd=Path(__file__).resolve().parents[1],env={'PATH':os.defpath},capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0,child.stderr)
        self.assertEqual(json.loads(child.stdout)['status'],'REPORTED')
        self.ledger.db.execute("UPDATE state SET payload='{}'")
        out=io.StringIO()
        with redirect_stdout(out):code=main([str(self.path),'--now',str(T)])
        self.assertEqual(code,2);self.assertEqual(json.loads(out.getvalue())['status'],'RECOVERY_REQUIRED')

    def copied_attack(self, name, mutate):
        target=Path(self.tmp.name)/(name+'.sqlite')
        with sqlite3.connect(target) as c:
            self.ledger.db.backup(c);mutate(c);c.commit()
            before=list(c.iterdump())
        with self.assertRaises(RecoveryRequired):self.report(path=target)
        with sqlite3.connect(target) as c:self.assertEqual(before,list(c.iterdump()))

    def test_review_single_open_cost_basis_corruption_and_partial_remaining_basis(self):
        self.apply(event());original=self.dump()
        def corrupt_cost(c):
            state=json.loads(c.execute('SELECT payload FROM state').fetchone()[0])
            state['positions']['SYNTHETIC_A']['cost_left']='0'
            c.execute('UPDATE state SET payload=?',(canonical(state),))
        self.copied_attack('open-cost-zero',corrupt_cost)
        self.assertEqual(original,self.dump())
        fills=self.apply(event(T+5,reserve_sol='160'))
        r=self.report(T+5);inventory=r['open_inventory'][0]
        self.assertEqual(r['partial_sell_fill_count'],1)
        with sqlite3.connect(self.path) as c:
            buy=json.loads(c.execute("SELECT payload FROM outcomes WHERE json_extract(payload,'$.side')='buy'").fetchone()[0])
        sell=next(o for o in fills if o.get('side')=='sell')
        entry=D(buy['amount_sol'])+D(buy['fee_sol'])
        remaining=entry*(D(buy['quantity'])-D(sell['quantity']))/D(buy['quantity'])
        self.assertAlmostEqual(D(inventory['cost_left_sol']),remaining,places=25)
        self.assertAlmostEqual(D(r['net_realized_pnl_sol']),D(sell['proceeds_sol'])-(entry-remaining),places=25)
        original=self.dump();self.copied_attack('partial-cost-zero',corrupt_cost)
        def corrupt_initial(c):
            state=json.loads(c.execute('SELECT payload FROM state').fetchone()[0])
            state['positions']['SYNTHETIC_A']['initial_cost']='0'
            c.execute('UPDATE state SET payload=?',(canonical(state),))
        self.copied_attack('partial-initial-cost',corrupt_initial)
        self.assertEqual(original,self.dump())

    def test_review_paired_closed_pnl_checkpoint_forgery_rejects_cash_loss_as_profit(self):
        self.apply(event());self.apply(event(T+5,danger=True))
        baseline=self.report(T+5)
        self.assertLess(D(baseline['net_realized_pnl_sol']),0)
        self.assertEqual(baseline['closed_trade_count'],1)
        self.assertAlmostEqual(D(baseline['closed_trade_realized_pnl_sol']),
                               D(json.loads(self.ledger.db.execute('SELECT payload FROM state').fetchone()[0])['cash'])-D(self.cfg['initial_equity_sol']),places=25)
        original=self.dump()
        def forge(c):
            seq,payload=c.execute("SELECT seq,payload FROM outcomes WHERE json_extract(payload,'$.side')='sell'").fetchone()
            outcome=json.loads(payload);outcome['realized_pnl_sol']=str(D(outcome['realized_pnl_sol'])+1)
            c.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(outcome),seq))
            state=json.loads(c.execute('SELECT payload FROM state').fetchone()[0])
            state['realized_pnl']=str(D(state['realized_pnl'])+1)
            c.execute('UPDATE state SET payload=?',(canonical(state),))
        self.copied_attack('paired-closed-profit',forge)
        self.assertEqual(original,self.dump())

    def test_partial_paired_trade_pnl_and_remaining_basis_attack_rejects(self):
        self.apply(event());self.apply(event(T+5,reserve_sol='160'));original=self.dump()
        def forge(c):
            seq,payload=c.execute("SELECT seq,payload FROM outcomes WHERE json_extract(payload,'$.side')='sell'").fetchone()
            outcome=json.loads(payload);outcome['realized_pnl_sol']=str(D(outcome['realized_pnl_sol'])+1)
            c.execute('UPDATE outcomes SET payload=? WHERE seq=?',(canonical(outcome),seq))
            state=json.loads(c.execute('SELECT payload FROM state').fetchone()[0])
            state['realized_pnl']=str(D(state['realized_pnl'])+1)
            state['positions']['SYNTHETIC_A']['trade_pnl']=str(D(state['positions']['SYNTHETIC_A']['trade_pnl'])+1)
            state['positions']['SYNTHETIC_A']['cost_left']='0'
            c.execute('UPDATE state SET payload=?',(canonical(state),))
        self.copied_attack('paired-partial-profit-basis',forge)
        self.assertEqual(original,self.dump())
