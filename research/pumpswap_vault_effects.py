"""Conditional legacy vault arithmetic against provider-observed endpoints.

Never authenticates CPI success, deployed code, privileges or recipient permission.
"""
import base64
import re

from desk.providers import SOL, PUMPSWAP
from desk.security import TOKEN_PROGRAM, account_bytes, base58
from desk.programs import address, schemas
from desk.instructions import JUPITER
from desk.route_coverage import instruction_receipt

SEMANTICS_COMMIT = 'ad2b81274075c45e6ef428e52479b7d3d8f0dd6a'
SEMANTICS_BLOBS = {'token/program/src/processor.rs': '7056f2e707ed93282d19ec56e9711d22a24b7498',
                   'token/program/src/state.rs': '8723065ff09a9238a4b123c440144791bd25fbbc'}


def _require(ok, reason):
    if not ok:
        raise ValueError(reason)


def _u64(value):
    _require(type(value) is int and 0 <= value < 2**64, 'INVALID_U64')
    return value


def _amount(value):
    _require(type(value) is str and len(value) <= 20 and re.fullmatch(r'0|[1-9][0-9]*', value), 'INVALID_RAW_AMOUNT')
    return _u64(int(value))


def check_pumpswap_vault_effects(inventory, bindings, value, keys):
    """No IO or mutation; compare two existing vaults without inventing prestate."""
    out = {'provider_observed_conservation_agreement': False, 'passed': False,
           'full_route_policy_passed': False, 'transaction_policy_ok': False,
           'source_authenticated': False, 'runtime_cpi_success_verified': False,
           'runtime_privileges_authenticated': False, 'deployed_program_authenticated': False,
           'finality_authenticated': False, 'authenticated_lifecycle_accepted': False,
           'ownership_approved': False, 'eligible_for_trading': False,
           'reasons': [], 'vaults': [], 'transfers': [],
           'semantics_commit': SEMANTICS_COMMIT, 'semantics_blobs': dict(SEMANTICS_BLOBS),
           'unverified_effect_context': ['INDIVIDUAL_CPI_OUTCOMES', 'DEPLOYED_PROGRAM_EQUIVALENCE', 'PRE_RAW_ACCOUNT_STATE', 'OUTER_ROUTER_EFFECTS'],
           'notice': 'Conditional SPL transfer semantics/provider-observed conservation only; no CPI success, deployed-program authenticity or recipient permission.'}
    try:
        _require(type(keys) is list and 1 <= len(keys) <= 64 and len(set(keys)) == len(keys), 'KEYS_MISSING_DUPLICATE_OR_BOUND')
        for key in keys:
            address(key)
        _require(type(value) is dict and value.get('err', 'missing') is None, 'OBSERVATION_FAILED_OR_UNKNOWN')
        _require(type(bindings) is dict and bindings.get('passed') is True, 'SELL_BINDINGS_UNAVAILABLE')
        names = bindings['account_bindings']
        needed = ('pool', 'user', 'base_mint', 'quote_mint', 'pool_base_token_account', 'pool_quote_token_account',
                  'user_base_token_account', 'user_quote_token_account', 'protocol_fee_recipient_token_account', 'coin_creator_vault_ata')
        for field in needed:
            address(names[field])
        _require(names['quote_mint'] == SOL and names['base_mint'] != SOL, 'VAULT_MINT_PROFILE_UNSUPPORTED')
        vaults = {names['pool_base_token_account']: names['base_mint'], names['pool_quote_token_account']: SOL}
        _require(len(vaults) == 2 and all(k in keys for k in vaults), 'VAULT_IDENTITY_MISSING_OR_ALIASED')
        recipients = {names[f] for f in ('user_quote_token_account', 'protocol_fee_recipient_token_account', 'coin_creator_vault_ata')}
        if 'buyback_fee_recipient_token_account' in names:
            address(names['buyback_fee_recipient_token_account'])
            recipients.add(names['buyback_fee_recipient_token_account'])
        _require(len(recipients) == 3 + int('buyback_fee_recipient_token_account' in names), 'RECIPIENT_ROLE_ALIAS')
        _require(not recipients.intersection(vaults) and names['user_base_token_account'] not in vaults, 'VAULT_ROLE_ALIAS')
        rows = inventory['instructions']
        _require(type(rows) is list and 1 <= len(rows) <= 256 and inventory.get('stack_metadata_verified') is True, 'INVENTORY_MISSING_OR_BOUND')
        paths = set()
        for row in rows:
            _require(type(row) is dict and type(row.get('instruction')) is str and len(row['instruction']) <= 16
                     and re.fullmatch(r'(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))?', row['instruction']), 'INSTRUCTION_PATH_INVALID')
            _require(row['instruction'] not in paths, 'DUPLICATE_INSTRUCTION_PATH')
            paths.add(row['instruction'])
            _require(type(row.get('accounts')) is list and len(row['accounts']) <= 64, 'INSTRUCTION_ACCOUNTS_INVALID')
            for key in row['accounts']:
                address(key)
            _require(type(row.get('data_base64')) is str and len(row['data_base64']) <= 1644, 'INSTRUCTION_DATA_BOUND')
        sell = [r for r in rows if r['instruction'] == bindings['instruction']]
        _require(len(sell) == 1 and sell[0]['program'] == PUMPSWAP and bindings.get('checked_instructions') == [instruction_receipt(sell[0])], 'SELL_RECEIPT_CONFLICT')
        spec = next(s for s in schemas()[PUMPSWAP].values() if s['name'] == 'sell')
        sell_raw = base64.b64decode(sell[0]['data_base64'], validate=True)
        _require(len(sell_raw) == 24 and sell_raw[:8] == bytes(spec['discriminator'])
                 and len(sell[0]['accounts']) >= len(spec['accounts']), 'SELL_RAW_LAYOUT_CONFLICT')
        for i, entry in enumerate(spec['accounts']):
            _require(names.get(entry['name']) == sell[0]['accounts'][i], 'SELL_RAW_ACCOUNT_BINDING_CONFLICT')
            if entry.get('address'):
                _require(names[entry['name']] == entry['address'], 'SELL_FIXED_ACCOUNT_CONFLICT')
        _require(names['base_token_program'] == names['quote_token_program'] == TOKEN_PROGRAM, 'SELL_TOKEN_PROGRAM_CONFLICT')
        if 'buyback_fee_recipient_token_account' in names:
            _require(sell[0]['accounts'][-1] == names['buyback_fee_recipient_token_account'], 'SELL_RAW_BUYBACK_BINDING_CONFLICT')
        parents = [r for r in rows if r['instruction'] == sell[0].get('parent_instruction')]
        _require(len(parents) == 1 and parents[0]['program'] == JUPITER and parents[0].get('stack_height') == 1
                 and sell[0].get('parent_program') == JUPITER and sell[0].get('stack_height') == 2, 'SELL_OUTER_PARENT_CONFLICT')
        metadata = []
        for field in ('preTokenBalances', 'postTokenBalances'):
            balances = value[field]
            _require(type(balances) is list and len(balances) <= 64, 'TOKEN_METADATA_MISSING_OR_BOUND')
            mapped = {}
            for balance in balances:
                idx = _u64(balance['accountIndex'])
                _require(idx < len(keys) and idx not in mapped, 'TOKEN_INDEX_DUPLICATE_OR_RANGE')
                address(balance['mint']); address(balance['owner'])
                _require(balance['programId'] == TOKEN_PROGRAM, 'TOKEN_PROGRAM_UNSUPPORTED')
                amount = _amount(balance['uiTokenAmount']['amount'])
                decimals = balance['uiTokenAmount']['decimals']
                _require(type(decimals) is int and 0 <= decimals <= 255, 'TOKEN_DECIMALS_INVALID')
                mapped[idx] = (balance['mint'], balance['owner'], amount, decimals)
            metadata.append(mapped)
        for key in vaults:
            _require(all(keys.index(key) in side for side in metadata), 'VAULT_TOKEN_ENDPOINT_MISSING')
        accounts = value['accounts']
        _require(type(accounts) is list and len(accounts) == len(keys), 'POST_ACCOUNTS_MISSING')
        native = []
        for field in ('preBalances', 'postBalances'):
            balances = value[field]
            _require(type(balances) is list and len(balances) == len(keys), 'NATIVE_ENDPOINTS_MISSING')
            native.append([_u64(n) for n in balances])
        flow = {k: 0 for k in vaults}
        input_sum = user_output = 0
        for row in rows:
            touched = set(row['accounts']).intersection(vaults)
            # Exact enclosing call frames are retained as unresolved effect
            # context, not treated as safe/no-op instructions.
            if not touched or row is sell[0] or row is parents[0]:
                continue
            raw = base64.b64decode(row['data_base64'], validate=True)
            _require(row['program'] == TOKEN_PROGRAM and raw and raw[0] in (3, 12), 'UNSUPPORTED_VAULT_TOUCHING_OPERATION')
            checked = raw[0] == 12
            _require(len(raw) == (10 if checked else 9) and len(row['accounts']) == (4 if checked else 3), 'TRANSFER_LAYOUT_INVALID')
            _require(row.get('parent_instruction') == bindings['instruction'] and row.get('parent_program') == PUMPSWAP
                     and type(row.get('stack_height')) is int and row['stack_height'] == 3, 'TRANSFER_DIRECT_PARENT_MISMATCH')
            source = row['accounts'][0]; dest = row['accounts'][2 if checked else 1]; authority = row['accounts'][-1]
            role = None
            if source == names['user_base_token_account'] and dest == names['pool_base_token_account'] and authority == names['user']:
                role = 'base_input'
            elif source == names['pool_quote_token_account'] and dest in recipients and authority == names['pool']:
                role = 'quote_output'
            elif source == dest and source in vaults and authority == names['pool']:
                role = 'self_transfer'
            _require(role is not None, 'TRANSFER_VAULT_IDENTITY_OR_AUTHORITY_CONFLICT')
            mint = vaults[next(iter(touched))]
            if checked:
                _require(row['accounts'][1] == mint, 'TRANSFER_MINT_CONFLICT')
                idx = keys.index(next(iter(touched)))
                _require(idx in metadata[0] and raw[9] == metadata[0][idx][3], 'TRANSFER_DECIMALS_CONFLICT')
            n = int.from_bytes(raw[1:9], 'little')
            if role == 'base_input': input_sum += n
            if role == 'quote_output' and dest == names['user_quote_token_account']: user_output += n
            if source in flow: flow[source] -= n
            if dest in flow: flow[dest] += n
            out['transfers'].append({'instruction': row['instruction'], 'source': source, 'destination': dest, 'amount_raw': str(n), 'role': role})
        _require(input_sum == int.from_bytes(sell_raw[8:16], 'little'), 'SELL_RAW_INPUT_SUM_CONFLICT')
        _require(user_output >= int.from_bytes(sell_raw[16:24], 'little'), 'SELL_RAW_OUTPUT_MINIMUM_CONFLICT')
        for key, mint in vaults.items():
            idx = keys.index(key)
            _require(idx in metadata[0] and idx in metadata[1], 'VAULT_TOKEN_ENDPOINT_MISSING')
            before, after = metadata[0][idx], metadata[1][idx]
            _require(before[:2] == after[:2] == (mint, names['pool']) and before[3] == after[3], 'VAULT_METADATA_IDENTITY_CONFLICT')
            account = accounts[idx]
            _require(type(account) is dict and account.get('owner') == TOKEN_PROGRAM and account.get('executable') is False, 'VAULT_POST_ACCOUNT_INVALID')
            _require(type(account.get('data')) is list and type(account['data'][0]) is str and len(account['data'][0]) <= 220, 'VAULT_RAW_STATE_BOUND')
            raw = account_bytes(account)
            _require(len(raw) == 165 and base58(raw[:32]) == mint and base58(raw[32:64]) == names['pool'] and raw[108] == 1, 'VAULT_RAW_IDENTITY_OR_STATE_CONFLICT')
            for start in (72, 109, 129):
                _require(int.from_bytes(raw[start:start+4], 'little') in (0, 1), 'VAULT_COPTION_INVALID')
            _require(raw[72:76] == bytes(4) and raw[121:129] == bytes(8) and raw[129:133] == bytes(4), 'VAULT_ADVERSE_CONTROL')
            _require(int.from_bytes(raw[64:72], 'little') == after[2], 'VAULT_RAW_AMOUNT_CONFLICT')
            _require(_u64(account['lamports']) == native[1][idx], 'VAULT_RAW_LAMPORT_CONFLICT')
            is_native = int.from_bytes(raw[109:113], 'little')
            _require(is_native == int(mint == SOL), 'VAULT_NATIVE_PROFILE_CONFLICT')
            token_delta = after[2] - before[2]
            native_delta = native[1][idx] - native[0][idx]
            if mint == SOL:
                reserve = int.from_bytes(raw[113:121], 'little')
                _require(before[3] == 9 and native[1][idx] >= reserve and native[1][idx] - reserve == after[2], 'WSOL_POST_RESERVE_CONFLICT')
                # Post reserve is observed; this equation tests, never asserts,
                # that pre lamports had the same reserve without a pre byte witness.
                _require(native[0][idx] >= reserve and native[0][idx] - reserve == before[2], 'WSOL_PRE_RESERVE_EQUATION_CONFLICT')
            residual = token_delta - flow[key]
            lamport_residual = native_delta - (flow[key] if mint == SOL else 0)
            out['vaults'].append({'account': key, 'mint': mint, 'pre_raw': str(before[2]), 'post_raw': str(after[2]),
                'conditional_transfer_delta_raw': str(flow[key]), 'observed_delta_raw': str(token_delta),
                'token_residual_raw': str(residual), 'lamport_residual_raw': str(lamport_residual),
                'pre_raw_account_state_available': False})
            if residual: out['reasons'].append('VAULT_TOKEN_RESIDUAL:' + key)
            if lamport_residual: out['reasons'].append('VAULT_LAMPORT_RESIDUAL:' + key)
        out['provider_observed_conservation_agreement'] = not out['reasons']
        out['passed'] = out['provider_observed_conservation_agreement']
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        out['reasons'].append(str(exc) if isinstance(exc, ValueError) else 'MALFORMED_OR_MISSING_WITNESS')
    return out
