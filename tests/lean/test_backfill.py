"""SYNTHETIC_TEST_ONLY: discovery gap backfill (lean/backfill.py). Fake Helius object, real discovery database, real migration
transactions (the desk's graduation fixture re-keyed per token, exactly like tests/lean/fakeworld.py). No network.

Provenance: the transaction body is tests.test_graduation_witness.fixture() (a trimmed, synthetic copy of a mainnet pump.fun
migrate transaction, see its docstring) with the mint / pool / PDAs replaced per token; signatures are derived from the token tag.
"""
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk import graduation_witness as g
from desk.model import canonical, digest
from desk.programs import unbase58
from desk.security import base58
from discovery import continuous as discovery
from lean import backfill as B
from lean import candidates as C
from lean.providers import LANE_SHED, ProviderError
from lean.sources import Source
from tests.lean.fakeworld import Token
from tests.test_graduation_witness import fixture as migration_fixture

T = 1_800_000_000.0                   # "now" of the tests
MIN_AGE, MAX_AGE = 300, 7200
SCREEN = {'min_age_seconds': MIN_AGE, 'max_age_seconds': MAX_AGE}


def signature_of(token):
    return base58(bytes([token.tag + 50]) * 64)


def tx_for(token, *, slot, block_time, err=None):
    """A getTransaction(jsonParsed) result for ``token``'s migration."""
    raw0, mint0, pool0 = migration_fixture()
    raw = copy.deepcopy(raw0)
    authority = g._pda([b'pool-authority', unbase58(token.mint)], g.PUMP)
    curve = g._pda([b'bonding-curve', unbase58(token.mint)], g.PUMP)
    replacements = {mint0: token.mint, pool0: token.pool,
                    g._pda([b'pool-authority', unbase58(mint0)], g.PUMP): authority,
                    g._pda([b'bonding-curve', unbase58(mint0)], g.PUMP): curve}
    for ix in raw['transaction']['message']['instructions']:
        ix['accounts'] = [replacements.get(x, x) for x in ix['accounts']]
    ix = raw['meta']['innerInstructions'][0]['instructions'][0]
    data = unbase58(ix['data'])
    for old, new in replacements.items():
        data = data.replace(unbase58(old), unbase58(new))
    ix['data'] = base58(data)
    raw['transaction']['signatures'] = [signature_of(token)]
    raw['slot'], raw['blockTime'] = slot, int(block_time)
    raw['meta']['err'] = err
    return raw


def plain_tx(sig, *, slot, block_time):
    """A transaction that mentions the authority but is not a migration."""
    return {'slot': slot, 'blockTime': int(block_time), 'meta': {'err': None, 'innerInstructions': [], 'logMessages': []},
            'transaction': {'signatures': [sig], 'message': {'accountKeys': [], 'instructions': []}}}


class Clock:
    def __init__(self, t=T):
        self.t = float(t)

    def time(self):
        return self.t


