"""Fresh-start experiment bootstrap and flat-ledger rotation; dry run unless --execute.

Creates a brand-new, empty store set for one experiment version using only the
repository's own schema creators and activation APIs. It never opens, migrates,
resets or deletes an old store set, never touches the shared provider-pacing or
discovery databases (the shared pacing database is only validated), never reads
provider credentials and never performs provider I/O, signing or broadcasting.

    python -m tools.ops.fresh_start plan   --root R --config C --pacing-db P --discovery-db D
    python -m tools.ops.fresh_start apply  --root R ... --execute --approved-plan-hash H
    python -m tools.ops.fresh_start rotate --from OLD --to R ... --execute --approved-plan-hash H

`apply`/`rotate` are dry runs (they print the plan) without --execute.
"""
import argparse
from contextlib import closing
import datetime
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from desk import allowance_policy as allowance
from desk import decision_runner, paper_cycle as cycle, paper_cycle_cli as cli
from desk import paper_terminal_reconciliation as terminal
from desk import provider_pacing as pacing, runtime_compatibility as runtime
from desk.evidence import EvidenceStore
from desk.history_progress import HistoryProgress
from desk.job_persistence import JobPersistence
from desk.model import canonical, digest
from desk.monitoring_budget import MonitoringBudget
from desk.paper_checkpoint import RecoveryRequired, validate_checkpoint
from desk.programs import address
from tools import paper_entry_dispatcher as dispatcher

VERSION = 1
MANIFEST = 'fresh-start-manifest.json'
SCHEDULER_LOCK = 'paper-scheduler.lock'
STORES = {
    'research_db': 'research.sqlite',
    'evidence_db': 'evidence.sqlite',
    'ledger_db': 'paper-ledger.sqlite',
    'decisions_db': 'paper-decisions.sqlite',
    'journal': 'entry-dispatch/dispatch.sqlite',
}
# Repository initialiser used for each store (printed by `plan`).
INITIALISERS = {
    'research_db': 'desk.job_persistence.JobPersistence + upgrade_allowance (daily 1000, queue 25)',
    'evidence_db': 'desk.evidence.EvidenceStore + desk.history_progress.HistoryProgress '
                   '(lifetime 18 requests per investigation)',
    'ledger_db': 'desk.paper_cycle.initialize (fresh ledger, own implementation hash, no successor rows)',
    'monitoring_budget': 'desk.monitoring_budget.MonitoringBudget.provision + prepare/activate_upgrade '
                         '(3600 requests per rolling hour)',
    'decisions_db': 'desk.decision_runner._initialize_journal',
    'journal': 'tools.paper_entry_dispatcher.initialize (activated context)',
    SCHEDULER_LOCK: 'exclusive 0600 regular file; inode pinned via DESK_PAPER_SCHEDULER_IDENTITY',
}
PRODUCTION_TAKER = '6E2G75Z3uJEnPo9EvzmLTxp8KB78m3RDsFBjoCTVHZD2'
PYTHON = '/opt/solana-desk/.venv/bin/python'
OPEN_OK_MODES = ('RUNNING', 'ENTRY_PAUSED')


class FreshStartError(ValueError):
    pass


