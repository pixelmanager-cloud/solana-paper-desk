"""Disconnected syntax fixtures; saved CPIs remain parsed-only, never raw proof."""
import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path
from research.token2022_instructions import (
    normalize_token2022_instruction as normalize, TOKEN_2022,
    INITIALIZE_METADATA, UPDATE_METADATA_AUTHORITY, MAX_STRING_BYTES, MAX_RAW_BYTES,
)

ALPHABET='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def key(raw):
    number=int.from_bytes(raw,'big');text=''
    while number:number,digit=divmod(number,58);text=ALPHABET[digit]+text
    return '1'*(len(raw)-len(raw.lstrip(b'\0')))+text

M,A,T,H,C,U=[key(bytes([i])*32) for i in range(1,7)]
TOKEN_LEGACY='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
SYSTEM='11111111111111111111111111111111'
ATA='ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
PUMP='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
FLAGS=('authority_authenticated','evidence_authenticated','caller_verified','caller_privileges_verified',
       'cpi_success_verified','effect_order_verified','deployed_code_verified','account_state_verified',
       'lifecycle_verified','ownership_approved','token2022_approved','eligible_for_trading')


def strings(*values):
    return b''.join(len(value.encode()).to_bytes(4,'little')+value.encode() for value in values)


def profiles():
    return {
        'initializeMetadataPointer':(bytes([39,0])+b'\0'*32+bytes([1])*32,[M]),
        'initializeMint2':(bytes([20,6])+bytes([2])*32+b'\0',[M]),
        'getAccountDataSize':(bytes([21,7,0]),[M]),
        'initializeImmutableOwner':(bytes([22]),[T]),
        'initializeAccount3':(bytes([18])+bytes([5])*32,[T,M]),
        'initializeTokenMetadata':(INITIALIZE_METADATA+strings('Name','SYMBOL','https://invalid.test/uri'),[M,A,M,A]),
        'updateTokenMetadataAuthority':(UPDATE_METADATA_AUTHORITY+b'\0'*32,[M,A]),
        'mintTo':(bytes([7])+(10**15).to_bytes(8,'little'),[M,T,A]),
        'setAuthority':(bytes([6,0,0]),[M,A]),
        'transferChecked':(bytes([12])+(17376518166910).to_bytes(8,'little')+b'\x06',[T,M,H,C]),
    }


