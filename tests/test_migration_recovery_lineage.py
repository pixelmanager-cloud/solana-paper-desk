"""Actual first/continuation/extension and three-intent preserved journal."""
import base64
import copy
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch
from pathlib import Path
from solders.pubkey import Pubkey
from tests import test_dispatch_preparation_retirement as old_fixture
from tests.test_graduation_witness import fixture
from tests.test_paper_read_sources import Response
from desk import paper_migration_no_entry as recovery, runtime_compatibility as runtime
from desk import runtime_continuation as continuation, runtime_extensions as extension
from desk import ownership_acquisition as acquisition, migration_slot_intake as intake, paper_read_sources as transport
from desk import graduation_witness as graduation, paper_cycle as cycle, monitoring_budget as monitoring
from desk import _migration_decline_legacy_v1 as original_extractor
from desk.model import canonical,digest
from desk.security import base58
from desk.programs import unbase58
from tools import paper_entry_dispatcher as dispatcher


def mint_fixture(n):
    raw,mint,pool=fixture()
    new=base58(bytes([n])*32)
    authority=graduation._pda([b'pool-authority',unbase58(new)],graduation.PUMP)
    curve=graduation._pda([b'bonding-curve',unbase58(new)],graduation.PUMP)
    freshpool=graduation._pda([b'pool',b'\0\0',unbase58(authority),unbase58(new),unbase58(graduation.SOL)],graduation.AMM)
    replacements={mint:new,pool:freshpool,
      graduation._pda([b'pool-authority',unbase58(mint)],graduation.PUMP):authority,
      graduation._pda([b'bonding-curve',unbase58(mint)],graduation.PUMP):curve}
    def replace(x):
        if isinstance(x,dict):return {k:replace(v) for k,v in x.items()}
        if isinstance(x,list):return list(map(replace,x))
        return replacements.get(x,x) if isinstance(x,str) else x
    raw=replace(raw)
    event=raw['meta']['innerInstructions'][0]['instructions'][0]
    data=unbase58(event['data'])
    for before,after in replacements.items():data=data.replace(unbase58(before),unbase58(after))
    event['data']=base58(data[:-32]+bytes(32))
    raw['transaction']['signatures']=[base58(bytes([n])*64)]
    return raw,new,freshpool