class FakeHelius:
    """getSignaturesForAddress / getTransaction over scripted data; records every call."""

    def __init__(self):
        self.rows = []                    # newest first: {'signature','slot','blockTime','err'}
        self.txs = {}                     # signature -> result (or an Exception to raise, or None)
        self.calls = []
        self.shed_on = set()              # methods that raise LANE_SHED
        self.fail_on = {}                 # method -> ProviderError

    def add(self, sig, slot, block_time, err=None, tx=None):
        self.rows.insert(0, {'signature': sig, 'slot': slot, 'blockTime': int(block_time), 'err': err})
        self.rows.sort(key=lambda r: -r['slot'])
        if tx is not None:
            self.txs[sig] = tx

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method in self.shed_on:
            raise ProviderError(LANE_SHED, True, meta={'why': 'no_token'})
        if method in self.fail_on:
            raise self.fail_on[method]
        if method == 'getSignaturesForAddress':
            address, options = params
            rows = list(self.rows)
            if options.get('before'):
                rows = rows[[r['signature'] for r in rows].index(options['before']) + 1:]
            if options.get('until'):
                ids = [r['signature'] for r in rows]
                rows = rows[:ids.index(options['until'])] if options['until'] in ids else rows
            return copy.deepcopy(rows[:options.get('limit', 1000)]), b'', {}
        if method == 'getTransaction':
            result = self.txs.get(params[0])
            if isinstance(result, Exception):
                raise result
            return copy.deepcopy(result), b'', {}
        raise AssertionError('unexpected method ' + method)

    def count(self, method):
        return sum(1 for m, _ in self.calls if m == method)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.state = self.dir / 'state'
        self.state.mkdir()
        self.db = self.dir / 'continuous.sqlite'
        with patch.object(discovery.time, 'time', return_value=T - 100_000):
            discovery.initialize(self.db)
        self.seq = 0
        self.clock = Clock()
        self.helius = FakeHelius()
        self.tokens = [Token(21 + 2 * i) for i in range(8)]

    def feed(self, signature):
        """The discovery listener stored this signature (what 'seen by the feed' means)."""
        self.seq += 1
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO raw_events(seq,source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?,?)',
                      (self.seq, 'confirmed:' + signature, T - 1000, 1, '{}', 'h'))

    def source(self, **cfg):
        cfg = {'interval_s': 120, **cfg}
        return B.BackfillSource(self.helius, self.db, self.state, cfg=B.make_config(cfg), screen_cfg=SCREEN, clock=self.clock.time)

    def migration(self, token, slot, age, *, in_feed=False):
        """A graduation ``age`` seconds old; optionally already stored by the feed."""
        sig = signature_of(token)
        self.helius.add(sig, slot, T - age, tx=tx_for(token, slot=slot, block_time=T - age))
        if in_feed:
            self.feed(sig)
        return sig

    def mints(self, found):
        return [c.mint for c in found]


class ConfigTests(unittest.TestCase):
    def test_defaults_and_strictness(self):
        self.assertIsNone(B.make_config(None))
        cfg = B.make_config({})
        self.assertEqual((cfg['interval_s'], cfg['authority'], cfg['enabled'], cfg['name']), (120, discovery.AUTHORITY, True, 'backfill'))
        self.assertTrue(B.make_config({'_comment': 'x', 'enabled': False})['enabled'] is False)
        for bad in ({'nope': 1}, {'interval_s': 5}, {'interval_s': 3601}, {'interval_s': 120.0}, {'interval_s': True}, {'limit': 0},
                    {'limit': 1001}, {'max_pages': 0}, {'max_tx_per_poll': 0}, {'lookback_s': 10}, {'authority': 'not-an-address'},
                    {'authority': 5}, {'enabled': 'yes'}, {'name': 'Bad Name'}, {'name': ''}, {'credits_per_transaction': -1},
                    {'day_history': 0}, [], 'x'):
            with self.assertRaises(B.ConfigError, msg=bad):
                B.make_config(bad)

    def test_build_is_none_when_disabled_or_absent(self):
        for cfg in (None, {'enabled': False}):
            self.assertIsNone(B.build(cfg, helius_low=None, discovery_db='x', state_dir='y'))

    def test_the_default_authority_is_the_one_discovery_filters_on(self):
        self.assertEqual(B.AUTHORITY, discovery.AUTHORITY)


