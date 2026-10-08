"""Durable evidence-gated investigation consumer; no fills invented."""
import json,sqlite3,time
from pathlib import Path
from .model import canonical,digest

_JOURNAL_INIT_TIMEOUT = 5.0


def _initialize_journal(c):
    """Bound WAL/schema contention before the normal writer transaction.

    SQLite's journal-mode switch can return BUSY immediately despite the
    connection timeout. Retry only BUSY/LOCKED, with a shared monotonic deadline
    and short SQLite busy waits. Schema creation is one serialized transaction.
    """
    deadline = time.monotonic() + _JOURNAL_INIT_TIMEOUT
    try:
        for attempt in range(50):
            remaining = max(0, deadline - time.monotonic())
            c.execute(f'PRAGMA busy_timeout={min(100, int(remaining * 1000))}')
            try:
                mode = c.execute('PRAGMA journal_mode=WAL').fetchone()
                if mode is None or mode[0] != 'wal':
                    raise sqlite3.OperationalError('Decision journal WAL unavailable')
                c.execute('PRAGMA synchronous=FULL')
                c.execute('BEGIN IMMEDIATE')
                c.execute('CREATE TABLE IF NOT EXISTS decisions(scan_id TEXT PRIMARY KEY,source_hash TEXT NOT NULL,source_payload TEXT NOT NULL,decision TEXT NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS decision_evaluations(scan_id TEXT NOT NULL,policy TEXT NOT NULL,source_hash TEXT NOT NULL,source_payload TEXT NOT NULL,decision TEXT NOT NULL,PRIMARY KEY(scan_id,policy))')
                c.execute('COMMIT')
                return
            except sqlite3.Error as error:
                if c.in_transaction:
                    c.execute('ROLLBACK')
                code = getattr(error, 'sqlite_errorcode', 0) & 0xff
                remaining = deadline - time.monotonic()
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or remaining <= 0 or attempt == 49:
                    raise
                time.sleep(min(0.025, remaining))
    finally:
        # Preserve the existing bounded writer timeout for the consumer batch.
        c.execute('PRAGMA busy_timeout=15000')


def assess(scan,now,store=None,progress=None):
    report=json.loads(scan['result']) if scan['result'] else {}
    reasons=[]
    if scan['status']!='COMPLETE':reasons.append('INVESTIGATION_'+scan['status'])
    if not isinstance(report,dict):raise ValueError('Investigation result is not an object')
    if report.get('mint')!=scan['mint']:reasons.append('INVESTIGATION_MINT_UNVERIFIED')
    observed=report.get('observed_at')
    if type(observed) is not int or not 0<=now-observed<=10:reasons.append('INVESTIGATION_NOT_FRESH_FOR_ENTRY')
    for name in ('findings','unknowns'):
        values=report.get(name,[])
        if not isinstance(values,list) or any(not isinstance(v,str) for v in values):raise ValueError('Invalid investigation reasons')
        reasons.extend(values)
    from .entry_evidence import evaluate,POLICY
    evidence=evaluate(report,store,scan=scan,progress=progress)
    continued=evidence['continuation_evidence']
    if 'progress_hash' in continued:
        evidence['ownership_progress_hash']=continued['progress_hash']
        evidence['ownership_requests_used']=continued['requests_used']
    if 'history' in continued:
        evidence['continued_ownership_history']=continued['history']
    reasons.extend(evidence['reasons'])
    return {'kind':'paper_candidate_decision','policy':POLICY,'entry_evidence':evidence,'scan_id':scan['id'],'mint':scan['mint'],
        'evaluated_at':now,'observed_at':observed,'source_hash':digest(dict(scan)),'decision':'REJECT',
        'eligible_for_trading':False,'reasons':sorted(set(reasons)),
        'notice':'Persisted evidence gates evaluated. Unverified bundle, strategy or route inputs block entry; no fill is created.'}


def consume(source,destination,*,now=None,limit=20,evidence_db=None):
    now=int(time.time()) if now is None else now
    if type(now) is not int or now<0 or type(limit) is not int or not 1<=limit<=50:raise ValueError('Invalid consumer bounds')
    from .evidence import EvidenceStore
    from .entry_evidence import POLICY
    store=EvidenceStore(evidence_db,read_only=True) if evidence_db and Path(evidence_db).is_file() else None
    source=Path(source).resolve();destination=Path(destination).resolve()
    if not source.is_file():raise ValueError('Research database missing')
    if source==destination:raise ValueError('Decision journal must be separate from research')
    destination.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(destination.as_uri(),uri=True,timeout=15,isolation_level=None);c.row_factory=sqlite3.Row
    try:
        _initialize_journal(c)
        c.execute('ATTACH DATABASE ? AS research',(source.as_uri()+'?mode=ro',))
        c.execute('BEGIN IMMEDIATE')
        try:
            # Do not advance a rowid watermark over queued/running work. An
            # earlier row may finish after a later row was already consumed.
            # First consume previously unseen scans, then revised ownership
            # evidence for a bounded recent window. Never discard older decisions.
            rows=c.execute("SELECT s.id,s.mint,s.created,s.status,s.result FROM research.scans s LEFT JOIN main.decision_evaluations d ON d.scan_id=s.id AND (d.policy=? OR d.policy LIKE ?) WHERE s.status IN ('COMPLETE','FAILED','INTERRUPTED') AND d.scan_id IS NULL ORDER BY s.rowid LIMIT ?",(POLICY,POLICY+':%',limit)).fetchall()
            recent=c.execute("SELECT id,mint,created,status,result FROM research.scans WHERE status='COMPLETE' ORDER BY rowid DESC LIMIT 50").fetchall()
            seen={r['id'] for r in rows};rows+= [r for r in recent if r['id'] not in seen]
            output=[]
            for row in rows:
                source_payload=canonical(dict(row))
                if len(source_payload.encode())>2*1024*1024:raise ValueError('Investigation exceeds consumer size budget')
                decision=assess(row,now,store)
                key=decision['entry_evidence']['continuation_evidence'].get('revision_hash')
                revision=POLICY+(':'+key if key else '')
                if c.execute('SELECT 1 FROM decision_evaluations WHERE scan_id=? AND policy=?',(row['id'],revision)).fetchone():continue
                c.execute('INSERT INTO decision_evaluations VALUES(?,?,?,?,?)',(row['id'],revision,decision['source_hash'],source_payload,canonical(decision)))
                c.execute('INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)',(row['id'],decision['source_hash'],source_payload,canonical(decision)))
                output.append(decision)
                if len(output)>=limit:break
            c.execute('COMMIT')
        except BaseException:c.execute('ROLLBACK');raise
        return {'mode':'REJECT_ONLY','consumed':len(output),'decisions':output,'automatic_entry_enabled':False}
    finally:c.close()


def recent_decisions(path):
    """Read the bounded, current-policy entry-gate journal without creating it."""
    from .entry_evidence import POLICY
    path=Path(path)
    if not path.is_file():return {'status':'NOT_CONFIGURED','decisions':[]}
    c=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)
    try:
        rows=c.execute('SELECT decision FROM decision_evaluations WHERE rowid IN (SELECT MAX(rowid) FROM decision_evaluations WHERE policy=? OR policy LIKE ? GROUP BY scan_id) ORDER BY rowid DESC LIMIT 20',(POLICY,POLICY+':%')).fetchall()
        return {'status':'EVIDENCE_GATES_CONNECTED','automatic_entry_enabled':False,'decisions':[json.loads(row[0]) for row in rows]}
    except (sqlite3.Error,ValueError):return {'status':'UNAVAILABLE','decisions':[]}
    finally:c.close()
