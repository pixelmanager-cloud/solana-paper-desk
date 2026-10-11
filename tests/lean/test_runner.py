"""SYNTHETIC_TEST_ONLY: the lean runner and its entry point against the REAL lean modules; only HTTP is faked
(tests/lean/fakeworld.py). Fixtures only, no network, no credentials. The full scenario is tests/lean/test_e2e_real.py."""
import io
import json
import os
import sqlite3
import tempfile
import signal
import threading
import time
import unittest
from decimal import Decimal
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
    token_specs = ({}, {})

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        self.tokens = [Token(31 + 2 * i, **spec) for i, spec in enumerate(self.token_specs)]
        self.world = World(self.root, self.tokens, self.clock)
        self.world.add_frames([T0 + 60 * i for i in range(len(self.tokens))])
        self.cfg = entry.load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        patcher = mock.patch('logging.basicConfig')            # entry.main must not reconfigure logging for the test run
        patcher.start()
        self.addCleanup(patcher.stop)

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
                       'NoNewPrivileges=true', 'ProtectSystem=strict', 'User=solana-desk', 'Restart=always',
                       'WorkingDirectory=/opt/solana-desk-lean'):
            self.assertIn(needed, directives)
        self.assertEqual([d for d in directives if d.startswith('ReadWritePaths=')], ['ReadWritePaths=/var/lib/solana-desk-lean'])
        self.assertIn('--keys-file %d/provider-keys.json', unit)
        self.assertIn('DELETE-journal', unit)                     # the read-only discovery open is documented
        self.assertIn('never immutable=1', unit)
        exec_start = [d for d in directives if d.startswith('ExecStart=')]
        self.assertEqual(len(exec_start), 1)
        self.assertTrue(exec_start[0].startswith('ExecStart=/opt/solana-desk/.venv/bin/python -m lean '))


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


# ------------------------------------------------------------------------------------------------ L09b resilience
def steps(*points):
    def path(age):
        value = Decimal(1)
        for start, m in points:
            if age >= start:
                value = Decimal(str(m))
        return value
    return path


FOREVER = T0 + 10 ** 9


class Driven(Base):
    def drive(self, r, until, step=60, hook=None):
        tick = self.clock.time()
        while tick <= until:
            self.clock.t = max(self.clock.t, tick)
            if hook:
                hook(tick)
            r.position_pass()
            r.candidate_pass()
            self.assertTrue(r.store.check_invariants())
            tick += step

    def closed(self, r):
        roles = {t.mint: i for i, t in enumerate(self.tokens)}
        return {roles[c['mint']]: c for c in r.store.closed_positions()}


class QuoteMarkTests(Driven):
    """D1: with Helius permanently down, a Jupiter sell quote (exit lane) marks the position, so exits still fire."""
    token_specs = ({'path': steps((600, 0.7))}, {'path': steps((300, 1.3))})

    def test_permanent_helius_outage_still_exits_by_stop_and_max_hold_via_quote_marks(self):
        r = self.runner()
        self.drive(r, T0 + 60)
        self.assertEqual(len(r.store.positions()), 2)
        self.world.outages['helius'] = (T0 + 90, FOREVER)
        self.drive(r, T0 + 60 + 21700, step=300)
        closed = self.closed(r)
        self.assertEqual({i: c['state']['reason'] for i, c in closed.items()}, {0: 'STOP', 1: 'MAX_HOLD'})
        self.assertGreaterEqual(closed[1]['ts'] - T0 - 60, 21600)
        exits = [json.loads(d['features']) for d in r.store.rows('decisions', limit=10000) if d['kind'] == 'exit' and d['action'] == 'SELL']
        self.assertEqual({e['mark_source'] for e in exits}, {'quote'})
        self.assertGreater(r.counts['quote_marks'], 10)
        self.assertGreater(r.errors_by_code['HTTP_503'], 10)
        self.assertIsNone(r.halted)


class UnexitableTests(Driven):
    """D2: a wanted exit with a dead route is written off at zero proceeds after unexitable_after_s (across a restart)."""
    token_specs = ({'path': steps((300, 0.7))},)

    def test_dead_route_is_written_off_after_two_hours_and_frees_the_slot(self):
        r = self.runner()
        self.drive(r, T0)
        (position,) = r.store.positions().values()
        self.tokens[0].no_route = True
        self.drive(r, T0 + 3000, step=300)
        (state,) = r.store.position_states().values()
        since = state['state']['exit_failing_since']
        self.assertEqual(state['state']['exit_failing_reason'], 'HTTP_400')
        r.store.close()
        r = self.runner()                                                     # the failure clock survives a restart
        self.drive(r, since + 7200 - 60, step=300)
        self.assertEqual(len(r.store.positions()), 1)                         # not before 2 h
        self.drive(r, since + 7200 + 300, step=300)
        self.assertEqual(r.store.positions(), {})
        (closed,) = r.store.closed_positions()
        self.assertEqual(closed['state']['reason'], 'UNEXITABLE')
        (fill,) = [f for f in r.store.rows('fills') if f['side'] == 'sell']
        self.assertEqual((fill['sol_lamports'], fill['fee_lamports'], fill['realized_lamports']), (0, 0, -position.cost_lamports))
        self.assertEqual(r.store.realized(), -position.cost_lamports)
        self.assertEqual(r.store.cash(), r.store.initial_cash - position.cost_lamports)
        self.assertEqual(r.portfolio(r._now()).open_mints, frozenset())
        self.assertTrue(r.store.check_position_states())