class FeedGapTests(Base):
    def test_only_signatures_missing_from_the_feed_are_fetched_and_gaps_are_counted(self):
        a, b, c = self.tokens[:3]
        self.migration(a, 100, 1000, in_feed=True)             # the feed has it
        self.migration(b, 101, 900)                             # the feed missed it
        self.helius.add('PLAIN', 102, T - 800, tx=plain_tx('PLAIN', slot=102, block_time=T - 800))   # authority tx, not a migration
        src = self.source()
        found, cursor = src.iter_new(0)
        self.assertEqual(self.mints(found), [b.mint])
        self.assertEqual(cursor, 102)
        fetched = [p[0] for m, p in self.helius.calls if m == 'getTransaction']
        self.assertEqual(sorted(fetched), sorted([signature_of(b), 'PLAIN']))     # the signature the feed has was never fetched
        today = src.health()['today']
        self.assertEqual((today['authority_txs'], today['seen_by_feed'], today['missing_from_feed'], today['missed_migrations'],
                          today['not_migration']), (3, 1, 2, 1, 1))
        self.assertAlmostEqual(today['gap_rate'], 2 / 3)
        self.assertEqual(today['credits_estimate'], today['calls_signatures'] + today['calls_transaction'])

    def test_candidate_has_the_discovery_shape_and_labels(self):
        token = self.tokens[0]
        self.migration(token, 100, 1000)
        found, _ = self.source().iter_new(0)
        cand = found[0]
        self.assertIsInstance(cand, C.Candidate)
        self.assertEqual((cand.mint, cand.pool, cand.signature, cand.slot, cand.seq), (token.mint, token.pool, signature_of(token), 100, 100))
        self.assertEqual(cand.migrated_at, T - 1000)                  # on-chain block time, not the local clock
        self.assertEqual(cand.meta, {'source': 'backfill', 'latency_s': 1000.0, 'beyond_max_age': False})
        self.assertTrue(cand.payload_hash)

    def test_same_candidate_as_the_discovery_scan_for_the_same_transaction(self):
        token = self.tokens[0]
        tx = tx_for(token, slot=100, block_time=T - 1000)
        wire = {'method': 'transactionNotification', 'params': {'result': {
            'signature': signature_of(token), 'slot': 100, 'blockTime': tx['blockTime'],
            'transaction': {'transaction': tx['transaction'], 'meta': tx['meta']}}}}
        other = self.dir / 'other.sqlite'
        with patch.object(discovery.time, 'time', return_value=T - 100_000):
            discovery.initialize(other)
        with sqlite3.connect(other) as c:
            c.execute('INSERT INTO raw_events(seq,source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?,?)',
                      (1, 'confirmed:x', T - 1000, 100, canonical(wire), digest(wire)))
        via_feed = C.scan_new(other, 0, now=T, cfg=SCREEN).candidates[0]
        via_backfill = B.migration_candidate(tx, signature_of(token), now=T, max_age=MAX_AGE)
        for field in ('mint', 'pool', 'signature', 'slot', 'migrated_at'):
            self.assertEqual(getattr(via_feed, field), getattr(via_backfill, field), field)

    def test_not_a_clean_migration_is_never_a_candidate(self):
        token = self.tokens[0]
        good = tx_for(token, slot=100, block_time=T - 1000)
        sig = signature_of(token)
        failed = tx_for(token, slot=100, block_time=T - 1000, err={'InstructionError': [0, 'Custom']})
        wrong_sig = copy.deepcopy(good)
        wrong_sig['transaction']['signatures'] = [signature_of(self.tokens[1])]
        no_time = {k: v for k, v in good.items() if k != 'blockTime'}
        no_meta = {k: v for k, v in good.items() if k != 'meta'}
        wrong_pool = copy.deepcopy(good)
        for ix in wrong_pool['transaction']['message']['instructions']:
            ix['accounts'] = [self.tokens[1].pool if a == token.pool else a for a in ix['accounts']]
        for bad in (failed, wrong_sig, no_time, no_meta, wrong_pool, None, [], 'x', {}, plain_tx(sig, slot=1, block_time=1)):
            self.assertIsNone(B.migration_candidate(bad, sig, now=T, max_age=MAX_AGE), repr(bad)[:60])
        self.assertIsNotNone(B.migration_candidate(good, sig, now=T, max_age=MAX_AGE))

    def test_two_events_in_one_transaction_are_ambiguous_not_a_candidate(self):
        token = self.tokens[0]
        tx = tx_for(token, slot=100, block_time=T - 1000)
        group = tx['meta']['innerInstructions'][0]
        group['instructions'].append(copy.deepcopy(group['instructions'][0]))
        self.assertIsNone(B.migration_candidate(tx, signature_of(token), now=T, max_age=MAX_AGE))


