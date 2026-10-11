"""SYNTHETIC_TEST_ONLY: lean.held_risk units (L11F): configuration, the pure checks, the exact trigger boundaries, the baseline choice,
the consecutive-read confirmation, valuation, persistence and replay of the risk state, and the isolation of a failing check. The flows
(forced exits, the failing exit, D2's write-off, restart) are in tests/lean/test_e2e_held_risk.py with the real modules."""
import base64
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from lean import held_risk as H, paper
from lean.providers import ProviderError
from lean.store import Store

MINT = 'MintMintMintMintMintMintMintMintMintMint11'
ROW = {'open_fill_id': 7, 'state': {'pool': 'Pool'}}


def mint_account(*, freeze=False, short=False):
    data = bytearray(81 if short else 82)
    data[44] = 6
    if freeze and not short:
        data[46:50] = (1).to_bytes(4, 'little')
    return {'data': [base64.b64encode(bytes(data)).decode(), 'base64'], 'owner': 'Tokenkeg', 'lamports': 1}


class ConfigTests(unittest.TestCase):
    def test_defaults_are_the_documented_ones_and_the_module_is_off(self):
        cfg = H.Config.from_dict({})
        self.assertEqual((cfg.rug_liq_drop_frac, cfg.unsellable_after_s, cfg.unsellable_value_ttl_s, cfg.route_probe_s, cfg.freeze_check_every,
                          cfg.freeze_forces_exit, cfg.pool_gone_confirmations, cfg.enabled),
                         (Decimal('0.7'), 120, 30, 60, 6, False, 2, False))
        self.assertEqual(H.Config.from_dict(cfg.as_dict()).as_dict(), cfg.as_dict())
        self.assertEqual(H.Config.from_dict(None).as_dict(), cfg.as_dict())

    def test_everything_invalid_is_refused(self):
        for bad in ({'nope': 1}, {'unsellable_writeoff_s': 600}, {'rug_liq_drop_frac': '0'}, {'rug_liq_drop_frac': '1'},
                    {'rug_liq_drop_frac': 'x'}, {'rug_liq_drop_frac': '1.5'}, {'unsellable_after_s': 0}, {'unsellable_after_s': 1.5},
                    {'unsellable_after_s': True}, {'unsellable_value_ttl_s': 0}, {'route_probe_s': -1}, {'freeze_check_every': 0},
                    {'pool_gone_confirmations': 0}, {'pool_gone_confirmations': 21}, {'pool_gone_confirmations': True},
                    {'freeze_forces_exit': 1}, {'enabled': 'yes'}, {'enabled': 1}):
            with self.subTest(bad=bad), self.assertRaises(H.HeldRiskConfigError):
                H.Config.from_dict(bad)
        with self.assertRaises(H.HeldRiskConfigError):
            H.Config.from_dict([])

    def test_absent_or_disabled_builds_nothing_and_only_an_explicit_true_builds_the_module(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = Store(Path(tmp.name) / 'lean.sqlite', initial_cash_sol='1', code_version='c', strategy_version='s')
        self.addCleanup(store.close)
        for value in (None, {}, {'enabled': False}, {'rug_liq_drop_frac': '0.5'}):
            self.assertIsNone(H.build(value, store, code_version='c', strategy_version='s', clock=lambda: 0), value)
        self.assertIsInstance(H.build({'enabled': True}, store, code_version='c', strategy_version='s', clock=lambda: 0), H.HeldRisk)


class PureChecks(unittest.TestCase):
    def test_freeze_authority_is_read_from_the_coption_tag(self):
        self.assertTrue(H.freeze_authority_set(mint_account(freeze=True)))
        self.assertFalse(H.freeze_authority_set(mint_account()))
        for evidence in (None, {}, {'data': 'x'}, {'data': ['!!!', 'base64']}, mint_account(short=True), [], 7):
            self.assertFalse(H.freeze_authority_set(evidence), evidence)           # missing evidence is not evidence of a freeze

    def test_net_value_is_the_paper_sell_without_ever_going_negative(self):
        pcfg = paper.PaperConfig(fee_lamports=50_000, slippage_bps=50)
        self.assertEqual(H.net_lamports(1_000_000_000, pcfg), 1_000_000_000 * 9950 // 10000 - 50_000)
        self.assertEqual(H.net_lamports(50_000, pcfg), 0)
        self.assertEqual(H.net_lamports(1, pcfg), 0)


class FakeRunner:
    """Just enough of the Runner for the module's own methods."""

    def __init__(self, store, now=1000):
        self.store, self.marks, self.errors, self.t = store, {}, [], now
        self.pcfg = paper.PaperConfig()
        self.stop = type('Stop', (), {'is_set': staticmethod(lambda: False)})()

    def _now(self):
        return self.t

    def clock(self):
        return float(self.t)

    def _error(self, code, **kw):
        self.errors.append((code, kw.get('scope')))


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'lean.sqlite', initial_cash_sol='10', code_version='c', strategy_version='s')
        self.addCleanup(self.store.close)
        self.now = 1_000
        self.runner = FakeRunner(self.store)
        self.risk = self.make()

    def make(self, **cfg):
        return H.HeldRisk(self.store, H.Config.from_dict({'enabled': True, **cfg}), code_version='c', strategy_version='s', clock=lambda: float(self.now))

    def events(self, ev=None):
        rows = [json.loads(r['payload']) for r in self.store.rows('events', kind='held_risk', limit=1000)]
        return [p for p in rows if ev is None or p['ev'] == ev]

    def read(self, risk, reserve, at, *, mint=MINT, row=None, missing=False):
        """One marks read followed by the module's no-I/O step."""
        row = row or ROW
        if missing:
            risk.observe_missing(mint, at)
        else:
            risk.observe(mint, 1, reserve, at)
        self.runner.t = at
        risk.after_marks(self.runner, {mint: object()}, {mint: row})
        return risk.life(mint, row['open_fill_id'])


class TriggerBoundaries(Harness):
    counter = 100

    def detect(self, baseline, now_reserve, **cfg):
        TriggerBoundaries.counter += 1                                               # a fresh lifecycle: triggers are sticky and replayed
        row = {'open_fill_id': self.counter, 'state': {'pool': 'Pool'}}
        risk = self.make(**cfg)
        risk.life(MINT, self.counter).baseline_quote = baseline
        return self.read(risk, now_reserve, self.now, row=row).trigger

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
        self.assertIsNone(self.detect(0, 0))
        risk = self.make()
        life = risk.life(MINT, 7)
        life.baseline_quote = 1000
        risk.after_marks(self.runner, {MINT: object()}, {MINT: ROW})                 # no read this pass (e.g. a provider failure)
        self.assertIsNone(life.trigger)

    def test_a_reading_is_used_in_its_own_pass_only(self):
        risk = self.make()
        risk.life(MINT, 7).baseline_quote = 1000
        risk.observe(MINT, 1, 999, self.now)                                          # a healthy read ...
        risk.after_marks(self.runner, {MINT: object()}, {MINT: ROW})
        risk.after_marks(self.runner, {MINT: object()}, {MINT: ROW})                  # ... is not re-read in the next pass
        self.assertIsNone(risk.life(MINT, 7).trigger)

    def test_a_trigger_fires_and_is_recorded_once(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        for reason in ('LIQUIDITY_DROP', 'POOL_GONE', 'NO_ROUTE'):
            risk._trigger(MINT, ROW, life, reason, {})
        self.assertEqual(life.trigger[0], 'LIQUIDITY_DROP')
        self.assertEqual([e['reason'] for e in self.events('trigger')], ['LIQUIDITY_DROP'])
        self.assertEqual((risk.counts['triggers'], risk.trigger(MINT, 7), risk.trigger(MINT, 8), risk.trigger('other', 7)), (1, 'LIQUIDITY_DROP', None, None))


class Confirmation(Harness):
    def test_pool_gone_needs_consecutive_empty_reads_and_a_good_read_resets(self):
        risk = self.make()
        self.assertIsNone(self.read(risk, 0, 1000, missing=True).trigger)             # 1 of 2
        self.assertEqual(risk.life(MINT, 7).vault_misses, 1)
        self.assertIsNone(self.read(risk, 500, 1010).trigger)                         # a good read: reset
        self.assertEqual(risk.life(MINT, 7).vault_misses, 0)
        self.assertIsNone(self.read(risk, 0, 1020, missing=True).trigger)             # 1 of 2 again, not 2
        self.assertEqual(self.read(risk, 0, 1030, missing=True).trigger[0], 'POOL_GONE')
        self.assertEqual([e['ev'] for e in self.events() if e['ev'].startswith('vault')], ['vault_miss', 'vault_seen', 'vault_miss', 'vault_miss'])

    def test_the_same_read_is_not_counted_twice(self):
        risk = self.make()
        self.read(risk, 0, 1000, missing=True)
        risk.observe_missing(MINT, 1000)                                              # the same mark_at again (same read)
        risk.after_marks(self.runner, {MINT: object()}, {MINT: ROW})
        self.assertEqual(risk.life(MINT, 7).vault_misses, 1)

    def test_a_read_that_did_not_happen_neither_confirms_nor_resets(self):
        risk = self.make()
        self.read(risk, 0, 1000, missing=True)
        risk.after_marks(self.runner, {MINT: object()}, {MINT: ROW})                  # a provider failure: no read at all
        self.assertEqual(risk.life(MINT, 7).vault_misses, 1)

    def test_one_confirmation_means_one_empty_read(self):
        risk = self.make(pool_gone_confirmations=1)
        self.assertEqual(self.read(risk, 0, 1000, missing=True).trigger[0], 'POOL_GONE')

    def test_pool_account_checks_are_confirmed_too(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        pool = {'owner': 'ProgramA', 'data': ['', 'base64']}
        risk._read_accounts(MINT, ROW, life, mint_account(), pool)
        self.assertEqual((life.pool_owner, life.trigger, life.account_misses), ('ProgramA', None, 0))     # the first owner read is the baseline
        risk._read_accounts(MINT, ROW, life, mint_account(), dict(pool, owner='ProgramB'))
        self.assertEqual((life.trigger, life.account_misses), (None, 1))               # one migrated read is not enough
        risk._read_accounts(MINT, ROW, life, mint_account(), dict(pool))
        self.assertEqual(life.account_misses, 0)                                       # back to normal: reset
        risk._read_accounts(MINT, ROW, life, mint_account(), None)
        risk._read_accounts(MINT, ROW, life, mint_account(), None)
        self.assertEqual((life.trigger[0], life.trigger[1]['why']), ('POOL_GONE', 'pool account closed'))
        self.assertEqual([e['ev'] for e in self.events() if e['ev'].startswith('account')],
                         ['account_miss', 'account_seen', 'account_miss', 'account_miss'])

    def test_freeze_flag_is_recorded_once_and_forces_an_exit_only_when_configured(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        for _ in range(3):
            risk._read_accounts(MINT, ROW, life, mint_account(freeze=True), {'owner': 'P'})
        self.assertEqual((len(self.events('freeze_flag')), life.trigger, risk.counts['freeze_flags']), (1, None, 1))
        forcing = self.make(freeze_forces_exit=True)
        other = forcing.life(MINT, 9)
        forcing._read_accounts(MINT, {'open_fill_id': 9}, other, mint_account(freeze=True), {'owner': 'P'})
        self.assertEqual(other.trigger[0], 'FREEZE')

    def test_a_triggered_lifecycle_is_not_re_examined(self):
        risk = self.make()
        life = risk.life(MINT, 7)
        risk._trigger(MINT, ROW, life, 'NO_ROUTE', {})
        risk._read_accounts(MINT, ROW, life, mint_account(), None)
        self.assertEqual((life.account_misses, [e['ev'] for e in self.events('account_miss')]), (0, []))


class Valuation(Harness):
    def value(self, risk, state, at):
        row = {'open_fill_id': 7, 'state': state}
        life = risk.life(MINT, 7)
        life.trigger = ('NO_ROUTE', {})
        self.runner.t = at
        risk.after_marks(self.runner, {MINT: object()}, {MINT: row})
        return self.runner.marks.get(MINT)

    def test_a_failing_rug_exit_is_worth_zero_without_a_fresh_executable_quote(self):
        risk = self.make()
        self.assertEqual(self.value(risk, {'exit_failing_since': 900}, 1000), (Decimal(0), 1000, 'held_risk'))

    def test_a_fresh_executable_quote_is_the_value_and_a_stale_one_is_not(self):
        risk = self.make(unsellable_value_ttl_s=30)
        risk.life(MINT, 7).last_good = (123_000_000, 990)
        self.assertEqual(self.value(risk, {'exit_failing_since': 900}, 1000)[0], Decimal(123_000_000) / 10 ** 9)       # 10 s old
        self.assertEqual(self.value(risk, {'exit_failing_since': 900}, 1020)[0], Decimal(123_000_000) / 10 ** 9)       # 30 s: still inside
        self.assertEqual(self.value(risk, {'exit_failing_since': 900}, 1021)[0], Decimal(0))                           # 31 s: worth 0
        risk.life(MINT, 7).last_good = (123_000_000, 2000)                                                              # a quote "from the future"
        self.assertEqual(self.value(risk, {'exit_failing_since': 900}, 1021)[0], Decimal(0))

    def test_only_a_triggered_position_with_a_failing_exit_is_valued_here(self):
        risk = self.make()
        self.runner.marks[MINT] = (Decimal('0.5'), 990, 'vaults')
        risk.after_marks(self.runner, {MINT: object()}, {MINT: {'open_fill_id': 7, 'state': {'exit_failing_since': 900}}})   # no trigger
        self.assertEqual(self.runner.marks[MINT], (Decimal('0.5'), 990, 'vaults'))
        risk.life(MINT, 7).trigger = ('NO_ROUTE', {})
        risk.after_marks(self.runner, {MINT: object()}, {MINT: {'open_fill_id': 7, 'state': {}}})                           # exit not failing yet
        self.assertEqual(self.runner.marks[MINT], (Decimal('0.5'), 990, 'vaults'))


class Baseline(Harness):
    def setUp(self):
        super().setUp()
        self.store.add_candidate(MINT, ts=1.0)

    def screen(self, ts, action, quote):
        self.store.add_decision('screen', action, mint=MINT, features={} if quote is None else {'quote_gross_raw': quote}, ts=ts)

    def open_fill(self, ts):
        fill = paper.buy(paper.Quote(MINT, 'buy', 20_000_000, 10 ** 9, 6, ts, ref='r'), '0.02', paper.PaperConfig())
        return self.store.add_fill(fill, state={'event': 'open', 'state': {'pool': 'Pool'}})

    def baseline(self, open_id):
        life = self.risk.life(MINT, open_id)
        self.risk._baseline(self.runner, MINT, {'open_fill_id': open_id}, life)
        return life.baseline_quote, [e['source'] for e in self.events('baseline')]

    def test_the_baseline_is_the_latest_pass_before_the_buy_not_the_oldest_row(self):
        for i in range(25):
            self.screen(10.0 + i, 'REJECT', 1)                                          # twenty-plus other rows ...
        self.screen(40.0, 'PASS', 111)                                                  # an old PASS (an earlier lifecycle's screen)
        self.screen(50.0, 'FAILED', 999)
        self.screen(60.0, 'PASS', 222)                                                  # <- the screen tied to this BUY
        open_id = self.open_fill(70.0)
        self.screen(80.0, 'PASS', 333)                                                  # a screen AFTER the buy must not count
        self.assertEqual(self.baseline(open_id), (222, ['screen']))

    def test_a_pass_without_the_reserve_or_no_screen_falls_back_to_the_first_mark(self):
        self.screen(60.0, 'PASS', None)
        open_id = self.open_fill(70.0)
        self.assertEqual(self.baseline(open_id), (None, []))                             # nothing yet: try again next pass
        self.risk.observe(MINT, 1, 777, 1000)
        self.assertEqual(self.baseline(open_id), (777, ['first_mark']))

    def test_the_baseline_is_written_once_and_replayed(self):
        self.screen(60.0, 'PASS', 222)
        open_id = self.open_fill(70.0)
        self.baseline(open_id)
        self.assertEqual(self.make().lives[(MINT, open_id)].baseline_quote, 222)
        risk = self.make()
        risk.after_marks(self.runner, {MINT: object()}, {MINT: {'open_fill_id': open_id, 'state': {}}})
        self.assertEqual(len(self.events('baseline')), 1)                                # replayed baselines are not rewritten


class Persistence(Harness):
    def test_the_state_is_rebuilt_from_the_events(self):
        risk = self.make()
        for payload in ({'ev': 'baseline', 'quote_reserve_lamports': 123, 'pool_owner': 'P', 'source': 'screen'},
                        {'ev': 'vault_miss', 'n': 2, 'at': 990}, {'ev': 'account_miss', 'n': 1}, {'ev': 'no_route', 'since': 900},
                        {'ev': 'freeze_flag'}, {'ev': 'trigger', 'reason': 'NO_ROUTE', 'detail': {'since': 900}}):
            risk._write({'mint': MINT, 'open_fill_id': 7, **payload})
        rebuilt = self.make().lives[(MINT, 7)]
        self.assertEqual((rebuilt.baseline_quote, rebuilt.pool_owner, rebuilt.vault_misses, rebuilt.last_miss_at, rebuilt.account_misses,
                          rebuilt.no_route_since, rebuilt.freeze_flagged, rebuilt.trigger),
                         (123, 'P', 2, 990, 1, 900, True, ('NO_ROUTE', {'since': 900})))
        again = self.make()
        self.assertEqual((again.counts['freeze_flags'], again.counts['triggers'], again.trigger(MINT, 7)), (1, 1, 'NO_ROUTE'))

    def test_resets_clear_the_counters(self):
        risk = self.make()
        for ev in ({'ev': 'vault_miss', 'n': 1, 'at': 1}, {'ev': 'vault_seen'}, {'ev': 'account_miss', 'n': 1}, {'ev': 'account_seen'},
                   {'ev': 'no_route', 'since': 9}, {'ev': 'route_ok'}):
            risk._write({'mint': MINT, 'open_fill_id': 7, **ev})
        life = self.make().lives[(MINT, 7)]
        self.assertEqual((life.vault_misses, life.account_misses, life.no_route_since), (0, 0, None))

    def test_each_lifecycle_has_its_own_state(self):
        self.risk._write({'ev': 'trigger', 'mint': MINT, 'open_fill_id': 7, 'reason': 'POOL_GONE', 'detail': {}})
        again = self.make()
        self.assertEqual((again.trigger(MINT, 7), again.trigger(MINT, 8)), ('POOL_GONE', None))        # a later re-entry starts clean

    def test_unreadable_events_are_skipped_not_fatal(self):
        self.store.record('held_risk', {'ev': 'baseline'}, code_version='c', strategy_version='s')          # no mint
        self.store.record('held_risk', {'ev': 'baseline', 'mint': MINT, 'open_fill_id': 'x'}, code_version='c', strategy_version='s')
        self.store.record('held_risk', {'ev': 'surprise', 'mint': MINT, 'open_fill_id': 7}, code_version='c', strategy_version='s')
        self.assertEqual(self.make().health()['failing_now'], [])

    def test_the_replay_reads_every_event_not_just_the_first_page(self):
        for i in range(2500):
            self.risk._write({'ev': 'vault_miss', 'mint': MINT, 'open_fill_id': 7, 'n': i, 'at': i})
        self.risk._write({'ev': 'trigger', 'mint': MINT, 'open_fill_id': 7, 'reason': 'POOL_GONE', 'detail': {}})   # the 2501st event
        self.assertEqual(self.make().trigger(MINT, 7), 'POOL_GONE')
        previous, H.REPLAY_PAGE = H.REPLAY_PAGE, 7
        try:
            self.assertEqual(self.make().trigger(MINT, 7), 'POOL_GONE')
        finally:
            H.REPLAY_PAGE = previous


class Probes(Harness):
    class Jupiter:
        def __init__(self, outcomes):
            self.outcomes, self.calls = list(outcomes), 0

        def quote(self, *args):
            self.calls += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return type('Q', (), {'out_amount': outcome})(), b'{}', {}

    def setUp(self):
        super().setUp()
        self.runner.taker = 'Taker'
        self.position = type('P', (), {'qty_raw': 10 ** 9})()

    def probe(self, outcomes, risk=None, at=1000, **kw):
        risk = risk or self.make(**kw)
        self.runner.exit_providers = type('Prov', (), {'jupiter': self.Jupiter(outcomes)})()
        life = risk.life(MINT, 7)
        risk._probe(self.runner, MINT, self.position, ROW, life, at)
        return risk, life

    def test_a_non_transient_failure_starts_the_clock_and_the_boundary_triggers(self):
        no_route = ProviderError('HTTP_400', False, raw=b'{}')
        risk, life = self.probe([no_route], at=1000)
        self.assertEqual((life.no_route_since, life.trigger), (1000, None))
        risk, life = self.probe([no_route], risk=risk, at=1119)
        self.assertIsNone(life.trigger)                                                # 119 s < 120 s
        risk, life = self.probe([no_route], risk=risk, at=1120)
        self.assertEqual(life.trigger[0], 'NO_ROUTE')                                  # exactly 120 s
        self.assertEqual([e['ev'] for e in self.events() if e['ev'] != 'trigger'], ['no_route'])          # the start is written once

    def test_an_outage_is_not_no_route_and_a_good_quote_clears_the_clock_and_remembers_the_value(self):
        risk, life = self.probe([ProviderError('HTTP_503', True), ProviderError('TIMEOUT', True)])
        self.assertIsNone(life.no_route_since)
        risk, life = self.probe([ProviderError('HTTP_400', False)], risk=risk, at=1010)
        self.assertEqual(life.no_route_since, 1010)
        risk, life = self.probe([1_000_000_000], risk=risk, at=1020)
        self.assertEqual((life.no_route_since, life.last_good), (None, (1_000_000_000 * 9950 // 10000 - 50_000, 1020)))
        self.assertEqual([e['ev'] for e in self.events()], ['no_route', 'route_ok'])

    def test_a_worthless_quote_is_a_route_but_not_a_value(self):
        risk, life = self.probe([1_000])
        self.assertIsNone(life.last_good)

    def test_probe_keeps_no_raw_bytes_on_success(self):
        self.probe([5 * 10 ** 9])
        self.assertEqual(self.store.rows('observations', limit=10), [])


class Isolation(Harness):
    def test_a_bug_in_the_checks_is_an_isolated_error_not_a_halt(self):
        def boom(*args):
            raise RuntimeError('SYNTHETIC')
        self.risk.after_marks = boom
        self.risk.after_marks_safe(self.runner, {}, {})
        self.risk.after_exits = boom
        self.risk.after_exits_safe(self.runner)
        self.assertEqual(self.runner.errors, [('HELD_RISK_FAILED', 'held_risk')] * 2)

    def test_accounting_and_store_failures_still_escape(self):
        for error in (paper.AccountingHalt('x'), sqlite3.OperationalError('x')):
            self.risk.after_marks = lambda *a, error=error: (_ for _ in ()).throw(error)
            with self.assertRaises(type(error)):
                self.risk.after_marks_safe(self.runner, {}, {})

    def test_a_failing_account_read_is_recorded_and_skipped(self):
        class Helius:
            def get_multiple_accounts(self, keys):
                raise ProviderError('HTTP_503', True, provider='helius', raw=b'down')
        self.runner.exit_providers = type('Prov', (), {'helius': Helius()})()
        self.risk._account_checks(self.runner, {MINT: ROW})
        self.assertEqual(self.runner.errors, [('HTTP_503', 'held_risk')])
        self.assertIsNone(self.risk.life(MINT, 7).trigger)


if __name__ == '__main__':
    unittest.main()
