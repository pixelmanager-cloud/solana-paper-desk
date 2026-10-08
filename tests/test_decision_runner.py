import json,sqlite3,tempfile,unittest
from pathlib import Path
from desk.decision_runner import consume
class DecisionRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);p=Path(self.tmp.name);self.source=p/'research.sqlite';self.dest=p/'decisions.sqlite'
        with sqlite3.connect(self.source) as c:c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
    def add(self,key,status='COMPLETE',report=None):
        if report is None:report={'mint':'mint','observed_at':100,'eligible_for_trading':True,'findings':[],'unknowns':[]}
        with sqlite3.connect(self.source) as c:c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',(key,'mint',99,status,json.dumps(report)))
    def run_once(self,**kw):return consume(self.source,self.dest,now=100,**kw)
    def test_asserted_approval_is_rejected_and_restart_does_not_duplicate(self):
        self.add('a');r=self.run_once();self.assertEqual(r['consumed'],1);self.assertEqual(r['decisions'][0]['decision'],'REJECT')
        self.assertIn('TOKEN_RAW_EVIDENCE_UNAVAILABLE',r['decisions'][0]['reasons']);self.assertEqual(self.run_once()['consumed'],0)
    def test_earlier_running_job_is_not_skipped_when_it_finishes_later(self):
        self.add('first','RUNNING');self.add('second');self.assertEqual(self.run_once()['decisions'][0]['scan_id'],'second')
        with sqlite3.connect(self.source) as c:c.execute("UPDATE scans SET status='COMPLETE' WHERE id='first'")
        self.assertEqual(self.run_once()['decisions'][0]['scan_id'],'first')
    def test_failed_and_stale_jobs_keep_rejection_reasons(self):
        self.add('failed','FAILED',{'error':'provider failed'});r=self.run_once()['decisions'][0]
        self.assertIn('INVESTIGATION_FAILED',r['reasons']);self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY',r['reasons'])
    def test_malformed_batch_rolls_back_all_decisions(self):
        self.add('a');self.add('b',report={'findings':'bad'})
        with self.assertRaises(ValueError):self.run_once()
        with sqlite3.connect(self.dest) as c:self.assertEqual(c.execute('SELECT count(*) FROM decisions').fetchone()[0],0)
    def test_bounded_batches_and_saved_source_hash(self):
        from desk.model import digest
        self.add('a');self.add('b');self.assertEqual(self.run_once(limit=1)['consumed'],1);self.assertEqual(self.run_once(limit=1)['consumed'],1)
        with sqlite3.connect(self.dest) as c:
            for source_hash,payload in c.execute('SELECT source_hash,source_payload FROM decisions'):self.assertEqual(digest(json.loads(payload)),source_hash)
    def test_missing_source_and_same_destination_rejected(self):
        with self.assertRaises(ValueError):consume(self.source,self.source)
        self.source.unlink()
        with self.assertRaises(ValueError):self.run_once()
        self.assertFalse(self.dest.exists())
    def test_existing_rejections_are_preserved_with_versioned_evaluation(self):
        self.add('a');self.run_once()
        with sqlite3.connect(self.dest) as c:
            c.execute("UPDATE decisions SET decision='original historical decision'")
            c.execute('DELETE FROM decision_evaluations')
        self.assertEqual(self.run_once()['consumed'],1)
        with sqlite3.connect(self.dest) as c:
            self.assertEqual(c.execute('SELECT decision FROM decisions').fetchone()[0],'original historical decision')
            self.assertEqual(c.execute('SELECT count(*) FROM decision_evaluations').fetchone()[0],1)
    def test_dashboard_projection_does_not_create_database(self):
        from desk.decision_runner import recent_decisions
        self.assertEqual(recent_decisions(self.dest)['status'],'NOT_CONFIGURED');self.assertFalse(self.dest.exists())
        self.add('a');self.run_once();r=recent_decisions(self.dest)
        self.assertEqual(r['status'],'EVIDENCE_GATES_CONNECTED');self.assertFalse(r['automatic_entry_enabled'])
        self.assertIn('bundle_exposure',r['decisions'][0]['entry_evidence']['gates'])
