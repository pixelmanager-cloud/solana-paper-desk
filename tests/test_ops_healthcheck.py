"""24/7 operations: healthcheck, notifier and fresh-start unit templates. Fixtures only, no network."""
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from desk import provider_pacing
from desk.ledger import Ledger
from discovery import continuous
from tools.ops import healthcheck, notify

NOW = 1_800_000_000
TOKEN = '123456789:AAEhBOweik6ad9r_QXMENQjcrY4_fake_token_value'
REPO = Path(__file__).resolve().parents[1]
FRESH = REPO / 'deploy' / 'fresh'
MINT = 'So11111111111111111111111111111111111111112'


def stamp(ts):
    return time.strftime('%a %Y-%m-%d %H:%M:%S UTC', time.gmtime(ts))


class FakeSystemctl:
    def __init__(self):
        self.units = {}
        self.calls = []

    def set(self, unit, **props):
        self.units.setdefault(unit, {}).update(props)

    def __call__(self, argv):
        self.calls.append(list(argv))
        assert argv[:3] == ['systemctl', '--timestamp=utc', 'show'], argv
        unit = argv[3]
        if unit not in self.units:
            return SimpleNamespace(returncode=1, stdout='', stderr='not found')
        out = '\n'.join('%s=%s' % kv for kv in self.units[unit].items())
        return SimpleNamespace(returncode=0, stdout=out + '\n', stderr='')


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(os.path.realpath(self.tmp.name))
        self.root = self.base / 'exp'
        self.shared = self.base / 'shared'
        for d in (self.root, self.shared, self.base / 'backups', self.base / 'state'):
            d.mkdir(mode=0o700)
        os.chmod(self.base, 0o700)
        (self.root / 'entry-dispatch').mkdir(mode=0o700)
        self.discovery = self.shared / 'continuous.sqlite'
        continuous.initialize(self.discovery)
        self.pacing = self.shared / 'provider-pacing.sqlite'
        provider_pacing.initialize(self.pacing)
        self.systemd = FakeSystemctl()
        self.healthy_units()
        self.set_discovery(frame=NOW - 10, event=NOW - 60)
        self.write_ledger(mode='RUNNING', positions={})
        self.write_evidence(used=10)
        self.backup(NOW - 3600)

    # -- fixture builders (real initialisers where they exist) ---------------
    def healthy_units(self):
        for unit, spec in healthcheck.UNITS.items():
            if spec['kind'] == 'timer':
                self.systemd.set(unit, ActiveState='active', UnitFileState='enabled', LastTriggerUSec=stamp(NOW - 30),
                                 ActiveEnterTimestamp=stamp(NOW - 7200), NRestarts='0')
            elif spec['kind'] == 'service':
                self.systemd.set(unit, ActiveState='active', SubState='running', Result='success', NRestarts='0')
            else:
                self.systemd.set(unit, ActiveState='inactive', SubState='dead', Result='success', NRestarts='0')

    def set_discovery(self, frame, event):
        # Original discovery records are immutable (trigger-guarded), so rebuild the store each time.
        self.discovery.unlink()
        continuous.initialize(self.discovery)
        with closing(sqlite3.connect(self.discovery)) as c:
            if frame is not None:
                c.execute("INSERT INTO reservations VALUES(1,?,'FRAME')", (frame,))
                c.execute("INSERT INTO completions VALUES(1,?,10,0,'OK',NULL)", (frame,))
            if event is not None:
                c.execute("INSERT INTO raw_events VALUES(1,'s1',?,1,'{}','h')", (event,))
            c.commit()

    def write_ledger(self, mode, positions, realized='0', fills=(), events=True, checkpoint=True):
        path = self.root / 'paper-ledger.sqlite'
        path.unlink(missing_ok=True)
        ledger = Ledger(str(path))
        db = ledger.db
        if events:
            db.execute("INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('e1',?, '{}', 'h')", (NOW - 100,))
            for i, fill in enumerate(fills):
                db.execute("INSERT INTO outcomes(event_id,payload) VALUES('e1',?)", (json.dumps(fill),))
        if checkpoint:
            state = {'cash': '5', 'realized_pnl': realized, 'positions': positions, 'mode': mode, 'last_ts': NOW - 100}
            db.execute("INSERT INTO state VALUES(1,?)", (json.dumps(state),))
        ledger.close()

    def position(self, **extra):
        return dict({'qty': '1', 'mark_at': NOW - 60, 'opened_at': NOW - 600, 'exit_blocked': None,
                     'mark_status': 'MODEL_ESTIMATE'}, **extra)

    def write_evidence(self, used=0, cap=60, blocked=None, null_passes=(), reserve_outcomes=True, window=3600):
        path = self.root / 'evidence.sqlite'
        path.unlink(missing_ok=True)
        with closing(sqlite3.connect(path)) as c:
            c.execute('CREATE TABLE paper_monitoring_budget(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,ledger TEXT NOT NULL,config_hash TEXT NOT NULL,code_hash TEXT NOT NULL,cap INTEGER NOT NULL,window_seconds INTEGER NOT NULL,high_water REAL NOT NULL,total INTEGER NOT NULL,blocked TEXT)')
            c.execute('CREATE TABLE paper_monitoring_reservations(id INTEGER PRIMARY KEY,at REAL NOT NULL,scan_id TEXT NOT NULL,mint TEXT NOT NULL,checkpoint_hash TEXT NOT NULL,method TEXT NOT NULL,params_hash TEXT NOT NULL)')
            c.execute('CREATE TABLE paper_monitoring_outcomes(reservation_id INTEGER PRIMARY KEY REFERENCES paper_monitoring_reservations(id),evidence_hash TEXT NOT NULL)')
            c.execute('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute("INSERT INTO paper_monitoring_budget VALUES(1,1,'l','c','k',?,?,0,?,?)", (cap, window, used, blocked))
            for i in range(used):
                c.execute("INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)", (i + 1, NOW - 100 - i, 's', 'm', 'h', 'x', 'p'))
                if reserve_outcomes:
                    c.execute("INSERT INTO paper_monitoring_outcomes VALUES(?,?)", (i + 1, 'e'))
            c.execute("INSERT INTO paper_observation_passes VALUES('ok','i','o')")
            for name in null_passes:
                c.execute("INSERT INTO paper_observation_passes VALUES(?,?,NULL)", (name, 'i'))
            c.commit()

    def backup(self, ts, style='created_utc'):
        d = self.base / 'backups' / ('daily-%d' % ts)
        d.mkdir(exist_ok=True)
        iso = time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime(ts))
        (d / 'manifest.json').write_text(json.dumps({style: iso}))

    def report(self, *extra, units=True, usage=None):
        argv = ['--root', str(self.root), '--discovery-db', str(self.discovery), '--pacing-db', str(self.pacing),
                '--backup-root', str(self.base / 'backups'), *extra]
        if not units:
            argv.append('--no-systemd')
        out = io.StringIO()
        usage = usage or (lambda p: SimpleNamespace(total=100 << 30, used=10 << 30, free=90 << 30))
        with redirect_stdout(out):
            code = healthcheck.main(argv, runner=self.systemd, clock=lambda: NOW, usage=usage)
        lines = out.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1, 'exactly one JSON line')
        return code, json.loads(lines[0])

    def find(self, report, name):
        return [c for c in report['checks'] if c['check'] == name]

    def severities(self, report):
        return {c['check']: c['severity'] for c in report['checks']}


