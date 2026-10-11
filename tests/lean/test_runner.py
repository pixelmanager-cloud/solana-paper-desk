"""SYNTHETIC_TEST_ONLY: the lean runner and its entry point against the REAL lean modules; only HTTP is faked
(tests/lean/fakeworld.py). Fixtures only, no network, no credentials. The full scenario is tests/lean/test_e2e_real.py."""
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from lean import __main__ as entry, candidates as C, paper, runner as R
from lean.paper import AccountingHalt
from lean.store import Store, StoreError
from tests.lean.fakeworld import FakeTime, T0, Token, World

ROOT = Path(__file__).resolve().parents[2]
KEYS = {'helius': 'TEST-HELIUS-KEY-0000', 'jupiter': 'TEST-JUPITER-KEY-0000'}



def setUpModule():
    """No network: every socket connect fails for this module (the HTTP layer is the fake world's opener)."""
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class Base(unittest.TestCase):
    roles = ('a', 'b')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        self.tokens = [Token(31 + 2 * i) for i in range(len(self.roles))]
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames([T0 + 60 * i for i in range(len(self.tokens))])
        self.cfg = entry.load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.runners = []

    def runner(self):
        r = entry.build_runner(self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS,
                               code_version='runner-test', clock=self.clock.time,
                               transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time,
                                                 'monotonic': self.clock.monotonic, 'sleep': self.clock.sleep, 'rng': lambda: 0.5})
        self.addCleanup(r.store.close)
        return r


class EntryPointTests(Base):
    def test_example_config_lists_every_key_the_runner_reads_and_unknown_keys_are_refused(self):
        raw = json.loads((ROOT / 'config' / 'lean' / 'lean.example.json').read_text())
        self.assertEqual(set(raw) - {'_comment'}, set(entry.CONFIG_KEYS))
        self.assertTrue(Path(self.cfg['strategy_config']).is_file())
        bad = self.root / 'bad.json'
        bad.write_text(json.dumps({**raw, 'entry_probe_sol': 0.02}))
        with self.assertRaises(entry.ConfigError):
            entry.load_config(bad)
        bad.write_text(json.dumps({k: v for k, v in raw.items() if k != 'initial_cash_sol'}))
        with self.assertRaises(entry.ConfigError):
            entry.load_config(bad)

    def test_first_start_creates_the_store_with_the_configured_cash_and_a_restart_keeps_it(self):
        r = self.runner()
        self.assertEqual(r.store.initial_cash, 10 * paper.LAMPORTS)
        self.assertEqual((r.cursor, r.halted), (0, None))
        r.store.close()
        again = self.runner()
        self.assertEqual(again.store.initial_cash, 10 * paper.LAMPORTS)
        again.store.close()
        self.cfg['initial_cash_sol'] = '11'
        with self.assertRaises(StoreError):
            self.runner()

    def test_main_loads_production_credential_names_and_never_prints_keys(self):
        secret = 'PROD-SECRET-HELIUS-123456'
        keys = self.root / 'provider-keys.json'
        keys.write_text(json.dumps({'HELIUS_API_KEY': secret, 'JUPITER_API_KEY': 'PROD-SECRET-JUP-9'}))
        keys.chmod(0o400)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('lean.providers.default_opener', self.world.opener), redirect_stdout(out), redirect_stderr(err):
            rc = entry.main(['--config', str(ROOT / 'config' / 'lean' / 'lean.example.json'), '--state-dir', str(self.state),
                             '--discovery-db', str(self.world.discovery_db), '--keys-file', str(keys), '--once'])
        self.assertEqual(rc, 0)
        self.assertTrue((self.state / 'lean.sqlite').is_file())
        self.assertTrue((self.state / 'health.json').is_file())
        everything = out.getvalue() + err.getvalue() + (self.state / 'health.json').read_text()
        self.assertNotIn(secret, everything)
        keys.chmod(0o644)                                         # world-readable: refused, generic message only
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(entry.main(['--config', str(ROOT / 'config' / 'lean' / 'lean.example.json'), '--state-dir',
                                         str(self.state), '--discovery-db', str(self.world.discovery_db), '--keys-file', str(keys)]), 2)
        self.assertNotIn(secret, err.getvalue())
        self.assertIn('KEY_FILE_INVALID', err.getvalue())

    def test_unit_file_hardening(self):
        unit = (ROOT / 'deploy' / 'lean' / 'desk-lean.service').read_text()
        directives = [line.strip() for line in unit.splitlines() if line.strip() and not line.startswith('#')]
        for needed in ('Environment=LEAN_CODE_VERSION=', 'CapabilityBoundingSet=', 'PrivateDevices=true', 'ProtectKernelTunables=true',
                       'ProtectKernelModules=true', 'ProtectKernelLogs=true', 'SystemCallFilter=@system-service',
                       'NoNewPrivileges=true', 'ProtectSystem=strict', 'User=solana-desk', 'Restart=on-failure'):
            self.assertIn(needed, directives)
        self.assertEqual([d for d in directives if d.startswith('ReadWritePaths=')], ['ReadWritePaths=/var/lib/solana-desk-lean'])
        self.assertIn('--keys-file %d/provider-keys.json', unit)
        self.assertIn('-shm', unit)                               # the read-only WAL open is documented


