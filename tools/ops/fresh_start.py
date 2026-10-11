"""Fresh-start experiment bootstrap and flat-ledger rotation; dry run unless --execute.

Creates a brand-new, empty store set for one experiment version using only the
repository's own schema creators and activation APIs. It never opens, migrates,
resets or deletes an old store set, never touches the shared provider-pacing or
discovery databases (the shared pacing database is only validated), never reads
provider credentials and never performs provider I/O, signing or broadcasting.

    python -m tools.ops.fresh_start plan   --root R --config C --pacing-db P --discovery-db D
    python -m tools.ops.fresh_start apply  --root R ... --execute --approved-plan-hash H
    python -m tools.ops.fresh_start rotate --from OLD --to R ... --execute --approved-plan-hash H
    python -m tools.ops.fresh_start render-dropins --root R --out DIR
    python -m tools.ops.fresh_start render-units   --root R --out DIR --release-dir REL

`apply`/`rotate` are dry runs (they print the plan) without --execute. A real `apply`/`rotate` runs as the
service user (`--service-user`, default solana-desk) or as root with `--chown <service user>`.
`render-dropins` turns the applied root's manifest into one complete systemd drop-in per unit.
"""
import argparse
from contextlib import closing, contextmanager
import datetime
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
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
SERVICE_USER = 'solana-desk'
HELD_BLOCKER = 'RUNTIME_REVIEW_AND_PATH_BINDINGS_REQUIRED'   # the deploy/ template's own held-cycle blocker
BACKUP_PARENT = '/var/backups/solana-desk'
DROPIN_MARKER = '# desk-fresh-start-managed v1'
DROPIN_NAME = '70-fresh-store.conf'
# Units whose base template carries ConditionPathExists= on an OLD store path. A drop-in that only
# changes ExecStart would leave the old condition in force, so the unit would be silently skipped
# (condition failed) or, worse, keyed to the archived store. Each is reset and re-pointed.
CONDITION_STORE = {
    'desk-paper-entry-dispatcher': 'journal',
    'desk-paper-held-cycle': 'ledger_db',
    'desk-paper-monitor': 'ledger_db',
    'desk-decisions': 'research_db',
}
# Wall timeouts the base units must carry (T09 F8/F11): entry worst case ~282 s plus pacing waits, held 10 s pass
# deadline plus export. A drop-in that resets ExecStart must not silently fall back to a stock 180 s.
TIMEOUT_START = {'desk-paper-entry-dispatcher': 600, 'desk-paper-held-cycle': 120, 'desk-paper-monitor': 60,
                 'desk-decisions': 60, 'desk-backup': 300}
# The only systemd specifier a rendered command line may contain (the credentials directory).
SPECIFIER_ARGS = frozenset({'%d/provider-keys.json'})


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
         pool_fee_bps='25', rotate_from=None, enable_held=False, backup_dir=None):
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
    if cfg.get('paper_usd_valuation_version') not in (1, 2):
        raise FreshStartError('Explicit Kraken experiment (usd valuation v1 or v2) required')
    if len({root, config, pacing_db, discovery_db}) != 4 or root in (config.parent, pacing_db.parent, discovery_db.parent):
        raise FreshStartError('Distinct context paths required; shared stores must live outside the new root')
    if rotate_from is not None:
        old = Path(rotate_from)
        if root == old or root in old.parents or old in root.parents:
            raise FreshStartError('New root must be a sibling store set, never inside or around the old one')
    paths = {k: str(v) for k, v in paths_for(root).items()}
    backup_dir = _absolute(backup_dir if backup_dir is not None else f'{BACKUP_PARENT}/fresh-{root.name}', 'backup-dir')
    if type(enable_held) is not bool:
        raise FreshStartError('enable_held must be explicit true/false')
    if backup_dir == root or root in backup_dir.parents or backup_dir in root.parents:
        raise FreshStartError('Backups must live outside the store root')
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
        'enable_held': enable_held, 'backup_dir': str(backup_dir),
        'notes': held_notes(enable_held),
        'no_successor_pins': True, 'no_reconciliation_receipts': True,
        'entry_authorized': False, 'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False,
    }
    value['units'] = unit_arguments(value, scheduler_identity='<DEVICE:INODE of paper-scheduler.lock, printed by apply>')
    value['unit_sections'] = unit_sections(value)
    return value


