"""SYNTHETIC_TEST_ONLY: funnel report with known-count fixtures.

Stores are built with the repo's own schemas (dispatcher journal DDL, the real Ledger class) and
hand-written minimal tables where the report only reads a few columns. One end-to-end test reads
real dispatcher fixture stores (actual discovery frames decoded by the production decoder).
"""
import contextlib
import hashlib
import io
import json
import os
from decimal import Decimal
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from desk.ledger import Ledger
from desk.model import canonical
from tools import paper_entry_dispatcher as dispatcher
from tools.research import funnel_report as fr

BASE = 1_702_080_000                      # 2023-12-09T00:00:00Z, a UTC day boundary
H0, H1, D2 = BASE + 10 * 3600, BASE + 11 * 3600, BASE + 86400 + 3600
NOW = D2 + 1500


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hint(mint, received):
    return {'seq': 1, 'mint': mint, 'pool': 'P-' + mint, 'signature': 'S-' + mint, 'slot': 100, 'migrated_at': float(received)}


def cycle(status='COMPLETE', *, outcomes=(), blockers=(), attempted=9, used=None, scan='x'):
    return {'kind': 'paper_cycle_v1', 'status': status, 'outcomes': list(outcomes), 'blockers': list(blockers),
            'attempted_requests': attempted, 'budget': {scan: {'used': attempted if used is None else used, 'ceiling': 18}}}


BUY = {'type': 'fill', 'side': 'buy', 'mint': 'm'}


