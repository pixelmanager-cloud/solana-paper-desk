"""Off-path identity (T14G item 6): with the flag absent, one fixture pass leaves byte-identical ledger content.

The golden file was produced by code WITHOUT any fill-realism hook (origin/integration/r1) running this very fixture
(`PaperCycleTests.run_cycle`, flag absent); this test runs the same fixture on the current code and compares the whole
cycle result and the ledger `events`, `outcomes`, `state` rows. Only random ids are masked, and the exact list of
masked field names is asserted, so widening the mask is a conscious edit.

Regenerate (on a tree without the hook):  python -m tests.test_fill_realism_identity --write tests/golden/...json
"""
import json
import re
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

GOLDEN = Path(__file__).resolve().parent / 'golden' / 'fill_realism_off_identity.json'
HEX = re.compile(r'(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{32})(?![0-9a-f])')
# Random per run (found by running the same tree twice and diffing): every id derived from the random scan/pass ids.
MASK_FIELDS = frozenset({'attempt_refs', 'collector_refs', 'entry_event_id', 'event_hash', 'event_id', 'events', 'evidence_hash',
                         'intent_hash', 'outcomes', 'pass_id', 'quote_hash', 'scan_id', 'source_hash', 'usd_evidence_refs',
                         'payload_hash'})
# Deterministic digests that legitimately stay visible (config, fixed provider bytes ...). Anything else that looks
# like an id and is NOT masked fails the test: a new field must be classified deliberately.
VISIBLE_HEX_FIELDS = frozenset({'feature_manifest_hash', 'history_hashes', 'mint_hash', 'payload_sha256', 'pool_hash',
                                'request_evidence_refs', 'request_sha256', 'source_hashes'})


def parse_nested(value):
    if isinstance(value, str) and value[:1] in '{[':
        try:
            return parse_nested(json.loads(value))
        except ValueError:
            return value
    if isinstance(value, dict):
        return {k: parse_nested(v) for k, v in value.items()}
    if isinstance(value, list):
        return [parse_nested(v) for v in value]
    return value


def normalise(value, field=None, used=None, leaked=None):
    """Mask random ids by field name; record which names were masked and which hex ids stayed visible."""
    used = set() if used is None else used
    leaked = set() if leaked is None else leaked
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if isinstance(key, str) and HEX.fullmatch(key):
                used.add('<dict key>')
                key = '<RANDOM_ID>'
            out[key] = normalise(item, key if isinstance(key, str) else field, used, leaked)[0]
        return out, used, leaked
    if isinstance(value, list):
        return [normalise(item, field, used, leaked)[0] for item in value], used, leaked
    if isinstance(value, str) and HEX.search(value):
        if field in MASK_FIELDS:
            used.add(field)
            return HEX.sub('<RANDOM_ID>', value), used, leaked
        leaked.add(field)
    return value, used, leaked


def snapshot(fixture, result):
    with closing(sqlite3.connect(fixture.path)) as c:
        ledger = {
            'events': [[r[0], r[1], r[2], parse_nested(r[3]), r[4]] for r in c.execute('SELECT seq,event_id,ts,payload,payload_hash FROM events ORDER BY seq')],
            'outcomes': [[r[0], r[1], parse_nested(r[2])] for r in c.execute('SELECT seq,event_id,payload FROM outcomes ORDER BY seq')],
            'state': [parse_nested(r[0]) for r in c.execute('SELECT payload FROM state')],
            'metadata_keys': sorted(r[0] for r in c.execute('SELECT key FROM metadata'))}
    return {'result': parse_nested(result), 'ledger': ledger}


def run_fixture():
    from tests import test_paper_cycle as fx
    fixture = fx.PaperCycleTests('run_cycle')
    fixture.setUp()
    fixture.http_calls, fixture.sell_output = [], 10_000_000
    result = fixture.actual_cycle()
    return fixture, result


class OffPathIdentityTests(unittest.TestCase):
    def test_flag_absent_leaves_the_ledger_and_cycle_result_byte_identical_to_code_without_the_hook(self):
        fixture, result = run_fixture()
        self.addCleanup(fixture.doCleanups)
        self.assertNotIn('paper_fill_realism_version', fixture.cfg)
        masked, used, leaked = normalise(snapshot(fixture, result))
        self.assertEqual(sorted(used), sorted(MASK_FIELDS | {'<dict key>'}),       # budget is keyed by the random scan id
                         'the masked field names must be exactly the declared list')
        self.assertEqual(sorted(leaked), sorted(VISIBLE_HEX_FIELDS),               # everything else stays visible and compared
                         'ids left visible must be exactly the declared deterministic fields')
        golden = json.loads(GOLDEN.read_text())
        self.assertEqual(json.dumps(masked, sort_keys=True), json.dumps(golden, sort_keys=True))

    def test_the_realism_files_are_never_created_when_off(self):
        fixture, result = run_fixture()
        self.addCleanup(fixture.doCleanups)
        from desk import fill_realism as fr
        self.assertFalse(fr.store_path(fixture.path).exists())
        self.assertNotIn('fill_realism', result)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--write':
        fixture, result = run_fixture()
        masked, used, leaked = normalise(snapshot(fixture, result))
        Path(sys.argv[2]).write_text(json.dumps(masked, sort_keys=True, indent=1) + '\n')
        print(json.dumps({'masked_fields': sorted(used), 'visible_hex_fields': sorted(x for x in leaked if x)}))
        fixture.doCleanups()
    else:
        unittest.main()
