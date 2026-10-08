"""Disconnected exact log/inventory correspondence; never final syscall proof.

Whole log entries are parsed, never program-generated embedded lines. Runtime
success precedes frame pop/caller synchronization in the pinned source model.
No runtime/lifecycle consumer imports this research module.
"""
import copy
import re

from research.execution_trace import reconstruct_execution_trace

PROFILE = 'runtime-log-witness-v1'
MAX_LOG_ENTRIES = 2048
MAX_LOG_BYTES = 262144
MAX_DEPTH = 16
_KEY = r'[1-9A-HJ-NP-Za-km-z]{32,44}'
_INVOKE = re.compile(r'Program (' + _KEY + r') invoke \[([1-9][0-9]*)\]')
_AUX = re.compile(r'Program (' + _KEY + r') consumed [0-9]+ of [0-9]+ compute units|Program return: (' + _KEY + r') [A-Za-z0-9+/=]*')
_END = re.compile(r'Program (' + _KEY + r') (success|failed: [^\r\n]+)')
SOURCE_PROVENANCE = {
    'repository': 'solana-labs/solana',
    'commit': 'd9f20e951a06b61e4505da0955228020b96a8915',
    'paths': {
        'program-runtime/src/stable_log.rs': '748c4d7639214a510db193a5254342c2a47d24cb',
        'program-runtime/src/invoke_context.rs': '8259c2ed2bcc7ac4b315d1e76feebf53224f5641',
        'programs/bpf_loader/src/syscalls/cpi.rs': '13f9cbaf905275cbc07ac96642b2c7667911851f',
    },
}


