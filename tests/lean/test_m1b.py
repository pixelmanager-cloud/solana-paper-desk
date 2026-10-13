"""M1b: the pre-deploy review fixes of M1. Each test failed on the merged M1 (26e9c52) before its fix.

  1 the exit back-off never delays a STOP / DANGER / LIQUIDATE / MAX_HOLD / TIME_STOP / BOT_EXIT exit (only a stuck / zero-mark
    exit backs off), and a change of exit reason resets it
  2 the shared MarkModel tolerates a schedule stamped (float) by another thread a little after the runner's (int) mark time
  3 a stale schedule is flagged on the marks; an impossible supply never picks a fee tier
  4 risk_external says ``kept: false`` when raw_policy errors_only dropped the body
  5 vault-model path rows are labelled with their OWN fee; a path that mixes models is never labelled; the learner defaults to
    the latest run's lean.json hash
  6 an open position keeps the mark model (and vault fee) it was opened with across a config switch, both directions
"""
import contextlib
import io
import json
import socket
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from lean import adapters as A, labels as LB, learn, marks as M, paper, paths as P, risk_external as R
from lean.store import Store
from tests.lean import test_risk_external as TR
from tests.lean.fakeworld import T0, Token
from tests.lean.test_e2e_real import ROOT, steps
from tests.lean.test_m1 import EFFECTIVE, World1, boosted, model

FIX = ROOT / 'fixtures'


def setUpModule():
    patcher = mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network used'))
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


# ======================================================================================================== 1 exit back-off
class ExitBackoff(World1):
    def test_review_repro_a_stop_after_a_failed_take_profit_fills_when_the_route_returns(self):
        """Up to 1.6 at +60 s (TAKE_PROFIT wanted), no route from +60 s to +200 s, then down to 0.5 with the route back: the STOP
        fills at +200 s (M1: +250 s, the take-profit's back-off)."""
        token = Token(61, path=steps((60, 1.6), (200, 0.5)), no_route_from=60, route_back_at=200)
        r = self.build(token)
        self.run_until(r, 0)
        self.run_until(r, 400, candidates=False)
        (closed,) = self.closed(r, token.mint)
        self.assertEqual(closed['state']['reason'], 'STOP')
        sell = r.store.rows('fills', mint=token.mint, limit=10)[-1]
        self.assertEqual(sell['ts'], T0 + 200)

    def test_a_hold_resets_the_back_off_of_the_same_reason(self):
        """A take-profit without a route backs off; the price falls back (HOLD); the take-profit is wanted again with the route
        back: it is quoted at once, not after the earlier back-off."""
        token = Token(66, path=steps((60, 1.6), (255, 1.0), (265, 1.6)), no_route_from=50, route_back_at=265)
        r = self.build(token)
        self.run_until(r, 0)
        self.run_until(r, 400, candidates=False)
        sell = [f for f in r.store.rows('fills', mint=token.mint, limit=10) if f['side'] == 'sell'][0]
        self.assertEqual(sell['ts'], T0 + 270)

    def test_a_stop_without_a_route_is_quoted_every_pass(self):
        token = Token(62, path=steps((60, 0.5)), no_route_from=50, route_back_at=300)
        r = self.build(token)
        self.run_until(r, 0)
        self.run_until(r, 400, candidates=False)
        sell = r.store.rows('fills', mint=token.mint, limit=10)[-1]
        self.assertEqual(sell['ts'], T0 + 300)
        self.assertEqual(r.m1_counts['exit_backoff_skips'], 0)

    def test_a_stop_whose_quote_is_worthless_is_backed_off_although_the_mark_is_not_zero(self):
        """The pool still marks ~0.5, but every route now eats the output (STUCK_POSITION): that STOP may wait."""
        token = Token(65, path=steps((60, 0.5)))
        r = self.build(token)
        self.run_until(r, 0)
        token.route_fee_bps = 9974                              # after the entry: the quote keeps 0.01 % of the pool output
        self.run_until(r, 600, candidates=False)
        self.assertGreater(r.marks[token.mint][0], 0)
        quotes = r.store.rows('observations', mint=token.mint, kind='quote:exit', limit=1000)
        self.assertLessEqual(len(quotes), 16)                   # 10, 20, 40, 60, 60 ... instead of one per 10 s pass (55)
        self.assertGreater(r.m1_counts['exit_backoff_skips'], 0)

    def test_priority_exits_are_never_backed_off_but_stuck_and_zero_mark_ones_are(self):
        r = self.build(Token(63))
        r._exit_attempt_failed('m', 1, 'TAKE_PROFIT', stuck=False)
        self.assertTrue(r._backoff_active('m', 1, 'TAKE_PROFIT', Decimal('1.5')))
        for reason in ('STOP', 'DANGER', 'LIQUIDATE', 'MAX_HOLD', 'TIME_STOP', 'BOT_EXIT'):
            r._exit_backoff.clear()
            r._exit_attempt_failed('m', 1, reason, stuck=False)
            self.assertFalse(r._backoff_active('m', 1, reason, Decimal('0.5')), reason)
            self.assertTrue(r._backoff_active('m', 1, reason, Decimal('0')), reason)          # a zero mark
            r._exit_attempt_failed('m', 1, reason, stuck=True)
            self.assertTrue(r._backoff_active('m', 1, reason, Decimal('0.5')), reason)        # the quote said worthless

    def test_a_new_exit_reason_resets_the_back_off(self):
        r = self.build(Token(64))
        for _ in range(4):
            r._exit_attempt_failed('m', 1, 'TAKE_PROFIT', stuck=False)
        self.assertEqual(r._exit_backoff['m'][1], 4)
        self.assertFalse(r._backoff_active('m', 1, 'TRAILING_STOP', Decimal('1.2')))
        self.assertNotIn('m', r._exit_backoff)
        r._exit_attempt_failed('m', 1, 'TRAILING_STOP', stuck=False)
        self.assertEqual(r._exit_backoff['m'][1], 1)                                       # starts again at 10 s


