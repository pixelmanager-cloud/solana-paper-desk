"""Persisted synthetic diagnostics; ownership waiver never invents an entry."""
import base64
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from desk.evidence import EvidenceStore
from desk.model import canonical,digest
from desk.paper_experimental_policy import paper_candidate,ownership_history_rule,PAPER_EXPERIMENTAL,PAPER_STRICT
from desk.security import TOKEN_PROGRAM,TOKEN_2022
from desk.pools import verify_pool
from tests import test_pools as pools


class PaperExperimentalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/'research.sqlite';self.evidence=self.root/'evidence.sqlite'
        self.store=EvidenceStore(self.evidence)
        # Existing production pool decoder and verifier, actual persisted RPCs.
        self.poolcase=pools.PoolTests();self.poolcase.setUp()
        self.mint=str(self.poolcase.mint)
        replay=verify_pool(str(self.poolcase.pool),self.mint,self.poolcase.rpc,capture=self.store.save)
        self.assertTrue(replay['liquidity_control_verified'])
        self.pool_hash=replay['evidence_hash']
        poolraw=self.store.load(self.pool_hash)
        self.mintaccount=poolraw['result']['value'][-1]
        self.mint_hash=self.store.save({'method':'getAccountInfo','params':[self.mint,{'encoding':'base64','commitment':'confirmed'}],
                                      'result':{'value':self.mintaccount}})
        # A gross same-bank holder snapshot covering the entire raw supply.
        holder=bytearray(165);holder[:32]=bytes(self.poolcase.mint);holder[32:64]=bytes(self.poolcase.creator)
        holder[64:72]=(10**15).to_bytes(8,'little');holder[108]=1
        account=str(self.poolcase.vaults[0])
        self.holder_hash=self.store.save({'method':'getMultipleAccounts','params':[[self.mint,account],
                  {'encoding':'base64','commitment':'confirmed','minContextSlot':101}],
                  'result':{'context':{'slot':101},'value':[self.mintaccount,self.poolcase.account(holder,TOKEN_PROGRAM)]}})
        self.report={'mint':self.mint,'observed_at':110,'calls':3,'findings':[],'unknowns':[],
                     'mint_evidence_hash':self.mint_hash,'holder_snapshot':{'evidence_hash':self.holder_hash},
                     'verified_pools':[{'evidence_hash':self.pool_hash}]}
        with sqlite3.connect(self.db) as c:c.execute('CREATE TABLE scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
        self.save()

    def save(self):
        self.report.pop('report_hash',None);self.report['report_hash']=digest(self.report)
        self.scan={'id':'scan','mint':self.mint,'created':100,'status':'COMPLETE','result':canonical(self.report)}
        with sqlite3.connect(self.db) as c:
            c.execute('DELETE FROM scans');c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',tuple(self.scan.values()))
        self.source=digest(self.scan)

    def read(self,**kwargs):
        with patch('socket.socket',side_effect=AssertionError('No network')):
            return paper_candidate(self.db,self.evidence,'scan',source_hash=kwargs.pop('source_hash',self.source),
                                   now=kwargs.pop('now',110),**kwargs)

    def test_actual_controls_pass_history_risk_allowed_only_explicit_mode(self):
        strict=self.read();experimental=self.read(mode=PAPER_EXPERIMENTAL)
        self.assertFalse(strict['ownership_history']['risk_accepted'])
        self.assertIn('OWNERSHIP_HISTORY_UNRESOLVED',strict['blockers'])
        self.assertTrue(experimental['ownership_history']['risk_accepted'])
        self.assertEqual(experimental['risk_flags'],['UNRESOLVED_OWNERSHIP_HISTORY'])
        self.assertNotIn('OWNERSHIP_HISTORY_UNRESOLVED',experimental['blockers'])
        self.assertEqual(experimental['ownership_history']['status'],'UNKNOWN')
        self.assertTrue(experimental['ownership_history']['unknown_reasons'])
        for result in (strict,experimental):
            self.assertEqual(result['decision'],'REJECT');self.assertFalse(result['eligible_for_trading'])
            self.assertFalse(result['source_authenticated']);self.assertFalse(result['ownership_complete'])
            self.assertEqual(result['source_hash'],self.source)
            self.assertEqual(result['policy_version'],1)
            for name in ('top10_pct','dev_pct','bundle_pct','cluster_pct'):
                self.assertEqual(result['fields'][name]['status'],'UNKNOWN');self.assertIsNone(result['fields'][name]['value'])
            self.assertIn('PERSISTED_CURRENT_MARKET_EVENT_UNAVAILABLE',result['blockers'])
            self.assertIn('EXACT_ENTRY_QUANTITY_AND_COST_UNAVAILABLE',result['blockers'])
            self.assertNotIn('event',result)

    def test_raw_known_token_hazards_always_block_risk_waiver(self):
        for reason,offset in [('ACTIVE_MINT_AUTHORITY',0),('ACTIVE_FREEZE_AUTHORITY',46)]:
            raw=bytearray(base64.b64decode(self.mintaccount['data'][0]));raw[offset:offset+4]=(1).to_bytes(4,'little')
            bad=self.poolcase.account(raw,TOKEN_PROGRAM)
            self.report['mint_evidence_hash']=self.store.save({'method':'getAccountInfo','params':[self.mint,{'encoding':'base64','commitment':'confirmed'}],'result':{'value':bad}})
            self.save()
            for mode in (PAPER_STRICT,PAPER_EXPERIMENTAL):
                result=self.read(mode=mode);self.assertIn(reason,result['known_hazards'])
                self.assertIn(reason,result['blockers']);self.assertFalse(result['ownership_history']['risk_accepted'])
                self.assertEqual(result['decision'],'REJECT')

    def test_token2022_exclusion_and_unknown_controls_never_waived(self):
        raw=copy.deepcopy(self.mintaccount);raw['owner']=TOKEN_2022
        self.report['mint_evidence_hash']=self.store.save({'method':'getAccountInfo','params':[self.mint,{'encoding':'base64','commitment':'confirmed'}],'result':{'value':raw}})
        self.save();result=self.read(mode=PAPER_EXPERIMENTAL)
        self.assertIn('TOKEN_2022_NOT_ALLOWED',result['known_hazards']);self.assertFalse(result['ownership_history']['risk_accepted'])
        self.report['mint_evidence_hash']='0'*64;self.save();result=self.read(mode=PAPER_EXPERIMENTAL)
        self.assertIn('TOKEN_RAW_EVIDENCE_UNAVAILABLE',result['blockers'])
        self.assertFalse(result['ownership_history']['risk_accepted']);self.assertEqual(result['known_hazards'],[])

    def test_known_withdrawable_liquidity_rejects(self):
        self.poolcase.lp_supply=100
        replay=verify_pool(str(self.poolcase.pool),self.mint,self.poolcase.rpc,capture=self.store.save)
        self.report['verified_pools']=[{'evidence_hash':replay['evidence_hash']}];self.save()
        result=self.read(mode=PAPER_EXPERIMENTAL)
        self.assertIn('OUTSTANDING_WITHDRAWABLE_LP_SUPPLY',result['known_hazards'])
        self.assertFalse(result['ownership_history']['risk_accepted'])

    def test_source_revision_stale_future_and_missing_records_block(self):
        self.assertIn('SOURCE_HASH_MISMATCH',self.read(mode=PAPER_EXPERIMENTAL,source_hash='f'*64)['blockers'])
        self.assertIn('SOURCE_REVISION_MISMATCH',self.read(mode=PAPER_EXPERIMENTAL,revision_hash='f'*64)['blockers'])
        for now in (109,121):
            result=self.read(mode=PAPER_EXPERIMENTAL,now=now)
            self.assertIn('INVESTIGATION_NOT_FRESH_FOR_ENTRY',result['blockers']);self.assertFalse(result['ownership_history']['risk_accepted'])
        with sqlite3.connect(self.db) as c:c.execute('DELETE FROM scans')
        self.assertIn('SOURCE_MISSING_MALFORMED_OR_OVERSIZED',self.read()['blockers'])

    def test_arbitrary_unknowns_and_findings_are_not_reclassified_as_history(self):
        self.report['unknowns']=['EXACT_ROUTE_COST_UNVERIFIED'];self.report['findings']=['KNOWN_DANGER'];self.save()
        result=self.read(mode=PAPER_EXPERIMENTAL)
        self.assertIn('EXACT_ROUTE_COST_UNVERIFIED',result['blockers']);self.assertIn('KNOWN_DANGER',result['blockers'])
        self.assertFalse(result['ownership_history']['risk_accepted'])
        self.assertNotIn('EXACT_ROUTE_COST_UNVERIFIED',result['ownership_history']['unknown_reasons'])

    def test_forged_summary_market_flags_never_create_candidate(self):
        self.report.update(eligible_for_trading=True,source_authenticated=True,ownership_complete=True,
                           reserve_sol=999,price_at=110,bundle_pct=0,route_available=True)
        self.save();result=self.read(mode=PAPER_EXPERIMENTAL)
        self.assertEqual(result['decision'],'REJECT');self.assertFalse(result['eligible_for_trading'])
        self.assertEqual(result['fields']['bundle_pct']['status'],'UNKNOWN')
        self.assertIn('PERSISTED_CURRENT_MARKET_EVENT_UNAVAILABLE',result['blockers'])

    def test_read_only_and_old_strict_assess_unchanged(self):
        from desk.decision_runner import assess
        def dump(path):
            with sqlite3.connect(path) as c:return list(c.iterdump())
        before=[dump(self.db),dump(self.evidence)]
        self.read(mode=PAPER_EXPERIMENTAL)
        old=assess(self.scan,110,EvidenceStore(self.evidence,read_only=True))
        self.assertEqual(old['decision'],'REJECT');self.assertFalse(old['eligible_for_trading'])
        self.assertEqual(before,[dump(self.db),dump(self.evidence)])

    def test_explicit_mode_bounds_and_rule_is_not_entry_admission(self):
        for mode in ('paper','LIVE',None):
            with self.assertRaises(ValueError):self.read(mode=mode)
        for now in (True,-1,2**63):
            with self.assertRaises(ValueError):self.read(now=now)
        rule=ownership_history_rule(mode=PAPER_EXPERIMENTAL,history_reasons=['TRANSFER_RAW_HISTORY_UNAVAILABLE'],nonhistory_controls_passed=True,known_hazards=[])
        self.assertTrue(rule['risk_accepted']);self.assertFalse(rule['ownership_verified'])
        self.assertNotIn('eligible_for_trading',rule)

    def test_observed_history_conflicts_and_unrecognized_failures_not_waived(self):
        for reason in ('ACCOUNT_HISTORY_CONFLICTING_TRANSACTION','UNRECOGNIZED_FAILURE'):
            rule=ownership_history_rule(mode=PAPER_EXPERIMENTAL,history_reasons=[reason],nonhistory_controls_passed=True,known_hazards=[])
            self.assertFalse(rule['risk_accepted']);self.assertTrue(rule['blocks_history'])
            self.assertEqual(rule['unwaived_reasons'],[reason])