class CandidateLoopTests(Base):
    def test_cursor_is_an_int_from_zero_persisted_with_l03_helpers(self):
        r = self.runner()
        self.assertEqual(r.cursor, 0)
        self.clock.t = T0 + 60
        r.candidate_pass()
        self.assertEqual(C.read_cursor(self.state / 'cursor.json'), 2)
        self.assertEqual(r.cursor, 2)

    def test_discovery_unavailable_is_recorded_and_retried_next_pass(self):
        r = self.runner()
        moved = self.root / 'moved.sqlite'
        os.rename(self.world.discovery_db, moved)
        r.candidate_pass()
        self.assertEqual(r.errors_by_code, {'DISCOVERY_UNAVAILABLE': 1})
        self.assertEqual((r.halted, r.cursor), (None, 0))
        os.rename(moved, self.world.discovery_db)
        r.candidate_pass()
        self.assertEqual(r.cursor, 1)

    def test_kill_switch_stops_entries_but_positions_are_still_managed(self):
        r = self.runner()
        r.candidate_pass()
        self.assertEqual(len(r.store.positions()), 1)
        (self.state / 'KILL').write_text('')
        self.clock.t = T0 + 600
        self.assertEqual(r.candidate_pass(), 0)
        self.assertEqual(r.store.counts()['candidates'], 1)
        before = len(self.world.calls)
        r.position_pass()
        self.assertIn(('helius', 'getMultipleAccounts'), {(p, m) for p, m, _ in self.world.calls[before:]})

    def test_store_failure_while_handling_a_candidate_is_a_persistent_halt(self):
        r = self.runner()
        with mock.patch.object(r.store, 'add_decision', side_effect=sqlite3.OperationalError('disk I/O error')):
            r.candidate_pass()
        self.assertTrue(r.halted.startswith('STORE_FAILURE'), r.halted)
        self.assertEqual(r.store.positions(), {})
        r.store.close()
        again = self.runner()
        self.assertTrue(again.halted.startswith('STORE_FAILURE'))
        self.clock.t = T0 + 120
        self.assertEqual(again.candidate_pass(), 0)                        # still halted after the restart
        self.assertEqual(again.position_pass(), 0)

    def test_accounting_halt_is_persisted_and_an_operator_can_clear_it(self):
        r = self.runner()
        with mock.patch.object(r.store, 'add_fill', side_effect=AccountingHalt('cash does not reconcile')):
            r.candidate_pass()
        self.assertEqual(r.halted, 'ACCOUNTING_INVARIANT: cash does not reconcile')
        r.store.close()
        keys = self.root / 'k.json'
        keys.write_text(json.dumps(KEYS)); keys.chmod(0o600)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = entry.main(['--config', str(ROOT / 'config' / 'lean' / 'lean.example.json'), '--state-dir', str(self.state),
                             '--discovery-db', str(self.world.discovery_db), '--keys-file', str(keys), '--clear-halt'])
        self.assertEqual(rc, 0)
        self.assertIn('HALT_CLEARED', out.getvalue())
        self.assertIsNone(self.runner().halted)

    def test_startup_checks_invariants_a_position_without_state_halts(self):
        store = Store(self.state / 'lean.sqlite', initial_cash_sol='10', code_version='x', strategy_version='lean-1') \
            if self.state.mkdir(mode=0o700) is None else None
        quote = paper.Quote(self.tokens[0].mint, 'buy', 10 ** 8, 10 ** 9, 6, T0)
        store.add_fill(paper.buy(quote, '0.1', paper.PaperConfig()))         # a fill without its position state
        store.close()
        r = self.runner()
        self.assertIn('position state', r.halted)
        self.assertEqual(r.store.halt_reason(), r.halted)
        self.assertEqual(r.position_pass(), 0)

    def test_transient_screen_failure_is_retried_on_the_next_pass(self):
        self.world.outages['helius'] = (T0, T0 + 30)
        r = self.runner()
        r.candidate_pass()
        self.assertEqual(len(r.retry), 1)
        self.assertEqual(r.store.positions(), {})
        self.clock.t = T0 + 60
        r.candidate_pass()
        self.assertEqual(r.retry, [])
        self.assertEqual(len(r.store.positions()), 1)
        actions = [d['action'] for d in r.store.rows('decisions') if d['kind'] == 'screen' and d['mint'] == self.tokens[0].mint]
        self.assertEqual(actions, ['FAILED', 'PASS'])


class HealthTests(Base):
    def test_health_is_written_atomically_under_concurrency_with_no_temp_left(self):
        r = self.runner()
        errors = []

        def write():
            try:
                for _ in range(25):
                    r.write_health()
                    json.loads((self.state / 'health.json').read_text())
                    r._count('errors')
            except Exception as error:              # noqa: BLE001
                errors.append(error)
        threads = [threading.Thread(target=write) for _ in range(6)]
        [t.start() for t in threads]; [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(r.counts['errors'], 150)                          # the shared counter is locked
        self.assertEqual(sorted(p.name for p in self.state.iterdir() if p.name.startswith('.health')), [])
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual((health['kind'], health['execution_status'], health['live_readiness']), ('lean_health_v1', 'EXECUTION_UNVERIFIED', False))

    def test_run_loops_stop_on_signal_and_write_a_final_health(self):
        r = self.runner()
        thread = threading.Thread(target=r.run, kwargs={'candidate_interval': 0.01, 'position_interval': 0.01, 'install_signals': False})
        thread.start()
        while r.last['position_loop'] is None or r.last['candidate_loop'] is None:
            thread.join(0.01)
        r.stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(r.halted)
        self.assertEqual(json.loads((self.state / 'health.json').read_text())['halted'], None)


if __name__ == '__main__':
    unittest.main()
