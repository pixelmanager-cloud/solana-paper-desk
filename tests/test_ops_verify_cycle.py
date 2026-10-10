"""SYNTHETIC_TEST_ONLY: round-trip snapshot and cold-restart comparison.

Ledgers are produced by the real engine through Ledger.apply (config/paper.json,
synthetic events from tests.helpers). The engine's constant-product fills carry no
execution label, so the fixture adds the EXECUTION_UNVERIFIED label to the stored
fill payloads exactly as quote-mode fills carry it. Budget stores use the DDL of
desk/monitoring_budget.py, desk/history_progress.py and desk/provider_pacing.py.
No provider, network or production store is involved.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.paper_cycle_cli import _config
from tests.helpers import ROOT, T, config, event
from tools.ops import verify_cycle as vc

MONITOR_DDL = (
    'CREATE TABLE paper_monitoring_budget(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL,'
    'ledger TEXT NOT NULL,config_hash TEXT NOT NULL,code_hash TEXT NOT NULL,cap INTEGER NOT NULL,'
    'window_seconds INTEGER NOT NULL,high_water REAL NOT NULL,total INTEGER NOT NULL,blocked TEXT)',
    'CREATE TABLE paper_monitoring_reservations(id INTEGER PRIMARY KEY,at REAL NOT NULL,scan_id TEXT NOT NULL,'
    'mint TEXT NOT NULL,checkpoint_hash TEXT NOT NULL,method TEXT NOT NULL,params_hash TEXT NOT NULL)',
    'CREATE TABLE paper_monitoring_outcomes(reservation_id INTEGER PRIMARY KEY '
    'REFERENCES paper_monitoring_reservations(id),evidence_hash TEXT NOT NULL)')


def tree_hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(root).rglob('*')) if p.is_file()}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Resolve once: a symlinked TMPDIR must not look like a symlinked store.
        self.data = Path(os.path.realpath(self.tmp.name)) / 'data'
        self.data.mkdir()
        self.config_path = self.data.parent / 'paper.json'
        self.config_path.write_text(json.dumps(config()))
        self.cfg = _config(self.config_path)
        self.ledger_path = self.data / 'paper.sqlite'
        self.ledger = Ledger(self.ledger_path)
        self.addCleanup(lambda: self.ledger.close())

    def apply(self, *events):
        for e in events:
            self.ledger.apply(e, self.cfg, transition, initial_state)

    def label(self):
        """Quote-mode fills carry the label; add it to the engine's stored fills."""
        db = self.ledger.db
        for seq, payload in db.execute('SELECT seq,payload FROM outcomes').fetchall():
            v = json.loads(payload)
            if v.get('type') == 'fill' and 'execution_status' not in v:
                v['execution_status'] = vc.LABEL
                db.execute('UPDATE outcomes SET payload=? WHERE seq=?', (json.dumps(v), seq))

    def round_trip(self, mint='SYNTHETIC_A', at=T):
        self.apply(event(at, mint=mint), event(at + 5, mint=mint, reserve_sol='160'),
                   event(at + 10, mint=mint, danger=True, reserve_sol='160'))

    def snap(self, **kw):
        self.label_once()
        return vc.snapshot(self.data, 'paper.sqlite', self.config_path, **kw)

    def label_once(self):
        self.label()

    def sql(self, statement, *args):
        self.ledger.db.execute(statement, args)

    def fresh_snap(self):
        return copy.deepcopy(self.snap())


class SnapshotStatusTests(Base):
    def test_no_fills_when_events_exist_but_nothing_traded(self):
        self.apply(event(T, danger=True))
        s = self.snap()
        self.assertEqual((s['status'], s['fill_count'], s['open_positions'], s['round_trips']),
                         ('NO_FILLS', 0, [], []))
        self.assertEqual(s['cash_sol'], self.cfg['initial_equity_sol'])

    def test_open_position_reports_entry_identity_and_label(self):
        self.apply(event(T))
        s = self.snap()
        self.assertEqual(s['status'], 'OPEN_POSITION')
        (p,) = s['open_positions']
        self.assertEqual((p['mint'], p['entry_ts'], p['execution_status']),
                         ('SYNTHETIC_A', T, 'EXECUTION_UNVERIFIED'))
        self.assertEqual(p['entry_event_id'], f'SYNTHETIC_A:{T}')
        self.assertGreater(float(p['entry_price_sol_per_token']), 0)
        self.assertEqual(s['execution_status'], 'EXECUTION_UNVERIFIED')

    def test_round_trip_validates_with_net_accounting(self):
        self.round_trip()
        s = self.snap()
        self.assertEqual(s['status'], 'VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP')
        (trip,) = s['round_trips']
        self.assertEqual(s['open_positions'], [])
        self.assertGreaterEqual(len(trip['sells']), 2)
        self.assertEqual(s['realized_pnl_sol'], json.loads(
            self.ledger.db.execute('SELECT payload FROM state').fetchone()[0])['realized_pnl'])

    def test_closed_trip_and_open_position_attributed_separately(self):
        self.round_trip('SYNTHETIC_A', T)
        self.apply(event(T + 100, mint='SYNTHETIC_B'))
        s = self.snap()
        self.assertEqual(s['status'], 'OPEN_POSITION')
        self.assertEqual([p['mint'] for p in s['open_positions']], ['SYNTHETIC_B'])
        self.assertEqual([p['mint'] for p in s['round_trips']], ['SYNTHETIC_A'])
        self.assertNotEqual(s['open_positions'][0]['entry_fill_hash'], s['round_trips'][0]['entry_fill_hash'])


