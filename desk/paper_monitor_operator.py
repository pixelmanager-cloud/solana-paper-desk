"""Coordinator provisioning and advisory read-only monitoring pacing.

Locks protect the snapshot; run_once independently reacquires its own locks.
A competing operator may consume budget between them. This guard is not a
reservation and never weakens the durable source/cycle guards or failure latch.
"""
from contextlib import contextmanager
import sqlite3
import time
from . import paper_cycle as cycle
from .job_persistence import canonical_job_path
from .history_progress import canonical_ownership_path
from .monitoring_budget import MonitoringBudget


@contextmanager
def _context(research_db,evidence_db,ledger_db,cfg):
    research=canonical_job_path(research_db);evidence=canonical_ownership_path(evidence_db);ledger=canonical_job_path(ledger_db)
    if len({research,evidence,ledger})!=3 or not all(p.is_file() for p in (research,evidence,ledger)):
        raise ValueError('Distinct existing databases required')
    with cycle._worker_lock(research) as worker:
        if worker is None:raise ValueError('Research worker busy')
        with cycle._lock(str(evidence)+'.ownership-invocation.lock') as acquired:
            if not acquired:raise ValueError('Evidence invocation busy')
            with cycle._lock(str(ledger)+'.paper-cycle.lock') as acquired:
                if not acquired:raise ValueError('Cycle ledger busy')
                _,store,_=cycle._existing_context(research,evidence,())
                yield store,ledger,cycle._state(ledger,cfg)


def provision(research_db,evidence_db,ledger_db,cfg):
    cycle._config(cfg)
    with _context(research_db,evidence_db,ledger_db,cfg) as (store,ledger,state):
        with store.connect() as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'paper_monitoring_%' LIMIT 1").fetchone():
                raise ValueError('Existing monitoring schema is never reprovisioned')
        MonitoringBudget(store,ledger,cfg).provision()
    return {'kind':'paper_monitoring_provision_v1','status':'PROVISIONED',
            'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False,'automatic_entry_enabled':False}


def preflight(research_db,evidence_db,ledger_db,cfg,*,clock=time.time):
    cycle._config(cfg)
    with _context(research_db,evidence_db,ledger_db,cfg) as (store,ledger,state):
        now=int(clock());snapshot=MonitoringBudget(store,ledger,cfg,clock=clock).snapshot()
        required=5*len(state['positions'])
        marks=[]
        for mint,p in state['positions'].items():
            at=p.get('mark_at');age=now-at if type(at) is int and 0<=at<=now else None
            marks.append({'mint':mint,'original_mark_at':at,'age_seconds':age,
                'saved_mark_status':p.get('mark_status'),
                'fresh_by_age':age is not None and age<=cfg['price_ttl_seconds']})
        blockers=list(snapshot.get('blockers',[]))
        if snapshot.get('status')!='AVAILABLE':blockers.append('MONITORING_BUDGET_UNAVAILABLE')
        if snapshot.get('remaining',0)<required:blockers.append('MONITORING_PASS_ALLOWANCE_INSUFFICIENT')
        return {'kind':'paper_monitoring_preflight_v1','status':'BLOCKED' if blockers else 'COMPLETE',
                'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False,
                'blockers':blockers,'required_pass_reads':required,'monitoring_budget':snapshot,
                'saved_marks':marks,'scope':'SNAPSHOT_NOT_RESERVATION_OR_CONTINUOUS_TTL_COVERAGE'}