class HaltSemanticsTests(Driven):
    """D4/D9: a halt stops new entries only (exits continue) and an operator clear reaches the running process."""
    token_specs = ({'path': steps((300, 0.7))}, {})

    def test_halted_runner_still_exits_and_a_clear_resumes_entries_without_restart(self):
        r = self.runner()
        self.drive(r, T0)
        r.store.record('halt', {'reason': 'TEST_HALT'}, code_version='x', strategy_version='lean-1')
        with self.assertLogs('lean.runner', 'WARNING'):
            self.drive(r, T0 + 360)
        self.assertEqual(r.halted, 'TEST_HALT')
        self.assertEqual(self.closed(r)[0]['state']['reason'], 'STOP')        # the exit ran while halted
        self.assertEqual(r.store.counts()['candidates'], 1)                   # no new entry
        r.store.record('halt_cleared', {'by': 'operator'}, code_version='x', strategy_version='lean-1')
        self.drive(r, T0 + 420)
        self.assertIsNone(r.halted)
        self.assertEqual(r.store.counts()['candidates'], 2)

    def test_a_halt_is_logged_at_error_and_never_raises_even_if_health_cannot_be_written(self):
        r = self.runner()
        with mock.patch.object(r, 'write_health', side_effect=OSError('disk full')), \
                self.assertLogs('lean.runner', 'ERROR') as logs:
            r._halt('STORE_FAILURE: OperationalError')
        self.assertIn('HALTED', '\n'.join(logs.output))
        self.assertEqual(r.store.halt_reason(), 'STORE_FAILURE: OperationalError')


class StaleMarkTests(Driven):
    """D8: a failed read evicts stale marks; equity then uses a quote-mark or, failing that, cost with a flag."""
    token_specs = ({'path': steps((0, 1.1))},)

    def test_stale_marks_are_evicted_and_the_position_is_valued_at_cost_with_a_flag(self):
        r = self.runner()
        self.drive(r, T0 + 60)
        mint = self.tokens[0].mint
        self.assertEqual(r.marks[mint][2], 'vaults')
        self.world.outages = {'helius': (T0 + 90, FOREVER), 'jupiter': (T0 + 90, FOREVER)}
        self.drive(r, T0 + 180)
        self.assertNotIn(mint, r.marks)
        portfolio = r.portfolio(r._now())
        (position,) = r.store.positions().values()
        self.assertEqual(portfolio.equity, Decimal(r.store.cash()) / 10 ** 9 + Decimal(position.cost_lamports) / 10 ** 9)
        self.assertEqual(r.cost_basis_mints, [mint])
        r.write_health()
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual((health['cost_basis_mints'], health['marks']), ([mint], {}))


class FirstStartTests(Base):
    def test_first_start_skips_history_older_than_the_age_window(self):
        """D7: no cursor file yet -> start at the first frame newer than now - max_age_seconds."""
        self.clock.t = T0 + 20_000
        r = self.runner()
        r.candidate_pass()
        self.assertEqual(C.read_cursor(self.state / 'cursor.json'), 2)
        self.assertEqual(r.store.counts()['candidates'], 0)

    def test_code_version_is_a_content_hash_without_the_env_and_never_unknown(self):
        """D6."""
        with mock.patch.dict(os.environ, {'LEAN_CODE_VERSION': ''}):
            first, second = entry.code_version(), entry.code_version()
        self.assertEqual(first, second)
        self.assertRegex(first, r'^h:[0-9a-f]{64}$')
        with mock.patch.dict(os.environ, {'LEAN_CODE_VERSION': 'abc123'}):
            self.assertEqual(entry.code_version(), 'abc123')
        self.assertNotIn('subprocess', Path(entry.__file__).read_text())


class Boom(BaseException):
    """Escapes ``except Exception``: simulates a loop thread dying."""


class ThreadedRunTests(Driven):
    token_specs = ({'path': steps((300, 0.7))}, {})

    def setUp(self):
        super().setUp()
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))

    def test_real_threaded_run_trades_then_stops_cleanly_on_sigterm(self):
        """D11: Runner.run() with both real loop threads; SIGTERM is delivered to this process."""
        r = self.runner()
        done = threading.Event()

        def clock_and_signal():
            while self.clock.time() < T0 + 900 and not r.dead:
                self.clock.advance(30)
                time.sleep(0.01)
            done.wait(0.05)
            os.kill(os.getpid(), signal.SIGTERM)
        helper = threading.Thread(target=clock_and_signal)
        helper.start()
        clean = r.run(candidate_interval=0.005, position_interval=0.005, install_signals=True)
        helper.join(5)
        self.assertTrue(clean)
        self.assertTrue(r.stop.is_set())
        self.assertEqual(r.dead, {})
        self.assertEqual([t.name for t in threading.enumerate() if t.name in ('candidates', 'positions')], [])
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual(set(health['heartbeat']), {'candidates', 'positions'})
        self.assertTrue(all(health['heartbeat'].values()))
        self.assertGreaterEqual(r.store.counts()['fills'], 1)
        self.assertTrue(r.store.check_invariants())

    def test_a_dead_loop_thread_makes_the_process_exit_non_zero(self):
        """D3: a loop thread that dies stops the other loop; main returns non-zero (systemd Restart=always)."""
        keys = self.root / 'k.json'
        keys.write_text(json.dumps(KEYS)); keys.chmod(0o600)
        with mock.patch.object(R.Runner, 'position_pass', side_effect=Boom()), \
                mock.patch('lean.providers.default_opener', self.world.opener), self.assertLogs('lean.runner', 'CRITICAL'):
            rc = entry.main(['--config', str(ROOT / 'config' / 'lean' / 'lean.example.json'), '--state-dir', str(self.state),
                             '--discovery-db', str(self.world.discovery_db), '--keys-file', str(keys)])
        self.assertEqual(rc, 4)
        health = json.loads((self.state / 'health.json').read_text())
        self.assertEqual(health['dead_loops'], {'positions': 'Boom'})


if __name__ == '__main__':
    unittest.main()