class AgeTests(Base):
    def test_young_signatures_wait_and_do_not_advance_the_cursor(self):
        old, young = self.tokens[:2]
        self.migration(old, 100, 1000)
        self.migration(young, 101, MIN_AGE - 1)
        src = self.source()
        found, cursor = src.iter_new(0)
        self.assertEqual((self.mints(found), cursor), ([old.mint], 100))
        self.assertEqual(src.health()['today']['authority_txs'], 1)
        self.clock.t += 130
        found, cursor = src.iter_new(100)
        self.assertEqual((self.mints(found), cursor), ([young.mint], 101))

    def test_boundary_exactly_min_age_is_ready(self):
        self.migration(self.tokens[0], 100, MIN_AGE)
        self.assertEqual(len(self.source().iter_new(0)[0]), 1)

    def test_max_age_boundary_matches_the_screen_exceeds_not_equals(self):
        self.migration(self.tokens[0], 100, MAX_AGE)
        self.migration(self.tokens[1], 101, MAX_AGE + 1)
        found, _ = self.source().iter_new(0)
        self.assertEqual([c.beyond_max_age for c in found], [False, True])      # the screen rejects TOO_OLD only when age > max_age

    def test_a_migration_older_than_max_age_is_still_emitted_but_flagged(self):
        token = self.tokens[0]
        self.migration(token, 100, MAX_AGE + 600)
        src = self.source()
        found, _ = src.iter_new(0)
        self.assertEqual(self.mints(found), [token.mint])
        self.assertTrue(found[0].beyond_max_age)
        self.assertEqual(found[0].meta['latency_s'], MAX_AGE + 600)
        self.assertEqual(src.health()['today']['beyond_max_age'], 1)
        self.assertGreater(T - found[0].migrated_at, MAX_AGE)         # the screen's own TOO_OLD test will reject it


class ProviderDecimalTests(Base):
    """Live 2026-10-14: providers parse fractions as Decimal (exact JSON); migration_candidate must accept that shape."""

    def test_a_decimal_parsed_transaction_is_a_candidate(self):
        import json as _json
        from decimal import Decimal
        token = self.tokens[0]
        tx = tx_for(token, slot=100, block_time=T - 400)
        exact = _json.loads(_json.dumps({**tx, 'meta': {**tx['meta'], 'fee_ui': 0.000005}}), parse_float=Decimal)
        plain = B.migration_candidate(_json.loads(_json.dumps(tx)), signature_of(token), now=T, max_age=MAX_AGE)
        got = B.migration_candidate(exact, signature_of(token), now=T, max_age=MAX_AGE)
        self.assertIsNotNone(got)
        self.assertEqual((got.mint, got.pool), (plain.mint, plain.pool))


class EntryAgeCapTests(Base):
    """backfill.max_entry_age_s (coordinator, 2026-10-14): a late-found graduation is recorded but never entered once older
    than the cap, even when screen.max_age_seconds would still allow it."""

    def test_cap_is_carried_and_the_screen_rejects_too_old_above_it(self):
        from lean import candidates as C
        self.migration(self.tokens[0], 100, MIN_AGE + 20)
        self.migration(self.tokens[1], 101, 700)
        found, _ = self.source(max_entry_age_s=600).iter_new(0)
        self.assertEqual([c.entry_max_age_s for c in found], [600, 600])
        self.assertEqual([c.beyond_max_age for c in found], [False, True])
        cfg = {**C.DEFAULTS, 'min_age_seconds': MIN_AGE, 'max_age_seconds': MAX_AGE}
        for cand, too_old in zip(found, [False, True]):
            reasons = []
            try:
                C._screen(cand, None, cfg, T, None, reasons, [], [], {})
            except Exception:
                pass                                     # later stages need providers; the age check runs first
            self.assertEqual('TOO_OLD' in reasons, too_old)

    def test_absent_cap_keeps_the_screen_max_age(self):
        self.migration(self.tokens[0], 100, 700)
        found, _ = self.source().iter_new(0)
        self.assertIsNone(found[0].entry_max_age_s)
        self.assertFalse(found[0].beyond_max_age)

    def test_cap_bounds(self):
        for bad in (59, 'x', 8 * 86400):
            with self.assertRaises(B.ConfigError):
                B.make_config({'max_entry_age_s': bad})


