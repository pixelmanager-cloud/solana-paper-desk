"""Explicit genesis-to-finalized-cutoff research seed. No entry approval.

Lock order is canonical research worker -> canonical evidence invocation. Raw
setup pages and immutable heads commit together. The existing queue, counter,
history replay and source-seal APIs are the only authorities for their state.
"""
import fcntl
import json
import zlib
from .evidence import EvidenceStore
from .history_progress import HistoryProgress,canonical_ownership_path,ownership_lock_path
from .job_persistence import JobPersistence,BIRTH_ACQUISITION_V1,canonical_job_path
from .model import canonical,digest
from .programs import address
from .replay_history import replay_history
from .security import mint_policy


class _Setup:
    def __init__(self,store,descriptor,admission):
        self.store=store;self.descriptor=descriptor;self.identity=descriptor['scan_id']
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ownership_acquisition_setup(scan_id TEXT PRIMARY KEY,job_descriptor_hash TEXT NOT NULL,mint_hash TEXT,cutoff_hash TEXT)')
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT 1 FROM ownership_acquisition_setup WHERE scan_id=?',(self.identity,)).fetchone()
            if not existing:
                if admission['requests_used'] or admission['state']!='ADMITTED':
                    raise ValueError('Missing charged acquisition setup')
                c.execute('INSERT INTO ownership_acquisition_setup VALUES(?,?,NULL,NULL)',(self.identity,digest(descriptor)))
            c.execute("CREATE TRIGGER IF NOT EXISTS acquisition_setup_update BEFORE UPDATE ON ownership_acquisition_setup WHEN NEW.scan_id!=OLD.scan_id OR NEW.job_descriptor_hash!=OLD.job_descriptor_hash OR (OLD.mint_hash IS NOT NULL AND NEW.mint_hash IS NOT OLD.mint_hash) OR (OLD.cutoff_hash IS NOT NULL AND NEW.cutoff_hash IS NOT OLD.cutoff_hash) BEGIN SELECT RAISE(ABORT,'Immutable acquisition setup'); END")
            # Separate guard also upgrades databases created before this repair.
            # NEW.rowid detects writes through rowid, _rowid_ and oid aliases,
            # including OR REPLACE collisions that otherwise delete another job.
            c.execute("CREATE TRIGGER IF NOT EXISTS acquisition_setup_identity BEFORE UPDATE ON ownership_acquisition_setup WHEN NEW.rowid IS NOT OLD.rowid BEGIN SELECT RAISE(ABORT,'Immutable acquisition row identity'); END")
            c.execute("CREATE TRIGGER IF NOT EXISTS acquisition_setup_delete BEFORE DELETE ON ownership_acquisition_setup BEGIN SELECT RAISE(ABORT,'Immutable acquisition setup'); END")
            c.execute("CREATE TRIGGER IF NOT EXISTS acquisition_setup_replace BEFORE INSERT ON ownership_acquisition_setup WHEN EXISTS(SELECT 1 FROM ownership_acquisition_setup WHERE scan_id=NEW.scan_id OR rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'Immutable acquisition setup'); END")
        self.read()

    def read(self):
        with self.store.connect() as c:
            row=c.execute('SELECT job_descriptor_hash,mint_hash,cutoff_hash FROM ownership_acquisition_setup WHERE scan_id=?',(self.identity,)).fetchone()
        if not row or row[0]!=digest(self.descriptor):raise ValueError('Acquisition database/descriptor binding mismatch')
        result={'mint_hash':row[1],'cutoff_hash':row[2]}
        for name,method,params in [('mint_hash','getAccountInfo',[self.descriptor['mint'],{'encoding':'base64','commitment':'confirmed'}]),
                                   ('cutoff_hash','getSlot',[{'commitment':'finalized'}])]:
            if result[name] is not None:
                record=self.store.load(result[name])
                if record.get('method')!=method or record.get('params')!=params:raise ValueError('Acquisition setup request mismatch')
                self.validate(name,record['result'])
                result['mint' if name=='mint_hash' else 'cutoff']=record['result']
        if result['cutoff_hash'] and not result['mint_hash']:raise ValueError('Cutoff has no mint evidence')
        return result

    @staticmethod
    def validate(name,result):
        if name=='cutoff_hash':
            if type(result) is not int or not 0<=result<(1<<64)-1:raise ValueError('Finalized cutoff unavailable')
        elif not isinstance(result,dict) or 'value' not in result:
            raise ValueError('Mint response unavailable')

    def save(self,name,record):
        if name not in ('mint_hash','cutoff_hash'):raise ValueError('Invalid setup stage')
        self.validate(name,record['result'])
        raw=canonical(record).encode();key=digest(record);compressed=zlib.compress(raw)
        if len(raw)>16*1024*1024:raise ValueError('Evidence page exceeds size budget')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old=self.read()[name]
                if old and old!=key:raise ValueError('Immutable acquisition setup changed')
                if not c.execute('SELECT 1 FROM pages WHERE hash=?',(key,)).fetchone():
                    used=c.execute('SELECT COALESCE(SUM(length(payload)),0) FROM pages').fetchone()[0]
                    if used+len(compressed)>self.store.max_bytes:raise ValueError('Evidence storage budget exhausted')
                    c.execute('INSERT INTO pages VALUES(?,?,?)',(key,compressed,len(raw)))
                c.execute('UPDATE ownership_acquisition_setup SET '+name+'=? WHERE scan_id=?',(key,self.identity))
                c.commit()
            except BaseException:c.rollback();raise
        self.read()


