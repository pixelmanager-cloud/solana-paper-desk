"""Conservative outer/CPI instruction inventory, separate from full route approval."""
import base64
from .programs import unbase58,address
from .decode import integer
from .security import TOKEN_PROGRAM,TOKEN_2022
from .providers import PUMPSWAP

SYSTEM='11111111111111111111111111111111'
COMPUTE='ComputeBudget111111111111111111111111111111'
ATA='ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL'
JUPITER='JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4'
ALLOWED_PROGRAMS={SYSTEM,COMPUTE,ATA,TOKEN_PROGRAM,PUMPSWAP,JUPITER}
# This inventory is deliberately narrower than all legal SPL instructions.
TOKEN_OPERATIONS={1:('initialize_account',1,4),3:('transfer',9,3),9:('close_account',1,3),
    12:('transfer_checked',10,4),16:('initialize_account2',33,3),17:('sync_native',1,1),
    18:('initialize_account3',33,2),21:('get_account_data_size',1,1),22:('immutable_owner',1,1)}
DANGEROUS_TOKEN_OPS={4:'APPROVE',5:'REVOKE',6:'SET_AUTHORITY',7:'MINT_TO',8:'BURN',
    10:'FREEZE',11:'THAW',13:'APPROVE_CHECKED',14:'MINT_TO_CHECKED',15:'BURN_CHECKED'}


def parsed_instruction(program,parsed):
    kind=parsed.get('type');info=parsed.get('info')
    if not isinstance(info,dict):raise ValueError('Missing parsed instruction info')
    def u64(n):
        value=integer(n)
        if value>=2**64:raise ValueError('Instruction amount overflow')
        return value.to_bytes(8,'little')
    def keys(*names):return [address(info[n]) for n in names]
    if program in (TOKEN_PROGRAM,TOKEN_2022):
        dangerous={'approve':4,'revoke':5,'setAuthority':6,'mintTo':7,'burn':8,'freezeAccount':10,
                   'thawAccount':11,'approveChecked':13,'mintToChecked':14,'burnChecked':15}
        if kind in dangerous:return bytes([dangerous[kind]]),[]
        if kind=='transfer':return b'\x03'+u64(info['amount']),keys('source','destination','authority')
        if kind=='transferChecked':
            amount=info['tokenAmount'];decimals=integer(amount['decimals'])
            if decimals>255:raise ValueError('Invalid instruction decimals')
            return b'\x0c'+u64(amount['amount'])+bytes([decimals]),keys('source','mint','destination','authority')
        if kind=='closeAccount':return b'\x09',keys('account','destination','owner')
        if kind=='syncNative':return b'\x11',keys('account')
        if kind=='initializeImmutableOwner':return b'\x16',keys('account')
        if kind=='getAccountDataSize':
            if info.get('extensionTypes',[]) not in ([],['immutableOwner']):raise ValueError('Unsupported requested account extensions')
            return b'\x15',keys('mint')
        if kind=='initializeAccount3':return b'\x12'+unbase58(address(info['owner'])),keys('account','mint')
        if kind=='initializeAccount2':return b'\x10'+unbase58(address(info['owner'])),keys('account','mint','rentSysvar')
        if kind=='initializeAccount':return b'\x01',keys('account','mint','owner','rentSysvar')
    if program==SYSTEM:
        if kind=='transfer':return bytes([2,0,0,0])+u64(info['lamports']),keys('source','destination')
        if kind=='createAccount':
            return bytes(4)+u64(info['lamports'])+u64(info['space'])+unbase58(address(info['owner'])),keys('source','newAccount')
        # Deliberately do not normalize assign/allocate/nonce/seed variants into transfer.
    raise ValueError('Unsupported parsed instruction')


