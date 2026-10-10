"""SYNTHETIC_TEST_ONLY: the dispatcher with paper_watchlist_version 1 (journal schema 2).

Real dispatcher, admission, acquisition, intake, history, cycle, engine and ledger through the
repository's DispatcherTests fixture; only wire bytes are injected. The market fixture is varied
through the reserve account amounts, so "new fixture data" is genuinely new provider data.
"""
import copy
import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk import paper_cycle as cycle, paper_read_sources as transport, watchlist
from desk.model import canonical, digest
from desk.programs import unbase58
from desk.security import base58
from tests import test_kraken_lifecycle as kraken
from tests import test_paper_entry_dispatcher as fixture
from tests.helpers import config
from tests.test_live_strategy_features import transaction, POOL as TRADE_POOL
from tests.test_paper_read_sources import Response
from tools import paper_entry_dispatcher as tool

GOOD = (10**12, 10**11)   # reserve accounts of the fixture: market cap and liquidity pass
THIN = (10**12, 10**9)    # tiny liquidity: the engine rejects MARKET_CAP and LIQUIDITY (both NOT_YET)

_SOURCE = inspect.getsource(kraken.actual_cycle)
assert '(0,10**12),(1,10**11)' in _SOURCE, 'fixture changed: update the reserve override'
_NAMESPACE = dict(vars(kraken))
exec(_SOURCE.replace('(0,10**12),(1,10**11)', '(0,outer.reserve_a),(1,outer.reserve_b)'), _NAMESPACE)
actual_cycle_with_reserves = _NAMESPACE['actual_cycle']


class Base(unittest.TestCase):
    extra = {}
    ctx_override = {}      # applied to every planned context from the start, so the journal's pinned context agrees

    def setUp(self):
        self.wdir = Path(os.path.realpath(tempfile.mkdtemp()))
        os.chmod(self.wdir, 0o700)
        self.addCleanup(lambda: __import__('shutil').rmtree(self.wdir, ignore_errors=True))
        self.watch = self.wdir / 'watchlist.sqlite'
        watchlist.initialize(self.watch)
        original = self.real_plan = tool.plan

        def plan(**kw):
            kw.setdefault('watchlist_db', str(self.watch))
            return {**original(**kw), **self.ctx_override}
        for p in (patch.object(fixture, 'config', lambda: {**config(), watchlist.KEY: 1, **self.extra}),
                  patch.object(tool, 'plan', side_effect=plan)):
            p.start()
            self.addCleanup(p.stop)
        self.f = fixture.DispatcherTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.ctx = tool.plan(**self.f.args)
        self.cfg = self.f.cfg

    # -- helpers -----------------------------------------------------------------
    def advance(self, seconds):
        self.f.f.at += seconds
        self.f.clock[0] = float(self.f.f.at)

    def run_dispatch(self, reserves=GOOD, execute=True, window='trades'):
        f = self.f
        outer = f
        empty = window == 'empty'    # no trades in the five-minute window (a real, still-changing condition)

        class Opener:
            def open(self, request, *, timeout):
                call = json.loads(request.data)
                rows = [outer.raw]
                if call['params'][1]['filters'].get('slot') != {'gte': 10, 'lt': 11}:
                    rows = []
                    for i in range(0 if empty else 40):
                        raw = transaction('dispatch-flow-' + str(i), 10 + i, outer.f.at - 1, quote=200, base=100,
                                          wallet=base58((i + 1).to_bytes(32, 'big')))
                        ix = raw['transaction']['message']['instructions'][0]
                        ix['data'] = base58(unbase58(ix['data']).replace(unbase58(TRADE_POOL), unbase58(outer.pool)))
                        rows.append(raw)
                return Response(canonical({'jsonrpc': '2.0', 'id': 'paper-read-v1',
                                           'result': {'data': rows, 'paginationToken': None}}).encode())
        original = cycle.run_once

        def setup_rpc(method, params):
            # The fixture's version pins exactly one intent; with re-evaluations the invariant is
            # "the intent being served is durable and unresolved before any provider I/O".
            outer.assertEqual(outer.count('intents'), outer.count('results') + 1, 'Durable intent must precede I/O')
            outer.calls.append(method)
            if method == 'getAccountInfo':
                return {'value': copy.deepcopy(outer.f.protocol.rpc('getMultipleAccounts', [])['value'][6])}
            if method == 'getSlot':
                return 120
            outer.assertEqual(method, 'getTransactionsForAddress')
            return {'data': []}

        def entry_cycle(research, evidence, ledger, cfg, **kw):
            item = kw['candidates'][0]
            h = SimpleNamespace(f=outer.f, target=item.target, item=item, path=outer.ledger, cfg=outer.cfg,
                                http_calls=[], sell_output=100_000_000, buy_output_raw=10_000_000,
                                reserve_a=reserves[0], reserve_b=reserves[1])

            def run(**args):
                return original(research, evidence, ledger, cfg, wall_clock=lambda: outer.f.at,
                                monotonic=lambda: outer.f.tick, dependency_blockers=(), **args)
            h.run_cycle = run
            return actual_cycle_with_reserves(h, candidates=(item,))
        with (patch.object(tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=setup_rpc),
              patch.object(transport, 'build_opener', return_value=Opener()),
              patch.dict(os.environ, {'HELIUS_API_KEY': 'SYNTHETIC', 'JUPITER_API_KEY': 'SYNTHETIC'}),
              patch.object(cycle, 'run_once', side_effect=entry_cycle), patch.object(tool.entry.time, 'sleep'),
              patch.object(tool.time, 'time', side_effect=lambda: f.f.at)):
            return f.invoke(execute=execute, systemd_credentials=execute)

    def journal(self, table):
        with closing(sqlite3.connect(self.f.journal)) as c:
            return c.execute(f'SELECT * FROM {table} ORDER BY rowid').fetchall()

    def events(self):
        with closing(watchlist.Watchlist(self.watch, read_only=True)) as w:
            return w.events()

    def scans(self):
        with self.f.f.jobs.connect() as c:
            return [tuple(r) for r in c.execute('SELECT * FROM scans ORDER BY created,id')]


