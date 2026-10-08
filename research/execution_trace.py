"""Pure JSON transaction invocation reconstruction, disconnected from approval.

This offline research namespace is intentionally outside the installed desk*
runtime package. Import from a repository checkout as research.execution_trace.
No production consumer may import it; packaging/integration needs later review.

Input is the saved RPC transaction object containing transaction.message, meta
and version, NOT a provider adapter, notification wrapper or signed byte decoder.
The order below is message outer order + recorded CPI invocation-start order.
There are no return events or per-operation state snapshots in this format:
caller writes before/after its children, caught CPI failures, and intermediate
state timing remain unknown. A successful transaction does not prove each CPI
succeeded. Missing stack heights never become an outer-prefix caller guess.

Source pins describe official historical JSON format, not authenticity of an
input response, lookup-table contents at its slot, or deployed code semantics.
Limits preserve the existing 64-key / 256-instruction inspection profile.
"""
import copy
import hashlib
import json

from desk.legacy_controls import normalize_legacy_control
from desk.programs import address, unbase58
from desk.security import TOKEN_PROGRAM

PROFILE = 'raw-json-invocation-trace-v1'
_COMMIT = 'd9f20e951a06b61e4505da0955228020b96a8915'
_PINS = (
    ('transaction-status/src/lib.rs', '0eb13d36819c4a1d8cf6dfa11918fc6e70b42f84'),
    ('transaction-status/src/parse_accounts.rs', '6ad0ec82a6fdad0cf914ebd724f7f3163b2f615e'),
    ('sdk/program/src/message/account_keys.rs', 'f0ab7deeef0987ecfab443f6ac5f78efa0a323a8'),
    ('sdk/program/src/message/versions/v0/loaded.rs', 'c8edbff58b5522aaf864b7d7dbce15112cf551e5'),
    ('sdk/src/inner_instruction.rs', '1a715979ebf1c5e307a0b39110f205e15a5bca27'),
    ('sdk/src/transaction_context.rs', '7df7fc96d6793303c26e3f25c1ec9f7fd0015efd'),
)
_CONTROL_TAGS = {4, 5, 6, 10, 11, 13, 21, 22}


class _Invalid(ValueError):
    pass


def _key(value):
    try:
        return address(value)
    except ValueError:
        raise _Invalid('MALFORMED_KEY') from None


def _index(value, size):
    if type(value) is not int or not 0 <= value < size or value > 255:
        raise _Invalid('INVALID_INSTRUCTION_INDEX')
    return value


def _lookup_counts(message, version):
    if 'addressTableLookups' not in message:
        if version == 'legacy':
            return (0, 0)
        raise _Invalid('LOOKUP_DESCRIPTORS_UNAVAILABLE')
    lookups = message['addressTableLookups']
    if not isinstance(lookups, list) or len(lookups) > 64:
        raise _Invalid('MALFORMED_LOOKUP_DESCRIPTORS')
    if version == 'legacy' and lookups:
        raise _Invalid('LEGACY_LOOKUP_CONTRADICTION')
    counts, tables = [0, 0], set()
    for lookup in lookups:
        if not isinstance(lookup, dict) or set(lookup) != {'accountKey', 'writableIndexes', 'readonlyIndexes'}:
            raise _Invalid('MALFORMED_LOOKUP_DESCRIPTORS')
        table = _key(lookup['accountKey'])
        if table in tables:
            raise _Invalid('DUPLICATE_LOOKUP_TABLE')
        tables.add(table)
        seen = set()
        for segment, field in enumerate(('writableIndexes', 'readonlyIndexes')):
            indices = lookup[field]
            if not isinstance(indices, list) or len(indices) > 64:
                raise _Invalid('MALFORMED_LOOKUP_INDICES')
            for index in indices:
                _index(index, 256)
                if index in seen:
                    raise _Invalid('DUPLICATE_LOOKUP_INDEX')
                seen.add(index)
            counts[segment] += len(indices)
    return tuple(counts)


