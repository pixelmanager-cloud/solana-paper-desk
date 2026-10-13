"""W2a shadow arms: unit and adversarial tests (the end-to-end world is tests/lean/test_e2e_arms.py). Fixtures only, no network."""
import copy
import dataclasses
import json
import os
import sqlite3
import tempfile
import types
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest import mock

from lean import arms as W, paper, providers, strategy as S
from lean import __main__ as entry
from lean.paper import Fill
from lean.store import Store
from tests.lean import test_e2e_arms as E
from tests.lean.test_strategy import DEFAULT, cfg as strategy_cfg, pos, sellq

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'config' / 'lean' / 'lean.example.json'
T0 = E.T0
STRATEGY_T0 = 1_000_000                                    # tests/lean/test_strategy.py's whole-second clock


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def identity(name='a', version='v1', config_hash='h' * 64):
    return types.SimpleNamespace(name=name, strategy_version=version, config_hash=config_hash, code_version='test')


# ------------------------------------------------------------------------------------------------------ the full take-profit
class FullTakeProfit(unittest.TestCase):
    """strategy.full_take_profit_ratio (optional): one TAKE_PROFIT of the whole remaining position; absent = the ladder alone."""

    def decide(self, mark, config, **kw):
        position = pos(cost_left=D('0.1'))
        now = STRATEGY_T0 + 60
        base = S.exit_decision(position, D(mark), None, now, config, mark_at=now - 1, **kw)
        if base.action != 'SELL':
            return base
        return S.exit_decision(position, D(mark), sellq(base.qty, out=mark, at=now - 1), now, config, mark_at=now - 1, **kw)

    def test_sells_everything_at_the_ratio(self):
        c = strategy_cfg(full_take_profit_ratio='1.10')
        below = self.decide('0.1099', c)
        self.assertEqual(below.action, 'HOLD')
        at = self.decide('0.11', c)
        self.assertEqual((at.action, at.reason, at.fraction, at.qty), ('SELL', 'TAKE_PROFIT', D(1), D(1000)))
        self.assertEqual(at.after_fill, {})
        self.assertEqual(self.decide('0.5', c).fraction, D(1))                      # also well above, still the whole position

    def test_without_the_key_the_ladder_is_the_only_take_profit(self):
        c = strategy_cfg()
        self.assertIsNone(c.full_take_profit_ratio)
        self.assertEqual(self.decide('0.11', c).action, 'HOLD')
        rung = self.decide('0.14', c)
        self.assertEqual((rung.reason, rung.fraction), ('TAKE_PROFIT', D('0.3')))
        self.assertEqual(c.config_hash, strategy_cfg().config_hash)                  # nothing about an existing config changes

    def test_stop_hold_and_forced_exits_rank_above_it(self):
        c = strategy_cfg(full_take_profit_ratio='1.10', max_hold_seconds=30)
        self.assertEqual(self.decide('0.12', c).reason, 'MAX_HOLD')                  # age 60 s >= 30 s: the hold wins over the profit
        c = strategy_cfg(full_take_profit_ratio='1.10')
        self.assertEqual(self.decide('0.12', c, liquidate=True).reason, 'LIQUIDATE')
        self.assertEqual(self.decide('0.0821', c).action, 'HOLD')                    # above the 0.82 stop, below the profit
        self.assertEqual((self.decide('0.0819', c).action, self.decide('0.0819', c).reason), ('SELL', 'STOP'))

    def test_the_value_is_validated(self):
        raw = json.loads(DEFAULT.read_text())
        for bad in ('1', '0.9', 'x', None, True, 'NaN', 'Infinity'):
            with self.assertRaises(S.ConfigError, msg=repr(bad)):
                S.StrategyConfig.from_dict({**raw, 'full_take_profit_ratio': bad})
        self.assertEqual(S.StrategyConfig.from_dict({**raw, 'full_take_profit_ratio': 1.25}).full_take_profit_ratio, D('1.25'))
        self.assertNotEqual(strategy_cfg(full_take_profit_ratio='1.10').config_hash, strategy_cfg(full_take_profit_ratio='1.11').config_hash)


