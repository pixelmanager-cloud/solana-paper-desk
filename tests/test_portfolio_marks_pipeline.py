"""SYNTHETIC_TEST_ONLY: batched portfolio marks through the REAL dispatcher -> cycle -> engine pipeline.

paper_portfolio_mark_source_version 1 with the held-mark TTL left at the 10 s price TTL (no relaxation). The stores come
from tools.ops.fresh_start; the wire is the repository's synthetic bytes (a getMultipleAccounts answer is built from the
open positions' real PDAs). The data clock moves between the steps like a real tick; nothing is refreshed by hand.
"""
import base64
import copy
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from desk import paper_concurrency as pc, paper_cycle as cycle, portfolio_marks as pm
from desk import paper_read_sources as transport
from desk.model import canonical
from desk.providers import PUMPSWAP, SOL
from tests import test_ops_fresh_start_lifecycle as lifecycle
from tests import test_paper_concurrency_pipeline as pipe
from tests import test_paper_entry_dispatcher as base
from tools import paper_entry_dispatcher as tool

b64 = lambda raw: base64.b64encode(raw).decode()


def marks_cycle(h, *, positions=(), candidates=None, usd_refs=(), monitoring=False):
    """`tests.test_kraken_lifecycle.actual_cycle` plus one branch: the batched marks request is answered from the fake chain."""
    from contextlib import ExitStack
    from urllib.parse import parse_qs, urlsplit
    from desk.programs import unbase58
    from desk.security import base58
    from tests.test_live_strategy_features import transaction, POOL as TRADE_POOL
    from tests.test_paper_read_sources import Response
    outer = h

    class Opener:
        def open(self, request, *, timeout):
            outer.http_calls.append(request)
            if request.method == 'POST':
                call = json.loads(request.data)
                method, params = call['method'], call['params']
                if method == 'getMultipleAccounts' and params[1].get('minContextSlot') == 0:
                    outer.chain.marks_calls.append((outer.f.at, params[0]))
                    if outer.chain.marks_failure:
                        raise ConnectionResetError('SYNTHETIC_TEST_ONLY marks transport failure')
                    result = {'context': {'slot': 900 + len(outer.chain.marks_calls)}, 'value': outer.chain.marks_answer(params[0])}
                elif method == 'getSlot':
                    result = 110
                elif method == 'getBlockTime':
                    result = outer.f.at - 1
                elif method == 'getTransactionsForAddress':
                    rows = []
                    for i in range(40):
                        row = transaction('cycle-' + str(i), 10 + i, outer.f.at - 1, quote=200, base=100,
                                          wallet=base58((i + 1).to_bytes(32, 'big')))
                        ix = row['transaction']['message']['instructions'][0]
                        ix['data'] = base58(unbase58(ix['data']).replace(unbase58(TRADE_POOL), unbase58(outer.target.pool)))
                        rows.append(row)
                    result = {'data': rows, 'paginationToken': None}
                else:
                    result = copy.deepcopy(outer.f.protocol.rpc(method, params))
                    if method == 'getAccountInfo' and params[0] == outer.target.mint:
                        result = {'context': {'slot': 100}, 'value': copy.deepcopy(outer.f.protocol.rpc('getMultipleAccounts', [])['value'][6])}
                    if method == 'getMultipleAccounts':
                        for index, amount in ((0, 10 ** 12), (1, 10 ** 11)):
                            raw = bytearray(base64.b64decode(result['value'][index]['data'][0]))
                            raw[64:72] = amount.to_bytes(8, 'little')
                            result['value'][index]['data'][0] = base64.b64encode(raw).decode()
                        accounts = [result['value'][6]]
                    elif params[0] == outer.target.mint:
                        accounts = [result['value']]
                    else:
                        accounts = []
                    for account in accounts:
                        raw = bytearray(base64.b64decode(account['data'][0]))
                        raw[36:44] = (10 ** 13).to_bytes(8, 'little')
                        account['data'][0] = base64.b64encode(raw).decode()
                wire = canonical({'jsonrpc': '2.0', 'id': transport.RPC_ID, 'result': result}).encode()
            elif 'api.kraken.com/0/public/Trades?' in request.full_url:
                wire = ('{"error":[],"result":{"SOLUSD":[["100.00000","1.00000",' + str(outer.f.at - 1)
                        + '.25,"s","l","",123]],"last":"123000000000"}}').encode()
            elif '/price/v3?' in request.full_url:
                wire = canonical({SOL: {'usdPrice': 100, 'blockId': 100, 'decimals': 9}}).encode()
            else:
                q = {k: v[0] for k, v in parse_qs(urlsplit(request.full_url).query).items()}
                output = getattr(outer, 'buy_output_raw', 1_000_000) if q['inputMint'] == SOL else int(q['amount']) * 8
                wire = canonical({'inputMint': q['inputMint'], 'outputMint': q['outputMint'], 'swapMode': 'ExactIn',
                    'inAmount': q['amount'], 'outAmount': str(output), 'otherAmountThreshold': str(output * 99 // 100),
                    'slippageBps': 100, 'routePlan': [{'percent': 100, 'swapInfo': {'inputMint': q['inputMint'],
                    'outputMint': q['outputMint'], 'inAmount': q['amount'], 'outAmount': str(output), 'ammKey': outer.target.pool}}]}).encode()
            return Response(wire)
    with ExitStack() as stack:
        stack.enter_context(patch.object(transport.os.environ, 'get', return_value='SYNTHETIC_TEST_ONLY'))
        stack.enter_context(patch.object(transport, 'build_opener', return_value=Opener()))
        stack.enter_context(patch.object(cycle.time, 'time', return_value=h.f.at))
        stack.enter_context(patch.object(cycle.time, 'monotonic', return_value=1))
        return h.run_cycle(position_targets=positions, candidates=(h.item,) if candidates is None else candidates,
                           source_factory=transport.PaperReadSources, usd_evidence_refs=usd_refs, monitoring=monitoring)


class MarksPipeline(pipe.ConcurrentPipeline):
    marks_flag = True
    ttl = None

    def setUp(self):
        value = json.loads(lifecycle.EXAMPLE.read_text())
        value[pc.KEY], value['max_positions'] = 1, 4
        if self.marks_flag:
            value[pm.KEY], value[pm.FEE_KEY] = 1, '25'
        else:
            value[pc.TTL_KEY] = 120
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        path = folder / 'marks-experiment.json'
        path.write_text(json.dumps(value))
        with patch.object(lifecycle, 'EXAMPLE', path):
            lifecycle.FreshFixture.setUp(self)
        self.assertEqual(pc.selected(self.cfg), 1)
        self.second_raw, self.entered, self.preflight_at = None, {}, None
        self.reserves = {}                 # mint -> (base_raw, quote_raw) served by the fake chain
        self.marks_calls = []              # every getMultipleAccounts marks request seen on the wire
        self.marks_failure = None
        self.real_run_once = cycle.run_once

    # -- the fake chain answer for one batched marks request ---------------------------------------------------------------
    def marks_answer(self, keys):
        values = []
        for i in range(0, len(keys), 3):
            proto = next(p for _, p in self.entered.values() if str(p.pool) == keys[i])
            mint = str(proto.mint)
            base_raw, quote_raw = self.reserves.get(mint, (10 ** 12, 4 * 10 ** 12))
            def token(m, amount):
                d = bytearray(165)
                d[:32], d[32:64], d[64:72], d[108] = bytes(m), bytes(proto.pool), amount.to_bytes(8, 'little'), 1
                return {'owner': pm.TOKEN_PROGRAM, 'executable': False, 'data': [b64(bytes(d)), 'base64']}
            values += [{'owner': PUMPSWAP, 'executable': False, 'data': [b64(proto.raw), 'base64']},
                       token(proto.mint, base_raw), token(proto.Pubkey.from_string(SOL), quote_raw)]
        return values

    def dispatch(self, *, intents, raw, protocol=None):
        outer = self
        proto = protocol or self.second
        pool_of_candidate = str(proto.pool)

        def setup_rpc(method, params):
            outer.assertEqual(outer.count('intents'), intents, 'Durable intent must precede I/O')
            outer.calls.append(method)
            if method == 'getAccountInfo':
                return {'value': copy.deepcopy(outer.f.protocol.rpc('getMultipleAccounts', [])['value'][6])}
            if method == 'getSlot':
                return 120
            outer.assertEqual(method, 'getTransactionsForAddress')
            return {'data': []}

        class Opener:
            def open(self, request, *, timeout):
                call = json.loads(request.data)
                outer.calls.append('intake')
                outer.assertEqual(outer.count('intents'), intents)
                rows = [raw]
                if call['params'][1]['filters'].get('slot') != {'gte': 10, 'lt': 11}:
                    rows = []
                    for i in range(40):
                        row = base.transaction('dispatch-flow-' + str(i), 10 + i, outer.f.at - 1, quote=200, base=100,
                                               wallet=base.base58((i + 1).to_bytes(32, 'big')))
                        ix = row['transaction']['message']['instructions'][0]
                        ix['data'] = base.base58(base.unbase58(ix['data']).replace(base.unbase58(base.TRADE_POOL),
                                                                                   base.unbase58(pool_of_candidate)))
                        rows.append(row)
                return base.Response(canonical({'jsonrpc': '2.0', 'id': 'paper-read-v1',
                                                'result': {'data': rows, 'paginationToken': None}}).encode())
        original = cycle.run_once
        budget = tool.monitor.MonitoringBudget

        def entry_cycle(research, evidence, ledger, cfg, **kw):
            item = kw['candidates'][0]
            view = SimpleNamespace(at=outer.f.at, tick=outer.f.tick, protocol=proto)
            h = SimpleNamespace(f=view, target=item.target, item=item, path=outer.ledger, cfg=outer.cfg,
                                http_calls=[], sell_output=100_000_000, buy_output_raw=10_000_000)

            def run(**args):
                return original(research, evidence, ledger, cfg, wall_clock=lambda: outer.f.at,
                                monotonic=lambda: outer.f.tick, dependency_blockers=(), **args)
            h.run_cycle = run
            h.chain = outer
            result = marks_cycle(h, candidates=(item,))
            outer.cycle_result = result
            outer.entered[item.target.mint] = (item, proto)
            return result
        with (patch.object(tool.cli, '_credentials'), patch('desk.providers.helius_rpc', side_effect=setup_rpc),
              patch.object(transport, 'build_opener', return_value=Opener()),
              patch.dict('os.environ', {'HELIUS_API_KEY': 'SYNTHETIC', 'JUPITER_API_KEY': 'SYNTHETIC'}),
              patch.object(cycle, 'run_once', side_effect=entry_cycle), patch.object(tool.entry.time, 'sleep'),
              patch.object(tool.time, 'time', side_effect=lambda: self.f.at),
              patch.object(tool.monitor, 'MonitoringBudget',
                           side_effect=lambda store, ledger, cfg: budget(store, ledger, cfg, clock=lambda: max(self.f.at, pipe.REAL_TIME()))),
              patch.object(tool.concurrency, 'clock', side_effect=lambda: self.f.at)):
            return self.invoke(execute=True, systemd_credentials=True)

    def held_leg(self, mint, *, sell_output=82_000_000):
        with patch.object(cycle, 'run_once', self.real_run_once):
            return super().held_leg(mint, sell_output=sell_output)

    def idle(self, seconds=65):
        self.f.at += seconds

    def open_positions(self, count):
        self.first_entry()
        for n in range(2, count + 1):
            self.append_second_candidate(seed=20 + n)
            self.idle()
            result = self.dispatch(intents=n, raw=self.second_raw)
            self.assertEqual((result['status'], result['paper_status']), ('DISPATCHED', 'COMPLETE'), result)
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), count)

    def append_second_candidate(self, seed=17):
        original = self.append_distinct_migration
        with patch.object(self, 'append_distinct_migration', lambda **kw: original(seed=seed, **kw)):
            return super().append_second_candidate()

    def ledger_events(self, kind):
        with sqlite3.connect(self.ledger) as c:
            return [json.loads(p) for (p,) in c.execute('SELECT payload FROM events ORDER BY rowid') if json.loads(p).get('kind') == kind]

    def outcomes(self):
        with sqlite3.connect(self.ledger) as c:
            return [json.loads(p) for (p,) in c.execute('SELECT payload FROM outcomes ORDER BY rowid')]


