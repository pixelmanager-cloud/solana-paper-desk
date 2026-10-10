"""SYNTHETIC_TEST_ONLY: first-BUY entry latch (tools/ops/entry_latch.py).

Ledgers come from the real quote-paper cycle harness (synthetic wire bytes, no
provider access). Every adversarial case asserts the ledger dump is unchanged
when the tool must not write.
"""
import contextlib
import fcntl
import io
import json
from dataclasses import replace
from pathlib import Path
import sqlite3
import unittest

from desk import engine, paper_cycle as cycle, quote_execution as qe
from desk.ledger import Ledger
from tests import test_paper_cycle as fixtures
from tools.ops import entry_latch


class EntryLatchTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.PaperCycleTests('run_cycle')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.http_calls = []
        self.fx.sell_output = 10_000_000
        self.path = self.fx.path
        self.cfg_path = Path(self.fx.f.tmp.name) / 'latch-config.json'
        self.cfg_path.write_text(json.dumps(self.fx.cfg))
        self.now = self.fx.f.at + 5

    def buy(self):
        entry = self.fx.actual_cycle()
        self.assertEqual(entry['status'], 'COMPLETE', entry)
        self.assertTrue(any(x.get('side') == 'buy' for x in entry['outcomes']))
        return entry

    def run_tool(self, *extra, clock=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = entry_latch.main(['--ledger', str(self.path), '--config', str(self.cfg_path), *extra],
                                    clock=clock or (lambda: self.now))
        return code, json.loads(out.getvalue())

    def control_events(self):
        with sqlite3.connect(self.path) as c:
            return [r[0] for r in c.execute("SELECT event_id FROM events WHERE event_id LIKE 'entry-latch:%'")]

    def mode(self):
        return cycle._state(self.path, self.fx.cfg)['mode']

    def test_no_buy_is_noop_even_with_apply(self):
        before = fixtures.dump(self.path)
        code, report = self.run_tool('--apply')
        self.assertEqual((code, report['status'], report['buy_fills']), (0, 'NO_BUY', 0))
        self.assertEqual(fixtures.dump(self.path), before)

    def test_buy_default_is_read_only_report(self):
        self.buy()
        before = fixtures.dump(self.path)
        code, report = self.run_tool()
        self.assertEqual((code, report['status'], report['mode']), (0, 'WOULD_PAUSE_ENTRY', 'RUNNING'))
        self.assertEqual(report['fill_labels'], ['EXECUTION_UNVERIFIED'])
        self.assertEqual(report['provider_requests'], 0)
        self.assertEqual(fixtures.dump(self.path), before)

    def test_apply_pauses_once_and_is_idempotent(self):
        self.buy()
        code, report = self.run_tool('--apply')
        self.assertEqual((code, report['status'], report['action']), (0, 'LATCHED', 'PAUSE_ENTRY'))
        self.assertEqual(self.mode(), 'ENTRY_PAUSED')
        self.assertEqual(len(self.control_events()), 1)
        after_first = fixtures.dump(self.path)
        code, report = self.run_tool('--apply')
        self.assertEqual((code, report['status']), (0, 'NOOP_ENTRY_ALREADY_BLOCKED'))
        self.assertEqual(fixtures.dump(self.path), after_first)

    def test_held_monitoring_and_exit_still_work_after_latch(self):
        entry = self.buy()
        # The fixture's cycle clock is frozen at f.at; a later control ts would make
        # the (equal-second) held events out of order, which is a harness artifact.
        self.assertEqual(self.run_tool('--apply', clock=lambda: self.fx.f.at)[1]['status'], 'LATCHED')
        position = cycle._state(self.path, self.fx.cfg)['positions'][self.fx.target.mint]
        item = replace(self.fx.item, target=replace(self.fx.target, amount_raw=qe.raw_quantity(position['qty'], 6)))
        refs = tuple(entry['usd_evidence_refs'])
        mark = self.fx.actual_cycle(positions=(item,), candidates=(), usd_refs=refs)
        self.assertEqual(mark['status'], 'COMPLETE', mark)
        self.assertEqual(self.mode(), 'ENTRY_PAUSED')
        self.fx.sell_output = 7_000_000
        exit_result = self.fx.actual_cycle(positions=(item,), candidates=(), usd_refs=refs)
        self.assertEqual(exit_result['status'], 'COMPLETE', exit_result)
        self.assertTrue(any(x.get('side') == 'sell' for x in exit_result['outcomes']))
        final = cycle._state(self.path, self.fx.cfg)
        self.assertEqual((final['positions'], final['mode']), ({}, 'ENTRY_PAUSED'))
        # Flat and paused: nothing further, and no second control is ever written.
        self.assertEqual(self.run_tool('--apply')[1]['status'], 'NOOP_ENTRY_ALREADY_BLOCKED')
        self.assertEqual(len(self.control_events()), 1)

    def test_operator_resume_is_not_overridden_by_second_latch(self):
        self.buy()
        self.run_tool('--apply')
        resume = {'schema_version': 1, 'kind': 'control', 'event_id': 'operator:resume:test',
                  'ts': self.now + 1, 'actor': 'operator', 'command': 'RESUME'}
        ledger = Ledger(self.path, must_exist=True)
        try:
            ledger.apply(resume, self.fx.cfg, qe.bind_transition(resume, ()), engine.initial_state)
        finally:
            ledger.close()
        self.assertEqual(self.mode(), 'RUNNING')
        before = fixtures.dump(self.path)
        code, report = self.run_tool('--apply', clock=lambda: self.now + 10)
        self.assertEqual((code, report['status']), (0, 'NOOP_ALREADY_LATCHED_ONCE'))
        self.assertEqual(fixtures.dump(self.path), before)

    def test_exit_only_is_never_downgraded_to_entry_paused(self):
        self.buy()
        ledger = Ledger(self.path, must_exist=True)
        event = {'schema_version': 1, 'kind': 'control', 'event_id': 'operator:exit-only:test',
                 'ts': self.now, 'actor': 'operator', 'command': 'EXIT_ONLY'}
        try:
            ledger.apply(event, self.fx.cfg, qe.bind_transition(event, ()), engine.initial_state)
        finally:
            ledger.close()
        before = fixtures.dump(self.path)
        code, report = self.run_tool('--apply', clock=lambda: self.now + 1)
        self.assertEqual((code, report['status']), (0, 'NOOP_ENTRY_ALREADY_BLOCKED'))
        self.assertEqual(self.mode(), 'EXIT_ONLY')
        self.assertEqual(fixtures.dump(self.path), before)

    def test_exit_blocked_position_still_ends_with_entries_blocked(self):
        self.buy()
        ledger = Ledger(self.path, must_exist=True)
        clock = {'schema_version': 1, 'kind': 'clock', 'event_id': 'monitor:expire:test',
                 'ts': self.now + 3600, 'actor': 'paper_monitor'}
        try:  # Expired marks with no provider data: engine sets exit_blocked -> EXIT_ONLY.
            ledger.apply(clock, self.fx.cfg, qe.bind_transition(clock, ()), engine.initial_state)
        finally:
            ledger.close()
        state = cycle._state(self.path, self.fx.cfg)
        self.assertTrue(any(p['exit_blocked'] for p in state['positions'].values()))
        self.assertEqual(state['mode'], 'EXIT_ONLY')
        code, report = self.run_tool('--apply', clock=lambda: self.now + 3601)
        self.assertEqual((code, report['status']), (0, 'NOOP_ENTRY_ALREADY_BLOCKED'))
        self.assertEqual(self.mode(), 'EXIT_ONLY')

    def test_clock_behind_ledger_fails_closed_without_write(self):
        self.buy()
        before = fixtures.dump(self.path)
        code, report = self.run_tool('--apply', clock=lambda: 1)
        self.assertEqual((code, report['status']), (2, 'CLOCK_BEHIND_LEDGER'))
        self.assertEqual(fixtures.dump(self.path), before)

    def test_changed_config_fails_closed_without_write(self):
        self.buy()
        before = fixtures.dump(self.path)
        self.cfg_path.write_text(json.dumps({**self.fx.cfg, 'max_positions': 1}))
        code, report = self.run_tool('--apply')
        self.assertEqual((code, report['status']), (2, 'UNAVAILABLE'))
        self.assertEqual(fixtures.dump(self.path), before)
        self.assertNotIn(str(self.path), json.dumps(report))

    def test_busy_cycle_lock_defers_without_write(self):
        self.buy()
        before = fixtures.dump(self.path)
        with open(str(self.path) + '.paper-cycle.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, report = self.run_tool('--apply')
        self.assertEqual((code, report['status']), (3, 'LEDGER_BUSY'))
        self.assertEqual(fixtures.dump(self.path), before)
        self.assertEqual(self.run_tool('--apply')[1]['status'], 'LATCHED')

    def test_symlinked_or_missing_ledger_refused(self):
        self.buy()
        link = Path(self.fx.f.tmp.name) / 'link.sqlite'
        link.symlink_to(self.path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = entry_latch.main(['--ledger', str(link), '--config', str(self.cfg_path), '--apply'])
        self.assertEqual(code, 2)
        self.assertEqual(self.mode(), 'RUNNING')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(entry_latch.main(['--ledger', str(self.path) + '.missing',
                                               '--config', str(self.cfg_path), '--apply']), 2)

    def test_forged_latch_event_does_not_count_for_a_different_buy(self):
        # A pre-existing control with the latch prefix but another buy id must not suppress this latch.
        self.buy()
        forged = {'schema_version': 1, 'kind': 'control', 'event_id': 'entry-latch:v1:not-the-first-buy',
                  'ts': self.now - 1, 'actor': 'operator', 'command': 'RESUME'}
        ledger = Ledger(self.path, must_exist=True)
        try:
            ledger.apply(forged, self.fx.cfg, qe.bind_transition(forged, ()), engine.initial_state)
        finally:
            ledger.close()
        self.assertEqual(self.run_tool('--apply')[1]['status'], 'LATCHED')


if __name__ == '__main__':
    unittest.main()