# ------------------------------------------------------------------------------------------------------------------- config
class ConfigTests(unittest.TestCase):
    def raw(self):
        return json.loads(EXAMPLE.read_text())['arms']

    def load(self, **changes):
        raw = copy.deepcopy(self.raw())
        raw.update(changes)
        return W.config(raw, base_dir=ROOT / 'config' / 'lean')

    def test_the_example_block(self):
        cfg = entry.load_config(EXAMPLE)['arms']
        self.assertIsInstance(cfg, dict)
        self.assertEqual([a['name'] for a in cfg['arms']], ['hold2m', 'tp110', 'nocluster_hold2m', 'live_copy'])
        resolved = {a['name']: W.resolve_strategy(a['strategy_config'], ROOT / 'config' / 'lean') for a in cfg['arms']}
        main = json.loads(DEFAULT.read_text())
        self.assertEqual(resolved['live_copy'], main)                                       # the control IS the main strategy
        self.assertEqual({k: v for k, v in resolved['hold2m'].items() if k != 'strategy_version'},
                         {k: v for k, v in {**main, 'max_hold_seconds': 120}.items() if k != 'strategy_version'})
        self.assertEqual(resolved['hold2m']['stop_fraction'], main['stop_fraction'])         # the main stop stays
        self.assertEqual(resolved['tp110']['full_take_profit_ratio'], '1.10')
        self.assertEqual(resolved['tp110']['stop_fraction'], '0.18')
        self.assertEqual(S.StrategyConfig.from_dict(resolved['tp110']).initial_stop_ratio, D('0.82'))
        nocluster = [a for a in cfg['arms'] if a['name'] == 'nocluster_hold2m'][0]
        self.assertEqual(nocluster['entry_filter'], {'all': [{'feature': 'largest_cluster_supply_pct', 'op': '<=', 'value': '0.71',
                                                              'source': 'clusters'}], 'wait_s': 0.0})
        self.assertEqual(resolved['nocluster_hold2m']['max_hold_seconds'], 120)
        self.assertEqual(len({S.StrategyConfig.from_dict(r).config_hash for r in resolved.values()}), 4)

    def test_a_copy_of_the_config_elsewhere_is_the_same_config(self):
        first = entry.load_config(EXAMPLE)['arms']
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lean.json'
            raw = json.loads(EXAMPLE.read_text())
            raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
            path.write_text(json.dumps(raw))
            second = entry.load_config(path)['arms']                    # no strategy-default.json beside it: read when the arms are built
        self.assertEqual(first, second)
        self.assertEqual(entry.lean_config_hash({'arms': first}), entry.lean_config_hash({'arms': second}))

    def test_forms_of_strategy_config(self):
        main = json.loads(DEFAULT.read_text())
        cfg = W.config({'arms': [
            {'name': 'by_path', 'strategy_config': 'strategy-default.json'},
            {'name': 'inline', 'strategy_config': {**main, 'strategy_version': 'inline-1', 'stop_fraction': '0.2'}},
            {'name': 'based', 'strategy_config': {'base': 'strategy-default.json', 'set': {'strategy_version': 'based-1', 'max_hold_seconds': 60}}}]},
            base_dir=ROOT / 'config' / 'lean')
        got = {a['name']: W.resolve_strategy(a['strategy_config'], ROOT / 'config' / 'lean') for a in cfg['arms']}
        self.assertEqual(got['by_path'], main)
        self.assertEqual((got['inline']['strategy_version'], got['inline']['stop_fraction']), ('inline-1', '0.2'))
        self.assertEqual((got['based']['max_hold_seconds'], got['based']['stop_fraction']), (60, main['stop_fraction']))

    def test_bad_configs_are_refused(self):
        main = json.loads(DEFAULT.read_text())
        ok = {'name': 'x', 'strategy_config': 'strategy-default.json'}

        def refuse(raw, why):
            with self.assertRaises(W.ArmsConfigError, msg=why):
                W.config(raw, base_dir=ROOT / 'config' / 'lean')
        refuse({'arms': []}, 'no arms')
        refuse({}, 'arms missing')
        refuse({'arms': 'x'}, 'arms not a list')
        refuse({'arms': [ok], 'typo': 1}, 'unknown key')
        refuse({'arms': [ok, dict(ok)]}, 'duplicate names')
        refuse({'arms': [dict(ok, name='main')]}, 'reserved name')
        refuse({'arms': [dict(ok, name='Hold-2m')]}, 'bad name')
        refuse({'arms': [dict(ok, extra=1)]}, 'unknown arm key')
        refuse({'arms': [{'name': 'x'}]}, 'no strategy')
        refuse({'arms': [dict(ok, strategy_config={**main, 'stopp_fraction': '0.1'})]}, 'typo in a strategy key')
        refuse({'arms': [dict(ok, strategy_config={'base': 'strategy-default.json', 'set': {'max_hold_seconds': 0}})]}, 'invalid override')
        refuse({'arms': [dict(ok, strategy_config={'base': 'strategy-default.json', 'x': 1})]}, 'unknown base key')
        refuse({'arms': [dict(ok, strategy_config=5)]}, 'strategy type')
        refuse({'arms': [ok], 'enabled': 'yes'}, 'enabled not bool')
        refuse({'arms': [ok], 'queue_size': 0}, 'queue size')
        refuse({'arms': [ok], 'max_scale': 0.5}, 'max_scale below 1')
        refuse({'arms': [ok], 'initial_cash_sol': '-1'}, 'cash')
        refuse({'arms': [ok], 'db': ''}, 'db')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'cluster_count', 'op': '=~', 'value': 1})]}, 'operator')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'cluster_count', 'op': '<', 'value': 'x'})]}, 'value')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'cluster_count', 'op': '<', 'value': True})]}, 'bool value')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'nonsense', 'op': '<', 'value': 1, 'source': 'clusters'})]}, 'not a clusters figure')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'cluster_count', 'op': '<', 'value': 1, 'source': 'nowhere'})]}, 'source')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'cluster_count', 'op': '<', 'value': 1, 'wait_s': -1})]}, 'wait')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'cluster_count', 'op': '<', 'value': 1, 'all': []})]}, 'both forms')
        refuse({'arms': [dict(ok, entry_filter={'all': []})]}, 'empty all')
        refuse({'arms': [dict(ok, entry_filter={'feature': 'Bad Name', 'op': '<', 'value': 1})]}, 'feature name')
        refuse({'arms': [ok] * 2}, 'two copies of one name')

    def test_disabled_builds_nothing(self):
        cfg = self.load(enabled=False)
        self.assertFalse(cfg['enabled'])
        self.assertIsNone(W.build(cfg, runner=None, state_dir='/nonexistent', keys={}, code_version='x'))
        self.assertIsNone(W.build(None, runner=None, state_dir='/nonexistent', keys={}, code_version='x'))

    def test_a_strategy_file_that_is_not_there_yet_is_read_when_the_arms_are_built(self):
        raw = {'arms': [{'name': 'x', 'strategy_config': 'nowhere.json'}]}
        cfg = W.config(raw, base_dir=ROOT / 'config' / 'lean')                    # loads (a copied config), ...
        with self.assertRaises(W.ArmsConfigError):                                 # ... but cannot be resolved
            W.resolve_strategy(cfg['arms'][0]['strategy_config'], ROOT / 'config' / 'lean')
        broken = Path(tempfile.mkdtemp()) / 's.json'
        self.addCleanup(lambda: broken.unlink() if broken.exists() else None)
        broken.write_text('{"strategy_version": "x"}')                            # present but invalid: strict at load
        with self.assertRaises(W.ArmsConfigError):
            W.config({'arms': [{'name': 'x', 'strategy_config': str(broken)}]})


class ProductionConfig(unittest.TestCase):
    """Arms OFF: the deployed shapes load unchanged and keep their hash."""

    def old_hash(self, cfg):
        import hashlib
        plain = {k: v for k, v in cfg.items() if k not in ('arms', 'prefetch')}                      # (W2b's prefetch block is OFF in these)
        if 'paths' in plain:
            plain['paths'] = {k: v for k, v in plain['paths'].items() if k != 'hazard_path_sample'}   # (W2b: 0 = off)
        text = json.dumps(entry._strip_comments(plain), sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)
        return hashlib.sha256(text.encode()).hexdigest()

    def test_the_live_shapes_load_with_arms_off(self):
        for name in ('lean.r1-prod.json', 'lean.r1-live.json', 'lean.baseline.json'):
            cfg = entry.load_config(ROOT / 'tests' / 'lean' / name)
            self.assertIsNone(cfg['arms'], name)
            self.assertEqual(entry.lean_config_hash(cfg), self.old_hash(cfg), name)           # the hash as it was before W2a
            self.assertEqual(entry.CONFIG_KEYS['arms'], None)

    def test_adding_the_example_block_turns_it_on_and_changes_nothing_else(self):
        prod = ROOT / 'tests' / 'lean' / 'lean.r1-prod.json'
        base = entry.load_config(prod)
        raw = json.loads(prod.read_text())
        raw['arms'] = json.loads(EXAMPLE.read_text())['arms']
        with tempfile.TemporaryDirectory() as tmp:
            raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
            path = Path(tmp) / 'lean.json'
            path.write_text(json.dumps(raw))
            on = entry.load_config(path)
        self.assertEqual({k: v for k, v in on.items() if k != 'arms'}, {k: v for k, v in base.items() if k not in ('arms', 'strategy_config')}
                         | {'strategy_config': on['strategy_config']})
        self.assertEqual(len(on['arms']['arms']), 4)
        self.assertNotEqual(entry.lean_config_hash(on), entry.lean_config_hash(base))
        self.assertEqual(entry.lean_config_hash({**on, 'arms': None}), self.old_hash({**on, 'arms': None}))

    def test_a_typo_in_the_arms_block_stops_the_start(self):
        raw = json.loads(EXAMPLE.read_text())
        raw['arms']['arms'][0]['strategy_config']['set']['max_hold_secondss'] = 120
        with tempfile.TemporaryDirectory() as tmp:
            raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
            path = Path(tmp) / 'lean.json'
            path.write_text(json.dumps(raw))
            (Path(tmp) / 'strategy-default.json').write_text(DEFAULT.read_text())
            with self.assertRaises(entry.ConfigError):
                entry.load_config(path)


