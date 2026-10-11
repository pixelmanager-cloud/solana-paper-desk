"""SYNTHETIC_TEST_ONLY: classification, schedule and append-only store of the not-yet watchlist."""
import copy
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from desk import watchlist as wl
from tests.helpers import config

HINT = {'seq': 7, 'payload_hash': 'a' * 64, 'raw_hash': 'b' * 64, 'received_at': 1000.0, 'mint': 'MINT',
        'pool': 'POOL', 'signature': 'SIG', 'slot': 5}
CFG = {**config(), wl.KEY: 1}
NOT_YET = ['MARKET_CAP']


class ClassificationTests(unittest.TestCase):
    def test_every_listed_not_yet_code_is_not_yet_and_everything_else_is_permanent(self):
        for code in wl.NOT_YET:
            self.assertEqual(wl.classify([code]), 'NOT_YET', code)
        for code in wl.PERMANENT_EXAMPLES:
            self.assertEqual(wl.classify([code]), 'PERMANENT', code)

    def test_unclassified_or_mixed_or_empty_or_malformed_is_permanent(self):
        self.assertEqual(wl.classify(['SOME_NEW_ENGINE_CODE']), 'PERMANENT')
        self.assertEqual(wl.classify(['MARKET_CAP', 'DANGER']), 'PERMANENT')   # one hazard poisons the set
        self.assertEqual(wl.classify(['MARKET_CAP', 'SOME_NEW_ENGINE_CODE']), 'PERMANENT')
        for bad in ([], (), None, 'MARKET_CAP', [None], [''], [1], {'MARKET_CAP'}):
            self.assertEqual(wl.classify(bad), 'PERMANENT', repr(bad))
        self.assertEqual(wl.classify(('LIQUIDITY', 'MARKET_CAP')), 'NOT_YET')

    def test_permanent_examples_never_overlap_the_not_yet_table(self):
        self.assertFalse(set(wl.PERMANENT_EXAMPLES) & set(wl.NOT_YET))

    def test_reason_codes_extraction_for_every_known_result_shape(self):
        self.assertEqual(wl.reason_codes({'outcomes': [{'type': 'reject', 'reasons': ['MARKET_CAP', 'LIQUIDITY']}]}),
                         (['LIQUIDITY', 'MARKET_CAP'], False))
        self.assertEqual(wl.reason_codes({'outcomes': [{'type': 'reject', 'reason': 'ENTRY_SCORE'}]}),
                         (['ENTRY_SCORE'], False))
        self.assertEqual(wl.reason_codes({'outcomes': [{'type': 'fill', 'side': 'buy'}]}), ([], True))
        self.assertEqual(wl.reason_codes({'blockers': ['MARKET_PRODUCER_BLOCKED'],
                                          'diagnostics': [{'blockers': ['X']}, {'graduation': {}}]}),
                         (['MARKET_PRODUCER_BLOCKED', 'X'], False))
        self.assertEqual(wl.reason_codes({'kind': 'history_preparation_no_entry_v1',
                                          'reason': 'HISTORY_FEATURE_EMPTY_WINDOW'}),
                         (['HISTORY_FEATURE_EMPTY_WINDOW'], False))
        self.assertEqual(wl.reason_codes({'kind': 'dispatcher_token_rejection_v1',
                                          'token_policy': {'reasons': ['ACTIVE_MINT_AUTHORITY']}}),
                         (['ACTIVE_MINT_AUTHORITY'], False))
        for junk in (None, [], 'x', {}, {'outcomes': 'x'}, {'outcomes': [None, 3]}, {'blockers': [1]}):
            self.assertEqual(wl.reason_codes(junk), ([], False))
        # An unrecognised rejected result yields no codes, which classifies PERMANENT (fail closed).
        self.assertEqual(wl.classify(wl.reason_codes({'kind': 'something_new'})[0]), 'PERMANENT')


