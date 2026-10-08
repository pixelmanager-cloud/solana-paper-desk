"""Reconstruct finalized history from request-bound saved pages, without RPC."""
from .history import collect_history
from .model import digest
from .account_history import account_inventory
from .launch import launch_anchor
from .reconcile import reconcile_movements


def replay_history(coverage, store):
    if not isinstance(coverage,dict):raise ValueError('History coverage required')
    copy=dict(coverage);claimed=copy.pop('evidence_hash',None)
    if claimed!=digest(copy):raise ValueError('History coverage hash mismatch')
    pages=coverage['pages']
    if not isinstance(pages,list) or not 1<=len(pages)<=20:raise ValueError('History replay page budget')
    position=0
    def rpc(method,params):
        nonlocal position
        if position>=len(pages):raise ValueError('Replay exceeded recorded pages')
        page=pages[position];position+=1
        manifest=store.load(page['request_evidence_hash'])
        if (manifest.get('kind')!='history_request_v1' or manifest.get('method')!=method
                or manifest.get('params')!=params or manifest.get('response_hash')!=page['payload_hash']):
            raise ValueError('History request binding mismatch')
        return store.load(page['payload_hash'])
    observations,rebuilt=collect_history(coverage['address'],coverage['start'],coverage['end'],rpc,
        max_pages=len(pages),capture=digest,token_accounts=coverage['token_accounts_filter'],slot_range=coverage.get('slot_range'))
    if position!=len(pages) or rebuilt!=coverage:raise ValueError('History coverage replay mismatch')
    return observations,rebuilt


def reconstruct_launch_history(report,store):
    """Launch and observed movements, explicitly separate from complete transfers."""
    mint=report['mint'];queries=report.get('history_queries',[])
    if not isinstance(queries,list) or len(queries)>18:raise ValueError('History query budget')
    if sum(len(q.get('pages',[])) for q in queries)>18:raise ValueError('History total page budget')
    matches=[q for q in queries if q.get('address')==mint and q.get('token_accounts_filter')=='none']
    if len(matches)!=1:raise ValueError('One mint discovery query required')
    observations,coverage=replay_history(matches[0],store)
    inventory=account_inventory(mint,observations,coverage)
    anchors=[]
    for observation in observations:
        if any(x.get('mint')==mint for x in observation.get('mint_initializations',[])):
            anchors.append(launch_anchor(observation,mint))
    verified=[a for a in anchors if a['verified']]
    reasons=[]
    if len(verified)!=1:reasons.append('LAUNCH_ANCHOR_UNVERIFIED')
    merged={o['signature']:o for o in observations};conflicts=set();account_reasons=[];verified_accounts=[]
    frontier={row['address'] for row in inventory['accounts']}
    for account in sorted(frontier):
        candidates=[q for q in queries if q.get('address')==account and q.get('token_accounts_filter')=='none'
                    and q.get('start')==coverage['start'] and q.get('end')==coverage['end'] and q.get('slot_range')==coverage.get('slot_range')]
        if len(candidates)!=1:
            account_reasons.append('ACCOUNT_HISTORY_QUERY_MISSING_OR_AMBIGUOUS');continue
        try:
            rows,account_coverage=replay_history(candidates[0],store)
        except (ValueError,KeyError,TypeError,IndexError):
            account_reasons.append('ACCOUNT_HISTORY_RAW_REPLAY_FAILED');continue
        if account_coverage['query_coverage_verified']:verified_accounts.append(account)
        else:account_reasons.extend(account_coverage['reasons'])
        for row in rows:
            signature=row['signature']
            if signature in conflicts:continue
            if signature in merged and merged[signature]['payload_hash']!=row['payload_hash']:
                conflicts.add(signature);merged.pop(signature);account_reasons.append('ACCOUNT_HISTORY_CONFLICTING_TRANSACTION');continue
            merged[signature]=row
            touched={r['account'] for r in row.get('token_deltas',[]) if r['mint']==mint}
            touched|={r['account'] for r in row.get('token_account_initializations',[]) if r['mint']==mint}
            if touched-frontier:account_reasons.append('HISTORICAL_ACCOUNT_FRONTIER_NOT_CLOSED')
    combined=list(merged.values())
    from .ordering import collect_ordering
    from .continuity import reconcile_history
    proofs=report.get('account_history',{}).get('block_ordering',{}).get('proofs',[])
    if not isinstance(proofs,list) or len(proofs)>2:raise ValueError('Ordering proof budget')
    def block_rpc(method,params):
        for proof in proofs:
            saved=store.load(proof['evidence_hash'])
            if method==saved['method'] and params==[saved['slot'],saved['config']]:return saved['result']
        raise ValueError('Block order evidence unavailable')
    ordering=collect_ordering(mint,combined,block_rpc,capture=digest)
    continuity=reconcile_history(mint,combined,ordering=ordering)
    movements=[{'signature':o['signature'],**reconcile_movements(mint,o)} for o in combined]
    relevant=[m for m in movements if m['accounts'] or m['reasons']]
    funding=None
    if len(verified)==1:
        from .ownership_signals import funding_groups
        funding=funding_groups(mint,combined,queries,store,ordering,verified[0]['slot'])
    return {'launch_verified':len(verified)==1,'launch_reasons':reasons,'anchors':anchors,
            'query_coverage_verified':coverage['query_coverage_verified'],
            'query_reasons':coverage['reasons'],'history_start':coverage['start'],'history_end':coverage['end'],
            'coverage_hash':coverage['evidence_hash'],'slot_range':coverage.get('slot_range'),
            'page_hashes':[p['payload_hash'] for p in coverage['pages']],
            'request_hashes':[p['request_evidence_hash'] for p in coverage['pages']],
            'inventory':inventory,'observed_transaction_count':len(combined),'funding':funding,
            'account_queries':{'required':len(frontier),'verified':len(verified_accounts),
                              'unverified_accounts':sorted(frontier-set(verified_accounts)),
                              'reasons':sorted(set(account_reasons))},
            'block_ordering':ordering,'account_continuity':continuity,
            'observed_movements':{'passed':bool(relevant) and all(m['passed'] for m in relevant),
                                  'transaction_count':len(relevant),'failures':[m for m in relevant if not m['passed']]},
            'transfer_history_complete':False,'eligible_for_trading':False}
