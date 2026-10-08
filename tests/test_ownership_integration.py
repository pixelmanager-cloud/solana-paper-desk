"""Synthetic legacy launch/history/bank fixtures, never live acceptance.

Every raw instruction, token balance, request manifest and bank is persisted and
replayed by production code. Only provider I/O is replaced with fixture replies.
"""
import base64,copy,json,sqlite3,tempfile,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from solders.pubkey import Pubkey
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.history_progress import HistoryProgress
from desk.model import digest
from desk.ownership_worker import advance,saved_progress
from desk.programs import schemas,unbase58
from desk.providers import PUMP
from desk.security import TOKEN_PROGRAM,base58


def string(value):
    data=value.encode();return len(data).to_bytes(4,'little')+data


def synthetic_launch():
    mint=base58(bytes([1])*32);owner=base58(bytes([2])*32);account=base58(bytes([3])*32)
    program=Pubkey.from_string(PUMP)
    curve=str(Pubkey.find_program_address([b'bonding-curve',unbase58(mint)],program)[0])
    authority=str(Pubkey.find_program_address([b'mint-authority'],program)[0])
    event_authority=str(Pubkey.find_program_address([b'__event_authority'],program)[0])
    names={'mint':mint,'mint_authority':authority,'bonding_curve':curve,
           'associated_bonding_curve':account,'user':owner,'event_authority':event_authority,'program':PUMP}
    spec=next(s for s in schemas()[PUMP].values() if s['name']=='create')
    accounts=[a.get('address',names.get(a['name'],owner)) for a in spec['accounts']]
    create_data=bytes(spec['discriminator'])+string('Synthetic')+string('SYN')+string('fixture://ownership')+unbase58(owner)
    # Complete pinned modern CreateEvent, serialized independently of decoder.
    event_data=(bytes.fromhex('e445a52e51cb9a1d1b72a94ddeeb6376')+
                string('Synthetic')+string('SYN')+string('fixture://ownership')+
                b''.join(unbase58(x) for x in (mint,curve,owner,owner))+
                (100).to_bytes(8,'little',signed=True)+
                b''.join(n.to_bytes(8,'little') for n in (100,0,100,100))+
                unbase58(TOKEN_PROGRAM)+b'\0\0'+bytes(32)+bytes(8)+bytes(8)+b'\0')
    def token(kind,info):return {'programId':TOKEN_PROGRAM,'parsed':{'type':kind,'info':info}}
    raw={'slot':10,'blockTime':100,'signature':'synthetic-launch',
         'transaction':{'signatures':['synthetic-launch'],'message':{
             'accountKeys':[{'pubkey':x} for x in (mint,owner,account)],
             'instructions':[{'programId':PUMP,'accounts':accounts,'data':base58(create_data)}]}},
         'meta':{'err':None,'preTokenBalances':[],
                 'postTokenBalances':[{'accountIndex':2,'mint':mint,'owner':owner,'programId':TOKEN_PROGRAM,
                                       'uiTokenAmount':{'amount':'100','decimals':0}}],
                 'innerInstructions':[{'index':0,'instructions':[
                     token('initializeMint2',{'mint':mint,'mintAuthority':authority,'freezeAuthority':None,'decimals':0}),
                     token('initializeAccount3',{'account':account,'mint':mint,'owner':owner}),
                     token('mintTo',{'mint':mint,'account':account,'amount':'100'}),
                     {'programId':PUMP,'accounts':[event_authority,PUMP],'data':base58(event_data)}]}]}}
    m=bytearray(82);m[36:44]=(100).to_bytes(8,'little');m[45]=1
    h=bytearray(165);h[:32]=unbase58(mint);h[32:64]=unbase58(owner);h[64:72]=(100).to_bytes(8,'little');h[108]=1
    values=[{'owner':TOKEN_PROGRAM,'executable':False,'data':[base64.b64encode(x).decode(),'base64']} for x in (m,h)]
    return mint,owner,account,raw,values


class OwnershipIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.db=root/'research.sqlite';self.evidence=root/'evidence.sqlite'
        self.store=EvidenceStore(self.evidence)
        self.mint,self.owner,self.account,self.raw,self.values=synthetic_launch()
        mintkey=self.store.save({'method':'getAccountInfo','params':[self.mint,{'encoding':'base64','commitment':'confirmed'}],
                                 'result':{'value':self.values[0]}})
        _,coverage=collect_history(self.mint,90,110,lambda *a:{'data':[self.raw]},max_pages=1,
                                   capture=self.store.save,token_accounts='none')
        self.report={'mint':self.mint,'observed_at':110,'calls':7,'findings':[],'unknowns':[],
                     'mint_evidence_hash':mintkey,'history_queries':[coverage]}
        self.report['report_hash']=digest(self.report)
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',('scan',self.mint,110,'COMPLETE',json.dumps(self.report)))
            c.row_factory=sqlite3.Row;self.scan=c.execute('SELECT * FROM scans').fetchone()
        self.calls=[];self.missing_account=False;self.new_frontier=False;self.bad_balance=False
    def rpc(self,method,params):
        self.calls.append((method,copy.deepcopy(params)))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],7+len(self.calls))
        if method=='getMultipleAccounts':
            self.assertEqual(params,[[self.mint,self.account],{'encoding':'base64','commitment':'finalized'}])
            values=copy.deepcopy(self.values)
            if self.bad_balance:
                h=bytearray(base64.b64decode(values[1]['data'][0]));h[64:72]=(99).to_bytes(8,'little')
                values[1]['data'][0]=base64.b64encode(h).decode()
            return {'context':{'slot':20},'value':values}
        if method=='getBlockTime':self.assertEqual(params,[20]);return 105
        self.assertEqual(method,'getTransactionsForAddress')
        self.assertEqual(params[1]['filters']['slot'],{'gte':0,'lt':21})
        self.assertNotIn('blockTime',params[1]['filters'])
        # Raw bank and cutoff are durably visible before any bounded replay I/O.
        bank=HistoryProgress(EvidenceStore(self.evidence)).bank('scan')
        self.assertEqual(self.store.load(bank['snapshot_hash'])['result']['context']['slot'],20)
        self.assertEqual(self.store.load(bank['block_time_hash'])['params'],[20])
        if params[0]==self.account and self.missing_account:raise OSError('fixture account unavailable')
        rows=[self.raw]
        if self.new_frontier and params[0]==self.mint:
            new=base58(bytes([4])*32)
            rows=copy.deepcopy(rows)
            rows[0]['transaction']['message']['accountKeys'].append({'pubkey':new})
            rows[0]['meta']['innerInstructions'][0]['instructions'].insert(2,{
                'programId':TOKEN_PROGRAM,'parsed':{'type':'initializeAccount3','info':{
                    'account':new,'mint':self.mint,'owner':self.owner}}})
            rows[0]['meta']['postTokenBalances'].append({'accountIndex':3,'mint':self.mint,'owner':self.owner,
                'programId':TOKEN_PROGRAM,'uiTokenAmount':{'amount':'0','decimals':0}})
        return {'data':rows}
    def run_worker(self,max_calls=4):return advance(self.db,self.evidence,'scan',self.rpc,max_calls=max_calls)
    def test_unmocked_persisted_initialization_to_matching_bank_replay(self):
        before=self.scan['result'];first=self.run_worker(max_calls=1)
        self.assertEqual(first['requests_used'],8)
        bank_hash=first['snapshot_evidence']['snapshot_hash']
        result=self.run_worker()
        self.assertEqual(result['requests_used'],11);self.assertEqual(result['provider_calls'],3)
        self.assertTrue(result['history']['launch_verified'])
        self.assertTrue(result['history']['inventory']['initialization_inventory_verified'])
        self.assertEqual(result['history']['account_queries']['verified'],1)
        self.assertTrue(result['history']['account_continuity']['passed'])
        self.assertTrue(result['snapshot']['reconciled']);self.assertEqual(result['snapshot']['slot'],20)
        self.assertFalse(result['eligible_for_trading']);self.assertFalse(result['snapshot']['common_control_verified'])
        self.assertEqual(result['snapshot_evidence']['snapshot_hash'],bank_hash)
        persisted=saved_progress(EvidenceStore(self.evidence,read_only=True),self.scan)
        self.assertEqual(persisted['snapshot'],result['snapshot'])
        self.assertTrue(self.run_worker()['snapshot']['reconciled']);self.assertEqual(len(self.calls),4)
        with sqlite3.connect(self.db) as c:self.assertEqual(c.execute('SELECT result FROM scans').fetchone()[0],before)
    def test_missing_account_query_cannot_pass_captured_bank(self):
        self.missing_account=True;result=self.run_worker()
        self.assertEqual(result['status'],'PROVIDER_RETRY_REQUIRED');self.assertEqual(result['requests_used'],11)
        self.assertEqual(result['history']['account_queries']['verified'],0)
        self.assertFalse(result['snapshot']['reconciled']);self.assertFalse(result['eligible_for_trading'])
    def test_new_account_in_bounded_inventory_rejects_original_bank(self):
        self.new_frontier=True;result=self.run_worker()
        self.assertEqual(result['status'],'SNAPSHOT_ACCOUNT_COVERAGE_MISMATCH')
        self.assertEqual(result['requests_used'],10);self.assertFalse(result['snapshot']['reconciled'])
        self.assertEqual(result['history']['inventory']['account_count'],2)
        self.assertEqual(sum(m=='getMultipleAccounts' for m,p in self.calls),1)
    def test_unmocked_history_bank_balance_mismatch(self):
        self.bad_balance=True;result=self.run_worker()
        self.assertFalse(result['snapshot']['reconciled'])
        self.assertIn('HISTORY_SNAPSHOT_BALANCE_OR_CONTROL_MISMATCH',result['snapshot']['reasons'])
    def test_interrupted_invocation_releases_lock_without_resetting_bank_or_budget(self):
        first=self.run_worker(1);bank_hash=first['snapshot_evidence']['snapshot_hash']
        def interrupted(method,params):
            self.assertEqual((method,params),('getBlockTime',[20]))
            with self.store.connect() as c:self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],9)
            self.calls.append((method,params));raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):advance(self.db,self.evidence,'scan',interrupted)
        result=self.run_worker()
        self.assertTrue(result['snapshot']['reconciled']);self.assertEqual(result['requests_used'],12)
        self.assertEqual(result['snapshot_evidence']['snapshot_hash'],bank_hash)
        self.assertEqual(sum(m=='getMultipleAccounts' for m,p in self.calls),1)

    def test_interleaved_invocation_cannot_replace_published_head(self):
        self.interleaved_invocations(self.evidence,self.evidence)

    def test_real_path_worker_blocks_symlink_worker_publication(self):
        alias=self.evidence.parent/'alias.sqlite';alias.symlink_to(self.evidence)
        self.interleaved_invocations(self.evidence,alias)
        self.assertFalse(Path(str(alias)+'.ownership-invocation.lock').exists())
        self.assertFalse(Path(str(alias)+'.ownership.lock').exists())

    def test_symlink_worker_blocks_real_path_worker_publication(self):
        alias=self.evidence.parent/'alias.sqlite';alias.symlink_to(self.evidence)
        self.interleaved_invocations(alias,self.evidence)
        self.assertFalse(Path(str(alias)+'.ownership-invocation.lock').exists())
        self.assertFalse(Path(str(alias)+'.ownership.lock').exists())

    def test_hardlink_aliases_are_rejected_without_budget_or_head_changes(self):
        import os
        first=self.run_worker(1);alias=self.evidence.parent/'hardlink.sqlite'
        os.link(self.evidence,alias)
        for path in (self.evidence,alias):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError,'one hard link'):
                    advance(self.db,path,'scan',lambda *a:self.fail('Hardlink provider I/O'))
                with self.assertRaisesRegex(ValueError,'one hard link'):
                    HistoryProgress(EvidenceStore(path))
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],8)
        self.assertEqual(saved_progress(self.store,self.scan)['evidence_hash'],first['evidence_hash'])
        alias.unlink()
        self.assertTrue(self.run_worker()['snapshot']['reconciled'])

    def interleaved_invocations(self,first_path,second_path):
        paused=threading.Event();release=threading.Event();original_save=EvidenceStore.save
        def save(store,payload):
            if payload.get('kind')=='ownership_progress_v1' and threading.current_thread().name.startswith('first'):
                paused.set()
                if not release.wait(10):raise AssertionError('Publication fixture timed out')
            return original_save(store,payload)
        with patch.object(EvidenceStore,'save',save),ThreadPoolExecutor(max_workers=1,thread_name_prefix='first') as pool:
            future=pool.submit(advance,self.db,first_path,'scan',self.rpc,max_calls=1)
            try:
                self.assertTrue(paused.wait(10))
                busy=advance(self.db,second_path,'scan',self.rpc)
                self.assertEqual(busy['status'],'BUSY');self.assertEqual(busy['provider_calls'],0)
                self.assertIsNone(saved_progress(self.store,self.scan))
                with self.store.connect() as c:self.assertEqual(c.execute('SELECT used FROM ownership_budgets').fetchone()[0],8)
            finally:release.set()
            older=future.result(timeout=10)
        self.assertEqual(saved_progress(self.store,self.scan)['evidence_hash'],older['evidence_hash'])
        newer=advance(self.db,second_path,'scan',self.rpc,max_calls=4)
        self.assertTrue(newer['snapshot']['reconciled']);self.assertEqual(newer['requests_used'],11)
        head=saved_progress(self.store,self.scan)
        self.assertEqual(head['evidence_hash'],newer['evidence_hash']);self.assertEqual(head['requests_used'],11)
        self.assertEqual(head['snapshot_evidence'],older['snapshot_evidence'] | {'block_time_hash':newer['snapshot_evidence']['block_time_hash']})
        self.assertEqual(self.run_worker()['requests_used'],11)
        self.assertEqual(saved_progress(self.store,self.scan)['snapshot_evidence'],newer['snapshot_evidence'])
