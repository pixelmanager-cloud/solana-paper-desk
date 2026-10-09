"""Original collector mint bytes through the real builder and entry gate."""
import base64
import copy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from desk import engine, quote_execution as qe
from desk.model import digest,canonical
from desk.ledger import Ledger
from desk.paper_view import _history_preflight
from desk.security import entry_token_policy, TOKEN_2022
from tests import test_paper_market_adapter as fixtures


class TokenHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PaperMarketAdapterTests();self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.cfg=json.loads((Path(__file__).resolve().parents[1]/'config/paper.json').read_text())
        self.cfg.update(experimental_policy_version=3,paper_signal_policy_version=3,
                        paper_quote_execution_version=1)

    def plan(self,event):
        return qe.plan(engine.initial_state(self.cfg),event,self.cfg,(self.f.observation.quote,))

    def metadata_collector(self):
        f=self.f;original=f.fixture.sources[f.target.scan_id];size=[0]
        def rpc(method,params,**options):
            payload=original.rpc(method,params,**options)
            if method=='getAccountInfo':payload['value']['unknown_retained_metadata']='x'*size[0]
            return payload
        f.fixture.sources[f.target.scan_id]=replace(original,rpc=rpc)
        def collect(count):
            size[0]=count
            f.observation=f.fixture.collect(candidates=(f.target,)).observations[0]
            self.assertIsNone(f.observation.failure)
            return f.build()
        return collect

    def test_actual_collector_near_journal_boundary_positive_then_beyond_rejected(self):
        collect=self.metadata_collector();base=collect(0)
        overhead=len(canonical(base['event']).encode())
        # One original field appears in account plus the twice-preserved original
        # mint observation. ASCII byte growth is exactly three per source byte.
        count=(262144-overhead)//3
        good=collect(count);e=good['event'];self.assertIsNotNone(e)
        size=len(canonical(e).encode());self.assertLessEqual(size,262144)
        self.assertGreaterEqual(size,262142)
        self.assertEqual(entry_token_policy(e),[])
        ledger=Ledger(Path(self.f.fixture.tmp.name)/'size-bound.sqlite');self.addCleanup(ledger.close)
        outcomes=ledger.apply(e,self.cfg,qe.bind_transition(e,(self.f.observation.quote,)),engine.initial_state)
        self.assertTrue(any(o.get('type')=='reject' for o in outcomes))
        _history_preflight(ledger.db)  # actual consumer accepts the committed event
        bad=collect(count+1);self.assertIsNone(bad['event'])
        self.assertIn('PAPER_EVENT_BYTE_LIMIT_EXCEEDED',bad['blockers'])
        self.assertGreater(len(canonical(bad['draft']).encode()),262144)
        self.assertEqual(bad['draft']['token_evidence']['account']['unknown_retained_metadata'],'x'*(count+1))
        self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)

    def test_reviewed_100000_byte_unknown_metadata_preserved_not_published(self):
        out=self.metadata_collector()(100000);source=self.f.observation.mint.source
        self.assertLess(len(source.original_json.encode()),262144)
        self.assertIsNone(out['event'])
        self.assertEqual(out['blockers'],['PAPER_EVENT_BYTE_LIMIT_EXCEEDED'])
        self.assertGreater(len(canonical(out['draft']).encode()),262144)
        self.assertEqual(out['draft']['token_evidence']['original_json'],source.original_json)
        self.assertEqual(out['draft']['token_evidence']['account']['unknown_retained_metadata'],'x'*100000)
        for key in self.f.observation.evidence_refs:
            self.assertEqual(digest(self.f.fixture.progress.store.load(key)),key)

    def test_original_mint_account_and_source_identity_reach_actual_entry_policy(self):
        f=self.f;original=json.loads(f.observation.mint.source.original_json)
        refs=tuple(f.observation.evidence_refs)
        charges=f.fixture.progress.admission(f.target.scan_id)['requests_used']
        out=f.build();e=out['event'];self.assertIsNotNone(e)
        evidence=e['token_evidence'];source=f.observation.mint.source
        self.assertEqual(evidence,{'mint':f.target.mint,'observed_at':source.observed_at,
            'account':original['account'],'slot':f.observation.mint.slot,
            'source_id':source.source_id,'source_hash':source.raw_hash,
            'original_json':source.original_json})
        self.assertEqual(evidence['account'],original['original_rpc_observation']['result']['value'])
        self.assertEqual(digest(json.loads(evidence['original_json'])),evidence['source_hash'])
        self.assertEqual(evidence['source_hash'],e['paper_source_evidence']['mint_hash'])
        self.assertEqual(evidence['observed_at'],e['paper_source_evidence']['mint_at'])
        self.assertEqual(entry_token_policy(e),[])
        self.assertFalse(out['entry_authorized']);self.assertFalse(out['execution_verified'])
        self.assertEqual(f.observation.evidence_refs,refs)
        self.assertEqual(f.fixture.progress.admission(f.target.scan_id)['requests_used'],charges)
        # This fixture has known measured churn/other gate failures. Raw mint
        # handoff fixes only the token gate, never promotes the candidate.
        result=self.plan(e)
        self.assertFalse(result['quote_demands'])
        reasons=result['outcomes'][-1]['reasons']
        self.assertNotIn('TOKEN_EVIDENCE_MISSING_OR_MISMATCHED',reasons)
        self.assertIn('MOMENTUM_OR_OBSERVED_CHURN',reasons)
        missing=copy.deepcopy(e);missing.pop('token_evidence')
        self.assertIn('TOKEN_EVIDENCE_MISSING_OR_MISMATCHED',self.plan(missing)['outcomes'][-1]['reasons'])
        # No mutable alias into the retained typed observation/store.
        evidence['account']['owner']='invalid'
        self.assertEqual(json.loads(source.original_json),original)
        self.assertEqual(f.build()['event']['token_evidence']['account'],original['account'])

    def test_present_raw_mint_hazards_stale_or_mismatched_evidence_still_reject(self):
        original=self.f.build()['event']
        for attack in ('mint-authority','freeze-authority','token2022','truncated','stale','different-mint'):
            e=copy.deepcopy(original);ev=e['token_evidence']
            raw=bytearray(base64.b64decode(ev['account']['data'][0]))
            if attack=='mint-authority':raw[0:4]=(1).to_bytes(4,'little');raw[4:36]=bytes([1])*32
            elif attack=='freeze-authority':raw[46:50]=(1).to_bytes(4,'little');raw[50:82]=bytes([1])*32
            elif attack=='token2022':ev['account']['owner']=TOKEN_2022
            elif attack=='truncated':raw=raw[:81]
            elif attack=='stale':ev['observed_at']=e['ts']-11
            elif attack=='different-mint':ev['mint']='So11111111111111111111111111111111111111112'
            ev['account']['data'][0]=base64.b64encode(raw).decode()
            with self.subTest(attack=attack):
                reasons=entry_token_policy(e);self.assertTrue(reasons)
                planned=self.plan(e);self.assertFalse(planned['quote_demands'])
                self.assertTrue(set(reasons)<=set(planned['outcomes'][-1]['reasons']))

    def test_unbound_sources_and_known_context_hazards_do_not_publish_event(self):
        f=self.f;original=f.observation
        for attack in ('hash','source','account'):
            source=original.mint.source
            if attack=='hash':source=replace(source,raw_hash='0'*64)
            elif attack=='source':source=replace(source,source_id='untrusted-rpc')
            else:
                raw=json.loads(source.original_json);raw['account']['owner']=TOKEN_2022
                source=replace(source,original_json=json.dumps(raw),raw_hash=digest(raw))
            f.observation=replace(original,mint=replace(original.mint,source=source))
            try:
                with self.subTest(attack=attack):
                    out=f.build();self.assertIsNone(out['event'])
                    self.assertIn('COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID',out['blockers'])
            finally:f.observation=original
        out=f.build(context=replace(f.context,known_hazards=('KNOWN_ADVERSE_EVIDENCE',)))
        self.assertIsNone(out['event']);self.assertIn('KNOWN_ADVERSE_EVIDENCE',out['blockers'])


if __name__=='__main__':unittest.main()