def inventory(outer,value,keys,wallet,*,compiled=None):
    reasons=[];rows=[];contexts={};program_at={}
    if not isinstance(outer,list) or not 1<=len(outer)<=64:raise ValueError('Invalid outer instruction count')
    if len(keys)!=len(set(keys)):raise ValueError('Duplicate transaction keys')
    def resolve(index):
        if type(index) is not int or not 0<=index<len(keys):raise ValueError('Invalid compiled instruction index')
        return keys[index]
    for index,ix in enumerate(outer):
        program=address(ix['programId']);accounts=[address(a['pubkey']) for a in ix['accounts']]
        if program not in keys or any(a not in keys for a in accounts):raise ValueError('Outer instruction key missing')
        path=str(index);contexts[path]={'stack_height':1,'parent_instruction':None,'parent_program':None,
            'requested_account_metas':[{k:a.get(k) for k in ('pubkey','isSigner','isWritable')} for a in ix['accounts']], 'declared_account_privileges':None};program_at[path]=program
        rows.append((path,program,base64.b64decode(ix['data'],validate=True),accounts))
    groups=value.get('innerInstructions')
    if not isinstance(groups,list):reasons.append('INNER_INSTRUCTIONS_UNAVAILABLE');groups=[]
    seen=set()
    for group in groups:
        parent=group.get('index')
        if type(parent) is not int or not 0<=parent<len(outer) or parent in seen:raise ValueError('Invalid inner instruction group')
        seen.add(parent)
        nested=group.get('instructions')
        if not isinstance(nested,list):raise ValueError('Missing inner instructions')
        stack={1:str(parent)}
        for index,ix in enumerate(nested):
            if len(rows)>=256:raise ValueError('Instruction inspection budget exceeded')
            path=f'{parent}.{index}';height=ix.get('stackHeight');parent_path=None
            if type(height) is not int or not 2<=height<=16:
                reasons.append('INSTRUCTION_STACK_METADATA_MISSING_OR_INVALID');stack={1:str(parent)}
            elif height-1 not in stack:
                reasons.append('INSTRUCTION_STACK_TRANSITION_UNVERIFIED');stack={1:str(parent)}
            else:
                parent_path=stack[height-1]
                stack={level:value for level,value in stack.items() if level<height};stack[height]=path
            contexts[path]={'stack_height':height,'parent_instruction':parent_path,'parent_program':program_at.get(parent_path),
                            'requested_account_metas':None,'declared_account_privileges':None}
            if 'programIdIndex' in ix:
                program=resolve(ix['programIdIndex']);accounts=[resolve(i) for i in ix['accounts']]
                data=unbase58(ix['data'])
            elif 'programId' in ix:
                program=address(ix['programId'])
                if program not in keys:raise ValueError('Inner program missing from transaction keys')
                if 'parsed' in ix:
                    try:data,accounts=parsed_instruction(program,ix['parsed'])
                    except (ValueError,KeyError,TypeError,OverflowError):
                        reasons.append('UNSUPPORTED_PARSED_INSTRUCTION');data=b'';accounts=[]
                else:
                    data=unbase58(ix['data']);accounts=[address(a) for a in ix['accounts']]
                if any(a not in keys for a in accounts):raise ValueError('Inner account missing from transaction keys')
            else:
                reasons.append('UNSUPPORTED_INNER_INSTRUCTION_ENCODING');continue
            program_at[path]=program
            rows.append((path,program,data,accounts))
    privilege_reasons=[];declarations=None;witness=None;loaded_status='UNAVAILABLE'
    if compiled is None:privilege_reasons.append('OUTER_MESSAGE_PRIVILEGES_UNAVAILABLE')
    else:
        try:
            from .compile import declared_message_privileges
            declarations=declared_message_privileges(compiled['raw'],compiled.get('lookup_snapshot'),outer,keys,wallet)
            if 'loadedAddresses' in value:
                if value['loadedAddresses']!=declarations['loaded_addresses']:
                    raise ValueError('Simulation loaded membership/order contradicts message')
                loaded_status='MATCHED_UNAUTHENTICATED'
            from copy import deepcopy
            witness={'unsigned_transaction':base64.b64encode(compiled['raw']).decode(),
                     'lookup_snapshot':deepcopy(compiled.get('lookup_snapshot')),'wallet':wallet}
            for item in declarations['outer']:
                contexts[item['instruction']]['declared_account_privileges']=item['account_privileges']
        except (ValueError,KeyError,TypeError,IndexError):
            declarations=None;witness=None;privilege_reasons.append('OUTER_MESSAGE_PRIVILEGES_CONTRADICTORY_OR_INVALID')
    summary=[]
    for path,program,data,accounts in rows:
        operation='opaque';flags=[]
        if program not in ALLOWED_PROGRAMS:flags.append('UNSUPPORTED_ROUTE_PROGRAM')
        if program==TOKEN_2022:flags.append('TOKEN_2022_ROUTE_NOT_ALLOWED')
        if program in (TOKEN_PROGRAM,TOKEN_2022):
            tag=data[0] if data else None
            if tag in DANGEROUS_TOKEN_OPS:
                operation=DANGEROUS_TOKEN_OPS[tag].lower();flags.append('TOKEN_'+DANGEROUS_TOKEN_OPS[tag]+'_NOT_ALLOWED')
            elif tag in TOKEN_OPERATIONS:
                operation,length,count=TOKEN_OPERATIONS[tag]
                valid_length=data in (b'\x15',b'\x15\x07\x00') if tag==21 else len(data)==length
                if not valid_length or len(accounts)<count:flags.append('TOKEN_INSTRUCTION_LAYOUT_MISMATCH')
                if tag==9 and len(accounts)>=3 and accounts[2]==wallet and accounts[1]!=wallet:
                    flags.append('TOKEN_CLOSE_TO_EXTERNAL_RECIPIENT')
            else:flags.append('UNSUPPORTED_TOKEN_INSTRUCTION')
        elif program==SYSTEM:
            tag=int.from_bytes(data[:4],'little') if len(data)>=4 else None
            if tag==0:operation='create_account';valid=len(data)==52 and len(accounts)>=2
            elif tag==2:operation='transfer';valid=len(data)==12 and len(accounts)>=2
            else:operation='unsupported_system_operation';valid=False
            if not valid:flags.append('UNSUPPORTED_SYSTEM_INSTRUCTION')
        elif program==COMPUTE:
            operation='compute_budget'
            if not data or data[0] not in (2,3) or len(data)!={2:5,3:9}.get(data[0]):flags.append('UNSUPPORTED_COMPUTE_BUDGET_INSTRUCTION')
        elif program==ATA:
            operation='associated_account'
            if data not in (b'',b'\x00',b'\x01') or len(accounts)<6:flags.append('UNSUPPORTED_ASSOCIATED_ACCOUNT_INSTRUCTION')
        reasons.extend(flags)
        summary.append({**contexts[path],'instruction':path,'program':program,'operation':operation,'accounts':accounts,'data_base64':base64.b64encode(data).decode(),'reasons':flags})
    return {'stack_metadata_verified':not any(r.startswith('INSTRUCTION_STACK_') for r in reasons),'inventory_checks_passed':not reasons,'full_route_policy_passed':False,
            'reasons':sorted(set(reasons)),'programs':sorted({r[1] for r in rows}),'instructions':summary,
            'outer_message_privileges':declarations,'outer_privilege_evidence':witness,
            'transaction_keys':list(keys),'outer_privilege_reasons':privilege_reasons,'simulation_loaded_addresses_status':loaded_status,
            'runtime_cpi_privileges_authenticated':False,'source_authenticated':False,'finality_authenticated':False,
            'notice':'Program IDs and basic token/system operations inspected. Router arguments, route account bindings and recipient/rent policy still require validation; never a trading approval.'}
