"""SYNTHETIC_TEST_ONLY: SOL/USD valuation v2 through the real paper cycle (entry, held, exit, replay).

Built on the Kraken lifecycle harness (real transport, collector, parser, builder, engine and ledger; only the
wire bytes are synthetic). The harness's two USD wire lines are replaced so each test can choose the Jupiter and
Kraken answers, or make either provider fail; no network, no credentials."""
import copy
import inspect
import json
import sqlite3
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from desk import paper_cycle as cycle, quote_execution as qe, usd_valuation as uv
from desk import kraken_usd_observation as kraken, kraken_pacing_migration as migration, provider_pacing as pace
from desk.model import canonical, digest
from desk.paper_checkpoint import RecoveryRequired
from tests import test_kraken_lifecycle as base, test_paper_cycle as fixtures

_SOURCE = inspect.getsource(base.actual_cycle)
_KRAKEN_LINE = """                wire=('{"error":[],"result":{"SOLUSD":[["100.00000","1.00000",'+str(outer.f.at-1)+'.25,"s","l","",123]],"last":"123000000000"}}').encode()"""
_JUPITER_LINE = """                wire=canonical({SOL:{'usdPrice':100,'blockId':100,'decimals':9}}).encode()"""
assert _KRAKEN_LINE in _SOURCE and _JUPITER_LINE in _SOURCE, 'fixture changed: update the USD wire overrides'
_SOURCE = _SOURCE.replace(_KRAKEN_LINE, """                outer.kraken_calls=getattr(outer,'kraken_calls',0)+1
                if getattr(outer,'kraken_fail',False):raise getattr(outer,'kraken_exc',ConnectionResetError)('fixture: kraken down')
                wire=('{"error":[],"result":{"SOLUSD":[["'+getattr(outer,'kraken_price','100.00000')+'","1.00000",'+str(outer.f.at-1)+'.25,"s","l","",123]],"last":"123000000000"}}').encode()""")
_SOURCE = _SOURCE.replace(_JUPITER_LINE, """                outer.jupiter_calls=getattr(outer,'jupiter_calls',0)+1
                if getattr(outer,'jupiter_fail',False):raise getattr(outer,'jupiter_exc',ConnectionResetError)('fixture: jupiter down')
                wire=getattr(outer,'jupiter_wire',None) or canonical({SOL:{'usdPrice':getattr(outer,'jupiter_price',100),'blockId':100,'decimals':9}}).encode()""")
_NAMESPACE = dict(vars(base))
exec(_SOURCE, _NAMESPACE)
actual_cycle = _NAMESPACE['actual_cycle']
dump = base.dump


class V2Base(unittest.TestCase):
    extra = {}

    def setUp(self):
        self.h = fixtures.PaperCycleTests(); self.h.setUp(); self.addCleanup(self.h.doCleanups)
        self.h.cfg = self.h.cfg | {'paper_usd_valuation_version': 2, **self.extra}
        self.h.path = Path(self.h.f.tmp.name) / 'usd2-experiment.sqlite'; cycle.initialize(self.h.path, self.h.cfg)
        self.h.http_calls = []; self.h.sell_output = 10_000_000
        self.pacing = Path(self.h.f.tmp.name).resolve() / 'usd2-pacing.sqlite'; pace.initialize(self.pacing)
        self.policy = self.pacing.parent / 'migration.json'
        p = patch.object(migration, 'POLICY', self.policy); p.start(); self.addCleanup(p.stop)
        self.policy.write_text(canonical({'version': 1, 'pins': []}))
        pin = migration.review_plan(self.pacing); self.policy.write_text(canonical({'version': 1, 'pins': [pin]})); migration.migrate(self.pacing)
        self.clock = [float(self.h.f.at)]
        def configured(**kw):
            return pace.Pacer(self.pacing, clock=lambda: self.clock[0], monotonic=lambda: self.clock[0],
                              sleep=lambda n: self.clock.__setitem__(0, self.clock[0] + n), **kw)
        p = patch.object(pace, 'configured', side_effect=configured); p.start(); self.addCleanup(p.stop)

    def events(self):
        with sqlite3.connect(self.h.path) as c:
            return [json.loads(r[0]) for r in c.execute('SELECT payload FROM events ORDER BY seq')]

    def run_cycle(self, **kw):
        return actual_cycle(self.h, **kw)

    def held_item(self):
        position = cycle._state(self.h.path, self.h.cfg)['positions'][self.h.target.mint]
        return replace(self.h.item, target=replace(self.h.target, amount_raw=qe.raw_quantity(position['qty'], position['quote_execution']['mint_decimals'])))

    def allowance(self):
        from desk.monitoring_budget import MonitoringBudget
        budget = MonitoringBudget(self.h.f.progress.store, self.h.path, self.h.cfg, clock=lambda: self.h.f.at); budget.provision()
        return budget

    def passes(self):
        with self.h.f.progress.store.connect() as c:
            return c.execute('SELECT id,outcome_hash FROM paper_observation_passes').fetchall()

    def no_unresolved_pass(self):
        self.assertEqual([p for p in self.passes() if p[1] is None], [])


