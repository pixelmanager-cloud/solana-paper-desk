"""SYNTHETIC_TEST_ONLY audit A: permanent gate latches and replay-cost growth on a fresh store.

Fixtures only; no network, no provider keys. Tests marked expectedFailure describe
behaviour a 48 hour unattended fresh-store run needs and the current code does not
provide. Green tests characterise pins that would otherwise surprise the operator.
"""
import json
import os
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from desk import paper_cycle as cycle, paper_read_sources as transport
from desk.paper_checkpoint import RecoveryRequired
from desk import paper_terminal_reconciliation as terminal
from desk import history_preparation_rejection as rejection
from tests import test_history_preparation_phase as prep_fixture
from tests import test_paper_entry_dispatcher as dispatch_fixture
from tests import test_paper_cycle as cycle_fixture
from tests import test_history_first_paper_entry as history_first_fixture
from tools import paper_entry_dispatcher as tool


class DispatcherTransientFault(unittest.TestCase):
    def setUp(self):
        self.h = dispatch_fixture.DispatcherTests('test_dry_run_no_admission_credentials_or_io_and_context_activation')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_single_transient_acquisition_timeout_permanently_kills_dispatcher(self):
        """One OSError/timeout on the very first acquisition RPC (a routine Helius 429/timeout)
        leaves an intent without a result in the dispatcher journal. Even the DRY RUN of every
        later timer tick then raises 'Unresolved dispatch; no retry or automatic recovery', so the
        entry timer is dead until a coordinator authors a reviewed reconciliation. T01 (typed
        no-entry rejections) does not touch this path. Needed: a transient pre-charge/charged
        provider fault must not make a later dry run or a different hint unusable."""
        h = self.h
        with patch.object(tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=OSError('timeout')):
            with self.assertRaises((ValueError, OSError)):
                h.invoke(execute=True, systemd_credentials=True)
        self.assertEqual(h.count('intents'), 1)
        # T22: was `results == 0` (the intent stayed unresolved forever). It is now closed FAILED_CHARGED.
        self.assertEqual(h.count('results'), 1)
        # A healthy provider is back; the timer's next tick must at least be able to run.
        self.assertIn(h.invoke()['status'], ('NO_CANDIDATE', 'DRY_RUN'))


class HistoryFirstTransientFault(unittest.TestCase):
    def setUp(self):
        self.h = history_first_fixture.HistoryFirstTests('test_dry_run_no_credentials_no_history_or_provider_spend')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_transient_history_error_in_preparation_latches_whole_store(self):
        """FRESH store, first history-first candidate. The first history page read raises a
        typed CycleBlocked (what a Helius HTTP 429 becomes). prepare() inserted the NULL pass
        before the read and cycle.run_once does not catch CycleBlocked around prepare(); the
        pass keeps outcome_hash NULL forever and EVERY later tick (any candidate, and the
        held-position monitor, which calls the same terminal.gate) returns
        OBSERVATION_RECOVERY_REQUIRED with zero provider spend. Not a typed rejection, so a
        T01 'no-entry terminal outcome' does not clear it."""
        h = self.h
        h.row['provenance'] = 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'
        h.save()

        def transient(progress, item, *args):
            progress.reserve(item.target.scan_id)
            raise cycle.CycleBlocked('SOURCE_HTTP_429')
        with patch.object(history_first_fixture.tool.cli, '_credentials'), \
                patch.object(history_first_fixture.tool.cycle, '_history', side_effect=transient):
            with self.assertRaises(cycle.CycleBlocked):
                h.invoke(live=True, systemd_credentials=True)
        with closing(h.f.f.progress.store.connect()) as c:
            null_passes = c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0]
        # RED: the transient fault must not leave a permanent unresolved pass.
        self.assertEqual(null_passes, 0)


class PreparationRejectionReplayGrowth(unittest.TestCase):
    """Every retained NO_ENTRY preparation rejection is fully replayed by every terminal.gate."""

    def setUp(self):
        self.h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.rejected()
        self.research = self.h.context['research_db']

    def filler(self, n, start=0):
        for i in range(start, start + n):
            self.h.store.save({'audit_filler': i, 'pad': 'y' * 200})

    def loads_during_gate(self):
        count = [0]
        real = terminal._load

        def counting(*args, **kwargs):
            count[0] += 1
            return real(*args, **kwargs)
        with patch.object(terminal, '_load', side_effect=counting):
            self.assertIsNone(terminal.gate(self.h.store, self.research, ('b' * 32,)))
        return count[0]

    @unittest.expectedFailure
    def test_gate_decodes_every_unrelated_evidence_page_per_retained_rejection(self):
        """rejection._attempts() decompresses+parses EVERY row of the evidence `pages` table to
        find the rejection's 3-6 attempt records, once per retained rejection per gate call:
        O(R*P). Measured here: ~0.17 s per gate at R=1, +0.35 ms per unrelated page. At R=100
        rejections and P=3000 pages that is ~120 s per gate call; the dispatcher calls the gate
        >= 8 times per dispatch (and once per acquisition RPC) under TimeoutStartSec=180, the held
        cycle under TimeoutStartSec=20. Needed: gate cost independent of unrelated page count."""
        base = self.loads_during_gate()
        self.filler(300)
        grown = self.loads_during_gate()
        self.assertEqual(grown, base, 'gate decoded %d extra unrelated pages' % (grown - base))

    @unittest.expectedFailure
    def test_unrelated_pages_beyond_4096_latch_the_gate_permanently(self):
        """With ONE retained preparation rejection, the evidence store reaching 4097 pages (any
        content: history pages, attempts, intents, results; no pruning exists) makes
        rejection._attempts raise 'Preparation attempt inventory bound'. terminal.gate raises,
        paper_cycle maps it to OBSERVATION_RECOVERY_REQUIRED, the dispatcher raises: every entry
        AND every held-position exit is blocked for good, purely by store growth."""
        self.filler(4100)
        self.assertIsNone(terminal.gate(self.h.store, self.research, ('b' * 32,)))


