"""Reproduce the production entry gate against COPIES of the stores. Paper only.

Copies every ``*.sqlite`` under ``--data`` into a private ``--workdir`` with the
sqlite backup API (read-only sources), then runs, in a guarded subprocess whose
cwd is ``--release-dir``: the terminal gate, the dispatcher ``plan``/``_preflight``
path (plus a comparison of the activated journal context with a fresh plan), and
the scheduler entry pre-check (``tools.paper_scheduler`` in ``--plan`` mode).

The child process runs under an audit hook that records and refuses network
access, process creation, sqlite connections outside the workdir and any
mutation outside the workdir. Provider keys are never passed to the child and
the tool refuses to start while they are present in this process environment.
Nothing here signs, broadcasts, retries provider work or writes to live stores.

The evidence store, receipts and the dispatcher journal bind ABSOLUTE store
paths (for example the monitoring budget row names the ledger path, and the gate
opens it). To make the gate see identical paths without touching live files, the
child runs in a private mount namespace (``unshare --mount``) where the private
copy directory is bind-mounted over ``--data``: the live files are not even
visible to the child. Where namespaces are unavailable the tool refuses unless
``--no-namespace`` is given; that mode only works for stores that bind no
absolute paths and reports ``isolation: NONE``. Device/inode identities of the
copies differ from live files, so only ``paths.*.device``, ``paths.*.inode`` and
the config copy's path are excluded from the journal-context comparison (reported
as ``excluded_context_fields``). The store paths and the journal path ARE compared.

Fresh layout (T34): the SHARED pacing and discovery databases live outside the experiment root,
and ``--pacing-db``, ``--discovery-db`` and ``--ledger`` therefore also accept an ABSOLUTE path. Such
a store must be canonical (no symlink or ``..`` component) and a regular single-link file; it is copied
into ``<workdir>/.external/<absolute path>`` with the same backup API, and in the namespace that copy is
bind-mounted over its real directory, so the child sees it at the real path while the rest of that
directory (for example the archived old stores) is hidden. An absolute path inside ``--data`` is treated
exactly like the relative name. The external store's directory must not contain or sit inside ``--data`` or
``--workdir``. The same live checks apply (pacing mode 0600, expected owner, no group/world-writable
directory), a ``-wal``/``-shm``/``-journal`` appearing beside an external store or a changed sha256 is a
blocker. Note the real entry scheduler requires the ledger to sit beside the research database, so an
external ledger is copied and gated but the scheduler pre-check will report it.

Requirements: the namespace mode needs Linux ``unshare`` + ``mount`` and either
euid 0 (the VPS case: stores are owned by ``solana-desk``, the tool runs as root,
so ownership is checked against ``--expect-owner``, default the owner of ``--data``)
or unprivileged user namespaces. Where that probe fails the namespace tests skip.

Live-store safety: a plain read-only open of a WAL database creates ``-wal``/``-shm``
beside it (root-owned when run as root, which the service user cannot write).
A WAL-mode source (database header byte 18 == 2) with no ``-wal`` beside it is therefore
read with ``immutable=1`` (no sidecars); a rollback-journal source (for example the pacing
database, rewritten every ~2 s) is never opened immutable. Any sidecar or other file that still appears under
``--data`` is a blocker, and live sources are re-hashed (sha256) after the run.
"""
import sys
sys.dont_write_bytecode = True   # the parent must not write bytecode beside live code/data either
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import stat
import subprocess
import time

MAX_FILES = 64
MAX_PENDING = 256
DEFAULT_MAX_BYTES = 4 * 1024 * 1024 * 1024
FORBIDDEN_ENV = ('HELIUS_API_KEY', 'JUPITER_API_KEY', 'KRAKEN_API_KEY', 'KRAKEN_API_SECRET',
                 'CREDENTIALS_DIRECTORY')
RESULT_PREFIX = 'PREFLIGHT_RESULT:'
# Only device/inode identities and the config copy's path legitimately differ on copies.
SKIPPED_CONTEXT_FIELDS = ('paths.*.device', 'paths.*.inode', 'paths.config.path')
# Stores that may be named by an absolute path outside --data (fresh layout: shared pacing and discovery databases).
EXTERNAL_KEYS = ('ledger', 'pacing_db', 'discovery_db')
EXTERNAL_DIR = '.external'