def _keys(message, meta, version):
    values = message.get('accountKeys')
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise _Invalid('KEY_INSPECTION_BUDGET_OR_SHAPE')
    counts = _lookup_counts(message, version)
    loaded = meta.get('loadedAddresses')
    segments = None
    if 'loadedAddresses' in meta and loaded is not None:
        if not isinstance(loaded, dict) or set(loaded) != {'writable', 'readonly'}:
            raise _Invalid('MALFORMED_LOADED_ADDRESSES')
        segments = []
        for field in ('writable', 'readonly'):
            if not isinstance(loaded[field], list) or len(loaded[field]) > 64:
                raise _Invalid('MALFORMED_LOADED_ADDRESSES')
            segments.append([_key(key) for key in loaded[field]])
        if tuple(map(len, segments)) != counts:
            raise _Invalid('LOOKUP_COUNT_DISAGREEMENT')
    rows = []
    if all(isinstance(value, str) for value in values):
        rows = [{'pubkey': _key(value), 'segment': 'static', 'witness': value} for value in values]
        if segments is None and any(counts):
            raise _Invalid('LOADED_ADDRESSES_UNAVAILABLE')
        for segment, keys in zip(('loaded_writable', 'loaded_readonly'), segments or ([], [])):
            rows.extend({'pubkey': key, 'segment': segment, 'witness': key} for key in keys)
    elif all(isinstance(value, dict) for value in values):
        # jsonParsed already expands lookup keys. Do not append them again.
        static_count = len(values) - sum(counts)
        if static_count < 1:
            raise _Invalid('LOOKUP_COUNT_DISAGREEMENT')
        for index, value in enumerate(values):
            if not {'pubkey', 'signer', 'writable', 'source'} <= set(value):
                raise _Invalid('PARSED_KEY_FIELDS_UNAVAILABLE')
            if type(value['signer']) is not bool or type(value['writable']) is not bool:
                raise _Invalid('MALFORMED_PARSED_KEY_FLAGS')
            expected_source = 'transaction' if index < static_count else 'lookupTable'
            if value['source'] != expected_source:
                raise _Invalid('PARSED_KEY_SOURCE_DISAGREEMENT')
            segment = ('static' if index < static_count else 'loaded_writable'
                       if index < static_count + counts[0] else 'loaded_readonly')
            if segment != 'static' and value['signer']:
                raise _Invalid('LOADED_SIGNER_CONTRADICTION')
            # Writable loaded keys may be demoted by the runtime, so false does
            # not partition the expanded key list into writable/readonly groups.
            if segment == 'loaded_readonly' and value['writable']:
                raise _Invalid('LOADED_WRITABLE_CONTRADICTION')
            rows.append({'pubkey': _key(value['pubkey']), 'segment': segment,
                         'witness': copy.deepcopy(value)})
        if segments is not None:
            expected = [row['pubkey'] for row in rows[static_count:]]
            if expected != segments[0] + segments[1]:
                raise _Invalid('PARSED_LOADED_KEYS_DISAGREE')
    else:
        raise _Invalid('MIXED_KEY_ENCODING')
    if len(rows) > 64:
        raise _Invalid('KEY_INSPECTION_BUDGET_OR_SHAPE')
    if len({row['pubkey'] for row in rows}) != len(rows):
        raise _Invalid('DUPLICATE_TRANSACTION_KEY')
    return rows


