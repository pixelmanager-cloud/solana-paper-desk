"""Release staging, systemd cutover and rollback for coordinator use; dry-run by default.

Every mutation (filesystem writes outside the staging tree, systemctl state changes)
goes through ``Ops``. Without ``--apply`` nothing is mutated and the exact commands
are printed. No provider I/O, no signing, no network.

Drop-ins written here carry MARKER as their FIRST line so that rollback removes only its own files.
"""
import argparse
import fnmatch
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
ROOT_UID = 0                      # tests patch this: the suite itself may run as uid 0
SEAL_PATTERNS = ('*.sqlite', '*.sqlite-wal', '*.sqlite-shm', '*.sqlite-journal',
                 '*.json', '*.jsonl')      # the ONLY files seal may change (sqlite family + JSON evidence); `*.lock` is never touched
SEAL_FILE_MODE, SEAL_DIR_MODE, SEAL_SHARED_DIR_MODE = 0o440, 0o550, 0o1770
SEAL_KIND = 'seal_manifest_v1'
MAX_MANIFEST_BYTES = 64 << 20
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
# Drop-ins the fresh-start flow writes. Rollback removes ONLY files whose first line is the matching marker.
FRESH_MARKER = '# desk-fresh-start-managed v1'      # tools.ops.fresh_start render-dropins (70-fresh-store.conf)
LATCH_MARKER = '# desk-entry-latch-managed v1'       # deploy/fresh/dropins/70-entry-latch.conf
MANAGED_DROPINS = {DROPIN: MARKER, '70-fresh-store.conf': FRESH_MARKER, '70-entry-latch.conf': LATCH_MARKER}
SECTION_KEYS = {'condition_path_exists', 'read_write_paths', 'timeout_start_sec'}
START_MARGIN_SECONDS = 60          # polling deadline = the unit's TimeoutStartUSec + this
POLL_SECONDS = 2.0
DEFAULT_START_TIMEOUT = 600.0      # when systemctl reports no parsable TimeoutStartUSec
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

    @staticmethod
    def chown(path, owner, group):
        """Overridable in tests: only root can change ownership."""
        shutil.chown(path, user=owner, group=group)

    def do(self, description, function):
        """Run a filesystem mutation only when applying; always list it in the plan."""
        self.planned.append(description)
        if not self.apply:
            return None
        self.record({'event': 'fs', 'what': description})
        return function()

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


def _sections(unit, sections):
    """Validate the manifest's non-ExecStart settings (full-drop-in content) for one unit."""
    if not isinstance(sections, dict) or set(sections) != SECTION_KEYS:
        raise CutoverError('unit_sections %s must have exactly %s' % (unit, sorted(SECTION_KEYS)))
    cond, rw, timeout = (sections['condition_path_exists'], sections['read_write_paths'],
                         sections['timeout_start_sec'])
    for path in ([] if cond is None else [cond]) + (rw if isinstance(rw, list) else [None]):
        if type(path) is not str or not path.startswith('/') or CONTROL_RE.search(path) or '..' in path.split('/'):
            raise CutoverError('unit_sections %s paths must be absolute strings without control characters' % unit)
    if not rw:
        raise CutoverError('unit_sections %s needs read_write_paths' % unit)
    if timeout is not None and (type(timeout) is not int or not 1 <= timeout <= 3600):
        raise CutoverError('unit_sections %s timeout_start_sec must be an int in 1..3600' % unit)
    return {'condition_path_exists': cond, 'read_write_paths': list(rw), 'timeout_start_sec': timeout}


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
    sections = {}
    if kind == 'fresh_start_manifest_v1':
        units = data.get('units')
        legacy = False
        raw_sections = data.get('unit_sections')
        if not isinstance(units, dict) or not isinstance(raw_sections, dict) or set(units) != set(raw_sections):
            raise CutoverError('manifest units and unit_sections must name the same units')
        sections = {(k if k.endswith('.service') else k + '.service'): v for k, v in raw_sections.items()}
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
            if unit in sections:
                out[unit]['Sections'] = _sections(unit, sections[unit])
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
    sections = (spec or {}).get('Sections')
    if sections and sections['condition_path_exists'] is not None:
        # Reset first: the base unit's ConditionPathExists= would otherwise stay in force (old store path).
        lines += ['[Unit]', 'ConditionPathExists=', 'ConditionPathExists=' + sd_word(sections['condition_path_exists'])]
    lines.append('[Service]')
    lines.append('WorkingDirectory=%s' % release)
    if sections:
        lines.append('Environment=')           # reset: no base-unit environment may point at an old store
    for item in (spec or {}).get('Environment', []):
        lines.append('Environment=' + sd_word(item))
    if sections:
        lines.append('ReadWritePaths=' + ' '.join(sd_word(p) for p in sections['read_write_paths']))
        if sections['timeout_start_sec'] is not None:
            lines.append('TimeoutStartSec=%d' % sections['timeout_start_sec'])
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


def shared_paths(roots, extra):
    base = [root.rstrip('/') + '/' + rel for root in roots for rel in SHARED_RELATIVE] + list(extra)
    return {path + suffix for path in base for suffix in SHARED_SUFFIXES}


def archived_refs(text, roots, shared):
    """Paths under any archived store root that are not an explicitly shared input."""
    found = []
    for root in roots:
        root = root.rstrip('/')
        pattern = re.compile(r'(?<![A-Za-z0-9_.\-/])' + re.escape(root) + r'(?![A-Za-z0-9_.\-])([^\s"\';,]*)')
        for m in pattern.finditer(text):
            token = root + m.group(1)
            if token not in shared:
                found.append(token)
    return found


