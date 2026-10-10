"""Read-only coordinator producer; no provider, eligibility or fill invocation.

build_market_event consumes actual PR120 TargetObservation, replays PR107 raw
observations and retained collector envelopes, PR119 window trades, PR122 USD.
Context and evidence loader are trusted coordinator injections, never candidate
JSON. Source IDs and hashes bind local records, not provider authentication.
Numeric pool fee is an explicit paper-model assumption supplied by worker06;
quote execution owns all quantity-specific modeled fees/slippage/fills.
"""
from dataclasses import dataclass
from decimal import localcontext
import json

from .live_observation import ProviderObservation, ingest_mint, ingest_pool, ingest_quote
from .live_strategy_features import calculate
from .model import (canonical, digest, decimal, validate_event, PAPER_EXPERIMENTAL,
                    EXPERIMENTAL_HISTORY_FIELDS, OWNERSHIP_HISTORY_RISK,
                    OBSERVABLE_SIGNAL_PROFILE, OBSERVABLE_FORMULAS, OBSERVABLE_LIMITATIONS,
                    BOOST_SIGNAL_PROFILE,BOOST_VOLUME_FEATURE,BOOST_VOLUME_FORMULA)
from .paper_observation_collector import TargetObservation, ObservationTarget
from .sol_usd_observation import parse_sol_usd
from .security import mint_policy

OBSERVABLE_FLOW_CHURN_CONCENTRATION_V1 = OBSERVABLE_SIGNAL_PROFILE


@dataclass(frozen=True)
class MarketContext:
    now: int
    target: ObservationTarget  # existing coordinator target, not submitted JSON
    rpc_source_id: str
    quote_source_id: str
    provenance: str
    graduated_at: int | None  # actual chain graduation, never receive time
    holder_at: int | None     # null is explicitly omitted freshness, never now
    known_hazards: tuple[str, ...]  # coordinator's existing diagnostics
    pool_fee_bps: str | None  # explicit model assumption, not an observed fee proof
    history_as_of: int | None = None  # original query end, independent of decision now
    token_profile_version: int = 0
    usd_valuation_version: int = 0


