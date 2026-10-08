"""Offline authentication of signed message bytes, never of RPC execution data.

Reconstruct compiled legacy/v0 JSON directly; do not recompile account metas or
use loaded-address metadata. No keypairs, signing, transaction creation or IO.
A valid signature proves the declared key signed these bytes, not finality,
execution, ownership, economic permission or resolved lookup-address contents.
"""
import base64
import hashlib
import json
import math

from solders.hash import Hash
from solders.instruction import CompiledInstruction
from solders.message import Message, MessageV0, MessageHeader, MessageAddressTableLookup, to_bytes_versioned
from solders.pubkey import Pubkey
from solders.signature import Signature

from desk.programs import unbase58
from desk.security import base58

MAX_SOURCE_BYTES = 1 << 20
MAX_SOURCE_DEPTH = 32
MAX_SOURCE_NODES = 32768
MAX_KEYS = 64
MAX_OUTER_INSTRUCTIONS = 64
MAX_PACKET_BYTES = 1232
_COMMIT = 'd9f20e951a06b61e4505da0955228020b96a8915'
SOURCE_PINS = (
    ('sdk/program/src/message/legacy.rs', '1a6a9239f4e0aaff47f02356ead414f5abc05414'),
    ('sdk/program/src/message/versions/v0/mod.rs', 'df001bb19ce0bcb8b3d4d60a070b1cea64346020'),
    ('sdk/program/src/message/versions/mod.rs', '301490a2aa7e7d2a8ecd00c5e34d378de26b74d7'),
    ('sdk/src/signature.rs', 'e3cc900e49efc1e47505bf698b4b7dcbf8ab7931'),
)


def _need(condition, code):
    if not condition:
        raise ValueError(code)


def _reject_nonfinite(_):
    raise ValueError('SOURCE_NONFINITE_NUMBER')


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        _need(key not in result, 'DUPLICATE_JSON_FIELD')
        result[key] = value
    return result


