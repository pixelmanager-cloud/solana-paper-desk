"""Disconnected Token-2022 raw instruction syntax, never lifecycle acceptance.

Only resolved public keys and decoded raw bytes are accepted. Parsed witnesses
are retained separately, never used to reconstruct bytes/accounts. No providers,
production imports, state decoder, caller reconstruction or approval paths.
Source pins establish reference formats, not deployed code or actual CPI effects.
"""
import copy
import hashlib
import json

PROFILE = 'token2022-observed-instruction-syntax-v1'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
MAX_RAW_BYTES = 16384
MAX_STRING_BYTES = 4096
_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
_UNSET = object()
INITIALIZE_METADATA = bytes.fromhex('d2e11ea258b84d8d')
UPDATE_METADATA_AUTHORITY = bytes.fromhex('d7e4a6e45464567b')
_COMMIT = 'd9ffb9787187b6bc29adda1a6b389b9931377e03'
_PINS = (
    ('solana-program/token-2022', _COMMIT, 'interface/src/instruction.rs', '0c80e6d86274846945a881584bcdb02bcc6a0145'),
    ('solana-program/token-2022', _COMMIT, 'interface/src/extension/metadata_pointer/instruction.rs', '1f3f542ff499c0757e950e80543df4734ed56d16'),
    ('solana-program/token-2022', _COMMIT, 'program/src/processor.rs', 'f5816ef243c6ea3aa283ef471988f08e2251d361'),
    ('solana-program/token-2022', _COMMIT, 'program/src/extension/token_metadata/processor.rs', 'f488deb80fbcda1481dc15e6dd98cb9873a262a2'),
    ('solana-program/token-metadata', 'fb7755d1520af9fb2cda2fbfbcceb0248080121a', 'interface/src/instruction.rs', '39903167fda0f3ba680bf7c4615b7fd7edf2033e'),
)


class _Invalid(ValueError):
    pass


def _base58(raw):
    number = int.from_bytes(raw, 'big'); text = ''
    while number:
        number, digit = divmod(number, 58); text = _ALPHABET[digit] + text
    return '1' * (len(raw)-len(raw.lstrip(b'\0'))) + text


