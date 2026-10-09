"""SYNTHETIC_TEST_ONLY: supported paper profile; no live token acceptance."""
import base64
import copy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from solders.pubkey import Pubkey
from desk import paper_cycle as cycle, quote_execution as qe
from desk.security import TOKEN_2022, TOKEN_PROGRAM, mint_policy, holding_policy, entry_token_policy
from desk.token2022_paper import selected, NAME
from desk.model import digest, canonical, load_config
from desk.ownership_acquisition import acquire
from tests import test_token2022_state as layout
from tests import test_paper_cycle as cycle_fixtures, test_ownership_acquisition as acquisition_fixtures
from tests.test_paper_cycle import dump


def account(raw,program=TOKEN_2022):
    return {'owner':program,'executable':False,'data':[base64.b64encode(raw).decode(),'base64']}


def mint_bytes(mint,base=None):
    return layout.mint_bytes(base=base,pointer=mint,metadata_data=layout.metadata(mint=mint))


class PolicyTests(unittest.TestCase):
    def check(self,raw,mint=layout.MINT):
        return mint_policy(account(raw),mint=mint,token_profile_version=1)

    def test_supplied_public_capture_is_structural_only_and_stale_rejects(self):
        from desk.live_observation import ingest_mint, ProviderObservation, ObservationError
        fixture=json.loads((Path(__file__).parents[1]/'fixtures/token2022-metadata-only-public.json').read_text())
        self.assertEqual(fixture['provenance'],'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')
        raw=base64.b64decode(fixture['account']['data'][0]);self.assertEqual(len(raw),391)
        original=copy.deepcopy(fixture)
        policy=mint_policy(fixture['account'],mint=fixture['mint'],token_profile_version=1)
        self.assertEqual(policy['decision'],'PASS_TOKEN_POLICY')
        with patch('socket.socket',side_effect=AssertionError('No URI or provider access')):
            with self.assertRaises(ObservationError):
                ingest_mint(lambda:ProviderObservation('stale-public-fixture',1,
                    {'mint':fixture['mint'],'account':fixture['account'],'slot':fixture['context']['slot']}),
                    mint=fixture['mint'],now=100,token_profile_version=1)
        self.assertEqual(fixture,original)

    def test_actual_identity_required_and_legacy_default_strict(self):
        raw=mint_bytes(layout.MINT)
        self.assertEqual(self.check(raw)['decision'],'PASS_TOKEN_POLICY')
        self.assertEqual(mint_policy(account(raw))['reasons'],['TOKEN_2022_NOT_ALLOWED'])
        for mint in (None,layout.OTHER):
            self.assertEqual(self.check(raw,mint)['decision'],'SKIP')
        self.assertEqual(self.check(mint_bytes(layout.MINT,layout.mint_base(supply=123,decimals=8)))['supply_raw'],'123')

    def test_all_unsupported_extension_types_and_malformed_tails_block(self):
        good=mint_bytes(layout.MINT)
        for kind in list(range(0,30))+[65535]:
            if kind in (18,19):continue
            with self.subTest(kind=kind):self.assertEqual(self.check(good+layout.tlv(kind))['decision'],'SKIP')
        for raw in (good+layout.tlv(18,bytes(64)),good+b'\0',good+bytes(4),good[:-1],good[:165],good+layout.tlv(19,b'x')):
            with self.subTest(length=len(raw)):self.assertEqual(self.check(raw)['decision'],'SKIP')

    def test_authorities_metadata_identity_utf8_and_payloads_block(self):
        variants=[layout.mint_bytes(pointer_authority=layout.OWNER),layout.mint_bytes(pointer=layout.OTHER),
            layout.mint_bytes(metadata_data=layout.metadata(authority=layout.OWNER)),
            layout.mint_bytes(metadata_data=layout.metadata(mint=layout.OTHER)),
            layout.mint_bytes(metadata_data=layout.metadata(pairs=[('x','y')])),
            layout.mint_bytes(metadata_data=layout.metadata(text={'name':b'\xff','symbol':'s','uri':'u'})),
            layout.mint_bytes(base=layout.mint_base(authority=layout.OWNER)),
            layout.mint_bytes(base=layout.mint_base(freeze=layout.OWNER)),
            layout.mint_bytes(base=layout.mint_base(initialized=2)),
            layout.mint_bytes(base=layout.mint_base(supply=0))]
        for raw in variants:
            with self.subTest(raw=raw[-8:].hex()):self.assertEqual(self.check(raw)['decision'],'SKIP')
        modified=account(mint_bytes(layout.MINT));modified['executable']=0
        self.assertEqual(mint_policy(modified,mint=layout.MINT,token_profile_version=1)['decision'],'SKIP')

    def test_immutable_owner_is_not_delegate_close_freeze_or_identity_waiver(self):
        def checked(raw,mint=layout.MINT,owner=layout.OWNER):
            return holding_policy(account(raw),mint,owner,token_profile_version=1)
        self.assertEqual(checked(layout.account_bytes())['decision'],'PASS_HOLDING_POLICY')
        for raw in (layout.account_bytes(delegate=layout.OTHER),layout.account_bytes(close=layout.OTHER),
                    layout.account_bytes(allowance=1),layout.account_bytes(state=2),
                    layout.account_bytes(native=1),layout.account_bytes(entries=b''),
                    layout.account_bytes(entries=layout.tlv(7,b'x')),layout.account_bytes(entries=layout.tlv(7)+layout.tlv(7)),
                    layout.account_bytes(entries=layout.tlv(7)+layout.tlv(2,bytes(8)))):
            self.assertEqual(checked(raw)['decision'],'SKIP')
        self.assertEqual(checked(layout.account_bytes(),owner=layout.OTHER)['decision'],'SKIP')
        self.assertEqual(checked(layout.account_bytes(),mint=layout.OTHER)['decision'],'SKIP')
        self.assertEqual(holding_policy(account(layout.account_bytes()),layout.MINT,layout.OWNER)['decision'],'SKIP')

    def test_config_requires_exact_explicit_paper_versions_no_event_waiver(self):
        cfg={'mode':'paper','paper_signal_policy_version':3,'paper_quote_execution_version':1,'paper_token_profile_version':1}
        self.assertEqual(selected(cfg),1);self.assertEqual(selected({}),0)
        for value in (True,None,0,2,'1',1.0):
            with self.assertRaises(ValueError):selected({**cfg,'paper_token_profile_version':value})
        for changed in ({'mode':'live'},{'paper_signal_policy_version':2},{'paper_quote_execution_version':None}):
            with self.assertRaises(ValueError):selected({**cfg,**changed})
        event={'mint':layout.MINT,'ts':100,'extensions_safe':True,
               'paper_token_profile_version':1,'token_evidence':{'mint':layout.MINT,'observed_at':100,'account':account(mint_bytes(layout.MINT))}}
        self.assertEqual(entry_token_policy(event),['TOKEN_2022_NOT_ALLOWED'])
        self.assertEqual(entry_token_policy(event,cfg),[])
        event['token_evidence']['observed_at']=89
        self.assertEqual(entry_token_policy(event,cfg),['TOKEN_EVIDENCE_STALE'])


