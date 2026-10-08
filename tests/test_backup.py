import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from desk.backup import run,DATABASES
from desk.storage import verify
class BackupTests(unittest.TestCase):
    def test_complete_backup_restore_and_safe_retention(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);data=p/'data';data.mkdir();root=p/'backups';root.mkdir()
            for name in DATABASES:
                with sqlite3.connect(data/name) as c:c.execute('create table marker(value)');c.execute('insert into marker values(7)')
            unrelated=root/'daily-20000101T000000Z';unrelated.mkdir();(unrelated/'notes').write_text('preserve')
            first=Path(run(data,root,keep=1)['backup']);old=root/'daily-20000102T000000Z';first.rename(old)
            report=run(data,root,keep=1);self.assertEqual(report['removed'],[old.name]);self.assertTrue(unrelated.exists())
            dest=Path(report['backup']);m=json.loads((dest/'manifest.json').read_text())
            for r in m['databases']:
                self.assertTrue(verify(dest/r['file'],r['sha256']))
                with sqlite3.connect(dest/r['file']) as c:self.assertEqual(c.execute('select value from marker').fetchone(),(7,))
    def test_partial_failure_publishes_nothing_and_preserves_prior(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);data=p/'data';data.mkdir();root=p/'backups';root.mkdir();prior=root/'keep';prior.write_text('existing')
            for name in DATABASES:
                with sqlite3.connect(data/name) as c:c.execute('create table marker(value)')
            (data/DATABASES[-1]).write_bytes(b'corrupt')
            with self.assertRaises(sqlite3.DatabaseError):run(data,root)
            self.assertEqual({x.name for x in root.iterdir()},{'.daily.lock','keep'})
    def test_missing_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):run(d,Path(d)/'backup')
    def test_low_space_does_not_start(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for name in DATABASES:(p/name).touch()
            with patch('desk.backup.shutil.disk_usage') as usage:
                usage.return_value.free=0
                with self.assertRaisesRegex(ValueError,'headroom'):run(p,p/'backups')
    def test_paper_positions_and_watchdog_restore_without_fills(self):
        from desk.ledger import Ledger
        from desk.engine import initial_state,transition
        from desk.monitor import tick
        from desk.backup import PAPER_DATABASE
        from tests.helpers import config,event,T
        import shutil
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);data=p/'data';data.mkdir();root=p/'backups'
            for name in DATABASES:
                with sqlite3.connect(data/name) as c:c.execute('create table marker(value)')
            ledger=Ledger(data/PAPER_DATABASE);ledger.apply(event(),config(),transition,initial_state);ledger.close()
            tick(data/PAPER_DATABASE,config(),now=T+11)
            ledger=Ledger(data/PAPER_DATABASE);before=ledger.report();ledger.close()
            result=run(data,root);self.assertEqual(result['databases'],5)
            dest=Path(result['backup']);manifest=json.loads((dest/'manifest.json').read_text());self.assertEqual(manifest['paper_ledger'],'included')
            entry=next(x for x in manifest['databases'] if x['file']==PAPER_DATABASE);self.assertTrue(verify(dest/PAPER_DATABASE,entry['sha256']))
            restored=p/'restored.sqlite';shutil.copyfile(dest/PAPER_DATABASE,restored)
            self.assertEqual(tick(restored,config(),now=T+12)['status'],'NO_CHANGE')
            ledger=Ledger(restored);after=ledger.report();ledger.close()
            self.assertEqual(before['replay_hash'],after['replay_hash']);self.assertEqual(after['state']['mode'],'EXIT_ONLY')
            self.assertFalse(any(x.get('side')=='sell' for x in after['outcomes']))
    def test_absent_paper_is_explicit_and_not_created(self):
        from desk.backup import PAPER_DATABASE
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for name in DATABASES:
                with sqlite3.connect(p/name) as c:c.execute('create table marker(value)')
            result=run(p,p/'backups');manifest=json.loads((Path(result['backup'])/'manifest.json').read_text())
            self.assertEqual(manifest['paper_ledger'],'not_configured');self.assertFalse((p/PAPER_DATABASE).exists())
    def test_broken_paper_symlink_is_not_silently_omitted(self):
        from desk.backup import PAPER_DATABASE
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/PAPER_DATABASE).symlink_to(p/'absent')
            with self.assertRaisesRegex(ValueError,'symlink'):run(p,p/'backups')
    def test_five_database_backups_participate_in_retention(self):
        from desk.backup import PAPER_DATABASE
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for name in (*DATABASES,PAPER_DATABASE):
                with sqlite3.connect(p/name) as c:c.execute('create table marker(value)')
            first=Path(run(p,p/'backups',keep=1)['backup']);old=first.parent/'daily-20000101T000000Z';first.rename(old)
            result=run(p,p/'backups',keep=1);self.assertEqual(result['removed'],[old.name])
    def test_paper_created_during_backup_aborts_publication(self):
        from desk.backup import PAPER_DATABASE,snapshot
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for name in DATABASES:
                with sqlite3.connect(p/name) as c:c.execute('create table marker(value)')
            def capture(source,destination):
                result=snapshot(source,destination)
                if not (p/PAPER_DATABASE).exists():
                    with sqlite3.connect(p/PAPER_DATABASE) as c:c.execute('create table marker(value)')
                return result
            with patch('desk.backup.snapshot',side_effect=capture):
                with self.assertRaisesRegex(ValueError,'membership changed'):run(p,p/'backups')
            self.assertEqual({x.name for x in (p/'backups').iterdir()},{'.daily.lock'})
    def test_decision_journal_is_included_and_verified(self):
        from desk.backup import DECISION_DATABASE
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for name in (*DATABASES,DECISION_DATABASE):
                with sqlite3.connect(p/name) as c:c.execute('create table marker(value)')
            result=run(p,p/'backups');dest=Path(result['backup']);manifest=json.loads((dest/'manifest.json').read_text())
            self.assertEqual(manifest['decision_journal'],'included');self.assertEqual(result['databases'],5)
            item=next(x for x in manifest['databases'] if x['file']==DECISION_DATABASE)
            self.assertTrue(verify(dest/DECISION_DATABASE,item['sha256']))
