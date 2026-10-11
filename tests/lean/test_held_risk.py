"""SYNTHETIC_TEST_ONLY: lean.held_risk units (L11): configuration, the pure checks, the exact trigger boundaries, persistence and
replay of the risk state, and the isolation of a failing check. The flows (exits, unsellable, write-off, restart) are in
tests/lean/test_e2e_held_risk.py with the real modules."""
import base64
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from lean import held_risk as H, paper
from lean.providers import ProviderError
from lean.store import Store

MINT = 'MintMintMintMintMintMintMintMintMintMint11'


def mint_account(*, freeze=False, short=False):
    data = bytearray(81 if short else 82)
    data[44] = 6
    if freeze and not short:
        data[46:50] = (1).to_bytes(4, 'little')
    return {'data': [base64.b64encode(bytes(data)).decode(), 'base64'], 'owner': 'Tokenkeg', 'lamports': 1}


class ConfigTests(unittest.TestCase):
    def test_defaults_are_the_documented_ones(self):
        cfg = H.Config.from_dict({})
        self.assertEqual((cfg.rug_liq_drop_frac, cfg.unsellable_after_s, cfg.unsellable_writeoff_s, cfg.route_probe_s,
                          cfg.freeze_check_every, cfg.freeze_forces_exit, cfg.enabled),
                         (Decimal('0.7'), 120, 21600, 60, 6, False, True))
        self.assertEqual(H.Config.from_dict(cfg.as_dict()).as_dict(), cfg.as_dict())
        self.assertEqual(H.Config.from_dict(None).as_dict(), cfg.as_dict())

    def test_everything_invalid_is_refused(self):
        for bad in ({'nope': 1}, {'rug_liq_drop_frac': '0'}, {'rug_liq_drop_frac': '1'}, {'rug_liq_drop_frac': 'x'},
                    {'rug_liq_drop_frac': '1.5'}, {'unsellable_after_s': 0}, {'unsellable_after_s': 1.5}, {'unsellable_after_s': True},
                    {'unsellable_writeoff_s': 60, 'unsellable_after_s': 120}, {'route_probe_s': -1}, {'freeze_check_every': 0},
                    {'freeze_forces_exit': 1}, {'enabled': 'yes'}):
            with self.subTest(bad=bad), self.assertRaises(H.HeldRiskConfigError):
                H.Config.from_dict(bad)
        with self.assertRaises(H.HeldRiskConfigError):
            H.Config.from_dict([])

    def test_disabled_builds_nothing(self):
        self.assertIsNone(H.build({'enabled': False}, None, code_version='c', strategy_version='s', clock=lambda: 0))


class PureChecks(unittest.TestCase):
    def test_freeze_authority_is_read_from_the_coption_tag(self):
        self.assertTrue(H.freeze_authority_set(mint_account(freeze=True)))
        self.assertFalse(H.freeze_authority_set(mint_account()))
        for evidence in (None, {}, {'data': 'x'}, {'data': ['!!!', 'base64']}, mint_account(short=True), [], 7):
            self.assertFalse(H.freeze_authority_set(evidence), evidence)           # missing evidence is not evidence of a freeze

    def test_net_value_is_the_paper_sell_without_ever_going_negative(self):
        pcfg = paper.PaperConfig(fee_lamports=50_000, slippage_bps=50)
        self.assertEqual(H._net_lamports(1_000_000_000, pcfg), 1_000_000_000 * 9950 // 10000 - 50_000)
        self.assertEqual(H._net_lamports(50_000, pcfg), 0)
        self.assertEqual(H._net_lamports(1, pcfg), 0)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'lean.sqlite', initial_cash_sol='10', code_version='c', strategy_version='s')
        self.addCleanup(self.store.close)
        self.now = 1_000.0
        self.risk = self.make()

    def make(self, **cfg):
        return H.HeldRisk(self.store, H.Config.from_dict(cfg), code_version='c', strategy_version='s', clock=lambda: self.now)

    def events(self, ev=None):
        rows = [json.loads(r['payload']) for r in self.store.rows('events', kind='held_risk', limit=1000)]
        return [p for p in rows if ev is None or p['ev'] == ev]


