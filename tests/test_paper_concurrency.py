"""SYNTHETIC_TEST_ONLY: opt-in concurrent paper entries (paper_concurrent_entries_version 1).

Engine tests use the real engine/ledger with config/paper.json and synthetic events. Scheduler,
monitor-service, cycle and dispatcher tests use the repository's existing synthetic fixtures;
no provider, network or production store is involved. Default behaviour (flag absent) is
asserted unchanged next to every flag-on behaviour.
"""
import contextlib
import copy
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from desk import paper_concurrency as pc
from desk.engine import equity, initial_state, transition
from desk.ledger import Ledger
from desk.model import canonical
from tests.helpers import T, config, control, event

KEY = pc.KEY
TTL = pc.TTL_KEY


def cfg_on(**changes):
    return {**config(), KEY: 1, TTL: 120, **changes}


def position(mark_at=T, opened_at=T, **changes):
    return {'qty': '1', 'initial_qty': '1', 'cost_left': '0.1', 'initial_cost': '0.1', 'trade_pnl': '0',
            'opened_at': opened_at, 'exit_blocked': None, 'mark_status': 'MODEL_ESTIMATE', 'stage': 0,
            'stop_ratio': '0.8', 'peak_ratio': '1', 'touched_15': False, 'mark_value': '0.1',
            'mark_at': mark_at, 'pool': 'P', 'entry_scores': {}, 'provenance': 'SYNTHETIC_TEST_ONLY',
            'taker': None, **changes}


def state(positions=None, mode='RUNNING'):
    return {'mode': mode, 'positions': positions or {}}


