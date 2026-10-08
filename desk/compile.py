"""Compile provider instructions with verified lookup tables and null signatures."""
import base64
from .programs import address
from .security import account_bytes


def route_instructions(response):
    if response.get('tipInstruction'):raise ValueError('Tipped routes not allowed')
    items=[*response.get('computeBudgetInstructions',[]),*response.get('setupInstructions',[]),response['swapInstruction']]
    if response.get('cleanupInstruction'):items.append(response['cleanupInstruction'])
    items.extend(response.get('otherInstructions',[]))
    if not 1<=len(items)<=64:raise ValueError('Instruction budget exceeded')
    return items


def compile_unsigned(response,wallet,blockhash,rpc):
    from solders.pubkey import Pubkey
    from solders.hash import Hash
    from solders.instruction import Instruction,AccountMeta
    from solders.message import MessageV0
    from solders.signature import Signature
    from solders.transaction import VersionedTransaction
    from solders.address_lookup_table_account import AddressLookupTable,AddressLookupTableAccount
    address(wallet);outer=route_instructions(response);instructions=[]
    for ix in outer:
        address(ix['programId']);metas=[]
        if len(ix['accounts'])>64:raise ValueError('Instruction account budget exceeded')
        for a in ix['accounts']:
            address(a['pubkey'])
            if type(a['isSigner']) is not bool or type(a['isWritable']) is not bool:raise ValueError('Invalid account flags')
            if a['isSigner'] and a['pubkey']!=wallet:raise ValueError('Unexpected additional signer')
            metas.append(AccountMeta(Pubkey.from_string(a['pubkey']),a['isSigner'],a['isWritable']))
        instructions.append(Instruction(Pubkey.from_string(ix['programId']),base64.b64decode(ix['data'],validate=True),metas))
    table_map=response.get('addressesByLookupTableAddress') or {}
    if not isinstance(table_map,dict) or len(table_map)>8:raise ValueError('Invalid lookup tables')
    tables=[];lookup_snapshot=None
    if table_map:
        for key in table_map:address(key)
        actual=rpc('getMultipleAccounts',[list(table_map),{'encoding':'base64','commitment':'confirmed'}])
        lookup_snapshot={'method':'getMultipleAccounts','params':[list(table_map),{'encoding':'base64','commitment':'confirmed'}],'result':actual}
        if len(actual['value'])!=len(table_map):raise ValueError('Missing lookup table response')
        for key,value in zip(table_map,actual['value']):
            if not value or value['owner']!='AddressLookupTab1e1111111111111111111111111' or value.get('executable') is not False:raise ValueError('Invalid lookup table owner')
            table=AddressLookupTable.deserialize(account_bytes(value))
            if [str(a) for a in table.addresses]!=table_map[key]:raise ValueError('Lookup table differs from quote')
            tables.append(AddressLookupTableAccount(Pubkey.from_string(key),table.addresses))
    msg=MessageV0.try_compile(Pubkey.from_string(wallet),instructions,tables,Hash.from_string(blockhash))
    if msg.header.num_required_signatures!=1:raise ValueError('Unexpected signer count')
    raw=bytes(VersionedTransaction.populate(msg,[Signature.default()]))
    keys=[str(k) for k in msg.account_keys];lookup={str(t.key):t.addresses for t in tables}
    for writable in (True,False):
        for item in msg.address_table_lookups:
            indices=item.writable_indexes if writable else item.readonly_indexes
            keys.extend(str(lookup[str(item.account_key)][i]) for i in indices)
    if len(raw)>1232 or len(keys)>64 or len(keys)!=len(set(keys)):raise ValueError('Transaction inspection budget exceeded')
    return {'raw':raw,'keys':keys,'outer':outer,'lookup_snapshot':lookup_snapshot}
