"""SYNTHETIC_TEST_ONLY: round-trip snapshot and cold-restart comparison.

Ledgers are produced by the real engine through Ledger.apply (config/paper.json,
synthetic events from tests.helpers). The engine's constant-product fills carry no
execution label, so the fixture adds the EXECUTION_UNVERIFIED label to the stored
fill payloads exactly as quote-mode fills carry it. Budget stores are built with the
real APIs: EvidenceStore + MonitoringBudget.provision() (paper_monitoring_* live in
evidence.sqlite), HistoryProgress (ownership_budgets), provider_pacing.initialize
(rollback-journal store) and the dispatcher's own journal DDL.
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

from unittest import mock

from desk import provider_pacing
from desk.engine import initial_state, transition
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.ledger import Ledger
from desk.monitoring_budget import MonitoringBudget
from desk.paper_cycle_cli import _config
from tests.helpers import ROOT, T, config, event
from tools.ops import verify_cycle as vc
from tools.paper_entry_dispatcher import SCHEMAS as JOURNAL_SCHEMAS

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


class StoreBase(Base):
    def stores(self, *, reservations=2, outcomes=1, used=2, duplicate=False, journal=True):
        """Real schemas: monitoring + ownership in evidence.sqlite, DELETE-journal pacing/journal."""
        store = EvidenceStore(self.data / 'evidence.sqlite')
        # provision() creates the REAL DDL and immutability triggers. Its checkpoint-identity
        # binding needs a quote-mode ledger, which this constant-product fixture is not, so
        # only that binding is stubbed; the schema under test is the production one.
        with mock.patch.object(MonitoringBudget, '_checkpoint', return_value=(None, None)):
            MonitoringBudget(store, self.ledger_path,
                             {'mode': 'paper', 'paper_quote_execution_version': 1}).provision()
        HistoryProgress(store)  # creates the real ownership_budgets DDL
        with contextlib.closing(store.connect()) as c:
            c.execute('BEGIN')  # the store connection autocommits; one fsync, not one per row
            c.executemany('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)',
                          [(i, 100.0 + i, 'scan', 'M', 'ck', 'getAccountInfo', 'same' if duplicate else f'p{i}')
                           for i in range(1, reservations + 1)])
            c.executemany('INSERT INTO paper_monitoring_outcomes VALUES(?,?)',
                          [(i, f'e{i}') for i in range(1, outcomes + 1)])
            c.execute('UPDATE paper_monitoring_budget SET total=?,high_water=100.5', (used,))
            c.execute("INSERT INTO ownership_budgets VALUES('b1','h1',4,18)")
            c.execute('COMMIT')
        provider_pacing.initialize(self.data / 'provider-pacing.sqlite')
        with contextlib.closing(sqlite3.connect(self.data / 'provider-pacing.sqlite')) as c:
            c.execute("UPDATE state SET next_at=10,high_water=9.5 WHERE provider='helius'")
            c.commit()
        if journal:
            (self.data / 'entry-dispatch').mkdir()
            with contextlib.closing(sqlite3.connect(self.data / 'entry-dispatch' / 'j.sqlite')) as c:
                for ddl in JOURNAL_SCHEMAS.values():
                    c.execute(ddl)
                c.execute("INSERT INTO context VALUES(1,'{}','h')")
                c.execute("INSERT INTO intents VALUES('i1','MINT','SIG','{}','hi')")
                c.commit()

    def evidence(self, sql, *args, unguard=False):
        """Direct tamper/progress write; unguard drops the immutability triggers first."""
        with contextlib.closing(sqlite3.connect(self.data / 'evidence.sqlite')) as c:
            if unguard:
                for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                                         "AND name LIKE 'paper_monitoring_%'").fetchall():
                    c.execute(f'DROP TRIGGER {name}')
            c.execute(sql, args)
            c.commit()

class StoresAndReadOnlyTests(StoreBase):
    def test_budget_pacing_ownership_and_journal_sections(self):
        self.apply(event(T))
        self.stores()
        s = self.snap()
        m = s['monitoring']
        self.assertEqual((m['used_total'], m['cap'], m['reserved_pending'], m['high_water']), (2, 60, 1, 100.5))
        # cap 60 / window 3600 s is the real MonitoringBudget.provision() default (the old
        # hand-written fixture assumed a cap of 3600); the tool reports whatever the store holds.
        self.assertEqual(m['window_seconds'], 3600)
        self.assertEqual(m['reservations']['count'], 2)
        self.assertEqual(s['ownership']['used_total'], 4)
        self.assertEqual(dict((p[0], p[1]) for p in s['pacing']['providers'])['helius'], 9.5)
        self.assertEqual(s['dispatcher']['journals']['j.sqlite']['intents']['count'], 1)
        self.assertEqual(s['dispatcher']['journals']['j.sqlite']['results']['count'], 0)
        self.assertEqual(vc.snapshot(self.data, 'paper.sqlite', self.config_path)['monitoring']['used_total'], 2)

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


class CompareTests(StoreBase):
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
        self.evidence("INSERT INTO paper_monitoring_reservations VALUES(3,103,'scan','M','ck','getAccountInfo','p3')")
        self.evidence('UPDATE paper_monitoring_budget SET total=3,high_water=103.5')
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
        self.evidence("UPDATE paper_monitoring_reservations SET params_hash='rewritten' WHERE id=1", unguard=True)
        b = self.fresh_snap()
        self.assertTrue(any('monitoring reservations' in f
                            for f in vc.compare(a, b, allow_progress=True)['failures']))
        self.evidence("UPDATE paper_monitoring_reservations SET params_hash='p1' WHERE id=1", unguard=True)
        self.evidence('UPDATE paper_monitoring_budget SET total=1')
        self.assertIn('monitoring budget regressed', vc.compare(a, self.fresh_snap(), allow_progress=True)['failures'])
        self.evidence('UPDATE ownership_budgets SET used=1')
        self.assertIn('ownership budget regressed or rewritten',
                      vc.compare(a, self.fresh_snap(), allow_progress=True)['failures'])
        with contextlib.closing(sqlite3.connect(self.data / 'provider-pacing.sqlite')) as c:
            c.execute('UPDATE state SET high_water=1')
            c.commit()
        self.assertIn('pacing high-water regressed',
                      vc.compare(a, self.fresh_snap(), allow_progress=True)['failures'])

    def test_duplicate_monitoring_charge_fails_even_when_progress_allowed(self):
        self.apply(event(T))
        self.stores()
        a = self.fresh_snap()
        self.evidence("INSERT INTO paper_monitoring_reservations VALUES(3,103,'scan','M','ck','getAccountInfo','p1')")
        self.evidence('UPDATE paper_monitoring_budget SET total=3')
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
        with contextlib.closing(sqlite3.connect(self.data / 'entry-dispatch' / 'j.sqlite')) as c:
            c.execute("UPDATE intents SET hash='changed'")
            c.commit()
        self.assertTrue(any('dispatcher' in f for f in vc.compare(a, self.fresh_snap(), allow_progress=True)['failures']))


def _uris(fn):
    """URIs the tool passes to sqlite3.connect while running fn."""
    real, seen = sqlite3.connect, []

    def spy(target, *args, **kwargs):
        seen.append(str(target))
        return real(target, *args, **kwargs)
    with mock.patch.object(vc.sqlite3, 'connect', side_effect=spy):
        fn()
    return seen


class OpenModeTests(StoreBase):
    """immutable=1 is only valid for a quiesced WAL database (header bytes 18/19 == 2)."""

    def snapshot_uris(self):
        return _uris(lambda: vc.snapshot(self.data, 'paper.sqlite', self.config_path))

    def test_rollback_journal_stores_are_opened_mode_ro_never_immutable(self):
        self.apply(event(T))
        self.stores()
        self.label()
        for name in ('provider-pacing.sqlite', 'j.sqlite', 'evidence.sqlite'):
            header = (next(self.data.rglob(name))).read_bytes()[:100]
            self.assertEqual((header[18], header[19]), (1, 1), name)  # fixture really is rollback-journal
        uris = self.snapshot_uris()
        for name in ('provider-pacing.sqlite', 'j.sqlite', 'evidence.sqlite'):
            used = [u for u in uris if name in u]
            self.assertTrue(used, name)
            for u in used:
                self.assertIn('mode=ro', u)
                self.assertNotIn('immutable', u)

    def test_quiesced_wal_ledger_is_still_opened_immutable_without_sidecars(self):
        self.apply(event(T))
        self.label()
        self.ledger.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.ledger.close()
        names = {p.name for p in self.data.iterdir()}
        (used,) = [u for u in self.snapshot_uris() if 'paper.sqlite' in u]
        self.assertIn('immutable=1', used)
        self.assertEqual(names, {p.name for p in self.data.iterdir()})

    def test_wal_ledger_with_live_sidecars_is_opened_mode_ro(self):
        self.apply(event(T))
        self.label()
        self.assertTrue((self.data / 'paper.sqlite-wal').stat().st_size > 0)
        (used,) = [u for u in self.snapshot_uris() if 'paper.sqlite' in u]
        self.assertIn('mode=ro', used)
        self.assertNotIn('immutable', used)

    def test_rollback_header_with_stray_empty_wal_name_is_not_immutable(self):
        """A rollback DB must not become immutable just because no sidecar exists."""
        self.apply(event(T))
        self.stores()
        (used,) = [u for u in _uris(lambda: vc._connect(self.data / 'provider-pacing.sqlite', 'p').close())]
        self.assertNotIn('immutable', used)


class LargePrefixTests(StoreBase):
    """--allow-progress must prove the exact old prefix even beyond the 20k rolling bound."""
    OLD = 20_501  # odd: with stride 2 the old final row is not a shared stride mark

    def setUp(self):
        super().setUp()
        self.apply(event(T))
        self.stores(reservations=self.OLD, outcomes=0, used=self.OLD)
        self.a = self.fresh_snap()

    def append(self, n=10):
        for i in range(self.OLD + 1, self.OLD + n + 1):
            self.evidence('INSERT INTO paper_monitoring_reservations VALUES(?,?,?,?,?,?,?)',
                          i, 100.0 + i, 'scan', 'M', 'ck', 'getAccountInfo', f'p{i}')
        self.evidence('UPDATE paper_monitoring_budget SET total=?,high_water=?', self.OLD + n, 100.0 + self.OLD + n)

    def later(self, **kw):
        return copy.deepcopy(vc.snapshot(self.data, 'paper.sqlite', self.config_path, **kw))

    def test_fixture_exceeds_the_rolling_bound(self):
        self.assertGreater(self.a['monitoring']['reservations']['stride'], 1)

    def test_rewriting_the_last_old_row_is_detected(self):
        self.append()
        self.evidence("UPDATE paper_monitoring_reservations SET params_hash='rewritten' WHERE id=?",
                      self.OLD, unguard=True)
        result = vc.compare(self.a, self.later(anchor=self.a), allow_progress=True)
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertTrue(any('monitoring reservations' in f and 'changed' in f for f in result['failures']), result)

    def test_clean_append_passes_with_anchor(self):
        self.append()
        result = vc.compare(self.a, self.later(anchor=self.a), allow_progress=True)
        self.assertEqual(result['status'], 'PASS', result)

    def test_unanchored_later_snapshot_fails_closed_instead_of_weakly_passing(self):
        self.append()
        self.evidence("UPDATE paper_monitoring_reservations SET params_hash='rewritten' WHERE id=?",
                      self.OLD, unguard=True)
        result = vc.compare(self.a, self.later(), allow_progress=True)
        self.assertEqual(result['status'], 'FAIL', result)
        self.assertTrue(any('exact' in f for f in result['failures']), result)

    def test_anchor_must_be_a_snapshot(self):
        with self.assertRaisesRegex(vc.VerifyError, 'anchor'):
            vc.snapshot(self.data, 'paper.sqlite', self.config_path, anchor={'kind': 'other'})


class ConcurrentPositionTests(Base):
    """Known answers from the real engine: buy A, buy B, sell B, sell A (and a TP ladder)."""
    A_COST, A_PROCEEDS = vc.Decimal('0.083383333'), vc.Decimal('0.08027137885694432096051171510')
    B_COST, B_PROCEEDS = vc.Decimal('0.083331467'), vc.Decimal('0.08022146884979907839936384698')

    def interleave(self):
        self.apply(event(T, mint='A'), event(T + 100, mint='A'), event(T + 100, mint='B'),
                   event(T + 110, mint='B', danger=True), event(T + 110, mint='A'),
                   event(T + 120, mint='A', danger=True))

    def ladder(self):
        self.apply(event(T, mint='A'), event(T + 5, mint='A', reserve_sol='140'),
                   event(T + 10, mint='A', reserve_sol='200'), event(T + 15, mint='A', reserve_sol='300'),
                   event(T + 20, mint='A', reserve_sol='400'), event(T + 25, mint='A', danger=True, reserve_sol='400'))

    def near(self, a, b):
        self.assertLess(abs(vc.Decimal(a) - vc.Decimal(b)), vc.Decimal('1e-20'), (a, b))

    def test_buy_a_buy_b_sell_b_sell_a(self):
        self.interleave()
        s = self.snap()
        self.assertEqual(s['status'], 'VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP')
        self.assertEqual([t['mint'] for t in s['round_trips']], ['B', 'A'])  # order of closing
        trips = {t['mint']: t for t in s['round_trips']}
        for mint, cost, proceeds in (('A', self.A_COST, self.A_PROCEEDS), ('B', self.B_COST, self.B_PROCEEDS)):
            self.near(trips[mint]['initial_cost_sol'], cost)
            self.near(trips[mint]['proceeds_sol'], proceeds)
            self.near(trips[mint]['realized_pnl_sol'], proceeds - cost)
            self.assertEqual(len(trips[mint]['sells']), 1)
        a, b = trips['A'], trips['B']
        self.assertLess(a['entry_ts'], b['entry_ts'])               # genuinely concurrent holding
        self.assertLess(b['entry_ts'], b['sells'][0]['ts'])
        self.assertLess(b['sells'][0]['ts'], a['sells'][0]['ts'])
        self.assertNotEqual(a['entry_fill_hash'], b['entry_fill_hash'])
        self.near(s['realized_pnl_sol'], '-0.00622195229325660064012443792')
        self.near(s['cash_sol'], '4.993778047706743399359875562')
        self.near(s['cash_sol'], vc.Decimal(s['initial_equity_sol']) + vc.Decimal(s['realized_pnl_sol']))

    def test_open_leg_stays_open_and_cash_identity_holds_midway(self):
        self.apply(event(T, mint='A'), event(T + 100, mint='A'), event(T + 100, mint='B'),
                   event(T + 110, mint='B', danger=True))
        s = self.snap()
        self.assertEqual(s['status'], 'OPEN_POSITION')
        self.assertEqual([p['mint'] for p in s['open_positions']], ['A'])
        self.assertEqual([p['mint'] for p in s['round_trips']], ['B'])
        self.near(s['cash_sol'], vc.Decimal('5') - self.A_COST + (self.B_PROCEEDS - self.B_COST))

    def test_second_entry_into_an_open_mint_rejected(self):
        self.interleave()
        self.label()
        db = self.ledger.db
        (event_row,) = db.execute("SELECT event_id,ts,payload FROM events WHERE event_id=?", (f'A:{T}',)).fetchall()
        db.execute('INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)',
                   ('A:dup', T + 50, event_row[2], 'x'))
        buy = db.execute("SELECT payload FROM outcomes WHERE json_extract(payload,'$.side')='buy' "
                         "AND event_id=?", (f'A:{T}',)).fetchone()[0]
        db.execute('INSERT INTO outcomes(event_id,payload) VALUES(?,?)', ('A:dup', buy))
        with self.assertRaisesRegex(vc.VerifyError, 'second entry|positions differ'):
            vc.snapshot(self.data, 'paper.sqlite', self.config_path)

    def test_take_profit_ladder_partial_sells_known_answers(self):
        self.ladder()
        s = self.snap()
        self.assertEqual(s['status'], 'VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP')
        (trip,) = s['round_trips']
        pnls = ['0.02315507537451075384445949449', '0.04726511301176613076668924175',
                '0.04757196054095702631185581278', '0.04757196054095702631185581281']
        qtys = ['245.4386484637804162503206175', '245.4386484637804162503206175',
                '163.6257656425202775002137450', '163.6257656425202775002137451']
        self.assertEqual([x['quantity'] for x in trip['sells']], qtys)
        for sell, pnl in zip(trip['sells'], pnls):
            self.near(sell['realized_pnl_sol'], pnl)
        self.near(trip['realized_pnl_sol'], sum(vc.Decimal(x) for x in pnls))
        self.near(s['cash_sol'], vc.Decimal(s['initial_equity_sol']) + vc.Decimal(trip['realized_pnl_sol']))

    def shift_pnl(self, delta):
        """Move PnL between two sells: every aggregate (cash, realized, total basis) is unchanged."""
        rows = self.ledger.db.execute("SELECT seq,payload FROM outcomes WHERE "
                                      "json_extract(payload,'$.reason')='TAKE_PROFIT' ORDER BY seq").fetchall()
        for (seq, payload), sign in zip(rows[:2], (1, -1)):
            v = json.loads(payload)
            v['realized_pnl_sol'] = str(vc.Decimal(v['realized_pnl_sol']) + sign * vc.Decimal(delta))
            self.ledger.db.execute('UPDATE outcomes SET payload=? WHERE seq=?', (json.dumps(v), seq))

    def test_pnl_shifted_between_sells_breaks_per_sell_cost_basis(self):
        self.ladder()
        self.label()
        self.shift_pnl('0.001')
        with self.assertRaisesRegex(vc.VerifyError, 'proportional cost basis'):
            vc.snapshot(self.data, 'paper.sqlite', self.config_path)

    def test_untampered_ladder_passes_the_per_sell_check(self):
        self.ladder()
        self.assertEqual(self.snap()['status'], 'VALIDATED_LIVE_DATA_PAPER_ROUND_TRIP')


class PacingVolatilityTests(StoreBase):
    def pacing(self, sql, *args):
        with contextlib.closing(sqlite3.connect(self.data / 'provider-pacing.sqlite')) as c:
            c.execute(sql, args)
            c.commit()

    def setUp(self):
        super().setUp()
        self.apply(event(T))
        self.stores()
        self.a = self.fresh_snap()

    def test_volatile_fields_are_reported_not_compared(self):
        self.pacing("UPDATE state SET next_at=999,blocked_until=50,pending='ticket' WHERE provider='helius'")
        self.pacing("INSERT INTO waiters VALUES('w1','helius','held',1,2)")
        b = self.fresh_snap()
        result = vc.compare(self.a, b)
        self.assertEqual(result['status'], 'PASS', result)
        self.assertIn('pacing_volatile', result['volatile'])
        self.assertEqual(vc.compare(self.a, b, allow_progress=True)['status'], 'PASS')

    def test_volatile_values_are_kept_out_of_the_strict_section(self):
        keys = {k for row in self.a['pacing']['providers'] for k in ([row] if False else [])}
        self.assertEqual(keys, set())
        for row in self.a['pacing']['providers']:
            self.assertEqual(len(row), 2)  # [provider, high_water] only
        self.assertIn('pacing_volatile', self.a)

    def test_high_water_may_rise_but_never_fall(self):
        self.pacing("UPDATE state SET high_water=20 WHERE provider='helius'")
        self.assertEqual(vc.compare(self.a, self.fresh_snap())['status'], 'PASS')
        up = self.fresh_snap()
        self.pacing("UPDATE state SET high_water=1 WHERE provider='helius'")
        down = self.fresh_snap()
        for allow in (False, True):
            result = vc.compare(up, down, allow_progress=allow)
            self.assertEqual(result['status'], 'FAIL', result)
            self.assertIn('pacing high-water regressed', result['failures'])

    def test_removed_provider_row_fails(self):
        b = self.fresh_snap()
        b['pacing']['providers'] = b['pacing']['providers'][:-1]
        self.assertIn('pacing high-water regressed', vc.compare(self.a, b)['failures'])


class DuplicateFillKeyTests(unittest.TestCase):
    def conn(self):
        c = sqlite3.connect(':memory:')
        c.executescript('CREATE TABLE outcomes(seq INTEGER PRIMARY KEY,event_id TEXT,payload TEXT);'
                        'CREATE TABLE events(event_id TEXT PRIMARY KEY,ts INTEGER,payload TEXT);')
        return c

    def put(self, c, seq, event_id, side, mint, with_event=True):
        if with_event:
            c.execute('INSERT INTO events VALUES(?,?,?)', (event_id, T, json.dumps({'mint': mint})))
        c.execute('INSERT INTO outcomes VALUES(?,?,?)', (seq, event_id, json.dumps(
            {'type': 'fill', 'execution_status': vc.LABEL, 'side': side, 'mint': mint})))

    def test_distinct_fills_whose_concatenated_identity_collides_are_not_duplicates(self):
        c = self.conn()
        # 'E'+'buy'+'sellM' == 'Ebuy'+'sell'+'M': a concatenated key calls these the same fill.
        self.put(c, 1, 'E', 'buy', 'sellM')
        self.put(c, 2, 'Ebuy', 'sell', 'M')
        self.assertEqual(len(vc._fills(c, {'last_ts': T})), 2)

    def test_true_duplicate_identity_still_rejected(self):
        c = self.conn()
        self.put(c, 1, 'E', 'buy', 'M')
        self.put(c, 2, 'E', 'buy', 'M', with_event=False)
        with self.assertRaisesRegex(vc.VerifyError, 'duplicate fill identity'):
            vc._fills(c, {'last_ts': T})


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

    def test_anchor_from_option_records_exact_old_counts(self):
        self.round_trip()
        self.label()
        a, b = str(self.data.parent / 'a.json'), str(self.data.parent / 'b.json')
        base = ['snapshot', '--data', str(self.data), '--ledger', 'paper.sqlite', '--config', str(self.config_path)]
        self.assertEqual(self.run_cli(*base, '--out', a)[0], 0)
        old = json.loads(Path(a).read_text())
        self.apply(event(T + 500, mint='SYNTHETIC_Z'))
        self.label()
        self.assertEqual(self.run_cli(*base, '--out', b, '--anchor-from', a)[0], 0)
        new = json.loads(Path(b).read_text())
        self.assertIn(str(old['tables']['events']['count']), new['tables']['events']['rolling'])
        code, out, _ = self.run_cli('compare', a, b, '--allow-progress')
        self.assertEqual((code, json.loads(out)['status']), (0, 'PASS'))
        self.assertEqual(self.run_cli(*base, '--out', str(self.data.parent / 'c.json'),
                                      '--anchor-from', str(self.data.parent / 'missing.json'))[0], 2)

    def test_corrupt_ledger_is_exit_two_not_a_pass(self):
        self.round_trip()
        self.label()
        self.sql('DELETE FROM state')
        code, _, err = self.run_cli('snapshot', '--data', str(self.data), '--ledger', 'paper.sqlite',
                                    '--config', str(self.config_path), '--out', str(self.data.parent / 'x.json'))
        self.assertEqual(code, 2)
        self.assertIn('CHECKPOINT_MISSING', err)
        self.assertFalse((self.data.parent / 'x.json').exists())


class FreshLayoutTests(StoreBase):
    """Fresh layout: ledger/evidence/journal in the experiment root, shared pacing and discovery outside it."""

    def setUp(self):
        super().setUp()
        from discovery import continuous
        self.round_trip()
        self.label()
        self.stores()
        self.shared = self.data.parent / 'shared'
        (self.shared / 'discovery').mkdir(parents=True)
        self.pacing = self.shared / 'provider-pacing.sqlite'
        os.replace(self.data / 'provider-pacing.sqlite', self.pacing)
        self.discovery = self.shared / 'discovery' / 'continuous.sqlite'
        continuous.initialize(self.discovery)
        with contextlib.closing(sqlite3.connect(self.discovery)) as c:
            c.execute("INSERT INTO reservations VALUES(1, 100.0, 'listen')")
            c.execute("INSERT INTO completions VALUES(1, 101.0, 10, 1, 'RECEIVED', NULL)")
            c.execute("INSERT INTO raw_events VALUES(1, 's1', 102.0, 5, '{}', 'h')")
            c.commit()
        self.external_ledger = self.shared / 'ledger.sqlite'
        with contextlib.closing(sqlite3.connect(self.external_ledger)) as target:
            self.ledger.db.backup(target)

    def fresh(self, **kw):
        kw.setdefault('pacing', str(self.pacing))
        return vc.snapshot(self.data, 'paper.sqlite', self.config_path, **kw)

    def test_default_name_in_a_fresh_root_silently_misses_the_pacing_store(self):
        legacy = vc.snapshot(self.data, 'paper.sqlite', self.config_path)
        self.assertEqual(legacy['pacing'], {'present': False})            # why the explicit option exists

    def test_absolute_pacing_store_is_snapshotted(self):
        s = self.fresh()
        self.assertEqual(s['pacing']['present'], True)
        self.assertEqual(dict(map(tuple, s['pacing']['providers']))['helius'], 9.5)
        self.assertTrue(s['pacing_volatile']['present'])

    def test_explicit_absolute_pacing_must_exist(self):
        with self.assertRaisesRegex(vc.VerifyError, 'named explicitly but not present'):
            self.fresh(pacing=str(self.shared / 'missing.sqlite'))

    def test_bad_absolute_paths_fail_closed(self):
        link = self.shared / 'link.sqlite'
        link.symlink_to(self.pacing)
        linked_dir = self.data.parent / 'shared-link'
        linked_dir.symlink_to(self.shared)
        for label, value in {'symlink': str(link), 'symlinked directory': str(linked_dir / 'provider-pacing.sqlite'),
                             'directory': str(self.shared), 'dotdot': str(self.shared / '..' / 'shared' / 'provider-pacing.sqlite')}.items():
            with self.subTest(label), self.assertRaises(vc.VerifyError):
                self.fresh(pacing=value)

    def test_relative_paths_with_directories_are_still_refused(self):
        for value in ('shared/provider-pacing.sqlite', '../x.sqlite', '.', ''):
            with self.subTest(value), self.assertRaises(vc.VerifyError):
                self.fresh(pacing=value)
            with self.subTest(ledger=value), self.assertRaises(vc.VerifyError):
                vc.snapshot(self.data, value, self.config_path)

    def test_absolute_ledger_path_inside_and_outside_the_root(self):
        by_name = self.fresh()
        by_path = vc.snapshot(self.data, str(self.ledger_path), self.config_path, pacing=str(self.pacing))
        outside = vc.snapshot(self.data, str(self.external_ledger), self.config_path, pacing=str(self.pacing))
        for other in (by_path, outside):
            self.assertEqual(other['checkpoint_digest'], by_name['checkpoint_digest'])
            self.assertEqual((other['status'], other['fill_count']), (by_name['status'], by_name['fill_count']))
        self.assertEqual(outside['ledger'], str(self.external_ledger))
        self.assertEqual(by_name['ledger'], 'paper.sqlite')

    def test_symlinked_or_missing_absolute_ledger_fails_closed(self):
        link = self.shared / 'ledger-link.sqlite'
        link.symlink_to(self.external_ledger)
        for value in (str(link), str(self.shared / 'nope.sqlite')):
            with self.subTest(value), self.assertRaises((vc.VerifyError, OSError)):
                vc.snapshot(self.data, value, self.config_path, pacing=str(self.pacing))

    def test_discovery_store_is_recorded_for_information_only(self):
        s = self.fresh(discovery=str(self.discovery))
        self.assertEqual(s['discovery']['counts'], {'reservations': 1, 'completions': 1, 'raw_events': 1})
        self.assertEqual((s['discovery']['latest_completion_at'], s['discovery']['latest_event_at']), (101.0, 102.0))
        self.assertNotIn('discovery', self.fresh())                          # defaults stay backward compatible
        with contextlib.closing(sqlite3.connect(self.discovery)) as c:       # the feed keeps growing: not strict
            c.execute("INSERT INTO raw_events VALUES(2, 's2', 200.0, 6, '{}', 'h2')")
            c.commit()
        later = self.fresh(discovery=str(self.discovery))
        self.assertEqual(vc.compare(s, later)['status'], 'PASS')
        self.assertEqual(vc.compare(s, self.fresh())['status'], 'PASS')

    def test_explicit_discovery_must_exist_and_be_canonical(self):
        link = self.shared / 'discovery' / 'link.sqlite'
        link.symlink_to(self.discovery)
        for value in (str(self.shared / 'discovery' / 'nope.sqlite'), str(link), 'sub/continuous.sqlite'):
            with self.subTest(value), self.assertRaises(vc.VerifyError):
                self.fresh(discovery=value)

    def test_external_stores_are_opened_mode_ro_and_never_modified(self):
        before = tree_hashes(self.shared)
        uris = _uris(lambda: vc.snapshot(self.data, str(self.external_ledger), self.config_path,
                                         pacing=str(self.pacing), discovery=str(self.discovery)))
        for path in (self.pacing, self.discovery):                 # rollback-journal stores: never immutable
            matching = [u for u in uris if str(path) in u]
            self.assertTrue(matching, path)
            self.assertTrue(all(u.endswith('?mode=ro') for u in matching), (path, matching))
        ledger_uris = [u for u in uris if str(self.external_ledger) in u]
        self.assertEqual(len(ledger_uris), 1)                       # the copy is a quiesced WAL file: immutable is the existing rule
        self.assertTrue(ledger_uris[0].endswith(('?mode=ro', '?immutable=1')))
        self.assertEqual(tree_hashes(self.shared), before)
        self.assertEqual(sorted(p.name for p in self.shared.rglob('*')),
                         sorted(['provider-pacing.sqlite', 'discovery', 'continuous.sqlite', 'ledger.sqlite']))

    def test_cold_restart_compare_across_external_layout(self):
        a = self.fresh(discovery=str(self.discovery))
        b = self.fresh(discovery=str(self.discovery))
        self.assertEqual(vc.compare(a, b)['status'], 'PASS')
        with contextlib.closing(sqlite3.connect(self.pacing)) as c:           # the shared pacing high water may rise
            c.execute("UPDATE state SET high_water=20 WHERE provider='helius'")
            c.commit()
        self.assertEqual(vc.compare(a, self.fresh(discovery=str(self.discovery)))['status'], 'PASS')
        with contextlib.closing(sqlite3.connect(self.pacing)) as c:           # ... and never fall
            c.execute("UPDATE state SET high_water=1 WHERE provider='helius'")
            c.commit()
        self.assertEqual(vc.compare(a, self.fresh(discovery=str(self.discovery)))['status'], 'FAIL')

    def test_cli_options(self):
        out = self.data.parent / 'snap.json'
        argv = ['snapshot', '--data', str(self.data), '--ledger', str(self.external_ledger), '--config', str(self.config_path),
                '--pacing-db', str(self.pacing), '--discovery-db', str(self.discovery), '--out', str(out)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = vc.main(argv)
        self.assertEqual(code, 0, stderr.getvalue())
        doc = json.loads(out.read_text())
        self.assertEqual((doc['ledger'], doc['pacing']['present'], doc['discovery']['present']),
                         (str(self.external_ledger), True, True))
        legacy = self.data.parent / 'legacy.json'
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = vc.main(['snapshot', '--data', str(self.data), '--ledger', 'paper.sqlite', '--config', str(self.config_path),
                            '--pacing', str(self.pacing), '--out', str(legacy)])
        self.assertEqual(code, 0)                                            # the old flag spelling still works
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = vc.main(['snapshot', '--data', str(self.data), '--ledger', 'paper.sqlite', '--config', str(self.config_path),
                            '--pacing-db', str(self.shared / 'nope.sqlite'), '--out', str(self.data.parent / 'bad.json')])
        self.assertEqual(code, 2)
        self.assertIn('named explicitly but not present', err.getvalue())
        self.assertFalse((self.data.parent / 'bad.json').exists())


if __name__ == '__main__':
    unittest.main()