class AdversarialLedgerTests(Base):
    def assertRejects(self, pattern):
        with self.assertRaisesRegex(vc.VerifyError, pattern):
            vc.snapshot(self.data, 'paper.sqlite', self.config_path)

    def test_duplicate_fill_detected(self):
        self.round_trip()
        self.label()
        self.sql('INSERT INTO outcomes(event_id,payload) SELECT event_id,payload FROM outcomes '
                 "WHERE json_extract(payload,'$.side')='buy'")
        self.assertRejects('duplicate fill identity')

    def test_duplicate_sell_detected_as_oversell_or_duplicate(self):
        self.round_trip()
        self.label()
        self.sql('INSERT INTO outcomes(event_id,payload) SELECT event_id,payload FROM outcomes '
                 "WHERE json_extract(payload,'$.side')='sell' ORDER BY seq DESC LIMIT 1")
        self.assertRejects('duplicate fill identity')

    def test_cash_mismatch_detected(self):
        self.round_trip()
        self.label()
        state = json.loads(self.ledger.db.execute('SELECT payload FROM state').fetchone()[0])
        state['cash'] = str(vc.Decimal(state['cash']) + vc.Decimal('0.001'))
        self.sql('UPDATE state SET payload=?', json.dumps(state))
        self.assertRejects('checkpoint cash differs')

    def test_realized_pnl_mismatch_detected(self):
        self.round_trip()
        self.label()
        state = json.loads(self.ledger.db.execute('SELECT payload FROM state').fetchone()[0])
        state['realized_pnl'] = '9'
        self.sql('UPDATE state SET payload=?', json.dumps(state))
        self.assertRejects('realized PnL differs')

    def test_sell_before_buy_rejected(self):
        self.round_trip()
        self.label()
        self.sql("UPDATE outcomes SET seq=seq+1000 WHERE json_extract(payload,'$.side')='buy'")
        self.assertRejects('no open entry')

    def test_sell_not_after_its_entry_rejected(self):
        self.round_trip()
        self.label()
        self.sql('UPDATE events SET ts=? WHERE ts=?', T, T + 5)
        self.assertRejects('is not after its entry')

    def test_unlabelled_fill_rejected(self):
        self.round_trip()
        self.assertRejects('lacks the EXECUTION_UNVERIFIED label')

    def test_wrong_label_rejected(self):
        self.round_trip()
        self.label()
        self.sql("UPDATE outcomes SET payload=json_set(payload,'$.execution_status','VERIFIED') "
                 "WHERE json_extract(payload,'$.side')='buy'")
        self.assertRejects('lacks the EXECUTION_UNVERIFIED label')

    def test_open_position_missing_from_checkpoint_rejected(self):
        self.apply(event(T))
        self.label()
        state = json.loads(self.ledger.db.execute('SELECT payload FROM state').fetchone()[0])
        state['positions'] = {}
        self.sql('UPDATE state SET payload=?', json.dumps(state))
        self.assertRejects('positions differ from the fill journal')

    def test_oversell_rejected(self):
        self.round_trip()
        self.label()
        self.sql("UPDATE outcomes SET payload=json_set(payload,'$.quantity','999999999') "
                 "WHERE json_extract(payload,'$.side')='sell'")
        self.assertRejects('exceeds the entry quantity')

    def test_sell_with_other_pool_identity_rejected(self):
        self.round_trip()
        self.label()
        self.sql("UPDATE events SET payload=json_set(payload,'$.pool','OTHER_POOL') WHERE ts>?", T)
        self.assertRejects('position identity mismatch')

    def test_cost_basis_tampering_rejected(self):
        self.round_trip()
        self.label()
        self.sql("UPDATE outcomes SET payload=json_set(payload,'$.realized_pnl_sol','5') "
                 "WHERE json_extract(payload,'$.side')='sell'")
        self.assertRejects('cost basis|checkpoint')

    def test_orphan_outcome_and_missing_checkpoint_rejected(self):
        self.round_trip()
        self.label()
        self.sql("INSERT INTO outcomes(event_id,payload) VALUES('ghost','{\"type\":\"x\"}')")
        self.assertRejects('EVENT_JOURNAL_INCOMPLETE')
        self.sql("DELETE FROM outcomes WHERE event_id='ghost'")
        self.sql('DELETE FROM state')
        self.assertRejects('CHECKPOINT_MISSING')

    def test_config_mismatch_rejected(self):
        self.apply(event(T))
        self.label()
        other = config()
        other['initial_equity_sol'] = '6'
        self.config_path.write_text(json.dumps(other))
        self.assertRejects('SAVED_CONFIG_MISMATCH')

    def test_never_initialized_ledger_is_not_a_clean_pass(self):
        self.assertRejects('LEDGER_NEVER_INITIALIZED')

    def test_symlinked_ledger_and_data_refused(self):
        self.apply(event(T))
        self.label()
        link = self.data / 'link.sqlite'
        link.symlink_to(self.ledger_path)
        with self.assertRaisesRegex(vc.VerifyError, 'regular non-symlink'):
            vc.snapshot(self.data, 'link.sqlite', self.config_path)
        alias = self.data.parent / 'alias'
        alias.symlink_to(self.data)
        with self.assertRaisesRegex(vc.VerifyError, 'canonical'):
            vc.snapshot(alias, 'paper.sqlite', self.config_path)

    def test_store_names_cannot_escape_data_directory(self):
        self.apply(event(T))
        self.label()
        with self.assertRaisesRegex(vc.VerifyError, 'plain names'):
            vc.snapshot(self.data, '../paper.sqlite', self.config_path)