# Resolves the directory behind a dir_fd. /proc/self/fd exists only on Linux; macOS needs F_GETPATH.
# Anything else fails closed (the hook then refuses the mutation).
FD_PATH_HELPER = r'''
def fd_path(fd, platform=None, fcntl_module=None):
    platform = sys.platform if platform is None else platform
    if platform.startswith('linux'):
        return os.readlink('/proc/self/fd/%d' % fd)
    if platform == 'darwin':
        fcntl_module = fcntl_module if fcntl_module is not None else __import__('fcntl')
        if hasattr(fcntl_module, 'F_GETPATH'):
            raw = fcntl_module.fcntl(fd, fcntl_module.F_GETPATH, b'\0' * 1024)
            return os.fsdecode(raw.split(b'\0', 1)[0])
    raise OSError('dir_fd path resolution unsupported on ' + platform)
'''

CHILD_TEMPLATE = r'''
import contextlib, io, json, os, sys, time
from pathlib import Path
from urllib.parse import unquote, urlparse
spec = json.loads(sys.argv[1])
ROOTS = [os.path.realpath(r) for r in spec['allowed_roots']]
violations = []
NET = {'socket.connect', 'socket.sendto', 'socket.sendmsg', 'socket.getaddrinfo', 'socket.gethostbyname',
       'socket.gethostbyaddr', 'socket.getnameinfo', 'socket.bind'}
PROC = {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork', 'os.forkpty'}
# event -> positions of path arguments and the dir_fd that anchors each (or None).
MUTATE = {'os.remove': [(0, 1)], 'os.rmdir': [(0, 1)], 'os.mkdir': [(0, 2)], 'os.chmod': [(0, 2)],
          'os.chown': [(0, 3)], 'os.utime': [(0, 3)], 'os.truncate': [(0, None)], 'os.mkfifo': [(0, 2)],
          'os.mknod': [(0, 3)], 'os.rename': [(0, 2), (1, 3)], 'os.link': [(0, 2), (1, 3)],
          'os.symlink': [(1, 2)], 'shutil.copyfile': [(0, None), (1, None)], 'shutil.copytree': [(0, None), (1, None)],
          'shutil.move': [(0, None), (1, None)], 'shutil.rmtree': [(0, None)]}
WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

#FD_PATH_HELPER#

def inside(p):
    try:
        if isinstance(p, bytes):
            p = os.fsdecode(p)
        elif not isinstance(p, str):
            p = os.fspath(p)
        if p.startswith('file:'):
            p = unquote(urlparse(p).path)
        if p == os.devnull:
            return True
        q = os.path.realpath(p)
    except Exception:
        return False
    return any(q == r or q.startswith(r + os.sep) for r in ROOTS)

def refuse(kind, event, detail=''):
    violations.append({'kind': kind, 'event': event, 'detail': str(detail)[:200]})
    raise OSError('PREFLIGHT_GUARD: ' + kind + ' blocked (' + event + ')')

def hook(event, args):
    if event in NET:
        refuse('NETWORK', event)
    elif event in PROC:
        refuse('PROCESS', event)
    elif event == 'sqlite3.connect':
        if str(args[0]) != ':memory:' and not inside(args[0]):
            refuse('SQLITE_OUTSIDE_WORKDIR', event, args[0])
    elif event == 'open':
        path, _mode, flags = args
        if not isinstance(path, int) and isinstance(flags, int) and flags & WRITE and not inside(path):
            refuse('WRITE_OUTSIDE_WORKDIR', event, path)
    elif event in MUTATE:
        for index, fd_index in MUTATE[event]:
            target = args[index] if index < len(args) else None
            if not isinstance(target, (str, bytes, os.PathLike)):
                continue
            target = os.fsdecode(target) if isinstance(target, bytes) else os.fspath(target)
            fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
            if isinstance(fd, int) and not isinstance(fd, bool) and fd >= 0 and not os.path.isabs(target):
                try:
                    target = os.path.join(fd_path(fd), target)
                except OSError:
                    refuse('MUTATION_OUTSIDE_WORKDIR', event, 'unresolvable dir_fd')
            if not inside(target):
                refuse('MUTATION_OUTSIDE_WORKDIR', event, target)

import socket
def deny(*a, **k):
    refuse('NETWORK', 'socket.api')
for name in ('connect', 'connect_ex', 'sendto'):
    setattr(socket.socket, name, deny)
socket.create_connection = deny
socket.getaddrinfo = deny
sys.addaudithook(hook)

release = spec['release']
sys.path.insert(0, release)
os.chdir(release)
P = spec['paths']
result = {'stages': {}, 'violations': violations}
mods = {}

def run(name, fn):
    t = time.monotonic()
    try:
        out = {'ok': True}
        out.update(fn())
    except Exception as e:
        out = {'ok': False, 'error': (type(e).__name__ + ': ' + str(e))[:300]}
    out['seconds'] = round(time.monotonic() - t, 3)
    result['stages'][name] = out

def load():
    from desk import runtime_compatibility, paper_terminal_reconciliation, paper_monitor_operator, paper_cycle_cli
    mods.update(runtime=runtime_compatibility, terminal=paper_terminal_reconciliation,
                monitor=paper_monitor_operator, cli=paper_cycle_cli)
    result['implementation_hash'] = runtime_compatibility.implementation_hash()
    return {}

def gate_stage():
    cfg = mods['cli']._config(P['config'])
    with mods['monitor']._context(P['research'], P['evidence'], P['ledger'], cfg) as (store, ledger, state):
        positions, mode = len(state['positions']), state['mode']
        blocked = mods['terminal'].gate(store, Path(P['research']), (), ledger_locked=str(ledger))
    return {'gate_result': blocked, 'positions': positions, 'mode': mode}

def dispatch_args():
    import sqlite3
    from contextlib import closing
    tool = mods['tool']
    journal_context = None
    if Path(P['journal']).exists():
        with closing(sqlite3.connect(Path(P['journal']).as_uri() + '?mode=ro', uri=True)) as c:
            journal_context = tool._read_journal(c)['context'].get(1)
    given = spec['dispatch']
    values = {k: given.get(k) for k in ('taker', 'amount_raw', 'pool_fee_bps')}
    for k in values:
        if values[k] is None and journal_context is not None:
            values[k] = journal_context[k]
    if any(v is None for v in values.values()):
        raise ValueError('taker, amount-raw and pool-fee-bps required when no activated journal context exists')
    args = dict(config=P['config'], research_db=P['research'], evidence_db=P['evidence'], ledger_db=P['ledger'],
                discovery_db=P['discovery'], pacing_db=P['pacing'], journal=P['journal'], **values)
    return args, journal_context

def preflight_stage():
    from tools import paper_entry_dispatcher as tool
    mods['tool'] = tool
    args, journal_context = dispatch_args()
    ctx = tool.plan(**args)
    out = {'context_mismatch': False, 'refusal_reason': None, 'journal_context': 'ABSENT', 'journal_context_diff': []}
    if journal_context is not None:
        def normal(value):
            value = dict(value)
            value['paths'] = {k: v['path'] for k, v in value['paths'].items() if k != 'config'}
            return value
        a, b = normal(ctx), normal(journal_context)
        diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
        out['journal_context'] = 'MATCH_EXCLUDING_IDENTITIES' if not diff else 'MISMATCH'
        out['journal_context_diff'] = diff
        out['context_mismatch'] = bool(diff)
    try:
        tool._preflight(ctx)
    except ValueError as e:
        out['refusal_reason'] = str(e)[:300]
        if str(e) == 'Reviewed context changed':
            out['context_mismatch'] = True
    return out

def scheduler_stage():
    from tools import paper_scheduler as sched
    args, _ = dispatch_args()
    flags = ['--config', args['config'], '--research-db', args['research_db'], '--evidence-db', args['evidence_db'],
             '--ledger-db', args['ledger_db'], '--discovery-db', args['discovery_db'],
             '--pacing-db', args['pacing_db'], '--journal', args['journal'], '--taker', args['taker'],
             '--pool-fee-bps', str(args['pool_fee_bps']), '--amount-raw', str(args['amount_raw']), '--plan']
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = sched.main(['--research-db', args['research_db'], '--mode', 'entry', '--'] + flags)
    text = buffer.getvalue().strip().splitlines()
    try:
        status = json.loads(text[-1]).get('status')
    except Exception:
        status = None
    return {'exit_code': code, 'status': status, 'output_tail': text[-1][:300] if text else ''}

run('import', load)
if result['stages']['import']['ok']:
    run('gate', gate_stage)
    run('preflight', preflight_stage)
    run('scheduler', scheduler_stage)
sys.stdout.write('\n' + spec['prefix'] + json.dumps(result, sort_keys=True) + '\n')
'''
CHILD = CHILD_TEMPLATE.replace('#FD_PATH_HELPER#', FD_PATH_HELPER)


