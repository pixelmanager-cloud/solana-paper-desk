"""SYNTHETIC_TEST_ONLY: real handoff accounting and original-wire paper cycle.

Private fixture policy pins authorize only temporary fixture paths. No production
policy, provider response, budget validator or checkpoint validator is replaced.
"""
from dataclasses import replace
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desk import paper_cycle, quote_execution
from tests import test_monitoring_handoff as handoff_fixture
from tests import test_token2022_paper as token_fixture
from tests.test_paper_cycle import dump
from tests.helpers import T


class Token2022HandoffCompositionTests(unittest.TestCase):
    def test_actual_entry_partial_exit_close_restart_preserves_retired_history(self):
        h = handoff_fixture.MonitoringHandoffTests()
        h.setUp()
        self.addCleanup(h.doCleanups)
        v = token_fixture.VerticalProfileTests()
        v.setUp()
        self.addCleanup(v.doCleanups)
        cycle = v.f
        # Transfer only explicitly synthetic protocol bytes and migration source
        # records into the real preserved research/evidence context.
        previous = cycle.f.progress.store
        refs = []
        for ref in cycle.item.graduation_refs:
            record = previous.load(ref)
            response = previous.load(record['response_hash'])
            response['data'][0]['blockTime'] += T + 1 - cycle.f.at
            from desk.programs import unbase58
            from desk.security import base58
            ix = response['data'][0]['meta']['innerInstructions'][0]['instructions'][0]
            raw = bytearray(unbase58(ix['data']))
            raw[136:144] = response['data'][0]['blockTime'].to_bytes(8, 'little', signed=True)
            ix['data'] = base58(raw)
            record['response_hash'] = h.store.save(response)
            refs.append(h.store.save(record))
        h.f.context.protocol = cycle.f.protocol
        h.f.context.at = T + 1
        cycle.f = h.f.context
        cycle.target = h.target
        cycle.item = replace(cycle.item, target=h.target, graduated_at=T + 1 - 600,
                             history_as_of=T + 1, graduation_refs=tuple(refs))
        cycle.path = h.new
        cycle.cfg = h.cfg
        # The reused wire fixture patches transport credential lookup. Isolate
        # that lookup from the real pacing environment used by handoff checks.
        from desk import paper_read_sources
        transport_os = SimpleNamespace(environ=SimpleNamespace(get=os.environ.get))
        boundary = patch.object(paper_read_sources, 'os', transport_os)
        boundary.start()
        self.addCleanup(boundary.stop)
        old_ref = h.charge_original()
        retired = dump(h.old)
        source = h.f.context.jobs.source(h.target.scan_id)
        self.assertEqual(h.activate()['status'], 'BOUND')
        self.assertEqual(h.active.snapshot()['total_used'], 1)
        entry = cycle.actual_cycle()
        self.assertEqual(entry['status'], 'COMPLETE', entry)
        self.assertTrue(any(row.get('side') == 'buy' for row in entry['outcomes']))
        admission = cycle.f.progress.admission(h.target.scan_id)
        self.assertEqual(admission['requests_used'], 9)
        while cycle.f.progress.admission(h.target.scan_id)['requests_used'] < 18:
            self.assertTrue(cycle.f.progress.reserve(h.target.scan_id))
        admission = cycle.f.progress.admission(h.target.scan_id)
        research = dump(h.research)
        position = paper_cycle._state(h.new, h.cfg)['positions'][h.target.mint]
        item = replace(cycle.item, graduation_refs=(), target=replace(
            h.target, amount_raw=quote_execution.raw_quantity(position['qty'], 6)))
        cycle.sell_output = 30_000_000
        partial = cycle.actual_cycle(positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(partial['status'], 'COMPLETE', partial)
        self.assertEqual(partial['monitoring_attempted_requests'], 5)
        position = paper_cycle._state(h.new, h.cfg)['positions'][h.target.mint]
        item = replace(item, target=replace(item.target,
            amount_raw=quote_execution.raw_quantity(position['qty'], 6)))
        cycle.sell_output = 6_000_000
        closed = cycle.actual_cycle(positions=(item,), candidates=(), monitoring=True)
        self.assertEqual(closed['status'], 'COMPLETE', closed)
        self.assertEqual(closed['monitoring_attempted_requests'], 4)
        self.assertEqual(paper_cycle._state(h.new, h.cfg)['positions'], {})
        self.assertEqual(h.active.snapshot()['total_used'], 10)
        self.assertEqual(cycle.f.progress.admission(h.target.scan_id), admission)
        self.assertEqual(cycle.f.jobs.source(h.target.scan_id), source)
        self.assertEqual(dump(h.research), research)
        self.assertEqual(dump(h.old), retired)
        self.assertIsNone(h.store.load(old_ref)['failure_code'])
        before = len(cycle.http_calls)
        restart = cycle.actual_cycle(candidates=(), monitoring=True)
        self.assertEqual(restart['status'], 'COMPLETE', restart)
        self.assertEqual(restart['attempted_requests'], 0)
        self.assertEqual(len(cycle.http_calls), before)
        self.assertEqual(h.active.snapshot()['total_used'], 10)
        self.assertEqual(cycle.f.progress.admission(h.target.scan_id), admission)
        self.assertEqual(dump(h.old), retired)
