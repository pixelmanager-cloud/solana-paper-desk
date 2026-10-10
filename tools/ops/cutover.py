"""Release staging, systemd cutover and rollback for coordinator use; dry-run by default.

Every mutation (filesystem writes outside the staging tree, systemctl state changes)
goes through ``Ops``. Without ``--apply`` nothing is mutated and the exact commands
are printed. No provider I/O, no signing, no network.

Drop-ins written here carry MARKER so that rollback removes only its own files.
"""
import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import time
from pathlib import Path

MARKER = '# desk-cutover-managed v1'
DROPIN = '60-reviewed-release.conf'
UNIT_RE = re.compile(r'desk-[a-z0-9][a-z0-9-]{0,62}\.(service|timer)\Z')
COMMIT_RE = re.compile(r'[0-9a-f]{7,40}\Z')
SHA_RE = re.compile(r'[0-9a-f]{64}\Z')
ENV_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=[^\x00-\x1f]*\Z')
LOOPBACK_NAMES = {'localhost'}
BIND_FLAGS = {'--host', '--bind', '--listen', '--address', '--addr', '-H', '-b'}
MAX_MEMBERS = 100_000
MAX_BYTES = 1 << 30
DIGEST_CODE = ("import sys;sys.path.insert(0,'.');"
               "from desk.runtime_compatibility import implementation_hash;"
               "print(implementation_hash())")


class CutoverError(Exception):
    pass


