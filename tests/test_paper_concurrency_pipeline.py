"""SYNTHETIC_TEST_ONLY: two REAL dispatcher entries on fresh-start stores with paper_concurrent_entries_version 1.

No mark is refreshed by hand. The stores come from tools.ops.fresh_start.apply (3600/h monitoring), the entries run
the real dispatcher -> acquisition -> intake -> history preparation -> collector -> quotes -> engine pipeline on
the repository's synthetic wire bytes, and the only thing the test controls is how much time passes between the
two decisions (the held-first legs, preparation and quotes of a real tick). No network, credentials or provider.
"""
import copy
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk import paper_cycle as cycle, paper_concurrency as pc, provider_pacing as pace
from desk import paper_read_sources as transport
from desk.model import canonical
from tests import test_ops_fresh_start_lifecycle as lifecycle
from tests import test_paper_entry_dispatcher as base
from tools import paper_entry_dispatcher as tool
from tools.ops import verify_cycle

REAL_TIME = time.time    # captured before any patch: the allowance was provisioned on the real clock


class ConcurrentPipeline(lifecycle.FreshFixture):
    drop_token_profile = True   # the inherited wire fixtures predate token profile 2 (see FreshFixture)
    ttl = 120

    def setUp(self):
        value = json.loads(lifecycle.EXAMPLE.read_text())
        value[pc.KEY], value[pc.TTL_KEY] = 1, self.ttl
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / 'concurrent-experiment.json'
        path.write_text(json.dumps(value))
        with patch.object(lifecycle, 'EXAMPLE', path):
            super().setUp()
        self.assertEqual(pc.selected(self.cfg), 1)
        self.second_raw = None
        self.entered = {}
        self.preflight_at = None      # the tick starts (and preflights) before preparation and quotes elapse

    # -- the second candidate: same dispatcher, a distinct migration notification ---------------------------
    def second_protocol(self, mint):
        """A PoolTests-style fixture for the second mint (same PDA scheme as append_distinct_migration)."""
        from tests.test_pools import PoolTests, PUMP, PUMPSWAP, SOL, TOKEN_PROGRAM, ATA
        proto = PoolTests()
        proto.setUp()
        Pubkey = proto.Pubkey
        proto.mint = Pubkey.from_string(mint)
        proto.creator = Pubkey.find_program_address([b'pool-authority', bytes(proto.mint)], Pubkey.from_string(PUMP))[0]
        proto.pool, proto.bump = Pubkey.find_program_address(
            [b'pool', bytes(2), bytes(proto.creator), bytes(proto.mint), bytes(Pubkey.from_string(SOL))], Pubkey.from_string(PUMPSWAP))
        proto.lp = Pubkey.find_program_address([b'pool_lp_mint', bytes(proto.pool)], Pubkey.from_string(PUMPSWAP))[0]
        proto.vaults = [Pubkey.find_program_address([bytes(proto.pool), bytes(Pubkey.from_string(TOKEN_PROGRAM)), bytes(m)],
                                                    Pubkey.from_string(ATA))[0] for m in [proto.mint, Pubkey.from_string(SOL)]]
        old = proto.raw
        head = old[:8] + bytes([proto.bump]) + bytes(2)
        tail = old[8 + 1 + 2 + 32 * 6:]
        proto.raw = head + b''.join(bytes(x) for x in [proto.creator, proto.mint, Pubkey.from_string(SOL), proto.lp, *proto.vaults]) + tail
        return proto

    def append_second_candidate(self):
        mint = self.append_distinct_migration()
        self.second = self.second_protocol(mint)
        with sqlite3.connect(self.discovery) as c:
            wire = json.loads(c.execute('SELECT payload FROM raw_events ORDER BY seq DESC LIMIT 1').fetchone()[0])
        result = wire['params']['result']
        raw = copy.deepcopy(self.raw)
        raw['transaction'], raw['meta'] = result['transaction']['transaction'], result['transaction']['meta']
        raw['slot'], raw['blockTime'] = result['slot'], result['blockTime']
        self.second_raw = raw
        return mint

    def dispatch(self, *, intents, raw, protocol=None):
        """`DispatcherTests.live` for the nth dispatch (it hard-codes one intent and the first candidate's bytes)."""
        outer = self
        proto = protocol or self.second
        pool_of_candidate = str(proto.pool)

        def setup_rpc(method, params):
            outer.assertEqual(outer.count('intents'), intents, 'Durable intent must precede I/O')
            outer.calls.append(method)
            if method == 'getAccountInfo':
                return {'value': copy.deepcopy(outer.f.protocol.rpc('getMultipleAccounts', [])['value'][6])}
            if method == 'getSlot':
                return 120
            outer.assertEqual(method, 'getTransactionsForAddress')
            return {'data': []}

        class Opener:
            def open(self, request, *, timeout):
                outer.calls.append('intake')
                outer.assertEqual(outer.count('intents'), intents)
                call = json.loads(request.data)
                rows = [raw]
                if call['params'][1]['filters'].get('slot') != {'gte': 10, 'lt': 11}:
                    rows = []
                    for i in range(40):
                        row = base.transaction('dispatch-flow-' + str(i), 10 + i, outer.f.at - 1, quote=200, base=100,
                                               wallet=base.base58((i + 1).to_bytes(32, 'big')))
                        ix = row['transaction']['message']['instructions'][0]
                        ix['data'] = base.base58(base.unbase58(ix['data']).replace(base.unbase58(base.TRADE_POOL),
                                                                                   base.unbase58(pool_of_candidate)))
                        rows.append(row)
                return base.Response(canonical({'jsonrpc': '2.0', 'id': 'paper-read-v1',
                                                'result': {'data': rows, 'paginationToken': None}}).encode())
        original = cycle.run_once
        budget = tool.monitor.MonitoringBudget      # the dispatcher's own allowance reads use the modelled clock too

        def entry_cycle(research, evidence, ledger, cfg, **kw):
            item = kw['candidates'][0]
            view = SimpleNamespace(at=outer.f.at, tick=outer.f.tick, protocol=proto)
            h = SimpleNamespace(f=view, target=item.target, item=item, path=outer.ledger, cfg=outer.cfg,
                                http_calls=[], sell_output=100_000_000, buy_output_raw=10_000_000)

            def run(**args):
                return original(research, evidence, ledger, cfg, wall_clock=lambda: outer.f.at,
                                monotonic=lambda: outer.f.tick, dependency_blockers=(), **args)
            h.run_cycle = run
            result = base.actual_cycle(h, candidates=(item,))
            outer.entered[item.target.mint] = (item, proto)
            return result
        with (patch.object(tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=setup_rpc),
              patch.object(transport, 'build_opener', return_value=Opener()),
              patch.dict('os.environ', {'HELIUS_API_KEY': 'SYNTHETIC', 'JUPITER_API_KEY': 'SYNTHETIC'}),
              patch.object(cycle, 'run_once', side_effect=entry_cycle), patch.object(tool.entry.time, 'sleep'),
              patch.object(tool.time, 'time', side_effect=lambda: self.f.at),
              patch.object(tool.monitor, 'MonitoringBudget',
                           side_effect=lambda store, ledger, cfg: budget(store, ledger, cfg, clock=lambda: max(self.f.at, REAL_TIME()))),
              patch.object(tool.concurrency, 'clock',
                           side_effect=lambda: self.f.at if self.preflight_at is None else self.preflight_at)):
            return self.invoke(execute=True, systemd_credentials=True)

    def first_entry(self):
        result = self.dispatch(intents=1, raw=self.raw, protocol=self.f.protocol)
        self.assertEqual((result['status'], result['paper_status']), ('DISPATCHED', 'COMPLETE'), result)
        state = cycle._state(self.ledger, self.cfg)
        self.assertEqual(len(state['positions']), 1)
        return result, state

    def real_tick_before_entry(self):
        """Idle time, then the held-first legs of a real tick (every open position, oldest mark first), then the
        preparation and bounded cycle that precede the engine decision. Returns the marks the legs wrote."""
        self.f.at += (60 - self.f.at % 60) + 5                      # idle: the marks go stale and a new minute starts
        marks = {}
        for mint in pc.held_order(cycle._state(self.ledger, self.cfg)['positions']):
            self.f.at += 8                                          # one leg is ~7.8 s
            leg = self.held_leg(mint)
            self.assertEqual((leg['status'], leg['monitoring_attempted_requests']), ('COMPLETE', 5), leg)
            marks[mint] = cycle._state(self.ledger, self.cfg)['positions'][mint]['mark_at']
        self.preflight_at = self.f.at                               # the dispatcher (and its pre-I/O estimate) starts here
        self.f.at += 30                                             # preparation (<= 18 s) + bounded cycle before the decision
        return marks

    def monitoring_rows(self):
        with sqlite3.connect(self.exp / 'evidence.sqlite') as c:
            return (c.execute('SELECT COUNT(*) FROM paper_monitoring_reservations').fetchone()[0],
                    c.execute('SELECT COUNT(*) FROM paper_monitoring_outcomes').fetchone()[0])

    def held_leg(self, mint, *, sell_output=82_000_000):
        """One REAL held leg (monitoring cycle, charged to the monitoring allowance) for one open position."""
        from dataclasses import replace
        from desk import quote_execution as qe
        item, proto = self.entered[mint]
        position = cycle._state(self.ledger, self.cfg)['positions'][mint]
        target = replace(item.target, amount_raw=qe.raw_quantity(position['qty'], 6))
        leg_item = replace(item, target=target)
        outer = self
        original = cycle.run_once
        view = SimpleNamespace(at=self.f.at, tick=self.f.tick, protocol=proto)
        h = SimpleNamespace(f=view, target=target, item=leg_item, path=self.ledger, cfg=self.cfg, http_calls=[],
                            sell_output=sell_output, buy_output_raw=10_000_000)
        h.run_cycle = lambda **a: original(self.f.jobs.path, self.f.progress.store.path, self.ledger, self.cfg,
                                           wall_clock=lambda: outer.f.at, monotonic=lambda: outer.f.tick,
                                           dependency_blockers=(), **a)
        return base.actual_cycle(h, positions=(leg_item,), candidates=(), usd_refs=(), monitoring=True)

    def charges(self):
        with self.f.jobs.connect() as c:
            scans = [r[0] for r in c.execute('SELECT id FROM scans ORDER BY rowid')]
        return {s: self.f.progress.admission(s)['requests_used'] for s in scans}

    def null_passes(self):
        return [p for p in self.passes() if p[1] is None]


