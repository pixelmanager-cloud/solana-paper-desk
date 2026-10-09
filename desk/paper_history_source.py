"""One-shot finalized window adapter for HistoryProgress.advance's reservation.

Caller holds research then evidence invocation lock. Construct immediately before
advance; never use this adapter with an independently charged RPC wrapper.
"""
import math
import time

from .history_progress import HistoryProgress
from .paper_read_sources import PaperReadSources, PaperReadError, MAX_REQUEST_BYTES, RPC_ID
from .model import canonical
from . import original_byte_slot_transport as wire


class PaperHistorySource:
    def __init__(self, progress, scan_id, history_id, *, timeout_seconds):
        if not isinstance(progress, HistoryProgress):
            raise PaperReadError('HISTORY_BINDING_INVALID')
        self.progress, self.scan_id, self.history_id = progress, scan_id, history_id
        self.before = progress.snapshot(history_id)
        with progress.store.connect() as connection:
            row = connection.execute('SELECT budget FROM ownership_history WHERE id=?', (history_id,)).fetchone()
        query = self.before['query']
        if (not row or row[0] != scan_id or 'slot_range' in query
                or query['token_accounts_filter'] != 'none' or query['end'] - query['start'] != 301
                or self.before['status'] == 'DONE'):
            raise PaperReadError('HISTORY_BINDING_INVALID')
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 15:
            raise PaperReadError('DEADLINE_INVALID')
        self.started = time.monotonic()
        self.deadline = self.started + timeout_seconds
        self.called = False
        self.evidence_hash = None

    def __call__(self, method, params):
        before = self.before
        query = before['query']
        options = {'transactionDetails':'full','sortOrder':'asc','limit':100,
                   'commitment':'finalized','encoding':'jsonParsed','maxSupportedTransactionVersion':1,
                   'filters':{'blockTime':{'gte':query['start'],'lt':query['end']},
                              'status':'any','tokenAccounts':'none'}}
        coverage = before['coverage']
        if coverage and coverage['next_cursor']:
            options['paginationToken'] = coverage['next_cursor']
        if (self.called or method != 'getTransactionsForAddress' or type(params) is not list
                or canonical(params) != canonical([query['address'], options])):
            raise PaperReadError('HISTORY_BINDING_INVALID')
        body = canonical({'jsonrpc':'2.0','id':RPC_ID,'method':method,'params':params}).encode()
        if len(body) > MAX_REQUEST_BYTES: raise PaperReadError('REQUEST_INVALID')
        current = self.progress.snapshot(self.history_id)
        if (current['query'] != query or current['coverage'] != coverage
                or current['status'] != 'PENDING' or current['attempts'] != before['attempts'] + 1
                or current['requests_used'] != before['requests_used'] + 1):
            raise PaperReadError('HISTORY_RESERVATION_REQUIRED')
        self.called = True
        now = time.monotonic()
        if not math.isfinite(now) or now < self.started or now >= self.deadline:
            raise PaperReadError('DEADLINE_EXCEEDED')
        adapter = PaperReadSources(self.progress, self.scan_id)
        owner = self
        class ReservedProgress:
            store = owner.progress.store
            def admission(self, identity): return owner.progress.admission(identity)
            def reserve(self, identity):
                state = owner.progress.snapshot(owner.history_id)
                if identity != owner.scan_id or state != current:
                    raise PaperReadError('HISTORY_RESERVATION_REQUIRED')
                return True  # advance already durably charged this exact invocation.
        adapter.progress = ReservedProgress()
        try:
            result, self.evidence_hash = adapter._attempt(method, wire._parse(body)['params'], body, self.deadline - now)
            return result
        except PaperReadError as error:
            self.evidence_hash = error.evidence_hash
            raise