# ======================================================================================================== 2 / 3 MarkModel
class Schedule(unittest.TestCase):
    def test_a_schedule_stamped_just_after_the_mark_time_is_fresh(self):
        m = model(at=100.7)                                     # the paths thread, float seconds
        self.assertIsNotNone(m.schedule(100))                   # the runner, int mark time of the same second
        _, info = m.value(10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9, pool_info={'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 15},
                          now=100, fee_lamports=0)
        self.assertEqual(info['fee_source'], 'dynamic')
        self.assertIsNone(m.schedule(100.7 - 61))               # but not an arbitrary future read

    def test_a_schedule_kept_after_failed_reads_is_flagged_stale(self):
        m = model(at=0.0)
        args = (10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9)
        info_in = {'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 15}
        self.assertFalse(m.value(*args, pool_info=info_in, now=5, fee_lamports=0)[1]['schedule_stale'])
        m.observe_call([None], 10.0)                            # the FeeConfig vanished
        _, info = m.value(*args, pool_info=info_in, now=20, fee_lamports=0)
        self.assertEqual((info['fee_source'], info['schedule_stale']), ('dynamic', True))
        m.observe_call([{'owner': 'x', 'data': ['AAAA', 'base64'], 'executable': False}], 30.0)       # undecodable
        self.assertTrue(m.value(*args, pool_info=info_in, now=40, fee_lamports=0)[1]['schedule_stale'])
        m.observe_call([json.loads((FIX / 'mainnet-dynamic-fees.json').read_text())['response']['value']], 50.0)
        self.assertFalse(m.value(*args, pool_info=info_in, now=60, fee_lamports=0)[1]['schedule_stale'])
        self.assertGreater(m.health()['schedule_stale_marks'], 0)
        # a first good read after failed ones (the decode path, not the unchanged-bytes one) clears it too
        m = model(with_schedule=False)
        m.observe_call([None], 0.0)
        m.observe_call([json.loads((FIX / 'mainnet-dynamic-fees.json').read_text())['response']['value']], 1.0)
        _, info = m.value(*args, pool_info=info_in, now=2, fee_lamports=0)
        self.assertEqual((info['fee_source'], info['schedule_stale']), ('dynamic', False))

    def test_an_impossible_supply_never_picks_a_tier(self):
        m = model()
        args = (10 ** 9, 2 * 10 ** 14, 80 * 10 ** 9)
        for info_in in ({'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 14},                    # below the pool's base
                        {'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 30, 'decimals': 6},      # 1e24 tokens
                        {'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 15, 'decimals': 12}):    # 1e3 tokens
            _, info = m.value(*args, pool_info=info_in, now=1, fee_lamports=0)
            self.assertEqual((info['fee_source'], info['fee_fallback'], info.get('supply_invalid')), ('config', True, True), info_in)
        _, info = m.value(*args, pool_info={'virtual_quote_reserves_raw': 18 * 10 ** 9, 'supply_raw': 10 ** 15, 'decimals': 6},
                          now=1, fee_lamports=0)
        self.assertEqual(info['fee_source'], 'dynamic')
        self.assertEqual(M.screen_pool_info({'quote_gross_raw': '5', 'quote_spendable_raw': '4', 'supply_raw': '9', 'decimals': 6})['decimals'], 6)


# ======================================================================================================== 4 risk_external
class RiskExternalRaw(unittest.TestCase):
    def test_kept_is_false_when_errors_only_dropped_the_body(self):
        for policy, kept in (('all', True), ('errors_only', False)):
            with self.subTest(policy):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                clock, fake = TR.FakeClock(), TR.FakeRugCheck()
                store = Store(Path(tmp.name) / 'lean.sqlite', initial_cash_sol='10', code_version='t', strategy_version='s',
                              clock=clock.time, raw_policy=policy)
                self.addCleanup(store.close)
                cfg = R.config({'enabled': True})
                rec = R.RiskExternal(store=store, client=TR.client(fake, clock, rate_per_s=cfg['rate_per_s']), cfg=cfg,
                                     code_version='t', strategy_version='s', clock=clock.time, monotonic=clock.monotonic)
                m = TR.mint(1)
                fake.reports[m] = TR.report(m)
                cid = store.add_candidate(m, pool=TR.POOL, ts=clock.time())
                store.add_decision('screen', 'PASS', mint=m, candidate_id=cid, ts=clock.time())
                rec.submit(TR.Candidate(seq=1, mint=m, pool=TR.POOL, signature='s', slot=1, migrated_at=TR.T0, payload_hash='h'),
                           cid, TR.PASS)
                while rec.process_one():
                    pass
                row = json.loads(store.rows('observations', kind=R.KIND, limit=10)[0]['raw'])
                self.assertEqual(row['raw']['kept'], kept)
                self.assertEqual(rec.stats['raw_kept'], int(kept))
                (body,) = store.rows('observations', kind=R.RAW_KIND, limit=10)
                self.assertEqual(body['full_bytes'], row['raw']['bytes'])                   # the sha / size are always there


# ======================================================================================================== 5 labels / learner
def paths_file(tmp, rows, *, columns=True):
    path = Path(tmp) / 'paths.sqlite'
    store = P.PathStore(path)
    target = {'mint': 'M', 'pool': 'p', 'base_vault': 'b', 'quote_vault': 'q', 'decimals': 6, 'fee_raw': 0, 'qty_raw': 10 ** 12,
              'cost_lamports': 2 * 10 ** 8, 'basis': 'fill'}
    store.insert_path({'mint': 'M', 'candidate_id': 1, 'start_ts': T0, 'ends_ts': T0 + 3600, 'entered': 1, 'stage': None, 'reason': '[]',
                       'screen_reasons': '[]', 'holders_checked': 1, 'features': json.dumps({'quote_spendable_raw': str(80 * 10 ** 9)}),
                       'target': json.dumps(target), 'carried_from': None, 'code_version': 'c', 'strategy_version': 's'})
    store.add(marks=rows)
    store.close()
    return sqlite3.connect(path)


class Labels(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.pcfg = paper.PaperConfig(fee_lamports=250_000, slippage_bps=50)

    def test_vault_rows_are_labelled_with_their_own_fee(self):
        base, quote = 2 * 10 ** 14, 80 * 10 ** 9
        db = paths_file(self.tmp, [('M', T0 + 15, 1, base, quote, '0', '0', 'OK', 'vault', None, None, 25, 'config'),
                                   ('M', T0 + 30, 1, base, quote, '0', '0', 'OK')])           # a pre-M1 row: NULL model and fee
        (path,), _ = LB.load_paths([db])
        expected = int(A.mark(10 ** 12, base, quote, pool_fee_bps=25, pcfg=self.pcfg) * paper.LAMPORTS)
        for point in path.points:                               # lean.json now says 120: the rows were marked at 25
            self.assertEqual(LB.mark_lamports(point, 10 ** 12, 120, self.pcfg), expected)
            self.assertEqual(LB.sell_quote_lamports(point, 10 ** 12, 120),
                             quote * 10 ** 12 // (base + 10 ** 12) * (10000 - 25) // 10000)

    def test_a_path_that_mixes_models_is_never_labelled(self):
        base, quote = 2 * 10 ** 14, 80 * 10 ** 9
        db = paths_file(self.tmp, [('M', T0 + 15, 1, base, quote, '0', '0', 'OK', 'vault', None, None, 25, 'config'),
                                   ('M', T0 + 30, 1, base, quote, '0', '0', 'OK', 'effective', 18 * 10 ** 9, 0, 120, 'dynamic')])
        (path,), _ = LB.load_paths([db])
        label = LB.label_path(path, T0, LB.reference(path, self.pcfg), LB.LabelConfig(), pool_fee_bps=120, pcfg=self.pcfg)
        self.assertEqual((label.usable, label.why_insufficient), (False, 'MIXED_MARK_MODELS'))


class LearnerDefault(World1):
    def test_the_learner_defaults_to_the_latest_run_unless_all_configs(self):
        a, b = boosted(71), boosted(72)
        r = self.build(a, b, ready=[T0, T0 + 4000])
        r.l07r_start_paths()
        self.run_until(r, 3900, tick=60)
        r.paths.poll()
        r.store.close()
        self.cfg['stale_mark_s'] = 31
        r = self.runner()
        r.l07r_start_paths()
        self.run_until(r, 8000, tick=60)
        r.paths.poll()
        latest = json.loads(r.store.rows('events', kind='start')[-1]['payload'])['lean_config_hash']
        self.assertEqual(learn.latest_config_hash(self.state / 'lean.sqlite'), latest)
        out = Path(self.root) / 'out'
        out.mkdir()
        args = ['--lean-db', str(self.state / 'lean.sqlite'), '--paths-db', str(self.state / 'paths.sqlite'), '--out-dir', str(out),
                '--now', str(T0 + 9000)]
        with mock.patch.object(learn, 'build_dataset', wraps=learn.build_dataset) as spy, contextlib.redirect_stdout(io.StringIO()):
            learn.main(args)
            self.assertEqual(spy.call_args.kwargs['config_hash'], latest)
            learn.main(args + ['--all-configs', '--now', str(T0 + 9001)])
            self.assertIsNone(spy.call_args.kwargs['config_hash'])
            learn.main(args + ['--config-hash', latest[:10], '--now', str(T0 + 9002)])
            self.assertEqual(spy.call_args.kwargs['config_hash'], latest[:10])


# ======================================================================================================== 6 pinned model
class PinnedModel(World1):
    def ratio(self, r, mint):
        held = A.strategy_position(r.store.positions()[mint], r.store.position_states()[mint]['state'])
        return r.marks[mint][0] / held.cost_left

    def test_effective_positions_stay_effective_after_a_rollback_to_vault(self):
        token = boosted(81)
        r = self.build(token, cfg_over=EFFECTIVE)
        self.run_until(r, 0)
        pool_info = r.store.position_states()[token.mint]['state']['pool_info']
        self.assertEqual((pool_info['model'], pool_info['pool_fee_bps']), ('effective', 120))
        r.store.close()
        self.cfg.update(marks=M.config(None), pool_fee_bps=25)                  # the rollback
        r = self.runner()
        self.assertEqual(r.mark_model.model, 'vault')
        self.clock.t = T0 + 10
        r.position_pass()
        self.assertEqual(self.closed(r, token.mint), [])                       # a vault mark would read ~0.80: STOP at once
        self.assertTrue(Decimal('0.95') < self.ratio(r, token.mint) < 1)
        info = r.mark_info[token.mint]
        self.assertEqual((info['model'], info['pinned_from_open'], info['fee_source']), ('effective', True, 'dynamic'))
        # a position opened after the rollback is vault
        meta = json.loads(r.store.rows('observations', kind='marks', limit=100)[-1]['meta'])
        self.assertEqual(meta['layouts'][token.mint], 3)

    def test_vault_positions_stay_vault_at_their_fee_after_a_switch_to_effective(self):
        token = boosted(82)
        r = self.build(token)
        self.run_until(r, 0)
        pool_info = r.store.position_states()[token.mint]['state']['pool_info']
        self.assertEqual((pool_info['model'], pool_info['pool_fee_bps']), ('vault', 25))
        r.store.close()
        self.cfg.update(EFFECTIVE)
        r = self.runner()
        self.clock.t = T0 + 5
        r.position_pass()
        # it is marked exactly as it would have been without the switch (the vault mark at 25 bps, which stops a boosted pool)
        (closed,) = self.closed(r, token.mint)
        self.assertEqual(closed['state']['reason'], 'STOP')
        features = json.loads(r.store.rows('decisions', mint=token.mint, kind='exit', limit=10)[-1]['features'])
        info = features['mark_info']
        self.assertEqual((info['model'], info['fee_bps'], info['pinned_from_open']), ('vault', 25, True))
        self.assertEqual(Decimal(features['mark_sol']),
                         A.mark(r.store.rows('fills', mint=token.mint, limit=10)[0]['qty_raw'], info['base_raw'], info['quote_raw'],
                                pool_fee_bps=25, pcfg=r.pcfg))

    def test_a_pre_m1_position_without_a_pin_is_vault(self):
        m = M.MarkModel({'model': 'effective'}, pool_fee_bps=120)
        self.assertEqual(m.pinned(None), ('vault', M.LEGACY_VAULT_FEE_BPS))
        self.assertEqual(M.MarkModel(None, pool_fee_bps=30).pinned(None), ('vault', 30))
        self.assertEqual(m.pinned({'virtual_quote_reserves_raw': 1, 'supply_raw': 2}), ('effective', 120))    # an M1 effective open


if __name__ == '__main__':
    unittest.main()