def _utc(now=None):
    return datetime.datetime.fromtimestamp(time.time() if now is None else now, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _absolute(value, what):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts:
        raise FreshStartError(f'{what}: canonical absolute path required')
    return p


def _no_symlink_chain(p, what):
    """Every existing component must be a real directory/file, never a link."""
    probe = p
    while True:
        if probe.is_symlink():
            raise FreshStartError(f'{what}: symlink in path refused')
        if probe.parent == probe:
            return
        probe = probe.parent


def _new_root(value):
    root = _absolute(value, 'root')
    _no_symlink_chain(root, 'root')
    if os.path.lexists(root):
        raise FreshStartError('root must not exist yet')
    parent = root.parent
    if not parent.is_dir() or parent.resolve() != parent:
        raise FreshStartError('root parent must be an existing canonical directory')
    return root


def _existing_file(value, what):
    p = _absolute(value, what)
    _no_symlink_chain(p, what)
    if not p.is_file() or p.resolve() != p:
        raise FreshStartError(f'{what}: canonical existing file required')
    return p


def paths_for(root):
    root = Path(root)
    result = {key: root / name for key, name in STORES.items()}
    result['scheduler_lock'] = root / SCHEDULER_LOCK
    result['manifest'] = root / MANIFEST
    return result


def plan(*, root, config, pacing_db, discovery_db, taker=PRODUCTION_TAKER, amount_raw=100_000_000,
         pool_fee_bps='25', rotate_from=None):
    """Static plan: validates inputs, creates nothing, performs no store I/O on the new root."""
    root = _new_root(root)
    config = _existing_file(config, 'config')
    pacing_db = _existing_file(pacing_db, 'pacing-db')
    discovery_db = _existing_file(discovery_db, 'discovery-db')
    address(taker)
    if type(amount_raw) is not int or not 0 < amount_raw < 2**64:
        raise FreshStartError('Exact positive u64 entry size required')
    if type(pool_fee_bps) is not str or not pool_fee_bps:
        raise FreshStartError('Reviewed fee hypothesis string required')
    cfg = cli._config(config)
    cycle._config(cfg)
    if cfg.get('paper_usd_valuation_version') != 1:
        raise FreshStartError('Explicit Kraken experiment (usd valuation v1) required')
    if len({root, config, pacing_db, discovery_db}) != 4 or root in (config.parent, pacing_db.parent, discovery_db.parent):
        raise FreshStartError('Distinct context paths required; shared stores must live outside the new root')
    if rotate_from is not None:
        old = Path(rotate_from)
        if root == old or root in old.parents or old in root.parents:
            raise FreshStartError('New root must be a sibling store set, never inside or around the old one')
    paths = {k: str(v) for k, v in paths_for(root).items()}
    value = {
        'version': VERSION, 'kind': 'fresh_start_plan_v1', 'root': str(root),
        'config': str(config), 'config_hash': digest(cfg), 'config_version': cfg.get('version'),
        'source_hash': runtime.implementation_hash(),
        'shared': {'pacing_db': str(pacing_db), 'discovery_db': str(discovery_db)},
        'taker': taker, 'amount_raw': amount_raw, 'pool_fee_bps': pool_fee_bps,
        'stores': paths, 'initialisers': INITIALISERS,
        'budgets': {'monitoring_requests_per_rolling_hour': allowance.NEW_MONITORING,
                    'investigation_admissions_per_day': allowance.NEW_DAILY,
                    'investigation_queue': allowance.NEW_QUEUE, 'investigation_lifetime_requests': 18},
        'rotate_from': None if rotate_from is None else str(rotate_from),
        'no_successor_pins': True, 'no_reconciliation_receipts': True,
        'entry_authorized': False, 'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False,
    }
    value['units'] = unit_arguments(value, scheduler_identity='<DEVICE:INODE of paper-scheduler.lock, printed by apply>')
    return value


def unit_arguments(p, *, scheduler_identity):
    """Arguments each desk-* unit needs; ExecStart is the python interpreter plus `argv`."""
    s = p['stores']
    pacing_env = f"DESK_PROVIDER_PACING_DB={p['shared']['pacing_db']}"
    sched_env = f'DESK_PAPER_SCHEDULER_IDENTITY={scheduler_identity}'
    common = ['--config', p['config'], '--research-db', s['research_db'], '--evidence-db', s['evidence_db'],
              '--ledger-db', s['ledger_db']]
    return {
        'desk-paper-entry-dispatcher': {
            'environment': [pacing_env, sched_env],
            'argv': ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'entry', '--',
                     *common, '--discovery-db', p['shared']['discovery_db'], '--pacing-db', p['shared']['pacing_db'],
                     '--journal', s['journal'], '--taker', p['taker'], '--amount-raw', str(p['amount_raw']),
                     '--pool-fee-bps', p['pool_fee_bps']]},
        'desk-paper-held-cycle': {
            'environment': [pacing_env, sched_env],
            'argv': ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'held', '--',
                     *common, '--pool-fee-bps', p['pool_fee_bps'], '--systemd-credentials']},
        'desk-paper-monitor': {
            'environment': [sched_env],
            'argv': ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'expire', '--',
                     '--db', s['ledger_db'], '--config', p['config']]},
        'desk-decisions': {
            'environment': [sched_env],
            'argv': ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'decisions', '--',
                     '--db', s['research_db'], '--journal', s['decisions_db'], '--evidence-db', s['evidence_db']]},
        'desk-dashboard': {
            'environment': [f"DESK_PAPER_SCHEDULER_LOCK={s['scheduler_lock']}"],
            'argv': ['-m', 'desk', '--secrets-file', '%d/provider-keys.json', 'serve', '--db', s['research_db'],
                     '--port', '8765']},
    }


def _check_pacing_environment(pacing_db):
    if os.environ.get(pacing.ENV) != str(pacing_db):
        raise FreshStartError(f'{pacing.ENV} must equal --pacing-db (explicit shared pacing; never reset)')


def _make_private_dir(path):
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _identity(path):
    s = Path(path).stat()
    return {'path': str(path), 'device': s.st_dev, 'inode': s.st_ino}


