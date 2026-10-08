"""Disconnected, bounded Token-2022 endpoint-state structural diagnostics.

No transport, instruction approval, historical-control proof, or runtime consumer.
Exact candidate extensions: mint {18, 19}, account {7}. Source-built bytes and
caller-supplied envelope/bindings never authenticate a snapshot. Profile matching
is NOT lifecycle acceptance. See docs/reviews/token2022-state-decoder.md for pins,
strict-policy differences from the official permissive unpackers, and gaps.
"""
import hashlib

from solders.pubkey import Pubkey

TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
PROFILE = 'create-v2-token2022-endpoint-structure-v1'
MAX_STATE_BYTES = 8192
STRING_LIMITS = {'name': 256, 'symbol': 64, 'uri': 2048,
                 'additional_key': 256, 'additional_value': 2048}
MAX_ADDITIONAL_PAIRS = 16
SOURCES = (
    ('solana-program/token-2022', 'd9ffb9787187b6bc29adda1a6b389b9931377e03',
     'interface/src/state.rs', '8c94447787faee9ef05ba752392df6db2d00cb1d'),
    ('solana-program/token-2022', 'd9ffb9787187b6bc29adda1a6b389b9931377e03',
     'interface/src/extension/mod.rs', '12f066a14f0db3ee65543a48919a82a870db44a6'),
    ('solana-program/token-2022', 'd9ffb9787187b6bc29adda1a6b389b9931377e03',
     'interface/src/extension/metadata_pointer/mod.rs',
     '0d334fa95d79a94d317bd1345f6f5dec745ae1af'),
    ('solana-program/token-2022', 'd9ffb9787187b6bc29adda1a6b389b9931377e03',
     'interface/src/extension/immutable_owner.rs',
     'a0294f34ea156b9218886bd768db50f6f5f17333'),
    ('solana-program/token-metadata', 'fb7755d1520af9fb2cda2fbfbcceb0248080121a',
     'interface/src/state.rs', '0c5cef2cc5e8c074daa15de3a458aa0722ec5938'),
)


class _Invalid(ValueError):
    def __init__(self, code, offset):
        self.code, self.offset = code, offset


def _key(raw):
    return str(Pubkey.from_bytes(raw))


def _nullable(raw):
    # MaybeNull<Address>, not the base state's four-byte COption encoding.
    return {'encoding': 'nullable-address32', 'raw_hex': raw.hex(),
            'present': any(raw), 'value': _key(raw) if any(raw) else None}


def _coption(raw, offset, numeric=False):
    tag, body = int.from_bytes(raw[:4], 'little'), raw[4:]
    if tag not in (0, 1):
        raise _Invalid('COPTION_TAG_INVALID', offset)
    # Official pack(None) leaves the old body untouched. Retain, do not interpret.
    return {'encoding': 'coption-u64' if numeric else 'coption-address',
            'offset': offset, 'tag': tag, 'payload_hex': body.hex(),
            'present': tag == 1,
            'value': (int.from_bytes(body, 'little') if numeric else _key(body))
            if tag else None}


class _MetadataReader:
    def __init__(self, data, offset):
        self.data, self.offset, self.position = data, offset, 0

    def take(self, size):
        start = self.position
        if start + size > len(self.data):
            raise _Invalid('METADATA_TRUNCATED', self.offset + start)
        self.position += size
        return self.data[start:self.position]

    def u32(self):
        return int.from_bytes(self.take(4), 'little')

    def string(self, field):
        start = self.position
        size = self.u32()
        if size > STRING_LIMITS[field]:
            raise _Invalid('METADATA_STRING_BOUND_EXCEEDED', self.offset + start)
        data = self.take(size)
        try:
            value = data.decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            raise _Invalid('METADATA_UTF8_INVALID', self.offset + start + 4) from None
        return {'value': value, 'byte_length': size, 'raw_hex': data.hex()}


