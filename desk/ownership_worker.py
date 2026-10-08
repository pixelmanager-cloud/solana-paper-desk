"""Advance one ownership investigation within its original RPC allowance."""
import json,sqlite3
from pathlib import Path
from .model import digest
from .evidence import EvidenceStore
from .history_progress import HistoryProgress
from .replay_history import replay_history,reconstruct_launch_history
from .security import mint_policy


def advance(source,evidence_db,scan_id,rpc,*,max_calls=2):
    if type(max_calls) is not int or not 1<=max_calls<=4:raise ValueError('One to four continuation requests allowed')
    with sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory=sqlite3.Row
        row=c.execute("SELECT id,mint,created,status,result FROM scans WHERE id=? AND status='COMPLETE'",(scan_id,)).fetchone()
    if row is None:raise ValueError('Completed scan required')
    report=json.loads(row['result']);summary=dict(report);claimed=summary.pop('report_hash',None)
    if claimed!=digest(summary) or report.get('mint')!=row['mint']:raise ValueError('Immutable source report invalid')
    calls=report.get('calls')
    if type(calls) is not int or not 0<=calls<=18:raise ValueError('Original request usage missing')
    store=EvidenceStore(evidence_db);progress=HistoryProgress(store)
    mint_record=store.load(report['mint_evidence_hash'])
    if mint_record['method']!='getAccountInfo' or mint_record['params']!=[row['mint'],{'encoding':'base64','commitment':'confirmed'}]:raise ValueError('Mint evidence mismatch')
    policy=mint_policy(mint_record['result']['value'])
    if policy['decision']!='PASS_TOKEN_POLICY':
        return {'scan_id':scan_id,'status':'UNSUPPORTED_TOKEN','reasons':policy['reasons'],'provider_calls':0,'eligible_for_trading':False}
    progress.budget(scan_id,digest(dict(row)),calls)
    queries=report.get('history_queries',[])
    mint_queries=[q for q in queries if q.get('address')==row['mint'] and q.get('token_accounts_filter')=='none']
    if len(mint_queries)!=1:raise ValueError('One request-bound mint query required')
    mint_key=progress.seed(scan_id,mint_queries[0]);spent=0;blocked=None
    while spent<max_calls:
        current=progress.snapshot(mint_key)
        if current['status']!='DONE':
            result=progress.advance(mint_key,rpc)
            if result.get('blocked') or result.get('busy'):blocked=result.get('blocked','BUSY');break
            spent+=1
            if result['status']=='RETRYABLE_ERROR':blocked='PROVIDER_RETRY_REQUIRED';break
            continue
        observations,coverage=replay_history(current['coverage'],store)
        from .account_history import account_inventory
        inventory=account_inventory(row['mint'],observations,coverage)
        if not inventory['initialization_inventory_verified']:blocked='ACCOUNT_INVENTORY_UNVERIFIED';break
        keys=[]
        for account in inventory['accounts']:
            matches=[q for q in queries if q.get('address')==account['address'] and q.get('start')==coverage['start'] and q.get('end')==coverage['end'] and q.get('token_accounts_filter')=='none' and q.get('slot_range')==coverage.get('slot_range')]
            if len(matches)>1:raise ValueError('Ambiguous account query')
            key=progress.seed(scan_id,matches[0]) if matches else progress.create(scan_id,account['address'],coverage['start'],coverage['end'],coverage.get('slot_range'))
            keys.append(key)
        pending=[key for key in keys if progress.snapshot(key)['status']!='DONE']
        if not pending:break
        result=progress.advance(pending[0],rpc)
        if result.get('blocked') or result.get('busy'):blocked=result.get('blocked','BUSY');break
        spent+=1
        if result['status']=='RETRYABLE_ERROR':blocked='PROVIDER_RETRY_REQUIRED';break
    with store.connect() as c:
        saved=[json.loads(r[0]) for r in c.execute('SELECT coverage FROM ownership_history WHERE budget=? AND coverage IS NOT NULL ORDER BY id',(scan_id,))]
    # Preserve funding queries, replacing only exactly matching resumed queries.
    identities={(q['address'],q['start'],q['end'],q['token_accounts_filter']) for q in saved}
    combined=[q for q in queries if (q['address'],q['start'],q['end'],q['token_accounts_filter']) not in identities]+saved
    updated={**report,'history_queries':combined}
    result={'scan_id':scan_id,'source_report_hash':claimed,'provider_calls':spent,
            'requests_used':progress.snapshot(mint_key)['requests_used'],'status':blocked or 'HISTORY_REPLAYED',
            'history':reconstruct_launch_history(updated,store),'eligible_for_trading':False}
    key=store.save({'kind':'ownership_progress_v1','source_hash':digest(dict(row)),'history_queries':combined,**result})
    with store.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS ownership_heads(scan_id TEXT PRIMARY KEY,evidence_hash TEXT NOT NULL)')
        c.execute('INSERT INTO ownership_heads VALUES(?,?) ON CONFLICT(scan_id) DO UPDATE SET evidence_hash=excluded.evidence_hash',(scan_id,key))
    return {**result,'evidence_hash':key}


def saved_progress(store,scan):
    """Resolve progress only for the exact immutable source investigation."""
    if store is None:return None
    with store.connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ownership_heads'").fetchone():return None
        row=c.execute('SELECT evidence_hash FROM ownership_heads WHERE scan_id=?',(scan['id'],)).fetchone()
    if not row:return None
    record=store.load(row[0])
    if record.get('kind')!='ownership_progress_v1' or record.get('source_hash')!=digest(dict(scan)):
        raise ValueError('Ownership progress source mismatch')
    return {**record,'evidence_hash':row[0]}