class NotYetThenAcceptedTests(Base):
    def test_rejected_at_t0_is_scheduled_not_due_early_and_accepted_30_minutes_later_as_a_new_scan(self):
        first = self.run_dispatch(THIN)
        self.assertEqual((first['status'], first['paper_status']), ('DISPATCHED', 'COMPLETE'), first)
        scan1 = first['scan_id']
        (result_row,) = self.journal('results')
        outcomes = json.loads(result_row[1])['result']['outcomes']
        self.assertEqual([(o['type'], sorted(o['reasons'])) for o in outcomes], [('reject', ['LIQUIDITY', 'MARKET_CAP'])])
        used1 = self.f.f.progress.admission(scan1)['requests_used']
        self.assertGreater(used1, 0)
        original = (self.journal('intents'), self.journal('results'), self.scans())

        # The next executing dispatch reconciles the journal into the watchlist; nothing is due yet.
        self.advance(300)
        self.assertEqual(self.run_dispatch(GOOD)['status'], 'NO_CANDIDATE')
        (row,) = self.events()
        self.assertEqual((row['kind'], row['evaluation'], row['scan_id'], row['classification']),
                         ('ENROLLED', 1, scan1, 'NOT_YET'))
        self.assertEqual(json.loads(row['codes']), ['LIQUIDITY', 'MARKET_CAP'])
        result_at = json.loads(result_row[1])['at']
        self.assertEqual(row['next_eval_at'], result_at + 600)          # first backoff: 10 minutes
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['status'], 'NO_CANDIDATE')

        self.advance(600 - 300)                                           # now >= next_eval_at, t0 + ~10 minutes
        dry = self.run_dispatch(GOOD, execute=False)
        self.assertEqual((dry['status'], dry['hint']['evaluation'], dry['hint']['mint']),
                         ('DRY_RUN', 2, original[0][0][1]))

        self.advance(1800 - 600)                                          # t0 + 30 minutes, with new (good) data
        second = self.run_dispatch(GOOD)
        self.assertEqual((second['status'], second['paper_status']), ('DISPATCHED', 'COMPLETE'), second)
        self.assertNotEqual(second['scan_id'], scan1)                     # a NEW scan with a new intent id
        self.assertNotEqual(second['dispatch_id'], first['dispatch_id'])
        positions = cycle._state(self.f.ledger, self.cfg)['positions']
        self.assertEqual(list(positions), [self.f.mint])                  # accepted: paper BUY filled

        intents, results = self.journal('intents'), self.journal('results')
        self.assertEqual([i[3] for i in intents], [1, 2])                 # evaluation column
        self.assertEqual([json.loads(i[4])['evaluation'] for i in intents], [1, 2])
        self.assertEqual(len({i[1] for i in intents}), 1)                 # same mint, two intents
        # Original scan, intent, result and charges untouched; second scan charged on its own budget.
        self.assertEqual((intents[:1], results[:1], self.scans()[:1]), (original[0], original[1], original[2]))
        self.assertEqual(self.f.f.progress.admission(scan1)['requests_used'], used1)
        used2 = self.f.f.progress.admission(second['scan_id'])
        self.assertGreater(used2['requests_used'], 0)
        self.assertEqual(used2['request_ceiling'], 18)
        self.assertEqual(len(self.scans()), 2)

        # While the BUY is held the dispatcher refuses (unchanged rule), but the journal was already
        # validated and reconciled first: the accepted evaluation closes the mint's chain.
        with self.assertRaises(ValueError):
            self.run_dispatch(GOOD)
        self.assertEqual([(e['kind'], e['evaluation']) for e in self.events()], [('ENROLLED', 1), ('ACCEPTED', 2)])
        with tool._journal(self.f.journal) as c:                          # reconciliation is idempotent
            tool._reconcile_watchlist(tool.plan(**self.f.args), c, self.f.f.at)
        self.assertEqual(len(self.events()), 2)

    def test_a_foreign_scan_of_the_same_mint_makes_the_reevaluation_ambiguous_and_it_is_skipped(self):
        self.run_dispatch(THIN)
        self.advance(300)
        self.run_dispatch(GOOD)                                           # reconcile: ENROLLED, due in 600 s
        self.advance(1800)
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['status'], 'DRY_RUN')   # due and consistent
        self.f.f.jobs.admit(self.f.mint)                                  # an unrelated screen scan of the mint
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['status'], 'NO_CANDIDATE')
        self.assertEqual(len(self.journal('intents')), 1)

    def test_cold_restart_between_evaluations_preserves_the_watchlist(self):
        self.run_dispatch(THIN)
        self.advance(300)
        self.run_dispatch(GOOD)
        before = self.events()
        self.assertEqual(len(before), 1)
        # A restart is a fresh process: new handles, re-planned context, nothing cached.
        self.f.ctx = tool.plan(**self.f.args)
        self.assertEqual(self.events(), before)
        self.advance(1800)
        self.assertEqual(self.run_dispatch(GOOD)['paper_status'], 'COMPLETE')
        self.assertEqual(self.events()[0], before[0])


