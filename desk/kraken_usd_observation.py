"""Kraken SOLUSD recent-trade valuation only; no network or slot assertion.

Primary public contract: https://docs.kraken.com/api-reference/market-data/get-recent-trades
Price and trade time are parsed from original bytes using Decimal, never float.
A provider timestamp and trusted host clock do not authenticate market truth.
"""
import base64
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext
import hashlib
import json
import re
from urllib.parse import urlencode

from .sol_usd_observation import _unique_object, _reject_constant, _json_decimal, _json_integer
from .model import digest

URL='https://api.kraken.com/0/public/Trades?pair=SOLUSD&count=1'
PARAMS={'pair':'SOLUSD','count':'1'}
SOURCE='kraken-solusd-recent-trades-paper-v1'
METHOD='kraken_solusd_trades_v1'
MAX_BYTES=65536


def selected(cfg):
    if 'paper_usd_valuation_version' not in cfg:return 0
    v=cfg['paper_usd_valuation_version']
    if type(v) is int and v==2:
        # Shared interface: version 2 (Jupiter primary, this module as the fallback) validates itself.
        from .usd_valuation import selected as selected_v2
        return selected_v2(cfg)
    if (type(v) is not int or v!=1 or cfg.get('mode')!='paper'
            or type(cfg.get('paper_signal_policy_version')) is not int or cfg['paper_signal_policy_version']!=3
            or type(cfg.get('paper_quote_execution_version')) is not int or cfg['paper_quote_execution_version']!=1):
        raise ValueError('Kraken valuation requires explicit paper USD1/signal3/quote1')
    return v


def timestamp(value):
    if type(value) is not str or len(value)>64 or re.fullmatch(r'(?:0|[1-9][0-9]{0,18})(?:\.[0-9]{1,18})?',value) is None:
        raise ValueError('Exact bounded host timestamp required')
    n=Decimal(value)
    if not 0<=n<2**63:raise ValueError('Host timestamp range')
    return n


@dataclass(frozen=True)
class KrakenTradesResponse:
    method:str
    url:str
    raw_payload:bytes
    acquired_at:str
    http_status:int=200


@dataclass(frozen=True)
class TrustedTimeBounds:
    now:str


@dataclass(frozen=True)
class KrakenUsdObservation:
    status:str
    usd_price:Decimal|None
    price_at:int|None
    trade_at:str|None
    acquired_at:str
    blockers:tuple
    request_sha256:str
    payload_sha256:str
    bounds:TrustedTimeBounds
    block_id:None=None
    source:str=SOURCE
    purpose:str='USD_VALUATION_ONLY'


def provider_error(raw):
    """Bounded provider-declared error; no interpretation as market evidence."""
    if type(raw) is not bytes or len(raw)>MAX_BYTES:raise ValueError('Kraken response byte bound')
    p=json.loads(raw.decode('utf-8'),object_pairs_hook=_unique_object,parse_float=_json_decimal,parse_int=_json_integer,parse_constant=_reject_constant)
    if type(p) is not dict or type(p.get('error')) is not list or len(p['error'])>32:raise ValueError('Kraken error shape')
    if any(type(e) is not str or not 1<=len(e)<=256 for e in p['error']):raise ValueError('Kraken error bound')
    return bool(p['error'])


def parse_kraken_usd(observation,*,bounds):
    # Numeric token/exponent budgets require enough precision to distinguish a
    # sub-28-digit stale/future boundary; default Decimal context would round it.
    with localcontext() as ctx:
        ctx.prec=512
        return _parse_kraken_usd(observation,bounds=bounds)


