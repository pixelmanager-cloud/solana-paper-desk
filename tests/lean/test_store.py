"""SYNTHETIC_TEST_ONLY: the lean store. Schema, append-only guards, derived cash/positions, invariants, tamper detection and seeded
property tests over random fill sequences. Fixtures only; no network."""
import os
import random
import sqlite3
import tempfile
import threading
from decimal import Decimal
import unittest
from pathlib import Path

from lean import paper, store as store_module
from lean.paper import AccountingHalt, Fill, PaperConfig, Quote, buy, sell
from lean.store import Store, StoreError, redact

CODE, STRATEGY = 'abc1234', 'default-v1'
CFG = PaperConfig()
MINTS = ['A' * 32, 'B' * 32, 'C' * 32]
START = 5 * paper.LAMPORTS


def bq(mint, lamports, out, ts):
    return Quote(mint, 'buy', lamports, out, 6, ts, ref='r')


def sq(mint, qty, out, ts):
    return Quote(mint, 'sell', qty, out, 6, ts, ref='r')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(os.path.realpath(self.tmp.name)) / 'lean.sqlite'
        self.store = Store(self.path, initial_cash_sol=5, code_version=CODE, strategy_version=STRATEGY, clock=lambda: 1000.0)
        self.addCleanup(self.store.close)

    def fill_buy(self, mint=MINTS[0], sol='0.02', out=1_000_000_000, ts=1.0):
        fill = buy(bq(mint, paper.to_lamports(sol), out, ts), sol, CFG)
        return fill, self.store.add_fill(fill)

    def raw_db(self):
        return sqlite3.connect(self.path)

    def lift_guards(self, db):
        for name in store_module.GUARDS:
            db.execute(f'DROP TRIGGER IF EXISTS {name}')


