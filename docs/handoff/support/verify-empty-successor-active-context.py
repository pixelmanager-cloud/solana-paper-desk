"""Coordinator final accepted-validator check, before any service restart."""
import argparse
import time
from contextlib import ExitStack, closing
import json
from pathlib import Path
import sqlite3
from desk import monitoring_handoff, monitoring_successor, paper_cycle
from desk import paper_terminal_reconciliation as terminal
from desk import runtime_compatibility as runtime
from desk.evidence import EvidenceStore
from desk.model import digest
from desk.monitoring_budget import MonitoringBudget
from desk.paper_cycle_cli import _config
from desk.paper_observe_cli import _worker_lock

p = argparse.ArgumentParser()
for name in ('pin', 'pin-hash', 'config', 'runtime-source', 'successor-pin', 'successor-hash'):
    p.add_argument('--' + name, required=True)
a = p.parse_args()
pin = json.loads(Path(a.pin).read_text())
cfg = _config(a.config)
if digest(pin) != a.pin_hash:
    raise ValueError('Exact independently reviewed pin required')
monitoring_successor.approved(pin)
if runtime.implementation_hash() != a.runtime_source or pin['new_source'] != '77de75a248fd087e62bcc2bac4a581c667905dfcb709e28ed8e80a58afcd4d54' or digest(cfg) != pin['new_config_hash']:
    raise ValueError('Reviewed source/config mismatch')
ctx = pin['context']
store = EvidenceStore(ctx['evidence_db'], read_only=True)
# Match the accepted budget diagnostics contract without constructor migrations.
# Only gate/read/snapshot validators are invoked below; no reservation or outcome.
store.read_only = False
with ExitStack() as locks:
    if locks.enter_context(_worker_lock(ctx['research_db'])) is None:
        raise ValueError('Research busy')
    if not locks.enter_context(paper_cycle._lock(ctx['evidence_db'] + '.ownership-invocation.lock')):
        raise ValueError('Evidence busy')
    if not locks.enter_context(paper_cycle._lock(ctx['new_ledger_db'] + '.paper-cycle.lock')):
        raise ValueError('New ledger busy')
    with closing(store.connect()) as c:
        first = monitoring_handoff.read(c)
        edges = monitoring_successor.rows(c, first)
        if len(edges) != pin['sequence'] or not edges or edges[-1] != (pin, a.pin_hash):
            raise ValueError('Unexpected active successor')
    # The gate obtains retired-ledger locks itself. Do not prelock those files.
    gate_started=time.monotonic()
    if terminal.gate(store, ctx['research_db'], (), ledger_locked=ctx['new_ledger_db']) is not None:
        raise ValueError('Unresolved runtime recovery gate')
    gate_elapsed_seconds=time.monotonic()-gate_started
    # The preceding full gate replay validates every installed receipt once.
    # Under these same locks, confirm all prior and newly retired identities.
    from desk import paper_http403_retirement as http, paper_preparation_retirement as prep
    from desk import paper_dispatch_preparation_retirement as dispatchprep, paper_migration_no_entry as migration
    from desk import paper_intake_uncaptured_retirement as intake
    with closing(store.connect()) as c:
        records=terminal._rows(c)+http.rows(c)+prep.rows(c)+dispatchprep.rows(c)+migration.rows(c)+intake.rows(c)
    retired={v['scan_id'] for v in records}
    expected_retired={'d796b12f49594e8097cc6cbb46299edb','ce675338c1374812b49247f5d2850a2d','d21df76a3f974eeaab5329885ed9a6af','d98bb0c070c24c35ac782d9d0d88fb8b','86b0c16242934b768a0ddc0847d9b09d','218c824a072944b2a73dac1503ba3038','289545cf78f143e391115a9debecc2ed','df8ac72596cf4d30bf552aaf1b2c6adf','2e186c3728c045c2a237a51e55eaa546','7703021e2a14412788c3aefed0d60d66'}
    if not expected_retired<=retired:raise ValueError('Original or new retirement missing')
    state = paper_cycle._state(Path(ctx['new_ledger_db']), cfg)
    from desk.engine import initial_state
    if state != initial_state(cfg):
        raise ValueError('New experiment changed before service start')
    status = MonitoringBudget(store, ctx['new_ledger_db'], cfg).snapshot()
    if status['status'] != 'AVAILABLE' or status['blockers'] or status['cap'] != 3600:
        raise ValueError('Monitoring unavailable')
    if status['total_used'] != pin['budget'][7]:
        raise ValueError('Monitoring usage changed before service start')
from desk.provider_pacing import Pacer
from desk import kraken_pacing_migration
pacer = Pacer(ctx['pacing_db'])
with closing(pacer._connect()) as c:
    migration = kraken_pacing_migration.read(c, Path(ctx['pacing_db']))
    if migration is None:
        raise ValueError('Reviewed Kraken pacing receipt missing')
    if c.execute('SELECT 1 FROM state WHERE pending IS NOT NULL').fetchone() or c.execute('SELECT 1 FROM waiters').fetchone():
        raise ValueError('Pacing not quiescent before restart')
print(json.dumps({'status': 'ACCEPTED_VALIDATORS_PASS_PRE_RESTART', 'source': a.runtime_source,
                  'gate_elapsed_seconds':gate_elapsed_seconds, 'pin_hash': a.pin_hash, 'cash': state['cash'], 'positions': len(state['positions']),
                  'monitoring': status, 'retired_scans_preserved': True}))

# Run outside the ledger lock because the dispatcher lineage validator acquires it.
from tools import paper_entry_dispatcher as dispatcher
from desk import runtime_empty_history_successor as successor
successor_pin=json.loads(Path(a.successor_pin).read_text())
if digest(successor_pin)!=a.successor_hash:raise ValueError('Exact successor hash required')
successor.approved(successor_pin)
if successor_pin['successor']!=a.runtime_source or successor_pin['config_hash']!=digest(cfg):raise ValueError('Successor runtime/config mismatch')
with sqlite3.connect('file:'+ctx['new_ledger_db']+'?mode=ro',uri=True) as c:
    if successor.read(c)!=(successor_pin,a.successor_hash):raise ValueError('Installed successor mismatch')
expected=successor_pin['dispatch_successor']
if expected['source_hash']!=a.runtime_source:raise ValueError('Dispatcher source mismatch')
with terminal.verified_bytes_scope():
 dispatcher._preflight(expected)
 with dispatcher._journal(expected['journal']) as c:
     values=dispatcher._validate(c,expected)
     if len(values['intents'])!=13 or len(values['results'])!=8 or 'e0618c54f9534b218c4263097853ff8d' in values['results']:raise ValueError('Original journal changed')
 print(json.dumps({'status':'SAME_DISPATCH_JOURNAL_VALIDATED','context_hash':digest(expected),'original_intents':13,'original_results':8}))
