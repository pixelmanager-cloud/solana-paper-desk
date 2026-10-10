"""Synthetic verbose histories; partial provider coverage never becomes entry proof."""
import copy
from contextlib import closing
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
from desk.evidence import EvidenceStore
from desk.history import collect_history
from desk.model import digest
from desk.live_strategy_features import calculate,MAX_RECORDS,MAX_RECORD_BYTES
from desk.streaming_history import RetainedHistoryPages
from desk import history_preparation_rejection as rejection
from tests import test_live_strategy_features as f

class StreamingFeatureTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.store=EvidenceStore(Path(tmp.name)/'evidence.sqlite')
        self.at=f.T+2

    def capture(self,n=150,*,terminal=True):
        position=[0]
        def rpc(method,params):
            start=position[0];position[0]+=50
            rows=[]
            for i in range(start,min(start+50,n)):
                row=f.transaction(signature='synthetic-'+str(i),slot=i+10,ts=f.T,quote=200+i,base=100,wallet=f.PUBLIC['decoded']['fields']['user'])
                row['meta']['logMessages']=['Program log: SYNTHETIC_VERBOSE '+('x'*19000)]
                rows.append(row)
            return {'data':rows,'paginationToken':None if terminal and position[0]>=n else 'cursor-'+str(position[0])}
        _,coverage=collect_history(f.POOL,self.at-300,self.at+1,rpc,max_pages=(n+49)//50,capture=self.store.save,token_accounts='none',page_size=50,retain_observations=False)
        return coverage

    def measure(self,coverage):
        pairs=RetainedHistoryPages(self.store,coverage)
        return calculate(pairs.records(),pool=f.POOL,as_of=self.at,provenance='SYNTHETIC_TEST_ONLY',history_pages=pairs,semantics_version=2)

    def test_150_verbose_records_stream_all_trades_and_keep_legacy_judgment(self):
        coverage=self.capture();pairs=RetainedHistoryPages(self.store,coverage)
        bounds=rejection.bounds(self.store,coverage,semantics_version=2)
        self.assertGreater(bounds['record_bytes'],2*1024*1024)
        self.assertEqual(bounds['records'],150)
        self.assertIsNone(rejection.reason_for(bounds,9,7,exhausted=True,semantics_version=2))
        self.assertEqual(rejection.reason_for(bounds,9,7,exhausted=True,semantics_version=1),'HISTORY_FEATURE_AGGREGATE_BYTES_EXCEEDED')
        tracemalloc.start()
        try:
            result=self.measure(coverage);_,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertTrue(result['window']['coverage_complete'],result['blockers'])
        self.assertEqual(result['trade_count'],150)
        self.assertEqual(result['fields']['momentum_at']['value'],f.T)
        self.assertEqual(result['fields']['net_buy_ratio']['value'],'1')
        self.assertFalse(result['eligible_for_trading'])
        self.assertLess(peak,12*1024*1024)  # retained originals >2MiB; bounded page working set
        self.assertEqual(len(pairs.refs),3)
        self.assertFalse(hasattr(pairs,'rows'))

    def test_partial_three_pages_continue_without_claiming_exhaustion(self):
        coverage=self.capture(terminal=False);result=self.measure(coverage)
        self.assertFalse(result['window']['coverage_complete'])
        self.assertNotEqual(result['fields']['momentum_at']['status'],'MEASURED_WINDOW')
        bounds=rejection.bounds(self.store,coverage,semantics_version=2)
        self.assertIsNone(rejection.reason_for(bounds,9,7,semantics_version=2))

    def test_mutated_page_hash_omission_cursor_order_and_timestamp_refuse(self):
        coverage=self.capture()
        bad=copy.deepcopy(coverage);bad['pages'].pop(1);bad.pop('evidence_hash');bad['evidence_hash']=digest(bad)
        self.assertFalse(self.measure(bad)['window']['coverage_complete'])
        key=coverage['pages'][1]['payload_hash']
        with closing(self.store.connect()) as c:c.execute("UPDATE pages SET payload=x'00' WHERE hash=?",(key,))
        self.assertFalse(self.measure(coverage)['window']['coverage_complete'])
        for mutation in ('slot','blockTime'):
            rows=[f.transaction(signature='a',slot=20,ts=f.T),f.transaction(signature='b',slot=21,ts=f.T)]
            rows[1][mutation]=19 if mutation=='slot' else f.T+100
            pairs=f.history_pages(rows,now=self.at)
            result=calculate(iter(rows),pool=f.POOL,as_of=self.at,provenance='SYNTHETIC_TEST_ONLY',history_pages=iter(pairs),semantics_version=2)
            self.assertFalse(result['window']['coverage_complete'])

    def test_record_event_and_trade_limits_fail_closed(self):
        rows=[f.transaction(signature=str(i),slot=i+1) for i in range(MAX_RECORDS+1)]
        # No page >100: explicit matched iterator is still bounded by records.
        result=calculate(iter(rows),pool=f.POOL,as_of=self.at,provenance='SYNTHETIC_TEST_ONLY',history_pages=[],semantics_version=2)
        self.assertIn('MALFORMED_OR_BOUNDED_INPUT_UNAVAILABLE',result['blockers'])
        row=f.transaction();row['padding']='x'*MAX_RECORD_BYTES
        result=calculate(iter([row]),pool=f.POOL,as_of=self.at,provenance='SYNTHETIC_TEST_ONLY',history_pages=[],semantics_version=2)
        self.assertFalse(result['window']['coverage_complete'])
        row=f.transaction();row['transaction']['message']['instructions']*=257
        result=calculate(iter([row]),pool=f.POOL,as_of=self.at,provenance='SYNTHETIC_TEST_ONLY',history_pages=[],semantics_version=2)
        self.assertIn('MALFORMED_OR_BOUNDED_INPUT_UNAVAILABLE',result['blockers'])

    def test_trade_budget_exhaustion_keeps_no_valid_prefix_measurement(self):
        rows=[f.transaction(signature='a',slot=10),f.transaction(signature='b',slot=11)]
        pairs=f.history_pages(rows,now=self.at)
        with patch('desk.live_strategy_features.MAX_TRADES',1):
            result=calculate(iter(rows),pool=f.POOL,as_of=self.at,provenance='SYNTHETIC_TEST_ONLY',history_pages=pairs,semantics_version=2)
        self.assertFalse(result['window']['coverage_complete'])
        self.assertIsNone(result['fields']['net_buy_ratio']['value'])
        self.assertIn('MALFORMED_OR_BOUNDED_INPUT_UNAVAILABLE',result['blockers'])

    def test_page_scalar_bound_precedes_content_fetch(self):
        coverage=self.capture();key=coverage['pages'][0]['payload_hash']
        with closing(self.store.connect()) as c:c.execute('UPDATE pages SET raw_bytes=? WHERE hash=?',(2*1024*1024+1,key))
        with patch('desk.paper_terminal_reconciliation.zlib.decompressobj',side_effect=AssertionError('no unbounded decode')):
            pairs=RetainedHistoryPages(self.store,coverage)
            with self.assertRaises(ValueError):pairs._load(key)