def held_notes(enable_held):
    if enable_held:
        return ['desk-paper-held-cycle is emitted WITHOUT --dependency-blocker (--enable-held): it will monitor '
                'and exit open positions. Enable it before enabling the entry timer.']
    return [f'desk-paper-held-cycle is emitted WITH --dependency-blocker {HELD_BLOCKER}: it exits BLOCKED (rc 2) '
            'and does NOT monitor positions. Re-plan with --enable-held (a new plan hash) before enabling entries.']


def unit_arguments(p, *, scheduler_identity):
    """Arguments each desk-* unit needs; ExecStart is the python interpreter plus `argv`.

    Keys are unit names without `.service`; `environment`/`argv` are lowercase, argv has no interpreter.
    """
    s = p['stores']
    pacing_env = f"DESK_PROVIDER_PACING_DB={p['shared']['pacing_db']}"
    sched_env = f'DESK_PAPER_SCHEDULER_IDENTITY={scheduler_identity}'
    common = ['--config', p['config'], '--research-db', s['research_db'], '--evidence-db', s['evidence_db'],
              '--ledger-db', s['ledger_db']]
    held = ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'held', '--',
            *common, '--pool-fee-bps', p['pool_fee_bps'], '--systemd-credentials']
    if not p.get('enable_held'):
        held += ['--dependency-blocker', HELD_BLOCKER]
    roots = Path(s['manifest']).parent
    stamp = ("import datetime,sys;from tools.ops import backup;"
             "d=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ');"
             f"sys.exit(backup.main(['--data',{str(roots)!r},'--destination',{p['backup_dir']!r}+'/daily-'+d,"
             f"'--label','daily','--expect-count',{str(len(STORES))!r}]))")
    return {
        'desk-paper-entry-dispatcher': {
            'environment': [pacing_env, sched_env],
            'argv': ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'entry', '--',
                     *common, '--discovery-db', p['shared']['discovery_db'], '--pacing-db', p['shared']['pacing_db'],
                     '--journal', s['journal'], '--taker', p['taker'], '--amount-raw', str(p['amount_raw']),
                     '--pool-fee-bps', p['pool_fee_bps'], '--execute', '--systemd-credentials']},
        'desk-paper-held-cycle': {'environment': [pacing_env, sched_env], 'argv': held},
        'desk-paper-monitor': {
            'environment': [sched_env],
            'argv': ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'expire', '--',
                     '--db', s['ledger_db'], '--config', p['config']]},
        'desk-decisions': {
            'environment': [sched_env],
            'argv': ['-m', 'tools.paper_scheduler', '--research-db', s['research_db'], '--mode', 'decisions', '--',
                     '--db', s['research_db'], '--journal', s['decisions_db'], '--evidence-db', s['evidence_db']]},
        'desk-dashboard': {
            # Jobs.once() leases the scheduler lock only when DESK_PAPER_SCHEDULER_LOCK is set and then
            # requires the reviewed inode identity; /api/paper reads the selected ledger from the data dir.
            # DESK_PROVIDER_PACING_DB puts dashboard scans on the shared 2 s pacing (T09 F11).
            'environment': [f"DESK_PAPER_SCHEDULER_LOCK={s['scheduler_lock']}", sched_env,
                            f"DESK_PAPER_LEDGER_DB={s['ledger_db']}", pacing_env],
            'argv': ['-m', 'desk', '--secrets-file', '%d/provider-keys.json', 'serve', '--db', s['research_db'],
                     '--port', '8765']},
        # desk.backup needs launches/raw/active-paper stores the fresh set does not have; tools.ops.backup
        # copies every *.sqlite under the root and refuses an existing destination, hence the dated name.
        'desk-backup': {'environment': [], 'argv': ['-c', stamp]},
    }