# ------------------------------------------------------------------------------------------------------------------ filters
class ScriptedReader:
    def __init__(self, table):
        self.table, self.asked = table, []

    def latest(self, kind, mint, upto_ts):
        self.asked.append((kind, mint, upto_ts))
        return self.table.get((kind, mint), ('NO_ROW', None, None))


def flt(feature='largest_cluster_supply_pct', op='<=', value='0.71', source='clusters', wait_s=0):
    return W.filter_config({'feature': feature, 'op': op, 'value': value, 'source': source, 'wait_s': wait_s})


class FilterTests(unittest.TestCase):
    def verdict(self, f, table=None, screen=None):
        return W.evaluate_filter(f, screen or {}, ScriptedReader(table or {}), 'M', 100.0)

    def row(self, value, **meta):
        return ('OK', 90.0, {'largest_cluster_supply_pct': value, **meta})

    def test_operators_and_the_boundary(self):
        cases = [('<=', '0.71', '0.71', 'PASS'), ('<=', '0.71', '0.7101', 'REJECT'), ('<', '0.71', '0.71', 'REJECT'),
                 ('<', '0.71', '0.7099', 'PASS'), ('>=', '1', '1', 'PASS'), ('>', '1', '1', 'REJECT'), ('==', '1', '1.0', 'PASS'),
                 ('!=', '1', '1', 'REJECT')]
        for op, threshold, value, expected in cases:
            got, _ = self.verdict(flt(op=op, value=threshold), {('clusters', 'M'): self.row(float(value))})
            self.assertEqual(got, expected, (op, threshold, value))

    def test_readiness_is_decided_by_when_the_row_was_written(self):
        for status, why in (('NO_ROW', 'NO_ROW'), ('AFTER', 'WRITTEN_AFTER_DECISION'), ('UNREADABLE', 'STORE_UNREADABLE')):
            got, detail = self.verdict(flt(), {('clusters', 'M'): (status, 120.0, None)})
            self.assertEqual((got, detail['why']), ('NOT_READY', why))
        got, detail = self.verdict(flt(), {('clusters', 'M'): self.row(None, unavailable={'largest_cluster_supply_pct': 'HOLDERS_UNAVAILABLE'})})
        self.assertEqual((got, detail['why'], detail['unavailable']), ('NOT_READY', 'VALUE_NULL', 'HOLDERS_UNAVAILABLE'))
        got, detail = self.verdict(flt(), {('clusters', 'M'): self.row(0.1, flags={'largest_cluster_supply_pct': 'AT_COLLECTION'})})
        self.assertEqual((got, detail['why']), ('NOT_READY', 'AT_COLLECTION'))
        # a flag on ANOTHER figure does not make this one unready
        got, _ = self.verdict(flt(), {('clusters', 'M'): self.row(0.1, flags={'cluster_count': 'AT_COLLECTION'})})
        self.assertEqual(got, 'PASS')
        got, _ = self.verdict(flt(), {('clusters', 'M'): self.row('abc')})
        self.assertEqual(got, 'NOT_READY')

    def test_the_reader_is_asked_for_what_was_known_at_the_decision(self):
        reader = ScriptedReader({('clusters', 'M'): self.row(0.2)})
        W.evaluate_filter(flt(), {}, reader, 'M', 1234.5)
        self.assertEqual(reader.asked, [('clusters', 'M', 1234.5)])

    def test_screen_source_and_several_conditions(self):
        f = W.filter_config({'all': [{'feature': 'market_cap_usd', 'op': '>=', 'value': 60000, 'source': 'screen'},
                                     {'feature': 'cluster_count', 'op': '<=', 'value': 2, 'source': 'clusters'}]})
        table = {('clusters', 'M'): ('OK', 1.0, {'cluster_count': 1})}
        self.assertEqual(self.verdict(f, table, {'market_cap_usd': '60000.0'})[0], 'PASS')
        self.assertEqual(self.verdict(f, table, {'market_cap_usd': '59999'})[0], 'REJECT')
        self.assertEqual(self.verdict(f, table, {})[0], 'NOT_READY')                          # a missing screen figure never passes
        self.assertEqual(self.verdict(f, {}, {'market_cap_usd': '70000'})[0], 'NOT_READY')   # the second condition has no row
        got, detail = self.verdict(f, {('clusters', 'M'): ('OK', 1.0, {'cluster_count': 5})}, {'market_cap_usd': '70000'})
        self.assertEqual((got, detail['feature'], detail['value']), ('REJECT', 'cluster_count', '5'))

    def test_the_real_reader_sees_only_rows_written_before_the_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'lean.sqlite'
            store = Store(path, initial_cash_sol='10', code_version='t', strategy_version='t')
            self.addCleanup(store.close)
            store.add_observation('clusters', b'{}', mint='M', meta={'largest_cluster_supply_pct': 0.2}, ts=100.0)
            store.add_observation('clusters', b'{}', mint='M', meta={'largest_cluster_supply_pct': 9.0}, ts=200.0)
            store.add_observation('clusters', b'{}', mint='N', meta={'largest_cluster_supply_pct': 0.1}, ts=300.0)
            store.add_observation('flow', b'{}', mint='O', meta={'x': 1}, ts=50.0)
            reader = W.RowsReader(path)
            self.addCleanup(reader.close)
            self.assertEqual(reader.latest('clusters', 'M', 99.9)[0], 'AFTER')
            self.assertEqual(reader.latest('clusters', 'M', 100.0)[2]['largest_cluster_supply_pct'], 0.2)
            self.assertEqual(reader.latest('clusters', 'M', 199.9)[2]['largest_cluster_supply_pct'], 0.2)          # the 9.0 row is later
            self.assertEqual(reader.latest('clusters', 'M', 200.0)[2]['largest_cluster_supply_pct'], 9.0)
            self.assertEqual(reader.latest('clusters', 'N', 299.0), ('AFTER', 300.0, None))
            self.assertEqual(reader.latest('clusters', 'Z', 1e12)[0], 'NO_ROW')
            self.assertEqual(reader.latest('flow', 'M', 1e12)[0], 'NO_ROW')                                         # another kind
            with self.assertRaises(sqlite3.OperationalError):                                                         # read-only
                reader._connect().execute("INSERT INTO meta VALUES('x','y')")
            self.assertEqual(W.RowsReader(Path(tmp) / 'missing.sqlite').latest('clusters', 'M', 1.0)[0], 'UNREADABLE')


