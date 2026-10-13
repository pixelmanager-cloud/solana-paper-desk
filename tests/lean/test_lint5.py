"""SYNTHETIC_TEST_ONLY: LINT5 integration fixes for the Round C branches (C1 replay / tune, C3 census, C4 backfill, C2 curves).

C1: a mark row the recorder wrote with ``marks.model: effective`` is replayed with ITS model (vault + virtual reserves) and ITS
fee_bps through the labels / marks helpers; a vault row keeps the vault reserves and the fixture tier; a proposal needs the
proposed policy's own test-block CI lower bound above zero.
"""
import json
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import adapters as A, labels as LB, marks as M, paths as L, replay as R, strategy as S, tune as T
from tests.lean.fakeworld import T0
from tests.lean.test_m1 import EFFECTIVE, World1, boosted
from tests.lean.test_e2e_real import ROOT, steps

BASE = S.StrategyConfig.load(ROOT / 'config' / 'lean' / 'strategy-default.json')
B = 10 ** 15
Q0 = 80 * 10 ** 9
VIRT = 40 * 10 ** 9
SIZE = 200_000_000


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


def _db(path, rows, start=1000.0):
    store = L.PathStore(path)
    store.insert_path(dict(mint='M1', candidate_id=1, start_ts=start, ends_ts=start + 6 * 3600, entered=1, stage=None, reason='[]',
                           screen_reasons='[]', holders_checked=1, features=json.dumps({'supply_raw': str(B)}), target='{}',
                           carried_from=None, code_version='c', strategy_version='s'))
    store.add(marks=rows)
    store.close()
    return path


def eff_rows(ratios, *, model='effective', fee=120, virtual=VIRT):
    return [('M1', 1000.0 + 15 * i, i, B, int(Q0 * r), None, None, 'OK', model, virtual if model == 'effective' else None, 0, fee,
             'dynamic' if model == 'effective' else 'config') for i, r in enumerate(ratios)]