class CursorTests(Base):
    def test_until_is_the_last_handled_signature_after_the_runner_persisted_the_cursor(self):
        a, b, c = self.tokens[:3]
        self.migration(a, 100, 2000)
        self.migration(b, 101, 1900)
        src = self.source()
        found, cursor = src.iter_new(0)
        self.assertEqual(cursor, 101)
        self.migration(c, 102, 1800)
        self.clock.t += 130
        found2, cursor2 = src.iter_new(cursor)                        # the runner persisted 101
        self.assertEqual((self.mints(found2), cursor2), ([c.mint], 102))
        second_call = [p for m, p in self.helius.calls if m == 'getSignaturesForAddress'][1]
        self.assertEqual(second_call[1]['until'], signature_of(b))
        self.assertNotIn('before', second_call[1])

    def test_a_crash_before_the_cursor_was_persisted_refetches_the_overlap_not_lose_it(self):
        a, b = self.tokens[:2]
        self.migration(a, 100, 2000)
        self.migration(b, 101, 1900)
        src = self.source()
        src.iter_new(0)                                               # batch returned ...
        again = self.source()                                         # ... but the process died before the cursor moved: cursor 0
        found, _ = again.iter_new(0)
        self.assertEqual(sorted(self.mints(found)), sorted([a.mint, b.mint]))
        self.assertNotIn('until', [p for m, p in self.helius.calls if m == 'getSignaturesForAddress'][-1][1])
        # the same process re-walking the overlap does not double count it
        src2 = self.source()
        src2.iter_new(0)
        self.clock.t += 130
        src2.iter_new(0)
        self.assertEqual((src2.health()['today']['authority_txs'], src2.health()['today']['missed_migrations']), (2, 2))

    def test_state_survives_a_restart(self):
        a, b = self.tokens[:2]
        self.migration(a, 100, 2000)
        src = self.source()
        _, cursor = src.iter_new(0)
        src.iter_new(cursor)                                          # commits
        self.migration(b, 101, 1900)
        again = self.source()
        self.clock.t += 130
        found, cursor2 = again.iter_new(cursor)
        self.assertEqual((self.mints(found), cursor2), ([b.mint], 101))
        self.assertEqual([p for m, p in self.helius.calls if m == 'getSignaturesForAddress'][-1][1]['until'], signature_of(a))

    def test_corrupt_state_is_refused_not_reset(self):
        (self.state / B.STATE_FILE).write_text('{not json')
        with self.assertRaises(Exception):
            self.source()
        (self.state / B.STATE_FILE).write_text(json.dumps({'version': 99, 'slot': 0, 'days': {}}))
        with self.assertRaises(B.ConfigError):
            self.source()

    def test_first_start_is_bounded_by_the_lookback_and_never_walks_to_genesis(self):
        self.migration(self.tokens[0], 100, 100_000)                  # far older than lookback_s: not examined
        self.migration(self.tokens[1], 101, 1000)
        src = self.source(lookback_s=3600)
        found, _ = src.iter_new(0)
        self.assertEqual(self.mints(found), [self.tokens[1].mint])
        self.assertEqual(src.health()['today']['authority_txs'], 1)

    def test_without_until_the_walk_stops_at_the_cursor_slot(self):
        self.migration(self.tokens[0], 100, 3000)
        self.migration(self.tokens[1], 101, 2000)
        found, _ = self.source().iter_new(101)
        self.assertEqual(self.mints(found), [self.tokens[1].mint])    # slot >= cursor: equal slot is re-examined (runner dedupes by mint)

    def test_cursor_never_moves_backward(self):
        found, cursor = self.source().iter_new(500)
        self.assertEqual((found, cursor), ([], 500))


