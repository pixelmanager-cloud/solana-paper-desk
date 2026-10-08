"""Pure legacy SPL control syntax normalization, deliberately disconnected.

No state, signatures, CPI callers, program deployment or lifecycle is verified.
PR27's unsupported-control blockers must not be resolved using this output.
A public finalized legacy Pump birth fixture and independent lifecycle checker
remain separate dependencies. Parsed-only captures retain witnesses but cannot
establish exact raw layout. Source pins describe historical reference semantics,
not the code deployed at a transaction's slot.

Accepted raw profiles are canonical SetAuthority (roles 0..3), Approve,
ApproveChecked, Revoke, FreezeAccount, ThawAccount, InitializeImmutableOwner and
GetAccountDataSize (empty or the ATA builder's single ImmutableOwner request).
Multisig/extra accounts and every other extension/tag/suffix remain unsupported.
Normalization of a dangerous operation is never a safe-operation verdict.
"""
import copy
import hashlib
import re

from .programs import address
from .security import TOKEN_PROGRAM, TOKEN_2022, base58


PROFILE = 'legacy-control-syntax-v1'
# Git blob IDs pin complete official files, not excerpts or caller assertions.
_SPL_COMMIT = 'ad2b81274075c45e6ef428e52479b7d3d8f0dd6a'
_PINS = (
    ('solana-labs/solana-program-library', _SPL_COMMIT,
     'token/program/src/instruction.rs', 'e798abdea4dc930354b970dd3dc36f098d4bb4d3'),
    ('solana-labs/solana-program-library', _SPL_COMMIT,
     'token/program/src/processor.rs', '7056f2e707ed93282d19ec56e9711d22a24b7498'),
    ('solana-labs/solana-program-library', _SPL_COMMIT,
     'associated-token-account/program/src/processor.rs', '20767a247d15212c031d891d3e098e60e4c904b6'),
    ('solana-labs/solana-program-library', _SPL_COMMIT,
     'token/program-2022/src/instruction.rs', 'ddd56fc8447207964feb5278cd062089103ff2ce'),
    ('solana-labs/solana-program-library', _SPL_COMMIT,
     'token/program-2022/src/extension/mod.rs', '6a6af568f38c176bf295a73b643095da7adf2ed2'),
    ('solana-labs/solana', 'd9f20e951a06b61e4505da0955228020b96a8915',
     'transaction-status/src/parse_token.rs', 'c7111ee622ad4e5f4d8e8d5c931a63c797366e13'),
)
_ROLES = ('mintTokens', 'freezeAccount', 'accountOwner', 'closeAccount')
_KINDS = {4: 'approve', 5: 'revoke', 6: 'setAuthority', 10: 'freezeAccount',
          11: 'thawAccount', 13: 'approveChecked', 21: 'getAccountDataSize',
          22: 'initializeImmutableOwner'}
_UNSET = object()
_PATH = re.compile(r'(0|[1-9][0-9]{0,5})(?:\.(0|[1-9][0-9]{0,5}))?\Z')


class _Invalid(ValueError):
    def __init__(self, reason, field=None):
        self.reason, self.field = reason, field


def _require(info, field):
    if field not in info:
        raise _Invalid('PARSED_FIELD_MISSING', field)
    return info[field]


def _address(value, field):
    try:
        return address(value)
    except ValueError:
        raise _Invalid('CONTROL_ADDRESS_INVALID', field) from None


def _amount(value, field):
    # Official parsed token amounts are base-10 strings. No float/UI conversion.
    if (not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,20}', value)
            or int(value) >= 2**64):
        raise _Invalid('PARSED_AMOUNT_INVALID', field)
    return str(int(value))


def _operation(kind, **fields):
    result = dict(kind=kind, target_account=None, target_mint=None, authority=None,
                  authority_role=None, delegate=None, amount_raw=None, decimals=None,
                  new_authority_presence='not_applicable', new_authority=None,
                  requested_extension_types=None)
    result.update(fields)
    return result


