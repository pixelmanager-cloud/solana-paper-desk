"""Bounded finalized address history with explicit, non-promotable coverage."""
from .decode import decode
from .programs import address
from .model import digest


def collect_history(owner,start,end,rpc,max_pages=2,capture=None,token_accounts="balanceChanged",slot_range=None,*,page_size=100):
    address(owner)
    if type(start) is not int or type(end) is not int or not 0<=start<end or not 1<=max_pages<=20:
        raise ValueError('Invalid history bounds')
    if type(page_size) is not int or not 1<=page_size<=100:raise ValueError('Invalid history page size')
    if token_accounts not in ("none","balanceChanged","all"):raise ValueError("Invalid token account scope")
    if slot_range is not None and (not isinstance(slot_range,dict) or set(slot_range)!={'gte','lt'}
            or any(type(v) is not int for v in slot_range.values()) or not 0<=slot_range['gte']<slot_range['lt']):
        raise ValueError('Invalid history slot bounds')
    observations=[];seen={};cursors=set();cursor=None;reasons=[];pages=[];last_slot=None;exhausted=False
    for _ in range(max_pages):
        opts={'transactionDetails':'full','sortOrder':'asc','limit':page_size,'commitment':'finalized',
              'encoding':'jsonParsed','maxSupportedTransactionVersion':1,
              'filters':{'blockTime':{'gte':start,'lt':end},'status':'any','tokenAccounts':token_accounts}}
        if slot_range is not None:
            opts['filters'].pop('blockTime');opts['filters']['slot']=dict(slot_range)
        if cursor:opts['paginationToken']=cursor
        params=[owner,opts]
        response=rpc('getTransactionsForAddress',params)
        rows=response.get('data')
        if not isinstance(rows,list) or len(rows)>page_size:raise ValueError('Invalid history page')
        page_hash=digest(response)
        persisted=capture(response)==page_hash if capture else False
        if capture and not persisted:raise ValueError('Persisted history hash mismatch')
        page={'request_cursor':cursor,'payload_hash':page_hash,'records':len(rows),'persisted':persisted}
        if capture:
            manifest={'kind':'history_request_v1','method':'getTransactionsForAddress','params':params,'response_hash':page_hash}
            key=capture(manifest)
            if key!=digest(manifest):raise ValueError('Persisted history request hash mismatch')
            page['request_evidence_hash']=key
        pages.append(page)
        for raw in rows:
            try:obs=decode(raw)
            except (ValueError,KeyError,TypeError,IndexError):
                reasons.append('HISTORY_DECODE_GAP');continue
            signature=obs['signature'];fingerprint=digest(raw)
            if signature in seen:
                reasons.append('HISTORY_DUPLICATE_RECORD' if seen[signature]==fingerprint else 'HISTORY_CONFLICTING_RECORD')
                continue
            seen[signature]=fingerprint
            at=obs['block_time'];slot=obs['slot']
            if slot_range is not None and (type(slot) is not int or not slot_range['gte']<=slot<slot_range['lt']):
                reasons.append('HISTORY_SLOT_OUTSIDE_QUERY');continue
            if type(at) is not int or (slot_range is None and not start<=at<end):
                reasons.append('HISTORY_TIME_OUTSIDE_QUERY');continue
            if last_slot is not None and slot<last_slot:reasons.append('HISTORY_ORDER_REGRESSION')
            last_slot=slot
            obs['commitment']='finalized_provider_response'
            if obs['status']=='OBSERVED':observations.append(obs)
        next_cursor=response.get('paginationToken')
        if next_cursor is not None and (not isinstance(next_cursor,str) or not next_cursor):
            reasons.append('HISTORY_INVALID_CURSOR');break
        if next_cursor is None:
            exhausted=True;break
        if not rows:reasons.append('HISTORY_EMPTY_NONTERMINAL_PAGE')
        if next_cursor in cursors:
            reasons.append('HISTORY_CURSOR_CYCLE');break
        cursors.add(next_cursor);cursor=next_cursor
    if not exhausted:reasons.append('HISTORY_RANGE_NOT_EXHAUSTED')
    coverage={'address':owner,'token_accounts_filter':token_accounts,'start':start,'end':end,'pages':pages,'next_cursor':cursor if not exhausted else None,
              'query_range_exhausted':exhausted,'query_coverage_verified':exhausted and not reasons,
              'raw_pages_persisted':bool(pages) and all(p['persisted'] for p in pages),
              'launch_history_complete':False,'reasons':sorted(set(reasons)),
              'notice':'Address-query coverage is not complete token transfer, launch or funding coverage.'}
    if slot_range is not None:coverage['slot_range']=dict(slot_range)
    if page_size!=100:coverage['page_size']=page_size
    coverage['evidence_hash']=digest(coverage)
    return observations,coverage
