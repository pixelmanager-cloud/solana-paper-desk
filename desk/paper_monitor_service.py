"""One positions-only invocation; scheduler/activation belongs to coordinator."""
import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from . import paper_cycle_cli as cli
from . import paper_concurrency as concurrency
from .paper_target_export import export_targets, ExportBlocked


def _legs(args,cfg,output,invocation):
    """One cycle per held position, least recently marked (or unresolved) first.

    Each leg is an ordinary positions-only monitoring cycle with exactly one target,
    so its own deadline, budget reservation and exit rules apply unchanged. Legs
    beyond the wall budget are deferred to the next pass, never skipped permanently:
    the order is recomputed from the ledger, so a restart resumes where it stopped.
    """
    from .job_persistence import canonical_job_path
    from .paper_cycle import _state
    payload=json.loads(output.read_text())
    positions=_state(canonical_job_path(args.ledger_db),cfg)['positions']
    exported={row['mint']:row for row in payload['position_targets']}
    if set(exported)!=set(positions):raise ValueError('Exported targets differ from checkpoint positions')
    run,deferred=concurrency.plan_legs(positions,args.wall_seconds)
    code=0
    from . import portfolio_marks
    if portfolio_marks.selected(cfg):
        # Start of the pass: ONE batched marks refresh for all positions (a positions-less monitoring cycle). A failed
        # refresh is charged and leaves the marks stale; it never stops the exit legs below.
        refresh=output.with_name('targets-marks.json')
        refresh.write_text(json.dumps({**payload,'position_targets':[]}))
        invocation[invocation.index('--targets')+1]=str(refresh)
        cli.main(invocation)
    for mint in run:
        leg=output.with_name('targets-'+mint[:8]+'.json')
        leg.write_text(json.dumps({**payload,'position_targets':[exported[mint]]}))
        invocation[invocation.index('--targets')+1]=str(leg)
        code=max(code,cli.main(invocation))
    print(json.dumps({'kind':'paper_monitor_service_legs_v1','legs_run':len(run),'legs_deferred':len(deferred),
        'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}))
    return code


def main(argv=None):
    parser=argparse.ArgumentParser(description='One held-position pass; no candidates/admissions/provisioning')
    for name in ('config','research-db','evidence-db','ledger-db','pool-fee-bps'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--systemd-credentials',action='store_true')
    parser.add_argument('--wall-seconds',type=float,default=None,
                        help='Concurrent-entries experiments only: wall cap on held legs per pass '
                             '(default: every open position, oldest mark first)')
    parser.add_argument('--dependency-blocker',action='append',default=[])
    args=parser.parse_args(argv)
    try:
        cfg=cli._config(args.config)
        if args.wall_seconds is not None and not 0<args.wall_seconds<=3600:raise ValueError('Wall budget bound')
        # Pending dependency review stops before export/database/credentials.
        if args.dependency_blocker:
            if len(args.dependency_blocker)>16 or any(not 1<=len(x)<=128 for x in args.dependency_blocker):
                raise ValueError('Blocker bound')
            print(json.dumps({'kind':'paper_monitor_service_v1','status':'BLOCKED',
                'blockers':args.dependency_blocker,'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}))
            return 2
        with tempfile.TemporaryDirectory(prefix='paper-held-targets-') as directory:
            output=Path(directory)/'targets.json'
            exported=export_targets(args.research_db,args.evidence_db,output,
                pool_fee_bps=args.pool_fee_bps,ledger_db=args.ledger_db,cfg=cfg)
            if exported['status']!='EXPORTED':raise ValueError('Exporter unavailable')
            if exported['positions']==0:
                print(json.dumps({'kind':'paper_monitor_service_v1','status':'COMPLETE',
                    'blockers':[],'positions':0,'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}))
                return 0
            invocation=['--config',args.config,'once','--research-db',args.research_db,
                '--evidence-db',args.evidence_db,'--ledger-db',args.ledger_db,
                '--targets',str(output),'--monitoring']
            if args.systemd_credentials:invocation.append('--systemd-credentials')
            if concurrency.selected(cfg):
                return _legs(args,cfg,output,invocation)
            return cli.main(invocation)
    except (ExportBlocked,ValueError,OSError,sqlite3.Error,KeyError,TypeError,OverflowError,RecursionError):
        print(json.dumps({'kind':'paper_monitor_service_v1','status':'UNAVAILABLE',
            'blockers':['HELD_TARGET_CONFIG_OR_RUNTIME_UNAVAILABLE'],
            'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}))
        return 2


if __name__=='__main__':raise SystemExit(main())