class AcquisitionProfileTests(unittest.TestCase):
    def setUp(self):
        self.f=acquisition_fixtures.AcquisitionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.values[0]=account(mint_bytes(self.f.mint))

    def test_persisted_acquisition_profile_and_resume_no_reset(self):
        f=self.f
        result=acquire(f.db,f.evidence,f.rpc,mint=f.mint,paper_token_profile_version=1)
        self.assertNotEqual(result['status'],'UNSUPPORTED_TOKEN',result)
        uid=result['scan_id'];descriptor=f.jobs().descriptor(uid)
        self.assertEqual(descriptor['paper_token_profile_version'],1)
        self.assertEqual(result['report']['token_policy']['decision'],'PASS_TOKEN_POLICY')
        before=dump(f.db),dump(f.evidence)
        with self.assertRaises(ValueError):acquire(f.db,f.evidence,f.rpc,scan_id=uid)
        self.assertEqual((dump(f.db),dump(f.evidence)),before)
        retry=acquire(f.db,f.evidence,f.rpc,scan_id=uid,paper_token_profile_version=1)
        self.assertEqual(retry['status'],'ALREADY_COMPLETE');self.assertEqual(len(f.calls),3)
        self.assertEqual(f.progress().admission(uid)['requests_used'],3)

    def test_hazard_rejects_before_cutoff_and_history(self):
        self.f.values[0]=account(mint_bytes(self.f.mint)+layout.tlv(14,bytes(64)))
        result=acquire(self.f.db,self.f.evidence,self.f.rpc,mint=self.f.mint,paper_token_profile_version=1)
        self.assertEqual(result['status'],'UNSUPPORTED_TOKEN');self.assertEqual(result['provider_calls'],1)


