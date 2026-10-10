"""SYNTHETIC_TEST_ONLY: T23F item 5. The first-BUY entry latch (PAUSE_ENTRY, T07) combined with the opt-in EXIT_ONLY
auto-recovery (F5). Real engine and ledger: every entry path (dispatcher, history-first, direct scheduler) ends in
engine.transition, so the ledger mode is the one place the latch can live."""
import tempfile
import unittest
from pathlib import Path

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from tests.helpers import T, config, control, event


class LatchWithRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.cfg = {**config(), 'paper_exit_only_recovery_version': 1}
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.sqlite'); self.addCleanup(self.ledger.close)

    def apply(self, e):
        return self.ledger.apply(e, self.cfg, transition, initial_state)

    def state(self):
        return self.ledger.report()['state']

    def blocked_open_position(self):
        out = self.apply(event(T, graduated_at=T - 600))
        self.assertTrue(any(o.get('side') == 'buy' for o in out))
        self.apply(event(T + 60, graduated_at=T - 600, route_available=False))      # exit cannot be quoted
        state = self.state()
        self.assertEqual((state['mode'], state['exit_only_cause']), ('EXIT_ONLY', 'UNRESOLVED_EXIT'))

    def clear_the_exit(self):
        self.apply(event(T + 120, graduated_at=T - 600))                            # fresh, routable held event
        self.assertFalse(any(p.get('exit_blocked') for p in self.state()['positions'].values()))

    def second_entry(self):
        self.apply(event(T + 239, graduated_at=T - 600))                            # keep the open position's mark fresh
        return self.apply(event(T + 240, mint='SYNTHETIC_B', graduated_at=T - 600))

    def second_entry_fills(self):
        return any(o.get('side') == 'buy' for o in self.second_entry())

    def test_latch_applied_during_the_engine_pause_survives_the_recovery_on_every_entry_path(self):
        self.blocked_open_position()
        self.apply(control(T + 61, 'PAUSE_ENTRY'))                                  # what entry_latch --apply delivers
        state = self.state()
        self.assertEqual(state['mode'], 'ENTRY_PAUSED')
        self.assertNotIn('exit_only_cause', state)                                  # operator authority replaced the cause
        self.clear_the_exit()
        self.assertEqual(self.state()['mode'], 'ENTRY_PAUSED')                      # recovery never reaches RUNNING
        out = self.second_entry()
        self.assertFalse(any(o.get('side') == 'buy' for o in out))
        self.assertEqual([o['reason'] for o in out if o['type'] == 'reject'], ['ENTRY_PAUSED'])
        self.assertEqual(self.state()['mode'], 'ENTRY_PAUSED')

    def test_latch_applied_before_the_unresolved_exit_is_never_downgraded_to_the_recoverable_pause(self):
        out = self.apply(event(T, graduated_at=T - 600))
        self.assertTrue(any(o.get('side') == 'buy' for o in out))
        self.apply(control(T + 10, 'PAUSE_ENTRY'))
        self.apply(event(T + 60, graduated_at=T - 600, route_available=False))      # exit blocked while latched
        state = self.state()
        self.assertEqual(state['mode'], 'ENTRY_PAUSED')                             # risk() only converts RUNNING
        self.assertNotIn('exit_only_cause', state)
        self.clear_the_exit()
        self.assertEqual(self.state()['mode'], 'ENTRY_PAUSED')
        self.assertFalse(self.second_entry_fills())

    def test_without_the_latch_recovery_reopens_entries_so_the_latch_must_run_before_every_entry_tick(self):
        # Documents the window the latch exists for: ExecStartPre applies it (idempotently) before each entry pass.
        self.blocked_open_position()
        self.clear_the_exit()
        self.assertEqual(self.state()['mode'], 'RUNNING')
        self.assertTrue(self.second_entry_fills())

    def test_a_late_latch_still_wins_over_a_pending_recovery(self):
        self.blocked_open_position()
        self.apply(control(T + 61, 'PAUSE_ENTRY'))
        self.apply(control(T + 62, 'PAUSE_ENTRY'))                                  # idempotent re-delivery
        self.clear_the_exit()
        self.assertEqual(self.state()['mode'], 'ENTRY_PAUSED')