class BudgetTests(Base):
    def test_poll_interval_is_respected(self):
        self.migration(self.tokens[0], 100, 1000)
        src = self.source()
        src.iter_new(0)
        calls = len(self.helius.calls)
        self.clock.t += 119
        self.assertEqual(src.iter_new(100), ([], 100))
        self.assertEqual(len(self.helius.calls), calls)
        self.clock.t += 2
        src.iter_new(100)
        self.assertGreater(len(self.helius.calls), calls)

    def test_transaction_budget_per_poll_resumes_in_order_without_skipping(self):
        sigs = [self.migration(self.tokens[i], 100 + i, 3000 - i) for i in range(5)]
        src = self.source(max_tx_per_poll=2)
        got, cursor = [], 0
        for n in range(4):
            found, cursor = src.iter_new(cursor)
            if n == 0:
                self.assertEqual(len(found), 2)                       # the per-poll budget binds ...
                self.assertEqual(self.helius.count('getTransaction'), 2)
            got += self.mints(found)
            self.clock.t += 130
        self.assertEqual(got, [t.mint for t in self.tokens[:5]])      # ... and the rest follows, oldest first, each exactly once
        self.assertEqual(self.helius.count('getTransaction'), 5)
        self.assertEqual(src.health()['today']['missed_migrations'], 5)

    def test_pages_are_bounded_and_overflow_is_reported(self):
        for i in range(8):
            sig = 'S%02d' % i
            self.helius.add(sig, 100 + i, T - 3000 + i, tx=plain_tx(sig, slot=100 + i, block_time=T - 3000 + i))
        src = self.source(limit=3, max_pages=2, max_tx_per_poll=200)
        src.iter_new(0)
        self.assertEqual(self.helius.count('getSignaturesForAddress'), 2)
        today = src.health()['today']
        self.assertEqual((today['overflow_polls'], today['authority_txs']), (1, 6))    # the 2 oldest were NOT examined, and it says so
        self.assertEqual(self.helius.calls[1][1][1]['before'], 'S05')

    def test_exact_page_boundary_is_not_an_overflow(self):
        for i in range(4):
            sig = 'S%02d' % i
            self.helius.add(sig, 100 + i, T - 3000 + i, tx=plain_tx(sig, slot=100 + i, block_time=T - 3000 + i))
        src = self.source(limit=2, max_pages=2, max_tx_per_poll=200)
        src.iter_new(0)
        # the second page is full too; whether more exist is unknown: counted as an overflow so a gap can never hide
        self.assertEqual(src.health()['today']['overflow_polls'], 1)