class VerticalProfileTests(unittest.TestCase):
    def setUp(self):
        self.f=cycle_fixtures.PaperCycleTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f;protocol=f.f.protocol
        # Original source-built pool now owns the Token-2022 ATA; no event booleans.
        ata=Pubkey.find_program_address([bytes(protocol.pool),bytes(Pubkey.from_string(TOKEN_2022)),bytes(protocol.mint)],
                                      Pubkey.from_string('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'))[0]
        raw=bytearray(protocol.raw);raw[139:171]=bytes(ata);protocol.raw=bytes(raw)
        rpc=protocol.rpc
        def wrapped(method,params):
            result=copy.deepcopy(rpc(method,params))
            if method=='getMultipleAccounts':
                base=base64.b64decode(result['value'][6]['data'][0])
                result['value'][6]=account(mint_bytes(f.target.mint,base))
                vault=base64.b64decode(result['value'][0]['data'][0])
                result['value'][0]=account(vault+b'\x02'+layout.tlv(7))
            return result
        protocol.rpc=wrapped
        f.cfg={**f.cfg,'paper_token_profile_version':1}
        f.path=Path(f.f.tmp.name)/'token2022-new-experiment.sqlite'
        cycle.initialize(f.path,f.cfg)
        f.http_calls=[];f.sell_output=10_000_000

    def test_actual_entry_mark_exit_restart_and_costs(self):
        # Reuse the existing actual vertical fixture assertions, not duplicate wrappers.
        self.f.test_actual_entry_mark_full_exit_restart_originals_and_costs()
        from desk.experiment_report import experiment_report
        self.assertIsNotNone(experiment_report(self.f.path,now=self.f.f.at))

    def test_actual_partial_full_exit_exact_size_restart(self):
        self.f.test_actual_partial_then_full_exit_exact_sizes_within18()

    def test_saved_profile_cannot_adopt_old_ledger_or_be_removed_on_restart(self):
        f=self.f
        old=Path(f.f.tmp.name)/'cycle.sqlite';before=dump(old)
        with self.assertRaises(cycle.CycleBlocked):cycle._state(old,f.cfg)
        self.assertEqual(dump(old),before)
        entry=f.actual_cycle();self.assertEqual(entry['status'],'COMPLETE',entry)
        before=dump(f.path)
        with self.assertRaises(cycle.CycleBlocked):cycle._state(f.path,{k:v for k,v in f.cfg.items() if k!='paper_token_profile_version'})
        self.assertEqual(dump(f.path),before)
        state=cycle._state(f.path,f.cfg)
        position=state['positions'][f.target.mint]
        self.assertIn('UNRESOLVED_OWNERSHIP_HISTORY',position['entry_policy']['risk_flags'])
        self.assertEqual(position['quote_execution']['status'],'EXECUTION_UNVERIFIED')
        self.assertEqual(json.loads(position['quote_execution']['original_mint_json'])['account']['owner'],TOKEN_2022)

    def test_profile_omission_and_hazard_reject_no_fill(self):
        f=self.f
        # Default collector remains strict, regardless of Token-2022 account bytes.
        f.cfg.pop('paper_token_profile_version');f.path=Path(f.f.tmp.name)/'strict-again.sqlite';cycle.initialize(f.path,f.cfg)
        strict=f.actual_cycle();self.assertFalse(any(x.get('type')=='fill' for x in strict['outcomes']))
        self.assertEqual(cycle._state(f.path,f.cfg)['positions'],{})
