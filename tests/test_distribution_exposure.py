"""Synthetic fixtures only: no providers, credentials, signers or network."""
from dataclasses import replace
import unittest
from desk.distribution_exposure import (Balance, Classification, Kind, Limits,
                                       Position, Seed, Transfer, ValidatedSnapshot, trace_exposure)


class ExposureTests(unittest.TestCase):
    def snapshot(self, initial, current, **changes):
        rows = lambda amounts: tuple(Balance(a, 'owner-' + a, n) for a, n in amounts.items())
        return replace(ValidatedSnapshot('mint', 0, 20, sum(initial.values()),
                       rows(initial), rows(current), 'snapshot-fixture', 'history-fixture', True, True), **changes)

    def labels(self, amounts, **kinds):
        return tuple(Classification(a, 'owner-' + a, 'mint', kinds.get(a, Kind.PRIVATE),
                                    0, 20, ('classification-fixture-' + a,), True) for a in amounts)

    def transfer(self, slot, source, destination, amount, index=0):
        return Transfer(f'record-{slot}-{index}', Position(slot, 0, index), source, destination,
                        amount, f'transfer-fixture-{slot}-{index}')

    def run_trace(self, initial, current, transfers=(), seeds=None, labels=None, **kw):
        return trace_exposure(self.snapshot(initial, current), tuple(transfers),
                              tuple(seeds if seeds is not None else [Seed('a', initial['a'])]),
                              self.labels(current) if labels is None else labels, **kw)

    def test_split_fanout_exact_large_integers(self):
        n = 2**63 + 37
        r = self.run_trace({'a': n, 'b': 0, 'c': 0}, {'a': 0, 'b': 3, 'c': n-3},
                           [self.transfer(1, 'a', 'b', 3), self.transfer(2, 'a', 'c', n-3)])
        self.assertEqual(r.lower_bound, n)
        self.assertEqual(r.unresolved, 0)
        self.assertEqual([h.lower_bound for h in r.holders], [0, 3, n-3])

    def test_split_relay_replays_before_later_receipt(self):
        r = self.run_trace({'a': 10, 'b': 0, 'c': 0}, {'a': 0, 'b': 5, 'c': 5},
                           [self.transfer(3, 'a', 'b', 5), self.transfer(2, 'b', 'c', 5),
                            self.transfer(1, 'a', 'b', 5)])
        self.assertEqual(r.lower_bound, 10)

    def test_commingling_does_not_invent_fifo(self):
        r = self.run_trace({'a': 10, 'b': 10, 'c': 0}, {'a': 0, 'b': 10, 'c': 10},
                           [self.transfer(1, 'a', 'b', 10), self.transfer(2, 'b', 'c', 10)])
        self.assertEqual(r.lower_bound, 0)
        self.assertEqual(r.unresolved, 20)

    def test_guaranteed_partial_outflow(self):
        r = self.run_trace({'a': 10, 'b': 2, 'c': 0}, {'a': 0, 'b': 3, 'c': 9},
                           [self.transfer(1, 'a', 'b', 10), self.transfer(2, 'b', 'c', 9)])
        self.assertEqual(r.holders[2].lower_bound, 7)
        self.assertEqual(r.holders[2].possible, 9)

    def test_cycles_conserve_current_exposure(self):
        r = self.run_trace({'a': 10, 'b': 0}, {'a': 10, 'b': 0},
                           [self.transfer(1, 'a', 'b', 10), self.transfer(2, 'b', 'a', 10)])
        self.assertEqual(r.lower_bound, 10)
        self.assertEqual(r.processed_transfers, 2)

    def test_depth_limit_keeps_downstream_unresolved(self):
        initial = dict(a=10, b=0, c=0, d=0, e=0)
        current = dict(a=0, b=0, c=0, d=0, e=10)
        r = self.run_trace(initial, current, [self.transfer(i, a, b, 10)
                          for i, (a, b) in enumerate(zip('abcd', 'bcde'), 1)])
        self.assertEqual((r.lower_bound, r.unresolved), (0, 10))
        self.assertIn('DEPTH_LIMIT', r.reasons)

    def test_verified_service_hub_does_not_create_private_attribution(self):
        initial, current = dict(a=10, b=0, c=0), dict(a=0, b=4, c=6)
        r = self.run_trace(initial, current, [self.transfer(1, 'a', 'b', 10),
                           self.transfer(2, 'b', 'c', 6)], labels=self.labels(current, b=Kind.SERVICE))
        self.assertEqual((r.lower_bound, r.unresolved, r.excluded), (0, 6, 4))

    def test_exact_account_binding_not_owner_wide_exclusion(self):
        snapshot = self.snapshot(dict(a=5, b=5), dict(a=5, b=5))
        snapshot = replace(snapshot, initial=(Balance('a', 'same', 5), Balance('b', 'same', 5)),
                           current=(Balance('a', 'same', 5), Balance('b', 'same', 5)))
        c = Classification('a', 'same', 'mint', Kind.POOL, 0, 20, ('pool-proof',), True)
        r = trace_exposure(snapshot, (), (Seed('a', 5),), (c,))
        self.assertEqual((r.excluded, r.unresolved), (5, 5))

    def test_missing_labels_default_unresolved(self):
        r = self.run_trace(dict(a=10), dict(a=10), labels=())
        self.assertEqual((r.lower_bound, r.unresolved, r.excluded), (0, 10, 0))

    def test_unverified_stale_wrong_mint_owner_or_unproven_label(self):
        c = self.labels(dict(a=10))[0]
        for change in [dict(verified=False), dict(verified=1), dict(valid_through_slot=19),
                       dict(valid_from_slot=1), dict(mint='other'), dict(owner='other'),
                       dict(evidence_refs=()), dict(kind='service')]:
            with self.subTest(change=change):
                r = self.run_trace(dict(a=10), dict(a=10), labels=(replace(c, **change),))
                self.assertEqual(r.unresolved, 10)
                self.assertEqual(r.excluded, 0)

    def test_conflicting_classifications_never_choose_one(self):
        c = self.labels(dict(a=10))[0]
        r = self.run_trace(dict(a=10), dict(a=10), labels=(c, replace(c, kind=Kind.POOL), c))
        self.assertEqual(r.unresolved, 10)
        self.assertIn('CLASSIFICATION_CONFLICT', r.reasons)

    def test_unverified_snapshot_cannot_exclude_service(self):
        for changes in [dict(validated=False), dict(history_complete=False), dict(snapshot_ref=''),
                        dict(history_ref='')]:
            r = trace_exposure(self.snapshot(dict(a=10), dict(a=10), **changes), (),
                               (Seed('a', 10),), self.labels(dict(a=10), a=Kind.POOL))
            self.assertEqual((r.lower_bound, r.unresolved, r.excluded), (0, 10, 0))

    def test_missing_paths_and_insufficient_source_fail_closed(self):
        for t in [self.transfer(1, 'missing', 'b', 10), self.transfer(1, 'b', 'a', 10)]:
            r = self.run_trace(dict(a=10, b=0), dict(a=0, b=10), [t])
            self.assertEqual((r.lower_bound, r.unresolved), (0, 10))

    def test_missing_transfer_reconciliation_blocks_lower_bound(self):
        r = self.run_trace(dict(a=10, b=0), dict(a=5, b=5))
        self.assertIn('ENDING_BALANCE_MISMATCH', r.reasons)
        self.assertEqual(r.lower_bound, 0)

    def test_control_change_blocks_attribution(self):
        s = self.snapshot(dict(a=10), dict(a=10))
        r = trace_exposure(replace(s, current=(Balance('a', 'changed', 10),)), (), (Seed('a', 10),))
        self.assertEqual(r.unresolved, 10)
        self.assertIn('ACCOUNT_CONTINUITY_UNRESOLVED', r.reasons)

    def test_transfer_limit_discards_partial_lower_bounds(self):
        r = self.run_trace(dict(a=10, b=0), dict(a=0, b=10),
                           [self.transfer(1, 'a', 'b', 10), self.transfer(2, 'b', 'a', 1)],
                           limits=Limits(max_transfers=1))
        self.assertEqual(r.lower_bound, 0)
        self.assertIn('TRANSFER_LIMIT', r.reasons)

    def test_duplicate_records_and_ambiguous_positions_rejected(self):
        t = self.transfer(1, 'a', 'b', 5)
        for second in [t, replace(t, record_id='different'),
                       replace(t, position=Position(2, 0, 0), amount=4)]:
            with self.assertRaises(ValueError):
                self.run_trace(dict(a=10, b=0), dict(a=0, b=10), [t, second])

    def test_same_slot_verified_numeric_instruction_order(self):
        r = self.run_trace(dict(a=10, b=0, c=0), dict(a=0, b=0, c=10),
                           [self.transfer(1, 'b', 'c', 10, 10), self.transfer(1, 'a', 'b', 10, 2)])
        self.assertEqual(r.lower_bound, 10)

    def test_before_boundary_after_cutoff_rejected(self):
        for slot in [0, 21]:
            with self.assertRaises(ValueError):
                self.run_trace(dict(a=10, b=0), dict(a=0, b=10), [self.transfer(slot, 'a', 'b', 10)])

    def test_integer_types_and_hard_limits(self):
        for amount in [True, 1.0, '1', -1]:
            with self.assertRaises(ValueError):
                self.run_trace(dict(a=10), dict(a=10), seeds=[Seed('a', amount)])
        for limits in [Limits(max_depth=4), Limits(max_accounts=10001), Limits(max_transfers=10001),
                       Limits(max_depth=True), Limits(max_depth=0), Limits(max_accounts=1)]:
            with self.assertRaises(ValueError):
                self.run_trace(dict(a=10, b=0), dict(a=10, b=0), limits=limits)

    def test_supply_duplicate_accounts_duplicate_seeds_rejected(self):
        s = self.snapshot(dict(a=10), dict(a=10))
        for snapshot, seeds in [(replace(s, supply=11), (Seed('a', 10),)),
                                (replace(s, current=s.current*2), (Seed('a', 10),)),
                                (s, (Seed('a', 5), Seed('a', 5)))]:
            with self.assertRaises(ValueError):
                trace_exposure(snapshot, (), seeds)

    def test_self_transfer_no_duplication_and_originals_unchanged(self):
        s = self.snapshot(dict(a=10), dict(a=10))
        ts = (self.transfer(1, 'a', 'a', 10),)
        r = trace_exposure(s, ts, (Seed('a', 10),), self.labels(dict(a=10)))
        self.assertEqual(r.lower_bound, 10)
        self.assertEqual(s.current[0].amount, 10)
        self.assertEqual(ts[0].amount, 10)
        self.assertIn('transfer-fixture-1-0', r.evidence_refs)
        self.assertFalse(hasattr(r, 'eligible_for_trading'))

    def test_unknown_intermediate_cannot_launder_lower_bound(self):
        initial, current = dict(a=10, b=0, c=0), dict(a=0, b=0, c=10)
        labels = tuple(c for c in self.labels(current) if c.account != 'b')
        r = self.run_trace(initial, current, [self.transfer(1, 'a', 'b', 10),
                           self.transfer(2, 'b', 'c', 10)], labels=labels)
        self.assertEqual((r.lower_bound, r.unresolved), (0, 10))

    def test_empty_cohort_never_means_zero_exposure(self):
        r = self.run_trace(dict(a=10), dict(a=10), seeds=[])
        self.assertEqual(r.unresolved, 10)
        self.assertIn('COHORT_SEEDS_MISSING', r.reasons)

    def test_interval_bounds_against_exhaustive_token_allocation_oracle(self):
        # Independent discrete-token allocations exercise splitting, commingling,
        # cycles and repeated sends without choosing a provenance convention.
        from itertools import product
        edges = [('a', 'b'), ('b', 'a'), ('b', 'c'), ('c', 'b')]
        for sequence in product(edges, repeat=3):
            balances = dict(a=3, b=2, c=0)
            possible = {(2, 0, 0)}
            transfers = []
            for slot, (source, destination) in enumerate(sequence, 1):
                amount = min(2, balances[source])
                if not amount:
                    continue
                si, di = 'abc'.index(source), 'abc'.index(destination)
                allocations = set()
                for state in possible:
                    for moved in range(max(0, amount-(balances[source]-state[si])),
                                       min(amount, state[si])+1):
                        next_state = list(state)
                        next_state[si] -= moved
                        next_state[di] += moved
                        allocations.add(tuple(next_state))
                possible = allocations
                balances[source] -= amount
                balances[destination] += amount
                transfers.append(self.transfer(slot, source, destination, amount))
            r = self.run_trace(dict(a=3, b=2, c=0), balances, transfers, seeds=[Seed('a', 2)])
            for index, holder in enumerate(r.holders):
                self.assertLessEqual(holder.lower_bound, min(s[index] for s in possible))
                self.assertGreaterEqual(holder.possible, max(s[index] for s in possible))
