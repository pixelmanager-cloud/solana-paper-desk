"""SYNTHETIC_TEST_ONLY: the discovery gap backfill end to end with the REAL runner, screen, store and providers (only HTTP is faked).

World (tests/lean/fakeworld.py): 7 migrations on the authority address, in time order:
  old     3 h old, in neither the feed nor ... (the feed missed it; found by the backfill far beyond max_age_seconds)
  f1, f2  the discovery feed stored these (source_id 'confirmed:<signature>', the listener's own format)
  PLAIN   an unrelated transaction that mentions the authority (not a migration)
  m1, m2  the feed MISSED these: only the backfill can find them
  hz      the feed missed it, and its mint authority is still active (a hazard)
The backfill polls getSignaturesForAddress / getTransaction through the fake Helius (extra_rpc) on the real LOW lane.

Proves: missed graduations are entered as source 'backfill' with their detection latency; signatures the feed has are never fetched
(no credit); a graduation older than max_age is recorded and rejected TOO_OLD, never entered; a hazard is rejected as usual; the gap
is reported in health; a restart neither re-fetches nor duplicates; with the key absent nothing changes (default OFF).
"""
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from desk.model import canonical, digest
from lean import backfill as B
from lean.__main__ import build_runner, load_config
from tests.lean.fakeworld import FakeTime, T0, Token, World
from tests.lean.test_backfill import plain_tx, signature_of, tx_for
from tests.lean.test_e2e_real import KEYS

ROOT = Path(__file__).resolve().parents[2]
END = T0 + 1500