def check_archived(unit, texts, args, shared):
    if unit in args.allow_archived:
        return
    for text in texts:
        refs = archived_refs(text, args.archived_roots, shared)
        if refs:
            raise CutoverError('Refusing %s: it references an archived store root %s (%s); the old stores must '
                               'never be written by the new experiment (use --allow-archived only for a deliberate '
                               'exception)' % (unit, ', '.join(args.archived_roots), ', '.join(sorted(set(refs))[:3])))


def parse_timespan(value, default=DEFAULT_START_TIMEOUT):
    """systemd timespan ('10min', '1min 30s', '600s', '2.5s', 'infinity') -> seconds; default when unparsable."""
    units = {'us': 1e-6, 'ms': 1e-3, 's': 1.0, 'sec': 1.0, 'min': 60.0, 'h': 3600.0, 'hr': 3600.0, 'd': 86400.0}
    parts = re.findall(r'(\d+(?:\.\d+)?)\s*([a-z]+)', (value or '').strip().lower())
    if not parts or any(unit not in units for _, unit in parts):
        return default
    return sum(float(n) * units[unit] for n, unit in parts)


def start_unit(ops, unit):
    """``systemctl start --no-block`` then poll until the unit leaves ``activating``.

    A blocking ``start`` of a oneshot returns only when the job finishes (the entry pass may legitimately run 600 s),
    which a fixed runner timeout would turn into a false failure while the unit keeps running. The deadline is the
    unit's own TimeoutStartUSec plus a margin, so systemd's timeout fires first and is reported as the failure."""
    ops.mutate(['systemctl', 'start', '--no-block', unit])
    if not ops.apply:
        return
    limit = parse_timespan(show(ops, unit, ['TimeoutStartUSec']).get('TimeoutStartUSec')) + START_MARGIN_SECONDS
    deadline = ops.monotonic() + limit
    while show(ops, unit, ['ActiveState']).get('ActiveState') in PENDING_STATES:
        if ops.monotonic() >= deadline:
            raise CutoverError('%s still activating after %.0f s (TimeoutStartUSec + %d s)'
                               % (unit, limit, START_MARGIN_SECONDS))
        ops.pause(POLL_SECONDS)


def partner_unit(unit):
    return unit[:-len('.service')] + '.timer' if unit.endswith('.service') else unit[:-len('.timer')] + '.service'


def cutover(args, ops):
    check_units(args.units, 'units')
    check_units(args.keep_off, 'keep-off')
    check_units(args.configure_only, 'configure-only')
    check_units(args.allow_archived, 'allow-archived')
    check_units(args.enable, 'enable')
    groups = [set(args.units), set(args.keep_off), set(args.configure_only)]
    if any(a & b for i, a in enumerate(groups) for b in groups[i + 1:]):
        raise CutoverError('A unit appears in more than one of --units/--keep-off/--configure-only')
    if not args.units:
        raise CutoverError('No units to cut over')
    if not (0 <= args.settle_seconds <= 600):
        raise CutoverError('--settle-seconds must be between 0 and 600')
    args.archived_roots = list(args.archived_root or [DEFAULT_ARCHIVED_ROOT])
    for root in args.archived_roots:
        if not os.path.isabs(root) or root.rstrip('/') in ('', '/'):
            raise CutoverError('--archived-root must be an absolute path other than /')
    for extra in args.shared_path:
        if not os.path.isabs(extra):
            raise CutoverError('--shared-path must be absolute')
    for unit in args.units:
        if 'entry-dispatcher' in unit and not args.allow_entry:
            raise CutoverError('Refusing to start %s without --allow-entry (entries stay a separate step)' % unit)
    for unit in args.units + args.enable:
        if 'paper-monitor' in unit and not args.allow_monitor:
            raise CutoverError('Refusing %s without --allow-monitor: the stale-mark watchdog flags every position stale '
                               'between slow held passes and EXIT_ONLY sticks (T09 F5) until T23 lands' % unit)
    for unit in args.enable:
        if 'entry-dispatcher' in unit:
            raise CutoverError('--enable never covers the entry dispatcher: entries are enabled deliberately (RUNBOOK step 9)')
        if unit not in args.units:
            raise CutoverError('--enable %s must also be in --units' % unit)
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
    shared = shared_paths(args.archived_roots, args.shared_path)
    for unit in services:
        spec = store_env.get(unit) or {}
        base_exec, base_env = existing_unit_lines(unit_dir, template_dir, unit)
        exec_text = spec.get('ExecStart') or '\n'.join(base_exec)
        reason = non_loopback_bind(exec_text)
        if reason:
            raise CutoverError('Refusing %s: ExecStart may bind off loopback (%s)' % (unit, reason))
        check_archived(unit, [exec_text] + list(spec.get('Environment', [])) + base_env, args, shared)
        if 'paper-held-cycle' in unit and '--dependency-blocker' in exec_text and not args.allow_held_blocker:
            raise CutoverError('Refusing %s: its ExecStart carries --dependency-blocker, so the held pass exits BLOCKED '
                               'and never monitors or exits a position; re-plan fresh_start with --enable-held '
                               '(or pass --allow-held-blocker)' % unit)
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
              'keep_off': keep_off, 'store_env_ignored': ignored, 'enabled': []}
    ops.record({'event': 'cutover-begin', 'release': str(release), 'digest': digest,
                'units': args.units, 'dropins': report['dropins']})
    started, enabled = [], []
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
            start_unit(ops, unit)
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
        # Boot persistence is the LAST step: only units that started and stayed healthy are enabled.
        for unit in args.enable:
            if ops.apply and is_enabled(ops, unit) in ENABLED_STATES:
                report['enabled'].append({'unit': unit, 'already': True})
                continue
            enabled.append(unit)
            ops.mutate(['systemctl', 'enable', unit])
            if ops.apply:
                if is_enabled(ops, unit) not in ENABLED_STATES:
                    raise CutoverError('%s is not enabled after systemctl enable' % unit)
            report['enabled'].append({'unit': unit, 'already': False})
    except BaseException as exc:
        stopped = []
        for unit in reversed(enabled):             # never leave a failed cutover enabled for the next boot
            try:
                ops.mutate(['systemctl', 'disable', unit])
            except CutoverError:
                pass
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