class TriggerBoundaries(Harness):
    ROW = {'open_fill_id': 7}

    def detect(self, baseline, now_reserve, **cfg):
        risk = self.make(**cfg)
        life = risk.life(MINT, 7)
        life.baseline_quote = baseline
        risk.observe(MINT, 1, now_reserve, self.now)
        risk._detect_reserves(MINT, self.ROW, life)
        return life.trigger

    def test_liquidity_must_fall_by_MORE_than_the_fraction(self):
        self.assertIsNone(self.detect(1000, 300))                                    # exactly 70 % down: not more than 70 %
        self.assertIsNone(self.detect(1000, 301))
        self.assertEqual(self.detect(1000, 299)[0], 'LIQUIDITY_DROP')
        self.assertEqual(self.detect(1000, 0)[0], 'LIQUIDITY_DROP')
        self.assertIsNone(self.detect(1000, 1000))
        self.assertIsNone(self.detect(1000, 5000))                                   # liquidity going UP is not a rug

    def test_the_fraction_is_configurable(self):
        self.assertEqual(self.detect(1000, 499, rug_liq_drop_frac='0.5')[0], 'LIQUIDITY_DROP')
        self.assertIsNone(self.detect(1000, 500, rug_liq_drop_frac='0.5'))

    def test_no_baseline_or_no_reading_never_triggers(self):
        self.assertIsNone(self.detect(None, 0))
        self.assertIsNone(self.detect(0, 0))
        risk = self.make()
        life = risk.life(MINT, 7)
        life.baseline_quote = 1000
        risk._detect_reserves(MINT, self.ROW, life)                                  # no reserve read yet
        self.assertIsNone(life.trigger)

    def test_a_missing_vault_account_is_pool_gone(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        risk.observe_missing(MINT, self.now)
        risk._detect_reserves(MINT, self.ROW, life)
        self.assertEqual(life.trigger[0], 'POOL_GONE')
        risk.observe(MINT, 1, 5, self.now)                                           # a later good read clears the missing flag
        self.assertNotIn(MINT, risk.vault_missing)

    def test_a_trigger_fires_and_is_recorded_once(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        for reason in ('LIQUIDITY_DROP', 'POOL_GONE', 'NO_ROUTE'):
            risk._trigger(MINT, self.ROW, life, reason, {})
        self.assertEqual(life.trigger[0], 'LIQUIDITY_DROP')
        self.assertEqual([e['reason'] for e in self.events('trigger')], ['LIQUIDITY_DROP'])
        self.assertEqual(risk.counts['triggers'], 1)

    def test_pool_account_checks(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        pool = {'owner': 'ProgramA', 'data': ['', 'base64']}
        risk._read_accounts(MINT, self.ROW, life, mint_account(), pool)
        self.assertEqual((life.pool_owner, life.trigger), ('ProgramA', None))         # the first owner read is the baseline
        risk._read_accounts(MINT, self.ROW, life, mint_account(), dict(pool))
        self.assertIsNone(life.trigger)
        risk._read_accounts(MINT, self.ROW, life, mint_account(), dict(pool, owner='ProgramB'))
        self.assertEqual(life.trigger[0], 'POOL_GONE')
        other = risk.life(MINT, 8)
        risk._read_accounts(MINT, {'open_fill_id': 8}, other, mint_account(), None)
        self.assertEqual(other.trigger[1]['why'], 'pool account closed')

    def test_freeze_flag_is_recorded_once_and_forces_an_exit_only_when_configured(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        for _ in range(3):
            risk._read_accounts(MINT, self.ROW, life, mint_account(freeze=True), {'owner': 'P'})
        self.assertEqual((len(self.events('freeze_flag')), life.trigger, risk.counts['freeze_flags']), (1, None, 1))
        forcing = self.make(freeze_forces_exit=True)
        other = forcing.life(MINT, 9)
        forcing._read_accounts(MINT, {'open_fill_id': 9}, other, mint_account(freeze=True), {'owner': 'P'})
        self.assertEqual(other.trigger[0], 'FREEZE')


class Persistence(Harness):
    def test_the_state_is_rebuilt_from_the_events(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        risk._write({'ev': 'baseline', 'mint': MINT, 'open_fill_id': 7, 'quote_reserve_lamports': 123, 'pool_owner': 'P', 'source': 'screen'})
        risk._write({'ev': 'no_route', 'mint': MINT, 'open_fill_id': 7, 'since': 900, 'code': 'HTTP_400', 'last_good_value_lamports': 55})
        risk._write({'ev': 'unsellable', 'mint': MINT, 'open_fill_id': 7, 'since': 1020, 'value_lamports': 55, 'reason': 'NO_ROUTE:HTTP_400'})
        risk._write({'ev': 'freeze_flag', 'mint': MINT, 'open_fill_id': 7})
        risk._write({'ev': 'trigger', 'mint': MINT, 'open_fill_id': 7, 'reason': 'NO_ROUTE', 'detail': {}})
        again = self.make()
        rebuilt = again.lives[(MINT, 7)]
        self.assertEqual((rebuilt.baseline_quote, rebuilt.pool_owner, rebuilt.no_route_since, rebuilt.last_good_value, rebuilt.freeze_flagged),
                         (123, 'P', 900, 55, True))
        self.assertEqual(rebuilt.unsellable, {'since': 1020, 'value_lamports': 55, 'reason': 'NO_ROUTE:HTTP_400'})
        self.assertEqual((again.counts['unsellable'], again.counts['freeze_flags'], again.counts['triggers']), (1, 1, 1))
        self.assertEqual(again.health()['unsellable_now'], [MINT])

    def test_route_ok_clears_the_no_route_clock_and_closed_clears_the_unsellable_listing(self):
        risk = self.make()
        risk._write({'ev': 'no_route', 'mint': MINT, 'open_fill_id': 7, 'since': 900})
        risk._write({'ev': 'route_ok', 'mint': MINT, 'open_fill_id': 7})
        self.assertIsNone(self.make().lives[(MINT, 7)].no_route_since)
        risk._write({'ev': 'unsellable', 'mint': MINT, 'open_fill_id': 7, 'since': 1, 'value_lamports': 0})
        risk._write({'ev': 'closed', 'mint': MINT, 'open_fill_id': 7, 'reason': 'RUG_WRITEOFF', 'value_lamports': 0})
        again = self.make()
        self.assertEqual((again.health()['unsellable_now'], again.counts['writeoffs']), ([], 1))

    def test_each_lifecycle_has_its_own_state(self):
        risk = self.make()
        risk._write({'ev': 'unsellable', 'mint': MINT, 'open_fill_id': 7, 'since': 1, 'value_lamports': 0})
        again = self.make()
        self.assertIsNotNone(again.life(MINT, 7).unsellable)
        self.assertIsNone(again.life(MINT, 8).unsellable)                              # a later re-entry of the mint starts clean

    def test_unreadable_events_are_skipped_not_fatal(self):
        self.store.record('held_risk', {'ev': 'baseline'}, code_version='c', strategy_version='s')          # no mint
        self.store.record('held_risk', {'ev': 'baseline', 'mint': MINT, 'open_fill_id': 'x'}, code_version='c', strategy_version='s')
        self.store.record('held_risk', {'ev': 'surprise', 'mint': MINT, 'open_fill_id': 7}, code_version='c', strategy_version='s')
        self.assertEqual(self.make().health()['unsellable_now'], [])


class Isolation(Harness):
    class Runner:
        def __init__(self):
            self.errors = []

        def _error(self, code, **kw):
            self.errors.append((code, kw.get('scope')))

    def test_a_bug_in_the_checks_is_an_isolated_error_not_a_halt(self):
        runner = self.Runner()

        def boom(*args):
            raise RuntimeError('SYNTHETIC')
        self.risk.tick = boom
        self.risk.tick_safe(runner, {}, {}, 1)
        self.assertEqual(runner.errors, [('HELD_RISK_FAILED', 'held_risk')])

    def test_accounting_and_store_failures_still_escape(self):
        runner = self.Runner()
        for error in (paper.AccountingHalt('x'),):
            def boom(*args, error=error):
                raise error
            self.risk.tick = boom
            with self.assertRaises(paper.AccountingHalt):
                self.risk.tick_safe(runner, {}, {}, 1)
        import sqlite3
        self.risk.tick = lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError('x'))
        with self.assertRaises(sqlite3.Error):
            self.risk.tick_safe(runner, {}, {}, 1)

    def test_a_failing_account_read_is_recorded_and_skipped(self):
        class Helius:
            def get_multiple_accounts(self, keys):
                raise ProviderError('HTTP_503', True, provider='helius', raw=b'down')

        class Providers:
            helius = Helius()
        runner = self.Runner()
        runner.exit_providers = Providers()
        held = {MINT: {'open_fill_id': 7, 'state': {'pool': 'Pool'}}}
        self.risk._account_checks(runner, held, 1)
        self.assertEqual(runner.errors, [('HTTP_503', 'held_risk')])
        self.assertIsNone(self.risk.life(MINT, 7).trigger)

    def test_nothing_to_manage_means_the_normal_strategy_runs(self):
        class R:
            clock = staticmethod(lambda: 1000.0)
        self.assertIsNone(self.risk.manage(R(), MINT, None, {'open_fill_id': 7}))


if __name__ == '__main__':
    unittest.main()
