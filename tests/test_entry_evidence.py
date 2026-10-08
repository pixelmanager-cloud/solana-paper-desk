import copy,json,tempfile,unittest
from pathlib import Path
from desk.entry_evidence import evaluate
from desk.evidence import EvidenceStore
from desk.model import digest

class EntryEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=EvidenceStore(Path(self.tmp.name)/'evidence.sqlite')
        fixture=json.loads((Path(__file__).resolve().parents[1]/'fixtures/mainnet-holder-snapshot.json').read_text())
        e=fixture['enumeration'];r=fixture['rpc']['response']
        self.saved={'method':'getMultipleAccounts','params':[[e['mint'],*[x['address'] for x in e['accounts']]],{'encoding':'base64','commitment':'confirmed','minContextSlot':e['indexed_slot_max']}],'result':r}
        key=self.store.save(self.saved)
        mintkey=self.store.save({'method':'getAccountInfo','params':[e['mint'],{'encoding':'base64','commitment':'confirmed'}],'result':{'value':r['value'][0]}})
        self.report={'mint':e['mint'],'mint_evidence_hash':mintkey,'holder_snapshot':{'evidence_hash':key},'verified_pools':[]}
    def check(self):
        report=copy.deepcopy(self.report);report['report_hash']=digest(report)
        return evaluate(report,self.store)
    def test_real_holder_capture_drives_components_not_summary_flags(self):
        self.report['holder_snapshot']['verified']=False
        result=self.check()
        self.assertEqual(result['gates']['holder_snapshot']['status'],'VERIFIED_COMPONENT')
        self.assertEqual(result['gates']['token_controls']['status'],'VERIFIED_COMPONENT')
        self.assertGreater(result['metrics']['holder_owner_count'],0)
        self.assertFalse(result['eligible_for_trading'])
        self.assertIn('CURRENT_HOLDER_BUNDLE_EXPOSURE_UNVERIFIED',result['reasons'])
    def test_missing_raw_data_cannot_be_replaced_by_approval_flags(self):
        self.report['holder_snapshot']={'verified':True,'evidence_hash':'0'*64}
        self.report['eligible_for_trading']=True
        result=self.check();self.assertEqual(result['gates']['holder_snapshot']['status'],'BLOCKED')
        self.assertNotIn('gross_top10_supply_pct',result['metrics'])
    def test_wrong_mint_reference_is_blocked(self):
        self.report['mint']='wrong';result=self.check()
        self.assertEqual(result['gates']['holder_snapshot']['status'],'BLOCKED')
        self.assertEqual(result['gates']['token_controls']['status'],'BLOCKED')
    def test_partial_positive_supply_is_blocked_even_with_valid_hash(self):
        self.saved['params'][0].pop();self.saved['result']['value'].pop()
        self.report['holder_snapshot']['evidence_hash']=self.store.save(self.saved)
        self.assertIn('ATOMIC_HOLDER_SUPPLY_NOT_RECONCILED',self.check()['reasons'])
    def test_changed_summary_hash_is_blocked(self):
        self.report['report_hash']='0'*64
        self.assertIn('REPORT_HASH_MISSING_OR_MISMATCH',evaluate(self.report,self.store)['reasons'])
    def test_missing_store_is_fail_closed(self):
        self.assertIn('TOKEN_RAW_EVIDENCE_UNAVAILABLE',evaluate(self.report,None)['reasons'])
