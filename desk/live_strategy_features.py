"""Bounded offline PumpSwap rolling SAMPLE measurements, never entry approval.

calculate(raw_transactions, pool=..., as_of=..., provenance=...) accepts existing
getTransactionsForAddress/transactionNotification payloads, not decoded claims
or BUY_INTENT summaries. Same-pool raw quote units cancel in volume ratios;
net_buy_ratio is buy principal / total principal, NOT native-wallet net wealth.
Sparse captures never certify the full window. Optional history_pages are pairs
of actual history_request_v1 request manifests and raw responses. Exhaustion
means provider-declared pool-query coverage, never authenticated chain truth.
No storage/provider dependency.
"""
from decimal import Decimal, localcontext
import re
from .decode import decode
from .model import canonical, digest

MAX_RECORDS = 256
MAX_RECORD_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
PUMPSWAP = 'pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA'
PROVENANCES = {'SYNTHETIC_TEST_ONLY','PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE'}
UNKNOWN = {
    'flow':'FLOW_SCORE_DEFINITION_AND_MEASUREMENTS_UNAVAILABLE',
    'flow_at':'FLOW_COMPONENT_OBSERVATION_UNAVAILABLE',
    'flow_confirmed':'FLOW_ATTESTATION_UNAVAILABLE',
    'wash_score':'WASH_CLASSIFICATION_UNAVAILABLE',
    'dev_launches_7d':'CREATOR_ATTRIBUTION_AND_SEVEN_DAY_HISTORY_UNAVAILABLE',
    'fresh_wallet_ratio':'WALLET_AGE_HISTORY_UNAVAILABLE',
    'manip_safety':'MANIPULATION_MODEL_UNAVAILABLE',
    'manip_flow':'MANIPULATION_MODEL_UNAVAILABLE',
    'sol_usd':'TIMESTAMPED_SOL_USD_PRICE_UNAVAILABLE',
    'market_cap_usd':'CURRENT_SUPPLY_AND_USD_VALUATION_UNAVAILABLE',
    'reserve_sol':'CANONICAL_SOL_QUOTE_POOL_SNAPSHOT_REQUIRED',
    'reserve_tokens':'CANONICAL_POOL_SNAPSHOT_AND_MINT_DECIMALS_REQUIRED',
    'pool_fee_bps':'CURRENT_EXACT_ROUTE_FEES_UNAVAILABLE',
    'graduated_at':'VERIFIED_POOL_CREATION_TIME_UNAVAILABLE',
}
MEASURED = ('net_buy_ratio','unique_buyers_5m','volume_vs_liq','drawdown_from_high',
            'price_at','momentum_at')


def _integer(value):
    if type(value) is not int or not 0<=value<2**63:raise ValueError('Invalid chain integer')
    return value


def _positive(value):
    if type(value) is not int or not 0<value<2**64:raise ValueError('Invalid event amount')
    return Decimal(value)