def _instruction(ix, keys):
    result = {'program': None, 'account_indices': None, 'accounts': None,
              'raw_hex': None, 'raw_sha256': None, 'raw_complete': False,
              'parsed_supplied': isinstance(ix, dict) and 'parsed' in ix,
              'parsed_agreement': None, 'control_comparison': None,
              'reasons': [], 'errors': [], 'witness': copy.deepcopy(ix)}
    try:
        if not isinstance(ix, dict):
            raise _Invalid('MALFORMED_INSTRUCTION')
        if set(ix) - {'programIdIndex', 'programId', 'accounts', 'data', 'stackHeight', 'parsed', 'program'}:
            raise _Invalid('UNSUPPORTED_INSTRUCTION_FIELDS')
        if 'programIdIndex' in ix:
            result['program'] = keys[_index(ix['programIdIndex'], len(keys))]
            if 'programId' in ix and _key(ix['programId']) != result['program']:
                raise _Invalid('RAW_PARSED_PROGRAM_DISAGREEMENT')
            accounts = ix.get('accounts')
            if not isinstance(accounts, list) or len(accounts) > 64:
                raise _Invalid('MALFORMED_INSTRUCTION_ACCOUNTS')
            indices = [_index(index, len(keys)) for index in accounts]
            result['account_indices'] = indices
            result['accounts'] = [keys[index] for index in indices]
        else:
            result['program'] = _key(ix.get('programId'))
            if result['program'] not in keys:
                raise _Invalid('INSTRUCTION_KEY_NOT_IN_TRANSACTION')
            if 'accounts' in ix:
                accounts = ix['accounts']
                if not isinstance(accounts, list) or len(accounts) > 64:
                    raise _Invalid('MALFORMED_INSTRUCTION_ACCOUNTS')
                result['accounts'] = [_key(key) for key in accounts]
                if any(key not in keys for key in result['accounts']):
                    raise _Invalid('INSTRUCTION_KEY_NOT_IN_TRANSACTION')
                result['account_indices'] = [keys.index(key) for key in result['accounts']]
        if 'data' in ix:
            data = ix['data']
            if not isinstance(data, str) or len(data) > 20000:
                raise _Invalid('MALFORMED_INSTRUCTION_DATA')
            try:
                raw = unbase58(data) if data else b''
            except ValueError:
                raise _Invalid('MALFORMED_INSTRUCTION_DATA') from None
            result['raw_hex'] = raw.hex()
            result['raw_sha256'] = hashlib.sha256(raw).hexdigest()
            if result['accounts'] is None:
                raise _Invalid('RAW_ACCOUNTS_UNAVAILABLE')
            result['raw_complete'] = True
        else:
            raw = None
            result['reasons'].append('RAW_INSTRUCTION_UNAVAILABLE')
        if 'parsed' in ix:
            parsed = ix['parsed']
            if not isinstance(parsed, dict) or not isinstance(parsed.get('type'), str) or not isinstance(parsed.get('info'), dict):
                raise _Invalid('MALFORMED_PARSED_INSTRUCTION')
            if raw is not None:
                if result['program'] == TOKEN_PROGRAM and raw and raw[0] in _CONTROL_TAGS:
                    comparison = normalize_legacy_control(result['program'], raw=raw,
                                                          accounts=result['accounts'], parsed=parsed)
                    result['control_comparison'] = comparison
                    result['parsed_agreement'] = comparison['representations_match']
                    if not comparison['normalization_complete']:
                        raise _Invalid('RAW_PARSED_CONTROL_DISAGREEMENT_OR_INVALID')
                else:
                    # Do not invent general parsed semantics or silently ignore
                    # a second representation. A future pinned comparator is needed.
                    result['reasons'].append('RAW_PARSED_COMPARISON_UNSUPPORTED')
        elif raw is None:
            raise _Invalid('INSTRUCTION_REPRESENTATION_UNAVAILABLE')
    except _Invalid as exc:
        result['errors'].append(str(exc))
    return result