# ------------------------------------------------- inventory and drop-in archive
ARCHIVE_PREFIX = 'systemd-archive-'
ARCHIVE_MANIFEST = 'manifest.json'
INVENTORY_KIND = 'systemd_inventory_v1'
ARCHIVE_KIND = 'systemd_archive_v1'


def desk_entries(unit_dir):
    """Every ``desk-*`` entry (unit file, drop-in directory, wants directory, symlink) directly under unit_dir."""
    return sorted(p for p in Path(unit_dir).iterdir() if p.name.startswith('desk-'))


def _walk(root):
    """(relative path, absolute path) for ``root`` and everything below it, depth first, no symlink following."""
    yield root, root
    if root.is_dir() and not root.is_symlink():
        for child in sorted(root.iterdir()):
            yield from _walk(child)


def entry_record(unit_dir, path):
    info = path.lstat()
    rel = str(path.relative_to(unit_dir))
    base = {'path': rel, 'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
    if stat.S_ISLNK(info.st_mode):
        return dict(base, type='symlink', target=os.readlink(path))
    if stat.S_ISDIR(info.st_mode):
        return dict(base, type='dir')
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise CutoverError('Refusing to archive a hard-linked file: %s' % path)
        return dict(base, type='file', size=info.st_size, sha256=sha256_file(path))
    raise CutoverError('Refusing to archive a special file: %s' % path)


def tree_records(unit_dir, tops):
    records = []
    for top in tops:
        for _, path in _walk(top):
            records.append(entry_record(unit_dir, path))
    return records


def inventory(args, ops):
    """Read-only listing of every desk unit entry plus ``systemctl cat`` of each unit, saved under --out."""
    unit_dir = canonical_dir(args.unit_dir, '--unit-dir')
    entries = desk_entries(unit_dir)
    records = tree_records(unit_dir, entries)
    listing = []
    for r in records:
        listing.append('%s %o %d:%d %s%s' % (r['type'], r['mode'], r['uid'], r['gid'], r['path'],
                                              (' -> ' + r['target']) if r['type'] == 'symlink' else ''))
    units = sorted(p.name for p in entries if UNIT_RE.fullmatch(p.name))
    cats = {}
    for unit in units:
        result = ops.read(['systemctl', 'cat', unit])
        cats[unit] = (result.stdout or '') if result.returncode == 0 else '# systemctl cat failed: %s\n' % (result.stderr or '').strip()[:200]
    report = {'action': 'inventory', 'entries': len(records), 'units': units, 'applied': ops.apply,
              'out': args.out, 'listing': listing}
    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            raise CutoverError('--out must be an absolute path')

        def write():
            out.mkdir(mode=0o700)
            texts = {'ls-la.txt': '\n'.join(listing) + '\n'}
            texts.update({'systemctl-cat-%s.txt' % u: t for u, t in cats.items()})
            digests = {}
            for name, text in texts.items():
                fd = os.open(out / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
                digests[name] = hashlib.sha256(text.encode()).hexdigest()
            fd = os.open(out / 'inventory.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'kind': INVENTORY_KIND, 'unit_dir': str(unit_dir), 'records': records, 'files': digests},
                          stream, sort_keys=True, indent=1)
                stream.flush()
                os.fsync(stream.fileno())
            ops.fsync_dir(out)
        if out.exists() or out.is_symlink():
            raise CutoverError('--out already exists: %s' % out)
        if not out.parent.is_dir() or out.parent.resolve() != out.parent:
            raise CutoverError('--out parent must be an existing canonical directory')
        ops.do('write inventory (ls -la listing, systemctl cat of %d units) to %s' % (len(units), out), write)
    return report


def _copy_entry(src, dst, record):
    kind = record['type']
    if kind == 'symlink':
        os.symlink(record['target'], dst)
    elif kind == 'dir':
        os.mkdir(dst, 0o700)
    else:
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out, open(src, 'rb') as stream:
            shutil.copyfileobj(stream, out)
            out.flush()
            os.fsync(out.fileno())


def _finish_entry(dst, record):
    """Ownership (best effort: only root can chown) and the exact recorded mode; dirs are finished last."""
    if record['type'] == 'symlink':
        try:
            os.lchown(dst, record['uid'], record['gid'])
        except PermissionError:
            pass
        return
    try:
        os.chown(dst, record['uid'], record['gid'])
    except PermissionError:
        pass
    os.chmod(dst, record['mode'])


def copy_tree_exact(src_root, dst_root, records):
    """Recreate ``records`` (relative paths) from src_root under dst_root; directories get their mode last."""
    for record in records:
        src, dst = src_root / record['path'], dst_root / record['path']
        dst.parent.mkdir(parents=True, exist_ok=True)
        _copy_entry(src, dst, record)
        if record['type'] != 'dir':
            _finish_entry(dst, record)
    for record in reversed([r for r in records if r['type'] == 'dir']):
        _finish_entry(dst_root / record['path'], record)


COMPARABLE = ('type', 'mode', 'uid', 'gid', 'target', 'sha256', 'size')     # owner and group are part of the identity


def matches_record(root, record):
    try:
        now = entry_record(root, root / record['path'])
    except (OSError, CutoverError):
        return False
    return all(now.get(k) == record.get(k) for k in COMPARABLE)


def verify_records(root, records):
    """Raise unless ``root`` holds exactly these records (type, mode, uid, gid, target and sha256 of every file)."""
    for record in records:
        path = root / record['path']
        try:
            now = entry_record(root, path)
        except (OSError, CutoverError) as exc:
            raise CutoverError('Archive verification failed for %s: %s' % (record['path'], exc)) from exc
        if any(now.get(k) != record.get(k) for k in COMPARABLE):
            raise CutoverError('Archive verification failed for %s: content, mode or owner differs' % record['path'])


def remove_tree(path):
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        for child in path.iterdir():
            remove_tree(child)
        path.rmdir()


def resume_archive(ops, unit_dir, archive):
    """Finish an archive step that died while removing the originals (T32G item 7).

    The archive and its manifest are written and verified BEFORE the first original is removed, so an existing archive
    WITH a manifest means the copy is complete: verify it again, prove that every original still present is exactly what the
    manifest says (nothing edited since, nothing extra in a directory about to be removed), then remove what is left.
    An archive without a manifest is a copy that never finished: the originals were not touched, and nothing is guessed.
    """
    manifest_path = archive / ARCHIVE_MANIFEST
    if archive.is_symlink() or not archive.is_dir():
        raise CutoverError('Archive already exists and is not a directory: %s' % archive)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise CutoverError('Archive %s is incomplete (no manifest): the originals were not touched; check them, remove the '
                           'partial archive by hand and run the step again' % archive)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('kind') != ARCHIVE_KIND or manifest.get('version') != 1 or manifest.get('unit_dir') != str(unit_dir):
        raise CutoverError('Archive manifest does not match this unit directory')
    records = manifest['records']
    verify_records(archive, records)                              # the copy is intact
    by_path = {r['path']: r for r in records}
    tops = sorted({r['path'].split('/')[0] for r in records})
    for record in records:
        path = unit_dir / record['path']
        if (path.exists() or path.is_symlink()) and not matches_record(unit_dir, record):
            raise CutoverError('Refusing to resume: %s differs from the archive manifest' % record['path'])
    for top in tops:
        current = unit_dir / top
        if not (current.exists() or current.is_symlink()):
            continue
        extra = [str(p.relative_to(unit_dir)) for _, p in _walk(current) if str(p.relative_to(unit_dir)) not in by_path]
        if extra:
            raise CutoverError('Refusing to resume: %s holds entries the archive manifest does not list: %s'
                               % (top, ', '.join(sorted(extra)[:5])))
        if UNIT_RE.fullmatch(top) and active_now(ops, top):
            raise CutoverError('Unit %s is active; stop it before archiving its configuration' % top)
    remaining = [unit_dir / top for top in tops if (unit_dir / top).exists() or (unit_dir / top).is_symlink()]
    report = {'action': 'archive-dropins', 'archive': str(archive), 'applied': ops.apply, 'resumed': True,
              'directories': sorted(t for t in tops if t.endswith('.d')),
              'unit_files': sorted(t for t in tops if not t.endswith('.d')),
              'files': sum(r['type'] == 'file' for r in records), 'removed_now': sorted(p.name for p in remaining)}

    def finish():
        for path in remaining:
            remove_tree(path)
        ops.fsync_dir(unit_dir)
    ops.do('resume: remove %d originals already proven archived in %s' % (len(remaining), archive), finish)
    ops.mutate(['systemctl', 'daemon-reload'])
    return report


def archive_dropins(args, ops):
    """MOVE every ``desk-*.d`` directory, and the desk unit files the fresh set replaces, into a new archive.

    Nothing is layered on top of the Codex-era drop-in stack: after this step the unit directory holds only
    what the fresh install writes. The archive keeps content, modes, owners and a sha256 manifest; the copy is
    verified against the manifest BEFORE any original is removed, and ``rollback --restore-archive`` verifies
    it again and puts the tree back exactly.
    """
    unit_dir = canonical_dir(args.unit_dir, '--unit-dir')
    archive_root = canonical_dir(args.archive_root, '--archive-root')
    name = args.name or ARCHIVE_PREFIX + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    if not re.fullmatch(r'systemd-archive-[A-Za-z0-9._-]{1,64}', name):
        raise CutoverError('--name must look like systemd-archive-<label>')
    archive = archive_root / name
    if archive.exists() or archive.is_symlink():
        return resume_archive(ops, unit_dir, archive)
    fresh = {}
    if args.fresh_units:
        fresh_dir = canonical_dir(args.fresh_units, '--fresh-units')
        for path in sorted(fresh_dir.iterdir()):
            if UNIT_RE.fullmatch(path.name) and path.is_file():
                fresh[path.name] = sha256_file(path)
        if not fresh:
            raise CutoverError('--fresh-units holds no desk-*.service/.timer files')
    tops = []
    for path in desk_entries(unit_dir):
        if path.name.endswith('.d') and path.is_dir() and not path.is_symlink():
            tops.append(path)
        elif path.name in fresh:
            tops.append(path)
        elif path.is_symlink() and path.name in fresh:
            tops.append(path)
    for top in tops:
        if UNIT_RE.fullmatch(top.name) and active_now(ops, top.name):
            raise CutoverError('Unit %s is active; stop it before archiving its configuration' % top.name)
    records = tree_records(unit_dir, tops)
    manifest = {'kind': ARCHIVE_KIND, 'version': 1, 'unit_dir': str(unit_dir), 'created_utc': time.strftime(
        '%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'records': records, 'fresh_units': fresh}
    report = {'action': 'archive-dropins', 'archive': str(archive), 'applied': ops.apply,
              'directories': sorted(t.name for t in tops if t.name.endswith('.d')),
              'unit_files': sorted(t.name for t in tops if not t.name.endswith('.d')),
              'files': sum(r['type'] == 'file' for r in records)}

    def move():
        archive.mkdir(mode=0o700)
        copy_tree_exact(unit_dir, archive, records)
        fd = os.open(archive / ARCHIVE_MANIFEST, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(manifest, stream, sort_keys=True, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        ops.fsync_dir(archive)
        ops.fsync_dir(archive_root)
        verify_records(archive, records)            # the copy is proven before any original disappears
        for top in tops:
            remove_tree(top)
        ops.fsync_dir(unit_dir)
    ops.do('archive %d entries (%d files) from %s to %s, then remove the originals' % (
        len(tops), report['files'], unit_dir, archive), move)
    ops.mutate(['systemctl', 'daemon-reload'])
    return report


def plan_restore(args, unit_dir, will_remove):
    """Validate a restore WITHOUT touching anything; ``will_remove`` are the managed files rollback deletes first."""
    archive = canonical_dir(args.restore_archive, '--restore-archive')
    manifest_path = archive / ARCHIVE_MANIFEST
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise CutoverError('Archive manifest missing: %s' % manifest_path)
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('kind') != ARCHIVE_KIND or manifest.get('version') != 1 or manifest.get('unit_dir') != str(unit_dir):
        raise CutoverError('Archive manifest does not match this unit directory')
    records, fresh = manifest['records'], manifest['fresh_units']
    verify_records(archive, records)                  # the archive itself is intact before anything is touched
    archived_tops = {r['path'] for r in records if '/' not in r['path']}
    by_path = {r['path']: r for r in records}
    pending = []                                      # (path, kind) removals needed so the archive can go back
    for top in archived_tops:
        current = unit_dir / top
        if not (current.exists() or current.is_symlink()):
            continue                                  # already removed (a complete or half-finished archive step)
        if top.endswith('.d'):
            # An archived original that is still present (the archive step died part-way through removing it) is
            # fine when it is exactly what the manifest records; anything else is somebody's work and is refused.
            leftover = [str(p.relative_to(unit_dir)) for _, p in _walk(current)
                        if p != current and not p.is_dir() and p not in will_remove
                        and not (str(p.relative_to(unit_dir)) in by_path and matches_record(unit_dir, by_path[str(p.relative_to(unit_dir))]))]
            if leftover:
                raise CutoverError('Refusing restore: %s holds files nobody archived or rolled back: %s'
                                   % (top, ', '.join(sorted(leftover)[:5])))
            pending.append(current)
        elif current.is_file() and not current.is_symlink() and sha256_file(current) == fresh.get(top):
            pending.append(current)                   # our rendered unit, replaced by the archived original
        elif matches_record(unit_dir, by_path[top]):
            pending.append(current)                   # the original itself, never removed: removed and put back identically
        else:
            raise CutoverError('Refusing restore: %s is not the rendered unit this flow installed' % top)
    for name, digest in sorted(fresh.items()):
        current = unit_dir / name
        if name in archived_tops or not (current.exists() or current.is_symlink()):
            continue
        if current.is_file() and not current.is_symlink() and sha256_file(current) == digest:
            pending.append(current)                   # a rendered unit that had no original: remove it
        else:
            raise CutoverError('Refusing restore: %s changed since it was installed' % name)
    for path in unit_dir.glob('desk-*.d'):            # drop-in directories only this flow created, now empty
        if path.is_dir() and not path.is_symlink() and path.name not in archived_tops and path not in pending \
                and not any(not p.is_dir() and p not in will_remove for _, p in _walk(path) if p != path):
            pending.append(path)

    return {'archive': archive, 'records': records, 'archived_tops': archived_tops, 'pending': pending}


def run_restore(plan, ops, unit_dir):
    archive, records, pending = plan['archive'], plan['records'], plan['pending']

    def restore():
        for path in pending:
            if path.exists() or path.is_symlink():
                remove_tree(path)
        copy_tree_exact(archive, unit_dir, records)
        ops.fsync_dir(unit_dir)
        verify_records(unit_dir, records)             # byte-for-byte: type, mode and sha256 of every file
    ops.do('restore %d archived entries from %s (removing %d fresh entries first)' % (
        len(plan['archived_tops']), archive, len(pending)), restore)
    return {'archive': str(archive), 'restored': sorted(plan['archived_tops']),
            'removed_fresh': sorted(p.name for p in pending)}


# ------------------------------------------------------------- archived store permissions
def _root_only(info):
    """Already private to root (owner uid 0, no group/other access): sealing must never loosen it."""
    return info.st_uid == ROOT_UID and not info.st_mode & 0o077


def _seal_record(root, path, info, sha=False):
    rel = '.' if path == root else path.relative_to(root).as_posix()
    record = {'path': rel, 'type': 'dir' if stat.S_ISDIR(info.st_mode) else 'file', 'mode': stat.S_IMODE(info.st_mode),
              'uid': info.st_uid, 'gid': info.st_gid}
    if record['type'] == 'file':
        record['size'] = info.st_size
        if sha:
            record['sha256'] = sha256_file(path)
    return record


def seal_archive(args, ops):
    """Make the archived (old) stores read-only to the service user; never touch anything that is live.

    Only an explicit allow-list is changed: the sqlite family (``*.sqlite``, ``-wal``, ``-shm``, ``-journal``), the JSON evidence
    files (``*.json``, ``*.jsonl``) plus any ``--seal-pattern``. Files change to ``root:<service group> 0440``, directories to ``0550``, and the directories that hold
    a SHARED database (the old root for provider-pacing.sqlite, discovery/ for continuous.sqlite) to ``1770`` (a root with no
    ``--shared-path``, e.g. a rotated experiment root, stays ``0550``): the shared
    databases use a rollback journal, so SQLite must create and remove ``<db>-journal`` beside them, group write allows
    that, the sticky bit stops the service user from deleting or renaming any root-owned (archived) file there, and nobody
    outside the group gets any access. Everything that is already private to root (uid 0, no group/other bits) stays as it is.

    Never touched, whatever the patterns say: every ``*.lock`` file (continuous discovery opens ``<db>.discovery.lock``
    read-write; the pacing holders, paper-cycle, invocation, scheduler and dispatcher locks are flock'ed by running
    processes), the shared databases, and every file outside the allow-list (reported as ``unlisted``).

    Before the first change a manifest (path, type, uid, gid, mode, sha256) is written to ``--manifest`` (new file, 0600,
    outside the root); ``unseal`` restores exactly that. Symlinks, hard links and special files abort the step.
    """
    root = Path(args.root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise CutoverError('--root must be a canonical absolute directory')
    shared = []
    for item in args.shared_path:
        path = Path(item)
        if not path.is_absolute() or path.resolve() != path or root not in path.parents:
            raise CutoverError('--shared-path must be a canonical absolute path inside --root: %s' % item)
        shared.append(path)
    patterns = list(SEAL_PATTERNS)
    for pattern in args.seal_pattern:
        if 'lock' in pattern.lower() or not pattern or '/' in pattern:
            raise CutoverError('--seal-pattern must be a plain file-name pattern and must not name lock files: %r' % pattern)
        patterns.append(pattern)
    manifest = Path(args.manifest)
    if not manifest.is_absolute() or manifest.name in ('', '.', '..'):
        raise CutoverError('--manifest must be an absolute file path')
    if manifest == root or root in manifest.parents:
        raise CutoverError('--manifest is inside --root, which is sealed with the stores; put it outside: %s' % manifest)
    if not manifest.parent.is_dir() or manifest.parent.resolve() != manifest.parent:
        raise CutoverError('--manifest parent must be an existing canonical directory')
    if manifest.exists() or manifest.is_symlink():
        raise CutoverError('--manifest already exists: %s' % manifest)
    shared_names = {p for base in shared for p in [base] + [Path(str(base) + sfx) for sfx in SHARED_SUFFIXES[1:]]}
    shared_dirs = {p.parent for p in shared}          # only a directory that holds a shared database; a root without one is plain 0550
    plan = {'files': [], 'sealed_dirs': [], 'shared_dirs': [], 'kept_root_only': [], 'locks': [], 'unlisted': [], 'infos': {}}
    for path in sorted(root.rglob('*')) + [root]:
        info = path.lstat()
        plan['infos'][path] = info
        if stat.S_ISLNK(info.st_mode):
            raise CutoverError('Refusing to seal a symlink: %s' % path)
        if stat.S_ISDIR(info.st_mode):
            if _root_only(info):
                plan['kept_root_only'].append(path)
            else:
                (plan['shared_dirs'] if path in shared_dirs else plan['sealed_dirs']).append(path)
        elif stat.S_ISREG(info.st_mode):
            if path.name.endswith('.lock'):
                plan['locks'].append(path)                    # live: opened read-write by running processes
                continue
            if info.st_nlink != 1:
                raise CutoverError('Refusing to seal a hard-linked file: %s' % path)
            if path in shared_names:
                continue
            if not any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns):
                plan['unlisted'].append(path)
            elif _root_only(info):
                plan['kept_root_only'].append(path)
            else:
                plan['files'].append(path)
        else:
            raise CutoverError('Refusing to seal a special file: %s' % path)
    changed = plan['files'] + plan['sealed_dirs'] + plan['shared_dirs']
    untouched = sorted(plan['locks'] + plan['unlisted'] + [p for p in shared_names if p.exists()])

    def write_manifest():
        text = json.dumps({
            'kind': SEAL_KIND, 'version': 1, 'root': str(root), 'service_group': args.service_group,
            'created_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'sealed': [_seal_record(root, p, plan['infos'][p], sha=True) for p in changed],
            'untouched': sorted(('.' if p == root else p.relative_to(root).as_posix()) for p in untouched),
            'kept_root_only': sorted(p.relative_to(root).as_posix() if p != root else '.' for p in plan['kept_root_only'])},
            sort_keys=True, indent=1)
        fd = os.open(manifest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        ops.fsync_dir(manifest.parent)

    def seal():
        for path in plan['files']:
            ops.chown(path, 'root', args.service_group)
            os.chmod(path, SEAL_FILE_MODE)
        for path in sorted(plan['sealed_dirs'], key=lambda p: -len(p.parts)):
            ops.chown(path, 'root', args.service_group)
            os.chmod(path, SEAL_DIR_MODE)
        for path in plan['shared_dirs']:
            ops.chown(path, 'root', args.service_group)
            os.chmod(path, SEAL_SHARED_DIR_MODE)
    ops.do('write the seal manifest (path, uid, gid, mode, sha256 of %d entries) to %s' % (len(changed), manifest),
           write_manifest)
    ops.do('seal %d files (root:%s 0440), %d directories (0550), %d shared directories (1770) under %s; '
           '%d lock files and %d other files are left exactly as they are'
           % (len(plan['files']), args.service_group, len(plan['sealed_dirs']), len(plan['shared_dirs']), root,
              len(plan['locks']), len(plan['unlisted'])), seal)
    return {'action': 'seal-archive', 'root': str(root), 'applied': ops.apply, 'files': len(plan['files']),
            'sealed_dirs': len(plan['sealed_dirs']), 'shared_dirs': sorted(str(p) for p in plan['shared_dirs']),
            'untouched': sorted(str(p) for p in shared), 'untouched_locks': sorted(str(p) for p in plan['locks']),
            'unlisted': sorted(str(p) for p in plan['unlisted']),
            'kept_root_only': sorted(str(p) for p in plan['kept_root_only']),
            'manifest': str(manifest), 'service_group': args.service_group}


def unseal(args, ops):
    """Restore every entry a ``seal-archive`` manifest recorded: its exact numeric owner, group and mode.

    Refuses (changing nothing) when a recorded entry is missing, has another type, or a file's content no longer matches
    its recorded sha256: a sealed store must not have changed, and restoring write access onto a modified store would hide
    that. Idempotent: running it on an already restored tree re-applies the same values.
    """
    path = Path(args.manifest)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CutoverError('--manifest must be an absolute path to a regular file')
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise CutoverError('Seal manifest exceeds the size bound')
    try:
        data = json.loads(path.read_text())
    except ValueError as exc:
        raise CutoverError('Seal manifest is not valid JSON') from exc
    if type(data) is not dict or data.get('kind') != SEAL_KIND or data.get('version') != 1 or type(data.get('sealed')) is not list:
        raise CutoverError('Not a seal manifest (kind %s)' % SEAL_KIND)
    root = Path(str(data.get('root')))
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise CutoverError('Seal manifest root is not a canonical absolute directory: %s' % root)
    entries = []
    for entry in data['sealed']:
        rel = entry.get('path') if type(entry) is dict else None
        if (type(rel) is not str or rel.startswith('/') or '..' in Path(rel).parts or entry.get('type') not in ('file', 'dir')
                or any(type(entry.get(k)) is not int or entry[k] < 0 for k in ('mode', 'uid', 'gid'))
                or (entry['type'] == 'file' and not re.fullmatch(r'[0-9a-f]{64}', str(entry.get('sha256'))))):
            raise CutoverError('Seal manifest entry is malformed: %r' % (rel,))
        target = root if rel == '.' else root / rel
        entries.append((target, entry))
    problems = []
    for target, entry in entries:
        try:
            info = target.lstat()
        except OSError:
            problems.append('%s is missing' % entry['path'])
            continue
        if stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) != (entry['type'] == 'dir') \
                or (entry['type'] == 'file' and not stat.S_ISREG(info.st_mode)):
            problems.append('%s changed type' % entry['path'])
        elif entry['type'] == 'file' and sha256_file(target) != entry['sha256']:
            problems.append('%s was modified while sealed' % entry['path'])
    if problems:
        raise CutoverError('Refusing to unseal: %s' % '; '.join(problems[:5]))

    def restore():
        for target, entry in sorted((e for e in entries if e[1]['type'] == 'file'), key=lambda e: str(e[0])):
            ops.chown(target, entry['uid'], entry['gid'])
            os.chmod(target, entry['mode'])
        for target, entry in sorted((e for e in entries if e[1]['type'] == 'dir'), key=lambda e: -len(e[0].parts)):
            ops.chown(target, entry['uid'], entry['gid'])
            os.chmod(target, entry['mode'])
    ops.do('unseal %d entries under %s from %s' % (len(entries), root, path), restore)
    return {'action': 'unseal', 'root': str(root), 'applied': ops.apply, 'entries': len(entries), 'manifest': str(path)}


# ------------------------------------------------------------------ rollback

def rollback(args, ops):
    check_units(args.units, 'units')
    unit_dir = canonical_dir(args.unit_dir, '--unit-dir')
    removed, skipped, managed = [], [], []
    candidates = []
    for unit in args.units:
        found = False
        for name, marker in MANAGED_DROPINS.items():
            path = unit_dir / (unit + '.d') / name
            if path.is_symlink() or not path.is_file():
                continue
            found = True
            current = path.read_text(errors='replace')
            lines = current.splitlines()
            if not lines or lines[0] != marker:           # a marker quoted later proves nothing
                skipped.append({'unit': unit, 'reason': 'not written by cutover', 'file': name})
                continue
            if unit not in managed:
                managed.append(unit)
            candidates.append((unit, path, current))
        if not found:
            skipped.append({'unit': unit, 'reason': 'no drop-in'})
    # Removing a drop-in under a running unit (or a timer that would fire the old release at the archived
    # stores) is never silent: related units must be idle, or --stop must stop them first.
    restore_plan = plan_restore(args, unit_dir, {path for _, path, _ in candidates}) if args.restore_archive else None
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
    disabled = []
    if args.disable:
        for unit in related:
            if ops.apply and is_enabled(ops, unit) not in ENABLED_STATES:
                continue
            ops.mutate(['systemctl', 'disable', unit])
            disabled.append(unit)
    restored = run_restore(restore_plan, ops, unit_dir) if restore_plan else None
    ops.mutate(['systemctl', 'daemon-reload'])
    return {'action': 'rollback', 'removed': removed, 'skipped': skipped, 'stopped': stopped, 'disabled': disabled,
            'applied': ops.apply, 'restored_archive': restored,
            'note': 'Units are not restarted; restart deliberately after verifying the old release. Rendered unit files '
                    'stay installed after a plain rollback; use --restore-archive to put the archived originals back '
                    '(it removes the rendered units it installed).'}


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
    c.add_argument('--archived-root', action='append', default=None,
                   help='old store root (repeatable; default %s); no unit may reference it except the shared '
                        'inputs. Pass every archived root when rotating a fresh store set' % DEFAULT_ARCHIVED_ROOT)
    c.add_argument('--allow-archived', nargs='*', default=[], help='units explicitly allowed to reference the archived root')
    c.add_argument('--shared-path', action='append', default=[],
                   help='extra absolute path under the archived root that is a shared input (repeatable); '
                        'provider-pacing.sqlite and discovery/continuous.sqlite under each archived root are always allowed')
    c.add_argument('--settle-seconds', type=float, default=10.0, help='wait before the second health check')
    c.add_argument('--unit-dir', default='/etc/systemd/system')
    c.add_argument('--template-dir', default=None, help='fallback dir for base unit files when reading ExecStart')
    c.add_argument('--expect-digest')
    c.add_argument('--allow-entry', action='store_true')
    c.add_argument('--allow-monitor', action='store_true',
                   help='permit starting/enabling desk-paper-monitor (stale-mark watchdog); only after T23 lands')
    c.add_argument('--allow-held-blocker', action='store_true',
                   help='permit a held-cycle ExecStart that still carries --dependency-blocker (it never monitors)')
    c.add_argument('--enable', nargs='*', default=[],
                   help='units from --units to `systemctl enable` (boot persistence) AFTER they start and settle healthy; '
                        'never the entry dispatcher')
    c.add_argument('--replace-existing', action='store_true')
    q = sub.add_parser('seal-archive', help='archived store root read-only to the service user (shared inputs excepted)')
    q.add_argument('--root', required=True)
    q.add_argument('--shared-path', action='append', default=[], help='shared database inside --root left untouched (repeatable)')
    q.add_argument('--service-group', default='solana-desk')
    q.add_argument('--manifest', required=True, help='NEW file (0600, outside --root) listing path/uid/gid/mode/sha256 of every entry changed')
    q.add_argument('--seal-pattern', action='append', default=[],
                   help='extra file-name pattern to seal beyond the sqlite family (repeatable); lock files can never be named')
    u = sub.add_parser('unseal', help='restore the exact owner/group/mode a seal-archive manifest recorded')
    u.add_argument('--manifest', required=True)
    i = sub.add_parser('inventory', help='save ls -la and systemctl cat of every desk unit before changing anything')
    i.add_argument('--unit-dir', default='/etc/systemd/system')
    i.add_argument('--out', help='new directory (0700) for the saved inventory; omit to print only')
    a = sub.add_parser('archive-dropins', help='MOVE desk-*.d directories and replaced unit files into a verified archive')
    a.add_argument('--unit-dir', default='/etc/systemd/system')
    a.add_argument('--archive-root', default='/var/backups/solana-desk')
    a.add_argument('--name', help='archive directory name (default systemd-archive-<UTC>)')
    a.add_argument('--fresh-units', help='directory of rendered fresh unit files: same-named installed units are archived too')
    r = sub.add_parser('rollback')
    r.add_argument('--units', nargs='+', required=True)
    r.add_argument('--unit-dir', default='/etc/systemd/system')
    r.add_argument('--stop', action='store_true', help='stop active managed units (and their timers) first')
    r.add_argument('--restore-archive', help='an archive-dropins directory: restore it exactly after the drop-ins are removed')
    r.add_argument('--disable', action='store_true',
                   help='also `systemctl disable` the units and their timer/service partners (undo --enable)')
    return p


def main(argv=None, runner=None, sleep=None):
    args = build_parser().parse_args(argv)
    ops = Ops(apply=args.apply, runner=runner, journal=Path(args.journal), sleep=sleep)
    try:
        report = {'stage': stage, 'cutover': cutover, 'rollback': rollback, 'inventory': inventory,
                  'archive-dropins': archive_dropins, 'seal-archive': seal_archive,
                  'unseal': unseal}[args.command](args, ops)
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
