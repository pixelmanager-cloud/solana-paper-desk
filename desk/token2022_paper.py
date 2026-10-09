"""Narrow original-byte Token-2022 paper profile; no history or execution proof.

Pinned primary layouts: token-2022 d9ffb9787187b6bc29adda1a6b389b9931377e03
interface/src/{state.rs,extension/mod.rs,extension/metadata_pointer/mod.rs};
token-metadata fb7755d1520af9fb2cda2fbfbcceb0248080121a interface/src/state.rs.
Mint TLV {MetadataPointer18,TokenMetadata19}; account TLV {ImmutableOwner7}.
Unknown allocation tails/types and mutable metadata authorities remain rejected.
"""
from .programs import address, unbase58

VERSION=1
NAME='metadata-only-immutable-owner-paper-v1'
BOOST_NAME='metadata-only-boosted-quote-paper-v2'

def profile_name(version):
    check_version(version)
    return BOOST_NAME if version==2 else NAME

MAX_STATE_BYTES=8192


def selected(cfg):
    if 'paper_token_profile_version' not in cfg:return 0
    value=cfg['paper_token_profile_version']
    if (type(value) is not int or value not in (VERSION,2) or cfg.get('mode')!='paper'
            or type(cfg.get('paper_signal_policy_version')) is not int
            or cfg['paper_signal_policy_version']!=3
            or type(cfg.get('paper_quote_execution_version')) is not int
            or cfg['paper_quote_execution_version']!=1):
        raise ValueError('Explicit paper Token-2022 profiles1/2 require signal3/quote1')
    return value


def check_version(value):
    if type(value) is not int or value not in (0,VERSION,2):
        raise ValueError('Unsupported paper token profile version')


def _tlv(raw,kind):
    if not 166<=len(raw)<=MAX_STATE_BYTES or len(raw)==355:
        raise ValueError('TOKEN2022_LAYOUT_INVALID')
    if raw[165]!=kind or (kind==1 and any(raw[82:165])):
        raise ValueError('TOKEN2022_ACCOUNT_TYPE_OR_PADDING_INVALID')
    offset=166; entries={}
    while offset<len(raw):
        # Official multisig collision adjustment, no arbitrary allocation tails.
        if offset==355 and len(raw)==357 and raw[offset:]==bytes(2):break
        if len(raw)-offset<4:raise ValueError('TOKEN2022_TLV_TRUNCATED')
        tag=int.from_bytes(raw[offset:offset+2],'little')
        size=int.from_bytes(raw[offset+2:offset+4],'little');offset+=4
        if tag in entries:raise ValueError('TOKEN2022_DUPLICATE_EXTENSION')
        if tag not in ({18,19} if kind==1 else {7}):
            raise ValueError('TOKEN2022_EXTENSION_NOT_SUPPORTED')
        if offset+size>len(raw):raise ValueError('TOKEN2022_TLV_TRUNCATED')
        entries[tag]=raw[offset:offset+size];offset+=size
    if set(entries)!=({18,19} if kind==1 else {7}):
        raise ValueError('TOKEN2022_REQUIRED_EXTENSIONS_MISSING')
    return entries


def mint_base(raw,mint):
    address(mint);identity=unbase58(mint)
    entries=_tlv(raw,1);pointer=entries[18]
    if len(pointer)!=64 or any(pointer[:32]) or pointer[32:]!=identity:
        raise ValueError('TOKEN2022_METADATA_POINTER_CONTROL_OR_IDENTITY')
    metadata=entries[19]
    if len(metadata)<68 or any(metadata[:32]) or metadata[32:64]!=identity:
        raise ValueError('TOKEN2022_METADATA_CONTROL_OR_IDENTITY')
    offset=64
    for limit in (256,64,2048):
        if offset+4>len(metadata):raise ValueError('TOKEN2022_METADATA_TRUNCATED')
        size=int.from_bytes(metadata[offset:offset+4],'little');offset+=4
        if size>limit or offset+size>len(metadata):raise ValueError('TOKEN2022_METADATA_STRING_BOUND')
        try:metadata[offset:offset+size].decode('utf-8')
        except UnicodeError:raise ValueError('TOKEN2022_METADATA_UTF8_INVALID') from None
        offset+=size
    if offset+4!=len(metadata) or metadata[offset:offset+4]!=bytes(4):
        raise ValueError('TOKEN2022_ADDITIONAL_METADATA_OR_TRAILING_BYTES')
    return raw[:82]


def account_base(raw):
    if _tlv(raw,2)[7]!=b'':raise ValueError('TOKEN2022_IMMUTABLE_OWNER_LENGTH_INVALID')
    if int.from_bytes(raw[109:113],'little')!=0:
        raise ValueError('TOKEN2022_NATIVE_ACCOUNT_NOT_SUPPORTED')
    return raw[:165]
