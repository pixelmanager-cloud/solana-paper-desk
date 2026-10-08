"""Disconnected bounded typed-chain dispatch; no DB, migration, issuer or RPC.

Inputs are immutable snapshots, NOT authenticated coordinator boundaries. Future
callers must supply one guarded protected read and independently reviewed pins.
Integrity/capability syntax never authenticates source, journal, state or finality.
"""
from dataclasses import dataclass
import json

from .model import canonical, digest
from .pool_receipt_ledger import ApprovedSource, CoordinatorBoundary, LedgerError, _decode
from .pool_vault_admission import GENESIS, NETWORK, PROFILE
from .programs import address

PARENT_PROFILE = 'pumpswap-common-bank-parent-point-v2'
FORMAT_PROFILE = 'pool-receipt-ledger-format-v2'
TRANSITION_SENTINEL = '!transition!'
UNKNOWN_POOL = '!unknown-pool!'
UNKNOWN_MINT = '!unknown-mint!'
UNKNOWN_SLOT = '!unknown-slot!'
MAX_RECORDS = 10000
MAX_BYTES = 8192
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_CAPABILITIES = 256
MAX_SCOPED_RECORDS = 256
MAX_REFERENCES = 128
ROW_FIELDS = ('seq','publication_id','pool','mint','slot','source_id','payload',
              'payload_hash','previous_hash','row_hash')
BINDING = {'capture_id','scan_id','descriptor_hash','source_hash','seal_hash',
           'revision_hash','budget_hash','journal_hash'}
SOURCE = {'source_id','source_kind','network','genesis_hash'}
OBSERVATION = SOURCE | BINDING | {'pool','mint','slot','snapshot_time','captured_at','manifest_hash'}
MARKER = SOURCE | BINDING | {'pool','mint','slot','status','evidence_hashes'}
CAPABILITY = SOURCE | {'version','profile','issuance_enabled'}
CERTIFICATE = {'kind','version','legacy_descriptor_hash','legacy_count','legacy_head_hash',
               'dispatch_hash','schema_hash','capabilities'}
DESCRIPTOR = {'version','ledger_id','profile','root','root_identity','ledger_path','ledger_identity',
              'lock_path','lock_identity','evidence_path','evidence_identity','sources',
              'allow_synthetic_fixtures','max_age_seconds'}
# Pin exact dispatch semantics rather than accept an opaque caller-selected version.
DISPATCH_HASH = digest({'format':FORMAT_PROFILE,'rows':ROW_FIELDS,
    'v1':PROFILE,'parent':PARENT_PROFILE,'observation_fields':sorted(OBSERVATION),
    'marker_fields':sorted(MARKER),'certificate_fields':sorted(CERTIFICATE),
    'capability_fields':sorted(CAPABILITY),'transition_sentinel':TRANSITION_SENTINEL,
    'unknown_sentinels':[UNKNOWN_POOL,UNKNOWN_MINT,UNKNOWN_SLOT],
    'types':['transition','legacy_observation','common_bank_observation','unresolved_marker'],
    'marker_statuses':['PENDING','FAILED','INCOMPLETE','UNAVAILABLE'],
    'no_marker_resolution':True})


class ChainUnavailable(ValueError):
    """Entire snapshot refused; no usable prefix or cached fallback."""


def _need(condition, code):
    if not condition: raise ChainUnavailable(code)


def _hash(value):
    return type(value) is str and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def _identity(value):
    return type(value) is str and 1<=len(value)<=128 and all(
        c.isascii() and (c.isalnum() or c in '_.:-') for c in value)


def _slot(value): return type(value) is int and 0<=value<2**64

def _time(value): return type(value) is int and -2**63<=value<2**63


def _pairs(pairs):
    out={}
    for key,value in pairs:
        _need(key not in out,'CHAIN_DUPLICATE_JSON_KEY');out[key]=value
    return out


def _json(body):
    _need(type(body) is str and len(body)<=MAX_BYTES,'CHAIN_METADATA_BOUND')
    _need(len(body.encode())<=MAX_BYTES,'CHAIN_METADATA_BOUND')
    value=json.loads(body,object_pairs_hook=_pairs,
                     parse_constant=lambda _: (_need(False,'CHAIN_NONFINITE_JSON')))
    stack=[(value,0)];count=0
    while stack:
        node,depth=stack.pop();count+=1
        _need(depth<=12 and count<=1024,'CHAIN_JSON_RESOURCE_BOUND')
        if type(node) is dict:stack.extend((v,depth+1) for v in node.values())
        elif type(node) is list:stack.extend((v,depth+1) for v in node)
        else:_need(type(node) in (str,int,bool,type(None)),'CHAIN_JSON_VALUE_INVALID')
    _need(type(value) is dict and body==canonical(value),'CHAIN_NONCANONICAL_METADATA')
    return value


