import json,tempfile,unittest
from pathlib import Path
from desk.paper_view import paper_status
from desk.ledger import Ledger
from desk.engine import transition,initial_state
from tests.helpers import config,event,T

class PaperViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'active-paper.sqlite';self.cfg=config()
    def tearDown(self):self.tmp.cleanup()
    def enter(self):
        ledger=Ledger(self.path)
        ledger.apply(event(),self.cfg,transition,initial_state);ledger.close()
    def test_missing_ledger_not_created_or_reported_as_zero(self):
        r=paper_status(self.path,now=T)
        self.assertEqual(r['status'],'NOT_CONFIGURED');self.assertIsNone(r['cash_sol']);self.assertFalse(self.path.exists())
    def test_empty_ledger_has_no_fabricated_balance(self):
        Ledger(self.path).close();r=paper_status(self.path,now=T)
        self.assertEqual(r['status'],'EMPTY_LEDGER');self.assertIsNone(r['realized_pnl_sol'])
    def test_position_survives_reopen_and_keeps_synthetic_label(self):
        self.enter();r=paper_status(self.path,now=T+1)
        self.assertEqual(r['status'],'LEDGER_PRESENT');self.assertEqual(len(r['positions']),1)
        self.assertEqual(r['positions'][0]['provenance'],'SYNTHETIC_TEST_ONLY')
        self.assertIsNotNone(r['estimated_equity_sol']);self.assertFalse(r['automatic_entry_enabled'])
        self.assertEqual(r['runner_status'],'NOT_CONNECTED');self.assertFalse(r['positions'][0]['valuation_verified'])
    def test_stale_marks_withhold_equity_and_unrealized_pnl(self):
        self.enter();r=paper_status(self.path,now=T+self.cfg['price_ttl_seconds']+1)
        self.assertIsNone(r['estimated_equity_sol']);self.assertIsNone(r['positions'][0]['unrealized_pnl_sol'])
        self.assertEqual(r['positions'][0]['mark_status'],'STALE')
    def test_future_mark_is_not_fresh(self):
        self.enter();self.assertIsNone(paper_status(self.path,now=T-1)['estimated_equity_sol'])
    def test_blocked_exit_retains_position_and_reason(self):
        self.enter();ledger=Ledger(self.path)
        ledger.apply(event(T+1,danger=True,provenance='MAINNET_OBSERVATION'),self.cfg,transition,initial_state);ledger.close()
        r=paper_status(self.path,now=T+1)
        self.assertEqual(len(r['positions']),1);self.assertEqual(r['positions'][0]['mark_status'],'UNVERIFIED_EXIT')
        self.assertIsNone(r['estimated_equity_sol']);self.assertTrue(any(o['outcome']['type']=='blocked_exit' for o in r['recent_outcomes']))
    def test_corrupt_db_is_redacted_not_empty_success(self):
        self.path.write_text('secret sensitive data')
        r=paper_status(self.path,now=T);self.assertEqual(r['status'],'LEDGER_UNAVAILABLE');self.assertNotIn('secret',json.dumps(r))
    def test_reader_does_not_modify_checkpoint(self):
        self.enter();before=self.path.read_bytes();paper_status(self.path,now=T)
        self.assertEqual(self.path.read_bytes(),before)
