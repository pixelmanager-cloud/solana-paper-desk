"""Executable protocol evidence, not implementation or acceptance of a new flow.

All replies/deaths are synthetic. Tests deliberately reproduce the current
unpublished-cutoff retry gap while preserving fail-closed entry behavior.
"""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk import coordinator_rpc, providers
from desk.common_bank_journal import CommonBankJournal, JournalBlocked
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import BIRTH_ACQUISITION_V1,JobPersistence
from desk.model import canonical,digest
from desk.ownership_acquisition import acquire,_Setup
from desk.pool_receipt_ledger import ApprovedSource
from tests.test_ownership_integration import synthetic_launch


def _trace(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()] if Path(path).exists() else []


def _rpc(evidence,trace,uid,cutoff):
    mint,_,_,raw,values=synthetic_launch()
    def rpc(method,params):
        before=_trace(trace);a=HistoryProgress(EvidenceStore(evidence)).admission(uid)
        assert a['requests_used']==len(before)+1 and a['request_ceiling']==18
        with open(trace,'a') as out:
            out.write(canonical({'method':method,'params':params,'reserved':a['requests_used'],
                                 'declared_slot':cutoff if method=='getSlot' else None})+'\n')
            out.flush();os.fsync(out.fileno())
        if method=='getAccountInfo':return {'value':values[0]}
        if method=='getSlot':return cutoff
        assert method=='getTransactionsForAddress' and params[0]==mint
        assert params[1]['filters']['slot']=={'gte':0,'lt':cutoff+1}
        return {'data':[copy.deepcopy(raw)]}
    return rpc


def _die_after_return_before_cutoff_save(research,evidence,trace,uid):
    original=_Setup.save
    def save(setup,name,record):
        if name=='cutoff_hash':
            assert record['result']==20
            os._exit(121)
        return original(setup,name,record)
    with patch.object(_Setup,'save',save):acquire(research,evidence,_rpc(evidence,trace,uid,20),scan_id=uid)
    raise AssertionError('Fixture death boundary not reached')


class RequestBoundCutoffContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.research=self.root/'research.sqlite';self.evidence=self.root/'evidence.sqlite';self.trace=self.root/'trace.jsonl'
        self.mint,_,_,self.raw,self.values=synthetic_launch();self.jobs=JobPersistence(self.research)
        with patch('desk.job_persistence.time.time',return_value=100000):
            self.uid=self.jobs.admit(self.mint,kind=BIRTH_ACQUISITION_V1,evidence_db=self.evidence)

    def test_existing_slot_filter_is_bound_to_saved_observation_and_original_manifest(self):
        observed={}
        rpc=_rpc(self.evidence,self.trace,self.uid,20)
        def probe(method,params):
            if method=='getSlot':
                with EvidenceStore(self.evidence).connect() as c:
                    observed['setup']=c.execute('SELECT mint_hash,cutoff_hash FROM ownership_acquisition_setup').fetchone()
                    observed['tables']={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    observed['pages']=c.execute('SELECT count(*) FROM pages').fetchone()[0]
                    observed['budget']=c.execute('SELECT used,ceiling FROM ownership_budgets').fetchone()
            return rpc(method,params)
        result=acquire(self.research,self.evidence,probe,scan_id=self.uid);report=result['report'];store=EvidenceStore(self.evidence,read_only=True)
        self.assertEqual(observed['budget'],(2,18));self.assertIsNotNone(observed['setup'][0]);self.assertIsNone(observed['setup'][1])
        self.assertEqual(observed['pages'],1)
        self.assertFalse(any('intent' in name or name.startswith('common_bank_') for name in observed['tables']))
        cutoff=store.load(report['acquisition']['cutoff_hash'])
        self.assertEqual(cutoff,{'method':'getSlot','params':[{'commitment':'finalized'}],'result':20})
        query=report['history_queries'][0];page=query['pages'][0];request=store.load(page['request_evidence_hash'])
        history_call=next(row for row in _trace(self.trace) if row['method']=='getTransactionsForAddress')
        self.assertEqual(request['params'],history_call['params']);self.assertEqual(request['response_hash'],page['payload_hash'])
        self.assertEqual(query['slot_range'],{'gte':0,'lt':21});self.assertNotIn('blockTime',request['params'][1]['filters'])
        self.assertEqual(report['calls'],3);self.assertFalse(result['eligible_for_trading'])
        source=self.jobs.source(self.uid);before=self.evidence.read_bytes()
        terminal=acquire(self.research,self.evidence,lambda *a:self.fail('Saved cutoff must not move'),scan_id=self.uid)
        self.assertEqual(terminal['provider_calls'],0);self.assertEqual(source,self.jobs.source(self.uid));self.assertEqual(before,self.evidence.read_bytes())

    @unittest.skipUnless(os.name=='posix','Actual process-death fixture')
    def test_actual_returned_unsaved_cutoff_is_ambiguous_and_current_api_requeries_later_slot(self):
        ctx=multiprocessing.get_context('spawn');p=ctx.Process(target=_die_after_return_before_cutoff_save,
                args=(self.research,self.evidence,self.trace,self.uid))
        self.addCleanup(lambda:p.kill() if p.is_alive() else None);p.start();p.join(20);self.assertEqual(p.exitcode,121)
        store=EvidenceStore(self.evidence);progress=HistoryProgress(store)
        a=progress.admission(self.uid);self.assertEqual((a['state'],a['requests_used'],a['request_ceiling']),('ADMITTED',2,18))
        with store.connect() as c:
            before=c.execute('SELECT mint_hash,cutoff_hash FROM ownership_acquisition_setup').fetchone()
            self.assertIsNotNone(before[0]);self.assertIsNone(before[1]);self.assertEqual(c.execute('SELECT count(*) FROM pages').fetchone()[0],1)
        self.assertEqual([r['declared_slot'] for r in _trace(self.trace) if r['method']=='getSlot'],[20])
        # This intentionally reproduces the missing terminal-uncertainty contract:
        # the same charged scan currently takes a NEW finalized slot on reopening.
        result=acquire(self.research,self.evidence,_rpc(self.evidence,self.trace,self.uid,900),scan_id=self.uid)
        self.assertEqual(result['report']['acquisition']['cutoff'],900)
        self.assertEqual(result['report']['history_queries'][0]['slot_range'],{'gte':0,'lt':901})
        self.assertEqual([r['declared_slot'] for r in _trace(self.trace) if r['method']=='getSlot'],[20,900])
        self.assertEqual([r['reserved'] for r in _trace(self.trace)],[1,2,3,4])
        self.assertEqual(progress.admission(self.uid)['requests_used'],4);self.assertFalse(result['eligible_for_trading'])
        with store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM ownership_budgets').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT mint_hash FROM ownership_acquisition_setup').fetchone()[0],before[0])
            old=digest({'method':'getSlot','params':[{'commitment':'finalized'}],'result':20})
            self.assertFalse(c.execute('SELECT 1 FROM pages WHERE hash=?',(old,)).fetchone())

    def test_original_wire_slot_envelope_is_rejected_not_silently_normalized_as_cutoff(self):
        def rpc(method,params):
            if method=='getAccountInfo':return {'value':self.values[0]}
            if method=='getSlot':return {'jsonrpc':'2.0','id':1,'result':20}
            self.fail('Unsupported wire reply must not grant a history cutoff')
        result=acquire(self.research,self.evidence,rpc,scan_id=self.uid)
        self.assertEqual(result['status'],'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED')
        self.assertEqual(result['requests_used'],2);self.assertFalse(result['eligible_for_trading'])
        with EvidenceStore(self.evidence).connect() as c:self.assertIsNone(c.execute('SELECT cutoff_hash FROM ownership_acquisition_setup').fetchone()[0])

    @unittest.skipUnless(Path('/proc/self/mountinfo').is_file(),'Existing Linux journal guard')
    def test_accepted_common_bank_journal_cannot_bootstrap_unsealed_discovery(self):
        def rpc(method,params):
            if method=='getAccountInfo':return {'value':self.values[0]}
            if method=='getSlot':raise OSError('fixture uncertain request')
            self.fail('No discovery before known cutoff')
        acquire(self.research,self.evidence,rpc,scan_id=self.uid)
        store=EvidenceStore(self.evidence)
        with store.connect() as c:
            before=c.execute('SELECT * FROM ownership_admissions').fetchall();tables=c.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall()
        j=CommonBankJournal(self.research,self.evidence,ApprovedSource('fixture','synthetic_fixture'))
        with j.locked() as s:
            with self.assertRaisesRegex(JournalBlocked,'SEALED'):s.create_run(self.uid,'discovery-bootstrap')
        with store.connect() as c:
            self.assertEqual(before,c.execute('SELECT * FROM ownership_admissions').fetchall())
            self.assertEqual(tables,c.execute('SELECT name FROM sqlite_master ORDER BY name').fetchall())
        self.assertEqual(HistoryProgress(store).admission(self.uid)['requests_used'],2)


