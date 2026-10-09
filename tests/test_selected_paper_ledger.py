"""Synthetic dual-ledger operator selection; no providers or provisioning."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desk import backup, dashboard
from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.paper_view import paper_status
from desk.storage import verify
from tests.helpers import config, event, T


class SelectedPaperLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=Path(self.tmp.name)/'data';self.data.mkdir()
        self.root=Path(self.tmp.name)/'backups'
        self.original=self.data/backup.PAPER_DATABASE
        self.selected=self.data/'token2022-paper.sqlite'
        self.env=patch.dict(os.environ,{},clear=True);self.env.start();self.addCleanup(self.env.stop)
        for name in backup.DATABASES:
            with sqlite3.connect(self.data/name) as c:
                c.execute('CREATE TABLE marker(value)');c.execute('INSERT INTO marker VALUES(?)',(name,))
        self.seed(self.original,'SYNTHETIC_ORIGINAL','5')
        self.seed(self.selected,'SYNTHETIC_SELECTED','7')

    def seed(self,path,mint,cash):
        cfg=config();cfg['initial_equity_sol']=cash
        ledger=Ledger(path)
        try:ledger.apply(event(mint=mint),cfg,transition,initial_state)
        finally:ledger.close()
        path.chmod(0o640)

    def select(self,path=None):
        os.environ[backup.PAPER_LEDGER_ENV]=str(self.selected if path is None else path)

    def endpoint(self):
        return dashboard.handler(SimpleNamespace(db=self.data/'research.sqlite'),8080)

    def get(self,endpoint=None,host='127.0.0.1:8080'):
        request=object.__new__(endpoint or self.endpoint());request.path='/api/paper'
        request.headers={'Host':host};request.respond=Mock()
        with patch('desk.paper_view.time.time',return_value=T+1):request.do_GET()
        return request.respond.call_args.args

    def dump(self,path):
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:return '\n'.join(c.iterdump())

    def originals(self):
        return {p.name:self.dump(p) for p in [*(self.data/n for n in backup.DATABASES),self.original,self.selected]}

    def archive(self):
        result=backup.run(self.data,self.root,keep=1);dest=Path(result['backup'])
        return dest,json.loads((dest/'manifest.json').read_text())

    def age(self,dest,index=1):
        old=self.root/f'daily-2000010{index}T000000Z';dest.rename(old);return old

    def test_legacy_dashboard_default_unchanged_and_read_only(self):
        before=self.originals();status,body=self.get()
        self.assertEqual(status,200);self.assertEqual(body,paper_status(self.original,now=T+1))
        self.assertEqual(body['status'],'LEDGER_PRESENT');self.assertFalse(body['automatic_entry_enabled'])
        self.assertEqual(self.originals(),before)

    def test_selected_dashboard_uses_real_reader_and_preserves_both_ledgers(self):
        self.select();before=self.originals();status,body=self.get()
        self.assertEqual(status,200);self.assertEqual(body,paper_status(self.selected,now=T+1))
        self.assertEqual(body['status'],'LEDGER_PRESENT')
        self.assertEqual(body['positions'][0]['mint'],'SYNTHETIC_SELECTED')
        self.assertNotEqual(body['cash_sol'],paper_status(self.original,now=T+1)['cash_sol'])
        self.assertFalse(body['automatic_entry_enabled']);self.assertEqual(self.originals(),before)

    def test_selected_corrupt_checkpoint_has_no_legacy_fallback(self):
        self.select()
        with sqlite3.connect(self.selected) as c:c.execute('DELETE FROM state')
        before=self.originals();_,body=self.get()
        self.assertEqual(body['status'],'RECOVERY_REQUIRED');self.assertIsNone(body['cash_sol'])
        self.assertEqual(self.originals(),before)

    def test_selected_wrong_runtime_keeps_existing_reader_gate(self):
        self.select()
        with sqlite3.connect(self.selected) as c:c.execute("UPDATE metadata SET value=? WHERE key='implementation_hash'",('0'*64,))
        _,body=self.get();self.assertEqual(body['status'],'RECOVERY_REQUIRED');self.assertIsNone(body['cash_sol'])

    def test_selected_malformed_sqlite_has_no_legacy_fallback(self):
        self.select();self.selected.write_bytes(b'not SQLite')
        _,body=self.get();self.assertEqual(body['status'],'LEDGER_UNAVAILABLE');self.assertIsNone(body['cash_sol'])

    def test_missing_or_empty_explicit_selection_never_provisions_or_falls_back(self):
        for value in ('',str(self.data/'missing.sqlite')):
            with self.subTest(value=value):
                os.environ[backup.PAPER_LEDGER_ENV]=value
                _,body=self.get();self.assertEqual(body['reason'],'PAPER_LEDGER_SELECTION_INVALID')
                self.assertIsNone(body['cash_sol'])
                with self.assertRaises((OSError,ValueError)):backup.run(self.data,self.root)
        self.assertFalse((self.data/'missing.sqlite').exists());self.assertFalse(self.root.exists())

    def test_invalid_selection_refused_before_dashboard_startup_mutation(self):
        self.select(self.data/'missing.sqlite')
        with patch('desk.dashboard.Jobs') as jobs,patch('desk.evidence.EvidenceStore') as store:
            with self.assertRaises((OSError,ValueError)):dashboard.serve(self.data/'research.sqlite')
        jobs.assert_not_called();store.assert_not_called()

    def test_noncanonical_outside_reserved_and_multiple_paths_rejected(self):
        outside=Path(self.tmp.name)/'outside.sqlite';outside.write_bytes(self.selected.read_bytes());outside.chmod(0o600)
        values=[str(outside),'relative.sqlite',str(self.data/'sub'/'..'/self.selected.name),
                str(self.original),*(str(self.data/n) for n in (*backup.DATABASES,backup.DECISION_DATABASE)),
                str(self.selected)+','+str(self.original),str(self.data/'bad.sqlite-journal')]
        for value in values:
            with self.subTest(value=value):
                os.environ[backup.PAPER_LEDGER_ENV]=value
                with self.assertRaises((ValueError,OSError)):backup.selected_paper_ledger(self.data)
                _,body=self.get();self.assertEqual(body['reason'],'PAPER_LEDGER_SELECTION_INVALID')

    def test_symlink_parent_leaf_broken_link_hardlink_directory_fifo_rejected(self):
        link=self.data/'link.sqlite';link.symlink_to(self.selected)
        broken=self.data/'broken.sqlite';broken.symlink_to(self.data/'missing.sqlite')
        directory=self.data/'directory.sqlite';directory.mkdir()
        fifo=self.data/'fifo.sqlite';os.mkfifo(fifo,0o600)
        parent=Path(self.tmp.name)/'alias';parent.symlink_to(self.data,target_is_directory=True)
        hard=self.data/'hard.sqlite';os.link(self.selected,hard)
        for path in (link,broken,directory,fifo,parent/self.selected.name,hard,self.selected):
            with self.subTest(path=path):
                self.select(path)
                with self.assertRaises((ValueError,OSError)):backup.selected_paper_ledger(self.data)
        hard.unlink()

    def test_unprotected_file_or_directory_refused(self):
        self.select()
        for mode in (0o644,0o660,0o700,0o604):
            with self.subTest(mode=mode):
                self.selected.chmod(mode)
                with self.assertRaises(ValueError):backup.selected_paper_ledger(self.data)
        self.selected.chmod(0o640);self.data.chmod(0o777)
        try:
            with self.assertRaises(ValueError):backup.selected_paper_ledger(self.data)
        finally:self.data.chmod(0o755)

    def test_dashboard_replacement_or_env_change_after_binding_refused(self):
        self.select();endpoint=self.endpoint()
        self.selected.rename(self.data/'saved-original.sqlite');self.seed(self.selected,'SYNTHETIC_REPLACEMENT','9')
        _,body=self.get(endpoint);self.assertEqual(body['reason'],'PAPER_LEDGER_SELECTION_INVALID')
        endpoint=self.endpoint();del os.environ[backup.PAPER_LEDGER_ENV]
        _,body=self.get(endpoint);self.assertEqual(body['reason'],'PAPER_LEDGER_SELECTION_INVALID')

    def test_replacement_during_actual_projection_withholds_result(self):
        self.select();endpoint=self.endpoint();original=paper_status
        def read(path):
            result=original(path,now=T+1)
            self.selected.rename(self.data/'saved-original.sqlite');self.seed(self.selected,'SYNTHETIC_REPLACEMENT','9')
            return result
        with patch('desk.paper_view.paper_status',side_effect=read):_,body=self.get(endpoint)
        self.assertEqual(body['reason'],'PAPER_LEDGER_SELECTION_INVALID');self.assertIsNone(body['cash_sol'])

    def test_loopback_host_guard_preserved(self):
        self.select();status,_=self.get(host='external.example');self.assertEqual(status,403)

    def test_backup_includes_both_original_and_selected_and_integrity(self):
        self.select();before=self.originals();dest,manifest=self.archive()
        self.assertEqual(manifest['additional_paper_ledger'],{'file':self.selected.name,'source':str(self.selected)})
        self.assertEqual(len(manifest['databases']),6);self.assertEqual(manifest['paper_ledger'],'included')
        for report in manifest['databases']:
            self.assertTrue(verify(dest/report['file'],report['sha256']))
            self.assertEqual(self.dump(dest/report['file']),before[report['file']])
        self.assertEqual(self.originals(),before)
        self.assertEqual(paper_status(dest/self.selected.name,now=T+1)['status'],'LEDGER_PRESENT')

    def test_default_backup_omits_unselected_additional_files(self):
        dest,manifest=self.archive();self.assertNotIn('additional_paper_ledger',manifest)
        self.assertFalse((dest/self.selected.name).exists());self.assertTrue((dest/self.original.name).exists())

    def test_explicit_backup_requires_preserving_original_ledger(self):
        self.select();self.original.unlink()
        with self.assertRaisesRegex(ValueError,'Original paper ledger'):backup.run(self.data,self.root)

    def test_corrupt_selected_backup_aborts_and_preserves_prior(self):
        dest,_=self.archive();old=self.age(dest);self.select();self.selected.write_bytes(b'corrupt SQLite')
        with self.assertRaises(sqlite3.DatabaseError):backup.run(self.data,self.root,keep=1)
        self.assertTrue(old.exists());self.assertEqual({p.name for p in self.root.iterdir()},{old.name,'.daily.lock'})

    def test_replacement_during_backup_aborts_without_publication(self):
        self.select();capture=backup.snapshot
        def replace(source,destination):
            result=capture(source,destination)
            if source==self.selected:
                self.selected.rename(self.data/'saved-original.sqlite');self.seed(self.selected,'SYNTHETIC_REPLACEMENT','9')
            return result
        with patch('desk.backup.snapshot',side_effect=replace):
            with self.assertRaisesRegex(ValueError,'identity changed'):backup.run(self.data,self.root)
        self.assertEqual({p.name for p in self.root.iterdir()},{'.daily.lock'})

    def test_legacy_and_additional_archives_share_retention(self):
        dest,_=self.archive();old=self.age(dest);self.select()
        dest,_=self.archive();self.assertFalse(old.exists());old=self.age(dest)
        del os.environ[backup.PAPER_LEDGER_ENV]
        self.archive();self.assertFalse(old.exists())

    def test_changed_selected_filename_remains_retention_compatible(self):
        self.select();dest,_=self.archive();old=self.age(dest)
        other=self.data/'paper-second-experiment.sqlite';self.seed(other,'SYNTHETIC_SECOND','8');self.select(other)
        self.archive();self.assertFalse(old.exists())

    def test_corrupt_manifest_hash_or_extra_membership_not_pruned(self):
        self.select();dest,m=self.archive();old=self.age(dest)
        m['databases'][-1]['sha256']='0'*64;(old/'manifest.json').write_text(json.dumps(m))
        dest,m=self.archive();other=self.age(dest,2)
        m['additional_paper_ledger']['file']='research.sqlite';(other/'manifest.json').write_text(json.dumps(m))
        self.archive();self.assertTrue(old.exists());self.assertTrue(other.exists())

    def test_selected_backup_rechecks_protection_before_publication(self):
        self.select();capture=backup.snapshot
        def change(source,destination):
            result=capture(source,destination)
            if source==self.selected:self.selected.chmod(0o644)
            return result
        with patch('desk.backup.snapshot',side_effect=change):
            with self.assertRaisesRegex(ValueError,'protected'):backup.run(self.data,self.root)
        self.assertEqual({p.name for p in self.root.iterdir()},{'.daily.lock'})

    def test_oversize_or_nonobject_retention_manifests_preserved(self):
        dest,_=self.archive();old=self.age(dest);(old/'manifest.json').write_text('[]')
        dest,_=self.archive();other=self.age(dest,2);(other/'manifest.json').write_text(' '*65537)
        self.archive();self.assertTrue(old.exists());self.assertTrue(other.exists())

    def test_verified_old_wal_header_archive_retained_without_sidecar_mutation(self):
        dest,m=self.archive();old=self.age(dest)
        file=old/self.original.name
        with sqlite3.connect(file) as c:self.assertEqual(c.execute('PRAGMA journal_mode=WAL').fetchone(),('wal',))
        for item in m['databases']:
            if item['file']==file.name:item['bytes']=file.stat().st_size;item['sha256']=backup.sha256(file)
        (old/'manifest.json').write_text(json.dumps(m))
        before={p.name for p in old.iterdir()}
        self.assertTrue(backup._retained_manifest(old,self.data));self.assertEqual({p.name for p in old.iterdir()},before)
        self.select();self.archive();self.assertFalse(old.exists())

    def test_deeply_nested_malformed_manifest_preserved_while_retention_continues(self):
        self.select();dest,_=self.archive();old=self.age(dest)
        malformed=self.root/'daily-20000102T000000Z';malformed.mkdir()
        raw='['*12000+'0'+']'*12000
        self.assertEqual(len(raw.encode()),24001)
        (malformed/'manifest.json').write_text(raw)
        dest,m=self.archive()
        self.assertFalse(old.exists());self.assertTrue(malformed.exists())
        self.assertEqual((malformed/'manifest.json').read_text(),raw)
        self.assertEqual(len(m['databases']),6)
        for item in m['databases']:self.assertTrue(verify(dest/item['file'],item['sha256']))
        old=self.age(dest,3);self.archive()
        self.assertFalse(old.exists());self.assertTrue(malformed.exists())
        self.assertEqual((malformed/'manifest.json').read_text(),raw)


if __name__=='__main__':unittest.main()
