"""Synthetic exact-byte profiles plus unchanged public reference; no RPC."""
import copy
import unittest
from desk.decode import decode
from desk.legacy_token_accounting import accounting, RENT, KINDS
from desk.security import base58, TOKEN_PROGRAM, TOKEN_2022
from desk.model import digest
from desk.account_history import account_inventory
from desk.continuity import reconcile_history
from desk.reconcile import reconcile_movements

KEYS=[base58(bytes([i])*32) for i in range(1,6)]
MINT,ACCOUNT,OWNER,DEST,FREEZE=KEYS


def profile(tag, amount=2**64-1, option=0):
    if tag in (0,20):return bytes([tag,6])+bytes([3])*32+bytes([option])+(bytes([5])*32 if option else b''),[MINT,RENT] if tag==0 else [MINT]
    if tag==1:return bytes([tag]),[ACCOUNT,MINT,OWNER,RENT]
    if tag in (16,18):return bytes([tag])+bytes([3])*32,[ACCOUNT,MINT,RENT] if tag==16 else [ACCOUNT,MINT]
    if tag==9:return bytes([tag]),[ACCOUNT,DEST,OWNER]
    if tag in (3,12):accounts=[ACCOUNT,MINT,DEST,OWNER] if tag==12 else [ACCOUNT,DEST,OWNER]
    elif tag in (7,14):accounts=[MINT,ACCOUNT,OWNER]
    else:accounts=[ACCOUNT,MINT,OWNER]
    return bytes([tag])+amount.to_bytes(8,'little')+(b'\x06' if tag in (12,14,15) else b''),accounts


def ix(tag,**kwargs):
    raw,accounts=profile(tag,**kwargs)
    return {'programId':TOKEN_PROGRAM,'accounts':accounts,'data':base58(raw)}


def transaction(instructions):
    keys=[OWNER,MINT,ACCOUNT,DEST,FREEZE,RENT,TOKEN_PROGRAM]
    compiled=[{'programIdIndex':6,'accounts':[keys.index(a) for a in instruction['accounts']],
               'data':instruction['data']} for instruction in instructions]
    return {'slot':10,'blockTime':100,'version':'legacy','transaction':{'signatures':['fixture'],
        'message':{'header':{'numRequiredSignatures':1,'numReadonlySignedAccounts':0,'numReadonlyUnsignedAccounts':1},
                   'accountKeys':keys,'instructions':compiled}},
        'meta':{'err':None,'preTokenBalances':[],'postTokenBalances':[],'innerInstructions':[]}}


