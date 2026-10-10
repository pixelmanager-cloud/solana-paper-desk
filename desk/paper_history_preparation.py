"""History-first preparation under the enclosing cycle's continuous locks."""
from dataclasses import replace
import math,sqlite3,time,uuid
from . import paper_cycle as cycle, paper_history_source
from .paper_history_source import PaperHistorySource
from . import history_preparation_rejection as rejection
PREPARATION_SECONDS = 18


class PreparationRejected(ValueError):
    """Deterministic bounded-input rejection; never transport uncertainty."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _PreparationBudget(cycle._Budget):
    """History-only phase; the fresh cycle still constructs its own ten seconds."""
    def __init__(self, progress, item, cfg):
        self.item = item
        self.cfg = cfg
        self.fresh_requests = 7 if cfg.get('paper_usd_valuation_version',0) == 1 else 9
        self.history_id = progress.create(item.target.scan_id,item.target.pool,
            item.history_as_of-300,item.history_as_of+1,
            page_size=paper_history_source.PAPER_HISTORY_PAGE_SIZE)
        super().__init__(progress,time.time,time.monotonic)

    def remaining(self):
        value = self.monotonic()
        if (type(value) not in (int,float) or not math.isfinite(value)
                or value < self.last or value >= self.start+PREPARATION_SECONDS):
            raise cycle.CycleBlocked('HISTORY_PREPARATION_DEADLINE_UNAVAILABLE')
        self.last = value
        return min(15,self.start+PREPARATION_SECONDS-value)

    def check_bounds(self):
        state = self.progress.snapshot(self.history_id)
        measured = rejection.bounds(self.progress.store,state['coverage'],cfg=self.cfg,as_of=self.item.history_as_of,semantics_version=2,required_measurements=True)
        reason = rejection.reason_for(measured,18,self.fresh_requests,now=self.now(),
            momentum_ttl=self.cfg['momentum_ttl_seconds'],
            exhausted=bool(state['coverage'] and state['coverage']['query_range_exhausted'] and state['coverage']['query_coverage_verified']),semantics_version=2,empty_window=True)
        if reason:raise PreparationRejected(reason)

    def call(self, scan, invoke):
        self.check_bounds()
        admission = self.progress.admission(scan)
        if admission is None or admission['state'] not in ('ADMITTED','SEALED'):
            raise cycle.CycleBlocked('PERSISTED_ADMISSION_REQUIRED')
        if admission['request_ceiling']-admission['requests_used'] < self.fresh_requests+1:
            raise PreparationRejected('HISTORY_FRESH_ENTRY_REQUESTS_UNAVAILABLE')
        result = super().call(scan,invoke)
        self.check_bounds()
        return result


def prepare(store,progress,ledger_db,cfg,item,*,research_db,pacing_path):
    item = replace(item, history_as_of=int(time.time()))
    budget = _PreparationBudget(progress,item,cfg)
    # Existing enclosing pass latch: crash/charged failure cannot be retried
    # as an unrelated healthy pass. No recovery/reset facility here.
    before = progress.admission(item.target.scan_id)
    identity = uuid.uuid4().hex
    with store.connect() as c:
        c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
    context = {'research_db':str(cycle.canonical_job_path(research_db)),
               'evidence_db':str(store.path),'ledger_db':str(cycle.canonical_job_path(ledger_db)),
               'pacing_db':str(pacing_path)}
    intent = store.save({**rejection.intent(store,ledger_db,cfg,item,before,context),'closure_v1':True})
    with store.connect() as c:
        c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', (identity,intent))
    try:
        from .paper_cycle_no_entry import ledger_snapshot
        ledger_before = ledger_snapshot(ledger_db)    # preparation never writes the ledger
    except (ValueError, OSError, sqlite3.Error):
        ledger_before = None
    try:
        as_of, _, _ = cycle._history(progress, item, budget, PaperHistorySource)
        if as_of != item.history_as_of: raise ValueError('Captured history changed')    # T22H L2: inside the held try
    except PreparationRejected as error:
        try:
            result = rejection.publish(store,progress,ledger_db,cfg,pass_id=identity,
                intent_hash=intent,history_id=budget.history_id,reason=error.code)
        except BaseException as failure:
            # T22G: a failure while publishing the typed rejection is not a transient provider fault. It must
            # never fall through to a later ABANDONED_CHARGED recovery: hold the pass (fail closed), then re-raise.
            from . import paper_pass_closure as closure
            closure.hold_durably(store, pass_id=identity, cause='PREPARATION_REJECTION_PUBLICATION_FAILED')
            raise failure
        return result
    except BaseException as error:
        # A transient/typed failure after a charge must not latch the store: retire the pass as
        # FAILED_CHARGED when provable (never for integrity causes); the failure still propagates.
        cycle._close_failed(store,progress,identity,error,(),ledger_before)
        raise
    outcome = store.save({'kind':'history_first_paper_preparation_outcome_v1',
                          'intent_hash':intent, 'history_as_of':as_of,
                          'admission':progress.admission(item.target.scan_id)})
    with store.connect() as c:
        c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND outcome_hash IS NULL', (outcome,identity))
    return item