def _accounts(accounts):
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 64:
        raise _Invalid('CONTROL_ACCOUNTS_MISSING_OR_INVALID')
    rows = []
    for index, value in enumerate(accounts):
        if isinstance(value, str):
            rows.append({'pubkey': _address(value, f'accounts.{index}'),
                         'signer_present': False, 'is_signer': None,
                         'writable_present': False, 'is_writable': None})
        elif isinstance(value, dict) and set(value) <= {'pubkey', 'isSigner', 'isWritable'}:
            row = {'pubkey': _address(value.get('pubkey'), f'accounts.{index}')}
            for field, label in (('isSigner', 'signer'), ('isWritable', 'writable')):
                if field in value and type(value[field]) is not bool:
                    raise _Invalid('CONTROL_ACCOUNT_FLAGS_INVALID', f'accounts.{index}.{field}')
                row[f'{label}_present'] = field in value
                row[f'is_{label}'] = value[field] if field in value else None
            rows.append(row)
        else:
            raise _Invalid('CONTROL_ACCOUNT_ENCODING_UNSUPPORTED', f'accounts.{index}')
    return rows


def _raw(raw, rows):
    if not isinstance(raw, bytes) or not raw:
        raise _Invalid('RAW_CONTROL_BYTES_MISSING_OR_INVALID')
    tag = raw[0]
    if tag not in _KINDS:
        raise _Invalid('CONTROL_TAG_UNSUPPORTED')
    kind = _KINDS[tag]
    count = {4: 3, 5: 2, 6: 2, 10: 3, 11: 3, 13: 4, 21: 1, 22: 1}[tag]
    if len(rows) != count:
        raise _Invalid('CONTROL_ACCOUNT_COUNT_UNSUPPORTED')
    keys = [row['pubkey'] for row in rows]
    if tag == 6:
        if len(raw) < 3:
            raise _Invalid('RAW_CONTROL_LAYOUT_MISMATCH')
        if raw[1] >= len(_ROLES):
            raise _Invalid('CONTROL_AUTHORITY_ROLE_UNSUPPORTED')
        option = raw[2]
        if option not in (0, 1):
            raise _Invalid('CONTROL_AUTHORITY_OPTION_INVALID')
        if len(raw) != (3 if option == 0 else 35):
            raise _Invalid('RAW_CONTROL_LAYOUT_MISMATCH')
        role = _ROLES[raw[1]]
        return _operation(kind, **{
            'target_mint' if raw[1] < 2 else 'target_account': keys[0],
            'authority': keys[1], 'authority_role': role,
            'new_authority_presence': 'explicit_none' if option == 0 else 'explicit_key',
            'new_authority': None if option == 0 else base58(raw[3:35])})
    if tag == 21:
        # The shared ATA builder emits u16 extension ID 7. Legacy execution
        # ignores it and returns 165; no extension or immutability is enabled.
        if raw not in (b'\x15', b'\x15\x07\x00'):
            raise _Invalid('CONTROL_EXTENSION_REQUEST_UNSUPPORTED')
        return _operation(kind, target_mint=keys[0],
                          requested_extension_types=[] if len(raw) == 1 else ['immutableOwner'])
    expected = {4: 9, 5: 1, 10: 1, 11: 1, 13: 10, 22: 1}[tag]
    if len(raw) != expected:
        raise _Invalid('RAW_CONTROL_LAYOUT_MISMATCH')
    if tag in (4, 13):
        return _operation(kind, target_account=keys[0],
                          target_mint=keys[1] if tag == 13 else None,
                          delegate=keys[-2], authority=keys[-1], authority_role='accountOwner',
                          amount_raw=str(int.from_bytes(raw[1:9], 'little')),
                          decimals=raw[9] if tag == 13 else None)
    if tag == 5:
        return _operation(kind, target_account=keys[0], authority=keys[1], authority_role='accountOwner')
    if tag in (10, 11):
        return _operation(kind, target_account=keys[0], target_mint=keys[1],
                          authority=keys[2], authority_role='freezeAccount')
    return _operation(kind, target_account=keys[0])