class PermanentTests(Base):
    def test_unclassified_code_is_permanent_and_the_mint_is_never_readmitted(self):
        with patch.dict(watchlist.NOT_YET):
            del watchlist.NOT_YET['LIQUIDITY']                            # now an unclassified code
            self.run_dispatch(THIN)
            self.advance(300)
            self.assertEqual(self.run_dispatch(GOOD)['status'], 'NO_CANDIDATE')
            (row,) = self.events()
            self.assertEqual((row['kind'], row['classification']), ('PERMANENT', 'PERMANENT'))
            scans_before = self.scans()
            for seconds in (600, 1800, 7200):
                self.advance(seconds)
                self.assertEqual(self.run_dispatch(GOOD)['status'], 'NO_CANDIDATE')
            self.assertEqual(self.scans(), scans_before)
            self.assertEqual(len(self.journal('intents')), 1)
            self.assertEqual(len(self.events()), 1)


class MaximumTests(Base):
    extra = {'paper_watchlist_max_reevaluations': 1}

    def test_configured_maximum_of_reevaluations_then_exhausted_and_never_again(self):
        self.run_dispatch(THIN)
        self.advance(300)
        self.run_dispatch(THIN, execute=True)                             # reconcile evaluation 1
        self.advance(600)
        second = self.run_dispatch(THIN)                                  # the single allowed re-evaluation
        self.assertEqual(second['paper_status'], 'COMPLETE')
        self.advance(300)
        self.assertEqual(self.run_dispatch(GOOD)['status'], 'NO_CANDIDATE')   # reconciles evaluation 2
        self.assertEqual([(e['kind'], e['evaluation']) for e in self.events()], [('ENROLLED', 1), ('EXHAUSTED', 2)])
        self.advance(86400)
        self.assertEqual(self.run_dispatch(GOOD)['status'], 'NO_CANDIDATE')
        self.assertEqual(len(self.journal('intents')), 2)