class SchemaTests(Base):
    def test_new_store_shape(self):
        self.assertEqual(self.store.initial_cash, START)
        self.assertEqual(self.store.cash(), START)
        self.assertEqual((self.store.positions(), self.store.realized()), ({}, 0))
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), '0o600')
        self.assertEqual(self.raw_db().execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertTrue(self.store.check_invariants())

    def test_new_store_needs_initial_cash_and_versions(self):
        with self.assertRaises(StoreError):
            Store(Path(self.tmp.name) / 'x.sqlite')
        bare = Store(Path(self.tmp.name) / 'y.sqlite', initial_cash_sol=1)
        self.addCleanup(bare.close)
        with self.assertRaises(StoreError):
            bare.add_error('X', transient=True)                                  # no version anywhere
        with self.assertRaises(StoreError):
            bare.record('k', {}, code_version='', strategy_version='s')
        self.assertEqual(bare.record('k', {'a': 1}, code_version='c', strategy_version='s'), 1)

    def test_reopen_keeps_state_and_refuses_a_different_initial_cash(self):
        self.fill_buy()
        cash, positions = self.store.cash(), self.store.positions()
        self.store.close()
        again = Store(self.path, code_version=CODE, strategy_version=STRATEGY)
        self.addCleanup(again.close)
        self.assertEqual((again.cash(), again.positions()), (cash, positions))
        again.close()
        with self.assertRaises(StoreError):
            Store(self.path, initial_cash_sol=6)
        self.store = Store(self.path, initial_cash_sol=5, code_version=CODE, strategy_version=STRATEGY)

    def test_schema_drift_and_hostile_files_are_refused(self):
        self.store.close()
        db = self.raw_db()
        db.execute('ALTER TABLE errors ADD COLUMN extra TEXT')
        db.commit()
        db.close()
        with self.assertRaisesRegex(StoreError, 'schema'):
            Store(self.path)
        self.store = Store(Path(self.tmp.name) / 'fresh.sqlite', initial_cash_sol=1)       # keep tearDown happy
        link = Path(self.tmp.name) / 'link.sqlite'
        link.symlink_to(Path(self.tmp.name) / 'fresh.sqlite')                            # a CLEAN store behind the link
        with self.assertRaisesRegex(StoreError, 'regular file'):
            Store(link)
        directory = Path(self.tmp.name) / 'adir'
        directory.mkdir()
        with self.assertRaisesRegex(StoreError, 'regular file'):
            Store(directory)
        with self.assertRaises(StoreError):
            Store(Path(self.tmp.name) / 'nodir' / 'x.sqlite', initial_cash_sol=1)

    def test_every_table_is_append_only(self):
        self.fill_buy()
        self.store.add_candidate('A' * 32, code_version=CODE, strategy_version=STRATEGY)
        self.store.add_observation('quote', b'raw')
        self.store.add_decision('entry', 'SKIP', mint='A' * 32)
        self.store.add_error('E', transient=False)
        self.store.record('note', {'x': 1}, code_version=CODE, strategy_version=STRATEGY)
        db = self.raw_db()
        for table in ('meta', 'events', 'candidates', 'observations', 'decisions', 'fills', 'errors'):
            for statement in (f'UPDATE {table} SET rowid=rowid', f'DELETE FROM {table}'):
                with self.subTest(statement=statement), self.assertRaisesRegex(sqlite3.IntegrityError, 'append-only'):
                    db.execute(statement)


class TypedHelperTests(Base):
    def test_candidate_is_idempotent_per_mint(self):
        a = self.store.add_candidate('Z' * 32, pool='P', signature='S', slot=5, migrated_at=9.5, hint_seq=3, meta={'k': 1})
        self.assertEqual(self.store.add_candidate('Z' * 32, pool='OTHER'), a)
        self.assertEqual(self.store.candidate_id('Z' * 32), a)
        self.assertIsNone(self.store.candidate_id('nope'))
        (row,) = self.store.rows('candidates')
        self.assertEqual((row['pool'], row['slot'], row['code_version'], row['strategy_version'], row['ts']), ('P', 5, CODE, STRATEGY, 1000.0))

    def test_observation_keeps_exact_bytes_and_verifies_them(self):
        raw = bytes(range(256)) * 4
        oid = self.store.add_observation('jupiter_quote', raw, mint='A' * 32, meta={'status': 200})
        got = self.store.observation(oid)
        self.assertEqual((got['raw'], got['kind'], got['mint'], got['meta']), (raw, 'jupiter_quote', 'A' * 32, {'status': 200}))
        self.assertIsNone(self.store.observation(999))
        db = self.raw_db()
        self.lift_guards(db)
        db.execute("UPDATE observations SET raw=x'00'")
        db.commit()
        with self.assertRaisesRegex(StoreError, 'hash'):
            self.store.observation(oid)

    def test_observation_bounds_and_types(self):
        for bad in ('text', None, b'x' * (store_module.MAX_RAW_BYTES + 1)):
            with self.assertRaises(StoreError):
                self.store.add_observation('k', bad)
        self.store.add_observation('k', b'x' * store_module.MAX_RAW_BYTES)

    def test_errors_and_metadata_never_keep_a_credential(self):
        self.store.add_error('HTTP_500', transient=True, mint='A' * 32,
                             message='GET https://rpc.example/?api-key=SUPERSECRET&x=1 failed; Authorization: Bearer TOP.SECRET.TOKEN')
        oid = self.store.add_observation('rpc', b'{}', meta={'url': 'https://h/?api-key=SUPERSECRET', 'nested': [{'h': 'x-api-key: SUPERSECRET'}]})
        (row,) = self.store.rows('errors')
        meta = self.store.rows('observations', where='WHERE id=?', args=(oid,))[0]['meta']
        for text in (row['message'], meta):
            self.assertNotIn('SUPERSECRET', text)
            self.assertNotIn('TOP.SECRET.TOKEN', text)
        self.assertEqual((row['transient'], row['mint'], row['code']), (1, 'A' * 32, 'HTTP_500'))
        self.assertLessEqual(len(row['message']), store_module.MAX_MESSAGE)

    def test_a_bare_bearer_token_is_redacted_even_without_a_header_name(self):
        self.assertNotIn('sekret123', redact('retry with Bearer sekret123 here'))
        self.assertIn('Bearer [REDACTED]', redact('retry with Bearer sekret123 here'))

    def test_candidate_mints_are_unique_in_the_schema_itself(self):
        self.store.add_candidate('U' * 32)
        db = self.raw_db()
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO candidates(ts,mint,meta,code_version,strategy_version) VALUES(1,?,'{}','c','s')", ('U' * 32,))

    def test_redact_cases(self):
        self.assertEqual(redact('a=1 pubkey: 11111111'), 'a=1 pubkey: 11111111')
        self.assertIn('[REDACTED]', redact('password=hunter2'))
        self.assertNotIn('hunter2', redact('password=hunter2'))
        self.assertEqual(redact(None), 'None')

    def test_decision_and_event_payload_bounds(self):
        self.store.add_decision('entry', 'BUY', mint='A' * 32, reasons=['ok'], features={'mcap': 1.5})
        (row,) = self.store.rows('decisions')
        self.assertEqual((row['kind'], row['action'], row['reasons'], row['features']), ('entry', 'BUY', '["ok"]', '{"mcap":1.5}'))
        with self.assertRaises(StoreError):
            self.store.add_decision('entry', 'X', features={'blob': 'x' * store_module.MAX_JSON_BYTES})
        with self.assertRaises(ValueError):
            self.store.add_decision('entry', 'X', features={'nan': float('nan')})
        for bad in (('', {}), ('k' * 65, {}), ('k', [])):
            with self.assertRaises(StoreError):
                self.store.record(*bad, code_version=CODE, strategy_version=STRATEGY)

    def test_timestamps_are_validated(self):
        for bad in (float('nan'), float('inf'), True, 'now'):
            with self.assertRaises(StoreError):
                self.store.add_error('E', transient=False, ts=bad)


class AccountingTests(Base):
    def test_cash_and_positions_are_derived_from_fills(self):
        fill, fid = self.fill_buy(sol='0.02')
        self.assertEqual(self.store.cash(), START - 20_050_000)
        (pos,) = self.store.positions().values()
        self.assertEqual((pos.mint, pos.qty_raw, pos.cost_lamports, pos.opened_at), (MINTS[0], 995_000_000, 20_050_000, 1.0))
        sold = sell(pos, sq(MINTS[0], 995_000_000, 40_000_000, 2.0), 1, CFG)
        self.store.add_fill(sold)
        self.assertEqual(self.store.positions(), {})
        self.assertEqual(self.store.realized(), sold.realized_lamports)
        self.assertEqual(self.store.cash(), START + sold.realized_lamports)
        self.assertTrue(self.store.check_invariants())
        rows = self.store.rows('fills')
        self.assertEqual([(r['side'], r['label'], r['cash_after'], r['qty_after']) for r in rows],
                         [('buy', 'EXECUTION_UNVERIFIED', START - 20_050_000, 995_000_000), ('sell', 'EXECUTION_UNVERIFIED', START + sold.realized_lamports, 0)])
        self.assertEqual({r['code_version'] for r in rows}, {CODE})

    def test_refused_fills_write_nothing(self):
        counts = self.store.counts()
        ghost = Fill(ts=1.0, mint=MINTS[1], side='sell', qty_raw=5, sol_lamports=5, fee_lamports=1, slippage_bps=50, decimals=6)
        with self.assertRaises(AccountingHalt):
            self.store.add_fill(ghost)                                      # sell without a position
        with self.assertRaises(AccountingHalt):
            self.store.add_fill(buy(bq(MINTS[0], 6 * paper.LAMPORTS, 10, 1.0), 6, CFG))     # more than the cash
        with self.assertRaises(StoreError):
            self.store.add_fill('not a fill')
        self.assertEqual(self.store.counts(), counts)
        self.assertEqual(self.store.cash(), START)

    def test_sell_more_than_held_is_refused_by_the_replay_and_by_the_database(self):
        self.fill_buy()
        (pos,) = self.store.positions().values()
        too_much = Fill(ts=2.0, mint=pos.mint, side='sell', qty_raw=pos.qty_raw + 1, sol_lamports=1, fee_lamports=0, slippage_bps=50, decimals=6,
                        cost_sold_lamports=pos.cost_lamports, realized_lamports=1 - pos.cost_lamports)
        with self.assertRaises(AccountingHalt):
            self.store.add_fill(too_much)
        db = self.raw_db()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'exceeds the position'):    # defence in depth: the trigger alone also says no
            db.execute("INSERT INTO fills(ts,mint,side,qty_raw,sol_lamports,fee_lamports,slippage_bps,decimals,cost_sold_lamports,realized_lamports,"
                       "label,cash_after,qty_after,cost_after,code_version,strategy_version) VALUES(2,?,'sell',?,1,0,50,6,0,0,'EXECUTION_UNVERIFIED',1,0,0,'c','s')",
                       (pos.mint, pos.qty_raw + 1))

    def test_live_labels_are_refused_by_the_schema(self):
        db = self.raw_db()
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO fills(ts,mint,side,qty_raw,sol_lamports,fee_lamports,slippage_bps,decimals,cost_sold_lamports,realized_lamports,"
                       "label,cash_after,qty_after,cost_after,code_version,strategy_version) VALUES(2,'m','buy',1,1,0,50,6,0,0,'LIVE',1,1,1,'c','s')")

    def test_the_derived_cache_follows_new_fills(self):
        self.assertEqual(self.store.cash(), START)
        self.fill_buy()
        self.assertLess(self.store.cash(), START)
        self.fill_buy(mint=MINTS[1])
        self.assertEqual(set(self.store.positions()), {MINTS[0], MINTS[1]})


