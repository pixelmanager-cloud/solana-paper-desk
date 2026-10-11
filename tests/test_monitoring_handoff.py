"""Actual archived 8098 source; synthetic DBs/config/receipts, no provider I/O."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
import zipfile
from unittest.mock import patch

from desk import monitoring_handoff as handoff, runtime_compatibility as runtime, allowance_policy as grants
from desk import paper_cycle, engine, quote_execution as quote, provider_pacing
from desk.ledger import Ledger
from desk.model import canonical, digest
from desk.monitoring_budget import MonitoringBudget, MonitoringBlocked
from tests import test_runtime_compatibility as runtime_fixtures
from tests.test_runtime_compatibility import dump, OLD
from tests.helpers import T

DEPLOYED='8098b4033adff886006e2f6af5ccb0ce4bfa02e242f5fb1b4c7402c05ddaa8c9'
ARCHIVE=Path(__file__).resolve().parents[1]/'fixtures/monitoring-handoff-predecessor-desk.zip'


class MonitoringHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f=runtime_fixtures.RuntimeCompatibilityTests();self.addCleanup(self.f.doCleanups);self.f.setUp()
        self.root=self.f.root;self.research=self.f.research;self.evidence=self.f.evidence;self.old=self.f.ledger
        from desk.paper_observation_collector import ObservationTarget
        self.target=ObservationTarget(next(iter(self.f.context.sources)),str(self.f.context.protocol.mint),
            str(self.f.context.protocol.pool),self.f.context.taker,10000000)
        self.oldroot=self.root/'deployed';self.oldroot.mkdir()
        with zipfile.ZipFile(ARCHIVE) as z:z.extractall(self.oldroot)
        self.edge=dict(self.f.edge,successor=DEPLOYED)
        self.f.policy.write_text(canonical({'version':1,'transitions':[self.edge]}))
        initial='''import json,sys
from desk.evidence import EvidenceStore
from desk.monitoring_budget import MonitoringBudget
a=json.loads(sys.argv[1]);store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
MonitoringBudget(store,a['old'],a['cfg']).provision()
'''
        result=subprocess.run([sys.executable,'-c',initial,canonical({'evidence':str(self.evidence),'old':str(self.old),'cfg':self.f.cfg})],cwd=self.f.oldroot,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.oldrun('''from desk import runtime_compatibility as rc
from desk.evidence import EvidenceStore
from desk.monitoring_budget import MonitoringBudget
from desk import allowance_policy as grants
from desk.model import digest
assert rc.implementation_hash()==a['deployed']
rc.POLICY=Path(a['policy'])
rc.transition(a['research'],a['evidence'],a['old'],a['cfg'],predecessor=a['original'],successor=a['deployed'])
store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
budget=MonitoringBudget(store,a['old'],a['cfg'],clock=lambda:a['at'])
with store.connect() as c:
 c.execute('BEGIN IMMEDIATE')
 prepared=budget.prepare_upgrade(c,at=a['at'],provenance=grants.PROVENANCE)
 budget.activate_upgrade(c,prepared);c.commit()
''')
        self.f.context.jobs.upgrade_allowance(at=T,provenance=grants.PROVENANCE)
        self.new=self.root/'token-profile-experiment.sqlite'
        # Opaque synthetic config delta. It grants no Token2022 profile semantics.
        self.cfg=self.f.cfg|{'paper_token_profile_version':1}
        paper_cycle.initialize(self.new,self.cfg)
        self.pacing=self.root/'shared-pacing.sqlite';provider_pacing.initialize(self.pacing)
        self.env=patch.dict(os.environ,{provider_pacing.ENV:str(self.pacing)});self.env.start();self.addCleanup(self.env.stop)
        with sqlite3.connect(self.old) as c:runtime_hash=c.execute('SELECT payload_hash FROM paper_runtime_transition').fetchone()[0]
        with sqlite3.connect(self.evidence) as c:grant_hash=grants.monitoring_policy(c)[1]
        with self.f.context.jobs.connect() as c:research_hash=grants.read(c,grants.RESEARCH)[1]
        self.pins={'context':{'research_db':str(self.research),'evidence_db':str(self.evidence),
            'old_ledger_db':str(self.old),'new_ledger_db':str(self.new),'pacing_db':str(self.pacing)},
            'old_source':DEPLOYED,'new_source':runtime.implementation_hash(),
            'old_config_hash':digest(self.f.cfg),'new_config_hash':digest(self.cfg),
            'old_runtime_hash':runtime_hash,'old_grant_hash':grant_hash,'research_grant_hash':research_hash}
        self.policy=self.root/'handoff-policy.json';self.policy.write_text(canonical({'version':1,'handoffs':[self.pins]}))
        p=patch.object(handoff,'POLICY',self.policy);p.start();self.addCleanup(p.stop)
        self.store=self.f.context.progress.store
        self.active=MonitoringBudget(self.store,self.new,self.cfg,clock=lambda:T+1)

    def oldrun(self,body,**extras):
        args={'deployed':DEPLOYED,'original':OLD,'policy':str(self.f.policy),
              'research':str(self.research),'evidence':str(self.evidence),'old':str(self.old),
              'cfg':self.f.cfg,'at':T,**extras}
        script='import json,sys\nfrom pathlib import Path\na=json.loads(sys.argv[1])\n'+body
        env=os.environ.copy();env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        result=subprocess.run([sys.executable,'-c',script,canonical(args)],cwd=self.oldroot,env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        return result.stdout

    def activate(self,**kw):
        return handoff.activate(self.research,self.evidence,self.old,self.new,self.pacing,self.cfg,
                                **({'pins':self.pins,'at':T}|kw))

    def charge_original(self, *, blocked=None):
        # Retained synthetic completed transport record; not a provider response.
        receipt={'kind':'open_paper_monitoring_reservation_v2','id':1,'total_used':1,'cap':3600,
                 'policy_hash':self.pins['old_grant_hash'],'window_seconds':3600,'reserved_at':T,
                 'checkpoint_hash':'a'*64,'mint':self.target.mint}
        record={'monitoring_reservation':receipt,'scan_id':'synthetic-completed-old',
                'method':'getSlot','params':[{'commitment':'finalized'}],'failure_code':None}
        key=self.store.save(record)
        with self.store.connect() as c:
            c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)',
                      (1,T,record['scan_id'],receipt['mint'],receipt['checkpoint_hash'],record['method'],digest(record['params'])))
            c.execute('INSERT INTO paper_monitoring_outcomes VALUES(1,?)',(key,))
            c.execute('UPDATE paper_monitoring_budget SET high_water=?,total=1,blocked=?',(T,blocked))
        return key

    def buy_new(self):
        from tests.test_quote_execution_v3_seam import QuoteV3SeamTests
        fixture=QuoteV3SeamTests();self.addCleanup(fixture.doCleanups);fixture.setUp()
        target=self.target
        fixture.fixture.mint=target.mint;fixture.fixture.pool=target.pool;fixture.fixture.wallet=target.taker
        buy=fixture.fixture.quote('buy',10_000_000,1_000_000,at=T+1)
        raw=quote.output_raw(buy,self.cfg);sell=fixture.fixture.quote('sell',raw,10_000_000,at=T+1)
        event=fixture.market(T+1);event['paper_source_evidence']={'scan_id':target.scan_id,'collector_refs':[]}
        ledger=Ledger(self.new,must_exist=True)
        try:out=ledger.apply(event,self.cfg,quote.bind_transition(event,(buy,sell)),engine.initial_state)
        finally:ledger.close()
        self.assertTrue(any(row.get('side')=='buy' for row in out))
        return target

    def test_preserves_originals_shared_usage_and_separate_experiment_restart(self):
        key=self.charge_original();old=dump(self.old);new=dump(self.new);research=dump(self.research);pacing=dump(self.pacing)
        with self.store.connect() as c:
            budget=c.execute('SELECT * FROM paper_monitoring_budget').fetchall();grant=grants.monitoring_policy(c)
            old_rows=c.execute('SELECT * FROM paper_monitoring_reservations').fetchall();outcomes=c.execute('SELECT * FROM paper_monitoring_outcomes').fetchall()
        result=self.activate();self.assertEqual(result['status'],'BOUND');self.assertTrue(result['separate_experiment'])
        self.assertEqual(dump(self.old),old);self.assertEqual(dump(self.new),new);self.assertEqual(dump(self.research),research);self.assertEqual(dump(self.pacing),pacing)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_budget').fetchall(),[(budget[0][0],3)+budget[0][2:]])
            self.assertEqual(grants.monitoring_policy(c),grant)
            self.assertEqual([r[:7] for r in c.execute('SELECT * FROM paper_monitoring_reservations')],old_rows)
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_outcomes').fetchall(),outcomes)
        before=dump(self.evidence);self.assertEqual(self.activate(at=T+100)['status'],'ALREADY_BOUND');self.assertEqual(dump(self.evidence),before)
        self.assertEqual(self.active.snapshot()['remaining'],3599);self.assertEqual(self.store.load(key)['failure_code'],None)
        target=self.buy_new()
        receipt=self.active.reserve_read(self.f.context.progress,target.scan_id,'getSlot',[{'commitment':'finalized'}])
        self.assertEqual((receipt['id'],receipt['context_hash']),(2,result['binding_hash']))
        record={'monitoring_reservation':receipt,'scan_id':target.scan_id,'method':'getSlot','params':[{'commitment':'finalized'}],'failure_code':None}
        self.active.retain_outcome(receipt,self.store.save(record))
        restarted=MonitoringBudget(self.store,self.new,self.cfg,clock=lambda:T+1)
        self.assertEqual(restarted.snapshot()['remaining'],3598)
        self.assertEqual(self.f.context.progress.admission(target.scan_id)['requests_used'],0)

    def test_frozen_deployed_binary_cannot_spend_even_after_old_code_entry(self):
        self.activate()
        output=self.oldrun('''from desk import runtime_compatibility as rc, engine, quote_execution as qe
from desk.ledger import Ledger
from desk.evidence import EvidenceStore
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from desk.history_progress import HistoryProgress
from tests.test_quote_execution_v3_seam import QuoteV3SeamTests
rc.POLICY=Path(a['policy']);assert rc.implementation_hash()==a['deployed']
f=QuoteV3SeamTests();f.setUp()
try:
 f.fixture.mint=a['mint'];f.fixture.pool=a['pool']
 e=f.market();e['paper_source_evidence']={'scan_id':a['scan'],'collector_refs':[]}
 buy=f.fixture.quote('buy',10_000_000,1_000_000);sell=f.fixture.quote('sell',qe.output_raw(buy,a['cfg']),10_000_000)
 ledger=Ledger(a['old'],must_exist=True)
 out=ledger.apply(e,a['cfg'],qe.bind_transition(e,(buy,sell)),engine.initial_state);ledger.close()
 assert any(row.get('side')=='buy' for row in out)
 store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
 progress=HistoryProgress.__new__(HistoryProgress);progress.store=store
 budget=MonitoringBudget(store,a['old'],a['cfg'],clock=lambda:a['at'])
 with store.connect() as c:
  before=c.execute('SELECT high_water,total,blocked FROM paper_monitoring_budget').fetchone()
  original_sql=list(c.iterdump())
 try:budget.reserve_read(progress,a['scan'],'getSlot',[{'commitment':'finalized'}])
 except MonitoringBlocked as exc:assert exc.code=='MONITORING_ACCOUNTING_INVALID';print(exc.code)
 else:raise AssertionError('Frozen old binary spent allowance')
 with store.connect() as c:
  assert c.execute('SELECT high_water,total,blocked FROM paper_monitoring_budget').fetchone()==before
  assert c.execute('SELECT count(*) FROM paper_monitoring_reservations').fetchone()==(0,)
  assert list(c.iterdump())==original_sql
 from desk.paper_read_sources import PaperReadSources,PaperReadError
 from unittest.mock import patch
 with patch('desk.paper_read_sources.os.environ.get') as credentials,patch('desk.paper_read_sources.build_opener') as opener:
  try:PaperReadSources(progress,a['scan'],monitoring_budget=budget).rpc('getSlot',[{'commitment':'finalized'}],timeout_seconds=1)
  except PaperReadError as exc:assert exc.code=='MONITORING_ACCOUNTING_INVALID'
  else:raise AssertionError('Frozen old transport performed I/O')
  credentials.assert_not_called();opener.assert_not_called()
 with store.connect() as c:assert list(c.iterdump())==original_sql
finally:f.doCleanups()
''',**{'scan':self.target.scan_id,'mint':str(self.f.context.protocol.mint),'pool':str(self.f.context.protocol.pool)})
        self.assertIn('MONITORING_ACCOUNTING_INVALID',output)
        # Retired history changed: successor must also refuse, never hide it.
        with self.assertRaises(MonitoringBlocked):self.active.snapshot()

    def test_legacy_sql_insert_and_current_old_context_refuse(self):
        self.activate();before=dump(self.evidence)
        retired=MonitoringBudget(self.store,self.old,self.f.cfg,clock=lambda:T)
        with self.assertRaises(MonitoringBlocked):retired.snapshot()
        with self.store.connect() as c:
            with self.assertRaises(sqlite3.Error):c.execute('INSERT INTO paper_monitoring_reservations VALUES(1,0,\'s\',\'m\',\'h\',\'getSlot\',\'h\')')
            with self.assertRaises(sqlite3.Error):c.execute("INSERT INTO paper_monitoring_reservations(id,at,scan_id,mint,checkpoint_hash,method,params_hash) VALUES(1,0,'s','m','h','getSlot','h')")
        self.assertEqual(dump(self.evidence),before)

    def test_latches_usage_and_clock_floor_survive_handoff(self):
        self.charge_original(blocked='CLOCK_ROLLBACK');self.activate(at=T+100)
        self.assertIn('MONITORING_RECOVERY_REQUIRED',self.active.snapshot()['blockers'])
        self.assertIn('MONITORING_CLOCK_ROLLBACK',self.active.snapshot()['blockers'])
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT high_water,total,blocked FROM paper_monitoring_budget').fetchone(),(T,1,'CLOCK_ROLLBACK'))

    def test_wrong_pins_missing_paths_and_unreviewed_policy_are_nonmutating(self):
        before=dump(self.evidence)
        for key in ('old_source','new_source','old_config_hash','new_config_hash','old_runtime_hash','old_grant_hash','research_grant_hash'):
            pins=copy.deepcopy(self.pins);pins[key]='0'*64
            with self.assertRaises(ValueError):self.activate(pins=pins)
            self.assertEqual(dump(self.evidence),before)
        self.policy.write_text('{"version":1,"handoffs":[]}')
        with self.assertRaises(ValueError):self.activate()
        self.assertEqual(dump(self.evidence),before)

    def test_atomic_failure_rolls_back_column_receipt_and_fence(self):
        before=dump(self.evidence)
        original=handoff.validate_active
        def interrupted(c,budget):
            if c.execute('SELECT 1 FROM sqlite_master WHERE name=?',(handoff.TABLE,)).fetchone():
                raise ValueError('synthetic interruption AFTER additive DDL')
            return original(c,budget)
        with patch.object(handoff,'validate_active',side_effect=interrupted):
            with self.assertRaises(MonitoringBlocked):self.activate()
        self.assertEqual(dump(self.evidence),before)

    def test_fence_guard_or_receipt_damage_refuses_readers_without_mutation(self):
        self.activate()
        with self.store.connect() as c:c.execute('DROP TRIGGER '+handoff.FENCE)
        before=dump(self.evidence)
        with self.assertRaises(MonitoringBlocked):self.active.snapshot()
        with self.assertRaises(ValueError):self.activate()
        self.assertEqual(dump(self.evidence),before)

    def test_untraded_new_ledger_pending_attempt_and_pacing_mismatch_refuse(self):
        before=dump(self.evidence)
        with patch.dict(os.environ,{provider_pacing.ENV:str(self.root/'different-pacing.sqlite')}):
            with self.assertRaises(ValueError):self.activate()
        self.assertEqual(dump(self.evidence),before)
        self.buy_new()
        with self.assertRaises(ValueError):self.activate()
        self.assertEqual(dump(self.evidence),before)

    def test_all_canonical_locks_and_unrelated_context_fail_nonmutating(self):
        import fcntl
        before=dump(self.evidence)
        for path in (str(self.research)+'.jobs-worker.lock',str(self.evidence)+'.ownership-invocation.lock',
                     str(self.old)+'.paper-cycle.lock',str(self.new)+'.paper-cycle.lock'):
            with open(path,'a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with self.assertRaises(ValueError):self.activate()
            self.assertEqual(dump(self.evidence),before)
        unrelated=self.root/'unrelated.sqlite';unrelated.write_text('SYNTHETIC_NOT_A_DATABASE')
        with self.assertRaises(ValueError):
            handoff.activate(unrelated,self.evidence,self.old,self.new,self.pacing,self.cfg,pins=self.pins,at=T)
        self.assertEqual(dump(self.evidence),before)

    def test_charged_lifetime18_and_nonempty_shared_pacing_state_preserved(self):
        for _ in range(18):self.assertTrue(self.f.context.progress.reserve(self.target.scan_id))
        self.assertFalse(self.f.context.progress.reserve(self.target.scan_id))
        pacer=provider_pacing.Pacer(self.pacing,clock=lambda:T)
        ticket=pacer.acquire('helius',timeout_seconds=1);pacer.finish('helius',ticket)
        before=dump(self.pacing);research=dump(self.research);admission=self.f.context.progress.admission(self.target.scan_id)
        self.activate()
        self.assertEqual(dump(self.pacing),before);self.assertEqual(dump(self.research),research)
        self.assertEqual(self.f.context.progress.admission(self.target.scan_id),admission)
        self.assertEqual(admission['requests_used'],18)

    def test_same_binding_replay_after_new_trade_and_mismatch_refusal(self):
        self.activate();self.buy_new();before=dump(self.evidence)
        self.assertEqual(self.activate(at=T+2)['status'],'ALREADY_BOUND')
        self.assertEqual(dump(self.evidence),before)
        self.policy.write_text('{"version":1,"handoffs":[]}')
        with self.assertRaises(MonitoringBlocked):self.active.snapshot()
        self.assertEqual(dump(self.evidence),before)

    def test_real_cli_malformed_and_unreviewed_inputs_no_traceback_or_mutation(self):
        cfg=self.root/'new-config.json';cfg.write_text(canonical(self.cfg))
        pins=self.root/'pins.json';pins.write_text('{"context":null}')
        before=dump(self.evidence)
        command=[sys.executable,'-m','desk.monitoring_handoff','--research-db',str(self.research),
            '--evidence-db',str(self.evidence),'--old-ledger-db',str(self.old),'--new-ledger-db',str(self.new),
            '--pacing-db',str(self.pacing),'--new-config',str(cfg),'--reviewed-pins',str(pins)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,2);self.assertEqual(result.stderr,'')
        self.assertEqual(json.loads(result.stdout)['status'],'UNAVAILABLE')
        self.assertEqual(dump(self.evidence),before)

    def test_pending_old_attempt_prevents_publication_and_preserves_originals(self):
        with self.store.connect() as c:
            c.execute("INSERT INTO paper_monitoring_reservations VALUES(1,?,'pending','mint',?,'getSlot',?)",(T,'a'*64,digest([{'commitment':'finalized'}])))
            c.execute('UPDATE paper_monitoring_budget SET high_water=?,total=1',(T,))
        before=dump(self.evidence)
        with self.assertRaisesRegex(ValueError,'Unresolved'):self.activate()
        self.assertEqual(dump(self.evidence),before)

    def test_budget_downgrade_and_wrong_receipt_binding_fail_without_changes(self):
        result=self.activate();target=self.buy_new()
        with self.store.connect() as c:
            before=list(c.iterdump())
            with self.assertRaises(sqlite3.Error):c.execute('UPDATE paper_monitoring_budget SET version=2')
            self.assertEqual(list(c.iterdump()),before)
        receipt=self.active.reserve_read(self.f.context.progress,target.scan_id,'getSlot',[{'commitment':'finalized'}])
        forged=dict(receipt,context_hash='0'*64)
        record={'monitoring_reservation':forged,'scan_id':target.scan_id,'method':'getSlot','params':[{'commitment':'finalized'}],'failure_code':None}
        key=self.store.save(record);before=dump(self.evidence)
        with self.assertRaises(MonitoringBlocked):self.active.retain_outcome(forged,key)
        self.assertEqual(dump(self.evidence),before)
        record['monitoring_reservation']=receipt;key=self.store.save(record)
        self.active.retain_outcome(receipt,key)
        self.assertEqual(self.active.snapshot()['total_used'],1)
        self.assertEqual(receipt['context_hash'],result['binding_hash'])

    def test_verified_old_open_position_prevents_handoff_without_mutation(self):
        self.oldrun('''from desk import runtime_compatibility as rc,engine,quote_execution as qe
from desk.ledger import Ledger
from tests.test_quote_execution_v3_seam import QuoteV3SeamTests
rc.POLICY=Path(a['policy'])
f=QuoteV3SeamTests();f.setUp()
try:
 e=f.market();ledger=Ledger(a['old'],must_exist=True)
 out=ledger.apply(e,a['cfg'],qe.bind_transition(e,(f.fixture.buy,f.fixture.exit)),engine.initial_state)
 assert any(row.get('side')=='buy' for row in out);ledger.close()
finally:f.doCleanups()
''')
        before=dump(self.evidence);old=dump(self.old)
        with self.assertRaisesRegex(ValueError,'Zero old positions'):self.activate()
        self.assertEqual(dump(self.evidence),before);self.assertEqual(dump(self.old),old)

    def test_actual_cli_reviewed_fixture_handoff_and_replay(self):
        cfg=self.root/'new-config.json';cfg.write_text(canonical(self.cfg))
        pins=self.root/'pins.json';pins.write_text(canonical(self.pins))
        script='''import sys
from pathlib import Path
from desk import monitoring_handoff as handoff,runtime_compatibility as runtime
handoff.POLICY=Path(sys.argv[1]);runtime.POLICY=Path(sys.argv[2])
raise SystemExit(handoff.main(sys.argv[3:]))
'''
        command=[sys.executable,'-c',script,str(self.policy),str(self.f.policy),
            '--research-db',str(self.research),'--evidence-db',str(self.evidence),
            '--old-ledger-db',str(self.old),'--new-ledger-db',str(self.new),'--pacing-db',str(self.pacing),
            '--new-config',str(cfg),'--reviewed-pins',str(pins)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'BOUND')
        before=dump(self.evidence)
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'ALREADY_BOUND')
        self.assertEqual(dump(self.evidence),before)