def apply(plan_value, *, now=None):
    """Create the new root and every store. Fails closed; a failed root is retained, never reused."""
    root = _new_root(plan_value['root'])
    pacing_db = Path(plan_value['shared']['pacing_db'])
    _check_pacing_environment(pacing_db)
    config = Path(plan_value['config'])
    cfg = cli._config(config)
    if digest(cfg) != plan_value['config_hash'] or runtime.implementation_hash() != plan_value['source_hash']:
        raise FreshStartError('Config or implementation changed since the approved plan')
    stores = {k: Path(v) for k, v in plan_value['stores'].items()}
    _make_private_dir(root)
    _make_private_dir(stores['journal'].parent)
    # --- research: queue + continuous-scanning allowance (daily 1000, queue 25)
    jobs = JobPersistence(stores['research_db'])
    jobs.upgrade_allowance(at=int(time.time() if now is None else now), provenance=allowance.PROVENANCE)
    # --- evidence + ownership progress tables (18-request lifetime ceiling is the admission default)
    progress = HistoryProgress(EvidenceStore(stores['evidence_db']))
    # --- fresh ledger: records its own implementation hash; no successor/extension rows
    cycle.initialize(stores['ledger_db'], cfg)
    # --- monitoring budget: provision (60) then the approved 3600/rolling-hour ceiling, under the
    # canonical research -> evidence -> ledger lock order. upgrade_existing() is not used because it
    # requires >=1 retained admission to prove its lock context; a store with none has nothing to prove.
    _provision_monitoring(progress.store, stores['ledger_db'], cfg, stores['research_db'], now)
    # --- decisions journal
    with closing(sqlite3.connect(stores['decisions_db'].as_uri(), uri=True, timeout=15, isolation_level=None)) as c:
        decision_runner._initialize_journal(c)
    os.chmod(stores['decisions_db'], 0o600)
    # --- scheduler lock (inode identity is pinned by the units)
    fd = os.open(stores['scheduler_lock'], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    # --- dispatcher journal with activated context (reuses the dispatcher's own plan/initialize)
    context = dispatcher.plan(
        config=str(config), research_db=str(stores['research_db']), evidence_db=str(stores['evidence_db']),
        ledger_db=str(stores['ledger_db']), discovery_db=plan_value['shared']['discovery_db'],
        pacing_db=str(pacing_db), journal=str(stores['journal']), taker=plan_value['taker'],
        amount_raw=plan_value['amount_raw'], pool_fee_bps=plan_value['pool_fee_bps'])
    dispatcher.initialize(context, approved_context_hash=digest(context))
    _self_check(context, stores)
    scheduler_identity = '%d:%d' % (stores['scheduler_lock'].stat().st_dev, stores['scheduler_lock'].stat().st_ino)
    manifest = {
        'version': VERSION, 'kind': 'fresh_start_manifest_v1', 'created_utc': _utc(now),
        'root': str(root), 'config': str(config), 'config_hash': digest(cfg), 'config_version': cfg.get('version'),
        'implementation_hash': runtime.implementation_hash(), 'plan_hash': digest(plan_value),
        'shared': plan_value['shared'], 'dispatcher_context_hash': digest(context),
        'scheduler_identity': scheduler_identity,
        'stores': {k: _identity(v) for k, v in stores.items() if k != 'manifest'},
        'rotated_from': plan_value['rotate_from'],
        'entry_authorized': False, 'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False,
    }
    manifest['units'] = unit_arguments(plan_value, scheduler_identity=scheduler_identity)
    fd = os.open(stores['manifest'], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical(manifest))
    return manifest


def _self_check(context, stores):
    """The new set must pass the real gates with no receipts or pins before it is declared ready."""
    with terminal.verified_bytes_scope():
        dispatcher._preflight(context)
    store = EvidenceStore(stores['evidence_db'], read_only=True)
    if terminal.gate(store, stores['research_db'], ()):
        raise FreshStartError('Fresh terminal gate is not clear')


def _provision_monitoring(store, ledger, cfg, research, now):
    from desk.paper_cycle import _lock
    from desk.paper_observe_cli import _worker_lock
    with _worker_lock(Path(research)) as worker:
        if worker is None:
            raise FreshStartError('Research worker busy')
        with _lock(str(store.path) + '.ownership-invocation.lock') as locked:
            if not locked:
                raise FreshStartError('Evidence invocation busy')
            with _lock(str(ledger) + '.paper-cycle.lock') as locked:
                if not locked:
                    raise FreshStartError('Paper cycle busy')
                budget = MonitoringBudget(store, ledger, cfg)
                budget.provision()
                clock = time.time() if now is None else now
                with closing(sqlite3.connect(store.path.resolve().as_uri() + '?mode=rw', uri=True,
                                             timeout=20, isolation_level=None)) as c:
                    c.execute('BEGIN IMMEDIATE')
                    try:
                        prepared = budget.prepare_upgrade(c, at=clock, provenance=allowance.PROVENANCE)
                        budget.activate_upgrade(c, prepared)
                        c.commit()
                    except BaseException:
                        c.rollback()
                        raise
                snapshot = budget.snapshot()
                if snapshot.get('status') != 'AVAILABLE' or snapshot.get('blockers'):
                    raise FreshStartError('Fresh monitoring budget not available after provisioning')


def verify_flat(old_root):
    """Read-only proof that the old store set is flat; the old root is never modified.

    Uses SQLite immutable=1 (no locks, no -shm/-wal creation) and therefore refuses a
    root with a non-empty -wal sidecar: that ledger is not quiesced.
    """
    old_root = _absolute(old_root, 'from')
    _no_symlink_chain(old_root, 'from')
    manifest_path = old_root / MANIFEST
    if not old_root.is_dir() or not manifest_path.is_file():
        raise FreshStartError('Old root with a fresh-start manifest required')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('kind') != 'fresh_start_manifest_v1' or manifest.get('root') != str(old_root):
        raise FreshStartError('Old manifest invalid')
    ledger = Path(manifest['stores']['ledger_db']['path'])
    if ledger.parent != old_root:
        raise FreshStartError('Old ledger is not inside the old root')
    wal = Path(str(ledger) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise FreshStartError('Old ledger has an un-checkpointed WAL; quiesce services first')
    # Structure/journal check only: the old ledger's runtime identity is expected to differ from the
    # current code (that is why we rotate), so read_checkpoint()'s runtime check is deliberately not used.
    with closing(sqlite3.connect(ledger.as_uri() + '?immutable=1', uri=True)) as c:
        c.execute('BEGIN')
        row = c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row is None:
            raise FreshStartError('Old ledger has no checkpoint')
        try:
            state = validate_checkpoint(c, row[0])
        except RecoveryRequired:
            raise FreshStartError('Old ledger checkpoint invalid; rotation refused') from None
    if state['positions']:
        raise FreshStartError('Old ledger has an open position or unresolved exit; rotation refused')
    if state['mode'] not in OPEN_OK_MODES:
        raise FreshStartError('Old ledger mode must be RUNNING or ENTRY_PAUSED; rotation refused')
    return {'old_root': str(old_root), 'mode': state['mode'], 'positions': 0, 'manifest_hash': digest(manifest)}


def tree_digest(root):
    """Byte digest of a directory tree (names, modes, contents) for before/after comparison."""
    import hashlib
    h = hashlib.sha256()
    for p in sorted(Path(root).rglob('*')):
        info = p.lstat()
        h.update(f'{p.relative_to(root)}|{stat.S_IMODE(info.st_mode)}|{info.st_size}\n'.encode())
        if p.is_file() and not p.is_symlink():
            h.update(p.read_bytes())
    return h.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument('command', choices=('plan', 'apply', 'rotate'))
    parser.add_argument('--root', '--to', dest='root', required=True)
    parser.add_argument('--from', dest='old_root')
    parser.add_argument('--config', required=True)
    parser.add_argument('--pacing-db', required=True)
    parser.add_argument('--discovery-db', required=True)
    parser.add_argument('--taker', default=PRODUCTION_TAKER)
    parser.add_argument('--amount-raw', type=int, default=100_000_000)
    parser.add_argument('--pool-fee-bps', default='25')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--approved-plan-hash')
    a = parser.parse_args(argv)
    try:
        if (a.command == 'rotate') != (a.old_root is not None):
            raise FreshStartError('--from is required for rotate and only valid there')
        flat = verify_flat(a.old_root) if a.command == 'rotate' else None
        value = plan(root=a.root, config=a.config, pacing_db=a.pacing_db, discovery_db=a.discovery_db,
                     taker=a.taker, amount_raw=a.amount_raw, pool_fee_bps=a.pool_fee_bps, rotate_from=a.old_root)
        if flat is not None:
            value['old_flat_proof'] = flat
        if a.command == 'plan' or not a.execute:
            print(canonical({'status': 'PLAN' if a.command == 'plan' else 'DRY_RUN', 'plan': value,
                             'plan_hash': digest(value), 'entry_authorized': False}))
            return 0
        if a.approved_plan_hash != digest(value):
            raise FreshStartError('Explicit approved plan hash required (see plan output)')
        manifest = apply(value)
        print(canonical({'status': 'APPLIED', 'manifest': manifest, 'entry_authorized': False}))
        return 0
    except (FreshStartError, ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print(canonical({'status': 'BLOCKED', 'reason': str(error)[:200], 'entry_authorized': False,
                         'live_readiness': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
