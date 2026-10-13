"""SYNTHETIC_TEST_ONLY: lean.census (C3) - config and budget, log matching, sampling and extrapolation, cursors, baseline,
truncation, shed / error / stop behaviour, append-only storage, pump.fun counting from the discovery database. No network."""
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from lean import census as K
from lean.providers import LANE_SHED, ProviderError
from tests.lean.census_chain import AGGREGATOR, DBC, LAUNCHLAB, TOKEN, ChainHelius, LaunchChain, logs
from tests.lean.fakeworld import FakeTime, T0, Token, World


def tx(kind, program=LAUNCHLAB, err=None):
    return {'meta': {'err': err, 'logMessages': logs(kind, program), 'postTokenBalances': []}}


def cfg(**kw):
    base = {'enabled': True, 'pump_graduations': False, 'launchpads': [{'name': 'll', 'program': LAUNCHLAB,
                                                                       'create_log_patterns': ['Instruction: Initialize']}],
            'max_tx_per_poll': 10, 'pace_s': 0.05, 'credit_budget_month': 10_000_000}
    base.update(kw)
    return K.config(base)


class ConfigTests(unittest.TestCase):
    def test_defaults_off_and_valid(self):
        c = K.config(None)
        self.assertFalse(c['enabled'])
        self.assertEqual([p['name'] for p in c['launchpads']], ['raydium_launchlab', 'meteora_dbc'])
        self.assertEqual((c['interval_s'], c['credit_budget_month']), (600, 50_000))
        self.assertEqual(K.worst_case_credits_per_month(c), 43_200)               # 2 * (1 + 4) * 4320

    def test_refusals(self):
        pad = {'name': 'a', 'program': LAUNCHLAB, 'create_log_patterns': ['Instruction: Initialize']}
        for bad in ({'typo': 1}, {'enabled': 1}, {'db': '../x.sqlite'}, {'db': 'x.db'}, {'db': ''}, {'interval_s': 59},
                    {'interval_s': True}, {'max_tx_per_poll': 21}, {'max_pages': 0}, {'pace_s': 0}, {'launchpads': 'x'},
                    {'launchpads': [dict(pad, extra=1)]}, {'launchpads': [dict(pad, name='Bad Name')]},
                    {'launchpads': [dict(pad, name='pump_fun_graduations')]}, {'launchpads': [pad, pad]},
                    {'launchpads': [dict(pad, program='short')]}, {'launchpads': [dict(pad, create_log_patterns=[])]},
                    {'launchpads': [dict(pad, create_log_patterns=['ab'])]}, {'launchpads': [dict(pad, create_log_patterns='x' * 9)]}):
            with self.assertRaises(K.ConfigError, msg=str(bad)):
                K.config(bad)

    def test_budget_is_enforced_at_load(self):
        with self.assertRaisesRegex(K.ConfigError, 'credits/month'):
            K.config({'interval_s': 60})                                           # 2 * 5 * 43200 = 432 000 > 50 000
        K.config({'interval_s': 60, 'max_tx_per_poll': 0, 'credit_budget_month': 100_000})   # 2 * 1 * 43200 = 86 400
        with self.assertRaises(K.ConfigError):
            K.config({'interval_s': 60, 'max_tx_per_poll': 0, 'credit_budget_month': 80_000})