class SelectionAndContextTests(Base):
    def test_candidate_supply_is_window_based_not_the_newest_500(self):
        now = self.f.f.at
        with closing(sqlite3.connect(self.f.discovery)) as c:
            for i in range(600):                                           # newer than the candidate, still too young
                c.execute('INSERT INTO raw_events(source_id,received_at,slot,payload,payload_hash) VALUES(?,?,?,?,?)',
                          (f'confirmed:filler-{i}', now - 100, 1000 + i, '{}', digest({})))
            c.commit()
        with tool._journal(self.f.journal) as c:
            legacy = {k: v for k, v in self.ctx.items() if k != 'journal_schema'}
            self.assertIsNone(tool._select(legacy, c, now))                # count-based: target is buried
            hint = tool._select(self.ctx, c, now)
        self.assertEqual((hint['mint'], hint['evaluation']), (self.f.mint, 1))

    def test_context_requires_the_watchlist_path_exactly_when_the_flag_is_selected(self):
        args = {k: v for k, v in self.f.args.items() if k != 'watchlist_db'}
        with self.assertRaisesRegex(ValueError, 'Watchlist path'):
            self.real_plan(**args)
        ctx = self.real_plan(**args, watchlist_db=str(self.watch))
        # T20F: fresh selection keeps the 7200 s dispatch window; only a re-evaluation may use the 21600 s engine window
        self.assertEqual((ctx['journal_schema'], ctx['maximum_age'], ctx['watchlist_maximum_age'], ctx['preparation_margin']),
                         (2, 7200, 21600, 900))
        self.assertIn('watchlist_db', ctx['paths'])
        self.assertEqual(ctx, self.ctx)


