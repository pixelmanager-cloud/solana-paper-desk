"""Bounded offline conditional lamport accounting; no verified-effect verdict.

Each observed raw System CreateAccount/Transfer path contributes once, even if
identical bytes occur at another path. Caller direct writes, closures, realloc,
rent and caught CPI failures are not explained by this model. Net agreement is
only arithmetic over unauthenticated supplied records, never recipient permission.
"""
import copy
import json
import math

from research.create_v2_auxiliary import normalize_create_v2_auxiliary, SYSTEM, COMPUTE, SOURCE_PINS
from research.execution_trace import reconstruct_execution_trace

U64_MAX = (1 << 64) - 1
MAX_SOURCE_BYTES = 1 << 20
MAX_SOURCE_NODES = 32768
MAX_SOURCE_DEPTH = 32
MAX_KEYS = 64
MAX_INSTRUCTIONS = 256
MESSAGE_PIN = ('solana-labs/solana', 'd9f20e951a06b61e4505da0955228020b96a8915',
               'sdk/program/src/message/legacy.rs', '1a6a9239f4e0aaff47f02356ead414f5abc05414')


def _u64(value):
    return type(value) is int and 0 <= value <= U64_MAX


def _bounded_json(record):
    nodes = 0
    estimated_bytes = 0
    ancestors = set()

    def walk(value, depth, path=()):
        nonlocal nodes, estimated_bytes
        nodes += 1
        if type(value) in (dict, list):
            estimated_bytes += 2 + 2 * len(value)
        elif type(value) in (str, int, float, bool) or value is None:
            if type(value) is str and len(value) > 20000:
                raise ValueError('SOURCE_STRING_BOUND')
            if type(value) is int and value.bit_length() > 256:
                raise ValueError('SOURCE_INTEGER_BOUND')
            estimated_bytes += len(json.dumps(value, ensure_ascii=True).encode())
        if estimated_bytes > MAX_SOURCE_BYTES:
            raise ValueError('SOURCE_BYTE_BOUND')
        if depth > MAX_SOURCE_DEPTH or nodes > MAX_SOURCE_NODES:
            raise ValueError('SOURCE_DEPTH_OR_NODE_BOUND')
        if type(value) in (dict, list):
            if id(value) in ancestors:
                raise ValueError('SOURCE_CYCLE')
            ancestors.add(id(value))
            if type(value) is dict:
                for key, item in value.items():
                    if type(key) is not str:
                        raise ValueError('SOURCE_NON_STRING_KEY')
                    walk(key, depth + 1, path + (key,))
                    walk(item, depth + 1, path + (key,))
            else:
                for index, item in enumerate(value):
                    walk(item, depth + 1, path + (index,))
            ancestors.remove(id(value))
        elif type(value) is str:
            if len(value) > 20000:
                raise ValueError('SOURCE_STRING_BOUND')
        elif type(value) is int:
            if value.bit_length() > 256:
                raise ValueError('SOURCE_INTEGER_BOUND')
        elif type(value) is float and len(path) == 5 and path[0] == 'meta' and path[1] in ('preTokenBalances', 'postTokenBalances') and type(path[2]) is int and path[3:] == ('uiTokenAmount', 'uiAmount') and math.isfinite(value):
            # Preserve original display-only RPC token metadata, never use it
            # in lamport arithmetic or convert it into raw monetary amounts.
            pass
        elif value is not None and type(value) is not bool:
            raise ValueError('SOURCE_NON_JSON_OR_FLOAT')
    walk(record, 0)
    if len(json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()) > MAX_SOURCE_BYTES:
        raise ValueError('SOURCE_BYTE_BOUND')