def _history_window(pages,*,pool,start,end,input_hashes):
    """Replay existing history_request_v1 manifests and raw responses.

    Exhaustion is a provider-declared query boundary, NOT authenticated chain
    completeness. No caller coverage flag or normalized report is accepted.
    """
    hashes=[]; records=[]; cursor=None; seen=set(); total=0; count=0; last_slot=None
    for page in pages:
        count+=1
        if count>8:raise ValueError('History page ceiling')
        total+=len(canonical(page).encode())
        if total>MAX_TOTAL_BYTES:raise ValueError('History byte ceiling')
        if type(page) is not dict or set(page)!={'request','response'}:raise ValueError('Raw history pair required')
        request,response=page['request'],page['response']
        if type(request) is not dict or type(response) is not dict:raise ValueError('Raw request/response required')
        if (request.get('kind')!='history_request_v1' or request.get('method')!='getTransactionsForAddress'
                or request.get('response_hash')!=digest(response)):raise ValueError('History response binding mismatch')
        params=request['params']
        if type(params) is not list or len(params)!=2 or params[0]!=pool:raise ValueError('Pool history required')
        opts=params[1]
        if type(opts) is not dict:raise ValueError('Raw request options required')
        if (set(opts)-{'transactionDetails','sortOrder','limit','commitment','encoding',
                     'maxSupportedTransactionVersion','filters','paginationToken'}
                or opts.get('transactionDetails')!='full' or opts.get('sortOrder')!='asc'
                or opts.get('commitment')!='finalized' or opts.get('encoding')!='jsonParsed'
                or type(opts.get('maxSupportedTransactionVersion')) is not int or opts['maxSupportedTransactionVersion']!=1
                or type(opts.get('limit')) is not int or not 1<=opts['limit']<=100
                or opts.get('filters')!={'blockTime':{'gte':start,'lt':end+1},'status':'any','tokenAccounts':'none'}
                or opts.get('paginationToken')!=cursor):raise ValueError('Exact window/cursor request required')
        bounds=opts['filters']['blockTime']
        if any(type(bounds[k]) is not int for k in ('gte','lt')):raise ValueError('Integer window bounds required')
        rows=response['data']
        if type(rows) is not list or len(rows)>opts['limit']:raise ValueError('Invalid history page')
        for row in rows:
            observation=decode(row)
            if not start<=_integer(observation['block_time'])<=end:raise ValueError('Out-of-window history')
            slot=_integer(observation['slot'])
            if last_slot is not None and slot<last_slot:raise ValueError('History order regression')
            last_slot=slot
            records.append(digest(row))
            if len(records)>MAX_RECORDS:raise ValueError('History record ceiling')
        hashes.extend([digest(request),digest(response)])
        next_cursor=response.get('paginationToken')
        if next_cursor is not None and (not isinstance(next_cursor,str) or not next_cursor or next_cursor in seen):
            raise ValueError('History cursor cycle')
        if count>1 and cursor is None:raise ValueError('Page after exhaustion')
        if next_cursor is not None:seen.add(next_cursor)
        cursor=next_cursor
    if count==0 or cursor is not None or sorted(records)!=sorted(input_hashes):
        raise ValueError('Exhausted matched history required')
    return sorted(set(hashes))

