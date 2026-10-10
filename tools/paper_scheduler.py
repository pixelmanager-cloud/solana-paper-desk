"""Serialize approved operator invocations; no service stopping or provider I/O."""
import argparse
import json
import os
import sys
from pathlib import Path
from desk.paper_scheduler import lease
from desk import paper_cycle
from desk.paper_cycle_cli import _config

COMMANDS={'entry':['-m','tools.paper_entry_dispatcher'],
          'held':['-m','desk.paper_monitor_service'],
          'expire':['-m','desk','paper-monitor'],
          'decisions':['-m','desk','consume-scans']}

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
    with lease(research) as fd:
        if fd is None:
            print(json.dumps({'status':'SCHEDULER_BUSY','attempted_requests':0,'entry_authorized':False}))
            return 0
        if a.mode=='entry':
            cfg=_config(argument('--config'))
            ledger=paper_cycle.canonical_job_path(argument('--ledger-db'))
            if ledger.parent!=research.parent:raise ValueError('Scheduler ledger context mismatch')
            state=paper_cycle._state(ledger,cfg)
            if state['positions'] or state['mode']!='RUNNING':
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
        return invoke(args)
    return 0
                # Every entry wake-up services existing positions instead; an
                # entry timer cannot starve held work by repeatedly winning.
                held=[]
                for name in ('--config','--research-db','--evidence-db','--ledger-db','--pool-fee-bps'):
                    held.extend([name,argument(name)])
                if '--systemd-credentials' in args:held.append('--systemd-credentials')
                os.set_inheritable(fd,True)
                os.execv(sys.executable,[sys.executable,*COMMANDS['held'],*held])
        # Exec replaces this process: no parent/child release gap, retained fd
        # survives exec and is released by normal exit, crash or systemd cleanup.
        os.set_inheritable(fd,True)
        os.execv(sys.executable,[sys.executable,*COMMANDS[a.mode],*args])
    return 0

if __name__=='__main__':raise SystemExit(main())