class Ops:
    """The only mutation path. ``apply=False`` records instead of executing."""

    def __init__(self, apply=False, runner=None, journal=None):
        self.apply = apply
        self.runner = runner or self._subprocess
        self.journal = journal
        self.planned = []

    @staticmethod
    def _subprocess(argv, cwd=None):
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=False)

    def read(self, argv, cwd=None):
        """Read-only command; executed even in dry-run."""
        return self.runner(list(argv), cwd)

    def mutate(self, argv):
        self.planned.append(shlex.join(argv))
        if not self.apply:
            return None
        self.record({'event': 'command', 'argv': list(argv)})
        result = self.runner(list(argv), None)
        if result.returncode != 0:
            raise CutoverError('Command failed: %s: %s' % (shlex.join(argv), (result.stderr or '').strip()[:300]))
        return result

    def write_file(self, path, text, mode=0o644):
        self.planned.append('write %s (%d bytes, mode %o)' % (path, len(text.encode()), mode))
        if not self.apply:
            return
        self.record({'event': 'write', 'path': str(path)})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.' + path.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)

    def remove_file(self, path):
        self.planned.append('remove %s' % path)
        if not self.apply:
            return
        self.record({'event': 'remove', 'path': str(path)})
        path.unlink()

    def copy_file(self, src, dst):
        self.planned.append('copy %s -> %s' % (src, dst))
        if not self.apply:
            return
        self.record({'event': 'copy', 'src': str(src), 'dst': str(dst)})
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as stream:
            shutil.copyfileobj(stream, out)

    def record(self, entry):
        if self.journal is None or not self.apply:
            return
        self.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = dict(entry, ts=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        fd = os.open(self.journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            stream.write(json.dumps(entry, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())


# ---------------------------------------------------------------- validation

def canonical_dir(path, label):
    path = Path(path)
    if not path.is_absolute():
        raise CutoverError(label + ' must be an absolute path')
    try:
        real = path.resolve(strict=True)
    except OSError as exc:
        raise CutoverError(label + ' missing: ' + str(exc)) from exc
    if real != path or path.is_symlink() or not real.is_dir():
        raise CutoverError(label + ' must be a canonical directory (no symlinks)')
    if real.stat().st_mode & 0o022:
        raise CutoverError(label + ' must not be group/world writable')
    return real


def check_units(units, label):
    for unit in units:
        if not UNIT_RE.fullmatch(unit):
            raise CutoverError('Invalid %s unit name: %r' % (label, unit))
    if len(set(units)) != len(units):
        raise CutoverError('Duplicate unit in ' + label)


def is_loopback_host(host):
    host = host.strip().strip('[]').lower()
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def split_host(value):
    value = value.strip()
    if value.startswith('['):
        return value[1:].partition(']')[0]
    if value.count(':') == 1:
        return value.partition(':')[0]
    return value


def non_loopback_bind(exec_text):
    """Return a reason string when an ExecStart text could bind off loopback."""
    for line in exec_text.splitlines():
        try:
            tokens = shlex.split(line.strip().lstrip('@-+!:'))
        except ValueError:
            return 'unparseable ExecStart'
        tokens = [t for t in tokens if t]
        for i, token in enumerate(tokens):
            flag, eq, value = token.partition('=')
            if flag in BIND_FLAGS:
                host = value if eq else (tokens[i + 1] if i + 1 < len(tokens) else '')
                if not is_loopback_host(split_host(host)):
                    return 'bind flag %s=%s' % (flag, host)
            if '0.0.0.0' in token or '[::]' in token or token in ('::', '*'):
                return 'wildcard address in %r' % token
            m = re.fullmatch(r'(\[[0-9a-fA-F:]+\]|[0-9A-Za-z._-]+):(\d{2,5})', token)
            if m and not is_loopback_host(m.group(1)):
                try:
                    ipaddress.ip_address(m.group(1).strip('[]'))
                except ValueError:
                    continue
                return 'non-loopback host:port %s' % token
    return None


def parse_store_env(path):
    """cutover-store-env v1: {"version":1,"units":{unit:{"Environment":[K=V],"ExecStart":"cmd"}}}"""
    if path is None:
        return {}
    raw = Path(path).read_bytes()
    if len(raw) > 1 << 20:
        raise CutoverError('store-env file too large')
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        raise CutoverError('store-env is not valid JSON: ' + str(exc)) from exc
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('units'), dict) \
            or set(data) - {'version', 'units'}:
        raise CutoverError('store-env must be {"version":1,"units":{...}}')
    out = {}
    for unit, spec in data['units'].items():
        check_units([unit], 'store-env')
        if not isinstance(spec, dict) or set(spec) - {'Environment', 'ExecStart'}:
            raise CutoverError('store-env unit %s has unsupported keys' % unit)
        env = spec.get('Environment', [])
        if type(env) is not list or not all(type(e) is str and ENV_RE.fullmatch(e) for e in env):
            raise CutoverError('store-env Environment must be KEY=VALUE strings without control characters')
        exec_start = spec.get('ExecStart')
        if exec_start is not None and (type(exec_start) is not str or not exec_start.strip()
                                       or re.search(r'[\x00-\x1f]', exec_start)
                                       or not exec_start.lstrip('@-+!:').startswith('/')):
            raise CutoverError('store-env ExecStart must be one absolute single-line command')
        out[unit] = {'Environment': env, 'ExecStart': exec_start}
    return out


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key ' + key)
        out[key] = value
    return out


def runtime_digest(release, ops, python):
    result = ops.read([python, '-I', '-c', DIGEST_CODE], cwd=str(release))
    out = (result.stdout or '').strip()
    if result.returncode != 0 or not SHA_RE.fullmatch(out):
        raise CutoverError('Could not compute runtime digest: ' + (result.stderr or '').strip()[:300])
    return out


# --------------------------------------------------------------------- stage

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def safe_members(tar, max_bytes=MAX_BYTES):
    members, seen, total = [], set(), 0
    for member in tar:
        if len(members) >= MAX_MEMBERS:
            raise CutoverError('Too many archive members')
        name = member.name
        if not name or '\x00' in name or '\\' in name or name.startswith('/') or re.match(r'[A-Za-z]:', name):
            raise CutoverError('Unsafe archive path: %r' % name)
        parts = Path(name).parts
        if any(p == '..' for p in parts):
            raise CutoverError('Archive path traversal: %r' % name)
        norm = os.path.normpath(name)
        if norm in seen:
            raise CutoverError('Duplicate archive member: %r' % name)
        seen.add(norm)
        if member.issym() or member.islnk():
            raise CutoverError('Archive links are not allowed: %r' % name)
        if not (member.isfile() or member.isdir()):
            raise CutoverError('Unsupported archive member type: %r' % name)
        total += max(member.size, 0)
        if total > max_bytes:
            raise CutoverError('Archive exceeds size bound')
        members.append(member)
    if not members:
        raise CutoverError('Empty archive')
    return members


def extract(tar, members, dest, strip):
    for member in members:
        parts = Path(os.path.normpath(member.name)).parts
        if strip:
            if len(parts) <= strip:
                continue
            parts = parts[strip:]
        target = dest.joinpath(*parts)
        if os.path.commonpath([dest, target]) != str(dest):
            raise CutoverError('Archive path escapes destination: %r' % member.name)
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
            continue
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        mode = 0o755 if member.mode & 0o100 else 0o644
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, 'wb') as out, tar.extractfile(member) as src:
            written = 0
            for block in iter(lambda: src.read(1 << 20), b''):
                written += len(block)
                if written > member.size:
                    raise CutoverError('Archive member larger than declared: %r' % member.name)
                out.write(block)


def stage(args, ops):
    if not SHA_RE.fullmatch(args.sha256):
        raise CutoverError('--sha256 must be 64 lowercase hex characters')
    if not COMMIT_RE.fullmatch(args.commit):
        raise CutoverError('--commit must be 7-40 lowercase hex characters')
    root = canonical_dir(args.release_root, '--release-root')
    tar_path = Path(args.tar)
    if tar_path.is_symlink() or not tar_path.is_file():
        raise CutoverError('--tar must be a regular file')
    actual = sha256_file(tar_path)
    if not hmac.compare_digest(actual, args.sha256):
        raise CutoverError('Archive sha256 mismatch: expected %s, got %s' % (args.sha256, actual))
    dest = root / args.commit
    if os.path.lexists(dest):
        raise CutoverError('Release already staged: %s' % dest)
    try:
        tar = tarfile.open(tar_path, 'r:*')
    except tarfile.TarError as exc:
        raise CutoverError('Unreadable archive: %s' % exc) from exc
    with tar:
        members = safe_members(tar, args.max_bytes)
        report = {'action': 'stage', 'release': str(dest), 'archive_sha256': actual,
                  'members': len(members), 'applied': ops.apply}
        if not ops.apply:
            ops.planned.append('extract %s -> %s (strip %d)' % (tar_path, dest, args.strip_components))
            return report
        ops.record({'event': 'stage', 'dest': str(dest), 'sha256': actual})
        tmp = root / ('.stage-%s-%d' % (args.commit, os.getpid()))
        tmp.mkdir(mode=0o755)
        try:
            extract(tar, members, tmp, args.strip_components)
            if not (tmp / 'desk').is_dir():
                raise CutoverError('Archive lacks desk/ (wrong --strip-components?)')
            report['runtime_digest'] = runtime_digest(tmp, ops, args.python)
            if args.expect_digest and report['runtime_digest'] != args.expect_digest:
                raise CutoverError('Runtime digest %s differs from --expect-digest' % report['runtime_digest'])
            os.rename(tmp, dest)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return report


# ------------------------------------------------------------------- cutover

def dropin_path(unit_dir, unit):
    return unit_dir / (unit + '.d') / DROPIN


def render_dropin(release, digest, spec, backup):
    lines = [MARKER, '# release: %s' % release, '# runtime-digest: %s' % digest]
    if backup:
        lines.append('# replaced-backup: %s' % backup)
    lines.append('[Service]')
    lines.append('WorkingDirectory=%s' % release)
    for item in (spec or {}).get('Environment', []):
        lines.append('Environment=%s' % shlex.quote(item))
    if (spec or {}).get('ExecStart'):
        lines += ['ExecStart=', 'ExecStart=' + spec['ExecStart']]
    return '\n'.join(lines) + '\n'


def existing_exec_text(unit_dir, template_dir, unit):
    texts = []
    for base in (unit_dir, template_dir):
        if base is None:
            continue
        for path in (Path(base) / unit, *sorted((Path(base) / (unit + '.d')).glob('*.conf'))):
            if path.is_file() and not path.is_symlink():
                texts += [l for l in path.read_text(errors='replace').splitlines() if l.startswith('ExecStart=')]
        if texts:
            break
    return '\n'.join(t.split('=', 1)[1] for t in texts)


def show(ops, unit, props):
    result = ops.read(['systemctl', 'show', unit] + ['-p' + p for p in props])
    if result.returncode != 0:
        raise CutoverError('systemctl show %s failed: %s' % (unit, (result.stderr or '').strip()[:200]))
    out = {}
    for line in (result.stdout or '').splitlines():
        key, _, value = line.partition('=')
        out[key] = value
    return out


def unit_ok(state):
    active = state.get('ActiveState')
    if active in ('active', 'activating'):
        return True
    return active == 'inactive' and state.get('Type') == 'oneshot' and state.get('Result') == 'success'


def active_now(ops, unit):
    return show(ops, unit, ['ActiveState']).get('ActiveState') in ('active', 'activating', 'reloading')


def cutover(args, ops):
    check_units(args.units, 'units')
    check_units(args.keep_off, 'keep-off')
    check_units(args.configure_only, 'configure-only')
    groups = [set(args.units), set(args.keep_off), set(args.configure_only)]
    if any(a & b for i, a in enumerate(groups) for b in groups[i + 1:]):
        raise CutoverError('A unit appears in more than one of --units/--keep-off/--configure-only')
    if not args.units:
        raise CutoverError('No units to cut over')
    for unit in args.units:
        if 'entry-dispatcher' in unit and not args.allow_entry:
            raise CutoverError('Refusing to start %s without --allow-entry (entries stay a separate step)' % unit)
    release_root = canonical_dir(args.release_root, '--release-root')
    release = canonical_dir(args.release, '--release')
    if release.parent != release_root or not (release / 'desk').is_dir():
        raise CutoverError('--release must be a staged directory directly under --release-root')
    unit_dir = canonical_dir(args.unit_dir, '--unit-dir')
    template_dir = Path(args.template_dir) if args.template_dir else None
    store_env = parse_store_env(args.store_env)
    stray = set(store_env) - set(args.units) - set(args.keep_off) - set(args.configure_only)
    if stray:
        raise CutoverError('store-env names units outside the cutover: %s' % sorted(stray))
    digest = runtime_digest(release, ops, args.python)
    if args.expect_digest and digest != args.expect_digest:
        raise CutoverError('Runtime digest %s differs from --expect-digest' % digest)

    everything = args.units + args.configure_only + args.keep_off
    services = [u for u in everything if u.endswith('.service')]
    for unit in everything:
        if unit.endswith('.timer'):
            service = unit[:-6] + '.service'
            if service not in everything:
                raise CutoverError('Timer %s needs its service %s in the cutover (else it would run an unpinned release)'
                                   % (unit, service))
    for unit in services:
        exec_text = (store_env.get(unit) or {}).get('ExecStart') or existing_exec_text(unit_dir, template_dir, unit)
        reason = non_loopback_bind(exec_text)
        if reason:
            raise CutoverError('Refusing %s: ExecStart may bind off loopback (%s)' % (unit, reason))
    for unit in args.keep_off:
        if active_now(ops, unit):
            raise CutoverError('Keep-off unit %s is already active; stop it deliberately first' % unit)

    plans = []
    for unit in services:
        path = dropin_path(unit_dir, unit)
        backup = None
        if path.is_symlink():
            raise CutoverError('Drop-in path is a symlink: %s' % path)
        if path.exists():
            current = path.read_text(errors='replace')
            if MARKER in current:
                m = re.search(r'^# replaced-backup: (.+)$', current, re.M)
                backup = m.group(1) if m else None
            elif not args.replace_existing:
                raise CutoverError('Unmanaged drop-in exists: %s (pass --replace-existing to back it up)' % path)
            else:
                backup = str(path.with_name(DROPIN + '.pre-cutover-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())))
        foreign = path.exists() and MARKER not in path.read_text(errors='replace')
        plans.append((unit, path, backup if foreign or backup else None, foreign,
                      render_dropin(release, digest, store_env.get(unit), backup)))

    report = {'action': 'cutover', 'release': str(release), 'runtime_digest': digest,
              'applied': ops.apply, 'started': [], 'dropins': [str(p[1]) for p in plans]}
    ops.record({'event': 'cutover-begin', 'release': str(release), 'digest': digest,
                'units': args.units, 'dropins': report['dropins']})
    started = []
    try:
        for unit, path, backup, foreign, text in plans:
            if foreign:
                ops.copy_file(path, Path(backup))
            ops.write_file(path, text)
        ops.mutate(['systemctl', 'daemon-reload'])
        if ops.apply:
            for unit in services:
                state = show(ops, unit, ['WorkingDirectory', 'ExecStart'])
                if state.get('WorkingDirectory') != str(release):
                    raise CutoverError('%s effective WorkingDirectory %r != %s' % (unit, state.get('WorkingDirectory'), release))
                reason = non_loopback_bind(re.sub(r'^\{ ?path=[^;]*; argv\[\]=', '', state.get('ExecStart', '')).replace(' ; ', '\n'))
                if reason:
                    raise CutoverError('%s effective ExecStart may bind off loopback (%s)' % (unit, reason))
        for unit in args.units:
            ops.mutate(['systemctl', 'start', unit])
            if ops.apply:
                started.append(unit)
        if ops.apply:
            for unit in args.units:
                state = show(ops, unit, ['ActiveState', 'SubState', 'Result', 'Type', 'WorkingDirectory'])
                if unit.endswith('.service') and state.get('WorkingDirectory') != str(release):
                    raise CutoverError('%s effective WorkingDirectory %r != %s' % (unit, state.get('WorkingDirectory'), release))
                if not unit_ok(state):
                    raise CutoverError('%s not healthy after start: %s' % (unit, state))
                report['started'].append({'unit': unit, 'ActiveState': state.get('ActiveState'),
                                          'Result': state.get('Result')})
            for unit in args.keep_off:
                if active_now(ops, unit):
                    raise CutoverError('Keep-off unit %s became active' % unit)
    except BaseException as exc:
        stopped = []
        for unit in reversed(started):
            try:
                ops.mutate(['systemctl', 'stop', unit])
                stopped.append(unit)
            except CutoverError:
                pass
        ops.record({'event': 'cutover-failed', 'error': str(exc), 'stopped': stopped,
                    'dropins_left_for_rollback': report['dropins']})
        if isinstance(exc, CutoverError):
            exc.args = (str(exc) + ' | stopped=%s; drop-ins left in place, run rollback to remove' % stopped,)
        raise
    ops.record({'event': 'cutover-done', 'started': args.units})
    return report


# ------------------------------------------------------------------ rollback

def rollback(args, ops):
    check_units(args.units, 'units')
    unit_dir = canonical_dir(args.unit_dir, '--unit-dir')
    removed, skipped = [], []
    for unit in args.units:
        path = dropin_path(unit_dir, unit)
        if path.is_symlink() or not path.is_file():
            skipped.append({'unit': unit, 'reason': 'no drop-in'})
            continue
        current = path.read_text(errors='replace')
        if MARKER not in current:
            skipped.append({'unit': unit, 'reason': 'not written by cutover'})
            continue
        ops.remove_file(path)
        m = re.search(r'^# replaced-backup: (.+)$', current, re.M)
        if m and Path(m.group(1)).is_file() and Path(m.group(1)).parent == path.parent:
            ops.copy_file(Path(m.group(1)), path)
        removed.append(str(path))
    ops.mutate(['systemctl', 'daemon-reload'])
    return {'action': 'rollback', 'removed': removed, 'skipped': skipped, 'applied': ops.apply,
            'note': 'Units are not stopped or restarted; restart deliberately after verifying the old release.'}


# ----------------------------------------------------------------------- cli

def build_parser():
    p = argparse.ArgumentParser(prog='tools.ops.cutover', description=__doc__)
    p.add_argument('--apply', action='store_true', help='mutate; default is a dry-run that prints commands')
    p.add_argument('--journal', default='/var/lib/solana-desk-cutover/cutover-journal.jsonl')
    p.add_argument('--python', default=sys.executable)
    sub = p.add_subparsers(dest='command', required=True)
    s = sub.add_parser('stage')
    s.add_argument('--tar', required=True)
    s.add_argument('--sha256', required=True)
    s.add_argument('--commit', required=True)
    s.add_argument('--release-root', default='/opt/solana-desk-releases')
    s.add_argument('--strip-components', type=int, choices=(0, 1), default=0)
    s.add_argument('--max-bytes', type=int, default=MAX_BYTES)
    s.add_argument('--expect-digest')
    c = sub.add_parser('cutover')
    c.add_argument('--release', required=True)
    c.add_argument('--release-root', default='/opt/solana-desk-releases')
    c.add_argument('--units', nargs='+', default=[])
    c.add_argument('--configure-only', nargs='*', default=[], help='get drop-ins but are never started')
    c.add_argument('--keep-off', nargs='*', default=[], help='never started; must not be active')
    c.add_argument('--store-env')
    c.add_argument('--unit-dir', default='/etc/systemd/system')
    c.add_argument('--template-dir', default=None, help='fallback dir for base unit files when reading ExecStart')
    c.add_argument('--expect-digest')
    c.add_argument('--allow-entry', action='store_true')
    c.add_argument('--replace-existing', action='store_true')
    r = sub.add_parser('rollback')
    r.add_argument('--units', nargs='+', required=True)
    r.add_argument('--unit-dir', default='/etc/systemd/system')
    return p


def main(argv=None, runner=None):
    args = build_parser().parse_args(argv)
    ops = Ops(apply=args.apply, runner=runner, journal=Path(args.journal))
    try:
        report = {'stage': stage, 'cutover': cutover, 'rollback': rollback}[args.command](args, ops)
    except CutoverError as exc:
        print(json.dumps({'status': 'REFUSED' if not ops.apply else 'FAILED', 'error': str(exc),
                          'commands': ops.planned}, indent=2))
        return 2
    report['status'] = 'APPLIED' if ops.apply else 'DRY_RUN'
    report['commands'] = ops.planned
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