class SupportedTransportCutoffContractTests(unittest.TestCase):
    def test_legacy_result_transport_loses_distinct_original_wire_envelopes(self):
        params=[{'commitment':'finalized'}]
        frames=[{'jsonrpc':'2.0','id':1,'result':20},{'jsonrpc':'2.0','id':99,'result':20}]
        with patch.object(providers,'api_key',return_value='synthetic-fixture-not-a-secret'),patch.object(providers,'fetch_json',side_effect=frames) as fetch:
            self.assertEqual([providers.helius_rpc('getSlot',params),providers.helius_rpc('getSlot',params)],[20,20])
        for call in fetch.call_args_list:
            self.assertEqual(call.args[1],{'jsonrpc':'2.0','id':1,'method':'getSlot','params':params})
        # Hardened coordinator parsing retains framing checks but still returns
        # only result; neither exact response bytes nor request bytes are exposed.
        for raw in (b'{"jsonrpc":"2.0","id":1,"result":20}',b' \n {"result":20,"id":1,"jsonrpc":"2.0"} \n'):
            self.assertEqual(coordinator_rpc._result(json.loads(raw)),20)

    def test_fixed_coordinator_transport_rejects_history_before_credentials_or_io(self):
        mint,_,_,_,_=synthetic_launch()
        params=[mint,{'transactionDetails':'full','sortOrder':'asc','limit':100,'commitment':'finalized',
                      'encoding':'jsonParsed','maxSupportedTransactionVersion':1,
                      'filters':{'slot':{'gte':0,'lt':21},'status':'any','tokenAccounts':'none'}}]
        with patch.object(coordinator_rpc.os.environ,'get',side_effect=AssertionError('No secret access')) as credential,patch.object(coordinator_rpc,'build_opener',side_effect=AssertionError('No I/O')) as network:
            with self.assertRaises(coordinator_rpc.CoordinatorRPCError):coordinator_rpc.HeliusMainnetRPC()('getTransactionsForAddress',params)
            credential.assert_not_called();network.assert_not_called()
