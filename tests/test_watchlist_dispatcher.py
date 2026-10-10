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

    def setUp(self):
        self.wdir = Path(os.path.realpath(tempfile.mkdtemp()))
        os.chmod(self.wdir, 0o700)
        self.addCleanup(lambda: __import__('shutil').rmtree(self.wdir, ignore_errors=True))
        self.watch = self.wdir / 'watchlist.sqlite'
        watchlist.initialize(self.watch)
        original = self.real_plan = tool.plan

        def plan(**kw):
            kw.setdefault('watchlist_db', str(self.watch))
            return original(**kw)
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

    def run_dispatch(self, reserves=GOOD, execute=True):
        f = self.f
        outer = f

        class Opener:
            def open(self, request, *, timeout):
                call = json.loads(request.data)
                rows = [outer.raw]
                if call['params'][1]['filters'].get('slot') != {'gte': 10, 'lt': 11}:
                    rows = []
                    for i in range(40):
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
        self.assertEqual((ctx['journal_schema'], ctx['maximum_age']), (2, 21600))
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
        intent['at'] = intent['hint']['received_at'] + 21600 + 1
        result.update(intent_hash=digest(intent), at=intent['at'] + 1)
        self.tamper('UPDATE intents SET payload=' + repr(canonical(intent)) + ',hash=' + repr(digest(intent)))
        self.tamper('UPDATE results SET payload=' + repr(canonical(result)) + ',hash=' + repr(digest(result)))
        with self.assertRaisesRegex(ValueError, 'Dispatch hint grammar invalid'):
            self.run_dispatch(GOOD, execute=False)
        # The same record one second inside the window is valid (the clock must not precede it).
        self.advance(25000)
        intent['at'] = intent['hint']['received_at'] + 21600
        result.update(intent_hash=digest(intent), at=intent['at'] + 1)
        self.tamper('UPDATE intents SET payload=' + repr(canonical(intent)) + ',hash=' + repr(digest(intent)))
        self.tamper('UPDATE results SET payload=' + repr(canonical(result)) + ',hash=' + repr(digest(result)))
        self.run_dispatch(GOOD, execute=False)


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