def _policy(setup):
    try:return mint_policy(setup['mint']['value'])
    except (ValueError,KeyError,TypeError,IndexError):
        return {'decision':'SKIP','reasons':['INVALID_MINT_EVIDENCE']}


def _source(descriptor,setup,admission,coverage,status):
    policy=_policy(setup) if setup['mint_hash'] else {'decision':'SKIP','reasons':['MINT_EVIDENCE_UNAVAILABLE']}
    unknowns=[status,'OWNERSHIP_ACCEPTANCE_NOT_ESTABLISHED']
    if coverage:unknowns+=coverage['reasons']
    report={'mint':descriptor['mint'],'observed_at':descriptor['admitted_at'],'mode':'RESEARCH_ONLY','decision':'SKIP',
            'eligible_for_trading':False,'findings':policy['reasons'],'unknowns':sorted(set(unknowns)),
            'calls':admission['requests_used'],'token_policy':policy,'history_queries':[coverage] if coverage else [],
            'acquisition':{'kind':BIRTH_ACQUISITION_V1,'job_descriptor_hash':digest(descriptor),
                           'mint_hash':setup['mint_hash'],'cutoff_hash':setup['cutoff_hash'],
                           'cutoff':setup.get('cutoff'),'status':status},
            'notice':'Birth-inclusive bounded research. Raw witnessed birth is not ownership, entry freshness or paper readiness.'}
    if setup['mint_hash']:report['mint_evidence_hash']=setup['mint_hash']
    report['report_hash']=digest(report)
    return {'id':descriptor['scan_id'],'mint':descriptor['mint'],'created':descriptor['admitted_at'],
            'status':'COMPLETE','result':canonical(report)}


def _validate_binding(report,descriptor,setup):
    binding=report.get('acquisition',{})
    if (binding.get('kind')!=BIRTH_ACQUISITION_V1 or binding.get('job_descriptor_hash')!=digest(descriptor)
            or binding.get('mint_hash')!=setup['mint_hash'] or binding.get('cutoff_hash')!=setup['cutoff_hash']
            or binding.get('cutoff')!=setup.get('cutoff')):
        raise ValueError('Prepared acquisition setup/source mismatch')

