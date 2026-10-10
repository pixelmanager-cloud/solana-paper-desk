"""SYNTHETIC_TEST_ONLY: candidate feature store. Known answers from stores built with the repo's real writers.

The ledger is written by the real ``Ledger.apply`` + ``engine.transition`` (so outcomes and reject reasons are the engine's own),
the dispatcher journal by the dispatcher's DDL, the counterfactual store by its own ``init``/``add_candidate``, and one end-to-end
test reads the real stores of a dispatched paper entry (actual discovery frame, journal, ledger, research and evidence DBs).
Adversarial cases: look-ahead stamps, missing measurement times, future-dated evidence, tampered rows, hostile paths.
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import canonical
from desk.security import TOKEN_2022
from tests import helpers
from tools import paper_entry_dispatcher as dispatcher
from tools.research import counterfactual as cf
from tools.research import features as ft
from tools.research import shadow_strategies as sh

T = helpers.T
NOW = T + 100_000
REPO = Path(__file__).resolve().parents[1]
FRESH = REPO / 'deploy' / 'fresh'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sql(path, statement, args=()):
    with closing(sqlite3.connect(path)) as c, c:
        return c.execute(statement, args).fetchall()


def hint(mint, received, seq=1, slot=100):
    return {'seq': seq, 'mint': mint, 'pool': 'P-' + mint, 'signature': 'S-' + mint, 'slot': slot, 'migrated_at': float(received)}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.ledger, self.store = self.dir / 'ledger.sqlite', self.dir / 'features.sqlite'
        self.cfg = helpers.config()
        ledger = Ledger(self.ledger)
        # A: clean, buys. B: rejected by the engine (market cap). Both through the REAL writer.
        for e in (helpers.event(ts=T, mint='A'), helpers.event(ts=T + 60, mint='B', market_cap_usd='1000')):
            ledger.apply(e, self.cfg, transition, initial_state)
        ledger.close()
        ft.init(self.store)

    def raw_event(self, **changes):
        """An event written straight into the journal table (hostile / unusual shapes the engine would refuse)."""
        e = helpers.event(**changes)
        sql(self.ledger, 'INSERT INTO events(event_id,ts,payload,payload_hash) VALUES(?,?,?,?)',
            (e['event_id'], e['ts'], canonical(e), 'h-' + e['event_id']))
        return e

    def ingest(self, **kw):
        kw.setdefault('now', NOW)
        kw.setdefault('ledger_db', self.ledger)
        return ft.ingest(self.store, **kw)

    def rows(self, **kw):
        return {(r['mint'], r['as_of']): r for r in ft.read_rows(self.store, **kw)}

    def row(self, mint, ts):
        return self.rows()[(mint, float(ts))]


class KnownAnswerTests(Fixture):
    def test_ledger_event_features_and_provenance(self):
        report = self.ingest()
        self.assertEqual((report['inserted'], report['invalid'], report['conflict']), (2, 0, 0))
        a = self.row('A', T)
        f = a['features']
        self.assertEqual((f['market_cap_usd'], f['reserve_sol'], f['top10_pct'], f['net_buy_ratio'], f['unique_buyers_5m']),
                         ('100000', '100', '10', '.8', 50))
        self.assertEqual(f['liquidity_usd'], '30000')                   # 2 * 100 SOL * 150 USD
        self.assertEqual(f['age_since_migration_seconds'], 600)         # ts - graduated_at
        self.assertEqual((f['token_2022'], f['holders_listed'], f['early_buys_listed'], f['holder_coverage_pct']), (False, 100, 100, '100'))
        self.assertIs(f['entered'], True)
        prov = a['provenance']
        self.assertEqual(prov['market_cap_usd']['source'], 'ledger.events')
        self.assertTrue(prov['market_cap_usd']['ref'].startswith('A:%d#' % T))
        self.assertEqual(prov['entered']['role'], 'decision')
        self.assertEqual(prov['top10_pct']['role'], 'input')
        self.assertEqual(prov['liquidity_usd']['derived'], '2*reserve_sol*sol_usd')

    def test_rejection_reasons_come_from_the_engines_own_outcomes(self):
        self.ingest()
        b = self.row('B', T + 60)
        self.assertEqual(b['features']['reject_reasons'], ['MARKET_CAP', 'STALE_PORTFOLIO'])
        self.assertNotIn('entered', b['features'])
        self.assertEqual(b['provenance']['reject_reasons']['source'], 'ledger.outcomes')
        self.assertEqual(b['features']['market_cap_usd'], '1000')

    def test_token_2022_flag_and_age_use_the_recorded_profile(self):
        e = helpers.event(ts=T + 5, mint='T22')
        e['token_evidence']['account']['owner'] = TOKEN_2022
        self.raw_event(**{k: e[k] for k in ('ts', 'mint', 'token_evidence')})
        self.ingest()
        self.assertIs(self.row('T22', T + 5)['features']['token_2022'], True)

    def test_discovery_and_journal_rows(self):
        journal = self.dir / 'dispatch.sqlite'
        for ddl in dispatcher.SCHEMAS.values():
            sql(journal, ddl)
        sql(journal, 'INSERT INTO intents VALUES(?,?,?,?,?)', ('id-D', 'D', 'S-D', canonical({'version': 1, 'at': T + 400}), 'h'))
        body = {'kind': 'paper_cycle_v1', 'status': 'COMPLETE', 'attempted_requests': 9,
                'outcomes': [{'type': 'reject', 'reason': 'COST_BUDGET', 'reasons': ['COST_BUDGET'], 'mint': 'D'}]}
        sql(journal, 'INSERT INTO results VALUES(?,?,?)', ('id-D', canonical({'version': 1, 'at': T + 420, 'scan_id': 'scan-D', 'result': body}), 'h'))
        self.ingest(hints=[hint('D', T + 10, seq=7, slot=555)], journal_db=journal)
        rows = self.rows()
        d0, d1, d2 = rows[('D', float(T + 10))], rows[('D', float(T + 400))], rows[('D', float(T + 420))]
        self.assertEqual((d0['features']['migrated_at'], d0['features']['migration_slot']), (T + 10.0, 555))
        self.assertEqual(d0['provenance']['migration_slot']['ref'], 'discovery.raw_events#7')
        self.assertIs(d1['features']['dispatched'], True)
        self.assertEqual((d2['features']['dispatch_death_reason'], d2['features']['dispatch_stage_reached'], d2['features']['requests_charged']),
                         ('ENGINE:COST_BUDGET', 'observations_ok', 9))
        self.assertEqual({p['role'] for p in d2['provenance'].values()}, {'decision'})

    def test_counterfactual_samples_are_known_only_from_their_sample_time(self):
        store = self.dir / 'cf.sqlite'
        cf.init(store, now=T)
        cf.add_candidate(store, mint='C', pool='P', signature='S', slot=1, migrated_at=T, seq=1, now=T)
        sql(store, "INSERT INTO samples(mint,horizon,due_at,sampled_at,slot,base_raw,quote_raw,price,status) VALUES('C',300,?,?,5,'2000','80000000000','1','OK')",
            (T + 300, T + 302))
        sql(store, "INSERT INTO samples(mint,horizon,due_at,sampled_at,status) VALUES('C',900,?,NULL,'PENDING')", (T + 900,))
        self.ingest(counterfactual_store=store)
        c = self.row('C', T + 302)
        self.assertEqual((c['features']['cf_base_vault_raw'], c['features']['cf_quote_vault_raw'], c['features']['cf_sample_horizon_seconds']),
                         ('2000', '80000000000', 300))
        sql(store, "INSERT INTO samples(mint,horizon,due_at,sampled_at,slot,base_raw,quote_raw,price,status) VALUES('C',1800,?,?,6,'1','1','1','POOL_DEAD')",
            (T + 1800, T + 1802))
        self.ingest(counterfactual_store=store)
        self.assertEqual(len([k for k in self.rows() if k[0] == 'C']), 1, 'unsampled and dead-pool horizons contribute nothing')

    def test_decisions_need_their_own_time_and_a_mint(self):
        decisions, research = self.dir / 'decisions.sqlite', self.dir / 'research.sqlite'
        sql(decisions, 'CREATE TABLE decisions(scan_id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,source_payload TEXT NOT NULL,decision TEXT NOT NULL)')
        sql(research, 'CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
        sql(research, "INSERT INTO scans VALUES('s1','E',1,'COMPLETE',NULL)")
        for scan, payload, decision in (('s1', {}, {'action': 'WATCH', 'ts': T + 50}),
                                        ('s2', {'mint': 'E2'}, {'action': 'SKIP'}),              # no time: cannot be placed
                                        ('s3', {'mint': 'E3'}, {'action': 'BUY', 'ts': T + 60})):
            sql(decisions, 'INSERT INTO decisions VALUES(?,?,?,?)', (scan, 'h', json.dumps(payload), json.dumps(decision)))
        report = self.ingest(decisions_db=decisions, research_db=research)
        self.assertEqual(self.row('E', T + 50)['features']['decision_label'], 'WATCH')
        self.assertEqual(self.row('E3', T + 60)['features']['decision_label'], 'BUY')
        self.assertNotIn('E2', {m for m, _ in self.rows()})
        self.assertEqual(report['skipped']['DECISION_NOT_PLACEABLE'], 1)


class NoLookAheadTests(Fixture):
    def test_measurement_stamped_after_the_event_is_dropped_and_counted(self):
        self.raw_event(ts=T + 10, mint='L', holder_at=T + 40)
        report = self.ingest()
        f = self.row('L', T + 10)['features']
        for name in ('top10_pct', 'dev_pct', 'bundle_pct', 'cluster_pct', 'fresh_wallet_ratio'):
            self.assertNotIn(name, f)
        self.assertIn('net_buy_ratio', f, 'other groups of the same event are unaffected')
        self.assertGreaterEqual(report['skipped']['LOOK_AHEAD_DROPPED'], 1)

    def test_every_stored_feature_is_measured_at_or_before_its_row(self):
        self.raw_event(ts=T + 10, mint='L', price_at=T + 11, flow_at=T + 12, graduated_at=T + 99)
        self.ingest()
        for row in ft.read_rows(self.store):
            for name, prov in row['provenance'].items():
                self.assertLessEqual(prov['at'], row['as_of'], (row['mint'], name))
        f = self.row('L', T + 10)['features']
        self.assertNotIn('age_since_migration_seconds', f)         # graduated_at in the future
        self.assertNotIn('market_cap_usd', f)
        self.assertNotIn('net_buy_ratio', f)
        self.assertNotIn('liquidity_usd', f)

    def test_collector_refuses_a_measurement_later_than_its_row(self):
        col = ft.Collector(now=NOW)
        self.assertFalse(col.add('m', 100, 'x', 1, source='s', ref='r', at=101))
        self.assertTrue(col.add('m', 100, 'x', 1, source='s', ref='r', at=100))
        self.assertEqual((col.skipped, len(col.records)), ({'LOOK_AHEAD_DROPPED': 1}, 1))
        self.assertFalse(col.add('m', 100, 'x', 2, source='s2', ref='r2'))           # same time, other value: first writer wins, counted
        self.assertEqual(col.records[('m', 100.0)]['features']['x'], 1)
        self.assertEqual(col.skipped['SAME_TIME_SOURCE_DISAGREEMENT'], 1)

    def test_token_and_bundle_evidence_observed_after_the_event_are_not_used(self):
        e = helpers.event(ts=T + 70, mint='K')
        e['token_evidence']['observed_at'] = T + 80
        e['bundle_evidence']['as_of'] = T + 90
        self.raw_event(**{k: e[k] for k in ('ts', 'mint', 'token_evidence', 'bundle_evidence')})
        report = self.ingest()
        f = self.row('K', T + 70)['features']
        for name in ('token_2022', 'holders_listed', 'early_buys_listed', 'holder_coverage_pct'):
            self.assertNotIn(name, f)
        self.assertEqual((report['skipped'].get('TOKEN_PROFILE_UNUSABLE'), report['skipped'].get('BUNDLE_EVIDENCE_TIME_UNUSABLE')), (1, 1))
        self.assertNotIn('LOOK_AHEAD_DROPPED', report['skipped'])

    def test_unknown_stays_unknown_no_defaults(self):
        self.raw_event(ts=T + 20, mint='U', holder_at=None)
        report = self.ingest()
        f = self.row('U', T + 20)['features']
        self.assertEqual(sorted(set(f) & {'top10_pct', 'dev_pct', 'bundle_pct'}), [])
        self.assertGreaterEqual(report['skipped']['MEASUREMENT_TIME_UNKNOWN'], 1)

    def test_missing_measurement_time_drops_the_value_rather_than_assuming_now(self):
        e = helpers.event(ts=T + 30, mint='M')
        del e['flow_at']
        self.raw_event(**{k: v for k, v in e.items() if k not in ('schema_version',)})
        sql(self.ledger, 'UPDATE events SET payload=? WHERE event_id=?', (canonical(e), e['event_id']))
        self.ingest()
        self.assertNotIn('net_buy_ratio', self.row('M', T + 30)['features'])

    def test_evidence_dated_after_now_is_refused(self):
        self.raw_event(ts=NOW + 5, mint='F', price_at=NOW + 5, holder_at=NOW + 5, flow_at=NOW + 5, momentum_at=NOW + 5, graduated_at=T)
        report = self.ingest()
        self.assertNotIn('F', {m for m, _ in self.rows()})
        self.assertGreaterEqual(report['skipped']['FUTURE_DATED_EVIDENCE'], 1)

    def test_invalid_values_are_skipped_not_repaired(self):
        self.raw_event(ts=T + 40, mint='V', top10_pct='NaN', danger='no', unique_buyers_5m=True, market_cap_usd='1e999999',
                       dev_pct=True, flow=7.5)
        report = self.ingest()
        f = self.rows().get(('V', float(T + 40)), {'features': {}})['features']
        for name in ('top10_pct', 'danger', 'unique_buyers_5m', 'dev_pct', 'flow', 'market_cap_usd'):
            self.assertNotIn(name, f)
        self.assertGreaterEqual(report['skipped']['FEATURE_VALUE_INVALID'], 6)

    def test_validate_row_rejects_look_ahead_and_unprovenanced_rows(self):
        ok = {'x': {'source': 's', 'ref': 'r', 'at': 10.0, 'role': 'input'}}
        self.assertIsNone(ft.validate_row('m', 10.0, {'x': 1}, ok))
        self.assertEqual(ft.validate_row('m', 9.0, {'x': 1}, ok), 'LOOK_AHEAD')
        self.assertEqual(ft.validate_row('m', 10.0, {'x': 1, 'y': 2}, ok), 'PROVENANCE_MISMATCH')
        self.assertEqual(ft.validate_row('m', 10.0, {}, {}), 'PROVENANCE_MISMATCH')
        self.assertEqual(ft.validate_row('m', float('nan'), {'x': 1}, ok), 'BAD_AS_OF')
        self.assertEqual(ft.validate_row('', 10.0, {'x': 1}, ok), 'BAD_MINT')
        self.assertEqual(ft.validate_row('m', 10.0, {'x': 1}, {'x': dict(ok['x'], ref='')}), 'NO_PROVENANCE')
        self.assertEqual(ft.validate_row('m', 10.0, {'x': 1}, {'x': dict(ok['x'], role='label')}), 'BAD_ROLE')
        self.assertEqual(ft.validate_row('m', 10.0, {'x': 1}, {'x': dict(ok['x'], at=None)}), 'BAD_FEATURE_TIME')
        self.assertEqual(ft.validate_row('m', 10.0, {'x': 'z' * 20000}, ok), 'ROW_TOO_LARGE')


class StoreIntegrityTests(Fixture):
    def test_rerun_is_idempotent_and_never_rewrites_rows(self):
        first = self.ingest()
        before = [(r['mint'], r['as_of'], r['row_hash']) for r in ft.read_rows(self.store)]
        again = self.ingest(now=NOW + 1)
        self.assertEqual((again['inserted'], again['duplicate'], again['conflict']), (0, first['inserted'], 0))
        self.assertEqual(before, [(r['mint'], r['as_of'], r['row_hash']) for r in ft.read_rows(self.store)])
        self.assertEqual(sql(self.store, 'SELECT COUNT(*) FROM ingest_runs')[0][0], 4)    # two runs, each start + summary

    def test_row_hashes_do_not_depend_on_ingest_order_or_time(self):
        self.ingest(now=NOW)
        other = self.dir / 'other.sqlite'
        ft.init(other)
        ft.ingest(other, ledger_db=self.ledger, now=NOW + 5000, since=T + 30)
        ft.ingest(other, ledger_db=self.ledger, now=NOW + 9000, until=T + 30)
        mine = {(r['mint'], r['as_of']): r['row_hash'] for r in ft.read_rows(self.store)}
        theirs = {(r['mint'], r['as_of']): r['row_hash'] for r in ft.read_rows(other)}
        self.assertEqual(mine, theirs)

    def test_different_content_for_an_existing_key_is_a_recorded_conflict_not_an_overwrite(self):
        self.ingest()
        kept = self.row('A', T)
        offered = {('A', float(T)): {'features': {'market_cap_usd': '1'}, 'provenance': {
            'market_cap_usd': {'source': 'x', 'ref': 'y', 'at': float(T), 'role': 'input'}}}}
        for _ in range(2):
            counts = ft.append_rows(self.store, offered, now=NOW)
            self.assertEqual((counts['conflict'], counts['inserted']), (1, 0))
        self.assertEqual(self.row('A', T), kept)
        self.assertEqual(sql(self.store, 'SELECT COUNT(*) FROM feature_conflicts')[0][0], 1, 'the same offer is recorded once')
        self.assertEqual(sql(self.store, 'SELECT kept_hash FROM feature_conflicts')[0][0], kept['row_hash'])

    def test_invalid_rows_are_never_stored(self):
        bad = {('A', 5.0): {'features': {'x': 1}, 'provenance': {'x': {'source': 's', 'ref': 'r', 'at': 9.0, 'role': 'input'}}}}
        self.assertEqual(ft.append_rows(self.store, bad, now=NOW)['invalid'], 1)
        self.assertEqual(sql(self.store, 'SELECT COUNT(*) FROM feature_rows')[0][0], 0)

    def test_records_are_append_only(self):
        self.ingest()
        ft.append_rows(self.store, {('A', float(T)): {'features': {'market_cap_usd': '1'}, 'provenance': {
            'market_cap_usd': {'source': 'x', 'ref': 'y', 'at': float(T), 'role': 'input'}}}}, now=NOW)   # so the conflicts table has a row
        for table in ft.GUARDED:
            self.assertTrue(sql(self.store, f'SELECT COUNT(*) FROM {table}')[0][0], table)
        for table in ft.GUARDED:
            for statement in (f'UPDATE {table} SET rowid=rowid', f'DELETE FROM {table}'):
                with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                    sql(self.store, statement)
        with self.assertRaises(sqlite3.IntegrityError):
            sql(self.store, "UPDATE feature_rows SET features='{}'")

    def test_verify_detects_tampered_rows(self):
        self.ingest()
        self.assertTrue(ft.verify(self.store)['ok'])
        copy_ = self.dir / 'tampered.sqlite'
        shutil.copyfile(self.store, copy_)
        with closing(sqlite3.connect(copy_)) as c, c:
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute("UPDATE feature_rows SET features=replace(features,'\"100000\"','\"999\"') WHERE mint='A'")
            c.execute("UPDATE feature_rows SET as_of=as_of-1000 WHERE mint='B'")
        report = ft.verify(copy_)
        self.assertFalse(report['ok'])
        self.assertEqual({(b['mint'], b['code']) for b in report['bad']}, {('A', 'ROW_HASH_MISMATCH'), ('B', 'LOOK_AHEAD')})

    def test_init_refuses_existing_or_symlinked_paths(self):
        with self.assertRaises(ft.FeatureError) as ctx:
            ft.init(self.store)
        self.assertEqual(ctx.exception.code, 'STORE_EXISTS')
        link = self.dir / 'link.sqlite'
        link.symlink_to(self.dir / 'nowhere.sqlite')
        with self.assertRaises(ft.FeatureError):
            ft.init(link)
        self.assertFalse((self.dir / 'nowhere.sqlite').exists())
        with self.assertRaises(ft.FeatureError) as ctx:
            ft.verify(link)
        self.assertEqual(ctx.exception.code, 'SYMLINKED_STORE')


class InputSafetyTests(Fixture):
    def test_input_stores_are_never_modified(self):
        journal = self.dir / 'dispatch.sqlite'
        for ddl in dispatcher.SCHEMAS.values():
            sql(journal, ddl)
        store = self.dir / 'cf.sqlite'
        cf.init(store, now=T)
        before = {p: sha(p) for p in (self.ledger, journal, store)}
        self.ingest(journal_db=journal, counterfactual_store=store)
        self.assertEqual(before, {p: sha(p) for p in before})

    def test_symlinked_hardlinked_or_missing_inputs_fail_closed(self):
        link = self.dir / 'link.sqlite'
        link.symlink_to(self.ledger)
        with self.assertRaises(ft.fr.FunnelError):
            ft.ingest(self.store, ledger_db=link, now=NOW)
        hard = self.dir / 'hard.sqlite'
        os.link(self.ledger, hard)
        with self.assertRaises(ft.fr.FunnelError):
            ft.ingest(self.store, ledger_db=self.ledger, now=NOW)
        with self.assertRaises(ft.fr.FunnelError):
            ft.ingest(self.store, ledger_db=self.dir / 'absent.sqlite', now=NOW)
        self.assertEqual(sql(self.store, 'SELECT COUNT(*) FROM feature_rows')[0][0], 0)

    def test_missing_tables_fail_closed(self):
        empty = self.dir / 'empty.sqlite'
        sql(empty, 'CREATE TABLE unrelated(x)')
        with self.assertRaises(ft.FeatureError) as ctx:
            ft.ingest(self.store, ledger_db=empty, now=NOW)
        self.assertEqual(ctx.exception.code, 'LEDGER_TABLES_MISSING')

    def test_corrupt_event_rows_are_skipped_and_counted(self):
        sql(self.ledger, "INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('bad',?,'{not json','h')", (T + 1,))
        sql(self.ledger, "INSERT INTO events(event_id,ts,payload,payload_hash) VALUES('ctl',?,?,'h')",
            (T + 2, canonical(helpers.control(T + 2, 'PAUSE'))))
        report = self.ingest()
        self.assertEqual(report['skipped'], {'EVENT_UNREADABLE': 1, 'NOT_A_MARKET_EVENT': 1})
        self.assertEqual(report['inserted'], 2)


class ExportTests(Fixture):
    def test_jsonl_export_matches_the_store_and_refuses_overwrite(self):
        self.ingest()
        out = self.dir / 'rows.jsonl'
        self.assertEqual(ft.export_jsonl(self.store, out), 2)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        lines = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual(lines, list(ft.read_rows(self.store)))
        self.assertEqual([l['mint'] for l in lines], ['A', 'B'])
        with self.assertRaises(FileExistsError):
            ft.export_jsonl(self.store, out)
        link = self.dir / 'l.jsonl'
        link.symlink_to(self.dir / 'victim.jsonl')
        with self.assertRaises(OSError):
            ft.export_jsonl(self.store, link)
        self.assertFalse((self.dir / 'victim.jsonl').exists())
        part = self.dir / 'part.jsonl'
        self.assertEqual(ft.export_jsonl(self.store, part, since=T + 30), 1)

    def test_shadow_features_load_in_t27_and_gate_entries_by_as_of(self):
        self.ingest()
        out = self.dir / 'features.json'
        features = ft.shadow_features(self.store)
        ft.write_json_exclusive(out, features)
        self.assertEqual(set(features), {'A', 'B'})
        self.assertEqual(features['A']['as_of'], T)
        self.assertTrue(set(features['A']) - {'as_of'} <= set(sh.NEUTRAL_FEATURES))
        self.assertEqual(features['A']['top10_pct'], '10')
        self.assertNotIn('entered', features['A'])
        self.assertNotIn('reject_reasons', features['B'])
        cand = {'mint': 'A', 'pool': 'P', 'migrated_at': T - 600, 'samples': [{'horizon': 3600, 'base_raw': 1, 'quote_raw': 1}]}
        loaded = sh.load_features(out, [cand])
        self.assertEqual(sh._features_for(loaded, 'A', T - 1), (None, False))        # unknown before as_of, entries refused
        fields, known = sh._features_for(loaded, 'A', T)
        self.assertTrue(known)
        self.assertEqual(fields['net_buy_ratio'], '.8')
        self.assertEqual(sh._features_for(loaded, 'ZZ', T), (None, True))             # unseen mints stay neutral

    def test_shadow_features_use_only_the_earliest_row_per_mint(self):
        self.raw_event(ts=T + 500, mint='A', top10_pct='55')           # a later event for A with different holder share
        self.ingest()
        features = ft.shadow_features(self.store)
        self.assertEqual((features['A']['as_of'], features['A']['top10_pct']), (T, '10'))
        self.assertEqual(ft.shadow_features(self.store, until=T + 1)['A']['as_of'], T)      # until is exclusive: rows known before it
        self.assertNotIn('B', ft.shadow_features(self.store, until=T + 1))
        self.assertEqual(ft.shadow_features(self.store, until=T), {})

    def test_shadow_export_never_carries_decision_or_foreign_source_fields(self):
        rec = {('X', float(T)): {'features': {'danger': True, 'top10_pct': '1', 'flow': '5', 'wash_score': '9', 'entered': True}, 'provenance': {
            'wash_score': {'source': 'ledger.events', 'ref': 'r', 'at': float(T), 'role': 'decision'},
            'entered': {'source': 'ledger.events', 'ref': 'r', 'at': float(T), 'role': 'input'},
            'danger': {'source': 'decisions.decisions', 'ref': 'r', 'at': float(T), 'role': 'decision'},
            'top10_pct': {'source': 'counterfactual.samples', 'ref': 'r', 'at': float(T), 'role': 'input'},
            'flow': {'source': 'ledger.events', 'ref': 'r', 'at': float(T), 'role': 'input'}}}}
        ft.append_rows(self.store, rec, now=NOW)
        self.assertEqual(ft.shadow_features(self.store)['X'], {'as_of': float(T), 'flow': '5'})

    def test_shadow_features_skip_rows_from_other_sources(self):
        self.ingest(hints=[hint('A', T - 50), hint('D', T + 5)])
        features = ft.shadow_features(self.store)
        self.assertEqual(features['A']['as_of'], T, 'a discovery row has no event fields, so the first usable row is the ledger event')
        self.assertNotIn('D', features)


class CliTests(Fixture):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ft.main(list(argv))
        return code, json.loads(out.getvalue())

    def test_init_ingest_verify_export_roundtrip(self):
        store = str(self.dir / 'cli.sqlite')
        self.assertEqual(self.run_cli('init', '--store', store)[0], 0)
        self.assertEqual(self.run_cli('init', '--store', store)[1]['code'], 'STORE_EXISTS')
        code, rep = self.run_cli('ingest', '--store', store, '--ledger', str(self.ledger), '--now', str(NOW))
        self.assertEqual((code, rep['inserted'], rep['paper_only']), (0, 2, True))
        self.assertEqual(self.run_cli('verify', '--store', store)[1]['ok'], True)
        self.assertEqual(self.run_cli('export-jsonl', '--store', store, '--out', str(self.dir / 'o.jsonl'))[1]['rows'], 2)
        self.assertEqual(self.run_cli('shadow-features', '--store', store, '--out', str(self.dir / 'f.json'))[1]['mints'], 2)
        self.assertEqual(self.run_cli('shadow-features', '--store', store, '--out', str(self.dir / 'f.json'))[0], 2)

    def test_unavailable_inputs_exit_two_with_a_typed_code(self):
        code, rep = self.run_cli('ingest', '--store', str(self.store), '--ledger', str(self.dir / 'absent.sqlite'), '--now', str(NOW))
        self.assertEqual((code, rep['code']), (2, 'STORE_MISSING'))

    def test_verify_exit_code_reports_a_bad_store(self):
        copy_ = self.dir / 'bad.sqlite'
        self.ingest()
        shutil.copyfile(self.store, copy_)
        with closing(sqlite3.connect(copy_)) as c, c:
            for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                c.execute(f'DROP TRIGGER "{name}"')
            c.execute("UPDATE feature_rows SET row_hash='x'")
        code, rep = self.run_cli('verify', '--store', str(copy_))
        self.assertEqual((code, rep['ok']), (2, False))


class RealStoresTests(unittest.TestCase):
    """The stores of a real dispatched paper entry (production decoder, dispatcher, cycle, ledger)."""

    def test_real_candidate_end_to_end(self):
        from tests import test_paper_entry_dispatcher as fixtures
        d = fixtures.DispatcherTests('test_dry_run_no_admission_credentials_or_io_and_context_activation')
        d.setUp()
        self.addCleanup(d.doCleanups)
        self.assertEqual(d.live()['status'], 'DISPATCHED')
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Path(os.path.realpath(tmp.name)) / 'features.sqlite'
        ft.init(store)
        now = d.f.at + 100
        before = {p: sha(p) for p in (d.discovery, d.journal, d.ledger)}
        report = ft.ingest(store, ledger_db=d.ledger, discovery_db=d.discovery, journal_db=d.journal, research_db=d.f.jobs.path, now=now)
        self.assertEqual(before, {p: sha(p) for p in before}, 'real input stores untouched')
        self.assertEqual(report['invalid'], 0, report)
        self.assertTrue(ft.verify(store)['ok'])
        rows = list(ft.read_rows(store))
        mints = {r['mint'] for r in rows}
        self.assertEqual(len(mints), 1, mints)
        names = {n for r in rows for n in r['features']}
        self.assertTrue({'migrated_at', 'dispatched', 'dispatch_stage_reached'} <= names, names)
        self.assertTrue({'entered'} <= names, 'the real paper BUY is recorded as the decision outcome')
        for r in rows:
            for prov in r['provenance'].values():
                self.assertLessEqual(prov['at'], r['as_of'])
        again = ft.ingest(store, ledger_db=d.ledger, discovery_db=d.discovery, journal_db=d.journal, now=now + 1)
        self.assertEqual((again['inserted'], again['conflict']), (0, 0))


class UnitTemplateTests(unittest.TestCase):
    def test_features_unit_is_a_limited_offline_research_unit(self):
        service = (FRESH / 'desk-features.service').read_text()
        timer = (FRESH / 'desk-features.timer').read_text()
        lines = service.splitlines()

        def one(key):
            values = [l.split('=', 1)[1] for l in lines if l.startswith(key + '=')]
            self.assertEqual(len(values), 1, key)
            return values[0]
        for key, value in (('CPUQuota', '100%'), ('CPUWeight', '20'), ('IOWeight', '20'), ('Nice', '10'),
                           ('IOSchedulingClass', 'idle'), ('Type', 'oneshot'), ('User', 'solana-desk'), ('ProtectSystem', 'strict'),
                           ('NoNewPrivileges', 'true'), ('ProtectHome', 'true'), ('PrivateTmp', 'true'), ('UMask', '0077'),
                           ('PrivateNetwork', 'true'), ('RestrictAddressFamilies', 'AF_UNIX')):
            self.assertEqual(one(key), value, key)
        for key in ('MemoryMax', 'TasksMax'):
            self.assertRegex(one(key), r'^[1-9]\d*[KMG]?$')
        self.assertNotIn('LoadCredential', service, 'no provider keys: it reads retained evidence only')
        self.assertNotRegex(service, r'(?m)^\[Install\]')
        self.assertIn('-m tools.research.features ingest', one('ExecStart'))
        self.assertIn('ReadOnlyPaths=<FRESH_ROOT>', service)
        write = one('ReadWritePaths').split()
        self.assertEqual(write, ['<FRESH_ROOT>/features'], 'only its own store directory is writable')
        self.assertNotIn('--discovery-db /var/lib/solana-desk/', service.replace('<DISCOVERY_DB>', ''))
        self.assertIn('OnUnitInactiveSec=', timer)
        self.assertIn('Unit=desk-features.service', timer)
        self.assertIn('WantedBy=timers.target', timer)


if __name__ == '__main__':
    unittest.main()
