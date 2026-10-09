"""Synthetic compiled predecessor + actual runtime receipt; no provider/signer I/O.

The predecessor is this desk plus an inert fixture marker. Hashes are recomputed,
not mocked. This proves the local reviewed identity contract, not authentication
of provider observations or the deployment environment.
"""
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import unittest

from desk import runtime_compatibility as runtime, monitoring_handoff as handoff
from desk.model import canonical,digest
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from tests import test_monitoring_handoff as fixtures
from tests.test_runtime_compatibility import dump
from tests.helpers import T


class MonitoringRuntimeUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MonitoringHandoffTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.root=self.f.root;self.source=runtime.implementation_hash()
        self.originroot=self.root/'synthetic-origin';self.originroot.mkdir()
        desk=Path(runtime.__file__).parent
        shutil.copytree(desk,self.originroot/'desk',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (self.originroot/'desk/synthetic_origin_marker.py').write_text('# Synthetic predecessor identity only.\n')
        source_root=self.originroot/'desk'
        self.origin=digest({str(p.relative_to(source_root)):p.read_text() for p in sorted(source_root.rglob('*'))
                           if p.is_file() and p.suffix in ('.py','.json')})
        self.assertNotEqual(self.origin,self.source)
        # Replace only an untraded, disposable synthetic setup ledger, before any
        # handoff. The operator path never initializes/replaces an existing DB.
        self.f.new.unlink()
        self.f.pins['new_source']=self.origin
        self.f.policy.write_text(canonical({'version':1,'handoffs':[self.f.pins]}))
        self.f.charge_original()
        args={'source':self.origin,'new':str(self.f.new),'cfg':self.f.cfg,'pins':self.f.pins,
              'policy':str(self.f.policy),'runtime_policy':str(self.f.f.policy),'at':T}
        self.run_origin('''from desk import paper_cycle
from desk import runtime_compatibility as runtime,monitoring_handoff as handoff
assert runtime.implementation_hash()==a['source']
runtime.POLICY=Path(a['runtime_policy']);handoff.POLICY=Path(a['policy'])
paper_cycle.initialize(a['new'],a['cfg'])
c=a['pins']['context']
handoff.activate(c['research_db'],c['evidence_db'],c['old_ledger_db'],c['new_ledger_db'],c['pacing_db'],a['cfg'],pins=a['pins'],at=a['at'])
''',args)
        self.edge={'predecessor':self.origin,'successor':self.source,'config_hash':digest(self.f.cfg),
                   'context':{'research_db':str(self.f.research),'evidence_db':str(self.f.evidence),'ledger_db':str(self.f.new)}}

    def run_origin(self,body,args):
        script='import json,sys\nfrom pathlib import Path\na=json.loads(sys.argv[1])\n'+body
        result=subprocess.run([sys.executable,'-c',script,canonical(args)],cwd=self.originroot,
                              capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def upgrade(self):
        self.f.f.policy.write_text(canonical({'version':1,'transitions':[self.f.edge,self.edge]}))
        return runtime.transition(self.f.research,self.f.evidence,self.f.new,self.f.cfg,
                                  predecessor=self.origin,successor=self.source)

    def unchanged(self):
        return [dump(p) for p in (self.f.old,self.f.evidence,self.f.research,self.f.pacing)]

    def test_exact_receipt_preserves_experiment_binding_charges_and_restart(self):
        original=self.unchanged();ledger=dump(self.f.new)
        with self.assertRaises(MonitoringBlocked):self.f.active.snapshot()
        self.assertEqual(self.upgrade()['status'],'RECORDED')
        self.assertEqual(self.unchanged(),original)
        self.assertTrue(set(ledger)<=set(dump(self.f.new)))
        with sqlite3.connect(self.f.new) as c:
            self.assertEqual(c.execute("SELECT value FROM metadata WHERE key='implementation_hash'").fetchone(),(self.origin,))
            self.assertEqual(runtime.require_runtime(c),self.source)
        active=MonitoringBudget(self.f.store,self.f.new,self.f.cfg,clock=lambda:T+1)
        self.assertEqual(active.snapshot()['remaining'],3599)
        self.assertEqual(active.snapshot()['high_water'],T)
        before=dump(self.f.new);self.assertEqual(self.upgrade()['status'],'ALREADY_RECORDED');self.assertEqual(dump(self.f.new),before)
        target=self.f.buy_new()
        receipt=active.reserve_read(self.f.f.context.progress,target.scan_id,'getSlot',[{'commitment':'finalized'}])
        self.assertEqual(receipt['id'],2)
        with self.f.store.connect() as c:self.assertEqual(receipt['context_hash'],handoff.read(c)[1])
        record={'monitoring_reservation':receipt,'scan_id':target.scan_id,'method':'getSlot',
                'params':[{'commitment':'finalized'}],'failure_code':None}
        active.retain_outcome(receipt,self.f.store.save(record))
        restarted=MonitoringBudget(self.f.store,self.f.new,self.f.cfg,clock=lambda:T+1)
        self.assertEqual(restarted.snapshot()['remaining'],3598)
        self.assertEqual(dump(self.f.old),original[0]);self.assertEqual(dump(self.f.research),original[2]);self.assertEqual(dump(self.f.pacing),original[3])

    def test_unreviewed_transition_and_missing_receipt_fail_nonmutating(self):
        before=self.unchanged();ledger=dump(self.f.new)
        with self.assertRaises(ValueError):runtime.transition(self.f.research,self.f.evidence,self.f.new,self.f.cfg,predecessor=self.origin,successor=self.source)
        with self.assertRaises(MonitoringBlocked):self.f.active.snapshot()
        self.assertEqual(self.unchanged(),before);self.assertEqual(dump(self.f.new),ledger)

    def test_receipt_tampering_rejected_with_original_budget_unchanged(self):
        self.upgrade();baseline=self.unchanged()
        with sqlite3.connect(self.f.new) as c:
            saved=json.loads(c.execute('SELECT payload FROM paper_runtime_transition').fetchone()[0])
            c.execute('DROP TRIGGER paper_runtime_transition_update')
        for field in ('predecessor','successor','config_hash','events_hash'):
            altered=copy.deepcopy(saved);altered[field]='0'*64
            with sqlite3.connect(self.f.new) as c:
                c.execute('UPDATE paper_runtime_transition SET payload=?,payload_hash=?',(canonical(altered),digest(altered)))
                c.execute(runtime._guards()['paper_runtime_transition_update'])
            before=dump(self.f.new)
            with self.assertRaises(MonitoringBlocked):self.f.active.snapshot()
            self.assertEqual(dump(self.f.new),before);self.assertEqual(self.unchanged(),baseline)
            with sqlite3.connect(self.f.new) as c:c.execute('DROP TRIGGER paper_runtime_transition_update')

    def test_wrong_context_or_revoked_policy_rejected(self):
        self.upgrade();baseline=self.unchanged()
        wrong=copy.deepcopy(self.edge);wrong['context']['ledger_db']=str(self.root/'other-ledger.sqlite')
        for entries in ([self.f.edge],[self.f.edge,wrong]):
            self.f.f.policy.write_text(canonical({'version':1,'transitions':entries}))
            with self.assertRaises(MonitoringBlocked):self.f.active.snapshot()
            self.assertEqual(self.unchanged(),baseline)

    def test_origin_metadata_config_or_caller_source_not_waived(self):
        self.upgrade();baseline=self.unchanged()
        with sqlite3.connect(self.f.new) as c:
            original=dict(c.execute('SELECT key,value FROM metadata'))
        for key,value in (('implementation_hash',self.source),('config_hash','0'*64),('config','{}')):
            with sqlite3.connect(self.f.new) as c:c.execute('UPDATE metadata SET value=? WHERE key=?',(value,key))
            before=dump(self.f.new)
            with self.assertRaises(MonitoringBlocked):self.f.active.snapshot()
            self.assertEqual(dump(self.f.new),before);self.assertEqual(self.unchanged(),baseline)
            with sqlite3.connect(self.f.new) as c:c.execute('UPDATE metadata SET value=? WHERE key=?',(original[key],key))
        self.f.active.code_hash=self.origin
        with self.assertRaises(MonitoringBlocked):self.f.active.snapshot()
        self.assertEqual(self.unchanged(),baseline)

    def test_genuine_predecessor_binary_refuses_after_receipt_nonmutating(self):
        self.upgrade();before=self.unchanged()
        self.run_origin('''from desk import runtime_compatibility as runtime,monitoring_handoff as handoff
from desk.monitoring_budget import MonitoringBudget,MonitoringBlocked
from desk.evidence import EvidenceStore
runtime.POLICY=Path(a['runtime_policy']);handoff.POLICY=Path(a['policy'])
assert runtime.implementation_hash()==a['source']
store=EvidenceStore(a['evidence'],read_only=True);store.read_only=False
budget=MonitoringBudget(store,a['new'],a['cfg'],clock=lambda:a['at'])
try:budget.snapshot()
except MonitoringBlocked:pass
else:raise AssertionError('Predecessor binary accepted successor receipt')
''',{'source':self.origin,'runtime_policy':str(self.f.f.policy),'policy':str(self.f.policy),
     'evidence':str(self.f.evidence),'new':str(self.f.new),'cfg':self.f.cfg,'at':T+1})
        self.assertEqual(self.unchanged(),before)

    def test_native_identity_rewrite_cannot_bypass_original_binding(self):
        self.upgrade();baseline=self.unchanged()
        with sqlite3.connect(self.f.new) as c:
            for name in runtime._guards():c.execute('DROP TRIGGER '+name)
            c.execute('DROP TABLE paper_runtime_transition')
            c.execute("UPDATE metadata SET value=? WHERE key='implementation_hash'",(self.source,))
            # Native resolver alone accepts this identity. The immutable handoff
            # origin MUST still reject it rather than infer a source migration.
            self.assertEqual(runtime.require_runtime(c),self.source)
        before=dump(self.f.new)
        with self.assertRaises(MonitoringBlocked):self.f.active.snapshot()
        self.assertEqual(dump(self.f.new),before);self.assertEqual(self.unchanged(),baseline)
