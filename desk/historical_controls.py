"""Syntax witnesses from already-bound legacy history; never an interval proof.

Internal helper: continuation_snapshot must first bind source/head/jobs/bank,
and the existing read guard must be held. The supplied ReplayView is the only
raw cache. Capture positions are identities, not authenticated effect order.
"""
from .account_keys import compiled_keys, compiled_instruction, index
from .decode import decode
from .legacy_controls import normalize_legacy_control
from .legacy_token_accounting import accounting, CONTROLS, KINDS
from .model import canonical, digest
from .programs import address, unbase58
from .security import TOKEN_PROGRAM, TOKEN_2022

MAX_OPERATIONS = 256
MAX_INSTRUCTIONS = 256
MAX_INSTRUCTION_BYTES = 16384
MAX_OUTPUT_BYTES = 1024 * 1024
LIFETIMES = {'initializeMint','initializeMint2','initializeAccount',
             'initializeAccount2','initializeAccount3','closeAccount'}
CONTROL_KINDS = set(CONTROLS.values())
MOVEMENTS = {'transfer','transferChecked','mintTo','mintToChecked','burn','burnChecked'}


class ControlLimit(ValueError):
    pass


def unavailable_controls():
    return {'profile':'observed-legacy-history-controls-v1', 'status':'UNAVAILABLE',
            'operations':[], 'gaps':[], 'frontier':[], 'query_evidence_hashes':[],
            'evidence_hashes':[], 'record_occurrences':0, 'unique_records':0,
            'observed_rows_enumerated':False, 'unknown_reasons':['CONTROL_RAW_HISTORY_UNAVAILABLE'],
            'order_basis':'Capture positions and original instruction ordinals only; no global chronology',
            'effect_order_verified':False, 'individual_cpi_success_verified':False,
            'historical_control_verified':False, 'source_authenticated':False,
            'ownership_approved':False, 'eligible_for_trading':False}


def _instruction_rows(raw):
    message = raw['transaction']['message']; meta = raw['meta']
    outer = message.get('instructions')
    if type(outer) is not list or len(outer) > MAX_INSTRUCTIONS:
        raise ValueError('Instruction bound')
    values = message.get('accountKeys')
    compiled = type(values) is list and bool(values) and all(type(k) is str for k in values)
    keys = compiled_keys(message,meta,raw.get('version'),raw['transaction']['signatures'])[0] if compiled else None
    rows = [(str(i),None,None,ix) for i,ix in enumerate(outer)]
    groups = meta.get('innerInstructions'); reasons = []
    if groups is None:
        reasons.append('CONTROL_INNER_INSTRUCTIONS_UNAVAILABLE'); groups = []
    if type(groups) is not list or len(groups) > MAX_INSTRUCTIONS:
        raise ValueError('Inner instruction bound')
    parents = set()
    for ordinal,group in enumerate(groups):
        if type(group) is not dict or type(group.get('instructions')) is not list:
            reasons.append('CONTROL_INNER_GROUP_MALFORMED'); continue
        try: parent = index(group['index'],len(outer))
        except (ValueError,KeyError,TypeError):
            reasons.append('CONTROL_INNER_PARENT_INVALID'); continue
        if parent in parents: reasons.append('CONTROL_INNER_PARENT_DUPLICATE')
        parents.add(parent)
        if len(rows)+len(group['instructions']) > MAX_INSTRUCTIONS:
            reasons.append('CONTROL_INSTRUCTION_LIMIT'); break
        rows.extend((f'{parent}.{i}',ordinal,i,ix) for i,ix in enumerate(group['instructions']))
    return rows,keys,reasons