def build_market_event(collected, *, context, load_evidence, raw_trades=(),
                       history_pages=None, usd_response=None, usd_bounds=None,
                       strategy_profile=None, usd_attempt=None):
    """Return event or diagnostic draft; quote objects stay with execution owner.

    Successful output is an experimental signal event, never engine admission.
    Known hazards reject even when ownership history is unknown. Raw collector
    evidence is loaded by exact existing references, bounded and checksum checked.
    No new DB, receipt, screening or provider architecture is introduced.
    """
    if (type(context) is not MarketContext or type(context.now) is not int or context.now<0
            or not callable(load_evidence) or type(context.known_hazards) is not tuple
            or len(context.known_hazards)>32 or any(type(x) is not str or not 1<=len(x)<=128 for x in context.known_hazards)
            or context.provenance not in ('SYNTHETIC_TEST_ONLY','PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE')):
        raise ValueError('Trusted bounded coordinator context required')
    out={'kind':'paper_market_adapter_v1','event':None,'draft':{},'blockers':[],
         'entry_authorized':False,'execution_verified':False,'evidence_refs':[],
         'risk_flags':['EXECUTION_UNVERIFIED',OWNERSHIP_HISTORY_RISK],
         'holder_freshness':'UNKNOWN_OMITTED' if context.holder_at is None else 'OBSERVED'}
    blockers=set(context.known_hazards)
    if strategy_profile!=OBSERVABLE_SIGNAL_PROFILE:blockers.add('EXPLICIT_OBSERVABLE_SIGNAL_PROFILE_REQUIRED')
    if type(collected) is not TargetObservation:
        out['blockers']=['COLLECTOR_TARGET_OBSERVATION_REQUIRED'];return out
    target=collected.target
    if type(context.target) is not ObservationTarget or target!=context.target:
        out['blockers']=['COORDINATOR_TARGET_BINDING_MISMATCH'];return out
    if collected.failure:blockers.add('COLLECTOR_OBSERVATION_REJECTED')
    try:
        target,mint,pool,quote,mint_raw,atomic_supply,records=_replay_collected(collected,context,load_evidence)
        out['evidence_refs']=sorted(records)
    except (ValueError,TypeError,KeyError,AttributeError,IndexError,OSError,RecursionError):
        out['blockers']=sorted(blockers|{'COLLECTOR_SOURCE_BINDING_OR_CONTENT_INVALID'});return out
    from .streaming_history import RetainedHistoryPages,RetainedHistoryRecords
    streaming=(type(history_pages) is RetainedHistoryPages and type(raw_trades) is RetainedHistoryRecords and raw_trades.pages is history_pages)
    if not streaming and (type(raw_trades) not in (tuple,list) or len(raw_trades)>256):
        out['blockers']=sorted(blockers|{'BOUNDED_RAW_TRADE_SEQUENCE_REQUIRED'});return out
    history_as_of=context.now if context.history_as_of is None else context.history_as_of
    if type(history_as_of) is not int or not 0<=context.now-history_as_of<=30:
        out['blockers']=sorted(blockers|{'CAPTURED_HISTORY_WINDOW_STALE_OR_FUTURE'});return out
    features=calculate(raw_trades,pool=target.pool,as_of=history_as_of,
                       provenance=context.provenance,history_pages=history_pages,token_profile_version=context.token_profile_version,semantics_version=2 if streaming else 1)
    volume_name=BOOST_VOLUME_FEATURE if context.token_profile_version==2 else 'volume_vs_liq'
    names=set(OBSERVABLE_FORMULAS)|{'net_buy_ratio','unique_buyers_5m',volume_name,'drawdown_from_high'}
    measurements={name:features['fields'][name] for name in sorted(names)}
    for name,row in measurements.items():
        if row['status']!='MEASURED_WINDOW':blockers.add('MISSING_WINDOW_MEASUREMENT:'+name)
        elif type(row['observed_at']) is not int or not 0<=context.now-row['observed_at']<=30:
            blockers.add('STALE_WINDOW_MEASUREMENT:'+name)
    profile={'name':OBSERVABLE_SIGNAL_PROFILE,'version':1,'formulas':dict(OBSERVABLE_FORMULAS),
             'limitations':list(OBSERVABLE_LIMITATIONS),'measurements':measurements,
             'window':features['window'],'feature_manifest_hash':features['manifest_hash'],
             'source_hashes':features['source_hashes'],'history_hashes':features['history_hashes'],
             'holder_freshness':out['holder_freshness'],
             'window_identity':{'captured_as_of':history_as_of,'decision_at':context.now,
                 'gap_seconds':context.now-history_as_of,
                 'scope':'CAPTURED_WINDOW_NOT_CONTINUOUS_COVERAGE'}}
    usd=None
    if usd_response is None or usd_bounds is None:blockers.add('SOL_USD_SOURCE_OR_EXACT_BLOCK_TIME_MISSING')
    else:
        try:
            if context.usd_valuation_version==1:
                from .kraken_usd_observation import parse_kraken_usd,timestamp,from_attempt
                if int(timestamp(usd_bounds.now))!=context.now:raise ValueError('clock mismatch')
                response,usd=from_attempt(usd_attempt,now=usd_bounds.now,scan=target.scan_id)
                if response!=usd_response:raise ValueError('Kraken original response mismatch')
            elif context.usd_valuation_version==0:
                if usd_bounds.now!=context.now:raise ValueError('clock mismatch')
                usd=parse_sol_usd(usd_response,bounds=usd_bounds)
            else:raise ValueError('USD version invalid')
            if usd.status!='MEASURED':blockers.update('SOL_USD:'+r for r in usd.blockers)
        except (ValueError,AttributeError):blockers.add('SOL_USD_TRUSTED_INPUT_INVALID')
    for name,at in (('graduated_at',context.graduated_at),('holder_at',context.holder_at)):
        if at is None and name=='holder_at':continue
        if type(at) is not int or not 0<=at<=context.now:blockers.add('ORIGINAL_'+name.upper()+'_MISSING_OR_INVALID')
    fee=None
    try:
        if context.pool_fee_bps is None:raise ValueError('missing fee assumption')
        fee=decimal(context.pool_fee_bps)
        if not 0<=fee<10000:raise ValueError('invalid fee assumption')
    except ValueError:blockers.add('EXPLICIT_PAPER_FEE_ASSUMPTION_REQUIRED')
    e={'schema_version':1,'kind':'market','ts':context.now,'mint':target.mint,'pool':target.pool,'taker':target.taker,
       # Detached original account already replayed and bound to retained RPC
       # envelope above; these bytes, not normalized PASS flags, feed entry policy.
       'token_evidence':{'mint':target.mint,'observed_at':mint.source.observed_at,
          'account':mint_raw['account'],'slot':mint.slot,'source_id':mint.source.source_id,
          'source_hash':mint.source.raw_hash,'original_json':mint.source.original_json},
       'venue':'pumpswap','provenance':context.provenance,'graduated':True,
       'mint_revoked':True,'freeze_revoked':True,'lp_verified':True,'extensions_safe':True,
       'data_healthy':not blockers,'flow_confirmed':False,'danger':bool(context.known_hazards),
       'route_available':True,'graduated_at':context.graduated_at,'holder_at':context.holder_at,
       'flow_at':measurements['directional_flow_proxy_v1']['observed_at'],
       'momentum_at':features['fields']['momentum_at']['observed_at'],
       'price_at':min(pool.source.observed_at,quote.source.observed_at,usd.price_at) if usd and usd.price_at is not None else None,
       'reserve_sol':str(pool.reserve_sol),'reserve_tokens':str(pool.reserve_tokens),
       'sol_usd':str(usd.usd_price) if usd and usd.usd_price is not None else None,
       'market_cap_usd':None,'pool_fee_bps':str(fee) if fee is not None else None,
       'flow':None,'wash_score':None,'manip_flow':None,'paper_signal_profile':profile,
       'paper_experimental':{'mode':PAPER_EXPERIMENTAL,'policy_version':3,
          'risk_flag':OWNERSHIP_HISTORY_RISK,'ownership_unknowns':{}},
       'paper_source_evidence':{'scan_id':target.scan_id,'collector_refs':out['evidence_refs'],
          'mint_hash':mint.source.raw_hash,'pool_hash':pool.source.raw_hash,'quote_hash':quote.source.raw_hash,
          'mint_at':mint.source.observed_at,'pool_at':pool.source.observed_at,'quote_at':quote.source.observed_at,
          'mint_slot':mint.slot,'pool_slot':pool.slot,'valuation_supply_raw':str(atomic_supply),
          'valuation_supply_slot':pool.slot,'valuation_supply_at':pool.source.observed_at,'source_authentication':'COORDINATOR_ASSERTED_NOT_CRYPTOGRAPHIC'},
       'paper_quote':{'direction':quote.direction,'input_raw':quote.input_raw,
          'estimated_output_raw':quote.estimated_output_raw,'minimum_output_raw':quote.minimum_output_raw,
          'source_hash':quote.source.raw_hash,'observed_at':quote.source.observed_at,
          'execution_status':'EXECUTION_UNVERIFIED'},
       'paper_fee_assumption':{'pool_fee_bps':str(fee) if fee is not None else None,'status':'MODEL_ASSUMPTION_NOT_FEE_PROOF'},
       'risk_flags':sorted(set(out['risk_flags'])|{'OBSERVABLE_PROXIES_NOT_SAFETY_PROOF'})}
    for name in EXPERIMENTAL_HISTORY_FIELDS:
        e[name]=None
        e['paper_experimental']['ownership_unknowns'][name]={'status':'UNKNOWN','reasons':['CURRENT_OWNERSHIP_HISTORY_MEASUREMENT_UNAVAILABLE']}
    for name in ('net_buy_ratio','unique_buyers_5m',volume_name,'drawdown_from_high'):e[name]=measurements[name]['value']
    if usd and usd.usd_price is not None:
        with localcontext() as ctx:
            ctx.prec=100
            e['market_cap_usd']=str((decimal(atomic_supply)/(10**mint.decimals))*pool.spot_sol_per_token*usd.usd_price)
        if context.usd_valuation_version==1:
            from .kraken_usd_observation import evidence as usd_evidence
            e['paper_usd_valuation']=usd_evidence(usd_attempt,now=usd_bounds.now,scan=target.scan_id)
            e['paper_source_evidence']['usd']={k:v for k,v in e['paper_usd_valuation'].items() if k!='attempt'}
        else:e['paper_source_evidence']['usd']={'request_sha256':usd.request_sha256,'payload_sha256':usd.payload_sha256,
            'acquired_at':usd.acquired_at,'price_at':usd.price_at,'block_id':usd.block_id,
            'valuation_basis':'CURRENT_RESERVE_SPOT_TIMES_SUPPLY_NOT_EXECUTABLE_PRICE',
            'trusted_slot_bounds':{'now':usd.bounds.now,'observed_at':usd.bounds.observed_at,
                'min_slot':usd.bounds.min_slot,'max_slot':usd.bounds.max_slot,'block_times':list(usd.bounds.block_times)}}
    if context.token_profile_version==2:
        e['volume_vs_liq']=None
        profile.update(name=BOOST_SIGNAL_PROFILE,version=2,volume_feature=BOOST_VOLUME_FEATURE,volume_formula=BOOST_VOLUME_FORMULA)
        profile['volume_reserve_basis']='EFFECTIVE_PRICING_NOT_PHYSICAL_LIQUIDITY'
        e['paper_pool_evidence']=pool_evidence(pool)
    e['event_id']='paper-market:'+digest(e)
    out['draft']=e
    # Match the existing journal reader ceiling; retain original evidence in its
    # store and diagnostic draft, but never publish an unreadable ledger event.
    if len(canonical(e).encode())>256*1024:
        blockers.add('PAPER_EVENT_BYTE_LIMIT_EXCEEDED')
    if not blockers:
        try:validate_event(e,mode=PAPER_EXPERIMENTAL,policy_version=3,token_profile_version=context.token_profile_version)
        except (ValueError,TypeError):blockers.add('EXPERIMENTAL_EVENT_CONTRACT_INVALID')
    if not blockers:out['event']=e
    out['blockers']=sorted(blockers)
    return out