def unit_sections(p):
    """Non-ExecStart systemd settings each drop-in must carry (full-drop-in content)."""
    s = p['stores']
    root = str(Path(s['manifest']).parent)
    pacing_dir = str(Path(p['shared']['pacing_db']).parent)
    result = {}
    for unit in ('desk-paper-entry-dispatcher', 'desk-paper-held-cycle', 'desk-paper-monitor',
                 'desk-decisions', 'desk-dashboard', 'desk-backup'):
        writable = [root]
        if unit in ('desk-paper-entry-dispatcher', 'desk-paper-held-cycle', 'desk-dashboard'):
            # The shared pacing database uses a rollback journal (<db>-journal is created and removed per write),
            # so SQLite needs its DIRECTORY writable; a single-file ReadWritePaths would break the 2 s pacing.
            # The archived stores in that directory are protected by chmod 0400 (RUNBOOK step 4) and by the cutover
            # archived-store guard, not by this setting.
            writable.append(pacing_dir)
        if unit == 'desk-backup':
            writable.append(p['backup_dir'])
        result[unit] = {'condition_path_exists': s[CONDITION_STORE[unit]] if unit in CONDITION_STORE else None,
                        'read_write_paths': writable, 'timeout_start_sec': TIMEOUT_START.get(unit)}
    return result


# ---- systemd rendering ------------------------------------------------------------------------
def _word(value, *, command=True):
    """Quote one word for a systemd unit: `%`->`%%`, quotes/backslashes escaped, and for ExecStart
    (command=True) `$`->`$$`; `$` is not special in Environment=."""
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FreshStartError('Unrenderable systemd word')
    if value in SPECIFIER_ARGS:
        return value
    text = value.replace('%', '%%')
    if command:
        text = text.replace('$', '$$')
    if re.fullmatch(r'[A-Za-z0-9_@%:,./=+$-]+' if command else r'[A-Za-z0-9_@%:,./=+-]+', text):
        return text
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def exec_line(argv):
    return ' '.join(_word(x) for x in [PYTHON, *argv])


def _assignment(key, value):
    if not isinstance(value, str) or '=' in key or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise FreshStartError('Unrenderable systemd setting')
    return f'{key}={value}'


def _path_word(path):
    if not str(path).startswith('/'):
        raise FreshStartError('Absolute path required')
    return _word(str(path))


def render_dropin(unit, manifest, *, marker=DROPIN_MARKER):
    """Complete drop-in for one unit, from an applied manifest. Marker is the first line."""
    if not marker.startswith('#') or '\n' in marker:
        raise FreshStartError('Marker must be a single comment line')
    spec = manifest['units'][unit]
    sections = manifest['unit_sections'][unit]
    lines = [marker, f'# unit: {unit}.service  install as: {unit}.service.d/{DROPIN_NAME}',
             f"# plan-hash: {manifest['plan_hash']}  config-version: {manifest['config_version']}"]
    condition = sections['condition_path_exists']
    if condition is not None:
        lines += ['[Unit]', 'ConditionPathExists=', 'ConditionPathExists=' + _path_word(condition)]
    lines.append('[Service]')
    lines.append('Environment=')
    for item in spec['environment']:
        lines.append('Environment=' + _word(_assignment(*item.split('=', 1)), command=False))
    lines.append('ReadWritePaths=' + ' '.join(_path_word(x) for x in sections['read_write_paths']))
    if sections.get('timeout_start_sec') is not None:
        lines.append('TimeoutStartSec=%d' % sections['timeout_start_sec'])
    lines += ['ExecStart=', 'ExecStart=' + exec_line(spec['argv'])]
    return '\n'.join(lines) + '\n'