class MatchTests(unittest.TestCase):
    P = ['Instruction: Initialize']

    def test_create_swap_and_failures(self):
        self.assertEqual(K.match_creates(tx('create'), LAUNCHLAB, self.P), 1)
        self.assertEqual(K.match_creates(tx('swap'), LAUNCHLAB, self.P), 0)
        self.assertEqual(K.match_creates(tx('create', err={'InstructionError': [0, 'Custom']}), LAUNCHLAB, self.P), 0)

    def test_other_programs_log_lines_never_count(self):
        self.assertEqual(K.match_creates(tx('token_only'), LAUNCHLAB, self.P), 0)       # the token program said "Initialize"
        self.assertEqual(K.match_creates(tx('outer_log'), LAUNCHLAB, self.P), 0)        # the aggregator said it
        self.assertEqual(K.match_creates(tx('create'), DBC, self.P), 0)                 # another program's tx

    def test_cpi_create_counts_for_the_inner_program(self):
        self.assertEqual(K.match_creates(tx('cpi_create'), LAUNCHLAB, self.P), 1)
        self.assertEqual(K.match_creates(tx('cpi_create'), AGGREGATOR, self.P), 0)

    def test_two_creates_in_one_transaction_and_multiple_patterns(self):
        t = {'meta': {'err': None, 'logMessages': logs('create', LAUNCHLAB) + logs('create', LAUNCHLAB)}}
        self.assertEqual(K.match_creates(t, LAUNCHLAB, self.P), 2)
        t2 = {'meta': {'err': None, 'logMessages': [f'Program {DBC} invoke [1]', 'Program log: Instruction: InitializeVirtualPoolWithToken2022',
                                                    f'Program {DBC} success']}}
        self.assertEqual(K.match_creates(t2, DBC, ['Instruction: InitializeVirtualPoolWithSplToken',
                                                   'Instruction: InitializeVirtualPoolWithToken2022']), 1)

    def test_garbage_is_zero_not_an_error(self):
        for bad in (None, {}, {'meta': None}, {'meta': {}}, {'meta': {'err': None, 'logMessages': 'x'}},
                    {'meta': {'err': None, 'logMessages': [1, None, {}]}}, 'x', []):
            self.assertEqual(K.match_creates(bad, LAUNCHLAB, self.P), 0)

    def test_sample_mints_skip_sol_and_cap(self):
        t = {'meta': {'postTokenBalances': [{'mint': K.SOL_MINT}, {'mint': 'A' * 40}, {'mint': 'A' * 40}, {'mint': 'B' * 40},
                                            {'mint': 'C' * 40}, {'mint': 'D' * 40}, 'junk']}}
        self.assertEqual(K.sample_mints(t), ['A' * 40, 'B' * 40, 'C' * 40])
        self.assertEqual(K.sample_mints({}), [])

    def test_spread(self):
        self.assertEqual(K.spread(list(range(10)), 0), [])
        self.assertEqual(K.spread(list(range(10)), 10), list(range(10)))
        self.assertEqual(K.spread(list(range(10)), 20), list(range(10)))
        self.assertEqual(K.spread(list(range(10)), 2), [2, 7])
        self.assertEqual(len(set(K.spread(list(range(1000)), 4))), 4)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.now = [T0]
        self.chain = LaunchChain()
        self.stop = threading.Event()
        self.sleeps = []
        self.db = K.CensusDb(self.dir / 'census.sqlite')
        self.addCleanup(self.db.close)

    def census(self, config=None, discovery_db=None):
        return K.Census(db=self.db, helius=ChainHelius(self.chain, lambda: self.now[0]), cfg=config or cfg(), code_version='cv',
                        discovery_db=discovery_db, clock=lambda: self.now[0], sleep=lambda s: self.sleeps.append(s), stop=self.stop)

    def rows(self, name='ll'):
        return self.db.rows(name)


