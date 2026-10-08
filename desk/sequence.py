"""Unsigned sequential-state RPC simulation; never a trading approval."""
import base64,hashlib,time
from .providers import helius_rpc
from .programs import address
from .security import account_bytes
from .model import digest


def account_identity(value):
    if value is None:return None
    if not isinstance(value,dict) or type(value.get('lamports')) is not int or value['lamports']<0 or type(value.get('executable')) is not bool:
        raise ValueError('Malformed sequence account state')
    return {'owner':address(value['owner']),'executable':value['executable'],'lamports':value['lamports'],
            'data_sha256':hashlib.sha256(account_bytes(value)).hexdigest()}


def simulate_sequence(transactions,watch,rpc=helius_rpc):
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    if not isinstance(transactions,list) or not 2<=len(transactions)<=5:raise ValueError('Sequence requires two to five transactions')
    if not isinstance(watch,list) or not 1<=len(watch)<=64 or len(set(watch))!=len(watch):raise ValueError('Invalid sequence account watchlist')
    for key in watch:address(key)
    encoded=[];hashes=[]
    for raw in transactions:
        if not isinstance(raw,bytes) or not 1<=len(raw)<=1232:raise ValueError('Invalid sequence transaction size')
        transaction=VersionedTransaction.from_bytes(raw)
        transaction.sanitize()
        if not transaction.signatures or any(sig!=Signature.default() for sig in transaction.signatures):
            raise ValueError('Sequence accepts null signatures only')
        encoded.append(base64.b64encode(raw).decode());hashes.append(hashlib.sha256(raw).hexdigest())
    config={'encoding':'base64','addresses':watch}
    started=time.monotonic();observed=int(time.time())
    result=rpc('simulateBundle',[{'encodedTransactions':encoded},{'transactionEncoding':'base64',
        'skipSigVerify':True,'replaceRecentBlockhash':True,'simulationBank':{'commitment':{'commitment':'confirmed'}},
        'preExecutionAccountsConfigs':[config for _ in encoded],'postExecutionAccountsConfigs':[config for _ in encoded]}])
    reasons=[];value=result.get('value',{});rows=value.get('transactionResults');slot=result.get('context',{}).get('slot')
    if type(slot) is not int or slot<0:reasons.append('SEQUENCE_SLOT_MISSING')
    if value.get('summary')!='succeeded':reasons.append('SEQUENCE_EXECUTION_FAILED')
    states=[]
    if not isinstance(rows,list) or len(rows)!=len(encoded):reasons.append('SEQUENCE_RESULT_COUNT_MISMATCH')
    else:
        for row in rows:
            if 'err' not in row or row['err'] is not None:reasons.append('SEQUENCE_TRANSACTION_FAILED')
            pair=[]
            for key in ('preExecutionAccounts','postExecutionAccounts'):
                accounts=row.get(key)
                if not isinstance(accounts,list) or len(accounts)!=len(watch):reasons.append('SEQUENCE_ACCOUNT_STATES_MISSING');pair.append(None)
                else:pair.append([account_identity(a) for a in accounts])
            states.append(pair)
        for left,right in zip(states,states[1:]):
            if left[1] is None or right[0] is None or left[1]!=right[0]:reasons.append('SEQUENCE_STATE_CONTINUITY_UNVERIFIED')
    if time.monotonic()-started>10:reasons.append('SEQUENCE_RESPONSE_STALE')
    return {'kind':'unsigned_sequence_diagnostic','passed':not reasons,'reasons':sorted(set(reasons)),
        'slot':slot,'observed_at':observed,'transaction_hashes':hashes,'watch':watch,'state_digests':digest(states),
        'result':result,'signed':False,'submitted':False,'eligible_for_trading':False,
        'notice':'Sequential state and execution diagnostic only. Buy/sell effects, all account controls and route policies still required.'}