def report_native_lamports(record):
    """Accept the raw RPC transaction object, not a caller-provided inventory.

    Predicted deltas assume every modeled invocation committed, including CPI.
    Supplied meta.err=None does not establish individual CPI success. Fee is the
    supplied meta.fee charged conditionally to message key zero, not independently
    computed from signatures, compute budget, rent or authenticated bank state.
    Signed deltas/residuals and aggregates are exact decimal strings; input
    balances/fee are strict JSON integers in u64 range. No floats or defaults in accounting. Original finite uiTokenAmount.uiAmount
    display metadata is retained only as an opaque source witness.
    """
    out = {'profile': 'offline-native-lamports-v1', 'source_sha256': None,
        'source_provenance': {'auxiliary_layouts': SOURCE_PINS, 'fee_payer_message': MESSAGE_PIN},
        'errors': [], 'gaps': [], 'unknowns': [
            'INPUT_AND_BALANCE_AUTHENTICITY_UNESTABLISHED', 'DECLARED_FEE_NOT_INDEPENDENTLY_ESTABLISHED',
            'RENT_EXEMPTION_AND_RENT_EFFECTS_UNESTABLISHED', 'DEPLOYED_PROGRAM_EFFECT_SEMANTICS_UNESTABLISHED',
            'NET_AGREEMENT_DOES_NOT_AUTHORIZE_RECIPIENTS', 'INTERMEDIATE_EFFECT_ORDER_UNESTABLISHED'],
        'trace_status': None, 'trace_reasons': [], 'trace_errors': [], 'key_witnesses': [],
        'inventory': [], 'movements': [], 'unmodeled_instruction_paths': [],
        'cpi_outcome_unknown_paths': [], 'accounts': [], 'aggregate': None,
        'declared_fee': None, 'fee_payer': None, 'transaction_success_claim': None,
        'comparison_complete': False, 'predicted_net_agreement': False,
        'observed_instruction_model_complete': False, 'status': 'unknown',
        'evidence_authenticated': False, 'balance_effects_verified': False,
        'fee_verified': False, 'rent_verified': False, 'recipient_authorization_verified': False,
        'individual_cpi_success_verified': False, 'effect_order_verified': False,
        'authenticated_lifecycle_accepted': False, 'lifecycle_verified': False,
        'ownership_approved': False, 'eligible_for_trading': False}
    try:
        _bounded_json(record)
    except ValueError as exc:
        out['errors'].append(str(exc)); out['status'] = 'rejected'
        return out
    trace = reconstruct_execution_trace(record)
    out['source_sha256'] = trace['source_sha256']
    out['trace_status'] = trace['status']
    out['key_witnesses'] = copy.deepcopy(trace['key_witnesses'])
    out['source_provenance']['trace_formats'] = trace['source_provenance']
    out['trace_errors'] = trace['errors']
    out['trace_reasons'] = trace['reasons']
    out['errors'].extend('TRACE:' + e for e in trace['errors'])
    out['unknowns'].extend('TRACE:' + r for r in trace['reasons'])
    out['transaction_success_claim'] = trace['execution_success_witness']
    if trace['execution_success_witness'] is not True:
        out['unknowns'].append('TRANSACTION_NOT_SUCCESSFUL_MODEL_COMMIT_ASSUMPTION_UNRESOLVED')
    keys = trace['key_witnesses']
    if not keys or len(keys) > MAX_KEYS or len(trace['instructions']) > MAX_INSTRUCTIONS:
        out['errors'].append('KEY_OR_INVENTORY_UNAVAILABLE_OR_BOUND')
    predicted = [0] * len(keys)
    seen = set()
    for row in trace['instructions']:
        path = row['instruction_path']
        item = {'instruction': copy.deepcopy(row), 'classification': 'unmodeled_program_effects',
                'auxiliary_syntax': None, 'effects_verified': False}
        out['inventory'].append(item)
        if path in seen:
            out['errors'].append('DUPLICATE_INVENTORY_PATH:' + path)
            continue
        seen.add(path)
        if row['inner_index'] is not None:
            out['cpi_outcome_unknown_paths'].append(path)
        if row['program'] in (SYSTEM, COMPUTE):
            syntax = normalize_create_v2_auxiliary(row['program'],
                raw=bytes.fromhex(row['raw_hex']) if row['raw_hex'] is not None else None,
                accounts=row['accounts'])
            item['auxiliary_syntax'] = syntax
            if syntax['syntax_complete'] and row['raw_complete'] and not row['errors']:
                op = syntax['operation']
                if row['program'] == COMPUTE:
                    item['classification'] = 'compute_syntax_only_declared_fee_not_derived'
                    continue
                source = op['payer'] if op['kind'] == 'createAccount' else op['source']
                target = op['target'] if op['kind'] == 'createAccount' else op['destination']
                amount = int(op['lamports_raw'])
                indices = row['account_indices']
                if indices is not None and len(indices) == 2 and all(type(i) is int and 0 <= i < len(keys) for i in indices):
                    # Repeated instruction references can be legal (self transfer).
                    # Accumulate signed deltas rather than deduplicating accounts.
                    predicted[indices[0]] -= amount
                    predicted[indices[1]] += amount
                    item['classification'] = 'conditional_system_movement'
                    out['movements'].append({'instruction_path': path, 'kind': op['kind'],
                        'source': source, 'destination': target, 'account_indices': list(indices),
                        'lamports_raw': str(amount), 'raw_sha256': row['raw_sha256'],
                        'direct_parent_path': row['direct_parent_path'],
                        'assumption': 'invocation_committed_not_individually_verified', 'effects_verified': False})
                    continue
            out['errors'].append('UNSUPPORTED_OR_MALFORMED_AUXILIARY:' + path)
        out['unmodeled_instruction_paths'].append(path)
    if out['unmodeled_instruction_paths']:
        out['unknowns'].append('UNMODELED_DIRECT_LAMPORT_MUTATIONS_OR_OTHER_PROGRAM_EFFECTS_POSSIBLE')
    if out['cpi_outcome_unknown_paths']:
        out['unknowns'].append('INDIVIDUAL_CPI_OUTCOMES_AND_CAUGHT_FAILURES_UNESTABLISHED')
    out['observed_instruction_model_complete'] = bool(out['inventory']) and trace['syntax_complete'] and not out['unmodeled_instruction_paths'] and not out['errors']
    meta = record.get('meta') if isinstance(record, dict) else None
    message = record.get('transaction', {}).get('message') if isinstance(record, dict) and isinstance(record.get('transaction'), dict) else None
    fee_ok = isinstance(meta, dict) and _u64(meta.get('fee'))
    if fee_ok:
        out['declared_fee'] = str(meta['fee'])
    else:
        out['errors'].append('DECLARED_FEE_MISSING_OR_INVALID_U64')
    header = message.get('header') if isinstance(message, dict) else None
    static = [k for k in keys if k['segment'] == 'static']
    fields = {'numRequiredSignatures', 'numReadonlySignedAccounts', 'numReadonlyUnsignedAccounts'}
    header_ok = isinstance(header, dict) and set(header) == fields and all(type(header[f]) is int and 0 <= header[f] <= 255 for f in fields)
    if header_ok:
        n, signed_ro, unsigned_ro = (header[f] for f in ('numRequiredSignatures', 'numReadonlySignedAccounts', 'numReadonlyUnsignedAccounts'))
        header_ok = 1 <= n <= len(static) and signed_ro < n and unsigned_ro <= len(static) - n
    if header_ok and keys:
        witness = keys[0]['witness']
        if isinstance(witness, dict) and (witness.get('signer') is not True or witness.get('writable') is not True):
            header_ok = False
    if not header_ok or not keys:
        out['errors'].append('FEE_PAYER_HEADER_MISSING_OR_CONTRADICTORY')
    else:
        out['fee_payer'] = {'account_index': 0, 'pubkey': keys[0]['pubkey'],
                            'binding': 'message_first_static_writable_signer_claim', 'authority_authenticated': False}
        if fee_ok:
            predicted[0] -= meta['fee']
    if isinstance(meta, dict) and meta.get('rewards') not in (None, []):
        out['unknowns'].append('NONEMPTY_REWARDS_EFFECTS_UNMODELED')
    balances = []
    for field in ('preBalances', 'postBalances'):
        values = meta.get(field) if isinstance(meta, dict) else None
        if not isinstance(values, list) or len(values) != len(keys) or not keys or not all(_u64(v) for v in values):
            out['errors'].append(field.upper() + '_MISSING_SHAPE_OR_U64')
        else:
            balances.append(values)
    if len(balances) == 2 and fee_ok and header_ok and keys:
        pre, post = balances
        for index, key in enumerate(keys):
            observed = post[index] - pre[index]
            residual = observed - predicted[index]
            out['accounts'].append({'account_index': index, 'pubkey': key['pubkey'], 'segment': key['segment'],
                'pre_lamports': str(pre[index]), 'post_lamports': str(post[index]),
                'observed_delta': str(observed), 'conditional_predicted_delta': str(predicted[index]),
                'residual': str(residual), 'net_agrees': residual == 0, 'effects_verified': False})
            if residual:
                out['gaps'].append('ACCOUNT_RESIDUAL:' + str(index))
            if not 0 <= pre[index] + predicted[index] <= U64_MAX:
                out['gaps'].append('CONDITIONAL_PREDICTED_ENDPOINT_OUTSIDE_U64:' + str(index))
        observed_sum, predicted_sum = sum(post) - sum(pre), sum(predicted)
        out['aggregate'] = {'pre_lamports': str(sum(pre)), 'post_lamports': str(sum(post)),
            'observed_delta': str(observed_sum), 'conditional_predicted_delta': str(predicted_sum),
            'declared_fee': str(meta['fee']), 'residual': str(observed_sum - predicted_sum),
            'absolute_account_residual_sum': str(sum(abs(int(a['residual'])) for a in out['accounts'])),
            'net_agrees': observed_sum == predicted_sum, 'effects_verified': False}
        if observed_sum != predicted_sum:
            out['gaps'].append('AGGREGATE_RESIDUAL')
        out['comparison_complete'] = trace['syntax_complete'] and not out['errors'] and trace['execution_success_witness'] is True
        out['predicted_net_agreement'] = out['comparison_complete'] and not out['gaps']
    out['observed_instruction_model_complete'] = out['observed_instruction_model_complete'] and not out['errors']
    out['errors'] = sorted(set(out['errors']))
    out['unknowns'] = sorted(set(out['unknowns']))
    out['status'] = 'rejected' if out['errors'] else 'gaps' if out['gaps'] else 'net_agreement_only' if out['predicted_net_agreement'] else 'unknown'
    return out