def _replay_collected(collected, context, load_evidence):
    """Shared original collector envelope replay; no event or permission flags."""
    target=collected.target
    if collected.failure or any(x is None for x in (collected.mint,collected.pool,collected.quote)):
        raise ValueError('missing collection')
    if not 1<=len(collected.evidence_refs)<=20:raise ValueError('reference bound')
    records={};total=0
    for key in collected.evidence_refs:
        if type(key) is not str or len(key)!=64:raise ValueError('bad reference')
        try:record=load_evidence(key)
        except Exception:raise ValueError('retained evidence unavailable') from None
        encoded=canonical(record).encode();total+=len(encoded)
        if len(encoded)>2*1024*1024 or total>8*1024*1024 or digest(record)!=key:raise ValueError('record mismatch')
        records[key]=record
    def original(source,identity):
        if (source.source_id!=identity or len(source.original_json.encode())>2*1024*1024
                or not 0<=context.now-source.observed_at<=10):raise ValueError('source/time mismatch')
        payload=json.loads(source.original_json)
        if digest(payload)!=source.raw_hash:raise ValueError('source checksum')
        return payload
    mint_raw=original(collected.mint.source,context.rpc_source_id)
    pool_raw=original(collected.pool.source,context.rpc_source_id)
    quote_raw=original(collected.quote.source,context.quote_source_id)
    attempt_key=quote_raw.get('evidence_hash')
    if 'evidence_hash' in quote_raw:
        from . import paper_terminal_reconciliation as terminal
        import base64
        from urllib.parse import urlencode,parse_qsl
        if type(attempt_key) is not str or len(attempt_key)!=64:raise ValueError('quote attempt reference')
        attempt=load_evidence(attempt_key)
        encoded=attempt.get('request_bytes_base64') if type(attempt) is dict else None
        if type(encoded) is not str or len(encoded)>4*((8192+2)//3):raise ValueError('quote request byte bound')
        request_bytes=base64.b64decode(encoded,validate=True)
        if not request_bytes or len(request_bytes)>8192 or base64.b64encode(request_bytes).decode()!=encoded:
            raise ValueError('quote request bytes malformed')
        pairs=parse_qsl(request_bytes.decode('ascii'),strict_parsing=True)
        if len(pairs)!=len(dict(pairs)) or urlencode(pairs).encode('ascii')!=request_bytes:
            raise ValueError('quote request query ambiguous')
        if (type(attempt) is not dict or len(canonical(attempt).encode())>3*1024*1024
                or digest(attempt)!=attempt_key or attempt.get('kind')!='paper_read_attempt_v1'
                or attempt.get('source_id')!=context.quote_source_id or attempt.get('scan_id')!=target.scan_id
                or attempt.get('method')!='jupiter_probe' or attempt.get('params')!=quote_raw['request']
                or type(attempt.get('http_status')) is not int or attempt['http_status']!=200
                or attempt.get('failure_code') is not None
                or type(attempt.get('observed_at')) is not int or attempt['observed_at']!=collected.quote.source.observed_at
                or dict(pairs)!=quote_raw['request']
                or terminal._wire(attempt['response_bytes_base64'],2*1024*1024)!=quote_raw['response']):
            raise ValueError('quote transport attempt binding')
        total+=len(canonical(attempt).encode())
        if total>8*1024*1024:raise ValueError('record mismatch')
        records[attempt_key]=attempt
    mint_envelope=mint_raw['original_rpc_observation']
    if (mint_envelope not in records.values() or mint_envelope['source_id']!=context.rpc_source_id
            or mint_envelope['params']!=[target.mint,{'encoding':'base64','commitment':'confirmed'}]
            or mint_envelope['method']!='getAccountInfo'
            or mint_envelope['acquired_at']!=collected.mint.source.observed_at
            or mint_raw['account']!=mint_envelope['result']['value']
            or mint_raw['slot']!=mint_envelope['result']['context']['slot']):raise ValueError('mint binding')
    if pool_raw not in records.values():raise ValueError('pool capture missing')
    if not any(r.get('method')=='getMultipleAccounts' and r.get('source_id')==context.rpc_source_id
               and r.get('params')==pool_raw['params'] and r.get('result')==pool_raw['result']
               and r.get('acquired_at')==collected.pool.source.observed_at for r in records.values()):
        raise ValueError('pool envelope binding')
    def quote_envelope(r):
        # Transport acquisition precedes collector completion/persistence. A
        # second boundary between them must not rebase the original quote time.
        started,completed=r.get('started_at'),r.get('acquired_at')
        return (r.get('method')=='jupiter_probe' and r.get('source_id')==context.quote_source_id
                and r.get('params')==[quote_raw['request']['inputMint'],quote_raw['request']['outputMint'],target.amount_raw,target.taker]
                and r.get('result')==quote_raw and type(started) is int and type(completed) is int
                and 0<=started<=collected.quote.source.observed_at<=completed<=context.now
                and completed-started<=10 and context.now-completed<=10
                and (attempt_key is not None or completed==collected.quote.source.observed_at))
    if not any(quote_envelope(r) for r in records.values()):
        raise ValueError('quote envelope binding')
    def reader(source,payload):return lambda:ProviderObservation(source.source_id,source.observed_at,payload)
    atomic_account=pool_raw['result']['value'][pool_raw['params'][0].index(target.mint)]
    if mint_raw['account']['owner']!=atomic_account['owner']:
        raise ValueError('Separate mint and atomic pool mint program owners disagree')
    mint=ingest_mint(reader(collected.mint.source,mint_raw),mint=target.mint,now=context.now,token_profile_version=context.token_profile_version)
    pool=ingest_pool(reader(collected.pool.source,pool_raw),mint=target.mint,pool=target.pool,now=context.now,token_profile_version=context.token_profile_version)
    quote=ingest_quote(reader(collected.quote.source,quote_raw),mint=mint,direction=collected.direction,
                       amount_raw=target.amount_raw,taker=target.taker,expected_pool=target.pool,now=context.now)
    if (mint!=collected.mint or pool!=collected.pool or quote!=collected.quote
            or pool.decimals!=mint.decimals or pool.slot<mint.slot):raise ValueError('typed normalized mutation')
    atomic_mint=mint_policy(pool_raw['result']['value'][pool_raw['params'][0].index(target.mint)],mint=target.mint,token_profile_version=context.token_profile_version)
    atomic_supply=int(atomic_mint['supply_raw'])
    if atomic_mint['decision']!='PASS_TOKEN_POLICY' or atomic_mint['decimals']!=mint.decimals or atomic_supply<=0:
        raise ValueError('atomic supply binding')
    return target,mint,pool,quote,mint_raw,atomic_supply,records


def pool_evidence(pool):
    return {'source_id':pool.source.source_id,'observed_at':pool.source.observed_at,
            'raw_hash':pool.source.raw_hash,'original_json':pool.source.original_json}
