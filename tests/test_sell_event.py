import base64,copy,json,unittest
from pathlib import Path
from desk.sell_event import check_sell_event
from desk.programs import instruction,unbase58
from desk.providers import PUMPSWAP
class SellEventTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1];self.capture=json.loads((root/'fixtures/mainnet-sell-event.json').read_text())
        self.fields=copy.deepcopy(self.capture['decoded']['fields']);self.schema=json.loads((root/'desk/schemas/pump_amm_sdk_2_1_0.json').read_text())
        f=self.fields;names={k:f[k] for k in ('pool','user','user_base_token_account','user_quote_token_account','protocol_fee_recipient','protocol_fee_recipient_token_account')};names['event_authority']=self.capture['instruction']['accounts'][0]
        self.bind={'passed':True,'instruction':'2.0','account_bindings':names}
        self.pool={'coin_creator':f['coin_creator'],'base_reserve_raw':str(f['pool_base_token_reserves']),'quote_reserve_raw':str(f['pool_quote_token_reserves'])}
        self.fees={'passed':True,'expected':dict(zip(('gross_quote_raw','lp_fee_raw','protocol_total_raw','creator_fee_raw','user_output_raw'),map(str,(f['quote_amount_out'],f['lp_fee'],f['protocol_fee'],f['coin_creator_fee'],f['user_quote_amount_out']))))}
        self.rec={'passed':True,'input_raw':str(f['base_amount_in']),'buyback_fee_raw':str(f['buyback_fee'])}
        for k in ('cashback','holder_rewards','virtual_quote_reserves','creator_fee_unclaimed'):self.fields[k]=0
        self.fields['can_boost']=False
        self.sell={'program':PUMPSWAP,'instruction':'2.0','data_base64':base64.b64encode(bytes(16)+f['min_quote_amount_out'].to_bytes(8,'little')).decode()}
    def row(self):
        raw=bytes.fromhex('e445a52e51cb9a1d')+bytes(next(e['discriminator'] for e in self.schema['events'] if e['name']=='SellEvent'))
        for field in next(t['type']['fields'] for t in self.schema['types'] if t['name']=='SellEvent'):
            t=field['type'];v=self.fields[field['name']]
            raw+=unbase58(v) if t=='pubkey' else bytes([v]) if t=='bool' else v.to_bytes(int(t[1:])//8,'little',signed=t[0]=='i')
        return {'program':PUMPSWAP,'instruction':'2.9','parent_instruction':'2.0','parent_program':PUMPSWAP,'stack_height':3,'accounts':[self.bind['account_bindings']['event_authority']],'data_base64':base64.b64encode(raw).decode()}
    def check(self,row=None):return check_sell_event({'stack_metadata_verified':True,'instructions':[self.sell,row or self.row()]},self.bind,self.fees,self.rec,self.pool)
    def test_public_appended_fee_field_decodes_without_prefix_approval(self):
        decoded=instruction(self.capture['instruction']);self.assertTrue(decoded['schema_complete']);self.assertIn('creator_fee_unclaimed',decoded['fields'])
    def test_synthetic_bound_event_consistency_is_not_full_approval(self):
        r=self.check();self.assertTrue(r['passed']);self.assertFalse(r['full_route_policy_passed'])
    def test_forged_event_recipient_and_proceeds_fail(self):
        self.fields['user']=self.fields['pool'];self.assertIn('SELL_EVENT_IDENTITY_MISMATCH',self.check()['reasons'])
        self.fields['user']=self.bind['account_bindings']['user'];self.fields['user_quote_amount_out']+=1
        self.assertIn('SELL_EVENT_AMOUNT_MISMATCH',self.check()['reasons'])
    def test_wrong_callback_authority_caller_and_extra_accounts_fail(self):
        row=self.row();row['accounts']=[self.fields['user']];self.assertFalse(self.check(row)['passed'])
        row=self.row();row['parent_instruction']='2.8';self.assertFalse(self.check(row)['passed'])
        row=self.row();row['accounts'].append(self.fields['user']);self.assertFalse(self.check(row)['passed'])
    def test_unknown_bytes_or_missing_new_field_fail(self):
        row=self.row();raw=base64.b64decode(row['data_base64'])
        for changed in (raw+b'\0',raw[:-8]):
            row['data_base64']=base64.b64encode(changed).decode();self.assertFalse(self.check(row)['passed'])
    def test_unclaimed_fee_behavior_is_not_silently_supported(self):
        self.fields['creator_fee_unclaimed']=1;self.assertIn('SELL_EVENT_SPECIAL_FEE_PROFILE_UNSUPPORTED',self.check()['reasons'])