class LegacyTokenAccountingTests(unittest.TestCase):
    def test_every_supported_profile_exact_fields_and_no_authority_claim(self):
        for tag,kind in KINDS.items():
            with self.subTest(tag=tag):
                normalized=accounting(ix(tag));self.assertEqual(normalized['status'],'ACCOUNTING_SYNTAX')
                self.assertEqual(normalized['type'],kind);row=normalized['row']
                self.assertNotIn('signer',row);self.assertNotIn('authority_verified',row)
                if tag in (0,20):
                    self.assertEqual(row,{'mint':MINT,'decimals':6,'mint_authority':OWNER,'freeze_authority':None})
                elif tag in (1,16,18):self.assertEqual(row,{'account':ACCOUNT,'mint':MINT,'owner':OWNER})
                elif tag==9:
                    self.assertIsNone(row['owner']);self.assertEqual(row['authority_account'],OWNER)
                    self.assertEqual(row['destination'],DEST)
                elif tag in (3,12):
                    self.assertEqual(row['source'],ACCOUNT);self.assertEqual(row['destination'],DEST)
                    self.assertEqual(row['mint'],MINT if tag==12 else None)
                else:
                    self.assertEqual(row['mint'],MINT);self.assertEqual(row['account'],ACCOUNT)
                    self.assertEqual(row['direction'],'mint' if tag in (7,14) else 'burn')
                if tag in (3,7,8,12,14,15):self.assertEqual(row['amount_raw'],str(2**64-1))
    def test_mint_option_one_byte_exact_none_and_some(self):
        for tag in (0,20):
            self.assertEqual(accounting(ix(tag,option=1))['row']['freeze_authority'],FREEZE)
            for option in (b'\x02',b'\x00\x00\x00\x00',b'\x01'):
                instruction=ix(tag);raw,_=profile(tag)
                instruction['data']=base58(raw[:34]+option)
                self.assertEqual(accounting(instruction)['status'],'MALFORMED')
    def test_all_truncations_and_trailing_bytes_never_create_accounting(self):
        for tag in KINDS:
            raw,_=profile(tag)
            for data in (raw[:-1],raw+b'\x00',raw+b'\xff'):
                with self.subTest(tag=tag,data=data):
                    instruction=ix(tag);instruction['data']=base58(data) if data else ''
                    self.assertNotEqual(accounting(instruction)['status'],'ACCOUNTING_SYNTAX')
    def test_account_count_extra_multisig_and_invalid_keys_rejected(self):
        for tag in KINDS:
            for change in ('missing','extra','invalid'):
                instruction=ix(tag)
                if change=='missing':instruction['accounts'].pop()
                elif change=='extra':instruction['accounts'].append(FREEZE)
                else:instruction['accounts'][0]='bad'
                with self.subTest(tag=tag,change=change):self.assertEqual(accounting(instruction)['status'],'MALFORMED')
    def test_rent_sysvar_binding(self):
        for tag in (0,1,16):
            instruction=ix(tag);instruction['accounts'][-1]=FREEZE
            self.assertEqual(accounting(instruction)['status'],'MALFORMED')
    def test_uint64_zero_and_max_exact_no_float(self):
        for tag in (3,7,8,12,14,15):
            for amount in (0,2**53+1,2**64-1):
                self.assertEqual(accounting(ix(tag,amount=amount))['row']['amount_raw'],str(amount))
    def test_unresolved_controls_unknown_and_token2022_inventory(self):
        tags=(2,4,5,6,10,11,13,19,21,22,255)
        for tag in tags:
            instruction={'programId':TOKEN_PROGRAM,'accounts':[ACCOUNT,OWNER],'data':base58(bytes([tag]))}
            raw=transaction([instruction]);result=decode(raw)
            self.assertEqual(len(result['token_control_operations']),1)
            self.assertEqual(result['token_control_operations'][0]['raw_tag'],tag)
            self.assertIn('UNSUPPORTED_TOKEN_CONTROL_OPERATION',result['limitations'])
            self.assertEqual(result['token_supply_changes'],[])
        for tag in KINDS:
            instruction=ix(tag);instruction['programId']=TOKEN_2022
            self.assertEqual(accounting(instruction)['status'],'UNRESOLVED')
        raw=transaction([ix(7)]);raw['transaction']['message']['accountKeys'][-1]=TOKEN_2022
        self.assertEqual(decode(raw)['token_supply_changes'],[])
        self.assertEqual(len(decode(raw)['token_control_operations']),1)
    def test_malformed_supported_instruction_recorded_without_partial_fields(self):
        raw=transaction([ix(7)]);raw['transaction']['message']['instructions'][0]['data']='invalid!'
        result=decode(raw);self.assertEqual(result['token_supply_changes'],[])
        self.assertEqual(result['token_control_operations'][0]['raw_status'],'MALFORMED')
        self.assertIn('MALFORMED_TOKEN_INSTRUCTION',result['limitations'])
    def test_compiled_normalization_keeps_hash_bytes_and_execution_blockers(self):
        raw=transaction([ix(20),ix(18),ix(7,amount=9)]);before=copy.deepcopy(raw)
        observation=decode(raw)
        self.assertEqual(raw,before);self.assertEqual(observation['payload_hash'],digest(before))
        self.assertEqual(observation['mint_initializations'][0]['mint'],MINT)
        self.assertEqual(observation['token_account_initializations'][0]['account'],ACCOUNT)
        self.assertEqual(observation['token_supply_changes'][0]['amount_raw'],'9')
        self.assertIn('RAW_TOKEN_EXECUTION_UNVERIFIED',observation['limitations'])
        self.assertIn('UNDECODED_TOKEN_INSTRUCTION',observation['limitations'])
        observation['commitment']='finalized_provider_response'  # Synthetic context, not real finality.
        coverage={'address':MINT,'token_accounts_filter':'none','query_coverage_verified':True,'raw_pages_persisted':True}
        inventory=account_inventory(MINT,[observation],coverage)
        self.assertFalse(inventory['initialization_inventory_verified'])
        self.assertIn('ACCOUNT_DISCOVERY_LAUNCH_ANCHOR_REQUIRED',inventory['reasons'])
        self.assertFalse(reconcile_movements(MINT,observation)['passed'])
        self.assertFalse(reconcile_history(MINT,[observation])['passed'])
    def test_checked_mint_program_decimals_and_init_bindings(self):
        for tag in (0,20,1,16,18,7,8,12,14,15):
            for mismatch in ('mint','program','decimals'):
                if mismatch=='decimals' and tag not in (0,20,12,14,15):continue
                raw=transaction([ix(tag)])
                balance={'accountIndex':2,'mint':MINT,'programId':TOKEN_PROGRAM,'owner':OWNER,
                         'uiTokenAmount':{'amount':'0','decimals':6}}
                if mismatch=='mint':
                    if tag in (0,20):continue
                    balance['mint']=FREEZE
                elif mismatch=='program':balance['programId']=TOKEN_2022
                else:balance['uiTokenAmount']['decimals']=5
                raw['meta']['postTokenBalances']=[balance]
                with self.subTest(tag=tag,mismatch=mismatch),self.assertRaises(ValueError):decode(raw)
    def test_checked_decimals_field_and_unchecked_no_invented_mint(self):
        for tag in (12,14,15):
            result=decode(transaction([ix(tag)]))
            row=(result['transfers'] or result['token_supply_changes'])[0]
            self.assertEqual(row['decimals'],6)
        self.assertIsNone(decode(transaction([ix(3)]))['transfers'][0]['mint'])
    def test_transient_raw_init_close_never_establishes_lifetime_from_syntax(self):
        observation=decode(transaction([ix(18),ix(9)]))
        observation['commitment']='finalized_provider_response'
        self.assertEqual(observation['token_account_closures'][0]['owner'],None)
        self.assertEqual(observation['token_account_initializations'][0]['owner'],OWNER)
        self.assertFalse(reconcile_history(MINT,[observation])['passed'])
        self.assertFalse(reconcile_movements(MINT,observation)['passed'])

    def test_checked_decimals_byte_range_and_missing_balance_never_filled(self):
        for decimals in (0,255):
            raw=transaction([ix(12)])
            data,_=profile(12)
            raw['transaction']['message']['instructions'][0]['data']=base58(data[:-1]+bytes([decimals]))
            result=decode(raw)
            self.assertEqual(result['transfers'][0]['decimals'],decimals)
            self.assertEqual(result['token_deltas'],[])
            self.assertIn('UNDECODED_TOKEN_INSTRUCTION',result['limitations'])

    def test_failed_transaction_has_no_normalized_lifetimes_or_controls(self):
        raw=transaction([ix(20),ix(18),ix(7)]);raw['meta']['err']={'InstructionError':[0,'fixture']}
        result=decode(raw)
        self.assertEqual(result['mint_initializations'],[]);self.assertEqual(result['token_account_initializations'],[])
        self.assertEqual(result['token_supply_changes'],[])
