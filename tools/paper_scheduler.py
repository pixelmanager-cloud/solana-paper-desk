"""Serialize approved operator invocations; no service stopping or provider I/O."""
import argparse
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path
from desk.paper_scheduler import lease
from desk import paper_cycle, paper_concurrency as concurrency
from desk.paper_cycle_cli import _config

COMMANDS={'entry':['-m','tools.paper_entry_dispatcher'],
          'held':['-m','desk.paper_monitor_service'],
          'expire':['-m','desk','paper-monitor'],
          'decisions':['-m','desk','consume-scans']}

# A held pass that finds the lease taken by a running entry waits this long for it instead of
# giving up until the next timer tick (concurrent-entries experiments only). Held unit budget:
# 40 s wait + 8 legs x 7.8 s = 102 s inside TimeoutStartSec=120.
HELD_LEASE_WAIT_SECONDS=40.0
LEASE_POLL_SECONDS=0.5

@contextlib.contextmanager
def _lease(research,wait=0.0,*,clock=time.monotonic,sleep=time.sleep):
    """`lease`, optionally polling for up to `wait` seconds. Default 0 is exactly `lease`."""
    deadline=clock()+wait
    while True:
        with lease(research) as fd:
            if fd is not None or clock()>=deadline:
                yield fd
                return
        sleep(LEASE_POLL_SECONDS)

def _held_wait(argument):
    """Wait only for a valid concurrent-entries config; every other configuration keeps today's behaviour."""
    try:
        return HELD_LEASE_WAIT_SECONDS if concurrency.selected(_config(argument('--config'))) else 0.0
    except (ValueError,OSError,KeyError,TypeError):
        return 0.0

def _concurrent_tick(args,argument,cfg,ledger):
    """Held first, always: every open position is monitored before any entry work.

    Returns a refusal document, or None when the entry dispatcher may be asked. The
    lease is held throughout, so an entry can never delay or interleave with an exit
    pass. The engine, budget and freshness gates still decide inside the dispatcher.
    """
    state=paper_cycle._state(ledger,cfg)
    held=None
    if state['positions']:
        from desk.paper_monitor_service import main as held_pass
        held_args=[x for name in ('--config','--research-db','--evidence-db','--ledger-db','--pool-fee-bps')
                   for x in (name,argument(name))]
        if '--systemd-credentials' in args:held_args.append('--systemd-credentials')
        captured=io.StringIO()
        with contextlib.redirect_stdout(captured):
            code=held_pass(held_args)
        held={'exit_code':code}
        if code!=0:
            return {'status':'HELD_MONITORING_DEGRADED','held_pass':held,'attempted_requests':0,'entry_authorized':False}
        state=paper_cycle._state(ledger,cfg)
    blockers=concurrency.entry_blockers(state,cfg,now=concurrency.clock())
    if blockers:
        return {'status':'CONCURRENT_ENTRY_BLOCKED','blockers':blockers,'held_pass':held,
                'attempted_requests':0,'entry_authorized':False}
    return None

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--research-db',required=True)
    p.add_argument('--mode',choices=COMMANDS,required=True)
    p.add_argument('arguments',nargs=argparse.REMAINDER)
    a=p.parse_args(argv)
    args=a.arguments[1:] if a.arguments[:1]==['--'] else a.arguments
    def argument(name):
        if args.count(name)!=1:raise ValueError('One exact '+name+' required')
        i=args.index(name)
        if i+1>=len(args):raise ValueError('Missing '+name)
        return args[i+1]
    research=paper_cycle.canonical_job_path(a.research_db)
    bound=argument('--db' if a.mode in ('expire','decisions') else '--research-db')
    if a.mode!='expire' and paper_cycle.canonical_job_path(bound)!=research:
        raise ValueError('Scheduler research context mismatch')
    with _lease(research,_held_wait(argument) if a.mode=='held' else 0.0) as fd:
        if fd is None:
            print(json.dumps({'status':'SCHEDULER_BUSY','attempted_requests':0,'entry_authorized':False}))
            return 0
        if a.mode=='entry':
            cfg=_config(argument('--config'))
            ledger=paper_cycle.canonical_job_path(argument('--ledger-db'))
            if ledger.parent!=research.parent:raise ValueError('Scheduler ledger context mismatch')
            state=paper_cycle._state(ledger,cfg)
            if concurrency.selected(cfg):
                refusal=_concurrent_tick(args,argument,cfg,ledger)
                if refusal is not None:
                    print(json.dumps(refusal,sort_keys=True))
                    return 0
            elif state['positions'] or state['mode']!='RUNNING':
                print(json.dumps({'status':'HELD_POSITION_PRIORITY','attempted_requests':0,'entry_authorized':False}))
                return 0
        # Existing commands run in this process while the lease remains held
        # across their inner lock release/reacquisition handoffs.
        if a.mode=='entry':
            from tools.paper_entry_dispatcher import main as invoke
        elif a.mode=='held':
            from desk.paper_monitor_service import main as invoke
        else:
            from desk.cli import main as invoke
            args=[('paper-monitor' if a.mode=='expire' else 'consume-scans'),*args]
            original_argv=sys.argv
            try:
                sys.argv=[original_argv[0],*args]
                return invoke()
            finally:
                sys.argv=original_argv
        return invoke(args)
    return 0

if __name__=='__main__':raise SystemExit(main())