def _metadata(data, offset, output):
    reader = _MetadataReader(data, offset)
    # Mutate only this freshly allocated witness so partial fields survive errors.
    output['update_authority'] = _nullable(reader.take(32))
    output['mint'] = _key(reader.take(32))
    for field in ('name', 'symbol', 'uri'):
        output[field] = reader.string(field)
    count_offset = reader.position
    count = reader.u32()
    output['additional_metadata_count'] = count
    output['additional_metadata'] = []
    if count > MAX_ADDITIONAL_PAIRS:
        raise _Invalid('METADATA_PAIR_BOUND_EXCEEDED', offset + count_offset)
    for _ in range(count):
        output['additional_metadata'].append({
            'key': reader.string('additional_key'),
            'value': reader.string('additional_value')})
    if reader.position != len(data):
        raise _Invalid('METADATA_TRAILING_BYTES', offset + reader.position)


def decode_token2022_state(raw, *, kind, address=None, program_owner=None,
                          executable=None, expected_mint=None,
                          expected_token_owner=None, expected_metadata=None):
    """Inspect supplied bytes; every approval/verification result remains false.

    kind is exactly 'mint' or 'account'. Address/program/executable are required
    unauthenticated envelope witnesses for a profile match; token accounts also
    require explicit expected mint and token owner. Mint self-metadata binding is
    against address. Optional expected_metadata must contain exactly name/symbol/
    uri strings and compares bytes without URI fetching. Supply/decimals are the
    proposed saved-profile endpoint (10**15/6), not universal Token-2022 rules.
    None raw means missing, not zero state; omitted executable means unknown.
    ImmutableOwner does not establish an ATA derivation or absence of past controls.
    """
    result = {'profile': PROFILE, 'kind': kind, 'sources': SOURCES,
              'input': {'address': address, 'program_owner': program_owner,
                        'executable': executable, 'expected_mint': expected_mint,
                        'expected_token_owner': expected_token_owner,
                        'expected_metadata': None},
              'raw_hex': None, 'raw_sha256': None, 'raw_length': None,
              'base': {}, 'extensions': [], 'tlv_padding': None, 'diagnostics': [],
              'decoding_complete': False, 'structural_profile_match': False,
              'snapshot_authenticated': False, 'lifecycle_verified': False,
              'authenticated_lifecycle_accepted': False, 'eligible': False,
              'unresolved_context': ['RAW_STATE_PROVENANCE_UNAUTHENTICATED',
                  'FINALITY_SLOT_AND_PROGRAM_BINARY_UNVERIFIED',
                  'PREDECESSOR_STATE_AND_CONTROL_HISTORY_MISSING',
                  'INSTRUCTION_CALLER_PRIVILEGES_AND_CPI_SUCCESS_UNVERIFIED',
                  'EFFECT_ORDER_AND_COVERAGE_UNVERIFIED']}

    def diagnostic(code, category='profile', offset=None):
        result['diagnostics'].append({'code': code, 'category': category,
                                      'offset': offset})

    def binding_key(value, label):
        if value is None:
            diagnostic(label + '_MISSING', 'context')
            return None
        try:
            if not isinstance(value, str) or str(Pubkey.from_string(value)) != value:
                raise ValueError
        except ValueError:
            diagnostic(label + '_INVALID', 'context')
            return None
        return value

    if kind not in ('mint', 'account'):
        diagnostic('STATE_KIND_UNSUPPORTED', 'layout')
        return result
    account_address = binding_key(address, 'ACCOUNT_ADDRESS')
    owner_program = binding_key(program_owner, 'PROGRAM_OWNER')
    if owner_program is not None and owner_program != TOKEN_2022:
        diagnostic('PROGRAM_OWNER_NOT_TOKEN2022', 'context')
    if executable is None:
        diagnostic('EXECUTABLE_MISSING', 'context')
    elif executable is not False:
        diagnostic('EXECUTABLE_NOT_EXPLICIT_FALSE', 'context')
    mint_binding = token_owner_binding = None
    if kind == 'account':
        mint_binding = binding_key(expected_mint, 'EXPECTED_MINT')
        token_owner_binding = binding_key(expected_token_owner, 'EXPECTED_TOKEN_OWNER')
        if expected_metadata is not None:
            diagnostic('EXPECTED_METADATA_NOT_APPLICABLE', 'context')
    elif expected_mint is not None or expected_token_owner is not None:
        diagnostic('ACCOUNT_BINDINGS_NOT_APPLICABLE', 'context')
    metadata_binding = None
    if expected_metadata is not None:
        if (not isinstance(expected_metadata, dict)
                or set(expected_metadata) != {'name', 'symbol', 'uri'}
                or not all(isinstance(v, str) for v in expected_metadata.values())):
            diagnostic('EXPECTED_METADATA_INVALID', 'context')
        else:
            metadata_binding = dict(expected_metadata)
            result['input']['expected_metadata'] = dict(metadata_binding)
    if raw is None:
        diagnostic('RAW_STATE_MISSING', 'context')
        return result
    if not isinstance(raw, (bytes, bytearray)):
        diagnostic('RAW_STATE_TYPE_INVALID', 'layout')
        return result
    raw = bytes(raw)
    result['raw_length'] = len(raw)
    if len(raw) > MAX_STATE_BYTES:
        diagnostic('RAW_STATE_BOUND_EXCEEDED', 'layout')
        return result
    result['raw_hex'] = raw.hex()
    result['raw_sha256'] = hashlib.sha256(raw).hexdigest()
    base = result['base']
    try:
        base_size = 82 if kind == 'mint' else 165
        if len(raw) < base_size:
            raise _Invalid('BASE_STATE_TRUNCATED', len(raw))
        if len(raw) == 355:
            raise _Invalid('MULTISIG_LENGTH_COLLISION', 355)
        if kind == 'mint':
            base['mint_authority'] = _coption(raw[:36], 0)
            base['supply'] = int.from_bytes(raw[36:44], 'little')
            base['decimals'] = raw[44]
            base['initialized_byte'] = raw[45]
            if raw[45] not in (0, 1):
                raise _Invalid('MINT_INITIALIZED_BYTE_INVALID', 45)
            base['initialized'] = raw[45] == 1
            base['freeze_authority'] = _coption(raw[46:82], 46)
            for field in ('mint_authority', 'freeze_authority'):
                if base[field]['present']:
                    diagnostic(field.upper() + '_PRESENT', offset=base[field]['offset'])
            if not base['initialized']:
                diagnostic('MINT_UNINITIALIZED', offset=45)
            if base['decimals'] != 6 or base['supply'] != 10**15:
                diagnostic('MINT_SUPPLY_OR_DECIMALS_PROFILE_MISMATCH', offset=36)
        else:
            base['mint'], base['token_owner'] = _key(raw[:32]), _key(raw[32:64])
            base['amount'] = int.from_bytes(raw[64:72], 'little')
            base['delegate'] = _coption(raw[72:108], 72)
            base['state_byte'] = raw[108]
            if raw[108] not in (0, 1, 2):
                raise _Invalid('ACCOUNT_STATE_BYTE_INVALID', 108)
            base['state'] = ('uninitialized', 'initialized', 'frozen')[raw[108]]
            base['is_native'] = _coption(raw[109:121], 109, numeric=True)
            base['delegated_amount'] = int.from_bytes(raw[121:129], 'little')
            base['close_authority'] = _coption(raw[129:165], 129)
            base['effective_close_authority'] = (base['close_authority']['value']
                                               if base['close_authority']['present']
                                               else base['token_owner'])
            if mint_binding is not None and base['mint'] != mint_binding:
                diagnostic('ACCOUNT_MINT_BINDING_MISMATCH', offset=0)
            if token_owner_binding is not None and base['token_owner'] != token_owner_binding:
                diagnostic('ACCOUNT_OWNER_BINDING_MISMATCH', offset=32)
            for field in ('delegate', 'is_native', 'close_authority'):
                if base[field]['present']:
                    diagnostic(field.upper() + '_PRESENT', offset=base[field]['offset'])
            if not base['delegate']['present'] and base['delegated_amount'] != 0:
                diagnostic('DELEGATED_AMOUNT_WITHOUT_DELEGATE', offset=121)
            if base['state'] != 'initialized':
                diagnostic('ACCOUNT_' + base['state'].upper(), offset=108)
        if len(raw) == base_size:
            diagnostic('EXACT_EXTENSION_SET_MISMATCH')
            result['decoding_complete'] = True
            return result
        if len(raw) < 166:
            raise _Invalid('EXTENDED_PREFIX_TRUNCATED', len(raw))
        if kind == 'mint' and any(raw[82:165]):
            raise _Invalid('MINT_PADDING_NONZERO', 82)
        if raw[165] != (1 if kind == 'mint' else 2):
            raise _Invalid('ACCOUNT_TYPE_MISMATCH', 165)
        position, seen = 166, set()
        payloads_complete = True
        while position < len(raw):
            # Official adjust_len_for_multisig adds exactly a u16 zero padding
            # type if used length is 355. No generic spare/reallocation tail.
            if position == 355 and len(raw) == 357 and raw[position:] == b'\0\0':
                result['tlv_padding'] = {'offset': 355, 'raw_hex': '0000',
                                         'reason': 'official-multisig-length-adjustment'}
                position = 357
                break
            if len(raw) - position < 4:
                raise _Invalid('TLV_HEADER_TRUNCATED_OR_RESERVED_TAIL', position)
            extension_type = int.from_bytes(raw[position:position + 2], 'little')
            size = int.from_bytes(raw[position + 2:position + 4], 'little')
            start = position + 4
            row = {'type': extension_type, 'length': size, 'offset': position,
                   'value_offset': start, 'raw_hex': raw[start:min(start + size, len(raw))].hex(),
                   'decoded': {}}
            result['extensions'].append(row)
            if extension_type == 0:
                raise _Invalid('TLV_UNINITIALIZED_OR_RESERVED_TAIL', position)
            if extension_type in seen:
                raise _Invalid('TLV_DUPLICATE_TYPE', position)
            seen.add(extension_type)
            if start + size > len(raw):
                raise _Invalid('TLV_VALUE_TRUNCATED', start)
            data = raw[start:start + size]
            if extension_type not in ({18, 19} if kind == 'mint' else {7}):
                diagnostic('EXTENSION_TYPE_UNSUPPORTED', 'unsupported', position)
                payloads_complete = False
            elif extension_type == 7:
                if size != 0:
                    raise _Invalid('IMMUTABLE_OWNER_LENGTH_INVALID', start)
                row['decoded']['immutable_owner_present'] = True
            elif extension_type == 18:
                if size != 64:
                    raise _Invalid('METADATA_POINTER_LENGTH_INVALID', start)
                row['decoded']['authority'] = _nullable(data[:32])
                row['decoded']['metadata_address'] = _nullable(data[32:])
                if row['decoded']['authority']['present']:
                    diagnostic('METADATA_POINTER_AUTHORITY_PRESENT', offset=start)
                pointer = row['decoded']['metadata_address']['value']
                if pointer is None or (account_address is not None and pointer != account_address):
                    diagnostic('METADATA_POINTER_NOT_SELF', offset=start + 32)
            else:
                _metadata(data, start, row['decoded'])
                metadata = row['decoded']
                if metadata['update_authority']['present']:
                    diagnostic('METADATA_UPDATE_AUTHORITY_PRESENT', offset=start)
                if account_address is not None and metadata['mint'] != account_address:
                    diagnostic('METADATA_MINT_BINDING_MISMATCH', offset=start + 32)
                if metadata['additional_metadata_count']:
                    diagnostic('ADDITIONAL_METADATA_UNSUPPORTED', offset=start)
                if metadata_binding is not None and any(
                        metadata[f]['value'] != metadata_binding[f] for f in metadata_binding):
                    diagnostic('METADATA_STRING_BINDING_MISMATCH', offset=start)
            position = start + size
        if seen != ({18, 19} if kind == 'mint' else {7}):
            diagnostic('EXACT_EXTENSION_SET_MISMATCH')
        result['decoding_complete'] = payloads_complete
    except _Invalid as error:
        diagnostic(error.code, 'layout', error.offset)
    result['structural_profile_match'] = (result['decoding_complete']
                                          and not result['diagnostics'])
    return result
