"""One diagnostic raw transaction per existing admission; no ownership acceptance.

Trusted caller injects a ONE-request, read-only, nonretrying RPC dependency.
Canonical evidence invocation lock then request lock serialize with ownership.
PENDING is durable BEFORE reservation: any interruption, including between that
claim and reservation, blocks recapture. No new capture ID/signature can replace
it. This prefers an explicit evidence gap over duplicate/hidden requests.
No budget admission/reset, queue dispatch, RPC adapter or source-row writes.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass, asdict
import fcntl
import json
import sqlite3

from .account_keys import compiled_keys, compiled_instruction, index
from .evidence import EvidenceStore
from .history_progress import HistoryProgress, canonical_ownership_path, ownership_lock_path
from .model import canonical, digest
from .programs import address, unbase58
from .security import base58

PROFILE='raw-transaction-diagnostic-v1'
CONFIG={'encoding':'json','commitment':'finalized','maxSupportedTransactionVersion':0}
MAX_INSTRUCTIONS=256
SCHEMA={
    'raw_transaction_claims':'CREATE TABLE raw_transaction_claims(scan_id TEXT PRIMARY KEY NOT NULL,body TEXT NOT NULL,body_hash TEXT UNIQUE NOT NULL)',
    'raw_transaction_results':'CREATE TABLE raw_transaction_results(scan_id TEXT PRIMARY KEY NOT NULL,body TEXT NOT NULL,body_hash TEXT UNIQUE NOT NULL)',
}
for table in ('raw_transaction_claims','raw_transaction_results'):
    for verb in ('UPDATE','DELETE'):
        name=table+'_'+verb.lower()
        SCHEMA[name]=f"CREATE TRIGGER {name} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Immutable raw capture'); END"
    name=table+'_insert'
    SCHEMA[name]=f"CREATE TRIGGER {name} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE scan_id=NEW.scan_id OR rowid=NEW.rowid OR body_hash=NEW.body_hash) BEGIN SELECT RAISE(ABORT,'Immutable raw capture identity'); END"


class CaptureBlocked(ValueError):pass


def need(condition, reason):
    if not condition:raise CaptureBlocked(reason)


def uint(value):return type(value) is int and 0<=value<2**64


def signature(value):
    need(isinstance(value,str) and 64<=len(value)<=88,'SIGNATURE_INVALID')
    raw=unbase58(value)
    need(len(raw)==64 and base58(raw)==value,'SIGNATURE_INVALID')
    return value


@dataclass(frozen=True)
class TransactionBinding:
    """Trusted existing ownership_admission descriptor hash, never candidate ID."""
    scan_id:str
    descriptor_hash:str
    mint:str
    signature:str


@dataclass(frozen=True)
class ReadOnlyRPC:
    """Out-of-band dependency: exactly one request, no discovery/hidden retries.

    source_id labels provenance only. This component does not authenticate the
    source, caller privileges, finalized chain state or signatures cryptographically.
    """
    source_id:str
    rpc:object


def validate_response(result, binding):
    """Validate raw compiled shape/identity only, never successful CPI effects."""
    need(isinstance(result,dict) and uint(result.get('slot')),'TRANSACTION_SLOT_OR_SHAPE_INVALID')
    tx=result.get('transaction');meta=result.get('meta')
    need(isinstance(tx,dict) and isinstance(meta,dict) and 'err' in meta,'TRANSACTION_METADATA_MISSING')
    signatures=tx.get('signatures')
    need(isinstance(signatures,list) and bool(signatures) and signatures[0]==binding.signature,'TRANSACTION_SIGNATURE_MISMATCH')
    for value in signatures:signature(value)
    need(len(set(signatures))==len(signatures),'DUPLICATE_TRANSACTION_SIGNATURE')
    message=tx.get('message');need(isinstance(message,dict),'COMPILED_MESSAGE_MISSING')
    keys,_=compiled_keys(message,meta,result.get('version'),signatures)
    need(binding.mint in keys,'TRANSACTION_MINT_NOT_PRESENT')
    outer=message.get('instructions');inner=meta.get('innerInstructions')
    need(isinstance(outer,list) and len(outer)<=MAX_INSTRUCTIONS,'OUTER_INSTRUCTION_SHAPE_INVALID')
    need(isinstance(inner,list),'RAW_CPI_WITNESS_UNAVAILABLE')
    instructions=list(outer);parents=set()
    for group in inner:
        need(isinstance(group,dict),'INNER_INSTRUCTION_SHAPE_INVALID')
        parent=index(group.get('index'),len(outer))
        need(parent not in parents,'DUPLICATE_INNER_PARENT');parents.add(parent)
        children=group.get('instructions');need(isinstance(children,list),'INNER_INSTRUCTION_SHAPE_INVALID')
        instructions.extend(children)
        need(len(instructions)<=MAX_INSTRUCTIONS,'INSTRUCTION_INSPECTION_LIMIT')
    for ix in instructions:
        resolved=compiled_instruction(ix,keys)
        # Require existing raw bytes, never reconstruct jsonParsed instructions.
        need(isinstance(resolved.get('data'),str) and len(resolved['data'])<=20000,'RAW_INSTRUCTION_DATA_MISSING')
        if resolved['data']:unbase58(resolved['data'])
        height=resolved.get('stackHeight')
        need(height is None or (type(height) is int and 1<=height<=64),'STACK_HEIGHT_INVALID')
    at=result.get('blockTime')
    need(at is None or (type(at) is int and -2**63<=at<2**63),'TRANSACTION_TIME_SHAPE_INVALID')
    error=meta['err']
    need(error is None or (isinstance(error,str) and bool(error)) or (isinstance(error,dict) and len(error)==1),'TRANSACTION_ERROR_SHAPE_INVALID')
    need(error is None,'TRANSACTION_FAILED')
    return {'slot':result['slot'],'signature':binding.signature,'mint':binding.mint,
            'raw_instruction_count':len(instructions),'inner_group_count':len(inner),
            'block_time_present':type(at) is int}


class RawTransactionCapture:
    def __init__(self,store,transport):
        need(type(transport) is ReadOnlyRPC and isinstance(transport.source_id,str)
             and 1<=len(transport.source_id)<=128 and callable(transport.rpc),'TRUSTED_READ_ONLY_RPC_REQUIRED')
        need(isinstance(store,EvidenceStore) and not store.read_only,'WRITABLE_EXISTING_EVIDENCE_REQUIRED')
        path=canonical_ownership_path(store.path)
        need(path.is_file(),'EXISTING_EVIDENCE_REQUIRED')
        store.path=path;self.store=store;self.transport=transport
        # Reject missing foundation before HistoryProgress can initialize tables.
        with closing(store.connect()) as c:
            names={r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        need({'ownership_admissions','ownership_budgets'}<=names,'EXISTING_ADMISSION_REQUIRED')

    @contextmanager
    def _locks(self):
        with open(ownership_lock_path(self.store,invocation=True),'a') as invocation:
            try:fcntl.flock(invocation,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise CaptureBlocked('BUSY') from None
            with open(ownership_lock_path(self.store),'a') as request:
                try:fcntl.flock(request,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise CaptureBlocked('BUSY') from None
                yield

    def _schema(self,c):
        c.execute('BEGIN IMMEDIATE')
        for name,sql in SCHEMA.items():
            old=c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()
            if old:need(old[0]==sql,'CAPTURE_SCHEMA_MISMATCH')
            else:c.execute(sql)
        c.commit()

    def _binding(self,progress,binding):
        need(type(binding) is TransactionBinding,'EXPLICIT_INVESTIGATION_BINDING_REQUIRED')
        need(isinstance(binding.scan_id,str) and 1<=len(binding.scan_id)<=128,'SCAN_ID_INVALID')
        need(isinstance(binding.descriptor_hash,str) and len(binding.descriptor_hash)==64
             and all(ch in '0123456789abcdef' for ch in binding.descriptor_hash),'DESCRIPTOR_HASH_INVALID')
        address(binding.mint);signature(binding.signature)
        admission=progress.admission(binding.scan_id)
        need(admission is not None and admission['descriptor_hash']==binding.descriptor_hash
             and admission['descriptor']['mint']==binding.mint,'EXISTING_ADMISSION_BINDING_MISMATCH')
        return admission

    def _record(self,c,table,scan_id):
        row=c.execute('SELECT body,body_hash FROM '+table+' WHERE scan_id=?',(scan_id,)).fetchone()
        if row is None:return None
        body=json.loads(row[0]);need(isinstance(body,dict) and canonical(body)==row[0] and digest(body)==row[1],'CAPTURE_RECORD_CORRUPT')
        return body

    def _finish(self,claim,body):
        result={**body,'claim_hash':digest(claim)}
        with closing(self.store.connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO raw_transaction_results VALUES(?,?,?)',(claim['binding']['scan_id'],canonical(result),digest(result)))
            c.commit()
        return result

    def _replay(self,claim,result,admission):
        request=self.store.load(claim['request_hash'])
        need(request=={'kind':'raw_transaction_request_v1','method':'getTransaction',
                       'params':[claim['binding']['signature'],CONFIG]},'CAPTURE_REQUEST_CHANGED')
        need(type(claim['initial_used']) is int and 0<=claim['initial_used']<=admission['requests_used'],'CAPTURE_BUDGET_REGRESSION')
        if result is None:return {'state':'PENDING','reason':'PRIOR_ATTEMPT_AMBIGUOUS','request_hash':claim['request_hash']}
        need(set(result)=={'claim_hash','state','reason','response_hash','reserved_used','diagnostics'}
             and result['claim_hash']==digest(claim) and result['state'] in ('COMPLETE','REJECTED','FAILED','REFUSED'),'CAPTURE_RESULT_CORRUPT')
        used=result['reserved_used']
        need(type(used) is int and claim['initial_used']<=used<=admission['requests_used'],'CAPTURE_BUDGET_REGRESSION')
        if result['state']=='REFUSED':
            need(result['response_hash'] is None and used==claim['initial_used'] and result['diagnostics'] is None
                 and result['reason'] in ('SOURCE_PREPARED','REQUEST_BUDGET_EXHAUSTED'),'CAPTURE_RESULT_CORRUPT')
        else:
            need(used>=claim['initial_used']+1,'CAPTURE_RESERVATION_MISSING')
            response=self.store.load(result['response_hash'])
            if result['state']=='FAILED':
                need(response=={'kind':'raw_transaction_failure_v1','method':'getTransaction','params':request['params']}
                     and result['reason']=='RPC_ATTEMPT_FAILED' and result['diagnostics'] is None,'CAPTURE_FAILURE_CHANGED')
            else:
                need(isinstance(response,dict) and set(response)=={'kind','method','params','result'}
                     and response['kind']=='rpc_response_v1' and response['method']=='getTransaction'
                     and response['params']==request['params'],'CAPTURE_RESPONSE_CHANGED')
                try:
                    diagnostics=validate_response(response['result'],TransactionBinding(**claim['binding']))
                    reason=None;state='COMPLETE'
                except (ValueError,KeyError,TypeError,IndexError) as exc:
                    diagnostics=None;reason=str(exc) if type(exc) is CaptureBlocked else 'RAW_TRANSACTION_INVALID';state='REJECTED'
                need(state==result['state'] and diagnostics==result['diagnostics'] and reason==result['reason'],'CAPTURE_VALIDATION_CHANGED')
        return {**result,'request_hash':claim['request_hash']}

    def capture(self,binding):
        outcome={'profile':PROFILE,'state':'BLOCKED','provider_calls':0,'reason':None,
                 'lifecycle_verified':False,'finality_authenticated':False,'signature_authenticated':False,
                 'source_authenticated':False,'caller_privileges_verified':False,'cpi_success_verified':False,
                 'account_state_verified':False,'ownership_approved':False,'eligible_for_trading':False,
                 'runtime_blockers':['RAW_STATE_AND_CALLER_CONTROLS_UNRESOLVED','INDEPENDENT_LIFECYCLE_REQUIRED'],
                 'transport_dependency':'Inject separately reviewed one-request transport; generic helius_rpc unsupported, PR49 dedicated transport available'}
        try:
            with self._locks():
                progress=HistoryProgress(self.store);admission=self._binding(progress,binding)
                outcome['requests_used']=admission['requests_used']
                with closing(self.store.connect()) as c:self._schema(c)
                identity={'profile':PROFILE,'evidence_db':str(self.store.path),'source_id':self.transport.source_id,'binding':asdict(binding)}
                with closing(self.store.connect()) as c:
                    claim=self._record(c,'raw_transaction_claims',binding.scan_id)
                    result=self._record(c,'raw_transaction_results',binding.scan_id)
                if claim:
                    need(set(claim)==set(identity)|{'initial_used','request_hash'} and all(claim[k]==v for k,v in identity.items()),'CAPTURE_BINDING_CHANGED')
                    outcome.update(self._replay(claim,result,admission),requests_used=admission['requests_used']);return outcome
                need(result is None,'CAPTURE_CLAIM_MISSING')
                request={'kind':'raw_transaction_request_v1','method':'getTransaction','params':[binding.signature,dict(CONFIG)]}
                request_hash=self.store.save(request)
                claim={**identity,'initial_used':admission['requests_used'],'request_hash':request_hash}
                with closing(self.store.connect()) as c:
                    c.execute('BEGIN IMMEDIATE')
                    c.execute('INSERT INTO raw_transaction_claims VALUES(?,?,?)',(binding.scan_id,canonical(claim),digest(claim)))
                    c.commit()
                # PENDING now exists durably, even if reservation or I/O crashes.
                if not progress.reserve(binding.scan_id):
                    reason='SOURCE_PREPARED' if progress.admission(binding.scan_id)['state']=='PREPARED' else 'REQUEST_BUDGET_EXHAUSTED'
                    terminal=self._finish(claim,{'state':'REFUSED','reason':reason,'response_hash':None,
                                               'reserved_used':admission['requests_used'],'diagnostics':None})
                else:
                    used=progress.admission(binding.scan_id)['requests_used'];outcome.update(provider_calls=1,requests_used=used)
                    try:raw=self.transport.rpc('getTransaction',json.loads(canonical(request['params'])))
                    except Exception:
                        response_hash=self.store.save({'kind':'raw_transaction_failure_v1','method':'getTransaction','params':request['params']})
                        terminal=self._finish(claim,{'state':'FAILED','reason':'RPC_ATTEMPT_FAILED','response_hash':response_hash,'reserved_used':used,'diagnostics':None})
                    else:
                        response_hash=self.store.save({'kind':'rpc_response_v1','method':'getTransaction','params':request['params'],'result':raw})
                        self.store.load(response_hash)  # Validate content-addressed persistence, not response truth.
                        try:diagnostics=validate_response(raw,binding);state='COMPLETE';reason=None
                        except (ValueError,KeyError,TypeError,IndexError) as exc:
                            diagnostics=None;state='REJECTED';reason=str(exc) if type(exc) is CaptureBlocked else 'RAW_TRANSACTION_INVALID'
                        terminal=self._finish(claim,{'state':state,'reason':reason,'response_hash':response_hash,'reserved_used':used,'diagnostics':diagnostics})
                outcome.update(terminal,request_hash=request_hash,requests_used=progress.admission(binding.scan_id)['requests_used'])
        except (ValueError,KeyError,TypeError,IndexError,OSError,sqlite3.Error) as exc:
            outcome['reason']=str(exc) if type(exc) is CaptureBlocked else 'CAPTURE_EVIDENCE_OR_BINDING_BLOCKED'
        return outcome