class SettingsTests(unittest.TestCase):
    def test_absent_flag_is_off_and_valid_flag_uses_documented_defaults(self):
        self.assertEqual(wl.selected(config()), 0)
        self.assertEqual(wl.selected(CFG), 1)
        s = wl.settings(CFG)
        self.assertEqual((s['backoff'], s['max_reevaluations'], s['max_age_seconds']), ([600, 1800, 7200], 4, 21600))

    def test_invalid_values_fail_closed(self):
        for flag in (0, 2, True, '1', 1.0, None):
            with self.assertRaises(ValueError, msg=repr(flag)):
                wl.selected({**config(), wl.KEY: flag})
        with self.assertRaises(ValueError):
            wl.selected({**CFG, 'mode': 'live'})
        for key, value in (('paper_watchlist_backoff_first_seconds', 59), ('paper_watchlist_backoff_first_seconds', '600'),
                           ('paper_watchlist_backoff_second_seconds', 100),  # shrinks below the first
                           ('paper_watchlist_backoff_third_seconds', 10**6), (wl.MAX_KEY, 0), (wl.MAX_KEY, 17),
                           (wl.MAX_KEY, True)):
            with self.assertRaises(ValueError, msg=(key, value)):
                wl.selected({**CFG, key: value})

    def test_backoff_schedule_is_ten_minutes_thirty_minutes_then_two_hours(self):
        self.assertEqual([wl.delay_after(n, CFG) for n in (1, 2, 3, 4, 5)], [600, 1800, 7200, 7200, 7200])
        custom = {**CFG, 'paper_watchlist_backoff_first_seconds': 120, 'paper_watchlist_backoff_second_seconds': 240,
                  'paper_watchlist_backoff_third_seconds': 480}
        self.assertEqual([wl.delay_after(n, custom) for n in (1, 2, 3, 9)], [120, 240, 480, 480])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.path = self.dir / 'watchlist.sqlite'
        wl.initialize(self.path)
        self.w = wl.Watchlist(self.path)
        self.addCleanup(lambda: self.w.close())

    def record(self, evaluation, codes=NOT_YET, at=2000.0, mint='MINT', scan=None, hint=HINT, cfg=CFG, **kw):
        return self.w.record(mint=mint, evaluation=evaluation, scan_id=scan or f'scan-{mint}-{evaluation}',
                             codes=codes, at=at, hint={**hint, 'mint': mint}, cfg=cfg, **kw)

    def test_new_store_is_private_and_never_adopts_or_resets_an_existing_file(self):
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaises(ValueError):
            wl.initialize(self.path)
        with self.assertRaises(ValueError):
            wl.initialize(self.dir / 'relative' / '..' / 'x.sqlite')

    def test_not_yet_is_scheduled_with_backoff_and_due_only_after_it_elapses(self):
        self.assertEqual(self.record(1, at=2000.0), 'ENROLLED')
        row = self.w.last('MINT')
        self.assertEqual((row['evaluation'], row['next_eval_at'], row['classification']), (1, 2600.0, 'NOT_YET'))
        self.assertEqual(self.w.due(2599.0, CFG), [])
        (item,) = self.w.due(2600.0, CFG)
        self.assertEqual((item['mint'], item['evaluation'], item['hint']['signature']), ('MINT', 2, 'SIG'))
        self.assertEqual(self.record(2, at=2600.0), 'ENROLLED')
        self.assertEqual(self.w.last('MINT')['next_eval_at'], 2600.0 + 1800)
        self.assertEqual(self.record(3, at=5000.0), 'ENROLLED')
        self.assertEqual(self.w.last('MINT')['next_eval_at'], 5000.0 + 7200)

    def test_default_maximum_of_four_reevaluations_then_exhausted(self):
        kinds = [self.record(n, at=2000.0 + 100 * n) for n in range(1, 6)]
        self.assertEqual(kinds, ['ENROLLED'] * 4 + ['EXHAUSTED'])
        self.assertEqual(self.w.due(10**9, CFG), [])
        with self.assertRaises(sqlite3.DatabaseError):  # a sixth evaluation can never be appended
            self.record(6, at=9000.0)
        short = {**CFG, wl.MAX_KEY: 1}
        self.assertEqual(self.record(1, mint='B', cfg=short), 'ENROLLED')
        self.assertEqual(self.record(2, mint='B', at=3000.0, cfg=short), 'EXHAUSTED')

    def test_permanent_unclassified_accepted_never_come_due_and_cannot_be_extended(self):
        self.assertEqual(self.record(1, codes=['DANGER'], mint='P'), 'PERMANENT')
        self.assertEqual(self.record(1, codes=['BRAND_NEW_CODE'], mint='U'), 'PERMANENT')
        self.assertEqual(self.record(1, codes=[], mint='E'), 'PERMANENT')
        self.assertEqual(self.record(1, codes=[], mint='A', accepted=True), 'ACCEPTED')
        self.assertEqual(self.w.due(10**9, CFG), [])
        for mint in 'PUEA':
            with self.assertRaises(sqlite3.DatabaseError, msg=mint):
                self.record(2, mint=mint, at=9000.0)
            with self.assertRaises(ValueError):
                self.w.evaluation_for(mint)

    def test_age_window_expiry_at_record_time_at_due_time_and_via_expire(self):
        # received_at 1000 + 21600 = 22600 is the last moment the engine age window is open. T20F: a re-evaluation
        # must also keep PREPARATION_MARGIN_SECONDS (900) of it, so the last schedulable/selectable moment is 21700.
        self.assertEqual(self.record(1, at=22100.0), 'EXPIRED')   # 22100 + 600 lands past the window
        self.assertEqual(self.record(1, mint='B', at=2000.0), 'ENROLLED')
        self.assertEqual(self.w.due(21700.0, CFG)[0]['mint'], 'B')
        self.assertEqual(self.w.due(21700.5, CFG), [])             # inside the margin: not selectable any more
        self.assertEqual(self.w.expire(22600.0, CFG), [])          # the window itself is still open
        self.assertEqual(self.w.expire(22601.0, CFG), ['B'])
        self.assertEqual(self.w.expire(22700.0, CFG), [])         # idempotent
        self.assertEqual(self.w.last('B')['kind'], 'EXPIRED')
        with self.assertRaises(sqlite3.DatabaseError):
            self.record(2, mint='B', at=23000.0)

    def test_scheduling_keeps_the_preparation_margin(self):
        # first backoff 600 s: the next evaluation starts at at+600 and must be <= 22600 - 900 = 21700
        self.assertEqual(self.record(1, mint='EDGE', at=21100.0), 'ENROLLED')       # next = 21700 exactly
        self.assertEqual(self.w.last('EDGE')['next_eval_at'], 21700.0)
        self.assertEqual(self.record(1, mint='LATE', at=21100.5), 'EXPIRED')        # next = 21700.5: no room to prepare

    def test_chain_rejects_skips_gaps_other_hints_and_time_travel(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.record(2)                                         # no evaluation 1
        self.record(1, at=2000.0)
        with self.assertRaises(sqlite3.DatabaseError):
            self.record(3, at=2700.0)                              # gap
        with self.assertRaises(sqlite3.DatabaseError):
            self.record(2, at=1999.0)                              # before the previous evaluation
        with self.assertRaises(sqlite3.DatabaseError):
            self.record(2, at=2700.0, hint={**HINT, 'signature': 'OTHER'})  # a different hint is another candidate
        with self.assertRaises(ValueError):
            self.record(2, at=2700.0, hint={**HINT, 'extra': 1})
        with self.assertRaises(ValueError):
            self.w.record(mint='MINT', evaluation=2, scan_id='s', codes=NOT_YET, at=2700.0,
                          hint={**HINT, 'mint': 'ANOTHER'}, cfg=CFG)

    def test_rows_are_immutable(self):
        self.record(1)
        for sql in ('UPDATE watchlist_events SET kind="ACCEPTED"', 'DELETE FROM watchlist_events',
                    'UPDATE watchlist_events SET next_eval_at=0'):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.w.db.execute(sql)
        self.assertEqual(len(self.w.events()), 1)

    def test_identical_record_is_idempotent_and_conflicting_record_is_refused(self):
        self.assertEqual(self.record(1, scan='s1'), 'ENROLLED')
        self.assertEqual(self.record(1, scan='s1'), 'ENROLLED')
        self.assertEqual(len(self.w.events()), 1)
        with self.assertRaises(ValueError):
            self.record(1, scan='s1', codes=['LIQUIDITY'])
        with self.assertRaises(sqlite3.DatabaseError):           # one scan can never describe two evaluations
            self.record(2, scan='s1', at=2700.0)

    def test_cold_restart_preserves_the_schedule_and_the_order_of_due_entries(self):
        self.record(1, mint='LATE', at=3000.0)
        self.record(1, mint='EARLY', at=2000.0)
        before = (self.w.events(), self.w.due(5000, CFG))
        self.w.close()
        self.w = wl.Watchlist(self.path)
        self.assertEqual((self.w.events(), self.w.due(5000, CFG)), before)
        self.assertEqual([d['mint'] for d in self.w.due(5000, CFG)], ['EARLY', 'LATE'])
        self.assertEqual(self.w.due(5000, CFG, limit=1)[0]['mint'], 'EARLY')

    def test_tampered_schema_symlink_hardlink_and_permissions_are_refused(self):
        self.w.close()
        with sqlite3.connect(self.path) as c:
            c.execute('DROP TRIGGER watchlist_events_update')
        with self.assertRaisesRegex(ValueError, 'schema or guards'):
            wl.Watchlist(self.path)
        other = self.dir / 'other.sqlite'
        wl.initialize(other)
        link = self.dir / 'link.sqlite'
        link.symlink_to(other)
        with self.assertRaises(ValueError):
            wl.Watchlist(link)
        hard = self.dir / 'hard.sqlite'
        os.link(other, hard)
        with self.assertRaises(ValueError):
            wl.Watchlist(other)
        hard.unlink()
        other.chmod(0o644)
        with self.assertRaises(ValueError):
            wl.Watchlist(other)
        self.w = wl.Watchlist.__new__(wl.Watchlist)
        self.w.db = sqlite3.connect(':memory:')

    def test_read_only_handle_cannot_write(self):
        self.record(1)
        reader = wl.Watchlist(self.path, read_only=True)
        self.addCleanup(reader.close)
        self.assertEqual(len(reader.due(5000, CFG)), 1)
        with self.assertRaises(sqlite3.OperationalError):
            reader.db.execute('DELETE FROM watchlist_events')
        with self.assertRaises(sqlite3.OperationalError):
            reader.record(mint='X', evaluation=1, scan_id='z', codes=NOT_YET, at=1.0, hint={**HINT, 'mint': 'X'}, cfg=CFG)


class DocumentationTests(unittest.TestCase):
    def test_mapping_table_in_docs_lists_every_classified_code(self):
        text = (Path(__file__).resolve().parents[1] / 'docs' / 'WATCHLIST.md').read_text()
        for code in (*wl.NOT_YET, *wl.CHECKPOINT_NOT_YET, *wl.PERMANENT_EXAMPLES):     # T16J: the checkpoint-scoped codes too
            self.assertIn(f'`{code}`', text, code)


class T20FClassificationTests(unittest.TestCase):
    """T20F items 1, 4 and 6 (unit level)."""

    def test_window_producer_codes_are_not_yet_and_every_other_producer_code_is_permanent(self):
        for code in ('MISSING_WINDOW_MEASUREMENT:net_buy_ratio', 'STALE_WINDOW_MEASUREMENT:unique_buyers_5m',
                     'MISSING_WINDOW_MEASUREMENT:buyer_volume_concentration_proxy_v1'):
            self.assertEqual(wl.classify([code]), 'NOT_YET', code)
        self.assertEqual(wl.classify(['MARKET_PRODUCER_BLOCKED', 'MISSING_WINDOW_MEASUREMENT:net_buy_ratio']), 'NOT_YET')
        for code in ('COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID', 'CAPTURED_HISTORY_WINDOW_STALE_OR_FUTURE',
                     'BOUNDED_RAW_TRADE_SEQUENCE_REQUIRED', 'SOL_USD_SOURCE_OR_EXACT_BLOCK_TIME_MISSING',
                     'MISSING_WINDOW_MEASUREMENT:', 'MISSING_WINDOW_MEASUREMENT:UPPER', 'MISSING_WINDOW_MEASUREMENT:a b',
                     'MISSING_WINDOW_MEASUREMENT:' + 'a' * 65, 'missing_window_measurement:x',
                     'USD_ORIGINAL_BINDING_INVALID', 'STALE_WINDOW_MEASUREMENTS:x'):
            self.assertEqual(wl.classify([code]), 'PERMANENT', code)
        self.assertEqual(wl.classify(['MISSING_WINDOW_MEASUREMENT:net_buy_ratio',
                                      'COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID']), 'PERMANENT')

    def test_the_real_t01_empty_window_result_is_not_yet_and_an_integrity_blocker_next_to_it_is_not(self):
        from tests.test_cycle_no_entry import fixture
        t = fixture(); self.addCleanup(t.doCleanups)
        result, scan = t.result, t.h.f.target.scan_id
        self.assertEqual(result['blockers'], ['MARKET_PRODUCER_BLOCKED'])
        codes, accepted = wl.reason_codes(result, scan_id=scan)
        self.assertFalse(accepted)
        self.assertIn('MARKET_PRODUCER_BLOCKED', codes)
        self.assertTrue(any(c.startswith('MISSING_WINDOW_MEASUREMENT:') for c in codes), codes)
        self.assertEqual(wl.classify(codes), 'NOT_YET')
        tainted = copy.deepcopy(result)
        next(d for d in tainted['diagnostics'] if 'blockers' in d)['blockers'].append('COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID')
        self.assertEqual(wl.classify(wl.reason_codes(tainted, scan_id=scan)[0]), 'PERMANENT')

    def test_only_the_candidates_own_outcomes_and_diagnostics_count(self):
        result = {'outcomes': [{'type': 'reject', 'reasons': ['MARKET_CAP'], 'mint': 'MINT'},
                               {'type': 'reject', 'reason': 'DANGER', 'mint': 'HELD'},                # a held position's reject
                               {'type': 'fill', 'side': 'buy', 'mint': 'HELD'}],                    # and its fill
                  'diagnostics': [{'scan_id': 'S1', 'blockers': ['LIQUIDITY']},
                                  {'scan_id': 'OTHER', 'blockers': ['DANGER']}]}
        self.assertEqual(wl.reason_codes(result, mint='MINT', scan_id='S1'), (['LIQUIDITY', 'MARKET_CAP'], False))
        self.assertEqual(wl.classify(wl.reason_codes(result, mint='MINT', scan_id='S1')[0]), 'NOT_YET')
        # without a candidate the old behaviour is unchanged (everything folded in => PERMANENT, accepted)
        self.assertEqual(wl.reason_codes(result), (['DANGER', 'LIQUIDITY', 'MARKET_CAP'], True))
        # an outcome or diagnostic that names no mint/scan cannot be attributed: it stays in and fails closed
        anon = {'outcomes': [{'type': 'reject', 'reason': 'OUT_OF_ORDER'}], 'diagnostics': [{'blockers': ['ODD']}]}
        self.assertEqual(wl.classify(wl.reason_codes(anon, mint='MINT', scan_id='S1')[0]), 'PERMANENT')
        # the held position's BUY must not make the candidate "accepted"
        self.assertFalse(wl.reason_codes({'outcomes': [{'type': 'fill', 'side': 'buy', 'mint': 'HELD'}]}, mint='MINT')[1])

    def test_capacity_pressure_starts_at_exactly_eighty_percent_of_each_bounded_table(self):
        from desk import history_preparation_rejection as prep, paper_cycle_no_entry as no_entry
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'evidence.sqlite'
            with sqlite3.connect(path) as c:
                c.execute(f'CREATE TABLE {prep.TABLE}(x)'); c.execute(f'CREATE TABLE {no_entry.TABLE}(x)')
            def fill(table, rows):
                with sqlite3.connect(path) as c:
                    c.execute(f'DELETE FROM {table}')
                    c.executemany(f'INSERT INTO {table} VALUES(?)', [(i,) for i in range(rows)])
            self.assertEqual(wl.capacity_pressure(path), [])
            fill(prep.TABLE, 409); self.assertEqual(wl.capacity_pressure(path), [])          # 0.8 * 512 = 409.6
            fill(prep.TABLE, 410)
            self.assertEqual(wl.capacity_pressure(path), [{'table': prep.TABLE, 'rows': 410, 'cap': 512}])
            fill(no_entry.TABLE, 6553); self.assertEqual(len(wl.capacity_pressure(path)), 1)  # 0.8 * 8192 = 6553.6
            fill(no_entry.TABLE, 6554)
            self.assertEqual([p['table'] for p in wl.capacity_pressure(path)], [prep.TABLE, no_entry.TABLE])
            with sqlite3.connect(path) as c:                                                  # absent tables: no pressure
                c.execute(f'DROP TABLE {prep.TABLE}'); c.execute(f'DROP TABLE {no_entry.TABLE}')
            self.assertEqual(wl.capacity_pressure(path), [])


if __name__ == '__main__':
    unittest.main()