class StoresAndReadOnlyTests(Base):
    def stores(self, *, reservations=2, outcomes=1, used=2, duplicate=False, journal=True):
        with sqlite3.connect(self.data / 'research.sqlite') as c:
            for ddl in MONITOR_DDL:
                c.execute(ddl)
            c.execute("INSERT INTO paper_monitoring_budget VALUES(1,1,'l','c','k',3600,3600,100.5,?,NULL)", (used,))
            for i in range(1, reservations + 1):
                c.execute('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)',
                          (i, 100.0 + i, 'scan', 'M', 'ck', 'getAccountInfo', 'same' if duplicate else f'p{i}'))
            for i in range(1, outcomes + 1):
                c.execute('INSERT INTO paper_monitoring_outcomes VALUES(?,?)', (i, f'e{i}'))
        with sqlite3.connect(self.data / 'evidence.sqlite') as c:
            c.execute('CREATE TABLE ownership_budgets(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,'
                      'used INTEGER NOT NULL,ceiling INTEGER NOT NULL)')
            c.execute("INSERT INTO ownership_budgets VALUES('b1','h1',4,18)")
        with sqlite3.connect(self.data / 'provider-pacing.sqlite') as c:
            c.executescript('CREATE TABLE state(provider TEXT PRIMARY KEY,next_at REAL NOT NULL,'
                            'blocked_until REAL NOT NULL,high_water REAL NOT NULL,pending TEXT);'
                            'CREATE TABLE waiters(ticket TEXT PRIMARY KEY);'
                            "INSERT INTO state VALUES('kraken',10,0,9.5,NULL);")
        if journal:
            (self.data / 'entry-dispatch').mkdir()
            with sqlite3.connect(self.data / 'entry-dispatch' / 'j.sqlite') as c:
                c.executescript('CREATE TABLE context(id INTEGER PRIMARY KEY,payload TEXT,hash TEXT);'
                                'CREATE TABLE intents(id TEXT PRIMARY KEY,payload TEXT,hash TEXT);'
                                'CREATE TABLE results(id TEXT PRIMARY KEY,payload TEXT,hash TEXT);'
                                "INSERT INTO context VALUES(1,'{}','h');INSERT INTO intents VALUES('i1','{}','hi');")

    def test_budget_pacing_ownership_and_journal_sections(self):
        self.apply(event(T))
        self.stores()
        s = self.snap()
        m = s['monitoring']
        self.assertEqual((m['used_total'], m['cap'], m['reserved_pending'], m['high_water']), (2, 3600, 1, 100.5))
        self.assertEqual(m['reservations']['count'], 2)
        self.assertEqual(s['ownership']['used_total'], 4)
        self.assertEqual(s['pacing']['providers'][0][3], 9.5)
        self.assertEqual(s['dispatcher']['journals']['j.sqlite']['intents']['count'], 1)
        self.assertEqual(s['dispatcher']['journals']['j.sqlite']['results']['count'], 0)

    def test_absent_optional_stores_are_reported_not_invented(self):
        self.apply(event(T))
        s = self.snap()
        self.assertEqual((s['monitoring'], s['ownership'], s['pacing'], s['dispatcher']),
                         ({'present': False},) * 3 + ({'present': False},))

    def test_snapshot_never_mutates_any_store_byte(self):
        self.round_trip()
        self.stores()
        self.label()
        self.ledger.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.ledger.close()
        before = tree_hashes(self.data)
        vc.snapshot(self.data, 'paper.sqlite', self.config_path)
        vc.snapshot(self.data, 'paper.sqlite', self.config_path)
        self.assertEqual(before, tree_hashes(self.data))

    def test_live_wal_sidecars_are_not_added_to_or_main_file_changed(self):
        self.round_trip()
        self.label()
        main = hashlib.sha256(self.ledger_path.read_bytes()).hexdigest()
        names = {p.name for p in self.data.iterdir()}
        self.assertTrue((self.data / 'paper.sqlite-wal').stat().st_size > 0)  # writer still open
        vc.snapshot(self.data, 'paper.sqlite', self.config_path)
        self.assertEqual(names, {p.name for p in self.data.iterdir()})
        self.assertEqual(main, hashlib.sha256(self.ledger_path.read_bytes()).hexdigest())

    def test_read_only_connection_rejects_writes(self):
        self.apply(event(T))
        with vc._connect(self.ledger_path, 'ledger') as c:
            with self.assertRaises(sqlite3.OperationalError):
                c.execute('DELETE FROM events')
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)


