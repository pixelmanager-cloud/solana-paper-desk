"""Synthetic original bytes; real history/receipt/gate validators, no HTTP."""
import copy
from contextlib import closing
import sqlite3
from tests import legacy_null_pass
import unittest
from unittest.mock import patch
from desk import history_preparation_rejection as rejection,paper_terminal_reconciliation as terminal
from desk import paper_empty_history_reconciliation as recovery
from tools import history_first_paper_entry as entry


class ProspectiveEmptyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_history_preparation_phase import PreparationTests
        self.p=PreparationTests();self.p.setUp();self.addCleanup(self.p.doCleanups)

    def test_verified_empty_window_publishes_no_entry_and_allows_next_scan(self):
        p=self.p
        with self.assertRaisesRegex(entry.PreparationRejected,'HISTORY_FEATURE_EMPTY_WINDOW'):
            p.advance({'data':[]})
        before=p.progress.admission(p.target.scan_id)
        result=p.publish('HISTORY_FEATURE_EMPTY_WINDOW')
        self.assertEqual(result['attempt_refs'].__len__(),1)
        self.assertEqual(before['requests_used'],7)
        self.assertEqual(result['status'],'NO_ENTRY');self.assertFalse(result['entry_authorized'])
        self.assertEqual(rejection.verify(p.store,p.progress,result)['kind'],'history_first_paper_preparation_v4')
        self.assertEqual(p.publish('HISTORY_FEATURE_EMPTY_WINDOW'),result)
        self.assertEqual(terminal.gate(p.store,p.context['research_db'],(p.target.scan_id,)),'REJECTED_SCAN_RETIRED')
        self.assertIsNone(terminal.gate(p.store,p.context['research_db'],('b'*32,)))
        self.assertEqual(p.progress.admission(p.target.scan_id),before)

    def test_incomplete_empty_and_nonempty_windows_do_not_claim_no_entry(self):
        p=self.p
        p.advance({'data':[],'paginationToken':'next'})
        with self.assertRaises(ValueError):p.publish('HISTORY_FEATURE_EMPTY_WINDOW')
        self.assertIsNone(p.marker())

    def test_missing_captured_attempt_and_changed_ledger_stay_blocked(self):
        p=self.p
        with self.assertRaises(entry.PreparationRejected):p.advance({'data':[]})
        state=p.progress.snapshot(p.budget.history_id)
        keys=rejection._attempts(p.store,p.target.scan_id,6,7)
        with p.store.connect() as c:c.execute('DELETE FROM pages WHERE hash=?',(keys[0][0],))
        with self.assertRaises(ValueError):p.publish('HISTORY_FEATURE_EMPTY_WINDOW')
        self.assertIsNone(p.marker())

    def test_empty_receipt_requires_explicit_policy_and_strict_shapes(self):
        for malformed in ({},None,[],{'association':recovery.ASSOCIATION}):
            self.assertFalse(recovery.shape(malformed))
            with self.assertRaises(ValueError):recovery.approved(malformed)

    def test_replay_proven_missing_measurement_matrix(self):
        from tests.test_live_strategy_features import transaction
        from desk.security import base58
        from desk.programs import unbase58
        from tests.test_live_strategy_features import POOL
        for mode in ('sell_only','latest_slot_tie','stale_trade'):
            with self.subTest(mode=mode):
                from tests.test_history_preparation_phase import PreparationTests
                p=PreparationTests();p.setUp()
                try:
                    rows=[transaction(base58((i+1).to_bytes(64,'big')),
                          100 if mode=='latest_slot_tie' else 100+i,
                          p.as_of-60 if mode=='stale_trade' else p.as_of-1,
                          side='sell' if mode=='sell_only' else 'buy',
                          wallet=base58(bytes([i+1])*32)) for i in range(40)]
                    for raw in rows:
                        ix=raw['transaction']['message']['instructions'][0]
                        ix['data']=base58(unbase58(ix['data']).replace(unbase58(POOL),unbase58(p.target.pool)))
                    with self.assertRaisesRegex(entry.PreparationRejected,'HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE'):
                        p.advance({'data':rows})
                    before=p.progress.admission(p.target.scan_id)
                    result=p.publish('HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE')
                    self.assertTrue(result['bounds']['missing_measurements'])
                    rejection.verify(p.store,p.progress,result)
                    self.assertIsNone(terminal.gate(p.store,p.context['research_db'],('b'*32,)))
                    self.assertEqual(p.progress.admission(p.target.scan_id),before)
                finally:p.doCleanups()

    def test_one_buyer_measured_window_is_not_missing_input_rejection(self):
        from tests.test_live_strategy_features import transaction,POOL
        from desk.security import base58
        from desk.programs import unbase58
        p=self.p
        rows=[transaction(base58((i+1).to_bytes(64,'big')),100+i,p.as_of-1,
                          wallet=base58(bytes([9])*32)) for i in range(40)]
        for raw in rows:
            ix=raw['transaction']['message']['instructions'][0]
            ix['data']=base58(unbase58(ix['data']).replace(unbase58(POOL),unbase58(p.target.pool)))
        p.advance({'data':rows})
        measured=rejection.bounds(p.store,p.progress.snapshot(p.budget.history_id)['coverage'],
            cfg=p.cfg,as_of=p.as_of,semantics_version=2,required_measurements=True)
        self.assertEqual(measured['missing_measurements'],[])
        with self.assertRaises(ValueError):p.publish('HISTORY_REQUIRED_MEASUREMENTS_UNAVAILABLE')
        self.assertIsNone(p.marker())