class HealthcheckTests(Fixture):
    def test_healthy_desk_is_ok_and_exits_zero(self):
        code, report = self.report()
        self.assertEqual((code, report['status']), (0, 'OK'), [c for c in report['checks'] if c['severity'] != 'OK'])
        self.assertTrue(report['read_only'])

    def test_stopped_discovery_is_critical_and_exit_two(self):
        self.systemd.set('desk-continuous-discovery.service', ActiveState='failed', Result='exit-code')
        code, report = self.report()
        self.assertEqual((code, report['status']), (2, 'CRITICAL'))
        self.assertEqual(self.severities(report)['unit:desk-continuous-discovery.service'], 'CRITICAL')

    def test_restart_counts_are_graded(self):
        self.systemd.set('desk-dashboard.service', NRestarts='4')
        self.assertEqual(self.severities(self.report()[1])['restarts:desk-dashboard.service'], 'WARN')
        self.systemd.set('desk-dashboard.service', NRestarts='12')
        self.assertEqual(self.severities(self.report()[1])['restarts:desk-dashboard.service'], 'CRITICAL')

    def test_clean_exit_zero_restarts_do_not_alert(self):
        # discovery restarts cleanly (exit 0) once a day; NRestarts accumulates but must stay OK
        for restarts in ('1', '4', '12', '40'):
            self.systemd.set('desk-continuous-discovery.service', NRestarts=restarts, Result='success',
                             ExecMainStatus='0', SubState='running')
            code, rep = self.report()
            self.assertEqual((code, rep['status']), (0, 'OK'), restarts)
            self.assertTrue(all(c['severity'] == 'OK' for c in self.find(rep, 'restarts:desk-continuous-discovery.service')))

    def test_non_zero_exit_restarts_still_alert(self):
        unit = 'desk-continuous-discovery.service'
        for kwargs in ({'Result': 'exit-code', 'ExecMainStatus': '1'}, {'Result': 'success', 'ExecMainStatus': '137'},
                       {'Result': 'signal', 'ExecMainStatus': '0'}, {'Result': 'success', 'SubState': 'auto-restart', 'ExecMainStatus': '1'},
                       {'Result': 'success', 'ExecMainStatus': 'garbage'}):
            self.systemd.set(unit, NRestarts='4', SubState='running')
            self.systemd.set(unit, **kwargs)
            self.assertEqual(self.severities(self.report()[1])['restarts:' + unit], 'WARN', kwargs)
            self.systemd.set(unit, NRestarts='12')
            self.assertEqual(self.severities(self.report()[1])['restarts:' + unit], 'CRITICAL', kwargs)

    def test_unit_missing_from_systemd_is_critical(self):
        del self.systemd.units['desk-dashboard.service']
        self.assertEqual(self.severities(self.report()[1])['unit:desk-dashboard.service'], 'CRITICAL')

    def test_manual_timers_may_be_off_but_enabled_and_inactive_is_critical(self):
        for unit in ('desk-paper-entry-dispatcher.timer', 'desk-paper-monitor.timer'):
            self.systemd.set(unit, ActiveState='inactive', UnitFileState='disabled')
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertIn('manual', self.find(report, 'unit:desk-paper-entry-dispatcher.timer')[0]['detail'])
        self.systemd.set('desk-paper-entry-dispatcher.timer', UnitFileState='enabled')
        self.assertEqual(self.severities(self.report()[1])['unit:desk-paper-entry-dispatcher.timer'], 'CRITICAL')

    def test_required_timer_inactive_is_critical(self):
        self.systemd.set('desk-paper-held-cycle.timer', ActiveState='inactive', UnitFileState='disabled')
        self.assertEqual(self.severities(self.report()[1])['unit:desk-paper-held-cycle.timer'], 'CRITICAL')

    def test_tick_recency_thresholds(self):
        timer = 'desk-paper-entry-dispatcher.timer'
        for age, expected in ((200, 'OK'), (400, 'WARN'), (1000, 'CRITICAL')):
            self.systemd.set(timer, LastTriggerUSec=stamp(NOW - age))
            self.assertEqual(self.severities(self.report()[1])['entry_tick'], expected, age)

    def test_tick_never_triggered_uses_activation_and_unparseable_is_visible(self):
        timer = 'desk-paper-held-cycle.timer'
        self.systemd.set(timer, LastTriggerUSec='n/a', ActiveEnterTimestamp=stamp(NOW - 4000))
        rep = self.report()[1]
        self.assertEqual(self.severities(rep)['held_tick'], 'CRITICAL')
        self.systemd.set(timer, LastTriggerUSec='yesterday-ish')
        self.assertEqual(self.severities(self.report()[1])['held_tick'], 'WARN')

    def test_discovery_frames_and_events_staleness(self):
        self.set_discovery(frame=NOW - 200, event=NOW - 60)
        self.assertEqual(self.severities(self.report()[1])['discovery_frames'], 'WARN')
        self.set_discovery(frame=NOW - 400, event=NOW - 60)
        self.assertEqual(self.severities(self.report()[1])['discovery_frames'], 'CRITICAL')
        self.set_discovery(frame=NOW - 10, event=NOW - 8000)
        self.assertEqual(self.severities(self.report()[1])['discovery_events'], 'CRITICAL')
        self.set_discovery(frame=None, event=None)
        self.assertEqual(self.severities(self.report()[1])['discovery_frames'], 'CRITICAL')

    def test_ledger_exit_only_with_unresolved_exit_is_critical(self):
        pos = self.position(exit_blocked='EXACT_FRESH_SELL_QUOTE_REQUIRED')
        self.write_ledger('EXIT_ONLY', {MINT: pos})
        code, rep = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(self.find(rep, 'exits')[0]['positions'], [MINT])

    def test_exit_only_without_blocked_position_is_warn_and_pause_is_warn(self):
        self.write_ledger('EXIT_ONLY', {})
        self.assertEqual(self.severities(self.report()[1])['exits'], 'WARN')
        self.write_ledger('ENTRY_PAUSED', {})
        self.assertEqual(self.severities(self.report()[1])['mode'], 'WARN')

    def test_open_position_mark_age_and_max_hold(self):
        self.write_ledger('RUNNING', {MINT: self.position(mark_at=NOW - 100)})
        self.assertEqual(self.find(self.report()[1], 'mark_age')[0]['severity'], 'OK')
        self.write_ledger('RUNNING', {MINT: self.position(mark_at=NOW - 500)})
        self.assertEqual(self.find(self.report()[1], 'mark_age')[0]['severity'], 'WARN')
        self.write_ledger('RUNNING', {MINT: self.position(mark_at=NOW - 2000, opened_at=NOW - 30000)})
        rep = self.report()[1]
        self.assertEqual(self.find(rep, 'mark_age')[0]['severity'], 'CRITICAL')
        self.assertEqual(self.find(rep, 'max_hold')[0]['severity'], 'CRITICAL')

    def test_missing_checkpoint_with_events_is_critical_but_empty_ledger_ok(self):
        self.write_ledger('RUNNING', {}, checkpoint=False)
        self.assertEqual(self.severities(self.report()[1])['ledger'], 'CRITICAL')
        self.write_ledger('RUNNING', {}, checkpoint=False, events=False)
        self.assertEqual(self.severities(self.report()[1])['ledger'], 'OK')

    def test_null_observation_pass_latch_is_critical_with_ids(self):
        self.write_evidence(null_passes=['pass-a', 'pass-b'])
        code, rep = self.report()
        self.assertEqual(code, 2)
        self.assertEqual(self.find(rep, 'observation_passes')[0]['null_pass_ids'], ['pass-a', 'pass-b'])

    def test_monitoring_headroom_uses_effective_cap_and_latch(self):
        self.write_evidence(used=2900)
        self.assertEqual(self.severities(self.report()[1])['monitoring_budget'], 'WARN')
        self.write_evidence(used=3500)
        self.assertEqual(self.severities(self.report()[1])['monitoring_budget'], 'CRITICAL')
        self.write_evidence(used=5, blocked='SOURCE_FAILURE')
        self.assertEqual(self.severities(self.report()[1])['monitoring_latch'], 'CRITICAL')
        self.write_evidence(used=5, reserve_outcomes=False)
        self.assertEqual(self.severities(self.report()[1])['monitoring_pending'], 'WARN')

    def test_pacing_blocked_and_orphaned_slot(self):
        with closing(sqlite3.connect(self.pacing)) as c:
            c.execute("UPDATE state SET blocked_until=?", (NOW + 90,))
            c.commit()
        rep = self.report()[1]
        pacing = [c for c in rep['checks'] if c['check'].startswith('pacing:')]
        self.assertTrue(pacing)
        self.assertEqual({c['severity'] for c in pacing}, {'CRITICAL'})
        self.assertIn('blocked for another 90s', pacing[0]['detail'])
        with closing(sqlite3.connect(self.pacing)) as c:
            c.execute("UPDATE state SET blocked_until=0, next_at=?, pending='ticket'", (NOW - 1000,))
            c.commit()
        details = ' '.join(c['detail'] for c in self.report()[1]['checks'] if c['check'].startswith('pacing'))
        self.assertIn('orphaned', details)

    def test_disk_thresholds(self):
        full = lambda free: (lambda p: SimpleNamespace(total=100 << 30, used=0, free=free))
        self.assertEqual(self.severities(self.report(usage=full(15 << 30))[1])['disk'], 'WARN')
        self.assertEqual(self.severities(self.report(usage=full(5 << 30))[1])['disk'], 'CRITICAL')
        self.assertEqual(self.severities(self.report(usage=full(50 << 30))[1])['disk'], 'OK')

    def test_backup_age_formats_and_absence(self):
        self.assertEqual(self.severities(self.report()[1])['backup'], 'OK')
        for p in (self.base / 'backups').iterdir():
            (p / 'manifest.json').unlink(); p.rmdir()
        self.assertEqual(self.severities(self.report()[1])['backup'], 'CRITICAL')
        self.backup(NOW - 40 * 3600, style='created_at')
        self.assertEqual(self.severities(self.report()[1])['backup'], 'WARN')
        self.backup(NOW - 60 * 3600)
        self.assertEqual(self.severities(self.report()[1])['backup'], 'WARN')  # newest still the 40h one
        for p in (self.base / 'backups').iterdir():
            (p / 'manifest.json').write_text(json.dumps({'created_utc': '2026-10-01T00:00:00Z'}))
        self.assertEqual(self.severities(self.report()[1])['backup'], 'CRITICAL')

    def test_failing_held_pass_is_critical_only_with_an_open_position(self):
        self.systemd.set('desk-paper-held-cycle.service', ActiveState='failed', Result='exit-code')
        self.assertEqual(self.severities(self.report()[1])['unit:desk-paper-held-cycle.service'], 'WARN')
        self.write_ledger('RUNNING', {MINT: self.position()})
        self.assertEqual(self.severities(self.report()[1])['unit:desk-paper-held-cycle.service'], 'CRITICAL')

    def test_failing_entry_pass_is_critical(self):
        self.systemd.set('desk-paper-entry-dispatcher.service', ActiveState='failed', Result='timeout')
        self.assertEqual(self.severities(self.report()[1])['unit:desk-paper-entry-dispatcher.service'], 'CRITICAL')

    def test_broken_probe_is_a_finding_not_a_crash(self):
        (self.root / 'evidence.sqlite').unlink()
        code, rep = self.report()
        self.assertEqual(code, 2)
        self.assertIn('probe failed', self.find(rep, 'evidence')[0]['detail'])

    def test_sqlite_error_escaping_a_probe_yields_critical_report_not_a_crash(self):
        args = SimpleNamespace(root=str(self.root), discovery_db=str(self.discovery), pacing_db=str(self.pacing),
                               backup_root=None, config=None, out=None, no_systemd=True, threshold=[])
        boom = sqlite3.DatabaseError('database disk image is malformed')
        with mock.patch.object(healthcheck, 'escalate_held_failure', side_effect=boom):
            rep = healthcheck.build_report(args, self.systemd, lambda: NOW)
            self.assertEqual(rep['status'], 'CRITICAL')
            self.assertEqual(rep['ts'], NOW)
            self.assertEqual(rep['checks'][0]['severity'], 'CRITICAL')
            out = io.StringIO()
            with redirect_stdout(out):
                code = healthcheck.main(['--root', str(self.root), '--discovery-db', str(self.discovery),
                                         '--pacing-db', str(self.pacing), '--no-systemd'], clock=lambda: NOW)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'CRITICAL')

    def test_symlinked_database_is_refused(self):
        target = self.base / 'real.sqlite'
        os.replace(self.pacing, target)
        self.pacing.symlink_to(target)
        code, rep = self.report()
        self.assertEqual(code, 2)
        self.assertIn('probe failed', self.find(rep, 'pacing')[0]['detail'])

    def test_unknown_threshold_name_is_rejected_loudly(self):
        code, rep = self.report('--threshold', 'bogus=1')
        self.assertEqual(code, 2)
        self.assertEqual(self.find(rep, 'healthcheck')[0]['severity'], 'CRITICAL')

    def test_threshold_override(self):
        self.systemd.set('desk-paper-entry-dispatcher.timer', LastTriggerUSec=stamp(NOW - 200))
        rep = self.report('--threshold', 'entry_tick_warn=100')[1]
        self.assertEqual(self.severities(rep)['entry_tick'], 'WARN')

    def test_never_writes_any_database_or_changes_mtime(self):
        paths = [p for p in list(self.root.rglob('*.sqlite')) + [self.discovery, self.pacing]]
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}
        before_names = sorted(str(p) for p in self.base.rglob('*'))
        self.report()
        self.report('--out', str(self.base / 'state' / 'health.json'))
        after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in paths}
        self.assertEqual(before, after)
        new = set(str(p) for p in self.base.rglob('*')) - set(before_names)
        # SQLite may create -wal/-shm sidecars when opening a WAL database read-only; nothing else appears.
        self.assertEqual({n for n in new if not n.endswith(('-wal', '-shm'))}, {str(self.base / 'state' / 'health.json')})

    def test_database_connections_reject_writes(self):
        for path in (self.discovery, self.pacing, self.root / 'evidence.sqlite', self.root / 'paper-ledger.sqlite'):
            with closing(healthcheck.ro(path)) as c:
                with self.assertRaises(sqlite3.OperationalError):
                    c.execute('CREATE TABLE intrusion(a)')

    def test_out_file_is_private_and_matches_stdout(self):
        out = self.base / 'state' / 'health.json'
        code, rep = self.report('--out', str(out))
        self.assertEqual(oct(out.stat().st_mode & 0o777), '0o600')
        self.assertEqual(json.loads(out.read_text()), rep)

    def test_only_systemctl_show_is_ever_invoked(self):
        self.report()
        self.assertTrue(self.systemd.calls)
        self.assertTrue(all(c[:3] == ['systemctl', '--timestamp=utc', 'show'] for c in self.systemd.calls))

    def test_budget_ddl_matches_repository_schema(self):
        source = (REPO / 'desk' / 'monitoring_budget.py').read_text()
        for column in ('cap INTEGER NOT NULL', 'window_seconds INTEGER NOT NULL', 'blocked TEXT'):
            self.assertIn(column, source)
        self.assertIn('reservation_id INTEGER PRIMARY KEY REFERENCES paper_monitoring_reservations(id)', source)
        self.assertIn('paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)',
                      (REPO / 'desk' / 'paper_cycle.py').read_text())