class PollTests(Env):
    def test_baseline_then_exact_counts(self):
        for i in range(3):
            self.chain.add(LAUNCHLAB, T0 - 100 + i, 'create')                    # history: never counted
        c = self.census()
        c.poll_once()
        r = self.rows()
        self.assertEqual((len(r), r[0]['note'], r[0]['new_count']), (1, 'BASELINE', None))
        self.assertEqual(self.db.cursor('ll'), self.chain.txs[LAUNCHLAB][-1]['sig'])
        self.now[0] += 600
        for kind in ('create', 'swap', 'create', 'swap', 'swap', 'create'):
            self.now[0] += 1
            self.chain.add(LAUNCHLAB, self.now[0], kind)
        self.chain.add(LAUNCHLAB, self.now[0], 'create', err={'InstructionError': [0, 'x']})   # failed: not a create
        self.now[0] += 1
        c.poll_once()
        row = self.rows()[-1]
        self.assertEqual((row['signatures'], row['failed'], row['sampled'], row['creates_in_sample'], row['new_count'], row['exact'],
                          row['truncated'], row['note']), (7, 1, 6, 3, 3, 1, 0, ''))
        self.assertEqual(len(json.loads(row['sample_mints'])), 3)
        self.assertEqual(row['label'], 'CENSUS')
        c.poll_once()                                                            # nothing new: an exact zero, not a repeat
        row = self.rows()[-1]
        self.assertEqual((row['signatures'], row['new_count'], row['exact']), (0, 0, 1))

    def test_sampling_extrapolates_and_says_so(self):
        c = self.census(cfg(max_tx_per_poll=4))
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        for i in range(20):
            self.chain.add(LAUNCHLAB, T0 + 1 + i, 'create' if i % 2 == 0 else 'swap')
        self.now[0] = T0 + 100
        c.poll_once()
        row = self.rows()[-1]
        self.assertEqual((row['signatures'], row['sampled'], row['exact'], row['note']), (20, 4, 0, 'SAMPLED'))
        self.assertEqual(row['new_count'], round(row['creates_in_sample'] * 20 / 4))
        self.assertEqual(row['creates_in_sample'], 2)                            # evenly spread picks 2 of the 10 creates
        self.assertEqual(row['new_count'], 10)

    def test_zero_sample_budget_gives_null_count_but_exact_signature_count(self):
        c = self.census(cfg(max_tx_per_poll=0))
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        for i in range(5):
            self.chain.add(LAUNCHLAB, T0 + 1 + i, 'create')
        self.now[0] = T0 + 50
        c.poll_once()
        row = self.rows()[-1]
        self.assertEqual((row['signatures'], row['sampled'], row['new_count'], row['exact']), (5, 0, None, 0))

    def test_truncation_is_flagged(self):
        c = self.census(cfg(sig_limit=5, max_pages=1, max_tx_per_poll=0))
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        for i in range(12):
            self.chain.add(LAUNCHLAB, T0 + 1 + i, 'swap')
        self.now[0] = T0 + 50
        c.poll_once()
        row = self.rows()[-1]
        self.assertEqual((row['signatures'], row['truncated']), (5, 1))

    def test_pages_are_followed_up_to_max_pages(self):
        c = self.census(cfg(sig_limit=5, max_pages=3, max_tx_per_poll=0))
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        for i in range(12):
            self.chain.add(LAUNCHLAB, T0 + 1 + i, 'swap')
        self.now[0] = T0 + 50
        c.poll_once()
        row = self.rows()[-1]
        self.assertEqual((row['signatures'], row['truncated']), (12, 0))

    def test_transient_error_writes_nothing_and_loses_nothing(self):
        c = self.census()
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        self.chain.add(LAUNCHLAB, T0 + 1, 'create')
        self.chain.fail['getSignaturesForAddress'] = lambda params: ProviderError('HTTP_503', True)
        self.now[0] = T0 + 10
        before = len(self.rows())
        c.poll_once()
        self.assertEqual(len(self.rows()), before)
        self.assertEqual(c.errors_by_code, {'HTTP_503': 1})
        self.chain.fail.clear()
        self.now[0] = T0 + 20
        c.poll_once()
        self.assertEqual(self.rows()[-1]['new_count'], 1)                          # picked up on the next poll

    def test_lane_shed_skips_without_a_row_or_a_cursor_move(self):
        c = self.census()
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        cursor = self.db.cursor('ll')
        self.chain.add(LAUNCHLAB, T0 + 1, 'create')
        self.chain.fail['getSignaturesForAddress'] = lambda params: ProviderError(LANE_SHED, True, meta={'why': 'shed_backoff'})
        self.now[0] = T0 + 10
        n = len(self.rows())
        c.poll_once()
        self.assertEqual((len(self.rows()), self.db.cursor('ll'), c.counts['shed'], c.counts['errors']), (n, cursor, 1, 0))

    def test_shed_during_sampling_keeps_what_was_learned(self):
        c = self.census()
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        for i in range(4):
            self.chain.add(LAUNCHLAB, T0 + 1 + i, 'create')
        seen = {'n': 0}

        def shed_after_two(params):
            seen['n'] += 1
            return ProviderError(LANE_SHED, True, meta={'why': 'shed_backoff'}) if seen['n'] > 2 else None
        self.chain.fail['getTransaction'] = shed_after_two
        self.now[0] = T0 + 20
        c.poll_once()
        row = self.rows()[-1]
        self.assertEqual((row['signatures'], row['sampled'], row['exact'], row['new_count']), (4, 2, 0, 4))

    def test_stop_mid_poll_writes_nothing(self):
        c = self.census()
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        for i in range(3):
            self.chain.add(LAUNCHLAB, T0 + 1 + i, 'create')
        cursor, n = self.db.cursor('ll'), len(self.rows())

        def stop_on_first_tx(params):
            self.stop.set()
        self.chain.fail['getTransaction'] = stop_on_first_tx
        self.now[0] = T0 + 20
        c.poll_once()
        self.assertEqual((len(self.rows()), self.db.cursor('ll')), (n, cursor))

    def test_hourly_cap_stops_calls(self):
        c = self.census(cfg(credit_budget_month=1440, max_tx_per_poll=0, interval_s=86400))     # 1440 // 720 = 2 calls per hour
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        for _ in range(5):
            c.poll_once()
            self.now[0] += 1
        self.assertLessEqual(len(self.chain.calls), 2)
        self.assertGreaterEqual(c.errors_by_code.get('HOURLY_BUDGET', 0), 1)

    def test_restart_continues_from_the_stored_cursor(self):
        c = self.census()
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        c.poll_once()
        self.chain.add(LAUNCHLAB, T0 + 1, 'create')
        self.now[0] = T0 + 10
        c.poll_once()
        c2 = self.census()                                                          # a new process, same census.sqlite
        self.chain.add(LAUNCHLAB, T0 + 11, 'create')
        self.now[0] = T0 + 20
        c2.poll_once()
        counts = [r['new_count'] for r in self.rows()]
        self.assertEqual(counts, [None, 1, 1])                                      # no second baseline, no double count
        self.assertEqual([r['note'] for r in self.rows()].count('BASELINE'), 1)

    def test_launchpads_are_independent(self):
        two = cfg(launchpads=[{'name': 'll', 'program': LAUNCHLAB, 'create_log_patterns': ['Instruction: Initialize']},
                              {'name': 'dbc', 'program': DBC, 'create_log_patterns': ['Instruction: Initialize']}])
        c = self.census(two)
        self.chain.add(LAUNCHLAB, T0 - 1, 'swap')
        self.chain.add(DBC, T0 - 1, 'swap')
        c.poll_once()
        self.chain.add(DBC, T0 + 1, 'create')
        self.chain.fail['getSignaturesForAddress'] = lambda params: ProviderError('HTTP_503', True) if params[0] == LAUNCHLAB else None
        self.now[0] = T0 + 10
        c.poll_once()
        self.assertEqual([r['new_count'] for r in self.rows('dbc')], [None, 1])
        self.assertEqual(len(self.rows('ll')), 1)

    def test_idle_program_never_baselines_until_it_has_a_signature(self):
        c = self.census()
        c.poll_once()
        self.assertEqual(self.rows(), [])
        self.chain.add(LAUNCHLAB, T0 + 1, 'swap')
        self.now[0] = T0 + 5
        c.poll_once()
        self.assertEqual([r['note'] for r in self.rows()], ['BASELINE'])