class RetainedEmptyTests(unittest.TestCase):
    @staticmethod
    def legacy_null_pass():
        """These tests cover retrospective receipts for passes retained NULL by the
        pre-fix runtime, so the generic prospective publication is disabled for
        this fixture only (assertions unchanged; see test_cycle_no_entry)."""
        from desk import paper_cycle_no_entry
        return patch.object(paper_cycle_no_entry,'publish',side_effect=ValueError('legacy NULL fixture'))

    def setUp(self):
        legacy_null_pass.install(self)    # T22: these tests certify retained pre-T22 NULL/unresolved states
        import inspect,json,textwrap,time
        from pathlib import Path
        from tests import test_history_first_paper_entry as first,test_paper_cycle as protocol
        from tests.test_paper_read_sources import Response
        from desk import paper_cycle as cycle,paper_read_sources as transport,provider_pacing as pacing
        from desk.model import canonical,digest
        self.h=first.HistoryFirstTests();self.h.setUp();self.addCleanup(self.h.doCleanups)
        h=self.h;f=h.f;f.f.at=int(time.time());f.http_calls=[];f.sell_output=100_000_000
        progress=f.f.progress
        from desk.ownership_acquisition import _Setup
        _Setup(progress.store,f.f.jobs.descriptor(f.target.scan_id),progress.admission(f.target.scan_id))
        for _ in range(4):self.assertTrue(progress.reserve(f.target.scan_id))
        source=f.f.jobs.source(f.target.scan_id)
        report={'mint':f.target.mint,'eligible_for_trading':False,'calls':4};report['report_hash']=digest(report)
        source.update(status='COMPLETE',result=canonical(report))
        admission=progress.admission(f.target.scan_id)
        progress.prepare_source(f.target.scan_id,admission['descriptor_hash'],source)
        progress.seal_source(f.target.scan_id,admission['descriptor_hash'],digest(source))
        with f.f.jobs.connect() as c:c.execute("UPDATE scans SET status='COMPLETE',result=? WHERE id=?",(source['result'],f.target.scan_id))
        for _ in range(2):self.assertTrue(progress.reserve(f.target.scan_id))
        f.cfg=f.cfg|{'paper_usd_valuation_version':1};f.path=Path(h.root)/'empty-kraken.sqlite';cycle.initialize(f.path,f.cfg)
        from desk import kraken_pacing_migration
        kraken_policy=Path(h.root)/'empty-kraken-policy.json'
        kp=patch.object(kraken_pacing_migration,'POLICY',kraken_policy);kp.start();self.addCleanup(kp.stop)
        kraken_policy.write_text(canonical({'version':1,'pins':[]}))
        kraken_pin=kraken_pacing_migration.review_plan(h.pace)
        kraken_policy.write_text(canonical({'version':1,'pins':[kraken_pin]}));kraken_pacing_migration.migrate(h.pace)
        h.config.write_text(canonical(f.cfg));h.row['provenance']='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE';h.save()
        code=textwrap.dedent(inspect.getsource(f.actual_cycle));a=code.index('    class Opener:');b=code.index('    with ExitStack()')
        namespace={**vars(protocol),'outer':f};exec(textwrap.dedent(code[a:b]),namespace);delegate=namespace['Opener']()
        owner=self;self.empty_history=True
        class HTTP:
            def open(self,request,*,timeout):
                if owner.empty_history and request.method=='POST' and json.loads(request.data)['method']=='getTransactionsForAddress':
                    return Response(canonical({'jsonrpc':'2.0','id':transport.RPC_ID,'result':{'data':[],'paginationToken':None}}).encode())
                if 'api.kraken.com/0/public/Trades?' in request.full_url:
                    return Response(canonical({'error':[],'result':{'SOLUSD':[['100','1',time.time()-1,'s','l','',123]],'last':'123'}}).encode())
                return delegate.open(request,timeout=timeout)
        self.http=HTTP()
        original_intent=rejection.intent;original_bounds=rejection.bounds;original_reason=rejection.reason_for
        def historical_intent(*a,**k):return {**original_intent(*a,**k),'kind':'history_first_paper_preparation_v3'}
        def historical_bounds(*a,**k):return original_bounds(*a,**{**k,'required_measurements':False})
        def historical_reason(*a,**k):return original_reason(*a,**{**k,'empty_window':False})
        with patch.object(first.tool.cli,'_credentials'),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY','JUPITER_API_KEY':'SYNTHETIC_TEST_ONLY'}),patch.object(transport,'build_opener',return_value=HTTP()),patch.object(rejection,'intent',side_effect=historical_intent),patch.object(rejection,'bounds',side_effect=historical_bounds),patch.object(rejection,'reason_for',side_effect=historical_reason),self.legacy_null_pass():
            self.result=h.invoke(live=True,systemd_credentials=True)
        self.assertEqual(self.result['blockers'],['MARKET_PRODUCER_BLOCKED'],self.result)
        self.assertEqual(self.result['investigation_attempted_requests'],5)
        self.store=f.f.progress.store;self.progress=f.f.progress
        self.ctx={'research_db':str(f.f.jobs.path),'evidence_db':str(self.store.path),'ledger_db':str(f.path),'pacing_db':str(h.pace)}
        source=terminal.runtime.implementation_hash()
        dc={'source_hash':source,'config_hash':digest(f.cfg),'paths':{k:{'path':v} for k,v in self.ctx.items()},'journal':str(Path(h.root)/'empty-dispatch.sqlite')}
        from tools import paper_entry_dispatcher as dispatcher
        di={'context_hash':digest(dc),'hint':{'mint':f.target.mint,'pool':f.target.pool}}
        dr={'intent_hash':digest(di),'scan_id':f.target.scan_id,'result':self.result}
        with sqlite3.connect(dc['journal']) as c:
            for sql in dispatcher.SCHEMAS.values():c.execute(sql)
            for sql in dispatcher._guards().values():c.execute(sql)
            c.execute('INSERT INTO context VALUES(1,?,?)',(canonical(dc),digest(dc)))
            c.execute('INSERT INTO intents VALUES(?,?,?,?,?)',('d'*32,f.target.mint,'fixture-signature',canonical(di),digest(di)))
            c.execute('INSERT INTO results VALUES(?,?,?)',('d'*32,canonical(dr),digest(dr)))
        Path(dc['journal']).chmod(0o600)
        with closing(self.store.connect()) as c:
            prior=c.execute('SELECT id,intent_hash,outcome_hash FROM paper_observation_passes WHERE outcome_hash IS NOT NULL').fetchone()
            pending=c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
            history=c.execute('SELECT id FROM ownership_history').fetchone()[0]
        cov=self.progress.snapshot(history)['coverage']
        self.details={'preparation_pass_id':prior[0],'preparation_intent_hash':prior[1],'preparation_outcome_hash':prior[2],
            'history_id':history,'coverage_hash':cov['evidence_hash'],'dispatcher_context':dc,'dispatch_id':'d'*32,'dispatcher_result_hash':digest(dr)}
        self.pending=pending
        self.policy=Path(h.root)/'empty-policy.json';self.policy.write_text(canonical({'version':1,'associations':[]}))
        policy_patch=patch.object(recovery,'POLICY',self.policy);policy_patch.start();self.addCleanup(policy_patch.stop)

    def test_original_empty_window_proof_and_append_preserve_null_and_usage(self):
        from desk.model import canonical
        f=self.h.f
        pin=recovery.plan(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),f.cfg,
            pass_id=self.pending[0],outcome_hash=self.result['evidence_hash'],empty_history=self.details,pacing_db=self.ctx['pacing_db'])
        self.policy.write_text(canonical({'version':1,'associations':[pin]}))
        before=self.progress.admission(f.target.scan_id)
        self.assertEqual(recovery.reconcile(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),f.cfg,pin=pin)['status'],'RECORDED')
        self.assertEqual(recovery.reconcile(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),f.cfg,pin=pin)['status'],'ALREADY_RECORDED')
        self.assertIsNone(terminal.gate(self.store,self.ctx['research_db'],()))
        self.assertEqual(terminal.gate(self.store,self.ctx['research_db'],(f.target.scan_id,)),'REJECTED_SCAN_RETIRED')
        self.assertEqual(before,self.progress.admission(f.target.scan_id))
        from desk import paper_cycle as cycle
        from desk.ledger import Ledger
        from desk import engine
        with closing(Ledger(f.path,must_exist=True)) as ledger:
            ledger.apply({**cycle.INIT,'event_id':'later-valid-clock','ts':int(__import__('time').time())},
                         f.cfg,engine.transition,engine.initial_state)
        self.assertIsNone(terminal.gate(self.store,self.ctx['research_db'],()))
        with closing(self.store.connect()) as c:self.assertIsNone(c.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?',(self.pending[0],)).fetchone()[0])

    def test_changed_charge_and_missing_fresh_capture_cannot_reconcile(self):
        f=self.h.f
        before=self.progress.admission(f.target.scan_id)
        self.assertTrue(self.progress.reserve(f.target.scan_id))
        with self.assertRaises(ValueError):
            recovery.plan(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),f.cfg,
                pass_id=self.pending[0],outcome_hash=self.result['evidence_hash'],empty_history=self.details,pacing_db=self.ctx['pacing_db'])
        self.assertEqual(self.progress.admission(f.target.scan_id)['requests_used'],before['requests_used']+1)
        self.assertEqual(terminal.gate(self.store,self.ctx['research_db'],()),'OBSERVATION_RECOVERY_REQUIRED')

    def test_missing_capture_stays_pending(self):
        f=self.h.f
        with closing(self.store.connect()) as c:c.execute('DELETE FROM pages WHERE hash=?',(self.result['attempt_refs'][0],))
        with self.assertRaises(ValueError):
            recovery.plan(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),f.cfg,
                pass_id=self.pending[0],outcome_hash=self.result['evidence_hash'],empty_history=self.details,pacing_db=self.ctx['pacing_db'])
        with closing(self.store.connect()) as c:self.assertIsNone(c.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?',(self.pending[0],)).fetchone()[0])

    def test_post_recovery_real_buy_monitor_full_exit(self):
        import time
        from dataclasses import replace
        from desk import paper_cycle as cycle,paper_read_sources as transport
        from desk.model import canonical
        from desk.programs import unbase58
        from desk.security import base58
        from desk.ownership_acquisition import _Setup
        from desk.monitoring_budget import MonitoringBudget
        from desk.quote_execution import raw_quantity
        f=self.h.f
        pin=recovery.plan(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),f.cfg,
            pass_id=self.pending[0],outcome_hash=self.result['evidence_hash'],empty_history=self.details,pacing_db=self.ctx['pacing_db'])
        self.policy.write_text(canonical({'version':1,'associations':[pin]}))
        recovery.reconcile(*(self.ctx[k] for k in ('research_db','evidence_db','ledger_db')),f.cfg,pin=pin)
        retired=f.target.scan_id;original=self.progress.admission(retired)
        f.f.at=int(time.time());f.target=f.f.target()
        _Setup(self.store,f.f.jobs.descriptor(f.target.scan_id),self.progress.admission(f.target.scan_id))
        self.empty_history=False
        manifest=self.store.load(f.item.graduation_refs[0]);response=copy.deepcopy(self.store.load(manifest['response_hash']))
        raw=response['data'][0];raw['blockTime']=f.f.at-600
        ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        data=bytearray(unbase58(ix['data']));data[136:144]=raw['blockTime'].to_bytes(8,'little',signed=True);ix['data']=base58(data)
        key=self.store.save(response);ref=self.store.save({**manifest,'response_hash':key})
        item=replace(f.item,target=f.target,provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',
                     graduated_at=f.f.at-600,graduation_refs=(ref,),history_as_of=None)
        with patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY','JUPITER_API_KEY':'SYNTHETIC_TEST_ONLY'}),patch.object(transport,'build_opener',return_value=self.http):
            result=cycle.run_once(f.f.jobs.path,self.store.path,f.path,f.cfg,candidates=(item,),dependency_blockers=())
            self.assertEqual(result['status'],'COMPLETE',result)
            self.assertTrue(any(x.get('side')=='buy' for x in result['outcomes']),result)
            position=cycle._state(f.path,f.cfg)['positions'][f.target.mint]
            allowance=MonitoringBudget(self.store,f.path,f.cfg);allowance.provision()
            held=replace(item,target=replace(f.target,amount_raw=raw_quantity(position['qty'],position['quote_execution']['mint_decimals'])))
            f.f.at=int(time.time());f.sell_output=10_000_000
            mark=cycle.run_once(f.f.jobs.path,self.store.path,f.path,f.cfg,candidates=(),position_targets=(held,),dependency_blockers=(),monitoring=True)
            self.assertEqual(mark['status'],'COMPLETE',mark)
            self.assertTrue(cycle._state(f.path,f.cfg)['positions'])
            f.sell_output=1_000_000;f.f.at=int(time.time())
            exited=cycle.run_once(f.f.jobs.path,self.store.path,f.path,f.cfg,candidates=(),position_targets=(held,),dependency_blockers=(),monitoring=True)
            self.assertEqual(exited['status'],'COMPLETE',exited)
            self.assertTrue(any(x.get('side')=='sell' for x in exited['outcomes']),exited)
        self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
        self.assertEqual(self.progress.admission(retired),original)
        self.assertIsNone(terminal.gate(self.store,self.ctx['research_db'],()))
