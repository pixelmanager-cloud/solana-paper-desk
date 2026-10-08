"""Durable, bounded history continuation. Provider attempts are charged before I/O."""
import json
from .model import canonical,digest
from .history import collect_history
from .replay_history import replay_history


class HistoryProgress:
    def __init__(self,store):
        if store.read_only:raise ValueError('Writable evidence store required')
        self.store=store
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ownership_budgets(id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,used INTEGER NOT NULL,ceiling INTEGER NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS ownership_history(id TEXT PRIMARY KEY,budget TEXT NOT NULL,query TEXT NOT NULL,coverage TEXT,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0)')
    def budget(self,identity,source_hash,used,ceiling=18):
        if not isinstance(identity,str) or not identity or not isinstance(source_hash,str) or len(source_hash)!=64:
            raise ValueError('Immutable investigation identity required')
        if type(used) is not int or type(ceiling) is not int or not 0<=used<=ceiling<=18:raise ValueError('Invalid request budget')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT source_hash,ceiling FROM ownership_budgets WHERE id=?',(identity,)).fetchone()
            if old and old!=(source_hash,ceiling):c.rollback();raise ValueError('Investigation identity changed')
            c.execute('INSERT OR IGNORE INTO ownership_budgets VALUES(?,?,?,?)',(identity,source_hash,used,ceiling));c.commit()
    def seed(self,budget,coverage):
        # Recompute every summary field from the stored request/response chain.
        replay_history(coverage,self.store)
        query={k:coverage[k] for k in ('address','start','end','token_accounts_filter')}
        if 'slot_range' in coverage:query['slot_range']=coverage['slot_range']
        key=digest({'budget':budget,'query':query})
        with self.store.connect() as c:
            if not c.execute('SELECT 1 FROM ownership_budgets WHERE id=?',(budget,)).fetchone():raise ValueError('Budget missing')
            c.execute('INSERT OR IGNORE INTO ownership_history(id,budget,query,coverage,status) VALUES(?,?,?,?,?)',
                      (key,budget,canonical(query),canonical(coverage),'DONE' if coverage['query_range_exhausted'] else 'PENDING'))
        return key
    def create(self,budget,owner,start,end,slot_range=None):
        from .programs import address
        address(owner)
        if type(start) is not int or type(end) is not int or not 0<=start<end:raise ValueError('Invalid history window')
        query={'address':owner,'start':start,'end':end,'token_accounts_filter':'none'}
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
        with open(str(self.store.path)+'.ownership.lock','a') as lock:
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
                    max_pages=len(pages)+1,capture=self.store.save,token_accounts=query['token_accounts_filter'],slot_range=query.get('slot_range'))
                with self.store.connect() as c:
                    c.execute('UPDATE ownership_history SET coverage=?,status=? WHERE id=?',
                        (canonical(updated),'DONE' if updated['query_range_exhausted'] else 'PENDING',key))
            except (ValueError,KeyError,TypeError,IndexError,OSError):
                # Never save provider error bodies or pretend a failed page advanced.
                with self.store.connect() as c:c.execute("UPDATE ownership_history SET status='RETRYABLE_ERROR' WHERE id=?",(key,))
            return self.snapshot(key)
