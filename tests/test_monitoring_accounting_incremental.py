"""T24/F14: incremental monitoring accounting. Fixtures only; no network, no provider keys.

The verified-prefix hint is an untrusted optimisation: whatever it says, tampering with a
retained reservation, outcome or evidence blob must still fail closed.
"""
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.monitoring_budget import MonitoringBlocked, AUDIT_SLICE
from tests import test_monitoring_budget as base


class _Fixture(base.MonitoringBudgetTests):
    pass


for _name in dir(base.MonitoringBudgetTests):
    if _name.startswith('test_'):
        setattr(_Fixture, _name, None)


class Incremental(_Fixture):
    def warm(self, n):
        for _ in range(n):
            if self.budget.snapshot()['remaining'] == 0:
                self.now += 3601                      # next rolling hour; the cap is not under test
            self.read()
        self.budget.snapshot()

    def sidecar(self):
        return self.budget._verified_path()

    def loads(self):
        calls = []
        real = EvidenceStore.load

        def counting(store, key, *a, **k):
            calls.append(key)
            return real(store, key, *a, **k)
        with patch.object(EvidenceStore, 'load', counting):
            self.budget.snapshot()
        return len(calls)

    def tamper(self, sql, args=()):
        with closing(sqlite3.connect(self.store.path)) as c:
            for table in ('paper_monitoring_reservations', 'paper_monitoring_outcomes'):
                for action in ('update', 'delete', 'insert'):
                    c.execute('DROP TRIGGER IF EXISTS %s_%s' % (table, action))
            c.execute(sql, args)
            c.commit()

    def test_cost_is_flat_in_history_and_sidecar_records_the_prefix(self):
        self.warm(20)
        small = self.loads()
        self.warm(60)
        large = self.loads()
        self.assertLessEqual(large, AUDIT_SLICE)
        self.assertEqual(small, large)
        data = json.loads(self.sidecar().read_text())
        self.assertEqual(data['through'], 80)

    def test_no_sidecar_means_full_verification_and_rewrites_it(self):
        self.warm(10)
        self.sidecar().unlink()
        self.assertEqual(self.loads(), 10)
        self.assertEqual(json.loads(self.sidecar().read_text())['through'], 10)

    def test_row_tuple_tamper_in_trusted_prefix_is_detected(self):
        self.warm(12)
        self.tamper('UPDATE paper_monitoring_reservations SET at=at+1 WHERE id=3')
        with self.assertRaises(MonitoringBlocked) as caught:
            self.budget.snapshot()
        self.assertEqual(caught.exception.code, 'MONITORING_ACCOUNTING_INVALID')

    def test_outcome_swap_in_trusted_prefix_is_detected(self):
        self.warm(12)
        other = self.store.save({'unrelated': 'valid page'})
        self.tamper('UPDATE paper_monitoring_outcomes SET evidence_hash=? WHERE reservation_id=2', (other,))
        with self.assertRaises(MonitoringBlocked):
            self.budget.snapshot()

    def test_forged_sidecar_cannot_hide_a_row_tamper(self):
        self.warm(12)
        self.tamper('UPDATE paper_monitoring_reservations SET mint=? WHERE id=5', ('x' * 44,))
        data = json.loads(self.sidecar().read_text())
        data['through'] = 12
        self.sidecar().write_text(json.dumps(data))      # stale digest: tuple digest no longer matches
        with self.assertRaises(MonitoringBlocked):
            self.budget.snapshot()

    def test_garbage_or_foreign_sidecars_are_ignored_not_trusted(self):
        self.warm(6)
        good = json.loads(self.sidecar().read_text())
        for text in ('not json', '[]', '{}', json.dumps({**good, 'through': 0}), json.dumps({**good, 'through': True}),
                     json.dumps({**good, 'digest': 'z' * 64}), json.dumps({**good, 'binding': 'f' * 64}),
                     json.dumps({**good, 'extra': 1}), json.dumps({**good, 'through': 10 ** 9})):
            self.sidecar().write_text(text)
            self.assertEqual(self.loads(), 6, text)
            self.assertEqual(self.budget.snapshot()['total_used'], 6)

    def test_blob_corruption_of_an_unverified_tail_row_is_detected(self):
        self.warm(8)
        self.read()                                     # row 9 is beyond the trusted prefix
        with self.store.connect() as c:
            key = c.execute('SELECT evidence_hash FROM paper_monitoring_outcomes WHERE reservation_id=9').fetchone()[0]
            c.execute('UPDATE pages SET payload=? WHERE hash=?', (b'corrupt', key))
        self.sidecar().write_text(self.sidecar().read_text())
        with self.assertRaises(MonitoringBlocked):
            self.budget.snapshot()

    def test_pending_reservation_stops_the_trusted_prefix(self):
        self.warm(5)
        with closing(sqlite3.connect(self.store.path)) as c:
            c.execute('DROP TRIGGER IF EXISTS paper_monitoring_outcomes_delete')
            c.execute('DELETE FROM paper_monitoring_outcomes WHERE reservation_id=3')
            c.commit()
        self.budget.snapshot()
        self.assertEqual(json.loads(self.sidecar().read_text())['through'], 2)


if __name__ == '__main__':
    unittest.main()
