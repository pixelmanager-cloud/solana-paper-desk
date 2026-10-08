"""Require a specific checked role for every outer/CPI instruction."""
import base64
from .instructions import SYSTEM
from .security import TOKEN_PROGRAM
from .providers import PUMPSWAP
from .dynamic_fees import fee_schema

REQUIRED=('effects','controls','debits','router','envelope','amm','recipients','fees','fee_query','fee_split','event','setup')


def check_route_coverage(inventory,checks):
    reasons=[];uncovered=[];roles=[];seen=set();fee_program=fee_schema()['address']
    missing=[name for name in REQUIRED if checks.get(name,{}).get('passed') is not True]
    if missing:reasons.append('ROUTE_COMPONENT_CHECKS_INCOMPLETE')
    if inventory.get('stack_metadata_verified') is not True:reasons.append('ROUTE_STACK_UNVERIFIED')
    def passed(name):return checks.get(name,{}).get('passed') is True
    for row in inventory.get('instructions',[]):
        path=row['instruction'];program=row['program'];role=None
        if path in seen:reasons.append('ROUTE_DUPLICATE_INSTRUCTION_PATH')
        seen.add(path)
        if row.get('stack_height')==1 and passed('envelope') and passed('router'):role='outer_envelope'
        elif program==PUMPSWAP:
            if path==checks.get('amm',{}).get('instruction') and passed('amm'):role='bound_sell'
            elif passed('event'):role='bound_sell_event'
        elif program==fee_program and passed('fee_query'):role='bound_fee_query'
        elif program in (TOKEN_PROGRAM,SYSTEM):
            raw=base64.b64decode(row['data_base64'],validate=True)
            if program==TOKEN_PROGRAM and raw and raw[0] in (3,12):
                if passed('recipients'):role='bound_token_transfer'
            elif passed('setup'):role='bound_wallet_setup'
        flags=[r for r in row.get('reasons',[]) if not (r=='UNSUPPORTED_ROUTE_PROGRAM' and program==fee_program and role=='bound_fee_query')]
        if flags:reasons.append('ROUTE_INSTRUCTION_FLAGS_UNRESOLVED');role=None
        if role is None:uncovered.append({'instruction':path,'program':program})
        else:roles.append({'instruction':path,'role':role})
    if not seen:reasons.append('ROUTE_INVENTORY_EMPTY')
    if uncovered:reasons.append('ROUTE_HAS_UNCHECKED_INSTRUCTIONS')
    # Coverage cannot turn the empirical fee split or historical evidence into
    # completed approval. This remains an explicit final blocker.
    return {'coverage_passed':not reasons,'full_route_policy_passed':False,'transaction_policy_ok':False,
        'reasons':sorted(set(reasons)),'incomplete_components':missing,'uncovered':uncovered,'roles':roles,
        'approval_blockers':['INDEPENDENT_FULL_ROUTE_VALIDATION_REQUIRED'],
        'notice':'Every instruction requires a checked role. Complete role coverage alone does not establish fresh full transaction approval.'}
