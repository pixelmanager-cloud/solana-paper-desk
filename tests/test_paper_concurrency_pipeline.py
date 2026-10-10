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

        def entry_cycle(research, evidence, ledger, cfg, **kw):
            item = kw['candidates'][0]
            view = SimpleNamespace(at=outer.f.at, tick=outer.f.tick, protocol=proto)
            h = SimpleNamespace(f=view, target=item.target, item=item, path=outer.ledger, cfg=outer.cfg,
                                http_calls=[], sell_output=100_000_000, buy_output_raw=10_000_000)

            def run(**args):
                return original(research, evidence, ledger, cfg, wall_clock=lambda: outer.f.at,
                                monotonic=lambda: outer.f.tick, dependency_blockers=(), **args)
            h.run_cycle = run
            return base.actual_cycle(h, candidates=(item,))
        with (patch.object(tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=setup_rpc),
              patch.object(transport, 'build_opener', return_value=Opener()),
              patch.dict('os.environ', {'HELIUS_API_KEY': 'SYNTHETIC', 'JUPITER_API_KEY': 'SYNTHETIC'}),
              patch.object(cycle, 'run_once', side_effect=entry_cycle), patch.object(tool.entry.time, 'sleep'),
              patch.object(tool.time, 'time', side_effect=lambda: self.f.at),
              patch.object(tool.concurrency, 'clock', side_effect=lambda: self.f.at)):
            return self.invoke(execute=True, systemd_credentials=True)

    def first_entry(self):
        with patch.object(tool.concurrency, 'clock', side_effect=lambda: self.f.at):
            result = self.live()
        self.assertEqual((result['status'], result['paper_status']), ('DISPATCHED', 'COMPLETE'), result)
        state = cycle._state(self.ledger, self.cfg)
        self.assertEqual(len(state['positions']), 1)
        return result, state

    def elapse_one_real_tick(self):
        """Held-first legs + preparation + quotes between the two decisions: 31..90 s, always crossing a minute."""
        delta = (60 - self.f.at % 60) + 30
        self.f.at += delta
        return delta

    def charges(self):
        with self.f.jobs.connect() as c:
            scans = [r[0] for r in c.execute('SELECT id FROM scans ORDER BY rowid')]
        return {s: self.f.progress.admission(s)['requests_used'] for s in scans}

    def null_passes(self):
        return [p for p in self.passes() if p[1] is None]


class ConcurrentEntryFills(ConcurrentPipeline):
    def test_second_entry_fills_while_holding_with_the_marks_as_old_as_a_real_tick_makes_them(self):
        first, state = self.first_entry()
        mark = next(iter(state['positions'].values()))['mark_at']
        self.append_second_candidate()
        delta = self.elapse_one_real_tick()
        second = self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual((second['status'], second['paper_status']), ('DISPATCHED', 'COMPLETE'), second)
        state = cycle._state(self.ledger, self.cfg)
        self.assertEqual(len(state['positions']), 2, 'a concurrent position was entered, not rejected')
        decision_ts = max(p['opened_at'] for p in state['positions'].values())
        self.assertGreater(decision_ts - mark, self.cfg['price_ttl_seconds'], 'the held mark was older than the 10 s price TTL')
        self.assertLessEqual(decision_ts - mark, self.ttl)
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

    def test_cold_restart_between_the_two_entries_changes_no_charge_reservation_or_fill(self):
        self.first_entry()
        self.append_second_candidate()
        before = self.snap()
        self.assertEqual(before['status'], 'OPEN_POSITION')
        self.assertEqual(before['fill_count'], 1)
        # Cold restart: every connection is new and nothing is replayed through the engine or the provider.
        again = self.snap()
        self.assertEqual(verify_cycle.compare(before, again)['status'], 'PASS')
        self.elapse_one_real_tick()
        second = self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(second['paper_status'], 'COMPLETE')
        after = self.snap()
        self.assertEqual(after['fill_count'], 2)
        progress = verify_cycle.compare(again, after, allow_progress=True)
        self.assertEqual((progress['status'], progress['mode']), ('PASS', 'allow_progress'), progress)
        # Strict comparison must notice that something DID change (the second fill and its charges).
        self.assertEqual(verify_cycle.compare(again, after)['status'], 'FAIL')
        # Restart again after the second entry: no duplicate fill or charge appears.
        self.assertEqual(verify_cycle.compare(after, self.snap())['status'], 'PASS')


class ConcurrentEntryRefusedBeforeAnyCharge(ConcurrentPipeline):
    ttl = 10   # explicit price-TTL-only experiment: valid, but no entry can fit while a position is held

    def test_doomed_entry_is_refused_before_intent_admission_or_request(self):
        self.first_entry()
        self.append_second_candidate()
        before = (self.charges(), self.count('intents'), self.count('results'))
        self.elapse_one_real_tick()
        with self.assertRaisesRegex(ValueError, 'PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'):
            self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual((self.charges(), self.count('intents'), self.count('results')), before)
        self.assertEqual(self.null_passes(), [])
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 1)


if __name__ == '__main__':
    unittest.main()