class C1EffectiveReplay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.costs = R.costs_from(None, R.load_fee_tiers())

    def prep(self, rows, costs=None):
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))                   # one recorder file per call
        db = _db(folder / 'paths.sqlite', rows)
        return R.prepare(R.load_paths(db)['M1'], costs or self.costs, SIZE)

    def test_effective_rows_use_virtual_reserves_and_their_own_fee(self):
        p = self.prep(eff_rows([1, 1.1, 1.3, 0.9]))
        self.assertEqual(p.bps, [120, 120, 120, 120])                       # the row's fee, not the fixture tier (125 at 80 SOL)
        self.assertTrue(all(pt is not None and pt.effective for pt in p.points))
        expect_qty = M.buy_output(SIZE, B, Q0 + VIRT, 120) * 9950 // 10000
        self.assertEqual(p.qty0, expect_qty)
        point = LB.Point(1015.0, B, int(Q0 * 1.1), virtual_raw=VIRT, fee_bps=120, model='effective')
        self.assertEqual(p.value(1, p.qty0, self.costs.pcfg), A.sol(LB.mark_lamports(point, p.qty0, None, self.costs.pcfg)))
        out = LB.sell_quote_lamports(point, p.qty0, None)
        self.assertEqual(p.proceeds(1, p.qty0, self.costs), out * 9950 // 10000 - 250_000)

    def test_flat_costs_never_override_an_effective_rows_fee(self):
        flat = R.costs_from({'fee_model': 'flat', 'flat_bps': 25}, R.load_fee_tiers())
        self.assertEqual(self.prep(eff_rows([1, 1.1]), flat).bps, [120, 120])

    def test_vault_rows_keep_the_vault_reserves_and_the_fixture_tier(self):
        p = self.prep(eff_rows([1, 1.1, 1.3], model='vault', fee=25))
        self.assertEqual(p.points, [])
        self.assertEqual(p.bps, [125, 125, 125])                            # the recorded 25 bps is the config fee, not the pool's
        spent = SIZE * (10000 - 125) // 10000
        self.assertEqual(p.qty0, B * spent // (Q0 + spent) * 9950 // 10000)

    def test_the_virtual_reserve_changes_the_decisions(self):
        # +40% on the vault is only +26.7% on vault + virtual: a full TP at 1.3 fires on the vault model only
        ratios = [1, 1, 1.4, 1.4, 1.4]
        pol = R.expand_grid([{'family': 'full_tp', 'x': 1.3}])[0]
        eff = R.replay_path(self.prep(eff_rows(ratios)), pol, BASE, self.costs, 0)
        vault = R.replay_path(self.prep(eff_rows(ratios, model='vault')), pol, BASE, self.costs, 0)
        self.assertEqual(vault.reasons, ['TAKE_PROFIT'])
        self.assertEqual(eff.reasons, ['PATH_END'])

    def test_a_malformed_effective_row_falls_back_to_vault_not_guessed(self):
        rows = eff_rows([1, 1.1])
        rows[1] = rows[1][:9] + (None,) + rows[1][10:]                      # virtual missing
        p = self.prep(rows)
        self.assertIsNotNone(p.points[0])
        self.assertIsNone(p.points[1])
        self.assertEqual(p.bps[1], 125)


class C1ReplayReproducesTheEffectiveRecorder(World1):
    """E2E (real runner + real recorder on fakeworld, marks.model effective): the replay's mark of the recorder's own reference
    position equals the recorder's ``net_sol`` on every row (same helper, same inputs)."""

    def test_replay_marks_equal_the_recorded_effective_marks(self):
        token = boosted(path=steps((120, 1.1), (240, 0.9)))
        r = self.build(token, cfg_over=EFFECTIVE)
        r.l07r_start_paths()
        for i in range(30):
            self.clock.t = T0 + 15 * i
            r.position_pass()
            r.candidate_pass()
            r.paths.poll()
        db = r.paths.db.path
        rows = sqlite3.connect(db).execute("SELECT ts, net_sol FROM path_marks WHERE mint=? AND status='OK' ORDER BY ts",
                                           (token.mint,)).fetchall()
        self.assertGreater(len(rows), 10)
        target = json.loads(sqlite3.connect(db).execute('SELECT target FROM paths WHERE mint=?', (token.mint,)).fetchone()[0])
        costs = R.costs_from({'fee_sol': str(A.sol(r.pcfg.fee_lamports))}, R.load_fee_tiers())
        prep = R.prepare(R.load_paths(db)[token.mint], costs, SIZE)
        self.assertTrue(prep.points and all(p is not None for p in prep.points))
        self.assertEqual(set(prep.bps), {120})
        recorded = {ts: Decimal(net) for ts, net in rows}
        for i, ts in enumerate(prep.ts):
            mine = prep.value(i, target['qty_raw'], costs.pcfg)
            self.assertLessEqual(abs(mine - recorded[ts]), Decimal('0.000000002'), (i, mine, recorded[ts]))


class C1ProposalNeedsItsOwnTestBlock(unittest.TestCase):
    def test_pooled_ci_above_zero_but_the_proposed_policys_block_below_is_refused(self):
        pol = R.expand_grid([{'family': 'hold', 'minutes': 2}])[0]
        wf = {'selections': [pol.name, pol.name], 'pooled': {'n': 100, 'ci_lo': 0.01},
              'folds': [{'selected': pol.name, 'test_ci_lo': 0.05}, {'selected': pol.name, 'test_ci_lo': -0.02}]}
        proposal, why = T.decide_proposal(wf, {'min_oos_trades': 30}, {}, [pol])
        self.assertIsNone(proposal)
        self.assertIn('TEST_BLOCK', why)
        wf['folds'][-1]['test_ci_lo'] = 0.001
        proposal, why = T.decide_proposal(wf, {'min_oos_trades': 30}, {}, [pol])
        self.assertEqual(proposal['policy'], pol.name)



# ================================================================================================================ C3
from tests.lean.test_census import Env as _CensusEnv, LAUNCHLAB as _LAUNCHLAB  # noqa: E402


class C3TransactionVersion(_CensusEnv):
    def test_census_samples_ask_for_version_1_transactions(self):
        from lean import providers
        seen = []
        self.chain.add(_LAUNCHLAB, T0 - 100, 'create')
        c = self.census()
        c.poll_once()
        self.chain.fail['getTransaction'] = lambda params: seen.append(params[1]) or None
        self.now[0] += 601
        self.chain.add(_LAUNCHLAB, self.now[0], 'create')
        self.now[0] += 1
        c.poll_once()
        self.assertTrue(seen)
        self.assertEqual({o['maxSupportedTransactionVersion'] for o in seen}, {providers.MAX_TX_VERSION})
        self.assertGreaterEqual(providers.MAX_TX_VERSION, 1)


# ======================================================================================================== config hash
class RoundCConfigHash(unittest.TestCase):
    """Merging C2/C3/C4 must not change lean_config_hash of a production config that does not have their keys."""

    def configs(self):
        from lean.__main__ import load_config
        return {name: load_config(ROOT / 'tests' / 'lean' / name) for name in ('lean.r1-prod.json', 'lean.r1-live.json', 'lean.baseline.json')}

    def test_absent_blocks_leave_the_hash_as_it_was(self):
        from lean.__main__ import NEUTRAL_WHEN_OFF, lean_config_hash
        for name, cfg in self.configs().items():
            before = {k: v for k, v in cfg.items() if k not in NEUTRAL_WHEN_OFF}         # the r1 config shape (no Round C keys)
            self.assertEqual(lean_config_hash(cfg), lean_config_hash(before), name)

    def test_turning_one_on_is_a_new_run(self):
        from lean.__main__ import lean_config_hash
        cfg = self.configs()['lean.r1-prod.json']
        base = lean_config_hash(cfg)
        on = dict(cfg, census=dict(cfg['census'], enabled=True))
        self.assertNotEqual(lean_config_hash(on), base)
        self.assertEqual(lean_config_hash(dict(cfg, census=None)), base)



# ================================================================================================================ C4
import copy as _copy  # noqa: E402

from desk.model import canonical as _canonical, digest as _digest  # noqa: E402
from lean import backfill as BF, candidates as CAND  # noqa: E402
from tests.lean.test_backfill import signature_of, tx_for  # noqa: E402
from tests.lean.test_e2e_backfill import BackfillEndToEnd  # noqa: E402
from tests.lean.fakeworld import Token  # noqa: E402

AGGREGATOR = 'JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
TB = 1_800_000_000.0


def cpi_tx(token, *, slot, block_time, after=None):
    """``token``'s migration as a CPI: another program's top-level instruction calls pump ``migrate`` (stack height 2), which
    emits its CompletePumpAmmMigrationEvent one frame deeper (3). ``after``: instructions appended to the inner group."""
    raw = tx_for(token, slot=slot, block_time=block_time)
    migrate = raw['transaction']['message']['instructions'][0]
    inner = raw['meta']['innerInstructions'][0]['instructions']
    raw['transaction']['message']['instructions'] = [{'programId': AGGREGATOR, 'accounts': [], 'data': '3Bxs4h24hBtQy9rw'}]
    raw['meta']['innerInstructions'] = [{'index': 0, 'instructions': [dict(migrate, stackHeight=2)]
                                         + [dict(ix, stackHeight=ix['stackHeight'] + 1) for ix in inner] + list(after or ())}]
    return raw


def wire(tx, sig):
    w = {'method': 'transactionNotification', 'params': {'result': {'signature': sig, 'slot': tx['slot'], 'blockTime': tx['blockTime'],
                                                                     'transaction': {'transaction': tx['transaction'], 'meta': tx['meta']}}}}
    return _canonical(w), _digest(w)


class C4CpiMigrations(unittest.TestCase):
    def setUp(self):
        self.token = Token(61)
        self.sig = signature_of(self.token)

    def test_a_cpi_migration_is_a_candidate_here_and_rejected_by_the_intake(self):
        tx = cpi_tx(self.token, slot=100, block_time=TB - 1000)
        cand = BF.migration_candidate(tx, self.sig, now=TB, max_age=7200)
        self.assertIsNotNone(cand)
        self.assertEqual((cand.mint, cand.pool, cand.signature), (self.token.mint, self.token.pool, self.sig))
        payload, h = wire(tx, self.sig)
        stats = BF._stats()
        self.assertIsNone(CAND._frame_candidate(1, TB - 1000, 100, h, payload, stats))          # the live intake gap
        self.assertEqual(stats['not_migration'], 1)
        self.assertIsNotNone(BF.frame_candidate_any_depth(1, TB - 1000, 100, h, payload, BF._stats()))

    def test_the_top_level_rule_is_unchanged(self):
        tx = tx_for(self.token, slot=100, block_time=TB - 1000)
        payload, h = wire(tx, self.sig)
        a = CAND._frame_candidate(1, TB - 1000, 100, h, payload, BF._stats())
        b = BF.frame_candidate_any_depth(1, TB - 1000, 100, h, payload, BF._stats())
        self.assertEqual(a, b)

    def test_an_event_outside_the_migrate_frame_or_at_the_wrong_depth_is_refused(self):
        tx = cpi_tx(self.token, slot=100, block_time=TB - 1000)
        group = tx['meta']['innerInstructions'][0]['instructions']
        wrong_depth = _copy.deepcopy(tx)
        wrong_depth['meta']['innerInstructions'][0]['instructions'][1]['stackHeight'] = 4
        returned = _copy.deepcopy(tx)                        # the migrate returned (a sibling at height 2) before the event
        event = returned['meta']['innerInstructions'][0]['instructions'].pop(1)
        returned['meta']['innerInstructions'][0]['instructions'] += [{'programId': AGGREGATOR, 'accounts': [], 'data': '3Bxs4h24hBtQy9rw',
                                                                      'stackHeight': 2}, event]
        twice = _copy.deepcopy(tx)
        twice['meta']['innerInstructions'][0]['instructions'].append(_copy.deepcopy(group[1]))
        for bad in (wrong_depth, returned, twice):
            self.assertIsNone(BF.migration_candidate(bad, self.sig, now=TB, max_age=7200))

    def test_get_transaction_asks_for_version_1(self):
        from lean import providers
        calls = []

        class Helius:
            def rpc(self, method, params):
                calls.append((method, params))
                if method == 'getSignaturesForAddress':
                    return [{'signature': 'S' * 88, 'slot': 5, 'blockTime': int(TB - 1000), 'err': None}], b'', {}
                return None, b'', {}
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'd.sqlite'
            from discovery import continuous as discovery
            with mock.patch.object(discovery.time, 'time', return_value=TB - 100_000):
                discovery.initialize(db)
            src = BF.BackfillSource(Helius(), db, tmp, cfg={}, screen_cfg={'min_age_seconds': 60, 'max_age_seconds': 7200}, clock=lambda: TB)
            src.iter_new(0)
        options = [p[1] for m, p in calls if m == 'getTransaction']
        self.assertEqual([o['maxSupportedTransactionVersion'] for o in options], [providers.MAX_TX_VERSION])


class C4CpiEndToEnd(BackfillEndToEnd):
    """The C4 e2e world with f2 stored by the feed AS A CPI migration (the intake rejects it) and m1 missed by the feed AS A CPI
    migration (fetched). Both are entered from the backfill; f2 costs no getTransaction."""
    for _name in [n for n in dir(BackfillEndToEnd) if n.startswith('test')]:
        locals()[_name] = None
    del _name

    def store_feed(self, names):
        for name in ('f2', 'm1'):                       # before the feed stores f2: these two migrations ran as a CPI
            token = self.tokens[name]
            sig = signature_of(token)
            old = self.txs[sig]
            self.txs[sig] = cpi_tx(token, slot=old['slot'], block_time=old['blockTime'])
        super().store_feed(names)

    def test_cpi_migrations_are_recovered_from_the_feed_and_from_the_chain(self):
        r = self.runner()
        self.run_until(r, self._end())
        meta, screens, entered = self.facts(r)
        self.assertEqual(meta['f2']['source'], 'backfill')                               # the intake rejected it; the backfill fed it
        self.assertEqual(meta['m1']['source'], 'backfill')
        self.assertEqual(meta['f1']['source'], 'pump_graduation')
        self.assertEqual(entered, {'f1', 'f2', 'm1', 'm2'})
        fetched = [c for c in self.world.calls if c[0] == 'helius' and c[1] == 'getTransaction']
        self.assertEqual(len(fetched), 5)                                                 # old, PLAIN, m1, m2, hz: never f1 / f2
        today = json.loads((self.state / 'health.json').read_text())['sources']['backfill']['today']
        self.assertEqual((today['seen_by_feed'], today['intake_rejected_migrations'], today['missed_migrations']), (2, 1, 4))
        self.assertEqual(r.store.counts()['candidates'], 6)

    def test_without_the_backfill_the_cpi_migration_the_feed_stored_is_lost_and_main_fills_are_identical(self):
        on = self.runner()
        self.run_until(on, self._end())
        fills_on = [(f['mint'], f['side'], f['qty_raw'], f['sol_lamports']) for f in on.store.rows('fills', limit=1000)
                    if self.role[f['mint']] == 'f1']
        on.store.close()
        cfg = dict(self.cfg)
        cfg['backfill'] = None
        self.state = self.root / 'state-off'
        self.clock.t = __import__('tests.lean.fakeworld', fromlist=['T0']).T0
        self.world.calls.clear()
        off = self.runner(cfg)
        self.run_until(off, self._end())
        meta, screens, entered = self.facts(off)
        self.assertEqual(set(meta), {'f1'})                                               # f2 (CPI) never reaches the trader
        fills_off = [(f['mint'], f['side'], f['qty_raw'], f['sol_lamports']) for f in off.store.rows('fills', limit=1000)
                     if self.role[f['mint']] == 'f1']
        self.assertTrue(fills_on)
        self.assertEqual(fills_on, fills_off)

    def _end(self):
        from tests.lean.test_e2e_backfill import END
        return END



# ================================================================================================================ C2
import hashlib as _hashlib  # noqa: E402

from lean import curves as CV  # noqa: E402
from lean.providers import LANE_SHED as _SHED, ProviderError as _PE  # noqa: E402
from tests.lean.test_curves import FakeRPC, curve_bytes, entry as curve_entry, key as curve_key, v2  # noqa: E402


class C2LayoutProvenance(unittest.TestCase):
    def test_discriminator_is_the_anchor_hash_and_the_schema_file_is_the_pinned_one(self):
        self.assertEqual(CV.DISCRIMINATOR, _hashlib.sha256(b'account:BondingCurve').digest()[:8])
        manifest = json.loads((ROOT / 'desk' / 'schemas' / 'manifest.json').read_text())
        pinned = manifest['files']['pump']['sha256']
        self.assertEqual(_hashlib.sha256((ROOT / 'desk' / 'schemas' / 'pump.json').read_bytes()).hexdigest(), pinned)
        for older in ('pump_previous1.json', 'pump_previous2.json'):          # the 115-byte layouts share every offset they have
            schema = json.loads((ROOT / 'desk' / 'schemas' / older).read_text())
            fields = next(t for t in schema['types'] if t['name'] == 'BondingCurve')['type']['fields']
            offset = 8
            for field in fields:
                self.assertEqual(CV.OFFSETS[field['name']], offset, (older, field['name']))
                offset += {'u64': 8, 'bool': 1, 'pubkey': 32}[field['type']]


class C2PollingMethod(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = [1_800_000_000.0]
        self.rpc = FakeRPC()

    def recorder(self, **raw):
        db = CV.CurveStore(Path(self.tmp.name) / ('c%d.sqlite' % len(raw)), clock=lambda: self.t[0])
        self.addCleanup(db.close)
        return CV.CurveRecorder(db=db, helius=self.rpc, cfg=CV.config({'enabled': True, 'min_free_gb': 0, **raw}), code_version='t',
                                clock=lambda: self.t[0])

    def test_the_method_is_configurable_and_v2_is_the_default(self):
        self.assertEqual(CV.config({'enabled': True})['method'], 'getProgramAccountsV2')
        v1 = self.recorder(method='getProgramAccounts')
        self.rpc.script.append({'context': {'slot': 5}, 'value': []})
        v1.poll()
        self.assertEqual(self.rpc.calls[-1][0], 'getProgramAccounts')
        self.assertNotIn('limit', self.rpc.calls[-1][1][1])
        new = self.recorder()
        self.rpc.script.append(v2([], slot=7))
        new.poll()
        self.assertEqual(self.rpc.calls[-1][0], 'getProgramAccountsV2')
        self.assertEqual(self.rpc.calls[-1][1][1]['limit'], 1000)

    def test_a_listing_without_a_slot_makes_the_next_poll_read_get_slot_first(self):
        r = self.recorder()
        self.rpc.script.append({'accounts': [curve_entry(curve_key(1), curve_bytes())]})
        r.poll()
        self.assertTrue(r.slotless)
        self.assertIsNone(r.cursor)
        self.t[0] += 60
        self.rpc.script += [900, {'accounts': []}]
        r.poll()
        self.assertEqual([c[0] for c in self.rpc.calls[-2:]], ['getSlot', 'getProgramAccountsV2'])
        self.assertEqual(r.cursor, 900)
        self.t[0] += 60
        self.rpc.script += [950, {'accounts': []}]
        r.poll()
        self.assertEqual(self.rpc.calls[-1][1][1]['changedSinceSlot'], 900)
        self.assertEqual(r.cursor, 950)
        self.t[0] += 60
        self.rpc.script += [_PE(_SHED, True)]                                       # a shed getSlot: skip, no listing, no credit
        before = len(self.rpc.calls)
        r.poll()
        self.assertEqual(len(self.rpc.calls), before + 1)
        self.assertEqual(r.cursor, 950)

    def test_the_default_budget_holds_over_simulated_hours(self):
        r = self.recorder()
        self.rpc.default = lambda method, params: v2([], slot=int(self.t[0]))
        spent = 0.0
        for _ in range(3 * 3600 // 10):                                             # a 10 s poll loop for 3 hours: the budget binds
            self.t[0] += 10
            r.poll()
        calls, credits = r.db.poll_usage(0)
        self.assertLessEqual(credits, 3 * 450_000 / 720 + 1e-6)
        self.assertLess(CV.estimate(CV.config(None))['credits_per_month'], 500_000)
        self.assertGreater(calls, 100)


if __name__ == '__main__':
    unittest.main()
