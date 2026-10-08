import json,sqlite3,tempfile,unittest
from pathlib import Path
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.model import digest
from desk.ownership_worker import advance,saved_progress
from desk.decision_runner import consume,recent_decisions

class OwnershipWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);root=Path(self.tmp.name)
        self.db=root/'research.sqlite';self.evidence=root/'evidence.sqlite';self.journal=root/'decisions.sqlite'
        self.store=EvidenceStore(self.evidence)
        fixture=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-holder-snapshot.json').read_text())
        self.mint=fixture['enumeration']['mint'];account=fixture['rpc']['response']['value'][0]
        self.mintkey=self.store.save({'method':'getAccountInfo','params':[self.mint,{'encoding':'base64','commitment':'confirmed'}],'result':{'value':account}})
        _,q=collect_history(self.mint,10,20,lambda *a:{'data':[],'paginationToken':'next'},max_pages=1,capture=self.store.save,token_accounts='none')
        self.report={'mint':self.mint,'observed_at':20,'calls':7,'findings':[],'unknowns':[],'mint_evidence_hash':self.mintkey,'history_queries':[q]}
        self.report['report_hash']=digest(self.report)
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',('scan',self.mint,20,'COMPLETE',json.dumps(self.report)))
    def test_continuation_revises_decision_without_overwriting_original(self):
        consume(self.db,self.journal,now=20,evidence_db=self.evidence)
        with sqlite3.connect(self.journal) as c:original=c.execute('SELECT decision FROM decisions').fetchone()[0]
        calls=[]
        def rpc(method,params):calls.append(params);return {'data':[]}
        r=advance(self.db,self.evidence,'scan',rpc)
        self.assertEqual(len(calls),1);self.assertEqual(r['requests_used'],8)
        self.assertFalse(r['eligible_for_trading']);self.assertFalse(r['history']['launch_verified'])
        result=consume(self.db,self.journal,now=100,evidence_db=self.evidence)
        self.assertEqual(result['consumed'],1);d=result['decisions'][0]
        self.assertEqual(d['entry_evidence']['ownership_progress_hash'],r['evidence_hash'])
        self.assertEqual(d['observed_at'],20);self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY',d['reasons'])
        self.assertEqual(consume(self.db,self.journal,now=100,evidence_db=self.evidence)['consumed'],0)
        self.assertEqual(len(recent_decisions(self.journal)['decisions']),1)
        with sqlite3.connect(self.journal) as c:self.assertEqual(c.execute('SELECT decision FROM decisions').fetchone()[0],original)
    def test_source_edit_cannot_reuse_budget(self):
        advance(self.db,self.evidence,'scan',lambda *a:{'data':[]})
        self.report['calls']=0;self.report.pop('report_hash');self.report['report_hash']=digest(self.report)
        with sqlite3.connect(self.db) as c:c.execute('UPDATE scans SET result=?',(json.dumps(self.report),))
        with self.assertRaises(ValueError):advance(self.db,self.evidence,'scan',lambda *a:self.fail('Rebound source'))
    def test_request_budget_survives_failures(self):
        def fail(*args):raise OSError('provider')
        for _ in range(11):advance(self.db,self.evidence,'scan',fail)
        r=advance(self.db,self.evidence,'scan',lambda *a:self.fail('Budget exceeded'))
        self.assertEqual(r['requests_used'],18);self.assertEqual(r['provider_calls'],0)
        self.assertEqual(r['status'],'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED')
    def test_malformed_source_does_not_make_provider_call(self):
        with sqlite3.connect(self.db) as c:c.execute("UPDATE scans SET result='{}'")
        with self.assertRaises(ValueError):advance(self.db,self.evidence,'scan',lambda *a:self.fail('Invalid source'))