class NotifyTests(Fixture):
    def setUp(self):
        super().setUp()
        self.state = notify.load_state(self.base / 'state' / 'n.json')
        self.stream = io.StringIO()
        self.journal = notify.JournalNotifier(self.stream)

    def finding(self, check='unit:desk-dashboard.service', severity='CRITICAL', detail='down', **extra):
        return dict({'check': check, 'severity': severity, 'detail': detail}, **extra)

    def run_alert(self, checks, now, notifiers=None, **kw):
        return notify.process_report({'checks': checks}, self.state, now, notifiers or [self.journal], **kw)

    def test_new_alert_sent_once_then_deduplicated(self):
        self.assertTrue(self.run_alert([self.finding()], NOW)['sent'])
        self.assertEqual(self.run_alert([self.finding()], NOW + 300), {'sent': False, 'reason': 'nothing new'})
        self.assertEqual(len(self.stream.getvalue().splitlines()), 1)

    def test_reminder_after_interval_and_escalation_immediate(self):
        self.run_alert([self.finding(severity='WARN')], NOW)
        self.assertFalse(self.run_alert([self.finding(severity='WARN')], NOW + 3600)['sent'])
        self.assertTrue(self.run_alert([self.finding(severity='CRITICAL')], NOW + 3700)['sent'])
        self.assertIn('ESCALATED', self.stream.getvalue())
        self.assertFalse(self.run_alert([self.finding()], NOW + 3700 + 1800)['sent'])
        self.assertTrue(self.run_alert([self.finding()], NOW + 3700 + 3601)['sent'])
        self.assertIn('STILL', self.stream.getvalue())

    def test_resolution_is_announced_once_and_state_cleared(self):
        self.run_alert([self.finding()], NOW)
        self.assertTrue(self.run_alert([], NOW + 300)['sent'])
        self.assertIn('RESOLVED unit:desk-dashboard.service', self.stream.getvalue())
        self.assertEqual(self.state['alerts'], {})
        self.assertFalse(self.run_alert([], NOW + 600)['sent'])

    def test_ok_findings_never_alert(self):
        self.assertFalse(self.run_alert([self.finding(severity='OK')], NOW)['sent'])

    def test_per_position_findings_have_distinct_keys(self):
        a = self.finding(check='mark_age', severity='WARN', mint='MINTAAAA')
        b = self.finding(check='mark_age', severity='WARN', mint='MINTBBBB')
        self.run_alert([a], NOW)
        self.assertTrue(self.run_alert([a, b], NOW + 60)['sent'])

    def test_rate_limit_caps_messages_per_hour_and_retries_later(self):
        for i in range(3):
            self.assertTrue(self.run_alert([self.finding(check='c%d' % i)], NOW + i, max_per_hour=3)['sent'])
        result = self.run_alert([self.finding(check='c-new')], NOW + 10, max_per_hour=3)
        self.assertEqual(result['reason'], 'rate limited')
        self.assertNotIn('c-new', self.state['alerts'])
        self.assertTrue(self.run_alert([self.finding(check='c-new')], NOW + 3700, max_per_hour=3)['sent'])

    def test_failed_delivery_is_not_marked_sent_and_is_retried(self):
        class Down:
            name = 'down'
            def send(self, *a): return False
        self.assertEqual(self.run_alert([self.finding()], NOW, [Down()])['reason'], 'delivery failed')
        self.assertEqual(self.state['alerts'], {})
        self.assertTrue(self.run_alert([self.finding()], NOW + 300)['sent'])

    def test_state_is_private_atomic_and_corrupt_state_resets_safely(self):
        path = self.base / 'state' / 'n.json'
        self.run_alert([self.finding()], NOW)
        notify.save_state(path, self.state)
        self.assertEqual(oct(path.stat().st_mode & 0o777), '0o600')
        path.write_text('{not json')
        self.assertEqual(notify.load_state(path)['alerts'], {})

    def test_ring_is_bounded(self):
        for i in range(40):
            self.run_alert([self.finding(check='x%d' % i)], NOW + i, max_per_hour=100)
        self.assertEqual(len(self.state['ring']), notify.RING)

    def credentials(self, content=None):
        d = self.base / 'creds'
        d.mkdir(exist_ok=True)
        (d / 'telegram.json').write_text(json.dumps(content or {'token': TOKEN, 'chat_id': '42'}))
        return d

    def test_telegram_absent_without_credentials_and_invalid_files_ignored(self):
        self.assertIsNone(notify.load_telegram(None))
        self.assertIsNone(notify.load_telegram(self.base / 'creds'))
        for bad in ({'token': 'x', 'chat_id': '1'}, {'token': TOKEN}, {'token': TOKEN, 'chat_id': 'abc'}, ['t']):
            self.assertIsNone(notify.load_telegram(self.credentials(bad)), bad)
        link = self.base / 'linkdir'
        link.mkdir()
        (link / 'telegram.json').symlink_to(self.base / 'creds' / 'telegram.json')
        self.assertIsNone(notify.load_telegram(link))

    def test_telegram_send_and_secret_never_leaks(self):
        seen = []

        def opener(request, timeout):
            seen.append((request.full_url, request.data))
            raise OSError('connect failed to %s' % request.full_url)   # message carries the token URL
        logs = []
        tg = notify.load_telegram(self.credentials(), opener)
        tg.log = logs.append
        report = base = self.base / 'state' / 'r.json'
        report.write_text(json.dumps({'kind': 'desk_healthcheck_v1', 'ts': NOW, 'status': 'CRITICAL', 'checks': [
            self.finding(detail='boom key=SUPERSECRET123 Authorization: Bearer abcdefghijklmnop %s' % TOKEN)]}))
        out = io.StringIO()
        err = io.StringIO()
        from contextlib import redirect_stderr
        with redirect_stderr(err):
            notify.main(['alert', '--report', str(report), '--state', str(self.base / 'state' / 'n.json'),
                         '--credentials-dir', str(self.credentials())], clock=lambda: NOW, opener=opener, stream=out)
        blob = out.getvalue() + err.getvalue() + (self.base / 'state' / 'n.json').read_text()
        self.assertTrue(seen, 'telegram was attempted')
        for secret in (TOKEN, TOKEN.split(':')[1], 'SUPERSECRET123', 'abcdefghijklmnop'):
            self.assertNotIn(secret, blob)
        self.assertIn('telegram send failed: OSError', blob)
        # failed Telegram delivery means the finding is retried, not silently dropped
        self.assertEqual(json.loads((self.base / 'state' / 'n.json').read_text())['alerts'], {})

    def test_telegram_success_body_contains_text_but_state_has_no_token(self):
        bodies = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout):
            bodies.append(request.data.decode())
            return Resp()
        out = io.StringIO()
        report = self.base / 'state' / 'r.json'
        report.write_text(json.dumps({'kind': 'desk_healthcheck_v1', 'ts': NOW, 'status': 'CRITICAL', 'checks': [self.finding()]}))
        notify.main(['alert', '--report', str(report), '--state', str(self.base / 'state' / 'n.json'),
                     '--credentials-dir', str(self.credentials())], clock=lambda: NOW, opener=opener, stream=out)
        self.assertEqual(len(bodies), 1)
        self.assertIn('chat_id=42', bodies[0])
        self.assertIn('CRITICAL', bodies[0])
        self.assertNotIn(TOKEN, (self.base / 'state' / 'n.json').read_text() + out.getvalue())
        self.assertIn('unit%3Adesk-dashboard.service', bodies[0])

    def write_report(self, ts, checks=None, name='r.json'):
        path = self.base / 'state' / name
        report = {'kind': 'desk_healthcheck_v1', 'status': 'OK', 'checks': checks or []}
        if ts is not None:
            report['ts'] = ts
        path.write_text(json.dumps(report))
        return path

    def run_main(self, report, *extra, now=NOW, creds=None):
        out = io.StringIO()
        argv = ['alert', '--report', str(report), '--state', str(self.base / 'state' / 'n.json'), *extra]
        if creds:
            argv += ['--credentials-dir', str(creds)]
        notify.main(argv, clock=lambda: now, stream=out)
        return out.getvalue()

    def test_stale_report_raises_critical_healthcheck_not_running(self):
        report = self.write_report(NOW - 601)   # > 2 * 300
        text = self.run_main(report)
        self.assertIn('healthcheck not running', text)
        self.assertIn('CRITICAL', text)
        self.assertEqual(self.state['alerts'], {})
        saved = json.loads((self.base / 'state' / 'n.json').read_text())['alerts']
        self.assertEqual(saved['healthcheck']['severity'], 'CRITICAL')

    def test_fresh_report_boundary_and_configurable_interval(self):
        for offset, age, interval, stale in ((0, 600, '300', False), (7200, 601, '300', True),
                                             (14400, 1000, '600', False), (21600, 1300, '600', True)):
            now = NOW + offset
            text = self.run_main(self.write_report(now - age), '--interval', interval, now=now)
            self.assertEqual('healthcheck not running' in text, stale, (age, interval))

    def test_stale_ok_report_does_not_hide_behind_old_ok_data(self):
        report = self.write_report(NOW - 100000, checks=[self.finding(severity='OK')])
        self.assertIn('healthcheck not running', self.run_main(report))

    def test_report_without_usable_timestamp_is_treated_as_stale(self):
        for ts in (None, 'yesterday', True, [1], float('nan')):
            path = self.base / 'state' / 'bad.json'
            body = {'kind': 'desk_healthcheck_v1', 'status': 'OK', 'checks': []}
            if ts is not None:
                body['ts'] = ts
            path.write_text(json.dumps(body))
            (self.base / 'state' / 'n.json').unlink(missing_ok=True)
            self.assertIn('healthcheck not running', self.run_main(path), ts)

    def test_stale_report_also_applies_to_daily_summary_blockers(self):
        report = self.write_report(NOW - 5000, checks=[])
        out = io.StringIO()
        notify.main(['daily', '--root', str(self.root), '--report', str(report), '--state', str(self.base / 'state' / 'd.json')],
                    clock=lambda: NOW, stream=out)
        self.assertIn('healthcheck not running', out.getvalue())

    def test_malformed_telegram_json_warns_without_secrets_and_falls_back_to_journal(self):
        d = self.base / 'creds'
        d.mkdir(exist_ok=True)
        cases = {'not json {' + TOKEN: 'x', json.dumps({'token': TOKEN}): 'x', json.dumps({'token': TOKEN, 'chat_id': 'abc'}): 'x',
                 json.dumps({'token': 'SECRETSHORT', 'chat_id': '1'}): 'x', json.dumps([TOKEN]): 'x'}
        for content in cases:
            (d / 'telegram.json').write_text(content)
            with self.assertLogs('tools.ops.notify', level='WARNING') as logs:
                self.assertIsNone(notify.load_telegram(d))
            blob = '\n'.join(logs.output)
            self.assertIn('telegram.json', blob)
            for secret in (TOKEN, TOKEN.split(':')[1], 'SECRETSHORT'):
                self.assertNotIn(secret, blob)
        (d / 'telegram.json').write_bytes(b'\xff\xfe' + TOKEN.encode())
        with self.assertLogs('tools.ops.notify', level='WARNING') as logs:
            self.assertIsNone(notify.load_telegram(d))
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        # end to end: default journal notifier still delivers
        (d / 'telegram.json').write_text('{broken')
        report = self.write_report(NOW, checks=[self.finding()])
        with self.assertLogs('tools.ops.notify', level='WARNING'):
            text = self.run_main(report, creds=d)
        self.assertIn('unit:desk-dashboard.service', text)
        self.assertIn('"telegram": false', text)

    def test_absent_telegram_json_is_silent(self):
        d = self.base / 'creds-empty'
        d.mkdir()
        with self.assertNoLogs('tools.ops.notify', level='WARNING'):
            self.assertIsNone(notify.load_telegram(d))

    def test_redact_patterns(self):
        for text in ('token=abc123', 'api_key: zzz', 'Bearer ' + 'a' * 20, '?key=SECRET&x=1', TOKEN):
            self.assertIn('[REDACTED]', notify.redact(text), text)
        self.assertEqual(notify.redact('plain text with mint ' + MINT), 'plain text with mint ' + MINT)
        self.assertNotIn('hidden', notify.redact('x hidden y', extra=['hidden']))

    def test_unreadable_report_becomes_a_critical_alert(self):
        out = io.StringIO()
        notify.main(['alert', '--report', str(self.base / 'nope.json'), '--state', str(self.base / 'state' / 'n.json')],
                    clock=lambda: NOW, stream=out)
        self.assertIn('notify_input', out.getvalue())

    def test_daily_summary_once_per_day_with_positions_pnl_trades_blockers(self):
        fills = [{'type': 'fill', 'side': 'buy'}, {'type': 'fill', 'side': 'sell'}, {'type': 'reject'}]
        self.write_ledger('RUNNING', {MINT: self.position()}, realized='0.0123', fills=fills)
        report = {'status': 'CRITICAL', 'checks': [self.finding(check='backup', detail='no backup found')]}
        result = notify.daily(self.root, report, self.state, NOW, [self.journal])
        self.assertTrue(result['sent'])
        text = self.stream.getvalue()
        for needle in ('realized_pnl=0.0123', 'So111111', '1 buy, 1 sell', 'backup: no backup found', 'EXECUTION_UNVERIFIED'):
            self.assertIn(needle, text)
        self.assertEqual(notify.daily(self.root, report, self.state, NOW + 60, [self.journal])['reason'], 'already sent today')
        self.assertTrue(notify.daily(self.root, report, self.state, NOW + 60, [self.journal], force=True)['sent'])
        self.assertTrue(notify.daily(self.root, report, self.state, NOW + 86400 + 60, [self.journal])['sent'])

    def test_cli_round_trip_persists_state(self):
        report = self.base / 'state' / 'r.json'
        report.write_text(json.dumps({'kind': 'desk_healthcheck_v1', 'ts': NOW, 'status': 'CRITICAL', 'checks': [self.finding()]}))
        argv = ['alert', '--report', str(report), '--state', str(self.base / 'state' / 'n.json')]
        out1, out2 = io.StringIO(), io.StringIO()
        notify.main(argv, clock=lambda: NOW, stream=out1)
        notify.main(argv, clock=lambda: NOW + 60, stream=out2)
        self.assertIn('"sent": true', out1.getvalue())
        self.assertIn('nothing new', out2.getvalue())