def _canonical_bounded(source):
    nodes, size, ancestors = 0, 0, set()

    def walk(value, depth):
        nonlocal nodes, size
        nodes += 1
        _need(nodes <= MAX_SOURCE_NODES and depth <= MAX_SOURCE_DEPTH, 'SOURCE_NODE_OR_DEPTH_BOUND')
        if type(value) in (dict, list):
            _need(id(value) not in ancestors, 'SOURCE_CYCLE')
            size += 2 + 2 * len(value)
            _need(size <= MAX_SOURCE_BYTES, 'SOURCE_BYTE_BOUND')
            ancestors.add(id(value))
            if type(value) is dict:
                for key, item in value.items():
                    _need(type(key) is str, 'SOURCE_NON_STRING_FIELD')
                    walk(key, depth + 1)
                    walk(item, depth + 1)
            else:
                for item in value:
                    walk(item, depth + 1)
            ancestors.remove(id(value))
        else:
            _need(value is None or type(value) in (str, int, bool, float), 'SOURCE_NON_JSON')
            if type(value) is str:
                _need(len(value) <= 20000, 'SOURCE_STRING_BOUND')
            elif type(value) is int:
                _need(value.bit_length() <= 256, 'SOURCE_INTEGER_BOUND')
            elif type(value) is float:
                # Original token display metadata may be floating point. All
                # signed integer fields are separately checked without coercion.
                _need(math.isfinite(value), 'SOURCE_NONFINITE_NUMBER')
            size += len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode())
            _need(size <= MAX_SOURCE_BYTES, 'SOURCE_BYTE_BOUND')
    walk(source, 0)
    raw = json.dumps(source, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    _need(len(raw) <= MAX_SOURCE_BYTES, 'SOURCE_BYTE_BOUND')
    return raw


def _key(value, code):
    _need(type(value) is str and 32 <= len(value) <= 44, code)
    try:
        result = Pubkey.from_string(value)
    except ValueError:
        raise ValueError(code) from None
    _need(str(result) == value, code)
    return result


def _index(value, count):
    _need(type(value) is int and 0 <= value < min(count, 256), 'INVALID_COMPILED_INDEX')
    return value


def validate_signed_message_witness(source):
    """Accept a raw result dict or saved rpc_response_v1 getTransaction envelope.

    Exact UTF-8 JSON bytes are also accepted: duplicate fields/nonfinite numbers
    reject, physical SHA256 is retained independently of canonical JSON SHA256.
    Dict input has no physical byte identity; source_sha256 hashes canonical
    unmodified input, matching the repository evidence-record convention.

    stackHeight, request/slot/metadata and resolved ALT values are NOT signed
    components. Only strict compiled message fields enter solders serialization.
    Header signer order, instruction order and repeated legal account references
    are preserved; no sorting/try_compile, signer inference or metadata repair.
    """
    out = {'profile': 'offline-signed-message-witness-v1',
        'source_sha256': None, 'source_bytes_sha256': None, 'source_format': None,
        'source_hash_kind': 'canonical_json_sorted_compact_utf8',
        'source_provenance': {'repository': 'solana-labs/solana', 'commit': _COMMIT,
                              'files': SOURCE_PINS, 'binding_dependency': 'solders==0.29.0'},
        'version': None, 'message_schema_complete': False, 'signature_checks_complete': False,
        'message_authenticated': False, 'message_bytes_base64': None, 'message_sha256': None,
        'message_size_bytes': None, 'transaction_wire_size_bytes': None,
        'static_account_keys': [], 'declared_lookup_slots': None,
        'signer_witnesses': [], 'errors': [], 'status': 'rejected',
        'unauthenticated_components': ['RPC_REQUEST_AND_SOURCE', 'SLOT_BLOCK_TIME_TRANSACTION_INDEX',
            'ALL_TRANSACTION_METADATA', 'OUTER_INSTRUCTION_STACK_HEIGHT', 'FINALITY_AND_BLOCK_INCLUSION',
            'ALT_RESOLVED_ADDRESSES_AND_BANK_STATE', 'CPI_PRIVILEGES_OUTCOMES_AND_EFFECTS'],
        'metadata_authenticated': False, 'finality_verified': False,
        'alt_resolved_values_authenticated': False, 'execution_success_verified': False,
        'signer_authorization_verified': False, 'authenticated_lifecycle_accepted': False,
        'lifecycle_verified': False, 'ownership_approved': False, 'eligible_for_trading': False}
    try:
        if type(source) is bytes:
            _need(len(source) <= MAX_SOURCE_BYTES, 'SOURCE_BYTE_BOUND')
            out['source_bytes_sha256'] = hashlib.sha256(source).hexdigest()
            try:
                source = json.loads(source.decode('utf-8'), object_pairs_hook=_unique_pairs,
                    parse_constant=_reject_nonfinite)
            except (UnicodeError, json.JSONDecodeError, RecursionError):
                raise ValueError('SOURCE_INVALID_UTF8_JSON') from None
        canonical = _canonical_bounded(source)
        out['source_sha256'] = hashlib.sha256(canonical).hexdigest()
        _need(type(source) is dict, 'RAW_TRANSACTION_RESULT_REQUIRED')
        if 'transaction' in source:
            record = source
            out['source_format'] = 'raw_transaction_result'
        else:
            _need(set(source) == {'kind', 'method', 'params', 'result'}
                  and source['kind'] == 'rpc_response_v1' and source['method'] == 'getTransaction'
                  and type(source['result']) is dict, 'SAVED_GET_TRANSACTION_ENVELOPE_REQUIRED')
            record = source['result']
            out['source_format'] = 'saved_getTransaction_envelope'
        version = record.get('version')
        _need(version == 'legacy' or (type(version) is int and version == 0), 'VERSION_MISSING_OR_UNSUPPORTED')
        out['version'] = version
        tx = record.get('transaction')
        _need(type(tx) is dict and set(tx) == {'message', 'signatures'}, 'COMPILED_TRANSACTION_SHAPE_REQUIRED')
        m = tx['message']
        mandatory = {'header', 'accountKeys', 'recentBlockhash', 'instructions'}
        _need(type(m) is dict and mandatory <= set(m) and not set(m) - mandatory - {'addressTableLookups'},
              'COMPILED_MESSAGE_SHAPE_REQUIRED')
        header = m['header']
        fields = ('numRequiredSignatures', 'numReadonlySignedAccounts', 'numReadonlyUnsignedAccounts')
        _need(type(header) is dict and set(header) == set(fields)
              and all(type(header[f]) is int and 0 <= header[f] <= 255 for f in fields), 'HEADER_INVALID')
        required, signed_ro, unsigned_ro = (header[f] for f in fields)
        static = m['accountKeys']
        _need(type(static) is list and 1 <= len(static) <= MAX_KEYS, 'STATIC_KEY_SHAPE_OR_BOUND')
        keys = [_key(k, 'STATIC_KEY_INVALID') for k in static]
        _need(len(set(static)) == len(static), 'DUPLICATE_STATIC_KEY')
        _need(1 <= required <= len(keys) and signed_ro < required and unsigned_ro <= len(keys) - required,
              'HEADER_COUNT_OR_FEE_PAYER_INVALID')
        signatures = tx['signatures']
        _need(type(signatures) is list and len(signatures) == required, 'SIGNATURE_COUNT_MISMATCH')
        sigs = []
        for value in signatures:
            _need(type(value) is str and 64 <= len(value) <= 88, 'SIGNATURE_ENCODING_OR_LENGTH_INVALID')
            try:
                sig = Signature.from_string(value)
            except ValueError:
                raise ValueError('SIGNATURE_ENCODING_OR_LENGTH_INVALID') from None
            _need(len(bytes(sig)) == 64 and str(sig) == value, 'SIGNATURE_ENCODING_OR_LENGTH_INVALID')
            sigs.append(sig)
        _need(len(set(signatures)) == len(signatures), 'DUPLICATE_SIGNATURE')
        blockhash = m['recentBlockhash']
        _key(blockhash, 'RECENT_BLOCKHASH_INVALID')
        recent = Hash.from_string(blockhash)
        lookups = m.get('addressTableLookups', [] if version == 'legacy' else None)
        _need(type(lookups) is list and len(lookups) <= MAX_KEYS and (version != 'legacy' or not lookups),
              'LOOKUP_SHAPE_VERSION_OR_BOUND')
        tables, descriptors, slots = set(), [], 0
        for lookup in lookups:
            _need(type(lookup) is dict and set(lookup) == {'accountKey','writableIndexes','readonlyIndexes'},
                  'LOOKUP_DESCRIPTOR_INVALID')
            key = _key(lookup['accountKey'], 'LOOKUP_TABLE_KEY_INVALID')
            _need(str(key) not in tables, 'DUPLICATE_LOOKUP_TABLE')
            tables.add(str(key))
            indexes, seen = [], set()
            for field in ('writableIndexes', 'readonlyIndexes'):
                values = lookup[field]
                _need(type(values) is list and len(values) <= MAX_KEYS, 'LOOKUP_INDEX_SHAPE_OR_BOUND')
                for value in values:
                    _index(value, 256)
                    _need(value not in seen, 'DUPLICATE_LOOKUP_INDEX')
                    seen.add(value)
                indexes.append(bytes(values))
                slots += len(values)
            _need(bool(seen), 'EMPTY_LOOKUP_DESCRIPTOR')
            descriptors.append(MessageAddressTableLookup(key, *indexes))
        total = len(keys) + slots
        _need(total <= MAX_KEYS, 'TOTAL_ACCOUNT_BOUND')
        outer = m['instructions']
        _need(type(outer) is list and len(outer) <= MAX_OUTER_INSTRUCTIONS, 'OUTER_INSTRUCTION_SHAPE_OR_BOUND')
        instructions = []
        for row in outer:
            _need(type(row) is dict and {'programIdIndex','accounts','data'} <= set(row)
                  and not set(row) - {'programIdIndex','accounts','data','stackHeight'}, 'RAW_COMPILED_INSTRUCTION_REQUIRED')
            program = _index(row['programIdIndex'], len(keys))
            _need(program != 0, 'PROGRAM_CANNOT_BE_FEE_PAYER')
            accounts = row['accounts']
            _need(type(accounts) is list and len(accounts) <= MAX_KEYS, 'INSTRUCTION_ACCOUNT_SHAPE_OR_BOUND')
            for value in accounts:
                _index(value, total)
            data = row['data']
            _need(type(data) is str and len(data) <= 2 * MAX_PACKET_BYTES, 'INSTRUCTION_DATA_INVALID_OR_BOUND')
            try:
                raw = unbase58(data) if data else b''
            except ValueError:
                raise ValueError('INSTRUCTION_DATA_INVALID_OR_BOUND') from None
            _need(base58(raw) == data and len(raw) <= MAX_PACKET_BYTES, 'INSTRUCTION_DATA_INVALID_OR_BOUND')
            instructions.append(CompiledInstruction(program, raw, bytes(accounts)))
        if version == 'legacy':
            message = Message.new_with_compiled_instructions(required, signed_ro, unsigned_ro, keys, recent, instructions)
        else:
            message = MessageV0(MessageHeader(required, signed_ro, unsigned_ro), keys, recent, instructions, descriptors)
            try:
                message.sanitize()
            except Exception:
                raise ValueError('MESSAGE_SANITIZATION_FAILED') from None
        raw_message = to_bytes_versioned(message)
        # With <=64 signers the canonical short-vec signature count takes one byte.
        wire_size = 1 + required * 64 + len(raw_message)
        _need(wire_size <= MAX_PACKET_BYTES, 'SIGNED_TRANSACTION_PACKET_BOUND')
        out.update(message_schema_complete=True, message_bytes_base64=base64.b64encode(raw_message).decode(),
            message_sha256=hashlib.sha256(raw_message).hexdigest(), message_size_bytes=len(raw_message),
            transaction_wire_size_bytes=wire_size, static_account_keys=list(static), declared_lookup_slots=slots)
        # Do not short-circuit: retain a result for EVERY declared signature.
        for i, sig in enumerate(sigs):
            valid = sig.verify(keys[i], raw_message)
            out['signer_witnesses'].append({'signature_index': i, 'static_account_index': i,
                'pubkey': static[i], 'signature': signatures[i], 'signature_verified': valid,
                'authorization_for_execution_verified': False})
            if not valid:
                out['errors'].append('SIGNATURE_VERIFICATION_FAILED:' + str(i))
        out['signature_checks_complete'] = True
        out['message_authenticated'] = not out['errors']
        out['status'] = 'signed_message_authenticated_only' if out['message_authenticated'] else 'rejected'
    except UnicodeError:
        out['errors'].append('SOURCE_NON_UTF8_SCALAR')
    except (ValueError, TypeError, OverflowError) as exc:
        out['errors'].append(str(exc) if isinstance(exc, ValueError) else 'SOURCE_OR_MESSAGE_MALFORMED')
    return out