class RetainedCountCaps(unittest.TestCase):
    def test_original_pass_count_bound_is_a_hard_latch(self):
        """Characterisation (green): >10000 rows in paper_observation_passes raise in
        terminal._passes -> gate -> OBSERVATION_RECOVERY_REQUIRED. Each run_once that passes the
        gate adds a row (held-position monitoring too), so unattended cadence consumes it
        (5 min held cycle ~ 288 rows/day while a position is open)."""
        h = prep_fixture.PreparationTests('test_global_gate_retirement_and_unrelated_scan')
        h.setUp()
        self.addCleanup(h.doCleanups)
        with closing(h.store.connect()) as c:
            c.execute('DELETE FROM paper_observation_passes')
            c.executemany('INSERT INTO paper_observation_passes VALUES(?,?,?)',
                          [('%032x' % i, '%064x' % i, '%064x' % i) for i in range(10001)])
        with self.assertRaisesRegex(ValueError, 'pass count bound'):
            terminal.gate(h.store, h.context['research_db'], ())


class PinMismatch(unittest.TestCase):
    def setUp(self):
        self.h = cycle_fixture.PaperCycleTests('test_empty_cycle_and_restart_preserve_new_experiment')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_any_byte_change_under_desk_blocks_every_cycle_until_reviewed_transition(self):
        """Characterisation (green): implementation hash covers every .py/.json byte under
        desk/. A comment-only hotfix (or landing T01 AFTER the fresh ledger is initialised) makes
        read_checkpoint raise RecoveryRequired(RUNTIME_IDENTITY_INVALID) (or CycleBlocked
        SAVED_IMPLEMENTATION_MISMATCH), so run_once blocks before reading, and
        recovery needs an entry in config/runtime-compatibility.json (max 8) plus a coordinator
        transition command. The dispatcher context (source_hash, tool_hash, config_hash, file
        inodes) additionally needs a pinned lineage edge."""
        h = self.h
        real = cycle.Path.read_text

        def edited(self_path, *args, **kwargs):
            text = real(self_path, *args, **kwargs)
            return text + '\n#' if self_path.name == 'paper_cycle.py' else text
        with patch.object(cycle.Path, 'read_text', edited):
            with self.assertRaises(RecoveryRequired) as caught:
                cycle._state(h.path, h.cfg)
        self.assertEqual(str(caught.exception), 'RUNTIME_IDENTITY_INVALID')


class HeldMonitoringTransientFault(unittest.TestCase):
    def setUp(self):
        self.h = cycle_fixture.PaperCycleTests('test_empty_cycle_and_restart_preserve_new_experiment')
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)

    def test_one_transient_monitoring_read_failure_strands_open_position_forever(self):
        """An open paper position, monitoring allowance provisioned. One transport error on the
        held mark read (timeout/429 -> charged failure) sets paper_monitoring_budget.blocked=
        'SOURCE_FAILURE' (monitoring_budget.retain_outcome) AND leaves the cycle pass NULL
        (paper_cycle: outcome only stored when COMPLETE or zero charge). From then on
        terminal.gate (via _monitoring) refuses everything, including the EXIT that would close
        the position and every new entry. Needed: a healthy retry exits the position."""
        h = self.h
        item, allowance = h.monitoring_fixture()

        def failure(*args, **kwargs):
            raise OSError('SYNTHETIC_TEST_ONLY')
        with patch.object(transport, 'build_opener', side_effect=failure), \
                patch.object(transport.os.environ, 'get', return_value='SYNTHETIC_TEST_ONLY'), \
                patch.object(transport.time, 'time', return_value=h.f.at):
            first = h.run_cycle(position_targets=(item,), candidates=(), monitoring=True,
                                source_factory=transport.PaperReadSources)
        self.assertEqual(first['status'], 'BLOCKED')
        # T22: 30_000_000 lamports is a 3x mark, which trips the 1.4x profit ladder and sells only 30%;
        # the scenario needs the healthy retry to EXIT, so the retry quote is a stop-loss quote instead.
        h.sell_output = 500_000
        restart = h.actual_cycle(positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(restart['status'], 'COMPLETE', restart.get('blockers'))
        self.assertEqual(cycle._state(h.path, h.cfg)['positions'], {})


if __name__ == '__main__':
    unittest.main()
