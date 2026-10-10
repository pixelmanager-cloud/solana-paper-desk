"""T12 adversarial review of T01: category (b) integrity conditions must not become normal rejections.

Each ``expectedFailure`` is a RED demonstration of a real defect found by review; remove the decorator
when the defect is fixed. Fixtures only (synthetic bytes of tests.test_empty_history_no_entry); no network.
"""
import copy
import unittest
from contextlib import closing

from desk import paper_cycle_no_entry as no_entry
from tests.test_cycle_no_entry import fixture

# Blockers the market adapter (desk/paper_market_adapter.py:65-74,113,183) emits for supplied evidence that is
# corrupt, contradictory or unbound. The adapter returns them with ``event=None``; paper_cycle.fulfill_quotes
# (desk/paper_cycle.py:383-384) then collapses every one of them into the single top-level code
# MARKET_PRODUCER_BLOCKED, which T01 allow-lists as a "normal" rejection.
INTEGRITY_BLOCKERS = (
    'COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID',   # collected originals do not replay / bind (:73-74)
    'COORDINATOR_TARGET_BINDING_MISMATCH',           # observation is for a different target (:66-68)
    'COLLECTOR_OBSERVATION_REJECTED',                # collector recorded a failure (:69)
    'SOL_USD_TRUSTED_INPUT_INVALID',                 # trusted USD input rejected (:113)
    'EXPERIMENTAL_EVENT_CONTRACT_INVALID',           # event failed its own contract (:183)
)
# Ordinary data-quality blockers of an empty/stale window: these ARE normal rejections.
NORMAL_BLOCKERS = ('MISSING_WINDOW_MEASUREMENT:net_buy_ratio', 'STALE_WINDOW_MEASUREMENT:unique_buyers_5m')


def with_producer_blockers(result, blockers):
    forged = copy.deepcopy({k: v for k, v in result.items() if k != 'evidence_hash'})
    for diagnostic in forged['diagnostics']:
        if 'blockers' in diagnostic:
            diagnostic['blockers'] = list(blockers)
    return forged


class NestedIntegrityBlockersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t = fixture(legacy=True)          # real MARKET_PRODUCER_BLOCKED pass left NULL
        cls.addClassCleanup(cls.t.doCleanups)
        cls.scan = cls.t.h.f.target.scan_id
        cls.after = cls.t.result['budget'][cls.scan]['used']
        cls.before = cls.after - cls.t.result['attempted_requests']

    def check(self, blockers):
        forged = with_producer_blockers(self.t.result, blockers)
        no_entry._check_result(forged, 'MARKET_PRODUCER_BLOCKED', self.scan, self.before, self.after)

    def test_ordinary_empty_window_blockers_are_accepted(self):
        """Control: the vocabulary of a genuine empty/stale window still validates."""
        self.check(NORMAL_BLOCKERS)
        real = [d['blockers'] for d in self.t.result['diagnostics'] if 'blockers' in d][0]
        self.check(real)

    def test_integrity_blockers_nested_in_producer_diagnostics_must_not_validate_as_normal(self):
        accepted = []
        for code in INTEGRITY_BLOCKERS:
            try:
                self.check((code,))
                accepted.append(code)
            except ValueError:
                pass
        self.assertEqual(accepted, [], 'category (b) integrity blockers accepted as category (a)')

    def test_publish_must_leave_the_null_latch_for_integrity_blockers(self):
        t = self.t
        store = t.store
        with closing(store.connect()) as c:
            pending = c.execute('SELECT id,intent_hash FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()
        ledger = no_entry.ledger_snapshot(t.ctx['ledger_db'])
        forged = with_producer_blockers(t.result, ('COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID',))
        with self.assertRaises(ValueError):
            no_entry.publish(store, t.progress, pass_id=pending[0], intent_hash=pending[1], result=forged, ledger=ledger)
        with closing(store.connect()) as c:   # the pass must still be NULL (fail closed)
            self.assertIsNone(c.execute('SELECT outcome_hash FROM paper_observation_passes WHERE id=?', (pending[0],)).fetchone()[0])


if __name__ == '__main__':
    unittest.main()