def _parse_kraken_usd(observation,*,bounds):
    if type(observation) is not KrakenTradesResponse or type(bounds) is not TrustedTimeBounds:raise ValueError('Typed Kraken clock/response required')
    if observation.method!='GET' or observation.url!=URL:raise ValueError('Exact SOLUSD Trades request required')
    if type(observation.raw_payload) is not bytes or len(observation.raw_payload)>MAX_BYTES:raise ValueError('Kraken response byte bound')
    if type(observation.http_status) is not int:raise ValueError('HTTP status required')
    now=timestamp(bounds.now);acquired=timestamp(observation.acquired_at)
    blockers=[];price=trade=None
    if not 0<=now-acquired<=10:blockers.append('ACQUISITION_STALE_OR_FUTURE')
    if observation.http_status!=200:blockers.append('HTTP_NOT_200')
    try:
        payload=json.loads(observation.raw_payload.decode('utf-8'),object_pairs_hook=_unique_object,
                           parse_float=_json_decimal,parse_int=_json_integer,parse_constant=_reject_constant)
        if type(payload) is not dict or set(payload)!={'error','result'} or type(payload['error']) is not list or payload['error']!=[]:raise ValueError('Kraken error/shape')
        result=payload['result']
        if type(result) is not dict or set(result)!={'SOLUSD','last'}:raise ValueError('Exact SOLUSD result required')
        if type(result['last']) is not str or re.fullmatch('[0-9]{1,32}',result['last']) is None:raise ValueError('Cursor shape')
        rows=result['SOLUSD']
        if type(rows) is not list or len(rows)!=1 or type(rows[0]) is not list or len(rows[0])!=7:raise ValueError('One exact recent trade required')
        p,volume,t,side,order,misc,identity=rows[0]
        if (type(p) is not str or len(p)>128 or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',p) is None
                or type(volume) is not str or len(volume)>128 or re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?',volume) is None):raise ValueError('Trade decimals')
        price=Decimal(p)
        if price<=0 or not price.is_finite() or Decimal(volume)<=0:raise ValueError('Positive trade required')
        if type(t) not in (int,Decimal) or not Decimal(t).is_finite() or not 0<Decimal(t)<2**63:raise ValueError('Trade timestamp')
        trade=Decimal(t)
        if side not in ('b','s') or order not in ('l','m') or misc!='' or type(identity) is not int or not 0<identity<2**64:raise ValueError('Trade fields')
        if not 0<=now-trade<=30 or trade>acquired:blockers.append('TRADE_STALE_OR_FUTURE')
    except (ValueError,TypeError,UnicodeError,RecursionError,DecimalException):
        blockers.append('KRAKEN_RESPONSE_INVALID')
    request=json.dumps({'method':'GET','url':URL},sort_keys=True,separators=(',',':'))
    return KrakenUsdObservation('UNKNOWN' if blockers else 'MEASURED',None if blockers else price,
        None if blockers or trade is None else int(trade),None if trade is None else format(trade,'f'),observation.acquired_at,
        tuple(blockers),hashlib.sha256(request.encode()).hexdigest(),hashlib.sha256(observation.raw_payload).hexdigest(),bounds)


def from_attempt(record,*,now,scan):
    expected={'kind','scan_id','requests_used','source_id','method','params','request_bytes_base64',
              'response_bytes_base64','observed_at','http_status','failure_code','acquired_at_decimal'}
    if type(record) is not dict or set(record) not in (expected,expected|{'monitoring_reservation'}):raise ValueError('Kraken attempt exact shape')
    if (record['kind']!='paper_read_attempt_v1' or record['scan_id']!=scan or record['source_id']!=SOURCE
            or record['method']!=METHOD or record['params']!=PARAMS or record['failure_code'] is not None
            or type(record['requests_used']) is not int or not 0<=record['requests_used']<=18
            or type(record['observed_at']) is not int or record['observed_at']!=int(timestamp(record['acquired_at_decimal']))):raise ValueError('Kraken attempt binding')
    if record['request_bytes_base64']!=base64.b64encode(urlencode(PARAMS).encode('ascii')).decode():raise ValueError('Kraken request bytes mismatch')
    wire=record['response_bytes_base64']
    if type(wire) is not str or len(wire)>4*((MAX_BYTES+2)//3):raise ValueError('Kraken wire byte bound')
    raw=base64.b64decode(wire,validate=True)
    if base64.b64encode(raw).decode()!=wire:raise ValueError('Kraken wire canonical encoding')
    response=KrakenTradesResponse('GET',URL,raw,record['acquired_at_decimal'],record['http_status'])
    parsed=parse_kraken_usd(response,bounds=TrustedTimeBounds(now))
    if parsed.status!='MEASURED':raise ValueError('Kraken valuation unavailable: '+','.join(parsed.blockers))
    return response,parsed


def evidence(record,*,now,scan):
    _,usd=from_attempt(record,now=now,scan=scan)
    return {'version':1,'source':SOURCE,'purpose':'USD_VALUATION_ONLY','scan_id':scan,'attempt_hash':digest(record),
            'attempt':record,'decision_at':now,'usd_price':str(usd.usd_price),'trade_at':usd.trade_at,
            'request_sha256':usd.request_sha256,'payload_sha256':usd.payload_sha256,
            'source_authentication':'PROVIDER_OBSERVATION_NOT_CRYPTOGRAPHIC','solana_slot_witness':None}


def validate_event(event,cfg):
    version=selected(cfg)
    value=event.get('paper_usd_valuation')
    if event.get('kind') not in ('market','quote_exit'):
        if value is not None:raise ValueError('USD valuation on unsupported event')
        return
    if not version:
        if value is not None:raise ValueError('Kraken evidence in legacy configuration')
        return
    if type(value) is not dict or type(value.get('decision_at')) is not str or int(timestamp(value['decision_at']))!=event['ts']:raise ValueError('Kraken event clock/config binding')
    rebuilt=evidence(value['attempt'],now=value['decision_at'],scan=value['scan_id'])
    if rebuilt!=value:raise ValueError('Kraken saved valuation modified')
    source=event['paper_source_evidence'] if event['kind']=='market' else event['source_evidence']
    if value['scan_id']!=source.get('scan_id'):raise ValueError('Kraken event scan binding')
    if event['kind']=='market':
        if event.get('sol_usd')!=value['usd_price'] or source.get('usd')!={k:v for k,v in value.items() if k!='attempt'}:raise ValueError('Kraken market valuation binding')