# ------------------------------------------------------------------------------------------------------------------- storage
def fill(side, mint='M', qty=1000, sol=200_000_000, fee=50_000, ts=1000.0, cost_sold=0, realized=0):
    return Fill(ts=ts, mint=mint, side=side, qty_raw=qty, sol_lamports=sol, fee_lamports=fee, slippage_bps=50, decimals=6,
                cost_sold_lamports=cost_sold, realized_lamports=realized, quote_ref='r')


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(os.path.realpath(self.tmp.name)) / 'arms.sqlite'
        self.store = W.ArmsStore(self.path)
        self.addCleanup(self.store.close)

    def test_rows_carry_the_identity_and_the_log_is_append_only(self):
        arm = identity('hold2m', 'lean-1-hold2m', 'ab' * 32)
        self.store.event(arm, 'arm_init', {'x': 1}, ts=1.0)
        self.store.decision(arm, 'entry', 'SKIP', mint='M', candidate_id=7, reasons=['COOLDOWN'], features={'a': D('1.5')}, ts=2.0)
        self.store.fill(arm, fill('buy'), cash_after=1, held=paper.Position('M', 1000, 200_050_000, 1000.0, 0, 6), candidate_id=7,
                        price_source='main_entry_quote:q0', flags=['SCALED_FROM_MAIN'], event='open', state={'stage': 0}, open_fill_id=None)
        for table in ('arm_events', 'arm_decisions', 'arm_fills', 'arm_states'):
            rows = self.store.rows(table)
            self.assertEqual(len(rows), 1, table)
            self.assertEqual((rows[0]['arm_name'], rows[0]['strategy_version'], rows[0]['config_hash'], rows[0]['code_version']),
                             ('hold2m', 'lean-1-hold2m', 'ab' * 32, 'test'), table)
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.db.execute('UPDATE %s SET ts=0' % table)
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.db.execute('DELETE FROM %s' % table)
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.db.execute('INSERT OR REPLACE INTO %s(id,ts) VALUES(1,0)' % table)
        self.assertEqual(json.loads(self.store.rows('arm_decisions')[0]['features']), {'a': '1.5'})
        self.assertEqual(self.store.rows('arm_fills')[0]['price_source'], 'main_entry_quote:q0')
        self.assertEqual(self.store.rows('arm_states')[0]['open_fill_id'], self.store.rows('arm_fills')[0]['id'])

    def test_a_fill_and_its_state_are_one_transaction(self):
        arm = identity()
        with self.assertRaises(sqlite3.IntegrityError):                                    # a bad state row: the fill must not stay
            self.store.fill(arm, fill('buy'), cash_after=1, held=None, candidate_id=1, price_source='x', flags=[], event='bogus', state={},
                            open_fill_id=None)
        self.assertEqual(self.store.rows('arm_fills'), [])

    def test_the_schema_is_versioned_and_never_migrated(self):
        self.store.close()
        self.assertEqual(sqlite3.connect(self.path).execute("SELECT value FROM arm_meta WHERE key='schema_version'").fetchone()[0], '1')
        again = W.ArmsStore(self.path)                                                       # reopening a good file works
        again.close()
        db = sqlite3.connect(self.path)
        db.execute("DROP TRIGGER arm_fills_no_update")
        db.commit()
        db.close()
        with self.assertRaises(W.ArmsStoreError):
            W.ArmsStore(self.path)
        other = Path(self.tmp.name) / 'v2.sqlite'
        good = W.ArmsStore(other)
        good.close()
        db = sqlite3.connect(other)
        db.execute("DROP TRIGGER arm_fills_no_delete")
        db.execute("CREATE TRIGGER arm_fills_no_delete BEFORE DELETE ON arm_fills BEGIN SELECT 1; END")
        db.commit()
        db.close()
        with self.assertRaises(W.ArmsStoreError):
            W.ArmsStore(other)
        link = Path(self.tmp.name) / 'link.sqlite'
        link.symlink_to(other)
        with self.assertRaises(W.ArmsStoreError):
            W.ArmsStore(link)
        junk = Path(self.tmp.name) / 'junk.sqlite'
        junk.write_bytes(b'not a database at all')
        with self.assertRaises(sqlite3.DatabaseError):
            W.ArmsStore(junk)
        self.assertTrue(junk.exists())                                                       # an existing file is never deleted

    def test_a_new_file_that_fails_to_initialise_is_not_left_behind(self):
        missing = Path(self.tmp.name) / 'nodir' / 'arms.sqlite'
        with self.assertRaises(W.ArmsStoreError):
            W.ArmsStore(missing)
        self.assertFalse(missing.exists())

    def test_unknown_tables_are_refused(self):
        with self.assertRaises(W.ArmsStoreError):
            self.store.rows('arm_meta')
        with self.assertRaises(W.ArmsStoreError):
            self.store.rows('fills; DROP TABLE arm_fills')