class JournalIntegrityTests(Base):
    def tamper(self, sql):
        with closing(sqlite3.connect(self.f.journal)) as c:
            for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                if name.startswith(('no_intents_u', 'no_results_u')):
                    c.execute(f'DROP TRIGGER {name}')
            c.execute(sql)
            present = {n for n, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
            for name, guard in tool._guards(2).items():   # restore the reviewed guards: only content differs
                if name not in present:
                    c.execute(guard)
            c.commit()

    def test_evaluation_column_and_payload_must_agree_or_the_journal_is_refused(self):
        self.run_dispatch(THIN)
        self.tamper('UPDATE intents SET evaluation=2')
        with self.assertRaisesRegex(ValueError, 'Dispatch evaluation binding invalid'):
            self.run_dispatch(GOOD, execute=False)

    def test_stale_hint_beyond_the_engine_age_window_is_refused_in_the_journal(self):
        """Rewrite intent AND result consistently so that only the age grammar can object."""
        self.run_dispatch(THIN)
        with closing(sqlite3.connect(self.f.journal)) as c:
            intent_id, intent_text = c.execute('SELECT id,payload FROM intents').fetchone()
            result_text = c.execute('SELECT payload FROM results WHERE id=?', (intent_id,)).fetchone()[0]
        intent, result = json.loads(intent_text), json.loads(result_text)
        intent['at'] = intent['hint']['received_at'] + 7200 + 1   # T20F: an evaluation-1 hint keeps the 7200 s dispatch window
        result.update(intent_hash=digest(intent), at=intent['at'] + 1)
        self.tamper('UPDATE intents SET payload=' + repr(canonical(intent)) + ',hash=' + repr(digest(intent)))
        self.tamper('UPDATE results SET payload=' + repr(canonical(result)) + ',hash=' + repr(digest(result)))
        with self.assertRaisesRegex(ValueError, 'Dispatch hint grammar invalid'):
            self.run_dispatch(GOOD, execute=False)
        # The same record one second inside the window is valid (the clock must not precede it).
        self.advance(25000)
        intent['at'] = intent['hint']['received_at'] + 7200
        result.update(intent_hash=digest(intent), at=intent['at'] + 1)
        self.tamper('UPDATE intents SET payload=' + repr(canonical(intent)) + ',hash=' + repr(digest(intent)))
        self.tamper('UPDATE results SET payload=' + repr(canonical(result)) + ',hash=' + repr(digest(result)))
        self.run_dispatch(GOOD, execute=False)


class T20FTests(Base):
    """T20F items 3, 5 and 6 at dispatcher level: preparation margin, windows, table pressure, fresh-over-due."""

    def hint_age(self):
        dry = self.run_dispatch(GOOD, execute=False)
        self.assertEqual(dry['status'], 'DRY_RUN', dry)
        return self.f.f.at - dry['hint']['received_at'], dry

    def to_age(self, age):
        current, _ = self.hint_age()
        self.advance(age - current)
        return age

    def reconcile_first_rejection(self, window='trades'):
        self.run_dispatch(THIN)
        self.advance(300)
        self.assertEqual(self.run_dispatch(GOOD)['status'], 'NO_CANDIDATE')       # reconciles evaluation 1
        self.assertEqual([e['kind'] for e in self.events()], ['ENROLLED'])

    def test_a_fresh_candidate_is_selectable_only_while_it_keeps_the_preparation_margin(self):
        # fresh window 7200 - margin 900 = 6300
        self.assertEqual(self.to_age(6300), 6300)
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['status'], 'DRY_RUN')
        self.advance(1)
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['status'], 'NO_CANDIDATE')
        self.assertEqual(len(self.journal('intents')), 0)                       # nothing stranded

    def test_fresh_selection_no_longer_admits_hints_up_to_21600_seconds_old(self):
        self.to_age(7000)                                                       # old v2 behaviour selected this
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['status'], 'NO_CANDIDATE')

    def test_a_reevaluation_may_use_the_long_window_but_keeps_the_margin(self):
        self.reconcile_first_rejection()
        age = self.hint_age_of_enrolled()
        self.advance(20700 - age)                                               # engine window 21600 - margin 900
        dry = self.run_dispatch(GOOD, execute=False)
        self.assertEqual((dry['status'], dry['hint']['evaluation']), ('DRY_RUN', 2))
        self.advance(1)
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['status'], 'NO_CANDIDATE')

    def hint_age_of_enrolled(self):
        (row,) = self.events()
        return self.f.f.at - json.loads(row['hint'])['received_at']

    def test_preparation_that_consumes_the_margin_does_not_strand_an_intent(self):
        """Selected with exactly the margin left, 800 s of preparation still fits; without the margin it would not."""
        delay = 800
        real = tool._preflight

        def slow(ctx, scan=None):
            real(ctx, scan)
            if scan is not None and not self.slowed:
                self.slowed = True
                self.advance(delay)                                              # preparation took 800 s
        self.slowed = False
        self.to_age(6300)
        with patch.object(tool, '_preflight', side_effect=slow):
            result = self.run_dispatch(THIN)
        # past the 'expired during preparation' check; the fixture's data cannot survive the 800 s jump afterwards,
        # so the cycle may block, which is irrelevant here: the intent was written, dispatched and resolved.
        self.assertEqual(result['status'], 'DISPATCHED', result)
        self.assertEqual((len(self.journal('intents')), len(self.journal('results'))), (1, 1))
        self.assertTrue(self.slowed)
        # Control: the old behaviour (no margin) picks the same candidate at 7199 s and strands the intent.
        self.assertGreater(6300 + delay, 7000)

    def test_a_fresh_hint_is_chosen_over_a_due_reevaluation(self):
        self.reconcile_first_rejection()
        self.advance(600)                                                       # evaluation 2 is due
        self.assertEqual(self.run_dispatch(GOOD, execute=False)['hint']['evaluation'], 2)
        fresh = self.f.append_distinct_migration()
        dry = self.run_dispatch(GOOD, execute=False)
        self.assertEqual((dry['hint']['mint'], dry['hint']['evaluation']), (fresh, 1))

    def test_reevaluations_pause_when_a_bounded_table_is_at_eighty_percent_but_fresh_hints_still_run(self):
        self.reconcile_first_rejection()
        self.advance(600)
        pressure = [{'table': 'paper_cycle_no_entry', 'rows': 6554, 'cap': 8192}]
        with patch.object(watchlist, 'capacity_pressure', return_value=pressure):
            paused = self.run_dispatch(GOOD, execute=False)
            self.assertEqual(paused['status'], 'NO_CANDIDATE')
            self.assertEqual(paused['watchlist_paused_table_capacity'], pressure)   # the health warning payload
            fresh = self.f.append_distinct_migration()
            dry = self.run_dispatch(GOOD, execute=False)
            self.assertEqual((dry['hint']['mint'], dry['hint']['evaluation']), (fresh, 1))
            self.assertEqual(dry['watchlist_paused_table_capacity'], pressure)
        self.assertEqual(len(self.events()), 1)                                  # nothing was written or deleted
        with patch.object(watchlist, 'capacity_pressure', return_value=[]):
            self.assertEqual(self.run_dispatch(GOOD, execute=False)['hint']['mint'], fresh)   # fresh still first


