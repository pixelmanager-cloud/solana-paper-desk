"""SYNTHETIC_TEST_ONLY: lean.ops (credit tracker, metering proxy, watchdog, ops container, morning report, unit files).
Real lean Store and report modules, a real AF_UNIX datagram socket for sd_notify; no network, no credentials."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from lean import ops
from lean.providers import ProviderError
from lean.store import Store

ROOT = Path(__file__).resolve().parents[2]
T0 = 1_800_000_000.0           # 2027-01-15 08:00:00 UTC


class Clock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t


def tracker(clock=None, **kw):
    return ops.CreditTracker(clock=clock or Clock(), **kw)


class ConfigTests(unittest.TestCase):
    def test_absent_or_disabled_is_off(self):
        self.assertIsNone(ops.load_ops_config(None))
        self.assertIsNone(ops.load_ops_config({'enabled': False}))

    def test_defaults_and_merged_costs(self):
        cfg = ops.load_ops_config({})
        self.assertEqual(cfg['watchdog_stale_s'], 180)
        self.assertEqual(cfg['credit_costs']['helius'], {'default': 1, 'getProgramAccounts': 10})
        cfg = ops.load_ops_config({'credit_costs': {'helius': {'getProgramAccounts': 25, 'getBlock': 2.5}}})
        self.assertEqual(cfg['credit_costs']['helius'], {'default': 1, 'getProgramAccounts': 25, 'getBlock': 2.5})
        self.assertEqual(cfg['credit_costs']['jupiter'], {'default': 0})

    def test_strict_validation(self):
        bad = [{'typo_key': 1}, [], 'x', {'credit_budget_month': 0}, {'credit_budget_month': -5}, {'credit_budget_month': True},
               {'credit_budget_month': float('nan')}, {'watchdog_stale_s': 5}, {'regime_interval_s': 10}, {'regime_interval_s': True},
               {'credit_flush_s': 'x'}, {'housekeeping_s': 0}, {'credit_costs': {'helius': 5}}, {'credit_costs': {'nobody': {}}},
               {'credit_costs': {'helius': {'x': -1}}}, {'credit_costs': {'helius': {'x': True}}},
               {'credit_costs': {'helius': {'x': float('inf')}}}, {'credit_costs': {'helius': {'': 1}}}]
        for raw in bad:
            with self.assertRaises(ops.OpsError, msg=str(raw)):
                ops.load_ops_config(raw)

    def test_watchdog_only_config_is_minimal(self):
        cfg = ops.watchdog_only_config()
        self.assertTrue(cfg['watchdog_only'])


class CreditCountingTests(unittest.TestCase):
    def test_counts_attempts_per_method_and_prices_with_the_table(self):
        t = tracker(costs={'helius': {'default': 1, 'getProgramAccounts': 10}})
        t.record('helius', 'getMultipleAccounts')
        t.record('helius', 'getMultipleAccounts', 3)            # three HTTP attempts of one logical call
        t.record('helius', 'getProgramAccounts')
        t.record('jupiter', 'quote', 2)
        t.record('kraken', 'sol_usd')
        h = t.health()
        self.assertEqual(h['calls'], {'helius': {'getMultipleAccounts': 4, 'getProgramAccounts': 1}, 'jupiter': {'quote': 2}, 'kraken': {'sol_usd': 1}})
        self.assertEqual(h['credits_used'], 4 + 10)               # jupiter and kraken are 0 by default
        self.assertTrue(h['costs_are_estimates'])

    def test_unknown_method_uses_the_provider_default_and_garbage_never_raises(self):
        t = tracker(costs={'helius': {'default': 2}})
        t.record('helius', 'someNewMethod')
        self.assertEqual(t.health()['credits_used'], 2)
        for args in (('nobody', 'x'), ('helius', None), ('helius', 'x', 'many'), ('helius', 'x', -4), (None, None)):
            t.record(*args)                                       # must not raise
        self.assertGreaterEqual(t.health()['credits_used'], 2)

    def test_month_rollover_starts_a_new_budget_window(self):
        clock = Clock(1_801_000_000)                               # late January 2027
        t = tracker(clock)
        t.record('helius', 'a')
        self.assertEqual(t.month, '2027-01')
        clock.t = 1_803_000_000                                   # February
        t.record('helius', 'a')
        self.assertEqual((t.month, t.health()['credits_used']), ('2027-02', 1))

    def test_snapshot_restore_roundtrip_and_other_month_is_ignored(self):
        clock = Clock()
        a = tracker(clock)
        a.record('helius', 'x', 7)
        snap = a.snapshot()
        b = tracker(clock, snapshot=snap)
        self.assertEqual((b.credits, b.calls, b.first_seen), (7.0, {'helius': {'x': 7}}, a.first_seen))
        self.assertEqual(json.loads(json.dumps(snap)), snap)       # plain JSON
        other = tracker(Clock(T0 + 40 * 86400), snapshot=snap)     # restored in a later month
        self.assertEqual(other.credits, 0.0)
        wrong = tracker(clock, snapshot=dict(snap, kind='other'))
        self.assertEqual(wrong.credits, 0.0)

    def test_old_hourly_buckets_are_pruned(self):
        clock = Clock()
        t = tracker(clock)
        t.record('helius', 'x')
        clock.t += 49 * 3600
        t.record('helius', 'x')
        self.assertEqual(len(t.hourly), 1)


class ProjectionTests(unittest.TestCase):
    def feed(self, t, clock, hours, per_hour):
        for _ in range(hours):
            for _ in range(per_hour):
                t.record('helius', 'x')
            clock.t += 3600

    def test_needs_ten_minutes_of_data(self):
        clock = Clock()
        t = tracker(clock, budget=100)
        t.record('helius', 'x', 1000)
        self.assertEqual(t.projection(), (None, 'INSUFFICIENT_DATA'))
        self.assertEqual(t.status()[1], 'INSUFFICIENT_DATA')
        self.assertTrue(t.allow('paths'))                          # unknown rate never sheds
        clock.t += 700
        self.assertEqual(t.projection()[1], 'OK')

    def test_steady_rate_projection_matches_the_arithmetic(self):
        clock = Clock()
        t = tracker(clock)
        self.feed(t, clock, 6, 100)                               # 100 credits/hour for 6 hours
        projected, state = t.projection()
        remaining = ops.month_end(clock.t) - clock.t
        self.assertEqual(state, 'OK')
        self.assertAlmostEqual(projected, 600 + 100 / 3600 * remaining, delta=2.0)

    def test_no_budget_never_sheds(self):
        clock = Clock()
        t = tracker(clock)
        self.feed(t, clock, 3, 10**5)
        self.assertEqual(t.status()[1], 'NO_BUDGET')
        self.assertTrue(all(t.allow(l) for l in ('paths', 'features', 'wallet_signals')))

    def test_shedding_low_priority_lanes_only_with_hysteresis(self):
        clock = Clock()
        shed = []
        t = tracker(clock, budget=30_000, on_shed=shed.append)
        self.feed(t, clock, 3, 100)                               # 100/h for the ~400 h left of the month: ~40k > 30k
        self.assertEqual(t.status()[1], 'SHEDDING')
        for lane in ('paths', 'features', 'wallet_signals'):
            self.assertFalse(t.allow(lane))
        for lane in ('screening', 'exit', 'marks', 'regime', 'anything-else'):
            self.assertTrue(t.allow(lane), lane)                  # screening and exits are never shed
        self.assertEqual(sorted(shed), ['features', 'paths', 'wallet_signals'])     # once per lane and episode
        for _ in range(5):
            t.allow('paths')
        self.assertEqual(sorted(shed), ['features', 'paths', 'wallet_signals'])
        self.assertEqual(t.health()['denied']['paths'], 6)
        # the rate falls below 90% of the budget -> shedding stops, the next episode announces again
        clock.t += 30 * 3600
        t.hourly.clear()
        t.credits = 0.0
        self.assertEqual(t.status()[1], 'OK')
        self.assertTrue(t.allow('paths'))
        self.assertEqual(t.health()['shed_lanes'], [])

    def test_hysteresis_band(self):
        clock = Clock()
        t = tracker(clock, budget=1000)
        self.feed(t, clock, 2, 10)
        projected = t.projection()[0]
        t.budget = projected * 0.95                               # above 90% of the budget, below 100%: not shedding yet
        self.assertEqual(t.status()[1], 'SHEDDING')
        t.budget = projected / 0.92                               # projected is 92% of the budget: stays shedding (>= 90%)
        self.assertEqual(t.status()[1], 'SHEDDING')
        t.budget = projected / 0.85
        self.assertEqual(t.status()[1], 'OK')

    def test_on_shed_failure_is_isolated(self):
        clock = Clock()
        t = tracker(clock, budget=10, on_shed=mock.Mock(side_effect=RuntimeError('boom')))
        self.feed(t, clock, 3, 100)
        self.assertFalse(t.allow('paths'))                         # no exception


class FakeClient:
    def __init__(self, fail=None, attempts=2):
        self.fail, self.attempts = fail, attempts
        self.constant = 'visible'

    def __repr__(self):
        return 'Helius(key=<redacted>)'

    def rpc(self, method, params):
        if self.fail:
            raise self.fail
        return {'ok': True}, b'{}', {'attempts': self.attempts}

    def get_multiple_accounts(self, keys):
        return {'value': []}, b'{}', {'attempts': 1}

    def quote(self, *a):
        return 'q', b'{}', {}

    def _private(self):
        return 'p'


class MeteredTests(unittest.TestCase):
    def setUp(self):
        self.t = tracker(Clock())

    def wrap(self, client=None):
        return ops.Metered(client or FakeClient(), 'helius', self.t)

    def test_counts_rpc_by_method_name_and_attempts_from_meta(self):
        client = self.wrap()
        self.assertEqual(client.rpc('getTokenLargestAccounts', ['m'])[0], {'ok': True})
        client.get_multiple_accounts(['a'])
        client.quote('x')
        self.assertEqual(self.t.calls, {'helius': {'getTokenLargestAccounts': 2, 'getMultipleAccounts': 1, 'quote': 1}})

    def test_failed_calls_are_counted_with_their_attempts_and_reraised_unchanged(self):
        error = ProviderError('HTTP_503', True, meta={'attempts': 3})
        client = self.wrap(FakeClient(fail=error))
        with self.assertRaises(ProviderError) as caught:
            client.rpc('getMultipleAccounts', [])
        self.assertIs(caught.exception, error)
        self.assertEqual(self.t.calls['helius']['getMultipleAccounts'], 3)
        with self.assertRaises(KeyError):
            self.wrap(FakeClient(fail=KeyError('k'))).rpc('x', [])
        self.assertEqual(self.t.calls['helius']['x'], 1)

    def test_passthrough_and_no_secret_in_repr(self):
        client = self.wrap()
        self.assertEqual(client.constant, 'visible')
        self.assertEqual(client._private(), 'p')                   # private names are not wrapped or counted
        self.assertNotIn('_private', self.t.calls.get('helius', {}))
        self.assertEqual(repr(client), 'Helius(key=<redacted>)')
        with self.assertRaises(AttributeError):
            client.missing

    def test_results_are_returned_untouched(self):
        client = self.wrap()
        result = client.rpc('m', [])
        self.assertEqual(result, ({'ok': True}, b'{}', {'attempts': 2}))

    def test_counting_failure_never_breaks_the_call(self):
        broken = mock.Mock()
        broken.record.side_effect = RuntimeError('tracker bug')
        client = ops.Metered(FakeClient(), 'helius', broken)
        # record() of the real tracker is exception-safe; a hostile tracker would propagate, so the proxy must too only for it
        with self.assertRaises(RuntimeError):
            client.rpc('m', [])
        self.t.record = mock.Mock(side_effect=None)
        self.assertEqual(self.wrap().rpc('m', [])[0], {'ok': True})

    def test_meter_wraps_a_providers_bundle(self):
        from lean.providers import Providers
        bundle = Providers(FakeClient(), FakeClient(), FakeClient())
        wrapped = ops.meter(bundle, self.t)
        wrapped.helius.rpc('a', [])
        wrapped.jupiter.quote()
        wrapped.kraken.rpc('b', [])
        self.assertEqual(set(self.t.calls), {'helius', 'jupiter', 'kraken'})
        self.assertIsInstance(wrapped, Providers)


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(os.path.realpath(self.tmp.name), 'notify.sock')
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(self.server.close)
        self.server.bind(self.path)
        self.server.settimeout(2)

    def test_sends_to_a_filesystem_socket(self):
        self.assertTrue(ops.sd_notify('WATCHDOG=1', environ={'NOTIFY_SOCKET': self.path}))
        self.assertEqual(self.server.recv(64), b'WATCHDOG=1')

    @unittest.skipUnless(sys.platform.startswith("linux"), "abstract AF_UNIX sockets are Linux-only (production is Linux)")
    def test_abstract_socket_form(self):
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(abstract.close)
        name = 'lean-test-%d' % os.getpid()
        abstract.bind('\0' + name)
        abstract.settimeout(2)
        self.assertTrue(ops.sd_notify('STOPPING=1', environ={'NOTIFY_SOCKET': '@' + name}))
        self.assertEqual(abstract.recv(64), b'STOPPING=1')

    def test_missing_or_broken_socket_is_a_silent_false(self):
        self.assertFalse(ops.sd_notify('X', environ={}))
        self.assertFalse(ops.sd_notify('X', environ={'NOTIFY_SOCKET': ''}))
        self.assertFalse(ops.sd_notify('X', environ={'NOTIFY_SOCKET': 'relative/path'}))
        self.assertFalse(ops.sd_notify('X', environ={'NOTIFY_SOCKET': self.path + '.gone'}))
        self.assertFalse(ops.sd_notify('X', environ={'NOTIFY_SOCKET': '/proc/self/environ'}))

    def test_interval_from_the_systemd_environment(self):
        self.assertIsNone(ops.watchdog_interval({}))
        self.assertIsNone(ops.watchdog_interval({'WATCHDOG_USEC': '0'}))
        self.assertIsNone(ops.watchdog_interval({'WATCHDOG_USEC': 'abc'}))
        self.assertEqual(ops.watchdog_interval({'WATCHDOG_USEC': '300000000'}), 60.0)    # half of 300 s, capped at 60 s
        self.assertEqual(ops.watchdog_interval({'WATCHDOG_USEC': '20000000'}), 10.0)
        self.assertEqual(ops.watchdog_interval({'WATCHDOG_USEC': '100'}), 1.0)
        self.assertIsNone(ops.watchdog_interval({'WATCHDOG_USEC': '20000000', 'WATCHDOG_PID': str(os.getpid() + 1)}))
        self.assertEqual(ops.watchdog_interval({'WATCHDOG_USEC': '20000000', 'WATCHDOG_PID': str(os.getpid())}), 10.0)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.beats = ops.Heartbeats(self.clock)
        self.sent = []
        self.stalls = []
        self.dog = ops.Watchdog(self.beats, stale_s=100, notify=lambda m: self.sent.append(m) or True,
                                on_stall=lambda loops, ages: self.stalls.append(loops),
                                environ={'WATCHDOG_USEC': '20000000'})

    def test_pings_only_while_both_loops_are_fresh(self):
        self.assertTrue(self.dog.check())
        self.clock.t += 90
        self.beats.beat('candidates'); self.beats.beat('positions')
        self.assertTrue(self.dog.check())
        self.clock.t += 90                                         # both were beaten 90 s ago: still fresh
        self.assertTrue(self.dog.check())
        self.clock.t += 20                                         # 110 s: both stale
        self.assertFalse(self.dog.check())
        self.assertEqual(self.sent, ['WATCHDOG=1'] * 3)
        self.assertEqual(self.dog.status, 'STALLED')

    def test_one_stalled_loop_withholds_the_ping_and_is_reported_once(self):
        for _ in range(4):
            self.clock.t += 60
            self.beats.beat('candidates')                          # positions never beats again
            self.dog.check()
        self.assertEqual(self.sent, ['WATCHDOG=1'])                # only the first, before positions went stale
        self.assertEqual(self.stalls, [['positions']])             # reported once, not every check
        self.assertEqual(self.dog.health()['stalled_loops'], ['positions'])

    def test_recovery_pings_again_and_a_new_stall_is_reported_again(self):
        self.clock.t += 150
        self.beats.beat('candidates')
        self.assertFalse(self.dog.check())
        self.beats.beat('positions')
        self.assertTrue(self.dog.check())
        self.assertEqual(self.dog.status, 'OK')
        self.clock.t += 150
        self.beats.beat('positions')
        self.assertFalse(self.dog.check())
        self.assertEqual(self.stalls, [['positions'], ['candidates']])

    def test_startup_grace_then_stale_without_any_beat(self):
        self.clock.t += 99
        self.assertTrue(self.dog.check())
        self.clock.t += 2
        self.assertFalse(self.dog.check())

    def test_disarmed_without_systemd_never_pings_but_still_tracks_status(self):
        dog = ops.Watchdog(self.beats, stale_s=100, notify=lambda m: self.sent.append(m) or True, environ={})
        self.assertEqual(dog.status, 'DISARMED')
        self.assertFalse(dog.check())
        self.assertEqual(self.sent, [])
        self.clock.t += 200
        dog.check()
        self.assertEqual(dog.status, 'STALLED')

    def test_failed_notify_is_not_counted_and_stall_callback_errors_are_isolated(self):
        dog = ops.Watchdog(self.beats, stale_s=100, notify=lambda m: False, environ={'WATCHDOG_USEC': '20000000'})
        self.assertFalse(dog.check())
        self.assertEqual(dog.pings, 0)
        bad = ops.Watchdog(self.beats, stale_s=1, on_stall=mock.Mock(side_effect=RuntimeError('x')), environ={'WATCHDOG_USEC': '20000000'})
        self.clock.t += 50
        self.assertFalse(bad.check())


class FakeRunner:
    """Just what Ops.start / attach use."""
    def __init__(self, store, clock):
        self.store, self.stop, self.clock = store, threading.Event(), clock
        self.code_version, self.strategy_version = 't', 's'

    def _sol_usd_value(self):
        return __import__('decimal').Decimal('150')


class OpsContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.clock = Clock()
        self.store = Store(self.dir / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s', clock=self.clock)
        self.addCleanup(self.store.close)
        self.sent = []
        self.cfg = ops.load_ops_config({'credit_budget_month': 1000, 'watchdog_stale_s': 60, 'regime_interval_s': 60, 'credit_flush_s': 10})

    def make(self, **kw):
        o = ops.Ops(self.cfg, clock=self.clock, notify=lambda m: self.sent.append(m) or True, environ={'WATCHDOG_USEC': '20000000'})
        o.attach(FakeRunner(self.store, self.clock))
        return o

    def test_flush_throttle_and_restore_across_a_restart(self):
        o = self.make()
        o.credits.record('helius', 'getMultipleAccounts', 5)
        self.assertFalse(o.flush())                                # < 10 s since attach
        self.clock.t += 11
        self.assertTrue(o.flush())
        self.assertFalse(o.flush())                                # not dirty any more
        o.credits.record('helius', 'getMultipleAccounts', 2)       # after the last flush: lost on a crash, bounded by the interval
        again = self.make()                                        # a new process on the same store
        self.assertEqual(again.credits.credits, 5.0)
        self.assertEqual(again.credits.calls, {'helius': {'getMultipleAccounts': 5}})
        o.flush(force=True)
        self.assertEqual(self.make().credits.credits, 7.0)

    def test_housekeeping_samples_the_regime_flushes_and_never_raises(self):
        o = self.make()
        o.credits.record('helius', 'x', 1)
        self.clock.t += 11
        o.housekeeping_once()
        self.assertEqual(len(self.store.rows('observations', kind='regime')), 1)
        self.assertEqual(len(self.store.rows('events', kind='credit_usage')), 1)
        o.housekeeping_once()                                      # not due, not dirty
        self.assertEqual(len(self.store.rows('observations', kind='regime')), 1)
        o.runner = None                                            # a broken runner: still no exception
        o.housekeeping_once()

    def test_shed_and_stall_become_error_rows_once(self):
        o = self.make()
        for _ in range(4):
            self.clock.t += 3600
            o.credits.record('helius', 'x', 5000)
        self.assertFalse(o.credits.allow('paths'))
        self.assertFalse(o.credits.allow('paths'))
        rows = [e for e in self.store.rows('errors') if e['code'] == 'CREDIT_SHED']
        self.assertEqual([(r['scope'], r['message']) for r in rows], [('credits', 'paths')])
        self.clock.t += 200
        o.beat('candidates')
        o.watchdog.check()
        o.watchdog.check()
        stalls = [e for e in self.store.rows('errors') if e['code'] == 'WATCHDOG_STALL']
        self.assertEqual([r['message'] for r in stalls], ['positions'])

    def test_health_is_json_and_carries_every_section(self):
        o = self.make()
        o.beat('candidates')
        o.housekeeping_once()
        h = o.health()['ops']
        json.dumps(h)
        self.assertEqual(set(h), {'credits', 'watchdog', 'regime'})
        self.assertEqual(h['credits']['status'], 'INSUFFICIENT_DATA')
        self.assertEqual(h['watchdog']['status'], 'OK')

    def test_watchdog_only_mode_meters_nothing_and_writes_no_rows(self):
        o = ops.Ops(ops.watchdog_only_config(), clock=self.clock, notify=lambda m: self.sent.append(m) or True,
                    environ={'WATCHDOG_USEC': '20000000'})
        o.attach(FakeRunner(self.store, self.clock))
        bundle = types.SimpleNamespace(helius=1)
        self.assertIs(o.meter(bundle), bundle)
        self.clock.t += 100
        o.housekeeping_once()
        self.assertEqual(self.store.rows('observations', kind='regime'), [])
        self.assertFalse(o.flush(force=True))
        self.assertEqual(self.store.rows('events', kind='credit_usage'), [])

    def test_real_threads_ping_and_say_stopping(self):
        server_dir = tempfile.mkdtemp(prefix='lean-wd-')
        self.addCleanup(shutil.rmtree, server_dir, True)
        path = os.path.join(server_dir, 's')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(server.close)
        server.bind(path)
        server.settimeout(5)
        cfg = dict(self.cfg, housekeeping_s=1)
        o = ops.Ops(cfg, clock=time.time, environ={'WATCHDOG_USEC': '2000000', 'NOTIFY_SOCKET': path},
                    notify=lambda m: ops.sd_notify(m, environ={'NOTIFY_SOCKET': path}))
        runner = FakeRunner(self.store, time.time)
        o.attach(runner)
        threads = o.start(runner)
        self.assertEqual(len(threads), 2)
        self.assertEqual(server.recv(64), b'WATCHDOG=1')
        runner.stop.set()
        for t in threads:
            t.join(5)
            self.assertFalse(t.is_alive())
        messages = []
        server.settimeout(0.5)
        try:
            while True:
                messages.append(server.recv(64))
        except socket.timeout:
            pass
        self.assertIn(b'STOPPING=1', messages)


# -------------------------------------------------------------------------------------------- morning report
def make_store(directory, clock):
    store = Store(Path(directory) / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='lean-1', clock=clock)
    store.add_candidate('M' * 43 + '1', pool='P', ts=clock())
    store.add_decision('screen', 'REJECT', mint='M' * 43 + '1', reasons=['LIQUIDITY_BELOW_MIN'], features={}, ts=clock())
    return store


class ReportDateTests(unittest.TestCase):
    def test_date_is_the_kst_calendar_date(self):
        import calendar
        self.assertEqual(ops.report_date(calendar.timegm((2026, 10, 10, 23, 0, 0))), '2026-10-11')     # 08:00 KST next day
        self.assertEqual(ops.report_date(calendar.timegm((2026, 10, 10, 14, 59, 59))), '2026-10-10')
        self.assertEqual(ops.report_date(calendar.timegm((2026, 10, 10, 15, 0, 0))), '2026-10-11')
        self.assertEqual(ops.report_date(calendar.timegm((2026, 12, 31, 23, 0, 0))), '2027-01-01')


class MorningReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.clock = Clock(T0)
        self.store = make_store(self.dir, self.clock)
        self.addCleanup(self.store.close)
        self.out = self.dir / 'reports'
        self.db = self.dir / 'lean.sqlite'

    def test_writes_the_dated_page_and_json_from_the_l06_report(self):
        result = ops.morning_report(self.db, self.out, now=T0)
        page = self.out / (ops.report_date(T0) + '.html')
        self.assertEqual((result['status'], result['html']), ('OK', str(page)))
        text = page.read_text()
        self.assertIn('Lean desk report', text)
        self.assertIn('LIQUIDITY_BELOW_MIN', text)
        data = json.loads((self.out / (ops.report_date(T0) + '.json')).read_text())
        self.assertEqual(data['morning']['date_kst'], ops.report_date(T0))
        self.assertEqual(oct(page.stat().st_mode & 0o777), '0o600')
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), '0o700')

    def test_rerun_the_same_day_keeps_the_page(self):
        ops.morning_report(self.db, self.out, now=T0)
        page = self.out / (ops.report_date(T0) + '.html')
        before = page.read_bytes()
        again = ops.morning_report(self.db, self.out, now=T0 + 60)
        self.assertEqual(again['status'], 'EXISTS')
        self.assertEqual(page.read_bytes(), before)
        later = ops.morning_report(self.db, self.out, now=T0 + 86400)
        self.assertEqual(later['status'], 'OK')

    def test_symlinked_or_unwritable_out_dir_is_refused(self):
        target = self.dir / 'elsewhere'
        target.mkdir()
        link = self.dir / 'link'
        link.symlink_to(target)
        with self.assertRaises(ops.OpsError):
            ops.morning_report(self.db, link, now=T0)
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_store_is_an_error_and_writes_nothing(self):
        with self.assertRaises(Exception):
            ops.morning_report(self.dir / 'missing.sqlite', self.out, now=T0)
        self.assertEqual(list(self.out.glob('*.html')), [])

    def test_the_store_is_only_read(self):
        before = self.store.counts()
        ops.morning_report(self.db, self.out, now=T0)
        self.assertEqual(self.store.counts(), before)

    def test_replay_page_only_with_l08_and_a_grid(self):
        # no grid
        result = ops.morning_report(self.db, self.out, now=T0)
        self.assertIn('no --grid', result['replay'])
        # grid but L08 absent
        grid = self.dir / 'grid.json'
        grid.write_text('{"stop_fraction": [0.1]}')
        with mock.patch.dict(sys.modules, {'lean.tune': None}):
            result = ops.morning_report(self.db, self.out, now=T0 + 86400, grid=grid)
        self.assertIn('not installed', result['replay'])
        # L08 present
        def fake_main(argv):
            out = Path(argv[argv.index('--out-dir') + 1])
            self.assertIn('--calibrate', argv)
            (out / 'tune.html').write_text('<html>REPLAY PAGE</html>')
            print(json.dumps({'status': 'OK'}))
            return 0
        with mock.patch.dict(sys.modules, {'lean.tune': types.SimpleNamespace(main=fake_main)}):
            result = ops.morning_report(self.db, self.out, now=T0 + 2 * 86400, grid=grid)
        date = ops.report_date(T0 + 2 * 86400)
        self.assertEqual(result['replay'], 'ok')
        self.assertEqual((self.out / (date + '-replay.html')).read_text(), '<html>REPLAY PAGE</html>')
        self.assertIn('href="%s-replay.html"' % date, (self.out / (date + '.html')).read_text())
        self.assertEqual([p.name for p in self.out.iterdir() if p.is_dir()], [])      # scratch dir removed

    def test_a_failing_replay_never_blocks_the_main_report(self):
        grid = self.dir / 'grid.json'
        grid.write_text('{}')
        for day, main in enumerate((lambda argv: 2, mock.Mock(side_effect=RuntimeError('boom')), lambda argv: 0), start=1):
            with mock.patch.dict(sys.modules, {'lean.tune': types.SimpleNamespace(main=main)}):
                result = ops.morning_report(self.db, self.out, now=T0 + 86400 * day, grid=grid)
            self.assertEqual(result['status'], 'OK')
            self.assertNotEqual(result['replay'], 'ok')

    def test_cli(self):
        out = subprocess.run([sys.executable, '-m', 'lean.ops', 'report', '--db', str(self.db), '--out-dir', str(self.out), '--now', str(T0)],
                             capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)['status'], 'OK')
        bad = subprocess.run([sys.executable, '-m', 'lean.ops', 'report', '--db', str(self.dir / 'nope.sqlite'), '--out-dir', str(self.out)],
                             capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(json.loads(bad.stdout)['status'], 'ERROR')


class UnitFileTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / 'deploy' / 'lean' / name).read_text()

    def test_timer_fires_at_0800_kst(self):
        timer = self.read('desk-lean-report.timer')
        self.assertIn('OnCalendar=*-*-* 23:00:00 UTC', timer)
        self.assertIn('Unit=desk-lean-report.service', timer)
        self.assertIn('WantedBy=timers.target', timer)
        if shutil.which('systemd-analyze'):
            out = subprocess.run(['systemd-analyze', 'calendar', '*-*-* 23:00:00 UTC'], capture_output=True, text=True,
                                 env=dict(os.environ, TZ='Asia/Seoul')).stdout
            self.assertRegex(out, r'Next elapse: \w+ \d{4}-\d\d-\d\d 08:00:00 KST')

    def test_report_service_is_a_sandboxed_no_network_oneshot_without_notifications(self):
        service = self.read('desk-lean-report.service')
        for line in ('Type=oneshot', 'User=solana-desk', 'ProtectSystem=strict', 'ReadWritePaths=/var/lib/solana-desk-lean',
                     'PrivateNetwork=true', 'NoNewPrivileges=true', 'CapabilityBoundingSet=', 'UMask=0077'):
            self.assertIn(line, service)
        exec_line = next(l for l in service.splitlines() if l.startswith('ExecStart='))
        self.assertIn('-m lean.ops report', exec_line)
        self.assertIn('--out-dir /var/lib/solana-desk-lean/reports', exec_line)
        for forbidden in ('mail', 'curl', 'telegram', 'webhook', 'LoadCredential'):
            self.assertNotIn(forbidden, service.lower())

    def test_main_unit_has_the_watchdog(self):
        service = self.read('desk-lean.service')
        self.assertIn('WatchdogSec=300', service)
        self.assertIn('NotifyAccess=main', service)
        self.assertIn('Restart=on-failure', service)


if __name__ == '__main__':
    unittest.main()