# --------------------------------------------------------------------------------------------------------------- quote maths
class QuoteTests(unittest.TestCase):
    def quote(self, i, o):
        return providers.Quote(i, o, o * 99 // 100, D('0.004'), ('Pump.fun Amm',))

    def test_scale_is_linear_and_exact(self):
        q = self.quote(200_000_000, 500_000_000_000)
        half = W.scale_quote(q, 100_000_000)
        self.assertEqual((half.in_amount, half.out_amount), (100_000_000, 250_000_000_000))
        odd = W.scale_quote(q, 123_456_789)
        self.assertEqual(odd.out_amount, 500_000_000_000 * 123_456_789 // 200_000_000)
        self.assertIsNone(odd.other_amount_threshold)
        self.assertEqual(odd.route_labels, q.route_labels)
        with self.assertRaises(ValueError):
            W.scale_quote(self.quote(10, 3), 1)                                                # scales to zero output
        with self.assertRaises(ValueError):
            W.scale_quote(q, 0)

    def test_the_modeled_sell_leg_follows_the_pool(self):
        run = E.Run(self)
        arms = run.r.arms
        base, gross = 2 * 10 ** 14, 80 * 10 ** 9
        screen = {'base_reserve_raw': str(base), 'quote_gross_raw': str(gross), 'virtual_quote_reserves_raw': '0', 'supply_raw': str(10 ** 15)}
        spend = 200_000_000
        net = spend * 10000 // (10000 + 120)
        bought = base * net // (gross + net)                                                  # buy with a 1.2 % fee off the input
        quote = self.quote(spend, bought)
        tokens = bought * 9950 // 10000
        out = arms._modeled_sell(screen, quote, tokens, T0)
        self.assertTrue(0.955 * spend < out < 0.995 * spend, out / spend)                    # fees both ways, almost no impact at 0.25 %
        big_net = 20 * net
        big_bought = base * big_net // (gross + big_net)
        bigger = arms._modeled_sell(screen, self.quote(20 * spend, big_bought), big_bought * 9950 // 10000, T0)
        self.assertLess(bigger / (20 * spend), out / spend)                                  # price impact grows with the size
        boosted = {**screen, 'virtual_quote_reserves_raw': str(30 * 10 ** 9)}
        self.assertGreater(arms._modeled_sell(boosted, quote, tokens, T0), out)              # virtual reserves make the pool deeper
        for broken in ({}, {**screen, 'base_reserve_raw': 'x'}, {k: v for k, v in screen.items() if k != 'supply_raw'}):
            self.assertIsNone(arms._modeled_sell(broken, quote, tokens, T0))
        self.assertIsNone(arms._modeled_sell(screen, self.quote(spend, base), tokens, T0))   # buying the whole pool is not modelable


# ------------------------------------------------------------------------------------------------------------------ statistics
def trade(mint, entry_at, hold, ret, **kw):
    cost = 1_000_000_000
    return {'mint': mint, 'entry_at': entry_at, 'exit_at': entry_at + hold, 'hold_s': hold, 'ret': D(ret),
            'pnl_lamports': int(cost * D(ret)), 'reason': kw.get('reason', 'MAX_HOLD')}


class StatsTests(unittest.TestCase):
    def test_ev_win_rate_tail_and_curve(self):
        trades = [trade('a', 10, 100, '0.10'), trade('b', 20, 100, '-0.60'), trade('c', 30, 120, '-0.02'), trade('d', 40, 140, '0.20')]
        s = W.stats(trades, 10 * 10 ** 9)
        self.assertEqual(s['trades'], 4)
        self.assertAlmostEqual(s['ev_per_trade'], (0.10 - 0.60 - 0.02 + 0.20) / 4)
        self.assertEqual((s['win_rate'], s['p_le_minus_50']), (0.5, 0.25))
        self.assertAlmostEqual(s['pnl_sol'], -0.32)
        self.assertEqual(s['median_hold_s'], 120)
        self.assertEqual([round(v, 4) for _, v in s['equity_curve']], [10.1, 9.5, 9.48, 9.68])
        self.assertEqual([t for t, _ in s['equity_curve']], [110, 120, 150, 180])
        lo, hi = s['ev_ci95']
        self.assertTrue(lo < s['ev_per_trade'] < hi)
        empty = W.stats([])
        self.assertEqual((empty['trades'], empty['ev_per_trade'], empty['win_rate'], empty['equity_curve']), (0, None, None, []))
        one = W.stats([trade('a', 1, 1, '0.5')])
        self.assertIsNone(one['ev_ci95'])
        many = W.stats([trade('m%d' % i, i, 10, '0.01') for i in range(1000)], 0, points=50)
        self.assertEqual(len(many['equity_curve']), 50)
        self.assertEqual(many['equity_curve'][-1][1], 10.0)

    def test_the_boundary_of_the_tail(self):
        s = W.stats([trade('a', 1, 1, '-0.5'), trade('b', 2, 1, '-0.4999')])
        self.assertEqual(s['p_le_minus_50'], 0.5)

    def test_paired_comparison(self):
        arm = [trade('a', 1, 120, '0.05'), trade('b', 2, 120, '-0.10'), trade('x', 3, 120, '0.30'), trade('a', 900, 120, '9')]
        main = [trade('a', 1, 2000, '-0.02'), trade('b', 2, 2000, '-0.05'), trade('y', 4, 2000, '-0.9')]
        p = W.paired(arm, main)
        self.assertEqual((p['pairs'], p['arm_only'], p['main_only']), (2, 1, 1))                 # the 2nd trade of 'a' is not a pair
        self.assertAlmostEqual(p['mean_diff'], ((0.05 + 0.02) + (-0.10 + 0.05)) / 2)
        self.assertAlmostEqual(p['arm_mean'], (0.05 - 0.10) / 2)
        self.assertAlmostEqual(p['main_mean'], (-0.02 - 0.05) / 2)
        self.assertEqual(p['arm_better_share'], 0.5)
        none = W.paired(arm, [])
        self.assertEqual((none['pairs'], none['arm_only'], none['mean_diff']), (0, 3, None))

    def test_closed_trades_from_the_log_are_rent_free_and_only_closed_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = W.ArmsStore(Path(tmp) / 'arms.sqlite')
            self.addCleanup(store.close)
            arm = identity('x')
            rent = 2_039_280
            exe_buy = {'execution': {'side': 'buy', 'ata_rent_paid_lamports': rent, 'ata_refund_lamports': 0}}
            exe_sell = {'execution': {'side': 'sell', 'ata_refund_lamports': rent, 'ata_rent_basis_sold_lamports': 0}}
            buy = fill('buy', fee=50_000 + rent, sol=1_000_000_000, ts=10.0)
            held = paper.Position('M', 1000, 1_000_000_000 + 50_000 + rent, 10.0, 0, 6)
            store.fill(arm, buy, cash_after=1, held=held, candidate_id=1, price_source='s', flags=[], event='open',
                       state={'stage': 0, **exe_buy}, open_fill_id=None)
            first = store.rows('arm_fills')[0]['id']
            sell = fill('sell', sol=1_100_000_000 + rent, fee=50_000, ts=130.0, cost_sold=held.cost_lamports, realized=1_100_000_000 + rent - 50_000 - held.cost_lamports)
            store.fill(arm, sell, cash_after=2, held=None, candidate_id=None, price_source='s', flags=['F'], event='closed',
                       state={'reason': 'MAX_HOLD', **exe_sell}, open_fill_id=first)
            store.fill(arm, fill('buy', mint='OPEN', ts=140.0), cash_after=3, held=held, candidate_id=2, price_source='s', flags=[],
                       event='open', state={'stage': 0}, open_fill_id=None)
            connection = W.open_ro(store.path)
            self.addCleanup(connection.close)
            trades = W.closed_trades(connection, 'x')
            self.assertEqual(len(trades), 1)
            t = trades[0]
            self.assertEqual((t['cost'], t['proceeds'], t['reason'], t['hold_s']), (1_000_050_000, 1_099_950_000, 'MAX_HOLD', 120.0))
            self.assertEqual(t['flags'], {'F'})
            self.assertAlmostEqual(float(t['ret']), 99_900_000 / 1_000_050_000, places=9)
            self.assertEqual(W.closed_trades(connection, 'nobody'), [])
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute('DELETE FROM arm_fills')


# ---------------------------------------------------------------------------------------------- one candidate, many arms
class EvidenceTests(unittest.TestCase):
    """The decisions of the arms on evidence the test controls (the world runs for real; a spy edits the evidence in flight)."""

    def setUp(self):
        self.run = E.Run(self)
        self.run.seed_clusters()
        self.edits = {}                                          # role -> function(evidence) -> evidence
        self.seen = {}
        real = W.Arms.submit

        def spy(arms, ev):
            role = self.run.role[ev.mint]
            self.seen[role] = ev
            edit = self.edits.get(role)
            real(arms, edit(ev) if edit else ev)
        patcher = mock.patch.object(W.Arms, 'submit', spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def arm(self, name):
        return [a for a in self.run.r.arms.arms if a.name == name][0]

    def test_a_hazard_the_main_arm_found_is_never_entered(self):
        self.edits['flat_a'] = lambda ev: dataclasses.replace(ev, hazard='HONEYPOT_SIM_BLOCKED')
        self.run.drive(until=T0 + 60)
        for name in ('hold2m', 'tp110', 'nocluster_hold2m', 'live_copy'):
            d = self.run.decisions(name, 'flat_a')
            self.assertEqual([(x['action'], x['reasons']) for x in d], [('SKIP', ['MAIN_HAZARD:HONEYPOT_SIM_BLOCKED'])], name)
            self.assertEqual(d[0]['features']['honeypot'], 'blocked')
        self.assertTrue(self.run.r.store.rows('fills', limit=10))                              # the main arm itself did enter it
        self.assertEqual(self.run.rows('arm_fills'), [])

    def test_an_entry_the_main_arm_dropped_for_latency_is_skipped_not_re_quoted(self):
        self.edits['flat_a'] = lambda ev: dataclasses.replace(ev, plan=None, aborted=True)
        self.run.drive(until=T0 + 60)
        for name in ('hold2m', 'tp110', 'live_copy'):
            self.assertEqual(self.run.decisions(name, 'flat_a')[0]['reasons'], ['MAIN_ENTRY_ABORTED_LATENCY'], name)
        self.assertEqual(self.run.r.arms.calls['jupiter_entry_quote'], 0)

    def test_a_shed_low_lane_skips_the_candidate_with_a_record(self):
        self.edits['flat_a'] = lambda ev: dataclasses.replace(ev, plan=None)
        providers.shed_low('jupiter', 600, clock=self.run.clock.monotonic, sleep=self.run.clock.sleep)
        self.run.drive(until=T0 + 60)
        for name in ('hold2m', 'tp110', 'live_copy'):
            d = self.run.decisions(name, 'flat_a')[0]
            self.assertEqual((d['action'], d['reasons']), ('SKIP', ['ARM_SKIPPED_SHED']), name)
        self.assertEqual(self.run.r.arms.calls['jupiter_entry_quote'], 0)                     # nothing was sent
        self.assertEqual(self.run.r.arms.counts['entry_shed'], 4)
        self.assertEqual(self.run.rows('arm_fills'), [])

    def test_the_candidate_is_decided_once_per_arm(self):
        self.run.drive(until=T0 + 60)
        ev = self.seen['flat_a']
        arms = self.run.r.arms
        before = len(self.run.rows('arm_decisions'))
        arms.submit(ev)
        arms.submit(dataclasses.replace(ev))
        self.assertEqual(len(self.run.rows('arm_decisions')), before)
        self.assertEqual(len(self.run.decisions('hold2m', 'flat_a')), 1)

    def test_wait_s_holds_the_candidate_and_enters_when_the_row_exists(self):
        self.arm('nocluster_hold2m').filter['wait_s'] = 200.0
        self.run.drive(until=T0 + 600)
        d = self.run.decisions('nocluster_hold2m', 'flat_b')                                    # flat_b: the row is written 30 s AFTER the decision
        self.assertEqual([(x['action'], x['reasons']) for x in d], [('DEFER', ['FEATURE_NOT_READY']), ('BUY', ['ENTRY'])])
        ready = self.run.ready['flat_b']
        bought = d[1]
        self.assertGreaterEqual(bought['ts'], ready + 30)                                      # decided when the row existed, not before
        self.assertLess(bought['ts'], ready + 30 + 2 * E.STEP)
        self.assertAlmostEqual(bought['features']['deferred_s'], bought['ts'] - self.seen['flat_b'].ts, places=2)
        self.assertGreater(bought['features']['deferred_s'], 20)
        self.assertEqual(bought['features']['price_source'], 'low_lane_entry_quote')             # a fresh quote, not the main arm's old one
        self.assertIn('LATENCY_MODEL_ABSENT', bought['features']['flags'])
        # no row ever: held until wait_s, then skipped as not ready
        d = self.run.decisions('nocluster_hold2m', 'dump_a')
        self.assertEqual([(x['action'], x['reasons']) for x in d], [('DEFER', ['FEATURE_NOT_READY']), ('SKIP', ['FEATURE_NOT_READY'])])
        self.assertGreaterEqual(d[1]['ts'], self.run.ready['dump_a'] + 200)
        # a row that exists but fails the filter is rejected at once, not held
        d = self.run.decisions('nocluster_hold2m', 'pump_a')
        self.assertEqual([x['reasons'] for x in d], [['ENTRY_FILTER']])
        for ev in self.arm('nocluster_hold2m').pending_entries:                                   # nothing is held longer than wait_s
            self.assertLess(self.run.clock.time() - ev.ts, 200 + E.STEP)

    def test_a_size_far_from_the_main_arms_is_priced_on_its_own_quote(self):
        self.run.r.arms.cfg['max_scale'] = 1.0                                                  # only an identical size may reuse a main quote
        self.run.drive(until=T0 + 400)
        sources = {}
        for name in ('hold2m', 'live_copy'):
            for role in ('flat_a', 'pump_a', 'dump_a'):
                d = self.run.decisions(name, role)[0]
                if d['action'] == 'BUY':
                    sources[(name, role)] = d['features']['price_source']
        self.assertTrue(all(v.startswith('main_entry_quote') for (n, _), v in sources.items() if n == 'live_copy'))
        self.assertTrue(any(v == 'low_lane_entry_quote' for (n, _), v in sources.items() if n == 'hold2m'), sources)

    def test_marks_of_a_mint_the_main_arm_holds_come_from_the_main_read(self):
        self.run.drive(until=T0 + 90)
        copy = self.arm('live_copy')
        self.assertEqual({m: v[2] for m, v in copy.marks.items()}, {m: 'main_marks' for m in copy.book.positions})
        self.assertTrue(copy.marks)
        main_marks = self.run.r.marks
        for mint, mark in copy.marks.items():
            self.assertEqual(mark[0], main_marks[mint][0])                                   # the same figure, to the lamport

    def test_the_arms_use_only_the_low_lane(self):
        lanes = []
        real = providers.Transport.call

        def spy(transport, *args, **kwargs):
            lanes.append((transport.provider, transport.lane))
            return real(transport, *args, **kwargs)
        arms = self.run.r.arms
        self.assertEqual({(arms.low.jupiter.transport.lane), (arms.low.helius.transport.lane)}, {'low'})
        self.assertIsNot(arms.low.helius.transport, self.run.r.exit_providers.helius.transport)
        self.assertIsNot(arms.low.jupiter.transport, self.run.r.providers.jupiter.transport)
        with mock.patch.object(providers.Transport, 'call', spy):
            self.run.drive(until=T0 + 1500)
        on = {lane: lanes.count(lane) for lane in set(lanes)}
        off = E.Run(self, arms=False)
        lanes.clear()
        with mock.patch.object(providers.Transport, 'call', spy):
            off.seed_clusters()
            off.drive(until=T0 + 1500)
        base = {lane: lanes.count(lane) for lane in set(lanes)}
        for lane in on:
            if lane[1] != 'low':
                self.assertEqual(on[lane], base[lane], lane)                                     # not one extra call on the main or exit lane
        extra = sum(n for lane, n in on.items() if lane[1] == 'low') - sum(n for lane, n in base.items() if lane[1] == 'low')
        calls = self.run.r.arms.calls
        self.assertEqual(extra, sum(calls.values()))
        self.assertGreater(extra, 0)

    def test_an_exit_with_no_quote_for_a_while_falls_back_to_a_flagged_mark_fill(self):
        providers.shed_low('jupiter', 100000, clock=self.run.clock.monotonic, sleep=self.run.clock.sleep)
        self.run.cfg['arms']['exit_wait_s'] = 30.0
        self.run.r.arms.cfg['exit_wait_s'] = 30.0
        # hold2m needs the low lane for its exit quote (the main arm does not exit flat_a for 45 minutes)
        self.run.drive(until=T0 + 400)
        trades = self.run.trades('hold2m')
        self.assertIn('flat_a', trades)
        self.assertEqual(trades['flat_a']['reason'], 'MAX_HOLD')
        fills = [f for f in self.run.rows('arm_fills', 'hold2m') if f['side'] == 'sell' and self.run.role[f['mint']] == 'flat_a']
        self.assertEqual(fills[0]['price_source'], 'mark_model')
        self.assertEqual(set(json.loads(fills[0]['flags'])), {'MARK_FILL', 'LATENCY_MODEL_ABSENT'})
        self.assertGreaterEqual(fills[0]['ts'] - trades['flat_a']['entry_at'], 120 + 30)       # it waited exit_wait_s for a real quote first
        self.assertGreater(self.run.r.arms.counts['exit_shed_waits'], 0)


class RunnerHooks(unittest.TestCase):
    """The few lines the runner carries for the arms: what they hand over, and that they cannot hurt the trader."""

    def setUp(self):
        self.run = E.Run(self, arms=True)
        self.r = self.run.r
        self.sent = []
        patcher = mock.patch.object(W.Arms, 'submit', lambda arms, ev: self.sent.append(ev))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.candidate = types.SimpleNamespace(mint='MINT1')
        self.result = types.SimpleNamespace(features={'pool': 'P', 'decimals': 6}, raw=[('accounts_mint_pool', b'{}')])

    def handle(self, **moves):
        self.r._w2a_candidate(self.candidate, 5, self.result, {'mint': 'MINT1', 'decimals': 6})
        for name, move in moves.items():
            move()
        self.r._w2a_after(self.candidate)

    def test_what_the_main_arm_did_is_handed_over(self):
        self.r.honeypot = types.SimpleNamespace(stats={'rejected': 3})
        self.handle()
        ev = self.sent[-1]
        self.assertEqual((ev.mint, ev.cid, ev.hazard, ev.aborted, ev.main_acted, ev.plan), ('MINT1', 5, None, False, False, None))
        self.assertEqual((ev.features['mint'], ev.screen, ev.raw), ('MINT1', {'pool': 'P', 'decimals': 6}, [('accounts_mint_pool', b'{}')]))
        self.assertEqual((ev.at, ev.ts), (int(self.run.clock.time()), self.run.clock.time()))
        plan = (('buy', 1, 2), ('sell', 3), {'fill': 'q0'})
        self.handle(plan=lambda: self.r._w2a_plan(*plan))
        self.assertEqual(self.sent[-1].plan, plan)
        self.handle(honeypot=lambda: self.r.honeypot.stats.update(rejected=4))
        self.assertEqual(self.sent[-1].hazard, 'HONEYPOT_SIM_BLOCKED')
        self.handle(abort=lambda: self.r.counts.update(entry_aborted_latency=self.r.counts.get('entry_aborted_latency', 0) + 1))
        self.assertTrue(self.sent[-1].aborted)
        self.assertFalse(self.sent[-1].hazard)
        self.handle(fill=lambda: self.r.counts.update(entered=self.r.counts['entered'] + 1))
        self.assertTrue(self.sent[-1].main_acted)

    def test_a_candidate_that_will_be_retried_is_handed_over_on_its_final_attempt(self):
        self.r.retry.append((self.candidate, 2))
        count = len(self.sent)
        self.handle()
        self.assertEqual(len(self.sent), count)
        self.r.retry.clear()
        self.handle()
        self.assertEqual(len(self.sent), count + 1)

    def test_nothing_is_handed_over_for_another_candidate_or_without_a_passed_screen(self):
        self.r._w2a_after(self.candidate)                                                    # no candidate noted
        self.r._w2a_candidate(types.SimpleNamespace(mint='OTHER'), 6, self.result, {})
        self.r._w2a_after(self.candidate)                                                    # a different one was noted
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.r._w2a)

    def test_a_failing_arms_object_cannot_reach_the_runner(self):
        broken = mock.Mock()
        broken.seen_sell_quote.side_effect = broken.seen_accounts.side_effect = RuntimeError('x')
        broken.marks_ready.side_effect = broken.submit.side_effect = RuntimeError('x')
        real = self.r.arms
        self.r.arms = broken
        self.r._w2a_candidate(self.candidate, 5, self.result, {})
        self.r._w2a_after(self.candidate)                                                    # swallowed
        self.r._w2a_marks_ready()                                                            # swallowed
        self.r.arms = real
        # no arms at all: every hook is a no-op
        self.r.arms = None
        self.r._w2a_candidate(self.candidate, 5, self.result, {})
        self.assertIsNone(self.r._w2a)
        self.r._w2a_after(self.candidate)
        self.r._w2a_marks_ready()
        self.assertEqual(self.r.health()['arms'], {'enabled': False, 'alive': False})

    def test_run_gives_the_work_to_its_own_thread(self):
        self.r.arms = W.Arms.__new__(W.Arms)
        self.assertEqual([name for name, _target, _args in self.r._aux_specs() if name == 'arms'], ['arms'])
        self.r.arms = None
        self.assertNotIn('arms', [name for name, _target, _args in self.r._aux_specs()])

    def test_the_arm_thread_runs_and_stops_with_the_runner(self):
        arms = self.run.r.arms
        self.assertFalse(arms.threaded)
        self.r.stop.set()                                                                    # run() starts, finds the stop set and returns
        with mock.patch.object(W.Arms, 'process') as process:
            self.assertTrue(self.r.run(install_signals=False))
        self.assertTrue(arms.threaded)
        self.assertTrue(process.called)
        health = self.r.health()['arms']
        self.assertEqual((health['enabled'], health['threaded']), (True, True))
        self.assertEqual(set(health['arms']), {'hold2m', 'tp110', 'nocluster_hold2m', 'live_copy'})


class ExitTimingTests(unittest.TestCase):
    def test_an_arm_exit_is_filled_one_execution_delay_after_the_decision(self):
        run = E.Run(self)
        run.seed_clusters()
        r, clock, hold = run.r, run.clock, run.r.arms.arms[0]
        tick, pend = T0, None
        while tick <= T0 + 400 and pend is None:
            if clock.time() < tick:
                clock.t = tick
            r.position_pass()
            if hold.pending_exits:
                pend = list(hold.pending_exits.values())[0]
            else:
                r.candidate_pass()
                tick += E.STEP
        self.assertIsNotNone(pend)
        self.assertEqual(pend.reason, 'MAX_HOLD')

        def sells():
            return [f for f in run.rows('arm_fills', 'hold2m') if f['side'] == 'sell']
        decided = pend.decided_at
        self.assertAlmostEqual(pend.due_at - decided, 3.0)                                     # exec_delay_s
        self.assertEqual(sells(), [])
        clock.t = decided + 1.5
        r.arms.process()
        self.assertEqual(sells(), [])                                                           # the delay has not elapsed
        clock.t = decided + 3.5
        r.arms.process()
        self.assertEqual(sells(), [])                                                           # elapsed, but the main arm's quote may still come
        clock.t = decided + 4.5
        r.arms.process()
        fill = sells()[0]
        self.assertEqual(fill['price_source'], 'low_lane_exit_quote')
        self.assertAlmostEqual(fill['ts'] - decided, 4.5, places=3)
        self.assertEqual(r.arms.calls['jupiter_exit_quote'], 1)

    def test_a_main_quote_taken_before_the_delay_is_never_the_fill(self):
        run = E.Run(self)
        arms = run.r.arms
        arms.seen_sell_quote('quote:exit', 'M', providers.Quote(1000, 5, None, None, ()), 7, 100)         # q0 of a main exit at t=100
        pend = W.PendingExit(mint='M', open_id=1, decided_at=100.4, due_at=103.4, decided_int=100, qty=1000, reason='STOP', mark=D(1),
                             mark_at=100, mark_source='x', liquidate=False)
        self.assertIsNone(arms._main_quote('M', 1000, pend, 103.5))                                       # q0 (t=100) < the delay (103)
        arms.seen_sell_quote('quote:exit', 'M', providers.Quote(1000, 6, None, None, ()), 8, 103)         # q1 of the same exit
        got = arms._main_quote('M', 1000, pend, 103.6)
        self.assertEqual((got[0].out_amount, got[1], got[3]), (6, 8, 'quote:exit'))
        self.assertIsNone(arms._main_quote('M', 1000, pend, 100 + 3.4 + 11))                              # older than the price ttl
        self.assertIsNone(arms._main_quote('M', 40000, pend, 103.6))                                      # a size far from the quoted one
        self.assertIsNone(arms._main_quote('OTHER', 1000, pend, 103.6))
        arms.seen_sell_quote('quote:buy', 'M', providers.Quote(1, 1, None, None, ()), 9, 104)             # not a sell quote: ignored
        self.assertEqual(len(arms._sell_quotes['M']), 2)


class IdleWakeTests(unittest.TestCase):
    def test_arms_holding_alone_get_their_exits_when_the_main_arm_is_flat(self):
        run = E.Run(self, scenario=[('solo', dict(), 0)])
        run.r.cfg = dataclasses.replace(run.r.cfg, min_market_cap_usd=D('50000000'), max_market_cap_usd=D('90000000'))   # the main gate refuses it
        run.drive(until=T0 + 300, process_arms=False)                                       # nobody but the position pass wakes the arms
        self.assertEqual(run.r.store.rows('fills', limit=10), [])
        self.assertEqual(run.r.store.positions(), {})
        trade = run.trades('hold2m')['solo']
        self.assertEqual(trade['reason'], 'MAX_HOLD')
        self.assertTrue(120 <= trade['hold_s'] <= 120 + 2 * E.STEP, trade['hold_s'])
        d = run.decisions('hold2m', 'solo')[0]
        self.assertEqual((d['action'], d['features']['price_source']), ('BUY', 'low_lane_entry_quote'))     # the main arm quoted nothing
        calls = run.r.arms.calls
        self.assertGreater(calls['helius_marks'], 0)                                        # the mint only an arm holds: batched low-lane reads
        self.assertEqual(run.r.arms.arms[0].marks, {})                                      # hold2m is flat again, no mark is kept


class UnexitableTests(unittest.TestCase):
    def test_an_exit_without_a_route_is_never_filled_at_the_mark_and_is_written_off_in_time(self):
        run = E.Run(self, scenario=[('closed_route', dict(no_route_from=60), 0)])        # the sell route closes 60 s after the entry
        run.r.unexitable_after_s = 300                                                    # (the live value is 2 h)
        run.r.arms.cfg['exit_wait_s'] = 30.0                                              # a TRANSIENT failure would be mark-filled by now
        run.drive(until=T0 + 400)
        hold = run.r.arms.arms[0]
        self.assertEqual(list(hold.pending_exits), list(hold.book.positions))             # still trying: wanted, not quotable, not filled
        self.assertEqual(hold.pending_exits[list(hold.pending_exits)[0]].failing_since is not None, True)
        self.assertEqual([f for f in run.rows('arm_fills', 'hold2m') if f['side'] == 'sell'], [])
        self.assertGreater(run.r.arms.calls['jupiter_exit_quote'], 1)                      # retried (backed off), each failure a counted call
        run.drive(start=T0 + 400 + E.STEP, until=T0 + 700)
        sell = [f for f in run.rows('arm_fills', 'hold2m') if f['side'] == 'sell']
        self.assertEqual(len(sell), 1)
        self.assertEqual((sell[0]['price_source'], sell[0]['sol_lamports'], sell[0]['fee_lamports']), ('write_off', 0, 0))
        trade = run.trades('hold2m')['closed_route']
        self.assertEqual((trade['reason'], trade['proceeds']), ('UNEXITABLE', 0))
        self.assertGreaterEqual(sell[0]['ts'] - T0, 120 + 300)                              # only after unexitable_after_s of trying
        self.assertEqual((hold.counts['write_offs'], hold.pending_exits, hold.book.positions), (1, {}, {}))
        self.assertNotIn('mark_model', {f['price_source'] for f in run.rows('arm_fills')})
        self.assertEqual(run.decisions('hold2m', 'closed_route', kind='exit')[-1]['reasons'], ['UNEXITABLE'])
        self.assertLess(trade['ret'], D('-0.99'))                                           # the whole cost is the loss
        self.assertEqual(hold.book.closed[-1]['state']['reason'], 'UNEXITABLE')
        self.assertGreater(hold.book.closed[-1]['state']['cooldown_until'], sell[0]['ts'])  # and it cools down like a stop


# ---------------------------------------------------------------------------------------------------------------- /arms
class ArmsCommand(unittest.TestCase):
    def test_the_summary_message(self):
        from lean import notify as N
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            lean_db = root / 'lean.sqlite'
            store = Store(lean_db, initial_cash_sol='10', code_version='t', strategy_version='lean-1')
            self.addCleanup(store.close)
            view = N.StoreView(N.open_store(lean_db))
            self.addCleanup(view.close)
            notifier = N.Notifier.__new__(N.Notifier)
            notifier.db = lean_db
            self.assertIn('꺼져', notifier.cmd_arms(view, T0))                                  # no arms.sqlite: off
            arms_store = W.ArmsStore(root / 'arms.sqlite')
            self.addCleanup(arms_store.close)
            for name in ('hold2m', 'live_copy'):
                arm = identity(name)
                arms_store.event(arm, 'arm_init', {'initial_cash_lamports': 10 ** 10}, ts=1.0)
            arm = identity('hold2m')
            for i, (cost, back) in enumerate(((1_000_000_000, 1_100_000_000), (1_000_000_000, 400_000_000))):
                mint = 'M%d' % i
                buy = fill('buy', mint=mint, sol=cost - 50_000, fee=50_000, ts=100.0 + i)
                held = paper.Position(mint, 1000, cost, 100.0, 0, 6)
                arms_store.fill(arm, buy, cash_after=1, held=held, candidate_id=i, price_source='s', flags=[], event='open',
                                state={'stage': 0}, open_fill_id=None)
                opened = arms_store.rows('arm_fills')[-1]['id']
                sell = fill('sell', mint=mint, sol=back + 50_000, fee=50_000, ts=300.0 + i, cost_sold=cost, realized=back - cost)
                arms_store.fill(arm, sell, cash_after=1, held=None, candidate_id=None, price_source='s', flags=[], event='closed',
                                state={'reason': 'MAX_HOLD'}, open_fill_id=opened)
            text = notifier.cmd_arms(view, T0)
            self.assertIn('섀도 암', text)
            self.assertIn('hold2m: 2건', text)
            self.assertIn('EV -25.00%', text)                                                    # (+0.10 - 0.60) / 2 on 1.0 SOL cost
            self.assertIn('승률 50.00%', text)
            self.assertIn('표본부족', text)
            self.assertIn('main: 0건', text)
            self.assertIn('live_copy: 0건', text)
        self.assertEqual(N.Notifier.COMMANDS['/arms'], 'cmd_arms')
        self.assertEqual(set(N.Notifier.COMMANDS), {'/status', '/today', '/pnl', '/arms'})     # /arms is the ONLY command added


if __name__ == '__main__':
    unittest.main()