def _archive_seed(store,source):
    """Move original-scan checkpoints out of continuation attempt accounting.

    Keep exact query/coverage/status/attempt bytes in an append-only archive.
    The seed report includes these spent attempts and raw page references.
    ownership_worker.seed then creates its usual zero-continuation baseline.
    The SINGLE ownership_budgets counter is neither copied nor reset.
    """
    queries=json.loads(source['result'])['history_queries']
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        try:
            c.execute('CREATE TABLE IF NOT EXISTS ownership_acquisition_history(history_id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,budget TEXT NOT NULL,query TEXT NOT NULL,coverage TEXT,status TEXT NOT NULL,attempts INTEGER NOT NULL)')
            for operation in ('UPDATE','DELETE'):
                c.execute("CREATE TRIGGER IF NOT EXISTS acquisition_archive_"+operation.lower()+" BEFORE "+operation+" ON ownership_acquisition_history BEGIN SELECT RAISE(ABORT,'Immutable acquisition archive'); END")
            c.execute("CREATE TRIGGER IF NOT EXISTS acquisition_archive_replace BEFORE INSERT ON ownership_acquisition_history WHEN EXISTS(SELECT 1 FROM ownership_acquisition_history WHERE history_id=NEW.history_id OR rowid=NEW.rowid) BEGIN SELECT RAISE(ABORT,'Immutable acquisition archive'); END")
            for coverage in queries:
                query={k:coverage[k] for k in ('address','start','end','token_accounts_filter')}
                if 'slot_range' in coverage:query['slot_range']=coverage['slot_range']
                identity=digest({'budget':source['id'],'query':query})
                row=c.execute('SELECT budget,query,coverage,status,attempts FROM ownership_history WHERE id=?',(identity,)).fetchone()
                old=c.execute('SELECT source_hash,budget,query,coverage,status,attempts FROM ownership_acquisition_history WHERE history_id=?',(identity,)).fetchone()
                if old:
                    if old[0]!=digest(source) or old[1:3]!=(source['id'],canonical(query)) or old[3]!=canonical(coverage) or row is not None:
                        raise ValueError('Acquisition checkpoint handoff conflict')
                    continue
                if not row or row[:3]!=(source['id'],canonical(query),canonical(coverage)):
                    raise ValueError('Acquisition checkpoint/source mismatch')
                c.execute('INSERT INTO ownership_acquisition_history VALUES(?,?,?,?,?,?,?)',(identity,digest(source),*row))
                c.execute('DELETE FROM ownership_history WHERE id=?',(identity,))
            c.commit()
        except BaseException:c.rollback();raise


def _publish(worker,claim,progress,descriptor,setup,admission):
    worker._acquisition_claim(claim)
    source=admission['prepared_source'];report=json.loads(source['result'])
    _validate_binding(report,descriptor,setup)
    _archive_seed(progress.store,source)
    progress.seal_source(claim.scan_id,admission['descriptor_hash'],digest(source))
    worker.publish_acquisition(claim,source)
    return report


