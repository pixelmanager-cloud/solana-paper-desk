"""SYNTHETIC_TEST_ONLY: LINT2 integration (L12F entry features + L13 wallet signals on integration/r1). Real lean modules,
only HTTP faked; no network, no credentials.

* The shipped example turns both features on through the REAL loader and ``python -m lean --once``.
* The LIVE production config (the integration/r1 example with an absolute strategy_config, initial_cash_sol "100",
  screen.min_age_seconds 60, ops.credit_budget_month null) still loads unchanged, and both new features stay OFF in it;
  adding exactly the two example keys turns them on.
"""
import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lean import __main__ as entry, features, wallet_signals
from tests.lean.fakeworld import FakeTime, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'config' / 'lean' / 'lean.example.json'
NEW_KEYS = ('features', 'wallet_signals')
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}


def production_r1():
    """The live config: the integration/r1 example (= today's example without the two LINT2 keys) with the production edits."""
    raw = json.loads(EXAMPLE.read_text())
    for key in NEW_KEYS:
        raw.pop(key)
    raw['strategy_config'] = str(ROOT / 'config' / 'lean' / 'strategy-default.json')
    raw['initial_cash_sol'] = '100'
    raw['screen']['min_age_seconds'] = 60
    raw['ops']['credit_budget_month'] = None
    return raw


class ShippedExample(unittest.TestCase):
    def test_the_real_loader_turns_both_features_on(self):
        raw = json.loads(EXAMPLE.read_text())
        self.assertEqual(set(raw) - {'_comment'}, set(entry.CONFIG_KEYS))
        cfg = entry.load_config(EXAMPLE)
        self.assertEqual(cfg['features'], {'enabled': True, 'enrich': True, 'queue_size': 500})
        ws = wallet_signals.make_config(cfg['wallet_signals'])
        self.assertEqual((ws['retain_raw'], ws['backfill_max_age_s'], ws['max_calls']), ('none', 1800, 16))
        self.assertTrue(cfg['paths']['enabled'])                       # the smart-wallet outcome source is on

    def test_python_m_lean_once_builds_both_and_never_starts_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            world = World(root, [Token(41), Token(43)], FakeTime(T0))
            world.add_frames([T0, T0])
            keys = root / 'provider-keys.json'
            keys.write_text(json.dumps({'HELIUS_API_KEY': KEYS['helius'], 'JUPITER_API_KEY': KEYS['jupiter']}))
            keys.chmod(0o400)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch('lean.providers.default_opener', world.opener), mock.patch('logging.basicConfig'), \
                    redirect_stdout(out), redirect_stderr(err):
                rc = entry.main(['--config', str(EXAMPLE), '--state-dir', str(root / 'state'), '--discovery-db',
                                 str(world.discovery_db), '--keys-file', str(keys), '--once'])
            self.assertEqual(rc, 0, err.getvalue())
            health = json.loads((root / 'state' / 'health.json').read_text())
            self.assertTrue(health['features']['enabled'])
            self.assertEqual(health['features']['calls'], 0)                 # --once never runs the recorder thread
            self.assertEqual(health['wallet_signals']['calls'], 0)
            self.assertFalse((root / 'state' / 'paths.sqlite').exists())     # nor opens paths.sqlite
            self.assertNotIn('TEST-HELIUS-KEY', out.getvalue() + err.getvalue() + json.dumps(health))


class ProductionConfig(unittest.TestCase):
    def write(self, raw, d):
        path = Path(d) / 'lean.json'
        path.write_text(json.dumps(raw))
        return path

    def test_the_live_config_still_loads_unchanged_with_both_features_off(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = entry.load_config(self.write(production_r1(), d))
            self.assertEqual(cfg['features'], features.config(None))
            self.assertFalse(cfg['features']['enabled'])
            self.assertIsNone(cfg['wallet_signals'])
            self.assertIsNone(cfg['ops']['credit_budget_month'])
            self.assertEqual((cfg['initial_cash_sol'], cfg['screen']['min_age_seconds']), ('100', 60))
            world = World(Path(os.path.realpath(d)), [Token(41)], FakeTime(T0))
            r = entry.build_runner(cfg, state_dir=Path(d) / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='lint2',
                                   transport_kwargs={'opener': world.opener})
            self.addCleanup(r.store.close)
            self.assertIsNone(r.features)
            self.assertIsNone(r.wallet_signals)

    def test_adding_exactly_the_two_example_keys_turns_them_on(self):
        shipped = json.loads(EXAMPLE.read_text())
        raw = production_r1()
        for key in NEW_KEYS:
            raw[key] = copy.deepcopy(shipped[key])
        with tempfile.TemporaryDirectory() as d:
            cfg = entry.load_config(self.write(raw, d))
            world = World(Path(os.path.realpath(d)), [Token(41)], FakeTime(T0))
            r = entry.build_runner(cfg, state_dir=Path(d) / 'state', discovery_db=world.discovery_db, keys=KEYS, code_version='lint2',
                                   transport_kwargs={'opener': world.opener})
            self.addCleanup(r.store.close)
            self.assertEqual(r.features.helius.transport.lane, 'low')
            self.assertEqual(r.wallet_signals.helius.transport.lane, 'low')
            self.assertEqual(r.wallet_signals.paths_db, Path(os.path.realpath(d)) / 'state' / 'paths.sqlite')


if __name__ == '__main__':
    unittest.main()
