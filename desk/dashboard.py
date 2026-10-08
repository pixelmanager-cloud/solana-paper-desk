"""Loopback-only research UI, persisted bounded job queue, no transaction endpoint."""
import json
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .programs import address
from .screen import screen


class Jobs:
    def __init__(self, db, scanner=screen):
        self.db, self.scanner = str(db), scanner
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY,mint TEXT,created INTEGER,status TEXT,result TEXT)')
            c.execute("UPDATE scans SET status='INTERRUPTED' WHERE status='RUNNING'")
        self.stopping = threading.Event()
    def connect(self):
        c = sqlite3.connect(self.db,timeout=15); c.row_factory=sqlite3.Row
        return c
    def submit(self,mint):
        address(mint)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT count(*) FROM scans WHERE status IN ('QUEUED','RUNNING')").fetchone()[0]>=3:
                raise ValueError('Queue full. Wait for the current scans to finish.')
            if c.execute('SELECT count(*) FROM scans WHERE created>?',(int(time.time())-86400,)).fetchone()[0]>=10:
                raise ValueError('Daily budget reached: 10 scans per rolling 24 hours.')
            if c.execute("SELECT 1 FROM scans WHERE mint=? AND status IN ('QUEUED','RUNNING')",(mint,)).fetchone():
                raise ValueError('This token already has a pending scan.')
            uid=uuid.uuid4().hex
            c.execute('INSERT INTO scans VALUES(?,?,?,?,?)',(uid,mint,int(time.time()),'QUEUED',None))
            return uid
    def list(self):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM scans ORDER BY created DESC,rowid DESC LIMIT 50').fetchall()
            return [{**dict(r),'result':json.loads(r['result']) if r['result'] else None} for r in rows]
    def once(self):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT * FROM scans WHERE status='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
            if not row:return False
            c.execute("UPDATE scans SET status='RUNNING' WHERE id=?",(row['id'],))
        try:
            result=self.scanner(row['mint']); status='COMPLETE'
        except Exception as exc:
            # Never surface provider exceptions, credential paths or response bodies.
            result={'error':'Scan failed; check provider access and retry within the daily budget.',
                    'error_type':type(exc).__name__,'eligible_for_trading':False};status='FAILED'
        with self.connect() as c:
            c.execute('UPDATE scans SET status=?,result=? WHERE id=?',(status,json.dumps(result),row['id']))
        return True
    def run(self):
        while not self.stopping.is_set():
            if not self.once():self.stopping.wait(1)


def handler(jobs,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,status,data,ctype='application/json'):
            body=data if isinstance(data,bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type',ctype)
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers();self.wfile.write(body)
        def trusted(self):
            allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
            return self.headers.get('Host') in allowed and self.headers.get('Origin') in (None,*('http://'+h for h in allowed))
        def do_GET(self):
            if not self.trusted():return self.respond(403,{'error':'Local access only'})
            if self.path=='/api/paper':
                from .paper_view import paper_status
                return self.respond(200,paper_status(Path(jobs.db).parent/'active-paper.sqlite'))
            if self.path=='/api/decisions':
                from .decision_runner import recent_decisions
                return self.respond(200,recent_decisions(Path(jobs.db).parent/'paper-decisions.sqlite'))
            if self.path=='/api/scans':return self.respond(200,jobs.list())
            if self.path=='/api/launches':
                from .decode import decode
                path=Path(jobs.db).parent/'launches.sqlite'
                launches=[]
                if path.exists():
                    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
                    try:
                        for received,payload in c.execute('SELECT received_at,payload FROM raw_events ORDER BY seq DESC LIMIT 30'):
                            try:
                                observation=decode(json.loads(payload))
                                for ix in observation['program_observations']:
                                    if ix.get('status')=='IDENTIFIED' and ix.get('kind')=='LAUNCH':
                                        launches.append({'mint':ix['mint'],'wallet':ix['wallet'],'slot':observation['slot'],
                                            'signature':observation['signature'],'received_at':received})
                            except (ValueError,KeyError,TypeError):continue
                    finally:c.close()
                return self.respond(200,{'launches':launches,'coverage':'Periodic bounded sample; not all launches'})
            if self.path=='/api/status':return self.respond(200,{'mode':'RESEARCH_ONLY','live_trading':False,
                'max_scans_per_day':10,'max_rpc_calls_per_scan':18,'automatic_entry_enabled':False})
            files={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if self.path not in files:return self.respond(404,{'error':'Not found'})
            name=files[self.path];ctype={'html':'text/html; charset=utf-8','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]
            self.respond(200,(Path(__file__).parent/'static'/name).read_bytes(),ctype)
        def do_POST(self):
            if not self.trusted() or self.headers.get('X-Desk-Request')!='1':return self.respond(403,{'error':'Local request required'})
            if self.path!='/api/scans':return self.respond(404,{'error':'Not found'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=512:raise ValueError('Invalid request size')
                value=json.loads(self.rfile.read(length))
                if not isinstance(value,dict):raise ValueError('Expected a token mint')
                uid=jobs.submit(value.get('mint'))
                self.respond(202,{'id':uid})
            except (ValueError,TypeError) as exc:self.respond(400,{'error':str(exc)})
    return Handler


def serve(db,port=8765):
    if not 1024<=port<=65535:raise ValueError('Invalid port')
    from .evidence import EvidenceStore
    evidence=EvidenceStore(Path(db).parent/'evidence.sqlite')
    jobs=Jobs(db,scanner=lambda mint:screen(mint,history_capture=evidence.save))
    server=ThreadingHTTPServer(('127.0.0.1',port),handler(jobs,port))
    worker=threading.Thread(target=jobs.run,daemon=True);worker.start()
    try:server.serve_forever()
    finally:jobs.stopping.set();server.server_close()