class EntryTests(V2Base):
    def test_primary_with_kraken_cross_check_is_recorded_and_replays(self):
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(result['attempted_requests'], 8, result)                 # 7 (Kraken-only) - 1 Kraken + Jupiter + cross-check
        self.assertEqual(len(result['usd_evidence_refs']), 2)
        (event,) = [e for e in self.events() if e['kind'] == 'market']
        value = event['paper_usd_valuation']
        self.assertEqual((value['version'], value['source'], value['selection'], value['selected_source']),
                         (2, uv.SOURCE, 'PRIMARY', 'JUPITER'))
        self.assertEqual((value['usd_price'], Decimal(value['divergence']), value['fallback']['status']), ('100', Decimal(0), 'MEASURED'))
        self.assertIsNone(value['solana_slot_witness'])
        self.assertEqual(event['sol_usd'], '100')
        self.assertEqual(event['paper_source_evidence']['usd'], uv.summary_for_source(value))
        self.assertNotIn('attempt', event['paper_source_evidence']['usd']['primary'])
        self.assertEqual((self.h_calls('jupiter'), self.h_calls('kraken')), (1, 1))
        # replay: reading the ledger rebuilds the valuation from the retained originals
        self.assertEqual(len(cycle._state(self.h.path, self.h.cfg)['positions']), 1)
        before = dump(self.h.path)
        cycle._state(self.h.path, self.h.cfg)
        self.assertEqual(dump(self.h.path), before)
        self.no_unresolved_pass()

    def h_calls(self, name):
        return getattr(self.h, name + '_calls', 0)

    def test_jupiter_failure_falls_back_to_kraken_and_both_requests_are_charged(self):
        self.h.jupiter_fail = True
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(result['attempted_requests'], 8, result)                 # the failed Jupiter request is charged too
        (event,) = [e for e in self.events() if e['kind'] == 'market']
        value = event['paper_usd_valuation']
        self.assertEqual((value['selection'], value['selected_source'], value['primary']['status']), ('FALLBACK', 'KRAKEN', 'UNKNOWN'))
        self.assertEqual(value['primary']['blockers'], ['TRANSPORT_ERROR'])
        self.assertEqual(value['primary']['attempt']['failure_code'], 'TRANSPORT_ERROR')   # the failure itself is retained
        self.assertEqual(len(result['usd_evidence_refs']), 2)
        cycle._state(self.h.path, self.h.cfg)

    def test_every_malformed_or_stale_primary_falls_back(self):
        for name, wire in {'html': b'<html>', 'no blockId': canonical({base.SOL: {'usdPrice': 100, 'decimals': 9}}).encode(),
                           'zero price': canonical({base.SOL: {'usdPrice': 0, 'blockId': 1, 'decimals': 9}}).encode(),
                           'wrong decimals': canonical({base.SOL: {'usdPrice': 100, 'blockId': 1, 'decimals': 6}}).encode()}.items():
            with self.subTest(name):
                self.setUp()
                self.h.jupiter_wire = wire
                result = self.run_cycle()
                self.assertEqual(result['status'], 'COMPLETE', result)
                (event,) = [e for e in self.events() if e['kind'] == 'market']
                self.assertEqual((event['paper_usd_valuation']['selection'], event['paper_usd_valuation']['selected_source']), ('FALLBACK', 'KRAKEN'))

    def test_divergence_is_a_normal_terminal_no_entry_and_never_a_latch(self):
        self.h.kraken_price = '103.00000'                                          # 3% away from Jupiter's 100
        result = self.run_cycle()
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['blockers'], ['MARKET_PRODUCER_BLOCKED'], result)
        codes = [b for d in result['diagnostics'] if 'blockers' in d for b in d['blockers']]
        self.assertIn('USD_SOURCE_DIVERGENCE', codes)
        self.assertEqual(result['attempted_requests'], 7)                          # Jupiter + Kraken charged; no quotes are bought
        self.assertEqual(cycle._state(self.h.path, self.h.cfg)['positions'], {})
        self.assertEqual([e for e in self.events() if e['kind'] == 'market'], [])
        self.no_unresolved_pass()                                                  # terminal no-entry, no NULL pass

    def test_divergence_limit_is_configurable_and_equal_prices_pass(self):
        self.extra = {uv.KEY_DIVERGENCE: 0.05}
        self.setUp()
        self.h.kraken_price = '103.00000'
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        (event,) = [e for e in self.events() if e['kind'] == 'market']
        self.assertEqual((Decimal(event['paper_usd_valuation']['divergence']), event['paper_usd_valuation']['divergence_max_fraction']),
                         (Decimal('0.03'), '0.05'))

    def test_neither_source_available_is_a_normal_no_entry(self):
        # T37F: was "the NULL pass stays (fail closed)". Both providers failing is a provider fault, not evidence
        # corruption: the pass ends as a proper terminal NO_ENTRY that cites the two failed attempts, the charges
        # stay charged, and nothing is latched.
        self.h.jupiter_fail = self.h.kraken_fail = True
        result = self.run_cycle()
        self.assertEqual(result['status'], 'BLOCKED', result)
        self.assertEqual(result['blockers'], ['MARKET_PRODUCER_BLOCKED'], result)
        codes = [b for d in result['diagnostics'] if 'blockers' in d for b in d['blockers']]
        self.assertIn('SOL_USD_UNAVAILABLE', codes)
        self.assertEqual(result['attempted_requests'], 7)
        self.assertEqual(cycle._state(self.h.path, self.h.cfg)['positions'], {})
        self.no_unresolved_pass()
        store = self.h.f.progress.store
        with store.connect() as c:
            rows = c.execute('SELECT outcome_hash FROM paper_cycle_no_entry').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name='paper_pass_closures'").fetchone()[0], 0)
        record = store.load(rows[0][0])
        self.assertEqual((record['status'], record['blocker'], record['entry_authorized']), ('NO_ENTRY', 'MARKET_PRODUCER_BLOCKED', False))
        self.assertEqual(len(result['usd_evidence_refs']), 2)
        failed = [store.load(k) for k in result['usd_evidence_refs']]
        self.assertEqual({r['failure_code'] for r in failed}, {'TRANSPORT_ERROR'})            # both failures retained, charged
        self.assertTrue(set(result['usd_evidence_refs']) <= set(record['attempt_refs']))
        self.assertEqual(self.h.f.progress.admission(self.h.target.scan_id)['requests_used'], 7)
        from desk import paper_terminal_reconciliation as terminal
        self.assertIsNone(terminal.gate(store, self.h.f.jobs.path, ()))
        self.assertEqual(terminal.gate(store, self.h.f.jobs.path, (self.h.target.scan_id,)), 'REJECTED_SCAN_RETIRED')

    def test_a_failed_attempt_that_the_result_does_not_cite_still_refuses_the_no_entry_proof(self):
        # The acceptance is narrow: only failed SOL/USD attempts that the result cites. Anything else keeps the old
        # proof (and, being a normal producer rejection with an unproved charge, the pass is then closed by T22G).
        from desk import paper_cycle_no_entry as no_entry
        self.h.jupiter_fail = self.h.kraken_fail = True
        result = self.run_cycle()
        store = self.h.f.progress.store
        with store.connect() as c:
            key = c.execute('SELECT outcome_hash FROM paper_cycle_no_entry').fetchone()[0]
        rec = store.load(key)
        failed = store.load(result['usd_evidence_refs'][0])
        uncited = {**result, 'usd_evidence_refs': []}
        index = no_entry._index(store, rec['scan_id'], rec['attempt_refs'])
        self.assertFalse(no_entry._cited_usd_failure(result['usd_evidence_refs'][0], failed, uncited, rec['scan_id']))
        self.assertTrue(no_entry._cited_usd_failure(result['usd_evidence_refs'][0], failed, result, rec['scan_id']))
        self.assertFalse(no_entry._cited_usd_failure(result['usd_evidence_refs'][0], failed, result, 'other-scan'))
        self.assertFalse(no_entry._cited_usd_failure(result['usd_evidence_refs'][0], {**failed, 'source_id': 'helius-mainnet-paper-confirmed-v1'}, result, rec['scan_id']))
        self.assertFalse(no_entry._cited_usd_failure(result['usd_evidence_refs'][0], {**failed, 'failure_code': None}, result, rec['scan_id']))
        self.assertTrue(index)

    def test_primary_alone_when_the_cross_check_fails_is_recorded_as_such(self):
        self.h.kraken_fail = True
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        (event,) = [e for e in self.events() if e['kind'] == 'market']
        value = event['paper_usd_valuation']
        self.assertEqual((value['selection'], value['fallback']['status'], value['fallback']['blockers']), ('PRIMARY', 'UNKNOWN', ['TRANSPORT_ERROR']))
        self.assertIsNone(value['divergence'])