class ConcurrentEntryFills(ConcurrentPipeline):
    def test_second_entry_fills_after_a_real_held_leg_with_the_mark_as_old_as_the_tick_makes_it(self):
        self.first_entry()
        self.append_second_candidate()
        marks = self.real_tick_before_entry()            # no mark is refreshed by hand: the leg is a real monitoring cycle
        (held_mint, leg_mark), = marks.items()
        second = self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual((second['status'], second['paper_status']), ('DISPATCHED', 'COMPLETE'), second)
        state = cycle._state(self.ledger, self.cfg)
        self.assertEqual(len(state['positions']), 2, 'a concurrent position was entered, not rejected')
        decision_ts = max(p['opened_at'] for p in state['positions'].values())
        age = decision_ts - leg_mark
        self.assertGreater(age, self.cfg['price_ttl_seconds'], 'the freshly refreshed mark was still older than 10 s')
        self.assertLessEqual(age, self.ttl)
        self.assertEqual(self.monitoring_rows(), (5, 5))  # the one leg's five requests, charged and answered
        self.assertEqual(self.null_passes(), [])
        self.assertFalse(self.gate())
        self.assertEqual(len(self.charges()), 2)

    def test_engine_remains_authoritative_when_marks_exceed_the_portfolio_ttl(self):
        """If the pre-I/O estimate is wrong (clock lies), the engine rejects with STALE_PORTFOLIO and nothing latches."""
        self.first_entry()
        self.append_second_candidate()
        self.f.at += 200                                       # far beyond the 120 s TTL
        outer = self
        real = tool.concurrency.entry_blockers
        with patch.object(tool.concurrency, 'entry_blockers', side_effect=lambda *a, **k: real(*a, **{**k, 'now': outer.f.at - 200})):
            second = self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(second['status'], 'DISPATCHED')
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 1)
        reasons = [json.loads(r[0]) for r in sqlite3.connect(self.ledger).execute("SELECT payload FROM outcomes")]
        self.assertTrue(any(o.get('reason') == 'STALE_PORTFOLIO' for o in reasons), reasons)
        self.assertEqual(self.null_passes(), [], 'a normal engine rejection is terminal, never a store-wide latch')
        self.assertFalse(self.gate())

    def snap(self):
        """tools.ops.verify_cycle snapshot of the live stores (read-only, new connections each time)."""
        target = self.exp / 'provider-pacing.sqlite'
        if not target.exists():      # verify_cycle wants every store inside --data; the shared pacing DB is a fixture copy
            shutil.copy(self.pacer, target)
            target.chmod(0o600)
        return verify_cycle.snapshot(str(self.exp), self.ledger.name, str(self.config))

    def test_cold_restart_keeps_every_fill_charge_and_monitoring_reservation(self):
        self.first_entry()
        self.append_second_candidate()
        entry_only = self.snap()
        self.assertEqual((entry_only['status'], entry_only['fill_count']), ('OPEN_POSITION', 1))
        self.real_tick_before_entry()                          # real leg: 5 monitoring reservations
        after_leg = self.snap()
        self.assertEqual(self.monitoring_rows(), (5, 5))
        self.assertEqual(verify_cycle.compare(entry_only, after_leg, allow_progress=True)['status'], 'PASS')
        self.assertEqual(verify_cycle.compare(entry_only, after_leg)['status'], 'FAIL')    # the charges are visible
        # Cold restart: every connection is new; nothing is replayed through the engine or a provider.
        self.assertEqual(verify_cycle.compare(after_leg, self.snap())['status'], 'PASS')
        second = self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(second['paper_status'], 'COMPLETE')
        two_open = self.snap()
        self.assertEqual(two_open['fill_count'], 2)
        self.assertEqual(self.monitoring_rows(), (5, 5), 'an entry spends investigation requests, never monitoring')
        self.assertEqual(verify_cycle.compare(after_leg, two_open, allow_progress=True)['status'], 'PASS')
        self.assertEqual(verify_cycle.compare(two_open, self.snap())['status'], 'PASS')    # restart with two open positions
        # Next tick: one leg per open position, each charged exactly once.
        self.real_tick_before_entry()
        self.assertEqual(self.monitoring_rows(), (15, 15))
        later = self.snap()
        self.assertEqual(later['fill_count'], 2)
        self.assertEqual(verify_cycle.compare(two_open, later, allow_progress=True)['status'], 'PASS')
        self.assertEqual(verify_cycle.compare(later, self.snap())['status'], 'PASS')        # and a last restart


class ConcurrentEntryRefusedBeforeAnyCharge(ConcurrentPipeline):
    ttl = 10   # explicit price-TTL-only experiment: valid, but no entry can fit while a position is held

    def test_doomed_entry_is_refused_before_intent_admission_or_request(self):
        self.first_entry()
        self.append_second_candidate()
        before = (self.charges(), self.count('intents'), self.count('results'))
        self.real_tick_before_entry()      # a REAL leg refreshed the mark moments ago: the old 8 s estimate would pass
        before = (before[0], before[1], before[2])
        with self.assertRaisesRegex(ValueError, 'PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'):
            self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual((self.charges(), self.count('intents'), self.count('results')), before)
        self.assertEqual(self.null_passes(), [])
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 1)


if __name__ == '__main__':
    unittest.main()