def _key(value):
    if not isinstance(value,str) or not 32 <= len(value) <= 44:
        raise _Invalid('ACCOUNT_KEY_INVALID')
    number = 0
    for character in value:
        if character not in _ALPHABET:raise _Invalid('ACCOUNT_KEY_INVALID')
        number = number*58 + _ALPHABET.index(character)
    raw = b'\0'*(len(value)-len(value.lstrip('1'))) + (number.to_bytes((number.bit_length()+7)//8,'big') if number else b'')
    if len(raw)!=32 or _base58(raw)!=value:raise _Invalid('ACCOUNT_KEY_INVALID')
    return value


def _accounts(accounts):
    if not isinstance(accounts,list) or not 1 <= len(accounts) <= 4:
        raise _Invalid('RAW_ACCOUNT_WITNESS_MISSING_OR_UNSUPPORTED')
    keys=[]
    for account in accounts:
        if isinstance(account,str):keys.append(_key(account))
        elif isinstance(account,dict) and {'pubkey'} <= set(account) <= {'pubkey','isSigner','isWritable'}:
            for flag in ('isSigner','isWritable'):
                if flag in account and type(account[flag]) is not bool:raise _Invalid('ACCOUNT_META_FLAG_INVALID')
            keys.append(_key(account['pubkey']))
        else:raise _Invalid('ACCOUNT_WITNESS_ENCODING_UNSUPPORTED')
    return keys


def _count(keys, expected):
    if len(keys)!=expected:raise _Invalid('ORDERED_ACCOUNT_COUNT_UNSUPPORTED')


def _length(raw, expected):
    if len(raw)!=expected:raise _Invalid('RAW_LENGTH_OR_TRAILING_BYTES')


def _optional32(raw):
    _length(raw,32)
    return {'presence':'explicit_none','value':None} if raw==b'\0'*32 else {'presence':'explicit_key','value':_base58(raw)}


def _spl_option(raw):
    if not raw or raw[0] not in (0,1):raise _Invalid('SPL_OPTION_TAG_INVALID')
    _length(raw,1 if raw[0]==0 else 33)
    return {'presence':'explicit_none','value':None} if raw[0]==0 else {'presence':'explicit_key','value':_base58(raw[1:])}


def _strings(raw):
    position=8; strings=[]
    for _ in range(3):
        if position+4>len(raw):raise _Invalid('METADATA_STRING_TRUNCATED')
        size=int.from_bytes(raw[position:position+4],'little');position+=4
        if size>MAX_STRING_BYTES:raise _Invalid('METADATA_STRING_INSPECTION_LIMIT')
        if position+size>len(raw):raise _Invalid('METADATA_STRING_TRUNCATED')
        try:strings.append(raw[position:position+size].decode('utf-8'))
        except UnicodeDecodeError:raise _Invalid('METADATA_STRING_UTF8_INVALID') from None
        position+=size
    if position!=len(raw):raise _Invalid('RAW_LENGTH_OR_TRAILING_BYTES')
    return strings


def _raw(raw, keys):
    if raw.startswith(INITIALIZE_METADATA):
        _count(keys,4)
        if keys[0]!=keys[2]:raise _Invalid('TOKEN2022_METADATA_MINT_BINDING_MISMATCH')
        name,symbol,uri=_strings(raw)
        return {'kind':'initializeTokenMetadata','metadata':keys[0],'update_authority_account':keys[1],
                'mint':keys[2],'mint_authority_account':keys[3],'name':name,'symbol':symbol,'uri':uri}
    if raw.startswith(UPDATE_METADATA_AUTHORITY):
        _count(keys,2);_length(raw,40)
        return {'kind':'updateTokenMetadataAuthority','metadata':keys[0],
                'current_metadata_authority_account':keys[1],'new_metadata_authority':_optional32(raw[8:])}
    tag=raw[0]
    if tag==39:
        if len(raw)<2:raise _Invalid('RAW_LENGTH_OR_TRAILING_BYTES')
        if raw[1]!=0:raise _Invalid('OPERATION_UNSUPPORTED')
        _count(keys,1);_length(raw,66)
        return {'kind':'initializeMetadataPointer','mint':keys[0],
                'pointer_authority':_optional32(raw[2:34]),'metadata_address':_optional32(raw[34:66])}
    if tag==20:
        _count(keys,1)
        if len(raw)<35:raise _Invalid('RAW_LENGTH_OR_TRAILING_BYTES')
        return {'kind':'initializeMint2','mint':keys[0],'decimals':raw[1],
                'mint_authority':_base58(raw[2:34]),'freeze_authority':_spl_option(raw[34:])}
    if tag==21:
        _count(keys,1)
        if raw!=b'\x15\x07\x00':raise _Invalid('REQUESTED_EXTENSION_PROFILE_UNSUPPORTED')
        return {'kind':'getAccountDataSize','mint':keys[0],'requested_extension_types':[7]}
    if tag==22:
        _count(keys,1);_length(raw,1)
        return {'kind':'initializeImmutableOwner','account':keys[0],
                'reference_semantics':'token2022_immutable_owner_extension_initialization_not_legacy_noop'}
    if tag==18:
        _count(keys,2);_length(raw,33)
        return {'kind':'initializeAccount3','account':keys[0],'mint':keys[1],'declared_owner':_base58(raw[1:])}
    if tag==7:
        _count(keys,3);_length(raw,9)
        return {'kind':'mintTo','mint':keys[0],'account':keys[1],'mint_authority_account':keys[2],
                'amount_raw':str(int.from_bytes(raw[1:9],'little'))}
    if tag==6:
        _count(keys,2)
        if len(raw)<3:raise _Invalid('RAW_LENGTH_OR_TRAILING_BYTES')
        if raw[1]!=0:raise _Invalid('AUTHORITY_ROLE_UNSUPPORTED')
        return {'kind':'setAuthority','mint':keys[0],'authority_role':'mintTokens',
                'current_mint_authority_account':keys[1],'new_mint_authority':_spl_option(raw[2:])}
    if tag==12:
        _count(keys,4);_length(raw,10)
        return {'kind':'transferChecked','source':keys[0],'mint':keys[1],'destination':keys[2],
                'transfer_authority_account':keys[3],'amount_raw':str(int.from_bytes(raw[1:9],'little')),'decimals':raw[9]}
    raise _Invalid('OPERATION_UNSUPPORTED')


def normalize_token2022_instruction(program, *, raw=None, accounts=None, parsed=_UNSET, evidence=None):
    """Normalize only exact supplied raw syntax, preserving untrusted witnesses.

    Accounts are ordered resolved pubkeys, optionally untrusted account metas.
    Numeric indices must be resolved elsewhere. Parsed info is NOT interpreted or
    compared; field presence preserves omitted versus explicit null witnesses.
    Evidence/caller hints and flags never authenticate any claim.
    """
    result={'profile':PROFILE,'program':program,'status':'UNRESOLVED','raw_operation':None,
            'raw_layout_complete':False,'normalization_complete':False,
            'raw_sha256':hashlib.sha256(raw).hexdigest() if isinstance(raw,bytes) else None,
            'raw_hex':raw.hex() if isinstance(raw,bytes) and len(raw)<=MAX_RAW_BYTES else None,
            'raw_length':len(raw) if isinstance(raw,bytes) else None,
            'raw_witness_present':isinstance(raw,bytes) and bool(raw),
            'account_witness':copy.deepcopy(accounts),'account_keys':None,
            'parsed_supplied':parsed is not _UNSET,'parsed_witness':None if parsed is _UNSET else copy.deepcopy(parsed),
            'parsed_field_presence':{},'representations_match':None,
            'untrusted_evidence':copy.deepcopy(evidence),'evidence_sha256':None,'reasons':[],
            'authority_authenticated':False,'evidence_authenticated':False,'caller_verified':False,
            'caller_privileges_verified':False,'cpi_success_verified':False,'effect_order_verified':False,
            'deployed_code_verified':False,'account_state_verified':False,'lifecycle_verified':False,
            'ownership_approved':False,'token2022_approved':False,'eligible_for_trading':False,
            'runtime_blockers':['TOKEN_2022_UNSUPPORTED','RAW_STATE_AND_LIFECYCLE_UNVERIFIED',
                                'CPI_SUCCESS_AND_CALLER_PRIVILEGES_UNVERIFIED','EFFECT_ORDER_UNVERIFIED'],
            'source_provenance':[{'repository':repo,'commit':commit,'path':path,'git_blob':blob} for repo,commit,path,blob in _PINS]}
    if isinstance(parsed,dict) and isinstance(parsed.get('info'),dict) and all(isinstance(k,str) for k in parsed['info']):
        info=parsed['info']
        fields=set(info)|{'authority','metadataAddress','freezeAuthority','newAuthority','updateAuthority',
                          'mintAuthority','mint','metadata','account','owner','amount','tokenAmount','extensionTypes'}
        result['parsed_field_presence']={field:field in info for field in sorted(fields)}
    if evidence is not None:
        try:
            if not isinstance(evidence,dict):raise ValueError()
            encoded=json.dumps(evidence,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
            if len(encoded)>65536:raise ValueError()
            result['evidence_sha256']=hashlib.sha256(encoded).hexdigest()
        except (TypeError,ValueError,OverflowError,RecursionError):result['reasons'].append('UNTRUSTED_EVIDENCE_SHAPE_INVALID')
    if program!=TOKEN_2022:result['reasons'].append('TOKEN2022_PROGRAM_BINDING_MISMATCH')
    if not isinstance(raw,bytes) or not raw:result['reasons'].append('RAW_INSTRUCTION_UNAVAILABLE')
    elif len(raw)>MAX_RAW_BYTES:result['reasons'].append('RAW_INSPECTION_LIMIT')
    try:keys=_accounts(accounts);result['account_keys']=keys
    except _Invalid as exc:result['reasons'].append(str(exc));keys=None
    if not result['reasons'] and keys is not None:
        try:
            result['raw_operation']=_raw(raw,keys);result['raw_layout_complete']=True
        except _Invalid as exc:result['reasons'].append(str(exc))
    result['reasons']=sorted(set(result['reasons']))
    result['normalization_complete']=result['raw_layout_complete'] and not result['reasons']
    if result['normalization_complete']:result['status']='SYNTAX_ONLY'
    return result