def report_runtime_log_witness(record):
    """Join supplied raw RPC record to exact ordered runtime frames, zero I/O.

    Candidate pairs are retained after a mismatch but never resynchronized or
    upgraded to complete. Every original trace row and log entry is preserved.
    Depth/order/program matches describe supplied metadata, not authenticated
    execution or signer privileges. Failed/caught branches remain unknown.
    """
    trace = reconstruct_execution_trace(record)
    meta = record.get('meta') if isinstance(record, dict) else None
    logs = meta.get('logMessages') if isinstance(meta, dict) else None
    result = {'profile': PROFILE, 'source_sha256': trace['source_sha256'],
              'trace': trace, 'source_record': copy.deepcopy(record),
              'source_log_messages': copy.deepcopy(logs),
              'log_entries': [], 'frames': [], 'instructions': [], 'unknowns': [],
              'status': 'unknown', 'join_complete': False,
              'input_authenticated': False, 'finality_verified': False,
              'final_syscall_success_verified': False, 'cpi_success_verified': False,
              'authority_authenticated': False, 'lifecycle_verified': False,
              'ownership_approved': False, 'eligible_for_trading': False,
              'source_provenance': copy.deepcopy(SOURCE_PROVENANCE)}
    for row in trace['instructions']:
        result['instructions'].append({'source_instruction': copy.deepcopy(row),
            'instruction_path': row['instruction_path'], 'candidate_frame_index': None,
            'log_match_complete': False, 'program_return': 'unknown',
            'final_syscall_outcome': 'unknown', 'cpi_success_verified': False})
    unknowns = result['unknowns']
    if not trace['syntax_complete']:
        unknowns.append('TRACE_INCOMPLETE')
    if not isinstance(logs, list):
        unknowns.append('LOG_MESSAGES_UNAVAILABLE_OR_MALFORMED')
        return result
    result['log_entries'] = [{'source_index': i, 'witness': copy.deepcopy(line),
                             'kind': 'uninspected'} for i, line in enumerate(logs)]
    if (len(logs) > MAX_LOG_ENTRIES or any(not isinstance(line, str) for line in logs)):
        unknowns.append('LOG_INSPECTION_BUDGET_OR_SHAPE')
        return result
    try:
        size = sum(len(line.encode('utf-8')) for line in logs)
    except UnicodeError:
        unknowns.append('LOG_ENCODING_INVALID')
        return result
    if size > MAX_LOG_BYTES:
        unknowns.append('LOG_BYTE_BUDGET_EXHAUSTED')
        return result
    stack, cursor = [], 0
    rows = trace['instructions']
    for index, line in enumerate(logs):
        entry = result['log_entries'][index]
        if line.startswith(('Program log:', 'Program data:')):
            entry['kind'] = 'program_text'
            continue
        if line == 'Log truncated':
            entry['kind'] = 'truncation'
            unknowns.append('LOG_TRUNCATED')
            break
        invoke, end = _INVOKE.fullmatch(line), _END.fullmatch(line)
        if invoke:
            program, depth_text = invoke.groups()
            # Bound textual depth before integer conversion.
            depth = int(depth_text) if len(depth_text) <= 2 else MAX_DEPTH + 1
            entry['kind'] = 'runtime_invoke'
            if depth > MAX_DEPTH or depth != len(stack) + 1:
                unknowns.append('RUNTIME_DEPTH_OR_STACK_MISMATCH')
                break
            if cursor >= len(rows):
                unknowns.append('EXTRA_RUNTIME_INVOCATION')
                break
            row = rows[cursor]
            parent = rows[stack[-1]['ordinal']]['instruction_path'] if stack else None
            if (row['program'] != program or row['stack_height'] != depth
                    or row['direct_parent_path'] != parent):
                unknowns.append('INSTRUCTION_PROGRAM_DEPTH_ORDER_OR_PARENT_MISMATCH')
                break
            frame = {'instruction_path': row['instruction_path'], 'ordinal': cursor,
                     'program': program, 'depth': depth, 'invoke_log_index': index,
                     'return_log_index': None, 'program_return': 'unknown',
                     'failed_descendant': False, 'final_syscall_outcome': 'unknown'}
            result['instructions'][cursor]['candidate_frame_index'] = len(result['frames'])
            result['frames'].append(frame)
            stack.append(frame)
            cursor += 1
        elif end:
            entry['kind'] = 'runtime_return'
            program, outcome = end.groups()
            if not stack or stack[-1]['program'] != program:
                unknowns.append('RUNTIME_RETURN_PROGRAM_OR_ORDER_MISMATCH')
                break
            frame = stack.pop()
            frame['return_log_index'] = index
            frame['program_return'] = 'success_log' if outcome == 'success' else 'failed_log'
            if outcome != 'success':
                unknowns.append('FAILED_OR_CAUGHT_PROGRAM_BRANCH')
                for ancestor in stack:
                    ancestor['failed_descendant'] = True
        elif line.startswith('Program ') and (' invoke' in line or ' success' in line or ' failed:' in line):
            entry['kind'] = 'malformed_runtime_record'
            unknowns.append('MALFORMED_RUNTIME_RECORD')
            break
        elif _AUX.fullmatch(line):
            entry['kind'] = 'auxiliary_log'
        else:
            entry['kind'] = 'unclassified_log'
            unknowns.append('UNCLASSIFIED_LOG_ENTRY')
    if stack:
        unknowns.append('RUNTIME_FRAMES_UNCLOSED')
    if cursor != len(rows):
        unknowns.append('INSTRUCTIONS_WITHOUT_RUNTIME_INVOCATION')
    if not logs:
        unknowns.append('LOG_MESSAGES_EMPTY')
    if not isinstance(meta, dict) or meta.get('err', 'missing') is not None:
        unknowns.append('TRANSACTION_FAILED_OR_STATUS_MISSING')
    result['unknowns'] = sorted(set(unknowns))
    result['join_complete'] = not result['unknowns']
    result['status'] = 'correspondence_only' if result['join_complete'] else 'unknown'
    for frame in result['frames']:
        item = result['instructions'][frame['ordinal']]
        item['program_return'] = frame['program_return']
        item['log_match_complete'] = result['join_complete']
    # Even a complete join leaves post-log pop/synchronization and authenticity
    # unresolved. No field above can imply final syscall or lifecycle success.
    return result