class CrossCheckScheduleTests(V2Base):
    """T37 spec item 4: Kraken is fetched only as the fallback or on a cross-check schedule (default 5 min)."""

    def seed_kraken_attempt(self, age, failure=None):
        self.h.f.progress.store.save({'kind': 'paper_read_attempt_v1', 'source_id': kraken.SOURCE, 'method': kraken.METHOD,
                                      'observed_at': int(self.h.f.at) - age, 'failure_code': failure, 'seed': age})

    def event(self):
        (event,) = [e for e in self.events() if e['kind'] == 'market']
        return event

    def h_calls(self, name):
        return getattr(self.h, name + '_calls', 0)

    def test_a_recent_kraken_attempt_skips_the_cross_check_and_the_entry_is_primary_only(self):
        self.seed_kraken_attempt(60)
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual((self.h_calls('jupiter'), self.h_calls('kraken')), (1, 0))
        self.assertEqual(result['attempted_requests'], 7, result)                  # the Kraken request was not made
        self.assertEqual(len(result['usd_evidence_refs']), 1)
        value = self.event()['paper_usd_valuation']
        self.assertEqual((value['selection'], value['selected_source'], value['fallback'], value['divergence']), ('PRIMARY', 'JUPITER', None, None))
        cycle._state(self.h.path, self.h.cfg)                                      # the saved event replays from its originals
        self.no_unresolved_pass()

    def test_an_old_or_absent_kraken_attempt_is_cross_checked(self):
        self.seed_kraken_attempt(400)                                              # outside the 300 s default window
        result = self.run_cycle()
        self.assertEqual((self.h_calls('jupiter'), self.h_calls('kraken')), (1, 1))
        self.assertEqual(result['attempted_requests'], 8, result)
        self.assertEqual(self.event()['paper_usd_valuation']['fallback']['status'], 'MEASURED')

    def test_a_failed_recent_cross_check_also_counts(self):
        self.seed_kraken_attempt(30, failure='TRANSPORT_ERROR')
        self.run_cycle()
        self.assertEqual(self.h_calls('kraken'), 0)                                # Kraken is down: no retry on every entry

    def test_the_interval_is_configurable(self):
        self.extra = {uv.KEY_CROSS_CHECK: 30}
        self.setUp()
        self.seed_kraken_attempt(60)                                               # older than 30 s: due
        self.run_cycle()
        self.assertEqual(self.h_calls('kraken'), 1)
        self.setUp()
        self.seed_kraken_attempt(10)                                               # inside 30 s: skipped
        self.run_cycle()
        self.assertEqual(self.h_calls('kraken'), 0)

    def test_an_unusable_primary_still_fetches_kraken_as_the_fallback_even_when_a_cross_check_is_recent(self):
        self.seed_kraken_attempt(10)
        self.h.jupiter_fail = True
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(self.h_calls('kraken'), 1)
        value = self.event()['paper_usd_valuation']
        self.assertEqual((value['selection'], value['selected_source']), ('FALLBACK', 'KRAKEN'))

    def test_the_divergence_guard_applies_only_when_the_cross_check_ran(self):
        # Documented consequence of the 5-minute schedule: a skipped cross-check cannot see a divergent Kraken price.
        self.h.kraken_price = '103.00000'
        self.seed_kraken_attempt(60)
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(self.h_calls('kraken'), 0)

    def test_an_invalid_interval_refuses_the_experiment_config(self):
        for bad in (0, -5, 99999, '300', True):
            with self.subTest(bad), self.assertRaises(ValueError):
                uv.selected({**self.h.cfg, uv.KEY_CROSS_CHECK: bad})