class FunnelFixture(unittest.TestCase):
    """13 distinct candidates with known fates (+1 duplicate hint)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.journal, self.research, self.ledger = (self.dir / n for n in ('dispatch.sqlite', 'research.sqlite', 'ledger.sqlite'))
        for ddl in dispatcher.SCHEMAS.values():
            self.sql(self.journal, ddl)
        self.sql(self.research, 'CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
        Ledger(self.ledger).close()
        self.hints = []
        # name: (received, intent delay, scan delay after intent or None, result body or 'NONE', result delay)
        plan = [
            ('M01', H0, 400, 5, cycle(outcomes=[BUY], attempted=9), 15),
            ('M02', H0 + 10, 500, 5, cycle(outcomes=[{'type': 'reject', 'reason': 'COST_BUDGET', 'reasons': ['COST_BUDGET', 'MAX_POSITIONS']}], attempted=9), 15),
            ('M03', H0 + 20, 600, 5, cycle('BLOCKED', blockers=['MARKET_PRODUCER_BLOCKED'], attempted=3), 15),
            ('M04', H0 + 30, 700, 5, {'kind': 'dispatcher_token_rejection_v1', 'token_policy': {'decision': 'SKIP', 'reasons': ['ACTIVE_MINT_AUTHORITY']}, 'requests_after': 1}, 15),
            ('M05', H0 + 40, 800, 5, {'kind': 'history_preparation_no_entry_v1', 'reason': 'INSUFFICIENT_HISTORY'}, 15),
            ('M06', H1, 900, 5, {'kind': 'dispatcher_migration_no_entry_v1', 'reason': 'UNSUPPORTED_QUOTE'}, 15),
            ('M09', H1 + 30, 1000, None, 'NONE', 0),                      # dispatched, never admitted
            ('M10', H1 + 40, 1100, 5, 'NONE', 0),                         # admitted, no result row
            ('M14', H1 + 50, 600, 5, cycle(outcomes=[BUY], attempted=7), 10),   # result BUY, not in ledger
        ]
        for name, received, delay, scan_delay, body, result_delay in plan:
            self.add_candidate(name, received, delay, scan_delay, body, result_delay)
        self.hints.append(hint('M07', H1 + 10))                           # expired, never dispatched
        self.hints.append(hint('M08', H1 + 20))                           # scanned elsewhere, no intent
        self.add_scan('M08', H1 + 20 + 400)
        self.hints.append(hint('M11', NOW - 100))                         # window not open yet
        self.hints.append(hint('M12', NOW - 1000))                        # awaiting dispatch
        self.hints.append(hint('M01', H0 + 999))                          # duplicate of M01
        self.add_buy('M01', H0 + 400 + 5 + 15 + 2)                         # M01's BUY, 2 s after its result

    def sql(self, path, statement, args=()):
        with contextlib.closing(sqlite3.connect(path)) as c, c:
            c.execute(statement, args)

    def add_scan(self, mint, created):
        self.sql(self.research, 'INSERT INTO scans VALUES(?,?,?,?,NULL)', ('scan-' + mint, mint, created, 'COMPLETE'))

    def add_buy(self, mint, ts):
        self.sql(self.ledger, "INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,'{}','h')", ('buy-' + mint, ts))
        self.sql(self.ledger, 'INSERT INTO outcomes(event_id,payload) VALUES(?,?)',
                 ('buy-' + mint, canonical({'type': 'fill', 'side': 'buy', 'mint': mint})))

    def add_candidate(self, name, received, delay, scan_delay, body, result_delay):
        self.hints.append(hint(name, received))
        at = received + delay
        self.sql(self.journal, 'INSERT INTO intents VALUES(?,?,?,?,?)', ('id-' + name, name, 'S-' + name,
                 canonical({'version': 1, 'at': at, 'hint': {'mint': name}}), 'h'))
        if body != 'NONE':
            self.sql(self.journal, 'INSERT INTO results VALUES(?,?,?)', ('id-' + name, canonical({
                'version': 1, 'at': at + (scan_delay or 0) + result_delay, 'scan_id': 'scan-' + name, 'result': body}), 'h'))
        if scan_delay is not None:
            self.add_scan(name, at + scan_delay)

    def build(self, **extra):
        return fr.build(hints=self.hints, journal=self.journal, research_db=self.research, ledger=self.ledger, now=NOW, **extra)


class KnownCountTests(FunnelFixture):
    def test_stage_counts_deaths_and_pending_are_exact(self):
        t = self.build()['total']
        self.assertEqual(t['candidates'], 13)
        self.assertEqual(t['reached'], {'discovered': 13, 'selectable': 11, 'dispatched': 9, 'admitted': 8,
                                        'history_ok': 4, 'observations_ok': 3, 'engine_eligible': 2, 'buy': 2})
        self.assertEqual(t['died_before'], {
            'selectable': {'ALREADY_SCANNED_NO_INTENT': 1},
            'dispatched': {'EXPIRED_NOT_DISPATCHED': 1},
            'admitted': {'UNRESOLVED:DISPATCHED_NOT_ADMITTED': 1},
            'history_ok': {'TOKEN:ACTIVE_MINT_AUTHORITY': 1, 'HISTORY:INSUFFICIENT_HISTORY': 1,
                           'MIGRATION:UNSUPPORTED_QUOTE': 1, 'UNRESOLVED:NO_RESULT': 1},
            'observations_ok': {'OBSERVATIONS:MARKET_PRODUCER_BLOCKED': 1},
            'engine_eligible': {'ENGINE:COST_BUDGET': 1}})
        self.assertEqual(t['pending'], {'selectable': {'WINDOW_NOT_OPEN': 1}, 'dispatched': {'AWAITING_DISPATCH': 1}})
        self.assertEqual(t['extra_reasons'], {'ENGINE:MAX_POSITIONS': 1})
        self.assertEqual(t['flags'], {'RESULT_BUY_NOT_IN_LEDGER': 1})
        deaths = sum(n for r in t['died_before'].values() for n in r.values())
        pending = sum(n for r in t['pending'].values() for n in r.values())
        self.assertEqual(t['reached']['buy'] + deaths + pending, t['candidates'], 'every candidate ends exactly once')
        self.assertEqual(self.build()['duplicate_hints_ignored'], 1)

    def test_hour_and_day_cohorts(self):
        r = self.build()
        hours = {k: (v['candidates'], v['reached']['buy']) for k, v in r['by_hour'].items()}
        self.assertEqual(hours, {'2023-12-09T10:00:00Z': (5, 1), '2023-12-09T11:00:00Z': (6, 1), '2023-12-10T01:00:00Z': (2, 0)})
        days = {k: (v['candidates'], v['reached']['dispatched'], v['reached']['buy']) for k, v in r['by_day'].items()}
        self.assertEqual(days, {'2023-12-09': (11, 9, 2), '2023-12-10': (2, 0, 0)})
        self.assertEqual(sum(v['candidates'] for v in r['by_hour'].values()), r['candidates'])

    def test_budget_usage_and_median_times(self):
        t = self.build()['total']
        by = t['requests_by_ending_stage']
        self.assertEqual((by['buy']['candidates_with_data'], by['buy']['attempted_total'], by['buy']['attempted_median']), (2, 16, 8.0))
        self.assertEqual((by['engine_eligible']['attempted_total'], by['engine_eligible']['investigation_used_total']), (9, 9))
        self.assertEqual(by['observations_ok']['attempted_total'], 3)
        self.assertEqual(by['history_ok']['attempted_total'], 1)         # token rejection: one charged read
        self.assertEqual(t['median_seconds']['discovery_to_dispatch'], 700.0)    # delays 400..1100, 600 twice -> median 700
        self.assertEqual(t['samples_seconds']['discovery_to_dispatch'], 9)
        self.assertEqual(t['median_seconds']['dispatch_to_admission'], 5.0)
        self.assertEqual(t['median_seconds']['admission_to_result'], 15.0)   # six results after 15 s, one (M14) after 10 s
        self.assertEqual(t['median_seconds']['result_to_buy'], 2.0)

    def test_ledger_buy_is_authoritative_when_result_lacks_it(self):
        self.add_buy('M02', H0 + 700)   # result said engine reject, ledger says it bought
        t = self.build()['total']
        self.assertEqual(t['reached']['buy'], 3)
        self.assertEqual(t['flags']['LEDGER_BUY_NOT_IN_RESULT'], 1)
        self.assertNotIn('engine_eligible', t['died_before'])

    def test_unreadable_journal_rows_are_unresolved_not_normal_rejections(self):
        self.sql(self.journal, "UPDATE results SET payload='not json' WHERE id='id-M03'")
        self.sql(self.journal, "UPDATE intents SET payload='not json' WHERE id='id-M04'")
        r = self.build()
        self.assertEqual(r['journal_unreadable_intents'], 1)
        died = r['total']['died_before']
        self.assertEqual(died['history_ok']['UNRESOLVED:RESULT_UNREADABLE'], 2)    # M03 (result) and M04 (intent)
        self.assertEqual(died['history_ok']['UNRESOLVED:NO_RESULT'], 1)            # M10 only
        self.assertNotIn('OBSERVATIONS:MARKET_PRODUCER_BLOCKED', died.get('observations_ok', {}))
        self.assertNotIn('TOKEN:ACTIVE_MINT_AUTHORITY', died['history_ok'])
        # a corrupt row is never turned into a typed normal rejection; the ledger still wins for M01
        self.sql(self.journal, "UPDATE results SET payload='not json' WHERE id='id-M01'")
        r = self.build()['total']
        self.assertEqual((r['reached']['buy'], r['flags'].get('LEDGER_BUY_NOT_IN_RESULT')), (2, 1))

    def test_decisions_budgets_and_counterfactual_sections(self):
        evidence = self.dir / 'evidence.sqlite'
        with contextlib.closing(sqlite3.connect(evidence)) as c, c:
            c.execute('CREATE TABLE ownership_budgets(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,used INTEGER NOT NULL,ceiling INTEGER NOT NULL)')
            c.executemany('INSERT INTO ownership_budgets VALUES(?,?,?,?)', [('a', 'h', 18, 18), ('b', 'h', 3, 18)])
            c.execute('CREATE TABLE paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.executemany('INSERT INTO paper_observation_passes VALUES(?,?,?)', [('1', 'i', 'o'), ('2', 'i', None)])
        decisions = self.dir / 'decisions.sqlite'
        with contextlib.closing(sqlite3.connect(decisions)) as c, c:
            c.execute('CREATE TABLE decisions(scan_id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,source_payload TEXT NOT NULL,decision TEXT NOT NULL)')
            c.executemany('INSERT INTO decisions VALUES(?,?,?,?)', [('1', 'h', '{}', '{"action":"SKIP"}'), ('2', 'h', '{}', '{"action":"SKIP"}'),
                                                                     ('3', 'h', '{}', '{"action":"WATCH"}')])
        cf = self.dir / 'counterfactual.sqlite'
        with contextlib.closing(sqlite3.connect(cf)) as c, c:
            c.execute('CREATE TABLE samples(mint TEXT NOT NULL,horizon INTEGER NOT NULL,status TEXT NOT NULL,price TEXT,PRIMARY KEY(mint,horizon))')
            c.executemany('INSERT INTO samples VALUES(?,?,?,?)', [
                ('M01', 300, 'OK', '1.0'), ('M01', 3600, 'OK', '2.0'),                      # BUY group, +100%
                ('M02', 300, 'OK', '1.0'), ('M02', 3600, 'OK', '0.5'),                      # engine COST_BUDGET group, -50%
                ('M04', 300, 'OK', '1.0'), ('M04', 3600, 'POOL_DEAD', None)])               # token group, pool died
        r = self.build(evidence_db=evidence, decisions_db=decisions, counterfactual_store=cf, horizon=3600)
        self.assertEqual((r['budgets']['budgets_exhausted'], r['budgets']['requests_used'], r['budgets']['observation_passes']),
                         (1, 21, {'total': 2, 'unresolved_null': 1}))
        self.assertEqual(r['decisions']['by_label'], {'SKIP': 2, 'WATCH': 1})
        groups = r['counterfactual']['groups']
        self.assertEqual((groups['BUY']['candidates'], groups['BUY']['with_return'], Decimal(groups['BUY']['median_return'])), (2, 1, Decimal(1)))
        cost = groups['DIED:ENGINE:COST_BUDGET']
        self.assertEqual((cost['with_return'], Decimal(cost['median_return'])), (1, Decimal('-0.5')))
        token = groups['DIED:TOKEN:ACTIVE_MINT_AUTHORITY']
        self.assertEqual((token['pool_died'], token['with_return']), (1, 0))
        self.assertEqual(self.build()['counterfactual'], {'status': 'NOT_PROVIDED'})


class ReadOnlyTests(FunnelFixture):
    def test_inputs_are_never_modified(self):
        paths = [self.journal, self.research, self.ledger]
        for p in paths:                        # leave no sidecars: plain files at rest
            with contextlib.closing(sqlite3.connect(p)) as c:
                c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = {str(p): (sha(p), p.stat().st_mtime_ns) for p in paths}
        listing = sorted(os.listdir(self.dir))
        self.build()
        self.assertEqual({str(p): (sha(p), p.stat().st_mtime_ns) for p in paths}, before)
        self.assertEqual(sorted(os.listdir(self.dir)), listing, 'no sidecar or other file may appear')

    def test_open_mode_rule_matches_status_tool(self):
        wal = self.dir / 'wal.sqlite'
        with contextlib.closing(sqlite3.connect(wal, isolation_level=None)) as c:
            c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE t(x)'); c.execute('INSERT INTO t VALUES(1)')
        self.assertEqual(sorted(p.name for p in self.dir.glob('wal.sqlite*')), ['wal.sqlite'])
        self.assertEqual(fr.open_mode(wal), 'immutable')
        with contextlib.closing(fr.connect_ro(wal)) as c:
            self.assertEqual(c.execute('SELECT x FROM t').fetchall(), [(1,)])
        self.assertEqual(sorted(p.name for p in self.dir.glob('wal.sqlite*')), ['wal.sqlite'], 'immutable read creates no sidecars')
        self.assertEqual(fr.open_mode(self.journal), 'ro', 'rollback-journal stores are never immutable')
        writer = sqlite3.connect(wal, isolation_level=None)       # live writer: -wal exists -> plain ro, sees committed rows
        self.addCleanup(writer.close)
        writer.execute('PRAGMA journal_mode=WAL'); writer.execute('PRAGMA wal_autocheckpoint=0'); writer.execute('INSERT INTO t VALUES(2)')
        self.assertEqual(fr.open_mode(wal), 'ro')
        with contextlib.closing(fr.connect_ro(wal)) as c:
            self.assertEqual(c.execute('PRAGMA query_only').fetchone(), (1,))   # defence in depth on top of mode=ro
            self.assertEqual(c.execute('SELECT x FROM t ORDER BY x').fetchall(), [(1,), (2,)])
            with self.assertRaises(sqlite3.OperationalError):
                c.execute('INSERT INTO t VALUES(9)')

    def test_symlinked_hardlinked_or_missing_stores_are_refused(self):
        link = self.dir / 'link.sqlite'; link.symlink_to(self.journal)
        hard = self.dir / 'hard.sqlite'; os.link(self.research, hard)
        for path, code in ((link, 'SYMLINKED_STORE'), (hard, 'STORE_NOT_SINGLE_REGULAR_FILE'), (self.dir / 'nope.sqlite', 'STORE_MISSING')):
            with self.subTest(code=code), self.assertRaises(fr.FunnelError) as raised:
                fr.connect_ro(path)
            self.assertEqual(raised.exception.code, code)
        out = io.StringIO()
        with patch.object(fr, 'discovery_hints', return_value=(self.hints, None)), contextlib.redirect_stdout(out):
            status = fr.main(['--discovery-db', str(self.journal), '--journal', str(link), '--research-db', str(self.research),
                              '--ledger', str(self.ledger)])
        self.assertEqual((status, json.loads(out.getvalue())['code']), (2, 'SYMLINKED_STORE'))

    def test_missing_tables_fail_closed_instead_of_zero_counts(self):
        empty = self.dir / 'empty.sqlite'; sqlite3.connect(empty).close()
        for kwargs, code in (({'journal': empty}, 'JOURNAL_TABLES_MISSING'), ({'research_db': empty}, 'SCANS_TABLE_MISSING'),
                             ({'ledger': empty}, 'LEDGER_TABLES_MISSING')):
            args = dict(hints=self.hints, journal=self.journal, research_db=self.research, ledger=self.ledger, now=NOW); args.update(kwargs)
            with self.subTest(code=code), self.assertRaises(fr.FunnelError) as raised:
                fr.build(**args)
            self.assertEqual(raised.exception.code, code)


class HtmlTests(FunnelFixture):
    def test_single_file_no_external_assets_and_escaped(self):
        with contextlib.closing(sqlite3.connect(self.journal)) as c:       # hostile code string coming from stored data
            row = json.loads(c.execute("SELECT payload FROM results WHERE id='id-M03'").fetchone()[0])
        row['result']['blockers'] = ['<script>alert(1)</script>"&']
        self.sql(self.journal, "UPDATE results SET payload=? WHERE id='id-M03'", (canonical(row),))
        text = fr.render_html(self.build())
        lowered = text.lower()
        for forbidden in ('http://', 'https://', '<script', '<link', ' src=', '@import', 'url('):
            self.assertNotIn(forbidden, lowered.replace('&lt;script&gt;', ''), forbidden)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', text)
        for stage in fr.STAGES:
            self.assertIn(stage, text)
        self.assertIn('2023-12-09T10:00:00Z', text)
        self.assertEqual(text.count('<!doctype html>'), 1)

    def test_output_is_created_exclusively_and_private(self):
        out = self.dir / 'funnel.html'
        stdout = io.StringIO()
        argv = ['--discovery-db', str(self.journal), '--journal', str(self.journal), '--research-db', str(self.research),
                '--ledger', str(self.ledger), '--out-html', str(out), '--now', str(NOW)]
        with patch.object(fr, 'discovery_hints', return_value=(self.hints, {'frames': 0, 'altered': 0, 'undecodable': 0,
                                                                              'not_migration': 0, 'ambiguous': 0, 'truncated': False})):
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(fr.main(argv), 0)
                self.assertEqual(out.stat().st_mode & 0o777, 0o600)
                self.assertIn('Candidate funnel', out.read_text())
                first = out.read_text()
                self.assertEqual(fr.main(argv), 2)              # refuses to overwrite
            self.assertEqual(out.read_text(), first)
            link = self.dir / 'link.html'; link.symlink_to(self.dir / 'elsewhere.html')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fr.main(argv[:-4] + ['--out-html', str(link), '--now', str(NOW)]), 2)
            self.assertFalse((self.dir / 'elsewhere.html').exists())


class RealFlowTests(unittest.TestCase):
    """Actual dispatcher fixtures: real discovery frame, real dispatch to a paper BUY, real ledger."""

    def test_one_real_candidate_reaches_buy_and_tampered_frame_is_not_used(self):
        from tests import test_paper_entry_dispatcher as fixtures
        d = fixtures.DispatcherTests('test_dry_run_no_admission_credentials_or_io_and_context_activation')
        d.setUp(); self.addCleanup(d.doCleanups)
        self.assertEqual(d.live()['status'], 'DISPATCHED')
        now = d.f.at + 100
        r = fr.build(discovery_db=d.discovery, journal=d.journal, research_db=d.f.jobs.path, ledger=d.ledger,
                     evidence_db=d.f.progress.store.path, now=now)
        self.assertEqual((r['frames']['frames'], r['frames']['altered'], r['candidates']), (r['frames']['frames'], 0, 1))
        self.assertEqual(r['total']['reached'], {s: 1 for s in fr.STAGES}, r['total'])
        self.assertEqual((r['total']['died_before'], r['total']['pending'], r['total']['flags']), ({}, {}, {}))
        self.assertGreaterEqual(r['ledger']['buy_fills_distinct_mints'], 1)
        # The CLI path on the same real stores: JSON on stdout plus an exclusive single-file HTML report.
        out_dir = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, out_dir, True)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = fr.main(['--discovery-db', str(d.discovery), '--journal', str(d.journal), '--research-db', str(d.f.jobs.path),
                              '--ledger', str(d.ledger), '--evidence-db', str(d.f.progress.store.path), '--now', str(now),
                              '--out-html', str(out_dir / 'funnel.html')])
        cli = json.loads(stdout.getvalue())
        self.assertEqual((status, cli['candidates'], cli['total']['reached']['buy'], cli['html_written']), (0, 1, 1, True))
        self.assertIn('Candidate funnel', (out_dir / 'funnel.html').read_text())
        # A discovery frame whose stored hash no longer matches its payload is counted and never used.
        copy = Path(tempfile.mkdtemp()).resolve(); self.addCleanup(shutil.rmtree, copy, True)
        altered = copy / 'discovery.sqlite'
        shutil.copyfile(d.discovery, altered)
        with contextlib.closing(sqlite3.connect(altered)) as c, c:
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute('UPDATE raw_events SET payload_hash=?', ('0' * 64,))
        r2 = fr.build(discovery_db=altered, journal=d.journal, research_db=d.f.jobs.path, ledger=d.ledger, now=now)
        self.assertEqual((r2['candidates'], r2['frames']['altered']), (0, r['frames']['frames']))


if __name__ == '__main__':
    unittest.main()