def _operation(ix, path, decoded):
    reasons = []; program = address(ix['programId'])
    if program not in (TOKEN_PROGRAM,TOKEN_2022): return None
    parsed = ix.get('parsed'); parsed_kind = parsed.get('type') if type(parsed) is dict else None
    if type(parsed_kind) is not str: parsed_kind = None
    raw = None
    if 'data' in ix:
        if type(ix['data']) is not str or len(ix['data']) > 256:
            reasons.append('CONTROL_RAW_DATA_INVALID_OR_OVERSIZED')
        else:
            try: raw = unbase58(ix['data'])
            except ValueError: reasons.append('CONTROL_RAW_DATA_INVALID_OR_OVERSIZED')
    tag = raw[0] if raw else None; raw_kind = KINDS.get(tag,CONTROLS.get(tag))
    # Never let a parsed movement conceal a raw control or the reverse.
    if not reasons:
        if parsed_kind in MOVEMENTS and 'data' not in ix: return None
        if raw_kind in MOVEMENTS and 'parsed' not in ix: return None
    kind = raw_kind or (parsed_kind if type(parsed_kind) is str and
                        parsed_kind in CONTROL_KINDS | LIFETIMES | MOVEMENTS else 'unknownTokenInstruction')
    result = {'kind':kind,'raw_kind':raw_kind,
              'parsed_kind':parsed_kind if parsed_kind in CONTROL_KINDS | LIFETIMES | MOVEMENTS else None,
              'program':program,'raw_tag':tag,'raw_operation':None,'parsed_operation':None,
              'raw_syntax_complete':False,'representations_match':None,'normalizer_profile':None,
              'instruction_hash':digest(ix),'target_account':None,'target_mint':None}
    if raw_kind in CONTROL_KINDS or parsed_kind in CONTROL_KINDS:
        kwargs = {'raw':raw,'accounts':ix.get('accounts')}
        if 'parsed' in ix: kwargs['parsed'] = parsed
        norm = normalize_legacy_control(program,**kwargs)
        result.update(raw_operation=norm['raw_operation'],parsed_operation=norm['parsed_operation'],
                      raw_syntax_complete=norm['normalization_complete'],
                      representations_match=norm['representations_match'],normalizer_profile=norm['profile'])
        reasons.extend(norm['reasons'])
    else:
        if raw is not None:
            norm = accounting(ix); result['normalizer_profile'] = 'legacy-token-accounting-syntax'
            if norm['status'] == 'ACCOUNTING_SYNTAX':
                result['raw_operation'] = norm['row']; result['raw_syntax_complete'] = True
            else: reasons.append('CONTROL_RAW_OPERATION_UNSUPPORTED_OR_MALFORMED')
        if 'parsed' in ix and decoded is not None:
            for field in ('mint_initializations','token_account_initializations','token_account_closures'):
                matches = [r for r in decoded[field] if r['instruction']==path]
                if len(matches)==1:
                    result['parsed_operation'] = {k:v for k,v in matches[0].items()
                                                   if k not in ('instruction','program','type')}
                    try:
                        for field in ('account','mint','owner','destination','mint_authority','freeze_authority'):
                            value = result['parsed_operation'].get(field)
                            if value is not None: address(value)
                    except ValueError:
                        result['parsed_operation'] = None
                        reasons.append('CONTROL_PARSED_LIFETIME_BINDING_INVALID')
        if result['parsed_operation'] is None and 'parsed' in ix:
            reasons.append('CONTROL_PARSED_OPERATION_UNSUPPORTED_OR_MALFORMED')
        if raw is not None and 'parsed' in ix:
            reasons.append('CONTROL_LIFETIME_RAW_PARSED_COMPARISON_UNVERIFIED')
    if raw is None: reasons.append('CONTROL_RAW_INSTRUCTION_UNAVAILABLE')
    if program == TOKEN_2022: reasons.append('TOKEN_2022_CONTROL_UNSUPPORTED')
    if kind == 'unknownTokenInstruction': reasons.append('CONTROL_OPERATION_UNKNOWN')
    operations = [o for o in (result['raw_operation'],result['parsed_operation']) if o]
    accounts = {o.get('target_account',o.get('account')) for o in operations} - {None}
    mints = {o.get('target_mint',o.get('mint')) for o in operations} - {None}
    if len(accounts)>1 or len(mints)>1: reasons.append('CONTROL_TARGET_CONTRADICTION')
    if len(accounts)==1: result['target_account'] = next(iter(accounts))
    if len(mints)==1: result['target_mint'] = next(iter(mints))
    result['unknown_reasons'] = sorted(set(reasons))
    return result