class Token2022InstructionTests(unittest.TestCase):
    def assert_unapproved(self,result):
        for flag in FLAGS:self.assertIs(result[flag],False,flag)
        self.assertIn('TOKEN_2022_UNSUPPORTED',result['runtime_blockers'])
    def test_exact_observed_profiles_normalize_only_supplied_syntax(self):
        for kind,(raw,accounts) in profiles().items():
            with self.subTest(kind=kind):
                result=normalize(TOKEN_2022,raw=raw,accounts=accounts)
                self.assertEqual(result['status'],'SYNTAX_ONLY');self.assertTrue(result['raw_layout_complete'])
                self.assertEqual(result['raw_operation']['kind'],kind)
                self.assertEqual(result['raw_sha256'],hashlib.sha256(raw).hexdigest())
                self.assertEqual(result['raw_hex'],raw.hex());self.assertEqual(result['raw_length'],len(raw))
                self.assertEqual(result['account_keys'],accounts);self.assert_unapproved(result)
    def test_account_roles_and_amounts_are_ordered_and_exact(self):
        def operation(kind):
            raw,accounts=profiles()[kind];return normalize(TOKEN_2022,raw=raw,accounts=accounts)['raw_operation']
        pointer=operation('initializeMetadataPointer')
        self.assertEqual(pointer['pointer_authority'],{'presence':'explicit_none','value':None})
        self.assertEqual(pointer['metadata_address'],{'presence':'explicit_key','value':M})
        init=operation('initializeMint2')
        self.assertEqual((init['mint'],init['decimals'],init['mint_authority']),(M,6,A))
        self.assertEqual(init['freeze_authority'],{'presence':'explicit_none','value':None})
        account=operation('initializeAccount3')
        self.assertEqual((account['account'],account['mint'],account['declared_owner']),(T,M,C))
        metadata=operation('initializeTokenMetadata')
        self.assertEqual((metadata['metadata'],metadata['update_authority_account'],metadata['mint'],metadata['mint_authority_account']),(M,A,M,A))
        mint=operation('mintTo');self.assertEqual((mint['mint'],mint['account'],mint['mint_authority_account'],mint['amount_raw']),(M,T,A,str(10**15)))
        transfer=operation('transferChecked')
        self.assertEqual((transfer['source'],transfer['mint'],transfer['destination'],transfer['transfer_authority_account'],transfer['amount_raw'],transfer['decimals']),
                         (T,M,H,C,'17376518166910',6))
    def test_every_truncated_prefix_and_suffix_unresolved(self):
        for kind,(raw,accounts) in profiles().items():
            for bad in [raw[:i] for i in range(len(raw))]+[raw+b'\0',raw+b'\xff',raw+b'\0'*32]:
                with self.subTest(kind=kind,length=len(bad)):
                    result=normalize(TOKEN_2022,raw=bad,accounts=accounts)
                    self.assertFalse(result['normalization_complete']);self.assertIsNone(result['raw_operation'])
                    self.assert_unapproved(result)
    def test_exact_counts_no_multisig_or_missing_accounts(self):
        for kind,(raw,accounts) in profiles().items():
            for bad in (None,[],accounts[:-1],accounts+[U],accounts+[U,C]):
                with self.subTest(kind=kind,accounts=bad):
                    result=normalize(TOKEN_2022,raw=raw,accounts=bad)
                    self.assertFalse(result['normalization_complete']);self.assertIsNone(result['raw_operation'])
    def test_wrong_program_never_reuses_legacy_or_foreign_profile(self):
        for program in (TOKEN_LEGACY,SYSTEM,PUMP,ATA,None,True):
            for raw,accounts in profiles().values():
                result=normalize(program,raw=raw,accounts=accounts)
                self.assertIn('TOKEN2022_PROGRAM_BINDING_MISMATCH',result['reasons'])
                self.assertIsNone(result['raw_operation']);self.assert_unapproved(result)
    def test_raw_type_empty_and_inspection_bound(self):
        for raw in (None,b'',bytearray(b'\x16'),'16',[22],b'\x16'*(MAX_RAW_BYTES+1)):
            result=normalize(TOKEN_2022,raw=raw,accounts=[T])
            self.assertFalse(result['normalization_complete']);self.assertIsNone(result['raw_operation'])
    def test_extension_zero_key_and_spl_some_zero_are_not_interchangeable(self):
        result=normalize(TOKEN_2022,raw=b'\x27\0'+b'\0'*64,accounts=[M])['raw_operation']
        self.assertEqual(result['pointer_authority'],{'presence':'explicit_none','value':None})
        self.assertEqual(result['metadata_address'],{'presence':'explicit_none','value':None})
        raw=b'\x14\x06'+bytes([2])*32+b'\x01'+b'\0'*32
        result=normalize(TOKEN_2022,raw=raw,accounts=[M])['raw_operation']
        self.assertEqual(result['freeze_authority'],{'presence':'explicit_key','value':SYSTEM})
        result=normalize(TOKEN_2022,raw=b'\x06\0\x01'+b'\0'*32,accounts=[M,A])['raw_operation']
        self.assertEqual(result['new_mint_authority'],{'presence':'explicit_key','value':SYSTEM})
        result=normalize(TOKEN_2022,raw=UPDATE_METADATA_AUTHORITY+b'\0'*32,accounts=[M,A])['raw_operation']
        self.assertEqual(result['new_metadata_authority'],{'presence':'explicit_none','value':None})
    def test_explicit_active_keys_preserved_without_approval_or_authority_collapse(self):
        pointer=normalize(TOKEN_2022,raw=b'\x27\0'+bytes([2])*32+bytes([6])*32,accounts=[M])
        self.assertEqual(pointer['raw_operation']['pointer_authority']['value'],A)
        self.assertEqual(pointer['raw_operation']['metadata_address']['value'],U);self.assert_unapproved(pointer)
        metadata=normalize(TOKEN_2022,raw=INITIALIZE_METADATA+strings('n','s','u'),accounts=[M,U,M,A])
        self.assertEqual(metadata['raw_operation']['update_authority_account'],U)
        self.assertEqual(metadata['raw_operation']['mint_authority_account'],A);self.assert_unapproved(metadata)
        update=normalize(TOKEN_2022,raw=UPDATE_METADATA_AUTHORITY+bytes([6])*32,accounts=[M,A])
        self.assertEqual(update['raw_operation']['new_metadata_authority']['value'],U);self.assert_unapproved(update)
    def test_invalid_options_one_byte_vs_four_byte_or_optional32(self):
        for option in (b'\x02',b'\0'*4,b'\x01',b'\x01'+b'\0'*31,b'\x01'+b'\0'*33):
            for prefix,accounts in ((b'\x14\x06'+bytes([2])*32,[M]),(b'\x06\0',[M,A])):
                result=normalize(TOKEN_2022,raw=prefix+option,accounts=accounts)
                self.assertFalse(result['normalization_complete'])
        for bad in (b'\x27\0\0'+bytes([1])*32,UPDATE_METADATA_AUTHORITY+b'\0'):
            self.assertFalse(normalize(TOKEN_2022,raw=bad,accounts=[M] if bad[0]==39 else [M,A])['normalization_complete'])
    def test_unknown_operations_roles_and_size_extension_profiles_unresolved(self):
        for raw in (b'\xff',b'\x27\x01'+bytes([1])*32,b'\x04'+b'\0'*8,
                    b'\x06\x01\0',b'\x06\x02\0',b'\x06\x03\0',b'\x06\xff\0',
                    b'\x15',b'\x15\x07',b'\x15\x08\0',b'\x15\x07\0\x07\0',b'\x15\x07\0\x0f\0'):
            accounts=[M,A] if raw[0]==6 else [M]
            result=normalize(TOKEN_2022,raw=raw,accounts=accounts)
            self.assertEqual(result['status'],'UNRESOLVED');self.assertIsNone(result['raw_operation'])
    def test_metadata_discriminators_borsh_and_self_mint_binding(self):
        self.assertEqual(hashlib.sha256(b'spl_token_metadata_interface:initialize_account').digest()[:8],INITIALIZE_METADATA)
        self.assertEqual(hashlib.sha256(b'spl_token_metadata_interface:update_the_authority').digest()[:8],UPDATE_METADATA_AUTHORITY)
        raw=INITIALIZE_METADATA+strings('Name','SYM','uri')
        for accounts in ([M,A,U,A],[M,M,A,A],[M,A,A,M]):
            self.assertFalse(normalize(TOKEN_2022,raw=raw,accounts=accounts)['normalization_complete'])
        for bad in (INITIALIZE_METADATA+(MAX_STRING_BYTES+1).to_bytes(4,'little'),
                    INITIALIZE_METADATA+(2**32-1).to_bytes(4,'little'),
                    INITIALIZE_METADATA+b'\x01\0\0\0\xff'+strings('s','u'),
                    INITIALIZE_METADATA+strings('n','s','u')+b'\0\0\0\0'):
            self.assertFalse(normalize(TOKEN_2022,raw=bad,accounts=[M,A,M,A])['normalization_complete'])
        good=INITIALIZE_METADATA+strings('한글','','u'*MAX_STRING_BYTES)
        self.assertTrue(normalize(TOKEN_2022,raw=good,accounts=[M,A,M,A])['normalization_complete'])
    def test_uint64_boundaries_and_decimal_byte_exact(self):
        for amount in (0,2**53+1,2**64-1):
            for decimals in (0,255):
                result=normalize(TOKEN_2022,raw=b'\x0c'+amount.to_bytes(8,'little')+bytes([decimals]),accounts=[T,M,H,C])['raw_operation']
                self.assertEqual(result['amount_raw'],str(amount));self.assertEqual(result['decimals'],decimals)
            result=normalize(TOKEN_2022,raw=b'\x07'+amount.to_bytes(8,'little'),accounts=[M,T,A])['raw_operation']
            self.assertEqual(result['amount_raw'],str(amount))
    def test_account_keys_metas_indices_and_claimed_signers_are_untrusted(self):
        raw,accounts=profiles()['mintTo']
        for bad in ([1,2,3],[M,T,'bad'],[M,T,{'pubkey':A,'isSigner':1}],[M,T,{'pubkey':A,'isSigner':True,'unknown':True}]):
            self.assertFalse(normalize(TOKEN_2022,raw=raw,accounts=bad)['normalization_complete'])
        metas=[{'pubkey':a,'isSigner':True,'isWritable':True} for a in accounts]
        result=normalize(TOKEN_2022,raw=raw,accounts=metas,evidence={'caller_program':PUMP,'authority_authenticated':True})
        self.assertTrue(result['normalization_complete']);self.assertEqual(result['account_witness'],metas)
        self.assert_unapproved(result)
    def test_wrong_caller_hints_never_become_cpi_privileges_or_effect_order(self):
        for caller in (PUMP,ATA,SYSTEM,None):
            for kind in ('initializeImmutableOwner','initializeMint2'):
                raw,accounts=profiles()[kind]
                evidence={'caller_program':caller,'instruction_path':'2.7','stack_height':3,'err':None,'cpi_success_verified':True}
                result=normalize(TOKEN_2022,raw=raw,accounts=accounts,evidence=evidence)
                self.assert_unapproved(result);self.assertEqual(result['untrusted_evidence'],evidence)
                self.assertNotIn('parent_instruction',result);self.assertNotIn('effect_order',result)
    def test_parsed_explicit_none_missing_and_conflicts_never_synthesize(self):
        for info in ({},{'newAuthority':None},{'newAuthority':U}):
            parsed={'type':'setAuthority','info':info}
            result=normalize(TOKEN_2022,parsed=parsed,accounts=None)
            self.assertFalse(result['normalization_complete']);self.assertIsNone(result['raw_operation'])
            self.assertEqual(result['parsed_field_presence']['newAuthority'],'newAuthority' in info)
            self.assertEqual(result['parsed_witness'],parsed);self.assertIn('RAW_INSTRUCTION_UNAVAILABLE',result['reasons'])
        raw,accounts=profiles()['setAuthority'];parsed={'type':'setAuthority','info':{'newAuthority':U}}
        result=normalize(TOKEN_2022,raw=raw,accounts=accounts,parsed=parsed)
        self.assertEqual(result['raw_operation']['new_mint_authority']['value'],None)
        self.assertIsNone(result['representations_match']);self.assert_unapproved(result)
    def test_missing_parsed_and_explicit_null_are_distinct_witnesses(self):
        absent=normalize(TOKEN_2022)
        explicit=normalize(TOKEN_2022,parsed=None)
        self.assertFalse(absent['parsed_supplied']);self.assertTrue(explicit['parsed_supplied'])
        self.assertIsNone(absent['raw_operation']);self.assertIsNone(explicit['raw_operation'])
        self.assert_unapproved(absent);self.assert_unapproved(explicit)

    def test_saved_cp_is_stay_missing_raw_and_keep_original_provenance(self):
        path=Path(__file__).resolve().parents[1]/'fixtures/mainnet-launch.json'
        original=path.read_bytes();fixture=json.loads(original)
        self.assertEqual(hashlib.sha256(original).hexdigest(),'d80cf9876fdb3e9b465e4103fa14bf2baaf09483dad9a27a57ff7339624b3bf2')
        container=fixture['payload']['params']['result']['transaction'];seen={}
        for group in container['meta']['innerInstructions']:
            for i,ix in enumerate(group['instructions']):
                if ix.get('programId')!=TOKEN_2022:continue
                self.assertNotIn('data',ix);self.assertNotIn('accounts',ix)
                evidence={'provenance':fixture['provenance'],'source_sha256':hashlib.sha256(original).hexdigest(),
                          'instruction_path':f"{group['index']}.{i}",'stack_height':ix['stackHeight']}
                result=normalize(ix['programId'],parsed=ix['parsed'],evidence=evidence)
                self.assertIn('RAW_INSTRUCTION_UNAVAILABLE',result['reasons']);self.assertIsNone(result['raw_operation'])
                self.assertFalse(result['raw_witness_present']);self.assert_unapproved(result)
                self.assertEqual(result['untrusted_evidence'],evidence);seen[evidence['instruction_path']]=result
        self.assertEqual(len(seen),13)
        self.assertFalse(seen['2.1']['parsed_field_presence']['authority'])
        self.assertFalse(seen['2.2']['parsed_field_presence']['freezeAuthority'])
        self.assertTrue(seen['2.11']['parsed_field_presence']['newAuthority'])
        self.assertIsNone(seen['2.11']['parsed_witness']['info']['newAuthority'])
        self.assertEqual(path.read_bytes(),original)
    def test_witnesses_not_mutated_and_hashes_bind_exact_input(self):
        raw,accounts=profiles()['transferChecked'];evidence={'request_evidence_hash':'fixture','nested':{'caller':PUMP}}
        parsed={'type':'transferChecked','info':{'mint':M}}
        before=copy.deepcopy((accounts,evidence,parsed));result=normalize(TOKEN_2022,raw=raw,accounts=accounts,evidence=evidence,parsed=parsed)
        self.assertEqual((accounts,evidence,parsed),before)
        expected=hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(result['evidence_sha256'],expected)
        result['untrusted_evidence']['nested']['caller']=SYSTEM
        self.assertEqual(evidence,before[1])
    def test_research_only_import_boundary_and_source_pins(self):
        path=Path(__file__).resolve().parents[1]/'research/token2022_instructions.py'
        tree=ast.parse(path.read_text())
        imports=[n.module if isinstance(n,ast.ImportFrom) else a.name for n in ast.walk(tree) if isinstance(n,(ast.Import,ast.ImportFrom)) for a in (n.names if isinstance(n,ast.Import) else [None])]
        self.assertEqual(set(imports),{'copy','hashlib','json'})
        result=normalize(TOKEN_2022)
        self.assertEqual(len(result['source_provenance']),5)
        self.assertTrue(all(len(pin['git_blob'])==40 and len(pin['commit'])==40 for pin in result['source_provenance']))
    def test_immutable_owner_is_not_legacy_noop_or_actual_state_proof(self):
        raw,accounts=profiles()['initializeImmutableOwner'];result=normalize(TOKEN_2022,raw=raw,accounts=accounts)
        self.assertIn('not_legacy_noop',result['raw_operation']['reference_semantics'])
        self.assertFalse(result['account_state_verified']);self.assertFalse(result['lifecycle_verified'])
        legacy=normalize(TOKEN_LEGACY,raw=raw,accounts=accounts)
        self.assertIsNone(legacy['raw_operation'])
    def test_invalid_evidence_cannot_authenticate_or_complete_normalization(self):
        for evidence in ([],{'amount':float('nan')},{'field':'x'*65537}):
            result=normalize(TOKEN_2022,raw=b'\x16',accounts=[T],evidence=evidence)
            self.assertIn('UNTRUSTED_EVIDENCE_SHAPE_INVALID',result['reasons']);self.assert_unapproved(result)