class ConfigLoadValidationTests(unittest.TestCase):
    """T23G item 3/5: a bad versioned flag is refused when the config is LOADED, not discovered mid-cycle."""

    def load(self, **extra):
        import json
        from desk.model import load_config
        path = Path(tempfile.mkdtemp()) / 'config.json'
        path.write_text(json.dumps({**config(), **extra}))
        return load_config(path)

    def test_valid_flags_load_and_absent_flags_stay_off(self):
        self.assertNotIn('paper_exit_only_recovery_version', self.load())
        self.assertEqual(self.load(paper_exit_only_recovery_version=1)['paper_exit_only_recovery_version'], 1)
        self.assertEqual(self.load(paper_entry_latch_version=1)['paper_entry_latch_version'], 1)

    def test_every_lazily_read_flag_refuses_a_bad_value_at_load(self):
        from desk.model import LAZY_VERSION_FLAGS
        for key in LAZY_VERSION_FLAGS:
            for bad in (0, 2, -1, True, False, '1', 1.0, None, [1], {'v': 1}):
                with self.subTest(key=key, value=bad), self.assertRaises(ValueError):
                    self.load(**{key: bad})

    def test_the_engine_would_otherwise_raise_only_inside_a_cycle(self):
        # The same bad value used to load fine and fail in engine.risk(); the loader now agrees with the engine.
        from desk import engine
        for bad in (2, '1', 1.0):
            with self.assertRaises(ValueError):
                engine._exit_only_recovery({'paper_exit_only_recovery_version': bad})
            with self.assertRaises(ValueError):
                self.load(paper_exit_only_recovery_version=bad)

    def test_concurrent_entries_flag_is_validated_with_its_max_positions_at_load(self):
        self.assertEqual(self.load(paper_concurrent_entries_version=1)['paper_concurrent_entries_version'], 1)
        with self.assertRaises(ValueError):
            self.load(paper_concurrent_entries_version=1, max_positions=1)

    def test_the_fresh_example_config_enables_recovery_and_the_latch_and_loads_with_the_real_loader(self):
        from desk.model import load_config
        root = Path(__file__).resolve().parents[1]
        cfg = load_config(root / 'config' / 'experiments' / 'paper-kraken-fresh.example.json')
        self.assertEqual(cfg['paper_exit_only_recovery_version'], 1)
        self.assertEqual(cfg['paper_entry_latch_version'], 1)
        from desk import engine
        self.assertTrue(engine._exit_only_recovery(cfg))