def calculate(raw_transactions,*,pool,as_of,provenance,window_seconds=300,ttl_seconds=30,history_pages=None,token_profile_version=0):
    """Pure read-only calculation; callers must bind pool/mint/quote separately.

    Fixed five-minute window keeps unique_buyers_5m's meaning exact. Output
    values are observed-sample measurements, or MEASURED_WINDOW under an exact
    exhausted provider query. Three v1 observable proxies are window-gated:
    directional flow=100*buy/total, wallet churn=sum(2*min(buy,sell))/total,
    buyer concentration=max(wallet buy)/buy. They do not prove wash/common
    ownership and never populate legacy flow/wash/manipulation fields. Missing/invalid/conflicting/time-uncertain records withhold
    measurements rather than silently treating them as zero or safe.
    """
    from .token2022_paper import check_version
    check_version(token_profile_version)
    _integer(as_of)
    if type(window_seconds) is not int or window_seconds!=300:raise ValueError('Five-minute window required')
    if type(ttl_seconds) is not int or not 1<=ttl_seconds<=300:raise ValueError('Invalid component TTL')
    if not isinstance(pool,str) or not 1<=len(pool)<=128 or not isinstance(provenance,str) or provenance not in PROVENANCES:
        raise ValueError('Pool identity and explicit provenance required')
    start=max(0,as_of-window_seconds)
    fields={name:{'status':'UNKNOWN','value':None,'observed_at':None,'blockers':[reason]}
            for name,reason in UNKNOWN.items()}
    for name in ('directional_flow_proxy_v1','same_wallet_churn_proxy_v1','buyer_volume_concentration_proxy_v1'):
        fields[name]={'status':'UNKNOWN','value':None,'observed_at':None,'blockers':['WINDOW_COVERAGE_UNVERIFIED']}
    for name in MEASURED:
        fields[name]={'status':'UNKNOWN','value':None,'observed_at':None,'blockers':['NO_SUPPORTED_TRADES']}
    result={'schema':'rolling_trade_sample_v1','pool':pool,'provenance':provenance,
            'evaluated_at':as_of,'window':{'start_inclusive':start,'end_inclusive':as_of,
            'coverage_complete':False,'blockers':['WINDOW_COVERAGE_UNVERIFIED'],
            'observed_start':None,'observed_end':None},'fields':fields,
            'history_hashes':[],'coverage_authenticity':'UNVERIFIED_PROVIDER_DECLARATION',
            **({'reserve_basis':'EFFECTIVE_PRICING_NOT_PHYSICAL_LIQUIDITY','pool_profile_version':2} if token_profile_version==2 else {}),'proxy_version':'observable-flow-churn-concentration-v1','source_hashes':[],'records_seen':0,'duplicate_records':0,'failed_records':0,
            'trade_count':0,'provider_calls':0,'eligible_for_trading':False,
            'scope':'Observed pool sample only; event amounts are not authenticated fills or full route effects',
            'blockers':['WINDOW_COVERAGE_UNVERIFIED','POOL_MINT_QUOTE_BINDING_REQUIRED']}
    fatal=set(); trades=[]; record_times=[]; identities={}; hashes=[]; input_hashes=[]; total=0
    try:
        for payload in raw_transactions:
            result['records_seen']+=1
            if result['records_seen']>MAX_RECORDS:raise ValueError('Record ceiling')
            encoded=canonical(payload).encode();total+=len(encoded)
            if len(encoded)>MAX_RECORD_BYTES or total>MAX_TOTAL_BYTES:raise ValueError('Byte ceiling')
            observation=decode(payload)
            key=observation['signature'];hash_value=digest(payload);input_hashes.append(hash_value)
            if key in identities:
                if identities[key]!=hash_value:fatal.add('CONFLICTING_TRANSACTION_IDENTITY')
                else:result['duplicate_records']+=1
                continue
            identities[key]=hash_value;hashes.append(hash_value)
            timestamp=_integer(observation['block_time']);slot=_integer(observation['slot'])
            if timestamp>as_of:fatal.add('FUTURE_TRANSACTION_TIME');continue
            if timestamp<start:continue
            # Bank metadata constrains the window even when execution failed
            # or the record contributes no supported trade volume.
            record_times.append((slot,timestamp))
            if observation['status']=='FAILED':result['failed_records']+=1;continue
            paths=set(); record_trades=len(trades)
            for row in observation['program_observations']:
                if row.get('program')!=PUMPSWAP:continue
                if row.get('status') not in ('IDENTIFIED','EVENT_DECODED'):
                    fatal.add('UNSUPPORTED_OR_PARTIAL_EVENT');continue
                if row.get('name') not in ('BuyEvent','SellEvent'):continue
                f=row['fields']
                if f.get('pool')!=pool:continue
                if row.get('schema_complete') is not True or _integer(f['timestamp'])!=timestamp:
                    fatal.add('EVENT_CHAIN_TIME_MISMATCH');continue
                path=row['instruction']
                if path in paths:fatal.add('DUPLICATE_EVENT_PATH');continue
                paths.add(path)
                if re.fullmatch(r'\d+(?:\.\d+)?',path) is None:raise ValueError('Invalid event path')
                side='buy' if row['name']=='BuyEvent' else 'sell'
                quote=_positive(f['quote_amount_in' if side=='buy' else 'quote_amount_out'])
                base=_positive(f['base_amount_out' if side=='buy' else 'base_amount_in'])
                wallet=f['user']
                if not isinstance(wallet,str) or not 1<=len(wallet)<=128:raise ValueError('Missing wallet')
                reserve=_positive(f['pool_quote_token_reserves'])
                if token_profile_version==2:
                    virtual=f.get('virtual_quote_reserves')
                    if type(virtual) is not int or not -(2**127)<=virtual<2**127 or type(f.get('can_boost')) is not bool:raise ValueError('Missing signed pricing profile')
                    if virtual>0 and f['can_boost'] is not True:raise ValueError('Contradictory boost event')
                    reserve=_positive(int(reserve)+virtual)
                trades.append({'ts':timestamp,'slot':slot,'signature':key,
                               'path':tuple(map(int,path.split('.'))),'side':side,
                               'liquidity_supported':token_profile_version==2 or type(f.get('virtual_quote_reserves')) is int and f['virtual_quote_reserves']==0 and f.get('can_boost') is False,
                               'quote':quote,'base':base,'wallet':wallet,'reserve':reserve})
            for intent in observation['program_observations']:
                if (intent.get('program')!=PUMPSWAP or intent.get('pool')!=pool
                        or intent.get('kind') not in ('BUY_INTENT','SELL_INTENT')):continue
                path=intent['instruction']
                # Flat inner paths omit CPI ancestry. Do not guess which nested
                # invocation produced an event merely from its outer index.
                if re.fullmatch(r'\d+',path) is None:
                    fatal.add('TRADE_INVOCATION_EVENT_BINDING_UNRESOLVED');continue
                side='buy' if intent['kind']=='BUY_INTENT' else 'sell'
                matches=[trade for trade in trades[record_trades:]
                         if trade['path'][0]==int(path) and len(trade['path'])==2
                         and trade['side']==side and trade['wallet']==intent.get('wallet')]
                if not matches:fatal.add('TRADE_INTENT_WITHOUT_COMPLETE_EVENT')
                elif len(matches)!=1:fatal.add('TRADE_INVOCATION_EVENT_BINDING_UNRESOLVED')
    except (ValueError,KeyError,TypeError,OverflowError,RecursionError):
        fatal.add('MALFORMED_OR_BOUNDED_INPUT_UNAVAILABLE')
    ordered_times=sorted(record_times)
    if any(a[1]>b[1] for a,b in zip(ordered_times,ordered_times[1:]) if a[0]!=b[0]):
        fatal.add('CHAIN_TIME_SLOT_ORDER_CONFLICT')
    slot_times={}
    for slot,timestamp in record_times:slot_times.setdefault(slot,set()).add(timestamp)
    if any(len(times)>1 for times in slot_times.values()):fatal.add('SAME_SLOT_CHAIN_TIME_CONFLICT')
    coverage=False
    if history_pages is not None:
        try:
            result['history_hashes']=_history_window(history_pages,pool=pool,start=start,end=as_of,input_hashes=input_hashes)
            coverage=not (fatal-{'TRADE_COMPONENT_STALE'})
        except (ValueError,KeyError,TypeError,OverflowError,RecursionError):
            result['blockers'].append('HISTORY_WINDOW_BINDING_OR_EXHAUSTION_UNAVAILABLE')
    result['window']['coverage_complete']=coverage
    result['window']['blockers']=[] if coverage else ['WINDOW_COVERAGE_UNVERIFIED']
    if coverage:result['blockers'].remove('WINDOW_COVERAGE_UNVERIFIED')
    result['source_hashes']=sorted(hashes)
    result['trade_count']=len(trades)
    if trades:
        result['window']['observed_start']=min(t['ts'] for t in trades)
        result['window']['observed_end']=max(t['ts'] for t in trades)
        observed=max(t['ts'] for t in trades)
        if as_of-observed>ttl_seconds:fatal.add('TRADE_COMPONENT_STALE')
    if fatal-{'TRADE_COMPONENT_STALE'}:
        coverage=False
        result['window']['coverage_complete']=False
        result['window']['blockers']=['WINDOW_COVERAGE_UNVERIFIED']
        result['blockers'].append('WINDOW_COVERAGE_UNVERIFIED')
    if fatal or not trades:
        reasons=sorted(fatal or {'NO_SUPPORTED_TRADES'})
        for name in MEASURED:fields[name]['blockers']=reasons
    else:
        latest_slot=max(t['slot'] for t in trades)
        latest=[t for t in trades if t['slot']==latest_slot]
        latest_signatures={t['signature'] for t in latest}
        ordering=len(latest_signatures)==1
        last=max(latest,key=lambda t:t['path']) if ordering else None
        with localcontext() as context:
            context.prec=50
            volume=sum((t['quote'] for t in trades),Decimal(0))
            buys=sum((t['quote'] for t in trades if t['side']=='buy'),Decimal(0))
            values={'net_buy_ratio':str(buys/volume),
                    'unique_buyers_5m':len({t['wallet'] for t in trades if t['side']=='buy'}),
                    'momentum_at':observed}
            if last:
                peak=max(t['quote']/t['base'] for t in trades)
                if last['liquidity_supported']:values['volume_vs_liq']=str(volume/(2*last['reserve']))
                else:fields['volume_vs_liq']['blockers']=['SPECIAL_POOL_LIQUIDITY_PROFILE_UNSUPPORTED']
                values.update(drawdown_from_high=str(1-(last['quote']/last['base'])/peak),price_at=last['ts'])
            for name,value in values.items():
                fields[name]={'status':'MEASURED_WINDOW' if coverage else 'MEASURED_SAMPLE','value':value,'observed_at':observed,
                              'blockers':[] if coverage else ['WINDOW_COVERAGE_UNVERIFIED']}
            if not ordering:
                for name in ('drawdown_from_high','volume_vs_liq','price_at'):
                    fields[name]['blockers']=['LATEST_SLOT_ORDER_UNRESOLVED']
    if coverage and not fatal:
        with localcontext() as context:
            context.prec=50
            wallets={}
            for trade in trades:
                amounts=wallets.setdefault(trade['wallet'],{'buy':Decimal(0),'sell':Decimal(0)})
                amounts[trade['side']]+=trade['quote']
            buy=sum((v['buy'] for v in wallets.values()),Decimal(0))
            total=buy+sum((v['sell'] for v in wallets.values()),Decimal(0))
            at=max((t['ts'] for t in trades),default=None)
            values={'same_wallet_churn_proxy_v1':str(sum((2*min(v['buy'],v['sell']) for v in wallets.values()),Decimal(0))/total) if total else '0'}
            if total:values['directional_flow_proxy_v1']=str(100*buy/total)
            if buy:values['buyer_volume_concentration_proxy_v1']=str(max(v['buy'] for v in wallets.values())/buy)
            for name in ('directional_flow_proxy_v1','buyer_volume_concentration_proxy_v1'):
                if name not in values:fields[name]['blockers']=['NO_VOLUME_DENOMINATOR']
            for name,value in values.items():
                fields[name]={'status':'MEASURED_WINDOW','value':value,'observed_at':at,'blockers':[]}
            if not trades:
                fields['unique_buyers_5m']={'status':'MEASURED_WINDOW','value':0,'observed_at':None,'blockers':[]}
    elif coverage:
        for name in ('directional_flow_proxy_v1','same_wallet_churn_proxy_v1','buyer_volume_concentration_proxy_v1'):
            fields[name]['blockers']=sorted(fatal)
    result['blockers']=sorted(set(result['blockers'])|fatal|{reason for field in fields.values() for reason in field['blockers']})
    if token_profile_version==2:
        from .model import BOOST_VOLUME_FEATURE,BOOST_VOLUME_FORMULA
        fields[BOOST_VOLUME_FEATURE]=fields['volume_vs_liq']
        fields['volume_vs_liq']={'status':'UNKNOWN','value':None,'observed_at':None,
            'blockers':['HISTORICAL_PHYSICAL_FEE_BUCKETS_UNAVAILABLE']}
        result['volume_feature']=BOOST_VOLUME_FEATURE
        result['volume_formula']=BOOST_VOLUME_FORMULA
    result['manifest_hash']=digest(result)
    return result
