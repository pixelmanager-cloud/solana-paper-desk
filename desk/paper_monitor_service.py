"""One positions-only invocation; scheduler/activation belongs to coordinator."""
import argparse
import json
import sqlite3
import tempfile
from pathlib import Path
from . import paper_cycle_cli as cli
from .paper_target_export import export_targets, ExportBlocked


def main(argv=None):
    parser=argparse.ArgumentParser(description='One held-position pass; no candidates/admissions/provisioning')
    for name in ('config','research-db','evidence-db','ledger-db','pool-fee-bps'):
        parser.add_argument('--'+name,required=True)
    parser.add_argument('--systemd-credentials',action='store_true')
    parser.add_argument('--dependency-blocker',action='append',default=[])
    args=parser.parse_args(argv)
    try:
        cfg=cli._config(args.config)
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
            return cli.main(invocation)
    except (ExportBlocked,ValueError,OSError,sqlite3.Error,KeyError,TypeError,OverflowError,RecursionError):
        print(json.dumps({'kind':'paper_monitor_service_v1','status':'UNAVAILABLE',
            'blockers':['HELD_TARGET_CONFIG_OR_RUNTIME_UNAVAILABLE'],
            'execution_status':'EXECUTION_UNVERIFIED','live_readiness':False}))
        return 2


if __name__=='__main__':raise SystemExit(main())