class FourPositions(MarksPipeline):
    def test_four_open_positions_all_marks_within_10s_with_one_request_per_refresh(self):
        before = None
        self.first_entry()
        for n in range(2, 5):
            self.append_second_candidate(seed=20 + n)
            self.idle()                                       # every earlier executable mark is now far older than 10 s
            positions = cycle._state(self.ledger, self.cfg)['positions']
            self.assertTrue(all(self.f.at - p['mark_at'] > self.cfg['price_ttl_seconds'] for p in positions.values()))
            reserved = self.monitoring_rows()
            calls = len(self.marks_calls)
            result = self.dispatch(intents=n, raw=self.second_raw)
            self.assertEqual((result['status'], result['paper_status']), ('DISPATCHED', 'COMPLETE'), result)
            # two refreshes per entry (before planning and again after the quote reads), each ONE request for all positions
            self.assertEqual(len(self.marks_calls) - calls, 2)
            self.assertEqual([len(keys) for _, keys in self.marks_calls[calls:]], [3 * (n - 1)] * 2)
            rows = self.monitoring_rows()
            self.assertEqual((rows[0] - reserved[0], rows[1] - reserved[1]), (2, 2))
        state = cycle._state(self.ledger, self.cfg)
        self.assertEqual(len(state['positions']), 4)
        marks = self.ledger_events('portfolio_marks')
        self.assertEqual(len(marks), 6)
        last = marks[-1]
        self.assertEqual(len(last['marks']), 3)
        self.assertLessEqual(last['ts'] - last['observed_at'], 10)
        fills = [o for o in self.outcomes() if o.get('type') == 'fill' and o['side'] == 'buy']
        self.assertEqual(len(fills), 4)
        # the held positions' executable marks stayed untouched: only valuation moved
        self.assertTrue(all(p['mark_at'] < last['observed_at'] for m, p in state['positions'].items() if m in last['marks']))
        self.assertEqual(self.null_passes(), [])
        self.assertFalse(self.gate())

    def test_each_refresh_is_retained_original_evidence_and_replays_identically(self):
        self.open_positions(2)
        event = self.ledger_events('portfolio_marks')[-1]
        record = self.f.progress.store.load(event['evidence_hash'])
        self.assertEqual((record['method'], record['params'][0]), ('getMultipleAccounts', pm.request_keys(
            {m: cycle._state(self.ledger, self.cfg)['positions'][m] for m in cycle._state(self.ledger, self.cfg)['positions'] if m in event['marks']})))
        self.assertEqual(record['observed_at'], event['observed_at'])
        reply = json.loads(base64.b64decode(record['response_bytes_base64']))['result']
        self.assertEqual(reply['context']['slot'], event['slot'])
        pm.validate_event(event)


