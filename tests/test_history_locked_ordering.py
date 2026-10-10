"""Native clocks, actual replay/transport/cycle; synthetic HTTPS bytes only."""
import copy,inspect,json,textwrap,time,unittest
from pathlib import Path
from unittest.mock import patch
from desk import paper_cycle as cycle, paper_terminal_reconciliation as terminal
from desk import paper_read_sources as transport, provider_pacing as pacing
from desk.model import canonical
from desk.programs import unbase58
from desk.security import base58
from tests import test_history_first_paper_entry as fixture, test_paper_cycle as protocol
from tests.test_paper_read_sources import Response

class LockedHistoryTests(unittest.TestCase):
    def test_native_slow_gate_precedes_new_history_and_fresh_entry(self):
        h=fixture.HistoryFirstTests();h.setUp();self.addCleanup(h.doCleanups)
        from desk import kraken_pacing_migration
        policy=Path(h.root)/'kraken-migration.json';policy.write_text(canonical({'version':1,'pins':[]}))
        pinning=patch.object(kraken_pacing_migration,'POLICY',policy);pinning.start();self.addCleanup(pinning.stop)
        pin=kraken_pacing_migration.review_plan(h.pace)
        policy.write_text(canonical({'version':1,'pins':[pin]}));kraken_pacing_migration.migrate(h.pace)
        f=h.f;f.f.at=int(time.time());f.http_calls=[];f.sell_output=100_000_000
        f.cfg=f.cfg|{'paper_usd_valuation_version':1}
        f.path=Path(h.root)/'native-kraken.sqlite';cycle.initialize(f.path,f.cfg)
        h.config.write_text(canonical(f.cfg));h.row['amount_raw']=100_000_000
        manifest=f.f.progress.store.load(f.item.graduation_refs[0]);response=copy.deepcopy(f.f.progress.store.load(manifest['response_hash']))
        raw=response['data'][0];raw['blockTime']=f.f.at-600
        ix=raw['meta']['innerInstructions'][0]['instructions'][0]
        data=bytearray(unbase58(ix['data']));data[136:144]=raw['blockTime'].to_bytes(8,'little',signed=True);ix['data']=base58(data)
        key=f.f.progress.store.save(response);ref=f.f.progress.store.save({**manifest,'response_hash':key})
        h.row.update(provenance='PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE',graduation_refs=[ref]);h.save()
        code=textwrap.dedent(inspect.getsource(f.actual_cycle));a=code.index('    class Opener:');b=code.index('    with ExitStack()')
        namespace={**vars(protocol),'outer':f};exec(textwrap.dedent(code[a:b]),namespace);delegate=namespace['Opener']()
        order=[];original_gate=terminal.gate;latest=[None]
        def slow_gate(*args,**kw):
            order.append('gate');time.sleep(7.451)
            return original_gate(*args,**kw)
        class HTTP:
            def open(self,request,*,timeout):
                if request.method=='POST' and json.loads(request.data)['method']=='getTransactionsForAddress':
                    order.append('history');f.f.at=int(time.time());latest[0]=f.f.at-1
                if 'api.kraken.com/0/public/Trades?' in request.full_url:
                    wire=canonical({'error':[],'result':{'SOLUSD':[['100','1',time.time()-1,'s','l','',123]],'last':'123'}}).encode()
                    return Response(wire)
                return delegate.open(request,timeout=timeout)
        with patch.object(fixture.tool.cli,'_credentials'),patch.dict('os.environ',{'HELIUS_API_KEY':'SYNTHETIC_TEST_ONLY','JUPITER_API_KEY':'SYNTHETIC_TEST_ONLY'}),patch.object(transport,'build_opener',return_value=HTTP()),patch.object(terminal,'gate',side_effect=slow_gate):
            result=h.invoke(live=True,systemd_credentials=True)
        self.assertEqual(result['status'],'COMPLETE',result)
        self.assertTrue(any(row.get('side')=='buy' for row in result['outcomes']),result)
        self.assertTrue(order[:order.index('history')]);self.assertNotIn('gate',order[order.index('history'):])
        self.assertLessEqual(int(time.time())-latest[0],15)
        self.assertLessEqual(f.f.progress.admission(f.target.scan_id)['requests_used'],18)
        with f.f.progress.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM paper_observation_passes WHERE outcome_hash IS NULL').fetchone()[0],0)
    def test_history_first_rejects_mixed_or_held_mode_before_work(self):
        from tests.helpers import config
        for args in ({'candidates':()},{'monitoring':True},{'controls':({'kind':'control'},)}):
            with self.subTest(args=args),patch.object(terminal,'gate',side_effect=AssertionError('gate')):
                with self.assertRaises(ValueError):cycle.run_once('missing','missing','missing',config(),dependency_blockers=(),history_first=True,**args)
