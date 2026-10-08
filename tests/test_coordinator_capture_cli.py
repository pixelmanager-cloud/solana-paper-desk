"""Local Linux CLI subprocess fixtures; no real credentials or provider calls."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.model import canonical
from tests import test_raw_transaction_capture as fixtures

ROOT = Path(__file__).resolve().parents[1]
KEY = 'FAKE_CLI_SECRET_never_output'
# Test-side injection only: production CLI has no transport/failure flags.
RUNNER = r'''
import json,os,runpy,sqlite3,sys
from pathlib import Path
from unittest.mock import patch
from desk.coordinator_rpc import HeliusMainnetRPC
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.raw_transaction_capture import RawTransactionCapture
from tests.test_raw_transaction_capture import transaction
mode,db,log=sys.argv[1:4];sys.argv=['desk.coordinator_capture_cli',*sys.argv[4:]]
def fixture(self,method,params):
    # Production component must have committed its claim and charged the shared
    # admission before invocation. Query read-only, without initializing a DB.
    with sqlite3.connect(Path(db).resolve().as_uri()+'?mode=ro',uri=True) as c:
        used=c.execute('SELECT used FROM ownership_budgets WHERE id=?',('scan',)).fetchone()[0]
        assert used>=1 and used<=18
        assert c.execute('SELECT count(*) FROM raw_transaction_claims').fetchone()[0]==1
    with open(log,'a') as f:f.write(json.dumps([method,params,used])+'\n')
    assert method=='getTransaction'
    if mode=='rpc-fail':raise OSError(os.environ['HELIUS_API_KEY'])
    if mode=='null':return None
    raw=transaction()
    raw['fixture_untrusted_detail']=os.environ['HELIUS_API_KEY']
    return raw
original_finish=RawTransactionCapture._finish
def fail_finish(self,claim,body):
    with self.store.connect() as c:
        c.execute("CREATE TRIGGER fixture_fail BEFORE INSERT ON raw_transaction_results BEGIN SELECT RAISE(ABORT,'secret publication failure'); END")
    return original_finish(self,claim,body)
with patch.object(HeliusMainnetRPC,'__call__',fixture),patch('socket.create_connection',side_effect=AssertionError('No network')):
    if mode=='terminal-fail':
        with patch.object(RawTransactionCapture,'_finish',fail_finish):runpy.run_module('desk.coordinator_capture_cli',run_name='__main__')
    elif mode=='die-before-reserve':
        with patch.object(HistoryProgress,'reserve',lambda *a:os._exit(77)):runpy.run_module('desk.coordinator_capture_cli',run_name='__main__')
    else:runpy.run_module('desk.coordinator_capture_cli',run_name='__main__')
'''
GUARD = r'''
import runpy,sys
from unittest.mock import patch
mode=sys.argv[1];sys.argv=['desk.coordinator_capture_cli',*sys.argv[2:]]
with patch('sqlite3.connect',side_effect=AssertionError('DB accessed')),patch('socket.create_connection',side_effect=AssertionError('Network accessed')):
    if mode=='import':import desk.coordinator_capture_cli
    else:runpy.run_module('desk.coordinator_capture_cli',run_name='__main__')
'''


class CoordinatorCaptureHelpTests(unittest.TestCase):
    def test_import_help_and_argument_error_no_db_or_network(self):
        for mode,args,code in [('import',[],0),('run',['--help'],0),('run',[],2),
                               ('run',['--url',KEY],2),('run',['--source',KEY],2)]:
            with self.subTest(args=args):
                result=subprocess.run([sys.executable,'-c',GUARD,mode,*args],cwd=ROOT,
                                      capture_output=True,text=True,timeout=10)
                self.assertEqual(result.returncode,code,result.stderr)
                self.assertNotIn(KEY,result.stdout+result.stderr)
                self.assertEqual(result.stderr,'')
                if args==['--help']:self.assertIn('Local coordinator only',result.stdout)


@unittest.skipUnless(sys.platform=='linux' and Path('/proc/self/mountinfo').is_file(), 'Linux local coordinator path contract')
class CoordinatorCaptureCLITests(unittest.TestCase):
    def setUp(self):
        work=ROOT/'work';work.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=work);self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'evidence.sqlite';self.log=self.root/'calls.jsonl'
        self.store=EvidenceStore(self.path);self.progress=HistoryProgress(self.store)
        descriptor={'kind':'ownership_admission_v1','scan_id':'scan','mint':fixtures.MINT,'created':100}
        self.admission=self.progress.admit('scan',descriptor)
        self.path.chmod(0o600)

    def args(self,**changes):
        values={'evidence-db':str(self.path),'scan-id':'scan','descriptor-hash':self.admission['descriptor_hash'],
                'mint':fixtures.MINT,'signature':fixtures.SIGNATURE}
        values.update(changes)
        result=['--coordinator-diagnostic']
        for key,value in values.items():result.extend(['--'+key,value])
        return result

    def run_cli(self,mode='complete',args=None):
        env={**os.environ,'HELIUS_API_KEY':KEY}
        result=subprocess.run([sys.executable,'-c',RUNNER,mode,str(self.path),str(self.log),
                               *(self.args() if args is None else args)],cwd=ROOT,env=env,
                              capture_output=True,text=True,timeout=15)
        self.assertNotIn(KEY,result.stdout+result.stderr)
        self.assertNotIn('secret publication failure',result.stdout+result.stderr)
        self.assertEqual(result.stderr,'')
        if result.returncode==77:return result,None
        payload=json.loads(result.stdout)
        self.assertLess(len(result.stdout),1200)
        self.assertNotIn('reason',payload)
        self.assertNotIn(str(self.path),result.stdout)
        for key in ('eligible_for_trading','ownership_approved','lifecycle_verified','finality_authenticated',
                    'source_authenticated','signature_authenticated','caller_privileges_verified',
                    'cpi_success_verified','account_state_verified'):
            self.assertIs(payload[key],False)
        return result,payload

    def snapshot(self):
        with closing(self.store.connect()) as c:
            tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return (c.execute('SELECT type,name,sql FROM sqlite_master ORDER BY name').fetchall(),
                    [(table,c.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()) for table in tables])

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def file_snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_ino, p.stat().st_mode,
                         p.stat().st_mtime_ns) for p in self.root.iterdir() if p.is_file()}

    def test_closed_wal_missing_wrong_and_valid_binding_never_open_sqlite_or_mutate(self):
        from desk.coordinator_capture_cli import _parser, _run
        with closing(self.store.connect()) as c:
            self.assertEqual(c.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        self.assertFalse(Path(str(self.path)+'-wal').exists())
        before = self.file_snapshot()
        for changes in ({'scan-id':'missing'}, {'descriptor-hash':'0'*64},
                        {'mint':fixtures.OWNER}, {}):
            with self.subTest(changes=changes):
                # Real CLI subprocess reproduces the original sidecar-creation bug.
                self.assertEqual(self.run_cli(args=self.args(**changes))[1]['state'], 'BLOCKED')
                self.assertEqual(self.file_snapshot(), before)
                with patch('sqlite3.connect', side_effect=AssertionError('SQLite opened')) as connect:
                    with self.assertRaises(ValueError):
                        _run(_parser().parse_args(self.args(**changes)))
                    connect.assert_not_called()
        self.assertEqual(self.calls(), [])

    def test_active_wal_committed_evidence_not_ignored_or_mutated(self):
        with closing(self.store.connect()) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            # Committed current budget exists only in WAL, not a stale DB copy.
            writer.execute("UPDATE ownership_budgets SET used=18 WHERE id='scan'")
            self.assertGreater(Path(str(self.path)+'-wal').stat().st_size, 0)
            before = self.file_snapshot()
            for changes in ({'scan-id':'missing'}, {'descriptor-hash':'0'*64},
                            {'mint':fixtures.OWNER}, {}):
                self.assertEqual(self.run_cli(args=self.args(**changes))[1]['state'], 'BLOCKED')
                self.assertEqual(self.file_snapshot(), before)
            self.assertEqual(writer.execute("SELECT used FROM ownership_budgets WHERE id='scan'").fetchone()[0],18)
        self.assertEqual(self.calls(), [])

    def test_rollback_writer_in_progress_blocks_without_writes(self):
        with closing(self.store.connect()) as writer:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE ownership_budgets SET used=18 WHERE id='scan'")
            before = self.file_snapshot()
            for changes in ({'scan-id':'missing'}, {}):
                self.assertEqual(self.run_cli(args=self.args(**changes))[1]['state'], 'BLOCKED')
                self.assertEqual(self.file_snapshot(), before)
            writer.rollback()
        self.assertEqual(self.calls(), [])

    def test_preflight_guard_blocks_writer_and_mode_transition_across_sqlite_close(self):
        from desk.coordinator_capture_cli import _path, _preflight_guard
        path, identity = _path(str(self.path))
        with _preflight_guard(path, identity):
            # Closing a real same-process reader must not drop the OFD guard.
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as reader:
                self.assertEqual(reader.execute('SELECT count(*) FROM ownership_admissions').fetchone()[0],1)
            code = '''
import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=0)
for statement in ('BEGIN IMMEDIATE','PRAGMA journal_mode=WAL'):
    try:c.execute(statement)
    except sqlite3.OperationalError as e:
        assert 'locked' in str(e)
    else:raise AssertionError('Writer bypassed preflight lock')
c.close()
'''
            result=subprocess.run([sys.executable,'-c',code,str(path)],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
        # The lock is released rather than leaving the admission permanently frozen.
        self.assertEqual(self.run_cli()[1]['state'],'COMPLETE')

    def test_existing_rollback_journal_blocks_before_sqlite_open(self):
        from desk.coordinator_capture_cli import _parser, _run
        journal=Path(str(self.path)+'-journal');journal.write_bytes(b'fixture hot or stale journal')
        journal.chmod(0o600);before=self.file_snapshot()
        with patch('sqlite3.connect', side_effect=AssertionError('SQLite opened')) as connect:
            with self.assertRaises(ValueError):_run(_parser().parse_args(self.args()))
            connect.assert_not_called()
        self.assertEqual(self.run_cli()[1]['state'],'BLOCKED')
        self.assertEqual(self.file_snapshot(),before);self.assertEqual(self.calls(),[])

    def test_unsupported_lock_fails_closed_without_sqlite_fallback(self):
        from desk.coordinator_capture_cli import _parser, _run
        before=self.file_snapshot()
        with patch('fcntl.fcntl', side_effect=OSError('unsupported OFD lock')), \
             patch('sqlite3.connect', side_effect=AssertionError('SQLite opened')) as connect:
            with self.assertRaises(OSError):_run(_parser().parse_args(self.args()))
            connect.assert_not_called()
        self.assertEqual(self.file_snapshot(),before);self.assertEqual(self.calls(),[])

    def test_success_replay_and_original_admission_preserved(self):
        admission=self.progress.admission('scan')
        first,out=self.run_cli()
        self.assertEqual(first.returncode,0);self.assertEqual(out['state'],'COMPLETE')
        self.assertEqual((out['requests_used'],out['rpc_attempts'],out['request_ceiling']),(1,1,18))
        for key in ('request_hash','response_hash','claim_hash'):self.assertEqual(len(out[key]),64)
        before=self.snapshot()
        second,replay=self.run_cli(mode='rpc-fail')
        self.assertEqual(second.returncode,0);self.assertEqual(replay['state'],'COMPLETE')
        self.assertEqual(replay['rpc_attempts'],0);self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(self.calls()),1)
        after=self.progress.admission('scan')
        self.assertEqual(after['descriptor'],admission['descriptor'])
        self.assertEqual(after['descriptor_hash'],admission['descriptor_hash'])
        self.assertEqual(after['requests_used'],1)

    def test_missing_wrong_scan_descriptor_mint_and_invalid_signature_no_writes(self):
        before=self.snapshot()
        for changes in ({'scan-id':'missing'},{'descriptor-hash':'0'*64},{'mint':fixtures.OWNER},
                        {'signature':'0'*88},{'scan-id':KEY+'\ninvalid'},{'descriptor-hash':KEY}):
            with self.subTest(changes=changes):
                result,out=self.run_cli(args=self.args(**changes))
                self.assertNotEqual(result.returncode,0);self.assertEqual(out['state'],'BLOCKED')
                self.assertEqual(out['rpc_attempts'],0);self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.calls(),[])

    def test_missing_database_and_missing_foundation_never_created(self):
        missing=self.root/'missing.sqlite'
        result,out=self.run_cli(args=self.args(**{'evidence-db':str(missing)}))
        self.assertEqual(out['state'],'BLOCKED');self.assertFalse(missing.exists())
        empty=self.root/'empty.sqlite';EvidenceStore(empty);empty.chmod(0o600)
        before=empty.read_bytes()
        result,out=self.run_cli(args=self.args(**{'evidence-db':str(empty)}))
        self.assertEqual(out['state'],'BLOCKED');self.assertEqual(empty.read_bytes(),before)
        self.assertEqual(self.calls(),[])

    def test_alias_hardlink_unsafe_permissions_and_sidecar_symlink_block_before_io(self):
        before=self.snapshot()
        alias=self.root/'alias.sqlite';alias.symlink_to(self.path)
        self.assertEqual(self.run_cli(args=self.args(**{'evidence-db':str(alias)}))[1]['state'],'BLOCKED')
        alias.unlink()
        os.link(self.path,alias)
        self.assertEqual(self.run_cli()[1]['state'],'BLOCKED');alias.unlink()
        self.path.chmod(0o644)
        self.assertEqual(self.run_cli()[1]['state'],'BLOCKED');self.path.chmod(0o600)
        self.root.chmod(0o755)
        self.assertEqual(self.run_cli()[1]['state'],'BLOCKED');self.root.chmod(0o700)
        side=Path(str(self.path)+'.ownership.lock');side.symlink_to(self.path)
        self.assertEqual(self.run_cli()[1]['state'],'BLOCKED');side.unlink()
        self.assertEqual(self.snapshot(),before);self.assertEqual(self.calls(),[])

    def test_canonical_signature_rebinding_cannot_create_second_claim(self):
        self.run_cli();before=self.snapshot()
        result,out=self.run_cli(args=self.args(signature=fixtures.OTHER_SIGNATURE))
        self.assertEqual(out['state'],'BLOCKED');self.assertEqual(out['rpc_attempts'],0)
        self.assertEqual(self.snapshot(),before);self.assertEqual(len(self.calls()),1)

    def test_exhausted_budget_refusal_replay_no_reset_or_provider(self):
        for _ in range(18):self.assertTrue(self.progress.reserve('scan'))
        result,out=self.run_cli()
        self.assertEqual(out['state'],'REFUSED');self.assertEqual(out['requests_used'],18)
        self.assertEqual(out['rpc_attempts'],0);before=self.snapshot()
        self.assertEqual(self.run_cli()[1]['state'],'REFUSED');self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.calls(),[]);self.assertFalse(self.progress.reserve('scan'))

    def test_seventeenth_existing_attempt_leaves_only_one_diagnostic_charge(self):
        for _ in range(17):self.progress.reserve('scan')
        result,out=self.run_cli();self.assertEqual(out['state'],'COMPLETE')
        self.assertEqual(out['requests_used'],18);self.assertEqual(len(self.calls()),1)
        self.assertEqual(self.run_cli()[1]['rpc_attempts'],0)

    def test_provider_failure_sanitized_charged_once_and_replayed(self):
        result,out=self.run_cli(mode='rpc-fail')
        self.assertEqual(out['state'],'FAILED');self.assertEqual(out['requests_used'],1)
        self.assertNotIn(KEY,canonical(self.store.load(out['response_hash'])))
        before=self.snapshot()
        replay=self.run_cli()[1]
        self.assertEqual(replay['state'],'FAILED');self.assertEqual(replay['rpc_attempts'],0)
        self.assertEqual(self.snapshot(),before);self.assertEqual(len(self.calls()),1)

    def test_null_capture_rejected_without_fabrication_or_retry(self):
        out=self.run_cli(mode='null')[1]
        self.assertEqual(out['state'],'REJECTED');self.assertEqual(out['requests_used'],1)
        self.assertIsNone(self.store.load(out['response_hash'])['result'])
        self.assertEqual(self.run_cli()[1]['state'],'REJECTED');self.assertEqual(len(self.calls()),1)

    def test_failed_terminal_journal_stays_pending_and_charged_no_recapture(self):
        out=self.run_cli(mode='terminal-fail')[1]
        self.assertEqual(out['state'],'BLOCKED');self.assertEqual(out['requests_used'],1)
        before=self.snapshot()
        replay=self.run_cli()[1]
        self.assertEqual(replay['state'],'PENDING');self.assertEqual(replay['requests_used'],1)
        self.assertEqual(replay['rpc_attempts'],0);self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(self.calls()),1)

    def test_partial_journal_loss_not_repaired_or_recaptured(self):
        self.run_cli()
        with self.store.connect() as c:c.execute('DROP TRIGGER raw_transaction_claims_update')
        before=self.snapshot();out=self.run_cli()[1]
        self.assertEqual(out['state'],'BLOCKED');self.assertEqual(out['rpc_attempts'],0)
        self.assertEqual(self.snapshot(),before);self.assertEqual(len(self.calls()),1)

    def test_durable_claim_before_reservation_death_replays_pending(self):
        result,out=self.run_cli(mode='die-before-reserve');self.assertEqual(result.returncode,77)
        self.assertEqual(self.progress.admission('scan')['requests_used'],0)
        replay=self.run_cli()[1]
        self.assertEqual(replay['state'],'PENDING');self.assertEqual(replay['requests_used'],0)
        self.assertEqual(self.calls(),[])

    def test_invalid_arguments_no_database_creation_or_dynamic_error_text(self):
        missing=self.root/'not-created.sqlite'
        args=self.args(**{'evidence-db':str(missing)})+['--credential',KEY]
        result,out=self.run_cli(args=args)
        self.assertEqual(result.returncode,2);self.assertFalse(missing.exists())
        self.assertEqual(self.calls(),[])
