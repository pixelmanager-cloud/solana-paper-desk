"""Projection boundaries over real persisted fixture replay, never live RPC."""
import base64
from contextlib import contextmanager
import copy
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk.control_obligations import inventory, read_platform_available, ReplayView, UNRESOLVED
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.live_features import candidate_snapshot
from desk.model import canonical, digest
from desk.ownership_worker import advance, saved_progress
from desk.security import TOKEN_PROGRAM, TOKEN_2022
from tests import test_ownership_multihistory_integration as multi


requires_linux_reads = unittest.skipUnless(
    read_platform_available(), 'Positive diagnostic replay requires approved Linux LP64 OFD read contract')


class ControlObligationsTests(unittest.TestCase):
    def setUp(self):
        self.h = multi.OwnershipMultiHistoryIntegrationTests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        h = self.h
        _, initial = collect_history(h.f['mint'], 90, 120, h.transport, max_pages=2,
                                     capture=h.store.save, token_accounts='none')
        key = h.store.save({'method':'getAccountInfo',
                           'params':[h.f['mint'], {'encoding':'base64','commitment':'confirmed'}],
                           'result':{'value':h.f['snapshot']['result']['value'][0]}})
        report = {'mint':h.f['mint'], 'observed_at':120, 'calls':3, 'findings':[],
                  'unknowns':[], 'mint_evidence_hash':key, 'history_queries':[initial]}
        report['report_hash'] = digest(report)
        self.db = h.root / 'research.sqlite'
        self.scan = {'id':'multi','mint':h.f['mint'],'created':120,'status':'COMPLETE',
                     'result':canonical(report)}
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)', tuple(self.scan.values()))
        h.calls.clear()
        for _ in range(4): advance(self.db, h.path, 'multi', h.transport, max_calls=3)
        self.head = saved_progress(EvidenceStore(h.path, read_only=True), self.scan)
        self.assertEqual(self.head['requests_used'], 13)
        self.assertEqual(len(h.calls), 10)

    def project(self, now=120):
        return candidate_snapshot(self.db, self.h.path, 'multi',
                                  revision_hash=self.head['evidence_hash'], now=now)

    def direct(self, scan=None):
        return inventory(scan or self.scan, EvidenceStore(self.h.path, read_only=True),
                         revision_hash=self.head['evidence_hash'], now=120)

    def publish(self, record):
        record.pop('evidence_hash', None)
        key = self.h.store.save(record)
        with self.h.store.connect() as c:
            c.execute('UPDATE ownership_heads SET evidence_hash=? WHERE scan_id=?', (key,'multi'))
        self.head = {**record, 'evidence_hash':key}

    def bank(self, raw, clock=None):
        key = self.h.store.save(raw)
        clock_key = self.h.store.save(clock or self.h.f['block_time'])
        with self.h.store.connect() as c:
            c.execute('UPDATE ownership_banks SET snapshot_hash=?,clock_hash=? WHERE budget=?',
                      (key,clock_key,'multi'))
        record = self.h.store.load(self.head['evidence_hash'])
        record['snapshot_evidence'] = {'snapshot_hash':key,'block_time_hash':clock_key}
        self.publish(record)

    def assert_reject(self, result):
        self.assertEqual(result['decision'], 'REJECT')
        for flag in ('historical_control_verified','noninterference_verified','lifecycle_verified',
                     'chain_authenticated','common_control_verified','ownership_approved','eligible_for_trading'):
            self.assertIs(result[flag], False)
        for name, reason in UNRESOLVED.items():
            self.assertEqual(result['obligations'][name]['status'], 'UNRESOLVED')
            self.assertIn(reason, result['blockers'])
        original = dict(result); original.pop('manifest_hash')
        self.assertEqual(result['manifest_hash'], digest(original))

    @requires_linux_reads
    def test_thirteen_request_components_exact_hashes_original_age_and_no_writes(self):
        before = {p.name:p.read_bytes() for p in self.h.root.iterdir() if p.is_file()}
        with patch('desk.providers.helius_rpc', side_effect=AssertionError('no provider')):
            first = self.project(); second = self.project()
        self.assertEqual(first, second)
        result = first['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'OBSERVED_COMPONENT')
        self.assertEqual(result['obligations']['historical_accounting']['status'], 'RECONCILED_COMPONENT')
        self.assertEqual((result['requests_used'], result['observed_at'], result['snapshot_slot']), (13,120,20))
        self.assertEqual(result['source_binding']['mode'], 'LEGACY_COMPLETED_SOURCE')
        self.assertEqual(len(result['obligations']['endpoint_controls']['accounts']), 3)
        for q in self.head['history_queries']:
            for page in q['pages']:
                self.assertIn(page['payload_hash'], result['evidence_hashes'])
                self.assertIn(page['request_evidence_hash'], result['evidence_hashes'])
        self.assertTrue(set(result['evidence_hashes']) <= set(first['evidence_hashes']))
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.h.root.iterdir() if p.is_file()})
        self.assertEqual(len(self.h.calls), 10)
        self.assertFalse(first['eligible_for_trading'])
        self.assertEqual(first['fields']['top10_pct']['status'], 'UNKNOWN')

    @requires_linux_reads
    def test_forged_summary_cannot_discharge_stronger_obligations(self):
        record = self.h.store.load(self.head['evidence_hash'])
        record.update(common_control_verified=True, eligible_for_trading=True,
                      chain_authenticated=True, historical_control_verified=True)
        record['snapshot'].update(reconciled=True, slot=999, observed_at=999)
        self.publish(record)
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['snapshot_slot'], 20)
        self.assertEqual(result['observed_at'], 120)

    def test_foreign_source_and_cross_mint_are_unbound_even_rehashed(self):
        for change in ({'id':'other'}, {'mint':self.h.f['accounts'][0]}, {'created':121}):
            with self.subTest(change=change):
                result = self.direct({**self.scan, **change})
                self.assert_reject(result)
                self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')
        report = json.loads(self.scan['result']); report['observed_at'] = 121
        report.pop('report_hash'); report['report_hash'] = digest(report)
        result = self.direct({**self.scan,'result':canonical(report)})
        self.assertIsNone(result['observed_at'])

    def test_missing_and_tampered_raw_pages_fail_closed(self):
        key = self.head['history_queries'][1]['pages'][0]['payload_hash']
        with self.h.store.connect() as c:
            c.execute('DELETE FROM pages WHERE hash=?', (key,))
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['historical_accounting']['status'], 'UNRESOLVED')
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')

    def test_missing_account_query_and_forged_hash_leave_accounting_unknown(self):
        record = self.h.store.load(self.head['evidence_hash'])
        record['history_queries'].pop()
        record['snapshot']['reconciled'] = True
        self.publish(record)
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['historical_accounting']['status'], 'UNRESOLVED')
        record['history_queries'][0]['pages'][0]['payload_hash'] = 'e'*64
        self.publish(record)
        self.assertEqual(self.project()['control_obligations']['obligations']['endpoint_controls']['status'], 'UNRESOLVED')

    def test_tampered_content_under_original_hash_not_accepted(self):
        key = self.head['history_queries'][1]['pages'][0]['payload_hash']
        other = self.h.store.save({'forged':True})
        with self.h.store.connect() as c:
            columns = [r[1] for r in c.execute('PRAGMA table_info(pages)') if r[1] != 'hash']
            for column in columns:
                c.execute(f'UPDATE pages SET {column}=(SELECT {column} FROM pages WHERE hash=?) WHERE hash=?', (other,key))
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')

    def test_zero_allowance_active_delegate_is_not_clean_endpoint(self):
        raw = copy.deepcopy(self.h.f['snapshot'])
        value = raw['result']['value'][1]
        data = bytearray(base64.b64decode(value['data'][0]))
        data[72:76] = (1).to_bytes(4,'little')
        data[76:108] = bytes([9])*32
        data[121:129] = bytes(8)
        value['data'][0] = base64.b64encode(data).decode()
        self.bank(raw)
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')
        self.assertTrue(result['obligations']['endpoint_controls']['unknown_reasons'])

    def test_budget_mismatch_cannot_be_replaced_by_summary_counter(self):
        with self.h.store.connect() as c:
            c.execute('UPDATE ownership_budgets SET used=3 WHERE id=?', ('multi',))
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertIsNone(result['requests_used'])
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')

    def test_missing_frontier_cannot_be_hidden_by_reconciled_summary(self):
        raw = copy.deepcopy(self.h.f['snapshot'])
        raw['params'][0].pop(); raw['result']['value'].pop()
        self.bank(raw)
        result = self.project()['control_obligations']
        self.assert_reject(result)

        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')

    @requires_linux_reads
    def test_transient_approve_revoke_blocks_accounting_despite_clean_endpoint(self):
        h = self.h; account = h.f['accounts'][0]; owner = h.f['owners'][account]
        h.records[-1]['transaction']['message']['instructions'].extend([
            {'programId':TOKEN_PROGRAM,'parsed':{'type':'approve','info':{
                'source':account,'owner':owner,'delegate':owner,'amount':'0'}}},
            {'programId':TOKEN_PROGRAM,'parsed':{'type':'revoke','info':{'source':account,'owner':owner}}}])
        record = h.store.load(self.head['evidence_hash'])
        queries = []
        for q in record['history_queries']:
            _, replacement = collect_history(q['address'],q['start'],q['end'],h.transport,
                max_pages=2,capture=h.store.save,token_accounts='none',slot_range=q['slot_range'])
            with h.store.connect() as c:
                c.execute('UPDATE ownership_history SET coverage=? WHERE budget=? AND query=?',
                          (canonical(replacement),'multi',canonical({k:q[k] for k in
                           ('address','start','end','token_accounts_filter','slot_range')})))
            queries.append(replacement)
        record['history_queries'] = queries; record['snapshot']['reconciled'] = True
        self.publish(record)
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'OBSERVED_COMPONENT')
        self.assertEqual(result['obligations']['historical_accounting']['status'], 'UNRESOLVED')

    @requires_linux_reads
    def test_later_t_not_birth_s_and_mismatched_history_cutoff_blocks_accounting(self):
        raw = copy.deepcopy(self.h.f['snapshot']); raw['result']['context']['slot'] = 21
        clock = copy.deepcopy(self.h.f['block_time']); clock['params'] = [21]
        self.bank(raw,clock)
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['snapshot_slot'], 21)
        self.assertEqual(result['obligations']['historical_accounting']['status'], 'UNRESOLVED')
        self.assertIn('HISTORY_SNAPSHOT_SLOT_BOUNDARY_UNVERIFIED', result['blockers'])

    def test_token2022_endpoint_never_promoted(self):
        raw = copy.deepcopy(self.h.f['snapshot'])
        raw['result']['value'][0]['owner'] = TOKEN_2022
        self.bank(raw)
        result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')

    @requires_linux_reads
    def test_stale_observation_keeps_historical_components_but_no_refresh(self):
        result = self.project(now=131)['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['observed_at'], 120)
        self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY', result['blockers'])
        self.assertEqual(result['obligations']['historical_accounting']['status'], 'RECONCILED_COMPONENT')

    def test_resource_ceiling_and_foreign_head_fail_closed(self):
        with patch('desk.control_obligations.MAX_REPLAY_BYTES', 0):
            result = self.project()['control_obligations']
        self.assert_reject(result)
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')
        with self.h.store.connect() as c:
            c.execute('UPDATE ownership_heads SET evidence_hash=?', ('f'*64,))
        self.assertEqual(self.project()['control_obligations']['obligations']['historical_accounting']['status'], 'UNRESOLVED')

    def test_wal_rejected_without_sidecar_creation_or_hidden_wal_snapshot(self):
        for path in (self.db,self.h.path):
            with self.subTest(path=path):
                c = sqlite3.connect(path)
                self.assertEqual(c.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
                c.close()
                before = {p.name:p.read_bytes() for p in self.h.root.iterdir() if p.is_file()}
                result = self.project()['control_obligations']
                self.assert_reject(result)
                self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')
                self.assertEqual(before,{p.name:p.read_bytes() for p in self.h.root.iterdir() if p.is_file()})
                with sqlite3.connect(path) as c: c.execute('PRAGMA journal_mode=DELETE')

    @requires_linux_reads
    def test_guard_keeps_revision_and_budget_stable_across_sqlite_connection_closes(self):
        from desk import control_obligations as module
        original = module.continuation_snapshot
        def replay(*args, **kwargs):
            result = original(*args, **kwargs)
            script = ('import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=0); '
                      'c.execute("BEGIN IMMEDIATE"); c.execute("UPDATE ownership_budgets SET used=0"); c.commit()')
            attempt = subprocess.run([sys.executable,'-c',script,str(self.h.path)],capture_output=True,text=True)
            self.assertNotEqual(attempt.returncode, 0)
            self.assertIn('database is locked', attempt.stderr)
            return result
        with patch.object(module,'continuation_snapshot',side_effect=replay):
            result = self.direct()
        self.assertEqual(result['requests_used'], 13)
        self.assertEqual(result['revision_hash'], self.head['evidence_hash'])
        self.assert_reject(result)


class SealedControlObligationsTests(unittest.TestCase):
    @requires_linux_reads
    def test_sealed_source_projects_only_when_exact_persisted_binding_replays(self):
        from tests import test_sealed_continuation_entry_evidence as sealed
        fixture = sealed.SealedContinuationEntryEvidenceTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        def project():
            return inventory(fixture.scan, EvidenceStore(fixture.evidence, read_only=True),
                             revision_hash=fixture.head['evidence_hash'], now=110)
        result = project()
        self.assertEqual(result['source_binding']['mode'], 'SEALED_ADMISSION')
        self.assertEqual(result['source_binding']['budget_source_hash'], fixture.binding)
        self.assertEqual(result['requests_used'], 11)
        self.assertEqual(result['obligations']['historical_accounting']['status'], 'RECONCILED_COMPONENT')
        with fixture.store.connect() as c:
            c.execute('UPDATE ownership_admissions SET completed_source_hash=?', ('f'*64,))
        result = project()
        self.assertEqual(result['obligations']['endpoint_controls']['status'], 'UNRESOLVED')
        self.assertFalse(result['ownership_approved'])


class PortableControlBoundaryTests(unittest.TestCase):
    def test_failed_lock_capability_never_falls_back_to_database_read(self):
        from desk import control_obligations as module
        @contextmanager
        def rejected_guard(*args):
            raise OSError('unsupported lock command')
            yield
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root)/'research.sqlite',Path(root)/'evidence.sqlite']
            for path in paths: path.write_bytes(b'unchanged unread database')
            before = {p.name:p.read_bytes() for p in Path(root).iterdir()}
            with patch.object(module,'read_platform_available',return_value=True), \
                 patch.object(Path,'read_text',return_value='0 0 0:1 / / rw - tmpfs none rw'), \
                 patch('desk.coordinator_capture_cli._preflight_guard',side_effect=rejected_guard), \
                 patch('sqlite3.connect',side_effect=AssertionError('No fallback database read')):
                result = candidate_snapshot(*paths,'scan',revision_hash='a'*64,now=1)
            self.assertEqual(result['read_status'], 'UNAVAILABLE')
            self.assertEqual(result['components'], {})
            self.assertEqual(result['evidence_hashes'], [])
            self.assertEqual(result['control_obligations']['read_status'], 'UNAVAILABLE')
            self.assertFalse(result['eligible_for_trading'])
            self.assertEqual(before,{p.name:p.read_bytes() for p in Path(root).iterdir()})

    def test_unsupported_platforms_do_not_open_resolve_or_modify_databases(self):
        from desk import control_obligations as module
        with tempfile.TemporaryDirectory() as root:
            paths = [Path(root)/'research.sqlite', Path(root)/'evidence.sqlite']
            for path in paths: path.write_bytes(b'unchanged unread database')
            before = {p.name:p.read_bytes() for p in Path(root).iterdir()}
            store = EvidenceStore(paths[1], read_only=True)
            scan = {'id':'scan','mint':'untrusted','created':1,'status':'COMPLETE','result':'{}'}
            for platform in ('darwin','win32','freebsd'):
                with self.subTest(platform=platform), \
                     patch.object(module.sys,'platform',platform), \
                     patch('sqlite3.connect',side_effect=AssertionError('No database opening')), \
                     patch.object(Path,'resolve',side_effect=AssertionError('No path resolution')), \
                     patch.object(Path,'read_text',side_effect=AssertionError('No mountinfo reading')), \
                     patch.object(module.os,'open',side_effect=AssertionError('No file opening')):
                    result = candidate_snapshot(*paths,'scan',revision_hash='a'*64,now=1)
                    direct = inventory(scan,store,revision_hash='a'*64,now=1)
                self.assertEqual(result['read_status'], 'UNAVAILABLE')
                self.assertIn('DIAGNOSTIC_READ_PLATFORM_UNAVAILABLE',result['reasons'])
                self.assertEqual(result['components'], {})
                self.assertEqual(result['evidence_hashes'], [])
                self.assertIsNone(result['observed_at'])
                self.assertTrue(all(field['status']=='UNKNOWN' for field in result['fields'].values()))
                for value in (result['control_obligations'],direct):
                    self.assertEqual(value['read_status'], 'UNAVAILABLE')
                    self.assertIn('DIAGNOSTIC_READ_PLATFORM_UNAVAILABLE',value['blockers'])
                    self.assertTrue(all(o['status']=='UNRESOLVED' for o in value['obligations'].values()))
                    self.assertFalse(value['eligible_for_trading'])
                    self.assertIsNone(value['requests_used'])
            self.assertEqual(before,{p.name:p.read_bytes() for p in Path(root).iterdir()})

    def test_unsupported_platform_missing_paths_do_not_create_files(self):
        from desk import control_obligations as module
        with tempfile.TemporaryDirectory() as root, patch.object(module.sys,'platform','darwin'), \
             patch('sqlite3.connect',side_effect=AssertionError('No database opening')):
            paths = [Path(root)/'missing-research.sqlite',Path(root)/'missing-evidence.sqlite']
            result = candidate_snapshot(*paths,'scan',revision_hash='a'*64,now=1)
            self.assertEqual(result['read_status'], 'UNAVAILABLE')
            self.assertIn('DIAGNOSTIC_READ_PLATFORM_UNAVAILABLE', result['reasons'])
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_hash_and_resource_checks_are_portable_and_cache_copies_are_isolated(self):
        class Store:
            read_only = True
            path = 'unused'
            def load(self,key): return {'raw':['original']}
        store = Store(); key = digest(store.load(None))
        view = ReplayView(store)
        view.load(key)['raw'].append('forged')
        self.assertEqual(view.load(key), {'raw':['original']})
        with self.assertRaises(ValueError): view.load('f'*64)
        with patch('desk.control_obligations.MAX_REPLAY_BYTES',0):
            with self.assertRaises(ValueError): ReplayView(store).load(key)
        with patch('desk.control_obligations.MAX_HASHES',0):
            with self.assertRaises(ValueError): ReplayView(store).load(key)

