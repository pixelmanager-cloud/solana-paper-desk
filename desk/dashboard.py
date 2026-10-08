"""Loopback-only research UI, persisted bounded job queue, no transaction endpoint."""
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .dashboard_diagnostics import DashboardDiagnostics
from .screen import screen
from .job_persistence import JobPersistence, BIRTH_ACQUISITION_V1


class Jobs:
    def __init__(self, db, scanner=screen):
        self.persistence = JobPersistence(db)
        self.db, self.scanner = str(self.persistence.path), scanner
        self.persistence.recover()
        self.stopping = threading.Event()
    def connect(self):
        return self.persistence.connect()
    def submit(self,mint):
        return self.persistence.admit(mint)
    def submit_acquisition(self,mint,evidence_db):
        """Admission only; no acquisition executor, CLI or request budget here."""
        return self.persistence.admit(mint,kind=BIRTH_ACQUISITION_V1,evidence_db=evidence_db)
    def descriptor(self,scan_id):
        return self.persistence.descriptor(scan_id)
    def list(self):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM scans ORDER BY created DESC,rowid DESC LIMIT 50').fetchall()
            return [{**dict(r),'result':json.loads(r['result']) if r['result'] else None} for r in rows]
    def once(self):
        with self.persistence.worker() as worker:
            if worker is None:return False
            claim=worker.claim_screen()
            if claim is None:return False
            try:
                result=self.scanner(claim.mint); status='COMPLETE'
            except Exception as exc:
                # Never surface provider exceptions, credential paths or response bodies.
                result={'error':'Scan failed; check provider access and retry within the daily budget.',
                        'error_type':type(exc).__name__,'eligible_for_trading':False};status='FAILED'
            worker.publish(claim,status,result)
            return True
    def run(self):
        while not self.stopping.is_set():
            if not self.once():self.stopping.wait(1)


def handler(jobs,port,*,evidence_db=None):
    diagnostics = DashboardDiagnostics(jobs.db, evidence_db if evidence_db is not None
                                       else Path(jobs.db).parent/'evidence.sqlite')
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
            if self.path.split('?',1)[0]=='/api/evidence-diagnostics':
                try:
                    if len(self.path)>512:raise ValueError('Oversized query')
                    parsed=urlsplit(self.path)
                    if parsed.fragment:raise ValueError('Invalid query')
                    query=parse_qs(parsed.query,keep_blank_values=True,
                                   strict_parsing=True,max_num_fields=1)
                    if set(query)!={'scan_id'} or len(query['scan_id'])!=1:
                        raise ValueError('Only scan ID is accepted')
                except ValueError:
                    return self.respond(400,{'status':'UNAVAILABLE','decision':'REJECT',
                        'eligible_for_trading':False,'diagnostics':None,'reasons':['INVALID_DIAGNOSTIC_QUERY']})
                status,body=diagnostics.inspect(query['scan_id'][0])
                return self.respond(status,body)
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
    server=ThreadingHTTPServer(('127.0.0.1',port),handler(jobs,port,evidence_db=evidence.path))
    worker=threading.Thread(target=jobs.run,daemon=True);worker.start()
    try:server.serve_forever()
    finally:jobs.stopping.set();server.server_close()
