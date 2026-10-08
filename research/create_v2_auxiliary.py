"""Disconnected exact auxiliary syntax, not permission for account effects.

System CreateAccount/Transfer, two ComputeBudget operations, and the pinned
Pump fee-program get_fees_with_quote_mint only. No caller, rent, fees, outcomes,
authorization or safe-operation verdict. Unsupported forms retain raw witnesses.
"""
import hashlib

from solders.pubkey import Pubkey

from desk.dynamic_fees import fee_schema
from desk.programs import BorshReader

SYSTEM = '11111111111111111111111111111111'
COMPUTE = 'ComputeBudget111111111111111111111111111111'
FEES = 'pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ'
SOURCE_PINS = (
    ('solana-labs/solana', 'd9f20e951a06b61e4505da0955228020b96a8915',
     'sdk/program/src/system_instruction.rs', 'bb66b4fbce6b67cc40849978b96027357637b609'),
    ('solana-labs/solana', 'd9f20e951a06b61e4505da0955228020b96a8915',
     'sdk/src/compute_budget.rs', 'c903be13c214464cfb8ce0776cdc4db4b2a65d49'),
    ('pump-fun/pump-public-docs', 'cb188ce08b5069196eef1f3e4a0c43b70099793b',
     'idl/pump_fees.json', 'e740baaa16f1e874403ea8d4a7e81179f7eea3cf'),
)


def normalize_create_v2_auxiliary(program, *, raw=None, accounts=None):
    """Normalize bounded raw syntax; ordered accounts must already be resolved.

    Reads the existing checksum-verified official fee IDL and BorshReader, not
    the production AMM-specific fee permission checker. All approvals stay false.
    """
    result = {'program': program, 'raw_hex': raw.hex() if isinstance(raw, bytes) and len(raw) <= 1024 else None,
              'raw_sha256': hashlib.sha256(raw).hexdigest() if isinstance(raw, bytes) else None,
              'account_keys': list(accounts) if isinstance(accounts, list) else None,
              'operation': None, 'syntax_complete': False, 'reasons': [],
              'source_pins': SOURCE_PINS, 'authority_authenticated': False,
              'effect_order_verified': False, 'cpi_success_verified': False,
              'lifecycle_verified': False, 'ownership_approved': False,
              'eligible_for_trading': False}
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > 1024:
            raise ValueError('AUXILIARY_RAW_MISSING_OR_BOUND_EXCEEDED')
        if not isinstance(accounts, list) or len(accounts) > 2:
            raise ValueError('AUXILIARY_ACCOUNT_COUNT_UNSUPPORTED')
        for value in accounts:
            if not isinstance(value, str) or str(Pubkey.from_string(value)) != value:
                raise ValueError('AUXILIARY_KEY_INVALID')
        if program == SYSTEM:
            if len(accounts) != 2:
                raise ValueError('SYSTEM_ACCOUNT_COUNT_UNSUPPORTED')
            tag = int.from_bytes(raw[:4], 'little')
            if tag == 0 and len(raw) == 52:
                result['operation'] = {'kind': 'createAccount', 'payer': accounts[0], 'target': accounts[1],
                    'lamports_raw': str(int.from_bytes(raw[4:12], 'little')),
                    'space': int.from_bytes(raw[12:20], 'little'),
                    'program_owner': str(Pubkey.from_bytes(raw[20:52]))}
            elif tag == 2 and len(raw) == 12:
                result['operation'] = {'kind': 'transfer', 'source': accounts[0], 'destination': accounts[1],
                    'lamports_raw': str(int.from_bytes(raw[4:12], 'little'))}
            else:
                raise ValueError('SYSTEM_TAG_LENGTH_OR_SUFFIX_UNSUPPORTED')
        elif program == COMPUTE:
            if accounts:
                raise ValueError('COMPUTE_ACCOUNTS_UNSUPPORTED')
            if raw[0] == 2 and len(raw) == 5:
                result['operation'] = {'kind': 'setComputeUnitLimit', 'units': int.from_bytes(raw[1:], 'little')}
            elif raw[0] == 3 and len(raw) == 9:
                result['operation'] = {'kind': 'setComputeUnitPrice', 'micro_lamports_raw': str(int.from_bytes(raw[1:], 'little'))}
            else:
                raise ValueError('COMPUTE_TAG_LENGTH_OR_SUFFIX_UNSUPPORTED')
        elif program == FEES:
            schema = fee_schema()
            spec = next(s for s in schema['instructions'] if s['name'] == 'get_fees_with_quote_mint')
            if len(accounts) != 2 or len(raw) != 57 or raw[:8] != bytes(spec['discriminator']):
                raise ValueError('FEE_QUERY_DISCRIMINATOR_ACCOUNTS_LENGTH_OR_SUFFIX_UNSUPPORTED')
            if spec['args'] != [{'name': 'is_pump_pool', 'type': 'bool'},
                    {'name': 'market_cap_lamports', 'type': 'u128'}, {'name': 'quote_mint', 'type': 'pubkey'}]:
                raise ValueError('FEE_QUERY_PRIMARY_SCHEMA_UNSUPPORTED')
            reader = BorshReader(raw[8:], {})
            arguments = {arg['name']: reader.read(arg['type']) for arg in spec['args']}
            arguments['market_cap_lamports'] = str(arguments['market_cap_lamports'])
            result['operation'] = {'kind': 'getFeesWithQuoteMint', 'fee_config': accounts[0],
                                   'config_program': accounts[1], **arguments}
        else:
            raise ValueError('AUXILIARY_PROGRAM_UNSUPPORTED')
        result['syntax_complete'] = True
    except (ValueError, KeyError, TypeError) as exc:
        result['reasons'].append(str(exc))
    return result
