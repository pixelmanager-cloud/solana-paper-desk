"""SYNTHETIC_TEST_ONLY: lean.sources - the Source interface, PumpGraduationSource, per-source cursors, strict config,
failure isolation and the per-source funnel. Fixtures only; no network."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lean import candidates as C, sources as S
from lean.store import Store
from tests.lean.fakeworld import FakeTime, T0, Token, World


def cand(seq, mint=None):
    return C.Candidate(seq=seq, mint=mint or ('M%d' % seq) * 8, pool='P', signature='S', slot=seq, migrated_at=T0, payload_hash='h')


class Fake(S.Source):
    def __init__(self, name, batches=None, error=None):
        self.name, self.batches, self.error, self.calls = name, list(batches or []), error, []

    def iter_new(self, cursor):
        self.calls.append(cursor)
        if self.error:
            raise self.error
        return self.batches.pop(0) if self.batches else ([], cursor)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))
        self.errors, self.handled, self.seen = [], [], set()

    def poll(self, sources, stopped=lambda: False):
        return S.SourceSet(sources, self.dir).poll(
            lambda name, c: (self.handled.append((name, c.mint)), self.seen.add(c.mint)), exists=lambda m: m in self.seen,
            stopped=stopped, on_error=lambda *a: self.errors.append(a))


class PumpSourceTests(unittest.TestCase):
    def test_equals_scan_new_on_the_same_discovery_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = FakeTime(T0)
            tokens = [Token(21 + 2 * i) for i in range(3)]
            world = World(Path(os.path.realpath(tmp)), tokens, clock)
            world.add_frames([T0, T0 + 10, T0 + 20])
            clock.t = T0 + 4000
            direct = C.scan_new(world.discovery_db, 0, now=clock.time(), limit=500)
            source = S.PumpGraduationSource(world.discovery_db, clock=clock.time)
            found, nxt = source.iter_new(0)
            self.assertEqual(found, list(direct.candidates))
            self.assertEqual(nxt, direct.next_cursor)
            self.assertEqual(len(found), 3)
            self.assertEqual(source.name, 'pump_graduation')


class ConfigTests(unittest.TestCase):
    def good(self, **kw):
        return dict({'name': 'pump_b', 'type': 'pump_graduation', 'discovery_db': '/tmp/x.sqlite'}, **kw)

    def test_valid_spec_builds(self):
        built = S.build_sources([self.good()])
        self.assertEqual([s.name for s in built], ['pump_b'])

    def test_refusals(self):
        for bad in (self.good(type='raydium_cpmm'), self.good(type='meteora_dlmm'), self.good(name='pump_graduation'),
                    self.good(name='Bad Name'), self.good(name=''), self.good(discovery_db=''), self.good(extra=1)):
            with self.assertRaises(S.SourceConfigError, msg=str(bad)):
                S.build_sources([bad])
        with self.assertRaises(S.SourceConfigError):
            S.build_sources([self.good(), self.good()])
        with self.assertRaises(S.SourceConfigError):
            S.build_sources('nope')

    def test_unsupported_amm_types_say_so(self):
        with self.assertRaisesRegex(S.SourceConfigError, 'SOURCE_TYPE_UNSUPPORTED'):
            S.build_sources([self.good(type='raydium_amm_v4')])

    def test_source_set_refuses_reserved_and_duplicate_names(self):
        with self.assertRaises(S.SourceConfigError):
            S.SourceSet([Fake('a'), Fake('a')], '.')
        with self.assertRaises(S.SourceConfigError):
            S.SourceSet([Fake('pump_graduation')], '.')


class SourceSetTests(Env):
    def test_each_source_has_its_own_forward_only_cursor(self):
        a, b = Fake('a', [([cand(1), cand(2)], 5)]), Fake('b', [([cand(1, 'X' * 32)], 3)])
        self.assertEqual(self.poll([a, b]), 3)
        self.assertEqual(C.read_cursor(self.dir / 'cursor-a.json'), 5)    # next_cursor: skipped frames count too
        self.assertEqual(C.read_cursor(self.dir / 'cursor-b.json'), 3)
        self.poll([a, b])
        self.assertEqual((a.calls, b.calls), ([0, 5], [0, 3]))

    def test_cursor_does_not_pass_unhandled_candidates_when_stopped(self):
        a = Fake('a', [([cand(1), cand(2), cand(3)], 9)])
        count = {'n': 0}

        def stopped():
            count['n'] += 1
            return count['n'] > 2                     # one source check + one candidate, then stop
        self.assertEqual(self.poll([a], stopped), 1)
        self.assertEqual(C.read_cursor(self.dir / 'cursor-a.json'), 1)    # NOT 9: candidates 2 and 3 are still owed

    def test_known_mint_is_handled_once_across_sources(self):
        same = 'Z' * 32
        a, b = Fake('a', [([cand(1, same)], 1)]), Fake('b', [([cand(1, same)], 1)])
        self.assertEqual(self.poll([a, b]), 1)
        self.assertEqual(self.handled, [('a', same)])
        self.assertEqual(C.read_cursor(self.dir / 'cursor-b.json'), 1)    # b still advances past the duplicate

    def test_a_failing_source_is_isolated_and_reported(self):
        bad, good = Fake('bad', error=RuntimeError('boom')), Fake('good', [([cand(1)], 1)])
        self.assertEqual(self.poll([bad, good]), 1)
        self.assertEqual(self.errors, [('SOURCE_FAILED', False, 'bad', 'RuntimeError')])
        self.assertFalse((self.dir / 'cursor-bad.json').exists())

    def test_unavailable_discovery_is_transient(self):
        self.poll([Fake('x', error=C.DiscoveryUnavailable('locked'))])
        self.assertEqual([(e[0], e[1]) for e in self.errors], [('DISCOVERY_UNAVAILABLE', True)])

    def test_corrupt_cursor_skips_the_source_and_is_never_reset(self):
        path = self.dir / 'cursor-a.json'
        path.write_text('{not json')
        a = Fake('a', [([cand(1)], 1)])
        self.assertEqual(self.poll([a]), 0)
        self.assertEqual(a.calls, [])
        self.assertEqual(path.read_text(), '{not json')
        self.assertEqual(self.errors[0][0], 'SOURCE_CURSOR_INVALID')

    def test_cursor_that_moves_backward_or_is_not_an_int_is_refused(self):
        C.write_cursor(self.dir / 'cursor-a.json', 7)
        for bad in (3, '9', None, 7.5):
            self.errors.clear()
            self.poll([Fake('a', [([cand(1)], bad)])])
            self.assertEqual(self.errors[0][:3], ('SOURCE_FAILED', False, 'a'), bad)
            self.assertEqual(C.read_cursor(self.dir / 'cursor-a.json'), 7)
        self.assertEqual(self.handled, [])

    def test_empty_batch_still_advances_the_cursor(self):
        self.poll([Fake('a', [([], 40)])])
        self.assertEqual(C.read_cursor(self.dir / 'cursor-a.json'), 40)


class FunnelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(os.path.realpath(self.tmp.name)) / 'lean.sqlite', initial_cash_sol=10, code_version='c',
                           strategy_version='s', clock=lambda: 5.0)
        self.addCleanup(self.store.close)

    def test_counts_per_source_and_legacy_rows_are_pump(self):
        s = self.store
        for mint, meta in (('A' * 32, {}), ('B' * 32, {'source': 'pump_graduation'}), ('C' * 32, {'source': 'pump_b'}),
                           ('D' * 32, {'source': 'pump_b'})):
            cid = s.add_candidate(mint, pool='p', signature='s', slot=1, migrated_at=1.0, hint_seq=1, meta=meta)
            s.add_decision('screen', 'PASS' if mint[0] in 'AC' else 'REJECT', mint=mint, candidate_id=cid,
                           reasons=[] if mint[0] in 'AC' else ['LIQUIDITY_BELOW_MIN'])
        f = s.rows('candidates', limit=10)
        self.assertEqual(len(f), 4)
        funnel = S.funnel_by_source(s)
        self.assertEqual(sorted(funnel), ['pump_b', 'pump_graduation'])
        self.assertEqual((funnel['pump_graduation']['candidates'], funnel['pump_graduation']['passed'],
                          funnel['pump_graduation']['screen_rejected']), (2, 1, 1))
        self.assertEqual((funnel['pump_b']['candidates'], funnel['pump_b']['passed'], funnel['pump_b']['screen_rejected']), (2, 1, 1))
        self.assertEqual(funnel['pump_b']['rejection_reasons'], {'LIQUIDITY_BELOW_MIN': 1})

    def test_last_screen_supersedes_the_first(self):
        s = self.store
        m = 'E' * 32
        cid = s.add_candidate(m, pool='p', signature='s', slot=1, migrated_at=1.0, hint_seq=1, meta={'source': 'x'})
        s.add_decision('screen', 'REJECT', mint=m, candidate_id=cid, reasons=['TOO_YOUNG'])
        s.add_decision('screen', 'PASS', mint=m, candidate_id=cid, reasons=[])
        b = S.funnel_by_source(s)['x']
        self.assertEqual((b['screened'], b['passed'], b['screen_rejected'], b['rejection_reasons']), (1, 1, 0, {}))


if __name__ == '__main__':
    unittest.main()
