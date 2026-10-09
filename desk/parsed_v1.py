"""Bounded jsonParsed-v1 observations, not signed-wire/runtime authentication.

Primary semantics pinned to anza-xyz/solana-sdk commit
891412dceb0a7d3d4116ac295eef519882138090, message/src/versions/v1/{mod,
message,config}.rs. RPC JSON normalization: solana.com/docs/rpc/json-structures.
No ALT support in v1; compiled JSON/wire support deliberately remains separate.
"""
import math
from .programs import address, unbase58

MAX_NODES = 16384
MAX_UTF8 = 2 * 1024 * 1024  # Diagnostic JSON bound, NOT the 4096-byte wire limit.


def _require(ok):
    if not ok: raise ValueError('Invalid jsonParsed v1 shape')


def _uint(value, bits):
    _require(type(value) is int and 0 <= value < 2**bits)
    return value


def _bounded(value):
    stack=[(value,0)]; nodes=total=0
    while stack:
        item,depth=stack.pop();nodes+=1
        _require(depth<=16 and nodes<=MAX_NODES)
        if type(item) is dict:
            _require(len(item)<=128 and all(type(k) is str for k in item))
            stack.extend((v,depth+1) for v in item.values())
            stack.extend((k,depth+1) for k in item)
        elif type(item) is list:
            _require(len(item)<=1024);stack.extend((v,depth+1) for v in item)
        elif type(item) is str:
            _require(len(item)<=32768)
            size=len(item.encode('utf-8'));_require(size<=32768);total+=size
        else:
            _require(item is None or type(item) in (bool,int,float))
            if type(item) is float:_require(math.isfinite(item))
            if type(item) is int:_require(-(2**64)<=item<2**64)
        _require(total<=MAX_UTF8)


def _instruction(ix, keys, *, outer):
    _require(type(ix) is dict)
    parsed='parsed' in ix
    fields={'parsed','program','programId','stackHeight'} if parsed else {'accounts','data','programId','stackHeight'}
    _require(set(ix)==fields and address(ix['programId']) in keys)
    height=ix['stackHeight']
    _require(type(height) is int and (height==1 if outer else 2<=height<=64))
    if not parsed:
        _require(type(ix['accounts']) is list and len(ix['accounts'])<=255)
        _require(all(address(k) in keys for k in ix['accounts']))
        # Duplicate instruction account references are legal; key list duplicates aren't.
        _require(type(ix['data']) is str and len(ix['data'])<=5600)
        _require(ix['data']=='' or len(unbase58(ix['data']))<=4096)
    else:
        _require(type(ix['program']) is str and 1<=len(ix['program'])<=128)
        body=ix['parsed'];_require(type(body) is dict and set(body)=={'type','info'})
        _require(type(body['type']) is str and 1<=len(body['type'])<=128 and type(body['info']) is dict)
        # Validate address syntax without treating data-encoded authorities as
        # indexed accounts or authenticating provider-parsed authority fields.
        account_fields={'source','destination','account','mint','newAccount','nonceAccount','recentBlockhashesSysvar'}
        address_fields=account_fields|{'owner','authority','base','nonceAuthority','newAuthority','mintAuthority','freezeAuthority','wallet'}
        for field in address_fields & body['info'].keys():
            value=body['info'][field]
            if value is None:
                _require(field in {'newAuthority','mintAuthority','freezeAuthority'})
            else:
                address(value)
                if field in account_fields:_require(value in keys)


