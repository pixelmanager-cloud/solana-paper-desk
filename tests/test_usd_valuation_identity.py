"""SYNTHETIC_TEST_ONLY: flag-absent byte identity of the paper ledger (T37F item 3).

With `paper_usd_valuation_version` absent (legacy, "version 0") or 1 (Kraken only) the whole entry -> held mark ->
exit lifecycle must write the SAME ledger bytes as before valuation 2 existed. GOLDEN holds sha256 digests of the
ledger's `events` and `outcomes` payload rows (in sequence order) produced by the same scripted lifecycle on
`origin/cloud/T22G` (the base before any USD v2 code, BASE_COMMIT below) and reproduced here; any byte difference
in an event or outcome under these two configurations fails the test. `metadata` is excluded on purpose: it carries
the implementation hash, which changes with every `desk/` edit by design.

Regenerate (only with a reviewed reason):  python -m tests.test_usd_valuation_identity --print
"""
import hashlib
import itertools
import json
import sqlite3
import sys
import unittest
import uuid
from dataclasses import replace
from unittest.mock import patch

from desk import paper_cycle as cycle, quote_execution as qe
from tests import test_kraken_lifecycle as kraken_fixture, test_paper_cycle as fixtures

BASE_COMMIT = '93c181bd8ff0'   # origin/cloud/T22G, before any USD v2 code (also reproduced on cloud/T37F)


def ledger_digests(path):
    out = {}
    with sqlite3.connect(path) as c:
        for table, order in (('events', 'seq'), ('outcomes', 'seq')):
            digest = hashlib.sha256()
            rows = c.execute(f'SELECT payload FROM {table} ORDER BY {order}').fetchall()
            for (payload,) in rows:
                digest.update(len(payload).to_bytes(8, 'big') + payload.encode())
            out[table] = [len(rows), digest.hexdigest()]
    return out


FROZEN = 1_800_000_000.0     # the fixtures derive every event time from the wall clock: freeze it for byte-stable output


def deterministic():
    """Freeze the wall clock and make every uuid4 (scan, pass and dispatch identities) a counter."""
    counter = itertools.count(1)
    return (patch('time.time', return_value=FROZEN),
            patch('uuid.uuid4', side_effect=lambda: uuid.UUID(int=next(counter))))


def run_v0():
    clock, ids = deterministic()
    with clock, ids:
        return _run_v0()


def _run_v0():
    h = fixtures.PaperCycleTests()
    h.setUp()
    h.http_calls = []
    h.sell_output = 10_000_000
    entry = h.actual_cycle()
    assert entry['status'] == 'COMPLETE', entry
    position = cycle._state(h.path, h.cfg)['positions'][h.target.mint]
    item = replace(h.item, target=replace(h.target, amount_raw=qe.raw_quantity(position['qty'], 6)))
    refs = tuple(entry['usd_evidence_refs'])
    mark = h.actual_cycle(positions=(item,), candidates=(), usd_refs=refs)
    assert mark['status'] == 'COMPLETE', mark
    h.sell_output = 7_000_000
    exit_result = h.actual_cycle(positions=(item,), candidates=(), usd_refs=refs)
    assert exit_result['status'] == 'COMPLETE', exit_result
    digests = ledger_digests(h.path)
    h.doCleanups()
    return digests


def run_v1():
    clock, ids = deterministic()
    with clock, ids:
        return _run_v1()


def _run_v1():
    t = kraken_fixture.KrakenLifecycleTests('test_entry_held_exit_restart_accounting_and_originals')
    t.setUp()
    h = t.h
    entry = kraken_fixture.actual_cycle(h)
    assert entry['status'] == 'COMPLETE', entry
    from desk.monitoring_budget import MonitoringBudget
    MonitoringBudget(h.f.progress.store, h.path, h.cfg, clock=lambda: h.f.at).provision()
    position = cycle._state(h.path, h.cfg)['positions'][h.target.mint]
    item = replace(h.item, target=replace(h.target, amount_raw=qe.raw_quantity(position['qty'], position['quote_execution']['mint_decimals'])))
    mark = kraken_fixture.actual_cycle(h, positions=(item,), candidates=(), monitoring=True)
    assert mark['status'] == 'COMPLETE', mark
    h.sell_output = 7_000_000
    exit_result = kraken_fixture.actual_cycle(h, positions=(item,), candidates=(), monitoring=True)
    assert exit_result['status'] == 'COMPLETE', exit_result
    digests = ledger_digests(h.path)
    t.doCleanups()
    return digests


GOLDEN_V0 = {"events": [4, "1a3a9220e8bba6b615d50d1d1c490fdd41a134a6b03867ddb4be2a50922bbdba"], "outcomes": [2, "fc1021cab7d837dc62aa60ab007bc500a161edd6571673b7b062999c49f73c72"]}
GOLDEN_V1 = {"events": [4, "678779587052dd747c0712a58be9c930b1a52badc5bdd384465c5d806d56faab"], "outcomes": [2, "1cd29d9a3d351e0882162bc5d2e1fb4c0584daa0846911c2d3719373666c9000"]}


class FlagAbsentByteIdentityTests(unittest.TestCase):
    def test_version_absent_ledger_bytes_are_unchanged(self):
        self.assertEqual(run_v0(), GOLDEN_V0)

    def test_version_1_ledger_bytes_are_unchanged(self):
        self.assertEqual(run_v1(), GOLDEN_V1)

    def test_the_scripted_lifecycle_is_itself_deterministic(self):
        self.assertEqual(run_v1(), run_v1())


if __name__ == '__main__':
    if '--print' in sys.argv:
        print(json.dumps({'GOLDEN_V0': run_v0(), 'GOLDEN_V1': run_v1()}, sort_keys=True))
    else:
        unittest.main()
