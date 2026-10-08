"""Cross-check original Pump creation facts; does not establish transfer coverage."""
from .providers import PUMP
from .programs import address
from .security import TOKEN_PROGRAM,TOKEN_2022


def launch_anchor(observation,mint):
    from solders.pubkey import Pubkey
    address(mint)
    reasons=[]
    if observation.get('status')!='OBSERVED' or observation.get('commitment')!='finalized_provider_response':
        reasons.append('LAUNCH_NOT_FINALIZED')
    instructions=[x for x in observation.get('program_observations',[]) if x.get('program')==PUMP and x.get('kind')=='LAUNCH' and x.get('status')=='IDENTIFIED' and x.get('mint')==mint]
    events=[x for x in observation.get('program_observations',[]) if x.get('program')==PUMP and x.get('name')=='CreateEvent' and x.get('fields',{}).get('mint')==mint and x.get('schema_complete') is True]
    initializations=[x for x in observation.get('mint_initializations',[]) if x.get('mint')==mint]
    result={'verified':False,'mint':mint,'signature':observation.get('signature'),'slot':observation.get('slot'),
            'transfer_history_complete':False}
    if len(instructions)!=1 or len(events)!=1 or len(initializations)!=1:
        return {**result,'reasons':sorted(set(reasons+['LAUNCH_CORROBORATION_MISSING_OR_AMBIGUOUS']))}
    ix,event,init=instructions[0],events[0],initializations[0];fields=event['fields'];accounts=ix['accounts']
    program=Pubkey.from_string(PUMP);mint_key=Pubkey.from_string(mint)
    expected_curve=str(Pubkey.find_program_address([b'bonding-curve',bytes(mint_key)],program)[0])
    expected_authority=str(Pubkey.find_program_address([b'mint-authority'],program)[0])
    if accounts.get('bonding_curve')!=expected_curve or fields.get('bonding_curve')!=expected_curve:
        reasons.append('LAUNCH_CURVE_PDA_MISMATCH')
    if accounts.get('mint_authority')!=expected_authority or init.get('mint_authority')!=expected_authority:
        reasons.append('LAUNCH_MINT_AUTHORITY_MISMATCH')
    token_program=accounts.get('token_program')
    if token_program not in (TOKEN_PROGRAM,TOKEN_2022) or init.get('program')!=token_program or fields.get('token_program',token_program)!=token_program:
        reasons.append('LAUNCH_TOKEN_PROGRAM_MISMATCH')
    if fields.get('user')!=ix.get('wallet') or not fields.get('creator'):
        reasons.append('LAUNCH_CREATOR_OR_USER_MISMATCH')
    else:address(fields['creator'])
    # Event and mint initialization must belong to the same outer instruction.
    parent=str(ix.get('instruction')).split('.')[0]
    if any(str(x.get('instruction')).split('.')[0]!=parent for x in (event,init)):
        reasons.append('LAUNCH_INSTRUCTION_SCOPE_MISMATCH')
    at=observation.get('block_time');event_at=fields.get('timestamp')
    if type(at) is not int or type(event_at) is not int or abs(at-event_at)>10:
        reasons.append('LAUNCH_TIMESTAMP_UNVERIFIED')
    if init.get('freeze_authority') is not None:reasons.append('LAUNCH_FREEZE_AUTHORITY')
    result.update({'verified':not reasons,'reasons':sorted(set(reasons)),
                   'creator':fields.get('creator'),'payer':ix.get('wallet'),'block_time':at,
                   'token_program':token_program,'bonding_curve':expected_curve,
                   'mayhem_mode':fields.get('is_mayhem_mode'),'notice':'Verified creation anchor only; ownership, funding, liquidity and transfer coverage require separate checks.'})
    return result
