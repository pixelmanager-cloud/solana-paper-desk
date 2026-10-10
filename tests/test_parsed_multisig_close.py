"""Synthetic parsed RPC close operands; no multisig/ownership approval.

Agave 2494c1ff56801f08f2d7bbf04d1ef2bded93cc74 parse_token.rs
CloseAccount + parse_signers (lines305-323,937-963) emits owner OR
multisigOwner/signers based on operand count, not authority account state.
"""
import copy
import unittest
from desk.decode import decode,TOKENS
from desk.graduation_witness import extract_graduation
from desk.model import digest
from desk.security import base58
from tests.test_graduation_witness import fixture
from tests import test_migration_slot_intake as intake_fixture

ACCOUNT=base58(bytes([31])*32)
AUTHORITY=base58(bytes([32])*32)
SIGNER=base58(bytes([33])*32)


def close_record(info,program=None,inner=True):
    raw,_,_=fixture()
    raw['transaction']['signatures']=[base58(bytes([34])*64)]
    ix={'programId':program or sorted(TOKENS)[0],'stackHeight':2 if inner else 1,
        'parsed':{'type':'closeAccount','info':copy.deepcopy(info)}}
    raw['transaction']['message']['instructions']=[] if inner else [ix]
    raw['meta']['innerInstructions']=[{'index':0,'instructions':[ix]}] if inner else []
    return raw


class ParsedMultisigCloseTests(unittest.TestCase):
    def info(self):
        return {'account':ACCOUNT,'destination':AUTHORITY,'multisigOwner':AUTHORITY,
                'signers':[AUTHORITY,SIGNER,AUTHORITY]}
    def test_outer_and_cpi_both_programs_preserve_operand_roles_order_duplicates(self):
        for program in sorted(TOKENS):
            for inner in (False,True):
                with self.subTest(program=program,inner=inner):
                    raw=close_record(self.info(),program,inner);before=digest(raw)
                    observed=decode(raw);row=observed['token_account_closures'][0]
                    self.assertIsNone(row['owner'])
                    self.assertEqual(row['multisig_owner'],AUTHORITY)
                    self.assertEqual(row['signers'],[AUTHORITY,SIGNER,AUTHORITY])
                    self.assertEqual(row['instruction'],'0.0' if inner else '0')
                    self.assertEqual(digest(raw),before)
                    self.assertIn('NOT_TRADE_EVIDENCE',observed['limitations'])
    def test_single_authority_output_unchanged(self):
        row=decode(close_record({'account':ACCOUNT,'destination':AUTHORITY,'owner':SIGNER}))['token_account_closures'][0]
        self.assertEqual(row['owner'],SIGNER)
        self.assertNotIn('multisig_owner',row);self.assertNotIn('signers',row)
    def test_missing_conflicting_malformed_and_bounded_signers_reject(self):
        variants=[{}, {'owner':AUTHORITY,**self.info()},
            {k:v for k,v in self.info().items() if k!='multisigOwner'},
            {k:v for k,v in self.info().items() if k!='signers'}]
        variants.extend({**self.info(),'signers':value} for value in (None,AUTHORITY,[],[None],['bad'],[SIGNER]*257))
        variants.extend({**self.info(),'multisigOwner':value} for value in (None,True,'bad'))
        for info in variants:
            with self.subTest(info=info):
                with self.assertRaises((ValueError,KeyError)):decode(close_record(info))
    def test_signer_bound_exact(self):
        info=self.info();info['signers']=[SIGNER]*256
        self.assertEqual(len(decode(close_record(info))['token_account_closures'][0]['signers']),256)
    def test_whole_page_graduation_keeps_valid_target_and_unrelated_close(self):
        target,mint,pool=fixture('migrate_v2');unrelated=close_record(self.info())
        baseline=extract_graduation([target],mint=mint,pool=pool,now=target['blockTime']+1,provenance='SYNTHETIC_TEST_ONLY')
        result=extract_graduation([target,unrelated],mint=mint,pool=pool,now=target['blockTime']+1,provenance='SYNTHETIC_TEST_ONLY')
        self.assertEqual(result['status'],'OBSERVED_MIGRATION',result)
        self.assertEqual(result['witnesses'],baseline['witnesses'])
        self.assertEqual(result['blockers'],[])
    def test_actual_retained_two_page_intake_replay_no_extra_calls(self):
        f=intake_fixture.MigrationSlotTests();self.addCleanup(f.doCleanups);f.setUp()
        unrelated=close_record(self.info());unrelated['blockTime']=f.raw['blockTime']
        f.responses=[{'data':[f.raw,unrelated],'paginationToken':'next'},{'data':[]}]
        result=f.intake()
        self.assertEqual(result['status'],'RETAINED_MIGRATION_WITNESS',result)
        self.assertEqual(result['requests_used'],5)
        self.assertEqual(len(f.opened),2)
        repeat=f.intake();self.assertEqual(repeat['provider_calls'],0)
        self.assertEqual(len(f.opened),2)
        self.assertFalse(result['entry_authorized']);self.assertFalse(result['source_authenticated'])