class ShrunkFreshWindowTests(Base):
    """The fixture's provider data cannot survive a two-hour gap (HISTORY_RECOVERY_REQUIRED), so the fresh window is
    shrunk to 1200 s (margin 100 s) for the whole test (journal context included). The logic under test, fresh window vs the
    watchlist's engine window, is identical to 7200 vs 21600."""
    ctx_override = {'maximum_age': 1200, 'preparation_margin': 100}

    def test_a_reevaluation_older_than_the_fresh_window_is_dispatched_end_to_end(self):
        self.run_dispatch(THIN)
        self.advance(300)
        self.assertEqual(self.run_dispatch(GOOD)['status'], 'NO_CANDIDATE')       # reconciles evaluation 1
        self.advance(600)
        (row,) = [e for e in watchlist.Watchlist(self.watch, read_only=True).events()]
        age = self.f.f.at - json.loads(row['hint'])['received_at']
        self.assertGreater(age, 1200)                                             # too old for a fresh candidate
        second = self.run_dispatch(GOOD)
        self.assertEqual((second['status'], second['paper_status']), ('DISPATCHED', 'COMPLETE'), second)
        self.assertEqual([i[3] for i in self.journal('intents')], [1, 2])
        self.assertEqual(list(cycle._state(self.f.ledger, self.cfg)['positions']), [self.f.mint])
        with tool._journal(self.f.journal) as c:                                   # an aged evaluation-2 intent validates
            tool._validate(c, tool.plan(**self.f.args))


class NoMarginTests(Base):
    """Control: the pre-T20F behaviour (no preparation margin) strands the candidate this way."""

    def setUp(self):
        p = patch.object(watchlist, 'PREPARATION_MARGIN_SECONDS', 0)
        p.start(); self.addCleanup(p.stop)
        super().setUp()

    def test_without_the_margin_the_same_preparation_expires_the_candidate_after_the_intent_is_written(self):
        real = tool._preflight
        state = {'slowed': False}

        def slow(ctx, scan=None):
            real(ctx, scan)
            if scan is not None and not state['slowed']:
                state['slowed'] = True
                self.advance(800)                                                  # preparation took 800 s
        _, dry = None, self.run_dispatch(GOOD, execute=False)
        self.advance(7190 - (self.f.f.at - dry['hint']['received_at']))
        self.assertEqual(self.ctx['preparation_margin'], 0)
        with patch.object(tool, '_preflight', side_effect=slow):
            with self.assertRaisesRegex(ValueError, 'expired during preparation'):
                self.run_dispatch(THIN)
        self.assertEqual((len(self.journal('intents')), len(self.journal('results'))), (1, 0))   # the stranded intent