def observed_controls(mint, snapshot, history, queries, view):
    """Retain observed syntax and conflicting versions, never first/last-wins.

    Identity indexes below contain output row references, not raw record caches.
    Only ReplayView loads persisted pages. No lexical-signature chronology.
    """
    result = unavailable_controls(); result['status'] = 'OBSERVED_INVENTORY'
    frontier = snapshot['params'][0]; result['frontier'] = list(frontier)
    relevant = [q for q in queries if q['address'] in frontier]
    reasons = []; hashes = set(); versions = {}; identities = {}; output_bytes = 0
    if any(sum(q['address']==key for q in relevant)!=1 for key in frontier):
        reasons.append('CONTROL_FRONTIER_QUERY_MISSING_OR_AMBIGUOUS')
    if history['account_queries']['reasons'] or history['query_reasons']:
        reasons.append('CONTROL_HISTORY_COVERAGE_UNVERIFIED')
        reasons.extend(history['account_queries']['reasons'] + history['query_reasons'])
    if history['block_ordering']['reasons']:
        reasons.append('CONTROL_TRANSACTION_ORDER_UNVERIFIED')
    if not history['inventory']['initialization_inventory_verified']:
        reasons.append('CONTROL_FRONTIER_COMPLETENESS_UNVERIFIED')
    for q in relevant:
        result['query_evidence_hashes'].append(q['evidence_hash'])
        if (q['token_accounts_filter']!='none' or q.get('slot_range')!={'gte':0,'lt':snapshot['result']['context']['slot']+1}
                or not q['query_coverage_verified']):
            reasons.append('CONTROL_QUERY_COVERAGE_UNVERIFIED')
            reasons.extend(q['reasons'])
        for page_index,page in enumerate(q['pages']):
            hashes.update((page['request_evidence_hash'],page['payload_hash']))
            for record_index,raw in enumerate(view.load(page['payload_hash'])['data']):
                result['record_occurrences'] += 1
                if result['record_occurrences'] > 1800: raise ControlLimit('Control record ceiling')
                fingerprint = digest(raw)
                origin = {'query_address':q['address'],'query_hash':q['evidence_hash'],
                          'request_hash':page['request_evidence_hash'],'page_hash':page['payload_hash'],
                          'page_index':page_index,'record_index':record_index}
                if fingerprint in identities:
                    for event in identities[fingerprint]:
                        event['capture_locations'].append(origin)
                        output_bytes += len(canonical(origin).encode())
                    if output_bytes > MAX_OUTPUT_BYTES: raise ControlLimit('Control output ceiling')
                    continue
                identities[fingerprint] = []
                try:
                    signature = raw.get('signature') or raw['transaction']['signatures'][0]
                    if (type(signature) is not str or not 64<=len(signature)<=88
                            or len(unbase58(signature))!=64): raise ValueError('Signature')
                    if raw['transaction']['signatures'] and raw['transaction']['signatures'][0]!=signature:
                        raise ValueError('Conflicting signature')
                    slot = raw['slot']
                    if type(slot) is not int or slot < 0: raise ValueError('Slot')
                    versions.setdefault(signature,set()).add(fingerprint)
                    if len(versions[signature])>1: reasons.append('CONTROL_CONFLICTING_TRANSACTION_WITNESSES')
                    rows,keys,gaps = _instruction_rows(raw); reasons.extend(gaps)
                    result['gaps'].extend({'transaction_hash':fingerprint,'reason':reason,
                                           'capture_location':origin} for reason in gaps)
                    if raw['meta'].get('err','missing') is not None:
                        reasons.append('CONTROL_FAILED_OR_UNKNOWN_TRANSACTION_OUTCOME')
                    try: decoded = decode(raw)
                    except (ValueError,KeyError,TypeError,IndexError):
                        decoded = None; reasons.append('CONTROL_TRANSACTION_DECODING_INCOMPLETE')
                    for path,group_ordinal,inner_ordinal,ix in rows:
                        try:
                            if type(ix) is not dict or len(canonical(ix).encode()) > MAX_INSTRUCTION_BYTES:
                                raise ValueError('Instruction size or shape')
                            resolved = compiled_instruction(ix,keys) if keys is not None else ix
                            event = _operation(resolved,path,decoded)
                            if event is None: continue
                            event.update(signature=signature,slot=slot,transaction_hash=fingerprint,
                                         instruction_hash=digest(ix),resolved_instruction_hash=digest(resolved),
                                         instruction_path=path,inner_group_ordinal=group_ordinal,
                                         inner_ordinal=inner_ordinal,capture_locations=[origin],
                                         transaction_status='PROVIDER_FAILED' if raw['meta']['err'] is not None else 'PROVIDER_SUCCEEDED',
                                         individual_cpi_success_verified=False,effect_order_verified=False,
                                         lifetime_role='UNVERIFIED_INITIALIZATION_OR_REINITIALIZATION' if event['kind'].startswith('initialize') else None)
                            event['stack_height_present'] = 'stackHeight' in ix
                            height = ix.get('stackHeight')
                            event['stack_height_witness'] = height if type(height) is int and 1<=height<=16 else None
                            if height is not None and event['stack_height_witness'] is None:
                                event['unknown_reasons'].append('CONTROL_STACK_HEIGHT_INVALID')
                            if event['target_account'] not in (None,*frontier[1:]):
                                event['unknown_reasons'].append('CONTROL_TARGET_OUTSIDE_BOUND_FRONTIER')
                            if event['target_mint'] not in (None,mint):
                                event['unknown_reasons'].append('CONTROL_TARGET_MINT_MISMATCH')
                            if event['target_account'] is None and event['target_mint'] is None:
                                event['unknown_reasons'].append('CONTROL_TARGET_UNRESOLVED')
                            event['unknown_reasons'] = sorted(set(event['unknown_reasons']))
                            reasons.extend(event['unknown_reasons'])
                            output_bytes += len(canonical(event).encode())
                            if len(result['operations']) >= MAX_OPERATIONS or output_bytes > MAX_OUTPUT_BYTES:
                                raise ControlLimit('Control operation ceiling')
                            result['operations'].append(event); identities[fingerprint].append(event)
                        except ControlLimit:
                            raise
                        except (ValueError,KeyError,TypeError,IndexError):
                            reason = 'CONTROL_INSTRUCTION_ROW_UNRESOLVED'; reasons.append(reason)
                            result['gaps'].append({'transaction_hash':fingerprint,'instruction_path':path,
                                                   'inner_group_ordinal':group_ordinal,'reason':reason,'capture_location':origin})
                except ControlLimit:
                    raise
                except (ValueError,KeyError,TypeError,IndexError):
                    reason = 'CONTROL_TRANSACTION_ROW_UNRESOLVED'; reasons.append(reason)
                    result['gaps'].append({'transaction_hash':fingerprint,'reason':reason,'capture_location':origin})
                if len(result['gaps']) > MAX_OPERATIONS:
                    raise ControlLimit('Control gap ceiling')
    result.update(unique_records=len(identities),evidence_hashes=sorted(hashes),
                  observed_rows_enumerated=not reasons,unknown_reasons=sorted(set(reasons)))
    if len(canonical(result).encode()) > MAX_OUTPUT_BYTES:
        raise ControlLimit('Control output ceiling')
    return result