def setUpModule():
    import socket
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class BackfillEndToEnd(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.state = self.root / 'state'
        self.clock = FakeTime(T0)
        names = ['old', 'f1', 'f2', 'm1', 'm2', 'hz']
        self.tokens = {n: Token(41 + 2 * i, **({'hazard': 'mint_authority'} if n == 'hz' else {})) for i, n in enumerate(names)}
        self.role = {t.mint: n for n, t in self.tokens.items()}
        self.world = World(self.root, list(self.tokens.values()), self.clock)
        # (name, slot, blockTime): migrations are `ready` = blockTime + 300 s after the block
        self.chain = [('old', 1000, T0 - 10_800), ('f1', 1100, T0 - 300), ('PLAIN', 1101, T0 - 200), ('f2', 1200, T0 - 240),
                      ('m1', 1300, T0 - 180), ('m2', 1400, T0 - 60), ('hz', 1500, T0)]
        self.chain.sort(key=lambda row: row[1])
        self.txs = {}
        for name, slot, block in self.chain:
            if name == 'PLAIN':
                self.txs['PLAIN'] = plain_tx('PLAIN', slot=slot, block_time=block)
            else:
                self.txs[signature_of(self.tokens[name])] = tx_for(self.tokens[name], slot=slot, block_time=block)
        self.store_feed(['f1', 'f2'])
        self.world.extra_rpc['getSignaturesForAddress'] = self.rpc_signatures
        self.world.extra_rpc['getTransaction'] = lambda params, t: self.txs.get(params[0])
        self.cfg = load_config(ROOT / 'config' / 'lean' / 'lean.example.json')
        self.cfg['backfill'] = B.make_config({'interval_s': 30})

    def store_feed(self, names):
        """What the discovery listener stored for these migrations (its own wire format and source_id)."""
        with sqlite3.connect(self.world.discovery_db) as c:
            for seq, name in enumerate(names, start=1):
                sig, tx = signature_of(self.tokens[name]), None
                tx = self.txs[sig]
                wire = {'method': 'transactionNotification', 'params': {'result': {
                    'signature': sig, 'slot': tx['slot'], 'blockTime': tx['blockTime'],
                    'transaction': {'transaction': tx['transaction'], 'meta': tx['meta']}}}}
                c.execute('INSERT INTO raw_events(seq,source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?,?)',
                          (seq, 'confirmed:' + sig, tx['blockTime'] + 2, tx['slot'], canonical(wire), digest(wire)))

    def rpc_signatures(self, params, t):
        address, options = params
        assert address == B.AUTHORITY
        rows = [{'signature': sig, 'slot': tx['slot'], 'blockTime': tx['blockTime'], 'err': None, 'memo': None,
                 'confirmationStatus': 'confirmed'} for sig, tx in self.txs.items() if tx['blockTime'] <= t]
        rows.sort(key=lambda r: -r['slot'])
        ids = [r['signature'] for r in rows]
        if options.get('before'):
            rows = rows[ids.index(options['before']) + 1:]
        if options.get('until') and options['until'] in ids:
            rows = rows[:[r['signature'] for r in rows].index(options['until'])]
        return rows[:options.get('limit', 1000)]

    def runner(self, cfg=None):
        return build_runner(cfg or self.cfg, state_dir=self.state, discovery_db=self.world.discovery_db, keys=KEYS,
                            code_version='e2e-backfill', clock=self.clock.time,
                            transport_kwargs={'opener': self.world.opener, 'clock': self.clock.time, 'monotonic': self.clock.monotonic,
                                              'sleep': self.clock.sleep, 'rng': lambda: 0.5})

    def run_until(self, r, end, start=None):
        tick = start or T0
        while tick <= end:
            if self.clock.time() < tick:
                self.clock.t = tick
            r.position_pass()
            r.candidate_pass()
            r.write_health()
            self.assertIsNone(r.halted, r.halted)
            self.assertTrue(r.store.check_invariants())
            tick += 60
        return tick

    def facts(self, r):
        store = r.store
        meta = {self.role[c['mint']]: json.loads(c['meta']) for c in store.rows('candidates', limit=1000)}
        screens = {}
        for d in store.rows('decisions', limit=10000):
            if d['kind'] == 'screen':
                screens[self.role[d['mint']]] = (d['action'], tuple(json.loads(d['reasons'])))
        entered = {self.role[f['mint']] for f in store.rows('fills', limit=1000) if f['side'] == 'buy'}
        fetched = [p[0] for method, p in getattr(self, 'rpc_log', [])]
        return meta, screens, entered

    def fetched_signatures(self):
        return [t for provider, method, t in self.world.calls if provider == 'helius' and method == 'getTransaction']

    # ------------------------------------------------------------------------------------------------------------
    def test_missed_graduations_are_found_labelled_and_entered_and_the_rest_is_untouched(self):
        r = self.runner()
        self.run_until(r, END)
        meta, screens, entered = self.facts(r)

        # the feed's own candidates follow the old path: no backfill label, normal entries
        self.assertEqual(set(meta), {'old', 'f1', 'f2', 'm1', 'm2', 'hz'})
        for name in ('f1', 'f2'):
            self.assertEqual(meta[name]['source'], 'pump_graduation', name)       # lean.watchlist.install tags the default source
            self.assertNotIn('latency_s', meta[name], name)
            self.assertEqual(screens[name][0], 'PASS')
        # the missed ones come from the backfill, with their detection latency (>= min age: the pass waited for it)
        for name, block in (('m1', T0 - 180), ('m2', T0 - 60), ('hz', T0), ('old', T0 - 10_800)):
            self.assertEqual(meta[name]['source'], 'backfill', name)
            self.assertGreaterEqual(meta[name]['latency_s'], 300, name)
            self.assertEqual(meta[name]['beyond_max_age'], name == 'old', name)
        self.assertEqual(entered, {'f1', 'f2', 'm1', 'm2'})
        # beyond max_age: recorded, rejected by the screen's own age window, never entered
        self.assertEqual(screens['old'], ('REJECT', ('TOO_OLD',)))
        # a hazard found by the backfill is rejected like any other
        self.assertEqual(screens['hz'][0], 'REJECT')
        self.assertIn('ACTIVE_MINT_AUTHORITY', screens['hz'][1])
        self.assertEqual(r.store.counts()['candidates'], 6)
        self.assertEqual(r.store.positions().keys() == {t.mint for n, t in self.tokens.items() if n in entered}, True)

    def test_signatures_the_feed_has_are_never_fetched(self):
        r = self.runner()
        self.run_until(r, END)
        calls = [c for c in self.world.calls if c[0] == 'helius' and c[1] == 'getTransaction']
        self.assertEqual(len(calls), 5)                                           # old, PLAIN, m1, m2, hz — not f1, f2
        self.assertGreaterEqual(sum(1 for c in self.world.calls if c[1] == 'getSignaturesForAddress'), 10)

    def test_the_gap_is_reported_in_health(self):
        r = self.runner()
        self.run_until(r, END)
        health = json.loads((self.state / 'health.json').read_text())
        today = health['sources']['backfill']['today']
        self.assertEqual((today['authority_txs'], today['seen_by_feed'], today['missing_from_feed'], today['missed_migrations'],
                          today['not_migration'], today['beyond_max_age']), (7, 2, 5, 4, 1, 1))
        self.assertAlmostEqual(today['gap_rate'], 5 / 7)
        self.assertEqual(today['credits_estimate'], today['calls_signatures'] + today['calls_transaction'])
        self.assertEqual(health['sources']['backfill']['source'], 'backfill')

    def test_restart_does_not_refetch_or_duplicate(self):
        r = self.runner()
        resume = self.run_until(r, T0 + 600)
        before = len(self.fetched_signatures())
        counts = r.store.counts()
        r.store.close()
        again = self.runner()
        self.run_until(again, END, start=resume)
        self.assertEqual(len(self.fetched_signatures()), before)                  # nothing already handled is fetched again
        self.assertEqual(again.store.counts()['candidates'], 6)
        self.assertEqual(sum(1 for c in again.store.rows('candidates', limit=100) if c['mint'] == self.tokens['m1'].mint), 1)
        self.assertGreaterEqual(counts['candidates'], 5)
        cursor = json.loads((self.state / 'cursor-backfill.json').read_text())['seq']
        self.assertEqual(cursor, 1500)

    def test_default_is_off_and_changes_nothing(self):
        cfg = dict(self.cfg)
        cfg['backfill'] = None
        r = self.runner(cfg)
        self.run_until(r, END)
        meta, screens, entered = self.facts(r)
        self.assertEqual(set(meta), {'f1', 'f2'})                                  # the missed graduations are simply lost, as before
        self.assertEqual(entered, {'f1', 'f2'})
        self.assertFalse(any(c[1] in ('getSignaturesForAddress', 'getTransaction') for c in self.world.calls))
        self.assertEqual(json.loads((self.state / 'health.json').read_text())['sources'], {})
        self.assertFalse((self.state / 'backfill-state.json').exists())

    def test_disabled_block_is_off_too(self):
        cfg = dict(self.cfg)
        cfg['backfill'] = B.make_config({'enabled': False})
        r = self.runner(cfg)
        self.run_until(r, T0 + 300)
        self.assertFalse(any(c[1] == 'getSignaturesForAddress' for c in self.world.calls))

    def test_a_shed_low_lane_costs_nothing_and_the_trader_keeps_trading(self):
        r = self.runner()
        r.extra_sources.sources[0].helius.transport.shed_low_lane()               # a 429 elsewhere sheds the whole low lane (30 s)
        self.run_until(r, T0)                                                     # one pass inside the shed window
        meta, screens, entered = self.facts(r)
        self.assertEqual(entered, set())                                           # (f1 is still 298 s old: nothing could enter yet)
        self.assertFalse(any(c[1] in ('getTransaction', 'getSignaturesForAddress') for c in self.world.calls))
        self.assertNotIn('SOURCE_FAILED', {e['code'] for e in r.store.rows('errors', limit=100)})
        self.assertEqual(json.loads((self.state / 'health.json').read_text())['sources']['backfill']['sheds'], 1)
        self.run_until(r, END, start=T0 + 60)                                      # the window ended: the backfill catches up
        self.assertEqual(self.facts(r)[2], {'f1', 'f2', 'm1', 'm2'})

if __name__ == '__main__':
    unittest.main()
