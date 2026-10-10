"""SYNTHETIC_TEST_ONLY: reviewed pacing-policy upgrade for the paid Helius / Jupiter plans (T36).

Production-shaped database: provider_pacing.initialize at the old 2.0 s cadence, then the real reviewed Kraken
migration (receipt, kraken policy and state rows). Fake clocks and fake units only: no provider, no network, no systemd.
"""
import copy
import hashlib
import io
import json
import os
from contextlib import closing, redirect_stdout
from email.message import Message
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk import kraken_pacing_migration as upgrade, provider_pacing as p
from desk.model import canonical
from tools.ops import pacing_policy as cli

REPO = Path(__file__).resolve().parents[1]
T0 = 1_000_000.0
SHA = '0123456789abcdef' * 4


class Clock:
    def __init__(self, wall=T0):
        self.wall = wall

    def time(self):
        return self.wall

    def sleep(self, delay):
        self.wall += delay


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / 'provider-pacing.sqlite'
        p.initialize(self.path)                                   # the production policy: helius/jupiter 2.0 s, backoff 30 s
        pins = self.root / 'kraken-pins.json'
        patcher = patch.object(upgrade, 'POLICY', pins)
        patcher.start()
        self.addCleanup(patcher.stop)
        pins.write_text(canonical({'version': 1, 'pins': []}))
        proposal = upgrade.review_plan(self.path)
        pins.write_text(canonical({'version': 1, 'pins': [proposal]}))
        upgrade.migrate(self.path)                                # the reviewed Kraken lane and its receipt
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE state SET next_at=?,high_water=? WHERE provider='helius'", (T0 - 500.5, T0 - 500.75))
            c.execute("UPDATE state SET next_at=?,high_water=? WHERE provider='jupiter'", (T0 - 400.25, T0 - 400.5))
            c.commit()
        self.policy_file = self.root / 'provider-pacing-policy.json'
        self.policy_file.write_bytes((REPO / 'config' / 'provider-pacing-policy.json').read_bytes())
        patcher = patch.object(p, 'POLICY_FILE', self.policy_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clock = Clock()

    # --- helpers
    def make(self, clock=None):
        clock = clock or self.clock
        return p.Pacer(self.path, clock=clock.time, monotonic=clock.time, sleep=clock.sleep)

    def apply(self, policy_id=None):
        return p.apply_policy(self.path, policy_id, clock=self.clock.time)

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as c:
            return c.execute(sql, args).fetchall()

    def kraken_lane(self):
        """Everything that belongs to the Kraken lane, as stored."""
        return {
            'policy': self.rows("SELECT * FROM policy WHERE provider='kraken'"),
            'state': self.rows("SELECT * FROM state WHERE provider='kraken'"),
            'receipt': self.rows('SELECT * FROM kraken_pacing_migration'),
            'schema': self.rows("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name LIKE 'kraken_pacing_%' ORDER BY name"),
        }

    def original_policy(self):
        return self.rows('SELECT * FROM policy ORDER BY provider')

    def gap(self, provider):
        pacer = self.make()
        first = pacer.acquire(provider, timeout_seconds=5)
        at_first = self.clock.wall
        pacer.finish(provider, first)
        second = pacer.acquire(provider, timeout_seconds=5)
        at_second = self.clock.wall
        pacer.finish(provider, second)
        return at_second - at_first

    def forge(self, *row):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('INSERT INTO pacing_policy_changes VALUES(?,?,?,?,?,?,?,?,?,?)', row)
            c.commit()

    def invalid(self):
        with self.assertRaisesRegex(p.PacingError, 'PACING_DATABASE_INVALID'):
            p.Pacer(self.path)


class PlanTests(Base):
    def test_plan_lists_exactly_the_reviewed_changes_and_writes_nothing(self):
        before = file_digest(self.path)
        plan = p.policy_plan(self.path)
        self.assertEqual(file_digest(self.path), before)
        self.assertEqual(plan['status'], 'PLAN')
        self.assertEqual(plan['policy_id'], '2026-10-11.paid-developer-plans.1')
        self.assertEqual(plan['changes'], [
            {'provider': 'helius', 'old_cadence': 2.0, 'new_cadence': 0.1, 'old_backoff': 30.0, 'new_backoff': 30.0},
            {'provider': 'jupiter', 'old_cadence': 2.0, 'new_cadence': 0.25, 'old_backoff': 30.0, 'new_backoff': 30.0}])
        self.assertEqual((plan['unchanged'], plan['kraken'], plan['table_present'], plan['quiescent_database']),
                         ([], 'UNTOUCHED', False, True))
        self.assertEqual(plan['config_sha256'], file_digest(self.policy_file))

    def test_the_shipped_policy_file_matches_the_decision(self):
        shipped = json.loads((REPO / 'config' / 'provider-pacing-policy.json').read_text())
        (entry,) = shipped['policies']
        self.assertEqual(entry['providers'], {'helius': {'cadence': 0.1, 'backoff': 30.0},
                                              'jupiter': {'cadence': 0.25, 'backoff': 30.0}})
        self.assertNotIn('kraken', entry['providers'])
        self.assertAlmostEqual(1 / 0.1, 10)                      # 10 req/s = 20% of the documented 50
        self.assertAlmostEqual(1 / 0.25, 4)                      # 4 req/s = 40% of the documented 10

    def test_unknown_policy_id_and_a_malformed_policy_file_refuse(self):
        with self.assertRaisesRegex(p.PacingError, 'PACING_POLICY_UNKNOWN'):
            p.policy_plan(self.path, 'nope')
        for label, text in {'duplicate key': '{"version":1,"version":1,"policies":[]}', 'empty': '{"version":1,"policies":[]}',
                            'kraken entry': json.dumps({'version': 1, 'policies': [{'id': 'x', 'reason': 'r', 'providers': {
                                'kraken': {'cadence': 0.1, 'backoff': 30.0}}}]}),
                            'too fast': json.dumps({'version': 1, 'policies': [{'id': 'x', 'reason': 'r', 'providers': {
                                'helius': {'cadence': 0.01, 'backoff': 30.0}}}]}),
                            'bool cadence': json.dumps({'version': 1, 'policies': [{'id': 'x', 'reason': 'r', 'providers': {
                                'helius': {'cadence': True, 'backoff': 30.0}}}]}),
                            'extra key': json.dumps({'version': 1, 'policies': [], 'x': 1}), 'not json': 'garbage'}.items():
            with self.subTest(label):
                self.policy_file.write_text(text)
                with self.assertRaises(ValueError):
                    p._reviewed()


class ApplyTests(Base):
    def test_apply_records_append_only_rows_and_never_edits_the_original_policy(self):
        original = self.original_policy()
        result = self.apply()
        self.assertEqual(result['status'], 'APPLIED')
        self.assertEqual(self.original_policy(), original)                 # the receipt-pinned rows are untouched
        rows = self.rows('SELECT id,provider,old_cadence,new_cadence,old_backoff,new_backoff,reason,config_sha256,policy_digest,applied_at '
                         'FROM pacing_policy_changes ORDER BY id')
        self.assertEqual([r[:6] for r in rows], [(1, 'helius', 2.0, 0.1, 30.0, 30.0), (2, 'jupiter', 2.0, 0.25, 30.0, 30.0)])
        entry = json.loads(self.policy_file.read_text())['policies'][0]
        self.assertTrue(all(r[6] == entry['reason'] and r[7] == file_digest(self.policy_file) and r[9] == T0 for r in rows))
        self.assertEqual(len({r[8] for r in rows}), 1)
        p.Pacer(self.path)                                                  # still validates

    def test_kraken_lane_is_byte_identical_and_still_pinned(self):
        before = self.kraken_lane()
        self.assertTrue(before['receipt'])
        self.apply()
        self.assertEqual(self.kraken_lane(), before)
        self.assertEqual(self.rows("SELECT version,cadence,backoff FROM policy WHERE provider='kraken'"), [(2, 2.0, 30.0)])
        self.assertEqual(self.make().providers, ('helius', 'jupiter', 'kraken'))
        self.assertAlmostEqual(self.gap('kraken'), 2.0, delta=0.1)          # Kraken pacing stays exactly 2.0 s

    def test_the_kraken_pin_file_is_not_touched(self):
        shipped = REPO / 'config' / 'kraken-pacing-migration.json'
        before = file_digest(shipped)
        self.apply()
        self.assertEqual(file_digest(shipped), before)

    def test_new_cadences_take_effect(self):
        self.assertAlmostEqual(self.gap('helius'), 2.0, delta=0.1)          # before: the old 0.5 req/s
        self.assertAlmostEqual(self.gap('jupiter'), 2.0, delta=0.1)
        self.apply()
        self.assertAlmostEqual(self.gap('helius'), 0.1, delta=0.1)
        self.assertAlmostEqual(self.gap('jupiter'), 0.25, delta=0.1)
        self.assertGreaterEqual(self.gap('helius'), 0.1)                    # never faster than the reviewed cadence
        self.assertGreaterEqual(self.gap('jupiter'), 0.25)

    def test_a_429_still_triggers_the_30_second_backoff_and_retry_after_is_honoured(self):
        self.apply()
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=5)
        now = self.clock.wall
        pacer.throttle('helius', Message(), ticket=ticket)                 # no Retry-After: fixed backoff
        self.assertEqual(self.rows("SELECT blocked_until FROM state WHERE provider='helius'")[0][0], now + 30.0)
        self.clock.wall = now + 31
        ticket = pacer.acquire('helius', timeout_seconds=5)
        now = self.clock.wall
        headers = Message()
        headers['Retry-After'] = '120'
        pacer.throttle('helius', headers, ticket=ticket)
        self.assertEqual(self.rows("SELECT blocked_until FROM state WHERE provider='helius'")[0][0], now + 120)
        with self.assertRaisesRegex(p.PacingError, 'DEADLINE_EXCEEDED'):
            pacer.acquire('helius', timeout_seconds=1)
        self.assertEqual(self.rows("SELECT backoff FROM policy WHERE provider='jupiter'"), [(30.0,)])

    def test_reapply_is_a_noop(self):
        self.apply()
        before = file_digest(self.path)
        again = self.apply()
        self.assertEqual((again['status'], again['changes']), ('ALREADY_APPLIED', []))
        self.assertEqual(file_digest(self.path), before)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM pacing_policy_changes'), [(2,)])

    def test_partial_state_records_only_the_difference(self):
        other = self.root / 'other.sqlite'
        p.initialize(other, jupiter_seconds=0.25)
        result = p.apply_policy(other, clock=self.clock.time)
        self.assertEqual([c['provider'] for c in result['changes']], ['helius'])
        self.assertEqual([u['provider'] for u in result['unchanged']], ['jupiter'])

    def test_nothing_to_do_does_not_even_add_the_table(self):
        other = self.root / 'other.sqlite'
        p.initialize(other, helius_seconds=0.1, jupiter_seconds=0.25)
        before = file_digest(other)
        self.assertEqual(p.apply_policy(other, clock=self.clock.time)['status'], 'NOTHING_TO_DO')
        self.assertEqual(file_digest(other), before)

    def test_apply_refuses_unless_the_database_is_quiescent_and_changes_nothing(self):
        before = file_digest(self.path)
        pacer = self.make()
        ticket = pacer.acquire('helius', timeout_seconds=5)               # a grant is pending
        pending_digest = file_digest(self.path)
        with self.assertRaisesRegex(p.PacingError, 'PACING_NOT_QUIESCENT'):
            self.apply()
        self.assertEqual(file_digest(self.path), pending_digest)
        pacer.finish('helius', ticket)
        with closing(sqlite3.connect(self.path)) as c:                     # a live waiter
            c.execute("INSERT INTO waiters VALUES(?,?,?,?,?)", ('a' * 32, 'helius', 'held', self.clock.wall, self.clock.wall + 5))
            c.commit()
        with self.assertRaisesRegex(p.PacingError, 'PACING_NOT_QUIESCENT'):
            self.apply()
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='pacing_policy_changes'"), [])
        with closing(sqlite3.connect(self.path)) as c:                     # an expired waiter is not live
            c.execute('UPDATE waiters SET created=?,expires=?', (self.clock.wall - 6, self.clock.wall - 1))
            c.commit()
        self.assertEqual(self.apply()['status'], 'APPLIED')
        self.assertNotEqual(before, file_digest(self.path))

    def test_clock_behind_the_recorded_high_water_refuses(self):
        with self.assertRaisesRegex(p.PacingError, 'PACING_CLOCK_INVALID'):
            p.apply_policy(self.path, clock=lambda: T0 - 10_000)
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='pacing_policy_changes'"), [])

    def test_a_newer_reviewed_entry_chains_from_the_previous_effective_value(self):
        self.apply()
        doc = json.loads(self.policy_file.read_text())
        doc['policies'].append({'id': 'second', 'reason': 'Second review', 'providers': {'helius': {'cadence': 0.2, 'backoff': 60.0}}})
        self.policy_file.write_text(json.dumps(doc))
        result = self.apply()
        self.assertEqual([(c['provider'], c['old_cadence'], c['new_cadence'], c['old_backoff'], c['new_backoff']) for c in result['changes']],
                         [('helius', 0.1, 0.2, 30.0, 60.0)])
        self.assertEqual(self.rows('SELECT id FROM pacing_policy_changes ORDER BY id'), [(1,), (2,), (3,)])
        p.Pacer(self.path)
        self.assertAlmostEqual(self.gap('helius'), 0.2, delta=0.1)
        self.assertAlmostEqual(self.gap('jupiter'), 0.25, delta=0.1)       # untouched by the second entry
        pacer = self.make()                                                  # the newest backoff (60 s) is the one a 429 uses
        ticket = pacer.acquire('helius', timeout_seconds=5)
        now = self.clock.wall
        pacer.throttle('helius', Message(), ticket=ticket)
        self.assertEqual(self.rows("SELECT blocked_until FROM state WHERE provider='helius'")[0][0], now + 60.0)
        # removing the first entry from the file orphans its rows: history is append-only in the file too
        first = copy.deepcopy(doc)
        del first['policies'][0]
        self.policy_file.write_text(json.dumps(first))
        self.invalid()

    def test_effective_values_per_provider(self):
        self.apply()
        with closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(p._effective(c, 'helius'), (0.1, 30.0))
            self.assertEqual(p._effective(c, 'jupiter'), (0.25, 30.0))
            self.assertEqual(p._effective(c, 'kraken'), (2.0, 30.0))

    def test_reclaim_arithmetic_uses_the_effective_cadence(self):
        """An orphaned grant is dated from next_at - cadence: the cadence in force, not the original policy row."""
        self.apply()
        self.assertEqual(p.upgrade(self.path), 'UPGRADED')                  # the reclaim table (T23F); orthogonal to the policy table
        ticket = 'a' * 32
        granted = T0 - 100.0
        with closing(sqlite3.connect(self.path)) as c:
            c.execute("UPDATE state SET next_at=?,pending=?,high_water=? WHERE provider='helius'", (granted + 0.1, ticket, granted))
            c.commit()
        self.assertEqual(self.make().reclaim_orphans(), ['helius'])         # no process holds the lock: the owner is gone
        (row,) = self.rows('SELECT provider,granted_at FROM pacing_reclaims')
        self.assertEqual((row[0], row[1]), ('helius', granted))

    def test_the_chain_is_validated_before_the_commit_and_a_failure_rolls_back(self):
        before = file_digest(self.path)
        real = p._reviewed
        calls = []

        def flaky():
            calls.append(1)
            return real() if len(calls) == 1 else ({}, 'f' * 64)           # the in-transaction validation sees no reviewed entry
        with patch.object(p, '_reviewed', side_effect=flaky):
            with self.assertRaisesRegex(p.PacingError, 'PACING_POLICY_INVALID'):
                self.apply()
        self.assertEqual(file_digest(self.path), before)
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='pacing_policy_changes'"), [])


