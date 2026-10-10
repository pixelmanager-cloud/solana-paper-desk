"""Repeatable retained-page iterators; no raw history collection or provider I/O."""
from .model import digest

MAX_PAGES=8
MAX_PAGE_BYTES=2*1024*1024

class RetainedHistoryPages:
    def __init__(self,store,coverage):
        self.store=store
        value=dict(coverage);key=value.pop('evidence_hash',None)
        if digest(value)!=key:raise ValueError('Retained coverage hash mismatch')
        pages=coverage['pages']
        if type(pages) is not list or not 1<=len(pages)<=MAX_PAGES:raise ValueError('History page ceiling')
        self.refs=tuple((p['request_evidence_hash'],p['payload_hash']) for p in pages)

    def _load(self,key):
        from .paper_terminal_reconciliation import _load
        return _load(self.store,key,max_raw_bytes=MAX_PAGE_BYTES)

    def __iter__(self):
        for request,response in self.refs:
            yield {'request':self._load(request),'response':self._load(response)}

    def _records(self):
        for pair in self:
            rows=pair['response']['data']
            if type(rows) is not list:raise ValueError('History records required')
            for row in rows:yield row

    def records(self):
        return RetainedHistoryRecords(self)

class RetainedHistoryRecords:
    def __init__(self,pages):self.pages=pages
    def __iter__(self):return self.pages._records()
