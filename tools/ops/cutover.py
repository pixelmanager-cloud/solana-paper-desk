"""Release staging, systemd cutover and rollback for coordinator use; dry-run by default.

Every mutation (filesystem writes outside the staging tree, systemctl state changes)
goes through ``Ops``. Without ``--apply`` nothing is mutated and the exact commands
are printed. No provider I/O, no signing, no network.

Drop-ins written here carry MARKER as their FIRST line so that rollback removes only its own files.
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
DEFAULT_ARCHIVED_ROOT = '/var/lib/solana-desk'
DEFAULT_INTERPRETER = '/opt/solana-desk/.venv/bin/python'
SHARED_RELATIVE = ('provider-pacing.sqlite', 'discovery/continuous.sqlite')
SHARED_SUFFIXES = ('', '-wal', '-shm', '-journal')
PENDING_STATES = {'activating', 'reloading', 'deactivating'}
IDLE_STATES = {'inactive', 'failed'}
ENABLED_STATES = {'enabled', 'enabled-runtime', 'linked', 'linked-runtime', 'alias', 'indirect'}
BAD_SUBSTATES = {'auto-restart', 'start', 'start-pre', 'start-post', 'reload', 'stop', 'stop-sigterm',
                 'stop-sigkill', 'stop-post', 'final-sigterm', 'final-sigkill', 'failed', 'dead'}
EXEC_PROPS = ('ExecStart', 'ExecStartPre', 'ExecStartPost', 'ExecStop', 'ExecReload', 'ExecCondition')
CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
SD_SAFE_RE = re.compile(r'[A-Za-z0-9_@+=:,./%-]+\Z')
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

    def __init__(self, apply=False, runner=None, journal=None, sleep=None, monotonic=None):
        self.apply = apply
        self.runner = runner or self._subprocess
        self.journal = journal
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        self.planned = []

    @staticmethod
    def _subprocess(argv, cwd=None):
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=False)

    def _run(self, argv, cwd=None):
        """Every runner failure (timeout, missing binary, ...) becomes a CutoverError."""
        try:
            return self.runner(list(argv), cwd)
        except CutoverError:
            raise
        except subprocess.TimeoutExpired as exc:
            raise CutoverError('Command timed out after %ss: %s' % (exc.timeout, shlex.join(argv))) from exc
        except Exception as exc:  # noqa: BLE001 - the contract is "never leak a raw runner exception"
            raise CutoverError('Command could not run: %s: %s' % (shlex.join(argv), exc)) from exc

    def read(self, argv, cwd=None):
        """Read-only command; executed even in dry-run."""
        return self._run(argv, cwd)

    def pause(self, seconds):
        self.planned.append('wait %ss for units to settle' % seconds)
        if self.apply and seconds > 0:
            self.sleep(seconds)

    def mutate(self, argv):
        self.planned.append(shlex.join(argv))
        if not self.apply:
            return None
        self.record({'event': 'command', 'argv': list(argv)})
        result = self._run(argv, None)
        if result.returncode != 0:
            raise CutoverError('Command failed: %s: %s' % (shlex.join(argv), (result.stderr or '').strip()[:300]))
        return result

    @staticmethod
    def fsync_dir(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_file(self, path, text, mode=0o644):
        self.planned.append('write %s (%d bytes, mode %o)' % (path, len(text.encode()), mode))
        if not self.apply:
            return
        self.record({'event': 'write', 'path': str(path)})
        created = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name('.' + path.name + '.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.replace(tmp, path)
        self.fsync_dir(path.parent)
        if created:
            self.fsync_dir(path.parent.parent)

    def remove_file(self, path):
        self.planned.append('remove %s' % path)
        if not self.apply:
            return
        self.record({'event': 'remove', 'path': str(path)})
        path.unlink()
        self.fsync_dir(path.parent)

    def copy_file(self, src, dst):
        self.planned.append('copy %s -> %s' % (src, dst))
        if not self.apply:
            return
        self.record({'event': 'copy', 'src': str(src), 'dst': str(dst)})
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as stream:
            shutil.copyfileobj(stream, out)
            out.flush()
            os.fsync(out.fileno())
        self.fsync_dir(Path(dst).parent)

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


def is_managed(text):
    """A drop-in is ours only if the marker is its first line (a marker quoted later proves nothing)."""
    lines = text.splitlines()
    return bool(lines) and lines[0] == MARKER


def sd_percent(text, keep_credentials=False):
    """Escape systemd specifiers. Only a whole-argument ``%d/`` prefix (credentials directory, used by the
    dashboard's ``--secrets-file %d/provider-keys.json``) may stay a specifier; everything else is literal."""
    if keep_credentials and text.startswith('%d/'):
        return '%d' + text[2:].replace('%', '%%')
    return text.replace('%', '%%')


def sd_word(text, exec_mode=False):
    """Quote one word for an ExecStart= (``exec_mode``) or Environment= line."""
    if CONTROL_RE.search(text):
        raise CutoverError('Control character in unit setting: %r' % text[:60])
    if exec_mode:
        text = text.replace('$', '$$')
    if text and text != ';' and SD_SAFE_RE.fullmatch(text):
        return text
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_exec(interpreter, argv):
    return ' '.join([sd_word(interpreter, True)] + [sd_word(sd_percent(a, True), True) for a in argv])


def check_interpreter(path):
    if type(path) is not str or not path.startswith('/') or CONTROL_RE.search(path) or ' ' in path \
            or '..' in Path(path).parts:
        raise CutoverError('--interpreter must be an absolute path without spaces or control characters')
    return path


def _t13_spec(unit, spec, interpreter):
    if set(spec) != {'environment', 'argv'}:
        raise CutoverError('unit_arguments %s must have exactly "environment" and "argv"' % unit)
    env, argv = spec['environment'], spec['argv']
    if type(env) is not list or not all(type(e) is str and ENV_RE.fullmatch(e) for e in env):
        raise CutoverError('unit_arguments %s environment must be KEY=VALUE strings without control characters' % unit)
    if type(argv) is not list or not argv or not all(type(a) is str and not CONTROL_RE.search(a) for a in argv):
        raise CutoverError('unit_arguments %s argv must be a non-empty list of strings without control characters' % unit)
    return {'Environment': [sd_percent(e) for e in env], 'ExecStart': render_exec(interpreter, argv)}


def parse_store_env(path, interpreter=DEFAULT_INTERPRETER):
    """Two shapes are accepted.

    cutover-store-env v1: {"version":1,"units":{unit.service:{"Environment":[K=V],"ExecStart":"cmd"}}}
    T13 ``unit_arguments`` (the ``units`` of ``fresh-start-manifest.json``, or the bare mapping):
    {"desk-x":{"environment":[K=V],"argv":[...]}} -- keys without ``.service``, argv without the interpreter.
    """
    if path is None:
        return {}
    interpreter = check_interpreter(interpreter)
    raw = Path(path).read_bytes()
    if len(raw) > 1 << 20:
        raise CutoverError('store-env file too large')
    try:
        data = json.loads(raw, object_pairs_hook=_no_duplicates)
    except ValueError as exc:
        raise CutoverError('store-env is not valid JSON: ' + str(exc)) from exc
    if not isinstance(data, dict):
        raise CutoverError('store-env must be a JSON object')
    kind = data.get('kind')
    if kind == 'fresh_start_plan_v1':
        raise CutoverError('store-env is a fresh_start plan (placeholder scheduler identity); use the manifest written by apply')
    if kind == 'fresh_start_manifest_v1':
        units = data.get('units')
        legacy = False
    elif 'version' in data or set(data) == {'units'}:
        if data.get('version') != 1 or not isinstance(data.get('units'), dict) or set(data) - {'version', 'units'}:
            raise CutoverError('store-env must be {"version":1,"units":{...}}')
        units = data['units']
        legacy = True
    else:
        units, legacy = data, False
    if not isinstance(units, dict):
        raise CutoverError('store-env units must be an object')
    out = {}
    for name, spec in units.items():
        unit = name if name.endswith('.service') else name + '.service'
        if not unit.endswith('.service') or not UNIT_RE.fullmatch(unit) or name.endswith('.timer'):
            raise CutoverError('store-env unit name not allowed: %r' % name)
        if unit in out:
            raise CutoverError('store-env names %s twice' % unit)
        if not isinstance(spec, dict):
            raise CutoverError('store-env unit %s must be an object' % name)
        if 'argv' in spec or 'environment' in spec:
            out[unit] = _t13_spec(unit, spec, interpreter)
            continue
        if not legacy and spec:
            raise CutoverError('store-env unit %s has unsupported keys' % name)
        if set(spec) - {'Environment', 'ExecStart'}:
            raise CutoverError('store-env unit %s has unsupported keys' % unit)
        env = spec.get('Environment', [])
        if type(env) is not list or not all(type(e) is str and ENV_RE.fullmatch(e) for e in env):
            raise CutoverError('store-env Environment must be KEY=VALUE strings without control characters')
        exec_start = spec.get('ExecStart')
        if exec_start is not None and (type(exec_start) is not str or not exec_start.strip()
                                       or CONTROL_RE.search(exec_start)
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

def sha256_stream(stream):
    h = hashlib.sha256()
    for block in iter(lambda: stream.read(1 << 20), b''):
        h.update(block)
    return h.hexdigest()


def sha256_file(path):
    with open(path, 'rb') as stream:
        return sha256_stream(stream)


def open_regular_file(path):
    """Open without following symlinks; the same handle is hashed and then extracted."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise CutoverError('--tar must be a regular file: %s' % exc) from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise CutoverError('--tar must be a regular file')
    return os.fdopen(fd, 'rb')


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
    with open_regular_file(args.tar) as stream:
        actual = sha256_stream(stream)
        if not hmac.compare_digest(actual, args.sha256):
            raise CutoverError('Archive sha256 mismatch: expected %s, got %s' % (args.sha256, actual))
        dest = root / args.commit
        if os.path.lexists(dest):
            raise CutoverError('Release already staged: %s' % dest)
        stream.seek(0)
        try:
            tar = tarfile.open(fileobj=stream, mode='r:*')
        except tarfile.TarError as exc:
            raise CutoverError('Unreadable archive: %s' % exc) from exc
        with tar:
            members = safe_members(tar, args.max_bytes)
            report = {'action': 'stage', 'release': str(dest), 'archive_sha256': actual,
                      'members': len(members), 'applied': ops.apply}
            if not ops.apply:
                ops.planned.append('extract %s -> %s (strip %d)' % (args.tar, dest, args.strip_components))
                return report
            ops.record({'event': 'stage', 'dest': str(dest), 'sha256': actual})
            tmp = root / ('.stage-%s-%d' % (args.commit, os.getpid()))
            try:
                tmp.mkdir(mode=0o755)
            except OSError as exc:
                raise CutoverError('Cannot create staging directory %s: %s' % (tmp, exc)) from exc
            made = False
            try:
                extract(tar, members, tmp, args.strip_components)
                if not (tmp / 'desk').is_dir():
                    raise CutoverError('Archive lacks desk/ (wrong --strip-components?)')
                report['runtime_digest'] = runtime_digest(tmp, ops, args.python)
                if args.expect_digest and report['runtime_digest'] != args.expect_digest:
                    raise CutoverError('Runtime digest %s differs from --expect-digest' % report['runtime_digest'])
                os.mkdir(dest, 0o755)            # exclusive: fails if the name appeared meanwhile
                made = True
                os.rename(tmp, dest)             # replaces only our own empty directory
            except CutoverError:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            except OSError as exc:
                shutil.rmtree(tmp, ignore_errors=True)
                if made:
                    try:
                        os.rmdir(dest)
                    except OSError:
                        pass
                raise CutoverError('Cannot stage %s: %s' % (args.commit, exc)) from exc
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
        lines.append('Environment=' + sd_word(item))
    if (spec or {}).get('ExecStart'):
        lines += ['ExecStart=', 'ExecStart=' + spec['ExecStart']]
    return '\n'.join(lines) + '\n'


def existing_unit_lines(unit_dir, template_dir, unit):
    """(Exec* values, Environment values) from the unit file and every drop-in of the first directory that
    defines an ExecStart. Our own drop-in is skipped: it is about to be replaced."""
    for base in (unit_dir, template_dir):
        if base is None:
            continue
        execs, envs = [], []
        for path in (Path(base) / unit, *sorted((Path(base) / (unit + '.d')).glob('*.conf'))):
            if path.name == DROPIN or not path.is_file() or path.is_symlink():
                continue
            for line in path.read_text(errors='replace').splitlines():
                key, _, value = line.strip().partition('=')
                if not value:
                    continue
                if key in EXEC_PROPS:
                    execs.append(value)
                elif key in ('Environment', 'EnvironmentFile'):
                    envs.append(value)
        if execs:
            return execs, envs
    return [], []


def show_multi(ops, unit, props):
    result = ops.read(['systemctl', 'show', unit] + ['-p' + p for p in props])
    if result.returncode != 0:
        raise CutoverError('systemctl show %s failed: %s' % (unit, (result.stderr or '').strip()[:200]))
    out = {}
    for line in (result.stdout or '').splitlines():
        key, _, value = line.partition('=')
        out.setdefault(key, []).append(value)
    return out


def show(ops, unit, props):
    return {key: values[-1] for key, values in show_multi(ops, unit, props).items()}


EXEC_STRUCT_RE = re.compile(r'argv\[\]=(.*?)(?: ; ignore_errors=| ; \})')


def exec_texts(values):
    """Command lines from ``systemctl show`` Exec* values: the argv[] part of each struct, else the raw text."""
    texts = []
    for value in values:
        found = EXEC_STRUCT_RE.findall(value)
        texts += found if found else [value]
    return texts


def is_enabled(ops, unit):
    result = ops.read(['systemctl', 'is-enabled', unit])
    lines = (result.stdout or '').strip().splitlines()
    return lines[0].strip().lower() if lines else 'not-found'


def unit_ok(state):
    active = state.get('ActiveState')
    if active == 'active':
        return state.get('SubState') not in BAD_SUBSTATES
    return active == 'inactive' and state.get('Type') == 'oneshot' and state.get('Result') == 'success'


def active_now(ops, unit):
    return show(ops, unit, ['ActiveState']).get('ActiveState') not in IDLE_STATES


def as_usec(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


START_PROPS = ['ActiveState', 'SubState', 'Result', 'Type', 'WorkingDirectory', 'MainPID', 'NRestarts',
               'ExecMainStartTimestampMonotonic', 'NextElapseUSecMonotonic', 'NextElapseUSecRealtime',
               'LastTriggerUSec']


def verify_started(unit, state, before, t0_usec, release):
    """Prove the NEW process/trigger exists, not merely that the unit name reports active."""
    if unit.endswith('.service') and state.get('WorkingDirectory') != str(release):
        raise CutoverError('%s effective WorkingDirectory %r != %s' % (unit, state.get('WorkingDirectory'), release))
    if not unit_ok(state):
        raise CutoverError('%s not healthy after start: %s' % (unit, state))
    if unit.endswith('.timer'):
        mono = as_usec(state.get('NextElapseUSecMonotonic'))
        real = (state.get('NextElapseUSecRealtime') or '').strip()
        last = (state.get('LastTriggerUSec') or '').strip()
        if not ((mono is not None and mono > 0) or real not in ('', '0', 'n/a', 'infinity')
                or last not in ('', '0', 'n/a')):
            raise CutoverError('%s is active but has no scheduled trigger' % unit)
        return
    started = as_usec(state.get('ExecMainStartTimestampMonotonic'))
    if started is None or started < t0_usec:
        raise CutoverError('%s ExecMainStartTimestamp is not newer than the cutover start (an old process may still '
                           'be running): %s' % (unit, state.get('ExecMainStartTimestampMonotonic')))
    if state.get('Type') == 'oneshot':
        return
    pid = state.get('MainPID')
    if pid in (None, '', '0') or pid == before.get('MainPID'):
        raise CutoverError('%s MainPID unchanged by start (%s): the old process is still running' % (unit, pid))


def shared_paths(root, extra):
    root = root.rstrip('/')
    base = [root + '/' + rel for rel in SHARED_RELATIVE] + list(extra)
    return {path + suffix for path in base for suffix in SHARED_SUFFIXES}


def archived_refs(text, root, shared):
    """Paths under the archived store root that are not an explicitly shared input."""
    root = root.rstrip('/')
    pattern = re.compile(r'(?<![A-Za-z0-9_.\-/])' + re.escape(root) + r'(?![A-Za-z0-9_.\-])([^\s"\';,]*)')
    found = []
    for m in pattern.finditer(text):
        token = root + m.group(1)
        if token not in shared:
            found.append(token)
    return found


def check_archived(unit, texts, args, shared):
    if unit in args.allow_archived:
        return
    for text in texts:
        refs = archived_refs(text, args.archived_root, shared)
        if refs:
            raise CutoverError('Refusing %s: it references the archived store root %s (%s); the old stores must '
                               'never be written by the new experiment (use --allow-archived only for a deliberate '
                               'exception)' % (unit, args.archived_root, ', '.join(sorted(set(refs))[:3])))


def partner_unit(unit):
    return unit[:-len('.service')] + '.timer' if unit.endswith('.service') else unit[:-len('.timer')] + '.service'


def cutover(args, ops):
    check_units(args.units, 'units')
    check_units(args.keep_off, 'keep-off')
    check_units(args.configure_only, 'configure-only')
    check_units(args.allow_archived, 'allow-archived')
    groups = [set(args.units), set(args.keep_off), set(args.configure_only)]
    if any(a & b for i, a in enumerate(groups) for b in groups[i + 1:]):
        raise CutoverError('A unit appears in more than one of --units/--keep-off/--configure-only')
    if not args.units:
        raise CutoverError('No units to cut over')
    if not (0 <= args.settle_seconds <= 600):
        raise CutoverError('--settle-seconds must be between 0 and 600')
    if not os.path.isabs(args.archived_root):
        raise CutoverError('--archived-root must be an absolute path')
    for extra in args.shared_path:
        if not os.path.isabs(extra):
            raise CutoverError('--shared-path must be absolute')
    for unit in args.units:
        if 'entry-dispatcher' in unit and not args.allow_entry:
            raise CutoverError('Refusing to start %s without --allow-entry (entries stay a separate step)' % unit)
    release_root = canonical_dir(args.release_root, '--release-root')
    release = canonical_dir(args.release, '--release')
    if release.parent != release_root or not (release / 'desk').is_dir():
        raise CutoverError('--release must be a staged directory directly under --release-root')
    unit_dir = canonical_dir(args.unit_dir, '--unit-dir')
    template_dir = Path(args.template_dir) if args.template_dir else None
    everything = args.units + args.configure_only + args.keep_off
    all_env = parse_store_env(args.store_env, args.interpreter)
    store_env = {u: v for u, v in all_env.items() if u in everything}
    ignored = sorted(set(all_env) - set(everything))
    digest = runtime_digest(release, ops, args.python)
    if args.expect_digest and digest != args.expect_digest:
        raise CutoverError('Runtime digest %s differs from --expect-digest' % digest)

    services = [u for u in everything if u.endswith('.service')]
    for unit in everything:
        if unit.endswith('.timer'):
            service = unit[:-6] + '.service'
            if service not in everything:
                raise CutoverError('Timer %s needs its service %s in the cutover (else it would run an unpinned release)'
                                   % (unit, service))
    shared = shared_paths(args.archived_root, args.shared_path)
    for unit in services:
        spec = store_env.get(unit) or {}
        base_exec, base_env = existing_unit_lines(unit_dir, template_dir, unit)
        exec_text = spec.get('ExecStart') or '\n'.join(base_exec)
        reason = non_loopback_bind(exec_text)
        if reason:
            raise CutoverError('Refusing %s: ExecStart may bind off loopback (%s)' % (unit, reason))
        check_archived(unit, [exec_text] + list(spec.get('Environment', [])) + base_env, args, shared)
    for unit in args.units:
        state = show(ops, unit, ['ActiveState']).get('ActiveState')
        if state not in IDLE_STATES:
            raise CutoverError('Unit %s is not inactive (ActiveState=%s): starting it would be a silent no-op and '
                               'the old code would keep running; stop it deliberately first' % (unit, state))
    keep_off = []
    for unit in args.keep_off:
        if active_now(ops, unit):
            raise CutoverError('Keep-off unit %s is already active; stop it deliberately first' % unit)
        before = is_enabled(ops, unit)
        keep_off.append({'unit': unit, 'enabled_before': before, 'enabled_after': before})

    plans = []
    for unit in services:
        path = dropin_path(unit_dir, unit)
        backup = None
        if path.is_symlink():
            raise CutoverError('Drop-in path is a symlink: %s' % path)
        foreign = False
        if path.exists():
            current = path.read_text(errors='replace')
            if is_managed(current):
                m = re.search(r'^# replaced-backup: (.+)$', current, re.M)
                backup = m.group(1) if m else None
            elif not args.replace_existing:
                raise CutoverError('Unmanaged drop-in exists: %s (pass --replace-existing to back it up)' % path)
            else:
                foreign = True
                backup = str(path.with_name(DROPIN + '.pre-cutover-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())))
        plans.append((unit, path, backup, foreign, render_dropin(release, digest, store_env.get(unit), backup)))

    report = {'action': 'cutover', 'release': str(release), 'runtime_digest': digest,
              'applied': ops.apply, 'started': [], 'dropins': [str(p[1]) for p in plans],
              'keep_off': keep_off, 'store_env_ignored': ignored}
    ops.record({'event': 'cutover-begin', 'release': str(release), 'digest': digest,
                'units': args.units, 'dropins': report['dropins']})
    started = []
    try:
        for entry in keep_off:
            if entry['enabled_before'] in ENABLED_STATES:
                ops.mutate(['systemctl', 'disable', entry['unit']])
                if ops.apply:
                    entry['enabled_after'] = is_enabled(ops, entry['unit'])
                    if entry['enabled_after'] in ENABLED_STATES:
                        raise CutoverError('Keep-off unit %s is still enabled (%s) after disable; a reboot could '
                                           'start it' % (entry['unit'], entry['enabled_after']))
                else:
                    entry['enabled_after'] = 'disabled (planned)'
        t0 = int(ops.monotonic() * 1e6)
        for unit, path, backup, foreign, text in plans:
            if foreign:
                ops.copy_file(path, Path(backup))
            ops.write_file(path, text)
        ops.mutate(['systemctl', 'daemon-reload'])
        if ops.apply:
            for unit in services:
                eff = show_multi(ops, unit, ['WorkingDirectory', 'Environment'] + list(EXEC_PROPS))
                if eff.get('WorkingDirectory', [''])[-1] != str(release):
                    raise CutoverError('%s effective WorkingDirectory %r != %s'
                                       % (unit, eff.get('WorkingDirectory', [''])[-1], release))
                texts = exec_texts([v for prop in EXEC_PROPS for v in eff.get(prop, []) if v])
                for text in texts:
                    reason = non_loopback_bind(text)
                    if reason:
                        raise CutoverError('%s effective ExecStart may bind off loopback (%s)' % (unit, reason))
                check_archived(unit, texts + eff.get('Environment', []), args, shared)
        before_start = {}
        for unit in args.units:
            if ops.apply:
                before_start[unit] = show(ops, unit, ['MainPID'])
                started.append(unit)             # recorded BEFORE start: a failed or hung start must be stopped too
            ops.mutate(['systemctl', 'start', unit])
        if ops.apply:
            first = {}
            for unit in args.units:
                state = show(ops, unit, START_PROPS)
                verify_started(unit, state, before_start[unit], t0, release)
                first[unit] = state
                report['started'].append({'unit': unit, 'ActiveState': state.get('ActiveState'),
                                          'Result': state.get('Result')})
            ops.pause(args.settle_seconds)
            for unit in args.units:
                state = show(ops, unit, START_PROPS)
                if not unit_ok(state):
                    raise CutoverError('%s not healthy after settle: %s' % (unit, state))
                if state.get('NRestarts') != first[unit].get('NRestarts'):
                    raise CutoverError('%s restarted during the settle window (NRestarts %s -> %s)'
                                       % (unit, first[unit].get('NRestarts'), state.get('NRestarts')))
                if first[unit].get('Type') != 'oneshot' and not unit.endswith('.timer') \
                        and state.get('MainPID') != first[unit].get('MainPID'):
                    raise CutoverError('%s changed MainPID during the settle window' % unit)
            for entry in keep_off:
                if active_now(ops, entry['unit']):
                    raise CutoverError('Keep-off unit %s became active' % entry['unit'])
                entry['enabled_after'] = is_enabled(ops, entry['unit'])
                if entry['enabled_after'] in ENABLED_STATES:
                    raise CutoverError('Keep-off unit %s became enabled' % entry['unit'])
        else:
            ops.pause(args.settle_seconds)
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
    removed, skipped, managed = [], [], []
    candidates = []
    for unit in args.units:
        path = dropin_path(unit_dir, unit)
        if path.is_symlink() or not path.is_file():
            skipped.append({'unit': unit, 'reason': 'no drop-in'})
            continue
        current = path.read_text(errors='replace')
        if not is_managed(current):
            skipped.append({'unit': unit, 'reason': 'not written by cutover'})
            continue
        managed.append(unit)
        candidates.append((unit, path, current))
    # Removing a drop-in under a running unit (or a timer that would fire the old release at the archived
    # stores) is never silent: related units must be idle, or --stop must stop them first.
    related = sorted({u for unit in managed for u in (unit, partner_unit(unit))}, key=lambda u: (u.endswith('.service'), u))
    live = [u for u in related if active_now(ops, u)]
    stopped = []
    if live and not args.stop:
        raise CutoverError('Refusing rollback: %s still active (a timer or service would keep running the new release '
                           'or fire the old one against archived stores); stop them or pass --stop' % ', '.join(live))
    for unit in live:
        ops.mutate(['systemctl', 'stop', unit])
        stopped.append(unit)
    if ops.apply:
        for unit in stopped:
            if active_now(ops, unit):
                raise CutoverError('%s did not stop; drop-ins were not removed' % unit)
    for unit, path, current in candidates:
        ops.remove_file(path)
        m = re.search(r'^# replaced-backup: (.+)$', current, re.M)
        if m and Path(m.group(1)).is_file() and Path(m.group(1)).parent == path.parent:
            ops.copy_file(Path(m.group(1)), path)
        removed.append(str(path))
    ops.mutate(['systemctl', 'daemon-reload'])
    return {'action': 'rollback', 'removed': removed, 'skipped': skipped, 'stopped': stopped, 'applied': ops.apply,
            'note': 'Units are not restarted; restart deliberately after verifying the old release.'}


# ----------------------------------------------------------------------- cli

def build_parser():
    p = argparse.ArgumentParser(prog='tools.ops.cutover', description=__doc__)
    p.add_argument('--apply', action='store_true', help='mutate; default is a dry-run that prints commands')
    p.add_argument('--journal', default='/var/lib/solana-desk-cutover/cutover-journal.jsonl')
    p.add_argument('--python', default=sys.executable, help='interpreter used to compute the runtime digest')
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
    c.add_argument('--keep-off', nargs='*', default=[], help='never started; must be inactive; disabled if enabled')
    c.add_argument('--store-env', help='cutover-store-env v1 JSON, or the units of fresh-start-manifest.json')
    c.add_argument('--interpreter', default=DEFAULT_INTERPRETER,
                   help='absolute python path prepended to T13 "argv" when rendering ExecStart')
    c.add_argument('--archived-root', default=DEFAULT_ARCHIVED_ROOT,
                   help='old store root; no unit may reference it except the shared inputs')
    c.add_argument('--allow-archived', nargs='*', default=[], help='units explicitly allowed to reference the archived root')
    c.add_argument('--shared-path', action='append', default=[],
                   help='extra absolute path under the archived root that is a shared input (repeatable); '
                        'provider-pacing.sqlite and discovery/continuous.sqlite are always allowed')
    c.add_argument('--settle-seconds', type=float, default=10.0, help='wait before the second health check')
    c.add_argument('--unit-dir', default='/etc/systemd/system')
    c.add_argument('--template-dir', default=None, help='fallback dir for base unit files when reading ExecStart')
    c.add_argument('--expect-digest')
    c.add_argument('--allow-entry', action='store_true')
    c.add_argument('--replace-existing', action='store_true')
    r = sub.add_parser('rollback')
    r.add_argument('--units', nargs='+', required=True)
    r.add_argument('--unit-dir', default='/etc/systemd/system')
    r.add_argument('--stop', action='store_true', help='stop active managed units (and their timers) first')
    return p


def main(argv=None, runner=None, sleep=None):
    args = build_parser().parse_args(argv)
    ops = Ops(apply=args.apply, runner=runner, journal=Path(args.journal), sleep=sleep)
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