def render_dropins(root, out, *, marker=DROPIN_MARKER):
    """Write one `<unit>.conf` per unit into a new directory `out`. Never overwrites."""
    root = _absolute(root, 'root')
    _no_symlink_chain(root, 'root')
    manifest = read_manifest(root)
    lock = root / SCHEDULER_LOCK
    info = lock.stat()
    if manifest['scheduler_identity'] != '%d:%d' % (info.st_dev, info.st_ino):
        raise FreshStartError('Scheduler lock identity differs from the manifest; refuse to render')
    out = _absolute(out, 'out')
    _no_symlink_chain(out, 'out')
    if os.path.lexists(out):
        raise FreshStartError('out must not exist yet')
    if not out.parent.is_dir() or out.parent.resolve() != out.parent:
        raise FreshStartError('out parent must be an existing canonical directory')
    rendered = {unit: render_dropin(unit, manifest, marker=marker) for unit in sorted(manifest['units'])}
    out.mkdir(mode=0o755)
    written = {}
    for unit, text in rendered.items():
        fd = os.open(out / f'{unit}.conf', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        written[unit] = str(out / f'{unit}.conf')
    return {'out': str(out), 'dropins': written, 'install_name': DROPIN_NAME, 'plan_hash': manifest['plan_hash']}


TEMPLATE_DIR = Path(__file__).resolve().parents[2] / 'deploy' / 'fresh'
DEFAULT_STATE_DIR = '/var/lib/solana-desk-health'
# Optional research / fast-exit helpers (T32G): rendered into <out>/research/ so the RUNBOOK's default install
# (`install $UNITS/*.service $UNITS/*.timer ...`) can never pick them up; an explicit RUNBOOK step installs them.
RESEARCH_UNITS = frozenset({'desk-counterfactual.service', 'desk-counterfactual.timer', 'desk-held-watcher.service',
                            'desk-paper-held-cycle.path'})
RESEARCH_DIR = 'research'
UNIT_SUFFIXES = ('.service', '.timer', '.path')
LATCH_TEMPLATE = 'dropins/70-entry-latch.conf'
LATCH_OUT = 'desk-paper-entry-dispatcher.service.d/70-entry-latch.conf'
SAFE_VALUE = re.compile(r'[A-Za-z0-9_@%:,./=+-]+')


DEFAULT_ARCHIVED_ROOT = '/var/lib/solana-desk'


def render_units(root, out, *, release_dir, state_dir=DEFAULT_STATE_DIR, templates=TEMPLATE_DIR,
                 archived_root=DEFAULT_ARCHIVED_ROOT):
    """Substitute the deploy/fresh placeholders from an applied root's manifest into a NEW directory `out`.

    Writes every unit/timer template plus the entry-latch drop-in (marker first line, so cutover rollback removes
    it). The optional research units (RESEARCH_UNITS, including the `.path` unit) go to `<out>/research/`, outside the
    default install glob. Nothing is installed or enabled; the operator copies the files (RUNBOOK steps 7a and 11). Refuses any placeholder
    it cannot fill and any value a unit file would need quoting for."""
    root = _absolute(root, 'root')
    _no_symlink_chain(root, 'root')
    manifest = read_manifest(root)
    info = (root / SCHEDULER_LOCK).stat()
    if manifest['scheduler_identity'] != '%d:%d' % (info.st_dev, info.st_ino):
        raise FreshStartError('Scheduler lock identity differs from the manifest; refuse to render')
    release_dir = _absolute(release_dir, 'release-dir')
    state_dir = _absolute(state_dir, 'state-dir')
    archived_root = _absolute(archived_root, 'archived-root')
    pacing, discovery = Path(manifest['shared']['pacing_db']), Path(manifest['shared']['discovery_db'])
    values = {
        'FRESH_ROOT': str(root), 'RELEASE_DIR': str(release_dir), 'CONFIG': manifest['config'],
        'SCHEDULER_IDENTITY': manifest['scheduler_identity'], 'PACING_DB': str(pacing),
        'PACING_DIR': str(pacing.parent), 'DISCOVERY_DB': str(discovery), 'DISCOVERY_DIR': str(discovery.parent),
        'TAKER': manifest['taker'], 'AMOUNT_RAW': str(manifest['amount_raw']),
        'POOL_FEE_BPS': manifest['pool_fee_bps'], 'BACKUP_ROOT': manifest['backup_dir'], 'STATE_DIR': str(state_dir),
        'ARCHIVED_ROOT': str(archived_root),
    }
    for key, value in values.items():
        if not SAFE_VALUE.fullmatch(value):
            raise FreshStartError(f'{key} needs systemd quoting; refuse to render')
    templates = Path(templates)
    sources = sorted(p for p in templates.iterdir() if p.suffix in UNIT_SUFFIXES)
    missing = sorted(RESEARCH_UNITS - {p.name for p in sources})
    if missing:
        raise FreshStartError(f'research unit templates missing: {missing}')
    sources.append(templates / LATCH_TEMPLATE)
    rendered = {}
    for source in sources:
        text = source.read_text()
        for key, value in values.items():
            text = text.replace(f'<{key}>', value)
        left = sorted(set(re.findall(r'<[A-Z_]+>', text)))
        if left:
            raise FreshStartError(f'{source.name}: unfilled placeholders {left}')
        name = LATCH_OUT if source.name == '70-entry-latch.conf' else source.name
        rendered[f'{RESEARCH_DIR}/{name}' if name in RESEARCH_UNITS else name] = text
    out = _absolute(out, 'out')
    _no_symlink_chain(out, 'out')
    if os.path.lexists(out):
        raise FreshStartError('out must not exist yet')
    if not out.parent.is_dir() or out.parent.resolve() != out.parent:
        raise FreshStartError('out parent must be an existing canonical directory')
    out.mkdir(mode=0o755)
    for name, text in rendered.items():
        target = out / name
        target.parent.mkdir(mode=0o755, exist_ok=True)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    return {'out': str(out), 'files': sorted(rendered), 'plan_hash': manifest['plan_hash'],
            'research': sorted(n for n in rendered if n.startswith(RESEARCH_DIR + '/'))}


def read_manifest(root):
    path = Path(root) / MANIFEST
    if not path.is_file() or path.is_symlink():
        raise FreshStartError('Fresh-start manifest required')
    manifest = json.loads(path.read_text())
    if (manifest.get('kind') != 'fresh_start_manifest_v1' or manifest.get('root') != str(root)
            or set(manifest.get('units', ())) != set(manifest.get('unit_sections', ()))):
        raise FreshStartError('Manifest invalid')
    return manifest


def _check_pacing_environment(pacing_db):
    if os.environ.get(pacing.ENV) != str(pacing_db):
        raise FreshStartError(f'{pacing.ENV} must equal --pacing-db (explicit shared pacing; never reset)')


def _make_private_dir(path):
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _identity(path):
    s = Path(path).stat()
    return {'path': str(path), 'device': s.st_dev, 'inode': s.st_ino}


def _owner(service_user, chown):
    """Return (pwd entry, must_chown). Refuses BEFORE anything is created."""
    try:
        entry = pwd.getpwnam(service_user)
    except KeyError:
        raise FreshStartError(f'Service user {service_user!r} does not exist') from None
    if chown is not None and chown != service_user:
        raise FreshStartError('--chown must name the service user')
    if os.geteuid() == entry.pw_uid:
        return entry, False
    if os.geteuid() == 0 and chown == service_user:
        return entry, True
    raise FreshStartError(f'apply must run as the service user {service_user!r} '
                          f'(or as root with --chown {service_user}); stores owned by anyone else fail the '
                          'scheduler lease owner check and SQLite writes')


def _finalize(root, entry, must_chown):
    """Every created entry: no links, owner = service user, dirs 0700, files 0600 (chown first, verify after)."""
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise FreshStartError('Unexpected symlink in new root')
        if must_chown and (info.st_uid, info.st_gid) != (entry.pw_uid, entry.pw_gid):
            os.lchown(path, entry.pw_uid, entry.pw_gid)
        if stat.S_IMODE(info.st_mode) != (0o700 if stat.S_ISDIR(info.st_mode) else 0o600):
            os.chmod(path, 0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
    for path in [root, *sorted(root.rglob('*'))]:
        info = path.lstat()
        if info.st_uid != entry.pw_uid or stat.S_IMODE(info.st_mode) != (0o700 if stat.S_ISDIR(info.st_mode) else 0o600):
            raise FreshStartError(f'Ownership/mode verification failed for {path}')


def _check_backup_dir(value):
    """Fail BEFORE anything is created: the backup unit and the healthcheck both need this exact directory."""
    path = _absolute(value, 'backup-dir')
    _no_symlink_chain(path, 'backup-dir')
    if not path.parent.is_dir() or path.parent.resolve() != path.parent:
        raise FreshStartError(f'backup-dir parent {path.parent} must exist (canonical directory) before apply')
    if os.path.lexists(path) and not path.is_dir():
        raise FreshStartError('backup-dir exists and is not a directory')
    return path


def _make_backup_dir(path, entry, must_chown):
    """0700 directory owned by the service user (created if missing, never recursively, never emptied)."""
    if not path.exists():
        path.mkdir(mode=0o700)
    if must_chown:
        os.lchown(path, entry.pw_uid, entry.pw_gid)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != entry.pw_uid or stat.S_IMODE(info.st_mode) & 0o077:
        raise FreshStartError('backup-dir must be a real directory owned by the service user with mode 0700')


def apply(plan_value, *, now=None, service_user=SERVICE_USER, chown=None):
    """Create the new root and every store. Fails closed; a failed root is retained, never reused."""
    owner, must_chown = _owner(service_user, chown)
    root = _new_root(plan_value['root'])
    backup_dir = _check_backup_dir(plan_value['backup_dir'])
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
        'rotated_from': plan_value['rotate_from'], 'service_user': service_user,
        'taker': plan_value['taker'], 'amount_raw': plan_value['amount_raw'], 'pool_fee_bps': plan_value['pool_fee_bps'],
        'enable_held': plan_value['enable_held'], 'backup_dir': plan_value['backup_dir'], 'notes': plan_value['notes'],
        'entry_authorized': False, 'execution_status': 'EXECUTION_UNVERIFIED', 'live_readiness': False,
    }
    manifest['units'] = unit_arguments(plan_value, scheduler_identity=scheduler_identity)
    manifest['unit_sections'] = unit_sections(plan_value)
    fd = os.open(stores['manifest'], os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(canonical(manifest))
    _finalize(root, owner, must_chown)
    _make_backup_dir(backup_dir, owner, must_chown)
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


def _old_stores(old_root, ledger=None, evidence=None, journal=None):
    """Old-set store paths: from its fresh-start manifest, or explicit paths for roots this tool did not create."""
    old_root = _absolute(old_root, 'from')
    _no_symlink_chain(old_root, 'from')
    if not old_root.is_dir():
        raise FreshStartError('Old root must be an existing directory')
    manifest_path = old_root / MANIFEST
    found = {}
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('kind') != 'fresh_start_manifest_v1' or manifest.get('root') != str(old_root):
            raise FreshStartError('Old manifest invalid')
        found = {key: Path(manifest['stores'][name]['path'])
                 for key, name in (('ledger', 'ledger_db'), ('evidence', 'evidence_db'), ('journal', 'journal'))}
        found['manifest_hash'] = digest(manifest)
    explicit = {'ledger': ledger, 'evidence': evidence, 'journal': journal}
    result = {'manifest_hash': found.pop('manifest_hash', None)}
    for key, value in explicit.items():
        chosen = _absolute(value, f'old-{key}') if value is not None else found.get(key)
        if chosen is None:
            raise FreshStartError(f'Old root has no fresh-start manifest: pass --old-{key} explicitly (ledger, evidence and journal)')
        _no_symlink_chain(chosen, f'old-{key}')
        if not chosen.is_file():
            raise FreshStartError(f'Old {key} store missing')
        result[key] = chosen
    return {'root': old_root, **result}


@contextmanager
def _ledger_lock(ledger):
    """Non-blocking exclusive paper-cycle lock of the OLD ledger; never creates the lock file."""
    lock = Path(str(ledger) + '.paper-cycle.lock')
    try:
        fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise FreshStartError('Old ledger paper-cycle lock file missing; refuse (cannot prove the ledger is quiesced)') from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FreshStartError('Old ledger paper-cycle lock is busy; quiesce the services first') from None
        yield
    finally:
        os.close(fd)


def _immutable(path):
    """Read-only handle that takes no locks and creates no sidecar; refuses a store with a live WAL."""
    wal = Path(str(path) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise FreshStartError(f'{Path(path).name} has an un-checkpointed WAL; quiesce services first')
    return closing(sqlite3.connect(Path(path).as_uri() + '?immutable=1', uri=True))


def _tables(c):
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def verify_flat(old_root, *, ledger=None, evidence=None, journal=None, _locked=False):
    """Read-only proof that the old store set is flat and quiescent; the old root is never modified.

    Holds the old ledger's paper-cycle lock (non-blocking, refuse if busy) for the whole check; `rotate`
    holds it across the check AND the bootstrap and passes _locked=True. Uses SQLite immutable=1 (no locks,
    no -shm/-wal creation) and refuses any store with a non-empty -wal sidecar. Refuses on: an open
    position / exit_blocked / pending exit, a mode other than RUNNING or ENTRY_PAUSED, a dispatcher intent
    without a result, an unresolved observation pass, or a monitoring reservation without an outcome.
    """
    old = _old_stores(old_root, ledger, evidence, journal)
    if not _locked:
        with _ledger_lock(old['ledger']):
            return verify_flat(old_root, ledger=ledger, evidence=evidence, journal=journal, _locked=True)
    if old['ledger'].parent != old['root'] and ledger is None:
        raise FreshStartError('Old ledger is not inside the old root')
    # Structure/journal check only: the old ledger's runtime identity is expected to differ from the
    # current code (that is why we rotate), so read_checkpoint()'s runtime check is deliberately not used.
    with _immutable(old['ledger']) as c:
        c.execute('BEGIN')
        row = c.execute('SELECT payload FROM state WHERE id=1').fetchone()
        if row is None:
            raise FreshStartError('Old ledger has no checkpoint')
        try:
            state = validate_checkpoint(c, row[0])
        except RecoveryRequired:
            raise FreshStartError('Old ledger checkpoint invalid; rotation refused') from None
    blocked = [m for m, p in state['positions'].items() if p.get('exit_blocked')]
    if state['positions']:
        raise FreshStartError('Old ledger has an open position or unresolved exit' +
                              (f' ({len(blocked)} exit_blocked)' if blocked else '') + '; rotation refused')
    if state['mode'] not in OPEN_OK_MODES:
        raise FreshStartError('Old ledger mode must be RUNNING or ENTRY_PAUSED; rotation refused')
    with _immutable(old['journal']) as c:
        names = _tables(c)
        if not {'intents', 'results'} <= names:
            raise FreshStartError('Old dispatcher journal has no intents/results tables')
        if c.execute('SELECT 1 FROM intents WHERE id NOT IN (SELECT id FROM results) LIMIT 1').fetchone():
            raise FreshStartError('Old dispatcher journal has an intent without a result; rotation refused')
    with _immutable(old['evidence']) as c:
        names = _tables(c)
        if 'paper_observation_passes' in names and c.execute(
                'SELECT 1 FROM paper_observation_passes WHERE outcome_hash IS NULL LIMIT 1').fetchone():
            raise FreshStartError('Old evidence store has an unresolved observation pass; rotation refused')
        if {'paper_monitoring_reservations', 'paper_monitoring_outcomes'} <= names and c.execute(
                'SELECT 1 FROM paper_monitoring_reservations r LEFT JOIN paper_monitoring_outcomes o '
                'ON o.reservation_id=r.id WHERE o.reservation_id IS NULL LIMIT 1').fetchone():
            raise FreshStartError('Old evidence store has a monitoring reservation without an outcome; rotation refused')
    return {'old_root': str(old['root']), 'mode': state['mode'], 'positions': 0, 'manifest_hash': old['manifest_hash'],
            'ledger': str(old['ledger']), 'evidence': str(old['evidence']), 'journal': str(old['journal'])}


def rotate(plan_value, *, old_root, ledger=None, evidence=None, journal=None, now=None,
           service_user=SERVICE_USER, chown=None, approved_plan_hash):
    """Check flatness and bootstrap the new set while holding the old ledger's paper-cycle lock throughout.

    The flat proof is part of the approved plan: it is recomputed under the lock and the approved hash must
    match the plan INCLUDING that proof, before anything is created.
    """
    old = _old_stores(old_root, ledger, evidence, journal)
    with _ledger_lock(old['ledger']):
        flat = verify_flat(old_root, ledger=ledger, evidence=evidence, journal=journal, _locked=True)
        bound = {**plan_value, 'old_flat_proof': flat}
        if approved_plan_hash != digest(bound):
            raise FreshStartError('Explicit approved plan hash required (see plan output)')
        return flat, apply(bound, now=now, service_user=service_user, chown=chown)


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


def _parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'apply', 'rotate'):
        p = sub.add_parser(name, allow_abbrev=False)
        p.add_argument('--root', '--to', dest='root', required=True)
        p.add_argument('--config', required=True)
        p.add_argument('--pacing-db', required=True)
        p.add_argument('--discovery-db', required=True)
        p.add_argument('--taker', default=PRODUCTION_TAKER)
        p.add_argument('--amount-raw', type=int, default=100_000_000)
        p.add_argument('--pool-fee-bps', default='25')
        p.add_argument('--backup-dir', help=f'default {BACKUP_PARENT}/fresh-<root name>; must exist (0700, service user) before the backup timer runs')
        p.add_argument('--enable-held', action='store_true',
                       help='emit desk-paper-held-cycle WITHOUT --dependency-blocker (required to monitor positions)')
        p.add_argument('--execute', action='store_true')
        p.add_argument('--approved-plan-hash')
        p.add_argument('--service-user', default=SERVICE_USER)
        p.add_argument('--chown', help='run as root and chown every created entry to this service user')
        if name == 'rotate':
            p.add_argument('--from', dest='old_root', required=True)
            p.add_argument('--old-ledger')
            p.add_argument('--old-evidence')
            p.add_argument('--old-journal')
    p = sub.add_parser('render-units', allow_abbrev=False,
                       help='fill the deploy/fresh unit templates from an applied root manifest (nothing is installed)')
    p.add_argument('--root', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--release-dir', required=True)
    p.add_argument('--state-dir', default=DEFAULT_STATE_DIR)
    p.add_argument('--archived-root', default=DEFAULT_ARCHIVED_ROOT,
                   help='the archived (old) store root; health/notify units get it as ReadOnlyPaths')
    p = sub.add_parser('render-dropins', allow_abbrev=False,
                       help='write one complete systemd drop-in per unit from an applied root manifest')
    p.add_argument('--root', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--marker', default=DROPIN_MARKER)
    return parser


def main(argv=None):
    a = _parser().parse_args(argv)
    try:
        if a.command == 'render-units':
            print(canonical({'status': 'RENDERED', **render_units(a.root, a.out, release_dir=a.release_dir,
                                                                  state_dir=a.state_dir,
                                                                  archived_root=a.archived_root)}))
            return 0
        if a.command == 'render-dropins':
            print(canonical({'status': 'RENDERED', **render_dropins(a.root, a.out, marker=a.marker)}))
            return 0
        kwargs = dict(root=a.root, config=a.config, pacing_db=a.pacing_db, discovery_db=a.discovery_db,
                      taker=a.taker, amount_raw=a.amount_raw, pool_fee_bps=a.pool_fee_bps,
                      enable_held=a.enable_held, backup_dir=a.backup_dir)
        old = {}
        if a.command == 'rotate':
            old = dict(old_root=a.old_root, ledger=a.old_ledger, evidence=a.old_evidence, journal=a.old_journal)
            if not a.execute:
                flat = verify_flat(**old)                 # read-only; takes and releases the old ledger lock
        value = plan(**kwargs, rotate_from=a.old_root if a.command == 'rotate' else None)
        if a.command == 'plan' or not a.execute:
            if a.command == 'rotate':
                value['old_flat_proof'] = flat
            print(canonical({'status': 'PLAN' if a.command == 'plan' else 'DRY_RUN', 'plan': value,
                             'plan_hash': digest(value), 'entry_authorized': False}))
            return 0
        if a.command == 'rotate':
            _, manifest = rotate(value, **old, service_user=a.service_user, chown=a.chown,
                                 approved_plan_hash=a.approved_plan_hash)
        else:
            if a.approved_plan_hash != digest(value):
                raise FreshStartError('Explicit approved plan hash required (see plan output)')
            manifest = apply(value, service_user=a.service_user, chown=a.chown)
        print(canonical({'status': 'APPLIED', 'manifest': manifest, 'entry_authorized': False}))
        return 0
    except (FreshStartError, ValueError, OSError, sqlite3.Error, KeyError, TypeError) as error:
        print(canonical({'status': 'BLOCKED', 'reason': str(error)[:200], 'entry_authorized': False,
                         'live_readiness': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