class StorageTests(Env):
    def test_rows_and_cursors_are_append_only(self):
        self.db.add_row(1.0, 'x', None, 'cv', note='n')
        self.db.add_cursor('x', 'sig', 1.0)
        for sql in ('UPDATE census SET new_count=5', 'DELETE FROM census', 'UPDATE cursors SET cursor=\'z\'', 'DELETE FROM cursors'):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                self.db.db.execute(sql)

    def test_label_is_fixed(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.db.db.execute("INSERT INTO census(ts,launchpad,exact,truncated,sample_mints,note,label,code_version) "
                               "VALUES(1,'x',0,0,'[]','','LIVE','cv')")


class PumpCountingTests(Env):
    def world(self, n=5):
        clock = FakeTime(T0)
        (self.dir / 'w').mkdir()
        world = World(self.dir / 'w', [Token(21 + 2 * i) for i in range(n)], clock)
        world.add_frames([T0 + 300 + 100 * i for i in range(n)])                      # received_at = T0, T0+100, ...
        self.now[0] = T0 + 10_000
        return world

    def pump(self, world):
        c = self.census(cfg(launchpads=[]), discovery_db=world.discovery_db)
        c.cfg['pump_graduations'] = True
        return c

    def test_first_poll_is_a_baseline_at_the_newest_frame(self):
        c = self.pump(self.world())
        c.poll_once()
        row = self.rows(K.PUMP_NAME)[0]
        self.assertEqual((row['note'], row['new_count']), ('BASELINE', None))
        self.assertEqual(self.db.cursor(K.PUMP_NAME), '5')
        c.poll_once()
        self.assertEqual(self.rows(K.PUMP_NAME)[-1]['new_count'], 0)                   # history is never "new"

    def test_graduations_after_the_cursor_are_counted_exactly(self):
        c = self.pump(self.world())
        self.db.add_cursor(K.PUMP_NAME, 2, self.now[0])                                # a baseline taken when 2 frames existed
        c.poll_once()
        row = self.rows(K.PUMP_NAME)[-1]
        self.assertEqual((row['note'], row['exact'], row['truncated']), ('discovery', 1, 0))
        self.assertEqual((row['new_count'], row['signatures']), (3, 3))                # frames 3, 4, 5
        self.assertEqual(len(json.loads(row['sample_mints'])), 3)
        self.assertEqual(self.db.cursor(K.PUMP_NAME), '5')
        c.poll_once()
        self.assertEqual(self.rows(K.PUMP_NAME)[-1]['new_count'], 0)                   # not counted twice

    def test_a_missing_discovery_database_is_an_error_not_a_row(self):
        c = self.census(cfg(launchpads=[]), discovery_db=self.dir / 'nope.sqlite')
        c.cfg['pump_graduations'] = True
        c.poll_once()
        self.assertEqual(self.rows(K.PUMP_NAME), [])
        self.assertTrue(c.errors_by_code)


if __name__ == '__main__':
    unittest.main()