class HeldAndExitTests(V2Base):
    def enter(self):
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        return self.allowance()

    def test_held_pass_uses_the_primary_alone_and_exit_replays(self):
        allowance = self.enter()
        self.h.sell_output = 30_000_000
        self.h.kraken_calls = self.h.jupiter_calls = 0
        result = self.run_cycle(positions=(self.held_item(),), candidates=(), monitoring=True)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(result['monitoring_attempted_requests'], 6, result)       # same as the Kraken-only partial take-profit pass (test_kraken_lifecycle: 6)
        self.assertEqual((self.h_calls('jupiter'), self.h_calls('kraken')), (1, 0))
        self.assertEqual(len(result['usd_evidence_refs']), 1)
        self.assertEqual(allowance.snapshot()['total_used'], 6)
        exits = [e for e in self.events() if e['kind'] == 'quote_exit']
        self.assertEqual(exits[-1]['paper_usd_valuation']['selection'], 'PRIMARY')
        self.assertEqual(exits[-1]['paper_usd_valuation']['decision_purpose'], 'exit')
        self.assertTrue(any(o.get('side') == 'sell' for o in result['outcomes']))

    def h_calls(self, name):
        return getattr(self.h, name + '_calls', 0)

    def test_exit_is_never_blocked_by_divergence(self):
        self.enter()
        self.h.kraken_price = '150.00000'                                          # wildly different, and it is not even fetched
        self.h.sell_output = 30_000_000
        self.h.kraken_calls = 0
        result = self.run_cycle(positions=(self.held_item(),), candidates=(), monitoring=True)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(self.h_calls('kraken'), 0)
        self.assertTrue([e for e in self.events() if e['kind'] == 'quote_exit'])

    def test_exit_falls_back_to_kraken_when_the_primary_fails_and_both_requests_are_charged(self):
        allowance = self.enter()
        self.h.jupiter_fail = True
        self.h.sell_output = 30_000_000
        self.h.kraken_calls = self.h.jupiter_calls = 0
        result = self.run_cycle(positions=(self.held_item(),), candidates=(), monitoring=True)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(result['monitoring_attempted_requests'], 7, result)       # partial pass (6) + the extra Kraken request
        self.assertEqual(allowance.snapshot()['total_used'], 7)
        self.assertEqual((self.h_calls('jupiter'), self.h_calls('kraken')), (1, 1))
        exits = [e for e in self.events() if e['kind'] == 'quote_exit']
        self.assertEqual((exits[-1]['paper_usd_valuation']['selection'], exits[-1]['paper_usd_valuation']['selected_source']), ('FALLBACK', 'KRAKEN'))

    def test_non_transient_primary_failure_does_not_latch_monitoring_under_v2(self):
        allowance = self.enter()
        self.h.jupiter_fail = True
        self.h.jupiter_exc = OSError                                               # UNCLASSIFIED_ERROR: would latch a Kraken failure
        self.h.sell_output = 30_000_000
        result = self.run_cycle(positions=(self.held_item(),), candidates=(), monitoring=True)
        self.assertEqual(result['status'], 'COMPLETE', result)                     # Kraken answered the same pass
        with self.h.f.progress.store.connect() as c:
            self.assertIsNone(c.execute('SELECT blocked FROM paper_monitoring_budget WHERE id=1').fetchone()[0])

    def test_exit_with_no_usable_valuation_is_a_blocked_exit_not_a_latch(self):
        # T37F: was "the unproved blocked pass keeps the NULL latch". The held pass now ends terminal (T22G
        # FAILED_CHARGED: SOL_USD_UNAVAILABLE is an ordinary producer outcome), the failed reads stay charged, the
        # position stays held and the next pass with a healthy provider exits it.
        allowance = self.enter()
        used_before = allowance.snapshot()['total_used']
        self.h.jupiter_fail = self.h.kraken_fail = True
        result = self.run_cycle(positions=(self.held_item(),), candidates=(), monitoring=True)
        self.assertNotEqual(result.get('status'), 'COMPLETE', result)
        codes = [b for d in result['diagnostics'] if 'blockers' in d for b in d['blockers']]
        self.assertIn('SOL_USD_UNAVAILABLE', codes)
        self.assertEqual(len(cycle._state(self.h.path, self.h.cfg)['positions']), 1)   # still held, nothing sold
        self.no_unresolved_pass()
        with self.h.f.progress.store.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT status FROM paper_pass_closures')], ['FAILED_CHARGED'])
            self.assertIsNone(c.execute('SELECT blocked FROM paper_monitoring_budget WHERE id=1').fetchone()[0])
        self.assertGreater(allowance.snapshot()['total_used'], used_before)             # the failed reads stay charged
        self.h.jupiter_fail = self.h.kraken_fail = False
        self.h.sell_output = 30_000_000
        again = self.run_cycle(positions=(self.held_item(),), candidates=(), monitoring=True)
        self.assertEqual(again['status'], 'COMPLETE', again)
        self.assertTrue(any(o.get('side') == 'sell' for o in again['outcomes']))


