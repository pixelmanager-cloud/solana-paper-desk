"""SYNTHETIC_TEST_ONLY: real HTTPResponse, pacing, receipts and accounting.

Only HTTPS acquisition and credential loading are injected. Temporary fixture
policy pins and inert predecessor marker do not authenticate provider evidence.
"""
import base64
import copy
from dataclasses import replace
from http.client import HTTPResponse
import inspect
import io
import json
import textwrap
import time
import unittest
from unittest.mock import patch

from desk import paper_cycle, paper_read_sources as transport, quote_execution
from desk.model import canonical
from desk.monitoring_budget import MonitoringBudget
from desk.programs import unbase58
from desk.providers import SOL
from desk.security import base58
from tests import test_history_first_paper_entry as operator_fixture
from tests import test_monitoring_runtime_upgrade as runtime_fixture
from tests import test_paper_cycle as wire_fixture
from tests.test_paper_read_sources import Response
from tests.test_runtime_compatibility import dump


class HistoryRuntimeCompositionTests(unittest.TestCase):
    def test_chunked_history_guarded_entry_runtime_receipt_and_monitoring_restart(self):
        self._scenario()

    def test_invalid_chunked_history_retains_charge_and_blocks_restart_without_retry(self):
        self._scenario(invalid_history=True)

    def _scenario(self, invalid_history=False):
        u = runtime_fixture.MonitoringRuntimeUpgradeTests()
        u.setUp()
        self.addCleanup(u.doCleanups)
        op = operator_fixture.HistoryFirstTests()
        op.setUp()
        self.addCleanup(op.doCleanups)
        h, f = u.f, op.f
        old_store = f.f.progress.store
        now = int(time.time())
        manifest = old_store.load(f.item.graduation_refs[0])
        response = copy.deepcopy(old_store.load(manifest['response_hash']))
        raw = response['data'][0]
        raw['blockTime'] = now - 600
        ix = raw['meta']['innerInstructions'][0]['instructions'][0]
        data = bytearray(unbase58(ix['data']))
        data[136:144] = raw['blockTime'].to_bytes(8, 'little', signed=True)
        ix['data'] = base58(data)
        manifest = {**manifest, 'response_hash': h.store.save(response)}
        ref = h.store.save(manifest)
        h.f.context.protocol = f.f.protocol
        h.f.context.at = now
        f.f, f.target, f.path, f.cfg = h.f.context, h.target, h.new, h.cfg
        f.item = replace(f.item, target=h.target, graduated_at=now-600,
                         history_as_of=now, graduation_refs=(ref,),
                         provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        # Public operator grammar is exercised with explicitly synthetic bytes;
        # this fixture never claims real public acquisition provenance.
        f.http_calls, f.sell_output = [], 10_000_000
        op.row = vars(h.target).copy() | {
            'provenance': 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',
            'pool_fee_bps': '25', 'graduation_refs': [ref], 'known_hazards': []}
        op.save()
        op.config.write_text(canonical(h.cfg))
        # HistoryFirstTests has its own real pacer; bind the actual handoff
        # pacer instead. Never replace the pacer validator or source identity.
        from desk import provider_pacing
        environment = {provider_pacing.ENV: str(h.pacing),
                       'HELIUS_API_KEY': 'SYNTHETIC_TEST_ONLY',
                       'JUPITER_API_KEY': 'SYNTHETIC_TEST_ONLY'}
        retired = dump(h.old)
        with h.store.connect() as c:
            original_reservations = c.execute('SELECT * FROM paper_monitoring_reservations').fetchall()
            original_outcomes = c.execute('SELECT * FROM paper_monitoring_outcomes').fetchall()
        with patch.dict('os.environ', environment):
            self.assertEqual(u.upgrade()['status'], 'RECORDED')
            code = textwrap.dedent(inspect.getsource(f.actual_cycle))
            start, end = code.index('    class Opener:'), code.index('    with ExitStack()')
            namespace = {**vars(wire_fixture), 'outer': f}
            exec(textwrap.dedent(code[start:end]), namespace)
            delegate = namespace['Opener']()
            history_bodies = []

            class Socket:
                def __init__(self, data): self.data = data
                def makefile(self, *args): return io.BytesIO(self.data)

            class HTTP:
                price_at = None
                def open(self, request, *, timeout):
                    if '/price/v3?' in request.full_url:
                        self.price_at = int(time.time()) - 1
                        return Response(canonical({SOL: {'usdPrice': 100, 'blockId': 100, 'decimals': 9}}).encode())
                    call = json.loads(request.data) if request.method == 'POST' else None
                    if call and call['method'] == 'getBlockTime':
                        return Response(canonical({'jsonrpc': '2.0', 'id': transport.RPC_ID,
                                                   'result': self.price_at}).encode())
                    result = delegate.open(request, timeout=timeout)
                    if call and call['method'] == 'getTransactionsForAddress':
                        body = b'{' if invalid_history else result.body
                        history_bodies.append(body)
                        split = len(body)//2
                        chunks = b''.join(f'{len(part):x}\r\n'.encode()+part+b'\r\n'
                                          for part in (body[:split], body[split:]) if part)
                        result = HTTPResponse(Socket(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
                                                     +chunks+b'0\r\n\r\n'))
                        result.begin()
                    return result

            http = HTTP()
            with patch.object(operator_fixture.tool.cli, '_credentials'), \
                    patch.object(transport, 'build_opener', return_value=http):
                if invalid_history:
                    with self.assertRaises(ValueError):
                        op.invoke(live=True, systemd_credentials=True)
                    admission = f.f.progress.admission(h.target.scan_id)
                    self.assertEqual(admission['requests_used'], 1)
                    calls = len(f.http_calls)
                    with self.assertRaises(ValueError):
                        op.invoke(live=True, systemd_credentials=True)
                    self.assertEqual(len(f.http_calls), calls)
                    self.assertEqual(f.f.progress.admission(h.target.scan_id), admission)
                    self.assertEqual(paper_cycle._state(h.new, h.cfg)['positions'], {})
                    self.assertEqual(dump(h.old), retired)
                    with h.store.connect() as c:
                        pages = [h.store.load(key) for (key,) in c.execute('SELECT hash FROM pages')]
                        self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0], 1)
                    self.assertTrue(any(page.get('method') == 'getTransactionsForAddress'
                        and page.get('response_bytes_base64') == base64.b64encode(b'{').decode()
                        for page in pages))
                    return
                entry = op.invoke(live=True, systemd_credentials=True)
                self.assertEqual(entry['status'], 'COMPLETE', entry)
                self.assertTrue(any(row.get('side') == 'buy' for row in entry['outcomes']))
                self.assertEqual(len(history_bodies), 1)
                self.assertEqual(f.f.progress.admission(h.target.scan_id)['requests_used'], 9)
                with h.store.connect() as c:
                    pages = [h.store.load(key) for (key,) in c.execute('SELECT hash FROM pages')]
                self.assertTrue(any(page.get('method') == 'getTransactionsForAddress'
                    and page.get('response_bytes_base64') == base64.b64encode(history_bodies[0]).decode()
                    and page.get('source_id') == transport.PaperReadSources.rpc_source_id for page in pages))
                admission = f.f.progress.admission(h.target.scan_id)
                position = paper_cycle._state(h.new, h.cfg)['positions'][h.target.mint]
                item = replace(f.item, graduation_refs=(), known_hazards=('SYNTHETIC_KNOWN_HAZARD',),
                    target=replace(h.target, amount_raw=quote_execution.raw_quantity(position['qty'], 6)))
                result = paper_cycle.run_once(h.research, h.evidence, h.new, h.cfg,
                    position_targets=(item,), candidates=(), dependency_blockers=(), monitoring=True)
                self.assertEqual(result['status'], 'COMPLETE', result)
                self.assertEqual(result['monitoring_attempted_requests'], 4)
                self.assertEqual(f.f.progress.admission(h.target.scan_id), admission)
                self.assertEqual(paper_cycle._state(h.new, h.cfg)['positions'], {})
                budget = MonitoringBudget(h.store, h.new, h.cfg)
                snapshot = budget.snapshot()
                self.assertEqual(snapshot['total_used'], 5)
                self.assertEqual(snapshot['blockers'], [])
                restart = paper_cycle.run_once(h.research, h.evidence, h.new, h.cfg,
                    candidates=(), dependency_blockers=(), monitoring=True)
                self.assertEqual(restart['status'], 'COMPLETE', restart)
                self.assertEqual(restart['attempted_requests'], 0)
        self.assertEqual(dump(h.old), retired)
        with h.store.connect() as c:
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_reservations WHERE id=1').fetchall(), original_reservations)
            self.assertEqual(c.execute('SELECT * FROM paper_monitoring_outcomes WHERE reservation_id=1').fetchall(), original_outcomes)
