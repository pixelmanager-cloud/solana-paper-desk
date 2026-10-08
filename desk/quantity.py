"""Lossless raw-debit unit witnesses. Integrity binding is not route approval."""
import hashlib
from decimal import Decimal, InvalidOperation

from .model import digest
from .programs import address
from .security import TOKEN_PROGRAM, account_bytes, mint_policy


def raw_u64(value):
    if type(value) is int:
        number = value
    elif (isinstance(value, str) and 1 <= len(value) <= 20 and value.isascii()
          and value.isdigit() and (value == '0' or value[0] != '0')):
        number = int(value)
    else:
        raise ValueError('QUANTITY_RAW_INTEGER_REQUIRED')
    if not 0 <= number < 2**64:
        raise ValueError('QUANTITY_RAW_U64_REQUIRED')
    return number


def _human(raw, decimals):
    # Tuple construction is exact even under precision=1, traps, tiny Emax or
    # nondefault rounding. Division/scaleb/quantize would use ambient context.
    return Decimal((0, tuple(int(c) for c in str(raw)), -decimals))


def _declared_human(value, expected):
    if not isinstance(value, (str, Decimal)) or isinstance(value, str) and len(value) > 512:
        raise ValueError('QUANTITY_HUMAN_DECIMAL_REQUIRED')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError('QUANTITY_HUMAN_DECIMAL_REQUIRED') from None
    if not number.is_finite() or number != expected:
        raise ValueError('QUANTITY_RAW_HUMAN_MISMATCH')


