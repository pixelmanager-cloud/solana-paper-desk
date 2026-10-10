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
import unittest
from pathlib import Path
from unittest.mock import patch

from desk import paper_concurrency as pc
from desk.engine import initial_state, transition
from desk.ledger import Ledger
from desk.model import canonical
from tests.helpers import T, config, event

KEY = pc.KEY


def cfg_on(**changes):
    return {**config(), KEY: 1, **changes}


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

    def test_portfolio_freshness_matches_engine_price_ttl(self):
        cfg = cfg_on()
        s = state({'A': position(mark_at=1000)})
        ttl = cfg['price_ttl_seconds']
        # Mark age at decision = (now - mark) + entry seconds; refused once it exceeds the engine TTL.
        edge = 1000 + int(ttl - pc.ENTRY_SECONDS)
        self.assertEqual(pc.entry_blockers(s, cfg, now=edge), [])
        self.assertEqual(pc.entry_blockers(s, cfg, now=edge + 1), ['PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'])
        self.assertEqual(pc.entry_blockers(state(), cfg, now=10**9), [])  # flat ledgers have no marks

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

    def test_engine_rejects_entry_when_a_held_mark_is_stale(self):
        """The binding constraint: every held mark must be within price_ttl at the entry decision."""
        self.enter('A', T)
        reject = self.apply(event(T + 60, mint='B'))[0]
        self.assertEqual((reject['type'], reject['reason']), ('reject', 'STALE_PORTFOLIO'))
        self.assertEqual(set(self.positions()), {'A'})
        self.assertEqual(pc.entry_blockers(state(self.positions()), self.cfg, now=T + 60), [
            'PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'])  # refused before spending any investigation request

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
        out = json.loads(self.run_entry(cfg_on(), now=T + 30))
        self.assertEqual(out['blockers'], ['PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'])
        self.assertEqual([c[0] for c in self.calls], ['held'])

    def test_full_book_and_paused_ledger_block_but_still_monitor(self):
        self.states = [state({m: position(mark_at=T) for m in 'ABCD'}, 'EXIT_ONLY')]
        out = json.loads(self.run_entry(cfg_on()))
        self.assertEqual(out['blockers'], ['LEDGER_MODE_NOT_RUNNING', 'MAX_POSITIONS_REACHED'])
        self.assertEqual([c[0] for c in self.calls], ['held'])


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
                '--pool-fee-bps', '25', '--wall-seconds', str(wall)]
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
        f = self.fixture(**{KEY: 1})
        result = self.second_pass(f)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertNotIn('ALL_OPEN_POSITION_TARGETS_REQUIRED', result['blockers'])
        self.assertEqual(result['diagnostics'][0]['blockers'], ['ENTRY_CONTROL_OR_EXISTING_POSITION'])
        self.assertFalse(any(x['type'] == 'fill' for x in result['outcomes']))
        self.assertEqual(result['attempted_requests'], 0)

    def test_flag_on_still_rejects_duplicate_position_targets(self):
        f = self.fixture(**{KEY: 1})
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
        with patch.object(module, 'config', lambda: {**config(), KEY: 1}):
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
                patch.object(tool.concurrency, 'clock', return_value=fresh + 60):
            with self.assertRaisesRegex(ValueError, 'PORTFOLIO_MARKS_TOO_OLD_FOR_ENTRY'):
                tool._preflight(ctx)
        with patch.object(tool.monitor.MonitoringBudget, 'snapshot', return_value={**full, 'remaining': 60}), \
                patch.object(tool.concurrency, 'clock', return_value=fresh):
            with self.assertRaisesRegex(ValueError, 'MONITORING_RESERVE_INSUFFICIENT'):
                tool._preflight(ctx)


if __name__ == '__main__':
    unittest.main()