class ReconcileRealShapeTests(unittest.TestCase):
    """T20F item 1 at the dispatcher boundary: the journal result is the real T01 empty-window cycle result."""

    def setUp(self):
        from tests.test_cycle_no_entry import fixture
        self.t = fixture(); self.addCleanup(self.t.doCleanups)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        d = Path(os.path.realpath(self.tmp.name))
        os.chmod(d, 0o700)
        self.watch = d / 'watchlist.sqlite'
        watchlist.initialize(self.watch)
        self.cfgfile = d / 'config.json'
        self.cfgfile.write_text(json.dumps({**config(), watchlist.KEY: 1}))
        self.ctx = {'paths': {'config': {'path': str(self.cfgfile)}, 'watchlist_db': {'path': str(self.watch)}}}
        self.scan = self.t.h.f.target.scan_id
        self.mint = self.t.h.f.target.mint

    def journal(self, result, evaluation=1, at=5000.0):
        c = sqlite3.connect(':memory:')
        c.execute('CREATE TABLE intents(id TEXT,mint TEXT,signature TEXT,evaluation INTEGER,payload TEXT,hash TEXT)')
        c.execute('CREATE TABLE results(id TEXT,payload TEXT,hash TEXT)')
        hint = {'seq': 1, 'payload_hash': 'a' * 64, 'raw_hash': 'b' * 64, 'received_at': 4000.0, 'mint': self.mint,
                'pool': 'POOL', 'signature': 'SIG', 'slot': 5}
        c.execute('INSERT INTO intents VALUES(?,?,?,?,?,?)', ('i1', self.mint, 'SIG', evaluation, canonical({'hint': hint}), ''))
        c.execute('INSERT INTO results VALUES(?,?,?)', ('i1', canonical({'scan_id': self.scan, 'at': at, 'result': result}), ''))
        self.addCleanup(c.close)
        return c

    def events(self):
        with closing(watchlist.Watchlist(self.watch, read_only=True)) as w:
            return w.events(), w.due(5600.0, {**config(), watchlist.KEY: 1})

    def test_empty_window_pass_is_enrolled_and_due_ten_minutes_later_as_evaluation_2(self):
        tool._reconcile_watchlist(self.ctx, self.journal(self.t.result), 5001.0)
        (row,), due = self.events()
        self.assertEqual((row['kind'], row['classification'], row['next_eval_at']), ('ENROLLED', 'NOT_YET', 5600.0))
        self.assertIn('MARKET_PRODUCER_BLOCKED', json.loads(row['codes']))
        self.assertTrue(any(c.startswith('MISSING_WINDOW_MEASUREMENT:') for c in json.loads(row['codes'])))
        self.assertEqual([(d['mint'], d['evaluation']) for d in due], [(self.mint, 2)])

    def test_an_integrity_blocker_in_the_same_result_makes_it_permanent(self):
        bad = copy.deepcopy(self.t.result)
        next(d for d in bad['diagnostics'] if 'blockers' in d)['blockers'].append('COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID')
        tool._reconcile_watchlist(self.ctx, self.journal(bad), 5001.0)
        (row,), due = self.events()
        self.assertEqual((row['kind'], row['classification']), ('PERMANENT', 'PERMANENT'))
        self.assertEqual(due, [])

    def test_a_held_positions_reject_in_the_same_pass_does_not_taint_the_classification(self):
        mixed = copy.deepcopy(self.t.result)
        mixed['outcomes'] = [{'type': 'reject', 'reason': 'DANGER', 'reasons': ['DANGER'], 'mint': 'SOME_HELD_MINT'}]
        mixed['diagnostics'].append({'scan_id': 'another-scan', 'blockers': ['COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID']})
        tool._reconcile_watchlist(self.ctx, self.journal(mixed), 5001.0)
        (row,), _ = self.events()
        self.assertEqual((row['kind'], row['classification']), ('ENROLLED', 'NOT_YET'))


class DefaultOffTests(unittest.TestCase):
    """Without the flag the context, journal schema and selection are exactly the schema-1 ones."""

    def setUp(self):
        self.f = fixture.DispatcherTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_context_is_unchanged_and_a_path_without_the_flag_is_refused(self):
        self.assertNotIn('journal_schema', self.f.ctx)
        self.assertNotIn('watchlist_db', self.f.ctx['paths'])
        self.assertEqual((self.f.ctx['maximum_age'], self.f.ctx['minimum_age']), (7200, 300))
        with self.assertRaisesRegex(ValueError, 'Watchlist path'):
            tool.plan(**self.f.args, watchlist_db=str(self.f.root / 'x.sqlite'))
        with closing(sqlite3.connect(self.f.journal)) as c:
            sql = dict(c.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        self.assertEqual(sql, tool.SCHEMAS)
        self.assertNotIn('evaluation', sql['intents'])

    def test_selected_flag_without_a_path_is_refused(self):
        flagged = self.f.root / 'flagged.json'
        flagged.write_text(canonical({**self.f.cfg, watchlist.KEY: 1}))
        with self.assertRaisesRegex(ValueError, 'Watchlist path'):
            tool.plan(**{**self.f.args, 'config': str(flagged)})

    def test_one_intent_per_mint_remains_enforced_in_schema_one(self):
        result = self.f.live()
        self.assertEqual(result['paper_status'], 'COMPLETE')
        with closing(sqlite3.connect(self.f.journal)) as c:
            row = c.execute('SELECT * FROM intents').fetchone()
            with self.assertRaises(sqlite3.DatabaseError):
                c.execute('INSERT INTO intents VALUES(?,?,?,?,?)', ('f' * 32, row[1], 'other-signature', '{}', 'h'))


if __name__ == '__main__':
    unittest.main()