def _parsed(parsed):
    if not isinstance(parsed, dict) or set(parsed) != {'type', 'info'}:
        raise _Invalid('PARSED_CONTROL_SHAPE_INVALID')
    kind, info = parsed['type'], parsed['info']
    if not isinstance(kind, str) or kind not in _KINDS.values():
        raise _Invalid('PARSED_CONTROL_TYPE_UNSUPPORTED')
    if not isinstance(info, dict):
        raise _Invalid('PARSED_CONTROL_INFO_INVALID')
    if any(not isinstance(field, str) for field in info):
        raise _Invalid('PARSED_CONTROL_INFO_INVALID')
    def key(field):
        return _address(_require(info, field), field)
    if kind == 'setAuthority':
        role = _require(info, 'authorityType')
        if not isinstance(role, str) or role not in _ROLES:
            raise _Invalid('CONTROL_AUTHORITY_ROLE_UNSUPPORTED', 'authorityType')
        target = 'mint' if role in _ROLES[:2] else 'account'
        allowed = {target, 'authorityType', 'authority', 'newAuthority'}
        value = _require(info, 'newAuthority')
        operation = _operation(kind, **{
            'target_mint' if target == 'mint' else 'target_account': key(target),
            'authority': key('authority'), 'authority_role': role,
            'new_authority_presence': 'explicit_none' if value is None else 'explicit_key',
            'new_authority': None if value is None else _address(value, 'newAuthority')})
    elif kind in ('approve', 'approveChecked'):
        allowed = {'source', 'delegate', 'owner', 'amount'} if kind == 'approve' else {
            'source', 'mint', 'delegate', 'owner', 'tokenAmount'}
        mint, decimals = None, None
        if kind == 'approveChecked':
            token = _require(info, 'tokenAmount')
            if not isinstance(token, dict) or not set(token) <= {'amount', 'decimals', 'uiAmount', 'uiAmountString'}:
                raise _Invalid('PARSED_TOKEN_AMOUNT_INVALID', 'tokenAmount')
            amount = _amount(_require(token, 'amount'), 'tokenAmount.amount')
            decimals = _require(token, 'decimals')
            if type(decimals) is not int or not 0 <= decimals <= 255:
                raise _Invalid('PARSED_DECIMALS_INVALID', 'tokenAmount.decimals')
            mint = key('mint')
        else:
            amount = _amount(_require(info, 'amount'), 'amount')
        operation = _operation(kind, target_account=key('source'), target_mint=mint,
                               authority=key('owner'), authority_role='accountOwner',
                               delegate=key('delegate'), amount_raw=amount, decimals=decimals)
    elif kind == 'revoke':
        allowed = {'source', 'owner'}
        operation = _operation(kind, target_account=key('source'), authority=key('owner'),
                               authority_role='accountOwner')
    elif kind in ('freezeAccount', 'thawAccount'):
        allowed = {'account', 'mint', 'freezeAuthority'}
        operation = _operation(kind, target_account=key('account'), target_mint=key('mint'),
                               authority=key('freezeAuthority'), authority_role='freezeAccount')
    elif kind == 'getAccountDataSize':
        allowed = {'mint', 'extensionTypes'}
        # Official parser omits this OPTIONAL field for an empty request only.
        extensions = info['extensionTypes'] if 'extensionTypes' in info else []
        if extensions not in ([], ['immutableOwner']):
            raise _Invalid('CONTROL_EXTENSION_REQUEST_UNSUPPORTED', 'extensionTypes')
        operation = _operation(kind, target_mint=key('mint'), requested_extension_types=copy.deepcopy(extensions))
    else:
        allowed = {'account'}
        operation = _operation(kind, target_account=key('account'))
    if set(info) - allowed:
        raise _Invalid('PARSED_CONTROL_EXTRA_FIELDS', ','.join(sorted(set(info) - allowed)))
    return operation


def _context(evidence):
    if evidence is None:
        return None
    if not isinstance(evidence, dict):
        raise _Invalid('CONTROL_EVIDENCE_SHAPE_INVALID')
    components = None
    for field in ('instruction_path', 'parent_instruction_path'):
        if field in evidence and evidence[field] is not None:
            value = evidence[field]
            if not isinstance(value, str) or not _PATH.fullmatch(value):
                raise _Invalid('CONTROL_EVIDENCE_PATH_INVALID', field)
            if field == 'instruction_path':
                components = [int(x) for x in value.split('.')]
    if 'caller_program' in evidence and evidence['caller_program'] is not None:
        _address(evidence['caller_program'], 'caller_program')
    for field, lower, upper in (('stack_height', 1, 16), ('slot', 0, 2**64 - 1)):
        if field in evidence and (type(evidence[field]) is not int or not lower <= evidence[field] <= upper):
            raise _Invalid('CONTROL_EVIDENCE_VALUE_INVALID', field)
    return components


