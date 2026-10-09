"""Coordinator-only history-first paper pass; dry-run unless --execute.

Uses existing admission, pacing, source and checkpoint validators. Never creates
an experiment/admission/pacer, signs, broadcasts, clears recovery or retries.
"""
import argparse
from contextlib import contextmanager, closing
from dataclasses import replace, asdict
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desk import paper_cycle as cycle, paper_cycle_cli as cli, provider_pacing as pacing
from desk.paper_history_source import PaperHistorySource
from desk.model import digest


@contextmanager
def _context(research_db, evidence_db, ledger_db, cfg, item):
    research = cycle.canonical_job_path(research_db)
    evidence = cycle.canonical_ownership_path(evidence_db)
    ledger = cycle.canonical_job_path(ledger_db)
    if len({research, evidence, ledger}) != 3 or not all(p.is_file() for p in (research, evidence, ledger)):
        raise ValueError('Distinct existing databases required')
    with cycle._worker_lock(research) as worker:
        if worker is None: raise ValueError('Research worker busy')
        with cycle._lock(str(evidence)+'.ownership-invocation.lock') as acquired:
            if not acquired: raise ValueError('Evidence invocation busy')
            with cycle._lock(str(ledger)+'.paper-cycle.lock') as acquired:
                if not acquired: raise ValueError('Paper cycle busy')
                jobs, store, progress = cycle._existing_context(research, evidence, (item.target,))
                state = cycle._state(ledger, cfg)
                if state['positions']: raise ValueError('No open positions permitted')
                if state['mode'] != 'RUNNING': raise ValueError('Entry mode not running')
                with store.connect() as c:
                    found = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'").fetchone()
                    if found and c.execute('SELECT 1 FROM paper_observation_passes WHERE outcome_hash IS NULL LIMIT 1').fetchone():
                        raise ValueError('Observation recovery required')
                graduation = cycle._graduation(store, item, int(time.time()))
                if graduation['status'] != 'OBSERVED_MIGRATION': raise ValueError('Retained migration required')
                admission = progress.admission(item.target.scan_id)
                if admission['request_ceiling']-admission['requests_used'] < 9:
                    raise ValueError('Insufficient lifetime investigation budget')
                yield store, progress


def _pacer():
    value = pacing.configured(priority='investigation')
    if value is None: raise ValueError('Configured durable pacer required')
    # Existing validator checks schema, private mode and canonical inode identity.
    value._validate()
    with closing(value._connect()) as c:
        if c.execute('SELECT 1 FROM state WHERE pending IS NOT NULL LIMIT 1').fetchone():
            raise ValueError('Pacer recovery required')
    return value.path, value.identity


def execute(config, research_db, evidence_db, ledger_db, targets, *, live=False, systemd_credentials=False):
    cfg = cli._config(config); cycle._config(cfg)
    positions, candidates, usd = cli.load_targets(targets)
    if positions or len(candidates) != 1 or usd:
        raise ValueError('Exactly one candidate and no retained USD or positions required')
    item = candidates[0]
    if live and item.provenance != 'PUBLIC_MAINNET_CAPTURE_NOT_TRADING_EVIDENCE':
        raise ValueError('Public mainnet provenance required')
    pin = _pacer()  # Before credentials, admission preparation or provider spend.
    with _context(research_db, evidence_db, ledger_db, cfg, item) as (store, progress):
        if not live:
            return {'status':'DRY_RUN','live_readiness':False,'execution_status':'EXECUTION_UNVERIFIED',
                    'attempted_requests':0,'blockers':[]}
        if not systemd_credentials: raise ValueError('Explicit systemd credentials required')
        cli._credentials()
        if _pacer() != pin: raise ValueError('Pacer identity changed')
        item = replace(item, history_as_of=int(time.time()))
        budget = cycle._Budget(progress, time.time, time.monotonic)
        # Existing enclosing pass latch: crash/charged failure cannot be retried
        # as an unrelated healthy pass. No recovery/reset facility here.
        before = progress.admission(item.target.scan_id)
        intent = store.save({'kind':'history_first_paper_preparation_v1',
                             'config_hash':digest(cfg), 'target':asdict(item), 'admission':before})
        identity = uuid.uuid4().hex
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS paper_observation_passes(id TEXT PRIMARY KEY,intent_hash TEXT NOT NULL,outcome_hash TEXT)')
            c.execute('INSERT INTO paper_observation_passes VALUES(?,?,NULL)', (identity,intent))
        as_of, _, _ = cycle._history(progress, item, budget, PaperHistorySource)
        if as_of != item.history_as_of: raise ValueError('Captured history changed')
        outcome = store.save({'kind':'history_first_paper_preparation_outcome_v1',
                              'intent_hash':intent, 'history_as_of':as_of,
                              'admission':progress.admission(item.target.scan_id)})
        with store.connect() as c:
            c.execute('UPDATE paper_observation_passes SET outcome_hash=? WHERE id=? AND outcome_hash IS NULL', (outcome,identity))
    time.sleep(2.0)  # Normal elapsed wait; no time/pacer/budget rebasing.
    if _pacer() != pin: raise ValueError('Pacer identity changed')
    return cycle.run_once(research_db,evidence_db,ledger_db,cfg,
                          candidates=(item,),usd_evidence_refs=(),dependency_blockers=())


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('config','research-db','evidence-db','ledger-db','targets'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--systemd-credentials',action='store_true')
    a=p.parse_args(argv)
    try:
        result=execute(a.config,a.research_db,a.evidence_db,a.ledger_db,a.targets,
                       live=a.execute,systemd_credentials=a.systemd_credentials)
        summary={k:result[k] for k in ('status','blockers','attempted_requests','execution_status','live_readiness') if k in result}
        summary['outcomes']=[{k:r[k] for k in ('type','side','reason') if k in r} for r in result.get('outcomes',[])]
        print(json.dumps(summary,sort_keys=True));return 0 if result['status'] in ('DRY_RUN','COMPLETE') else 2
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError,OverflowError,RecursionError):
        print(json.dumps({'status':'BLOCKED','blockers':['HISTORY_FIRST_PREFLIGHT_OR_ACQUISITION_UNAVAILABLE'],
                          'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}));return 2


if __name__=='__main__': raise SystemExit(main())
