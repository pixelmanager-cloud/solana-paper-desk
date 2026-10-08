"""Pinned Jupiter exact-input V2 argument/account checks for a narrow sell profile."""
from .route_coverage import outer_receipt
import base64,hashlib,json
from pathlib import Path
from functools import lru_cache
from .programs import BorshReader,address
from .instructions import JUPITER,ATA
from .security import TOKEN_PROGRAM
from .providers import SOL

@lru_cache(maxsize=1)
def route_schema():
    root=Path(__file__).parent/'schemas';manifest=json.loads((root/'jupiter_manifest.json').read_text())
    raw=(root/manifest['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=manifest['sha256']:raise ValueError('Jupiter schema checksum mismatch')
    schema=json.loads(raw)
    if schema['address']!=JUPITER or manifest['program']!=JUPITER:raise ValueError('Jupiter schema identity mismatch')
    return schema

class RouteReader(BorshReader):
    def read(self,t,depth=0):
        if depth>12:raise ValueError('Route nesting limit')
        if isinstance(t,dict) and 'defined' in t:
            spec=self.types[t['defined']['name']]
            if spec['kind']=='enum':
                index=self.take(1)[0];variants=spec['variants']
                if index>=len(variants):raise ValueError('Unknown route variant')
                variant=variants[index];fields=variant.get('fields',[])
                if fields and all(isinstance(f,dict) and 'name' in f for f in fields):
                    values={f['name']:self.read(f['type'],depth+1) for f in fields}
                else:values=[self.read(f,depth+1) for f in fields]
                return {'variant':variant['name'],'fields':values}
        return super().read(t,depth)


def check_sell_route(ix,mint,wallet,holding,amount,minimum_out):
    from solders.pubkey import Pubkey
    for key in (mint,wallet,holding):address(key)
    if type(amount) is not int or type(minimum_out) is not int or not 0<amount<2**64 or not 0<minimum_out<2**64:
        raise ValueError('Invalid route amounts')
    result={'passed':False,'full_route_policy_passed':False,'reasons':[]}
    reasons=result['reasons']
    if ix.get('programId')!=JUPITER:return {**result,'reasons':['UNSUPPORTED_ROUTER_PROGRAM']}
    schema=route_schema();raw=base64.b64decode(ix['data'],validate=True)
    spec=next((x for x in schema['instructions'] if bytes(x['discriminator'])==raw[:8]),None)
    if not spec or spec['name'] not in ('route_v2','shared_accounts_route_v2'):
        return {**result,'reasons':['UNSUPPORTED_ROUTER_INSTRUCTION']}
    reader=RouteReader(raw[8:],{x['name']:x['type'] for x in schema['types']})
    args={x['name']:reader.read(x['type']) for x in spec['args']}
    if reader.pos!=len(reader.data):reasons.append('ROUTE_TRAILING_BYTES')
    accounts=ix['accounts']
    if len(accounts)<len(spec['accounts']):return {**result,'reasons':['ROUTE_ACCOUNTS_MISSING']}
    named={x['name']:accounts[i]['pubkey'] for i,x in enumerate(spec['accounts'])}
    for i,entry in enumerate(spec['accounts']):
        account=accounts[i];address(account['pubkey'])
        if entry.get('address') and account['pubkey']!=entry['address']:reasons.append('ROUTER_FIXED_ACCOUNT_MISMATCH')
        if entry.get('signer') and account.get('isSigner') is not True:reasons.append('ROUTE_REQUIRED_SIGNER_MISSING')
        if entry.get('writable') and account.get('isWritable') is not True:reasons.append('ROUTE_WRITABLE_ACCOUNT_MISMATCH')
    if any(a.get('isSigner') and a['pubkey']!=wallet for a in accounts):reasons.append('ROUTE_UNEXPECTED_SIGNER')
    expected_output=str(Pubkey.find_program_address([bytes(Pubkey.from_string(wallet)),bytes(Pubkey.from_string(TOKEN_PROGRAM)),bytes(Pubkey.from_string(SOL))],Pubkey.from_string(ATA))[0])
    source=named.get('source_token_account',named.get('user_source_token_account'))
    dest=named.get('destination_token_account') if spec['name'].startswith('shared_') else named.get('user_destination_token_account')
    checks={'user_transfer_authority':wallet,'source_mint':mint,'destination_mint':SOL,
            'source_token_program':TOKEN_PROGRAM,'destination_token_program':TOKEN_PROGRAM,'program':JUPITER}
    if any(named.get(k)!=v for k,v in checks.items()) or source!=holding or dest!=expected_output:
        reasons.append('ROUTE_IDENTITY_OR_RECIPIENT_MISMATCH')
    if spec['name']=='route_v2' and named.get('destination_token_account')!=JUPITER:
        reasons.append('OPTIONAL_DESTINATION_NOT_ALLOWED')
    if 'id' in args:
        authority=str(Pubkey.find_program_address([b'authority',bytes([args['id']])],Pubkey.from_string(JUPITER))[0])
        if named.get('program_authority')!=authority:reasons.append('ROUTER_AUTHORITY_PDA_MISMATCH')
    if args['in_amount']!=amount:reasons.append('ROUTE_INPUT_AMOUNT_MISMATCH')
    if not 0<=args['slippage_bps']<=100:reasons.append('ROUTE_SLIPPAGE_EXCEEDS_BUDGET')
    if args['platform_fee_bps'] or args['positive_slippage_bps']:reasons.append('ROUTE_EXTRA_FEES_NOT_ALLOWED')
    floor=args['quoted_out_amount']*max(0,10000-args['slippage_bps'])//10000
    if floor<minimum_out:reasons.append('ROUTE_MINIMUM_BELOW_REQUIRED')
    plan=args['route_plan']
    if len(plan)!=1 or plan[0]['swap']['variant']!='PumpSwapSell' or plan[0]['bps']!=10000 or plan[0]['input_index']!=0 or plan[0]['output_index']!=1:
        reasons.append('ROUTE_OUTSIDE_DIRECT_PUMPSWAP_SELL_PROFILE')
    result.update({'passed':not reasons,'reasons':sorted(set(reasons)),'instruction':spec['name'],'arguments':args,
        'checked_instruction':outer_receipt(ix) if not reasons else None,'minimum_out_floor_raw':str(floor),'source_account':source,'destination_account':dest,
        'notice':'Router arguments and user bindings only. Inner AMM pool bindings, setup/cleanup recipients and complete instruction policy remain required.'})
    return result