class FailureTests(Base):
    def test_shed_before_anything_is_sent_returns_quietly_and_retries_soon(self):
        self.migration(self.tokens[0], 100, 1000)
        self.helius.shed_on.add('getSignaturesForAddress')
        src = self.source()
        self.assertEqual(src.iter_new(0), ([], 0))
        self.assertEqual(src.health()['sheds'], 1)
        self.helius.shed_on.clear()
        self.clock.t += 16                                            # well before interval_s: a shed does not cost a whole interval
        found, _ = src.iter_new(0)
        self.assertEqual(len(found), 1)

    def test_shed_mid_pass_keeps_what_was_found(self):
        a, b = self.tokens[:2]
        self.migration(a, 100, 3000)
        self.migration(b, 101, 2000)
        calls = {'n': 0}
        original = self.helius.rpc

        def shed_second_tx(method, params):
            if method == 'getTransaction':
                calls['n'] += 1
                if calls['n'] == 2:
                    raise ProviderError(LANE_SHED, True)
            return original(method, params)
        self.helius.rpc = shed_second_tx
        src = self.source()
        found, cursor = src.iter_new(0)
        self.assertEqual((self.mints(found), cursor), ([a.mint], 100))
        self.clock.t += 20
        found2, cursor2 = src.iter_new(cursor)
        self.assertEqual((self.mints(found2), cursor2), ([b.mint], 101))
        self.assertEqual(src.health()['today']['missed_migrations'], 2)       # b counted once, not twice

    def test_a_provider_error_is_raised_for_the_source_set_to_report(self):
        self.helius.fail_on['getSignaturesForAddress'] = ProviderError('HTTP_500', True)
        src = self.source()
        with self.assertRaises(ProviderError):
            src.iter_new(0)
        self.assertEqual((src.health()['errors'], src.health()['last_error']), (1, 'HTTP_500'))

    def test_unreadable_discovery_database_stops_the_pass_without_spending_credits(self):
        self.migration(self.tokens[0], 100, 1000)
        os.unlink(self.db)
        with self.assertRaises(C.DiscoveryUnavailable):
            self.source().iter_new(0)
        self.assertEqual(self.helius.count('getTransaction'), 0)

    def test_a_transaction_that_never_arrives_is_retried_then_given_up_not_stuck_forever(self):
        a, b = self.tokens[:2]
        sig_a = self.migration(a, 100, 3000)
        self.migration(b, 101, 2000)
        self.helius.txs[sig_a] = None                                 # not available (yet)
        src = self.source(max_attempts_per_signature=3)
        cursor, got = 0, []
        for _ in range(4):
            found, cursor = src.iter_new(cursor)
            got += self.mints(found)
            self.clock.t += 130
        self.assertEqual(got, [b.mint])                               # b was held up for 3 polls, then released
        today = src.health()['today']
        self.assertEqual((today['fetch_failed'], today['not_migration'], today['missed_migrations']), (1, 0, 1))

    def test_a_failing_transaction_call_is_retried_like_a_missing_one(self):
        a = self.tokens[0]
        sig = self.migration(a, 100, 3000)
        self.helius.txs[sig] = ProviderError('HTTP_500', True)
        src = self.source(max_attempts_per_signature=2)
        cursor = 0
        for _ in range(4):
            found, cursor = src.iter_new(cursor)
            self.clock.t += 130
        self.assertEqual(src.health()['today']['fetch_failed'], 1)
        for _ in range(2):                                            # a crash that re-walks the same signature does not count it again
            src.iter_new(0)
            self.clock.t += 130
        self.assertEqual(src.health()['today']['fetch_failed'], 1)

    def test_failed_on_chain_transactions_are_skipped_without_a_fetch(self):
        self.helius.add('BAD', 100, T - 1000, err={'InstructionError': [0, 'x']})
        src = self.source()
        self.assertEqual(src.iter_new(0), ([], 100))
        self.assertEqual((self.helius.count('getTransaction'), src.health()['today']['failed_tx']), (0, 1))

    def test_rows_without_a_block_time_are_never_used(self):
        self.helius.rows.append({'signature': 'X', 'slot': 100, 'blockTime': None, 'err': None})
        self.assertEqual(self.source().iter_new(0), ([], 0))
        self.assertEqual(self.helius.count('getTransaction'), 0)

    def test_malformed_signature_list_is_an_error(self):
        orig = self.helius.rpc
        self.helius.rpc = lambda m, p: ({'not': 'a list'}, b'', {})
        with self.assertRaises(ProviderError):
            self.source().iter_new(0)


class HealthTests(Base):
    def test_per_day_counters_and_history_bound(self):
        src = self.source(day_history=2)
        self.migration(self.tokens[0], 100, 1000)
        src.iter_new(0)
        for _ in range(3):
            self.clock.t += DAY_S
            src.iter_new(0)
        health = src.health()
        self.assertEqual(len(health['days']), 2)                      # older days are dropped
        self.assertEqual(health['today']['polls'], 1)
        self.assertIsNone(health['today']['gap_rate'])                # no signatures that day: unknown, not 0

    def test_health_is_json_safe(self):
        self.migration(self.tokens[0], 100, 1000)
        src = self.source()
        src.iter_new(0)
        json.dumps(src.health(), allow_nan=False)

    def test_is_a_source_with_a_name(self):
        src = self.source()
        self.assertIsInstance(src, Source)
        self.assertEqual(src.name, 'backfill')


DAY_S = 86_400


if __name__ == '__main__':
    unittest.main()
