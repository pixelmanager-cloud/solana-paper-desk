"""Synthetic state/trace construction is not a captured successful sell."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desk.programs import schemas, unbase58
from desk.providers import PUMPSWAP, SOL
from desk.instructions import JUPITER, inventory
from desk.security import TOKEN_PROGRAM, base58
from desk.route_coverage import instruction_receipt
from research.pumpswap_vault_effects import check_pumpswap_vault_effects as check


def synthetic():
    spec = next(s for s in schemas()[PUMPSWAP].values() if s['name'] == 'sell')
    names = {e['name']: base58(bytes([i + 20]) * 32) for i, e in enumerate(spec['accounts'])}
    names.update({e['name']: e['address'] for e in spec['accounts'] if e.get('address')})
    names.update(quote_mint=SOL, base_token_program=TOKEN_PROGRAM, quote_token_program=TOKEN_PROGRAM)
    keys = list(dict.fromkeys(names.values()))
    def row(path, program, raw, accounts, height, parent=None, parent_program=None):
        return dict(instruction=path, program=program, data_base64=base64.b64encode(raw).decode(),
                    accounts=accounts, stack_height=height, parent_instruction=parent, parent_program=parent_program)
    outer = row('0', JUPITER, b'opaque', [names['pool_base_token_account'], names['pool_quote_token_account']], 1)
    sell = row('0.0', PUMPSWAP, bytes(spec['discriminator']) + (10).to_bytes(8,'little') + (100).to_bytes(8,'little'),
               [names[e['name']] for e in spec['accounts']], 2, '0', JUPITER)
    rows = [outer, sell]
    transfers = [(names['user_base_token_account'], names['pool_base_token_account'], names['user'], names['base_mint'],10,6)]
    transfers += [(names['pool_quote_token_account'], names[f], names['pool'],SOL,n,9)
                  for f,n in [('user_quote_token_account',100),('protocol_fee_recipient_token_account',10),('coin_creator_vault_ata',10)]]
    for i,(src,dst,authority,mint,n,decimals) in enumerate(transfers,1):
        rows.append(row('0.'+str(i), TOKEN_PROGRAM, b'\x0c'+n.to_bytes(8,'little')+bytes([decimals]),
                        [src,mint,dst,authority],3,'0.0',PUMPSWAP))
    bindings = dict(passed=True,instruction='0.0',account_bindings=names,checked_instructions=[instruction_receipt(sell)])
    value = dict(err=None,preTokenBalances=[],postTokenBalances=[],preBalances=[0]*len(keys),postBalances=[0]*len(keys),accounts=[None]*len(keys))
    for field,mint,pre,post,decimals in [('pool_base_token_account',names['base_mint'],1000,1010,6),('pool_quote_token_account',SOL,5000,4880,9)]:
        index=keys.index(names[field]); reserve=2039280
        for side,amount in [('pre',pre),('post',post)]:
            value[side+'TokenBalances'].append(dict(accountIndex=index,mint=mint,owner=names['pool'],programId=TOKEN_PROGRAM,
                 uiTokenAmount=dict(amount=str(amount),decimals=decimals)))
            value[side+'Balances'][index]=reserve+amount if mint==SOL else reserve
        raw=bytearray(165);raw[:32]=unbase58(mint);raw[32:64]=unbase58(names['pool']);raw[64:72]=post.to_bytes(8,'little');raw[108]=1
        if mint==SOL: raw[109:113]=(1).to_bytes(4,'little');raw[113:121]=reserve.to_bytes(8,'little')
        value['accounts'][index]=dict(owner=TOKEN_PROGRAM,executable=False,lamports=value['postBalances'][index],data=[base64.b64encode(raw).decode(),'base64'])
    return dict(instructions=rows,stack_metadata_verified=True),bindings,value,keys


def mutate_raw(v, keys, names, field, offset, rawbytes):
    account=v['accounts'][keys.index(names[field])]
    raw=bytearray(base64.b64decode(account['data'][0]));raw[offset:offset+len(rawbytes)]=rawbytes
    account['data'][0]=base64.b64encode(raw).decode()


class VaultEffectsTests(unittest.TestCase):
    def assert_rejected(self, args, reason=None):
        result=check(*args);self.assertFalse(result['passed'],result)
        if reason:self.assertTrue(any(reason in r for r in result['reasons']),result)
        self.assertFalse(result['full_route_policy_passed']);self.assertFalse(result['eligible_for_trading'])

    def test_exact_positive_and_source_unchanged(self):
        args=synthetic();original=copy.deepcopy(args);r=check(*args)
        self.assertTrue(r['provider_observed_conservation_agreement'],r);self.assertEqual(args,original)
        self.assertEqual([v['conditional_transfer_delta_raw'] for v in r['vaults']],['10','-120'])
        for field in ('source_authenticated','runtime_cpi_success_verified','runtime_privileges_authenticated','deployed_program_authenticated','ownership_approved','eligible_for_trading','full_route_policy_passed','transaction_policy_ok'):
            self.assertIs(r[field],False)
        self.assertIn('PRE_RAW_ACCOUNT_STATE',r['unverified_effect_context'])

    def test_zero_and_self_transfers_have_no_effect(self):
        args=synthetic();inv,b,_,_=args;row=copy.deepcopy(inv['instructions'][2]);row['instruction']='0.5'
        n=b['account_bindings'];row['accounts']=[n['pool_base_token_account'],n['base_mint'],n['pool_base_token_account'],n['pool']]
        inv['instructions'].append(row)
        self.assertTrue(check(*args)['passed'])
        row['data_base64']=base64.b64encode(b'\x0c'+bytes(8)+b'\x06').decode()
        self.assertTrue(check(*args)['passed'])

    def test_unchecked_transfers_exactly_reconcile(self):
        args=synthetic()
        for row in args[0]['instructions'][2:]:
            raw=base64.b64decode(row['data_base64']);row['data_base64']=base64.b64encode(b'\x03'+raw[1:9]).decode();del row['accounts'][1]
        self.assertTrue(check(*args)['passed'])

    def test_token_residual_with_consistent_post_bytes(self):
        args=synthetic();_,b,v,keys=args;n=b['account_bindings']
        v['postTokenBalances'][0]['uiTokenAmount']['amount']='1011'
        mutate_raw(v,keys,n,'pool_base_token_account',64,(1011).to_bytes(8,'little'))
        self.assert_rejected(args,'VAULT_TOKEN_RESIDUAL')

    def test_raw_amount_identity_owner_state_and_controls(self):
        for offset,data,reason in [(64,(1011).to_bytes(8,'little'),'RAW_AMOUNT'),(0,bytes(32),'RAW_IDENTITY'),(32,bytes(32),'RAW_IDENTITY'),(108,b'\x02','RAW_IDENTITY'),(72,(2).to_bytes(4,'little'),'COPTION'),(72,(1).to_bytes(4,'little'),'ADVERSE_CONTROL'),(121,(1).to_bytes(8,'little'),'ADVERSE_CONTROL'),(129,(1).to_bytes(4,'little'),'ADVERSE_CONTROL')]:
            args=synthetic();mutate_raw(args[2],args[3],args[1]['account_bindings'],'pool_base_token_account',offset,data);self.assert_rejected(args,reason)

    def test_wsol_reserve_and_lamports(self):
        for mode in ('post','pre','rawlamports','nativeflag','reserve','base_lamports'):
            args=synthetic();_,b,v,keys=args;n=b['account_bindings'];i=keys.index(n['pool_quote_token_account'])
            if mode in ('post','pre'):v[mode+'Balances'][i]+=1
            elif mode=='rawlamports':v['accounts'][i]['lamports']+=1
            elif mode=='nativeflag':mutate_raw(v,keys,n,'pool_quote_token_account',109,bytes(4))
            elif mode=='reserve':mutate_raw(v,keys,n,'pool_quote_token_account',113,(2039281).to_bytes(8,'little'))
            else:
                i=keys.index(n['pool_base_token_account']);v['postBalances'][i]+=1;v['accounts'][i]['lamports']+=1
            self.assert_rejected(args)

    def test_missing_endpoints_never_zero(self):
        for field in ('preTokenBalances','postTokenBalances','preBalances','postBalances','accounts'):
            args=synthetic();args[2].pop(field);self.assert_rejected(args)
        for field in ('preTokenBalances','postTokenBalances'):
            args=synthetic();args[2][field].pop();self.assert_rejected(args,'ENDPOINT_MISSING')

    def test_duplicate_keys_metadata_paths_and_bounds(self):
        for mode in ('key','pre','post','path','keysbound','rowsbound','data','pathbound'):
            args=synthetic();inv,_,v,keys=args
            if mode=='key':keys[1]=keys[0]
            elif mode in ('pre','post'):v[mode+'TokenBalances'].append(copy.deepcopy(v[mode+'TokenBalances'][0]))
            elif mode=='path':inv['instructions'].append(copy.deepcopy(inv['instructions'][2]))
            elif mode=='keysbound':keys.extend(base58(bytes([i])*32) for i in range(100,165))
            elif mode=='rowsbound':inv['instructions']*=50
            elif mode=='pathbound':inv['instructions'][2]['instruction']='0.'+'1'*16
            else:inv['instructions'][2]['data_base64']='A'*1645
            self.assert_rejected(args)

    def test_strict_metadata_types_and_ranges(self):
        for field,values in [('accountIndex',[True,-1,2**64,1.0]),('amount',['01','1.0','-1',str(2**64),1]),('decimals',[True,256,-1,6.0])]:
            for bad in values:
                args=synthetic();row=args[2]['preTokenBalances'][0]
                if field=='accountIndex':row[field]=bad
                else:row['uiTokenAmount'][field]=bad
                self.assert_rejected(args)
        for bad in (True,-1,2**64,1.0):
            args=synthetic();args[2]['preBalances'][0]=bad;self.assert_rejected(args,'INVALID_U64')

    def test_raw_transfer_authority_mint_decimals_and_parent(self):
        for mode in ('authority','mint','decimals','parent','parent_program','depth','layout','destination'):
            args=synthetic();row=args[0]['instructions'][2]
            if mode=='authority':row['accounts'][-1]=SOL
            elif mode=='mint':row['accounts'][1]=SOL
            elif mode=='decimals':row['data_base64']=base64.b64encode(b'\x0c'+(10).to_bytes(8,'little')+b'\x09').decode()
            elif mode=='parent':row['parent_instruction']='0'
            elif mode=='parent_program':row['parent_program']=JUPITER
            elif mode=='depth':row['stack_height']=True
            elif mode=='layout':row['data_base64']=base64.b64encode(b'\x0c'+bytes(10)).decode()
            else:row['accounts'][0]=SOL
            self.assert_rejected(args)

    def test_unsupported_vault_touching_operations_even_zero(self):
        for tag in (4,6,7,8,9,10,17):
            args=synthetic();row=copy.deepcopy(args[0]['instructions'][2]);row['instruction']='0.5';row['data_base64']=base64.b64encode(bytes([tag])+bytes(8)).decode();args[0]['instructions'].append(row)
            self.assert_rejected(args,'UNSUPPORTED_VAULT_TOUCHING')
        args=synthetic();args[0]['instructions'][2]['program']=JUPITER;self.assert_rejected(args,'UNSUPPORTED_VAULT_TOUCHING')

    def test_forged_summary_and_receipt_rejected(self):
        for mode in ('names','receipt','rawsell','missingparent','failed'):
            args=synthetic();inv,b,_,_=args
            if mode=='names':b['account_bindings']['pool']=SOL
            elif mode=='receipt':b['checked_instructions']=[]
            elif mode=='rawsell':inv['instructions'][1]['data_base64']=base64.b64encode(bytes(24)).decode();b['checked_instructions']=[instruction_receipt(inv['instructions'][1])]
            elif mode=='missingparent':inv['instructions'].pop(0)
            else:b['passed']=False
            self.assert_rejected(args)

    def test_unchanged_public_capture_remains_unsupported(self):
        path=Path('fixtures/mainnet-sell-simulation.json');raw=path.read_bytes();source=json.loads(raw)
        inv=inventory(source['outer'],source['simulation'],source['keys'],source['wallet'])
        r=check(inv,{'passed':False},source['simulation'],source['keys'])
        self.assertFalse(r['passed']);self.assertIn('SELL_BINDINGS_UNAVAILABLE',r['reasons'])
        self.assertEqual(path.read_bytes(),raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),hashlib.sha256(path.read_bytes()).hexdigest())

    def test_output_paths_invoke_real_checker_without_extra_requests(self):
        # Minimal existing simulation fixture pattern, public wallet key only;
        # malformed route is deliberately not an approved direct Pump sell.
        from desk.simulate import simulate_sell
        from desk.replay_sell import replay_sell
        from desk.evidence import EvidenceStore
        import time
        wallet='7jFim7txj3DpYW8Q5LS29iB5mKjiCaJTcBxZZ4GxD2Sc'
        mint=base58(bytes([7])*32);holding=base58(bytes([8])*32);calls=[]
        raw=bytearray(165);raw[:32]=unbase58(mint);raw[32:64]=unbase58(wallet);raw[64:72]=(1000).to_bytes(8,'little');raw[108]=1
        token=dict(owner=TOKEN_PROGRAM,executable=False,data=[base64.b64encode(raw).decode(),'base64'])
        m=bytearray(82);m[36:44]=(10000).to_bytes(8,'little');m[45]=1
        def rpc(method,params):
            calls.append(method)
            if method=='getMultipleAccounts':return dict(context={'slot':123},value=[dict(owner='11111111111111111111111111111111',executable=False,lamports=100000),token,dict(owner=TOKEN_PROGRAM,executable=False,data=[base64.b64encode(m).decode(),'base64'])])
            if method=='getLatestBlockhash':return {'value':{'blockhash':base58(bytes([9])*32)}}
            if method=='simulateTransaction':return dict(context={'slot':124},value={'err':None,'accounts':[None,token]})
            raise AssertionError(method)
        def quote(*args):return dict(observed_at=int(time.time()),response=dict(inputMint=mint,outputMint=SOL,inAmount='10',otherAmountThreshold='1',swapInstruction=dict(programId=TOKEN_PROGRAM,data='',accounts=[dict(pubkey=wallet,isSigner=True,isWritable=True)])))
        with tempfile.TemporaryDirectory() as tmp:
            store=EvidenceStore(Path(tmp)/'evidence.sqlite')
            with patch('research.pumpswap_vault_effects.check_pumpswap_vault_effects',wraps=check) as actual:
                live=simulate_sell(mint,wallet,holding,10,rpc,quote,capture=store.save)
                replay=replay_sell(store,live['evidence_hash'])
                self.assertEqual(actual.call_count,2)
            self.assertEqual(calls,['getMultipleAccounts','getLatestBlockhash','simulateTransaction'])
            self.assertEqual(live['vault_effects'],replay['vault_effects']);self.assertFalse(replay['vault_effects']['passed'])
            self.assertFalse(live['transaction_policy_ok']);self.assertFalse(replay['eligible_for_trading'])