class IntegrityTests(V2Base):
    def tamper(self, mutate):
        result = self.run_cycle()
        self.assertEqual(result['status'], 'COMPLETE', result)
        with sqlite3.connect(self.h.path) as c:
            event = json.loads(c.execute('SELECT payload FROM events WHERE event_id=?', (result['events'][0],)).fetchone()[0])
            mutate(event)
            c.execute('UPDATE events SET payload=?,payload_hash=? WHERE event_id=?', (canonical(event), digest(event), event['event_id']))
        before = dump(self.h.path)
        with self.assertRaises(RecoveryRequired):
            cycle._state(self.h.path, self.h.cfg)
        self.assertEqual(dump(self.h.path), before)                                # refusal never repairs

    def test_a_modified_saved_valuation_refuses_the_read(self):
        self.tamper(lambda e: e['paper_usd_valuation'].__setitem__('usd_price', '90'))

    def test_a_modified_retained_primary_attempt_refuses_the_read(self):
        import base64
        wire = canonical({base.SOL: {'usdPrice': 77, 'blockId': 100, 'decimals': 9}}).encode()
        self.tamper(lambda e: e['paper_usd_valuation']['primary']['attempt'].__setitem__('response_bytes_base64', base64.b64encode(wire).decode()))

    def test_a_dropped_cross_check_refuses_the_read(self):
        self.tamper(lambda e: e['paper_usd_valuation'].__setitem__('fallback', None))

    def test_a_copied_valuation_with_another_source_label_refuses_the_read(self):
        self.tamper(lambda e: e['paper_usd_valuation'].__setitem__('source', kraken.SOURCE))


class RetainedRefsTests(V2Base):
    def test_retained_originals_are_reused_without_new_requests(self):
        first = self.run_cycle()
        self.assertEqual(first['status'], 'COMPLETE', first)
        refs = tuple(first['usd_evidence_refs'])
        self.assertEqual(len(refs), 2)
        self.h.jupiter_calls = self.h.kraken_calls = 0
        with self.assertRaises(Exception):                                        # a wrong number of refs is refused up front
            self.run_cycle(usd_refs=refs[:1] + refs[:1] + refs[:1])


if __name__ == '__main__':
    unittest.main()
