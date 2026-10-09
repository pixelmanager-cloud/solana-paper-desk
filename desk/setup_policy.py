"""Observed narrow WSOL ATA creation order; never successful CPI/effect proof."""
import base64
import re
from .route_coverage import instruction_receipt
from .instructions import ATA, SYSTEM
from .security import TOKEN_PROGRAM
from .providers import SOL
from .programs import address, unbase58


def check_sell_setup(inventory, wallet):
    from solders.pubkey import Pubkey
    address(wallet)
    target = str(Pubkey.find_program_address([unbase58(wallet), unbase58(TOKEN_PROGRAM), unbase58(SOL)], Pubkey.from_string(ATA))[0])
    out = {'passed': False, 'full_route_policy_passed': False, 'source_authenticated': False,
           'runtime_cpi_success_verified': False, 'deployed_program_authenticated': False,
           'eligible_for_trading': False, 'reasons': [], 'checked_instructions': [],
           'wallet_wsol_account': target, 'operations': {}, 'unresolved_inventory_reasons': [],
           'notice': 'Observed creation-sequence agreement only. Rent, prestate, CPI success, privileges, deployed code and full route approval remain unverified.'}
    def require(ok, reason):
        if not ok:
            raise ValueError(reason)
    try:
        require(type(inventory) is dict and inventory.get('stack_metadata_verified') is True, 'SETUP_STACK_UNVERIFIED')
        rows = inventory.get('instructions')
        require(type(rows) is list and 1 <= len(rows) <= 256, 'SETUP_INVENTORY_MISSING_OR_BOUND')
        errors = inventory.get('reasons')
        require(type(errors) is list and len(errors) <= 256 and all(type(x) is str and len(x) <= 256 for x in errors), 'SETUP_INVENTORY_ERRORS_MISSING_OR_MALFORMED')
        # An identified unsupported route program outside ATA is left to route
        # policy. Lost rows/accounts or any other error cannot prove enumeration.
        require(all(x == 'UNSUPPORTED_ROUTE_PROGRAM' for x in errors), 'SETUP_INVENTORY_ENUMERATION_OR_OPERATION_ERROR')
        require(type(inventory.get('inventory_checks_passed')) is bool and inventory['inventory_checks_passed'] == (not errors), 'SETUP_INVENTORY_SUMMARY_CONTRADICTORY')
        out['unresolved_inventory_reasons'] = list(errors)
        paths = {}; decoded = {}
        for row in rows:
            require(type(row) is dict, 'SETUP_ROW_MALFORMED')
            path = row.get('instruction')
            require(type(path) is str and len(path) <= 16 and re.fullmatch(r'(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*))?', path), 'SETUP_PATH_INVALID')
            require(path not in paths, 'SETUP_DUPLICATE_PATH')
            paths[path] = row
            require(type(row.get('stack_height')) is int and 1 <= row['stack_height'] <= 16, 'SETUP_DEPTH_INVALID')
            require(type(row.get('accounts')) is list and len(row['accounts']) <= 64, 'SETUP_ACCOUNTS_INVALID')
            address(row['program'])
            for key in row['accounts']: address(key)
            flags = row.get('reasons')
            require(type(flags) is list and len(flags) <= 256 and all(type(x) is str and x == 'UNSUPPORTED_ROUTE_PROGRAM' for x in flags), 'SETUP_ROW_OPERATION_OR_ENUMERATION_ERROR')
            require(not flags or 'UNSUPPORTED_ROUTE_PROGRAM' in errors, 'SETUP_ROW_ERRORS_CONTRADICTORY')
            require(type(row.get('data_base64')) is str and len(row['data_base64']) <= 1644, 'SETUP_DATA_INVALID_OR_BOUND')
            decoded[path] = base64.b64decode(row['data_base64'], validate=True)
        parents = [r for r in rows if r['program'] == ATA and r['stack_height'] == 1]
        require(len(parents) == 1, 'SETUP_REQUIRES_ONE_ATA_CREATION')
        parent = parents[0]; path = parent['instruction']
        require('.' not in path and parent.get('parent_instruction') is None and parent.get('parent_program') is None, 'SETUP_OUTER_CONTEXT_INVALID')
        require(not parent['reasons'] and decoded[path] == b'\x01' and parent['accounts'] == [wallet, target, wallet, SOL, SYSTEM, TOKEN_PROGRAM], 'SETUP_OUTER_ATA_BINDING_MISMATCH')
        children = [r for r in rows if r['instruction'].startswith(path + '.') or r.get('parent_instruction') == path]
        require(bool(children), 'SETUP_EXISTING_ATA_NOOP_UNSUPPORTED')
        require(all(r['instruction'].startswith(path + '.') and r['stack_height'] == 2 and r.get('parent_instruction') == path and r.get('parent_program') == ATA and not r['reasons'] for r in children), 'SETUP_DIRECT_CHILD_CONTEXT_INVALID')
        children.sort(key=lambda r: int(r['instruction'].split('.')[1]))
        require(len(children) == 4 and [r['instruction'] for r in children] == [path + '.' + str(i) for i in range(4)], 'SETUP_CREATION_SEQUENCE_MISSING_EXTRA_OR_GAPPED')
        size, create, immutable, initialize = children
        require(size['program'] == TOKEN_PROGRAM and decoded[size['instruction']] in (b'\x15', b'\x15\x07\x00') and size['accounts'] == [SOL], 'SETUP_SIZE_ORDER_OR_BINDING_MISMATCH')
        raw = decoded[create['instruction']]
        require(create['program'] == SYSTEM and len(raw) == 52 and raw[:4] == bytes(4) and create['accounts'] == [wallet, target] and 0 < int.from_bytes(raw[4:12], 'little') < 2**64 and int.from_bytes(raw[12:20], 'little') == 165 and raw[20:] == unbase58(TOKEN_PROGRAM), 'SETUP_CREATE_ORDER_BINDING_OR_FUNDED_BRANCH_UNSUPPORTED')
        require(immutable['program'] == TOKEN_PROGRAM and decoded[immutable['instruction']] == b'\x16' and immutable['accounts'] == [target], 'SETUP_IMMUTABLE_ORDER_OR_BINDING_MISMATCH')
        require(initialize['program'] == TOKEN_PROGRAM and decoded[initialize['instruction']] == b'\x12' + unbase58(wallet) and initialize['accounts'] == [target, SOL], 'SETUP_INITIALIZE_ORDER_OR_BINDING_MISMATCH')
        checked = [instruction_receipt(r) for r in children]
        child_paths = {r['instruction'] for r in children}
        for row in rows:
            if row['instruction'] in child_paths or row['program'] not in (SYSTEM, TOKEN_PROGRAM): continue
            raw = decoded[row['instruction']]
            if row['program'] == TOKEN_PROGRAM and raw and raw[0] in (3, 12): continue  # Separate recipient policy.
            if row['program'] == TOKEN_PROGRAM and raw == b'\x09' and row['stack_height'] == 1:
                require('.' not in row['instruction'] and row.get('parent_instruction') is None and row.get('parent_program') is None and row['accounts'] == [target, wallet, wallet], 'SETUP_OUTER_CLOSE_MISMATCH')
                checked.append(instruction_receipt(row))
            else:
                require(False, 'SETUP_OPERATION_OUTSIDE_WALLET_ATA')
        out.update(passed=True, checked_instructions=checked, operations={k: 1 for k in ('size', 'create', 'immutable', 'initialize')}, creation_parent=instruction_receipt(parent))
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        out['reasons'].append(str(exc) if isinstance(exc, ValueError) else 'SETUP_MALFORMED_WITNESS')
    return out