def quantity_witness(mint, wallet, amount, *, mint_source, simulation, keys,
                     transaction_hash, human_quantity=None):
    """Bind original legacy mint state to an exact observed net token debit.

    Sources are original RPC lookup envelopes or saved sequence pre-state rows.
    No floating uiAmount, caller decimals, normalized effect flag or price is used.
    Transaction hashes are local source identities, not authenticated execution.
    """
    address(mint); address(wallet)
    raw_amount = raw_u64(amount)
    if raw_amount == 0:
        raise ValueError('QUANTITY_POSITIVE_DEBIT_REQUIRED')
    if (not isinstance(transaction_hash, str) or len(transaction_hash) != 64
            or any(c not in '0123456789abcdef' for c in transaction_hash)):
        raise ValueError('QUANTITY_TRANSACTION_IDENTITY_REQUIRED')
    if mint_source.get('method') == 'getMultipleAccounts':
        source_keys, options = mint_source['params']
        if options.get('encoding') != 'base64' or options.get('commitment') not in ('confirmed', 'finalized'):
            raise ValueError('QUANTITY_MINT_SOURCE_INVALID')
        accounts = mint_source['result']['value']
        slot = mint_source['result']['context']['slot']
    elif mint_source.get('kind') == 'sequence_pre_execution_accounts':
        if mint_source.get('transaction_hash') != transaction_hash:
            raise ValueError('QUANTITY_TRANSACTION_IDENTITY_MISMATCH')
        source_keys = mint_source['keys']
        accounts = mint_source['result']['preExecutionAccounts']
        slot = mint_source['slot']
    else:
        raise ValueError('QUANTITY_MINT_SOURCE_MISSING')
    if (not isinstance(source_keys, list) or not 1 <= len(source_keys) <= 64
            or len(set(source_keys)) != len(source_keys) or mint not in source_keys
            or not isinstance(accounts, list) or len(accounts) != len(source_keys)
            or type(slot) is not int or slot < 0):
        raise ValueError('QUANTITY_MINT_IDENTITY_UNBOUND')
    for key in source_keys: address(key)
    account = accounts[source_keys.index(mint)]
    policy = mint_policy(account)
    if policy['decision'] != 'PASS_TOKEN_POLICY':
        raise ValueError('QUANTITY_MINT_STATE_UNSUPPORTED')
    mint_bytes = account_bytes(account)
    decimals = mint_bytes[44]  # Original initialized legacy Mint u8, not metadata.
    if human_quantity is not None:
        _declared_human(human_quantity, _human(raw_amount, decimals))
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 64
            or len(set(keys)) != len(keys) or wallet not in keys):
        raise ValueError('QUANTITY_SIMULATION_KEYS_INVALID')
    for key in keys: address(key)
    if simulation.get('err', 'unknown') is not None:
        raise ValueError('QUANTITY_SIMULATION_FAILED_OR_UNKNOWN')
    native = [simulation.get(k) for k in ('preBalances', 'postBalances')]
    if any(not isinstance(rows, list) or len(rows) != len(keys) for rows in native):
        raise ValueError('QUANTITY_BALANCE_METADATA_MISSING')
    native = [[raw_u64(n) for n in rows] for rows in native]
    maps = []
    for field in ('preTokenBalances', 'postTokenBalances'):
        rows = simulation.get(field)
        if not isinstance(rows, list) or len(rows) > len(keys):
            raise ValueError('QUANTITY_TOKEN_METADATA_MISSING')
        parsed = {}
        for row in rows:
            index = row['accountIndex']
            if type(index) is not int or not 0 <= index < len(keys) or index in parsed:
                raise ValueError('QUANTITY_TOKEN_INDEX_INVALID')
            if not isinstance(row.get('owner'), str) or not isinstance(row.get('mint'), str):
                raise ValueError('QUANTITY_TOKEN_IDENTITY_MISSING')
            amount_row = raw_u64(row['uiTokenAmount']['amount'])
            if row['mint'] == mint:
                meta_decimals = row['uiTokenAmount'].get('decimals')
                if (type(meta_decimals) is not int or not 0 <= meta_decimals <= 255
                        or meta_decimals != decimals or row.get('programId') != TOKEN_PROGRAM):
                    raise ValueError('QUANTITY_TOKEN_UNIT_CONFLICT')
                if 'uiAmountString' in row['uiTokenAmount']:
                    _declared_human(row['uiTokenAmount']['uiAmountString'], _human(amount_row, decimals))
            parsed[index] = (row['owner'], row['mint'], amount_row)
        maps.append(parsed)
    totals = [0, 0]
    witnessed = set()
    for index in maps[0].keys() | maps[1].keys():
        before, after = maps[0].get(index), maps[1].get(index)
        if not any(row and row[:2] == (wallet, mint) for row in (before, after)):
            continue
        if before and after and before[:2] != after[:2]:
            raise ValueError('QUANTITY_TOKEN_IDENTITY_CHANGED')
        if before is None and native[0][index] != 0 or after is None and native[1][index] != 0:
            raise ValueError('QUANTITY_TOKEN_ENDPOINT_MISSING')
        witnessed.add(index)
        for i, row in enumerate((before, after)):
            if row: totals[i] += row[2]
    if not witnessed or totals[0] - totals[1] != raw_amount:
        raise ValueError('QUANTITY_EXACT_RAW_DEBIT_MISMATCH')
    if max(totals) > int(policy['supply_raw']):
        raise ValueError('QUANTITY_SUPPLY_CONFLICT')
    # When the original simulation returned a Mint, contradictory state cannot
    # silently supply different units. Absence is not invented into a post-state.
    returned = simulation.get('accounts')
    if mint in keys and isinstance(returned, list) and len(returned) == len(keys):
        post_mint = returned[keys.index(mint)]
        if (not isinstance(post_mint, dict) or post_mint.get('owner') != account['owner']
                or post_mint.get('executable') is not False or account_bytes(post_mint) != mint_bytes):
            raise ValueError('QUANTITY_MINT_STATE_CONFLICT')
    result = {'kind': 'raw_debit_quantity_witness_v1', 'status': 'WITNESSED_RAW_QUANTITY',
              'mint': mint, 'wallet': wallet, 'token_program': account['owner'],
              'mint_decimals': decimals, 'mint_supply_raw': policy['supply_raw'],
              'raw_u64': str(raw_amount), 'human_decimal': format(_human(raw_amount, decimals), 'f'),
              'mint_source_slot': slot, 'transaction_hash': transaction_hash,
              'mint_source_hash': digest(mint_source), 'mint_account_hash': digest(account),
              'mint_bytes_hash': hashlib.sha256(mint_bytes).hexdigest(),
              'simulation_hash': digest(simulation), 'keys_hash': digest(keys),
              'token_account_indices': sorted(witnessed),
              'eligible_for_trading': False, 'transaction_policy_ok': False,
              'scope': 'Historical/local raw unit and net-debit consistency only; no route or execution approval'}
    result['witness_hash'] = digest(result)
    return result


def diagnostic_quantity(*args, **kwargs):
    """Missing/conflicting evidence stays an explicit unknown diagnostic."""
    try:
        return quantity_witness(*args, **kwargs)
    except (ValueError, KeyError, TypeError, IndexError, AttributeError, InvalidOperation, RecursionError) as exc:
        reason = str(exc)
        if not reason.startswith('QUANTITY_') or not reason.replace('_', '').isalnum():
            reason = 'QUANTITY_ORIGINAL_INPUTS_MISSING_OR_MALFORMED'
        return {'kind': 'raw_debit_quantity_witness_v1', 'status': 'UNKNOWN',
                'reasons': [reason], 'eligible_for_trading': False, 'transaction_policy_ok': False}