def acquire(research_db,evidence_db,rpc,*,mint=None,scan_id=None):
    """Create one admitted seed or resume its ID; <=4 acquisition attempts total, <=2 new pages/invocation."""
    if (mint is None)==(scan_id is None):raise ValueError('Specify exactly one mint or acquisition scan ID')
    if mint is not None:address(mint)
    if scan_id is not None and (not isinstance(scan_id,str) or not scan_id):raise ValueError('Acquisition scan ID required')
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db)
    if research==evidence:raise ValueError('Research and evidence databases must be separate')
    jobs=JobPersistence(research)
    with jobs.worker() as worker:
        if worker is None:return {'scan_id':scan_id,'status':'BUSY','provider_calls':0,'eligible_for_trading':False}
        if scan_id is not None:
            descriptor=jobs.descriptor(scan_id)
            if descriptor['kind']!=BIRTH_ACQUISITION_V1 or descriptor['evidence_db']!=str(evidence):
                raise ValueError('Acquisition database/dispatch mismatch')
        store=EvidenceStore(evidence)
        with open(ownership_lock_path(store,invocation=True),'a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'scan_id':scan_id,'status':'BUSY','provider_calls':0,'eligible_for_trading':False}
            if scan_id is None:
                scan_id=jobs.admit(mint,kind=BIRTH_ACQUISITION_V1,evidence_db=store.path)
                descriptor=jobs.descriptor(scan_id)
            progress=HistoryProgress(store)
            budget_descriptor={'kind':'ownership_admission_v1','scan_id':scan_id,'mint':descriptor['mint'],'created':descriptor['admitted_at']}
            admission=progress.admit(scan_id,budget_descriptor)
            setup=_Setup(store,descriptor,admission)
            current=jobs.source(scan_id)
            if current['status']=='COMPLETE':
                if admission['state']!='SEALED' or current!=admission['prepared_source']:raise ValueError('Completed acquisition source mismatch')
                # Verify setup links even on a zero-I/O terminal retry.
                report=json.loads(current['result'])
                _validate_binding(report,descriptor,setup.read())
                return {'scan_id':scan_id,'status':'ALREADY_COMPLETE','provider_calls':0,'requests_used':admission['requests_used'],
                        'report':report,'eligible_for_trading':False}
            claim=worker.claim_acquisition(scan_id);attempts=0;pages=0
            try:
                admission=progress.admission(scan_id)
                if admission['state'] in ('PREPARED','SEALED'):
                    report=_publish(worker,claim,progress,descriptor,setup.read(),admission)
                    return {'scan_id':scan_id,'status':'RECOVERED_COMPLETE','provider_calls':0,'requests_used':admission['requests_used'],
                            'report':report,'eligible_for_trading':False}
                def request(name,method,params):
                    nonlocal attempts
                    if progress.admission(scan_id)['requests_used']>=4 or not progress.reserve(scan_id):return False
                    attempts+=1
                    record={'method':method,'params':params,'result':rpc(method,params)}
                    setup.save(name,record);return True
                state=setup.read();status=None;coverage=None
                if not state['mint_hash']:
                    if not request('mint_hash','getAccountInfo',[descriptor['mint'],{'encoding':'base64','commitment':'confirmed'}]):
                        status='ACQUISITION_REQUEST_LIMIT_REACHED'
                    state=setup.read()
                if status is None and _policy(state)['decision']!='PASS_TOKEN_POLICY':status='UNSUPPORTED_TOKEN'
                if status is None and not state['cutoff_hash']:
                    if not request('cutoff_hash','getSlot',[{'commitment':'finalized'}]):status='ACQUISITION_REQUEST_LIMIT_REACHED'
                    state=setup.read()
                if status is None:
                    key=progress.create(scan_id,descriptor['mint'],0,descriptor['admitted_at']+1,{'gte':0,'lt':state['cutoff']+1})
                    while pages<2 and progress.admission(scan_id)['requests_used']<4:
                        before=progress.snapshot(key)
                        if before['status']=='DONE':break
                        result=progress.advance(key,rpc)
                        # Exactly one new page attempt is reserved by advance.
                        charged=result['requests_used']-before['requests_used'];attempts+=charged;pages+=charged
                        if result.get('busy'):
                            worker.interrupt_acquisition(claim)
                            return {'scan_id':scan_id,'status':'BUSY','provider_calls':attempts,
                                    'requests_used':result['requests_used'],'eligible_for_trading':False}
                        if result.get('blocked'):
                            status=result['blocked'];break
                        if result['status']=='RETRYABLE_ERROR':
                            worker.interrupt_acquisition(claim)
                            return {'scan_id':scan_id,'status':'PROVIDER_RETRY_REQUIRED','provider_calls':attempts,
                                    'requests_used':result['requests_used'],'eligible_for_trading':False}
                    current_history=progress.snapshot(key);coverage=current_history['coverage']
                    if coverage:
                        observations,_=replay_history(coverage,store)
                        from .account_history import account_inventory
                        inventory=account_inventory(descriptor['mint'],observations,coverage)
                        status=status or ('SEED_HISTORY_PARTIAL' if not coverage['query_range_exhausted'] else
                                          'SEED_HISTORY_ACQUIRED' if inventory['initialization_inventory_verified'] else 'LAUNCH_OR_INVENTORY_UNVERIFIED')
                    else:status=status or 'SEED_HISTORY_UNAVAILABLE'
                admission=progress.admission(scan_id)
                source=_source(descriptor,state,admission,coverage,status)
                admission=progress.prepare_source(scan_id,admission['descriptor_hash'],source)
                report=_publish(worker,claim,progress,descriptor,state,admission)
                return {'scan_id':scan_id,'status':status,'provider_calls':attempts,'requests_used':admission['requests_used'],
                        'report':report,'eligible_for_trading':False}
            except (ValueError,KeyError,TypeError,IndexError,OSError):
                # Preserve charged attempts and raw checkpoints. Do not expose
                # provider error bodies or make a failed invocation a new job.
                worker.interrupt_acquisition(claim)
                return {'scan_id':scan_id,'status':'ACQUISITION_RETRY_OR_EVIDENCE_BLOCKED','provider_calls':attempts,
                        'requests_used':progress.admission(scan_id)['requests_used'],'eligible_for_trading':False}
