"""SYNTHETIC_TEST_ONLY: does a held position always get a way out?

Drives one synthetic open position (real transport/collector/engine/ledger/budget/gate code, injected wire bytes
and clock) through exit-quote outages, stale quotes, a stop trigger with the quote source down, a cold restart in
the middle of a blocked exit and the post-exit entry gate. No provider requests.

Two wedges were found on r1-base (reports/T08.md) and are fixed:

* W1 (T08, desk/monitoring_budget.py): a read failure latched ``paper_monitoring_budget.blocked='SOURCE_FAILURE'``
  forever. T22 widened that fix: only an untrustworthy clock still latches.
* W2 (T22, desk/paper_pass_closure.py): every non-COMPLETE held pass that charged a request left
  ``paper_observation_passes.outcome_hash`` NULL and the global gate then refused every later pass. Such passes are
  now closed FAILED_CHARGED (or ABANDONED_CHARGED after a crash).
"""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import json
from email.message import Message
import sqlite3
import unittest
from urllib.error import HTTPError, URLError

from desk import paper_cycle as cycle, paper_terminal_reconciliation as terminal, quote_execution as qe
from desk.monitoring_budget import MonitoringBudget
from tests import test_kraken_lifecycle as kraken

SOL_COST = Decimal('0.01005')          # 0.01 SOL position + 0.00005 SOL fee
STOP_SELL_OUTPUT = 500_000              # lamports; far below the 0.82 stop ratio
STOP_PROCEEDS = Decimal('0.000442525')  # simulated 492525 lamports - 0.00005 fee
STOP_PNL = STOP_PROCEEDS - SOL_COST


class ScriptedCalls(list):
    """Replaces ``http_calls``: records every request, may raise per request."""
    def __init__(self):
        super().__init__()
        self.failure = None   # callable(request) -> exception | None
        self.hook = None      # callable(request), runs before failure injection

    def append(self, request):
        super().append(request)
        if self.hook:
            self.hook(request)
        error = self.failure(request) if self.failure else None
        if error is not None:
            raise error


def is_quote(request):
    return request.method == 'GET' and 'kraken' not in request.full_url and '/price/v3' not in request.full_url


def http_error(status):
    return HTTPError('https://quote.invalid/', status, 'synthetic', Message(), None)


def quote_outage(request):
    # urllib wraps a socket-level OSError in URLError; a bare-string reason is a configuration
    # error (e.g. 'unknown url type') and now latches (T25), so the outage must be a real one.
    return URLError(ConnectionRefusedError('synthetic quote outage')) if is_quote(request) else None