class MigrationRecoveryLineageTests(unittest.TestCase):
    def test_real_prefix_prospective_pin_equals_post_extension_and_apply_strict(self):
        predecessor=runtime.implementation_hash()
        f=old_fixture.DispatchPreparationRetirementTests();f.setUp();self.addCleanup(f.doCleanups)
        f.test_real_first_receipt_then_continuation_then_retirement_with_distinct_backup()
        producer=copy.deepcopy(f.pin['successor_context'])
        rp=patch.object(runtime,'POLICY',f.f.root/'runtime-policy.json');rp.start();self.addCleanup(rp.stop)
        cp=patch.object(continuation,'POLICY',f.f.root/'continuation-policy.json');cp.start();self.addCleanup(cp.stop)
        monitoring.MonitoringBudget(f.f.f.progress.store,f.f.ledger,f.f.cfg).provision()
        # The reused real-runtime fixture starts with synthetic native f64;
        # accounting stores that immutable origin, not the effective successor.
        with f.f.f.progress.store.connect() as c:
            c.execute("UPDATE paper_monitoring_budget SET code_hash=? WHERE id=1",('f'*64,))
        # Same original journal: existing retired unresolved + genuine completed
        # deterministic token rejection + a separately captured declined intake.
        with patch.object(runtime,'implementation_hash',return_value=predecessor),patch.object(monitoring,'_implementation',return_value=predecessor):
            with closing(sqlite3.connect(f.journal)) as c:
                original=dispatcher._read_journal(c)
            old_hint=next(iter(original['intents'].values()))['hint']
            def write_intent(identity,mint,pool,signature,seq):
                hint={**old_hint,'mint':mint,'pool':pool,'signature':signature,'seq':seq}
                value={'version':1,'context_hash':digest(producer),'at':f.f.f.at,'hint':hint}
                with closing(sqlite3.connect(f.journal)) as c:dispatcher._write(c,'intents',identity,value,hint)
                return value
            _,bad_mint,bad_pool=mint_fixture(18);bad_id='b'*32
            bad=write_intent(bad_id,bad_mint,bad_pool,base58(bytes([18])*64),2)
            def bad_rpc(method,params):
                account=copy.deepcopy(f.f.f.protocol.rpc('getMultipleAccounts',[])['value'][6])
                data=bytearray(base64.b64decode(account['data'][0]));data[:4]=(1).to_bytes(4,'little');data[4:36]=bytes([7])*32
                account['data'][0]=base64.b64encode(data).decode()
                return {'value':account}
            rejected=acquisition.acquire(f.f.f.jobs.path,f.f.f.progress.store.path,bad_rpc,mint=bad_mint)
            def publish(value):
                result={'version':1,'intent_hash':digest(bad),'at':f.f.f.at,'scan_id':rejected['scan_id'],'result':value}
                with closing(sqlite3.connect(f.journal)) as c:dispatcher._write(c,'results',bad_id,result)
            dispatcher._rejection(producer,bad,rejected['scan_id'],publish)
            raw,mint,pool=mint_fixture(19);identity='c'*32
            intent=write_intent(identity,mint,pool,raw['transaction']['signatures'][0],3)
            seed=[0]
            def rpc(method,params):
                if method=='getAccountInfo':return {'value':copy.deepcopy(f.f.f.protocol.rpc('getMultipleAccounts',[])['value'][6])}
                if method=='getSlot':return 120
                seed[0]+=1;return {'data':[],'paginationToken':'seed' if seed[0]==1 else None}
            captured=acquisition.acquire(f.f.f.jobs.path,f.f.f.progress.store.path,rpc,mint=mint)
            scan=captured['scan_id'];reads=[0]
            class Opener:
                def open(self,request,*,timeout):
                    reads[0]+=1
                    return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':{'data':[raw] if reads[0]==1 else [],'paginationToken':'last' if reads[0]==1 else None}}).encode())
            # Only historical producer intake uses its frozen original extractor.
            # Current prospective rejection and certificate proof stay unpatched.
            with patch.object(intake,'extract_graduation',original_extractor.extract_graduation),patch.object(transport,'build_opener',return_value=Opener()),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_ONLY'}):
                declined=intake.intake(f.f.f.jobs.path,f.f.f.progress.store.path,scan_id=scan,mint=mint,pool=pool,signature=raw['transaction']['signatures'][0],slot=raw['slot'],provenance=dispatcher.PROVENANCE)
            self.assertNotEqual(declined['status'],'RETAINED_MIGRATION_WITNESS')
            self.assertEqual(f.f.f.progress.admission(scan)['requests_used'],6)
        backup=f.f.root/'pre-intake-ledger.sqlite'
        with closing(sqlite3.connect(f.f.ledger)) as a,closing(sqlite3.connect(backup)) as b:a.backup(b)
        stopped=f.f.f.progress.store.save({'kind':'migration_dispatch_stopped_review_v1','dispatch_id':identity,'dispatch_intent_hash':digest(intent),'producer_source_hash':predecessor,'exit_status':2,'service_active':False,'timer_active':False,'observed_at':f.f.f.at+300})
        kwargs={'ledger_backup':str(backup),'stopped_witness_hash':stopped}
        # Synthetic next source identity; actual transition/checkpoint/policy
        # validators remain intact. No passing runtime or budget mocks.
        for p in (patch.object(runtime,'implementation_hash',return_value='d'*64),
                  patch.object(monitoring,'_implementation',return_value='d'*64)):
            p.start();self.addCleanup(p.stop)
        policy=f.f.root/'recovery.json';policy.write_text(canonical({'version':1,'recoveries':[]}))
        p=patch.object(recovery,'POLICY',policy);p.start();self.addCleanup(p.stop)
        # Plan against canonical live inodes and valid predecessor without
        # applying or pretending the successor is already the active runtime.
        with self.assertRaises(ValueError):recovery.review_plan(producer,identity,scan,**kwargs)
        proposed=recovery.review_plan(producer,identity,scan,review_source=predecessor,**kwargs)
        with self.assertRaisesRegex(ValueError,'read-only'):recovery.review_plan(producer,identity,scan,apply=True,review_source=predecessor,**kwargs)
        with closing(sqlite3.connect(f.f.ledger)) as c:
            first,first_hash,cont,cont_hash=extension._base(c,extended=False)
            snapshot=extension._snapshot(c,f.f.cfg)
        pin={k:snapshot[k] for k in ('checkpoint_hash','metadata_hash','events_count','events_hash','outcomes_count','outcomes_hash')}
        pin.update(sequence=1,first_receipt_hash=first_hash,continuation_receipt_hash=cont_hash,parent_receipt_hash=cont_hash,predecessor=predecessor,successor=runtime.implementation_hash(),config_hash=digest(f.f.cfg),context=cont['context'])
        ep=f.f.root/'extension.json';ep.write_text(canonical({'version':1,'extensions':[pin]}))
        p=patch.object(extension,'POLICY',ep);p.start();self.addCleanup(p.stop)
        extension.append_runtime(*f.args,**{k:pin[k] for k in ('sequence','first_receipt_hash','continuation_receipt_hash','parent_receipt_hash','predecessor','successor')})
        actual=recovery.review_plan(producer,identity,scan,**kwargs)
        self.assertEqual(proposed,actual)
        policy.write_text(canonical({'version':1,'recoveries':[actual]}))
        journal_bytes=f.journal.read_bytes()
        result=recovery.review_plan(producer,identity,scan,apply=True,**kwargs)
        self.assertEqual(result['status'],'RECORDED');self.assertEqual(f.journal.read_bytes(),journal_bytes)
        with closing(sqlite3.connect(f.journal)) as c:
            verified=dispatcher._validate(c,actual['successor_context'])
            self.assertEqual(len(verified['intents']),3);self.assertEqual(len(verified['results']),1)
            self.assertNotIn(identity,verified['results'])
        self.assertEqual(f.f.f.progress.admission(scan)['requests_used'],6)
        self.assertEqual(f.f.f.progress.admission(f.scan)['requests_used'],12)
        # Existing completed evidence cannot be borrowed from another invocation,
        # and a fourth unresolved intent cannot be covered by this certificate.
        from discovery import continuous as discovery
        fresh,mint4,pool4=mint_fixture(20)
        event=fresh['meta']['innerInstructions'][0]['instructions'][0]
        event['data']=base58(unbase58(event['data'])[:-32]+unbase58(graduation.SOL))
        notification={'method':'transactionNotification','params':{'result':{
            'signature':fresh['transaction']['signatures'][0],'slot':fresh['slot'],
            'blockTime':None,'transaction':{'transaction':fresh['transaction'],'meta':fresh['meta']}}}}
        source=discovery.Store(producer['paths']['discovery_db']['path'],clock=lambda:f.f.f.at-600)
        try:source.complete(source.reserve('RECEIVE'),payload=canonical(notification).encode())
        finally:source.close()
        with closing(sqlite3.connect(f.journal)) as c:
            selected=dispatcher._select(actual['successor_context'],c,f.f.f.at)
            self.assertEqual(selected['mint'],mint4)
            with self.assertRaises(sqlite3.Error):dispatcher._write(c,'intents',identity,intent,intent['hint'])
        current_context=actual['successor_context'];current_id='4'*32
        current_intent={'version':1,'context_hash':digest(current_context),'at':f.f.f.at,'hint':selected}
        with dispatcher._journal(f.journal) as c:dispatcher._write(c,'intents',current_id,current_intent,selected)
        seed[0]=0
        following=acquisition.acquire(f.f.f.jobs.path,f.f.f.progress.store.path,rpc,mint=mint4)
        next_scan=following['scan_id'];following_reads=[0]
        fresh_event=fresh['meta']['innerInstructions'][0]['instructions'][0]
        fresh_event['data']=base58(unbase58(fresh_event['data'])[:-32]+bytes([33])*32)
        class FollowingOpener:
            def open(self,request,*,timeout):
                following_reads[0]+=1
                body={'data':[fresh] if following_reads[0]==1 else [],'paginationToken':'last' if following_reads[0]==1 else None}
                return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':body}).encode())
        with patch.object(transport,'build_opener',return_value=FollowingOpener()),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_ONLY'}):
            declined=intake.intake(f.f.f.jobs.path,f.f.f.progress.store.path,scan_id=next_scan,mint=mint4,pool=pool4,signature=fresh['transaction']['signatures'][0],slot=fresh['slot'],provenance=dispatcher.PROVENANCE)
        self.assertNotEqual(declined['status'],'RETAINED_MIGRATION_WITNESS')
        with dispatcher._journal(f.journal) as c:
            ordinary=recovery.publish(c,current_context,current_id,next_scan)
            result={'version':1,'intent_hash':digest(current_intent),'at':f.f.f.at,'scan_id':next_scan,'result':ordinary}
            dispatcher._write(c,'results',current_id,result)
            verified=dispatcher._validate(c,current_context)
            self.assertEqual(len(verified['intents']),4);self.assertEqual(len(verified['results']),2)
        self.assertEqual(f.f.f.progress.admission(next_scan)['requests_used'],6)
        self.assertEqual(following_reads[0],2)
        # The next unclaimed candidate still remains selectable after a genuine
        # ordinary rejection on this same continued journal.
        next_raw,next_mint,next_pool=mint_fixture(21)
        next_event=next_raw['meta']['innerInstructions'][0]['instructions'][0]
        next_event['data']=base58(unbase58(next_event['data'])[:-32]+unbase58(graduation.SOL))
        notification['params']['result'].update(signature=next_raw['transaction']['signatures'][0],slot=next_raw['slot'],transaction={'transaction':next_raw['transaction'],'meta':next_raw['meta']})
        source=discovery.Store(producer['paths']['discovery_db']['path'],clock=lambda:f.f.f.at-600)
        try:source.complete(source.reserve('RECEIVE'),payload=canonical(notification).encode())
        finally:source.close()
        with closing(sqlite3.connect(f.journal)) as c:
            selected=dispatcher._select(current_context,c,f.f.f.at)
            self.assertEqual(selected['mint'],next_mint)
        with closing(sqlite3.connect(f.journal)) as c:
            c.execute('DROP TRIGGER no_intents_update')
            c.execute('UPDATE intents SET rowid=99 WHERE id=?',(bad_id,))
            c.execute(dispatcher._guards()['no_intents_update']);c.commit()
        with closing(sqlite3.connect(f.journal)) as c:
            with self.assertRaisesRegex(ValueError,'prefix'):dispatcher._validate(c,actual['successor_context'])
        with closing(sqlite3.connect(f.journal)) as c:
            c.execute('DROP TRIGGER no_intents_update')
            c.execute('UPDATE intents SET rowid=2 WHERE id=?',(bad_id,))
            c.execute(dispatcher._guards()['no_intents_update']);c.commit()
            future={'version':1,'context_hash':digest(actual['successor_context']),'at':f.f.f.at,'hint':selected}
            dispatcher._write(c,'intents','5'*32,future,selected)
        with closing(sqlite3.connect(f.journal)) as c:
            with self.assertRaisesRegex(ValueError,'Unresolved dispatch'):dispatcher._validate(c,actual['successor_context'])