class CompareTests(StoresAndReadOnlyTests):
    def test_cold_restart_with_reopened_database_passes(self):
        self.round_trip()
        self.stores()
        a = self.fresh_snap()
        self.ledger.close()
        self.ledger = Ledger(self.ledger_path, must_exist=True)  # service restart reopens the WAL ledger
        b = copy.deepcopy(vc.snapshot(self.data, 'paper.sqlite', self.config_path))
        result = vc.compare(a, b)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(vc.compare(a, b, allow_progress=True)['status'], 'PASS')

    def test_strict_compare_fails_on_any_new_activity(self):
        self.apply(event(T))
        self.stores()
        a = self.fresh_snap()
        self.apply(event(T + 5, reserve_sol='160'))
        b = self.fresh_snap()
        result = vc.compare(a, b)
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(any('fills' in f or 'checkpoint' in f for f in result['failures']), result)

    def test_allow_progress_accepts_append_only_growth(self):
        self.apply(event(T))
        self.stores(reservations=2, outcomes=1, used=2)
        a = self.fresh_snap()
        self.apply(event(T + 5, reserve_sol='160'), event(T + 10, danger=True, reserve_sol='160'))
        with sqlite3.connect(self.data / 'research.sqlite') as c:
            c.execute("INSERT INTO paper_monitoring_reservations VALUES(3,103,'scan','M','ck','getAccountInfo','p3')")
            c.execute('UPDATE paper_monitoring_budget SET total=3,high_water=103.5')
        b = self.fresh_snap()
        result = vc.compare(a, b, allow_progress=True)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertEqual(vc.compare(a, b)['status'], 'FAIL')
        self.assertEqual(vc.compare(b, a, allow_progress=True)['status'], 'FAIL')  # shrinking is not progress

    def test_allow_progress_detects_rewritten_old_prefix(self):
        self.apply(event(T))
        self.stores()
        a = self.fresh_snap()
        self.apply(event(T + 5, reserve_sol='160'))
        b = self.fresh_snap()
        b['fills'][0]['fill_hash'] = '0' * 64
        self.assertIn('fills: old prefix differs', vc.compare(a, b, allow_progress=True)['failures'])
        b = self.fresh_snap()
        b['tables']['events']['rolling']['1'] = '0' * 16
        self.assertTrue(any('ledger events' in f for f in vc.compare(a, b, allow_progress=True)['failures']))

    def test_rewritten_budget_rows_and_regressions_fail(self):
        self.apply(event(T))
        self.stores()
        a = self.fresh_snap()
        with sqlite3.connect(self.data / 'research.sqlite') as c:
            c.execute("UPDATE paper_monitoring_reservations SET params_hash='rewritten' WHERE id=1")
        b = self.fresh_snap()
        self.assertTrue(any('monitoring reservations' in f
                            for f in vc.compare(a, b, allow_progress=True)['failures']))
        with sqlite3.connect(self.data / 'research.sqlite') as c:
            c.execute("UPDATE paper_monitoring_reservations SET params_hash='p1' WHERE id=1")
            c.execute('UPDATE paper_monitoring_budget SET total=1')
        self.assertIn('monitoring budget regressed', vc.compare(a, self.fresh_snap(), allow_progress=True)['failures'])
        with sqlite3.connect(self.data / 'evidence.sqlite') as c:
            c.execute('UPDATE ownership_budgets SET used=1')
        self.assertIn('ownership budget regressed or rewritten',
                      vc.compare(a, self.fresh_snap(), allow_progress=True)['failures'])
        with sqlite3.connect(self.data / 'provider-pacing.sqlite') as c:
            c.execute('UPDATE state SET high_water=1')
        self.assertIn('pacing high-water regressed',
                      vc.compare(a, self.fresh_snap(), allow_progress=True)['failures'])

    def test_duplicate_monitoring_charge_fails_even_when_progress_allowed(self):
        self.apply(event(T))
        self.stores()
        a = self.fresh_snap()
        with sqlite3.connect(self.data / 'research.sqlite') as c:
            c.execute("INSERT INTO paper_monitoring_reservations VALUES(3,103,'scan','M','ck','getAccountInfo','p1')")
            c.execute('UPDATE paper_monitoring_budget SET total=3')
        result = vc.compare(a, self.fresh_snap(), allow_progress=True)
        self.assertIn('duplicate monitoring charge appeared', result['failures'])

    def test_duplicate_fill_in_either_snapshot_fails(self):
        self.apply(event(T))
        a = self.fresh_snap()
        b = copy.deepcopy(a)
        b['fills'].append(copy.deepcopy(b['fills'][0]))
        self.assertIn('B: duplicate fill', vc.compare(a, b)['failures'])

    def test_identity_changes_and_garbage_fail_closed(self):
        self.apply(event(T))
        a = self.fresh_snap()
        b = copy.deepcopy(a)
        b['config_hash'] = 'x'
        self.assertIn('config_hash differs', vc.compare(a, b, allow_progress=True)['failures'])
        self.assertEqual(vc.compare(a, {})['status'], 'FAIL')

    def test_dispatcher_journal_rewrite_fails(self):
        self.apply(event(T))
        self.stores()
        a = self.fresh_snap()
        with sqlite3.connect(self.data / 'entry-dispatch' / 'j.sqlite') as c:
            c.execute("UPDATE intents SET hash='changed'")
        self.assertTrue(any('dispatcher' in f for f in vc.compare(a, self.fresh_snap(), allow_progress=True)['failures']))