def reconstruct_execution_trace(record):
    """Retain a syntactic invocation preorder and direct-parent witnesses.

    Rejected = contradictory/malformed structure; unknown = missing indispensable
    information; complete = this limited syntactic profile only. No result grants
    execution effect order, CPI success, signer/PDA privileges or safe controls.
    Account references can repeat legally; duplicate transaction keys and inner
    group indices cannot. Paths are [outer] or [outer, inner-list ordinal], NOT a
    nesting path. Parent paths come solely from a valid stack-height sequence.
    """
    result = {'profile': PROFILE, 'source_record': copy.deepcopy(record),
              'source_sha256': None, 'key_witnesses': [], 'instructions': [],
              'reasons': [], 'errors': [], 'status': 'unknown',
              'syntax_complete': False, 'execution_success_witness': None,
              'evidence_authenticated': False, 'lookup_tables_authenticated': False,
              'authority_authenticated': False, 'operation_effect_order_verified': False,
              'cpi_success_verified': False, 'lifecycle_verified': False,
              'ownership_approved': False, 'eligible_for_trading': False,
              'unresolved_context': ['INPUT_AUTHENTICITY', 'LOOKUP_TABLE_SLOT_BINDING',
                                     'CALLER_PRIVILEGES', 'INDIVIDUAL_CPI_SUCCESS',
                                     'CALLER_EFFECT_TIMING', 'DEPLOYED_CODE_SEMANTICS',
                                     'HISTORICAL_STATE_AND_COVERAGE'],
              'order_kind': 'message_outer_and_recorded_inner_invocation_preorder',
              'source_provenance': [{'repository': 'solana-labs/solana', 'commit': _COMMIT,
                                     'path': path, 'git_blob': blob} for path, blob in _PINS]}
    try:
        result['source_sha256'] = hashlib.sha256(json.dumps(record, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if not isinstance(record, dict) or not isinstance(record.get('transaction'), dict):
            raise _Invalid('TRANSACTION_OBJECT_UNAVAILABLE')
        message = record['transaction'].get('message')
        meta = record.get('meta')
        if not isinstance(message, dict):
            raise _Invalid('MESSAGE_UNAVAILABLE')
        if not isinstance(meta, dict):
            result['reasons'].append('TRANSACTION_METADATA_UNAVAILABLE')
            meta = {}
        version = record.get('version')
        if version != 'legacy' and not (type(version) is int and version == 0):
            raise _Invalid('TRANSACTION_VERSION_UNAVAILABLE_OR_UNSUPPORTED')
        result['key_witnesses'] = _keys(message, meta, version)
        keys = [row['pubkey'] for row in result['key_witnesses']]
        outer = message.get('instructions')
        if not isinstance(outer, list) or not 1 <= len(outer) <= 64:
            raise _Invalid('OUTER_INSPECTION_BUDGET_OR_SHAPE')
        if 'err' not in meta:
            result['reasons'].append('EXECUTION_STATUS_UNAVAILABLE')
        else:
            result['execution_success_witness'] = meta['err'] is None
            if meta['err'] is not None:
                result['reasons'].append('FAILED_TRANSACTION_EXECUTED_OUTER_PREFIX_UNKNOWN')
        groups = meta.get('innerInstructions')
        if groups is None:
            result['reasons'].append('INNER_INSTRUCTIONS_UNAVAILABLE')
            groups = []
        if not isinstance(groups, list) or len(groups) > len(outer):
            raise _Invalid('MALFORMED_INNER_GROUPS')
        by_outer = {}
        for position, group in enumerate(groups):
            if not isinstance(group, dict) or set(group) != {'index', 'instructions'}:
                raise _Invalid('MALFORMED_INNER_GROUP')
            index = _index(group['index'], len(outer))
            if index in by_outer:
                raise _Invalid('DUPLICATE_INNER_GROUP_INDEX')
            if not isinstance(group['instructions'], list):
                raise _Invalid('MALFORMED_INNER_INSTRUCTIONS')
            by_outer[index] = (position, group['instructions'])
        if len(outer) + sum(len(group[1]) for group in by_outer.values()) > 256:
            raise _Invalid('INSTRUCTION_INSPECTION_BUDGET')
        for outer_index, instruction in enumerate(outer):
            row = _instruction(instruction, keys)
            row.update(instruction_path=str(outer_index), path_components=[outer_index],
                       outer_index=outer_index, inner_index=None, group_source_position=None,
                       source_path=f'transaction.message.instructions.{outer_index}',
                       stack_height=1, stack_height_presence=isinstance(instruction, dict) and 'stackHeight' in instruction,
                       direct_parent_path=None, caller_program=None, caller_context='transaction_root',
                       execution_observed=result['execution_success_witness'] is True)
            if isinstance(instruction, dict) and 'stackHeight' in instruction:
                height = instruction['stackHeight']
                # Official raw message JSON encodes outer stackHeight=null;
                # parsed outer JSON uses 1. Both retain intrinsic root depth.
                if height is not None and (type(height) is not int or height != 1):
                    row['errors'].append('OUTER_STACK_HEIGHT_CONTRADICTION')
            result['instructions'].append(row)
            stack = {1: row}
            previous_height = 1
            position, children = by_outer.get(outer_index, (None, []))
            for inner_index, instruction in enumerate(children):
                child = _instruction(instruction, keys)
                height = instruction.get('stackHeight') if isinstance(instruction, dict) else None
                child.update(instruction_path=f'{outer_index}.{inner_index}',
                             path_components=[outer_index, inner_index], outer_index=outer_index,
                             inner_index=inner_index, group_source_position=position,
                             source_path=f'meta.innerInstructions.{position}.instructions.{inner_index}',
                             stack_height=height,
                             stack_height_presence=isinstance(instruction, dict) and 'stackHeight' in instruction,
                             direct_parent_path=None, caller_program=None, caller_context='unknown',
                             execution_observed=True)
                if height is None:
                    child['reasons'].append('CALLER_STACK_HEIGHT_UNAVAILABLE')
                    stack = {1: row}
                    previous_height = None
                elif type(height) is not int or not 2 <= height <= 16:
                    child['errors'].append('INNER_STACK_HEIGHT_INVALID')
                    stack = {1: row}
                    previous_height = None
                elif previous_height is not None and height > previous_height + 1:
                    child['errors'].append('IMPOSSIBLE_STACK_JUMP')
                    stack = {1: row}
                    previous_height = None
                else:
                    parent = stack.get(height - 1)
                    if parent is None:
                        child['reasons'].append('CALLER_CHAIN_UNRESOLVED_AFTER_DEPTH_GAP')
                    else:
                        child['direct_parent_path'] = parent['instruction_path']
                        child['caller_program'] = parent['program'] if not parent['errors'] else None
                        child['caller_context'] = 'metadata_direct_parent' if child['caller_program'] else 'unknown'
                        if child['caller_program'] is None:
                            child['reasons'].append('CALLER_PROGRAM_UNRESOLVED')
                    stack = {depth: ancestor for depth, ancestor in stack.items() if depth < height}
                    stack[height] = child
                    previous_height = height
                result['instructions'].append(child)
        for ordinal, row in enumerate(result['instructions']):
            row['invocation_ordinal'] = ordinal
            result['reasons'].extend(row['reasons'])
            result['errors'].extend(row['errors'])
    except _Invalid as exc:
        reason = str(exc)
        if reason.endswith('UNAVAILABLE') or reason in {'TRANSACTION_VERSION_UNAVAILABLE_OR_UNSUPPORTED'}:
            result['reasons'].append(reason)
        else:
            result['errors'].append(reason)
    except (TypeError, ValueError) as exc:
        # Non-JSON / NaN input cannot have a stable evidence identity.
        result['errors'].append('NON_JSON_RECORD')
    result['reasons'] = sorted(set(result['reasons']))
    result['errors'] = sorted(set(result['errors']))
    result['syntax_complete'] = not result['reasons'] and not result['errors']
    result['status'] = 'rejected' if result['errors'] else 'complete' if result['syntax_complete'] else 'unknown'
    return result
