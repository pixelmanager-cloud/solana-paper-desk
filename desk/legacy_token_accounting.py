"""Exact legacy SPL accounting syntax; never CPI execution or authority proof.

Historical official semantics pinned to solana-labs/solana-program-library
ad2b81274075c45e6ef428e52479b7d3d8f0dd6a:
 token/program/src/instruction.rs blob e798abdea4dc930354b970dd3dc36f098d4bb4d3
 token/program/src/processor.rs blob 7056f2e707ed93282d19ec56e9711d22a24b7498
These pins do not establish deployed code or successful per-CPI execution.
Canonical byte/account profiles are intentionally narrower than permissive
upstream unpacking. No multisig suffix or unknown trailing data is accepted.
"""
from .programs import address, unbase58
from .security import TOKEN_PROGRAM, TOKEN_2022, base58

RENT = 'SysvarRent111111111111111111111111111111111'
KINDS = {0:'initializeMint',1:'initializeAccount',3:'transfer',7:'mintTo',8:'burn',
         9:'closeAccount',12:'transferChecked',14:'mintToChecked',15:'burnChecked',
         16:'initializeAccount2',18:'initializeAccount3',20:'initializeMint2'}
CONTROLS = {2:'initializeMultisig',4:'approve',5:'revoke',6:'setAuthority',
            10:'freezeAccount',11:'thawAccount',13:'approveChecked',19:'initializeMultisig2',
            21:'getAccountDataSize',22:'initializeImmutableOwner'}
COUNTS = {0:2,1:4,3:3,7:3,8:3,9:3,12:4,14:3,15:3,16:3,18:2,20:1}


def accounting(ix):
    """Return syntax status and one normalized accounting row, or unresolved tag.

    The caller retains UNDECODED_TOKEN_INSTRUCTION even for normalized syntax:
    existing lifetime/movement gates cannot mistake this for execution proof.
    """
    program=ix.get('programId')
    if program not in (TOKEN_PROGRAM,TOKEN_2022):return None
    try:raw=unbase58(ix.get('data',''))
    except ValueError:return {'status':'MALFORMED','type':'unknownRawTokenInstruction','tag':None}
    tag=raw[0] if raw else None
    kind=KINDS.get(tag,CONTROLS.get(tag,'unknownRawTokenInstruction'))
    result={'status':'UNRESOLVED','type':kind,'tag':tag}
    if program==TOKEN_2022:
        return {**result,'reason':'TOKEN_2022_RAW_UNSUPPORTED'}
    if tag not in KINDS:return result
    try:
        accounts=ix.get('accounts')
        if not isinstance(accounts,list) or len(accounts)!=COUNTS[tag]:raise ValueError('Account layout')
        keys=[address(k) for k in accounts]
        row={}
        if tag in (0,20):
            if len(raw)<35 or raw[34] not in (0,1) or len(raw)!=(35 if raw[34]==0 else 67):raise ValueError('Mint layout')
            if tag==0 and keys[1]!=RENT:raise ValueError('Rent binding')
            field='mint_initializations'
            row={'mint':keys[0],'decimals':raw[1],'mint_authority':base58(raw[2:34]),
                 'freeze_authority':base58(raw[35:67]) if raw[34] else None}
        elif tag in (1,16,18):
            if len(raw)!=(1 if tag==1 else 33):raise ValueError('Account initialization layout')
            if tag in (1,16) and keys[-1]!=RENT:raise ValueError('Rent binding')
            field='token_account_initializations'
            row={'account':keys[0],'mint':keys[1],'owner':keys[2] if tag==1 else base58(raw[1:33])}
        elif tag==9:
            if len(raw)!=1:raise ValueError('Close layout')
            field='token_account_closures'
            # Third account is declared close authority, NOT proven account owner.
            row={'account':keys[0],'destination':keys[1],'owner':None,'authority_account':keys[2]}
        else:
            checked=tag in (12,14,15)
            if len(raw)!=(10 if checked else 9):raise ValueError('Amount layout')
            amount=str(int.from_bytes(raw[1:9],'little'))
            if tag in (3,12):
                field='transfers'
                row={'asset':'TOKEN','source':keys[0],'destination':keys[2] if checked else keys[1],
                     'mint':keys[1] if checked else None,'amount_raw':amount,'endpoints_are_token_accounts':True}
            else:
                field='token_supply_changes'
                row={'mint':keys[1] if tag in (8,15) else keys[0],
                     'account':keys[0] if tag in (8,15) else keys[1],
                     'amount_raw':amount,'direction':'burn' if tag in (8,15) else 'mint'}
            if checked:row['decimals']=raw[9]
        return {**result,'status':'ACCOUNTING_SYNTAX','field':field,'row':row}
    except (ValueError,TypeError,KeyError,IndexError):return {**result,'status':'MALFORMED'}


def bind_balances(decoded, balances):
    """Reject contradictory available identity/decimals without filling gaps."""
    row=decoded['row'];field=decoded['field'];mint=row.get('mint')
    targets=([row['source'],row['destination']] if field=='transfers' else
             [row['account']] if 'account' in row else [])
    for balance in balances:
        if balance['account'] not in targets:continue
        if mint is not None and balance['mint']!=mint:raise ValueError('Raw token mint binding mismatch')
        if balance.get('program') not in (None,TOKEN_PROGRAM):raise ValueError('Raw token program binding mismatch')
        if 'decimals' in row and balance['decimals']!=row['decimals']:raise ValueError('Raw token decimals binding mismatch')
    if field=='mint_initializations':
        for balance in balances:
            if balance['mint']==mint and (balance['decimals']!=row['decimals'] or balance.get('program') not in (None,TOKEN_PROGRAM)):
                raise ValueError('Raw mint initialization balance binding mismatch')