class CliTests(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = vc.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_snapshot_compare_exit_codes_and_no_overwrite(self):
        self.round_trip()
        self.label()
        a, b = str(self.data.parent / 'a.json'), str(self.data.parent / 'b.json')
        base = ['snapshot', '--data', str(self.data), '--ledger', 'paper.sqlite', '--config', str(self.config_path)]
        code, out, _ = self.run_cli(*base, '--out', a)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['status'], 'VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP')
        self.assertEqual(oct(os.stat(a).st_mode & 0o777), '0o600')
        self.assertEqual(self.run_cli(*base, '--out', a)[0], 2)  # never overwrite a retained snapshot
        self.assertEqual(self.run_cli(*base, '--out', b)[0], 0)
        code, out, _ = self.run_cli('compare', a, b)
        self.assertEqual((code, json.loads(out)['status']), (0, 'PASS'))
        doc = json.loads(Path(b).read_text())
        doc['cash_sol'] = '1'
        Path(b).write_text(json.dumps(doc))
        code, out, _ = self.run_cli('compare', a, b)
        self.assertEqual((code, json.loads(out)['status']), (1, 'FAIL'))

    def test_corrupt_ledger_is_exit_two_not_a_pass(self):
        self.round_trip()
        self.label()
        self.sql('DELETE FROM state')
        code, _, err = self.run_cli('snapshot', '--data', str(self.data), '--ledger', 'paper.sqlite',
                                    '--config', str(self.config_path), '--out', str(self.data.parent / 'x.json'))
        self.assertEqual(code, 2)
        self.assertIn('CHECKPOINT_MISSING', err)
        self.assertFalse((self.data.parent / 'x.json').exists())


if __name__ == '__main__':
    unittest.main()