class SchedulerLatchPreCheckTests(unittest.TestCase):
    """T23G item 4: the scheduler's entry pre-check refuses on EVERY entry path once a BUY exists unlatched."""

    def setUp(self):
        import json
        from desk import paper_cycle as cycle
        from tests.test_paper_cycle import PaperCycleTests
        self.fx = PaperCycleTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.cfg = {**self.fx.cfg, 'paper_entry_latch_version': 1}
        self.fx.cfg = self.cfg
        self.fx.path = Path(self.fx.f.tmp.name) / 'latched.sqlite'
        cycle.initialize(self.fx.path, self.cfg)
        self.fx.http_calls = []
        self.fx.sell_output = 10_000_000
        self.root = Path(self.fx.f.tmp.name).resolve()
        self.config_path = self.root / 'latch-config.json'
        self.config_path.write_text(json.dumps(self.cfg))
        lock = self.root / 'paper-scheduler.lock'
        lock.touch(mode=0o600)
        info = lock.stat()
        import os
        from unittest.mock import patch
        env = patch.dict(os.environ, {'DESK_PAPER_SCHEDULER_IDENTITY': f'{info.st_dev}:{info.st_ino}'})
        env.start()
        self.addCleanup(env.stop)

    def run_entry(self, config_path=None):
        import contextlib
        import io
        import json
        from unittest.mock import patch
        from tools import paper_scheduler as wrapper
        research = str(self.fx.f.jobs.path)
        args = ['--research-db', research, '--mode', 'entry', '--', '--research-db', research,
                '--config', str(config_path or self.config_path), '--ledger-db', str(self.fx.path)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('tools.paper_entry_dispatcher.main', side_effect=AssertionError('entry invoked')):
            code = wrapper.main(args)
        return code, json.loads(out.getvalue())

    def buy(self):
        entry = self.fx.actual_cycle()
        self.assertEqual(entry['status'], 'COMPLETE', entry)

    def test_a_persisted_buy_without_the_latch_refuses_entry_and_changes_nothing(self):
        from tests.test_paper_cycle import dump
        self.buy()
        before = dump(self.fx.path)
        code, result = self.run_entry()
        self.assertEqual((code, result['status'], result['latch_status']), (0, 'ENTRY_LATCH_REQUIRED', 'WOULD_PAUSE_ENTRY'))
        self.assertEqual((result['attempted_requests'], result['entry_authorized']), (0, False))
        self.assertEqual(dump(self.fx.path), before)

    def test_after_the_latch_is_applied_the_ordinary_gate_decides_and_the_latch_check_is_quiet(self):
        import io
        import contextlib
        from tools.ops import entry_latch
        self.buy()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(entry_latch.main(['--ledger', str(self.fx.path), '--config', str(self.config_path), '--apply'],
                                              clock=lambda: self.fx.f.at + 5), 0)
        code, result = self.run_entry()
        self.assertEqual(result['status'], 'HELD_POSITION_PRIORITY')     # the legacy refusal, not the latch's

    def test_without_the_config_key_behaviour_is_unchanged(self):
        import json
        from desk import paper_cycle as cycle
        legacy = {k: v for k, v in self.cfg.items() if k != 'paper_entry_latch_version'}
        self.fx.cfg = legacy
        self.fx.path = Path(self.fx.f.tmp.name) / 'legacy.sqlite'
        cycle.initialize(self.fx.path, legacy)
        self.buy()
        path = self.root / 'legacy-config.json'
        path.write_text(json.dumps(legacy))
        self.assertEqual(self.run_entry(path)[1]['status'], 'HELD_POSITION_PRIORITY')

    def test_a_flat_ledger_with_no_buy_proceeds_to_the_dispatcher(self):
        from unittest.mock import patch
        calls = []
        import contextlib
        import io
        from tools import paper_scheduler as wrapper
        research = str(self.fx.f.jobs.path)
        args = ['--research-db', research, '--mode', 'entry', '--', '--research-db', research,
                '--config', str(self.config_path), '--ledger-db', str(self.fx.path)]
        with contextlib.redirect_stdout(io.StringIO()), patch('tools.paper_entry_dispatcher.main', side_effect=lambda a: calls.append(a) or 0):
            self.assertEqual(wrapper.main(args), 0)
        self.assertEqual(len(calls), 1)

    def test_latch_statuses_map_to_refuse_or_allow_and_any_failure_refuses(self):
        from unittest.mock import patch
        from tools import paper_scheduler as wrapper
        from tools.ops import entry_latch
        cases = {'WOULD_PAUSE_ENTRY': 'ENTRY_LATCH_REQUIRED', 'NO_BUY': None, 'NOOP_ENTRY_ALREADY_BLOCKED': None,
                 'NOOP_ALREADY_LATCHED_ONCE': None}                    # a deliberate operator RESUME is not undone
        for status, expected in cases.items():
            with patch.object(entry_latch, 'latch', return_value=(0, {'status': status})):
                got = wrapper._latch_refusal(self.cfg, self.fx.path)
            self.assertEqual(got and got['status'], expected, status)
        with patch.object(entry_latch, 'latch', return_value=(3, {'status': 'LEDGER_BUSY'})):
            self.assertEqual(wrapper._latch_refusal(self.cfg, self.fx.path)['status'], 'ENTRY_LATCH_REQUIRED')
        with patch.object(entry_latch, 'latch', side_effect=ValueError('boom')):
            self.assertEqual(wrapper._latch_refusal(self.cfg, self.fx.path)['status'], 'ENTRY_LATCH_UNAVAILABLE')
        self.assertIsNone(wrapper._latch_refusal({k: v for k, v in self.cfg.items() if k != 'paper_entry_latch_version'}, self.fx.path))


if __name__ == '__main__':
    unittest.main()
