"""Diagnostic checked-row coverage, never full route or source authorization.

Receipts are detached checker outputs, not signatures or trusted caller claims.
They bind role coverage to the exact inspected path/bytes/ordered accounts and
stack context; the independent complete route policy remains mandatory.
"""
import base64
from collections import Counter
from .instructions import SYSTEM,ATA,COMPUTE,JUPITER
from .security import TOKEN_PROGRAM
from .providers import PUMPSWAP
from .dynamic_fees import fee_schema

REQUIRED=('effects','controls','debits','router','envelope','amm','recipients','fees','fee_query','fee_split','event','setup')
ROW_CHECKS=('envelope','amm','recipients','fee_query','event','setup')
FIELDS=('instruction','program','data_base64','accounts','stack_height','parent_instruction','parent_program')


def instruction_receipt(row):
    """Detach only the inventory identity actually inspected by a checker."""
    if (not isinstance(row,dict) or type(row.get('accounts')) is not list
            or not all(type(a) is str for a in row['accounts'])
            or type(row.get('program')) is not str or type(row.get('data_base64')) is not str
            or type(row.get('stack_height')) is not int or not 1<=row['stack_height']<=16
            or any(row.get(k) is not None and type(row[k]) is not str
                   for k in ('instruction','parent_instruction','parent_program'))):
        raise ValueError('Malformed instruction receipt identity')
    return {name:list(row['accounts']) if name=='accounts' else row.get(name) for name in FIELDS}


def outer_receipt(ix,index=None):
    # Router checks one supplied outer instruction without knowing its index.
    # Envelope receipts supply the exact outer index; coverage requires both.
    return instruction_receipt({'instruction':str(index) if index is not None else None,
        'program':ix['programId'],'data_base64':ix['data'],
        'accounts':[a['pubkey'] for a in ix['accounts']],
        'stack_height':1,'parent_instruction':None,'parent_program':None})


def check_route_coverage(inventory,checks):
    reasons=[];uncovered=[];roles=[];fee_program=fee_schema()['address']
    missing=[name for name in REQUIRED if checks.get(name,{}).get('passed') is not True]
    if missing:reasons.append('ROUTE_COMPONENT_CHECKS_INCOMPLETE')
    if inventory.get('stack_metadata_verified') is not True:reasons.append('ROUTE_STACK_UNVERIFIED')
    def passed(name):return checks.get(name,{}).get('passed') is True
    rows=inventory.get('instructions',[])
    if not isinstance(rows,list) or len(rows)>256:
        rows=[];reasons.append('ROUTE_INVENTORY_MALFORMED_OR_EXCESSIVE')
    paths=Counter(r.get('instruction') for r in rows if isinstance(r,dict) and isinstance(r.get('instruction'),str))
    if any(n>1 for n in paths.values()):reasons.append('ROUTE_DUPLICATE_INSTRUCTION_PATH')
    receipts={name:checks.get(name,{}).get('checked_instructions') for name in ROW_CHECKS}
    unavailable=[name for name in ROW_CHECKS if passed(name) and
                 (not isinstance(receipts[name],list) or len(receipts[name])>256)]
    router=checks.get('router',{}).get('checked_instruction')
    if passed('router') and (not isinstance(router,dict) or set(router)!=set(FIELDS) or router.get('instruction') is not None):
        unavailable.append('router')
    if unavailable:reasons.append('ROUTE_CHECKER_RECEIPTS_UNAVAILABLE')
    def bound(name,identity):
        candidates=receipts.get(name)
        if not passed(name) or not isinstance(candidates,list) or len(candidates)>256:return False
        # Exactly one receipt for this path: no partial or ambiguous witness.
        matches=[r for r in candidates if isinstance(r,dict) and r.get('instruction')==identity['instruction']]
        return len(matches)==1 and set(matches[0])==set(FIELDS) and instruction_receipt(matches[0])==identity
    def router_bound(identity):
        expected={**identity,'instruction':None}
        return passed('router') and isinstance(router,dict) and set(router)==set(FIELDS) and instruction_receipt(router)==expected and sum(
            isinstance(r,dict) and r.get('stack_height')==1 and
            {**instruction_receipt(r),'instruction':None}==expected for r in rows)==1
    for row in rows:
        if not isinstance(row,dict):reasons.append('ROUTE_INVENTORY_MALFORMED_OR_EXCESSIVE');continue
        path=row.get('instruction');program=row.get('program');role=None
        try:
            identity=instruction_receipt(row)
            if not isinstance(path,str) or not path or paths[path]!=1:raise ValueError('Invalid or duplicate path')
            if not isinstance(identity['accounts'],list) or not all(isinstance(a,str) for a in identity['accounts']):raise ValueError('Invalid accounts')
            raw=base64.b64decode(row['data_base64'],validate=True)
            if row.get('stack_height')==1:
                if program in (COMPUTE,ATA,JUPITER,TOKEN_PROGRAM) and bound('envelope',identity):
                    if program!=JUPITER or router_bound(identity):role='outer_envelope'
            elif program==PUMPSWAP:
                if path==checks.get('amm',{}).get('instruction') and bound('amm',identity):role='bound_sell'
                elif bound('event',identity):role='bound_sell_event'
            elif program==fee_program and bound('fee_query',identity):role='bound_fee_query'
            elif program in (TOKEN_PROGRAM,SYSTEM):
                if program==TOKEN_PROGRAM and raw and raw[0] in (3,12):
                    if bound('recipients',identity):role='bound_token_transfer'
                elif bound('setup',identity):role='bound_wallet_setup'
        except (ValueError,KeyError,TypeError):
            reasons.append('ROUTE_INSTRUCTION_IDENTITY_MALFORMED');role=None
        flags=row.get('reasons',[])
        if not isinstance(flags,list) or not all(isinstance(r,str) for r in flags):
            flags=['MALFORMED_INSTRUCTION_FLAGS']
        flags=[r for r in flags if not (r=='UNSUPPORTED_ROUTE_PROGRAM' and program==fee_program and role=='bound_fee_query')]
        if flags:reasons.append('ROUTE_INSTRUCTION_FLAGS_UNRESOLVED');role=None
        if role is None:uncovered.append({'instruction':path,'program':program})
        else:roles.append({'instruction':path,'role':role})
    if not rows:reasons.append('ROUTE_INVENTORY_EMPTY')
    if uncovered:reasons.append('ROUTE_HAS_UNCHECKED_INSTRUCTIONS')
    return {'coverage_passed':not reasons,'full_route_policy_passed':False,'transaction_policy_ok':False,
        'reasons':sorted(set(reasons)),'incomplete_components':missing,'unavailable_bindings':sorted(set(unavailable)),
        'uncovered':uncovered,'roles':roles,'approval_blockers':['INDEPENDENT_FULL_ROUTE_VALIDATION_REQUIRED'],
        'notice':'Exact checked-row coverage is diagnostic consistency, not receipt authentication or fresh full transaction approval.'}
