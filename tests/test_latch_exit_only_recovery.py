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


if __name__ == '__main__':
    unittest.main()