class SharedRuleBugTests(Base):
    """If the single implementation of the rules were wrong, history written and replayed by it would still be self-consistent; the
    independent SQL sums in check_invariants must still catch it."""

    def buggy(self, cash_bonus=0, qty_bonus=0, cost_bonus=0):
        real = paper.apply_fill

        def apply(positions, cash, fill):
            new_positions, new_cash = real(positions, cash, fill)
            if fill.side == 'sell':
                new_cash += cash_bonus
                if (qty_bonus or cost_bonus) and fill.mint in new_positions:
                    held = new_positions[fill.mint]
                    new_positions[fill.mint] = paper.Position(held.mint, held.qty_raw + qty_bonus, held.cost_lamports + cost_bonus, held.opened_at,
                                                              held.realized_lamports, held.decimals)
            return new_positions, new_cash
        return apply

    def run_trades(self):
        self.fill_buy(MINTS[0])
        (pos,) = self.store.positions().values()
        self.store.add_fill(sell(pos, sq(MINTS[0], pos.qty_raw // 2, 30_000_000, 3.0), 0.5, CFG))

    def test_a_cash_bug_in_the_shared_rule_is_caught_by_the_sql_sums(self):
        from unittest.mock import patch
        with patch.object(store_module, 'apply_fill', self.buggy(cash_bonus=1)):
            self.run_trades()
            with self.assertRaisesRegex(AccountingHalt, 'cash does not equal|cash \\+ open cost'):
                self.store.check_invariants()

    def test_each_cash_identity_catches_a_bug_the_other_cannot_see(self):
        from unittest.mock import patch
        # cash 1 lamport short while the open cost is 1 lamport high: cash + open cost still equals initial + realized,
        # but cash no longer equals initial - buys + sells - fees
        with patch.object(store_module, 'apply_fill', self.buggy(cash_bonus=-1, cost_bonus=1)):
            self.run_trades()
            with self.assertRaisesRegex(AccountingHalt, 'cash does not equal initial'):
                self.store.check_invariants()

    def test_a_cost_basis_bug_alone_breaks_the_second_identity(self):
        from unittest.mock import patch
        # cash is right, the open cost is 1 lamport high: the plain sums agree, cash + open cost does not equal initial + realized
        with patch.object(store_module, 'apply_fill', self.buggy(cost_bonus=1)):
            self.run_trades()
            with self.assertRaisesRegex(AccountingHalt, 'cash \\+ open cost'):
                self.store.check_invariants()

    def test_a_quantity_bug_in_the_shared_rule_is_caught_by_the_sql_sums(self):
        from unittest.mock import patch
        with patch.object(store_module, 'apply_fill', self.buggy(qty_bonus=3)):
            self.run_trades()
            with self.assertRaisesRegex(AccountingHalt, 'positions differ'):
                self.store.check_invariants()


class TamperTests(Base):
    def setUp(self):
        super().setUp()
        self.fill_buy(MINTS[0])
        self.fill_buy(MINTS[1], sol='0.03', out=2_000_000_000, ts=2.0)
        (self.pos,) = [p for p in self.store.positions().values() if p.mint == MINTS[0]]
        self.store.add_fill(sell(self.pos, sq(MINTS[0], self.pos.qty_raw // 2, 30_000_000, 3.0), 0.5, CFG))
        self.assertTrue(self.store.check_invariants())

    def tamper(self, statement, args=()):
        self.store.close()
        db = self.raw_db()
        self.lift_guards(db)
        db.execute(statement, args)
        db.commit()
        db.close()
        # the schema check refuses the reopened store because the guards are gone: read it through a raw Store without validation
        self.store = Store.__new__(Store)
        self.store.path, self.store.code_version, self.store.strategy_version, self.store.clock = self.path, CODE, STRATEGY, lambda: 1.0
        self.store._lock, self.store._cache = threading.RLock(), None
        self.store.db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.addCleanup(self.store.db.close)

    def test_an_edited_fill_is_caught_by_the_stored_running_balances(self):
        self.tamper('UPDATE fills SET sol_lamports=sol_lamports+1 WHERE id=1')
        with self.assertRaises(AccountingHalt):
            self.store.check_invariants()

    def test_an_edited_quantity_is_caught(self):
        self.tamper('UPDATE fills SET qty_raw=qty_raw+7 WHERE id=3')
        with self.assertRaises(AccountingHalt):
            self.store.check_invariants()

    def test_a_deleted_fill_is_caught(self):
        self.tamper('DELETE FROM fills WHERE id=2')
        with self.assertRaises(AccountingHalt):
            self.store.check_invariants()

    def test_an_inserted_oversize_sell_is_caught(self):
        self.tamper("INSERT INTO fills(ts,mint,side,qty_raw,sol_lamports,fee_lamports,slippage_bps,decimals,cost_sold_lamports,realized_lamports,"
                    "label,cash_after,qty_after,cost_after,code_version,strategy_version) VALUES(9,?,'sell',?,1,0,50,6,0,1,'EXECUTION_UNVERIFIED',1,0,0,'c','s')",
                    (MINTS[2], 5))
        with self.assertRaises(AccountingHalt):
            self.store.check_invariants()

    def test_a_forged_balance_column_is_caught(self):
        self.tamper('UPDATE fills SET cash_after=cash_after+1 WHERE id=2')
        with self.assertRaises(AccountingHalt):
            self.store.check_invariants()

    def test_an_edited_initial_cash_is_caught(self):
        self.tamper("UPDATE meta SET value='999' WHERE key='initial_cash_lamports'")
        with self.assertRaises(AccountingHalt):
            self.store.check_invariants()


class PropertyTests(unittest.TestCase):
    """Random fill sequences against an independent reference model written here with plain integers."""

    def run_sequence(self, seed, steps=120):
        rnd = random.Random(seed)
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(os.path.realpath(tmp)) / 's.sqlite', initial_cash_sol=5, code_version=CODE, strategy_version=STRATEGY)
            try:
                fee, bps = rnd.choice([0, 10_000, 50_000]), rnd.choice([0, 25, 50, 300])
                cfg = PaperConfig(fee_lamports=fee, slippage_bps=bps)
                held, cash, realized, accepted = {}, START, 0, 0      # held: mint -> [qty, cost]
                for step in range(steps):
                    mint = rnd.choice(MINTS)
                    if mint in held and rnd.random() < 0.55:
                        qty0, cost0 = held[mint]
                        fraction = rnd.choice([1, 1, 0.5, 0.25, 0.9, 0.999])
                        qty = qty0 if fraction == 1 else int(qty0 * Decimal(str(fraction)))
                        if qty <= 0:
                            continue
                        out = rnd.randint(1, 10 ** 9)
                        position = store.positions()[mint]
                        fill = sell(position, sq(mint, qty, out, float(step)), fraction, cfg)
                        proceeds = out * (10_000 - bps) // 10_000
                        cost = cost0 if qty == qty0 else cost0 * qty // qty0
                        if cash + proceeds - fee < 0:
                            with self.assertRaises(AccountingHalt):
                                store.add_fill(fill)
                            continue
                        store.add_fill(fill)
                        cash += proceeds - fee
                        realized += proceeds - fee - cost
                        if qty == qty0:
                            del held[mint]
                        else:
                            held[mint] = [qty0 - qty, cost0 - cost]
                        accepted += 1
                    else:
                        lamports = rnd.randint(1, 3 * paper.LAMPORTS)
                        out = rnd.randint(1, 10 ** 12)
                        qty = out * (10_000 - bps) // 10_000
                        if qty <= 0:
                            continue
                        fill = buy(bq(mint, lamports, out, float(step)), Decimal(lamports) / paper.LAMPORTS, cfg)
                        if lamports + fee > cash:
                            with self.assertRaises(AccountingHalt):
                                store.add_fill(fill)
                            continue
                        store.add_fill(fill)
                        cash -= lamports + fee
                        q, c = held.get(mint, [0, 0])
                        held[mint] = [q + qty, c + lamports + fee]
                        accepted += 1
                    if step % 7 == 0:
                        self.assertTrue(store.check_invariants())
                    self.assertEqual(store.cash(), cash)
                self.assertTrue(store.check_invariants())
                self.assertEqual({m: [p.qty_raw, p.cost_lamports] for m, p in store.positions().items()}, held)
                self.assertEqual(store.realized(), realized)
                self.assertEqual(store.cash() + sum(c for _, c in held.values()), START + realized)       # the identity, with plain integers
                self.assertTrue(all(q > 0 for q, _ in held.values()))
                self.assertEqual(len(store.rows('fills', limit=10000)), accepted)
                return accepted
            finally:
                store.close()

    def test_random_sequences_match_the_reference_model(self):
        total = 0
        for seed in range(40):
            with self.subTest(seed=seed):
                total += self.run_sequence(seed)
        self.assertGreater(total, 800, 'the property test must actually trade')

    def test_reopening_in_the_middle_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(os.path.realpath(tmp)) / 's.sqlite'
            store = Store(path, initial_cash_sol=5, code_version=CODE, strategy_version=STRATEGY)
            store.add_fill(buy(bq(MINTS[0], 20_000_000, 10 ** 9, 1.0), '0.02', CFG))
            before = (store.cash(), store.positions(), store.realized())
            store.close()
            again = Store(path, code_version=CODE, strategy_version=STRATEGY)
            self.assertEqual((again.cash(), again.positions(), again.realized()), before)
            again.close()


class ThreadTests(Base):
    def test_concurrent_writers_do_not_corrupt_the_store(self):
        errors = []

        def work(n):
            try:
                for i in range(25):
                    self.store.positions()
                    self.store.add_error('E%d' % n, transient=bool(i % 2))
                    self.store.add_candidate('T%d-%d' % (n, i))
                    self.store.record('tick', {'n': n, 'i': i}, code_version=CODE, strategy_version=STRATEGY)
            except BaseException as error:           # noqa: BLE001
                errors.append(error)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        counts = self.store.counts()
        self.assertEqual((counts['errors'], counts['candidates'], counts['events']), (200, 200, 200))


if __name__ == '__main__':
    unittest.main()
