"""Synthetic original envelopes; acquisition and completion stay distinct."""
import copy
import base64,json
from dataclasses import replace
import unittest
from desk.paper_market_adapter import _replay_collected
from tests import test_paper_market_adapter as fixtures
from desk.model import canonical
from desk.live_observation import ProviderObservation,ingest_quote
from urllib.parse import urlencode


class QuoteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PaperMarketAdapterTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;store=f.fixture.progress.store
        raw=json.loads(f.observation.quote.source.original_json)
        attempt={'kind':'paper_read_attempt_v1','scan_id':f.target.scan_id,'requests_used':4,
                 'source_id':f.context.quote_source_id,'method':'jupiter_probe','params':raw['request'],
                 'request_bytes_base64':base64.b64encode(urlencode(raw['request']).encode()).decode(),
                 'response_bytes_base64':base64.b64encode(canonical(raw['response']).encode()).decode(),
                 'observed_at':f.now,'http_status':200,'failure_code':None}
        raw['evidence_hash']=store.save(attempt);self.attempt=attempt;self.raw=raw
        quote=ingest_quote(lambda:ProviderObservation(f.context.quote_source_id,f.now,raw),mint=f.observation.mint,
            direction='buy',amount_raw=f.target.amount_raw,taker=f.target.taker,expected_pool=f.target.pool,now=f.now)
        refs=list(f.observation.evidence_refs)
        for i,key in enumerate(refs):
            record=store.load(key)
            if record.get('method')=='jupiter_probe':refs[i]=store.save({**record,'result':raw})
        f.observation=replace(f.observation,quote=quote,evidence_refs=tuple(refs))

    def observation(self,**changes):
        f=self.f;store=f.fixture.progress.store;refs=list(f.observation.evidence_refs)
        for i,key in enumerate(refs):
            record=store.load(key)
            if record.get('method')=='jupiter_probe':
                changed=copy.deepcopy(record);changed.update(changes)
                refs[i]=store.save(changed)
                return replace(f.observation,evidence_refs=tuple(refs))
        raise AssertionError('Original quote envelope required')

    def test_later_completion_preserves_exact_original_quote_time(self):
        f=self.f;original=f.observation.quote.source
        obs=self.observation(acquired_at=f.now+1)
        replay=_replay_collected(obs,replace(f.context,now=f.now+1),f.fixture.progress.store.load)
        self.assertEqual(replay[3].source,original)
        self.assertEqual(replay[3].source.observed_at,f.now)
        self.assertEqual(f.fixture.progress.admission(f.target.scan_id)['requests_used'],4)

    def test_stale_future_and_invalid_completion_order_are_rejected(self):
        f=self.f
        for changes in ({'acquired_at':f.now+2},{'acquired_at':f.now-1},
                        {'started_at':f.now+1,'acquired_at':f.now+1},
                        {'started_at':f.now-11,'acquired_at':f.now},
                        {'acquired_at':True},{'started_at':None}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                _replay_collected(self.observation(**changes),replace(f.context,now=f.now+1),f.fixture.progress.store.load)
        for quote_at in (f.now-11,f.now+1):
            quote=replace(f.observation.quote,source=replace(f.observation.quote.source,observed_at=quote_at))
            obs=replace(self.observation(acquired_at=f.now),quote=quote)
            with self.subTest(quote_at=quote_at),self.assertRaises(ValueError):
                _replay_collected(obs,f.context,f.fixture.progress.store.load)

    def test_missing_forged_or_wrong_scan_attempt_never_authorizes_later_completion(self):
        f=self.f;store=f.fixture.progress.store;obs=self.observation(acquired_at=f.now+1)
        key=self.raw['evidence_hash']
        for altered in (None,{**self.attempt,'scan_id':'other'},
                        {**self.attempt,'failure_code':'TIMEOUT'},
                        {**self.attempt,'http_status':403},
                        {**self.attempt,'observed_at':f.now+1},
                        {**self.attempt,'request_bytes_base64':base64.b64encode(b'forged').decode()},
                        {**self.attempt,'response_bytes_base64':base64.b64encode(b'{}').decode()}):
            def load(k):
                if k==key:
                    if altered is None:raise ValueError('Missing retained attempt')
                    return altered
                return store.load(k)
            with self.subTest(altered=altered),self.assertRaises(ValueError):
                _replay_collected(obs,replace(f.context,now=f.now+1),load)

    def test_hash_valid_conflicting_attempt_identity_or_wire_is_rejected(self):
        f=self.f;store=f.fixture.progress.store
        for changes in ({'scan_id':'other'},{'source_id':'other'},
                        {'failure_code':'TIMEOUT'},{'http_status':403},{'observed_at':f.now+1},
                        {'request_bytes_base64':base64.b64encode(b'forged').decode()},
                        {'response_bytes_base64':base64.b64encode(b'{}').decode()}):
            raw=copy.deepcopy(self.raw);raw['evidence_hash']=store.save({**self.attempt,**changes})
            quote=ingest_quote(lambda:ProviderObservation(f.context.quote_source_id,f.now,raw),mint=f.observation.mint,
                direction='buy',amount_raw=f.target.amount_raw,taker=f.target.taker,expected_pool=f.target.pool,now=f.now)
            refs=list(f.observation.evidence_refs)
            for i,key in enumerate(refs):
                r=store.load(key)
                if r.get('method')=='jupiter_probe':refs[i]=store.save({**r,'result':raw,'acquired_at':f.now+1})
            obs=replace(f.observation,quote=quote,evidence_refs=tuple(refs))
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                _replay_collected(obs,replace(f.context,now=f.now+1),store.load)
