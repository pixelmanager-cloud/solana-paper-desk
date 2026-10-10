"""Coordinator-only exact one-successor activation; no providers or starts."""
import argparse,json,os,subprocess,sqlite3
from pathlib import Path
from contextlib import closing
from desk import runtime_compatibility as runtime,runtime_empty_history_successor as continuation
from desk import paper_empty_history_reconciliation as recovery, runtime_performance_continuation as parent
from desk.paper_cycle_cli import _config
from desk.paper_terminal_reconciliation import verified_bytes_scope
from desk.model import digest
from tools import paper_entry_dispatcher as dispatcher,history_first_paper_entry as entry
import hashlib
p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--pin-hash',required=True);p.add_argument('--recovery-hash',required=True);a=p.parse_args()
if runtime.implementation_hash()!=a.source:raise ValueError('Exact source required')
if os.environ.get('DESK_PROVIDER_PACING_DB')!='/var/lib/solana-desk/provider-pacing.sqlite':raise ValueError('Pacing binding')
units=('desk-dashboard.service','desk-paper-held-cycle.service','desk-paper-monitor.service','desk-decisions.service','desk-backup.service','desk-discovery.service','desk-recorder.service','desk-continuous-discovery.service','desk-paper-held-cycle.timer','desk-paper-monitor.timer','desk-decisions.timer','desk-backup.timer','desk-discovery.timer','desk-paper-entry-dispatcher.service','desk-paper-entry-dispatcher.timer')
for unit in units:
 if subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','--value'],text=True).strip() not in ('inactive','failed'):raise ValueError('Writer active: '+unit)
cfg=_config('/etc/solana-paper/paper-kraken-reviewed.json')
if digest(cfg)!='3dc3108e1c23ebbad97b88338e37f4012d3dd6b259f5a911624d799da1c11dff':raise ValueError('Config mismatch')

policy=json.loads(continuation.POLICY.read_text())
if len(policy['successors'])!=1:raise ValueError('One pin required')
pin=policy['successors'][0];continuation.approved(pin)
if digest(pin)!=a.pin_hash or pin['successor']!=a.source or pin['predecessor']!='0cf089b1bb4af6acf47b30d5d8443af1b2af7296bb173be7dbdc0101ba67d692':raise ValueError('Reviewed pin/source mismatch')
for identity in pin['dispatch_successor']['paths'].values():
 if dispatcher._identity(Path(identity['path']))!=identity:raise ValueError('Path identity changed')
if pin['dispatch_successor']['tool_hash']!=hashlib.sha256(Path(dispatcher.__file__).read_bytes()).hexdigest() or pin['dispatch_successor']['entry_tool_hash']!=hashlib.sha256(Path(entry.__file__).read_bytes()).hexdigest():raise ValueError('Tool identity mismatch')
paths=tuple(pin['context'][k] for k in ('research_db','evidence_db','ledger_db'))
recovery_policy=json.loads(recovery.POLICY.read_text())
if len(recovery_policy['associations'])!=1:raise ValueError('One exact recovery required')
rpin=recovery_policy['associations'][0];recovery.approved(rpin)
if digest(rpin)!=a.recovery_hash or pin['recovery_hash']!=a.recovery_hash:raise ValueError('Recovery identity mismatch')
with verified_bytes_scope():
 proposed_recovery=recovery.plan(*paths,cfg,pass_id=rpin['pass_id'],outcome_hash=rpin['outcome_hash'],
     empty_history=rpin['empty_history'],pacing_db=rpin['context']['pacing_db'],review_source=pin['predecessor'])
 if proposed_recovery!=rpin:raise ValueError('Reviewed recovery changed')
 proposed=continuation.plan(paths[2],dispatch_predecessor=pin['dispatch_predecessor'],dispatch_successor=pin['dispatch_successor'],recovery_pin=rpin)
 if proposed!=pin:raise ValueError('Exact reviewed proposal changed')
 retired=recovery.reconcile(*paths,cfg,pin=rpin)
 result=continuation.append(paths[2],pin=pin)
 with closing(sqlite3.connect(Path(paths[2]).as_uri()+'?mode=ro',uri=True)) as c:
  if parent.require(c)!=a.source:raise ValueError('Current runtime mismatch')
 print(json.dumps({'recovery':retired,'successor':result,'source':a.source,'services_started':False},sort_keys=True))