def validate(container):
    """Validate one provider-declared parsed observation without modifying it."""
    _bounded(container)
    _require(type(container.get('version')) is int and container['version']==1)
    _uint(container['slot'],64)
    if 'blockTime' in container and container['blockTime'] is not None:
        _uint(container['blockTime'],63)
    if 'transactionIndex' in container:_uint(container['transactionIndex'],32)
    _require(set(container)<= {'version','transaction','meta','slot','blockTime','transactionIndex','signature'})
    tx=container['transaction'];meta=container['meta']
    _require(type(tx) is dict and set(tx)=={'message','signatures'} and type(meta) is dict)
    message=tx['message']
    mandatory={'accountKeys','instructions','recentBlockhash','transactionConfig'}
    _require(type(message) is dict and set(message)==mandatory)
    address(message['recentBlockhash'])
    config=message['transactionConfig']
    fields={'priorityFee':64,'computeUnitLimit':32,'loadedAccountsDataSizeLimit':32,'heapSize':32}
    _require(type(config) is dict and set(config)==set(fields))
    for field,bits in fields.items():
        value=config[field]
        if value is not None:_uint(value,bits)
    heap=config['heapSize']
    _require(heap is None or 32768<=heap<=262144 and heap%1024==0)
    entries=message['accountKeys'];_require(type(entries) is list and 1<=len(entries)<=64)
    keys=[];signer_flags=[]
    for entry in entries:
        _require(type(entry) is dict and set(entry)=={'pubkey','signer','source','writable'})
        _require(entry['source']=='transaction' and type(entry['signer']) is bool and type(entry['writable']) is bool)
        keys.append(address(entry['pubkey']))
        signer_flags.append(entry['signer'])
    # RPC may demote reserved/program write privileges. Its writable flags
    # cannot reconstruct signed-header counts or original writable ordering.
    _require(len(set(keys))==len(keys) and entries[0]['writable'])
    _require(signer_flags==sorted(signer_flags,reverse=True))
    required=sum(e['signer'] for e in entries)
    _require(1<=required<=12)
    signatures=tx['signatures']
    _require(type(signatures) is list and len(signatures)==required)
    _require(all(type(s) is str and 64<=len(s)<=88 and len(unbase58(s))==64 for s in signatures))
    _require(set(meta)<= {'err','fee','preBalances','postBalances','preTokenBalances','postTokenBalances',
                         'innerInstructions','computeUnitsConsumed','costUnits','status','logMessages','rewards'})
    if 'status' in meta:
        status=meta['status'];_require(type(status) is dict)
        _require(status=={'Ok':None} if meta.get('err') is None else set(status)=={'Err'} and status['Err']==meta['err'])
    outer=message['instructions'];_require(type(outer) is list and len(outer)<=64)
    # A lower bound from visible fields can reject impossible wire sizes, but
    # parsed instruction payloads are unavailable: no exact wire-size claim.
    visible_wire=42+32*len(keys)+64*required+4*len(outer)
    visible_wire+=sum((8 if k=='priorityFee' else 4) for k,v in config.items() if v is not None)
    for ix in outer:
        _instruction(ix,keys,outer=True)
        if 'parsed' not in ix:
            visible_wire+=len(ix['accounts'])+(len(unbase58(ix['data'])) if ix['data'] else 0)
    _require(visible_wire<=4096)
    _require('err' in meta)
    for field in ('preBalances','postBalances'):
        values=meta[field];_require(type(values) is list and len(values)==len(keys))
        for value in values:_uint(value,64)
    identities={}
    for field in ('preTokenBalances','postTokenBalances'):
        values=meta[field];_require(type(values) is list and len(values)<=len(keys));seen=set()
        for value in values:
            _require(type(value) is dict and set(value)=={'accountIndex','mint','owner','programId','uiTokenAmount'})
            idx=_uint(value['accountIndex'],8);_require(idx<len(keys) and idx not in seen);seen.add(idx)
            for name in ('mint','owner','programId'):address(value[name])
            amount=value['uiTokenAmount']
            _require(type(amount) is dict and set(amount)=={'amount','decimals','uiAmount','uiAmountString'})
            identity=(value['mint'],value['owner'],value['programId'],amount['decimals'])
            _require(idx not in identities or identities[idx]==identity);identities[idx]=identity
            raw=amount['amount'];_require(type(raw) is str and 1<=len(raw)<=20 and raw.isascii() and raw.isdigit())
            _require(str(int(raw))==raw and int(raw)<2**64);_uint(amount['decimals'],8)
            _require(amount['uiAmount'] is None or type(amount['uiAmount']) in (int,float) and math.isfinite(amount['uiAmount']) and amount['uiAmount']>=0)
            _require(type(amount['uiAmountString']) is str and 1<=len(amount['uiAmountString'])<=260)
    inner=meta['innerInstructions'];_require(type(inner) is list and len(inner)<=len(outer));seen=set();count=0
    for group in inner:
        _require(type(group) is dict and set(group)=={'index','instructions'})
        idx=_uint(group['index'],8);_require(idx<len(outer) and idx not in seen);seen.add(idx)
        values=group['instructions'];_require(type(values) is list);count+=len(values);_require(count<=1024)
        for ix in values:_instruction(ix,keys,outer=False)
    if 'logMessages' in meta:
        logs=meta['logMessages']
        _require(logs is None or type(logs) is list and all(type(v) is str for v in logs))
    if 'rewards' in meta:_require(type(meta['rewards']) is list and all(type(v) is dict for v in meta['rewards']))
    _uint(meta['fee'],64)
    for field in ('computeUnitsConsumed','costUnits'):
        if field in meta:_uint(meta[field],64)
    return keys