class UnitTemplateTests(unittest.TestCase):
    """Render every template with placeholders filled and assert the T09 F8/F11 invariants."""
    MAP = {'FRESH_ROOT': '/var/lib/solana-desk/exp-1', 'RELEASE_DIR': '/opt/solana-desk-releases/abc1234',
           'CONFIG': '/etc/solana-paper/fresh.json', 'SCHEDULER_IDENTITY': '66306:1234',
           'PACING_DB': '/var/lib/solana-desk/provider-pacing.sqlite', 'PACING_DIR': '/var/lib/solana-desk',
           'DISCOVERY_DB': '/var/lib/solana-desk/discovery/continuous.sqlite',
           'DISCOVERY_DIR': '/var/lib/solana-desk/discovery', 'TAKER': '6E2G75Z3uJEnPo9EvzmLTxp8KB78m3RDsFBjoCTVHZD2',
           'AMOUNT_RAW': '100000000', 'POOL_FEE_BPS': '25', 'BACKUP_ROOT': '/var/backups/solana-desk/fresh-2026-10-11.paper-quote-kraken.fresh.1', 'STATE_DIR': '/var/lib/solana-desk-health',
           'ARCHIVED_ROOT': '/var/lib/solana-desk'}
    MANUAL_TIMERS = {'desk-paper-entry-dispatcher.timer', 'desk-paper-monitor.timer'}
    SCHEDULER_UNITS = ['desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.service',
                       'desk-paper-monitor.service', 'desk-decisions.service']

    @classmethod
    def setUpClass(cls):
        cls.raw = {p.name: p.read_text() for p in sorted(FRESH.glob('desk-*.service')) + sorted(FRESH.glob('desk-*.timer'))}
        cls.units = {}
        for name, text in cls.raw.items():
            rendered = text
            for key, value in cls.MAP.items():
                rendered = rendered.replace('<%s>' % key, value)
            cls.units[name] = rendered

    def directives(self, name, key):
        return [l.split('=', 1)[1] for l in self.units[name].splitlines() if l.startswith(key + '=')]

    def exec_start(self, name):
        (line,) = self.directives(name, 'ExecStart')
        return line

    def test_all_expected_units_exist(self):
        expected = {'desk-continuous-discovery.service', 'desk-dashboard.service', 'desk-backup.service', 'desk-backup.timer',
                    'desk-paper-entry-dispatcher.service', 'desk-paper-entry-dispatcher.timer', 'desk-paper-held-cycle.service',
                    'desk-paper-held-cycle.timer', 'desk-paper-monitor.service', 'desk-paper-monitor.timer',
                    'desk-decisions.service', 'desk-decisions.timer', 'desk-healthcheck.service', 'desk-healthcheck.timer',
                    'desk-notify-daily.service', 'desk-notify-daily.timer',
                    'desk-held-watcher.service',   # T28 adds the held watcher
                    'desk-counterfactual.service', 'desk-counterfactual.timer',   # T26 research sampler
                    'desk-features.service', 'desk-features.timer',   # T40 feature store (research)
                    'desk-notify-watchdog.service', 'desk-notify-watchdog.timer',   # T32F item 9
                    'desk-daily-report.service', 'desk-daily-report.timer'}   # T41 research report
        self.assertEqual(set(self.units), expected)
        self.assertEqual(set(healthcheck.UNITS) - set(self.units), set())

    def test_no_placeholder_is_left_and_every_placeholder_is_known(self):
        used = set()
        for name, text in self.raw.items():
            used |= set(re.findall(r'<([A-Z_]+)>', text))
        self.assertLessEqual(used, set(self.MAP))
        for name, text in self.units.items():
            self.assertNotRegex(re.sub(r'(?m)^#.*$', '', text), r'<[A-Z_]+>', name)

    def test_watchdog_is_a_separate_timer_that_detects_a_dead_healthcheck_within_fifteen_minutes(self):
        """T32F item 9: the healthcheck notifies only from its own ExecStopPost, so a dead timer needs its own watchdog."""
        timer, service = self.units['desk-notify-watchdog.timer'], self.units['desk-notify-watchdog.service']
        self.assertNotIn('desk-healthcheck', timer)                       # independent of the unit it watches
        period = int(re.search(r'OnUnitInactiveSec=(\d+)', timer).group(1))
        from tools.ops import notify
        stale_after = 2 * notify.DEFAULT_INTERVAL                         # check_freshness: older than 2 x interval is CRITICAL
        self.assertLessEqual(stale_after + period, 15 * 60)
        self.assertIn('tools.ops.notify alert', service)
        self.assertIn('health.json', service)
        self.assertIn('watchdog-state.json', service)                     # its own de-duplication state
        self.assertNotIn('notify-state.json', service.replace('watchdog-state.json', ''))
        self.assertIn('WantedBy=timers.target', timer)

    def test_health_and_notify_units_see_the_archived_root_read_only(self):
        """T32F item 8."""
        for name in ('desk-healthcheck.service', 'desk-notify-daily.service', 'desk-notify-watchdog.service'):
            read_only = ' '.join(self.directives(name, 'ReadOnlyPaths')).split()
            writable = ' '.join(self.directives(name, 'ReadWritePaths')).split()
            self.assertIn('/var/lib/solana-desk', read_only, name)
            self.assertNotIn('/var/lib/solana-desk', writable, name)          # the old root is never writable here

    def test_dashboard_uses_the_shared_pacing_database(self):
        """T32F item 6 (T09 F11): dashboard scans go through the shared 2 s pacing."""
        text = self.units['desk-dashboard.service']
        self.assertIn('Environment=DESK_PROVIDER_PACING_DB=/var/lib/solana-desk/provider-pacing.sqlite', text)
        self.assertIn('/var/lib/solana-desk', ' '.join(self.directives('desk-dashboard.service', 'ReadWritePaths')).split())

    def test_unit_names_match_the_cutover_tool_pattern(self):
        # Same pattern as tools.ops.cutover.UNIT_RE (T11, a separate branch; duplicated to stay independent).
        for name in self.units:
            self.assertRegex(name, r'^desk-[a-z0-9][a-z0-9-]{0,62}\.(service|timer)$')

    def test_every_timer_has_install_and_its_service(self):
        for name, text in self.units.items():
            if name.endswith('.timer'):
                self.assertIn('[Install]', text, name)
                self.assertIn('WantedBy=timers.target', text, name)
                target = (self.directives(name, 'Unit') or [name[:-6] + '.service'])[0]
                self.assertIn(target, self.units)

    def test_boot_start_set_and_manual_exceptions(self):
        for name, text in self.units.items():
            if name.endswith('.timer'):
                self.assertTrue(('desk-paper-' in name and name in self.MANUAL_TIMERS) or name not in self.MANUAL_TIMERS)
        for name in self.MANUAL_TIMERS:   # documented as manual-enable in the unit itself
            self.assertRegex(self.raw[name], r'(?i)enable|enabled')
        self.assertIn('WantedBy=multi-user.target', self.units['desk-continuous-discovery.service'])
        self.assertIn('WantedBy=multi-user.target', self.units['desk-dashboard.service'])

    def test_discovery_restarts_cleanly_instead_of_exiting_after_24h(self):
        text = self.units['desk-continuous-discovery.service']
        self.assertIn('\nRestart=always\n', text)
        self.assertRegex(text, r'RestartSec=\d+')
        self.assertIn('StartLimitBurst=', text)
        self.assertIn('StartLimitIntervalSec=', text)
        self.assertIn('--seconds 86400', text)

    def test_long_running_services_restart_on_failure_with_start_limits(self):
        for name in ('desk-dashboard.service',):
            self.assertIn('Restart=on-failure', self.units[name])
            self.assertIn('StartLimitBurst=', self.units[name])

    def test_entry_unit_executes_with_credentials_and_covering_timeout(self):
        cmd = self.exec_start('desk-paper-entry-dispatcher.service')
        self.assertIn(' --execute', cmd)
        self.assertIn(' --systemd-credentials', cmd)
        self.assertNotIn('--plan', cmd)
        self.assertGreaterEqual(int(self.directives('desk-paper-entry-dispatcher.service', 'TimeoutStartSec')[0]), 600)
        self.assertIn('LoadCredential=provider-keys.json:/etc/solana-desk/provider-keys.json', self.units['desk-paper-entry-dispatcher.service'])

    def test_held_timeout_covers_worst_case_and_has_no_dependency_blocker(self):
        self.assertGreaterEqual(int(self.directives('desk-paper-held-cycle.service', 'TimeoutStartSec')[0]), 120)
        for name in self.units:
            self.assertNotIn('--dependency-blocker', self.units[name], name)
        self.assertIn('--systemd-credentials', self.exec_start('desk-paper-held-cycle.service'))

    def test_scheduler_units_have_lease_identity_and_lock_precondition(self):
        for name in self.SCHEDULER_UNITS:
            text = self.units[name]
            self.assertIn('Environment=DESK_PAPER_SCHEDULER_IDENTITY=66306:1234', text, name)
            self.assertIn('ConditionPathExists=/var/lib/solana-desk/exp-1/paper-scheduler.lock', text, name)

    def test_entry_and_held_have_the_shared_pacing_database(self):
        for name in ('desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.service'):
            self.assertIn('Environment=DESK_PROVIDER_PACING_DB=/var/lib/solana-desk/provider-pacing.sqlite', self.units[name])
        self.assertIn('--pacing-db /var/lib/solana-desk/provider-pacing.sqlite', self.exec_start('desk-paper-entry-dispatcher.service'))

    def test_entry_held_monitor_use_the_same_config_and_ledger(self):
        def arg(cmd, flag):
            m = re.search(re.escape(flag) + r' (\S+)', cmd)
            return m.group(1) if m else None
        entry, held, mon = (self.exec_start(n) for n in ('desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.service', 'desk-paper-monitor.service'))
        self.assertEqual(arg(entry, '--config'), arg(held, '--config'))
        self.assertEqual(arg(held, '--config'), arg(mon, '--config'))
        self.assertEqual(arg(entry, '--ledger-db'), arg(held, '--ledger-db'))
        self.assertEqual(arg(held, '--ledger-db'), arg(mon, '--db'))
        self.assertEqual(arg(entry, '--research-db'), arg(held, '--research-db'))
        self.assertEqual(arg(entry, '--evidence-db'), arg(held, '--evidence-db'))

    def test_store_paths_match_fresh_start_layout(self):
        root = self.MAP['FRESH_ROOT']
        joined = ' '.join(self.exec_start(n) for n in self.SCHEDULER_UNITS)
        for rel in ('research.sqlite', 'evidence.sqlite', 'paper-ledger.sqlite', 'paper-decisions.sqlite', 'entry-dispatch/dispatch.sqlite'):
            self.assertIn('%s/%s' % (root, rel), joined)
        for name, text in self.units.items():
            for legacy in ('active-paper.sqlite', 'paper-kraken-', 'paper-quote-reviewed', '/config/paper.json', 'launches.sqlite', 'raw.sqlite'):
                self.assertNotIn(legacy, text, '%s mentions legacy %s' % (name, legacy))
        fresh_start = REPO / 'tools' / 'ops' / 'fresh_start.py'
        if fresh_start.exists():   # layout owner (T13); only checked once merged
            source = fresh_start.read_text()
            for rel in ('research.sqlite', 'evidence.sqlite', 'paper-ledger.sqlite', 'paper-decisions.sqlite', 'entry-dispatch/dispatch.sqlite'):
                self.assertIn(rel, source)

    def test_hardening_present_on_every_service(self):
        for name, text in self.units.items():
            if name.endswith('.service'):
                for needle in ('ProtectSystem=strict', 'NoNewPrivileges=true', 'ProtectHome=true', 'PrivateTmp=true', 'UMask=0077', 'MemoryMax=', 'User=solana-desk'):
                    self.assertIn(needle, text, '%s lacks %s' % (name, needle))

    def test_offline_units_have_no_network(self):
        for name in ('desk-paper-monitor.service', 'desk-decisions.service', 'desk-backup.service'):
            self.assertIn('RestrictAddressFamilies=AF_UNIX', self.units[name], name)
        for name in ('desk-paper-monitor.service', 'desk-decisions.service'):
            self.assertIn('\nPrivateNetwork=true\n', self.units[name], name)

    def test_credentials_only_where_providers_are_called_and_telegram_never_embedded(self):
        for name in ('desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.service', 'desk-continuous-discovery.service', 'desk-dashboard.service'):
            self.assertIn('LoadCredential=provider-keys.json', self.units[name])
        for name, text in self.units.items():
            self.assertNotRegex(text, r'\d{6,}:[A-Za-z0-9_-]{30,}', name)
            self.assertNotIn('telegram.json', text, name)
        example = (FRESH / 'telegram.conf.example').read_text()
        self.assertIn('LoadCredential=telegram.json:/etc/solana-desk/telegram.json', example)

    def test_nothing_binds_off_loopback(self):
        for name, text in self.units.items():
            for line in text.splitlines():
                if line.startswith('ExecStart='):
                    self.assertNotRegex(line, r'0\.0\.0\.0|\[::\]|--(host|bind|listen|addr|address)\b', name)
        self.assertNotRegex(self.units['desk-dashboard.service'], r'--(host|bind|listen)')

    def test_backup_unit_uses_unique_destination_and_prunes_only_after_success(self):
        text = self.units['desk-backup.service']
        self.assertIn('tools.ops.backup', text)
        self.assertNotIn('desk.backup', re.sub(r'(?m)^#.*$', '', text))
        self.assertRegex(text, r'--destination \S+/daily-\$\$\(date -u \+%%Y%%m%%dT%%H%%M%%SZ\)')
        self.assertIn('ExecStartPost=/usr/bin/find %s -maxdepth 1 -type d -name daily-*' % self.MAP['BACKUP_ROOT'], text)
        self.assertNotIn('ExecStopPost', text)

    def test_healthcheck_runs_notify_after_both_outcomes_and_is_read_only_mounted_where_possible(self):
        text = self.units['desk-healthcheck.service']
        self.assertIn('ExecStopPost=', text)
        self.assertIn('tools.ops.notify alert', text)
        self.assertIn('ReadOnlyPaths=', text)
        self.assertIn('tools.ops.healthcheck', self.exec_start('desk-healthcheck.service'))

    def test_oneshot_services_have_no_restart_and_timers_do_not_overlap(self):
        for name in ('desk-paper-entry-dispatcher', 'desk-paper-held-cycle', 'desk-paper-monitor', 'desk-decisions', 'desk-healthcheck'):
            self.assertNotIn('Restart=', self.units[name + '.service'])
            self.assertIn('OnUnitInactiveSec=', self.units[name + '.timer'])

    def test_deployed_templates_are_untouched(self):
        for name in ('desk-paper-entry-dispatcher.service', 'desk-paper-held-cycle.service', 'desk-continuous-discovery.service'):
            old = (REPO / 'deploy' / name).read_text()
            self.assertNotIn('<FRESH_ROOT>', old)


if __name__ == '__main__':
    unittest.main()