class ConfigAndGateTests(unittest.TestCase):
    def test_absent_flag_keeps_legacy_rule_exactly(self):
        cfg = config()
        self.assertEqual(pc.selected(cfg), 0)
        self.assertFalse(pc.entry_blocked(state(), cfg))
        self.assertTrue(pc.entry_blocked(state({'A': position()}), cfg))
        self.assertTrue(pc.entry_blocked(state(mode='EXIT_ONLY'), cfg))
        self.assertEqual(pc.entry_blockers(state({'A': position()}), cfg, monitoring_remaining=0, now=10**9),
                         ['HELD_POSITION_PRIORITY'])

    def test_flag_validation_fails_closed(self):
        self.assertEqual(pc.selected(cfg_on()), 1)
        for bad in (0, 2, True, '1', 1.0, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pc.selected({**config(), KEY: bad})
        for cap in (1, 9, '4', None, True):
            with self.assertRaises(ValueError, msg=repr(cap)):
                pc.selected(cfg_on(max_positions=cap))
        with self.assertRaises(ValueError):
            pc.selected({**cfg_on(), 'mode': 'live'})

    def test_concurrent_entry_allowed_while_holding_up_to_the_cap(self):
        cfg = cfg_on()
        s = state({'A': position(), 'B': position()})
        self.assertEqual(pc.entry_blockers(s, cfg), [])
        self.assertFalse(pc.entry_blocked(s, cfg))
        full = state({m: position() for m in 'ABCD'})
        self.assertEqual(pc.entry_blockers(full, cfg), ['MAX_POSITIONS_REACHED'])
        self.assertEqual(pc.entry_blockers(state({m: position() for m in 'ABC'}), cfg), [])

    def test_unresolved_exit_or_paused_mode_freezes_entries(self):
        cfg = cfg_on()
        for held in (position(exit_blocked='EXACT_FRESH_SELL_QUOTE_REQUIRED'),
                     position(mark_status='UNVERIFIED_EXIT')):
            self.assertEqual(pc.entry_blockers(state({'A': position(), 'B': held}), cfg), ['HELD_EXIT_UNRESOLVED'])
        for mode in ('EXIT_ONLY', 'ENTRY_PAUSED', 'LIQUIDATING', 'STOPPED'):
            self.assertIn('LEDGER_MODE_NOT_RUNNING', pc.entry_blockers(state({'A': position()}, mode), cfg))

    def test_monitoring_reserve_math_boundaries(self):
        cfg = cfg_on()
        one = state({'A': position()})
        need = pc.LEG_REQUESTS * 2 * pc.RESERVE_PASSES  # two positions once the new one is added
        self.assertEqual(need, 120)
        self.assertEqual(pc.monitoring_required(1), 60)
        self.assertEqual(pc.entry_blockers(one, cfg, monitoring_remaining=need - 1), ['MONITORING_RESERVE_INSUFFICIENT'])
        self.assertEqual(pc.entry_blockers(one, cfg, monitoring_remaining=need), [])
        # The legacy 60/hour allowance cannot carry a second concurrent position; 3600 carries eight.
        self.assertGreater(pc.monitoring_required(2), 60)
        self.assertLessEqual(pc.monitoring_required(8), 3600)

    def test_portfolio_freshness_matches_the_portfolio_ttl_and_includes_preparation(self):
        cfg = cfg_on()
        s = state({'A': position(mark_at=1000)})
        ttl = cfg[TTL]
        # T16G item 4: acquisition (12 s) and intake (10 s) also precede the decision, so 30.0 became 52.0
        # (loosened on purpose: the old figure left them out and under-refused doomed entries).
        self.assertEqual(pc.ENTRY_SECONDS, 52.0)
        self.assertEqual(pc.ENTRY_SECONDS, sum(seconds for _, seconds in pc.ENTRY_PHASES))
        self.assertEqual([name for name, _ in pc.ENTRY_PHASES], ['acquisition', 'intake', 'preparation', 'cycle'])
        # Mark age at decision = (now - mark) + entry seconds; refused once it exceeds the portfolio TTL.
        edge = 1000 + int(ttl - pc.ENTRY_SECONDS)
        self.assertEqual(pc.entry_blockers(s, cfg, now=edge), [])
        self.assertEqual(pc.entry_blockers(s, cfg, now=edge + 1), ['PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'])
        self.assertEqual(pc.entry_blockers(state(), cfg, now=10**9), [])  # flat ledgers have no marks
        # The price TTL alone (10 s) can never cover an entry, so every entry with a position is refused up front.
        strict = cfg_on(**{TTL: cfg['price_ttl_seconds']})
        self.assertEqual(pc.entry_blockers(s, strict, now=1000), ['PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'])

    def test_preparation_seconds_match_the_real_history_preparation_budget(self):
        from desk.paper_history_preparation import PREPARATION_SECONDS
        self.assertEqual(pc.PREPARATION_SECONDS, PREPARATION_SECONDS)

    def test_portfolio_ttl_is_only_valid_with_the_flag_and_inside_its_bounds(self):
        with self.assertRaises(ValueError):                     # key without the experiment
            pc.selected({**config(), TTL: 60})
        with self.assertRaises(ValueError):                     # flag without the key
            pc.selected({**config(), KEY: 1})
        base = config()
        for bad in (base['price_ttl_seconds'] - 1, 121, 600, '60', 60.0, True, None, 0, -5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pc.selected(cfg_on(**{TTL: bad}))
        for good in (base['price_ttl_seconds'], 60, 120):
            self.assertEqual(pc.selected(cfg_on(**{TTL: good})), 1)
        from desk.engine import portfolio_ttl
        self.assertEqual(portfolio_ttl(config()), config()['price_ttl_seconds'])   # absent: unchanged
        with self.assertRaises(ValueError):                     # the engine itself never trusts a bare key
            portfolio_ttl({**config(), TTL: 60})

    def test_timeline_arithmetic_for_every_book_size(self):
        """Why the portfolio TTL exists: N sequential legs plus preparation cannot fit the 10 s price TTL."""
        price = config()['price_ttl_seconds']
        for n in range(1, pc.MAX_CONCURRENT):          # N held positions, one more entry
            oldest = pc.held_wall_seconds(n) + pc.ENTRY_SECONDS   # first leg's mark ages through the rest of the tick
            self.assertGreater(oldest, price, n)        # the original 10 s rule can never admit a concurrent entry
            self.assertLess(oldest, 120, n)             # the bounded portfolio TTL (max 120 s) admits every book size

    def test_plan_legs_without_a_cap_covers_every_open_position(self):
        positions = {m: position(mark_at=i) for i, m in enumerate('ABCDEFGH')}
        run, deferred = pc.plan_legs(positions)
        self.assertEqual((len(run), deferred), (8, []))
        self.assertEqual(run, pc.held_order(positions))
        self.assertEqual(pc.plan_legs(positions, None), (run, []))
        self.assertEqual(len(pc.plan_legs(positions, 16)[0]), 2)       # an explicit cap still defers, oldest first

    def test_fresh_held_unit_covers_all_positions_inside_its_timeout(self):
        from tools import paper_scheduler as wrapper
        text = (Path(__file__).resolve().parents[1] / 'deploy' / 'fresh' / 'desk-paper-held-cycle.service').read_text()
        line = next(l for l in text.splitlines() if l.startswith('ExecStart='))
        wall = float(line.split('--wall-seconds ')[1].split()[0])
        timeout = int(next(l.split('=')[1] for l in text.splitlines() if l.startswith('TimeoutStartSec=')))
        self.assertGreaterEqual(wall, pc.held_wall_seconds(pc.MAX_CONCURRENT))           # all 8 legs fit the cap
        self.assertLessEqual(wrapper.HELD_LEASE_WAIT_SECONDS + wall + 10, timeout)       # lease wait + legs + margin
        self.assertEqual(len(pc.plan_legs({m: position(mark_at=i) for i, m in enumerate('ABCDEFGH')}, wall)[0]), 8)

    def test_held_order_and_legs_are_deterministic_unresolved_first(self):
        positions = {'C': position(mark_at=30), 'B': position(mark_at=20), 'A': position(mark_at=20, opened_at=1),
                     'D': position(mark_at=99, exit_blocked='X')}
        self.assertEqual(pc.held_order(positions), ['D', 'A', 'B', 'C'])
        self.assertEqual(pc.plan_legs(positions, 12), (['D'], ['A', 'B', 'C']))
        self.assertEqual(pc.plan_legs(positions, 0), (['D'], ['A', 'B', 'C']))  # at least one leg always runs
        self.assertEqual(pc.plan_legs(positions, 16)[0], ['D', 'A'])

    def test_wall_time_four_positions_fit_entry_unit_but_need_legs_in_held_unit(self):
        root = Path(__file__).resolve().parents[1]
        text = lambda name: (root / 'deploy' / name).read_text()

        def timeout(unit):
            return int(next(line.split('=')[1] for line in text(unit).splitlines() if line.startswith('TimeoutStartSec=')))

        four = pc.held_wall_seconds(4)
        self.assertAlmostEqual(four, 31.2)
        # Held-first inside the entry tick: 4 legs + one entry stay inside the entry unit timeout...
        self.assertLess(four + pc.ENTRY_SECONDS, timeout('desk-paper-entry-dispatcher.service'))
        # ...but cannot fit the 20 s held unit, so the held service splits the work into legs.
        self.assertGreater(four, timeout('desk-paper-held-cycle.service'))
        run, deferred = pc.plan_legs({m: position(mark_at=i) for i, m in enumerate('ABCD')}, 12)
        self.assertLessEqual(pc.held_wall_seconds(len(run)), timeout('desk-paper-held-cycle.service'))
        self.assertEqual((len(run), len(deferred)), (1, 3))


class EngineInterleavingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'l.sqlite'
        self.cfg = cfg_on()
        self.ledger = Ledger(self.path)
        self.addCleanup(lambda: self.ledger.close())

    def apply(self, e):
        return self.ledger.apply(e, self.cfg, transition, initial_state)

    def enter(self, mint, at, held=()):
        for h in held:  # refresh every held mark right before the decision (engine STALE_PORTFOLIO)
            self.apply(event(at - 1, mint=h))
        return self.apply(event(at, mint=mint))

    def positions(self):
        return json.loads(self.ledger.db.execute('SELECT payload FROM state').fetchone()[0])['positions']

    def test_two_entries_while_holding_through_the_engine(self):
        self.assertEqual([o['type'] for o in self.enter('A', T)], ['fill'])
        self.assertEqual([o['type'] for o in self.enter('B', T + 60, ['A'])], ['fill'])
        self.assertEqual(set(self.positions()), {'A', 'B'})

    def test_fifth_entry_refused_at_max_four_by_engine_not_bypassed(self):
        held = []
        for i, mint in enumerate('ABCD'):
            self.assertEqual(self.enter(mint, T + 60 * i, held)[0]['type'], 'fill')
            held.append(mint)
        reject = self.enter('E', T + 240, held)[0]
        self.assertEqual((reject['type'], reject['reason']), ('reject', 'MAX_POSITIONS'))
        self.assertEqual(len(self.positions()), 4)

    def test_exposure_cap_refusal(self):
        self.cfg = cfg_on(max_positions=8)
        self.ledger.close()
        self.ledger = Ledger(self.path)
        held = []
        for i in range(5):
            self.assertEqual(self.enter(f'M{i}', T + 60 * i, held)[0]['type'], 'fill')
            held.append(f'M{i}')
        reject = self.enter('M5', T + 300, held)[0]
        self.assertEqual((reject['type'], reject['reason']), ('reject', 'BELOW_MINIMUM'))

    def test_engine_rejects_entry_when_a_held_mark_is_older_than_the_portfolio_ttl(self):
        """The binding constraint: every held mark must be within the portfolio TTL at the entry decision."""
        self.enter('A', T)
        fresh_enough = self.apply(event(T + 60, mint='B'))[0]          # 60 s old mark, TTL 120: allowed
        self.assertEqual(fresh_enough['type'], 'fill')
        reject = self.apply(event(T + 200, mint='C'))[0]                # A is 200 s old: stale
        self.assertEqual((reject['type'], reject['reason']), ('reject', 'STALE_PORTFOLIO'))
        self.assertEqual(set(self.positions()), {'A', 'B'})
        self.assertEqual(pc.entry_blockers(state(self.positions()), self.cfg, now=T + 200), [
            'PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'])  # refused before spending any investigation request

    def test_price_ttl_only_config_keeps_the_original_ten_second_rule(self):
        cfg = cfg_on(**{TTL: config()['price_ttl_seconds']})
        self.ledger.close()
        self.ledger = Ledger(self.path)
        self.cfg = cfg
        self.enter('A', T)
        reject = self.apply(event(T + 60, mint='B'))[0]
        self.assertEqual((reject['type'], reject['reason']), ('reject', 'STALE_PORTFOLIO'))

    def fills(self):
        return self.ledger.db.execute(
            "SELECT count(*) FROM outcomes WHERE json_extract(payload,'$.type')='fill'").fetchone()[0]

    def interleave(self):
        self.enter('A', T)                                   # buy A
        self.enter('B', T + 60, ['A'])                       # buy B while holding A
        self.apply(event(T + 65, mint='A', reserve_sol='160'))   # partial sell A
        self.enter('C', T + 120, ['A', 'B'])                 # buy C while holding A, B
        self.apply(event(T + 130, mint='B', danger=True, reserve_sol='160'))  # close B
        self.apply(event(T + 140, mint='A', danger=True, reserve_sol='160'))  # close A

    def test_interleaved_buy_buy_sell_buy_sell_keeps_portfolio_cash_consistent(self):
        self.interleave()
        self.assertEqual(set(self.positions()), {'C'})
        view = pc.attribution(self.ledger.db, self.cfg)
        self.assertEqual([p['mint'] for p in view['open']], ['C'])
        self.assertEqual(sorted(p['mint'] for p in view['closed']), ['A', 'B'])
        self.assertNotEqual(view['closed'][0]['entry_seq'], view['closed'][1]['entry_seq'])

    def test_attribution_detects_tampered_cash_and_orphan_sell(self):
        self.interleave()
        saved = json.loads(self.ledger.db.execute('SELECT payload FROM state').fetchone()[0])
        broken = {**saved, 'cash': str(float(saved['cash']) + 0.001)}
        self.ledger.db.execute('UPDATE state SET payload=?', (json.dumps(broken),))
        with self.assertRaisesRegex(ValueError, 'checkpoint cash differs from fills'):
            pc.attribution(self.ledger.db, self.cfg)
        self.ledger.db.execute('UPDATE state SET payload=?', (json.dumps(saved),))
        self.ledger.db.execute("DELETE FROM outcomes WHERE json_extract(payload,'$.side')='buy' "
                               "AND json_extract(payload,'$.mint')='B'")
        with self.assertRaisesRegex(ValueError, 'sell without open entry'):
            pc.attribution(self.ledger.db, self.cfg)

    def test_cold_restart_with_three_open_positions_replays_without_duplicate_fills(self):
        events = []
        for i, mint in enumerate('ABC'):
            for h in 'ABC'[:i]:
                events.append(event(T + 60 * i - 1, mint=h))
            events.append(event(T + 60 * i, mint=mint))
        for e in events:
            self.apply(e)
        self.assertEqual(len(self.positions()), 3)
        before = (self.fills(), self.ledger.report(), pc.held_order(self.positions()))
        self.ledger.close()
        self.ledger = Ledger(self.path, must_exist=True)   # cold restart
        for e in reversed(events):                         # redelivery of every event, any order
            self.assertEqual(self.apply(e), [])
        self.assertEqual((self.fills(), self.ledger.report(), pc.held_order(self.positions())), before)
        self.assertEqual(pc.plan_legs(self.positions(), 12)[0], pc.held_order(self.positions())[:1])
        self.assertEqual(pc.attribution(self.ledger.db, self.cfg)['cash_sol'], before[1]['state']['cash'])


class RolloverTests(unittest.TestCase):
    """Three positions across UTC midnight, held legs 8 s apart, then the next entry decision."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'l.sqlite'

    @staticmethod
    def ev(ts, mint, **changes):
        return event(ts, mint=mint, graduated_at=ts - 600, **changes)    # the fixture's fixed token age spans only hours

    def book(self, **changes):
        cfg = cfg_on(**changes)
        ledger = Ledger(self.path)
        self.addCleanup(ledger.close)
        apply = lambda e: ledger.apply(e, cfg, transition, initial_state)
        base = T - 600                                        # 23:50 UTC on the previous day
        for i, mint in enumerate('ABC'):
            for held in 'ABC'[:i]:
                apply(self.ev(base + 60 * i - 1, held))
            self.assertEqual(apply(self.ev(base + 60 * i, mint))[0]['type'], 'fill')
        state = lambda: json.loads(ledger.db.execute('SELECT payload FROM state').fetchone()[0])
        return apply, state

    def legs_after_midnight(self, apply):
        out = []
        for i, mint in enumerate('ABC'):                      # real legs: 7.8 s each, oldest mark first
            out += apply(self.ev(T + 10 + 8 * i, mint))
        return out

    def test_rollover_fires_at_the_first_entry_after_all_legs_with_the_portfolio_ttl(self):
        apply, state = self.book()
        before = state()
        legs = self.legs_after_midnight(apply)
        # Pure held legs cannot roll the day over (each leg's own mark is old when it is evaluated):
        # the pre-existing behaviour with one position too. The counters only matter at an entry.
        self.assertEqual(state()['day'], before['day'])
        self.assertTrue(any(o.get('reason') == 'DAY_ROLLOVER_DEFERRED_UNVERIFIED_MARKS' for o in legs))
        entry = apply(self.ev(T + 57, 'D'))                   # held-first legs (31 s) + preparation + quotes
        self.assertEqual([o['type'] for o in entry][-1], 'fill', entry)
        after = state()
        self.assertNotEqual(after['day'], before['day'])
        self.assertEqual(after['day_gross_losses'], '0')
        self.assertEqual(len(after['positions']), 4)

    def test_without_the_portfolio_ttl_the_same_book_stays_stuck_on_yesterday_and_rejects(self):
        apply, state = self.book(**{TTL: config()['price_ttl_seconds']})
        before = state()
        self.legs_after_midnight(apply)
        entry = apply(self.ev(T + 57, 'D'))
        self.assertEqual((entry[-1]['type'], entry[-1]['reason']), ('reject', 'STALE_PORTFOLIO'))
        self.assertEqual(state()['day'], before['day'])        # the daily counters stay stale while 3 positions are held


class RolloverAfterMarkTests(RolloverTests):
    """T16G item 3: `paper_rollover_after_mark_version: 1` lets the held pass's final leg roll the day over."""
    ROLL = {'paper_rollover_after_mark_version': 1}

    def test_default_cfg_is_unchanged(self):
        apply, state = self.book()
        before = state()
        self.legs_after_midnight(apply)
        self.assertEqual(state()['day'], before['day'])           # key absent: byte-identical old behaviour

    def roll_case(self, mode=None, cap=None):
        changes = dict(self.ROLL)
        if cap:
            changes['max_positions'] = cap
        apply, state = self.book(**changes)
        before = state()
        if mode:
            apply(control(T + 1, mode))
        out, days = [], []
        for i, mint in enumerate('ABC'):
            out += apply(self.ev(T + 10 + 8 * i, mint))        # the held pass: 3 real legs, oldest mark first
            days.append(state()['day'])
        after = state()
        self.assertEqual(days[:2], [before['day']] * 2)         # not before every other mark is fresh
        self.assertNotEqual(days[2], before['day'])             # the FINAL leg rolls the day over
        self.assertEqual(after['day_gross_losses'], '0')
        self.assertEqual(after['day_start_equity'], str(equity(after)))
        self.assertTrue(any(o.get('reason') == 'DAY_ROLLOVER_AFTER_FRESH_MARKS' for o in out))
        self.assertEqual(len(after['positions']), 3)
        return after

    def test_book_full_no_candidate(self):
        self.roll_case(cap=3)

    def test_exit_only_mode(self):
        self.assertEqual(self.roll_case(mode='EXIT_ONLY')['mode'], 'EXIT_ONLY')

    def test_running_book_with_room(self):
        self.roll_case()

    def test_a_stale_other_mark_still_defers(self):
        apply, state = self.book(**self.ROLL)
        before = state()
        for i, mint in enumerate('AB'):                           # C never refreshed (a failed leg)
            apply(self.ev(T + 10 + 8 * i, mint))
        out = apply(self.ev(T + 26, 'A'))
        self.assertEqual(state()['day'], before['day'])
        self.assertFalse(any(o.get('reason') == 'DAY_ROLLOVER_AFTER_FRESH_MARKS' for o in out))

    def test_rule_requires_the_experiment_and_a_valid_value(self):
        for bad in ({'paper_rollover_after_mark_version': 2}, {'paper_rollover_after_mark_version': True},
                    {'paper_rollover_after_mark_version': '1'}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                pc.selected(cfg_on(**bad))
        with self.assertRaises(ValueError):
            pc.selected({**config(), 'paper_rollover_after_mark_version': 1})
        self.assertEqual(pc.selected(cfg_on(**self.ROLL)), 1)


# the inherited RolloverTests would run twice; only this class's own tests belong here
for _name in dir(RolloverTests):
    if _name.startswith('test_') and _name not in vars(RolloverAfterMarkTests):
        setattr(RolloverAfterMarkTests, _name, None)


class SchedulerTickTests(unittest.TestCase):
    """Entry mode with the lease, real wrapper, synthetic protected files; commands are probes."""

    def setUp(self):
        from tools import paper_scheduler as wrapper
        self.wrapper = wrapper
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.research = self.root / 'research.sqlite'
        self.ledger = self.root / 'ledger.sqlite'
        for p in (self.research, self.ledger):
            p.touch()
        lock = self.root / 'paper-scheduler.lock'
        lock.touch(mode=0o600)
        info = lock.stat()
        env = patch.dict(os.environ, {'DESK_PAPER_SCHEDULER_IDENTITY': f'{info.st_dev}:{info.st_ino}'})
        env.start()
        self.addCleanup(env.stop)
        self.cfg_path = self.root / 'config.json'
        self.states = [state()]
        self.calls = []
        patcher = patch.object(wrapper.paper_cycle, '_state', side_effect=lambda *_: copy.deepcopy(self.states[0]))
        patcher.start()
        self.addCleanup(patcher.stop)

    def contender(self):
        import subprocess
        import sys
        code = ("from desk.paper_scheduler import lease\nimport sys\n"
                "with lease(sys.argv[1]) as fd:print('BUSY' if fd is None else 'ACQUIRED')")
        return subprocess.check_output([sys.executable, '-c', code, str(self.research)], text=True).strip()

    def run_entry(self, cfg, held_code=0, held_effect=None, now=T):
        self.cfg_path.write_text(canonical(cfg))
        args = ['--research-db', str(self.research), '--mode', 'entry', '--',
                '--config', str(self.cfg_path), '--research-db', str(self.research),
                '--evidence-db', str(self.root / 'e.sqlite'), '--ledger-db', str(self.ledger), '--pool-fee-bps', '25']

        def held(argv):
            self.calls.append(('held', argv))
            self.assertEqual(self.contender(), 'BUSY')       # the lease is held through the held pass
            print('held-pass-output-must-not-leak')
            if held_effect:
                held_effect()
            return held_code

        def entry(argv):
            self.calls.append(('entry', argv))
            self.assertEqual(self.contender(), 'BUSY')       # ...and through the entry dispatch
            return 0

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('desk.paper_monitor_service.main', side_effect=held), \
                patch('tools.paper_entry_dispatcher.main', side_effect=entry), \
                patch.object(pc, 'clock', return_value=now):
            code = self.wrapper.main(args)
        self.assertEqual(code, 0)
        self.assertEqual(self.contender(), 'ACQUIRED')
        return out.getvalue()

    def test_default_off_is_unchanged_held_priority_and_never_runs_a_held_pass(self):
        self.states = [state({'A': position(mark_at=T)})]
        out = self.run_entry(config())
        self.assertEqual(out, '{"status": "HELD_POSITION_PRIORITY", "attempted_requests": 0, "entry_authorized": false}\n')
        self.assertEqual(self.calls, [])

    def test_flat_ledger_goes_straight_to_the_dispatcher(self):
        self.run_entry(cfg_on())
        self.assertEqual([c[0] for c in self.calls], ['entry'])

    def test_held_pass_runs_first_for_open_positions_then_entry(self):
        self.states = [state({'A': position(mark_at=T), 'B': position(mark_at=T)})]
        out = self.run_entry(cfg_on())
        self.assertEqual([c[0] for c in self.calls], ['held', 'entry'])
        held_args = self.calls[0][1]
        for name in ('--config', '--research-db', '--evidence-db', '--ledger-db', '--pool-fee-bps'):
            self.assertEqual(held_args.count(name), 1)
        self.assertEqual(out, '')   # the held pass output is captured, not mixed into the tick output

    def test_degraded_held_pass_blocks_entry_without_any_request(self):
        self.states = [state({'A': position(mark_at=T)})]
        out = json.loads(self.run_entry(cfg_on(), held_code=2))
        self.assertEqual((out['status'], out['attempted_requests'], out['entry_authorized']),
                         ('HELD_MONITORING_DEGRADED', 0, False))
        self.assertEqual([c[0] for c in self.calls], ['held'])

    def test_unresolved_exit_after_the_held_pass_freezes_entries(self):
        self.states = [state({'A': position(mark_at=T)})]

        def freeze():
            self.states = [state({'A': position(mark_at=T, exit_blocked='EXACT_FRESH_SELL_QUOTE_REQUIRED')})]
        out = json.loads(self.run_entry(cfg_on(), held_effect=freeze))
        self.assertEqual((out['status'], out['blockers']), ('CONCURRENT_ENTRY_BLOCKED', ['HELD_EXIT_UNRESOLVED']))
        self.assertEqual([c[0] for c in self.calls], ['held'])

    def test_marks_too_old_for_the_engine_ttl_defer_the_entry(self):
        self.states = [state({'A': position(mark_at=T)})]
        out = json.loads(self.run_entry(cfg_on(), now=T + 100))   # 100 s + 30 s of entry work > 120 s TTL
        self.assertEqual(out['blockers'], ['PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'])
        self.assertEqual([c[0] for c in self.calls], ['held'])

    def test_full_book_and_paused_ledger_block_but_still_monitor(self):
        self.states = [state({m: position(mark_at=T) for m in 'ABCD'}, 'EXIT_ONLY')]
        out = json.loads(self.run_entry(cfg_on()))
        self.assertEqual(out['blockers'], ['LEDGER_MODE_NOT_RUNNING', 'MAX_POSITIONS_REACHED'])
        self.assertEqual([c[0] for c in self.calls], ['held'])


class HeldLeaseWaitTests(SchedulerTickTests):
    """A held tick arriving while an entry holds the lease: bounded wait instead of a skipped pass.

    The entry is a real thread holding the real flock for a scaled duration; the held pass is the real
    wrapper. Time constants are scaled down (seconds -> fractions of a second) but the mechanism is real.
    """

    def run_held(self, cfg):
        self.cfg_path.write_text(canonical(cfg))
        args = ['--research-db', str(self.research), '--mode', 'held', '--',
                '--config', str(self.cfg_path), '--research-db', str(self.research),
                '--evidence-db', str(self.root / 'e.sqlite'), '--ledger-db', str(self.ledger), '--pool-fee-bps', '25']
        started = []

        def held(argv):
            started.append(time.monotonic())
            return 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch('desk.paper_monitor_service.main', side_effect=held):
            self.assertEqual(self.wrapper.main(args), 0)
        return started, out.getvalue()

    def entry_holds_lease(self, seconds):
        from desk.paper_scheduler import lease
        ready, finished = threading.Event(), []

        def entry():
            with lease(self.research) as fd:
                self.assertIsNotNone(fd)
                ready.set()
                time.sleep(seconds)
                finished.append(time.monotonic())
        thread = threading.Thread(target=entry)
        thread.start()
        self.assertTrue(ready.wait(5))
        self.addCleanup(thread.join)
        return thread, finished

    def test_held_pass_starts_right_after_a_running_entry_instead_of_skipping_the_tick(self):
        with patch.object(self.wrapper, 'HELD_LEASE_WAIT_SECONDS', 5.0):
            thread, finished = self.entry_holds_lease(1.0)
            started, out = self.run_held(cfg_on())
            thread.join()
        self.assertEqual(len(started), 1, out)
        latency = started[0] - finished[0]
        self.assertGreaterEqual(latency, 0)                                  # strictly after the entry released the lease
        self.assertLess(latency, self.wrapper.LEASE_POLL_SECONDS + 0.5)      # within one poll of the release
        self.assertNotIn('SCHEDULER_BUSY', out)

    def test_wait_is_bounded_by_the_budget_and_reports_busy_like_before(self):
        with patch.object(self.wrapper, 'HELD_LEASE_WAIT_SECONDS', 0.6):
            thread, finished = self.entry_holds_lease(2.0)
            t0 = time.monotonic()
            started, out = self.run_held(cfg_on())
            waited = time.monotonic() - t0
            thread.join()
        self.assertEqual(started, [])
        self.assertIn('SCHEDULER_BUSY', out)
        self.assertGreaterEqual(waited, 0.6)
        self.assertLess(waited, 1.6)

    def test_default_off_never_waits_and_is_byte_identical(self):
        with patch.object(self.wrapper, 'HELD_LEASE_WAIT_SECONDS', 5.0):
            thread, finished = self.entry_holds_lease(0.8)
            t0 = time.monotonic()
            started, out = self.run_held(config())
            waited = time.monotonic() - t0
            thread.join()
        self.assertEqual(started, [])
        self.assertLess(waited, 0.5)
        self.assertEqual(out, '{"status": "SCHEDULER_BUSY", "attempted_requests": 0, "entry_authorized": false}\n')

    def test_unreadable_config_never_waits(self):
        self.cfg_path.write_text('not json')
        self.assertEqual(self.wrapper._held_wait(lambda name: str(self.cfg_path)), 0.0)
        self.assertEqual(self.wrapper._held_wait(lambda name: (_ for _ in ()).throw(ValueError('missing'))), 0.0)


class MonitorLegTests(unittest.TestCase):
    def setUp(self):
        from desk import paper_monitor_service as service
        self.service = service
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.ledger = self.root / 'ledger.sqlite'
        self.ledger.touch()
        self.mints = ['A' * 32, 'B' * 32, 'C' * 32]
        self.positions = {self.mints[0]: position(mark_at=300), self.mints[1]: position(mark_at=100),
                          self.mints[2]: position(mark_at=200)}

    def run_service(self, cfg, wall=12.0, positions=None, checkpoint=None, codes=None):
        path = self.root / 'config.json'
        path.write_text(canonical(cfg))
        positions = self.positions if positions is None else positions
        checkpoint = positions if checkpoint is None else checkpoint
        seen, codes = [], list(codes or [])

        def export(research, evidence, output, **kw):
            rows = [{'scan_id': 's-' + m, 'mint': m, 'pool': 'p', 'taker': 't', 'amount_raw': 1,
                     'provenance': 'SYNTHETIC_TEST_ONLY', 'pool_fee_bps': '25', 'graduation_refs': [],
                     'known_hazards': []} for m in positions]
            Path(output).write_text(json.dumps({'position_targets': rows, 'candidates': [], 'usd_evidence_refs': []}))
            return {'status': 'EXPORTED', 'positions': len(rows)}

        def cycle_cli(argv):
            seen.append(json.loads(Path(argv[argv.index('--targets') + 1]).read_text()))
            return codes.pop(0) if codes else 0
        args = ['--config', str(path), '--research-db', 'r', '--evidence-db', 'e', '--ledger-db', str(self.ledger),
                '--pool-fee-bps', '25'] + ([] if wall is None else ['--wall-seconds', str(wall)])
        with patch.object(self.service, 'export_targets', side_effect=export), \
                patch.object(self.service.cli, 'main', side_effect=cycle_cli), \
                patch('desk.paper_cycle._state', return_value={'positions': checkpoint}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = self.service.main(args)
        return code, seen, out.getvalue()

    def test_default_off_runs_one_pass_with_every_position(self):
        code, seen, _ = self.run_service(config())
        self.assertEqual((code, len(seen), len(seen[0]['position_targets'])), (0, 1, 3))

    def test_flag_on_runs_least_recently_marked_first_within_wall_budget(self):
        code, seen, out = self.run_service(cfg_on(), wall=12.0)
        self.assertEqual(code, 0)
        self.assertEqual([[t['mint'] for t in s['position_targets']] for s in seen], [[self.mints[1]]])
        self.assertEqual(json.loads(out)['legs_deferred'], 2)
        code, seen, out = self.run_service(cfg_on(), wall=40.0)
        self.assertEqual([[t['mint'] for t in s['position_targets']] for s in seen],
                         [[self.mints[1]], [self.mints[2]], [self.mints[0]]])
        self.assertEqual(json.loads(out)['legs_deferred'], 0)

    def test_flag_on_default_covers_every_open_position_oldest_mark_first(self):
        code, seen, out = self.run_service(cfg_on(), wall=None)
        self.assertEqual(code, 0)
        self.assertEqual([[t['mint'] for t in s['position_targets']] for s in seen],
                         [[self.mints[1]], [self.mints[2]], [self.mints[0]]])
        self.assertEqual((json.loads(out)['legs_run'], json.loads(out)['legs_deferred']), (3, 0))

    def test_invalid_wall_budgets_fail_closed_without_running_a_leg(self):
        for bad in (0, -1, 3601, float('nan')):
            code, seen, out = self.run_service(cfg_on(), wall=bad)
            self.assertEqual((code, seen), (2, []), bad)
            self.assertIn('UNAVAILABLE', out)

    def test_every_leg_is_positions_only_and_a_failed_leg_does_not_hide_others(self):
        code, seen, _ = self.run_service(cfg_on(), wall=40.0)
        self.assertTrue(all(s['candidates'] == [] and s['usd_evidence_refs'] == [] for s in seen))
        code, seen, _ = self.run_service(cfg_on(), wall=40.0, codes=[2, 0, 0])
        self.assertEqual((code, len(seen)), (2, 3))

    def test_export_that_disagrees_with_the_checkpoint_fails_closed(self):
        extra = {**self.positions, 'D' * 32: position()}
        code, seen, out = self.run_service(cfg_on(), positions=self.positions, checkpoint=extra)
        self.assertEqual((code, seen), (2, []))
        self.assertIn('UNAVAILABLE', out)


class CycleGateTests(unittest.TestCase):
    """The real cycle: the per-item held-mint gate and the all-positions rule, flag on and off."""

    def fixture(self, **extra):
        from desk import paper_cycle as cycle
        from tests.test_paper_cycle import PaperCycleTests
        f = PaperCycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        if extra:  # a concurrent-entries experiment is a NEW ledger/config version
            f.cfg = {**f.cfg, **extra}
            f.path = Path(f.f.tmp.name) / 'concurrent.sqlite'
            cycle.initialize(f.path, f.cfg)
        f.http_calls = []
        f.sell_output = 10_000_000
        return f

    def second_pass(self, f):
        entry = f.actual_cycle()
        self.assertEqual(entry['status'], 'COMPLETE', entry)
        self.assertTrue(any(x.get('side') == 'buy' for x in entry['outcomes']))
        return f.actual_cycle(positions=(), candidates=(f.item,))

    def test_default_off_still_requires_every_open_position_target(self):
        result = self.second_pass(self.fixture())
        self.assertEqual(result['blockers'], ['ALL_OPEN_POSITION_TARGETS_REQUIRED'])
        self.assertEqual(result['attempted_requests'], 0)

    def test_flag_on_allows_a_candidate_pass_but_never_a_second_entry_into_a_held_mint(self):
        f = self.fixture(**{KEY: 1, TTL: 120})
        result = self.second_pass(f)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertNotIn('ALL_OPEN_POSITION_TARGETS_REQUIRED', result['blockers'])
        self.assertEqual(result['diagnostics'][0]['blockers'], ['ENTRY_CONTROL_OR_EXISTING_POSITION'])
        self.assertFalse(any(x['type'] == 'fill' for x in result['outcomes']))
        self.assertEqual(result['attempted_requests'], 0)

    def test_flag_on_still_rejects_duplicate_position_targets(self):
        f = self.fixture(**{KEY: 1, TTL: 120})
        entry = f.actual_cycle()
        self.assertEqual(entry['status'], 'COMPLETE', entry)
        duplicate = f.actual_cycle(positions=(f.item, f.item), candidates=())
        self.assertEqual(duplicate['blockers'], ['ALL_OPEN_POSITION_TARGETS_REQUIRED'])
        from dataclasses import replace
        not_held = replace(f.item, target=replace(f.target, mint=f.target.pool))
        stranger = f.actual_cycle(positions=(not_held,), candidates=())
        # Rejected before the subset rule: no persisted admission binds this mint (defence in depth;
        # the subset rule itself is only distinguishable for an admitted-but-unheld mint, which this
        # single-scan fixture cannot construct).
        self.assertEqual(stranger['blockers'], ['PERSISTED_ADMISSION_OR_SCHEMA_REQUIRED'])
        self.assertFalse(any(x['type'] == 'fill' for x in stranger['outcomes']))


class DispatcherPreflightTests(unittest.TestCase):
    """_preflight with one open position: budget reserve and mark freshness are checked before any I/O."""

    def fixture(self):
        from tests import test_paper_entry_dispatcher as module
        with patch.object(module, 'config', lambda: {**config(), KEY: 1, TTL: 120}):
            f = module.DispatcherTests()
            f.setUp()
        self.addCleanup(f.doCleanups)
        self.module = module
        result = f.live()
        self.assertEqual(result['paper_status'], 'COMPLETE', result)
        return f

    def test_flag_on_preflight_enforces_reserve_and_mark_freshness(self):
        f = self.fixture()
        tool = self.module.tool
        ctx = tool.plan(**f.args)
        full = {'status': 'AVAILABLE', 'blockers': [], 'remaining': 3600}
        fresh = f.f.at
        with patch.object(tool.monitor.MonitoringBudget, 'snapshot', return_value=full), \
                patch.object(tool.concurrency, 'clock', return_value=fresh):
            tool._preflight(ctx)  # held position + fresh mark + ample allowance: permitted
        with patch.object(tool.monitor.MonitoringBudget, 'snapshot', return_value=full), \
                patch.object(tool.concurrency, 'clock', return_value=fresh + 100):
            with self.assertRaisesRegex(ValueError, 'PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'):
                tool._preflight(ctx)
        with patch.object(tool.monitor.MonitoringBudget, 'snapshot', return_value={**full, 'remaining': 60}), \
                patch.object(tool.concurrency, 'clock', return_value=fresh):
            with self.assertRaisesRegex(ValueError, 'MONITORING_RESERVE_INSUFFICIENT'):
                tool._preflight(ctx)

    def test_pre_io_estimate_applies_only_before_the_irreversible_dispatch_intent(self):
        """A refusal raised after charges would leave an unresolved intent (permanent latch): later calls skip it."""
        f = self.fixture()
        tool = self.module.tool
        ctx = tool.plan(**f.args)
        with f.f.jobs.connect() as c:
            scan = c.execute('SELECT id FROM scans').fetchone()[0]
        starved = {'status': 'AVAILABLE', 'blockers': [], 'remaining': 60}
        with patch.object(tool.monitor.MonitoringBudget, 'snapshot', return_value=starved), \
                patch.object(tool.concurrency, 'clock', return_value=f.f.at + 10_000):
            with self.assertRaisesRegex(ValueError, 'Concurrent entry refused'):
                tool._preflight(ctx)                      # before the intent: refused with zero requests
            with patch.object(tool.concurrency, 'entry_blockers', side_effect=AssertionError('estimate re-run after charges')):
                tool._preflight(ctx, scan)                # after the intent: the engine alone decides


if __name__ == '__main__':
    unittest.main()