class Refused(Exception):
    pass


class PendingUnreadable(Exception):
    pass


# Injection points for tests: ownership is checked against the EXPECTED service user, never euid
# (the tool runs as root on the VPS while the stores belong to ``solana-desk``).
_stat = os.lstat
_getpwnam = pwd.getpwnam
SIDECAR_SUFFIXES = ('-wal', '-shm', '-journal')
BOOTSTRAP_HINTS = {
    'scheduler_lock': 'created with mode 0600 by the fresh-start bootstrap (tools.ops.fresh_start apply, '
                      'T13F pre-creates the lock); this tool never creates it in the live tree',
    'journal': 'created and activated by the fresh-start bootstrap (tools.ops.fresh_start apply, T13F '
               'activates the dispatcher journal context); this tool has no bypass',
}


def expected_owner(spec, data):
    if spec is None:
        return _stat(data).st_uid, 'DATA_OWNER'
    if spec.isdigit():
        return int(spec), 'FLAG_UID'
    try:
        return _getpwnam(spec).pw_uid, 'FLAG_USER'
    except KeyError:
        raise Refused('--expect-owner user not found: ' + spec) from None


def namespace_prefix():
    """argv prefix that runs a command in a private mount namespace, or None."""
    unshare = shutil.which('unshare')
    if unshare is None or shutil.which('mount') is None:
        return None
    prefix = [unshare, '--mount'] + ([] if os.geteuid() == 0 else ['--map-root-user'])
    try:
        probe = subprocess.run(prefix + ['true'], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return prefix if probe.returncode == 0 else None


def _canonical_dir(value, what):
    p = Path(value)
    if not p.is_absolute() or p.resolve() != p or not p.is_dir():
        raise Refused(f'{what} must be an existing canonical absolute directory')
    return p


def _relative(root, value, what):
    rel = Path(value)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise Refused(f'{what} must be a relative path inside --data')
    return rel


def _external_store(value, what):
    """An absolute store path: canonical (no symlink or alias component), an existing regular single-link file."""
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or '..' in path.parts:
        raise Refused(f'{what} must be a canonical absolute path (no symlink or ".." component)')
    try:
        info = path.lstat()
    except OSError:
        raise Refused(f'{what} does not exist: {path}') from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise Refused(f'{what} must be a regular single-link file: {path}')
    return path


def _mirror(work, path):
    """Where an external store's copy lives inside the workdir (mirrors the absolute path)."""
    return work / EXTERNAL_DIR / str(path).lstrip('/')


def _mount_points(externals):
    """Minimal set of real directories to cover with a bind of their mirrored copy (nested ones ride along)."""
    dirs = sorted({p.parent for p in externals}, key=lambda d: (len(d.parts), str(d)))
    top = []
    for d in dirs:
        if not any(t == d or t in d.parents for t in top):
            top.append(d)
    return top


def _discover(data, max_bytes):
    found, total = [], 0
    for base, dirs, files in os.walk(data, followlinks=False):
        for name in dirs:
            if (Path(base) / name).is_symlink():
                raise Refused('Symlinked directory under --data refused')
        for name in sorted(files):
            if not name.endswith('.sqlite'):
                continue
            path = Path(base) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise Refused(f'Store must be a regular single-link file: {path.relative_to(data)}')
            total += info.st_size
            found.append(path)
    if not found:
        raise Refused('No *.sqlite stores found under --data')
    if len(found) > MAX_FILES or total > max_bytes:
        raise Refused('Store count or size bound exceeded')
    return sorted(found)


def _sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _is_wal_file(path):
    """True only when the database header (byte 18, file-format write version) says WAL."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        head = os.read(fd, 100)
    finally:
        os.close(fd)
    return len(head) >= 20 and head[:16] == b'SQLite format 3\x00' and head[18] == 2


def _source_state(path):
    """Content identity of a live source: size, mtime and sha256 of the file and of its -wal."""
    info = path.stat()
    wal = path.with_name(path.name + '-wal')
    try:
        wal_state = (wal.stat().st_size, _sha256(wal))
    except FileNotFoundError:
        wal_state = None
    return info.st_size, info.st_mtime_ns, _sha256(path), wal_state


def _backup(src, dst):
    """Copy one live store into the workdir with the sqlite backup API (read-only source)."""
    # Same rule as tools.ops.status (T02F): immutable ONLY for a WAL-mode file (header byte 18 == 2)
    # with no -wal beside it: nothing is pending in a sidecar and SQLite then creates no -wal/-shm
    # next to a live store (none owned by root). A rollback-journal store (for example the pacing
    # database, rewritten every ~2 s) can change under the read and is never opened immutable.
    quiet = _is_wal_file(src) and not src.with_name(src.name + '-wal').exists()
    mode = 'mode=ro&immutable=1' if quiet else 'mode=ro'
    with closing(sqlite3.connect(src.as_uri() + '?' + mode, uri=True)) as source, \
            closing(sqlite3.connect(dst)) as target:
        source.backup(target)
    os.chmod(dst, 0o600)
    return {'bytes': dst.stat().st_size, 'sha256': _sha256(dst),
            'read_mode': 'IMMUTABLE_QUIET_WAL' if quiet else 'READ_ONLY'}


def _copy(data, work, sources):
    copies = []
    for src in sources:
        rel = src.relative_to(data)
        dst = work / rel
        dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(dst.parent, 0o700)
        copies.append({'path': str(rel), **_backup(src, dst)})
    return copies


def _copy_external(work, external):
    """Copies of the shared stores that live outside --data, mirrored under ``work/.external/<absolute path>``."""
    copies = []
    for role, src in sorted(external.items()):
        dst = _mirror(work, src)
        directories = [d for d in dst.parents if work in d.parents]
        for directory in reversed(directories):
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        copies.append({'path': str(src), 'external': True, 'role': role, **_backup(src, dst)})
    return copies


def _external_sidecars(paths):
    """Sidecar files (-wal/-shm/-journal) that exist beside the external live stores right now."""
    found = set()
    for path in paths:
        for suffix in SIDECAR_SUFFIXES:
            if os.path.lexists(str(path) + suffix):
                found.add(str(path) + suffix)
    return found


def _sealed_dir(info, group):
    """The shape `cutover seal-archive` gives the two shared directories of an archived root ($OLD, $OLD/discovery): root-owned,
    group = the service group, sticky bit, nothing for others (1770). The group write bit lets the service create its journal and
    lock files; the sticky bit keeps it from deleting or renaming the root-owned archive. Any other shape stays a finding."""
    mode = stat.S_IMODE(info.st_mode)
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and group is not None and info.st_gid == group
            and bool(mode & stat.S_ISVTX) and not mode & 0o007)


def _live_findings(data, names, uid, locations, external):
    """Modes/links the copy would mask (copies are forced to 0600/0700)."""
    found = []
    try:
        group = _stat(data).st_gid          # the service group: --data is created for the service user and its group
    except OSError:
        group = None

    def bad(label, path, forbidden, exact=None, sealed_ok=False):
        try:
            info = _stat(path)
        except OSError:
            found.append(f'{label}:MISSING')
            return
        mode = stat.S_IMODE(info.st_mode)
        if sealed_ok and _sealed_dir(info, group):
            return
        if (exact is not None and mode != exact) or mode & forbidden or info.st_nlink != 1 and path.is_file():
            found.append(f'{label}:MODE_{mode:o}_LINKS_{info.st_nlink}')
        if path.is_file() and info.st_uid != uid:
            found.append(f'{label}:OWNER_{info.st_uid}_EXPECTED_{uid}')
    journal = data / names['journal']
    if journal.exists():
        bad('journal', journal, 0o077)
        bad('journal_dir', journal.parent, 0o077)
    bad('pacing', locations['pacing_db'], 0, exact=0o600)
    for key in ('ledger', 'discovery_db'):
        if key in external:                      # same owner rule as for stores inside --data
            bad('external_' + key, external[key], 0)
    for key, path in external.items():
        bad('external_%s_dir' % key, path.parent, 0o022, sealed_ok=True)
    bad('research_dir', (data / names['research_db']).parent, 0o022)
    bad('scheduler_lock', (data / names['research_db']).parent / 'paper-scheduler.lock', 0o177)
    return found


def _pending(evidence):
    """NULL pass ids from the COPY. A fresh store has no table yet (nothing pending); any read
    error is NOT 'nothing pending': it raises so the caller can block."""
    try:
        with closing(sqlite3.connect(evidence.as_uri() + '?mode=ro', uri=True)) as c:
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_observation_passes'"
                         ).fetchone() is None:
                return [], False
            rows = c.execute('SELECT id FROM paper_observation_passes WHERE outcome_hash IS NULL '
                             'ORDER BY rowid LIMIT ?', (MAX_PENDING + 1,)).fetchall()
    except sqlite3.Error as error:
        raise PendingUnreadable(type(error).__name__) from None
    return [r[0] for r in rows[:MAX_PENDING]], len(rows) > MAX_PENDING


def _details(data, names, blockers):
    """Say exactly what is missing and which bootstrap step creates it (never a bypass)."""
    details = []
    for blocker in blockers:
        if blocker == 'LIVE_scheduler_lock:MISSING':
            path, hint = (names['research_db'].parent / 'paper-scheduler.lock'), BOOTSTRAP_HINTS['scheduler_lock']
        elif blocker == 'JOURNAL_ABSENT':
            path, hint = names['journal'], BOOTSTRAP_HINTS['journal']
        else:
            continue
        details.append({'blocker': blocker, 'missing': str(path), 'under': str(data), 'created_by': hint})
    return details


def _blockers(stages, violations):
    blockers = []
    if violations:
        blockers.append('GUARD_VIOLATION')
    for name in ('import', 'gate', 'preflight', 'scheduler'):
        stage = stages.get(name)
        if stage is None:
            blockers.append(f'{name.upper()}_MISSING')
            continue
        if not stage['ok']:
            blockers.append(f'{name.upper()}_ERROR')
    gate = stages.get('gate', {})
    if gate.get('ok') and gate.get('gate_result') is not None:
        blockers.append('GATE_' + str(gate['gate_result']))
    pre = stages.get('preflight', {})
    if pre.get('ok'):
        if pre['refusal_reason'] is not None:
            blockers.append('PREFLIGHT_REFUSED')
        if pre['journal_context'] == 'ABSENT':
            blockers.append('JOURNAL_ABSENT')
        if pre['context_mismatch']:
            blockers.append('CONTEXT_MISMATCH')
    sched = stages.get('scheduler', {})
    if sched.get('ok') and sched.get('status') != 'PLAN':
        blockers.append('SCHEDULER_' + str(sched.get('status') or 'NO_PLAN'))
    return blockers


def run(args):
    started = time.monotonic()
    present = [name for name in FORBIDDEN_ENV if os.environ.get(name)]
    if present:
        raise Refused('Provider credential environment present: ' + ','.join(present))
    data = _canonical_dir(args.data, '--data')
    release = _canonical_dir(args.release_dir, '--release-dir')
    if not (release / 'desk').is_dir():
        raise Refused('--release-dir must contain desk/')
    python = Path(args.python).resolve()
    if release == data or data in release.parents or data in python.parents:
        raise Refused('--release-dir and --python must not live under --data')
    prefix = None
    if not args.no_namespace:
        prefix = namespace_prefix()
        if prefix is None:
            raise Refused('Private mount namespace unavailable; stores binding absolute paths cannot be '
                          'reproduced. Rerun with --no-namespace to accept that limitation')
    work = Path(args.workdir)
    if not work.is_absolute() or work.resolve() != work or work.exists():
        raise Refused('--workdir must be a canonical absolute path that does not exist yet')
    _canonical_dir(str(work.parent), '--workdir parent')
    if work == data or data in work.parents or work in data.parents:
        raise Refused('--workdir and --data must not contain each other')
    config = Path(args.config)
    if not config.is_absolute() or config.resolve() != config or not config.is_file():
        raise Refused('--config must be an existing canonical absolute file')
    names, external = {}, {}
    for key in ('ledger', 'research_db', 'evidence_db', 'pacing_db', 'discovery_db', 'journal'):
        value, flag = getattr(args, key), '--' + key.replace('_', '-')
        if key in EXTERNAL_KEYS and os.path.isabs(value):
            path = _external_store(value, flag)
            if data in path.parents:
                names[key] = path.relative_to(data)           # absolute but inside the experiment root
            else:
                external[key] = path
        else:
            names[key] = _relative(data, value, flag)
    for path in external.values():
        d = path.parent
        if any(d == x or x in d.parents or d in x.parents for x in (data, work)):
            raise Refused('An external store directory must not contain or sit inside --data or --workdir: ' + str(d))
    sources = _discover(data, args.max_bytes)
    present_rel = {p.relative_to(data) for p in sources}
    for key, rel in names.items():
        if key != 'journal' and rel not in present_rel:
            raise Refused(f'Required store missing under --data: {rel} (a fresh store set is created by the '
                          'fresh-start bootstrap, tools.ops.fresh_start apply)')
    locations = {key: data / rel for key, rel in names.items()}
    locations.update(external)
    uid, owner_source = expected_owner(args.expect_owner, data)
    ext_sources = sorted(set(external.values()))
    before = {str(p): _source_state(p) for p in list(sources) + ext_sources}
    listing_before = {str(p) for p in data.rglob('*')}
    ext_sidecars_before = _external_sidecars(ext_sources)
    findings = _live_findings(data, names, uid, locations, external)
    work.mkdir(mode=0o700)
    os.chmod(work, 0o700)
    report = {'kind': 'preflight_dryrun_v1', 'paper_only': True, 'execution_status': 'EXECUTION_UNVERIFIED',
              'live_readiness': False, 'workdir': str(work), 'kept': bool(args.keep),
              'isolation': 'MOUNT_NAMESPACE' if prefix else 'NONE',
              'external_stores': {k: str(v) for k, v in sorted(external.items())},
              'expected_owner': {'uid': uid, 'source': owner_source}}
    try:
        (work / 'tmp').mkdir(mode=0o700)
        copy_started = time.monotonic()
        copies = _copy(data, work, sources) + _copy_external(work, external)
        report['copy'] = {'files': copies, 'seconds': round(time.monotonic() - copy_started, 3)}
        local_config = work / 'config.json'
        fd = os.open(local_config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(config.read_bytes())
        root = data if prefix else work

        def child_path(key):
            """Inside a namespace the child sees every store at its real absolute path (bound to the copy)."""
            if key in external:
                return str(external[key] if prefix else _mirror(work, external[key]))
            return str(root / names[key])
        paths = {short: child_path(key) for short, key in (
            ('research', 'research_db'), ('evidence', 'evidence_db'), ('pacing', 'pacing_db'),
            ('discovery', 'discovery_db'), ('journal', 'journal'), ('ledger', 'ledger'))}
        paths['config'] = str(local_config)
        lock = work / names['research_db'].parent / 'paper-scheduler.lock'
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        info = lock.stat()
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C.UTF-8',
               'TMPDIR': str(work / 'tmp'), 'PYTHONDONTWRITEBYTECODE': '1',
               'DESK_PROVIDER_PACING_DB': paths['pacing'],
               'DESK_PAPER_SCHEDULER_IDENTITY': f'{info.st_dev}:{info.st_ino}'}
        mounts = _mount_points(ext_sources) if prefix else []
        spec = {'allowed_roots': [str(work)] + ([str(data)] + [str(m) for m in mounts] if prefix else []), 'release': str(release), 'paths': paths, 'prefix': RESULT_PREFIX,
                'skipped_context_fields': list(SKIPPED_CONTEXT_FIELDS),
                'dispatch': {'taker': args.taker, 'amount_raw': args.amount_raw, 'pool_fee_bps': args.pool_fee_bps}}
        child_started = time.monotonic()
        try:
            command = [args.python, '-I', '-B', '-c', CHILD, json.dumps(spec)]
            if prefix:
                binds = [str(work), str(data)]
                for mount in mounts:
                    binds += [str(work / EXTERNAL_DIR / str(mount).lstrip('/')), str(mount)]
                command = prefix + ['sh', '-c', 'while [ "$1" != -- ]; do mount --bind "$1" "$2" || exit 1; shift 2; done; '
                                    'shift; exec "$@"', 'sh'] + binds + ['--'] + command
            proc = subprocess.run(command, cwd=release, env=env, capture_output=True, text=True,
                                  timeout=args.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise Refused('Child exceeded --timeout; stores copied only, live data untouched') from None
        child_seconds = round(time.monotonic() - child_started, 3)
        lines = [l for l in proc.stdout.splitlines() if l.startswith(RESULT_PREFIX)]
        if len(lines) != 1:
            raise Refused('Child produced no result (exit %s): %s' % (proc.returncode, proc.stderr.strip()[-300:]))
        child = json.loads(lines[0][len(RESULT_PREFIX):])
        stages, violations = child['stages'], child['violations']
        try:
            pending, truncated = _pending(work / names['evidence_db'])
            unreadable = False
        except PendingUnreadable:
            pending, truncated, unreadable = [], False, True   # unreadable is NOT 'nothing pending'
        blockers = _blockers(stages, violations) + ['LIVE_' + f for f in findings]
        if unreadable:
            blockers.append('PENDING_UNREADABLE')
        if not (data / names['journal']).exists() and 'JOURNAL_ABSENT' not in blockers:
            blockers.append('JOURNAL_ABSENT')   # reported even when a stage failed before looking
        report.update({
            'status': 'GUARD_VIOLATION' if violations else ('PASS' if not blockers else 'BLOCKED'),
            'blockers': blockers, 'implementation_hash': child.get('implementation_hash'),
            'gate': stages.get('gate'), 'preflight': stages.get('preflight'), 'scheduler': stages.get('scheduler'),
            'import': stages.get('import'), 'guard_violations': violations,
            'pending_null_pass_ids': pending, 'pending_null_pass_ids_truncated': truncated,
            'pending_null_pass_ids_readable': not unreadable,
            'blocker_details': _details(data, names, blockers),
            'excluded_context_fields': list(SKIPPED_CONTEXT_FIELDS),
            'timing': {'child_seconds': child_seconds, 'copy_seconds': report['copy']['seconds'],
                       'total_seconds': round(time.monotonic() - started, 3)}})
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)
            report['workdir_removed'] = not work.exists()
    after = {str(p): _source_state(p) for p in list(sources) + ext_sources}
    report['live_sources_unchanged'] = before == after
    created = sorted(str(Path(x).relative_to(data)) for x in {str(p) for p in data.rglob('*')} - listing_before)
    sidecars = [x for x in created if x.endswith(SIDECAR_SUFFIXES)]
    sidecars += sorted(_external_sidecars(ext_sources) - ext_sidecars_before)
    report['live_sidecars_created'] = sidecars
    report['live_files_created'] = [x for x in created if x not in sidecars]
    report['live_permission_findings'] = findings
    extra = (['LIVE_SOURCE_CHANGED_DURING_RUN'] if before != after else []) \
        + ['LIVE_SIDECAR_CREATED:' + x for x in sidecars] + ['LIVE_FILE_CREATED:' + x for x in report['live_files_created']]
    if extra:
        # Live writers moved while copying (copies may be cross-store inconsistent) or this tool left
        # files beside a live store (a root-owned -wal/-shm can lock the service user out): fail closed.
        report['blockers'] = report['blockers'] + extra
    if (extra or findings) and report['status'] == 'PASS':
        report['status'] = 'BLOCKED'
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--data', required=True)
    p.add_argument('--ledger', required=True, help='ledger file name relative to --data, or an absolute path (inside or outside --data)')
    p.add_argument('--config', required=True)
    p.add_argument('--release-dir', required=True)
    p.add_argument('--workdir', required=True)
    p.add_argument('--keep', action='store_true')
    p.add_argument('--no-namespace', action='store_true',
                   help='skip the private mount namespace (stores binding absolute paths will fail closed)')
    p.add_argument('--research-db', default='research.sqlite')
    p.add_argument('--evidence-db', default='evidence.sqlite')
    p.add_argument('--pacing-db', default='provider-pacing.sqlite',
                   help='relative to --data, or the absolute path of the SHARED pacing store outside it (copied, never opened live by the child)')
    p.add_argument('--discovery-db', default='discovery/continuous.sqlite',
                   help='relative to --data, or the absolute path of the SHARED discovery store outside it')
    p.add_argument('--journal', default='entry-dispatch/dispatch.sqlite')
    p.add_argument('--expect-owner', help='user name or numeric uid that must own the live store files '
                   '(default: the owner of --data; never the uid this tool runs as)')
    p.add_argument('--taker')
    p.add_argument('--amount-raw', type=int)
    p.add_argument('--pool-fee-bps')
    p.add_argument('--python', default=sys.executable)
    p.add_argument('--timeout', type=float, default=900.0)
    p.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
    args = p.parse_args(argv)
    try:
        report = run(args)
    except (Refused, OSError, sqlite3.Error, ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        print(json.dumps({'kind': 'preflight_dryrun_v1', 'status': 'REFUSED', 'reason': str(e)[:300],
                          'paper_only': True, 'live_readiness': False}, sort_keys=True))
        return 3
    print(json.dumps(report, sort_keys=True))
    return {'PASS': 0, 'BLOCKED': 2}.get(report['status'], 3)


if __name__ == '__main__':
    raise SystemExit(main())
