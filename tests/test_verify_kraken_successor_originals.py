"""Synthetic pinned additive transition; no provider calls or production files."""
from contextlib import closing
from pathlib import Path
import copy
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from desk import monitoring_successor as successor, kraken_pacing_migration as migration
from desk.model import canonical,digest
from desk import paper_cycle
from tests import test_monitoring_successor as fixtures
from tests.helpers import T
from tools import verify_kraken_successor_originals as helper

class OriginalsTests(unittest.TestCase):
    def setUp(self):
        f=fixtures.MonitoringSuccessorTests(); f.setUp(); self.addCleanup(f.doCleanups)
        f.activate()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.old=root/'pre';self.live=root/'live'
        self.old.mkdir();self.live.mkdir()
        self.cfg=f.cfg|{'synthetic_successor_version':2}
        self.new=self.live/'kraken-new.sqlite';paper_cycle.initialize(self.new,self.cfg)
        self.pin=f.plan(f.next,self.new,self.cfg,T+3)
        self.pin['context']={k:str(self.live/n) for k,n in (('research_db','research.sqlite'),('evidence_db','evidence.sqlite'),('old_ledger_db','token2022-boost-00b7c302.sqlite'),('new_ledger_db',self.new.name),('pacing_db','provider-pacing.sqlite'))}
        policy=root/'successor.json';policy.write_text(canonical({'version':1,'successors':[f.pin,self.pin]}))
        p=patch.object(successor,'POLICY',policy);p.start();self.addCleanup(p.stop)
        mapped={'research.sqlite':f.f.research,'evidence.sqlite':f.f.evidence,'provider-pacing.sqlite':f.f.pacing,'token2022-boost-00b7c302.sqlite':f.next}
        for name in helper.DATABASES:
            old=self.old/name;old.parent.mkdir(exist_ok=True)
            if name in mapped:shutil.copyfile(mapped[name],old)
            else:
                with closing(sqlite3.connect(old)) as c:
                    c.execute('CREATE TABLE originals(id TEXT,payload BLOB,amount)');c.execute("INSERT INTO originals VALUES('id',x'0012',5)");c.commit()
            old.chmod(0o600)
            target=self.live/name;target.parent.mkdir(exist_ok=True);shutil.copyfile(old,target)
            target.chmod(0o600)
        self.source=helper.runtime.implementation_hash()
        self.pacing=migration.review_plan(self.live/'provider-pacing.sqlite')
        policy=root/'pacing.json';policy.write_text(canonical({'version':1,'pins':[self.pacing]}))
        p=patch.object(migration,'POLICY',policy);p.start();self.addCleanup(p.stop)
        migration.migrate(self.live/'provider-pacing.sqlite')
        self.sql('evidence.sqlite','INSERT INTO '+successor.TABLE+' VALUES(?,?,?)',(2,canonical(self.pin),digest(self.pin)))
    def sql(self,name,sql,args=()):
        with closing(sqlite3.connect(self.live/name)) as c:c.execute(sql,args);c.commit()
    def verify(self):return helper.verify(self.old,self.live,source=self.source,successor_pin=self.pin,pacing_pin=self.pacing,config=self.cfg)
    def test_exact_pinned_additions_and_no_writes(self):
        before={p:p.read_bytes() for r in (self.old,self.live) for p in r.rglob('*.sqlite')}
        self.assertEqual(self.verify()['databases_checked'],10)
        self.assertEqual(before,{p:p.read_bytes() for p in before})
    def test_changed_original_rowid_rejected(self):
        self.sql('raw.sqlite','UPDATE originals SET rowid=7')
        with self.assertRaisesRegex(ValueError,'Original rows changed'):self.verify()
    def test_changed_type_or_bytes_rejected(self):
        self.sql('raw.sqlite',"UPDATE originals SET amount='5'")
        with self.assertRaisesRegex(ValueError,'Original rows changed'):self.verify()
    def test_changed_original_policy_refused(self):
        self.sql('provider-pacing.sqlite',"UPDATE policy SET cadence=3 WHERE provider='helius'")
        with self.assertRaises(ValueError):self.verify()
    def test_monotonic_original_state_change_still_refused(self):
        self.sql('provider-pacing.sqlite',"UPDATE state SET high_water=high_water+1 WHERE provider='helius'")
        with self.assertRaisesRegex(ValueError,'Original rows changed'):self.verify()
    def test_wrong_kraken_initial_state_refused(self):
        self.sql('provider-pacing.sqlite',"UPDATE state SET next_at=1 WHERE provider='kraken'")
        with self.assertRaisesRegex(ValueError,'Exact Kraken'):self.verify()
    def test_original_successor_prefix_changed_refused(self):
        self.sql('evidence.sqlite','DROP TRIGGER '+successor.TABLE+'_update')
        self.sql('evidence.sqlite','UPDATE '+successor.TABLE+" SET body='{}' WHERE seq=1")
        self.sql('evidence.sqlite',successor.guards()[successor.TABLE+'_update'])
        with self.assertRaises(ValueError):self.verify()
    def test_extra_successor_refused(self):
        self.sql('evidence.sqlite','INSERT INTO '+successor.TABLE+" VALUES(3,'{}','fake')")
        with self.assertRaisesRegex(ValueError,'Successor scalar bounds'):self.verify()
    def test_extra_schema_refused(self):
        self.sql('launches.sqlite','CREATE TABLE extra(x)')
        with self.assertRaisesRegex(ValueError,'Unexpected schema'):self.verify()
    def test_new_ledger_changed_refused(self):
        with closing(sqlite3.connect(self.new)) as c:c.execute('CREATE TABLE extra(x)');c.commit()
        with self.assertRaises(ValueError):self.verify()
    def test_pin_context_substitution_refused(self):
        self.pin['context']['pacing_db']=str(self.old/'provider-pacing.sqlite')
        with self.assertRaises(ValueError):self.verify()

    def test_original_sequence_change_refused(self):
        for root in (self.old,self.live):
            with closing(sqlite3.connect(root/'raw.sqlite')) as c:
                c.execute('CREATE TABLE counted(id INTEGER PRIMARY KEY AUTOINCREMENT)')
                c.execute('INSERT INTO counted DEFAULT VALUES');c.commit()
        self.sql('raw.sqlite',"UPDATE sqlite_sequence SET seq=seq+1")
        with self.assertRaisesRegex(ValueError,'Original rows changed'):self.verify()
    def test_generated_rowid_shadow_cannot_hide_identity(self):
        for root in (self.old,self.live):
            with closing(sqlite3.connect(root/'raw.sqlite')) as c:
                c.execute('CREATE TABLE shadow(x TEXT,rowid TEXT GENERATED ALWAYS AS (x) VIRTUAL)')
                c.execute("INSERT INTO shadow(x) VALUES('original')");c.commit()
        self.sql('raw.sqlite','UPDATE shadow SET _rowid_=8')
        with self.assertRaisesRegex(ValueError,'Original rows changed'):self.verify()
    def test_without_rowid_bytes_preserved(self):
        for root in (self.old,self.live):
            with closing(sqlite3.connect(root/'raw.sqlite')) as c:
                c.execute('CREATE TABLE wr(k TEXT PRIMARY KEY,v BLOB) WITHOUT ROWID')
                c.execute("INSERT INTO wr VALUES('k',x'0011')");c.commit()
        self.verify()
        self.sql('raw.sqlite',"UPDATE wr SET v=x'0012'")
        with self.assertRaisesRegex(ValueError,'Original rows changed'):self.verify()

    def test_null_provider_policy_row_refused(self):
        self.sql('provider-pacing.sqlite','INSERT INTO policy VALUES(NULL,2,2.0,30.0)')
        with self.assertRaises(ValueError):self.verify()

    def test_null_provider_state_row_refused(self):
        self.sql('provider-pacing.sqlite','INSERT INTO state VALUES(NULL,0,0,0,NULL)')
        with self.assertRaises(ValueError):self.verify()
