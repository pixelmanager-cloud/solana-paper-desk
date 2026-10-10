"""Durable, bounded history continuation. Provider attempts are charged before I/O."""
import json
from .model import canonical,digest
from .history import collect_history
from .replay_history import replay_history


def canonical_ownership_path(path):
    """Pin symlink/relative aliases; hard-linked databases are unsupported.

    SQLite and its sidecars require one stable pathname. Multiple hard links
    have no canonical pathname, so reject them before ownership writes or I/O.
    Operators must not rename/replace/link the database while workers run.
    """
    from pathlib import Path
    from stat import S_ISREG
    resolved=Path(path).resolve()
    try:identity=resolved.stat()
    except FileNotFoundError:return resolved
    if not S_ISREG(identity.st_mode) or identity.st_nlink!=1:
        raise ValueError('Ownership evidence database must be a regular file with one hard link')
    return resolved


def ownership_lock_path(store,invocation=False):
    # Recheck hard links before every lock, including direct HistoryProgress use.
    path=canonical_ownership_path(store.path)
    return str(path)+('.ownership-invocation.lock' if invocation else '.ownership.lock')


class HistoryProgress:
    def __init__(self,store):
        if store.read_only:raise ValueError('Writable evidence store required')
        store.path=canonical_ownership_path(store.path)
        self.store=store
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ownership_budgets(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,used INTEGER NOT NULL,ceiling INTEGER NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS ownership_history(id TEXT PRIMARY KEY,budget TEXT NOT NULL,query TEXT NOT NULL,coverage TEXT,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)')
            c.execute('CREATE TABLE IF NOT EXISTS ownership_admissions(id TEXT PRIMARY KEY,descriptor TEXT NOT NULL,state TEXT NOT NULL,prepared_source TEXT,prepared_used INTEGER,completed_source_hash TEXT)')
    def _admission(self,c,identity):
        return self.inspect_admission(c,identity)

    @staticmethod
    def inspect_admission(c,identity):
        """Validate existing admission in caller's transaction, without writes/locks.

        This does not create schema, admit a source or reserve capacity.
        """
        row=c.execute('SELECT a.descriptor,a.state,a.prepared_source,a.prepared_used,a.completed_source_hash,b.source_hash,b.used,b.ceiling FROM ownership_admissions a JOIN ownership_budgets b ON b.id=a.id WHERE a.id=?',(identity,)).fetchone()
        if not row:
            if c.execute('SELECT 1 FROM ownership_admissions WHERE id=?',(identity,)).fetchone():raise ValueError('Admission budget missing')
            return None
        descriptor=json.loads(row[0]);HistoryProgress._descriptor(identity,descriptor)
        if canonical(descriptor)!=row[0] or digest(descriptor)!=row[5]:raise ValueError('Admission descriptor mismatch')
        if row[1] not in ('ADMITTED','PREPARED','SEALED') or type(row[6]) is not int or type(row[7]) is not int or not 0<=row[6]<=row[7]<=18:
            raise ValueError('Admission state or budget invalid')
        source=json.loads(row[2]) if row[2] is not None else None
        if row[1]=='ADMITTED':
            if any(v is not None for v in row[2:5]):raise ValueError('Unexpected prepared source')
        else:
            HistoryProgress._completed_source(identity,descriptor,source)
            calls=json.loads(source['result'])['calls']
            if (type(row[3]) is not int or canonical(source)!=row[2] or digest(source)!=row[4]
                    or calls!=row[3] or not 0<=row[3]<=row[6]):
                raise ValueError('Prepared source or request count mismatch')
            if row[1]=='PREPARED' and row[3]!=row[6]:raise ValueError('Prepared request count changed')
        return {'descriptor':descriptor,'descriptor_hash':row[5],'state':row[1],
                'prepared_source':source,'prepared_requests_used':row[3],'completed_source_hash':row[4],
                'requests_used':row[6],'request_ceiling':row[7]}

    @staticmethod
    def _descriptor(identity,descriptor):
        from .programs import address
        if (not isinstance(identity,str) or not identity or not isinstance(descriptor,dict)
                or set(descriptor)!={'kind','scan_id','mint','created'}
                or descriptor['kind']!='ownership_admission_v1' or descriptor['scan_id']!=identity
                or type(descriptor['created']) is not int or descriptor['created']<0):
            raise ValueError('Immutable ownership admission descriptor required')
        address(descriptor['mint'])

    @staticmethod
    def _completed_source(identity,descriptor,source):
        if (not isinstance(source,dict) or set(source)!={'id','mint','created','status','result'}
                or source['id']!=identity or source['mint']!=descriptor['mint']
                or type(source['created']) is not int or source['created']!=descriptor['created']
                or source['status']!='COMPLETE' or not isinstance(source['result'],str)
                or len(source['result'].encode())>2*1024*1024):
            raise ValueError('Exact completed scan projection required')
        report=json.loads(source['result'])
        if not isinstance(report,dict):raise ValueError('Completed report required')
        summary=dict(report);claimed=summary.pop('report_hash',None)
        if (claimed!=digest(summary) or report.get('mint')!=descriptor['mint']
                or report.get('eligible_for_trading') is not False or type(report.get('calls')) is not int
                or not 0<=report['calls']<=18):raise ValueError('Completed report identity or budget invalid')

    def admit(self,identity,descriptor,ceiling=18):
        """Create/reopen a zero-origin admission on the single durable counter."""
        self._descriptor(identity,descriptor)
        if type(ceiling) is not int or not 1<=ceiling<=18:raise ValueError('Invalid request ceiling')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old=self._admission(c,identity)
                if old:
                    if old['descriptor']!=descriptor or old['request_ceiling']!=ceiling:raise ValueError('Admission identity changed')
                else:
                    if c.execute('SELECT 1 FROM ownership_budgets WHERE id=?',(identity,)).fetchone():raise ValueError('Existing completed budget cannot become an admission')
                    c.execute('INSERT INTO ownership_budgets VALUES(?,?,0,?)',(identity,digest(descriptor),ceiling))
                    c.execute("INSERT INTO ownership_admissions VALUES(?,?,'ADMITTED',NULL,NULL,NULL)",(identity,canonical(descriptor)))
                result=self._admission(c,identity);c.commit();return result
            except BaseException:c.rollback();raise

    def admission(self,identity):
        """Read immutable recovery data; legacy budgets return None."""
        with self.store.connect() as c:return self._admission(c,identity)

    def prepare_source(self,identity,descriptor_hash,source):
        """Persist exact intended COMPLETE row and freeze all new attempts."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old=self._admission(c,identity)
                if old is None or old['descriptor_hash']!=descriptor_hash:raise ValueError('Admission identity mismatch')
                self._completed_source(identity,old['descriptor'],source)
                if old['state']!='ADMITTED':
                    if old['prepared_source']!=source:raise ValueError('Prepared source changed')
                else:
                    if json.loads(source['result'])['calls']!=old['requests_used']:raise ValueError('Source must include every charged attempt')
                    c.execute("UPDATE ownership_admissions SET state='PREPARED',prepared_source=?,prepared_used=?,completed_source_hash=? WHERE id=?",
                              (canonical(source),old['requests_used'],digest(source),identity))
                result=self._admission(c,identity);c.commit();return result
            except BaseException:c.rollback();raise

    def seal_source(self,identity,descriptor_hash,source_hash):
        """Idempotently seal the prepared source; never rebind or reset usage."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                old=self._admission(c,identity)
                if (old is None or old['descriptor_hash']!=descriptor_hash or old['state']=='ADMITTED'
                        or old['completed_source_hash']!=source_hash):raise ValueError('Exact prepared source required')
                c.execute("UPDATE ownership_admissions SET state='SEALED' WHERE id=? AND state='PREPARED'",(identity,))
                result=self._admission(c,identity);c.commit();return result
            except BaseException:c.rollback();raise

    def budget(self,identity,source_hash,used,ceiling=18):
        if not isinstance(identity,str) or not identity or not isinstance(source_hash,str) or len(source_hash)!=64:
            raise ValueError('Immutable investigation identity required')
        if type(used) is not int or type(ceiling) is not int or not 0<=used<=ceiling<=18:raise ValueError('Invalid request budget')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            try:
                admission=self._admission(c,identity)
                if admission:
                    if (admission['state']!='SEALED' or admission['completed_source_hash']!=source_hash
                            or admission['prepared_requests_used']!=used or admission['request_ceiling']!=ceiling):
                        raise ValueError('Completed admission source is not sealed or does not match')
                else:
                    old=c.execute('SELECT source_hash,ceiling FROM ownership_budgets WHERE id=?',(identity,)).fetchone()
                    if old and old!=(source_hash,ceiling):raise ValueError('Investigation identity changed')
                    c.execute('INSERT OR IGNORE INTO ownership_budgets VALUES(?,?,?,?)',(identity,source_hash,used,ceiling))
                c.commit()
            except BaseException:c.rollback();raise

    def _reservation_blocker(self,c,identity):
        admission=self._admission(c,identity)
        if admission and admission['state']=='PREPARED':return 'INVESTIGATION_SOURCE_PREPARED'
        return None

    def reserve(self,budget):
        """Commit an attempted setup/bank request before invoking the provider."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if self._reservation_blocker(c,budget):c.rollback();return False
            changed=c.execute('UPDATE ownership_budgets SET used=used+1 WHERE id=? AND used<ceiling',(budget,)).rowcount
            c.commit()
        return bool(changed)

    def bank(self,budget):
        with self.store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ownership_banks(budget TEXT PRIMARY KEY,snapshot_hash TEXT NOT NULL,clock_hash TEXT)')
            row=c.execute('SELECT snapshot_hash,clock_hash FROM ownership_banks WHERE budget=?',(budget,)).fetchone()
        return {'snapshot_hash':row[0],'block_time_hash':row[1]} if row else None

    def capture_bank(self,budget,mint,accounts,rpc):
        """One locked, charged I/O step; a saved bank never moves on restart."""
        import fcntl
        from .ownership_snapshot import validate_bank
        with open(ownership_lock_path(self.store),'a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'blocked':'BUSY','attempted':False}
            bank=self.bank(budget)
            if bank and bank['block_time_hash']:return {**bank,'attempted':False}
            if bank:
                snapshot=self.store.load(bank['snapshot_hash'])
                validate_bank(mint,accounts,snapshot)
                method='getBlockTime';params=[snapshot['result']['context']['slot']]
            else:
                if not accounts or len(accounts)>99 or len(accounts)!=len(set(accounts)):
                    return {'blocked':'SNAPSHOT_ACCOUNT_COVERAGE_UNSUPPORTED','attempted':False}
                method='getMultipleAccounts';params=[[mint]+sorted(accounts),{'encoding':'base64','commitment':'finalized'}]
            if not self.reserve(budget):
                with self.store.connect() as c:blocked=self._reservation_blocker(c,budget)
                return {'blocked':blocked or 'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED','attempted':False}
            try:
                result=rpc(method,params)
                record={'method':method,'params':params,'result':result}
                if bank:
                    if type(result) is not int or result<0:raise ValueError('Snapshot block time unavailable')
                else:validate_bank(mint,accounts,record)
                # Publish raw bank evidence and its immutable head atomically.
                # A crash cannot leave a chosen cutoff with a missing head.
                import zlib
                raw=canonical(record).encode();key=digest(record)
                if len(raw)>16*1024*1024:raise ValueError('Evidence page exceeds size budget')
                compressed=zlib.compress(raw)
                with self.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    try:
                        if not c.execute('SELECT 1 FROM pages WHERE hash=?',(key,)).fetchone():
                            used=c.execute('SELECT COALESCE(SUM(length(payload)),0) FROM pages').fetchone()[0]
                            if used+len(compressed)>self.store.max_bytes:raise ValueError('Evidence storage budget exhausted')
                            c.execute('INSERT INTO pages VALUES(?,?,?)',(key,compressed,len(raw)))
                        if bank:c.execute('UPDATE ownership_banks SET clock_hash=? WHERE budget=? AND clock_hash IS NULL',(key,budget))
                        else:c.execute('INSERT INTO ownership_banks VALUES(?,?,NULL)',(budget,key))
                        c.commit()
                    except BaseException:
                        c.rollback();raise
            except (ValueError,KeyError,TypeError,IndexError,OSError):
                return {'blocked':'PROVIDER_RETRY_REQUIRED','attempted':True}
            return {**self.bank(budget),'attempted':True}

    def seed(self,budget,coverage):
        # Recompute every summary field from the stored request/response chain.
        replay_history(coverage,self.store)
        query={k:coverage[k] for k in ('address','start','end','token_accounts_filter')}
        if 'slot_range' in coverage:query['slot_range']=coverage['slot_range']
        if 'page_size' in coverage:query['page_size']=coverage['page_size']
        key=digest({'budget':budget,'query':query})
        with self.store.connect() as c:
            if not c.execute('SELECT 1 FROM ownership_budgets WHERE id=?',(budget,)).fetchone():raise ValueError('Budget missing')
            c.execute('INSERT OR IGNORE INTO ownership_history(id,budget,query,coverage,status) VALUES(?,?,?,?,?)',
                      (key,budget,canonical(query),canonical(coverage),'DONE' if coverage['query_range_exhausted'] else 'PENDING'))
        return key
    def create(self,budget,owner,start,end,slot_range=None,*,page_size=100):
        from .programs import address
        address(owner)
        if type(start) is not int or type(end) is not int or not 0<=start<end:raise ValueError('Invalid history window')
        if type(page_size) is not int or not 1<=page_size<=100:raise ValueError('Invalid history page size')
        query={'address':owner,'start':start,'end':end,'token_accounts_filter':'none'}
        if page_size!=100:query['page_size']=page_size
        if slot_range is not None:
            if set(slot_range)!={'gte','lt'} or any(type(v) is not int for v in slot_range.values()) or not 0<=slot_range['gte']<slot_range['lt']:raise ValueError('Invalid slot range')
            query['slot_range']=slot_range
        key=digest({'budget':budget,'query':query})
        with self.store.connect() as c:
            if not c.execute('SELECT 1 FROM ownership_budgets WHERE id=?',(budget,)).fetchone():raise ValueError('Budget missing')
            c.execute('INSERT OR IGNORE INTO ownership_history(id,budget,query,coverage,status) VALUES(?,?,?,?,?)',
                      (key,budget,canonical(query),None,'PENDING'))
        return key
    def snapshot(self,key):
        with self.store.connect() as c:
            row=c.execute('SELECT h.query,h.coverage,h.status,h.attempts,b.used,b.ceiling FROM ownership_history h JOIN ownership_budgets b ON b.id=h.budget WHERE h.id=?',(key,)).fetchone()
        if not row:raise ValueError('History job missing')
        return {'id':key,'query':json.loads(row[0]),'coverage':json.loads(row[1]) if row[1] else None,
                'status':row[2],'attempts':row[3],'requests_used':row[4],'request_ceiling':row[5]}
    def advance(self,key,rpc):
        # One process lock per evidence store. Concurrent processes fail quickly;
        # process death releases the lock, while the reserved request stays spent.
        import fcntl
        with open(ownership_lock_path(self.store),'a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {**self.snapshot(key),'busy':True}
            before=self.snapshot(key);coverage=before['coverage'];query=before['query']
            if coverage is not None:replay_history(coverage,self.store)
            if before['status']=='DONE':return before
            if coverage is not None and 'HISTORY_CURSOR_CYCLE' in coverage['reasons']:
                return {**before,'blocked':'HISTORY_CURSOR_CYCLE'}
            pages=coverage['pages'] if coverage is not None else []
            if len(pages)>=20:return {**before,'blocked':'HISTORY_PAGE_LIMIT'}
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                budget=c.execute('SELECT budget FROM ownership_history WHERE id=?',(key,)).fetchone()[0]
                blocked=self._reservation_blocker(c,budget)
                if blocked:c.rollback();return {**before,'blocked':blocked}
                if not c.execute('UPDATE ownership_budgets SET used=used+1 WHERE id=? AND used<ceiling',(budget,)).rowcount:
                    c.rollback();return {**before,'blocked':'INVESTIGATION_REQUEST_BUDGET_EXHAUSTED'}
                c.execute("UPDATE ownership_history SET attempts=attempts+1,status='PENDING' WHERE id=?",(key,));c.commit()
            index=0
            def provider(method,params):
                nonlocal index
                position=index;index+=1
                if position<len(pages):
                    page=pages[position];manifest=self.store.load(page['request_evidence_hash'])
                    if manifest['method']!=method or manifest['params']!=params:raise ValueError('Cached request mismatch')
                    return self.store.load(page['payload_hash'])
                if position!=len(pages):raise ValueError('Continuation request bound exceeded')
                return rpc(method,params)
            try:
                _,updated=collect_history(query['address'],query['start'],query['end'],provider,
                    max_pages=len(pages)+1,capture=self.store.save,token_accounts=query['token_accounts_filter'],slot_range=query.get('slot_range'),page_size=query.get('page_size',100))
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_history SET coverage=?,status=? WHERE id=?',
                        (canonical(updated),'DONE' if updated['query_range_exhausted'] else 'PENDING',key))
            except (ValueError,KeyError,TypeError,IndexError,OSError):
                # Never save provider error bodies or pretend a failed page advanced.
                with self.store.connect() as c:c.execute("UPDATE ownership_history SET status='RETRYABLE_ERROR' WHERE id=?",(key,))
            return self.snapshot(key)