class FailureAndFlagTests(MarksPipeline):
    def test_a_failed_marks_read_is_charged_and_ends_as_a_normal_stale_reject(self):
        self.first_entry()
        self.append_second_candidate(seed=22)
        self.idle()
        before = (self.monitoring_rows(), self.charges())
        self.marks_failure = True
        result = self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(result['status'], 'DISPATCHED')
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 1)
        reasons = [o.get('reason') for o in self.outcomes()]
        self.assertIn('STALE_PORTFOLIO', reasons)
        rows = self.monitoring_rows()
        self.assertEqual((rows[0] - before[0][0], rows[1] - before[0][1]), (1, 1), 'the failed attempt stays charged, with its outcome retained')
        self.assertEqual(self.null_passes(), [], 'a stale reject is terminal, never a store-wide latch')
        self.assertFalse(self.gate())
        self.assertEqual(self.ledger_events('portfolio_marks'), [])

    def test_a_position_whose_vault_fails_verification_keeps_a_stale_mark_and_rejects(self):
        self.open_positions(2)
        mint = sorted(cycle._state(self.ledger, self.cfg)['positions'])[0]
        marks_before = len(self.ledger_events('portfolio_marks'))
        original = MarksPipeline.marks_answer

        def broken(keys):
            values = original(self, keys)
            data = bytearray(base64.b64decode(values[1]['data'][0]))
            data[32:64] = bytes(32)                           # first position's base vault no longer owned by its pool
            values[1]['data'][0] = b64(bytes(data))
            return values
        self.append_second_candidate(seed=33)
        self.idle()
        with patch.object(self, 'marks_answer', broken):
            result = self.dispatch(intents=3, raw=self.second_raw)
        self.assertEqual(len(cycle._state(self.ledger, self.cfg)['positions']), 2)
        self.assertIn('STALE_PORTFOLIO', [o.get('reason') for o in self.outcomes()])
        self.assertEqual(self.null_passes(), [])

    def test_a_partial_sale_drops_the_valuation_mark_of_the_larger_remainder(self):
        self.open_positions(2)
        state = cycle._state(self.ledger, self.cfg)
        mint = min(state['positions'], key=lambda m: state['positions'][m]['opened_at'])   # the one the 2nd entry's refresh marked
        self.assertIn('portfolio_mark_at', state['positions'][mint])          # the 2nd entry's refresh marked both
        self.idle(5)
        leg = self.held_leg(mint, sell_output=150_000_000)                     # +80 %: the first take-profit sells part
        after = cycle._state(self.ledger, self.cfg)['positions']
        sells = [o for o in self.outcomes() if o.get('type') == 'fill' and o['side'] == 'sell' and o['mint'] == mint]
        self.assertEqual(len(sells), 1, leg)
        self.assertIn(mint, after)
        self.assertLess(float(after[mint]['qty']), float(state['positions'][mint]['qty']))
        for key in ('portfolio_mark_value', 'portfolio_mark_at', 'portfolio_mark_source'):
            self.assertNotIn(key, after[mint])

    def test_a_read_that_did_not_raise_the_monitoring_charge_is_never_applied(self):
        self.first_entry()
        self.append_second_candidate(seed=22)
        self.idle()
        real = transport.PaperReadSources.rpc_with_evidence

        def uncharged(source, method, params, *, timeout_seconds):
            if method == 'getMultipleAccounts' and params[1].get('minContextSlot') == 0:
                return {'context': {'slot': 1}, 'value': []}, 'f' * 64     # a source that skipped its reservation
            return real(source, method, params, timeout_seconds=timeout_seconds)
        with patch.object(transport.PaperReadSources, 'rpc_with_evidence', uncharged):
            self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(self.ledger_events('portfolio_marks'), [])
        self.assertIn('STALE_PORTFOLIO', [o.get('reason') for o in self.outcomes()])
        notes = [d for d in self.cycle_result['diagnostics'] if 'portfolio_marks' in d]
        self.assertEqual(notes[0]['code'], 'MONITORING_CHARGE_OR_ADMISSION_MISMATCH')

    def test_held_pass_start_refreshes_every_mark_with_one_request(self):
        self.open_positions(3)
        self.idle()
        before = (self.monitoring_rows(), len(self.marks_calls))
        item, proto = next(iter(self.entered.values()))
        view = SimpleNamespace(at=self.f.at, tick=self.f.tick, protocol=proto)
        h = SimpleNamespace(f=view, target=item.target, item=item, path=self.ledger, cfg=self.cfg, http_calls=[],
                            sell_output=82_000_000, buy_output_raw=10_000_000)
        h.chain = self
        h.run_cycle = lambda **a: self.real_run_once(self.f.jobs.path, self.f.progress.store.path, self.ledger, self.cfg,
                                                     wall_clock=lambda: self.f.at, monotonic=lambda: self.f.tick,
                                                     dependency_blockers=(), **a)
        with patch.object(tool.concurrency, 'clock', side_effect=lambda: self.f.at), \
                patch.object(cycle, 'run_once', self.real_run_once), \
                patch.object(tool.entry.time, 'sleep'), \
                patch.dict('os.environ', {'HELIUS_API_KEY': 'SYNTHETIC', 'JUPITER_API_KEY': 'SYNTHETIC'}):
            result = marks_cycle(h, positions=(), candidates=(), usd_refs=(), monitoring=True)
        self.assertEqual(result['status'], 'COMPLETE', result)
        self.assertEqual(len(self.marks_calls) - before[1], 1)
        rows = self.monitoring_rows()
        self.assertEqual((rows[0] - before[0][0], rows[1] - before[0][1]), (1, 1))
        last = self.ledger_events('portfolio_marks')[-1]
        self.assertEqual(sorted(last['marks']), sorted(cycle._state(self.ledger, self.cfg)['positions']))


class FlagAbsent(MarksPipeline):
    marks_flag = False

    def test_flag_absent_never_reads_marks_and_leaves_no_trace(self):
        self.first_entry()
        self.append_second_candidate(seed=22)
        self.f.at += (60 - self.f.at % 60) + 5
        self.held_leg(next(iter(self.entered)))
        self.f.at += 30
        result = self.dispatch(intents=2, raw=self.second_raw)
        self.assertEqual(result['paper_status'], 'COMPLETE')
        self.assertEqual(self.marks_calls, [])
        self.assertEqual(self.ledger_events('portfolio_marks'), [])
        state = cycle._state(self.ledger, self.cfg)
        self.assertTrue(all('portfolio_mark_at' not in p for p in state['positions'].values()))
        self.assertEqual(self.monitoring_rows(), (5, 5))     # only the one real held leg


for _cls in (FourPositions, FailureAndFlagTests, FlagAbsent):
    for _name in dir(pipe.ConcurrentPipeline):
        if _name.startswith('test_') and _name not in vars(_cls):
            setattr(_cls, _name, None)

if __name__ == '__main__':
    unittest.main()