def normalize_legacy_control(program, *, raw=None, accounts=None, parsed=_UNSET, evidence=None):
    """Return distinct raw/parsed witnesses, never authorization or a state proof.

    `raw` is decoded bytes, `accounts` are resolved pubkeys or explicit account
    metas (pubkey/isSigner/isWritable). Account indices must be resolved upstream.
    Parsed JSON is optional; when supplied it must contain every required field
    and agree exactly with raw bytes/bindings. `None` is a malformed supplied
    parsed object, distinct from not supplying it. Evidence is copied verbatim
    as untrusted input; syntax checks cannot authenticate caller/signers/order.
    """
    result = {
        'profile': PROFILE, 'program': program,
        'program_family': 'legacy' if program == TOKEN_PROGRAM else 'token2022' if program == TOKEN_2022 else 'unknown',
        'raw_operation': None, 'parsed_operation': None, 'raw_layout_complete': False,
        'parsed_supplied': parsed is not _UNSET, 'parsed_fields_complete': False,
        'representations_match': None, 'normalization_complete': False,
        'raw_sha256': hashlib.sha256(raw).hexdigest() if isinstance(raw, bytes) else None,
        'raw_hex': raw.hex() if isinstance(raw, bytes) and len(raw) <= 128 else None,
        'raw_length': len(raw) if isinstance(raw, bytes) else None,
        'raw_tag': raw[0] if isinstance(raw, bytes) and raw else None,
        'account_witness': copy.deepcopy(accounts), 'account_metas': None,
        'parsed_witness': None if parsed is _UNSET else copy.deepcopy(parsed),
        'parsed_field_presence': {}, 'untrusted_evidence': copy.deepcopy(evidence),
        'instruction_path_components': None, 'reasons': [], 'field_errors': [],
        'authority_authenticated': False, 'evidence_authenticated': False,
        'deployed_code_verified': False, 'lifecycle_verified': False,
        'ownership_approved': False, 'eligible_for_trading': False,
        'runtime_blockers': ['UNSUPPORTED_TOKEN_CONTROL_OPERATION', 'INDEPENDENT_CONTROL_LIFECYCLE_REQUIRED'],
        'source_provenance': [{'repository': repo, 'commit': commit, 'path': path, 'git_blob': blob}
                              for repo, commit, path, blob in _PINS],
    }
    if (isinstance(parsed, dict) and isinstance(parsed.get('info'), dict)
            and all(isinstance(field, str) for field in parsed['info'])):
        info = parsed['info']
        fields = set(info) | {'mint', 'account', 'source', 'authority', 'authorityType',
                              'newAuthority', 'owner', 'delegate', 'freezeAuthority',
                              'amount', 'tokenAmount', 'extensionTypes'}
        result['parsed_field_presence'] = {field: field in info for field in sorted(fields)}
    def error(exc):
        result['reasons'].append(exc.reason)
        if exc.field is not None:
            result['field_errors'].append({'reason': exc.reason, 'field': exc.field})
    try:
        result['instruction_path_components'] = _context(evidence)
    except _Invalid as exc:
        error(exc)
    if program != TOKEN_PROGRAM:
        result['reasons'].append('TOKEN_2022_CONTROL_UNSUPPORTED' if program == TOKEN_2022 else 'CONTROL_PROGRAM_UNSUPPORTED')
    else:
        try:
            if not isinstance(raw, bytes) or not raw:
                raise _Invalid('RAW_CONTROL_BYTES_MISSING_OR_INVALID')
            rows = _accounts(accounts)
            result['account_metas'] = rows
            result['raw_operation'] = _raw(raw, rows)
            result['raw_layout_complete'] = True
        except _Invalid as exc:
            error(exc)
        if parsed is not _UNSET:
            try:
                result['parsed_operation'] = _parsed(parsed)
                result['parsed_fields_complete'] = True
            except _Invalid as exc:
                error(exc)
        if result['raw_operation'] is not None and result['parsed_operation'] is not None:
            result['representations_match'] = result['raw_operation'] == result['parsed_operation']
            if not result['representations_match']:
                result['reasons'].append('CONTROL_RAW_PARSED_MISMATCH')
    result['reasons'] = sorted(set(result['reasons']))
    result['normalization_complete'] = result['raw_layout_complete'] and not result['reasons']
    # Reference rule only, conditional on real execution with known state/code.
    result['reference_semantics'] = None
    operation = result['raw_operation']
    if operation and operation['kind'] == 'initializeImmutableOwner':
        result['reference_semantics'] = 'legacy_no_state_write_requires_uninitialized_account; owner_is_not_immutable'
    elif operation and operation['kind'] == 'getAccountDataSize':
        result['reference_semantics'] = 'legacy_read_only_account_size_165; requested_extensions_do_not_enable_extensions'
    return result
