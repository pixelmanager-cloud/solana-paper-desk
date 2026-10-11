"""SYNTHETIC_TEST_ONLY; actual persistence/APIs, injected wire bytes, no network."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import paper_entry_dispatcher as tool
from desk import paper_cycle as cycle, provider_pacing as pace, kraken_pacing_migration as upgrade
from desk import paper_read_sources as transport
from desk.model import canonical, digest
from desk.monitoring_budget import MonitoringBudget
from desk.paper_observation_collector import ObservationTarget
from discovery import continuous as discovery
from tests import test_paper_observation_collector as fixture
from tests.test_graduation_witness import fixture as migration_fixture
from tests.test_kraken_lifecycle import actual_cycle
from tests.test_paper_read_sources import Response
from tests.helpers import config
from tests.test_live_strategy_features import transaction, POOL as TRADE_POOL
from desk.security import base58
from desk.programs import unbase58


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PaperObservationCollectorTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.f.at=int(time.time());self.root=Path(self.f.tmp.name).resolve()
        self.cfg=config()|{'paper_signal_policy_version':3,'paper_quote_execution_version':1,'paper_usd_valuation_version':1}
        self.config=self.root/'config.json';self.config.write_text(canonical(self.cfg))
        self.ledger=self.root/'ledger.sqlite';cycle.initialize(self.ledger,self.cfg)
        MonitoringBudget(self.f.progress.store,self.ledger,self.cfg).provision()
        self.pacer=self.root/'pacing.sqlite';pace.initialize(self.pacer)
        policy=self.root/'migration.json';p=patch.object(upgrade,'POLICY',policy);p.start();self.addCleanup(p.stop)
        policy.write_text(canonical({'version':1,'pins':[]}));proposal=upgrade.review_plan(self.pacer)
        policy.write_text(canonical({'version':1,'pins':[proposal]}));upgrade.migrate(self.pacer)
        self.clock=[float(self.f.at)]
        def configured(**kw):
            return pace.Pacer(self.pacer,clock=lambda:self.clock[0],monotonic=lambda:self.clock[0],sleep=lambda n:self.clock.__setitem__(0,self.clock[0]+n),**kw)
        p=patch.object(pace,'configured',side_effect=configured);p.start();self.addCleanup(p.stop)
        p=patch.dict(os.environ,{pace.ENV:str(self.pacer)});p.start();self.addCleanup(p.stop)
        self.discovery=self.root/'discovery.sqlite'
        with patch.object(discovery.time,'time',return_value=self.f.at-600):discovery.initialize(self.discovery)
        self.raw,self.mint,self.pool=migration_fixture()
        self.raw['transaction']['signatures']=[base58(bytes([9])*64)]
        self.raw['blockTime']=self.f.at-600
        ix=self.raw['meta']['innerInstructions'][0]['instructions'][0]
        data=bytearray(unbase58(ix['data']));data[136:144]=self.raw['blockTime'].to_bytes(8,'little',signed=True);ix['data']=base58(data)
        self.wire=canonical({'method':'transactionNotification','params':{'result':{
            'signature':self.raw['transaction']['signatures'][0],'slot':self.raw['slot'],
            'blockTime':self.raw['blockTime'],'transaction':{'transaction':self.raw['transaction'],'meta':self.raw['meta']}}}})
        d=discovery.Store(self.discovery,clock=lambda:self.f.at-600)
        try:d.complete(d.reserve('RECEIVE'),payload=self.wire.encode())
        finally:d.close()
        self.journal=self.root/'dispatch.sqlite'
        self.args=dict(config=str(self.config),research_db=str(self.f.jobs.path),evidence_db=str(self.f.progress.store.path),
            ledger_db=str(self.ledger),discovery_db=str(self.discovery),pacing_db=str(self.pacer),journal=str(self.journal),
            taker=self.f.taker,amount_raw=100_000_000,pool_fee_bps='25')
        self.ctx=tool.plan(**self.args)
        tool.initialize(self.ctx,approved_context_hash=digest(self.ctx))
        self.calls=[]

    def invoke(self,**kw):return tool.dispatch(tool.plan(**self.args),**kw)
    def count(self,table):
        with sqlite3.connect(self.journal) as c:return c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    def setup_rpc(self,method,params):
        self.assertEqual(self.count('intents'),1,'Durable intent must precede I/O')
        self.calls.append(method)
        if method=='getAccountInfo':return {'value':copy.deepcopy(self.f.protocol.rpc('getMultipleAccounts',[])['value'][6])}
        if method=='getSlot':return 120
        self.assertEqual(method,'getTransactionsForAddress');return {'data':[]}
    def live(self, roundtrip_output=100_000_000):
        outer=self
        class Opener:
            def open(self,request,*,timeout):
                outer.calls.append('intake')
                outer.assertEqual(outer.count('intents'),1)
                call=json.loads(request.data)
                rows=[outer.raw]
                if call['params'][1]['filters'].get('slot')!={'gte':10,'lt':11}:
                    rows=[]
                    for i in range(40):
                        raw=transaction('dispatch-flow-'+str(i),10+i,outer.f.at-1,quote=200,base=100,
                                        wallet=base58((i+1).to_bytes(32,'big')))
                        ix=raw['transaction']['message']['instructions'][0]
                        ix['data']=base58(unbase58(ix['data']).replace(unbase58(TRADE_POOL),unbase58(outer.pool)))
                        rows.append(raw)
                return Response(canonical({'jsonrpc':'2.0','id':'paper-read-v1','result':{'data':rows,'paginationToken':None}}).encode())
        original=cycle.run_once
        def entry_cycle(research,evidence,ledger,cfg,**kw):
            item=kw['candidates'][0]
            h=SimpleNamespace(f=outer.f,target=item.target,item=item,path=outer.ledger,cfg=outer.cfg,http_calls=[],sell_output=roundtrip_output,buy_output_raw=10_000_000)
            def run(**args):
                return original(research,evidence,ledger,cfg,wall_clock=lambda:outer.f.at,monotonic=lambda:outer.f.tick,dependency_blockers=(),**args)
            h.run_cycle=run
            return actual_cycle(h,candidates=(item,))
        with (patch.object(tool.cli,'_credentials'),patch('desk.providers.helius_rpc',side_effect=self.setup_rpc),
              patch.object(transport,'build_opener',return_value=Opener()),patch.dict(os.environ,{'HELIUS_API_KEY':'SYNTHETIC','JUPITER_API_KEY':'SYNTHETIC'}),
              patch.object(cycle,'run_once',side_effect=entry_cycle),patch.object(tool.entry.time,'sleep'),
              patch.object(tool.time,'time',return_value=self.f.at)):
            return self.invoke(execute=True,systemd_credentials=True)

    def test_dry_run_no_admission_credentials_or_io_and_context_activation(self):
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('credentials')):
            result=self.invoke()
        self.assertEqual(result['status'],'DRY_RUN');self.assertEqual(result['hint']['mint'],self.mint)
        self.assertEqual(self.count('intents'),0)
        with self.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM scans').fetchone()[0],0)
        with self.assertRaises(ValueError):tool.initialize(self.ctx,approved_context_hash='0'*64)

    def test_actual_admission_acquisition_intake_history_entry_restart_and_dedupe(self):
        result=self.live();self.assertEqual(result['status'],'DISPATCHED',result)
        self.assertEqual(result['paper_status'],'COMPLETE',result)
        self.assertEqual(self.count('results'),1)
        self.assertEqual(len(cycle._state(self.ledger,self.cfg)['positions']),1)
        # Held positions take priority; restarting does not create another scan.
        with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
        with self.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM scans').fetchone()[0],1)
        with tool._journal(self.journal) as c:
            tool._validate(c,tool.plan(**self.args))
            self.assertIsNone(tool._select(self.ctx,c,self.f.at))
        self.assertEqual(self.f.progress.admission(result['scan_id'])['request_ceiling'],18)

    def test_interrupted_intent_blocks_restart_without_credentials_retry_or_new_scan(self):
        original_write=tool._write
        def interrupted(*args,**kw):
            original_write(*args,**kw)
            raise ValueError('crashed after durable intent')
        with patch.object(tool.cli,'_credentials'),patch.object(tool,'_write',side_effect=interrupted):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
        self.assertEqual(self.count('intents'),1);self.assertEqual(self.count('results'),0)
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('retry')):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)

    def test_actual_charged_acquisition_failure_retains_pending_and_refuses_retry(self):
        with patch.object(tool.cli,'_credentials'),patch('desk.providers.helius_rpc',side_effect=ValueError('ambiguous')):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
        with self.f.jobs.connect() as c:scan=c.execute('SELECT id FROM scans').fetchone()[0]
        self.assertEqual(self.f.progress.admission(scan)['requests_used'],1)
        with patch('desk.providers.helius_rpc',side_effect=AssertionError('retry')):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
        self.assertEqual(self.count('results'),0)

    def test_already_admitted_even_completed_mint_not_selected(self):
        def rpc(method,params):
            if method=='getAccountInfo':return {'value':copy.deepcopy(self.f.protocol.rpc('getMultipleAccounts',[])['value'][6])}
            if method=='getSlot':return 120
            return {'data':[]}
        prior=tool.acquisition.acquire(self.f.jobs.path,self.f.progress.store.path,rpc,mint=self.mint)
        self.assertEqual(self.f.jobs.source(prior['scan_id'])['status'],'COMPLETE')
        self.assertEqual(self.invoke()['status'],'NO_CANDIDATE');self.assertEqual(self.count('intents'),0)

    def test_freshness_boundaries_and_future_exclusion(self):
        for age,expected in ((299,'NO_CANDIDATE'),(300,'DRY_RUN'),(7200,'DRY_RUN'),(7201,'NO_CANDIDATE'),(-1,'NO_CANDIDATE')):
            with self.subTest(age=age),patch.object(tool.time,'time',return_value=self.f.at-600+age):
                self.assertEqual(self.invoke()['status'],expected)

    def test_context_config_source_taker_size_and_inode_refuse_before_credentials(self):
        for key,value in (('source_hash','0'*64),('tool_hash','0'*64),('config_hash','0'*64),('taker',self.pool),('amount_raw',1)):
            bad=copy.deepcopy(self.ctx);bad[key]=value
            with self.subTest(key=key),patch.object(tool.cli,'_credentials',side_effect=AssertionError('credentials')):
                with self.assertRaises(ValueError):tool.dispatch(bad,execute=True,systemd_credentials=True)
        bad=copy.deepcopy(self.ctx);bad['paths']['ledger_db']['inode']+=1
        with self.assertRaises(ValueError):tool.dispatch(bad)

    def test_pacing_pending_and_waiters_block_without_intent_or_credentials(self):
        p=pace.configured(priority='investigation');ticket=p.acquire('helius',timeout_seconds=10)
        try:
            with patch.object(tool.cli,'_credentials',side_effect=AssertionError('credentials')):
                with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
        finally:p.finish('helius',ticket=ticket)
        self.assertEqual(self.count('intents'),0)

    def test_unresolved_observation_globally_blocks(self):
        with self.f.progress.store.connect() as c:
            c.execute('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute("INSERT INTO paper_observation_passes VALUES('unresolved','missing',NULL)")
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.count('intents'),0)

    def test_corrupt_discovery_hash_or_original_frame_rejects_nonmutating(self):
        with sqlite3.connect(self.discovery) as c:
            for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='raw_events'").fetchall():
                c.execute('DROP TRIGGER '+name)
            c.execute("UPDATE raw_events SET payload_hash=?",('0'*64,))
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.count('intents'),0)

    def test_journal_guard_replacement_and_oversize_refuse_before_materialization(self):
        with sqlite3.connect(self.journal) as c:
            c.execute('DROP TRIGGER no_context_update')
            c.execute('CREATE TRIGGER no_context_update BEFORE UPDATE ON context BEGIN SELECT 1; END')
        with patch.object(tool,'_parse',side_effect=AssertionError('payload materialized')):
            with self.assertRaises(ValueError):self.invoke()

    def test_real_cli_default_dry_run_and_invalid_activation_hash(self):
        from contextlib import redirect_stdout
        import io
        argv=[]
        for key,value in self.args.items():argv+=['--'+key.replace('_','-'),str(value)]
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tool.main(argv),0)
        self.assertEqual(json.loads(output.getvalue())['status'],'DRY_RUN')
        with redirect_stdout(io.StringIO()):self.assertEqual(tool.main(argv+['--initialize','--approved-context-hash','0'*64]),2)

    def test_paused_ledger_refuses_before_intent_or_credentials(self):
        from desk.ledger import Ledger
        from desk.engine import transition, initial_state
        Ledger(self.ledger).apply({'schema_version':1,'event_id':'operator-pause','ts':self.f.at,
            'kind':'control','actor':'operator','command':'PAUSE_ENTRY'},self.cfg,transition,initial_state)
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('credentials')):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
        self.assertEqual(self.count('intents'),0)

    def test_pacing_waiter_refuses_before_intent(self):
        with sqlite3.connect(self.pacer) as c:
            c.execute('INSERT INTO waiters VALUES(?,?,?,?,?)',('fixture-waiter','helius','investigation',self.f.at,self.f.at+20))
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.count('intents'),0)

    def test_partial_journal_and_oversize_context_reject_before_parse(self):
        with sqlite3.connect(self.journal) as c:
            c.execute('DROP TRIGGER no_context_update')
            c.execute("UPDATE context SET payload=?",('x'*(tool.MAX_PAYLOAD+1),))
            c.execute(tool._guards()['no_context_update'])
        with patch.object(tool,'_parse',side_effect=AssertionError('unbounded payload fetched')):
            with self.assertRaises(ValueError):self.invoke()

    def test_actual_known_mint_control_rejection_retains_charge_and_never_intakes(self):
        import base64
        def adverse(method,params):
            self.assertEqual(method,'getAccountInfo')
            result=self.setup_rpc(method,params)
            raw=bytearray(base64.b64decode(result['value']['data'][0]))
            raw[:4]=(1).to_bytes(4,'little');raw[4:36]=bytes([7])*32
            result['value']['data'][0]=base64.b64encode(raw).decode()
            return result
        with patch.object(tool.cli,'_credentials'),patch('desk.providers.helius_rpc',side_effect=adverse),patch.object(tool.migration,'intake',side_effect=AssertionError('unsafe intake')):
            result=self.invoke(execute=True,systemd_credentials=True)
        self.assertEqual(result['status'],'TOKEN_REJECTED')
        with self.f.jobs.connect() as c:scan=c.execute('SELECT id FROM scans').fetchone()[0]
        self.assertEqual(self.f.progress.admission(scan)['requests_used'],1)
        self.assertEqual(self.count('results'),1)
        self.assertEqual(self.invoke()['status'],'NO_CANDIDATE')
        self.assertEqual(self.calls,['getAccountInfo'])

    def rejection(self, *, mint_tag=1, freeze_tag=0, zero_supply=False):
        import base64
        def rpc(method,params):
            result=self.setup_rpc(method,params)
            raw=bytearray(base64.b64decode(result['value']['data'][0]))
            raw[:4]=mint_tag.to_bytes(4,'little');raw[4:36]=bytes([7])*32
            raw[46:50]=freeze_tag.to_bytes(4,'little');raw[50:82]=bytes([8])*32
            if zero_supply:raw[36:44]=bytes(8)
            result['value']['data'][0]=base64.b64encode(raw).decode()
            return result
        with patch.object(tool.cli,'_credentials'),patch('desk.providers.helius_rpc',side_effect=rpc),patch.object(tool.migration,'intake',side_effect=AssertionError('rejected mint intake')):
            return self.invoke(execute=True,systemd_credentials=True)

    def append_distinct_migration(self, *, no_op=False, null_time=False, depth=2, non_sol=False, malformed=False):
        from solders.pubkey import Pubkey
        from desk import graduation_witness as g
        raw=copy.deepcopy(self.raw)
        if non_sol:
            raw,_,_=migration_fixture('migrate_v2')
            raw['blockTime']=self.raw['blockTime']
            event=raw['meta']['innerInstructions'][0]['instructions'][0]
            data=bytearray(unbase58(event['data']));data[136:144]=raw['blockTime'].to_bytes(8,'little',signed=True)
            event['data']=base58(data)
        mint=str(Pubkey.from_bytes(bytes([17])*32))
        authority=g._pda([b'pool-authority',unbase58(mint)],g.PUMP)
        curve=g._pda([b'bonding-curve',unbase58(mint)],g.PUMP)
        quote=str(Pubkey.from_bytes(bytes([18])*32)) if non_sol else g.SOL
        pool=g._pda([b'pool',b'\0\0',unbase58(authority),unbase58(mint),unbase58(quote)],g.AMM)
        old_authority=g._pda([b'pool-authority',unbase58(self.mint)],g.PUMP)
        old_curve=g._pda([b'bonding-curve',unbase58(self.mint)],g.PUMP)
        replacements={self.mint:mint,self.pool:pool,old_authority:authority,old_curve:curve}
        if non_sol:replacements[g.SOL]=quote
        for ix in raw['transaction']['message']['instructions']:
            ix['accounts']=[replacements.get(x,x) for x in ix['accounts']]
        ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        data=unbase58(ix['data'])
        for old,new in replacements.items():data=data.replace(unbase58(old),unbase58(new))
        ix['data']=base58(data)
        ix['stackHeight']=depth
        if no_op:raw['meta']['innerInstructions']=[]
        if null_time:raw['blockTime']=None
        if malformed:del raw['transaction']['message']
        signature=base58(bytes([10])*64);raw['transaction']['signatures']=[signature]
        wire=canonical({'method':'transactionNotification','params':{'result':{
            'signature':signature,'slot':raw['slot'],'blockTime':raw['blockTime'],
            'transaction':{'transaction':raw['transaction'],'meta':raw['meta']}}}})
        d=discovery.Store(self.discovery,clock=lambda:self.f.at-500)
        try:d.complete(d.reserve('RECEIVE'),payload=wire.encode())
        finally:d.close()
        return mint

    def test_successful_noop_outer_migration_without_event_is_not_admitted(self):
        self.append_distinct_migration(no_op=True)
        with patch.object(tool.time,'time',return_value=self.f.at+6650),patch.object(tool.cli,'_credentials',side_effect=AssertionError('no-op credentials')):
            self.assertEqual(self.invoke(execute=True,systemd_credentials=True)['status'],'NO_CANDIDATE')
        self.assertEqual(self.count('intents'),0)
        with self.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM scans').fetchone()[0],0)
        self.assertEqual(self.calls,[])

    def test_null_blocktime_event_hint_preserves_original_and_requires_intake(self):
        mint=self.append_distinct_migration(null_time=True)
        result=self.invoke()
        self.assertEqual(result['status'],'DRY_RUN');self.assertEqual(result['hint']['mint'],mint)
        with sqlite3.connect(self.discovery) as c:
            retained=json.loads(c.execute('SELECT payload FROM raw_events ORDER BY seq DESC LIMIT 1').fetchone()[0])
        self.assertIsNone(retained['params']['result']['blockTime'])
        self.assertEqual(self.count('intents'),0);self.assertEqual(self.calls,[])

    def test_nested_event_not_direct_migration_child_is_not_candidate_hint(self):
        self.append_distinct_migration(null_time=True,depth=3)
        with patch.object(tool.time,'time',return_value=self.f.at+6650):
            self.assertEqual(self.invoke()['status'],'NO_CANDIDATE')
        self.assertEqual(self.count('intents'),0)

    def test_newest_completed_non_sol_migration_skipped_for_older_sol(self):
        unsupported=self.append_distinct_migration(non_sol=True,null_time=True)
        with sqlite3.connect(self.discovery) as c:
            raw=json.loads(c.execute('SELECT payload FROM raw_events ORDER BY seq DESC LIMIT 1').fetchone()[0])
        observations=tool.decode(raw)['program_observations']
        parent=next(o for o in observations if o.get('name')=='migrate_v2')
        event=next(o for o in observations if o.get('name')=='CompletePumpAmmMigrationEvent')
        self.assertEqual(parent['status'],'IDENTIFIED')
        self.assertEqual(event['status'],'EVENT_DECODED');self.assertIs(event['schema_complete'],True)
        self.assertEqual(event['fields']['mint'],parent['mint']);self.assertEqual(event['fields']['pool'],parent['pool'])
        with self.assertRaisesRegex(tool.migration.IntakeBlocked,'OBSERVED_POOL_BINDING_INVALID'):
            tool.migration._hints('preselection-probe',parent['mint'],parent['pool'],
                raw['params']['result']['signature'],raw['params']['result']['slot'],tool.PROVENANCE)
        result=self.invoke()
        self.assertEqual(result['status'],'DRY_RUN');self.assertEqual(result['hint']['mint'],self.mint)
        self.assertNotEqual(result['hint']['mint'],unsupported)
        self.assertEqual(self.count('intents'),0)
        with self.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM scans').fetchone()[0],0)

    def test_newest_retained_undecodable_notification_skips_without_integrity_waiver(self):
        self.append_distinct_migration(malformed=True)
        self.assertEqual(self.invoke()['hint']['mint'],self.mint)
        with sqlite3.connect(self.discovery) as c:
            c.execute('DROP TRIGGER raw_events_update')
            c.execute("UPDATE raw_events SET payload_hash=? WHERE seq=(SELECT MAX(seq) FROM raw_events)",('0'*64,))
            c.execute("CREATE TRIGGER raw_events_update BEFORE UPDATE ON raw_events BEGIN SELECT RAISE(ABORT,'Original discovery record is immutable'); END")
        with self.assertRaisesRegex(ValueError,'receipt invalid'):self.invoke()
        self.assertEqual(self.count('intents'),0);self.assertEqual(self.calls,[])

    def test_rejection_restart_replays_and_considers_only_distinct_unadmitted_mint(self):
        self.assertEqual(self.rejection()['status'],'TOKEN_REJECTED')
        mint=self.append_distinct_migration()
        with patch.object(tool.cli,'_credentials',side_effect=AssertionError('dry-run credentials')):
            result=self.invoke()
        self.assertEqual(result['status'],'DRY_RUN');self.assertEqual(result['hint']['mint'],mint)
        self.assertEqual(self.count('intents'),1);self.assertEqual(self.count('results'),1)
        with self.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM scans').fetchone()[0],1)
        self.insert_concurrent_pending()
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.calls,['getAccountInfo'])

    def test_freeze_only_and_both_authorities_are_explicit_rejections(self):
        self.assertEqual(self.rejection(mint_tag=0,freeze_tag=1)['status'],'TOKEN_REJECTED')
        self.assertEqual(self.invoke()['status'],'NO_CANDIDATE')

    def test_both_authorities_replayed_exactly(self):
        self.assertEqual(self.rejection(freeze_tag=1)['status'],'TOKEN_REJECTED')
        with sqlite3.connect(self.journal) as c:
            proof=json.loads(c.execute('SELECT payload FROM results').fetchone()[0])['result']
        self.assertEqual(proof['token_policy']['reasons'],['ACTIVE_MINT_AUTHORITY','ACTIVE_FREEZE_AUTHORITY'])
        self.assertEqual(proof['requests_after'],1)
        self.assertEqual(self.invoke()['status'],'NO_CANDIDATE')

    def test_invalid_option_with_authority_stays_unresolved(self):
        with self.assertRaises(ValueError):self.rejection(mint_tag=2)
        self.assertEqual(self.count('results'),0)
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.calls,['getAccountInfo'])

    def test_extra_nonallowlisted_zero_supply_stays_unresolved(self):
        with self.assertRaises(ValueError):self.rejection(zero_supply=True)
        self.assertEqual(self.count('results'),0)
        with self.assertRaises(ValueError):self.invoke()

    def test_rejection_restart_detects_corrupt_retained_mint_not_status_string(self):
        self.assertEqual(self.rejection()['status'],'TOKEN_REJECTED')
        with sqlite3.connect(self.journal) as c:
            proof=json.loads(c.execute('SELECT payload FROM results').fetchone()[0])['result']
        with sqlite3.connect(self.f.progress.store.path) as c:
            c.execute('UPDATE pages SET payload=? WHERE hash=?',(b'bad',proof['mint_response_hash']))
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.count('results'),1)

    def test_rejection_restart_detects_changed_charge(self):
        self.assertEqual(self.rejection()['status'],'TOKEN_REJECTED')
        with sqlite3.connect(self.f.progress.store.path) as c:
            c.execute('UPDATE ownership_budgets SET used=2')
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.count('results'),1)

    def test_recovery_race_before_rejection_publication_preserves_intent(self):
        original=tool.acquisition.acquire
        def acquire(*args,**kw):
            result=original(*args,**kw);self.insert_concurrent_pending();return result
        with patch.object(tool.acquisition,'acquire',side_effect=acquire):
            with self.assertRaises(ValueError):self.rejection()
        self.assertEqual(self.count('results'),0);self.assertEqual(self.count('intents'),1)
        with self.assertRaises(ValueError):self.invoke()

    def test_rejection_missing_retained_response_blocks_restart(self):
        self.assertEqual(self.rejection()['status'],'TOKEN_REJECTED')
        with sqlite3.connect(self.journal) as c:
            proof=json.loads(c.execute('SELECT payload FROM results').fetchone()[0])['result']
        with sqlite3.connect(self.f.progress.store.path) as c:
            c.execute('DELETE FROM pages WHERE hash=?',(proof['mint_response_hash'],))
        with self.assertRaises(ValueError):self.invoke()

    def test_rejection_changed_report_cannot_be_trusted_on_restart(self):
        self.assertEqual(self.rejection()['status'],'TOKEN_REJECTED')
        with self.f.jobs.connect() as c:
            row=c.execute('SELECT id,result FROM scans').fetchone()
            report=json.loads(row['result']);report['findings']=[]
            c.execute('UPDATE scans SET result=? WHERE id=?',(canonical(report),row['id']))
        with self.assertRaises(ValueError):self.invoke()
        self.assertEqual(self.count('results'),1)

    def test_pacing_waiter_before_rejection_publication_blocks(self):
        original=tool.acquisition.acquire
        def acquire(*args,**kw):
            result=original(*args,**kw)
            with sqlite3.connect(self.pacer) as c:
                c.execute('INSERT INTO waiters VALUES(?,?,?,?,?)',('rejection-waiter','helius','investigation',self.f.at,self.f.at+20))
            return result
        with patch.object(tool.acquisition,'acquire',side_effect=acquire):
            with self.assertRaises(ValueError):self.rejection()
        self.assertEqual(self.count('results'),0)
        with self.assertRaises(ValueError):self.invoke()

    def test_admission_race_after_selection_cannot_duplicate_complete_scan(self):
        def competitor():
            self.f.jobs.admit(self.mint,kind='BIRTH_ACQUISITION_V1',evidence_db=self.f.progress.store.path)
        with patch.object(tool.cli,'_credentials',side_effect=competitor),patch('desk.providers.helius_rpc',side_effect=AssertionError('provider')):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
        with self.f.jobs.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM scans').fetchone()[0],1)
        self.assertEqual(self.count('intents'),0);self.assertEqual(self.count('results'),0)


    def test_exact_operator_size_cost_rejection_is_preserved_and_not_retried(self):
        result=self.live(roundtrip_output=10_000_000)
        self.assertEqual(result['paper_status'],'COMPLETE')
        self.assertEqual(cycle._state(self.ledger,self.cfg)['positions'],{})
        with sqlite3.connect(self.journal) as c:
            record=json.loads(c.execute('SELECT payload FROM results').fetchone()[0])
        self.assertEqual(record['result']['outcomes'][0]['reason'],'COST_BUDGET')
        before=self.f.progress.admission(result['scan_id'])
        self.assertEqual(self.invoke()['status'],'NO_CANDIDATE')
        self.assertEqual(self.f.progress.admission(result['scan_id']),before)


    def insert_concurrent_pending(self):
        with tool.monitor._context(self.f.jobs.path,self.f.progress.store.path,self.ledger,self.cfg) as (store,ledger,state):
            intent=store.save({'kind':'concurrent_observation_intent_fixture','ledger':str(ledger)})
            with store.connect() as c:
                c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
                c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)',('concurrent-pending',intent))

    def test_acquisition_rechecks_concurrent_null_pass_before_rpc_preserves_charge(self):
        original=tool.acquisition.acquire
        def racing(*args,**kwargs):
            self.insert_concurrent_pending()
            return original(*args,**kwargs)
        with patch.object(tool.acquisition,'acquire',side_effect=racing):
            with self.assertRaises(ValueError):self.live()
        self.assertEqual(self.calls,[])
        with self.f.jobs.connect() as c:scan=c.execute('SELECT id FROM scans').fetchone()[0]
        self.assertEqual(self.f.progress.admission(scan)['requests_used'],1)
        self.assertEqual(self.count('intents'),1);self.assertEqual(self.count('results'),0)
        with patch('desk.providers.helius_rpc',side_effect=AssertionError('retry')):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)

    def test_intake_rechecks_concurrent_null_pass_before_http_preserves_original_charges(self):
        original=tool.migration.intake
        def racing(*args,**kwargs):
            self.insert_concurrent_pending()
            return original(*args,**kwargs)
        with patch.object(tool.migration,'intake',side_effect=racing):
            with self.assertRaises(ValueError):self.live()
        self.assertEqual(self.calls,['getAccountInfo','getSlot','getTransactionsForAddress'])
        with self.f.jobs.connect() as c:scan=c.execute('SELECT id FROM scans').fetchone()[0]
        self.assertEqual(self.f.progress.admission(scan)['requests_used'],3)
        self.assertEqual(self.count('intents'),1);self.assertEqual(self.count('results'),0)
        with patch.object(tool.migration,'intake',side_effect=AssertionError('retry')):
            with self.assertRaises(ValueError):self.invoke(execute=True,systemd_credentials=True)