def _source(value, allow_fixtures):
    _need(type(value) is dict and SOURCE<=set(value),'CHAIN_SOURCE_INVALID')
    _need(_identity(value['source_id']) and value['source_kind'] in
          ('coordinator_capture','synthetic_fixture') and value['network']==NETWORK
          and value['genesis_hash']==GENESIS,'CHAIN_SOURCE_INVALID')
    _need(value['source_kind']!='synthetic_fixture' or allow_fixtures,'CHAIN_SYNTHETIC_NOT_ALLOWED')
    return tuple(value[k] for k in sorted(SOURCE))


def _binding(value):
    for key in BINDING:
        _need(_identity(value[key]) if key in ('capture_id','scan_id') else _hash(value[key]),
              'CHAIN_CAPTURE_BINDING_INVALID')


@dataclass(frozen=True)
class ChainAnchor:
    """External byte-exact legacy baseline and reviewed certificate/schema pins.

    Not an authority token. Do not construct pins from candidate/evidence claims.
    """
    descriptor_body: str
    legacy_rows: tuple
    legacy_head: tuple
    certificate_hash: str
    schema_hash: str
    head: tuple


@dataclass(frozen=True)
class ChainSnapshot:
    descriptor_body: str
    descriptor_hash: str
    certificate_body: str
    certificate_hash: str
    head: tuple
    rows: tuple


@dataclass(frozen=True)
class TypedRecord:
    row: tuple
    version: int
    profile: str
    kind: str
    pool: str | None
    mint: str | None
    slot: int | None
    source_id: str
    evidence_hashes: tuple
    issuance_enabled: bool
    status: str


@dataclass(frozen=True)
class ScopeEnumeration:
    observations: tuple
    markers: tuple
    evidence_hashes: tuple
    reasons: tuple
    head: tuple

    def diagnostic(self):
        return {'schema':'common_bank_receipt_scope_diagnostic_v1',
                'observation_count':len(self.observations),'marker_count':len(self.markers),
                'evidence_hashes':list(self.evidence_hashes),'reasons':list(self.reasons),
                'head':list(self.head),'decision':'REJECT','provider_calls':0,
                'source_authenticated':False,'finality_authenticated':False,
                'protected_read_verified':False,'journal_completion_verified':False,
                'raw_conflicts_checked':False,'history_complete':False,
                'ownership_approved':False,'historical_interval_exclusion_allowed':False,
                'private_control_proven':False,'eligible_for_trading':False}


@dataclass(frozen=True)
class TypedChain:
    descriptor_body: str
    certificate_body: str
    certificate_hash: str
    transition_row: tuple
    records: tuple
    head: tuple

    def enumerate_scope(self, *, pool, mint, slot):
        """No source/profile/time selector; unknown identities block every scope."""
        try:
            address(pool);address(mint);_need(_slot(slot),'CHAIN_SCOPE_INVALID')
            observations=[];markers=[];refs=set();reasons=[]
            for record in self.records:
                unknown_identity=record.pool is None or record.mint is None
                matches=record.pool==pool and record.mint==mint
                if record.kind=='unresolved_marker':
                    applies=unknown_identity or (matches and (record.slot is None or record.slot==slot))
                    if not applies:continue
                    markers.append(record)
                    reason=('CHAIN_UNKNOWN_IDENTITY_MARKER' if unknown_identity else
                            'CHAIN_UNKNOWN_SLOT_MARKER' if record.slot is None else
                            'CHAIN_UNRESOLVED_'+record.status)
                    if reason not in reasons:reasons.append(reason)
                elif matches and record.slot==slot:observations.append(record)
                else:continue
                _need(len(observations)+len(markers)<=MAX_SCOPED_RECORDS,'CHAIN_SCOPE_RECORD_BOUND')
                refs.update(record.evidence_hashes)
                _need(len(refs)<=MAX_REFERENCES,'CHAIN_SCOPE_REFERENCE_BOUND')
            return ScopeEnumeration(tuple(observations),tuple(markers),tuple(sorted(refs)),tuple(reasons),self.head)
        except ChainUnavailable:raise
        except (ValueError,TypeError,KeyError,UnicodeError):
            raise ChainUnavailable('CHAIN_SCOPE_INVALID') from None