class ForgedChangeTests(Base):
    """INSERT is the only write the guards allow; every inserted row must still be a reviewed link in the chain."""

    def setUp(self):
        super().setUp()
        self.apply()
        self.entry_digest = self.rows('SELECT policy_digest FROM pacing_policy_changes WHERE id=1')[0][0]
        self.reason = self.rows('SELECT reason FROM pacing_policy_changes WHERE id=1')[0][0]

    def test_a_row_that_no_reviewed_entry_backs_is_rejected(self):
        self.forge(3, 'jupiter', 0.25, 0.05, 30.0, 30.0, self.reason, SHA, '0' * 64, T0)
        self.invalid()

    def test_valid_digest_with_altered_values_is_rejected(self):
        self.forge(3, 'jupiter', 0.25, 0.05, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0)
        self.invalid()

    def test_chain_break_wrong_old_values_is_rejected(self):
        self.forge(3, 'jupiter', 2.0, 0.25, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0)
        self.invalid()

    def test_altered_reason_bad_hash_shape_and_time_going_backwards_are_rejected(self):
        for label, row in {'reason': (3, 'jupiter', 0.25, 0.25, 30.0, 30.0, 'because', SHA, self.entry_digest, T0),
                           'sha': (3, 'jupiter', 0.25, 0.25, 30.0, 30.0, self.reason, 'XYZ', self.entry_digest, T0),
                           'time': (3, 'jupiter', 0.25, 0.25, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0 - 1),
                           'nan': (3, 'jupiter', 0.25, 0.25, 30.0, 30.0, self.reason, SHA, self.entry_digest, float('inf'))}.items():
            with self.subTest(label):
                self.forge(*row)
                self.invalid()
                with closing(sqlite3.connect(self.path)) as c:
                    c.execute('DROP TRIGGER pacing_policy_changes_no_delete')
                    c.execute('DELETE FROM pacing_policy_changes WHERE id=3')
                    c.execute(p.POLICY_GUARDS['pacing_policy_changes_no_delete'])
                    c.commit()
                p.Pacer(self.path)

    def test_an_id_gap_is_detected_even_if_the_order_guard_was_dropped_and_recreated(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DROP TRIGGER pacing_policy_changes_order')
            c.execute('INSERT INTO pacing_policy_changes VALUES(7,?,?,?,?,?,?,?,?,?)',
                      ('jupiter', 0.25, 0.25, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0))
            c.execute(p.POLICY_GUARDS['pacing_policy_changes_order'])         # schema looks untouched again
            c.commit()
        self.invalid()

    def test_the_number_of_change_rows_is_bounded(self):
        with patch.object(p, 'MAX_POLICY_CHANGES', 1):
            self.invalid()                                                     # two rows exist

    def test_text_in_a_numeric_column_is_rejected(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DROP TRIGGER pacing_policy_changes_no_update')
            c.execute("UPDATE pacing_policy_changes SET new_cadence='fast' WHERE id=2")
            c.execute(p.POLICY_GUARDS['pacing_policy_changes_no_update'])
            c.commit()
        self.invalid()

    def test_kraken_can_never_be_given_a_change_row(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.forge(3, 'kraken', 2.0, 0.1, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0)

    def test_rows_out_of_order_cannot_be_inserted(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.forge(7, 'jupiter', 0.25, 0.25, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0)
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(sqlite3.connect(self.path)) as c:
                c.execute('INSERT INTO pacing_policy_changes(provider,old_cadence,new_cadence,old_backoff,new_backoff,reason,'
                          'config_sha256,policy_digest,applied_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          ('jupiter', 0.25, 0.25, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0))

    def test_rows_cannot_be_updated_or_deleted(self):
        for sql in ('UPDATE pacing_policy_changes SET new_cadence=0.05', 'DELETE FROM pacing_policy_changes'):
            with self.subTest(sql), self.assertRaises(sqlite3.DatabaseError):
                with closing(sqlite3.connect(self.path)) as c:
                    c.execute(sql)

    def test_removing_a_guard_or_changing_the_schema_or_a_stored_value_is_detected(self):
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('DROP TRIGGER pacing_policy_changes_no_update')
            c.execute('UPDATE pacing_policy_changes SET new_cadence=0.05 WHERE id=1')
            c.commit()
        self.invalid()
        with closing(sqlite3.connect(self.path)) as c:
            c.execute('UPDATE pacing_policy_changes SET new_cadence=0.1 WHERE id=1')
            c.commit()
        self.invalid()                                                     # the guard is still missing
        with closing(sqlite3.connect(self.path)) as c:
            c.execute(p.POLICY_GUARDS['pacing_policy_changes_no_update'])
            c.commit()
        p.Pacer(self.path)

    def test_editing_the_reviewed_file_after_apply_invalidates_the_rows(self):
        doc = json.loads(self.policy_file.read_text())
        doc['policies'][0]['providers']['helius']['cadence'] = 0.05
        self.policy_file.write_text(json.dumps(doc))
        self.invalid()

    def test_a_missing_or_corrupt_reviewed_file_fails_closed(self):
        self.policy_file.write_text('not json')
        self.invalid()
        self.policy_file.unlink()
        self.invalid()

    def test_first_row_must_start_from_the_original_policy(self):
        other = self.root / 'fresh.sqlite'
        p.initialize(other)
        with closing(sqlite3.connect(other)) as c:
            c.execute(p.POLICY_SQL)
            for sql in p.POLICY_GUARDS.values():
                c.execute(sql)
            c.execute('INSERT INTO pacing_policy_changes VALUES(1,?,?,?,?,?,?,?,?,?)',
                      ('helius', 0.5, 0.1, 30.0, 30.0, self.reason, SHA, self.entry_digest, T0))   # claims an old value of 0.5
            c.commit()
        with self.assertRaisesRegex(p.PacingError, 'PACING_DATABASE_INVALID'):
            p.Pacer(other)

    def test_the_table_cannot_be_added_with_a_different_schema(self):
        other = self.root / 'fresh2.sqlite'
        p.initialize(other)
        with closing(sqlite3.connect(other)) as c:
            c.execute('CREATE TABLE pacing_policy_changes(id INTEGER PRIMARY KEY, anything TEXT)')
            c.commit()
        with self.assertRaisesRegex(p.PacingError, 'PACING_DATABASE_INVALID'):
            p.Pacer(other)


class MixedVersionTests(Base):
    def test_code_that_does_not_know_the_table_fails_closed(self):
        """Old code's table rule is `tables - {reclaim tables} == base set`; the extra table breaks it. Emulated by renaming
        the constant the validator uses, which is exactly what an old release lacks."""
        self.apply()
        p.Pacer(self.path)
        with patch.object(p, 'POLICY_TABLE', 'unknown_to_this_release'):
            with self.assertRaisesRegex(p.PacingError, 'PACING_DATABASE_INVALID'):
                p.Pacer(self.path)

    def test_before_apply_old_and_new_code_both_accept_the_database(self):
        p.Pacer(self.path)
        with patch.object(p, 'POLICY_TABLE', 'unknown_to_this_release'):
            p.Pacer(self.path)

    def test_the_gates_see_the_upgraded_database_through_configured(self):
        self.apply()
        with patch.dict(os.environ, {p.ENV: str(self.path)}):
            self.assertEqual(p.configured().providers, ('helius', 'jupiter', 'kraken'))


class CliTests(Base):
    UNITS = {u: 'inactive' for u in cli.DEFAULT_WRITERS}

    def run_cli(self, *argv, states=None):
        states = dict(self.UNITS if states is None else states)

        def runner(unit):
            value = states.get(unit, 'inactive')
            if isinstance(value, Exception):
                raise value
            return value
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv), runner)
        return code, json.loads(out.getvalue())

    def test_plan_and_dry_run_change_nothing_and_report_readiness(self):
        before = file_digest(self.path)
        code, plan = self.run_cli('plan', '--db', str(self.path))
        self.assertEqual((code, plan['status'], plan['ready_to_apply'], plan['all_units_quiet']), (0, 'PLAN', True, True))
        self.assertEqual(set(plan['units']), set(cli.DEFAULT_WRITERS))
        code, dry = self.run_cli('apply', '--db', str(self.path), '--policy', str(self.policy_file))
        self.assertEqual((code, dry['status']), (0, 'DRY_RUN'))
        self.assertIn('--execute', dry['note'])
        self.assertEqual(file_digest(self.path), before)

    def test_execute_applies_when_every_writer_is_quiet_and_is_idempotent(self):
        code, result = self.run_cli('apply', '--db', str(self.path), '--execute')
        self.assertEqual((code, result['status'], len(result['changes'])), (0, 'APPLIED', 2))
        after = file_digest(self.path)
        code, again = self.run_cli('apply', '--db', str(self.path), '--execute')
        self.assertEqual((code, again['status']), (0, 'ALREADY_APPLIED'))
        self.assertEqual(file_digest(self.path), after)
        code, plan = self.run_cli('plan', '--db', str(self.path))
        self.assertEqual((plan['changes'], plan['ready_to_apply']), ([], False))

    def test_a_running_writer_blocks_execute_but_not_plan(self):
        for unit in ('desk-paper-entry-dispatcher.timer', 'desk-paper-held-cycle.path', 'desk-dashboard.service', 'desk-counterfactual.service'):
            with self.subTest(unit):
                before = file_digest(self.path)
                states = {**self.UNITS, unit: 'active'}
                code, refused = self.run_cli('apply', '--db', str(self.path), '--execute', states=states)
                self.assertEqual(code, 2, refused)
                self.assertIn('not quiesced', refused['reason'])
                self.assertIn(unit, refused['reason'])
                self.assertEqual(file_digest(self.path), before)
                code, plan = self.run_cli('plan', '--db', str(self.path), states=states)
                self.assertEqual((code, plan['all_units_quiet'], plan['ready_to_apply']), (0, False, False))

    def test_unknown_unit_state_is_not_quiet(self):
        states = {**self.UNITS, 'desk-dashboard.service': OSError('no systemctl')}
        code, plan = self.run_cli('plan', '--db', str(self.path), states=states)
        self.assertTrue(plan['units']['desk-dashboard.service'].startswith('UNKNOWN:'))
        self.assertEqual((plan['all_units_quiet'], plan['ready_to_apply']), (False, False))
        code, refused = self.run_cli('apply', '--db', str(self.path), '--execute', states=states)
        self.assertEqual(code, 2)
        self.assertEqual(self.rows("SELECT name FROM sqlite_master WHERE name='pacing_policy_changes'"), [])

    def test_extra_required_units_are_added_to_the_list(self):
        code, refused = self.run_cli('apply', '--db', str(self.path), '--execute', '--require-quiesced', 'desk-extra.service',
                                     states={**self.UNITS, 'desk-extra.service': 'activating'})
        self.assertEqual(code, 2)
        self.assertIn('desk-extra.service', refused['reason'])

    def test_bad_arguments_refuse(self):
        other = self.root / 'other-policy.json'
        other.write_bytes(self.policy_file.read_bytes())
        for label, argv in {'relative db': ['plan', '--db', 'provider-pacing.sqlite'],
                            'other policy file': ['apply', '--db', str(self.path), '--policy', str(other)],
                            'unknown id': ['plan', '--db', str(self.path), '--policy-id', 'nope'],
                            'dotdot db': ['plan', '--db', str(self.root / 'x' / '..' / 'provider-pacing.sqlite')],
                            'missing db': ['plan', '--db', str(self.root / 'missing.sqlite')]}.items():
            with self.subTest(label):
                code, refused = self.run_cli(*argv)
                self.assertEqual((code, refused['status']), (2, 'REFUSED'))
        with self.subTest('non-canonical db'):
            link = self.root / 'link.sqlite'
            link.symlink_to(self.path)
            self.assertEqual(self.run_cli('plan', '--db', str(link))[0], 2)

    def test_db_path_validation_unit(self):
        link = self.root / 'l.sqlite'
        link.symlink_to(self.path)
        (self.root / 'sub').mkdir()
        for bad in ('x.sqlite', str(link), str(self.root / 'sub' / '..' / 'provider-pacing.sqlite')):
            with self.subTest(bad), self.assertRaises(cli.PolicyToolError):
                cli._db(bad)
        self.assertEqual(cli._db(str(self.path)), self.path)

    def test_database_not_quiescent_blocks_execute(self):
        pacer = self.make()
        pacer.acquire('jupiter', timeout_seconds=5)
        code, refused = self.run_cli('apply', '--db', str(self.path), '--execute')
        self.assertEqual((code, refused['reason']), (2, 'PACING_NOT_QUIESCENT'))
        code, plan = self.run_cli('plan', '--db', str(self.path))
        self.assertEqual((plan['quiescent_database'], plan['ready_to_apply'], plan['pending_providers']), (False, False, ['jupiter']))


if __name__ == '__main__':
    unittest.main()
