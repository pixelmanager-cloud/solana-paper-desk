"""Compile provider instructions with request-bound lookup tables and null signatures."""
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
    return {'raw':raw,'keys':keys,'outer':outer,'lookup_snapshot':lookup_snapshot,
            'declared_message_privileges':declared_message_privileges(raw,lookup_snapshot,outer,keys,wallet)}


def declared_message_privileges(raw,lookup_snapshot,outer,keys,wallet):
    """Reconstruct unsigned v0 declarations, not runtime privilege authentication.

    Lookup contents are request-bound diagnostic evidence only. Recompile the
    original ordered route metas to reject inconsistent header/index/flag claims.
    No RPC, signatures, CPI privileges or source/finality trust is manufactured.
    """
    import hashlib
    from solders.pubkey import Pubkey
    from solders.instruction import Instruction,AccountMeta
    from solders.message import MessageV0,to_bytes_versioned
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    from solders.address_lookup_table_account import AddressLookupTable,AddressLookupTableAccount
    from .account_keys import compiled_keys,compiled_instruction
    if type(raw) is not bytes or len(raw)>1232:raise ValueError('Invalid unsigned message bytes')
    tx=VersionedTransaction.from_bytes(raw);msg=tx.message
    if (not isinstance(msg,MessageV0) or bytes(tx)!=raw or tx.signatures!=[Signature.default()]
            or msg.header.num_required_signatures!=1):raise ValueError('Unsigned v0 message profile required')
    from solders.transaction import SanitizeError
    try:tx.sanitize()
    except SanitizeError:raise ValueError('Unsigned message sanitization failed') from None
    if str(msg.account_keys[0])!=address(wallet):raise ValueError('Message payer mismatch')
    if not isinstance(outer,list) or not 1<=len(outer)<=64:raise ValueError('Outer instruction budget exceeded')
    tables=[];lookup={}
    if lookup_snapshot is not None:
        if not isinstance(lookup_snapshot,dict) or lookup_snapshot.get('method')!='getMultipleAccounts':raise ValueError('Lookup witness method mismatch')
        params=lookup_snapshot['params'];result=lookup_snapshot['result']
        if (not isinstance(params,list) or len(params)!=2 or params[1]!={'encoding':'base64','commitment':'confirmed'}
                or not isinstance(params[0],list) or not 1<=len(params[0])<=8
                or len(set(params[0]))!=len(params[0]) or len(result['value'])!=len(params[0])):
            raise ValueError('Lookup witness request/count mismatch')
        for key,value in zip(params[0],result['value']):
            address(key)
            if not isinstance(value,dict) or value.get('owner')!='AddressLookupTab1e1111111111111111111111111' or value.get('executable') is not False:
                raise ValueError('Lookup witness owner mismatch')
            table=AddressLookupTable.deserialize(account_bytes(value))
            lookup[key]=[str(a) for a in table.addresses]
            tables.append(AddressLookupTableAccount(Pubkey.from_string(key),table.addresses))
    descriptors=[];loaded={'writable':[],'readonly':[]}
    for item in msg.address_table_lookups:
        key=str(item.account_key)
        if key not in lookup:raise ValueError('Loaded message membership witness missing')
        descriptors.append({'accountKey':key,'writableIndexes':list(item.writable_indexes),'readonlyIndexes':list(item.readonly_indexes)})
    for segment,field in (('writable','writableIndexes'),('readonly','readonlyIndexes')):
        for item in descriptors:
            for i in item[field]:
                if i>=len(lookup[item['accountKey']]):raise ValueError('Loaded table index unavailable')
                loaded[segment].append(lookup[item['accountKey']][i])
    header={'numRequiredSignatures':msg.header.num_required_signatures,
            'numReadonlySignedAccounts':msg.header.num_readonly_signed_accounts,
            'numReadonlyUnsignedAccounts':msg.header.num_readonly_unsigned_accounts}
    message={'header':header,'accountKeys':[str(a) for a in msg.account_keys],'addressTableLookups':descriptors}
    resolved,privileges=compiled_keys(message,{'loadedAddresses':loaded},0,[str(s) for s in tx.signatures])
    if not isinstance(keys,list) or keys!=resolved or len(keys)>64:raise ValueError('Message key membership/order mismatch')
    if len(outer)!=len(msg.instructions):raise ValueError('Outer message instruction count mismatch')
    original=[];declarations=[]
    for i,(ix,compiled) in enumerate(zip(outer,msg.instructions)):
        if not isinstance(ix,dict) or not isinstance(ix.get('accounts'),list) or len(ix['accounts'])>64:raise ValueError('Outer account shape invalid')
        metas=[]
        for a in ix['accounts']:
            address(a['pubkey'])
            if type(a.get('isSigner')) is not bool or type(a.get('isWritable')) is not bool:raise ValueError('Outer request flags missing/invalid')
            metas.append(AccountMeta(Pubkey.from_string(a['pubkey']),a['isSigner'],a['isWritable']))
        data=base64.b64decode(ix['data'],validate=True)
        view=compiled_instruction({'programIdIndex':compiled.program_id_index,'accounts':list(compiled.accounts)},resolved)
        if view['programId']!=ix['programId'] or view['accounts']!=[a['pubkey'] for a in ix['accounts']] or bytes(compiled.data)!=data:
            raise ValueError('Outer message path/program/raw/account mismatch')
        original.append(Instruction(Pubkey.from_string(address(ix['programId'])),data,metas))
        declarations.append({'instruction':str(i),'account_privileges':[
            {**privileges[j],'message_index':j} for j in compiled.accounts]})
    reproduced=MessageV0.try_compile(Pubkey.from_string(wallet),original,tables,msg.recent_blockhash)
    if bytes(reproduced)!=bytes(msg):raise ValueError('Message header/static/loaded declarations contradict route metas')
    return {'version':0,'message_hash':hashlib.sha256(to_bytes_versioned(msg)).hexdigest(),
        'header':header,'loaded_addresses':loaded,'account_privileges':privileges,'outer':declarations,
        'declarations_consistent':True,'alt_contents_authenticated':False,
        'source_authenticated':False,'finality_authenticated':False,'runtime_cpi_privileges_authenticated':False}