class HeldExitTests(unittest.TestCase):
    def setUp(self):
        self.k = kraken.KrakenLifecycleTests()
        self.k.setUp()
        self.addCleanup(self.k.doCleanups)
        self.h = self.k.h
        self.h.http_calls = ScriptedCalls()
        self.h.sell_output = 10_000_000
        result = kraken.actual_cycle(self.h)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.allowance = self.new_allowance()
        self.allowance.provision()
        self.cash_after_entry = Decimal(self.state()['cash'])

    # -- helpers ---------------------------------------------------------
    def new_allowance(self):
        h = self.h
        return MonitoringBudget(h.f.progress.store, h.path, h.cfg, clock=lambda: h.f.at)

    def state(self):
        return cycle._state(self.h.path, self.h.cfg)

    def position_item(self):
        position = self.state()['positions'][self.h.target.mint]
        return replace(self.h.item, target=replace(self.h.target, amount_raw=qe.raw_quantity(position['qty'], 6)))

    def held(self, *, tick=30):
        h = self.h
        h.f.at += tick
        before = len(h.http_calls)
        result = kraken.actual_cycle(h, positions=(self.position_item(),), candidates=(), monitoring=True)
        return result, len(h.http_calls) - before

    def fills(self):
        with closing(sqlite3.connect(self.h.path)) as c:
            rows = [json.loads(r[0]) for r in c.execute('SELECT payload FROM outcomes ORDER BY seq')]
        return sum(1 for o in rows if o.get('type') == 'fill')

    def passes(self):
        with closing(self.h.f.progress.store.connect()) as c:
            return c.execute('SELECT id,outcome_hash IS NULL FROM paper_observation_passes').fetchall()

    def reservations(self):
        with closing(self.h.f.progress.store.connect()) as c:
            reserved = c.execute('SELECT count(*) FROM paper_monitoring_reservations').fetchone()[0]
            outcomes = c.execute('SELECT count(*) FROM paper_monitoring_outcomes').fetchone()[0]
            blocked = c.execute('SELECT blocked FROM paper_monitoring_budget').fetchone()[0]
        return reserved, outcomes, blocked

    def assert_clean_stop_exit(self, cash_before):
        state = self.state()
        self.assertEqual(state['positions'], {})
        self.assertEqual(Decimal(state['cash']), cash_before + STOP_PROCEEDS)
        self.assertEqual(Decimal(state['realized_pnl']), STOP_PNL)

    # -- W1: transient failures must not latch the monitoring budget -------
    def test_transient_quote_failure_charges_but_does_not_latch_monitoring_budget(self):
        self.h.http_calls.failure = quote_outage
        result, requests = self.held()
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['blockers'], ['TRANSPORT_ERROR'])
        reserved, outcomes, blocked = self.reservations()
        self.assertEqual((reserved, outcomes), (requests, requests))  # failed attempt is charged and retained
        self.assertIsNone(blocked)
        snapshot = self.new_allowance().snapshot()
        self.assertEqual(snapshot['status'], 'AVAILABLE', snapshot)
        self.assertEqual(snapshot['total_used'], requests)

    def test_provider_availability_statuses_are_transient_but_rejections_latch(self):
        for status, latched in ((503, False), (429, False), (403, True), (401, True)):
            with self.subTest(status=status):
                case = HeldExitTests('test_provider_availability_statuses_are_transient_but_rejections_latch')
                case.setUp()
                self.addCleanup(case.doCleanups)
                case.h.http_calls.failure = lambda r, s=status: http_error(s) if is_quote(r) else None
                result, _ = case.held()
                self.assertEqual(result['blockers'], ['HTTP_REJECTED'], result)
                self.assertEqual(case.reservations()[2] == 'SOURCE_FAILURE', latched)

    # T22G: T22's test_no_provider_status_latches_the_monitoring_budget asserted that 401/403 do NOT latch the
    # allowance. That contradicts integration's T25F rules (a provider rejection means the key or plan is wrong and
    # must stop the reads until an operator looks), which test_provider_availability_statuses_are_transient_but_
    # rejections_latch above pins. T25F's rules are kept; the T22 test is removed, not loosened.

    # -- W2 characterisation: a failed held pass must not wedge later passes
    def test_transient_quote_error_then_good_quote_completes_exit(self):
        cash_before = self.cash_after_entry
        self.h.http_calls.failure = quote_outage
        self.h.sell_output = STOP_SELL_OUTPUT
        self.held()
        self.h.http_calls.failure = None
        result, _ = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assert_clean_stop_exit(cash_before)

    def test_transient_quote_error_then_good_quote_charges_exactly_the_attempts(self):
        cash_before = self.cash_after_entry
        self.h.sell_output = STOP_SELL_OUTPUT
        self.h.http_calls.failure = quote_outage
        failed, failed_requests = self.held()
        self.assertEqual(failed['blockers'], ['TRANSPORT_ERROR'])
        self.assertEqual(self.fills(), 1)                       # still only the BUY
        self.assertEqual(self.state()['mode'], 'RUNNING')       # no exit was attempted; nothing latched
        self.h.http_calls.failure = None
        result, requests = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual([o['reason'] for o in result['outcomes'] if o.get('type') == 'fill'], ['STOP'])
        self.assert_clean_stop_exit(cash_before)
        self.assertEqual(self.fills(), 2)                       # BUY + exactly one SELL
        reserved, outcomes, blocked = self.reservations()
        self.assertEqual((reserved, outcomes, blocked), (failed_requests + requests, failed_requests + requests, None))
        self.assertEqual(self.new_allowance().snapshot()['total_used'], failed_requests + requests)

    # -- stale quote ------------------------------------------------------
    def _make_quote_stale(self):
        def age(request):
            if is_quote(request):
                self.h.f.at += 60     # time passes while the quote is being fetched
        self.h.http_calls.hook = age

    def test_stale_quote_is_refused_without_exit_and_without_latching_budget(self):
        self.h.sell_output = STOP_SELL_OUTPUT
        self._make_quote_stale()
        result, requests = self.held()
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['blockers'], ['SOURCE_RESPONSE_STALE'])
        self.assertEqual(self.fills(), 1)
        self.assertEqual(self.reservations(), (requests, requests, None))
        self.assertEqual(self.new_allowance().snapshot()['status'], 'AVAILABLE')

    def test_stale_quote_then_fresh_quote_completes_exit(self):
        cash_before = self.cash_after_entry
        self.h.sell_output = STOP_SELL_OUTPUT
        self._make_quote_stale()
        self.held()
        self.h.http_calls.hook = None
        result, _ = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assert_clean_stop_exit(cash_before)

    def test_stale_quote_then_fresh_quote_charges_exactly_the_attempts(self):
        cash_before = self.cash_after_entry
        self.h.sell_output = STOP_SELL_OUTPUT
        self._make_quote_stale()
        _, stale_requests = self.held()
        self.h.http_calls.hook = None
        result, requests = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assert_clean_stop_exit(cash_before)
        self.assertEqual(self.fills(), 2)
        self.assertEqual(self.reservations(), (stale_requests + requests,) * 2 + (None,))

    # -- stop trigger while the quote source is down for several passes -----
    def test_stop_trigger_with_quote_source_down_charges_each_attempt_and_stays_in_allowance(self):
        cash_before = self.cash_after_entry
        self.h.sell_output = STOP_SELL_OUTPUT
        self.h.http_calls.failure = quote_outage
        charged = 0
        for _ in range(4):
            result, requests = self.held()
            self.assertEqual(result['blockers'], ['TRANSPORT_ERROR'], result)
            self.assertGreater(requests, 0)
            charged += requests
            self.assertEqual(self.fills(), 1)                     # never a phantom exit
            self.assertEqual(len(self.state()['positions']), 1)
            reserved, outcomes, blocked = self.reservations()
            self.assertEqual((reserved, outcomes, blocked), (charged, charged, None))
        snapshot = self.new_allowance().snapshot()
        self.assertEqual(snapshot['status'], 'AVAILABLE', snapshot)
        self.assertEqual((snapshot['total_used'], snapshot['window_used']), (charged, charged))
        self.assertLessEqual(charged, snapshot['cap'])
        self.h.http_calls.failure = None
        result, requests = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assert_clean_stop_exit(cash_before)
        self.assertEqual(self.reservations(), (charged + requests,) * 2 + (None,))
        self.assertLessEqual(self.new_allowance().snapshot()['window_used'], snapshot['cap'])

    def test_exhausted_monitoring_allowance_stops_requests_and_keeps_position_open(self):
        self.h.sell_output = STOP_SELL_OUTPUT
        self.h.http_calls.failure = quote_outage
        cap = self.allowance.snapshot()['cap']
        while self.new_allowance().snapshot()['remaining'] > 0:
            result, requests = self.held()
            if result['blockers'] != ['TRANSPORT_ERROR']:
                break
        self.assertEqual(self.new_allowance().snapshot()['window_used'], cap)
        result, requests = self.held()
        self.assertIn('MONITORING_REQUEST_BUDGET_EXHAUSTED', result['blockers'], result)
        self.assertEqual(requests, 0)                              # no request beyond the allowance
        self.assertEqual(len(self.state()['positions']), 1)
        self.assertEqual(self.reservations()[:2], (cap, cap))

    # -- unfillable exit quote + cold restart --------------------------------
    # A persisted exit_blocked/EXIT_ONLY checkpoint is not reachable from this
    # producer (the engine only persists it for stale marks or an unavailable
    # route); its restart behaviour is covered by
    # tests/test_cloud_ledger_restart.py::test_unsellable_partial_exit_and_recovery_remain_latched_after_restart.
    # What the live producer *can* do is demand a quote it cannot satisfy: the
    # pass raises UNRESOLVED_QUOTE_DEMAND after charging its reads and persists nothing.
    def _block_exit_with_unfillable_quote(self):
        self.h.sell_output = 40_000   # < fixed fee (50_000 lamports): no net proceeds
        result, requests = self.held()
        self.assertEqual(result['blockers'], ['UNRESOLVED_QUOTE_DEMAND'], result)
        state = self.state()
        self.assertEqual(state['mode'], 'RUNNING')
        self.assertIsNone(state['positions'][self.h.target.mint]['exit_blocked'])
        self.assertEqual(self.fills(), 1)
        return requests

    def test_unfillable_exit_quote_survives_cold_restart_without_duplicate_fill_or_charge(self):
        requests = self._block_exit_with_unfillable_quote()
        before = (self.fills(), self.reservations(), self.state())
        self.allowance = None       # cold restart: reopen every store from disk
        self.assertEqual(self.state(), before[2])
        self.assertEqual(self.new_allowance().snapshot()['total_used'], requests)
        self.assertEqual((self.fills(), self.reservations()), before[:2])

    def test_held_pass_after_restart_mid_blocked_exit_completes_exit(self):
        cash_before = self.cash_after_entry
        self._block_exit_with_unfillable_quote()
        self.h.sell_output = STOP_SELL_OUTPUT
        result, _ = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assert_clean_stop_exit(cash_before)

    def test_held_pass_after_restart_mid_blocked_exit_charges_exactly_the_attempts(self):
        cash_before = self.cash_after_entry
        first_requests = self._block_exit_with_unfillable_quote()
        self.allowance = None                                   # cold restart
        self.h.sell_output = STOP_SELL_OUTPUT
        result, requests = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assert_clean_stop_exit(cash_before)
        self.assertEqual(self.fills(), 2)
        self.assertEqual(self.reservations(), (first_requests + requests,) * 2 + (None,))

    # -- does a failed exit attempt block future entries after the exit? ------
    def test_failed_exit_attempt_is_closed_and_does_not_block_later_entries(self):
        """Answer to T08 question 5: a failed exit attempt no longer leaves a NULL pass behind."""
        self.h.sell_output = STOP_SELL_OUTPUT
        self.h.http_calls.failure = quote_outage
        self.held()
        self.h.http_calls.failure = None
        result, _ = self.held()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(self.state()['positions'], {})
        self.assertEqual([p for p in self.passes() if p[1]], [], self.passes())       # no NULL pass remains
        blocked = terminal.gate(self.h.f.progress.store, self.h.f.jobs.path, ('some-new-scan',),
                                ledger_locked=str(self.h.path))
        self.assertIsNone(blocked)


if __name__ == '__main__':
    unittest.main()