def _preflight_rows(rows):
    _need(type(rows) is tuple and len(rows)<=MAX_RECORDS,'CHAIN_RECORD_BOUND')
    size=0
    for row in rows:
        _need(type(row) is tuple and len(row)==10 and type(row[0]) is int,'CHAIN_ROW_INVALID')
        for index,value in enumerate(row[1:],1):
            _need(type(value) is str and len(value)<=(MAX_BYTES if index==6 else 128),'CHAIN_ROW_BOUND')
            size+=len(value.encode())
            _need(size<=MAX_TOTAL_BYTES,'CHAIN_TOTAL_BOUND')
        _need(len(row[6].encode())<=MAX_BYTES,'CHAIN_METADATA_BOUND')
    return size


def _validate(snapshot, anchor):
    _need(type(snapshot) is ChainSnapshot and type(anchor) is ChainAnchor,'CHAIN_TYPED_INPUT_REQUIRED')
    # Length/type preflight precedes JSON parsing, sorting, hashing or row copies.
    size=_preflight_rows(snapshot.rows)+_preflight_rows(anchor.legacy_rows)
    for body in (snapshot.descriptor_body,snapshot.certificate_body,anchor.descriptor_body):
        _need(type(body) is str and len(body)<=MAX_BYTES,'CHAIN_METADATA_BOUND')
        size+=len(body.encode())
    _need(size<=MAX_TOTAL_BYTES,'CHAIN_TOTAL_BOUND')
    descriptor=_json(snapshot.descriptor_body)
    _need(snapshot.descriptor_body==anchor.descriptor_body and _hash(snapshot.descriptor_hash)
          and digest(descriptor)==snapshot.descriptor_hash,'CHAIN_LEGACY_DESCRIPTOR_MISMATCH')
    _need(set(descriptor)==DESCRIPTOR and type(descriptor['version']) is int
          and descriptor['version']==1 and descriptor['profile']==PROFILE,'CHAIN_DESCRIPTOR_INVALID')
    _need(type(descriptor['allow_synthetic_fixtures']) is bool
          and type(descriptor['max_age_seconds']) is int and 1<=descriptor['max_age_seconds']<=300
          and _identity(descriptor['ledger_id']),'CHAIN_DESCRIPTOR_INVALID')
    for key,count in [('root_identity',2),('ledger_identity',2),('evidence_identity',2),('lock_identity',3)]:
        _need(type(descriptor[key]) is list and len(descriptor[key])==count
              and all(type(n) is int and n>=0 for n in descriptor[key]),'CHAIN_DESCRIPTOR_INVALID')
    for key in ('root','ledger_path','lock_path','evidence_path'):
        _need(type(descriptor[key]) is str and descriptor[key].startswith('/')
              and len(descriptor[key])<=4096,'CHAIN_DESCRIPTOR_INVALID')
    sources=descriptor['sources'];_need(type(sources) is list and 1<=len(sources)<=MAX_CAPABILITIES,'CHAIN_CAPABILITY_BOUND')
    legacy_sources={}
    for source in sources:
        _need(type(source) is dict and set(source)==SOURCE|{'profile'} and source['profile']==PROFILE,
              'CHAIN_LEGACY_SOURCE_INVALID')
        _source(source,descriptor['allow_synthetic_fixtures'])
        _need(source['source_id'] not in legacy_sources,'CHAIN_CAPABILITY_DUPLICATE')
        legacy_sources[source['source_id']]=source
    _need(type(anchor.legacy_head) is tuple and len(anchor.legacy_head)==2
          and type(anchor.legacy_head[0]) is int and anchor.legacy_head[0]==len(anchor.legacy_rows)
          and _hash(anchor.legacy_head[1]),'CHAIN_LEGACY_HEAD_INVALID')
    count=anchor.legacy_head[0]
    _need(len(snapshot.rows)>count and snapshot.rows[:count]==anchor.legacy_rows,'CHAIN_LEGACY_PREFIX_MISMATCH')
    certificate=_json(snapshot.certificate_body)
    _need(_hash(anchor.certificate_hash) and snapshot.certificate_hash==anchor.certificate_hash
          and digest(certificate)==anchor.certificate_hash,'CHAIN_CERTIFICATE_MISMATCH')
    _need(set(certificate)==CERTIFICATE and certificate['kind']=='pool_receipt_transition_certificate_v2'
          and type(certificate['version']) is int and certificate['version']==2
          and type(certificate['legacy_count']) is int and certificate['legacy_count']==count
          and certificate['legacy_descriptor_hash']==snapshot.descriptor_hash
          and certificate['legacy_head_hash']==anchor.legacy_head[1]
          and certificate['dispatch_hash']==DISPATCH_HASH and _hash(anchor.schema_hash)
          and certificate['schema_hash']==anchor.schema_hash,'CHAIN_TRANSITION_BINDING_INVALID')
    caps=certificate['capabilities'];_need(type(caps) is list and 1<=len(caps)<=MAX_CAPABILITIES,'CHAIN_CAPABILITY_BOUND')
    capabilities={};source_ids={}
    for cap in caps:
        _need(type(cap) is dict and set(cap)==CAPABILITY,'CHAIN_CAPABILITY_INVALID')
        identity=_source(cap,descriptor['allow_synthetic_fixtures'])
        _need(type(cap['version']) is int and (cap['version'],cap['profile']) in
              ((1,PROFILE),(2,PROFILE),(2,PARENT_PROFILE)) and type(cap['issuance_enabled']) is bool,
              'CHAIN_CAPABILITY_INVALID')
        key=(cap['source_id'],cap['version'],cap['profile'])
        _need(key not in capabilities,'CHAIN_CAPABILITY_DUPLICATE')
        _need(cap['source_id'] not in source_ids or source_ids[cap['source_id']]==identity,'CHAIN_SOURCE_REBIND')
        source_ids[cap['source_id']]=identity;capabilities[key]=cap
    for source_id,source in legacy_sources.items():
        cap=capabilities.get((source_id,1,PROFILE))
        _need(cap is not None and all(cap[k]==source[k] for k in SOURCE),'CHAIN_LEGACY_CAPABILITY_MISSING')
    config=CoordinatorBoundary(descriptor['ledger_id'],descriptor['root'],descriptor['ledger_path'],
        descriptor['evidence_path'],frozenset(ApprovedSource(**s) for s in sources),
        descriptor['allow_synthetic_fixtures'],descriptor['max_age_seconds'])
    extended_config=CoordinatorBoundary(config.ledger_id,config.root,config.ledger_path,config.evidence_path,
        frozenset(ApprovedSource(**{k:cap[k] for k in SOURCE|{'profile'}}) for cap in caps
                  if cap['version']==2 and cap['profile']==PROFILE),
        config.allow_synthetic_fixtures,config.max_age_seconds)
    _need(type(anchor.head) is tuple and len(anchor.head)==2 and type(anchor.head[0]) is int
          and 0<=anchor.head[0]<=MAX_RECORDS and _hash(anchor.head[1])
          and snapshot.head==anchor.head,'CHAIN_CURRENT_HEAD_PIN_MISMATCH')
    _need(type(snapshot.head) is tuple and len(snapshot.head)==2 and type(snapshot.head[0]) is int
          and snapshot.head[0]==len(snapshot.rows) and _hash(snapshot.head[1]),'CHAIN_HEAD_INVALID')
    previous=snapshot.descriptor_hash;publications=set();payloads=set();records=[];transition=None
    for seq,row in enumerate(snapshot.rows,1):
        number,publication,pool,mint,slot,source,payload,payload_hash,prior,row_hash=row
        data=_json(payload)
        _need(number==seq and _identity(publication) and publication not in publications
              and _hash(payload_hash) and payload_hash not in payloads and digest(data)==payload_hash
              and prior==previous and row_hash==digest({'seq':number,'publication_id':publication,
                  'payload_hash':payload_hash,'previous_hash':prior}),'CHAIN_ROW_INTEGRITY_INVALID')
        publications.add(publication);payloads.add(payload_hash)
        if seq==count+1:
            _need(previous==anchor.legacy_head[1] and data=={'version':2,'type':'transition',
                  'profile':FORMAT_PROFILE,'certificate_hash':anchor.certificate_hash}
                  and type(data['version']) is int and publication=='transition:'+anchor.certificate_hash
                  and (pool,mint,slot,source)==(TRANSITION_SENTINEL,)*4,'CHAIN_TRANSITION_INVALID')
            transition=row
        else:
            if seq<=count:
                # Exact original v1 parser, no I/O or production reader behavior change.
                receipt=_decode(payload,config);kind='legacy_observation';version=1;profile=PROFILE
                raw=data['receipt'];status='RECORDED_UNVERIFIED';refs=receipt.refs.hashes()
            else:
                _need(set(data)=={'version','type','profile','certificate_hash','observation'}
                      or set(data)=={'version','type','profile','certificate_hash','marker'},'CHAIN_PAYLOAD_INVALID')
                _need(type(data['version']) is int and data['version']==2
                      and data['certificate_hash']==anchor.certificate_hash,'CHAIN_PAYLOAD_CERTIFICATE_INVALID')
                version=2;kind=data['type'];profile=data['profile']
                if kind=='legacy_observation':
                    _need(profile==PROFILE and 'observation' in data,'CHAIN_TYPE_PROFILE_INVALID')
                    raw=data['observation'];refs=_decode(canonical({'version':1,'profile':PROFILE,'receipt':raw}),
                        extended_config).refs.hashes();status='RECORDED_UNVERIFIED'
                elif kind=='common_bank_observation':
                    _need(profile==PARENT_PROFILE and 'observation' in data,'CHAIN_TYPE_PROFILE_INVALID')
                    raw=data['observation'];_need(type(raw) is dict and set(raw)==OBSERVATION,'CHAIN_OBSERVATION_INVALID')
                    _binding(raw);_need(_slot(raw['slot']) and _time(raw['snapshot_time'])
                        and _time(raw['captured_at']) and _hash(raw['manifest_hash']),'CHAIN_OBSERVATION_INVALID')
                    refs=tuple(raw[k] for k in sorted(BINDING) if k.endswith('_hash'))+(raw['manifest_hash'],)
                    status='RECORDED_UNVERIFIED'
                elif kind=='unresolved_marker':
                    _need(profile==PARENT_PROFILE and 'marker' in data,'CHAIN_TYPE_PROFILE_INVALID')
                    raw=data['marker'];_need(type(raw) is dict and set(raw)==MARKER,'CHAIN_MARKER_INVALID')
                    _binding(raw);_need(raw['slot'] is None or _slot(raw['slot']),'CHAIN_MARKER_INVALID')
                    status=raw['status'];_need(status in ('PENDING','FAILED','INCOMPLETE','UNAVAILABLE'),'CHAIN_MARKER_INVALID')
                    refs=raw['evidence_hashes'];_need(type(refs) is list and len(refs)<=MAX_REFERENCES
                        and all(_hash(h) for h in refs) and len(set(refs))==len(refs),'CHAIN_MARKER_REFERENCE_INVALID')
                    refs=tuple(refs)+tuple(raw[k] for k in sorted(BINDING) if k.endswith('_hash'))
                else:raise ChainUnavailable('CHAIN_TYPE_PROFILE_INVALID')
            _source(raw,descriptor['allow_synthetic_fixtures'])
            for value in (raw['pool'],raw['mint']):
                if value is None:_need(kind=='unresolved_marker','CHAIN_SCOPE_INVALID')
                else:address(value)
            cap=capabilities.get((raw['source_id'],version,profile))
            _need(cap is not None and all(cap[k]==raw[k] for k in SOURCE),'CHAIN_CAPABILITY_MISSING')
            _need((pool,mint,slot,source)==(raw['pool'] if raw['pool'] is not None else UNKNOWN_POOL,
                raw['mint'] if raw['mint'] is not None else UNKNOWN_MINT,
                str(raw['slot']) if raw['slot'] is not None else UNKNOWN_SLOT,raw['source_id']),
                'CHAIN_SENTINEL_OR_INDEX_INVALID')
            records.append(TypedRecord(row,version,profile,kind,raw['pool'],raw['mint'],raw['slot'],
                raw['source_id'],tuple(sorted(set(refs))),cap['issuance_enabled'],status))
        previous=row_hash
    _need(previous==snapshot.head[1],'CHAIN_HEAD_MISMATCH')
    return TypedChain(snapshot.descriptor_body,snapshot.certificate_body,snapshot.certificate_hash,
                      transition,tuple(records),snapshot.head)


def validate_receipt_chain(snapshot, anchor):
    """Audit ALL bytes/records or raise; never loads state or grants provenance."""
    try:return _validate(snapshot,anchor)
    except ChainUnavailable:raise
    except (ValueError,TypeError,KeyError,IndexError,AttributeError,RecursionError,OverflowError,UnicodeError,LedgerError):
        raise ChainUnavailable('CHAIN_MALFORMED_INPUT') from None
